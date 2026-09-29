"""Export bounded, hand-calculated priority cases without touching live scenarios."""

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from dispatch.importer import import_sources, read_policy
from dispatch.models import Engineer, Matrix, Model, Profile, Request, Scenario, Skill
from dispatch.work_types import (
    CLASSIFICATION_VERSION,
    PRIORITY_POLICY,
    WorkType,
    classify_work,
    unassigned_by_priority,
)

ROOT = Path(__file__).resolve().parents[1]
WORK_FIELDS: dict[WorkType, tuple[str, str, Skill]] = {
    "emergency": ("Глобальная проблема", "Авария", "emergency"),
    "installation": ("Подключение", "Заявка на подключение", "installation"),
    "repair": ("Локальная заявка", "Нет линка", "local"),
    "additional": ("Дозаказ", "Дозаказ оборудования", "installation"),
}


class SampleRequest(Model):
    id: str = Field(min_length=1)
    work_type: WorkType
    service_s: int = Field(gt=0, le=86400)
    location_id: str | None = None
    window_start_s: int = Field(default=32400, ge=0, lt=86400)
    window_end_s: int = Field(default=39600, ge=0, lt=86400)
    released_at_s: int = Field(default=32400, ge=0, lt=86400)
    required_profile: Profile | None = None


class SampleEngineer(Model):
    id: str = Field(min_length=1)
    skills: list[Skill] = Field(min_length=1)
    profile: Profile = "car"
    start_location_id: str = "office"
    shift_start_s: int = Field(default=32400, ge=0, lt=86400)
    shift_end_s: int = Field(default=39600, gt=0, le=86400)


class PriorityCase(Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]+$")
    description: str = Field(min_length=1)
    requests: list[SampleRequest] = Field(min_length=1, max_length=6)
    engineers: list[SampleEngineer] = Field(min_length=1, max_length=2)
    roster: list[str] | None = None
    travel_s: int = Field(default=600, gt=0, le=86400)
    distance_m: int = Field(default=1000, gt=0, le=1_000_000)
    expected_routes: dict[str, list[str]]
    expected_missing: list[int] = Field(min_length=3, max_length=3)
    explanation: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_case(self):
        scenario = self.scenario()
        if set(self.expected_routes) != {e.id for e in scenario.engineers}:
            raise ValueError("Эталон должен содержать маршрут каждого инженера")
        assigned = [rid for ids in self.expected_routes.values() for rid in ids]
        if len(assigned) != len(set(assigned)):
            raise ValueError("В эталоне заявка назначена дважды")
        expected = unassigned_by_priority(self.work_types(), set(assigned))
        if list(expected) != self.expected_missing:
            raise ValueError(
                "Эталонные маршруты не соответствуют числу неназначенных по приоритету"
            )
        return self

    def work_types(self) -> dict[str, WorkType]:
        return {request.id: request.work_type for request in self.requests}

    def scenario(self) -> Scenario:
        requests = []
        for index, r in enumerate(self.requests):
            kind, subtype, skill = WORK_FIELDS[r.work_type]
            loc = r.location_id or r.id
            requests.append(
                Request(
                    id=r.id,
                    zone=self.id,
                    source_order=index,
                    source_line=index + 2,
                    location_id=loc,
                    address=f"Синтетическая точка {loc}",
                    kind=kind,
                    subtype=subtype,
                    skill=skill,
                    service_s=r.service_s,
                    window_start_s=r.window_start_s,
                    window_end_s=r.window_end_s,
                    released_at_s=r.released_at_s,
                    required_profile=r.required_profile,
                    urgent=r.work_type == "emergency",
                    work_type=r.work_type,
                    raw={"source": "handwritten_priority_fixture_v1"},
                )
            )
        engineers = [Engineer(zone=self.id, **e.model_dump()) for e in self.engineers]
        locations = list(
            dict.fromkeys(
                [e.start_location_id for e in engineers] + [r.location_id for r in requests]
            )
        )
        matrix = Matrix(
            locations=locations,
            duration_s=[[0 if a == b else self.travel_s for b in locations] for a in locations],
            distance_m=[[0 if a == b else self.distance_m for b in locations] for a in locations],
        )
        return Scenario(
            id=self.id,
            date="2026-09-20",
            office_address="Синтетический офис",
            requests=requests,
            engineers=engineers,
            roster=self.roster,
            objective_policy=PRIORITY_POLICY,
            matrices={e.profile: matrix.model_copy(deep=True) for e in engineers},
            manifest={
                "transport": "handwritten_priority_fixture_v1",
                "target_objective": "work_type_priority_v1",
                "classification_version": CLASSIFICATION_VERSION,
                "work_types": self.work_types(),
                "seed": None,
                "assumptions": [
                    "Ручной эталон: между разными точками постоянные заданные время и расстояние.",
                    "Длительности заданы явно для проверки правил, а не как новый производственный норматив.",
                    "Приоритеты проверяются лексикографически: аварии, подключения, остальные работы.",
                ],
            },
        )


