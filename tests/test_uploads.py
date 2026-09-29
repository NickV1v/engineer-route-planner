import csv
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from dispatch.api import create_app
from dispatch.checker import require_valid_plan
from dispatch.geocode import GeocodingError
from dispatch.geography import CoordinateEdit, GeographyStore, attach_geography
from dispatch.importer import location_id, read_norms
from dispatch.models import GeoPoint, Plan
from dispatch.progress import CalculationProgress
from dispatch.routing.contracts import RoutingError
from dispatch.store import PlanStore, VersionConflict
from dispatch.uploads import COLUMNS, MAX_BYTES, UploadStore, uploaded_scenario, validate_csv

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def norms():
    return read_norms(ROOT / "Нормативы.xlsx")


def csv_file(rows=None, *, office=True, delimiter=";", encoding="utf-8-sig"):
    default = {
        "Заявка": "one",
        "Тип заявки BK": "Подключение",
        "Тип заявки HD": "Подключение",
        "Начало": "17.08.2026 10:00",
        "Окончание": "17.08.2026 18:00",
        "Район": "Произвольный район",
        "Адрес": "Тестовый дом 1",
        "Широта": "55.71",
        "Долгота": "37.61",
    }
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter=delimiter)
    writer.writerow(COLUMNS)
    for changes in rows if rows is not None else [{}]:
        row = {**default, **changes}
        writer.writerow([row.get(k, "") for k in COLUMNS])
    if office:
        writer.writerow(["Адрес офиса", "Тестовый офис", "55.7", "37.6"])
    return buf.getvalue().encode(encoding)


@pytest.mark.parametrize("filename", ["Восток.csv", "Юго-восток.csv", "Югоцентр.csv"])
def test_prepared_samples_are_complete_and_valid(filename, norms, policy):
    data = (ROOT / "data/samples" / filename).read_bytes()
    item, errors = validate_csv(data, filename, norms, policy)
    assert not errors and item
    assert (
        len(item["requests"])
        == {"Восток.csv": 66, "Юго-восток.csv": 83, "Югоцентр.csv": 56}[filename]
    )
    assert item["report"]["rejected"] == 0
    coordinate_rows = [r for r in item["requests"] if not r.raw["Адрес"]]
    assert (
        len(coordinate_rows)
        == {"Восток.csv": 13, "Юго-восток.csv": 16, "Югоцентр.csv": 11}[filename]
    )
    assert len(coordinate_rows) < len(item["requests"]) / 2
    assert all(r.raw["Широта"] and r.raw["Долгота"] for r in coordinate_rows)
    assert all(
        not r.raw["Широта"] and not r.raw["Долгота"] for r in item["requests"] if r.raw["Адрес"]
    )


def test_yugocentr_office_uses_corrected_catalog_not_stale_csv(application, norms, policy):
    _, client, store = application
    catalog = GeographyStore(store)
    reference = json.loads((ROOT / "tests/fixtures/yugocentr_office_osm.json").read_text())
    address = reference["input_address"]
    key = location_id(address)
    catalog.edit(
        key, CoordinateEdit(address=address, expected_revision=0, lat=55.703034, lon=37.530584)
    )
    loaded = upload(client, (ROOT / "data/samples/Югоцентр.csv").read_bytes(), name="Югоцентр.csv")
    # The same existing upload must use a later catalogue correction on a new run.
    catalog.edit(
        key, CoordinateEdit(address=address, expected_revision=1, lat=55.6647568, lon=37.6158385)
    )
    scenario = uploaded_scenario(
        UploadStore(store).get(loaded["upload_id"]), loaded["upload_id"], [], policy, catalog
    )
    assert scenario.office_address == address
    point = scenario.geography[key].point
    bounds = reference["bounds"]
    assert bounds["south"] <= point.lat <= bounds["north"]
    assert bounds["west"] <= point.lon <= bounds["east"]


@pytest.mark.parametrize(
    "delimiter,encoding", [(";", "utf-8-sig"), (",", "utf-8"), ("\t", "cp1251")]
)
def test_arbitrary_filename_encoding_and_delimiter(delimiter, encoding, norms, policy):
    item, errors = validate_csv(
        csv_file(delimiter=delimiter, encoding=encoding), "Новый участок.csv", norms, policy
    )
    assert not errors
    assert item["id"] == "Новый участок"
    assert item["requests"][0].service_s == 4200
    assert item["requests"][0].skill == "installation"
    assert item["requests"][0].id == "Новый участок:one"


