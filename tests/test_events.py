from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from conftest import engineer, request
from conftest import scenario as draft_scenario
from fastapi.testclient import TestClient
from pydantic import ValidationError

from dispatch.api import create_app
from dispatch.baseline import solve_baseline
from dispatch.checker import check_plan, require_valid_plan
from dispatch.events import DayEvent, apply_event, plan_changes, replan
from dispatch.models import Plan, Scenario
from dispatch.objective import score_plan
from dispatch.optimizer import SearchOptions, SolverError, invoke_solver
from dispatch.publication import PublishRequest, publish
from dispatch.scenarios import make_scenario as make_draft_scenario
from dispatch.scheduling import repair_previous_plan
from dispatch.store import PlanStore, SessionNotFound, VersionConflict

SEARCH = SearchOptions(time_limit_ms=0, rounds=8, use_eliminate=True)


def scenario(*args, **kwargs):
    case = draft_scenario(*args, **kwargs)
    case.roster = [e.id for e in case.engineers]
    return case


def make_scenario(*args, **kwargs):
    case = make_draft_scenario(*args, **kwargs)
    active, _ = publish(
        case,
        solve_baseline(case),
        PublishRequest(
            operation_id="publish", expected_version=1, engineer_ids=[e.id for e in case.engineers]
        ),
    )
    return active


def event(at=32700, type="engineer_unavailable", **kwargs):
    return DayEvent(
        event_id="event-1", expected_version=1, at_s=at, type=type, search=SEARCH, **kwargs
    )


def day():
    return scenario(
        [request("A"), request("B", source_order=1, window_end_s=57600)],
        [engineer(), engineer("E2")],
    )


@pytest.mark.parametrize(
    "at,fixed",
    [
        (0, 0),
        (32400, 0),
        (32401, 1),
        (32700, 1),
        (33000, 1),
        (35000, 1),
        (36000, 1),
        (37500, 1),
        (40200, 1),
        (40201, 2),
        (86399, 2),
    ],
)
def test_departure_waiting_work_and_shift_boundaries(at, fixed):
    original = day()
    plan = solve_baseline(original)
    updated, result = replan(original, plan, event(at, engineer_id="E1"))
    final = require_valid_plan(updated, result["plan"])
    assert original.planning_state is None
    assert len(updated.planning_state.frozen_routes["E1"]) == fixed
    assert final.routes[0].visits == plan.routes[0].visits[:fixed]
    for route in final.routes:
        prefix = updated.planning_state.frozen_routes[route.engineer_id]
        assert route.visits[: len(prefix)] == prefix
        assert all(v.arrival_s - v.travel_s >= at for v in route.visits[len(prefix) :])
    assert score_plan(updated, final) <= score_plan(updated, repair_previous_plan(updated, plan))
    assert invoke_solver(updated, final, SEARCH, mode="evaluate").plan.routes == final.routes


def test_later_event_preserves_idle_time_after_earlier_event():
    original = scenario(
        day().requests + [request("C", source_order=2, released_at_s=43200, window_end_s=57600)],
        [engineer(), engineer("E2")],
    )
    initial = solve_baseline(original)
    one, first = replan(original, initial, event(40200, engineer_id="E1"))
    future = first["plan"].routes[1].visits
    assert future
    two, second = replan(one, first["plan"], event(future[-1].arrival_s + 1, engineer_id="E2"))
    require_valid_plan(two, second["plan"])
    assert second["plan"].routes[1].visits == future
    with pytest.raises(VersionConflict, match="раньше"):
        apply_event(two, second["plan"], event(0, type="cancel", request_id="C"))


def test_cancellation_and_checker_tampering():
    original = day()
    initial = solve_baseline(original)
    with pytest.raises(VersionConflict, match="начат"):
        apply_event(original, initial, event(type="cancel", request_id="A"))
    updated, result = replan(original, initial, event(type="cancel", request_id="B"))
    assert result["plan"].metrics.total == 1
    assert result["plan"].metrics.unassigned == 0
    assert result["changes"][0]["changes"] == ["cancelled"]
    broken = result["plan"].model_copy(deep=True)
    broken.routes[0].visits[0].start_s += 1
    assert any("закреплённая" in error for error in check_plan(updated, broken))
    # Even tampering with both prefix and plan must fail physical validation.
    updated.planning_state.frozen_routes["E1"][0].travel_s += 1
    broken.routes[0].visits[0] = updated.planning_state.frozen_routes["E1"][0]
    assert any("расчёт" in error for error in check_plan(updated, broken))


