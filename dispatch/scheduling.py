"""Schedule construction helpers. The checker intentionally does not import these."""

from itertools import chain

from dispatch.equipment import equipment_required
from dispatch.models import Engineer, Metrics, Plan, Rejection, Request, Route, Scenario, Visit


def frozen_routes(scenario: Scenario) -> dict[str, Route]:
    state = scenario.planning_state
    return {
        e.id: Route(
            engineer_id=e.id,
            visits=[v.model_copy(deep=True) for v in state.frozen_routes.get(e.id, [])]
            if state
            else [],
            distance_m=sum(v.distance_m for v in state.frozen_routes.get(e.id, [])) if state else 0,
        )
        for e in scenario.engineers
    }


def append_visit(
    scenario: Scenario,
    engineer: Engineer,
    route: Route,
    request: Request,
    requests: dict[str, Request],
    indices: dict,
) -> tuple[Visit | None, str]:
    state = scenario.planning_state
    if scenario.roster is not None and engineer.id not in scenario.roster:
        return None, "off_shift"
    if state and engineer.id in state.unavailable_engineers:
        return None, "unavailable"
    policy = state.policy if state else None
    reference = (
        {v.request_id: (eid, v) for eid, visits in policy.reference_routes.items() for v in visits}
        if policy
        else {}
    )
    prior = reference.get(request.id)
    if policy and policy.mode == "preserve":
        if request.id not in policy.eligible_request_ids:
            return None, "outside_event"
        if prior and prior[0] not in state.unavailable_engineers and prior[0] != engineer.id:
            return None, "fixed_engineer"
    if engineer.zone != request.zone:
        return None, "zone"
    if request.skill not in engineer.skills:
        return None, "skill"
    if request.required_profile is not None and request.required_profile != engineer.profile:
        return None, "transport"
    if scenario.equipment_issued is not None:
        required = equipment_required(
            chain((requests[v.request_id] for v in route.visits), [request])
        )
        issued = scenario.equipment_issued[engineer.id]
        if required.routers > issued.routers:
            return None, "equipment_routers"
        if required.set_top_boxes > issued.set_top_boxes:
            return None, "equipment_set_top_boxes"
    previous = route.visits[-1] if route.visits else None
    origin = requests[previous.request_id].location_id if previous else engineer.start_location_id
    departure = max(
        previous.finish_s if previous else engineer.shift_start_s,
        state.at_s if state else 0,
        request.released_at_s,
    )
    matrix = scenario.matrices[engineer.profile]
    i, j = indices[engineer.profile][origin], indices[engineer.profile][request.location_id]
    travel, distance = matrix.duration_s[i][j], matrix.distance_m[i][j]
    if travel is None:
        return None, "unreachable"
    arrival = departure + travel
    start = max(arrival, request.window_start_s, prior[1].start_s if prior else 0)
    if scenario.schedule_policy == "compact_v1" and previous is None:
        # Stay at the starting location until it is time to reach the first client.
        arrival = start
    finish = start + request.service_s
    if start > request.window_end_s:
        return None, "window"
    if finish > engineer.shift_end_s:
        return None, "shift"
    if (
        policy
        and policy.mode == "preserve"
        and prior
        and prior[0] not in state.unavailable_engineers
        and start > prior[1].start_s + policy.max_delay_s
    ):
        return None, "schedule_change"
    return Visit(
        request_id=request.id,
        arrival_s=arrival,
        start_s=start,
        finish_s=finish,
        travel_s=travel,
        waiting_s=start - arrival,
        distance_m=distance,
    ), ""


def build_plan(scenario: Scenario, routes: list[Route], rejections: list[Rejection]) -> Plan:
    requests = {r.id: r for r in scenario.requests}
    visits = [v for route in routes for v in route.visits]
    return Plan(
        scenario_id=scenario.id,
        input_hash=scenario.fingerprint(),
        routes=routes,
        unassigned=rejections,
        metrics=Metrics(
            total=len(requests),
            assigned=len(visits),
            unassigned=len(rejections),
            active_engineers=sum(bool(r.visits) for r in routes),
            distance_m=sum(v.distance_m for v in visits),
            travel_s=sum(v.travel_s for v in visits),
            waiting_s=sum(v.waiting_s for v in visits),
            service_s=sum(requests[v.request_id].service_s for v in visits),
        ),
    )


def repair_previous_plan(scenario: Scenario, previous: Plan) -> Plan:
    """Keep future assignments where feasible, without trusting old visit timestamps."""
    routes = frozen_routes(scenario)
    assigned = {v.request_id for route in routes.values() for v in route.visits}
    requests = {r.id: r for r in scenario.requests}
    indices = {
        p: {loc: i for i, loc in enumerate(m.locations)} for p, m in scenario.matrices.items()
    }
    engineers = {e.id: e for e in scenario.engineers}
    for old_route in previous.routes:
        route = routes[old_route.engineer_id]
        for old_visit in old_route.visits:
            if old_visit.request_id in assigned or old_visit.request_id not in requests:
                continue
            visit, _ = append_visit(
                scenario,
                engineers[route.engineer_id],
                route,
                requests[old_visit.request_id],
                requests,
                indices,
            )
            if visit:
                route.visits.append(visit)
                route.distance_m += visit.distance_m
                assigned.add(visit.request_id)
    rejections = [
        Rejection(
            request_id=r.id,
            reason="pending_search",
            explanation="Ожидает перепланирования.",
            checks={},
        )
        for r in scenario.requests
        if r.id not in assigned
    ]
    return build_plan(scenario, list(routes.values()), rejections)
