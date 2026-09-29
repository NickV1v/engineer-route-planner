"""Small exhaustive reference: enumerate dropped jobs, all assignments and permutations.

No feasibility pruning of incomplete tours: adding a stop can restore reachability
in a directed nonmetric matrix. Intentionally independent of production evaluators.
"""

from dispatch.models import Scenario


def route_score(case: Scenario, routes: list[list[int]]) -> tuple[int, ...] | None:
    assigned = set()
    distance = travel = urgent_delay = waiting = 0
    state = case.planning_state
    policy = state.policy if state else None
    reference = (
        {
            v.request_id: (eid, i, v.start_s)
            for eid, visits in policy.reference_routes.items()
            for i, v in enumerate(visits)
        }
        if policy
        else {}
    )
    actual = {}
    horizon = min((e.shift_start_s for e in case.engineers), default=0)
    for e, route in zip(case.engineers, routes):
        if case.equipment_issued is not None:
            stock = case.equipment_issued[e.id]
            if sum(case.requests[i].kind == "Подключение" for i in route) > stock.routers:
                return None
            if sum(case.requests[i].kind == "Дозаказ" for i in route) > stock.set_top_boxes:
                return None
        if case.roster is not None and e.id not in case.roster and route:
            return None
        if policy and policy.mode == "preserve":
            protected = {
                rid
                for rid, (owner, _, _) in reference.items()
                if owner not in state.unavailable_engineers
            }
            expected = (
                [v.request_id for v in policy.reference_routes.get(e.id, [])]
                if e.id not in state.unavailable_engineers
                else []
            )
            if [
                case.requests[idx].id for idx in route if case.requests[idx].id in protected
            ] != expected:
                return None
            if any(case.requests[idx].id not in policy.eligible_request_ids for idx in route):
                return None
        matrix = case.matrices[e.profile]
        locations = {value: i for i, value in enumerate(matrix.locations)}
        origin = locations[e.start_location_id]
        current = e.shift_start_s
        state = case.planning_state
        fixed = state.frozen_routes.get(e.id, []) if state else []
        if len(route) < len(fixed):
            return None
        for position, idx in enumerate(route):
            r = case.requests[idx]
            if idx in assigned or r.skill not in e.skills or r.zone != e.zone:
                return None
            if r.required_profile and r.required_profile != e.profile:
                return None
            destination = locations[r.location_id]
            leg = matrix.duration_s[origin][destination]
            if leg is None:
                return None
            if position < len(fixed):
                v = fixed[position]
                if v.request_id != r.id or v.arrival_s - leg < max(current, r.released_at_s):
                    return None
                start = v.start_s
                arrival = v.arrival_s
                if start < max(v.arrival_s, r.window_start_s):
                    return None
                if (v.start_s, v.finish_s, v.travel_s, v.waiting_s, v.distance_m) != (
                    start,
                    start + r.service_s,
                    leg,
                    start - v.arrival_s,
                    matrix.distance_m[origin][destination],
                ):
                    return None
            else:
                if state and e.id in state.unavailable_engineers:
                    return None
                departure = max(current, r.released_at_s, state.at_s if state else 0)
                arrival = departure + leg
                start = max(arrival, r.window_start_s)
                if r.id in reference:
                    owner, _, old_start = reference[r.id]
                    start = max(start, old_start)
                    if (
                        policy.mode == "preserve"
                        and owner not in state.unavailable_engineers
                        and start > old_start + policy.max_delay_s
                    ):
                        return None
                if position == 0 and case.schedule_policy == "compact_v1":
                    arrival = start
            waiting += start - arrival
            current = start + r.service_s
            if start > r.window_end_s or current > e.shift_end_s:
                return None
            distance += matrix.distance_m[origin][destination]
            travel += leg
            if r.work_type == "emergency" if case.objective_policy is not None else r.urgent:
                urgent_delay += start - max(r.window_start_s, horizon, r.released_at_s)
            assigned.add(idx)
            actual[r.id] = (e.id, position, start)
            origin = destination
    if case.roster is not None:
        changed = sum(
            owner not in state.unavailable_engineers
            and (rid not in actual or actual[rid][0] != owner)
            for rid, (owner, _, _) in reference.items()
        )
        reordered = sum(
            actual[a][1] > actual[b][1]
            for a, (ea, ia, _) in reference.items()
            for b, (eb, ib, _) in reference.items()
            if ea == eb
            and ia < ib
            and a in actual
            and b in actual
            and actual[a][0] == ea
            and actual[b][0] == eb
        )
        shifts = [
            abs(actual[rid][2] - start) for rid, (_, _, start) in reference.items() if rid in actual
        ]
        result = (
            len(case.requests) - len(assigned),
            sum(r.urgent and i not in assigned for i, r in enumerate(case.requests)),
            changed,
            reordered,
            sum(s > 0 for s in shifts),
            sum(shifts),
            urgent_delay,
            distance,
            travel,
        )
    else:
        result = (
            len(case.requests) - len(assigned),
            sum(bool(route) for route in routes),
            sum(r.urgent and i not in assigned for i, r in enumerate(case.requests)),
            urgent_delay,
            distance,
            travel,
        )
    if case.objective_policy == "work_type_priority_v1":
        missing = [r.work_type for i, r in enumerate(case.requests) if i not in assigned]
        prefix = (
            missing.count("emergency"),
            missing.count("installation"),
            sum(t not in ("emergency", "installation") for t in missing),
        )
        result = prefix + (result[2:] if case.roster is not None else (result[1], *result[3:]))
    if case.schedule_policy == "compact_v1":
        result = (*result[:-2], travel + waiting, *result[-2:])
    return result


def optimum(case: Scenario, scorer=route_score) -> tuple[int, ...]:
    if len(case.requests) > 6 or len(case.engineers) > 2:
        raise ValueError("Exhaustive oracle is limited to 6 jobs / 2 engineers")
    routes = [[] for _ in case.engineers]
    best = scorer(case, routes)

    def visit(idx):
        nonlocal best
        if idx == len(case.requests):
            score = scorer(case, routes)
            if score is not None and (best is None or score < best):
                best = score
            return
        visit(idx + 1)  # This request may remain unassigned.
        for route in routes:
            for position in range(len(route) + 1):
                route.insert(position, idx)
                visit(idx + 1)
                route.pop(position)

    visit(0)
    assert best is not None, "No feasible assignment, even with all non-frozen jobs dropped"
    return best