@pytest.mark.parametrize(
    "kind,service",
    [
        ("Подключение", 4200),
        ("Дозаказ", 1200),
        ("Локальная заявка", 1800),
        ("Глобальная проблема", 4800),
    ],
)
def test_urgent_uses_norms_release_time_and_preserves_matrix(imported, policy, kind, service):
    original = make_scenario(imported[0], policy, 12)
    initial = solve_baseline(original)
    addition = event(
        43200,
        type="add_urgent",
        request=dict(
            id="urgent-1",
            address="Новый синтетический адрес, 1",
            kind=kind,
            window_start_s=32400,
            window_end_s=72000,
        ),
    )
    updated, result = replan(original, initial, addition)
    assert len(updated.requests) == len(original.requests) + 1
    added = updated.requests[-1]
    assert added.service_s == service
    assert added.urgent == (kind == "Глобальная проблема")
    assert (
        added.work_type
        == {
            "Подключение": "installation",
            "Дозаказ": "additional",
            "Локальная заявка": "repair",
            "Глобальная проблема": "emergency",
        }[kind]
    )
    assert added.released_at_s == 43200
    for profile, matrix in original.matrices.items():
        n = len(matrix.locations)
        extended = updated.matrices[profile]
        assert len(extended.locations) == n + 1
        assert [row[:n] for row in extended.duration_s[:n]] == matrix.duration_s
        assert [row[:n] for row in extended.distance_m[:n]] == matrix.distance_m
    final = require_valid_plan(updated, result["plan"])
    for route in final.routes:
        for visit in route.visits:
            if visit.request_id == added.id:
                assert visit.arrival_s - visit.travel_s >= 43200
    assert len(original.requests) == 66
    with pytest.raises(VersionConflict, match="уже существовала"):
        apply_event(updated, final, addition)


def test_same_address_no_matrix_extension_and_no_engineers(imported, policy):
    original = make_scenario(imported[0], policy, 0)
    addition = event(
        43200,
        type="add_urgent",
        request=dict(
            id="same-address",
            address=original.requests[0].address,
            kind="Дозаказ",
            window_start_s=43200,
            window_end_s=72000,
        ),
    )
    updated, result = replan(original, solve_baseline(original), addition)
    assert updated.matrices == original.matrices
    assert result["plan"].metrics.assigned == 0
    assert result["plan"].metrics.unassigned == 67
    removed, cancelled = replan(
        updated, result["plan"], event(43200, type="cancel", request_id=updated.requests[-1].id)
    )
    with pytest.raises(VersionConflict, match="уже существовала"):
        apply_event(removed, cancelled["plan"], addition)


@pytest.mark.parametrize(
    "payload",
    [
        dict(type="cancel"),
        dict(type="cancel", request_id="A", engineer_id="E1"),
        dict(type="engineer_unavailable", engineer_id="E1", at_s=-1),
        dict(
            type="add_urgent",
            request=dict(
                id="U", address="   ", kind="Дозаказ", window_start_s=43200, window_end_s=43000
            ),
        ),
    ],
)
def test_event_contract_rejects_ambiguous_or_invalid_input(payload):
    with pytest.raises(ValidationError):
        DayEvent.model_validate(
            dict(event_id="x", expected_version=1, at_s=43200, **payload)
            if "at_s" not in payload
            else dict(event_id="x", expected_version=1, **payload)
        )


def test_diff_does_not_call_insertion_reordering():
    original = day()
    before = solve_baseline(original)
    after = before.model_copy(deep=True)
    added = after.routes[0].visits[0].model_copy(update={"request_id": "new"})
    after.routes[0].visits.insert(0, added)
    updated = original.model_copy(deep=True)
    updated.requests.append(request("new", source_order=2))
    changes = plan_changes(before, after, original, updated)
    assert len(changes) == 1 and changes[0]["request_id"] == "new"
    after.routes[0].visits.reverse()
    changes = plan_changes(before, after, original, updated)
    assert all("reordered" in c["changes"] for c in changes if c["request_id"] != "new")


@pytest.mark.parametrize("duplicate", [False, True])
def test_store_concurrent_events_atomic_and_idempotent(tmp_path, duplicate):
    store = PlanStore(tmp_path / "plans.sqlite")
    original = day()
    initial = store.create(original, {"plan": solve_baseline(original)})
    sid = initial["session_id"]
    barrier = Barrier(2)

    def commit(index):
        barrier.wait()
        try:
            return store.commit_event(
                sid,
                1,
                "same" if duplicate else str(index),
                "payload",
                original,
                {"plan": solve_baseline(original)},
            )
        except VersionConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(commit, [1, 2]))
    assert sum(r is not None for r in results) == (2 if duplicate else 1)
    if duplicate:
        assert results[0] == results[1]
        with pytest.raises(VersionConflict):
            store.find_event(sid, "same", "different")
    restarted = PlanStore(store.path)
    assert restarted.get(sid)[1]["version"] == 2
    assert restarted.get(sid, 1)[1] == initial
    with pytest.raises(SessionNotFound):
        restarted.get(sid, 3)


