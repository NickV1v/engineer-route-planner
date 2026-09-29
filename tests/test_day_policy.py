"""Published staffing, schedule stability, and review-before-commit regressions."""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from conftest import engineer, request, scenario
from fastapi.testclient import TestClient

from dispatch.api import create_app, with_scenario
from dispatch.baseline import solve_baseline
from dispatch.checker import check_plan, require_valid_plan
from dispatch.events import DayEvent, apply_event, replan
from dispatch.objective import DAY_COMPONENTS, score_plan
from dispatch.optimizer import SearchOptions, invoke_solver, solver_binary
from dispatch.publication import PublishRequest, publish
from dispatch.scheduling import append_visit, build_plan, frozen_routes
from dispatch.store import PlanStore

SEARCH = SearchOptions(time_limit_ms=0, rounds=8)


def explicit_plan(case, ids):
    routes = frozen_routes(case)
    requests = {r.id: r for r in case.requests}
    indices = {p: {loc: i for i, loc in enumerate(m.locations)} for p, m in case.matrices.items()}
    for e, route_ids in zip(case.engineers, ids):
        for rid in route_ids:
            v, reason = append_visit(case, e, routes[e.id], requests[rid], requests, indices)
            assert v, reason
            routes[e.id].visits.append(v)
            routes[e.id].distance_m += v.distance_m
    return require_valid_plan(case, build_plan(case, list(routes.values()), []))


def scheduled_case():
    case = scenario(
        [
            request("B", service_s=1200, window_end_s=50000),
            request("C", source_order=1, service_s=1200, window_end_s=36000),
        ],
        [engineer(), engineer("E2"), engineer("E3")],
    )
    plan = explicit_plan(case, [["B"], ["C"], []])
    published, response = publish(
        case,
        plan,
        PublishRequest(operation_id="publish", expected_version=1, engineer_ids=["E1", "E2"]),
    )
    return published, response["plan"]


def failure(**kwargs):
    return DayEvent(
        event_id="failure",
        expected_version=1,
        at_s=0,
        type="engineer_unavailable",
        engineer_id="E2",
        search=SEARCH,
        **kwargs,
    )


@pytest.mark.parametrize(
    "mode,delay,assigned",
    [("preserve", 0, 1), ("preserve", 900, 1), ("preserve", 1800, 2), ("flexible", 0, 2)],
)
def test_off_shift_never_called_and_delay_limit_is_hard(mode, delay, assigned):
    case, plan = scheduled_case()
    updated, result = replan(case, plan, failure(mode=mode, max_delay_s=delay))
    final = require_valid_plan(updated, result["plan"])
    assert final.metrics.assigned == assigned
    assert final.routes[2].visits == []
    assert final.routes[1].visits == []
    b = next(v for v in final.routes[0].visits if v.request_id == "B")
    assert b.start_s == (37800 if assigned == 2 else 36000)
    assert result["comparison"]["objective_components"] == DAY_COMPONENTS
    assert "active_engineers" not in DAY_COMPONENTS
    assert invoke_solver(updated, final, SEARCH, mode="evaluate").score == list(
        score_plan(updated, final)
    )
    from oracle import optimum

    # The off-shift third engineer cannot affect the mathematical optimum.
    two = updated.model_copy(deep=True)
    two.engineers.pop()
    two.planning_state.frozen_routes.pop("E3")
    two.planning_state.policy.reference_routes.pop("E3")
    assert optimum(two) == score_plan(updated, final)


def test_cancellation_preserves_promised_time_and_next_event_keeps_waiting():
    case = scenario([request("A", service_s=1200), request("B", source_order=1, service_s=1200)])
    case.roster = ["E1"]
    plan = solve_baseline(case)
    event = DayEvent(
        event_id="cancel",
        expected_version=1,
        at_s=0,
        type="cancel",
        request_id="A",
        max_delay_s=0,
        search=SEARCH,
    )
    updated, first = replan(case, plan, event)
    assert first["plan"].routes[0].visits[0].start_s == plan.routes[0].visits[1].start_s
    event = DayEvent(
        event_id="stop",
        expected_version=2,
        at_s=33000,
        type="engineer_unavailable",
        engineer_id="E1",
        search=SEARCH,
    )
    later, second = replan(updated, first["plan"], event)
    assert second["plan"].routes == first["plan"].routes
    require_valid_plan(later, second["plan"])