@pytest.mark.parametrize(
    "changes,column",
    [
        ({"Заявка": ""}, "Заявка"),
        ({"Тип заявки BK": "неизвестный"}, "Тип заявки BK"),
        ({"Начало": "09:00"}, "Начало"),
        ({"Окончание": "17.08.2026 09:00"}, "Начало / Окончание"),
        ({"Окончание": "18.08.2026 18:00"}, "Начало / Окончание"),
        ({"Длительность, мин": "0"}, "Длительность, мин"),
        ({"Длительность, мин": "1.5"}, "Длительность, мин"),
        ({"Длительность, мин": "9" * 5000}, "Длительность, мин"),
        ({"Навык": "летать"}, "Навык"),
        ({"Требуемый транспорт": "самолёт"}, "Требуемый транспорт"),
        ({"Приоритет": "важная"}, "Приоритет"),
        ({"Долгота": ""}, "Широта / Долгота"),
        ({"Широта": "NaN"}, "Широта / Долгота"),
        ({"Широта": "90"}, "Широта / Долгота"),
        ({"Адрес": "", "Широта": "", "Долгота": ""}, "Адрес"),
    ],
)
def test_invalid_cells_have_line_field_and_action(changes, column, norms, policy):
    item, issues = validate_csv(csv_file([changes]), "work.csv", norms, policy)
    assert item is None
    assert any(i.line == 2 and i.column == column and len(i.message) > 15 for i in issues)


def test_duplicate_ids_dates_and_coordinates_reject_whole_file(norms, policy):
    cases = [
        ([{}, {}], "повторяется"),
        (
            [{}, {"Заявка": "two", "Начало": "18.08.2026 10:00", "Окончание": "18.08.2026 18:00"}],
            "разные даты",
        ),
        ([{}, {"Заявка": "two", "Широта": "55.73"}], "разные координаты"),
    ]
    for rows, fragment in cases:
        item, issues = validate_csv(csv_file(rows), "work.csv", norms, policy)
        assert item is None and any(fragment in i.message for i in issues)


def test_different_neighbourhoods_and_duplicate_addresses_are_valid(norms, policy):
    item, errors = validate_csv(
        csv_file([{}, {"Заявка": "two", "Район": "Другой квартал"}]), "Участок.csv", norms, policy
    )
    assert not errors and len(item["requests"]) == 2
    assert item["requests"][0].location_id == item["requests"][1].location_id


def test_office_and_file_limits(norms, policy):
    for data, name, fragment in [
        (b"", "empty.csv", "пуст"),
        (b"\xef\xbb\xbf", "empty.csv", "пуст"),
        (csv_file(), "x.xlsx", "CSV"),
        (b"x" * (MAX_BYTES + 1), "x.csv", "2 МБ"),
        (b"\xff\xfe\x00a", "x.csv", "текстовым"),
        (csv_file(office=False), "x.csv", "офиса"),
        (csv_file() + "Адрес офиса;Ещё офис\n".encode(), "x.csv", "ровно одну"),
        (csv_file([{"Заявка": str(i)} for i in range(101)]), "x.csv", "100 заявок"),
        (b"ID;ID;\n", "x.csv", "не повторяться"),
    ]:
        item, issues = validate_csv(data, name, norms, policy)
        assert item is None and any(fragment in i.message for i in issues)


@pytest.fixture
def application(tmp_path):
    path = tmp_path / "api.sqlite"
    app = create_app(storage_path=path)
    with TestClient(app) as client:
        yield app, client, PlanStore(path)


