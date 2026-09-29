from pathlib import Path

import pytest

from dispatch.importer import import_sources, read_policy
from dispatch.models import Engineer, Matrix, Request, Scenario

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def disable_live_address_service(monkeypatch):
    monkeypatch.setenv("DADATA_API_KEY", "")
    monkeypatch.setenv("DISPATCH_ROUTING_MODE", "synthetic")


@pytest.fixture(scope="session")
def policy():
    return read_policy(ROOT / "config/scenario.json")


@pytest.fixture(scope="session")
def imported(policy):
    return import_sources(ROOT / "Обезличивание.zip", ROOT / "Нормативы.xlsx", policy)


def request(id="A", **changes):
    values = dict(
        id=id,
        zone="test",
        source_order=0,
        source_line=2,
        location_id=id,
        address=f"Тестовый адрес {id}",
        kind="Подключение",
        subtype="",
        window_start_s=36000,
        window_end_s=43200,
        service_s=4200,
        skill="installation",
    )
    values.update(changes)
    return Request(**values)


def engineer(id="E1", **changes):
    values = dict(
        id=id,
        zone="test",
        skills=["installation"],
        profile="car",
        start_location_id="office",
        shift_start_s=32400,
        shift_end_s=61200,
    )
    values.update(changes)
    return Engineer(**values)


def scenario(requests=None, engineers=None, edges=None):
    requests = [request()] if requests is None else requests
    engineers = [engineer()] if engineers is None else engineers
    locations = list(dict.fromkeys(["office"] + [r.location_id for r in requests]))
    durations = [[0 if a == b else 600 for b in locations] for a in locations]
    distances = [[0 if a == b else 1000 for b in locations] for a in locations]
    for (a, b), (t, d) in (edges or {}).items():
        i, j = locations.index(a), locations.index(b)
        durations[i][j], distances[i][j] = t, d
    matrix = Matrix(locations=locations, duration_s=durations, distance_m=distances)
    return Scenario(
        id="test",
        date="2026-08-17",
        office_address="Тестовый офис",
        requests=requests,
        engineers=engineers,
        matrices={e.profile: matrix for e in engineers},
        manifest={"transport": "test"},
    )