def test_healthy_assignments_and_order_cannot_be_removed_or_reordered():
    case = scenario(
        [request("A", service_s=1200), request("B", source_order=1, service_s=1200)],
        [engineer(), engineer("E2")],
    )
    case.roster = ["E1", "E2"]
    plan = solve_baseline(case)
    updated = apply_event(case, plan, failure())
    valid = solve_baseline(updated)
    for route_ids in [[["B", "A"], []], [["A"], []], [[], ["A", "B"]]]:
        body = dict(
            protocol_version="1.0",
            scenario=updated.model_dump(),
            input_hash=updated.fingerprint(),
            options=SEARCH.model_dump(),
            mode="evaluate",
            initial_routes=route_ids,
        )
        result = subprocess.run(
            [str(solver_binary())],
            input=json.dumps(body),
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 2
    broken = valid.model_copy(deep=True)
    broken.routes[0].visits.reverse()
    assert any("сохранение" in e for e in check_plan(updated, broken))


def test_day_search_does_not_empty_healthy_engineer_route():
    case = scenario(
        [request("A", service_s=1200), request("B", source_order=1, service_s=1200)],
        [engineer(), engineer("E2"), engineer("E3")],
    )
    case.roster = ["E1", "E2", "E3"]
    plan = explicit_plan(case, [["A"], ["B"], []])
    event = failure(mode="flexible").model_copy(update={"engineer_id": "E3"})
    updated, result = replan(case, plan, event)
    assert result["plan"].routes == plan.routes
    assert score_plan(updated, result["plan"])[2:6] == (0, 0, 0, 0)


def test_preview_publish_replay_atomic_commit_and_stale_alternative(tmp_path):
    path = tmp_path / "plans.sqlite"
    case, plan = scheduled_case()
    store = PlanStore(path)
    saved = store.create(case, with_scenario(case, {"plan": plan}))
    url = "/api/sessions/" + saved["session_id"]
    payload = failure().model_dump()
    with TestClient(create_app(storage_path=path)) as client:
        normal = client.post(url + "/events/preview", json=payload)
        assert normal.status_code == 200, normal.text
        normal = normal.json()
        assert normal["result"]["plan"]["metrics"]["assigned"] == 1
        assert client.get(url).json() == saved
        assert client.post(url + "/events/preview", json=payload).json() == normal
        assert client.post(url + "/events/preview", json={**payload, "at_s": 1}).status_code == 409
        flexible_payload = {**payload, "event_id": "alternative", "mode": "flexible"}
        assert client.post(url + "/events", json=flexible_payload).status_code == 409
        flexible = client.post(url + "/events/preview", json=flexible_payload).json()
        assert flexible["result"]["plan"]["metrics"]["assigned"] == 2
        assert client.get(url).json()["version"] == 1
        accepted = client.post(url + "/events/commit", json={"preview_id": flexible["preview_id"]})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json() == flexible["result"]
        assert (
            client.post(url + "/events/commit", json={"preview_id": flexible["preview_id"]}).json()
            == accepted.json()
        )
        assert (
            client.post(
                url + "/events/commit", json={"preview_id": normal["preview_id"]}
            ).status_code
            == 409
        )
        assert client.get(url + "?version=1").json() == saved
        assert client.post(url + "/events/commit", json={"preview_id": "0" * 32}).status_code == 404
    with TestClient(create_app(storage_path=path)) as restarted:
        assert restarted.get(url).json() == accepted.json()


def test_publish_requires_all_assigned_people_and_is_idempotent(tmp_path):
    path = tmp_path / "plans.sqlite"
    case = scenario(engineers=[engineer(), engineer("E2")])
    plan = solve_baseline(case)
    saved = PlanStore(path).create(case, with_scenario(case, {"plan": plan}))
    url = "/api/sessions/" + saved["session_id"]
    with TestClient(create_app(storage_path=path)) as client:
        # Old drafts activate automatically; an unassigned engineer is outside that roster.
        assert client.post(url + "/events/preview", json=failure().model_dump()).status_code == 422
        assert client.get(url).json() == saved
        payload = dict(operation_id="p", expected_version=1, engineer_ids=["E2"])
        assert client.post(url + "/publish", json=payload).status_code == 422
        payload["engineer_ids"] = ["E1", "E2"]
        result = client.post(url + "/publish", json=payload)
        assert result.status_code == 200, result.text
        result = result.json()
        assert result["version"] == 2
        assert result["plan"]["routes"] == saved["plan"]["routes"]
        assert result["staffing"]["scheduled"] == 2
        assert result["staffing"]["reserve_ids"] == ["E2"]
        assert client.post(url + "/publish", json=payload).json() == result
        assert (
            client.post(url + "/publish", json={**payload, "engineer_ids": ["E1"]}).status_code
            == 409
        )
        snapshot = client.get(url + "/snapshot").json()
        assert snapshot["scenario"]["roster"] == ["E1", "E2"]


def test_preview_competing_commits_only_one_wins(tmp_path):
    store = PlanStore(tmp_path / "plans.sqlite")
    case, plan = scheduled_case()
    session = store.create(case, {"plan": plan})["session_id"]
    previews = [
        store.save_preview(session, 1, str(i), str(i), case, {"plan": plan}) for i in range(2)
    ]
    barrier = Barrier(2)

    def commit(p):
        from dispatch.store import VersionConflict

        barrier.wait()
        try:
            return store.commit_preview(session, p["preview_id"])
        except VersionConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(commit, previews))
    assert sum(r is not None for r in results) == 1
    assert store.get(session)[1]["version"] == 2


