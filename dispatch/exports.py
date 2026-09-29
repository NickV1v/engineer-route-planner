"""Human-readable workbooks built only from an accepted, versioned plan."""

import math
import re
from datetime import date, timedelta
from io import BytesIO

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.pagebreak import Break

from dispatch.equipment import equipment_required
from dispatch.geography import location_address
from dispatch.importer import location_id
from dispatch.models import Engineer, Plan, Request, Scenario

PROFILES = {
    "car": "Автомобиль",
    "walk": "Пешком",
    "bicycle": "Велосипед",
    "public_transport_approx": "Общественный транспорт",
}
SKILLS = {
    "installation": "Подключения и дозаказы",
    "local": "Локальные работы",
    "emergency": "Аварийные работы",
}
REASONS = {
    "off_shift": "Не входит в смену",
    "outside_event": "Нужно разрешить перестановки",
    "fixed_engineer": "Заявка закреплена за другим инженером",
    "fixed_order": "Нужно изменить порядок выездов",
    "schedule_change": "Потребуется сдвиг согласованного расписания",
    "unavailable": "Инженер недоступен",
    "skill": "Нет нужной квалификации",
    "transport": "Не подходит транспорт",
    "equipment_routers": "Не хватает выданных роутеров",
    "equipment_set_top_boxes": "Не хватает выданных ТВ-приставок",
    "window": "Не удаётся приехать в окно клиента",
    "shift": "Работа не помещается в смену",
    "unreachable": "Не удалось построить путь до адреса",
    "zone": "Инженер работает в другой зоне",
    "not_selected": "Не удалось включить заявку в найденное расписание",
    "search_budget": "Расчёт завершился до назначения заявки",
    "no_engineers": "Нет доступных инженеров",
    "no_feasible_append": "Нет подходящего времени в конце маршрутов",
    "no_feasible_insertion": "Не удаётся совместить с другими выездами",
}
NAVY, BLUE, INK = "26364B", "2876DC", "26364B"


def engineer_name(engineer: Engineer) -> str:
    legacy = re.search(r":engineer-(\d+)$", engineer.id)
    return engineer.name or (f"Инженер {int(legacy[1])}" if legacy else engineer.id)


def clock(seconds: int) -> str:
    return f"{seconds // 3600:02}:{seconds % 3600 // 60:02}"


def excel_time(seconds: int | None):
    return timedelta(seconds=seconds) if seconds is not None else None


def minutes(seconds: int) -> float:
    return seconds / 60


def address(scenario: Scenario, key: str, original: str) -> str:
    record = scenario.geography.get(key)
    return location_address(record) if record else original


def _text(value: str) -> str:
    return ILLEGAL_CHARACTERS_RE.sub("", value)[:32767]


def _coordinates(scenario, key):
    record = scenario.geography.get(key)
    return (record.point.lat, record.point.lon) if record and record.point else (None, None)


def _printed_address(scenario, key, original):
    label = address(scenario, key, original)
    lat, lon = _coordinates(scenario, key)
    return f"{label}\n{lat:.6f}, {lon:.6f}" if lat is not None else label


def _row(sheet, values):
    """Write all user strings as literal text, including leading '=' and control characters."""
    row = sheet.max_row + 1 if sheet.cell(1, 1).value is not None else 1
    for column, value in enumerate(values, 1):
        cell = sheet.cell(row, column)
        if isinstance(value, str):
            cell.value = _text(value)
            cell.data_type = "s"
        else:
            cell.value = value
        cell.font = Font(name="Arial", size=11, color=INK)
        cell.alignment = Alignment(vertical="top", wrap_text=True)
        if isinstance(value, timedelta):
            cell.number_format = "[h]:mm:ss"
        elif isinstance(value, date):
            cell.number_format = "dd.mm.yyyy"
        elif isinstance(value, float):
            cell.number_format = "0.0"
    return row


def _banner(sheet, text, columns):
    row = _row(sheet, [text])
    sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=columns)
    cell = sheet.cell(row, 1)
    cell.font = Font(name="Arial", size=17, bold=True, color="FFFFFF")
    cell.fill = PatternFill("solid", fgColor=NAVY)
    cell.alignment = Alignment(vertical="center", wrap_text=True)
    width = sum(
        sheet.column_dimensions[get_column_letter(col)].width for col in range(1, columns + 1)
    )
    sheet.row_dimensions[row].height = max(34, math.ceil(len(text) / (width / 1.6)) * 23 + 10)


