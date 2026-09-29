"""Separate policies for staffing before publication and stability during the day."""

from dispatch.models import Plan, Scenario
from dispatch.work_types import PRIORITY_POLICY, unassigned_by_priority

OBJECTIVE_VERSION = "coverage_staff_urgency_distance_v1"
OBJECTIVE_COMPONENTS = [
    "unassigned",
    "active_engineers",
    "urgent_unassigned",
    "urgent_delay_s",
    "distance_m",
    "travel_s",
]
DAY_COMPONENTS = [
    "unassigned",
    "urgent_unassigned",
    "changed_assignments",
    "reordered_pairs",
    "shifted_visits",
    "schedule_shift_s",
    "urgent_delay_s",
    "distance_m",
    "travel_s",
]
PRIORITY_COMPONENTS = [
    "emergency_unassigned",
    "installation_unassigned",
    "regular_unassigned",
    "active_engineers",
    "emergency_delay_s",
    "distance_m",
    "travel_s",
]
PRIORITY_DAY_COMPONENTS = [*PRIORITY_COMPONENTS[:3], *DAY_COMPONENTS[2:6], *PRIORITY_COMPONENTS[4:]]


def objective_version(scenario: Scenario) -> str:
    if scenario.objective_policy == PRIORITY_POLICY:
        version = "work_type_day_stability_v1" if scenario.roster is not None else PRIORITY_POLICY
    else:
        version = "day_stability_v1" if scenario.roster is not None else OBJECTIVE_VERSION
    return version + "+compact_v1" if scenario.schedule_policy == "compact_v1" else version


def objective_components(scenario: Scenario) -> list[str]:
    if scenario.objective_policy == PRIORITY_POLICY:
        names = PRIORITY_DAY_COMPONENTS if scenario.roster is not None else PRIORITY_COMPONENTS
    else:
        names = DAY_COMPONENTS if scenario.roster is not None else OBJECTIVE_COMPONENTS
    return (
        [*names[:-2], "travel_wait_s", *names[-2:]]
        if scenario.schedule_policy == "compact_v1"
        else list(names)
    )


def stability_metrics(scenario: Scenario, plan: Plan) -> tuple[int, int, int, int]:
    state = scenario.planning_state
    if not state or not state.policy:
        return (0, 0, 0, 0)
    actual = {v.request_id: (r.engineer_id, v.start_s) for r in plan.routes for v in r.visits}
    changed = reordered = shifted = delay = 0
    routes = {r.engineer_id: r for r in plan.routes}
    for engineer, visits in state.policy.reference_routes.items():
        for visit in visits:
            assignment = actual.get(visit.request_id)
            if engineer not in state.unavailable_engineers and (
                assignment is None or assignment[0] != engineer
            ):
                changed += 1
            if assignment:
                delta = abs(assignment[1] - visit.start_s)
                shifted += delta > 0
                delay += delta
        ranks = {v.request_id: i for i, v in enumerate(routes[engineer].visits)}
        remaining = [ranks[v.request_id] for v in visits if v.request_id in ranks]
        reordered += sum(a > b for i, a in enumerate(remaining) for b in remaining[i + 1 :])
    return changed, reordered, shifted, delay


def score_plan(scenario: Scenario, plan: Plan) -> tuple[int, ...]:
    requests = {r.id: r for r in scenario.requests}
    visits = [v for route in plan.routes for v in route.visits]
    assigned = {v.request_id for v in visits}
    horizon = min((e.shift_start_s for e in scenario.engineers), default=0)
    unassigned = len(scenario.requests) - len(visits)
    urgent_unassigned = sum(r.urgent and r.id not in assigned for r in scenario.requests)
    urgent_delay = sum(
        v.start_s
        - max(requests[v.request_id].window_start_s, horizon, requests[v.request_id].released_at_s)
        for v in visits
        if (
            requests[v.request_id].work_type == "emergency"
            if scenario.objective_policy == PRIORITY_POLICY
            else requests[v.request_id].urgent
        )
    )
    distance, travel = sum(v.distance_m for v in visits), sum(v.travel_s for v in visits)
    efficiency = (
        (travel + sum(v.waiting_s for v in visits),)
        if scenario.schedule_policy == "compact_v1"
        else ()
    )
    if scenario.objective_policy == PRIORITY_POLICY:
        coverage = unassigned_by_priority({r.id: r.work_type for r in scenario.requests}, assigned)
        tie_breakers = (
            stability_metrics(scenario, plan)
            if scenario.roster is not None
            else (sum(bool(r.visits) for r in plan.routes),)
        )
        return (*coverage, *tie_breakers, urgent_delay, *efficiency, distance, travel)
    if scenario.roster is not None:
        return (
            unassigned,
            urgent_unassigned,
            *stability_metrics(scenario, plan),
            urgent_delay,
            *efficiency,
            distance,
            travel,
        )
    return (
        unassigned,
        sum(bool(r.visits) for r in plan.routes),
        urgent_unassigned,
        urgent_delay,
        *efficiency,
        distance,
        travel,
    )


def compare_plans(scenario: Scenario, baseline: Plan, optimized: Plan) -> dict:
    baseline_score = score_plan(scenario, baseline)
    optimized_score = score_plan(scenario, optimized)
    if baseline.input_hash != optimized.input_hash:
        raise ValueError("Нельзя сравнивать планы с разными входами")
    baseline_ids = {v.request_id for r in baseline.routes for v in r.visits}
    optimized_ids = {v.request_id for r in optimized.routes for v in r.visits}
    return {
        "objective_version": objective_version(scenario),
        "objective_components": objective_components(scenario),
        "baseline_score": baseline_score,
        "optimized_score": optimized_score,
        "improved": optimized_score < baseline_score,
        "assigned_delta": optimized.metrics.assigned - baseline.metrics.assigned,
        "engineers_delta": optimized.metrics.active_engineers - baseline.metrics.active_engineers,
        "distance_delta_m": optimized.metrics.distance_m - baseline.metrics.distance_m,
        "same_assigned_set": baseline_ids == optimized_ids,
    }
