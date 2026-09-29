"""Client devices: issuance, independent feasibility, events and immutable snapshots."""

import hashlib
import json
import random
import subprocess

import pytest
from conftest import engineer, request, scenario
from fastapi.testclient import TestClient
from oracle import optimum
from pydantic import ValidationError

from dispatch.api import create_app, with_scenario
from dispatch.baseline import solve_baseline
from dispatch.checker import check_plan, require_valid_plan
from dispatch.equipment import EQUIPMENT_POLICY, equipment_required
from dispatch.events import DayEvent, replan
from dispatch.models import Equipment, Scenario
from dispatch.objective import score_plan
from dispatch.optimizer import SearchOptions, invoke_solver, optimize, solver_binary
from dispatch.publication import PublishRequest, publish
from dispatch.scenarios import make_scenario
from dispatch.store import PlanStore

OPTIONS = SearchOptions(time_limit_ms=0, rounds=8)


def draft(requests=None, engineers=None):
    case = scenario(requests, engineers)
    case.equipment_policy = EQUIPMENT_POLICY
    # These fixtures use existing locations for events, without routing providers.
    case.manifest["norms"] = {"test": {"service_s": 600}}
    case.manifest["policy"] = {
        "norm_mapping": {
            kind: {"work": "test", "skill": "installation"}
            for kind in ("Подключение", "Дозаказ", "Локальная заявка", "Глобальная проблема")
        }
    }
    return case


def published(case, plan=None):
    return publish(
        case,
        plan or solve_baseline(case),
        PublishRequest(
            operation_id="issue", expected_version=1, engineer_ids=[e.id for e in case.engineers]
        ),
    )


def event(type, **fields):
    return DayEvent(
        event_id="event",
        expected_version=1,
        at_s=0,
        type=type,
        search=OPTIONS,
        **fields,
    )


def add(kind="Подключение", **fields):
    return event(
        "add_urgent",
        request={
            "id": "new",
            "address": "Тестовый адрес A",
            "kind": kind,
            "window_start_s": 36000,
            "window_end_s": 60000,
        },
        **fields,
    )


def keep_locations(case, key):
    # Deterministic test-only edges; no external routing or location providers.
    for matrix in case.matrices.values():
        if key not in matrix.locations:
            for row in matrix.duration_s:
                row.append(600)
            for row in matrix.distance_m:
                row.append(1000)
            matrix.duration_s.append([600] * len(matrix.locations) + [0])
            matrix.distance_m.append([1000] * len(matrix.locations) + [0])
            matrix.locations.append(key)


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("Подключение", (1, 0)),
        ("Дозаказ", (0, 1)),
        ("Локальная заявка", (0, 0)),
        ("Глобальная проблема", (0, 0)),
    ],
)
def test_device_requirements_follow_job_kind_not_skill(kind, expected):
    required = equipment_required([request(kind=kind, skill="emergency")])
    assert (required.routers, required.set_top_boxes) == expected


def test_issue_exact_accepted_assignments_without_reserve():
    case = draft(
        [
            request("A", window_start_s=50000, window_end_s=55000, service_s=600),
            request("B", source_order=1, kind="Дозаказ", service_s=600),
        ],
        [engineer(), engineer("E2", skills=["local"])],
    )
    result = optimize(case, OPTIONS)
    assert result["baseline"].metrics.assigned == 1
    assert result["plan"].metrics.assigned == 2
    assert case.equipment_issued is None
    active, response = published(case, result["plan"])
    assert active.equipment_issued == {
        "E1": Equipment(routers=1, set_top_boxes=1),
        "E2": Equipment(routers=0, set_top_boxes=0),
    }
    assert response["plan"].routes == result["plan"].routes
    require_valid_plan(active, response["baseline"])
    require_valid_plan(active, response["plan"])
    assert case.equipment_issued is None  # Publication never changes the draft.


@pytest.mark.parametrize(
    "kind,reason", [("Подключение", "equipment_routers"), ("Дозаказ", "equipment_set_top_boxes")]
)
@pytest.mark.parametrize("mode", ["preserve", "flexible"])
def test_extra_device_job_rejected_in_both_modes(kind, reason, mode):
    case, response = published(draft(engineers=[engineer(), engineer("E2")]))
    updated, result = replan(
        case, response["plan"], add(kind, mode=mode), transport_extension=keep_locations
    )
    assert updated.equipment_issued == case.equipment_issued
    for plan in (result["baseline"], result["plan"]):
        require_valid_plan(updated, plan)
        assert plan.metrics.assigned <= 1
        assert plan.metrics.unassigned >= 1
        assert all(reason in r.checks.values() for r in plan.unassigned)
    assert score_plan(updated, result["plan"]) == optimum(updated)


