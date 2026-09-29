"""Manual, fully local road-engine smoke test; requires a completed regional graph."""

import argparse
import json
from pathlib import Path

from research_routing import POINTS

from dispatch.routing.contracts import Coordinate
from dispatch.routing.roads import RoadRouter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Новая директория проверки")
    args = parser.parse_args()
    router = RoadRouter(args.graph)
    args.output.mkdir(parents=True, exist_ok=False)
    check_points = {**POINTS, "kashira_road": {"lat": 54.834744, "lon": 38.187295}}
    points = {key: Coordinate.model_validate(value) for key, value in check_points.items()}
    cases = [
        (profile, a, b, "ok")
        for profile in ("car", "walk", "bicycle")
        for city in ("moscow", "domodedovo")
        for a, b in ((city + "_a", city + "_b"), (city + "_b", city + "_a"))
    ] + [
        ("car", "moscow_a", "kashira_road", "ok"),
        ("car", "kashira_road", "moscow_a", "ok"),
        # Original technical point is 260 m from a drivable road in this extract.
        ("car", "moscow_a", "kashira", "unmatched"),
        ("car", "kashira", "moscow_a", "unmatched"),
    ]
    results = []
    for profile, origin, target, expected in cases:
        result = router.route(profile, origin, target, points[origin], points[target])
        name = f"{profile}_{origin}_{target}"
        (args.output / f"{name}.json").write_text(
            json.dumps(result.report(), ensure_ascii=False, indent=2)
        )
        checked = result.status == expected and (
            expected != "ok" or (result.duration_s > 0 and result.distance_m > 0)
        )
        results.append(
            {
                "case": name,
                "checked": checked,
                "status": result.status,
                "expected_status": expected,
                "duration_s": result.duration_s,
                "distance_m": result.distance_m,
            }
        )
        print(name, result.status, result.duration_s, result.distance_m, flush=True)
    report = {
        "network_id": router.network_id,
        "points": check_points,
        "cases": results,
        "note": "Технические точки, не подтверждённые входы зданий и не адреса заявок.",
    }
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if not all(row["checked"] for row in results):
        raise SystemExit("Не все локальные проверки пройдены; проверьте report.json")


if __name__ == "__main__":
    main()