def test_nonmetric_cancellation_requires_explicit_flexible_variant():
    # Removing A makes the direct office -> B edge unreachable.
    case = scenario(
        [request("A", service_s=1200), request("B", source_order=1, service_s=1200)],
        edges={("office", "B"): (None, None)},
    )
    case.roster = ["E1"]
    plan = solve_baseline(case)
    event = DayEvent(
        event_id="cancel", expected_version=1, at_s=0, type="cancel", request_id="A", search=SEARCH
    )
    with pytest.raises(ValueError, match="вариант с перестановками"):
        replan(case, plan, event)
    assert [v.request_id for v in plan.routes[0].visits] == ["A", "B"]
    updated, result = replan(case, plan, event.model_copy(update={"mode": "flexible"}))
    assert result["plan"].metrics.unassigned == 1
    require_valid_plan(updated, result["plan"])


def test_preserve_does_not_use_event_to_reallocate_old_backlog():
    from dispatch.models import Rejection

    case = scenario(
        [request("A", service_s=1200), request("B", source_order=1, service_s=1200)],
        [engineer(), engineer("E2")],
    )
    case.roster = ["E1", "E2"]
    # A valid accepted plan may intentionally leave a feasible job unassigned.
    plan = solve_baseline(case)
    routes = [r.model_copy(deep=True) for r in plan.routes]
    routes[0].visits.pop()
    routes[0].distance_m = sum(v.distance_m for v in routes[0].visits)
    plan = require_valid_plan(
        case,
        build_plan(
            case,
            routes,
            [
                Rejection(
                    request_id="B", reason="pending", explanation="Remaining backlog", checks={}
                )
            ],
        ),
    )
    normal, result = replan(case, plan, failure())
    assert result["plan"].metrics.assigned == 1
    assert result["plan"].unassigned[0].request_id == "B"
    flexible, alternative = replan(case, plan, failure(mode="flexible"))
    assert alternative["plan"].metrics.assigned == 2
    require_valid_plan(normal, result["plan"])
    require_valid_plan(flexible, alternative["plan"])
