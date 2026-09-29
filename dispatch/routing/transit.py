"""Static interval model, not a timetable router.

Each vehicle pattern has separate onboard positions. A passenger pays half a
headway on boarding, and stays onboard across intermediate stops. Connections
exist only where the input explicitly supplies walking links or shared stop IDs.
"""

from pydantic import Field, model_validator

from dispatch.models import Model
from dispatch.routing.contracts import (
    Coordinate,
    Journey,
    Leg,
    RoutingError,
    TransitMode,
    content_hash,
    separation_m,
)


class Pattern(Model):
    id: str = Field(min_length=1)
    line: str = Field(min_length=1)
    mode: TransitMode
    headway_s: int = Field(gt=0, le=86400)
    stops: list[str] = Field(min_length=2)
    hops: list[Leg]
    circular: bool = False
    boarding: list[bool] | None = None
    alighting: list[bool] | None = None

    @model_validator(mode="after")
    def valid_pattern(self):
        if any(
            flags is not None and len(flags) != len(self.stops)
            for flags in (self.boarding, self.alighting)
        ):
            raise ValueError("Права посадки/высадки не совпадают с остановками")
        if len(self.hops) != len(self.stops) - 1:
            raise ValueError("Число перегонов не совпадает с последовательностью остановок")
        if self.circular and self.stops[0] != self.stops[-1]:
            raise ValueError("Кольцевой вариант должен возвращаться в начальную остановку")
        for a, b, leg in zip(self.stops, self.stops[1:], self.hops):
            if (
                a == b
                or leg.from_id != a
                or leg.to_id != b
                or leg.line != self.line
                or leg.mode != self.mode
                or leg.duration_s <= 0
            ):
                raise ValueError("Некорректный перегон транспортного варианта")
        return self


class TransitNetwork(Model):
    schema_version: str = "static_intervals_v1"
    source: str = Field(min_length=1)
    # The topology is explicitly a snapshot, not proof of historic service dates.
    assumptions: list[str] = Field(min_length=1)
    nodes: dict[str, Coordinate]
    walking: list[Leg]
    patterns: list[Pattern]
    labels: dict[str, str] = Field(default_factory=dict)
    # Only these nodes can be reached from the street. Platforms under ground
    # must be entered through explicitly linked entrances.
    surface: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_network(self):
        if not (self.labels.keys() | self.surface.keys()) <= self.nodes.keys():
            raise ValueError("Подпись или уличная точка ссылается на неизвестный узел")
        if self.schema_version != "static_intervals_v1":
            raise ValueError("Неизвестная версия транспортной сети")
        if len({p.id for p in self.patterns}) != len(self.patterns):
            raise ValueError("Повтор ID транспортного варианта")
        if any(leg.mode != "walk" or leg.duration_s <= 0 for leg in self.walking):
            raise ValueError("Переход должен быть пешим и занимать положительное время")
        for leg in [*self.walking, *(hop for p in self.patterns for hop in p.hops)]:
            if leg.from_id not in self.nodes or leg.to_id not in self.nodes:
                raise ValueError("Неизвестный узел транспортной сети")
            if (
                separation_m(self.nodes[leg.from_id], leg.geometry[0]) > 100
                or separation_m(self.nodes[leg.to_id], leg.geometry[-1]) > 100
            ):
                raise ValueError("Геометрия участка не соответствует его узлам")
        return self


class TransitRouter:
    def __init__(self, network: TransitNetwork):
        try:
            import networkx as nx
        except ImportError as exc:
            raise RoutingError(
                "Установите зависимости маршрутизации: pip install -e '.[routing]'"
            ) from exc

        self._nx = nx
        self.network = TransitNetwork.model_validate(network.model_dump())
        self.network_id = content_hash(self.network.model_dump())
        self._graph = nx.DiGraph()
        for node in self.network.nodes:
            self._graph.add_node(("stop", node))
        for leg in self.network.walking:
            self._edge(("stop", leg.from_id), ("stop", leg.to_id), leg)
        for pattern in self.network.patterns:
            for i, stop in enumerate(pattern.stops):
                waiting = Leg(
                    mode="wait",
                    from_id=stop,
                    to_id=stop,
                    duration_s=(pattern.headway_s + 1) // 2,
                    distance_m=0,
                    line=pattern.line,
                    quality="estimated",
                    from_label=self.network.labels.get(stop),
                    to_label=self.network.labels.get(stop),
                )
                onboard = ("ride", pattern.id, i)
                if pattern.boarding is None or pattern.boarding[i]:
                    self._edge(("stop", stop), onboard, waiting)
                if pattern.alighting is None or pattern.alighting[i]:
                    self._edge(onboard, ("stop", stop), None)
                if i < len(pattern.hops):
                    self._edge(onboard, ("ride", pattern.id, i + 1), pattern.hops[i])
            if pattern.circular:
                self._edge(
                    ("ride", pattern.id, len(pattern.stops) - 1), ("ride", pattern.id, 0), None
                )

    def _edge(self, origin: tuple, target: tuple, leg: Leg | None):
        cost = leg.duration_s if leg else 0
        # Stable selection if two walking links connect the same nodes.
        rank = (cost, leg.distance_m if leg else 0, leg.model_dump_json() if leg else "")
        existing = self._graph.get_edge_data(origin, target)
        if existing is None or rank < existing["rank"]:
            self._graph.add_edge(origin, target, weight=cost, rank=rank, leg=leg)

    def route(self, origin: str, target: str) -> Journey:
        if origin not in self.network.nodes or target not in self.network.nodes:
            raise RoutingError("Точка отсутствует в транспортной сети")
        try:
            path = self._nx.shortest_path(
                self._graph, ("stop", origin), ("stop", target), weight="weight", method="dijkstra"
            )
        except self._nx.NetworkXNoPath:
            return Journey(
                from_id=origin, to_id=target, status="unreachable", network_id=self.network_id
            )
        legs = [self._graph[a][b]["leg"] for a, b in zip(path, path[1:])]
        return Journey(
            from_id=origin,
            to_id=target,
            status="ok",
            network_id=self.network_id,
            legs=[leg.model_copy(deep=True) for leg in legs if leg is not None],
            assumptions=[
                *self.network.assumptions,
                "Ожидание при каждой посадке равно половине интервала; расписания и закрытия не учитываются.",
            ],
        )
