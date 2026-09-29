"""Compact immutable topology and owned results for the exact C++ transit search."""

import ctypes as c
from functools import lru_cache
from pathlib import Path

from dispatch.routing.contracts import RoutingError

ROOT = Path(__file__).resolve().parents[2]
Int = c.c_int64


class Edge(c.Structure):
    _fields_ = [(name, Int) for name in ("target", "time", "walked", "boarded")]


class Seed(c.Structure):
    _fields_ = [(name, Int) for name in ("node", "time", "walked")]


class NativeLabel(c.Structure):
    _fields_ = [(name, Int) for name in ("node", "time", "walked", "previous", "via")]


@lru_cache(maxsize=1)
def library():
    binary = ROOT / "build/dispatch-transit.so"
    source = ROOT / "solver/transit_search.cpp"
    if not binary.is_file():
        return None  # The independent Python reference also supports installations without C++.
    if source.stat().st_mtime_ns > binary.stat().st_mtime_ns:
        raise RoutingError("Ядро транспортного поиска устарело. Выполните make solver.")
    lib = c.CDLL(str(binary))
    signatures = {
        "search": (
            c.c_void_p,
            [c.POINTER(Edge), Int, c.POINTER(Int), Int, c.POINTER(Seed), Int, Int, Int, c.c_int],
        ),
        "label_count": (Int, [c.c_void_p]),
        "labels": (c.POINTER(NativeLabel), [c.c_void_p]),
        "front": (c.POINTER(Int), [c.c_void_p, Int, c.POINTER(Int)]),
        "free": (None, [c.c_void_p]),
    }
    for name, (result, args) in signatures.items():
        function = getattr(lib, f"dispatch_transit_{name}")
        function.restype, function.argtypes = result, args
    return lib


class NativeGraph:
    def __init__(self, graph, lib):
        self.lib = lib
        self.nodes = list(graph)
        self.index = {node: i for i, node in enumerate(self.nodes)}
        self.legs = []
        edges, offsets = [], [0]
        for node in self.nodes:
            for target, edge in graph[node].items():
                leg = edge["leg"]
                edges.append(
                    Edge(
                        self.index[target],
                        edge["weight"],
                        leg.distance_m if leg and leg.mode == "walk" else 0,
                        bool(leg and leg.mode in {"metro", "bus", "tram", "train"}),
                    )
                )
                self.legs.append((leg,) if leg else ())
            offsets.append(len(edges))
        self.edges = (Edge * len(edges))(*edges)
        self.offsets = (Int * len(offsets))(*offsets)

    def search(self, access, model, constrained, label_type):
        seeds = (Seed * len(access))(
            *(
                Seed(
                    self.index[("stop", stop)],
                    sum(leg.duration_s for leg in legs),
                    sum(leg.distance_m for leg in legs),
                )
                for stop, legs in access
            )
        )
        handle = self.lib.dispatch_transit_search(
            self.edges,
            len(self.edges),
            self.offsets,
            len(self.nodes),
            seeds,
            len(seeds),
            model["journey_s"],
            model["total_walk_m"],
            constrained,
        )
        if not handle:
            raise RoutingError("Не удалось выполнить точный транспортный поиск")
        result = SearchResult(self, handle, access, label_type)
        return result, Fronts(result)


class SearchResult:
    def __init__(self, graph, handle, access, label_type):
        self.graph, self.handle, self.access, self.label_type = graph, handle, access, label_type
        self.count = graph.lib.dispatch_transit_label_count(handle)
        self.labels = graph.lib.dispatch_transit_labels(handle)

    def __del__(self):
        self.graph.lib.dispatch_transit_free(self.handle)

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if not 0 <= index < self.count:
            raise IndexError(index)
        label = self.labels[index]
        return self.label_type(
            (self.graph.nodes[label.node // 2], bool(label.node % 2)),
            label.time,
            label.walked,
            None if label.previous == -1 else label.previous,
            self.access[-label.via - 1][1] if label.via < 0 else self.graph.legs[label.via],
        )


class Fronts:
    def __init__(self, result):
        self.result = result

    def get(self, node, default=()):
        graph = self.result.graph
        base, boarded = node
        if base not in graph.index:
            return default
        size = Int()
        values = graph.lib.dispatch_transit_front(
            self.result.handle, graph.index[base] * 2 + bool(boarded), c.byref(size)
        )
        return values[: size.value] if size.value else default
