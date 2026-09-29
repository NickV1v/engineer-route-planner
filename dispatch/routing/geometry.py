"""Slice a mapped vehicle line at ordered stop projections; never bridge a gap."""

import math

from dispatch.routing.contracts import Coordinate, separation_m


def length_m(points: list[Coordinate]) -> float:
    return sum(separation_m(a, b) for a, b in zip(points, points[1:]))


def project(point: Coordinate, a: Coordinate, b: Coordinate) -> tuple[float, Coordinate, float]:
    scale = math.cos(math.radians(point.lat))
    dx, dy = (b.lon - a.lon) * scale, b.lat - a.lat
    px, py = (point.lon - a.lon) * scale, point.lat - a.lat
    fraction = min(1, max(0, (px * dx + py * dy) / (dx * dx + dy * dy))) if dx or dy else 0
    snap = Coordinate(
        lat=a.lat + fraction * (b.lat - a.lat), lon=a.lon + fraction * (b.lon - a.lon)
    )
    return fraction, snap, separation_m(point, snap)


def slice_stops(
    geometry: list[Coordinate], stops: list[Coordinate], max_snap_m=75
) -> list[list[Coordinate]]:
    if len(geometry) < 2 or len(stops) < 2:
        raise ValueError("Недостаточно геометрии/остановок")
    positions = []
    minimum = -1e-7
    for stop in stops:
        best = None
        pad_lat = max_snap_m / 110_000
        pad_lon = pad_lat / max(0.05, math.cos(math.radians(stop.lat)))
        for i in range(max(0, math.floor(minimum)), len(geometry) - 1):
            a, b = geometry[i], geometry[i + 1]
            if (
                stop.lat < min(a.lat, b.lat) - pad_lat
                or stop.lat > max(a.lat, b.lat) + pad_lat
                or stop.lon < min(a.lon, b.lon) - pad_lon
                or stop.lon > max(a.lon, b.lon) + pad_lon
            ):
                continue
            fraction, snap, gap = project(stop, a, b)
            position = i + fraction
            if position <= minimum + 1e-8:
                continue
            candidate = (gap, position, snap)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        if best is None or best[0] > max_snap_m:
            raise ValueError("Остановка не лежит на последовательной геометрии линии")
        _, minimum, snap = best
        positions.append((minimum, snap))
    result = []
    for (start, a), (end, b) in zip(positions, positions[1:]):
        points = [a, *geometry[math.floor(start) + 1 : math.ceil(end)], b]
        if length_m(points) <= 0:
            raise ValueError("Нулевой перегон")
        result.append(points)
    return result
