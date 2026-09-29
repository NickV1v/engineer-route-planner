"""CSV validation and editable planning inputs, independent of routing or geocoding."""

import csv
import io
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from pydantic import Field, model_validator

from dispatch.equipment import EQUIPMENT_POLICY
from dispatch.importer import location_id, sha256
from dispatch.models import (
    Engineer,
    GeoPoint,
    Matrix,
    Model,
    Profile,
    Request,
    Scenario,
    Skill,
)
from dispatch.scenarios import synthetic_leg
from dispatch.store import SessionNotFound
from dispatch.work_types import CLASSIFICATION_VERSION, PRIORITY_POLICY, classify_work

MAX_BYTES = 2 * 1024 * 1024
MAX_REQUESTS = 100
MAX_ERRORS = 100
SKILLS = {
    "Локальные работы": "local",
    "Работы на подключение и дозаказы": "installation",
    "Аварийные работы": "emergency",
}
TRANSPORTS = {
    "Автомобиль": "car",
    "Пешеход": "walk",
    "Велосипед": "bicycle",
    "Общественный транспорт": "public_transport_approx",
}
ALIASES = {
    "ID": "Заявка",
    "Тип": "Тип заявки BK",
    "Подтип": "Тип заявки HD",
    "Длительность": "Длительность, мин",
    "Транспорт": "Требуемый транспорт",
}
COLUMNS = [
    "Заявка",
    "Тип заявки BK",
    "Тип заявки HD",
    "Начало",
    "Окончание",
    "Район",
    "Адрес",
    "Длительность, мин",
    "Навык",
    "Требуемый транспорт",
    "Приоритет",
    "Широта",
    "Долгота",
]


class FileIssue(Model):
    line: int | None = None
    column: str = ""
    message: str


class InvalidCell(ValueError):
    def __init__(self, column, message):
        self.column = column
        super().__init__(message)


def coordinates(lat: str, lon: str, address: str) -> GeoPoint | None:
    if not lat and not lon:
        return None
    if not lat or not lon:
        raise InvalidCell(
            "Широта / Долгота", "Укажите обе координаты или оставьте обе ячейки пустыми."
        )
    try:
        a, b = float(lat.replace(",", ".")), float(lon.replace(",", "."))
        if (
            not math.isfinite(a)
            or not math.isfinite(b)
            or not -85 <= a <= 85
            or not -180 <= b <= 180
        ):
            raise ValueError()
    except ValueError:
        raise InvalidCell(
            "Широта / Долгота",
            "Нужны числовые координаты: широта от −85 до 85, долгота от −180 до 180.",
        ) from None
    return GeoPoint(lat=a, lon=b, label=address, source="csv", precision="manual")


def csv_datetime(value, column):
    for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise InvalidCell(column, "Укажите дату и время, например 17.08.2026 10:00.")


def dictionary(value, choices, column):
    if value in choices:
        return choices[value]
    if value in choices.values():
        return value
    raise InvalidCell(column, "Допустимые значения: " + ", ".join(choices) + ".")


