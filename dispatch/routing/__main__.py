"""Local journey inspection and explicit preparation of new planning inputs."""

import argparse
import json
from functools import partial
from pathlib import Path

from dispatch.baseline import solve_baseline
from dispatch.checker import require_valid_plan
from dispatch.models import Scenario
from dispatch.optimizer import SearchOptions, optimize
from dispatch.routing.contracts import Coordinate, Journey, RoutingError
from dispatch.routing.matrices import apply_transport, prepare_transport
from dispatch.routing.roads import COSTINGS, RoadRouter
from dispatch.routing.transit import TransitNetwork, TransitRouter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    transit = commands.add_parser("transit", help="Проверить комбинированную поездку")
    transit.add_argument("--network", type=Path, required=True)
    road = commands.add_parser("road", help="Проверить поездку по локальной дорожной сети")
    road.add_argument("--graph", type=Path, required=True)
    road.add_argument("--profile", choices=list(COSTINGS), required=True)
    road.add_argument("--points", type=Path, required=True, help="JSON: ID → {lat, lon}")
    for command in (transit, road):
        command.add_argument("--origin", required=True)
        command.add_argument("--target", required=True)
    prepare = commands.add_parser(
        "prepare", help="Подготовить новый день с транспортными матрицами"
    )
    prepare.add_argument("--scenario", type=Path, required=True)
    prepare.add_argument("--graph", type=Path)
    prepare.add_argument("--network", type=Path)
    prepare.add_argument("--output", type=Path, required=True, help="Новая директория результата")
    prepare.add_argument("--optimize", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "transit":
            router = TransitRouter(TransitNetwork.model_validate_json(args.network.read_text()))
            print(
                json.dumps(
                    router.route(args.origin, args.target).report(), ensure_ascii=False, indent=2
                )
            )
        elif args.command == "road":
            points = {
                key: Coordinate.model_validate(value)
                for key, value in json.loads(args.points.read_text()).items()
            }
            if args.origin not in points or args.target not in points:
                raise ValueError("ID отсутствует в файле координат")
            result = RoadRouter(args.graph).route(
                args.profile, args.origin, args.target, points[args.origin], points[args.target]
            )
            print(json.dumps(result.report(), ensure_ascii=False, indent=2))
        else:
            prepare_day(args)
    except (OSError, ValueError, RoutingError) as exc:
        parser.exit(1, f"Ошибка маршрутизации: {exc}\n")


def prepare_day(args):
    scenario = Scenario.model_validate_json(args.scenario.read_text())
    if args.output.exists():
        raise ValueError("Директория результата уже существует; выберите новую")
    if scenario.roster is not None or scenario.planning_state is not None:
        raise ValueError("Для новых матриц нужен черновик дня без опубликованной смены")
    profiles = {e.profile for e in scenario.engineers}
    coordinates = {
        key: Coordinate(lat=record.point.lat, lon=record.point.lon)
        if record and record.point
        else None
        for key in dict.fromkeys(
            [e.start_location_id for e in scenario.engineers]
            + [r.location_id for r in scenario.requests]
        )
        for record in [scenario.geography.get(key)]
    }
    routers = {}
    if profiles & set(COSTINGS):
        if args.graph is None:
            raise ValueError("Нужен --graph для дорожных профилей")
        road = RoadRouter(args.graph)

        def road_route(profile, origin, target):
            return road.route(profile, origin, target, coordinates[origin], coordinates[target])

        for profile in sorted(profiles & set(COSTINGS)):
            routers[profile] = partial(road_route, profile)
    if "public_transport_approx" in profiles:
        if args.network is None:
            raise ValueError("Нужен --network с метро, переходами и пешими подходами к адресам")
        network = TransitNetwork.model_validate_json(args.network.read_text())
        transit = TransitRouter(network)
        for key, point in coordinates.items():
            if point is not None and network.nodes.get(key) != point:
                raise ValueError(f"Нет узла с координатами адреса в транспортной сети: {key}")

        def transit_route(origin, target):
            if coordinates[origin] is None or coordinates[target] is None:
                return Journey(
                    from_id=origin,
                    to_id=target,
                    status="missing_coordinates",
                    network_id=transit.network_id,
                )
            return transit.route(origin, target)

        routers["public_transport_approx"] = transit_route
    package = prepare_transport(coordinates, routers)
    updated = apply_transport(scenario, package)
    baseline = require_valid_plan(updated, solve_baseline(updated))
    results = {"transport": package, "scenario": updated, "baseline": baseline}
    if args.optimize:
        results["optimized"] = optimize(updated, SearchOptions(time_limit_ms=0))["plan"]
    args.output.mkdir(parents=True, exist_ok=False)
    for name, model in results.items():
        (args.output / f"{name}.json").write_text(model.model_dump_json(indent=2) + "\n")
    print(
        f"Матрицы {package.fingerprint()}; baseline {baseline.metrics.assigned}/{baseline.metrics.total}; checker OK"
    )


if __name__ == "__main__":
    main()
