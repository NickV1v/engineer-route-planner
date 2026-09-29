import argparse
import json
from pathlib import Path

from dispatch.baseline import solve_baseline
from dispatch.checker import require_valid_plan
from dispatch.importer import import_sources, read_policy
from dispatch.objective import compare_plans
from dispatch.optimizer import SearchOptions, optimize
from dispatch.scenarios import make_scenario


def main():
    parser = argparse.ArgumentParser(
        description="Импорт синтетики и проверенный baseline для трёх зон"
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("artifacts"))
    parser.add_argument(
        "--optimize", action="store_true", help="Дополнительно запустить C++ и сравнить с baseline"
    )
    parser.add_argument(
        "--time-limit-ms",
        type=int,
        default=SearchOptions().time_limit_ms,
        help="0 — только воспроизводимый лимит вычислений",
    )
    args = parser.parse_args()
    policy = read_policy(args.root / "config/scenario.json")
    imported = import_sources(args.root / "Обезличивание.zip", args.root / "Нормативы.xlsx", policy)
    args.output.mkdir(parents=True, exist_ok=True)
    for item in imported:
        # Persist the report even if invalid rows prevent planning.
        report = args.output / f"{item['id']}.import.json"
        report.write_text(
            json.dumps(item["report"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        scenario = make_scenario(item, policy)
        plan = require_valid_plan(scenario, solve_baseline(scenario))
        for suffix, model in (("scenario", scenario), ("baseline", plan)):
            (args.output / f"{scenario.id}.{suffix}.json").write_text(
                model.model_dump_json(indent=2), encoding="utf-8"
            )
        print(
            f"{scenario.id}: {plan.metrics.assigned}/{plan.metrics.total} назначено, "
            f"{plan.metrics.active_engineers} инженеров; checker OK; матрица СИНТЕТИЧЕСКАЯ"
        )
        if args.optimize:
            result = optimize(scenario, SearchOptions(time_limit_ms=args.time_limit_ms))
            optimized = result["plan"]
            (args.output / f"{scenario.id}.optimized.json").write_text(
                optimized.model_dump_json(indent=2), encoding="utf-8"
            )
            comparison = {
                **compare_plans(scenario, plan, optimized),
                "search": result["search"].model_dump(),
                "options": result["options"].model_dump(),
                "binary_sha256": result["binary_sha256"],
                "input_hash": scenario.fingerprint(),
            }
            (args.output / f"{scenario.id}.comparison.json").write_text(
                json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                f"  C++: {optimized.metrics.assigned}/{optimized.metrics.total}; "
                f"{optimized.metrics.active_engineers} инженеров; {result['search'].status}; checker OK"
            )


if __name__ == "__main__":
    main()
