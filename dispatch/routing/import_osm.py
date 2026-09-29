"""Strict OSM topology import with explicit, versioned interval estimates."""

import math

from dispatch.routing.contracts import Coordinate, Leg, content_hash, separation_m
from dispatch.routing.geometry import length_m, slice_stops
from dispatch.routing.transit import Pattern, TransitNetwork

MODEL = {
    "version": "moscow_intervals_v1",
    "speed_kmh": {"metro": 40, "train": 45, "bus": 20, "tram": 18},
    "headway_s": {"metro": 240, "train": 600, "bus": 600, "tram": 600},
    "dwell_s": 25,
    "entrance_s": 120,
    "platform_s": 25,
    "transfer_s": 180,
    "walking_speed_kmh": 4.5,
}


def point(raw):
    return Coordinate(lon=raw[0], lat=raw[1])


class NetworkBuilder:
    def __init__(self):
        self.nodes, self.labels, self.surface = {}, {}, {}
        self.walking, self.patterns, self.rejected = [], [], []

    def node(self, key, coordinate, label, surface=None):
        if key in self.nodes and self.nodes[key] != coordinate:
            raise ValueError("Конфликт координат OSM ID")
        self.nodes[key] = coordinate
        self.labels[key] = label
        if surface:
            self.surface[key] = surface

    def passage(self, origin, target, overhead):
        a, b = self.nodes[origin], self.nodes[target]
        distance = math.ceil(separation_m(a, b))
        self.walking.append(
            Leg(
                mode="walk",
                from_id=origin,
                to_id=target,
                from_label=self.labels[origin],
                to_label=self.labels[target],
                duration_s=max(
                    1, overhead + math.ceil(distance / (MODEL["walking_speed_kmh"] / 3.6))
                ),
                distance_m=distance,
                geometry=[a, b],
                quality="estimated",
                geometry_quality="estimated",
            )
        )

    def rail(self, data):
        if data["schema_version"] != "rail_osm_v1":
            raise ValueError("Неизвестный формат железнодорожной сети")
        for key, stop in data["stops"].items():
            hub = "rail:" + key
            self.node(hub, point(stop["position"]), stop["name"])
            for entrance in stop["entrances"]:
                key = "entrance:" + entrance["id"]
                self.node(
                    key,
                    point(entrance["position"]),
                    f"{stop['name']} · {entrance['name']}",
                    "entrance",
                )
                if entrance["entry"]:
                    self.passage(key, hub, MODEL["entrance_s"])
                if entrance["exit"]:
                    self.passage(hub, key, MODEL["entrance_s"])
        for route in data["routes"]:
            if not route["tracks_complete"]:
                raise ValueError(f"Неполная геометрия рельсов {route['id']}")
            geometry = [point(p) for p in route["geometry"]]
            shapes = slice_stops(geometry, [point(s["position"]) for s in route["stops"]], 5)
            mode = "metro" if route["mode"] == "subway" else "train"
            line = ("Метро " if mode == "metro" else "МЦК/МЦД ") + route["line"]
            stop_ids = []
            for i, stop in enumerate(route["stops"]):
                coordinate = shapes[i][0] if i < len(shapes) else shapes[-1][-1]
                key = f"platform:{route['id']}:{i}"
                if route["circular"] and i == len(route["stops"]) - 1:
                    key = stop_ids[0]
                hub = "rail:" + stop["id"]
                self.node(key, coordinate, self.labels[hub])
                self.passage(hub, key, MODEL["platform_s"])
                self.passage(key, hub, MODEL["platform_s"])
                stop_ids.append(key)
            self.pattern(route["id"], line, mode, stop_ids, shapes, route["circular"])
        for group in data["transfers"]:
            for a in group:
                for b in group:
                    if a != b:
                        self.passage("rail:" + a, "rail:" + b, MODEL["transfer_s"])

    def pattern(
        self, key, line, mode, stops, shapes, circular=False, boarding=None, alighting=None
    ):
        hops = []
        for a, b, shape in zip(stops, stops[1:], shapes):
            distance = math.ceil(length_m(shape))
            hops.append(
                Leg(
                    mode=mode,
                    from_id=a,
                    to_id=b,
                    geometry=shape,
                    from_label=self.labels[a],
                    to_label=self.labels[b],
                    duration_s=math.ceil(distance / (MODEL["speed_kmh"][mode] / 3.6))
                    + MODEL["dwell_s"],
                    distance_m=distance,
                    line=line,
                    quality="estimated",
                )
            )
        self.patterns.append(
            Pattern(
                id=key,
                line=line,
                mode=mode,
                headway_s=MODEL["headway_s"][mode],
                stops=stops,
                hops=hops,
                circular=circular,
                boarding=boarding,
                alighting=alighting,
            )
        )

    def surface_routes(self, data, bbox=(35.5, 54.4, 40.0, 56.9)):
        elements = {(e["type"], e["id"]): e for e in data["elements"]}
        for relation in data["elements"]:
            tags = relation.get("tags", {})
            if relation["type"] != "relation" or tags.get("route") not in {
                "bus",
                "tram",
                "trolleybus",
            }:
                continue
            stops = [
                m
                for m in relation["members"]
                if m["role"].startswith("stop") and m["type"] == "node"
            ]
            raw_stops = [elements.get(("node", m["ref"])) for m in stops]
            if not any(
                s and bbox[0] <= s["lon"] <= bbox[2] and bbox[1] <= s["lat"] <= bbox[3]
                for s in raw_stops
            ):
                continue
            try:
                if tags.get("public_transport:version") != "2" or len(stops) < 2:
                    raise ValueError("Нет упорядоченных остановок PTv2")
                if any(s is None for s in raw_stops):
                    raise ValueError("Отсутствует узел остановки")
                if tags.get("access") in {"no", "private"} or tags.get("service") in {
                    "night",
                    "school",
                }:
                    raise ValueError("Не дневной общедоступный маршрут")
                ways = [
                    elements.get(("way", m["ref"]))
                    for m in relation["members"]
                    if m["type"] == "way" and m["role"] in {"", "forward", "backward"}
                ]
                if not ways or any(w is None for w in ways):
                    raise ValueError("Отсутствует путь маршрута")
                chain = join_ways([w["nodes"] for w in ways])
                if any(("node", n) not in elements for n in chain):
                    raise ValueError("Отсутствует узел геометрии")
                geometry = [
                    Coordinate(lat=elements["node", n]["lat"], lon=elements["node", n]["lon"])
                    for n in chain
                ]
                positions = [Coordinate(lat=s["lat"], lon=s["lon"]) for s in raw_stops]
                # OSM way orientation need not equal the relation's direction.
                try:
                    shapes = slice_stops(geometry, positions, 5)
                except ValueError:
                    shapes = slice_stops(list(reversed(geometry)), positions, 5)
                ids = []
                for i, s in enumerate(raw_stops):
                    key = f"busstop:n{s['id']}"
                    self.node(key, positions[i], s.get("tags", {}).get("name", "Остановка"))
                    ids.append(key)
                mode = "tram" if tags["route"] == "tram" else "bus"
                line = ("Трамвай " if mode == "tram" else "Автобус ") + tags.get(
                    "ref", str(relation["id"])
                )
                self.pattern(
                    f"osm:r{relation['id']}",
                    line,
                    mode,
                    ids,
                    shapes,
                    boarding=[not m["role"].endswith("exit_only") for m in stops],
                    alighting=[not m["role"].endswith("entry_only") for m in stops],
                )
                # PTv2 places a platform immediately after its stop_position.
                # A platform permits street access; a point on a carriageway
                # alone does not authorize crossing the road or a tram fence.
                for i, member in enumerate(relation["members"][:-1]):
                    if member not in stops:
                        continue
                    platform = relation["members"][i + 1]
                    if not platform["role"].startswith("platform"):
                        continue
                    element = elements.get((platform["type"], platform["ref"]))
                    coordinate = element_center(element, elements)
                    if coordinate is None or element.get("tags", {}).get("access") in {
                        "no",
                        "private",
                    }:
                        continue
                    stop_id = f"busstop:n{member['ref']}"
                    if separation_m(coordinate, self.nodes[stop_id]) > 100:
                        continue
                    platform_id = f"surface:{platform['type'][0]}{platform['ref']}"
                    self.node(
                        platform_id,
                        coordinate,
                        element.get("tags", {}).get("name", self.labels[stop_id]),
                        "stop",
                    )
                    if not platform["role"].endswith("exit_only"):
                        self.passage(platform_id, stop_id, 10)
                    if not platform["role"].endswith("entry_only"):
                        self.passage(stop_id, platform_id, 10)
            except ValueError as exc:
                self.rejected.append(
                    {"id": relation["id"], "ref": tags.get("ref"), "reason": str(exc)}
                )

    def finish(self, metadata):
        return TransitNetwork(
            source="OSM:" + content_hash({"source": metadata, "model": MODEL}),
            assumptions=[
                "© OpenStreetMap contributors, ODbL. Топология — снимок OSM, зафиксированный хешем исходных данных, не историческое расписание заявок.",
                "Модель moscow_intervals_v1: метро 40 км/ч, МЦК/МЦД 45, автобус 20, трамвай 18; остановка 25 с.",
                "Модельные интервалы: метро 4 мин, наземный транспорт и МЦК/МЦД 10 мин. Пробки, расписания, закрытия не учитываются.",
                "Проходы внутри станций и пересадки заданы связями OSM и оценкой времени; их геометрия схематична.",
                "Неполные автобусные линии исключены; отсутствие пути не доказывает отсутствие транспорта в городе.",
            ],
            nodes=self.nodes,
            labels=self.labels,
            surface=self.surface,
            walking=self.walking,
            patterns=self.patterns,
        )


def join_ways(ways: list[list[int]]) -> list[int]:
    """Concatenate ordered route members only at identical OSM node IDs."""
    for reverse in (False, True):
        chain = list(reversed(ways[0])) if reverse else ways[0].copy()
        for way in ways[1:]:
            if chain[-1] == way[0]:
                chain.extend(way[1:])
            elif chain[-1] == way[-1]:
                chain.extend(reversed(way[:-1]))
            else:
                break
        else:
            return chain
    raise ValueError("Разрыв упорядоченной геометрии маршрута")


def element_center(element, elements):
    if not element:
        return None
    if element["type"] == "node":
        return Coordinate(lat=element["lat"], lon=element["lon"])
    if element["type"] == "way":
        nodes = [elements.get(("node", n)) for n in element.get("nodes", [])]
        if nodes and all(nodes):
            return Coordinate(
                lat=sum(n["lat"] for n in nodes) / len(nodes),
                lon=sum(n["lon"] for n in nodes) / len(nodes),
            )
    # Unsupported multipolygon platforms remain unavailable rather than being
    # guessed by proximity or name.
    return None
