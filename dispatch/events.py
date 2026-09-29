"""Day-state transitions. Every input state and resulting plan is checked independently."""

import hashlib
import json
from typing import Callable, Literal

from pydantic import Field, model_validator

from dispatch.checker import require_valid_plan
from dispatch.geography import location_address
from dispatch.importer import location_id
from dispatch.models import (
    GeoLocation,
    GeoPoint,
    Model,
    Plan,
    PlanningState,
    Profile,
    ReplanningPolicy,
    Request,
    Scenario,
)
from dispatch.objective import compare_plans
from dispatch.optimizer import SearchOptions, optimize
from dispatch.scenarios import extend_matrices
from dispatch.scheduling import repair_previous_plan
from dispatch.store import VersionConflict
from dispatch.work_types import classify_work


class UrgentRequest(Model):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    address: str = Field(min_length=3, max_length=500)
    kind: Literal["Подключение", "Дозаказ", "Локальная заявка", "Глобальная проблема"]
    window_start_s: int = Field(ge=0, lt=86400)
    window_end_s: int = Field(ge=0, lt=86400)
    required_profile: Profile | None = None
    coordinates: GeoPoint | None = None
    original_address: str | None = Field(default=None, min_length=3, max_length=500)

    @model_validator(mode="after")
    def exact_coordinates(self):
        if self.coordinates and self.coordinates.precision not in {"house", "manual"}:
            raise ValueError("Для новой заявки нужно выбрать дом или указать точку вручную")
        return self


