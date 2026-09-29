import hashlib
import math

from dispatch.equipment import EQUIPMENT_POLICY
from dispatch.importer import location_id
from dispatch.models import Engineer, Matrix, Scenario
from dispatch.work_types import CLASSIFICATION_VERSION, PRIORITY_POLICY, classify_work


def synthetic_leg(origin: str, destination: str, profile: str, policy: dict) -> tuple[int, int]:
    speed = policy["synthetic_speed_m_per_s"][profile]
    if speed <= 0:
        raise ValueError("Скорость должна быть положительной")
    key = f"fixture-v1|{policy['seed']}|{origin}|{destination}".encode()
    value = int.from_bytes(hashlib.sha256(key).digest()[:4], "big")
    distance = 0 if origin == destination else 500 + value % 9501
    duration = math.ceil(distance / speed)
    if distance and profile == "public_transport_approx":
        duration += 300
    return duration, distance


def extend_matrices(scenario: Scenario, new_location: str) -> None:
    """Append only missing edges; never regenerate historical matrix values."""
    for profile, matrix in scenario.matrices.items():
        if new_location in matrix.locations:
            continue
        if scenario.manifest.get("transport") != "synthetic_fixture_v1":
            raise ValueError("Для нового адреса нет транспортных данных")
        policy = scenario.manifest["policy"]
        old_locations = list(matrix.locations)
        for i, origin in enumerate(old_locations):
            duration, distance = synthetic_leg(origin, new_location, profile, policy)
            matrix.duration_s[i].append(duration)
            matrix.distance_m[i].append(distance)
        new_edges = [
            synthetic_leg(new_location, destination, profile, policy)
            for destination in old_locations
        ]
        matrix.duration_s.append([t for t, _ in new_edges] + [0])
        matrix.distance_m.append([d for _, d in new_edges] + [0])
        matrix.locations.append(new_location)


def make_scenario(imported: dict, policy: dict, engineer_count: int | None = None) -> Scenario:
    if imported["report"]["rejected"]:
        raise ValueError(f"Исправьте ошибки импорта: {imported['report']['errors']}")
    count = policy["engineer_count"] if engineer_count is None else engineer_count
    if not 0 <= count <= 30:
        raise ValueError("Число инженеров должно быть от 0 до 30")
    office = location_id(imported["office_address"])
    engineers = []
    for i in range(count):
        shift = policy["shifts_s"][i % len(policy["shifts_s"])]
        engineers.append(
            Engineer(
                id=f"{imported['id']}:engineer-{i + 1:02}",
                zone=imported["id"],
                skills=policy["skills_cycle"][i % len(policy["skills_cycle"])],
                profile=policy["profiles_cycle"][i % len(policy["profiles_cycle"])],
                start_location_id=office,
                shift_start_s=shift[0],
                shift_end_s=shift[1],
            )
        )
    locations = list(dict.fromkeys([office] + [r.location_id for r in imported["requests"]]))
    matrices = {}
    for profile, speed in policy["synthetic_speed_m_per_s"].items():
        if speed <= 0:
            raise ValueError("Скорость должна быть положительной")
        durations, distances = [], []
        for origin in locations:
            time_row, distance_row = [], []
            for destination in locations:
                # Directed, deterministic fixture. These are NOT geographic distances.
                duration, distance = synthetic_leg(origin, destination, profile, policy)
                time_row.append(duration)
                distance_row.append(distance)
            durations.append(time_row)
            distances.append(distance_row)
        matrices[profile] = Matrix(locations=locations, duration_s=durations, distance_m=distances)
    return Scenario(
        id=imported["id"],
        date=imported["date"],
        office_address=imported["office_address"],
        requests=[
            r.model_copy(update={"work_type": classify_work(r.kind, r.subtype).work_type})
            for r in imported["requests"]
        ],
        objective_policy=PRIORITY_POLICY,
        schedule_policy="compact_v1",
        equipment_policy=EQUIPMENT_POLICY,
        engineers=engineers,
        matrices=matrices,
        manifest={
            "import": imported["report"],
            "norms": imported["norms"],
            "norms_sha256": imported["norms_sha256"],
            "policy": policy,
            "engineer_count": count,
            "transport": "synthetic_fixture_v1",
            "classification_version": CLASSIFICATION_VERSION,
            "assumptions": [
                "Матрицы полностью синтетические: не оценивают реальные поездки и пробег.",
                "Штат, навыки, транспорт и смены заданы конфигурацией, а не исходными данными.",
                "Клиентское оборудование задано синтетически: подключение — 1 роутер, дозаказ — 1 ТВ-приставка, остальные заявки — без устройств. После исходного расчёта состав смены и выдача фиксируются автоматически, точно под план, без резерва и передачи между инженерами.",
                "Все Глобальные проблемы, включая Информацию, получают норматив Аварий на ТКД.",
                "Срочность задаётся HD=Авария; baseline сохраняет исходный порядок строк.",
                "Приоритеты: аварии → подключения → ремонт / дозаказы. Неуточнённые типы временно имеют обычный приоритет; они не считаются подтверждёнными авариями.",
                "Нормативная дорога исключена; учитывается только время матрицы. Возврат в офис не требуется.",
            ],
        },
    )