def _sheet(book, name, widths, *, landscape=True):
    sheet = book.create_sheet(name)
    sheet.sheet_view.showGridLines = False
    for col, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(col)].width = width
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = "landscape" if landscape else "portrait"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.page_margins.left = sheet.page_margins.right = 0.25
    sheet.page_margins.top = sheet.page_margins.bottom = 0.4
    sheet.oddFooter.right.text = "Страница &P из &N"
    sheet.oddFooter.left.text = "Выезд · Плановое расписание · Москва (UTC+3)"
    return sheet


def _table(sheet, headers, rows, *, filterable=True):
    start = _row(sheet, headers)
    for cell in sheet[start]:
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=BLUE)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.row_dimensions[start].height = 34
    for values in rows:
        index = _row(sheet, values)
        height = 30
        for cell in sheet[index]:
            cell.border = Border(bottom=Side(style="hair", color="DCE4EF"))
            if (index - start) % 2:
                cell.fill = PatternFill("solid", fgColor="F5F8FC")
            width = sheet.column_dimensions[cell.column_letter].width
            if isinstance(cell.value, str):
                lines = sum(
                    max(1, math.ceil(len(s) / max(1, width - 2))) for s in cell.value.split("\n")
                )
                height = max(height, min(409, lines * 15 + 8))
        sheet.row_dimensions[index].height = height
    sheet.freeze_panes = f"A{start + 1}"
    sheet.print_title_rows = f"{start}:{start}"
    sheet.print_area = f"A1:{get_column_letter(len(headers))}{sheet.max_row}"
    if filterable:
        sheet.auto_filter.ref = f"A{start}:{get_column_letter(len(headers))}{sheet.max_row}"
    return start


def _rejection(rejection, scenario, job):
    if scenario.geography and not scenario.geography.get(job.location_id, None):
        return "Нужно уточнить адрес"
    if job.location_id in scenario.geography and not scenario.geography[job.location_id].point:
        return "Нужно уточнить адрес"
    codes = set(rejection.checks.values()) - {"off_shift", "unavailable"}
    if codes == {"skill"}:
        return "Нет инженера с нужной квалификацией"
    if codes and codes <= {"skill", "transport"}:
        return "Нет инженера с нужным навыком и транспортом"
    if len(codes) == 1:
        return REASONS.get(next(iter(codes)), "Не удалось включить заявку в расписание")
    return REASONS.get(rejection.reason, "В текущем расписании не нашлось подходящего времени")


def _priority(scenario, job):
    if scenario.objective_policy:
        return {"emergency": "1 · Авария", "installation": "2 · Подключение"}.get(
            job.work_type, "3 · Ремонт / дозаказ"
        )
    return "Срочная" if job.urgent else "Обычная"


def _job_address(scenario, job):
    return address(scenario, job.location_id, job.address)


def _issued(scenario, engineer):
    stock = (scenario.equipment_issued or {}).get(engineer.id)
    return (stock.routers, stock.set_top_boxes) if stock else (None, None)


def _route_rows(scenario, routes, engineers, jobs):
    for route in routes:
        engineer = engineers[route.engineer_id]
        common = [engineer_name(engineer), engineer.id, PROFILES[engineer.profile]]
        start_address = address(scenario, engineer.start_location_id, scenario.office_address)
        yield [
            *common,
            0,
            "Офис",
            start_address,
            None,
            None,
            None,
            None,
            excel_time(route.visits[0].arrival_s - route.visits[0].travel_s),
            0,
            0,
            0,
            0,
            None,
            None,
            None,
            *_coordinates(scenario, engineer.start_location_id),
        ]
        for index, visit in enumerate(route.visits, 1):
            job = jobs[visit.request_id]
            yield [
                *common,
                index,
                job.id,
                _job_address(scenario, job),
                job.kind,
                f"{clock(job.window_start_s)}–{clock(job.window_end_s)}",
                excel_time(visit.arrival_s),
                excel_time(visit.start_s),
                excel_time(visit.finish_s),
                minutes(visit.travel_s),
                visit.distance_m / 1000,
                minutes(visit.waiting_s),
                minutes(job.service_s),
                _priority(scenario, job),
                SKILLS[job.skill],
                job.address,
                *_coordinates(scenario, job.location_id),
            ]