@pytest.mark.parametrize("kind", ["Локальная заявка", "Глобальная проблема"])
def test_jobs_without_devices_can_be_added(kind):
    case, response = published(draft())
    updated, result = replan(case, response["plan"], add(kind), transport_extension=keep_locations)
    assert result["plan"].metrics.assigned == 2
    assert updated.equipment_issued == case.equipment_issued
    require_valid_plan(updated, result["plan"])


def test_cancel_releases_capacity_without_changing_issuance():
    case, response = published(draft())
    cancelled, result = replan(case, response["plan"], event("cancel", request_id="A"))
    assert result["plan"].metrics.assigned == 0
    assert cancelled.equipment_issued["E1"].routers == 1
    restored, result = replan(cancelled, result["plan"], add(), transport_extension=keep_locations)
    assert result["plan"].metrics.assigned == 1
    assert restored.equipment_issued == case.equipment_issued


def test_unavailable_engineer_does_not_transfer_devices_to_idle_colleague():
    case, response = published(draft(engineers=[engineer(), engineer("E2")]))
    updated, result = replan(
        case, response["plan"], event("engineer_unavailable", engineer_id="E1", mode="flexible")
    )
    assert result["plan"].metrics.assigned == 0
    assert result["plan"].unassigned[0].checks == {"E1": "unavailable", "E2": "equipment_routers"}
    assert updated.equipment_issued == case.equipment_issued
    assert score_plan(updated, result["plan"]) == optimum(updated)


def test_frozen_visits_keep_devices_committed_across_events():
    case, response = published(draft([request(service_s=600)]))
    first, result = replan(
        case,
        response["plan"],
        add(),
        transport_extension=keep_locations,
    )
    # Advance past the original job's scheduled finish before attempting the next insertion.
    later_event = add().model_copy(update={"at_s": 41000})
    later_event.request.id = "later"
    later, final = replan(first, result["plan"], later_event, transport_extension=keep_locations)
    assert later.planning_state.frozen_routes["E1"] == response["plan"].routes[0].visits
    assert final["plan"].metrics.assigned == 1
    assert final["plan"].metrics.unassigned == 2
    assert later.equipment_issued == case.equipment_issued
    require_valid_plan(later, final["plan"])


def test_reassignment_uses_only_colleagues_own_released_stock():
    case, response = published(
        draft(
            [
                request("A", service_s=600, window_end_s=36000),
                request("B", source_order=1, service_s=600, window_end_s=36000),
            ],
            [engineer(), engineer("E2")],
        )
    )
    assert [len(r.visits) for r in response["plan"].routes] == [1, 1]
    freed, result = replan(case, response["plan"], event("cancel", request_id="B"))
    moved, result = replan(freed, result["plan"], event("engineer_unavailable", engineer_id="E1"))
    assert result["plan"].routes[0].visits == []
    assert [v.request_id for v in result["plan"].routes[1].visits] == ["A"]
    assert moved.equipment_issued == case.equipment_issued
    assert score_plan(moved, result["plan"]) == optimum(moved)


def test_unused_router_cannot_replace_a_set_top_box():
    case, response = published(draft())
    freed, result = replan(case, response["plan"], event("cancel", request_id="A"))
    updated, result = replan(
        freed, result["plan"], add("Дозаказ"), transport_extension=keep_locations
    )
    assert result["plan"].metrics.assigned == 0
    assert result["plan"].unassigned[0].checks == {"E1": "equipment_set_top_boxes"}
    assert updated.equipment_issued["E1"].routers == 1


def test_small_equipment_cases_against_exhaustive_oracle():
    for seed in range(12):
        rng = random.Random(seed)
        case = draft(
            [
                request(
                    str(i),
                    source_order=i,
                    service_s=600,
                    kind=rng.choice(["Подключение", "Дозаказ", "Локальная заявка"]),
                )
                for i in range(5)
            ],
            [engineer(), engineer("E2")],
        )
        # Vary independent resources to exercise assignment and relocation choices.
        case.roster = [e.id for e in case.engineers]
        case.equipment_issued = {
            e.id: Equipment(routers=rng.randrange(3), set_top_boxes=rng.randrange(3))
            for e in case.engineers
        }
        case = Scenario.model_validate(case.model_dump())
        result = optimize(case, OPTIONS)
        assert score_plan(case, result["plan"]) == optimum(case)
        require_valid_plan(case, result["plan"])
        assert (
            invoke_solver(case, result["plan"], OPTIONS, mode="evaluate").plan.routes
            == result["plan"].routes
        )