class PriorityCases(Model):
    schema_version: Literal["priority_cases_v1"]
    cases: list[PriorityCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Повтор ID сценария")
        return self


def load_cases(path: Path = ROOT / "tests/fixtures/priority_cases.json") -> PriorityCases:
    return PriorityCases.model_validate_json(path.read_text(encoding="utf-8"))


def audit_sources(root: Path) -> dict:
    rows = []
    policy = read_policy(root / "config/scenario.json")
    for zone in import_sources(root / "Обезличивание.zip", root / "Нормативы.xlsx", policy):
        counts: Counter = Counter()
        unresolved = []
        for request in zone["requests"]:
            classification = classify_work(request.kind, request.subtype)
            counts[classification.work_type or "requires_review"] += 1
            if classification.work_type is None:
                unresolved.append(
                    {
                        "id": request.id,
                        "bk": request.kind,
                        "hd": request.subtype,
                        "reason": classification.reason,
                    }
                )
        rows.append(
            {
                "zone": zone["id"],
                "source_sha256": zone["report"]["source_sha256"],
                "total": len(zone["requests"]),
                "counts": dict(counts),
                "unresolved": unresolved,
            }
        )
    return {"classification_version": CLASSIFICATION_VERSION, "zones": rows}


def compare_current(collection: PriorityCases) -> dict:
    from dispatch.checker import require_valid_plan
    from dispatch.optimizer import SearchOptions, optimize

    rows = []
    for sample in collection.cases:
        scenario = sample.scenario()
        result = optimize(
            scenario, SearchOptions(time_limit_ms=0, rounds=8, max_evaluations=100_000)
        )
        row = {
            "id": sample.id,
            "input_hash": scenario.fingerprint(),
            "expected_missing": sample.expected_missing,
        }
        for key in ("baseline", "plan"):
            plan = require_valid_plan(scenario, result[key])
            assigned = {v.request_id for route in plan.routes for v in route.visits}
            row[key] = {
                "assigned_ids": sorted(assigned),
                "missing_by_priority": list(unassigned_by_priority(sample.work_types(), assigned)),
            }
        row["matches_target_coverage"] = (
            row["plan"]["missing_by_priority"] == sample.expected_missing
        )
        rows.append(row)
    return {
        "target_objective": "work_type_priority_v1",
        "note": "Проверка активной политики приоритетов на ручных эталонах.",
        "cases": rows,
    }


def main():
    parser = argparse.ArgumentParser(description="Подготовка эталонных сценариев новых приоритетов")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/priority-samples")
    parser.add_argument(
        "--audit-sources", action="store_true", help="Сверить BK/HD разрешённых синтетических CSV"
    )
    parser.add_argument(
        "--compare-current", action="store_true", help="Сравнить с текущим baseline/C++"
    )
    args = parser.parse_args()
    collection = load_cases()
    args.output.mkdir(parents=True, exist_ok=True)

    def write(name, value):
        (args.output / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    write("priority-cases.schema.json", PriorityCases.model_json_schema())
    for case in collection.cases:
        scenario = case.scenario()
        write(case.id + ".scenario.json", scenario.model_dump())
        write(
            case.id + ".expected.json",
            {
                "input_hash": scenario.fingerprint(),
                "routes": case.expected_routes,
                "unassigned_by_priority": case.expected_missing,
                "explanation": case.explanation,
            },
        )
    if args.audit_sources:
        write("source-classification.json", audit_sources(ROOT))
    if args.compare_current:
        report = compare_current(collection)
        write("current-solver-gap.json", report)
        matches = sum(row["matches_target_coverage"] for row in report["cases"])
        print(f"Текущее покрытие совпало с новыми приоритетами: {matches}/{len(collection.cases)}")
    print(f"Подготовлено {len(collection.cases)} ручных сценариев: {args.output}")


if __name__ == "__main__":
    main()
