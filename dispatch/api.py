import os
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Literal
from zipfile import BadZipFile

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi import Request as HttpRequest
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from dispatch.address_suggestions import (
    AddressSuggestion,
    AddressSuggestions,
    SuggestionError,
    SuggestionResolve,
    Suggestions,
    SuggestionSearch,
)
from dispatch.baseline import solve_baseline
from dispatch.checker import require_valid_plan
from dispatch.engineer_uploads import validate_engineers_csv
from dispatch.events import DayEvent, replan
from dispatch.exports import build_workbook
from dispatch.geocode import GeocodingError
from dispatch.geography import (
    CoordinateEdit,
    GeographyStore,
    attach_geography,
    location_address,
    refresh_snapshot,
    scenario_addresses,
)
from dispatch.importer import import_sources, location_id, read_norms, read_policy
from dispatch.models import Model, Plan, Scenario
from dispatch.optimizer import SearchOptions, SolverError, optimize
from dispatch.progress import CalculationProgress
from dispatch.publication import (
    PublishRequest,
    activate_plan,
    comparison_scenario,
    publish,
    staffing,
    start_day,
)
from dispatch.routing.contracts import RoutingError
from dispatch.routing.service import RoutingService
from dispatch.scenarios import make_scenario
from dispatch.store import PlanStore, SessionNotFound, VersionConflict
from dispatch.upload_geocoding import UploadGeocoder
from dispatch.uploads import (
    MAX_BYTES,
    EngineerInput,
    UploadStore,
    default_engineers,
    uploaded_geography,
    uploaded_scenario,
    validate_csv,
)

ROOT = Path(__file__).resolve().parents[1]


class RunOptions(Model):
    engineer_count: int = Field(default=12, ge=0, le=30, strict=True)


class OptimizeOptions(RunOptions):
    search: SearchOptions = Field(default_factory=SearchOptions)


class UploadRunOptions(Model):
    engineers: list[EngineerInput] = Field(max_length=30)
    search: SearchOptions = Field(default_factory=SearchOptions)
    excluded_request_ids: list[str] = Field(default_factory=list, max_length=100)


class CommitPreview(Model):
    preview_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class RefreshGeography(Model):
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    expected_version: int = Field(ge=1)


def scenario_summary(scenario: Scenario) -> dict:
    return {
        "id": scenario.id,
        "upload_id": scenario.manifest.get("upload_id"),
        "objective_policy": scenario.objective_policy,
        "schedule_policy": scenario.schedule_policy,
        "equipment_policy": scenario.equipment_policy,
        "equipment_issued": scenario.equipment_issued,
        "date": scenario.date,
        "office_address": scenario.office_address,
        "office_location_id": location_id(scenario.office_address),
        "requests": [
            {
                **r.model_dump(),
                **(
                    {"display_address": location_address(scenario.geography[r.location_id])}
                    if r.location_id in scenario.geography
                    and scenario.geography[r.location_id].point
                    else {}
                ),
            }
            for r in scenario.requests
        ],
        "import_report": scenario.manifest.get("import", {}),
        "norms": scenario.manifest.get("norms", {}),
        "policy": scenario.manifest.get("policy", {}),
        "geography": scenario.geography,
    }


def with_scenario(scenario: Scenario, response: dict) -> dict:
    return {
        **response,
        "scenario": scenario_summary(scenario),
        "engineers": scenario.engineers,
        "manifest": scenario.manifest,
        "staffing": staffing(scenario, Plan.model_validate(response["plan"])),
        "unavailable_engineers": scenario.planning_state.unavailable_engineers
        if scenario.planning_state
        else [],
        "validation": {"valid": True, "errors": []},
    }


