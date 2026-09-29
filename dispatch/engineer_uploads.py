"""Validate a reusable engineer CSV without geocoding, routing or plan mutations."""

import csv
import io
import re

from dispatch.importer import sha256
from dispatch.uploads import (
    MAX_BYTES,
    MAX_ERRORS,
    SKILLS,
    TRANSPORTS,
    EngineerInput,
    FileIssue,
    InvalidCell,
)

MAX_ENGINEERS = 30
COLUMNS = ("ID", "Имя", "Навыки", "Транспорт", "Начало смены", "Конец смены")
ALIASES = {"ФИО": "Имя", "Квалификации": "Навыки", "Навык": "Навыки"}
SKILL_NAMES = {
    **SKILLS,
    "Подключения": "installation",
    "Подключения и дозаказы": "installation",
}
PROFILE_NAMES = {**TRANSPORTS, "Пешком": "walk"}


def choice(value, choices, column):
    normalized = {k.casefold(): v for k, v in choices.items()}
    normalized.update({v: v for v in choices.values()})
    if value.casefold() not in normalized:
        raise InvalidCell(
            column, f"Неизвестное значение «{value}». Допустимы: {', '.join(choices)}."
        )
    return normalized[value.casefold()]


def shift_time(value, column):
    if column == "Конец смены" and value == "24:00":
        return 86400
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise InvalidCell(column, "Укажите время в формате ЧЧ:ММ, например 09:00.")
    hour, minute = map(int, value.split(":"))
    return hour * 3600 + minute * 60


def validate_engineers_csv(data: bytes, filename: str):
    errors: list[FileIssue] = []

    def error(message, line=None, column=""):
        if len(errors) < MAX_ERRORS:
            errors.append(FileIssue(line=line, column=column, message=message))

    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if not filename.lower().endswith(".csv"):
        error("Выберите CSV с инженерами. Таблицу Excel сначала сохраните в формате CSV.")
    if len(data) > MAX_BYTES:
        error("Файл больше 2 МБ. Оставьте не более 30 инженеров.")
    if not data.strip():
        error("Файл пуст. Добавьте заголовок и хотя бы одного инженера.")
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
            return None, [FileIssue(message="Не удалось прочитать текст. Сохраните CSV в UTF-8.")]
    if not text.strip():
        return None, [FileIssue(message="Файл пуст. Добавьте хотя бы одного инженера.")]
    engineers, seen = [], set()
    try:
        first = text.splitlines()[0]
        delimiter = max(";,\t", key=lambda d: len(next(csv.reader([first], delimiter=d))))
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        headers = [ALIASES.get(h.strip(), h.strip()) for h in next(reader)]
        for key in COLUMNS:
            if key != "Имя" and key not in headers:
                error(f"Добавьте обязательный столбец «{key}».", 1, key)
        if len(set(headers)) != len(headers) or "" in headers:
            error("Заголовки столбцов должны быть непустыми и не повторяться.", 1)
        unknown = set(headers) - set(COLUMNS)
        if unknown:
            error(
                "Неизвестные столбцы: "
                + ", ".join(sorted(unknown))
                + ". В файле нужны ID, имя, навыки, транспорт и смена. Все начинают из офиса заявок.",
                1,
            )
        if errors:
            return None, errors
        count = 0
        end_line = reader.line_num
        for cells in reader:
            line, end_line = end_line + 1, reader.line_num
            if not any(c.strip() for c in cells):
                continue
            count += 1
            if count > MAX_ENGINEERS:
                error("В одном файле допустимо не более 30 инженеров.", line)
                break
            if len(cells) != len(headers):
                error(
                    "Число ячеек не совпадает с заголовком. Проверьте разделители и кавычки.", line
                )
                continue
            row = dict(zip(headers, (c.strip() for c in cells)))
            try:
                key, name = row["ID"], row.get("Имя", "")
                if not key or len(key) > 150 or any(ord(c) < 32 for c in key):
                    raise InvalidCell("ID", "Укажите ID от 1 до 150 символов без переносов строк.")
                if key in seen:
                    raise InvalidCell(
                        "ID", f"ID «{key}» повторяется. У каждого инженера должен быть свой ID."
                    )
                seen.add(key)
                if len(name) > 150 or any(ord(c) < 32 for c in name):
                    raise InvalidCell(
                        "Имя", "Имя должно быть не длиннее 150 символов, без переносов строк."
                    )
                values = [v.strip() for v in re.split(r"[|,]", row["Навыки"])]
                if not all(values) or not 1 <= len(values) <= 3:
                    raise InvalidCell(
                        "Навыки",
                        "Укажите от 1 до 3 навыков через |, например installation | local.",
                    )
                skills = [choice(v, SKILL_NAMES, "Навыки") for v in values]
                if len(set(skills)) != len(skills):
                    raise InvalidCell("Навыки", "Навыки одного инженера не должны повторяться.")
                profile = choice(row["Транспорт"], PROFILE_NAMES, "Транспорт")
                start = shift_time(row["Начало смены"], "Начало смены")
                end = shift_time(row["Конец смены"], "Конец смены")
                if start >= end:
                    raise InvalidCell(
                        "Начало смены / Конец смены",
                        "Начало смены должно быть раньше конца в пределах одного дня.",
                    )
                engineers.append(
                    EngineerInput(
                        id=key,
                        name=name or None,
                        skills=skills,
                        profile=profile,
                        shift_start_s=start,
                        shift_end_s=end,
                    )
                )
            except InvalidCell as exc:
                error(str(exc), line, exc.column)
    except csv.Error:
        error(
            "Не удалось прочитать CSV. Проверьте кавычки и разделители.",
            reader.line_num if "reader" in locals() else 1,
        )
    if not engineers and not errors:
        error("В файле нет инженеров. Добавьте хотя бы одну строку после заголовка.")
    if errors:
        return None, errors
    return {
        "engineers": engineers,
        "report": {
            "source_file": filename,
            "source_sha256": sha256(data),
            "encoding": encoding,
            "accepted": len(engineers),
        },
    }, []
