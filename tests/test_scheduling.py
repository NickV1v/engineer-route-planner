import json
from pathlib import Path

import pytest
from conftest import engineer, request, scenario
from pydantic import ValidationError

from dispatch.baseline import solve_baseline
from dispatch.checker import check_plan
from dispatch.models import Scenario


def solve(case):
    plan = solve_baseline(case)
    assert check_plan(case, plan) == []
    return plan


def test_waiting_service_and_no_return_to_office():
    plan = solve(scenario())
    visit = plan.routes[0].visits[0]
    assert (visit.arrival_s, visit.start_s, visit.finish_s) == (33000, 36000, 40200)
    assert visit.waiting_s == 3000
    assert plan.metrics.service_s == 4200
    assert plan.metrics.distance_m == 1000  # Not a round trip.


@pytest.mark.parametrize(
    "arrival,shift_end,assigned", [(43200, 47400, 1), (43201, 61200, 0), (43200, 47399, 0)]
)
def test_window_is_inclusive_and_only_bounds_start(arrival, shift_end, assigned):
    case = scenario(
        engineers=[engineer(shift_end_s=shift_end)],
        edges={("office", "A"): (arrival - 32400, 1000)},
    )
    assert solve(case).metrics.assigned == assigned


def test_baseline_preserves_source_order_and_first_feasible_engineer():
    late = request("late", source_order=0, window_start_s=64800, window_end_s=72000)
    early = request("early", source_order=1, window_start_s=36000, window_end_s=43200)
    case = scenario(
        [early, late], [engineer("first", shift_end_s=79200), engineer("second", shift_end_s=79200)]
    )
    plan = solve(case)
    assert [v.request_id for v in plan.routes[0].visits] == ["late"]
    assert [v.request_id for v in plan.routes[1].visits] == ["early"]


def test_same_address_does_not_merge_jobs():
    case = scenario([request(), request("B", source_order=1, location_id="A", service_s=1200)])
    plan = solve(case)
    assert plan.metrics.assigned == 2
    assert plan.metrics.service_s == 5400
    assert plan.routes[0].visits[1].travel_s == 0


def test_skill_and_transport_must_match_the_same_engineer():
    case = scenario(
        [request(required_profile="bicycle")],
        [engineer(), engineer("E2", profile="bicycle", skills=["local"])],
    )
    plan = solve(case)
    assert plan.metrics.assigned == 0
    assert plan.unassigned[0].checks == {"E1": "transport", "E2": "skill"}


def test_directional_and_unreachable_edges():
    case = scenario(edges={("office", "A"): (None, None), ("A", "office"): (30, 10)})
    assert solve(case).unassigned[0].reason == "unreachable"
    case = scenario(edges={("office", "A"): (60, 100), ("A", "office"): (1000, 2000)})
    assert solve(case).metrics.distance_m == 100


def test_empty_inputs_and_idle_engineers():
    assert solve(scenario(requests=[])).metrics.active_engineers == 0
    assert solve(scenario(engineers=[])).unassigned[0].reason == "no_engineers"
    plan = solve(scenario(engineers=[engineer(), engineer("E2")]))
    assert plan.metrics.active_engineers == 1
    assert plan.routes[1].distance_m == 0


def test_other_zone_and_missing_matrix_points_are_rejected():
    with pytest.raises(ValidationError, match="Смешаны зоны"):
        scenario([request(zone="other")])
    case = scenario().model_dump()
    case["requests"][0]["location_id"] = "missing"
    with pytest.raises(ValidationError, match="отсутствуют точки"):
        Scenario.model_validate(case)


@pytest.mark.parametrize(
    "corruption", ["time", "duplicate", "missing", "metrics", "engineer", "hash", "skill"]
)
def test_checker_detects_corrupt_plan(corruption):
    case = scenario()
    plan = solve_baseline(case)
    if corruption == "time":
        plan.routes[0].visits[0].finish_s -= 1
    if corruption == "duplicate":
        plan.routes[0].visits.append(plan.routes[0].visits[0])
    if corruption == "missing":
        plan.routes[0].visits.clear()
    if corruption == "metrics":
        plan.metrics.distance_m += 1
    if corruption == "engineer":
        plan.routes[0].engineer_id = "unknown"
    if corruption == "hash":
        plan.input_hash = "wrong"
    if corruption == "skill":
        case.engineers[0].skills = ["local"]
        plan.input_hash = case.fingerprint()
    assert check_plan(case, plan)


def test_shared_fixture_expected_schedule():
    root = Path(__file__).parent / "fixtures"
    case = Scenario.model_validate_json((root / "small_scenario.json").read_text())
    expected = json.loads((root / "small_expected.json").read_text())
    plan = solve(case)
    assert plan.metrics.model_dump() == expected["metrics"]
    assert [r.model_dump() for r in plan.routes] == expected["routes"]
    assert [r.request_id for r in plan.unassigned] == expected["unassigned_ids"]