def _metrics(scenario, plan, routes, engineers):
    selected_ids = {r.engineer_id for r in routes}
    route_by_id = {r.engineer_id: r for r in plan.routes}
    roster = set(scenario.roster or selected_ids)
    unavailable = set(
        scenario.planning_state.unavailable_engineers if scenario.planning_state else []
    )
    for engineer in engineers.values():
        if engineer.id not in roster and engineer.id not in selected_ids:
            continue
        route = route_by_id.get(engineer.id)
        visits = route.visits if route else []
        yield [
            engineer_name(engineer),
            engineer.id,
            PROFILES[engineer.profile],
            "Недоступен" if engineer.id in unavailable else "На смене",
            excel_time(engineer.shift_start_s),
            excel_time(engineer.shift_end_s),
            len(visits),
            excel_time(visits[0].arrival_s - visits[0].travel_s) if visits else None,
            excel_time(visits[-1].finish_s) if visits else None,
            (route.distance_m if route else 0) / 1000,
            minutes(sum(v.travel_s for v in visits)),
            minutes(sum(v.waiting_s for v in visits)),
            minutes(sum(v.finish_s - v.start_s for v in visits)),
            *_issued(scenario, engineer),
        ]


def _plan_book(book, scenario, plan, routes, engineers, jobs, partial):
    sheet = _sheet(book, "Сводка", [49, 27, 27])
    _banner(sheet, "План выездов", 3)
    _row(sheet, ["Дата", date.fromisoformat(scenario.date)])
    _row(sheet, ["Часовой пояс", "Москва (UTC+3)"])
    _row(
        sheet,
        ["Офис", address(scenario, location_id(scenario.office_address), scenario.office_address)],
    )
    _row(sheet, ["Объём выгрузки", "Только выбранные маршруты" if partial else "Полный план"])
    _row(sheet, ["Неназначенные и исключённые заявки", "За весь план"])
    _row(sheet, ["Источник", scenario.manifest.get("import", {}).get("source_file", scenario.id)])
    for row in range(2, sheet.max_row + 1):
        sheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=3)
        text = str(sheet.cell(row, 2).value or "")
        sheet.row_dimensions[row].height = max(30, math.ceil(len(text) / 49) * 15 + 8)
    selected_visits = [v for route in routes for v in route.visits]
    all_visits = [v for route in plan.routes for v in route.visits]
    metrics = [
        ["Инженеры с заявками", len(routes), sum(bool(r.visits) for r in plan.routes)],
        ["Назначено заявок", len(selected_visits), len(all_visits)],
        [
            "Расстояние, км",
            sum(r.distance_m for r in routes) / 1000,
            plan.metrics.distance_m / 1000,
        ],
        [
            "В пути, мин",
            minutes(sum(v.travel_s for v in selected_visits)),
            minutes(plan.metrics.travel_s),
        ],
        [
            "Ожидание у клиентов, мин",
            minutes(sum(v.waiting_s for v in selected_visits)),
            minutes(plan.metrics.waiting_s),
        ],
        [
            "Работа на месте, мин",
            minutes(sum(v.finish_s - v.start_s for v in selected_visits)),
            minutes(plan.metrics.service_s),
        ],
        ["Без назначения", None, len(plan.unassigned)],
        ["Исключено перед расчётом", None, len(scenario.manifest.get("excluded_requests", []))],
    ]
    _table(sheet, ["Показатель", "В выгрузке", "В плане"], metrics, filterable=False)
    _row(
        sheet,
        ["Время и расстояние взяты из принятого плана. Фактическое выполнение не подтверждено."],
    )
    sheet.merge_cells(start_row=sheet.max_row, start_column=1, end_row=sheet.max_row, end_column=3)
    sheet.print_area = f"A1:C{sheet.max_row}"
    routes_sheet = _sheet(
        book,
        "Маршруты",
        [24, 26, 22, 7, 26, 48, 20, 18, 15, 15, 15, 14, 12, 15, 14, 22, 28, 48, 14, 14],
    )
    _table(
        routes_sheet,
        [
            "Инженер",
            "ID инженера",
            "Транспорт",
            "№",
            "ID заявки",
            "Адрес",
            "Тип работы",
            "Окно клиента",
            "Прибытие",
            "Начало работы",
            "Отбытие",
            "В пути, мин",
            "До точки, км",
            "Ожидание, мин",
            "Работа, мин",
            "Приоритет",
            "Квалификация",
            "Исходный адрес",
            "Широта",
            "Долгота",
        ],
        _route_rows(scenario, routes, engineers, jobs),
    )
    for row in routes_sheet.iter_rows(min_row=2, min_col=19, max_col=20):
        for cell in row:
            cell.number_format = "0.000000"
    metrics_sheet = _sheet(
        book, "Инженеры", [24, 26, 24, 18, 15, 15, 10, 15, 15, 14, 14, 16, 14, 17, 20]
    )
    _table(
        metrics_sheet,
        [
            "Инженер",
            "ID инженера",
            "Транспорт",
            "Статус",
            "Начало смены",
            "Конец смены",
            "Заявок",
            "Выезд из офиса",
            "Конец маршрута",
            "Путь, км",
            "В пути, мин",
            "Ожидание, мин",
            "Работа, мин",
            "Выдано роутеров",
            "Выдано ТВ-приставок",
        ],
        _metrics(
            scenario,
            plan,
            routes,
            {
                key: engineer
                for key, engineer in engineers.items()
                if not partial or key in {route.engineer_id for route in routes}
            },
        ),
    )
    rejected = _sheet(book, "Без назначения", [27, 48, 22, 18, 14, 24, 45, 65])
    _table(
        rejected,
        [
            "ID заявки",
            "Адрес",
            "Тип работы",
            "Окно клиента",
            "Работа, мин",
            "Квалификация",
            "Причина",
            "Проверки по инженерам",
        ],
        (
            [
                job.id,
                _job_address(scenario, job),
                job.kind,
                f"{clock(job.window_start_s)}–{clock(job.window_end_s)}",
                minutes(job.service_s),
                SKILLS[job.skill],
                _rejection(item, scenario, job),
                "\n".join(
                    f"{engineer_name(engineers[eid]) if eid in engineers else eid}: {REASONS.get(code, 'Не удалось назначить')}"
                    for eid, code in item.checks.items()
                ),
            ]
            for item in plan.unassigned
            for job in [jobs[item.request_id]]
        ),
    )
    excluded = _sheet(book, "Исключённые", [27, 48, 22, 18, 50])
    _table(
        excluded,
        ["ID заявки", "Адрес из файла", "Тип работы", "Окно клиента", "Причина"],
        (
            [
                j.id,
                j.address,
                j.kind,
                f"{clock(j.window_start_s)}–{clock(j.window_end_s)}",
                "Исключена диспетчером при уточнении адресов",
            ]
            for raw in scenario.manifest.get("excluded_requests", [])
            for j in [Request.model_validate(raw)]
        ),
    )


