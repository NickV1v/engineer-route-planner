"""Address-to-address interval routing with a hard total walking budget.

First search without a walking constraint. If the fastest journey satisfies
the budget it is also optimal with that budget. Otherwise use exact Pareto
label setting; a fast path with too much walking cannot hide a usable one.
"""

import heapq
from copy import copy
from dataclasses import dataclass

from dispatch.routing.contracts import Journey, content_hash, separation_m
from dispatch.routing.native import NativeGraph, library
from dispatch.routing.transit import TransitRouter
from dispatch.routing.walking import SpatialIndex

PUBLIC_MODEL = {
    "version": "door_transit_v1",
    "access_m": 1800,
    "entrance_access_m": 2200,
    "direct_walk_m": 2500,
    "total_walk_m": 5000,
    "journey_s": 21600,
    "stop_candidates": 16,
    "entrance_candidates": 12,
    "transfer_m": 600,
    "transfer_candidates": 6,
}


@dataclass(slots=True)
class Label:
    node: tuple
    time: int
    walked: int
    previous: int | None
    legs: tuple


class PublicRouter:
    def __init__(self, network, road_network_id, walking):
        self.router = TransitRouter(network)
        native = library()
        self._native = NativeGraph(self.router._graph, native) if native else None
        self.network = self.router.network
        self.walking = walking
        self.network_id = content_hash(
            {
                "transit": self.router.network_id,
                "roads": road_network_id,
                "model": PUBLIC_MODEL,
                "algorithm": "resource_dijkstra_v2",
            }
        )
        self.stops = SpatialIndex(
            {k: self.network.nodes[k] for k, v in self.network.surface.items() if v == "stop"}
        )
        self.entrances = SpatialIndex(
            {k: self.network.nodes[k] for k, v in self.network.surface.items() if v == "entrance"}
        )
        self._access = {}

    def for_calculation(self, walking):
        """Share immutable topology, but never address access results between runs."""
        router = copy(self)
        router.walking = walking
        router._access = {}
        return router

    def clear_calculation(self):
        self._access.clear()

    def access(self, key, point, outward):
        cache_key = (key, point.lat, point.lon, outward)
        if cache_key in self._access:
            return self._access[cache_key]
        candidates = self.stops.nearby(
            point, PUBLIC_MODEL["access_m"], PUBLIC_MODEL["stop_candidates"]
        )
        candidates += self.entrances.nearby(
            point, PUBLIC_MODEL["entrance_access_m"], PUBLIC_MODEL["entrance_candidates"]
        )
        result = []
        for stop in candidates:
            other = self.network.nodes[stop]
            args = (key, stop, point, other) if outward else (stop, key, other, point)
            journey = self.walking(*args)
            limit = (
                PUBLIC_MODEL["entrance_access_m"]
                if self.network.surface[stop] == "entrance"
                else PUBLIC_MODEL["access_m"]
            )
            if journey.status == "ok" and journey.distance_m <= limit:
                for leg in journey.legs:
                    leg.from_label = self.network.labels.get(leg.from_id, leg.from_label)
                    leg.to_label = self.network.labels.get(leg.to_id, leg.to_label)
                result.append((stop, tuple(journey.legs)))
        self._access[cache_key] = result
        return result

    def labels_from(self, access, *, constrained=True):
        if self._native is not None:
            return self._native.search(access, PUBLIC_MODEL, constrained, Label)
        return self.python_labels_from(access, constrained=constrained)

    def python_labels_from(self, access, *, constrained=True):
        """Independent reference implementation and portable fallback."""
        graph = self.router._graph
        labels, fronts, queue = [], {}, []

        def add(node, duration, walked, previous, legs):
            if duration > PUBLIC_MODEL["journey_s"] or (
                constrained and walked > PUBLIC_MODEL["total_walk_m"]
            ):
                return
            front = fronts.setdefault(node, [])
            if constrained:
                if any(labels[i].time <= duration and labels[i].walked <= walked for i in front):
                    return
                front[:] = [
                    i for i in front if labels[i].time < duration or labels[i].walked < walked
                ]
            else:
                if front and (labels[front[0]].time, labels[front[0]].walked) <= (duration, walked):
                    return
                front.clear()
            index = len(labels)
            labels.append(Label(node, duration, walked, previous, legs))
            front.append(index)
            heapq.heappush(queue, (duration, walked, index))

        for stop, legs in access:
            add(
                (("stop", stop), False),
                sum(leg.duration_s for leg in legs),
                sum(leg.distance_m for leg in legs),
                None,
                legs,
            )
        while queue:
            _, _, i = heapq.heappop(queue)
            label = labels[i]
            if i not in fronts[label.node]:
                continue
            base_node, boarded = label.node
            for node, edge in graph[base_node].items():
                leg = edge["leg"]
                add(
                    (
                        node,
                        boarded
                        or (leg is not None and leg.mode in {"metro", "bus", "train", "tram"}),
                    ),
                    label.time + edge["weight"],
                    label.walked + (leg.distance_m if leg and leg.mode == "walk" else 0),
                    i,
                    (leg,) if leg else (),
                )
        return labels, fronts

    def many(self, origin, coordinates):
        a = coordinates[origin]
        access = self.access(origin, a, True) if a else []
        fast_labels, fast_fronts = (
            self.labels_from(access, constrained=False) if access else ([], {})
        )
        constrained_result = None
        results = {}
        for target, b in coordinates.items():
            base = {"from_id": origin, "to_id": target, "network_id": self.network_id}
            if origin == target:
                results[target] = Journey(**base, status="ok")
                continue
            if a is None or b is None:
                results[target] = Journey(**base, status="missing_coordinates")
                continue
            best = None
            if separation_m(a, b) <= PUBLIC_MODEL["direct_walk_m"]:
                direct = self.walking(origin, target, a, b)
                if direct.status == "ok" and direct.distance_m <= PUBLIC_MODEL["direct_walk_m"]:
                    best = (direct.duration_s, direct.distance_m, None, tuple(direct.legs))
            egress = self.access(target, b, False)

            def choose(labels, fronts, direct, enforce):
                choice = direct
                for stop, legs in egress:
                    duration = sum(leg.duration_s for leg in legs)
                    distance = sum(leg.distance_m for leg in legs)
                    for boarded in (False, True):
                        limit = (
                            PUBLIC_MODEL["total_walk_m"]
                            if boarded
                            else PUBLIC_MODEL["direct_walk_m"]
                        )
                        for index in fronts.get((("stop", stop), boarded), ()):
                            label = labels[index]
                            rank = (label.time + duration, label.walked + distance)
                            if enforce and rank[1] > limit:
                                continue
                            if rank[0] <= PUBLIC_MODEL["journey_s"] and (
                                choice is None or rank < choice[:2]
                            ):
                                choice = (*rank, index, legs)
                return choice

            labels = fast_labels
            fastest = choose(fast_labels, fast_fronts, best, False)
            feasible = choose(fast_labels, fast_fronts, best, True)
            if fastest is not None and (feasible is None or fastest[:2] < feasible[:2]):
                if constrained_result is None:
                    constrained_result = self.labels_from(access)
                labels, fronts = constrained_result
                best = choose(labels, fronts, best, True)
            else:
                best = feasible
            if best is None:
                results[target] = Journey(**base, status="unreachable")
                continue
            _, _, index, tail = best
            chunks = [tail]
            while index is not None:
                label = labels[index]
                chunks.append(label.legs)
                index = label.previous
            legs = [leg.model_copy(deep=True) for chunk in reversed(chunks) for leg in chunk]
            results[target] = Journey(
                **base,
                status="ok",
                legs=legs,
                assumptions=[
                    *self.network.assumptions,
                    "Пешеходные подходы рассчитаны по дорогам. Суммарная ходьба не более 5 км; полностью пеший вариант не более 2,5 км.",
                    "Проверяются до 16 остановок в 1,8 км и 12 входов в 2,2 км; выбор среди доступного покрытия, не всех маршрутов города.",
                    "Каждая посадка включает половину модельного интервала; расчёт статический, предел поездки 6 часов.",
                ],
            )
        return results
