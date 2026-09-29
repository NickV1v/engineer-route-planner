"""Benchmark fresh routing for authorized synthetic scenarios; never warm the web cache."""

import argparse
import json
import threading
import time
from pathlib import Path

from dispatch.baseline import solve_baseline
from dispatch.checker import require_valid_plan
from dispatch.geography import GeographyStore, attach_geography
from dispatch.importer import import_sources, read_policy
from dispatch.optimizer import SearchOptions, optimize
from dispatch.routing.service import RoutingService
from dispatch.scenarios import make_scenario
from dispatch.store import PlanStore


def prepare(root, zones, output):
    output.mkdir(parents=True, exist_ok=True)
    policy = read_policy(root / "config/scenario.json")
    imported = import_sources(root / "Обезличивание.zip", root / "Нормативы.xlsx", policy)
    geography = GeographyStore(PlanStore(root / "artifacts/dispatch.sqlite"))
    service = RoutingService(root, mode="real")
    done = threading.Event()

    def progress():
        while not done.wait(10):
            print(json.dumps(service.progress, ensure_ascii=False), flush=True)

    thread = threading.Thread(target=progress, daemon=True)
    thread.start()
    try:
        for item in imported:
            if zones and item["id"] not in zones:
                continue
            started = time.monotonic()
            print(f"Подготовка {item['id']}", flush=True)
            with service.calculation() as calculation:
                scenario = calculation.new_day(
                    attach_geography(make_scenario(item, policy), geography)
                )
                performance = calculation.report()
            solve_started = time.perf_counter()
            baseline = require_valid_plan(scenario, solve_baseline(scenario))
            result = optimize(scenario, SearchOptions(time_limit_ms=0, rounds=2))
            require_valid_plan(scenario, result["plan"])
            performance["solver_ms"] = round((time.perf_counter() - solve_started) * 1000, 3)
            journeys = service.journeys(scenario, result["plan"])
            summary = {
                "zone": item["id"],
                "performance": performance,
                "elapsed_s": round(time.monotonic() - started, 1),
                "baseline": baseline.metrics.model_dump(),
                "optimized": result["plan"].metrics.model_dump(),
                "routing": scenario.manifest["routing"],
                "modes": sorted(
                    {leg["mode"] for route in journeys.values() for j in route for leg in j["legs"]}
                ),
            }
            (output / f"{item['id']}.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2)
            )
            (output / f"{item['id']}-journeys.json").write_text(
                json.dumps(journeys, ensure_ascii=False)
            )
            print(json.dumps(summary, ensure_ascii=False), flush=True)
    finally:
        done.set()
        thread.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zone", action="append")
    parser.add_argument("--output", type=Path, default=Path("artifacts/real-routing-checks"))
    args = parser.parse_args()
    prepare(Path(__file__).resolve().parents[1], args.zone, args.output)