def test_event_api_history_replay_conflicts_and_failure(tmp_path, monkeypatch):
    path = tmp_path / "plans.sqlite"
    with TestClient(create_app(storage_path=path)) as client:
        initial = client.post("/api/scenarios/Восток/baseline", json={"engineer_count": 12}).json()
        url = "/api/sessions/" + initial["session_id"]
        draft = initial
        assert initial["version"] == 1
        assert initial["staffing"]["status"] == "active"
        payload = event(
            43200,
            type="add_urgent",
            request=dict(
                id="new",
                address="Синтетическая улица, 10",
                kind="Дозаказ",
                window_start_s=50400,
                window_end_s=72000,
            ),
        ).model_dump()
        payload["expected_version"] = 1
        response = client.post(url + "/events", json=payload)
        assert response.status_code == 200, response.text
        current = response.json()
        assert current["version"] == 2
        assert current["plan"]["metrics"]["total"] == 67
        assert client.get(url + "?version=1").json() == draft
        assert client.get(url).json() == current
        assert client.post(url + "/events", json=payload).json() == current
        assert client.post(url + "/events", json={**payload, "at_s": 43201}).status_code == 409
        assert (
            client.post(url + "/events", json={**payload, "event_id": "stale"}).status_code == 409
        )
        snapshot = client.get(url + "/snapshot").json()
        require_valid_plan(
            Scenario.model_validate(snapshot["scenario"]), Plan.model_validate(snapshot["plan"])
        )
        assert client.get(url + "?version=99").status_code == 404
        assert client.get("/api/sessions/missing").status_code == 404
        assert client.post(url + "/events", json={}).status_code == 422

        def fail(*args, **kwargs):
            raise SolverError("Тестовый отказ солвера")

        with monkeypatch.context() as patch:
            patch.setattr("dispatch.events.optimize", fail)
            cancel = event(43200, type="cancel", request_id="Восток:event-new").model_dump()
            cancel.update(event_id="cancel", expected_version=2)
            assert client.post(url + "/events", json=cancel).status_code == 503
            assert client.get(url).json() == current
        third = client.post(url + "/events", json=cancel)
        assert third.status_code == 200, third.text
        assert third.json()["version"] == 3
        assert third.json()["plan"]["metrics"]["total"] == 66
        assert client.post(url + "/events", json=payload).json() == current
        assert client.get(url).json()["version"] == 3
    with TestClient(create_app(storage_path=path)) as restarted:
        assert restarted.get(url).json()["version"] == 3
        assert restarted.get(url + "?version=1").json() == draft


@pytest.mark.parametrize("mode", ["preserve", "flexible"])
def test_random_replans_against_exhaustive_oracle(record_property, mode):
    from oracle import optimum
    from test_optimizer import random_case

    from dispatch.optimizer import optimize

    hits = 0
    for seed in range(20):
        original = random_case(seed)
        original.roster = [e.id for e in original.engineers]
        plan = optimize(original, SEARCH)["plan"]
        at = [0, 250, 1000, 2000, 3500][seed % 5]
        updated, result = replan(original, plan, event(at, engineer_id="E1", mode=mode))
        exact = optimum(updated)
        actual = score_plan(updated, result["plan"])
        assert exact <= actual <= score_plan(updated, repair_previous_plan(updated, plan))
        assert check_plan(updated, result["plan"]) == []
        hits += exact == actual
        final, stopped = replan(
            updated, result["plan"], event(at + 500, engineer_id="E2", mode=mode)
        )
        assert score_plan(final, stopped["plan"]) == optimum(final)
    record_property(f"replanning_{mode}_exact_matches", f"{hits}/20")


@pytest.mark.parametrize("damage", ["dropped", "moved", "forged", "blocked"])
def test_cpp_rejects_invalid_frozen_history(damage):
    import json
    import subprocess

    from dispatch.optimizer import solver_binary

    original = day()
    updated, result = replan(original, solve_baseline(original), event(engineer_id="E1"))
    plan = result["plan"]
    routes = [[v.request_id for v in r.visits] for r in plan.routes]
    if damage == "dropped":
        routes[0] = []
    elif damage == "moved":
        routes[1].insert(0, routes[0].pop())
    elif damage == "blocked":
        routes[0].append(routes[1].pop())
    else:
        updated.planning_state.frozen_routes["E1"][0].travel_s += 1
    body = dict(
        protocol_version="1.0",
        scenario=updated.model_dump(),
        input_hash=updated.fingerprint(),
        options=SEARCH.model_dump(),
        mode="evaluate",
        initial_routes=routes,
    )
    process = subprocess.run(
        [str(solver_binary())], input=json.dumps(body), text=True, capture_output=True, timeout=10
    )
    assert process.returncode == 2
    assert not process.stdout
    assert "Infeasible" in json.loads(process.stderr)["error"]