def _waybills(book, scenario, routes, engineers, jobs):
    for number, route in enumerate(routes, 1):
        engineer = engineers[route.engineer_id]
        name = re.sub(r"[\\/*?:\[\]]", "_", _text(engineer_name(engineer))).strip("'")
        sheet = _sheet(
            book, f"{number:02} {name}"[:31], [5, 22, 43, 15, 11, 11, 11, 11, 10, 12, 17]
        )
        _banner(sheet, f"Маршрутный лист · {engineer_name(engineer)}", 11)
        issued_routers, issued_boxes = _issued(scenario, engineer)
        for left, right in [
            [
                f"Дата: {date.fromisoformat(scenario.date):%d.%m.%Y}",
                "Транспорт: " + PROFILES[engineer.profile],
            ],
            [
                f"Смена: {clock(engineer.shift_start_s)}–{clock(engineer.shift_end_s)}",
                "ID инженера: " + engineer.id,
            ],
            [
                f"Выезд: {clock(route.visits[0].arrival_s - route.visits[0].travel_s)}",
                f"Конец маршрута: {clock(route.visits[-1].finish_s)} · Путь: {route.distance_m / 1000:.1f} км",
            ],
            [
                f"Выдано роутеров: {issued_routers if issued_routers is not None else '—'}",
                f"Выдано ТВ-приставок: {issued_boxes if issued_boxes is not None else '—'}",
            ],
        ]:
            row = _row(sheet, [left, None, None, right])
            sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=3)
            sheet.merge_cells(start_row=row, start_column=4, end_row=row, end_column=11)
            sheet.row_dimensions[row].height = max(
                26, math.ceil(len(left) / 60) * 15 + 8, math.ceil(len(right) / 85) * 15 + 8
            )
        rows = [
            [
                0,
                "Офис",
                _printed_address(scenario, engineer.start_location_id, scenario.office_address),
                None,
                None,
                None,
                excel_time(route.visits[0].arrival_s - route.visits[0].travel_s),
                None,
                None,
                None,
                None,
            ]
        ]
        for index, visit in enumerate(route.visits, 1):
            job = jobs[visit.request_id]
            stock = equipment_required([job]) if scenario.equipment_policy else None
            stock_text = (
                ", ".join(
                    value
                    for value in [
                        f"Роутер: {stock.routers}" if stock and stock.routers else "",
                        f"ТВ-приставка: {stock.set_top_boxes}"
                        if stock and stock.set_top_boxes
                        else "",
                    ]
                    if value
                )
                or "—"
            )
            rows.append(
                [
                    index,
                    f"{job.id}\n{job.kind}",
                    _printed_address(scenario, job.location_id, job.address),
                    f"{clock(job.window_start_s)}–{clock(job.window_end_s)}",
                    excel_time(visit.arrival_s),
                    excel_time(visit.start_s),
                    excel_time(visit.finish_s),
                    minutes(visit.travel_s),
                    visit.distance_m / 1000,
                    minutes(visit.waiting_s),
                    stock_text,
                ]
            )
        header = _table(
            sheet,
            [
                "№",
                "Заявка / работа",
                "Адрес",
                "Окно клиента",
                "Прибытие",
                "Начало",
                "Отбытие",
                "Дорога, мин",
                "Путь, км",
                "Ожидание, мин",
                "Оборудование",
            ],
            rows,
            filterable=False,
        )
        sheet.print_title_rows = f"1:{header}"
        # Keep each stop on one page and repeat the route identity when printing.
        height = sum(sheet.row_dimensions[r].height or 15 for r in range(1, header + 1))
        page_height = height
        for row in range(header + 1, sheet.max_row + 1):
            row_height = sheet.row_dimensions[row].height or 30
            if page_height + row_height > 510:
                sheet.row_breaks.append(Break(id=row - 1))
                page_height = height
            page_height += row_height


