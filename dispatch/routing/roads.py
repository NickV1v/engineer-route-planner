"""Local Valhalla adapter: actual road geometry, static estimated travel time."""

import json
import math
from copy import copy
from pathlib import Path
from threading import Lock

from dispatch.routing.contracts import (
    Coordinate,
    Journey,
    Leg,
    RoadProfile,
    RoutingError,
    content_hash,
    separation_m,
)

COSTINGS = {"car": "auto", "walk": "pedestrian", "bicycle": "bicycle"}
BICYCLE_MODEL = {"version": "bicycle_dismount_legs_v1"}
ASSUMPTIONS = [
    "Время рассчитано по дорожной сети без пробок и календаря ограничений.",
    "Концы пути привязаны к сети в пределах 100 м; проход от двери до сети не включён.",
]


def road_network_id(roads, profile: RoadProfile) -> str:
    # Changing leg semantics must not overwrite archived journeys with the same key.
    if profile == "bicycle":
        return content_hash({"road_network": roads.network_id, "model": BICYCLE_MODEL})
    return roads.network_id


def rounded_parts(values: list[float], total: int, *, minimum: int = 0) -> list[int]:
    """Apportion quantized maneuver metrics while preserving the summary total."""
    if not sum(values):
        if total:
            raise ValueError("Пустые метрики манёвров при ненулевом итоге")
        return [0] * len(values)
    exact = [total * v / sum(values) for v in values]
    lower = [minimum if v else 0 for v in values]
    if sum(lower) > total:
        raise ValueError("Недостаточно времени для участков движения")
    result = [max(low, math.floor(v)) for low, v in zip(lower, exact)]
    while sum(result) < total:
        i = max(range(len(values)), key=lambda i: exact[i] - result[i])
        result[i] += 1
    while sum(result) > total:
        i = max(
            (i for i in range(len(values)) if result[i] > lower[i]),
            key=lambda i: result[i] - exact[i],
        )
        result[i] -= 1
    return result


def bicycle_legs(raw, points, origin, target) -> list[Leg]:
    """Preserve the engine's cycling/dismount boundaries, not its turn-by-turn splits."""
    groups = []
    previous = 0
    maneuvers = raw["maneuvers"]
    for item in maneuvers:
        start, end = item["begin_shape_index"], item["end_shape_index"]
        if (
            type(start) is not int
            or type(end) is not int
            or start != previous
            or not start <= end < len(points)
            or item["travel_mode"] not in {"bicycle", "pedestrian"}
        ):
            raise ValueError("Некорректные границы или режим веломаршрута")
        time, distance = number(item["time"]), 1000 * number(item["length"])
        previous = end
        if start == end:
            if time or distance:
                raise ValueError("Движение без геометрии")
            continue  # Arrival instruction, no travel.
        mode = "walk" if item["travel_mode"] == "pedestrian" else "bicycle"
        if groups and groups[-1]["mode"] == mode:
            groups[-1]["end"] = end
            groups[-1]["time"] += time
            groups[-1]["distance"] += distance
        else:
            groups.append(dict(mode=mode, start=start, end=end, time=time, distance=distance))
    if previous != len(points) - 1 or not groups:
        raise ValueError("Манёвры не покрывают геометрию маршрута")
    seconds = number(raw["summary"]["time"])
    meters = 1000 * number(raw["summary"]["length"])
    # Native JSON rounds each maneuver separately to milliseconds/meters.
    if (
        abs(sum(g["time"] for g in groups) - seconds) > 0.0011 * (len(maneuvers) + 1)
        or abs(sum(g["distance"] for g in groups) - meters) > len(maneuvers) + 1
    ):
        raise ValueError("Метрики манёвров расходятся с итогом маршрута")
    times = rounded_parts([g["time"] for g in groups], math.ceil(seconds), minimum=1)
    distances = rounded_parts([g["distance"] for g in groups], math.ceil(meters))
    ids = [origin, *(f"{origin}:dismount:{g['start']}" for g in groups[1:]), target]
    return [
        Leg(
            mode=g["mode"],
            from_id=ids[i],
            to_id=ids[i + 1],
            duration_s=times[i],
            distance_m=distances[i],
            geometry=points[g["start"] : g["end"] + 1],
            quality="estimated",
        )
        for i, g in enumerate(groups)
    ]


def number(value) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("Некорректное время или расстояние маршрута")
    return float(value)


