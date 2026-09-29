from dispatch.models import Plan, Rejection, Scenario
from dispatch.scheduling import append_visit, build_plan, frozen_routes

REASONS = {
    "zone": "Инженер относится к другой зоне",
    "skill": "Нет требуемого навыка",
    "transport": "Не подходит транспорт",
    "equipment_routers": "Не хватает выданных роутеров с учётом остальных работ инженера",
    "equipment_set_top_boxes": "Не хватает выданных ТВ-приставок с учётом остальных работ инженера",
    "unreachable": "Нет доступного пути",
    "window": "Начало работы выходит за клиентское окно",
    "shift": "Работа заканчивается после смены",
    "unavailable": "Инженер недоступен для новых назначений",
    "off_shift": "Инженер не входит в утверждённую смену",
    "outside_event": "Заявка не затронута событием",
    "fixed_engineer": "Назначение другому инженеру закреплено",
    "schedule_change": "Превышен допустимый сдвиг расписания",
}


def solve_baseline(scenario: Scenario) -> Plan:
    """Input-order, first-feasible, append-only scheduling after a frozen prefix."""
    routes = frozen_routes(scenario)
    assigned = {v.request_id for route in routes.values() for v in route.visits}
    requests = {r.id: r for r in scenario.requests}
    indices = {
        profile: {loc: i for i, loc in enumerate(matrix.locations)}
        for profile, matrix in scenario.matrices.items()
    }
    rejections = []
    state = scenario.planning_state
    if state and state.policy and state.policy.mode == "preserve":
        engineers = {e.id: e for e in scenario.engineers}
        for eid, visits in state.policy.reference_routes.items():
            if eid in state.unavailable_engineers:
                continue
            for old in visits:
                if old.request_id in assigned:
                    continue
                visit, reason = append_visit(
                    scenario,
                    engineers[eid],
                    routes[eid],
                    requests[old.request_id],
                    requests,
                    indices,
                )
                if visit is None:
                    raise ValueError(
                        "Не удалось сохранить расписание: "
                        + REASONS[reason]
                        + ". Рассчитайте вариант с перестановками."
                    )
                routes[eid].visits.append(visit)
                routes[eid].distance_m += visit.distance_m
                assigned.add(old.request_id)
    for request in sorted(scenario.requests, key=lambda r: r.source_order):
        if request.id in assigned:
            continue
        checks = {}
        for engineer in scenario.engineers:
            route = routes[engineer.id]
            visit, reason = append_visit(scenario, engineer, route, request, requests, indices)
            if visit:
                route.visits.append(visit)
                route.distance_m += visit.distance_m
                break
            checks[engineer.id] = reason
        else:
            reasons = set(checks.values())
            if not checks:
                reason, explanation = "no_engineers", "В сценарии нет инженеров."
            elif len(reasons) == 1:
                reason = next(iter(reasons))
                explanation = REASONS[reason] + " у всех проверенных инженеров."
            else:
                reason = "no_feasible_append"
                explanation = "Baseline не нашёл допустимого добавления в конец маршрута. Это не доказательство невозможности другого распределения."
            rejections.append(
                Rejection(
                    request_id=request.id, reason=reason, explanation=explanation, checks=checks
                )
            )
    return build_plan(scenario, list(routes.values()), rejections)
