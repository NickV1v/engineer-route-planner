"""Versioned display coordinates. They never change travel matrices implicitly."""

import json
from datetime import datetime, timezone

from pydantic import Field, model_validator

from dispatch.checker import require_valid_plan
from dispatch.importer import location_id
from dispatch.models import GeoLocation, GeoPoint, Model, Plan, Scenario
from dispatch.publication import comparison_scenario
from dispatch.store import PlanStore, VersionConflict


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def location_address(record: GeoLocation) -> str:
    """Display the confirmed label while keeping the original address as the lookup key."""
    if record.point and (record.status == "manual" or record.address.startswith("Координаты ")):
        return record.point.label.strip() or record.address
    return record.address


class CoordinateEdit(Model):
    address: str = Field(min_length=3, max_length=500)
    expected_revision: int = Field(ge=0)
    lat: float = Field(ge=-85, le=85, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    note: str = Field(default="", max_length=500)
    selected_point: GeoPoint | None = None

    @model_validator(mode="after")
    def consistent_selection(self):
        if self.selected_point and (
            self.selected_point.lat != self.lat
            or self.selected_point.lon != self.lon
            or self.selected_point.precision not in {"house", "manual"}
        ):
            raise ValueError("Выбранный дом не соответствует координатам")
        return self


class GeographyStore:
    def __init__(self, store: PlanStore):
        self.store = store
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS geography (
                    location_id TEXT PRIMARY KEY, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS geocode_cache (
                    cache_key TEXT PRIMARY KEY, data TEXT NOT NULL
                );
            """)

    def get(self, address: str) -> GeoLocation:
        key = location_id(address)
        with self.store.connection() as db:
            row = db.execute("SELECT data FROM geography WHERE location_id=?", (key,)).fetchone()
        return (
            GeoLocation.model_validate_json(row[0])
            if row
            else GeoLocation(location_id=key, address=address)
        )

    def save(self, record: GeoLocation, expected_revision: int) -> GeoLocation:
        if record.location_id != location_id(record.address):
            raise ValueError("Адрес не соответствует ID точки")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT data FROM geography WHERE location_id=?", (record.location_id,)
            ).fetchone()
            current = GeoLocation.model_validate_json(row[0]) if row else None
            if (
                current
                and current.status == record.status
                and current.point == record.point
                and current.note == record.note
                and current.candidates == record.candidates
                and current.query == record.query
            ):
                return current
            if (current.revision if current else 0) != expected_revision:
                raise VersionConflict("Координаты уже изменились. Обновите список адресов.")
            saved = GeoLocation.model_validate(
                {**record.model_dump(), "revision": expected_revision + 1, "updated_at": utc_now()}
            )
            db.execute(
                "INSERT INTO geography VALUES (?,?) ON CONFLICT(location_id) DO UPDATE SET data=excluded.data",
                (saved.location_id, saved.model_dump_json()),
            )
        return saved

    def edit(self, key: str, data: CoordinateEdit) -> GeoLocation:
        if key != location_id(data.address):
            raise ValueError("Адрес не соответствует ID точки")
        previous = self.get(data.address)
        edited = previous.model_copy(
            update={
                "status": "manual",
                "point": data.selected_point
                or GeoPoint(
                    lat=data.lat,
                    lon=data.lon,
                    label=data.address,
                    precision="manual",
                    source="dispatcher",
                ),
                "note": data.note,
            }
        )
        return self.save(edited, data.expected_revision)

    def cache_get(self, key: str):
        with self.store.connection() as db:
            row = db.execute("SELECT data FROM geocode_cache WHERE cache_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_put(self, key: str, value: dict):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO geocode_cache VALUES (?,?) ON CONFLICT(cache_key) DO NOTHING",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def collect(self, addresses: list[str]) -> dict[str, GeoLocation]:
        result = {}
        for address in addresses:
            record = self.get(address)
            result[record.location_id] = record
        return result


def scenario_addresses(scenario: Scenario) -> list[str]:
    starts = [
        scenario.geography[e.start_location_id].address
        for e in scenario.engineers
        if e.start_location_id in scenario.geography
    ]
    return list(
        dict.fromkeys([scenario.office_address] + [r.address for r in scenario.requests] + starts)
    )


def attach_geography(scenario: Scenario, catalog: GeographyStore) -> Scenario:
    updated = scenario.model_copy(deep=True)
    # Keep coordinates explicitly selected while creating a request, until superseded
    # by a versioned catalog edit. Existing snapshots themselves are never mutated.
    updated.geography.update(
        {
            key: record
            for key, record in catalog.collect(scenario_addresses(scenario)).items()
            if record.revision
            > (scenario.geography[key].revision if key in scenario.geography else 0)
        }
    )
    return Scenario.model_validate(updated.model_dump())


def refresh_snapshot(
    scenario: Scenario, previous: dict, catalog: GeographyStore
) -> tuple[Scenario, dict]:
    plan = require_valid_plan(scenario, Plan.model_validate(previous["plan"]))
    updated = attach_geography(scenario, catalog)
    if updated.geography == scenario.geography:
        raise VersionConflict("Координаты плана уже актуальны")
    response = {**previous, "geography_updated": True}
    # A coordinate update is metadata only while matrices remain synthetic.
    if scenario.manifest.get("transport") != "synthetic_fixture_v1":
        raise ValueError("Для изменения координат нужно заново подготовить дорожные матрицы")
    comparison_input = comparison_scenario(scenario, previous)
    updated_comparison = (
        comparison_input.model_copy(update={"geography": updated.geography}, deep=True)
        if comparison_input is not scenario
        else updated
    )
    for key in ("plan", "baseline"):
        if key in previous:
            old = plan if key == "plan" else Plan.model_validate(previous[key])
            target = updated if key == "plan" else updated_comparison
            old = old.model_copy(update={"input_hash": target.fingerprint()}, deep=True)
            response[key] = require_valid_plan(target, old)
    if previous.get("comparison", {}).get("scope") == "morning":
        response["comparison"] = {
            **previous["comparison"],
            "input_hash": updated_comparison.fingerprint(),
        }
    for key in ("event", "changes", "publication"):
        response.pop(key, None)
    return updated, response