def parse_route(
    data: dict, profile: RoadProfile, origin: str, target: str, network_id: str
) -> Journey:
    try:
        trip = data["trip"]
        if (
            type(trip["status"]) is not int
            or trip["status"] != 0
            or trip["units"] != "kilometers"
            or len(trip["legs"]) != 1
        ):
            raise ValueError("Неожиданный формат дорожного маршрута")
        raw = trip["legs"][0]
        shape = raw["shape"]
        if isinstance(shape, str):
            from valhalla.midgard.utils import decode_polyline

            shape = {
                "type": "LineString",
                "coordinates": decode_polyline(shape, precision=6, order="lnglat"),
            }
        if shape["type"] != "LineString":
            raise ValueError("Ожидалась GeoJSON LineString")
        points = []
        for point in shape["coordinates"]:
            if len(point) != 2 or any(isinstance(v, bool) for v in point):
                raise ValueError("Некорректные координаты GeoJSON")
            points.append(Coordinate(lon=point[0], lat=point[1]))
        leg = Leg(
            mode=profile,
            from_id=origin,
            to_id=target,
            duration_s=math.ceil(number(raw["summary"]["time"])),
            distance_m=math.ceil(1000 * number(raw["summary"]["length"])),
            geometry=points,
            quality="estimated",
        )
        return Journey(
            from_id=origin,
            to_id=target,
            status="ok",
            legs=bicycle_legs(raw, points, origin, target) if profile == "bicycle" else [leg],
            network_id=network_id,
            assumptions=ASSUMPTIONS,
        )
    except (KeyError, TypeError, ValueError, IndexError, OverflowError, RuntimeError) as exc:
        raise RoutingError("Некорректный ответ локального Valhalla") from exc


class RoadRouter:
    def __init__(self, graph_dir: Path):
        try:
            import valhalla
        except ImportError as exc:
            raise RoutingError(
                "Установите зависимости маршрутизации: pip install -e '.[routing]'"
            ) from exc
        try:
            manifest = json.loads((graph_dir / "manifest.json").read_text())
            config = json.loads((graph_dir / "valhalla.json").read_text())
            if manifest["schema_version"] != "valhalla_static_v1":
                raise ValueError("Неизвестная версия дорожного графа")
            if manifest["config_hash"] != content_hash(config):
                raise ValueError("Конфигурация графа изменена после сборки")
            if manifest["engine"] != valhalla.__version__:
                raise ValueError("Версия Valhalla отличается от версии сборки графа")
            # A completed build writes manifest.json last. Never route on a partial build.
            if not any((graph_dir / "tiles").rglob("*.gph")):
                raise ValueError("В графе нет дорожных тайлов")
            self.network_id = content_hash(manifest)
            self._assumptions = [*ASSUMPTIONS, *manifest["assumptions"]]
            # Graph bundles can move between machines. Verify the original config
            # first, then relocate only the storage path in memory. Keep network
            # identity (including connected transit and archives) unchanged.
            config.setdefault("mjolnir", {})["tile_dir"] = str((graph_dir / "tiles").resolve())
            # Suppress native logs in journey JSON; this does not affect routing costs.
            config.setdefault("logging", {})["type"] = ""
            self._config = config
            self._actor = valhalla.Actor(config)
        except (OSError, KeyError, ValueError, RuntimeError) as exc:
            raise RoutingError(f"Не удалось открыть дорожный граф: {exc}") from exc
        self._error_type = valhalla.ValhallaError
        self._lock = Lock()

    def fork(self):
        """An independent native actor for one worker; graph and costing stay identical."""
        result = copy(self)
        result._actor = type(self._actor)(self._config)
        result._lock = Lock()
        return result

    def route(
        self,
        profile: RoadProfile,
        origin: str,
        target: str,
        a: Coordinate | None,
        b: Coordinate | None,
    ) -> Journey:
        if profile not in COSTINGS:
            raise RoutingError("Дорожный движок не рассчитывает пассажирские поездки")
        network_id = road_network_id(self, profile)
        base = {"from_id": origin, "to_id": target, "network_id": network_id}
        if a is None or b is None:
            return Journey(**base, status="missing_coordinates")
        if origin == target:
            if a != b:
                raise RoutingError("Одинаковый ID соответствует разным координатам")
            return Journey(**base, status="ok")
        payload = {
            "locations": [{**p.model_dump(), "search_cutoff": 100} for p in (a, b)],
            "costing": COSTINGS[profile],
            "units": "kilometers",
            "shape_format": "polyline6",
            "directions_type": "maneuvers" if profile == "bicycle" else "none",
        }
        try:
            with self._lock:
                data = self._actor.route(payload)
        except self._error_type as exc:
            if exc.code in {170, 442}:
                return Journey(**base, status="unreachable")
            if exc.code == 171:
                return Journey(**base, status="unmatched")
            raise RoutingError(f"Ошибка Valhalla ({exc.code}): {exc.message}") from exc
        except RuntimeError as exc:
            raise RoutingError("Сбой локального Valhalla") from exc
        result = parse_route(data, profile, origin, target, network_id)
        result.assumptions = self._assumptions.copy()
        if (
            separation_m(a, result.legs[0].geometry[0]) > 100
            or separation_m(b, result.legs[-1].geometry[-1]) > 100
        ):
            raise RoutingError("Valhalla привязал адрес дальше 100 м от исходной точки")
        return result
