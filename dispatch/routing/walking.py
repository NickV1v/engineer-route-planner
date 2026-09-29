"""Bounded real street access, with short snap gaps explicitly marked as estimates."""

import math
from collections import defaultdict

from dispatch.routing.contracts import Coordinate, Journey, Leg, content_hash, separation_m
from dispatch.routing.roads import road_network_id

ACCESS_MODEL = {"version": "door_access_v1", "walk_mps": 1.25, "max_snap_m": 100}


def street_network_id(roads, profile):
    return content_hash({"road_network": road_network_id(roads, profile), "access": ACCESS_MODEL})


def door_journey(roads, profile, origin, target, a, b):
    network_id = street_network_id(roads, profile)
    if origin == target and a == b:
        return Journey(from_id=origin, to_id=target, status="ok", network_id=network_id)
    if a is not None and a == b:
        return Journey(
            from_id=origin,
            to_id=target,
            status="ok",
            network_id=network_id,
            legs=[
                Leg(
                    mode=profile,
                    from_id=origin,
                    to_id=target,
                    duration_s=0,
                    distance_m=0,
                    geometry=[a, b],
                    quality="estimated",
                )
            ],
        )
    result = roads.route(profile, origin, target, a, b)
    base = {**result.model_dump(), "network_id": network_id}
    if result.status != "ok" or origin == target:
        return Journey.model_validate(base)
    # model_dump already made independent nested dictionaries. Reconstruct once
    # below instead of deep-copying every coordinate and then validating it again.
    network_legs = base["legs"]
    # Distinct endpoint IDs keep the topology explicit even if a gap rounds to 0 m.
    network_legs[0]["from_id"], network_legs[-1]["to_id"] = origin + ":snap", target + ":snap"
    legs = []
    for p, q, from_id, to_id in [
        (a, result.legs[0].geometry[0], origin, network_legs[0]["from_id"]),
        (result.legs[-1].geometry[-1], b, network_legs[-1]["to_id"], target),
    ]:
        gap = math.ceil(separation_m(p, q))
        if gap > ACCESS_MODEL["max_snap_m"]:
            raise ValueError("Привязка к дороге дальше допустимого радиуса")
        legs.append(
            Leg(
                mode="walk",
                from_id=from_id,
                to_id=to_id,
                geometry=[p, q],
                duration_s=math.ceil(gap / ACCESS_MODEL["walk_mps"]),
                distance_m=gap,
                quality="estimated",
                geometry_quality="estimated",
            )
        )
    base["legs"] = [legs[0], *network_legs, legs[1]]
    base["assumptions"] = [
        note for note in result.assumptions if "проход от двери до сети не включён" not in note
    ] + [
        "Короткий подход к дорожной сети в пределах 100 м включён с оценкой 4,5 км/ч; его геометрия схематична."
    ]
    return Journey.model_validate(base)


class SpatialIndex:
    """Candidate selection only. Acceptance always checks actual walking distance."""

    def __init__(self, nodes: dict[str, Coordinate]):
        self.nodes = nodes
        self.grid = defaultdict(list)
        for key, point in nodes.items():
            self.grid[self.cell(point)].append(key)

    @staticmethod
    def cell(point):
        return math.floor(point.lon / 0.01), math.floor(point.lat / 0.01)

    def nearby(self, point, radius_m, limit):
        x, y = self.cell(point)
        dx = math.ceil(radius_m / (111_000 * max(0.05, math.cos(math.radians(point.lat)))) / 0.01)
        dy = math.ceil(radius_m / 111_000 / 0.01)
        candidates = []
        for i in range(x - dx, x + dx + 1):
            for j in range(y - dy, y + dy + 1):
                for key in self.grid.get((i, j), ()):
                    distance = separation_m(point, self.nodes[key])
                    if distance <= radius_m:
                        candidates.append((distance, key))
        return [key for _, key in sorted(candidates)[:limit]]