class DayEvent(Model):
    event_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    expected_version: int = Field(ge=1)
    at_s: int = Field(ge=0, lt=86400)
    type: Literal["add_urgent", "cancel", "engineer_unavailable"]
    request: UrgentRequest | None = None
    request_id: str | None = None
    engineer_id: str | None = None
    mode: Literal["preserve", "flexible"] = "preserve"
    max_delay_s: int = Field(default=900, ge=0, le=7200)
    search: SearchOptions = Field(default_factory=SearchOptions)

    @model_validator(mode="after")
    def valid_payload(self):
        fields = (
            self.request is not None,
            self.request_id is not None,
            self.engineer_id is not None,
        )
        expected = {
            "add_urgent": (True, False, False),
            "cancel": (False, True, False),
            "engineer_unavailable": (False, False, True),
        }
        if fields != expected[self.type]:
            raise ValueError("Поля события не соответствуют его типу")
        if self.request and (
            self.request.window_start_s > self.request.window_end_s
            or self.request.window_end_s < self.at_s
        ):
            raise ValueError("Окно новой заявки некорректно или уже закончилось")
        if self.request and not self.request.address.strip():
            raise ValueError("Адрес не может быть пустым")
        return self

    def fingerprint(self) -> str:
        values = self.model_dump()
        if self.request:
            # Preserve retry keys for requests created before interactive suggestions.
            for key in ("coordinates", "original_address"):
                if values["request"][key] is None:
                    values["request"].pop(key)
        if self.mode == "preserve" and self.max_delay_s == 900:
            values.pop("mode")
            values.pop("max_delay_s")
        return hashlib.sha256(
            json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()


def apply_event(
    scenario: Scenario,
    plan: Plan,
    event: DayEvent,
    *,
    transport_extension: Callable[[Scenario, str], None] = extend_matrices,
    transport_preparation: Callable[[Scenario, str | None], None] | None = None,
) -> Scenario:
    require_valid_plan(scenario, plan)
    if scenario.roster is None:
        raise VersionConflict("Сначала утвердите состав смены для этого плана.")
    previous_state = scenario.planning_state
    if previous_state and event.at_s < previous_state.at_s:
        raise VersionConflict("Время события раньше уже применённого события")
    # Reconstruct per-profile matrices independently, even if the caller reused model objects.
    updated = Scenario.model_validate(scenario.model_dump())
    new_location = None
    frozen = {}
    for route in plan.routes:
        already_frozen = (
            len(previous_state.frozen_routes.get(route.engineer_id, [])) if previous_state else 0
        )
        prefix = []
        for i, visit in enumerate(route.visits):
            if (
                i < already_frozen
                or visit.arrival_s - visit.travel_s < event.at_s
                or visit.start_s <= event.at_s
            ):
                prefix.append(visit.model_copy(deep=True))
            else:
                break
        frozen[route.engineer_id] = prefix
    updated.planning_state = PlanningState(
        at_s=event.at_s,
        frozen_routes=frozen,
        unavailable_engineers=list(previous_state.unavailable_engineers) if previous_state else [],
    )
    locked_ids = {v.request_id for prefix in frozen.values() for v in prefix}
    if event.type == "add_urgent":
        data = event.request
        request_id = f"{scenario.id}:event-{data.id}"
        history = scenario.manifest.get("events", [])
        if any(r.id == request_id for r in scenario.requests) or any(
            item.get("request", {}).get("id") == data.id for item in history if item.get("request")
        ):
            raise VersionConflict("Заявка с таким ID уже существовала в этом рабочем дне")
        rule = scenario.manifest["policy"]["norm_mapping"][data.kind]
        norm = scenario.manifest["norms"][rule["work"]]
        address = data.address.strip()
        subtype = (
            ("Авария" if data.kind == "Глобальная проблема" else "Событие диспетчера")
            if scenario.objective_policy is not None
            else "Срочное событие"
        )
        work_type = (
            classify_work(data.kind, subtype).work_type
            if scenario.objective_policy is not None
            else None
        )
        new_request = Request(
            id=request_id,
            zone=scenario.id,
            source_order=max((r.source_order for r in scenario.requests), default=-1) + 1,
            source_line=1,
            location_id=location_id(address),
            address=address,
            kind=data.kind,
            subtype=subtype,
            work_type=work_type,
            window_start_s=data.window_start_s,
            window_end_s=data.window_end_s,
            service_s=norm["service_s"],
            skill=rule["skill"],
            required_profile=data.required_profile,
            urgent=work_type == "emergency" if scenario.objective_policy is not None else True,
            released_at_s=event.at_s,
            raw={
                "source": "event",
                "event_id": event.event_id,
                **({"original_address": data.original_address} if data.original_address else {}),
            },
        )
        existing_locations = {r.location_id for r in scenario.requests} | {
            location_id(scenario.office_address),
            *[e.start_location_id for e in scenario.engineers],
        }
        if scenario.manifest.get("transport") == "routing_static_v1":
            # Cancelled visits leave matrix rows behind. Those rows still refer to
            # the coordinates used when preparing the immutable transport package.
            existing_locations.update(
                loc for matrix in scenario.matrices.values() for loc in matrix.locations
            )
        if data.coordinates and new_request.location_id in existing_locations:
            existing_geo = scenario.geography.get(new_request.location_id)
            old_point = existing_geo.point if existing_geo else None
            if old_point is None or (old_point.lat, old_point.lon) != (
                data.coordinates.lat,
                data.coordinates.lon,
            ):
                raise ValueError(
                    "Этот адрес уже используется в плане. Сначала уточните его через «Проверить адреса» "
                    "и примените координаты к плану."
                )
        if data.coordinates and new_request.location_id not in existing_locations:
            updated.geography[new_request.location_id] = GeoLocation(
                location_id=new_request.location_id,
                address=address,
                status="manual",
                point=data.coordinates,
                note="Адрес и точка выбраны диспетчером при создании заявки.",
            )
        new_location = new_request.location_id
        if transport_preparation is None:
            transport_extension(updated, new_location)
        updated.requests.append(new_request)
    elif event.type == "cancel":
        if event.request_id not in {r.id for r in scenario.requests}:
            raise ValueError("Отменяемая заявка не найдена")
        if event.request_id in locked_ids:
            raise VersionConflict(
                "Выезд к этой заявке уже начат или работа выполнена. Такая отмена пока не поддерживается."
            )
        updated.requests = [r for r in updated.requests if r.id != event.request_id]
    else:
        if event.engineer_id not in {e.id for e in scenario.engineers}:
            raise ValueError("Инженер не найден")
        if event.engineer_id in updated.planning_state.unavailable_engineers:
            raise VersionConflict("Инженер уже отмечен недоступным")
        if event.engineer_id not in scenario.roster:
            raise ValueError("Инженер не входит в утверждённую смену")
        updated.planning_state.unavailable_engineers.append(event.engineer_id)
    if transport_preparation is not None:
        transport_preparation(updated, new_location)
    active_ids = {r.id for r in updated.requests}
    active_locations = (
        {r.location_id for r in updated.requests}
        | {location_id(updated.office_address)}
        | {e.start_location_id for e in updated.engineers}
    )
    if updated.manifest.get("transport") == "routing_static_v1":
        active_locations.update(
            loc for matrix in updated.matrices.values() for loc in matrix.locations
        )
    updated.geography = {
        key: point for key, point in updated.geography.items() if key in active_locations
    }
    reference = {
        route.engineer_id: [
            v.model_copy(deep=True) for v in route.visits if v.request_id in active_ids
        ]
        for route in plan.routes
    }
    eligible = {v.request_id for visits in reference.values() for v in visits}
    if event.type == "add_urgent":
        eligible.add(request_id)
    updated.planning_state.policy = ReplanningPolicy(
        mode=event.mode,
        reference_routes=reference,
        eligible_request_ids=sorted(eligible if event.mode == "preserve" else active_ids),
        max_delay_s=event.max_delay_s,
    )
    updated.manifest.setdefault("events", []).append(
        event.model_dump(exclude={"search", "expected_version"})
    )
    updated.manifest["state_source"] = "schedule_simulation"
    return Scenario.model_validate(updated.model_dump())


def plan_changes(
    before: Plan, after: Plan, before_scenario: Scenario, after_scenario: Scenario
) -> list[dict]:
    def positions(plan):
        return {
            v.request_id: {"engineer_id": r.engineer_id, "start_s": v.start_s}
            for r in plan.routes
            for v in r.visits
        }

    old, new = positions(before), positions(after)
    reordered = set()
    new_routes = {r.engineer_id: r for r in after.routes}
    for route in before.routes:
        same_engineer = {
            v.request_id
            for v in route.visits
            if v.request_id in new and new[v.request_id]["engineer_id"] == route.engineer_id
        }
        previous = [v.request_id for v in route.visits if v.request_id in same_engineer]
        current = [
            v.request_id
            for v in new_routes[route.engineer_id].visits
            if v.request_id in same_engineer
        ]
        ranks = {rid: i for i, rid in enumerate(current)}
        for i, rid in enumerate(previous):
            for other in previous[i + 1 :]:
                if ranks[rid] > ranks[other]:
                    reordered.update((rid, other))
    before_requests = {r.id: r for r in before_scenario.requests}
    after_requests = {r.id: r for r in after_scenario.requests}
    changes = []
    for rid in sorted(before_requests.keys() | after_requests.keys()):
        tags = []
        if rid not in before_requests:
            tags.append("added")
        if rid not in after_requests:
            tags.append("cancelled")
        elif rid not in old and rid in new:
            tags.append("assigned")
        elif rid in old and rid not in new:
            tags.append("unassigned")
        elif rid in old and rid in new:
            if old[rid]["engineer_id"] != new[rid]["engineer_id"]:
                tags.append("reassigned")
            if old[rid]["start_s"] != new[rid]["start_s"]:
                tags.append("rescheduled")
            if rid in reordered:
                tags.append("reordered")
        if tags:
            request = after_requests.get(rid) or before_requests[rid]
            record = after_scenario.geography.get(
                request.location_id
            ) or before_scenario.geography.get(request.location_id)
            changes.append(
                {
                    "request_id": rid,
                    "address": location_address(record) if record else request.address,
                    "changes": tags,
                    "before": old.get(rid),
                    "after": new.get(rid),
                }
            )
    return changes


def replan(
    scenario: Scenario,
    plan: Plan,
    event: DayEvent,
    *,
    transport_extension: Callable[[Scenario, str], None] = extend_matrices,
    transport_preparation: Callable[[Scenario, str | None], None] | None = None,
) -> tuple[Scenario, dict]:
    updated = apply_event(
        scenario,
        plan,
        event,
        transport_extension=transport_extension,
        transport_preparation=transport_preparation,
    )
    initial = repair_previous_plan(updated, plan)
    # Cancelling a stop in a nonmetric matrix can invalidate the old suffix.
    # The baseline reports when preserving all commitments is impossible.
    if event.mode == "flexible":
        require_valid_plan(updated, initial)
    result = optimize(updated, event.search, warm_start=initial)
    return updated, {
        **result,
        "comparison": compare_plans(updated, result["baseline"], result["plan"]),
        "event": event.model_dump(),
        "changes": plan_changes(plan, result["plan"], scenario, updated),
        "frozen_request_ids": [
            v.request_id for prefix in updated.planning_state.frozen_routes.values() for v in prefix
        ],
        "event_at_s": event.at_s,
    }