def build_workbook(
    scenario: Scenario, response: dict, kind: str, engineer_ids: list[str] | None = None
) -> bytes:
    plan = Plan.model_validate(response["plan"])
    routes = [route for route in plan.routes if route.visits]
    engineers = {e.id: e for e in scenario.engineers}
    if engineer_ids is not None:
        selected = set(engineer_ids)
        if (
            not selected
            or len(selected) != len(engineer_ids)
            or not selected.issubset({r.engineer_id for r in routes})
        ):
            raise ValueError(
                "Выберите хотя бы один маршрут; ID инженеров должны быть уникальными и принадлежать плану."
            )
        routes = [route for route in routes if route.engineer_id in selected]
        # Rejection explanations still use all engineers; filter only the metrics table.
    if kind == "waybills" and not routes:
        raise ValueError("В плане нет назначенных заявок для маршрутных листов.")
    book = Workbook()
    book.remove(book.active)
    book.properties.creator = "Выезд"
    book.properties.title = f"План на {scenario.date}"
    jobs = {job.id: job for job in scenario.requests}
    if kind == "excel":
        _plan_book(book, scenario, plan, routes, engineers, jobs, engineer_ids is not None)
    elif kind == "waybills":
        _waybills(book, scenario, routes, engineers, jobs)
    else:
        raise ValueError("Неизвестный формат экспорта.")
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()
