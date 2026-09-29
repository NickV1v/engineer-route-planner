import json
import random
import subprocess

import pytest
from conftest import ROOT, engineer, request, scenario
from oracle import optimum
from pydantic import ValidationError

from dispatch.baseline import solve_baseline
from dispatch.checker import check_plan
from dispatch.models import Scenario
from dispatch.objective import score_plan
from dispatch.optimizer import (
    SLOTS,
    SearchOptions,
    SolverError,
    invoke_solver,
    optimize,
    solver_binary,
)
from dispatch.scenarios import make_scenario

OPTIONS = SearchOptions(time_limit_ms=0, rounds=8, max_evaluations=100_000)


def test_cpp_evaluator_matches_hand_calculated_fixture():
    case = Scenario.model_validate_json((ROOT / "tests/fixtures/small_scenario.json").read_text())
    baseline = solve_baseline(case)
    output = invoke_solver(case, baseline, OPTIONS, mode="evaluate")
    assert output.plan.routes == baseline.routes
    assert output.plan.metrics == baseline.metrics
    result = optimize(case, OPTIONS)
    assert score_plan(case, result["plan"]) == optimum(case)
    assert result["plan"].metrics.active_engineers == 1


def test_insertion_recovers_early_job_that_baseline_loses():
    case = scenario(
        [
            request("late", source_order=0, window_start_s=50000, window_end_s=55000),
            request("early", source_order=1),
        ]
    )
    result = optimize(case, OPTIONS)
    assert result["baseline"].metrics.assigned == 1
    assert result["plan"].metrics.assigned == 2
    assert [v.request_id for v in result["plan"].routes[0].visits] == ["early", "late"]


@pytest.mark.parametrize("arrival,shift_end", [(43200, 47400), (43201, 61200), (43200, 47399)])
def test_cpp_time_boundaries(arrival, shift_end):
    case = scenario(
        engineers=[engineer(shift_end_s=shift_end)],
        edges={("office", "A"): (arrival - 32400, 1000)},
    )
    assert score_plan(case, optimize(case, OPTIONS)["plan"]) == optimum(case)


@pytest.mark.parametrize(
    "case",
    [
        scenario(requests=[]),
        scenario(engineers=[]),
        scenario([request(required_profile="bicycle")]),
        scenario(edges={("office", "A"): (None, None)}),
    ],
)
def test_empty_and_incompatible_cases(case):
    result = optimize(case, OPTIONS)
    assert score_plan(case, result["plan"]) == optimum(case)


def test_budget_exhaustion_keeps_checked_incumbent():
    case = scenario()
    for options in [SearchOptions(max_evaluations=0), SearchOptions(rounds=0)]:
        result = optimize(case, options)
        assert result["plan"].routes == result["baseline"].routes
        assert result["search"].improvements == 0
        assert result["search"].status in ("evaluation_limit", "iteration_limit")


def test_reproducibility_and_non_regression_on_real_synthetic_sources(imported, policy):
    for item in imported:
        case = make_scenario(item, policy)
        a, b = optimize(case, OPTIONS), optimize(case, OPTIONS)
        assert a["plan"] == b["plan"]
        assert a["search"].model_dump(exclude={"elapsed_ms"}) == b["search"].model_dump(
            exclude={"elapsed_ms"}
        )
        assert check_plan(case, a["plan"]) == []
        assert score_plan(case, a["plan"]) <= score_plan(case, a["baseline"])


