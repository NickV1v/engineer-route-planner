"""Independent validation; deliberately does not call baseline scheduling helpers."""

from collections import Counter

from dispatch.models import Plan, Scenario


def check_plan(scenario: Scenario, plan: Plan) -> list[str]:
    errors = []
    if plan.scenario_id != scenario.id or plan.input_hash != scenario.fingerprint():
        errors.append("План относится к другой версии входа")
    requests = {r.id: r for r in scenario.requests}
    engineers = {e.id: e for e in scenario.engineers}
    route_ids = [r.engineer_id for r in plan.routes]
    if Counter(route_ids) != Counter(engineers.keys()):
        errors.append("Состав маршрутов не совпадает с составом инженеров")
    covered = [r.request_id for r in plan.unassigned]
    totals = {
        "total": len(requests),
        "assigned": 0,
        "unassigned": len(plan.unassigned),
        "active_engineers": 0,
        "distance_m": 0,
        "travel_s": 0,
        "waiting_s": 0,
        "service_s": 0,
    }
    for route in plan.routes:
        engineer = engineers.get(route.engineer_id)
        if engineer is None:
            errors.append(f"Неизвестный инженер: {route.engineer_id}")
            continue
        location, available = engineer.start_location_id, engineer.shift_start_s
        route_distance = 0
        routers = boxes = 0
        totals["active_engineers"] += bool(route.visits)
        matrix = scenario.matrices[engineer.profile]
        index = {loc: i for i, loc in enumerate(matrix.locations)}
        state = scenario.planning_state
        frozen = state.frozen_routes.get(engineer.id, []) if state else []
        policy = state.policy if state else None
        reference = (
            {v.request_id: v for vs in policy.reference_routes.values() for v in vs}
            if policy
            else {}
        )
        if scenario.roster is not None and engineer.id not in scenario.roster and route.visits:
            errors.append(f"{engineer.id}: не входит в утверждённую смену")
        if policy and policy.mode == "preserve":
            expected_ids = (
                [v.request_id for v in policy.reference_routes.get(engineer.id, [])]
                if engineer.id not in state.unavailable_engineers
                else []
            )
            protected = {
                v.request_id: eid
                for eid, vs in policy.reference_routes.items()
                if eid not in state.unavailable_engineers
                for v in vs
            }
            if [v.request_id for v in route.visits if v.request_id in protected] != expected_ids:
                errors.append(f"{engineer.id}: нарушено сохранение назначений или порядка")
            if any(v.request_id not in policy.eligible_request_ids for v in route.visits):
                errors.append(f"{engineer.id}: назначена заявка вне события")
        if route.visits[: len(frozen)] != frozen:
            errors.append(f"{engineer.id}: изменена закреплённая часть маршрута")
        if state and engineer.id in state.unavailable_engineers and len(route.visits) > len(frozen):
            errors.append(f"{engineer.id}: новые работы у недоступного инженера")
        for position, visit in enumerate(route.visits):
            covered.append(visit.request_id)
            request = requests.get(visit.request_id)
            if request is None:
                errors.append(f"Неизвестная заявка: {visit.request_id}")
                continue
            label = f"{engineer.id}/{request.id}"
            # Count the entire route, including frozen visits: their devices are committed.
            routers += request.kind == "Подключение"
            boxes += request.kind == "Дозаказ"
            if request.zone != engineer.zone or request.skill not in engineer.skills:
                errors.append(f"{label}: зона или навык")
            if request.required_profile and request.required_profile != engineer.profile:
                errors.append(f"{label}: транспорт")
            i, j = index[location], index[request.location_id]
            travel, distance = matrix.duration_s[i][j], matrix.distance_m[i][j]
            if travel is None:
                errors.append(f"{label}: недостижимое ребро")
                continue
            if position < len(frozen):
                # Historical pauses from previous events must be retained verbatim.
                departure = visit.arrival_s - travel
                if departure < max(available, request.released_at_s):
                    errors.append(f"{label}: невозможный выезд в истории")
                arrival = visit.arrival_s
            else:
                arrival = max(available, state.at_s if state else 0, request.released_at_s) + travel
            if position < len(frozen):
                start = visit.start_s
                if start < max(arrival, request.window_start_s):
                    errors.append(f"{label}: невозможное начало работы в истории")
            else:
                previous = reference.get(request.id)
                start = max(arrival, request.window_start_s, previous.start_s if previous else 0)
                if scenario.schedule_policy == "compact_v1" and position == 0:
                    arrival = start
                if (
                    policy
                    and policy.mode == "preserve"
                    and previous
                    and request.id in protected
                    and start > previous.start_s + policy.max_delay_s
                ):
                    errors.append(f"{label}: превышен допустимый сдвиг расписания")
            finish = start + request.service_s
            expected = (arrival, start, finish, travel, start - arrival, distance)
            actual = (
                visit.arrival_s,
                visit.start_s,
                visit.finish_s,
                visit.travel_s,
                visit.waiting_s,
                visit.distance_m,
            )
            if actual != expected:
                errors.append(f"{label}: неверный расчёт расписания или расстояния")
            if start > request.window_end_s or finish > engineer.shift_end_s:
                errors.append(f"{label}: нарушено окно или смена")
            location, available = request.location_id, finish
            route_distance += distance
            totals["assigned"] += 1
            totals["travel_s"] += travel
            totals["waiting_s"] += start - arrival
            totals["service_s"] += request.service_s
        if route.distance_m != route_distance:
            errors.append(f"{engineer.id}: неверный пробег")
        if scenario.equipment_issued is not None:
            issued = scenario.equipment_issued[engineer.id]
            if routers > issued.routers:
                errors.append(
                    f"{engineer.id}: требуется {routers} роутеров, выдано {issued.routers}"
                )
            if boxes > issued.set_top_boxes:
                errors.append(
                    f"{engineer.id}: требуется {boxes} ТВ-приставок, выдано {issued.set_top_boxes}"
                )
        totals["distance_m"] += route_distance
    if Counter(covered) != Counter(requests.keys()):
        errors.append("Заявки потеряны, дублируются или отсутствуют во входе")
    if plan.metrics.model_dump() != totals:
        errors.append("Метрики не совпадают с пересчётом")
    return errors


def require_valid_plan(scenario: Scenario, plan: Plan) -> Plan:
    errors = check_plan(scenario, plan)
    if errors:
        raise ValueError("План не прошёл независимую проверку: " + "; ".join(errors))
    return plan