def validate_csv(data: bytes, filename: str, norms: dict, policy: dict):
    """All-or-nothing validation; no network, matrices, solver, or persistent writes."""
    errors: list[FileIssue] = []

    def error(message, line=None, column=""):
        if len(errors) < MAX_ERRORS:
            errors.append(FileIssue(line=line, column=column, message=message))

    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if not filename.lower().endswith(".csv"):
        error("Выберите файл CSV. Таблицу Excel сначала сохраните в формате CSV.")
    if len(data) > MAX_BYTES:
        error("Файл больше 2 МБ. Разделите его на отдельные рабочие дни.")
    if not data.strip():
        error("Файл пуст. Добавьте заголовок и хотя бы одну заявку.")
    if b"\x00" in data:
        error("Файл не является текстовым CSV. Сохраните его в UTF-8 или Windows-1251.")
    if errors:
        return None, errors
    try:
        text, encoding = data.decode("utf-8-sig"), "utf-8-sig"
    except UnicodeDecodeError:
        try:
            text, encoding = data.decode("cp1251"), "cp1251"
        except UnicodeDecodeError:
            error("Не удалось прочитать текст. Сохраните CSV в UTF-8.")
            return None, errors
    if not text.strip():
        return None, [FileIssue(message="Файл пуст. Добавьте заголовок и хотя бы одну заявку.")]
    try:
        # Sniffer is confused by the shorter office footer; inspect the header only.
        first = text.splitlines()[0]
        delimiter = max(";,\t", key=lambda d: len(next(csv.reader([first], delimiter=d))))
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        headers = [ALIASES.get(h.strip(), h.strip()) for h in next(reader)]
        for key in ("Заявка", "Тип заявки BK", "Начало", "Окончание"):
            if key not in headers:
                error(f"Добавьте обязательный столбец «{key}».", 1, key)
        if "Адрес" not in headers and not {"Широта", "Долгота"}.issubset(headers):
            error("Добавьте столбец «Адрес» или оба столбца «Широта» и «Долгота».", 1)
        if len(set(headers)) != len(headers) or "" in headers:
            error("Заголовки столбцов должны быть непустыми и не повторяться.", 1)
        unknown = set(headers) - set(COLUMNS) - {"Подключение", "Гигабитное подключение"}
        if unknown:
            error(
                "Неизвестные столбцы: " + ", ".join(sorted(unknown)) + ". Сверьтесь с шаблоном.", 1
            )
        if errors:
            return None, errors
        requests, points, offices, dates, seen = [], {}, [], set(), set()
        count = 0
        for cells in reader:
            line = reader.line_num
            if not any(x.strip() for x in cells):
                continue
            if cells[0].strip().casefold() in {"офис", "адрес офиса"}:
                try:
                    if len(cells) < 2 or not cells[1].strip():
                        raise InvalidCell("Офис", "Укажите адрес офиса во второй ячейке строки.")
                    address = cells[1].strip()
                    if len(address) > 500:
                        raise InvalidCell("Офис", "Адрес не должен превышать 500 символов.")
                    if any(x.strip() for x in cells[4:]):
                        raise InvalidCell(
                            "Офис", "После адреса офиса допустимы только широта и долгота."
                        )
                    point = coordinates(
                        cells[2].strip() if len(cells) > 2 else "",
                        cells[3].strip() if len(cells) > 3 else "",
                        address,
                    )
                    offices.append(address)
                    if point:
                        if location_id(address) in points and points[location_id(address)] != point:
                            raise InvalidCell("Офис", "У адреса офиса и заявки разные координаты.")
                        points[location_id(address)] = point
                except InvalidCell as exc:
                    error(str(exc), line, exc.column)
                continue
            count += 1
            if count > MAX_REQUESTS:
                error(
                    "В одном файле допустимо не более 100 заявок. Разделите данные на отдельные участки.",
                    line,
                )
                break
            if len(cells) != len(headers):
                error(
                    f"Ожидалось {len(headers)} ячеек, получено {len(cells)}. Проверьте разделители и кавычки.",
                    line,
                )
                continue
            row = dict(zip(headers, (v.strip() for v in cells)))
            try:
                rid = row["Заявка"]
                if not rid or len(rid) > 100:
                    raise InvalidCell("Заявка", "Укажите ID заявки длиной от 1 до 100 символов.")
                if rid in seen:
                    raise InvalidCell(
                        "Заявка", f"ID «{rid}» повторяется. У каждой заявки должен быть свой ID."
                    )
                seen.add(rid)
                start, end = (
                    csv_datetime(row["Начало"], "Начало"),
                    csv_datetime(row["Окончание"], "Окончание"),
                )
                if start.date() != end.date() or start > end:
                    raise InvalidCell(
                        "Начало / Окончание",
                        "Начало должно быть не позже окончания, в пределах одного дня.",
                    )
                dates.add(start.date().isoformat())
                kind = row["Тип заявки BK"]
                mapping = policy["norm_mapping"].get(kind)
                if not mapping or mapping["work"] not in norms:
                    raise InvalidCell(
                        "Тип заявки BK",
                        "Допустимые виды работ: " + ", ".join(policy["norm_mapping"]) + ".",
                    )
                duration = row.get("Длительность, мин", "")
                if duration:
                    if (
                        not duration.isascii()
                        or not duration.isdigit()
                        or len(duration) > 4
                        or not 1 <= int(duration) <= 1440
                    ):
                        raise InvalidCell(
                            "Длительность, мин",
                            "Укажите целое число минут от 1 до 1440. Время в пути сюда не входит.",
                        )
                    service_s = int(duration) * 60
                else:
                    service_s = norms[mapping["work"]]["service_s"]
                address = row.get("Адрес", "")
                point = coordinates(row.get("Широта", ""), row.get("Долгота", ""), address)
                if not address:
                    if not point:
                        raise InvalidCell("Адрес", "Укажите адрес заявки или обе координаты.")
                    address = f"Координаты {point.lat}, {point.lon}"
                    point.label = address
                if len(address) > 500:
                    raise InvalidCell("Адрес", "Адрес не должен превышать 500 символов.")
                key = location_id(address)
                if point:
                    if key in points and points[key] != point:
                        raise InvalidCell(
                            "Широта / Долгота",
                            "У одинакового адреса разные координаты. Исправьте их или уточните адрес.",
                        )
                    points[key] = point
                skill = (
                    dictionary(row["Навык"], SKILLS, "Навык")
                    if row.get("Навык")
                    else mapping["skill"]
                )
                profile = (
                    dictionary(row["Требуемый транспорт"], TRANSPORTS, "Требуемый транспорт")
                    if row.get("Требуемый транспорт")
                    else None
                )
                priority = row.get("Приоритет", "")
                if priority and priority not in {"Обычная", "Срочная"}:
                    raise InvalidCell("Приоритет", "Укажите «Обычная» или «Срочная».")
                subtype = row.get("Тип заявки HD", "")
                requests.append(
                    Request(
                        id=rid,
                        zone="pending",
                        source_order=count - 1,
                        source_line=line,
                        location_id=key,
                        address=address,
                        kind=kind,
                        subtype=subtype,
                        window_start_s=start.hour * 3600 + start.minute * 60,
                        window_end_s=end.hour * 3600 + end.minute * 60,
                        service_s=service_s,
                        skill=skill,
                        required_profile=profile,
                        urgent=priority == "Срочная" if priority else subtype == "Авария",
                        work_type=classify_work(kind, subtype).work_type,
                        raw=row,
                    )
                )
            except InvalidCell as exc:
                error(str(exc), line, exc.column)
        if len(offices) != 1:
            error(
                "Добавьте ровно одну строку «Адрес офиса;адрес;широта;долгота» после заявок. Координаты можно оставить пустыми."
            )
        if len(dates) > 1:
            error("В файле заявки на разные даты. Для каждого рабочего дня нужен отдельный файл.")
        if not requests:
            error("В файле нет корректных заявок.")
    except (csv.Error, UnicodeError) as exc:
        error(f"Не удалось разобрать CSV: проверьте кавычки и разделители ({exc}).")
    if errors:
        return None, errors
    # The file defines one dispatch area; its 'Район' values are administrative
    # neighbourhoods, not separate rosters (the original samples contain many).
    zone = Path(filename).stem.removesuffix(" Синтетические данные")
    if not zone.strip() or len(zone) > 100:
        return None, [
            FileIssue(message="Название района или файла должно содержать от 1 до 100 символов.")
        ]
    for r in requests:
        r.id, r.zone = f"{zone}:{r.id}", zone
    return {
        "id": zone,
        "date": next(iter(dates)),
        "office_address": offices[0],
        "requests": requests,
        "points": {k: p.model_dump() for k, p in points.items()},
        "norms": norms,
        "report": {
            "source_file": filename,
            "source_sha256": sha256(data),
            "encoding": encoding,
            "accepted": len(requests),
            "rejected": 0,
            "candidate_rows": count,
            "errors": [],
            "duplicate_addresses": {
                a: n for a, n in Counter(r.address for r in requests).items() if n > 1
            },
        },
    }, []


