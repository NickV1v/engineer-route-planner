"""Reproducible component experiments; no tuning of scenario inputs per algorithm."""

import argparse
import json
import platform
from pathlib import Path

from dispatch.importer import import_sources, read_policy
from dispatch.objective import objective_components, objective_version, score_plan
from dispatch.optimizer import SearchOptions, optimize
from dispatch.scenarios import make_scenario


def main():
    parser = argparse.ArgumentParser(description="Сравнение компонентов C++-поиска")
    parser.add_argument("--output", type=Path, default=Path("artifacts/benchmark.json"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    policy = read_policy(root / "config/scenario.json")
    sources = import_sources(root / "Обезличивание.zip", root / "Нормативы.xlsx", policy)
    variants = {
        "insertion_only": {
            "rounds": 1,
            "use_local_search": False,
            "use_eliminate": False,
            "use_destroy": False,
        },
        "without_local_search": {"use_local_search": False},
        "with_elimination": {"use_eliminate": True},
        "without_destroy": {"use_destroy": False},
        "default": {},
    }
    report = {"platform": platform.platform(), "transport": "synthetic_fixture_v1", "rows": []}
    for source in sources:
        case = make_scenario(source, policy)
        for name, changes in variants.items():
            options = SearchOptions(time_limit_ms=0, **changes)
            result = optimize(case, options)
            row = {
                "zone": case.id,
                "variant": name,
                "objective_version": objective_version(case),
                "objective_components": objective_components(case),
                "input_hash": case.fingerprint(),
                "binary_sha256": result["binary_sha256"],
                "options": options.model_dump(),
                "baseline_score": score_plan(case, result["baseline"]),
                "score": score_plan(case, result["plan"]),
                "metrics": result["plan"].metrics.model_dump(),
                "search": result["search"].model_dump(),
            }
            report["rows"].append(row)
            print(case.id, name, row["score"], f"{result['search'].elapsed_ms:.1f} ms")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
