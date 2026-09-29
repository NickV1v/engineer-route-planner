"""Priority policy, independent optimum, and compatibility of immutable days."""

import json
import random
import subprocess

import pytest
from conftest import ROOT, engineer, request, scenario
from oracle import optimum, route_score
from pydantic import ValidationError
from test_optimizer import random_case

from dispatch.baseline import solve_baseline
from dispatch.checker import require_valid_plan
from dispatch.events import DayEvent, replan
from dispatch.models import PlanningState, ReplanningPolicy, Scenario
from dispatch.objective import compare_plans, objective_components, score_plan
from dispatch.optimizer import SearchOptions, invoke_solver, optimize, solver_binary
from dispatch.priority_samples import WORK_FIELDS, load_cases
from dispatch.scenarios import make_scenario
from dispatch.work_types import PRIORITY_POLICY, classify_work

SEARCH = SearchOptions(time_limit_ms=0, rounds=8, max_evaluations=100_000)
CASES = load_cases().cases


def activate(case):
    result = case.model_copy(deep=True)
    result.objective_policy = PRIORITY_POLICY
    for r in result.requests:
        r.work_type = classify_work(r.kind, r.subtype).work_type
    return Scenario.model_validate(result.model_dump())


@pytest.mark.parametrize("sample", CASES, ids=lambda c: c.id)
def test_cpp_and_python_match_full_exhaustive_priority_score(sample):
    case = sample.scenario()
    result = optimize(case, SEARCH)
    baseline, plan = result["baseline"], result["plan"]
    assert score_plan(case, plan) == optimum(case)
    assert score_plan(case, plan)[:3] == tuple(sample.expected_missing)
    assert plan.metrics.unassigned == sum(sample.expected_missing)
    assert plan.metrics.assigned + plan.metrics.unassigned == len(case.requests)
    assert invoke_solver(case, baseline, SEARCH, mode="evaluate").score == list(
        score_plan(case, baseline)
    )
    comparison = compare_plans(case, baseline, plan)
    assert len(comparison["objective_components"]) == len(comparison["optimized_score"])
    assert comparison["objective_version"] == (
        "work_type_day_stability_v1" if case.roster is not None else PRIORITY_POLICY
    )


@pytest.mark.parametrize("seed", range(20))
def test_random_priorities_against_independent_exhaustive_reference(seed, record_property):
    case = random_case(seed)
    rng = random.Random(seed)
    for r in case.requests:
        r.kind, r.subtype, r.skill = WORK_FIELDS[rng.choice(list(WORK_FIELDS))]
    case.engineers[1].skills = ["installation", "local", "emergency"]
    case = activate(case)
    # The legacy urgent flag is deliberately unrelated to the work type.
    result = optimize(case, SEARCH)
    plan = result["plan"]
    indices = {r.id: i for i, r in enumerate(case.requests)}
    routes = [[indices[v.request_id] for v in route.visits] for route in plan.routes]
    assert score_plan(case, plan) == route_score(case, routes)
    target = optimum(case)
    assert target <= score_plan(case, plan) <= score_plan(case, result["baseline"])
    record_property("matches_exact_optimum", str(score_plan(case, plan) == target))


def test_ambiguous_information_keeps_ordinary_priority_and_emergency_skill():
    case = activate(
        scenario(
            [
                request(
                    "info",
                    kind="Глобальная проблема",
                    subtype="Информация",
                    skill="emergency",
                    urgent=True,
                ),
                request("install", source_order=1),
            ],
            [engineer(skills=["installation", "emergency"], shift_end_s=40800)],
        )
    )
    assert case.requests[0].work_type is None
    result = optimize(case, SEARCH)
    assert [v.request_id for v in result["plan"].routes[0].visits] == ["install"]
    assert score_plan(case, result["plan"]) == (0, 0, 1, 1, 0, 1000, 600)
    assert result["baseline"].routes[0].visits[0].request_id == "info"


@pytest.mark.parametrize("mode", ["preserve", "flexible"])
@pytest.mark.parametrize("frozen", [False, True])
def test_priorities_respect_published_commitments_and_frozen_visits(mode, frozen):
    case = CASES[0].scenario()
    case.roster = ["E1"]
    initial = solve_baseline(case)  # Two repairs; the emergency remains unassigned.
    reference = {r.engineer_id: r.visits for r in initial.routes}
    case.planning_state = PlanningState(
        at_s=33001 if frozen else 32400,
        frozen_routes={"E1": initial.routes[0].visits[:1] if frozen else []},
        policy=ReplanningPolicy(
            mode=mode,
            reference_routes=reference,
            eligible_request_ids=[r.id for r in case.requests],
            max_delay_s=900,
        ),
    )
    case = Scenario.model_validate(case.model_dump())
    result = optimize(case, SEARCH)
    plan = require_valid_plan(case, result["plan"])
    assert score_plan(case, plan) == optimum(case)
    if mode == "preserve" or frozen:
        assert plan.routes[0].visits == initial.routes[0].visits
    else:
        assert [v.request_id for v in plan.routes[0].visits] == ["emergency"]
    assert "active_engineers" not in objective_components(case)
    assert len(score_plan(case, plan)) == 10


