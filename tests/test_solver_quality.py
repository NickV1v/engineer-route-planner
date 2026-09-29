"""Scheduling quality, compound moves and independent checks of the new policy."""

import json
import subprocess

import pytest
from conftest import engineer, request, scenario
from oracle import optimum
from test_optimizer import random_case
from test_work_type_priority import activate

from dispatch.baseline import solve_baseline
from dispatch.checker import check_plan
from dispatch.events import DayEvent, apply_event
from dispatch.models import PlanningState
from dispatch.objective import objective_components, score_plan
from dispatch.optimizer import SearchOptions, invoke_solver, optimize, solver_binary

SEARCH = SearchOptions(time_limit_ms=0, rounds=12, max_evaluations=150_000)


def test_first_departure_is_delayed_without_moving_work_or_shift():
    case = scenario([request(window_start_s=43200, window_end_s=46800)])
    old_hash = case.fingerprint()
    old = solve_baseline(case)
    assert old.routes[0].visits[0].waiting_s == 10200
    case.schedule_policy = "compact_v1"
    baseline = solve_baseline(case)
    visit = baseline.routes[0].visits[0]
    assert visit.arrival_s == visit.start_s == 43200
    assert visit.arrival_s - visit.travel_s == 42600
    assert visit.waiting_s == 0
    assert visit.finish_s == old.routes[0].visits[0].finish_s
    assert case.engineers[0].shift_start_s == 32400
    assert check_plan(case, baseline) == []
    assert invoke_solver(case, baseline, SEARCH, mode="evaluate").plan.routes == baseline.routes
    corrupted = baseline.model_copy(deep=True)
    corrupted.routes[0].visits[0].arrival_s -= 1
    assert check_plan(case, corrupted)
    case.schedule_policy = None
    assert case.fingerprint() == old_hash
    assert check_plan(case, old) == []


def test_departure_stays_after_release_and_event_time():
    case = scenario([request(window_start_s=32400, window_end_s=50000, released_at_s=36000)])
    case.schedule_policy = "compact_v1"
    case.planning_state = PlanningState(at_s=40000, frozen_routes={})
    visit = optimize(case, SEARCH)["plan"].routes[0].visits[0]
    assert visit.arrival_s == visit.start_s == 40600
    assert visit.arrival_s - visit.travel_s == 40000


@pytest.mark.parametrize("at_s,frozen", [(42600, False), (42601, True)])
def test_freezing_uses_the_actual_delayed_departure(at_s, frozen):
    case = scenario([request(window_start_s=43200, window_end_s=46800)])
    case.schedule_policy = "compact_v1"
    case.roster = ["E1"]
    plan = solve_baseline(case)
    event = DayEvent(
        event_id="unavailable",
        expected_version=1,
        at_s=at_s,
        type="engineer_unavailable",
        engineer_id="E1",
        search=SEARCH,
    )
    updated = apply_event(case, plan, event)
    result = optimize(updated, SEARCH)["plan"]
    assert bool(updated.planning_state.frozen_routes["E1"]) == frozen
    assert result.routes[0].visits == (plan.routes[0].visits if frozen else [])
    assert check_plan(updated, result) == []


def test_compound_move_uses_idle_engineer_to_unlock_a_different_skill():
    jobs = [
        request("A", skill="local", kind="Локальная заявка", source_order=0),
        request("B", source_order=1),
        request("C", skill="local", kind="Локальная заявка", source_order=2),
    ]
    for job in jobs:
        job.window_start_s = 0
        job.window_end_s = 1600
        job.service_s = 800
    locations = ["office", "A", "B", "C"]
    case = scenario(
        jobs,
        [
            engineer(skills=["installation", "local"], shift_start_s=0, shift_end_s=1600),
            engineer("E2", shift_start_s=0, shift_end_s=1600),
        ],
        {(a, b): (0, 0) for a in locations for b in locations},
    )
    # The first round must improve this incumbent rather than construct a fresh ordering.
    case.planning_state = PlanningState(at_s=0, frozen_routes={})
    result = optimize(case, SearchOptions(time_limit_ms=0, rounds=1, max_evaluations=20000))
    assert result["baseline"].metrics.assigned == 2
    assert result["baseline"].metrics.active_engineers == 1
    assert result["plan"].metrics.assigned == 3
    assert result["plan"].metrics.active_engineers == 2
    assert [v.request_id for v in result["plan"].routes[1].visits] == ["B"]
    assert score_plan(case, result["plan"]) == optimum(case)


def test_waiting_matters_before_distance_but_never_before_coverage():
    jobs = [
        request("A", source_order=0, window_start_s=32400, window_end_s=33000, service_s=600),
        request("B", source_order=1, window_start_s=43200, window_end_s=43800, service_s=600),
        request("C", source_order=2, window_start_s=33600, window_end_s=46800, service_s=600),
    ]
    case = activate(scenario(jobs))
    case.schedule_policy = "compact_v1"
    result = optimize(case, SEARCH)
    assert result["plan"].metrics.assigned == result["baseline"].metrics.assigned == 3
    assert result["plan"].metrics.waiting_s < result["baseline"].metrics.waiting_s
    assert score_plan(case, result["plan"]) == optimum(case)
    assert objective_components(case)[-3:] == ["travel_wait_s", "distance_m", "travel_s"]


@pytest.mark.parametrize("seed", range(20))
def test_compact_policy_against_exhaustive_independent_oracle(seed):
    case = activate(random_case(seed))
    case.schedule_policy = "compact_v1"
    result = optimize(case, SEARCH)
    assert check_plan(case, result["plan"]) == []
    assert optimum(case) <= score_plan(case, result["plan"]) <= score_plan(case, result["baseline"])
    assert (
        invoke_solver(case, result["baseline"], SEARCH, mode="evaluate").plan.routes
        == result["baseline"].routes
    )


def test_expanded_budget_and_unknown_policy_contract():
    options = SearchOptions()
    assert (options.rounds, options.max_evaluations, options.time_limit_ms) == (
        2048,
        32_000_000,
        60_000,
    )
    case = scenario([])
    assert optimize(case, options)["plan"].metrics.total == 0
    payload = {
        "protocol_version": "1.0",
        "mode": "evaluate",
        "scenario": case.model_dump(),
        "input_hash": case.fingerprint(),
        "initial_routes": [[]],
        "options": options.model_dump(),
    }
    payload["scenario"]["schedule_policy"] = "unknown"
    response = subprocess.run(
        [str(solver_binary())], input=json.dumps(payload), text=True, capture_output=True
    )
    assert response.returncode == 2
    assert "Unsupported schedule policy" in response.stderr
