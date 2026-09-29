"""Prepare complete immutable matrix inputs before running either optimizer."""

from collections import Counter
from typing import Callable

from pydantic import model_validator

from dispatch.models import Matrix, Model, Profile, Scenario
from dispatch.routing.contracts import Coordinate, Journey, RoutingError, content_hash


class PreparedTransport(Model):
    coordinates: dict[str, Coordinate | None]
    matrices: dict[Profile, Matrix]
    networks: dict[Profile, str]
    statuses: dict[Profile, dict[str, int]]
    assumptions: list[str]

    @model_validator(mode="after")
    def valid_package(self):
        if (
            not self.matrices
            or set(self.matrices) != set(self.networks)
            or set(self.matrices) != set(self.statuses)
        ):
            raise ValueError("Не совпадают профили транспортного пакета")
        for profile, matrix in self.matrices.items():
            if matrix.locations != list(self.coordinates):
                raise ValueError("Порядок точек не совпадает с матрицами")
            for i, origin in enumerate(matrix.locations):
                for j, target in enumerate(matrix.locations):
                    if (
                        i != j
                        and (self.coordinates[origin] is None or self.coordinates[target] is None)
                        and matrix.duration_s[i][j] is not None
                    ):
                        raise ValueError("Есть переезд без подтверждённых координат")
            counts = self.statuses[profile]
            reachable = sum(
                value is not None
                for i, row in enumerate(matrix.duration_s)
                for j, value in enumerate(row)
                if i != j
            )
            if (
                any(v < 0 for v in counts.values())
                or sum(counts.values()) != len(matrix.locations) ** 2
                or set(counts)
                - {"identity", "ok", "unreachable", "missing_coordinates", "unmatched"}
                or counts.get("identity", 0) != len(matrix.locations)
                or counts.get("ok", 0) != reachable
                or not self.networks[profile]
            ):
                raise ValueError("Некорректная статистика маршрутов")
        return self

    def fingerprint(self) -> str:
        return content_hash(self.model_dump())


def prepare_transport(
    coordinates: dict[str, Coordinate | None],
    routers: dict[Profile, Callable[[str, str], Journey]],
) -> PreparedTransport:
    """No fallback: all profiles finish successfully or nothing is published.

    Callable adapters bind their graph and coordinates before entering this loop.
    Diagonal entries are zero even for unresolved addresses (no movement).
    """
    locations = list(coordinates)
    matrices, networks, statuses = {}, {}, {}
    assumptions = set()
    if not locations or len(locations) > 1000:
        raise ValueError("Нужны 1–1000 точек для подготовки матриц")
    for profile, route in routers.items():
        durations, distances = [], []
        counts = Counter()
        for origin in locations:
            times, lengths = [], []
            for target in locations:
                journey = Journey.model_validate(route(origin, target).model_dump())
                if journey.from_id != origin or journey.to_id != target:
                    raise RoutingError("Маршрутизатор вернул другую пару точек")
                if profile in networks and networks[profile] != journey.network_id:
                    raise RoutingError("Транспортная сеть изменилась во время подготовки")
                networks[profile] = journey.network_id
                assumptions.update(journey.assumptions)
                allowed_modes = (
                    {"walk", "wait", "metro", "bus", "tram", "train"}
                    if profile == "public_transport_approx"
                    else {profile, "walk"}
                )
                if any(leg.mode not in allowed_modes for leg in journey.legs):
                    raise RoutingError("Участки маршрута не соответствуют транспортному профилю")
                if (
                    origin != target
                    and (coordinates[origin] is None or coordinates[target] is None)
                    and journey.status != "missing_coordinates"
                ):
                    raise RoutingError("Маршрут рассчитан без подтверждённых координат")
                if origin == target:
                    times.append(0)
                    lengths.append(0)
                    counts["identity"] += 1
                else:
                    times.append(journey.duration_s)
                    lengths.append(journey.distance_m)
                    counts[journey.status] += 1
            durations.append(times)
            distances.append(lengths)
        matrices[profile] = Matrix(locations=locations, duration_s=durations, distance_m=distances)
        statuses[profile] = dict(counts)
    return PreparedTransport(
        coordinates=coordinates,
        matrices=matrices,
        networks=networks,
        statuses=statuses,
        assumptions=sorted(assumptions),
    )


def apply_transport(scenario: Scenario, transport: PreparedTransport) -> Scenario:
    """Create a new draft input; never overwrite a published or ongoing day."""
    transport = PreparedTransport.model_validate(transport.model_dump())
    if scenario.roster is not None or scenario.planning_state is not None:
        raise ValueError("Транспортные матрицы можно подключить только к новому планированию дня")
    profiles = {engineer.profile for engineer in scenario.engineers}
    if not profiles.issubset(transport.matrices):
        raise ValueError("Нет подготовленных матриц для всех видов передвижения инженеров")
    locations = {r.location_id for r in scenario.requests} | {
        e.start_location_id for e in scenario.engineers
    }
    if not locations.issubset(transport.coordinates):
        raise ValueError("В транспортном пакете отсутствуют точки сценария")
    for key, coordinate in transport.coordinates.items():
        record = scenario.geography.get(key)
        point = record.point if record else None
        actual = Coordinate(lat=point.lat, lon=point.lon) if point else None
        if coordinate != actual:
            raise ValueError("Координаты сценария отличаются от подготовленных матриц")
    manifest = {
        **scenario.manifest,
        "transport": "routing_static_v1",
        "routing": {
            "package_hash": transport.fingerprint(),
            "networks": transport.networks,
            "statuses": transport.statuses,
        },
        "assumptions": [
            *[
                note
                for note in scenario.manifest.get("assumptions", [])
                if note
                != "Матрицы полностью синтетические: не оценивают реальные поездки и пробег."
            ],
            *transport.assumptions,
        ],
    }
    return Scenario.model_validate(
        {**scenario.model_dump(), "matrices": transport.matrices, "manifest": manifest}
    )