class EngineerInput(Model):
    id: str = Field(min_length=1, max_length=150)
    name: str | None = Field(default=None, min_length=1, max_length=150)
    skills: list[Skill] = Field(min_length=1, max_length=3)
    profile: Profile
    shift_start_s: int = Field(ge=0, lt=86400)
    shift_end_s: int = Field(gt=0, le=86400)

    @model_validator(mode="after")
    def consistent(self):
        if not self.id.strip() or self.name is not None and not self.name.strip():
            raise ValueError("ID и имя инженера не должны состоять из пробелов.")
        if self.shift_start_s >= self.shift_end_s:
            raise ValueError("Начало смены должно быть раньше окончания.")
        if len(set(self.skills)) != len(self.skills):
            raise ValueError("Навыки не должны повторяться.")
        return self


def default_engineers(zone, policy, count=12):
    return [
        EngineerInput(
            id=f"{zone}:engineer-{i + 1:02}",
            skills=policy["skills_cycle"][i % len(policy["skills_cycle"])],
            profile=policy["profiles_cycle"][i % len(policy["profiles_cycle"])],
            shift_start_s=policy["shifts_s"][i % len(policy["shifts_s"])][0],
            shift_end_s=policy["shifts_s"][i % len(policy["shifts_s"])][1],
        )
        for i in range(count)
    ]