def test_checker_and_cpp_independently_reject_overdrawn_plan():
    case, response = published(draft([request(), request("B", source_order=1, kind="Дозаказ")]))
    for resource, word in (("routers", "роутеров"), ("set_top_boxes", "ТВ-приставок")):
        broken = case.model_copy(deep=True)
        setattr(broken.equipment_issued["E1"], resource, 0)
        plan = response["plan"].model_copy(update={"input_hash": broken.fingerprint()})
        assert any(word in error for error in check_plan(broken, plan))
        assert raw_solver(broken.model_dump(), [["A", "B"]]).returncode == 2


def raw_solver(data, routes):
    return subprocess.run(
        [str(solver_binary())],
        input=json.dumps(
            {
                "protocol_version": "1.0",
                "scenario": data,
                "input_hash": "test",
                "options": OPTIONS.model_dump(),
                "mode": "evaluate",
                "initial_routes": routes,
            }
        ),
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize("value", [-1, 501, True, 1.5, "1", None])
def test_invalid_stock_rejected_by_both_contracts(value):
    case, _ = published(draft())
    data = case.model_dump()
    data["equipment_issued"]["E1"]["routers"] = value
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)
    assert raw_solver(data, [["A"]]).returncode == 2


@pytest.mark.parametrize(
    "change",
    [
        {"equipment_policy": "unknown"},
        {"equipment_policy": None},
        {"equipment_issued": None},
        {"equipment_issued": {}},
        {"equipment_issued": {"wrong": {"routers": 1, "set_top_boxes": 0}}},
        {"roster": None},
        {"roster": []},
    ],
)
def test_invalid_issuance_structure_rejected(change):
    case, _ = published(draft())
    data = {**case.model_dump(), **change}
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)
    assert raw_solver(data, [["A"]]).returncode == 2


def test_old_snapshots_retain_fingerprint_and_legacy_behavior():
    case = scenario()
    data = case.model_dump(
        exclude={
            "equipment_policy",
            "equipment_issued",
            "objective_policy",
            "schedule_policy",
            "roster",
            "geography",
        }
    )
    for r in data["requests"]:
        r.pop("work_type")
    for e in data["engineers"]:
        e.pop("name")
    digest = hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    restored = Scenario.model_validate(data)
    assert restored.fingerprint() == digest
    active, response = published(restored)
    assert active.equipment_issued is None
    assert (
        invoke_solver(active, response["plan"], OPTIONS, mode="evaluate").plan.routes
        == response["plan"].routes
    )


def test_new_imported_days_enable_equipment(imported, policy):
    for item in imported:
        case = make_scenario(item, policy)
        assert case.equipment_policy == EQUIPMENT_POLICY
        active, response = published(case)
        assert sum(s.routers for s in active.equipment_issued.values()) == sum(
            r.kind == "Подключение"
            for r in case.requests
            if r.id in {v.request_id for route in response["plan"].routes for v in route.visits}
        )


def test_issuance_survives_preview_commit_replay_history_and_restart(tmp_path):
    path = tmp_path / "equipment.sqlite"
    case = draft()
    saved = PlanStore(path).create(case, with_scenario(case, {"plan": solve_baseline(case)}))
    url = "/api/sessions/" + saved["session_id"]
    with TestClient(create_app(storage_path=path)) as client:
        payload = {"operation_id": "publish", "expected_version": 1, "engineer_ids": ["E1"]}
        response = client.post(url + "/publish", json=payload)
        assert response.status_code == 200, response.text
        issued = response.json()
        assert issued["scenario"]["equipment_issued"] == {"E1": {"routers": 1, "set_top_boxes": 0}}
        assert client.post(url + "/publish", json=payload).json() == issued
        cancellation = event("cancel", request_id="A").model_dump()
        cancellation["expected_version"] = 2
        preview = client.post(url + "/events/preview", json=cancellation)
        assert preview.status_code == 200, preview.text
        preview = preview.json()
        assert client.get(url).json() == issued
        committed = client.post(url + "/events/commit", json={"preview_id": preview["preview_id"]})
        assert committed.status_code == 200, committed.text
        committed = committed.json()
        assert committed["scenario"]["equipment_issued"] == issued["scenario"]["equipment_issued"]
        assert committed["plan"]["metrics"]["assigned"] == 0
        assert (
            client.post(url + "/events/commit", json={"preview_id": preview["preview_id"]}).json()
            == committed
        )
        assert client.get(url + "?version=1").json() == saved
        assert client.get(url + "?version=2").json() == issued
    with TestClient(create_app(storage_path=path)) as client:
        assert client.get(url).json() == committed
