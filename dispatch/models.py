from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dispatch.work_types import WorkType, classify_work

Profile = Literal["car", "walk", "bicycle", "public_transport_approx"]
Skill = Literal["installation", "local", "emergency"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Equipment(Model):
    routers: int = Field(ge=0, le=500)
    set_top_boxes: int = Field(ge=0, le=500)


class Request(Model):
    id: str
    zone: str
    source_order: int = Field(ge=0, le=1_000_000)
    source_line: int = Field(ge=1)
    location_id: str
    address: str
    kind: str
    subtype: str
    window_start_s: int = Field(ge=0, lt=86400)
    window_end_s: int = Field(ge=0, lt=86400)
    service_s: int = Field(gt=0, le=86400)
    skill: Skill
    required_profile: Profile | None = None
    urgent: bool = False
    work_type: WorkType | None = None
    released_at_s: int = Field(default=0, ge=0, lt=86400)
    raw: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_window(self):
        if self.window_start_s > self.window_end_s:
            raise ValueError("Начало окна позже окончания")
        return self


class Engineer(Model):
    id: str
    name: str | None = Field(default=None, min_length=1, max_length=150)
    zone: str
    skills: list[Skill]
    profile: Profile
    start_location_id: str
    shift_start_s: int = Field(ge=0, lt=86400)
    shift_end_s: int = Field(gt=0, le=86400)

    @model_validator(mode="after")
    def valid_shift(self):
        if self.shift_start_s >= self.shift_end_s or not self.skills:
            raise ValueError("Некорректная смена или пустой набор навыков")
        return self


class Matrix(Model):
    locations: list[str] = Field(max_length=1000)
    duration_s: list[list[int | None]]
    distance_m: list[list[int | None]]

    @model_validator(mode="after")
    def valid_matrix(self):
        n = len(self.locations)
        if len(set(self.locations)) != n:
            raise ValueError("Повтор location_id в матрице")
        for values in (self.duration_s, self.distance_m):
            if len(values) != n or any(len(row) != n for row in values):
                raise ValueError("Размер матрицы не совпадает с locations")
        for i in range(n):
            for j in range(n):
                t, d = self.duration_s[i][j], self.distance_m[i][j]
                if (t is None) != (d is None) or any(
                    v is not None and not 0 <= v <= 1_000_000_000 for v in (t, d)
                ):
                    raise ValueError("Некорректное ребро матрицы")
                if i == j and (t != 0 or d != 0):
                    raise ValueError("Диагональ матрицы должна быть нулевой")
        return self


class Visit(Model):
    request_id: str
    arrival_s: int = Field(ge=0)
    start_s: int = Field(ge=0)
    finish_s: int = Field(ge=0)
    travel_s: int = Field(ge=0)
    waiting_s: int = Field(ge=0)
    distance_m: int = Field(ge=0)


class ReplanningPolicy(Model):
    mode: Literal["preserve", "flexible"]
    reference_routes: dict[str, list[Visit]]
    eligible_request_ids: list[str]
    max_delay_s: int = Field(ge=0, le=7200)


class PlanningState(Model):
    at_s: int = Field(ge=0, lt=86400)
    frozen_routes: dict[str, list[Visit]]
    unavailable_engineers: list[str] = Field(default_factory=list)
    policy: ReplanningPolicy | None = None


class GeoPoint(Model):
    lat: float = Field(ge=-85, le=85, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    label: str = Field(max_length=1000)
    precision: Literal["house", "street", "locality", "manual", "unknown"]
    source: str = Field(max_length=300)


class GeoLocation(Model):
    location_id: str
    address: str = Field(min_length=1, max_length=500)
    revision: int = Field(default=0, ge=0)
    status: Literal["matched", "review", "missing", "manual"] = "missing"
    point: GeoPoint | None = None
    candidates: list[GeoPoint] = Field(default_factory=list, max_length=5)
    query: str = ""
    updated_at: str = ""
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def valid_point(self):
        if (self.status in {"matched", "manual"}) != (self.point is not None):
            raise ValueError("Подтверждённый адрес должен иметь координаты")
        if self.status == "matched" and self.point.precision != "house":
            raise ValueError("Автоматически принимается только совпадение до дома")
        return self


class Scenario(Model):
    schema_version: Literal["1.0"] = "1.0"
    id: str
    date: str
    timezone: Literal["Europe/Moscow"] = "Europe/Moscow"
    office_address: str
    requests: list[Request] = Field(max_length=500)
    engineers: list[Engineer] = Field(max_length=100)
    matrices: dict[Profile, Matrix]
    manifest: dict
    objective_policy: Literal["work_type_priority_v1"] | None = None
    schedule_policy: Literal["compact_v1"] | None = None
    equipment_policy: Literal["client_devices_v1"] | None = None
    equipment_issued: dict[str, Equipment] | None = None
    planning_state: PlanningState | None = None
    roster: list[str] | None = None
    geography: dict[str, GeoLocation] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_scenario(self):
        for values in (self.requests, self.engineers):
            if len({v.id for v in values}) != len(values):
                raise ValueError("Повтор ID")
            if any(v.zone != self.id for v in values):
                raise ValueError("Смешаны зоны обслуживания")
        if len({r.source_order for r in self.requests}) != len(self.requests):
            raise ValueError("Повтор source_order")
        if self.objective_policy is not None and any(
            r.work_type != classify_work(r.kind, r.subtype).work_type for r in self.requests
        ):
            raise ValueError("Вид работы не соответствует классификации BK/HD")
        locations = {r.location_id for r in self.requests}
        engineer_ids = {e.id for e in self.engineers}
        if self.roster is not None and (
            len(set(self.roster)) != len(self.roster) or not set(self.roster).issubset(engineer_ids)
        ):
            raise ValueError("Некорректный состав утверждённой смены")
        if self.equipment_issued is not None:
            if self.equipment_policy is None or self.roster is None:
                raise ValueError("Выдача оборудования требует политики и утверждённой смены")
            if set(self.equipment_issued) != engineer_ids:
                raise ValueError("Выдача оборудования должна быть указана для каждого инженера")
            if any(
                (stock.routers or stock.set_top_boxes) and eid not in self.roster
                for eid, stock in self.equipment_issued.items()
            ):
                raise ValueError("Нельзя выдать оборудование инженеру вне смены")
        elif self.equipment_policy is not None and self.roster is not None:
            raise ValueError("В утверждённой смене должна быть зафиксирована выдача оборудования")
        locations.update(e.start_location_id for e in self.engineers)
        geo_locations = locations | {
            loc for matrix in self.matrices.values() for loc in matrix.locations
        }
        if any(
            key != value.location_id or key not in geo_locations
            for key, value in self.geography.items()
        ):
            raise ValueError("Неизвестная точка в географии сценария")
        for e in self.engineers:
            if e.profile not in self.matrices:
                raise ValueError("Нет матрицы транспортного профиля")
        for matrix in self.matrices.values():
            if not locations.issubset(set(matrix.locations)):
                raise ValueError("В матрице отсутствуют точки сценария")
        if self.planning_state:
            state = self.planning_state
            engineers = {e.id for e in self.engineers}
            if not set(state.frozen_routes).issubset(engineers) or not set(
                state.unavailable_engineers
            ).issubset(engineers):
                raise ValueError("Неизвестный инженер в состоянии дня")
            frozen = [v for visits in state.frozen_routes.values() for v in visits]
            if len({v.request_id for v in frozen}) != len(frozen):
                raise ValueError("Повтор закреплённой заявки")
            if not {v.request_id for v in frozen}.issubset({r.id for r in self.requests}):
                raise ValueError("Неизвестная закреплённая заявка")
            if any(
                v.arrival_s - v.travel_s >= state.at_s and v.start_s > state.at_s for v in frozen
            ):
                raise ValueError("Закреплён ещё не начатый выезд")
            if state.policy:
                policy = state.policy
                request_ids = {r.id for r in self.requests}
                reference = [
                    v.request_id for route in policy.reference_routes.values() for v in route
                ]
                if (
                    not set(policy.reference_routes).issubset(engineer_ids)
                    or len(set(reference)) != len(reference)
                    or not set(reference).issubset(request_ids)
                    or len(set(policy.eligible_request_ids)) != len(policy.eligible_request_ids)
                    or not set(policy.eligible_request_ids).issubset(request_ids)
                    or not set(reference).issubset(policy.eligible_request_ids)
                ):
                    raise ValueError("Некорректный предыдущий план в политике перепланирования")
                if self.roster is None:
                    raise ValueError("Для перепланирования нужно утвердить смену")
        return self

    def fingerprint(self) -> str:
        values = self.model_dump()
        if self.objective_policy is None:
            values.pop("objective_policy")
        for field in ("equipment_policy", "equipment_issued", "schedule_policy"):
            if values[field] is None:
                values.pop(field)
        for request in values["requests"]:
            if request["work_type"] is None:
                request.pop("work_type")
        for engineer in values["engineers"]:
            if engineer["name"] is None:
                engineer.pop("name")
        if not self.geography:
            values.pop("geography")
        # Preserve fingerprints of immutable snapshots written before roster/policy support.
        if self.roster is None:
            values.pop("roster")
        if self.planning_state and self.planning_state.policy is None:
            values["planning_state"].pop("policy")
        payload = json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class Route(Model):
    engineer_id: str
    visits: list[Visit]
    distance_m: int


class Rejection(Model):
    request_id: str
    reason: str
    explanation: str
    checks: dict[str, str]


class Metrics(Model):
    total: int
    assigned: int
    unassigned: int
    active_engineers: int
    distance_m: int
    travel_s: int
    waiting_s: int
    service_s: int


class Plan(Model):
    schema_version: Literal["1.0"] = "1.0"
    scenario_id: str
    input_hash: str
    algorithm: Literal["baseline", "cpp_insertion_v1"] = "baseline"
    routes: list[Route]
    unassigned: list[Rejection]
    metrics: Metrics