def create_app(root: Path = ROOT, *, storage_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="Планирование выездов", version="0.7.0")
    store = PlanStore(
        storage_path
        or Path(os.environ.get("DISPATCH_STORAGE_PATH", root / "artifacts/dispatch.sqlite"))
    )
    geography = GeographyStore(store)
    suggestions = AddressSuggestions(geography, root / ".env.local")
    app.state.address_suggestions = suggestions
    routing = RoutingService(root)
    app.state.routing = routing
    uploads = UploadStore(store)
    upload_geocoder = UploadGeocoder(geography)
    app.state.upload_geocoder = upload_geocoder
    progress = CalculationProgress()
    app.state.calculation_progress = progress

    def response_with_routes(scenario, response):
        result = with_scenario(scenario, response)
        if scenario.manifest.get("transport") == "routing_static_v1":
            result["journeys"] = {
                key: routing.journeys(
                    comparison_scenario(scenario, response) if key == "baseline" else scenario,
                    Plan.model_validate(response[key]),
                )
                for key in ("plan", "baseline")
                if key in response
            }
        return result

    def run_replanning(scenario, plan, event, report=None):
        # Old drafts start on their first event; previews leave the stored version untouched.
        scenario, plan = activate_plan(scenario, plan)
        with routing.calculation(report) as calculation:
            started = perf_counter()
            updated, response = replan(
                scenario, plan, event, transport_preparation=calculation.refresh
            )
            if updated.manifest.get("transport") == "routing_static_v1":
                preparation_ms = sum(
                    calculation.timings[key]
                    for key in ("network_load_ms", "routing_ms", "archive_write_ms")
                )
                calculation.timings["solver_ms"] = max(
                    0, (perf_counter() - started) * 1000 - preparation_ms
                )
                response["performance"] = calculation.report()
            return updated, response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, exc):
        messages = []
        for error in exc.errors():
            location = list(error["loc"])
            prefix = ""
            if "engineers" in location:
                i = location.index("engineers")
                if len(location) > i + 1 and isinstance(location[i + 1], int):
                    prefix = f"Инженер {location[i + 1] + 1}: "
            field = str(location[-1])
            if "engineers" in location and field in {"start_address", "start_lat", "start_lon"}:
                messages.append(
                    prefix + "Все инженеры начинают из офиса. Уберите отдельную стартовую точку."
                )
                continue
            names = {
                "skills": "навыки (от 1 до 3)",
                "profile": "транспорт",
                "shift_start_s": "начало смены",
                "shift_end_s": "конец смены",
                "start_lat": "широта",
                "start_lon": "долгота",
                "engineers": "список инженеров (не более 30)",
                "start_address": "стартовый адрес",
            }
            message = (
                error["msg"].removeprefix("Value error, ")
                if error["type"] == "value_error"
                else f"проверьте поле «{names.get(field, field)}» — значение отсутствует или недопустимо."
            )
            messages.append(prefix + message)
        return JSONResponse({"detail": " ".join(messages)}, status_code=422)

    @app.get("/api/calculations/{calculation_id}")
    def calculation_status(calculation_id: str):
        return progress.get(calculation_id)

    @lru_cache(maxsize=1)
    def upload_reference():
        try:
            return read_policy(root / "config/scenario.json"), read_norms(root / "Нормативы.xlsx")
        except OSError as exc:
            raise HTTPException(503, "Не найдены настройки и нормативы заявок на сервере.") from exc

    def upload_draft(key, item):
        policy, _ = upload_reference()
        engineers = default_engineers(item["id"], policy, 30)
        draft = uploaded_scenario(
            item, key, engineers[: policy["engineer_count"]], policy, geography
        )
        return {
            "scenario": scenario_summary(draft),
            "engineers": engineers,
            "engineer_count": policy["engineer_count"],
        }

    @app.post("/api/uploads/validate")
    async def validate_upload(
        request: HttpRequest, filename: str = Query(min_length=1, max_length=250)
    ):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                return {
                    "valid": False,
                    "errors": [
                        {
                            "line": None,
                            "column": "",
                            "message": "Файл больше 2 МБ. Разделите его на отдельные рабочие дни.",
                        }
                    ],
                }
        policy, norms = upload_reference()
        item, errors = validate_csv(bytes(data), filename, norms, policy)
        if errors:
            return {"valid": False, "errors": errors}
        item["geography_revisions"] = {
            key: record.revision
            for key, record in geography.collect(
                [item["office_address"], *[r.address for r in item["requests"]]]
            ).items()
        }
        key = uploads.create(item)
        return {"valid": True, "errors": [], "upload_id": key, **upload_draft(key, item)}

    @app.get("/api/uploads/{upload_id}")
    def read_upload(upload_id: str):
        return {"upload_id": upload_id, **upload_draft(upload_id, uploads.get(upload_id))}

    @app.post("/api/engineers/validate")
    async def validate_engineers_upload(
        request: HttpRequest, filename: str = Query(min_length=1, max_length=250)
    ):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                return {
                    "valid": False,
                    "errors": [
                        {
                            "line": None,
                            "column": "",
                            "message": "Файл больше 2 МБ. Оставьте не более 30 инженеров.",
                        }
                    ],
                }
        item, errors = validate_engineers_csv(bytes(data), filename)
        if errors:
            return {"valid": False, "errors": errors}
        return {"valid": True, "errors": [], **item}

    @app.get("/api/uploads/{upload_id}/geography")
    def upload_geography(upload_id: str):
        return uploaded_geography(uploads.get(upload_id), geography)

    @app.get("/api/samples")
    def sample_files():
        return [
            {"name": p.name, "url": f"/api/samples/{p.name}"}
            for p in sorted((root / "data/samples").glob("*.csv"))
        ]

    @app.get("/api/samples/{filename}")
    def sample_file(filename: str):
        files = {p.name: p for p in (root / "data/samples").glob("*.csv")}
        if filename not in files:
            raise HTTPException(404, "Тестовый файл не найден")
        return FileResponse(
            files[filename], filename=filename, media_type="text/csv; charset=utf-8"
        )

    def run_upload(upload_id, options, optimize_plan, calculation_id):
        with progress.track(calculation_id) as report:
            item = uploads.get(upload_id)
            policy, _ = upload_reference()
            scenario = uploaded_scenario(
                item,
                upload_id,
                options.engineers,
                policy,
                geography,
                synthetic=routing.mode == "synthetic",
                excluded_request_ids=options.excluded_request_ids,
            )
            if routing.mode != "synthetic":
                upload_geocoder.prepare(scenario, report)
                missing = [key for key, record in scenario.geography.items() if not record.point]
                if missing:
                    progress.update(calculation_id, "Уточните адреса", 10, "needs_input")
                    return JSONResponse(
                        status_code=409,
                        content=jsonable_encoder(
                            {
                                "code": "address_review_required",
                                "detail": "Уточните адреса или исключите заявки перед расчётом.",
                                "review": {
                                    "upload_id": upload_id,
                                    "office_location_id": location_id(item["office_address"]),
                                    "geography": {
                                        **uploaded_geography(item, geography),
                                        **scenario.geography,
                                    },
                                    "requests": item["requests"],
                                    "excluded_request_ids": options.excluded_request_ids,
                                },
                            }
                        ),
                    )
            with routing.calculation(report) as calculation:
                scenario = calculation.new_day(scenario)
                progress.update(calculation_id, "Распределение заявок", 96)
                started = perf_counter()
                result = (
                    optimize(scenario, options.search)
                    if optimize_plan
                    else {"plan": require_valid_plan(scenario, solve_baseline(scenario))}
                )
                scenario, result = start_day(scenario, result)
                calculation.timings["solver_ms"] = (perf_counter() - started) * 1000
                if scenario.manifest.get("transport") == "routing_static_v1":
                    result["performance"] = calculation.report()
            progress.update(calculation_id, "Сохранение плана", 98)
            return store.create(scenario, response_with_routes(scenario, result))

    @app.post("/api/uploads/{upload_id}/optimize")
    def optimize_upload(
        upload_id: str,
        options: UploadRunOptions,
        calculation_id: str | None = Header(
            default=None, alias="X-Calculation-ID", pattern=r"^[a-zA-Z0-9_-]{1,64}$"
        ),
    ):
        return run_upload(upload_id, options, True, calculation_id)

    @app.post("/api/uploads/{upload_id}/baseline")
    def baseline_upload(
        upload_id: str,
        options: UploadRunOptions,
        calculation_id: str | None = Header(
            default=None, alias="X-Calculation-ID", pattern=r"^[a-zA-Z0-9_-]{1,64}$"
        ),
    ):
        return run_upload(upload_id, options, False, calculation_id)

    @app.exception_handler(RoutingError)
    @app.exception_handler(GeocodingError)
    async def routing_error(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.get("/api/routing/status")
    def routing_status():
        return routing.status()

    @app.exception_handler(SuggestionError)
    async def suggestion_error(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.post("/api/address-suggestions", response_model=Suggestions)
    def search_addresses(request: SuggestionSearch):
        return suggestions.search(request)

    @app.post("/api/address-suggestions/resolve", response_model=AddressSuggestion)
    def resolve_address(request: SuggestionResolve):
        return suggestions.resolve(request.suggestion_id)

    @app.exception_handler(SessionNotFound)
    async def missing_handler(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(VersionConflict)
    async def conflict_handler(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(SolverError)
    async def solver_handler(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(ValueError)
    async def value_handler(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @lru_cache(maxsize=1)
    def sources():
        policy = read_policy(root / "config/scenario.json")
        imported = import_sources(root / "Обезличивание.zip", root / "Нормативы.xlsx", policy)
        return policy, {item["id"]: item for item in imported}

    def get_source(scenario_id):
        try:
            policy, imported = sources()
        except (OSError, ValueError, BadZipFile) as exc:
            raise HTTPException(422, f"Ошибка входных файлов: {exc}") from exc
        if scenario_id not in imported:
            raise HTTPException(404, "Зона не найдена")
        return policy, imported[scenario_id]

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/scenarios")
    def list_scenarios():
        # Use the same loader/error handling as individual scenarios.
        get_source("Восток")
        policy, imported = sources()
        return [
            {
                "id": item["id"],
                "date": item["date"],
                "requests": len(item["requests"]),
                "rejected": item["report"]["rejected"],
                "office_address": item["office_address"],
                "default_engineer_count": policy["engineer_count"],
            }
            for item in imported.values()
        ]

    @app.get("/api/map-config")
    def map_config():
        return {
            "style_url": os.environ.get(
                "DISPATCH_MAP_STYLE_URL",
                ""
                if "DISPATCH_TILE_URL" in os.environ
                else "https://tiles.openfreemap.org/styles/bright",
            ),
            "tile_url": os.environ.get(
                "DISPATCH_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
            ),
            "attribution": os.environ.get(
                "DISPATCH_TILE_ATTRIBUTION", "© OpenStreetMap contributors"
            ),
            "attribution_url": os.environ.get(
                "DISPATCH_TILE_ATTRIBUTION_URL", "https://www.openstreetmap.org/copyright"
            ),
        }

    @app.get("/api/scenarios/{scenario_id}/geography")
    def scenario_geography(scenario_id: str):
        _, item = get_source(scenario_id)
        return geography.collect([item["office_address"], *[r.address for r in item["requests"]]])

    @app.put("/api/geography/{key}")
    def edit_coordinates(key: str, request: CoordinateEdit):
        return geography.edit(key, request)

    @app.get("/api/sessions/{session_id}/geography")
    def session_geography(session_id: str, version: int | None = Query(default=None, ge=1)):
        scenario, _ = store.get(session_id, version)
        # A new request can carry its explicitly selected coordinates in the snapshot.
        catalog = geography.collect(scenario_addresses(scenario))
        return {
            key: record
            if record.revision > scenario.geography.get(key, record).revision
            else scenario.geography.get(key, record)
            for key, record in catalog.items()
        }

    @app.post("/api/sessions/{session_id}/geography/refresh")
    def refresh_coordinates(session_id: str, request: RefreshGeography):
        operation_id = "geography:" + request.operation_id
        payload_hash = request.model_dump_json()
        existing = store.find_event(session_id, operation_id, payload_hash)
        if existing is not None:
            return existing
        scenario, previous = store.get(session_id)
        if previous["version"] != request.expected_version:
            raise VersionConflict("План уже изменился. Откройте текущую версию.")
        updated, response = refresh_snapshot(scenario, previous, geography)
        return store.commit_event(
            session_id,
            request.expected_version,
            operation_id,
            payload_hash,
            updated,
            response_with_routes(updated, response),
        )

    @app.get("/api/scenarios/{scenario_id}")
    def get_scenario(scenario_id: str):
        policy, item = get_source(scenario_id)
        return {
            "id": item["id"],
            "date": item["date"],
            "office_address": item["office_address"],
            "office_location_id": location_id(item["office_address"]),
            "requests": [r.model_dump() for r in item["requests"]],
            "import_report": item["report"],
            "norms": item["norms"],
            "policy": policy,
            "geography": geography.collect(
                [item["office_address"], *[r.address for r in item["requests"]]]
            ),
        }

    @app.post("/api/scenarios/{scenario_id}/baseline")
    def run_baseline(scenario_id: str, options: RunOptions):
        policy, item = get_source(scenario_id)
        try:
            scenario = attach_geography(
                make_scenario(item, policy, options.engineer_count), geography
            )
            with routing.calculation() as calculation:
                scenario = calculation.new_day(scenario)
                started = perf_counter()
                plan = require_valid_plan(scenario, solve_baseline(scenario))
                scenario, result = start_day(scenario, {"plan": plan})
                calculation.timings["solver_ms"] = (perf_counter() - started) * 1000
                performance = (
                    calculation.report()
                    if scenario.manifest.get("transport") == "routing_static_v1"
                    else None
                )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return store.create(
            scenario,
            response_with_routes(
                scenario,
                {
                    **result,
                    "engineers": scenario.engineers,
                    "manifest": scenario.manifest,
                    "validation": {"valid": True, "errors": []},
                    **({"performance": performance} if performance else {}),
                },
            ),
        )

    @app.post("/api/scenarios/{scenario_id}/optimize")
    def run_optimization(scenario_id: str, options: OptimizeOptions):
        policy, item = get_source(scenario_id)
        try:
            scenario = attach_geography(
                make_scenario(item, policy, options.engineer_count), geography
            )
            with routing.calculation() as calculation:
                scenario = calculation.new_day(scenario)
                started = perf_counter()
                result = optimize(scenario, options.search)
                scenario, result = start_day(scenario, result)
                calculation.timings["solver_ms"] = (perf_counter() - started) * 1000
                if scenario.manifest.get("transport") == "routing_static_v1":
                    result["performance"] = calculation.report()
        except SolverError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return store.create(
            scenario,
            response_with_routes(
                scenario,
                {
                    **result,
                    "engineers": scenario.engineers,
                    "manifest": scenario.manifest,
                    "validation": {"valid": True, "errors": []},
                },
            ),
        )

    @app.get("/api/sessions/{session_id}")
    def read_session(session_id: str, version: int | None = Query(default=None, ge=1)):
        try:
            return store.get(session_id, version)[1]
        except SessionNotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/sessions/{session_id}/snapshot")
    def read_snapshot(session_id: str, version: int | None = Query(default=None, ge=1)):
        try:
            scenario, response = store.get(session_id, version)
        except SessionNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        return JSONResponse(
            {
                **response,
                "scenario": scenario.model_dump(),
                **(
                    {"comparison_scenario": comparison_scenario(scenario, response).model_dump()}
                    if response.get("comparison", {}).get("scope") == "morning"
                    else {}
                ),
            },
            headers={
                "Content-Disposition": f'attachment; filename="dispatch-v{response["version"]}.json"'
            },
        )

    @app.get("/api/sessions/{session_id}/export")
    def export_plan(
        session_id: str,
        version: int = Query(ge=1),
        format: Literal["excel", "waybills"] = "excel",
        scope: Literal["all", "visible"] = "all",
        engineer_id: list[str] | None = Query(default=None),
    ):
        try:
            scenario, response = store.get(session_id, version)
            if scope == "all" and engineer_id is not None:
                raise ValueError("Для выбора инженеров укажите scope=visible.")
            content = build_workbook(
                scenario, response, format, (engineer_id or []) if scope == "visible" else None
            )
        except SessionNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        name = "plan" if format == "excel" else "route-sheets"
        return Response(
            content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="{name}-{scenario.date}.xlsx"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/sessions/{session_id}/events")
    def submit_event(session_id: str, event: DayEvent):
        try:
            existing = store.find_event(session_id, event.event_id, event.fingerprint())
            if existing is not None:
                return existing
            if event.mode == "flexible":
                raise VersionConflict(
                    "Вариант с перестановками нужно сначала просмотреть и принять."
                )
            scenario, previous = store.get(session_id)
            if previous["version"] != event.expected_version:
                raise VersionConflict(
                    "План уже изменился. Откройте текущую версию и повторите событие."
                )
            updated, response = run_replanning(
                scenario,
                Plan.model_validate(previous["plan"]),
                event,
            )
            return store.commit_event(
                session_id,
                event.expected_version,
                event.event_id,
                event.fingerprint(),
                updated,
                response_with_routes(updated, response),
            )
        except SessionNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except VersionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except SolverError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/sessions/{session_id}/publish")
    def publish_day(session_id: str, request: PublishRequest):
        operation = "publish:" + request.operation_id
        existing = store.find_event(session_id, operation, request.fingerprint())
        if existing is not None:
            return existing
        scenario, previous = store.get(session_id)
        if previous["version"] != request.expected_version:
            raise VersionConflict("План уже изменился. Откройте текущую версию.")
        updated, response = publish(scenario, Plan.model_validate(previous["plan"]), request)
        return store.commit_event(
            session_id,
            request.expected_version,
            operation,
            request.fingerprint(),
            updated,
            response_with_routes(updated, response),
        )

    @app.post("/api/sessions/{session_id}/events/preview")
    def preview_event(
        session_id: str,
        event: DayEvent,
        calculation_id: str | None = Header(
            default=None, alias="X-Calculation-ID", pattern=r"^[a-zA-Z0-9_-]{1,64}$"
        ),
    ):
        existing = store.find_preview(session_id, event.event_id, event.fingerprint())
        if existing is not None:
            return existing
        scenario, previous = store.get(session_id)
        if previous["version"] != event.expected_version:
            raise VersionConflict("План уже изменился. Откройте текущую версию.")
        if store.find_event(session_id, event.event_id, event.fingerprint()) is not None:
            raise VersionConflict("Это событие уже принято. Откройте текущую версию.")
        with progress.track(calculation_id) as report:
            updated, response = run_replanning(
                scenario, Plan.model_validate(previous["plan"]), event, report
            )
            progress.update(calculation_id, "Сохранение варианта", 98)
            return store.save_preview(
                session_id,
                event.expected_version,
                event.event_id,
                event.fingerprint(),
                updated,
                response_with_routes(updated, response),
            )

    @app.post("/api/sessions/{session_id}/events/commit")
    def commit_event_preview(session_id: str, request: CommitPreview):
        return store.commit_preview(session_id, request.preview_id)

    build = root / "frontend/dist"
    if build.exists():
        app.mount("/", StaticFiles(directory=build, html=True), name="frontend")
    return app


app = create_app()