class UploadStore:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )

    def create(self, item):
        from fastapi.encoders import jsonable_encoder

        key = uuid4().hex
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO uploads VALUES (?,?)",
                (key, json.dumps(jsonable_encoder(item), ensure_ascii=False)),
            )
        return key

    def get(self, key):
        with self.store.connection() as db:
            row = db.execute("SELECT data FROM uploads WHERE id=?", (key,)).fetchone()
        if not row:
            raise SessionNotFound("Загруженный файл не найден. Загрузите CSV ещё раз.")
        item = json.loads(row[0])
        item["requests"] = [Request.model_validate(r) for r in item["requests"]]
        return item


def uploaded_geography(item, catalog):
    records = catalog.collect([item["office_address"], *[r.address for r in item["requests"]]])
    revisions = item.get("geography_revisions", {})
    for key, point in item["points"].items():
        if records[key].revision <= revisions.get(key, 0):
            records[key] = records[key].model_copy(
                update={"point": GeoPoint.model_validate(point), "status": "manual"}
            )
    return records


def uploaded_scenario(
    item, upload_id, inputs, policy, catalog, *, synthetic=False, excluded_request_ids=()
):
    if len({e.id for e in inputs}) != len(inputs):
        raise ValueError("ID инженеров не должны повторяться.")
    geography = uploaded_geography(item, catalog)
    excluded = set(excluded_request_ids)
    if len(excluded) != len(excluded_request_ids) or not excluded.issubset(
        {r.id for r in item["requests"]}
    ):
        raise ValueError("Список исключений содержит повторные или неизвестные заявки.")
    requests = [r for r in item["requests"] if r.id not in excluded]
    locations_in_use = {location_id(item["office_address"]), *[r.location_id for r in requests]}
    geography = {key: record for key, record in geography.items() if key in locations_in_use}
    engineers = [
        Engineer(
            id=e.id,
            name=e.name,
            zone=item["id"],
            skills=e.skills,
            profile=e.profile,
            start_location_id=location_id(item["office_address"]),
            shift_start_s=e.shift_start_s,
            shift_end_s=e.shift_end_s,
        )
        for e in inputs
    ]
    locations = list(geography)
    profiles = list(dict.fromkeys(e.profile for e in engineers)) or ["car"]
    matrices = {}
    for profile in profiles:
        rows = [
            [
                (0, 0)
                if a == b
                else synthetic_leg(a, b, profile, policy)
                if synthetic
                else (None, None)
                for b in locations
            ]
            for a in locations
        ]
        matrices[profile] = Matrix(
            locations=locations,
            duration_s=[[v[0] for v in row] for row in rows],
            distance_m=[[v[1] for v in row] for row in rows],
        )
    return Scenario(
        id=item["id"],
        date=item["date"],
        office_address=item["office_address"],
        requests=requests,
        engineers=engineers,
        geography=geography,
        matrices=matrices,
        objective_policy=PRIORITY_POLICY,
        schedule_policy="compact_v1",
        equipment_policy=EQUIPMENT_POLICY,
        manifest={
            "upload_id": upload_id,
            "import": item["report"],
            "policy": policy,
            "norms": item["norms"],
            "engineer_inputs": [e.model_dump() for e in inputs],
            "excluded_request_ids": list(excluded_request_ids),
            "excluded_requests": [r.model_dump() for r in item["requests"] if r.id in excluded],
            "classification_version": CLASSIFICATION_VERSION,
            "transport": "synthetic_fixture_v1" if synthetic else "pending_routing",
            "assumptions": [
                "Состав доступных инженеров, навыки, транспорт и смены заданы диспетчером.",
                "Все инженеры начинают смену из офиса участка.",
                "При отсутствии длительности используется норматив работы на месте, без нормативной дороги.",
            ],
        },
    )