def random_case(seed):
    rng = random.Random(seed)
    requests = []
    for i in range(5):
        start = rng.randrange(0, 3000)
        requests.append(
            request(
                str(i),
                source_order=i,
                window_start_s=start,
                window_end_s=start + rng.randrange(300, 1500),
                service_s=rng.randrange(300, 1000),
                urgent=rng.random() < 0.3,
                skill=rng.choice(["installation", "local"]),
            )
        )
    people = [
        engineer(shift_start_s=0, shift_end_s=4000),
        engineer("E2", shift_start_s=1000, shift_end_s=6000, skills=["installation", "local"]),
    ]
    edges = {}
    locations = ["office"] + [r.location_id for r in requests]
    for a in locations:
        for b in locations:
            if a != b:
                edges[(a, b)] = (
                    (None, None)
                    if rng.random() < 0.12
                    else (rng.randrange(100, 900), rng.randrange(100, 1000))
                )
    return scenario(requests, people, edges)


def test_random_small_cases_against_exhaustive_oracle(record_property):
    hits = 0
    for seed in range(20):
        case = random_case(seed)
        exact = optimum(case)
        result = optimize(case, OPTIONS)
        score = score_plan(case, result["plan"])
        assert exact <= score <= score_plan(case, result["baseline"])
        assert check_plan(case, result["plan"]) == []
        hits += score == exact
        evaluated = invoke_solver(case, result["baseline"], OPTIONS, mode="evaluate")
        assert evaluated.plan.routes == result["baseline"].routes
    record_property("exact_optimum_matches", f"{hits}/20")


@pytest.mark.parametrize("failure", ["timeout", "crash", "empty", "malformed", "corrupt", "worse"])
def test_bad_process_output_is_rejected(monkeypatch, failure):
    case = scenario()
    valid = optimize(case, OPTIONS)
    payload = {
        "protocol_version": "1.0",
        "plan": valid["plan"].model_dump(),
        "score": list(score_plan(case, valid["plan"])),
        "search": valid["search"].model_dump(),
    }
    if failure == "corrupt":
        payload["plan"]["routes"][0]["visits"][0]["finish_s"] -= 1
    if failure == "worse":
        # A feasible empty plan is still forbidden when the baseline assigned a job.
        empty = solve_baseline(case.model_copy(update={"engineers": []}))
        payload["plan"]["routes"][0]["visits"] = []
        payload["plan"]["routes"][0]["distance_m"] = 0
        payload["plan"]["unassigned"] = [r.model_dump() for r in empty.unassigned]
        payload["plan"]["metrics"] = empty.metrics.model_dump()
        payload["score"] = [1, 0, 0, 0, 0, 0]

    def fake_run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("solver", 1)
        output = (
            ""
            if failure == "empty"
            else "no json"
            if failure == "malformed"
            else json.dumps(payload)
        )
        return subprocess.CompletedProcess(
            args, 2 if failure == "crash" else 0, stdout=output, stderr="test failure"
        )

    monkeypatch.setattr("dispatch.optimizer.subprocess.run", fake_run)
    with pytest.raises(SolverError):
        optimize(case, OPTIONS)


def test_busy_solver_is_not_spawned():
    assert SLOTS.acquire(blocking=False)
    assert SLOTS.acquire(blocking=False)
    try:
        with pytest.raises(SolverError, match="Два расчёта"):
            optimize(scenario(), OPTIONS)
    finally:
        SLOTS.release()
        SLOTS.release()


def test_binary_rejects_invalid_routes_without_crashing():
    case = scenario()
    body = {
        "protocol_version": "1.0",
        "scenario": case.model_dump(),
        "input_hash": case.fingerprint(),
        "options": OPTIONS.model_dump(),
        "mode": "evaluate",
        "initial_routes": [["A", "A"]],
    }
    result = subprocess.run(
        [str(solver_binary())], input=json.dumps(body), text=True, capture_output=True
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "Infeasible" in json.loads(result.stderr)["error"]


def test_contract_rejects_fractional_time_and_overflow():
    values = scenario().model_dump()
    values["requests"][0]["window_start_s"] = 3.5
    with pytest.raises(ValidationError):
        Scenario.model_validate(values)
    values = scenario().model_dump()
    values["matrices"]["car"]["distance_m"][0][1] = 2**63
    with pytest.raises(ValidationError):
        Scenario.model_validate(values)