@pytest.mark.parametrize("published", [False, True])
def test_empty_and_zero_budget_priority_cases(published):
    for requests, people in [([], []), ([], [engineer()]), ([request()], [])]:
        case = activate(scenario(requests, people))
        if published:
            case.roster = [e.id for e in people]
        for budget in [SearchOptions(rounds=0), SearchOptions(max_evaluations=0), SEARCH]:
            result = optimize(case, budget)
            assert score_plan(case, result["plan"]) == optimum(case)


def test_new_import_classifies_all_rows_without_modifying_imported_data(imported, policy):
    before = [[r.model_dump() for r in source["requests"]] for source in imported]
    ambiguous = 0
    for source in imported:
        case = make_scenario(source, policy)
        assert case.objective_policy == PRIORITY_POLICY
        assert len(case.requests) == len(source["requests"])
        for r, original in zip(case.requests, source["requests"]):
            assert r.work_type == classify_work(r.kind, r.subtype).work_type
            assert (r.raw, r.service_s, r.skill) == (
                original.raw,
                original.service_s,
                original.skill,
            )
            ambiguous += r.work_type is None
    assert ambiguous == 6
    assert before == [[r.model_dump() for r in source["requests"]] for source in imported]


def test_old_fingerprint_and_event_semantics_survive_new_default(imported, policy):
    raw = json.loads((ROOT / "tests/fixtures/small_scenario.json").read_text())
    # Obtained with the unmodified Scenario model at b80e73f (including its defaults).
    expected = "35f4f908b1fbc7b72bd191af35dcaaba100af288064f95e50dd85658d5264226"
    old = Scenario.model_validate(raw)
    assert old.fingerprint() == expected
    assert old.objective_policy is None
    assert len(invoke_solver(old, solve_baseline(old), SEARCH, mode="evaluate").score) == 6

    case = make_scenario(imported[0], policy)
    case.objective_policy = None
    case.equipment_policy = None  # Legacy days predate both optional policies.
    case.schedule_policy = None
    for r in case.requests:
        r.work_type = None
    case.roster = [e.id for e in case.engineers]
    before = case.model_dump_json()
    event = DayEvent(
        event_id="legacy-add",
        expected_version=1,
        at_s=32400,
        type="add_urgent",
        request={
            "id": "new",
            "address": case.requests[0].address,
            "kind": "Дозаказ",
            "window_start_s": 32400,
            "window_end_s": 72000,
        },
        search=SEARCH,
    )
    updated, result = replan(case, solve_baseline(case), event)
    assert updated.objective_policy is None
    assert updated.requests[-1].work_type is None
    assert updated.requests[-1].urgent
    assert updated.requests[-1].subtype == "Срочное событие"
    assert len(score_plan(updated, result["plan"])) == 9
    assert case.model_dump_json() == before


def test_invalid_or_missing_canonical_type_and_unknown_policy_are_rejected():
    raw = CASES[0].scenario().model_dump()
    for work_type in (None, "installation", "made_up"):
        invalid = json.loads(json.dumps(raw))
        invalid["requests"][0]["work_type"] = work_type
        with pytest.raises(ValidationError):
            Scenario.model_validate(invalid)
    raw["objective_policy"] = "unknown_policy"
    with pytest.raises(ValidationError):
        Scenario.model_validate(raw)


@pytest.mark.parametrize("invalid", ["policy", "type", "missing_type"])
def test_cpp_rejects_unknown_policy_and_invalid_priority_payload(invalid):
    case = CASES[0].scenario()
    source = case.model_dump()
    if invalid == "policy":
        source["objective_policy"] = "unknown_policy"
    elif invalid == "type":
        source["requests"][0]["work_type"] = "made_up"
    else:
        source["requests"][0].pop("work_type")
    payload = {
        "protocol_version": "1.0",
        "mode": "evaluate",
        "scenario": source,
        "input_hash": case.fingerprint(),
        "initial_routes": [[]],
        "options": SEARCH.model_dump(),
    }
    result = subprocess.run(
        [str(solver_binary())], input=json.dumps(payload), text=True, capture_output=True
    )
    assert result.returncode != 0