def upload(client, data=None, name="Произвольный участок.csv"):
    response = client.post(
        "/api/uploads/validate",
        params={"filename": name},
        content=data if data is not None else csv_file(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_validation_never_geocodes_routes_or_runs_solver(application, monkeypatch):
    app, client, store = application

    def forbidden(*args, **kwargs):
        pytest.fail("Validation must not calculate or geocode")

    monkeypatch.setattr(app.state.routing, "calculation", forbidden)
    monkeypatch.setattr(app.state.address_suggestions, "search", forbidden)
    monkeypatch.setattr(app.state.upload_geocoder, "prepare", forbidden)
    monkeypatch.setattr("dispatch.api.optimize", forbidden)
    monkeypatch.setattr("dispatch.api.solve_baseline", forbidden)
    valid = upload(client)
    assert valid["valid"] and valid["scenario"]["requests"][0]["service_s"] == 4200
    invalid = upload(client, csv_file([{"Широта": "bad"}]))
    assert not invalid["valid"] and "upload_id" not in invalid
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM uploads").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    restored = client.get(f"/api/uploads/{valid['upload_id']}").json()
    assert restored["scenario"] == valid["scenario"]


def test_uploaded_plan_uses_explicit_staff_constraints_and_restores(application):
    app, client, store = application
    loaded = upload(client, csv_file([{"Требуемый транспорт": "Автомобиль"}]))
    base = f"/api/uploads/{loaded['upload_id']}"
    engineers = loaded["engineers"][:1]
    engineers[0].update(
        profile="walk", skills=["installation"], shift_start_s=32400, shift_end_s=72000
    )
    rejected = client.post(base + "/baseline", json={"engineers": engineers}).json()
    assert rejected["plan"]["metrics"]["assigned"] == 0
    engineers[0]["profile"] = "car"
    result = client.post(
        base + "/optimize",
        headers={"X-Calculation-ID": "own-run"},
        json={"engineers": engineers, "search": {"time_limit_ms": 0}},
    )
    assert result.status_code == 200, result.text
    result = result.json()
    assert result["plan"]["metrics"]["assigned"] == 1
    assert result["scenario"]["upload_id"] == loaded["upload_id"]
    scenario, _ = store.get(result["session_id"])
    require_valid_plan(scenario, Plan.model_validate(result["plan"]))
    assert client.get("/api/calculations/own-run").json()["percent"] == 100
    assert client.get(f"/api/sessions/{result['session_id']}").json() == result
    assert client.get("/api/calculations/other-run").status_code == 404
    upload(client, csv_file([{"Заявка": ""}]))
    assert client.get(f"/api/sessions/{result['session_id']}").json() == result


def test_staff_validation_and_office_only_start(application):
    _, client, store = application
    loaded = upload(client)
    path = f"/api/uploads/{loaded['upload_id']}/baseline"
    e = loaded["engineers"][0]
    bad = client.post(path, json={"engineers": [{**e, "skills": []}]})
    assert bad.status_code == 422 and "Инженер 1" in bad.json()["detail"]
    bad = client.post(path, json={"engineers": [{**e, "shift_end_s": 10}]})
    assert bad.status_code == 422 and "Начало смены" in bad.json()["detail"]
    duplicate = client.post(path, json={"engineers": [e, e]})
    assert duplicate.status_code == 422 and "повторяться" in duplicate.json()["detail"]
    own = {**e, "start_address": "Другой старт", "start_lat": 55.72, "start_lon": 37.62}
    response = client.post(path, json={"engineers": [own]})
    assert response.status_code == 422
    assert "Все инженеры начинают из офиса" in response.json()["detail"]
    response = client.post(path, json={"engineers": [e]})
    assert response.status_code == 200, response.text
    scenario, _ = store.get(response.json()["session_id"])
    start = scenario.engineers[0].start_location_id
    assert start == location_id(scenario.office_address)
    assert scenario.geography[start].point.lat == 55.7
    assert start in scenario.matrices[e["profile"]].locations
    assert (
        client.get(f"/api/sessions/{response.json()['session_id']}/geography").json()[start][
            "point"
        ]["lat"]
        == 55.7
    )


def test_progress_is_observable_and_failure_never_reaches_100(application, monkeypatch):
    app, client, _ = application
    loaded = upload(client)
    started, release = Event(), Event()

    # A calculation failure is surfaced without creating an accepted plan.
    def fail(*args, **kwargs):
        started.set()
        assert release.wait(5)
        raise RoutingError("Тестовый сбой сети")

    monkeypatch.setattr("dispatch.routing.service.RoutingCalculation.new_day", fail)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(
            client.post,
            f"/api/uploads/{loaded['upload_id']}/baseline",
            headers={"X-Calculation-ID": "running"},
            json={"engineers": loaded["engineers"][:1]},
        )
        try:
            assert started.wait(5)
            state = client.get("/api/calculations/running").json()
            assert state["status"] == "running" and state["percent"] < 100
        finally:
            release.set()
        assert task.result().status_code == 503
    state = client.get("/api/calculations/running").json()
    assert state["status"] == "failed" and state["percent"] < 100


def test_progress_is_monotonic_and_isolated():
    tracker = CalculationProgress()
    with tracker.track("one") as update:
        update("Матрицы переездов", 1, 2)
        percent = tracker.get("one")["percent"]
        with tracker.track("two"):
            assert tracker.get("two")["percent"] == 0
            assert tracker.get("one")["percent"] == percent
        update("Загрузка дорожной и транспортной сети", 0, 1)
        assert tracker.get("one")["percent"] == percent
    assert tracker.get("one")["percent"] == 100
    with pytest.raises(VersionConflict):
        with tracker.track("one"):
            pass


def test_csv_points_override_old_catalog_but_later_edits_win(application, policy):
    _, client, store = application
    catalog = GeographyStore(store)
    key = location_id("Тестовый дом 1")

    def edit(revision, lat):
        return catalog.edit(
            key,
            CoordinateEdit(
                address="Тестовый дом 1", expected_revision=revision, lat=lat, lon=37.61
            ),
        )

    old = edit(0, 55.1)
    loaded = upload(client)
    item = UploadStore(store).get(loaded["upload_id"])
    scenario = uploaded_scenario(item, loaded["upload_id"], [], policy, catalog)
    assert scenario.geography[key].point.lat == 55.71
    assert attach_geography(scenario, catalog).geography[key].point.lat == 55.71
    edit(old.revision, 55.8)
    assert attach_geography(scenario, catalog).geography[key].point.lat == 55.8
    assert (
        client.get(f"/api/uploads/{loaded['upload_id']}/geography").json()[key]["point"]["lat"]
        == 55.8
    )
    assert scenario.geography[key].point.lat == 55.71


def test_new_addresses_resolved_only_on_real_calculation(application, monkeypatch):
    app, client, store = application
    calls = []
    catalog = GeographyStore(store)

    def lookup(address, **kwargs):
        calls.append(address)
        return catalog.get(address).model_copy(
            update={
                "status": "matched",
                "query": address,
                "point": GeoPoint(
                    lat=55.71, lon=37.61, label=address, precision="house", source="test"
                ),
            }
        )

    monkeypatch.setattr(app.state.upload_geocoder.client, "lookup", lookup)
    loaded = upload(client, csv_file([{"Широта": "", "Долгота": ""}]))
    assert calls == []
    app.state.routing.mode = "real"
    # Isolate this test from network graphs; prepare is covered by routing integration tests.
    monkeypatch.setattr("dispatch.routing.service.RoutingCalculation.new_day", lambda _, s: s)
    response = client.post(f"/api/uploads/{loaded['upload_id']}/baseline", json={"engineers": []})
    assert response.status_code == 200, response.text
    assert calls == ["Тестовый дом 1"]  # Office coordinates from CSV are never looked up.
    scenario, _ = store.get(response.json()["session_id"])
    assert scenario.geography[location_id("Тестовый дом 1")].point.lat == 55.71
    client.post(f"/api/uploads/{loaded['upload_id']}/baseline", json={"engineers": []})
    assert len(calls) == 1


def test_geocoder_failure_preserves_plan_and_does_not_route(application, monkeypatch):
    app, client, store = application
    loaded = upload(client, csv_file([{"Широта": "", "Долгота": ""}]))
    app.state.routing.mode = "real"

    def fail(*args, **kwargs):
        raise GeocodingError("offline")

    monkeypatch.setattr(app.state.upload_geocoder.client, "lookup", fail)

    def forbidden(*args, **kwargs):
        pytest.fail("Must not calculate after a provider failure")

    monkeypatch.setattr(app.state.routing, "calculation", forbidden)
    response = client.post(
        f"/api/uploads/{loaded['upload_id']}/baseline",
        json={"engineers": []},
        headers={"X-Calculation-ID": "no-geocoder"},
    )
    assert response.status_code == 503 and "координаты" in response.json()["detail"]
    assert client.get("/api/calculations/no-geocoder").json()["status"] == "failed"
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.parametrize("answer", ["Москва, Тестовая улица, 7", None, "offline"])
def test_reverse_geocoding_is_optional_display_only(application, monkeypatch, answer):
    app, client, store = application
    calls = []

    def reverse(point):
        calls.append((point.lat, point.lon))
        if answer == "offline":
            raise GeocodingError("offline")
        return answer

    def no_forward(*args, **kwargs):
        pytest.fail("Coordinates must not be geocoded as an address")

    monkeypatch.setattr(app.state.upload_geocoder.client, "reverse", reverse)
    monkeypatch.setattr(app.state.upload_geocoder.client, "lookup", no_forward)
    loaded = upload(client, csv_file([{"Адрес": ""}, {"Заявка": "two", "Адрес": ""}]))
    assert calls == []
    app.state.routing.mode = "real"

    def check_coordinates(_, scenario):
        request = scenario.requests[0]
        point = scenario.geography[request.location_id].point
        assert (point.lat, point.lon, point.source, point.precision) == (
            55.71,
            37.61,
            "csv",
            "manual",
        )
        assert request.address == "Координаты 55.71, 37.61"
        return scenario

    monkeypatch.setattr("dispatch.routing.service.RoutingCalculation.new_day", check_coordinates)
    response = client.post(f"/api/uploads/{loaded['upload_id']}/baseline", json={"engineers": []})
    assert response.status_code == 200, response.text
    assert calls == [(55.71, 37.61)]  # Two requests at one point need one lookup.
    result = response.json()
    label = "Рядом: " + answer if answer not in {None, "offline"} else "Координаты 55.71, 37.61"
    assert all(r["display_address"] == label for r in result["scenario"]["requests"])
    scenario, _ = store.get(result["session_id"])
    assert scenario.requests[0].raw["Адрес"] == ""
    require_valid_plan(scenario, Plan.model_validate(result["plan"]))
    key = scenario.requests[0].location_id
    restored = client.get(f"/api/sessions/{result['session_id']}/geography").json()
    assert restored[key]["point"]["label"] == label
    # A reverse label is scoped to the snapshot and must not seed a different coordinate.
    assert GeographyStore(store).get(scenario.requests[0].address).point is None


@pytest.mark.parametrize("endpoint", ["baseline", "optimize"])
def test_unresolved_addresses_stop_before_routing_and_solver(application, monkeypatch, endpoint):
    app, client, store = application
    loaded = upload(
        client,
        csv_file(
            [
                {},
                {
                    "Заявка": "missing",
                    "Адрес": "Несуществующий дом 999",
                    "Широта": "",
                    "Долгота": "",
                },
            ]
        ),
    )
    app.state.routing.mode = "real"
    catalog = GeographyStore(store)
    calls = []

    def lookup(address, **kwargs):
        calls.append(address)
        return catalog.get(address).model_copy(update={"query": address})

    def forbidden(*args, **kwargs):
        pytest.fail("Address review must happen before any route or solver calculation")

    monkeypatch.setattr(app.state.upload_geocoder.client, "lookup", lookup)
    monkeypatch.setattr(app.state.routing, "calculation", forbidden)
    monkeypatch.setattr("dispatch.api.optimize", forbidden)
    monkeypatch.setattr("dispatch.api.solve_baseline", forbidden)
    url = f"/api/uploads/{loaded['upload_id']}/{endpoint}"
    response = client.post(url, json={"engineers": []}, headers={"X-Calculation-ID": "review"})
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "address_review_required"
    assert body["review"]["geography"][location_id("Несуществующий дом 999")]["point"] is None
    assert len(body["review"]["requests"]) == 2
    progress = client.get("/api/calculations/review").json()
    assert progress["status"] == "needs_input" and progress["percent"] < 100
    assert client.post(url, json={"engineers": []}).status_code == 409
    assert calls == ["Несуществующий дом 999"]
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_discard_is_reversible_and_never_discards_the_office(application, monkeypatch):
    app, client, store = application
    data = (
        csv_file([{"Широта": "", "Долгота": ""}])
        .decode("utf-8-sig")
        .replace("Адрес офиса;Тестовый офис;55.7;37.6", "Адрес офиса;Неизвестный офис")
        .encode()
    )
    loaded = upload(client, data)
    app.state.routing.mode = "real"
    catalog = GeographyStore(store)
    monkeypatch.setattr(
        app.state.upload_geocoder.client,
        "lookup",
        lambda address, **kw: catalog.get(address).model_copy(update={"query": address}),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Office needs a confirmed point even when all requests are excluded")

    monkeypatch.setattr(app.state.routing, "calculation", forbidden)
    request_id = loaded["scenario"]["requests"][0]["id"]
    url = f"/api/uploads/{loaded['upload_id']}/optimize"
    response = client.post(url, json={"engineers": [], "excluded_request_ids": [request_id]})
    assert response.status_code == 409
    review = response.json()["review"]
    assert review["geography"][review["office_location_id"]]["point"] is None
    assert (
        len(client.get(f"/api/uploads/{loaded['upload_id']}").json()["scenario"]["requests"]) == 1
    )
    assert len(review["requests"]) == 1
    for invalid in ([request_id, request_id], ["unknown"], [review["office_location_id"]]):
        assert (
            client.post(url, json={"engineers": [], "excluded_request_ids": invalid}).status_code
            == 422
        )


def test_confirm_then_continue_uses_new_coordinates_and_records_exclusions(
    application, monkeypatch
):
    app, client, store = application
    loaded = upload(
        client,
        csv_file(
            [
                {"Адрес": "Дом для исправления", "Широта": "", "Долгота": ""},
                {"Заявка": "skip", "Адрес": "Дом для исключения", "Широта": "", "Долгота": ""},
            ]
        ),
    )
    app.state.routing.mode = "real"
    catalog = GeographyStore(store)
    calls = []

    def lookup(address, **kwargs):
        calls.append(address)
        return catalog.get(address).model_copy(update={"query": address})

    monkeypatch.setattr(app.state.upload_geocoder.client, "lookup", lookup)
    url = f"/api/uploads/{loaded['upload_id']}/baseline"
    assert client.post(url, json={"engineers": []}).status_code == 409
    record = catalog.get("Дом для исправления")
    corrected_address = "Москва, улица Исправленная, дом 5"
    saved = client.put(
        f"/api/geography/{record.location_id}",
        json={
            "address": record.address,
            "expected_revision": record.revision,
            "lat": 55.711,
            "lon": 37.611,
            "selected_point": {
                "lat": 55.711,
                "lon": 37.611,
                "label": corrected_address,
                "precision": "house",
                "source": "test",
            },
        },
    )
    assert saved.status_code == 200
    prepared = []

    def prepare(_calculation, scenario):
        prepared.append(scenario)
        assert all(record.point for record in scenario.geography.values())
        return scenario

    monkeypatch.setattr("dispatch.routing.service.RoutingCalculation.new_day", prepare)
    skip = loaded["scenario"]["requests"][1]["id"]
    result = client.post(url, json={"engineers": [], "excluded_request_ids": [skip]})
    assert result.status_code == 200, result.text
    run = result.json()
    assert len(prepared) == 1 and len(run["scenario"]["requests"]) == 1
    assert run["scenario"]["geography"][record.location_id]["point"]["lat"] == 55.711
    job = run["scenario"]["requests"][0]
    assert job["display_address"] == corrected_address
    assert job["address"] == "Дом для исправления"
    assert job["location_id"] == record.location_id
    assert job["raw"]["Адрес"] == "Дом для исправления"
    assert run["manifest"]["excluded_request_ids"] == [skip]
    assert run["manifest"]["excluded_requests"][0]["id"] == skip
    assert len(calls) == 2
    assert (
        len(client.get(f"/api/uploads/{loaded['upload_id']}").json()["scenario"]["requests"]) == 2
    )
    assert client.post(url, json={"engineers": []}).status_code == 409
    assert client.get(f"/api/sessions/{run['session_id']}").json() == run
    # Later corrections are visible to a new calculation, never to an accepted snapshot.
    changed = client.put(
        f"/api/geography/{record.location_id}",
        json={
            "address": record.address,
            "expected_revision": saved.json()["revision"],
            "lat": 55.711,
            "lon": 37.611,
            "selected_point": {**saved.json()["point"], "label": "Другое название"},
        },
    )
    assert changed.status_code == 200
    assert client.get(f"/api/sessions/{run['session_id']}").json() == run
    fresh = client.get(f"/api/uploads/{loaded['upload_id']}").json()
    assert fresh["scenario"]["requests"][0]["display_address"] == "Другое название"
