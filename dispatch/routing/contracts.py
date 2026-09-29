"""Versioned inputs and explicit journey legs shared by routing adapters."""

import hashlib
import json
import math
from typing import Literal

from pydantic import Field, model_validator

from dispatch.models import Model

RoadProfile = Literal["car", "walk", "bicycle"]
TransitMode = Literal["metro", "bus", "tram", "train"]
Mode = Literal["car", "walk", "bicycle", "metro", "bus", "tram", "train", "wait"]


class RoutingError(RuntimeError):
    """Unavailable engine, corrupt input, or invalid provider response; not 'no route'."""


def content_hash(value: dict) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


class Coordinate(Model):
    lat: float = Field(ge=-85, le=85, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)


def separation_m(a: Coordinate, b: Coordinate) -> float:
    """Only for snap/geometry validation, never for travel times."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b.lon - a.lon) / 2) ** 2
    )
    return 12_742_000 * math.asin(math.sqrt(min(1, h)))


class Leg(Model):
    mode: Mode
    from_id: str
    to_id: str
    duration_s: int = Field(ge=0, le=1_000_000_000)
    distance_m: int = Field(ge=0, le=1_000_000_000)
    geometry: list[Coordinate] = Field(default_factory=list)
    line: str | None = None
    quality: Literal["network", "estimated"]
    geometry_quality: Literal["network", "estimated"] = "network"
    from_label: str | None = None
    to_label: str | None = None

    @model_validator(mode="after")
    def valid_leg(self):
        if self.mode == "wait":
            if self.distance_m or self.geometry or self.from_id != self.to_id:
                raise ValueError("Ожидание не перемещает пассажира")
        elif len(self.geometry) < 2:
            raise ValueError("Для участка движения нужна геометрия")
        if self.mode in {"metro", "bus", "tram", "train", "wait"} and not self.line:
            raise ValueError("Для поездки и ожидания нужна линия")
        if self.mode in {"car", "walk", "bicycle"} and self.line is not None:
            raise ValueError("У дорожного участка нет линии транспорта")
        if self.distance_m and not self.duration_s:
            raise ValueError("Движение не может занимать ноль секунд")
        return self


class Journey(Model):
    from_id: str
    to_id: str
    status: Literal["ok", "unreachable", "missing_coordinates", "unmatched"]
    legs: list[Leg] = Field(default_factory=list)
    network_id: str = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_journey(self):
        if self.status != "ok" and self.legs:
            raise ValueError("Недоступный маршрут не должен содержать участки")
        if self.status == "ok":
            if not self.legs and self.from_id != self.to_id:
                raise ValueError("Пустой маршрут между разными точками")
            if self.legs and (
                self.legs[0].from_id != self.from_id
                or self.legs[-1].to_id != self.to_id
                or any(a.to_id != b.from_id for a, b in zip(self.legs, self.legs[1:]))
            ):
                raise ValueError("Разрыв цепочки участков")
        return self

    @property
    def duration_s(self) -> int | None:
        return sum(leg.duration_s for leg in self.legs) if self.status == "ok" else None

    @property
    def distance_m(self) -> int | None:
        return sum(leg.distance_m for leg in self.legs) if self.status == "ok" else None

    def report(self) -> dict:
        return {
            **self.model_dump(),
            "duration_s": self.duration_s,
            "distance_m": self.distance_m,
            "boarding_wait_s": sum(leg.duration_s for leg in self.legs if leg.mode == "wait"),
            "walking_s": sum(leg.duration_s for leg in self.legs if leg.mode == "walk"),
        }
