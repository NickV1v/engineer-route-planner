import hashlib
import json
from collections import Counter
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest
from conftest import scenario
from fastapi.testclient import TestClient
from pydantic import ValidationError

from dispatch.api import create_app
from dispatch.baseline import solve_baseline
from dispatch.checker import require_valid_plan
from dispatch.events import DayEvent, apply_event, plan_changes, replan
from dispatch.geocode import (
    GeocodingError,
    PhotonClient,
    auto_match,
    fallback_queries,
    normalize_query,
    parse_photon,
)
from dispatch.geocode import main as geocode_main
from dispatch.geography import CoordinateEdit, GeographyStore, attach_geography, refresh_snapshot
from dispatch.importer import location_id
from dispatch.models import GeoLocation, Plan, Scenario
from dispatch.optimizer import SearchOptions
from dispatch.publication import PublishRequest, publish
from dispatch.scenarios import make_scenario
from dispatch.store import PlanStore, VersionConflict

ADDRESS = "г. Москва, ул. Тестовая, д. 12 к 1"


def feature(**changes):
    props = dict(
        countrycode="RU",
        city="Москва",
        street="Тестовая улица",
        housenumber="12к1",
        osm_type="W",
        osm_id=123,
    )
    props.update(changes)
    return dict(
        type="Feature", geometry=dict(type="Point", coordinates=[37.6, 55.7]), properties=props
    )


def edit(address=ADDRESS, revision=0, **changes):
    return CoordinateEdit(
        address=address, expected_revision=revision, lat=55.7, lon=37.6, **changes
    )


def test_normalization_keeps_settlement_house_and_full_street_names():
    assert normalize_query("г. Ступино, улица Мира, д. 12к1") == "Ступино, улица Мира, 12 к1"
    assert (
        normalize_query("Город Москва, пр-кт.Волгоградский, д. 128 к 5")
        == "Москва, проспект Волгоградский, 128 к5"
    )
    assert auto_match(ADDRESS, feature()["properties"])
    assert auto_match("Москва, Тестовая улица, 83с4", feature(housenumber="83с4")["properties"])
    assert not auto_match(ADDRESS, feature(housenumber="12")["properties"])
    assert not auto_match(ADDRESS, feature(housenumber="120к1")["properties"])
    assert not auto_match(ADDRESS, feature(city="Кашира")["properties"])
    assert not auto_match(
        "Москва, улица Мира, д. 12/2 к 3",
        feature(street="улица Мира", housenumber="3")["properties"],
    )
    assert auto_match(
        "Москва, улица Мира, д. 12/2 к 3",
        feature(street="улица Мира", housenumber="12/2к3")["properties"],
    )
    assert not auto_match(
        "Москва, Симферопольский проезд, д. 7",
        feature(street="Симферопольский бульвар", housenumber="7")["properties"],
    )
    assert not auto_match(
        "Москва, улица Владимира, д.12к1", feature(street="улица Мира")["properties"]
    )
    assert not auto_match(
        "Москва, 2-й Котляковский переулок, д.1",
        feature(street="1-й Котляковский переулок", housenumber="1")["properties"],
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Москва Булатниковский пр-зд. д. 6к1", "Москва Булатниковский проезд, 6 к1"),
        ("г.Город Москва, наб.Семёновская, д. 3/1к2", "Москва, набережная Семёновская, 3/1 к2"),
        ("г. Москва, ул Юных Ленинцев, д 83с 4", "Москва, улица Юных Ленинцев, 83 с4"),
        ("Москва, ул.Грайвороновская, д. 10 к 2", "Москва, улица Грайвороновская, 10 к2"),
        (
            "МО, г. Кашира Кржижановского ул. д. 5/1",
            "Московская область, Кашира Кржижановского улица, 5/1",
        ),
        ("Домодедово, проезд.Советский 1-й, д. 1А", "Домодедово, проезд Советский 1-й, 1а"),
    ],
)
def test_query_spelling_and_house_tokens(raw, expected):
    assert normalize_query(raw) == expected
    assert normalize_query(expected) == expected


def test_ordinal_position_and_quarter_addresses_without_relaxing_house_identity():
    props = feature(city="Домодедово", street="1-й Советский проезд", housenumber="1А")[
        "properties"
    ]
    assert auto_match("Домодедово, проезд.Советский 1-й, д. 1А", props)
    assert not auto_match("Домодедово, проезд.Советский 2-й, д. 1А", props)
    props = feature(street="квартал Самаркандский Бульвар 137А", housenumber="к5")["properties"]
    assert auto_match("Москва, б-р.Самаркандский Квартал 137а, д. к5", props)
    assert not auto_match("Москва, б-р.Самаркандский Квартал 134а, д. к5", props)
    assert not auto_match(
        "Москва, улица Тестовая, д. 5/1", feature(housenumber="5к1")["properties"]
    )


def test_calendar_street_spelling_keeps_day_and_month():
    props = feature(city="Кашира", street="улица 8-го Марта", housenumber="22")["properties"]
    assert auto_match("Кашира, ул.8 Марта, д. 22", props)
    assert not auto_match("Кашира, ул.9 Марта, д. 22", props)
    assert not auto_match("Кашира, ул.8 Мая, д. 22", props)


@pytest.mark.parametrize(
    "address",
    [
        "Москва, улица Новая Тестовая, д. 12к1",
        "Москва, улица Тестовая Новая, д. 12к1",
        "Москва, 2-я Тестовая улица, д. 12к1",
        "Москва, посёлок Новый, Тестовая улица, д. 12к1",
        "Москва, Северный район, Тестовая улица, д. 12к1",
        "Новая Москва, Тестовая улица, д. 12к1",
    ],
)
def test_partial_street_and_locality_names_never_become_exact_matches(address):
    assert not auto_match(address, feature()["properties"])
    assert not fallback_queries(address, {"features": [feature()]})


def test_house_suggestions_explain_difference_without_accepting_a_correction():
    address = "Москва, улица Тестовая, д. 5/1"
    wrong_base = feature(housenumber="4к1")
    same_base = feature(housenumber="5к1", osm_id=124)
    same_base["geometry"]["coordinates"] = [37.61, 55.71]
    payload = {"features": [wrong_base, same_base]}
    record = parse_photon(address, payload, address)
    assert record.status == "review" and record.point is None
    assert record.candidates[0].source == "OpenStreetMap:W124"
    assert "5/1" in record.note and "5к1" in record.note
    assert "подтвердите" in record.note
    queries = fallback_queries(address, payload)
    assert len(queries) == 1 and queries[0][0] == "api/"
    assert queries[0][1]["q"].endswith("5/1")


def test_region_and_extra_locality_are_never_discarded_by_rewriting():
    address = "МО, г. Кашира Кржижановского ул. д. 5/1"
    props = feature(city="Кашира", state="Московская область", street="улица Кржижановского")
    queries = fallback_queries(address, {"features": [props]})
    assert queries[0][1]["q"] == "Московская область, Кашира, улица Кржижановского, 5/1"
    detailed = "Москва, микрорайон Новый, улица Тестовая, д. 12к1"
    assert not fallback_queries(detailed, {"features": [feature(district="микрорайон Новый")]})


@pytest.mark.parametrize("conflict", [False, True])
def test_expanded_search_merges_evidence_and_rechecks_the_whole_trace_offline(
    tmp_path, monkeypatch, conflict
):
    address = "Москва, улица Тестовая, д. 12"
    street = feature(housenumber=None, street=None, name="Тестовая улица", osm_key="highway")
    building = feature(housenumber="12", osm_key="building")
    second = feature(housenumber="12", osm_key="building", osm_id=456)
    if conflict:
        second["geometry"]["coordinates"] = [37.7, 55.7]
    else:
        second = building
    payloads = iter([{"features": [street]}, {"features": [building]}, {"features": [second]}])
    calls = []

    def fetch(request, timeout):
        calls.append(request.full_url)
        return BytesIO(json.dumps(next(payloads)).encode())

    monkeypatch.setattr("dispatch.geocode.urlopen", fetch)
    monkeypatch.setattr("dispatch.geocode.time.sleep", lambda *args: None)
    catalog = GeographyStore(PlanStore(tmp_path / "plans.sqlite"))
    client = PhotonClient(catalog, "https://example.test")
    result = client.lookup(address, expand=True)
    assert result.status == ("review" if conflict else "matched")
    assert len(calls) == 3 and "/structured?" in calls[1] and "layer=house" in calls[2]
    saved = catalog.cache_get(result.query)
    assert len(saved["requests"]) == 3

    def unexpected(*args, **kwargs):
        pytest.fail("A saved pipeline must replay without network access")

    monkeypatch.setattr("dispatch.geocode.urlopen", unexpected)
    assert client.lookup(address, cached_only=True, query=result.query) == result
    assert client.lookup(address, cached_only=True, expand=True) == result
    with pytest.raises(GeocodingError):
        client.lookup(ADDRESS, cached_only=True, query=result.query)
    with pytest.raises(GeocodingError):
        PhotonClient(catalog, "https://other.test").lookup(address, query=result.query)


def test_failure_of_extra_search_preserves_existing_catalog_and_manual_edits(tmp_path, monkeypatch):
    catalog = GeographyStore(PlanStore(tmp_path / "plans.sqlite"))
    before = catalog.edit(location_id(ADDRESS), edit())
    street = feature(housenumber=None)
    calls = []

    def fetch(request, timeout):
        calls.append(request.full_url)
        if len(calls) > 1:
            raise HTTPError(request.full_url, 429, "rate limited", {}, None)
        return BytesIO(json.dumps({"features": [street]}).encode())

    monkeypatch.setattr("dispatch.geocode.urlopen", fetch)
    monkeypatch.setattr("dispatch.geocode.time.sleep", lambda *args: None)
    with pytest.raises(GeocodingError):
        PhotonClient(catalog, "https://example.test").lookup(ADDRESS, expand=True)
    assert len(calls) == 2 and catalog.get(ADDRESS) == before


def test_building_is_selected_among_nearby_pois_but_distant_matches_remain_ambiguous():
    building = feature(osm_key="building", extent=[37.599, 55.701, 37.601, 55.699])
    shop = feature(osm_type="N", osm_id=456, osm_key="shop", name="Магазин")
    shop["geometry"]["coordinates"] = [37.6009, 55.7009]
    for features in ([building, shop], [shop, building]):
        found = parse_photon(ADDRESS, {"features": features}, ADDRESS)
        assert found.status == "matched" and found.point.source == "OpenStreetMap:W123"
    shop["geometry"]["coordinates"] = [37.62, 55.72]
    assert parse_photon(ADDRESS, {"features": [building, shop]}, ADDRESS).status == "review"
    shop["geometry"]["coordinates"] = [37.6001, 55.7001]
    shop["properties"]["osm_key"] = "building"
    assert parse_photon(ADDRESS, {"features": [building, shop]}, ADDRESS).status == "review"


def test_same_point_wrong_address_cannot_hide_a_building_and_exact_candidates_come_first():
    wrong = feature(housenumber="12к2", osm_key="shop", osm_type="N", osm_id=456)
    building = feature(osm_key="building")
    found = parse_photon(ADDRESS, {"features": [wrong, building]}, ADDRESS)
    assert found.point.source == "OpenStreetMap:W123"
    assert found.candidates[0] == found.point
    others = [feature(housenumber=str(n)) for n in range(5)]
    for n, other in enumerate(others):
        other["geometry"]["coordinates"] = [37.61 + n / 1000, 55.71]
    found = parse_photon(ADDRESS, {"features": [*others, building]}, ADDRESS)
    assert found.point == found.candidates[0]
    assert len(found.candidates) == 5


@pytest.mark.parametrize(
    "case",
    json.loads((Path(__file__).parent / "fixtures/geocode_responses.json").read_text())["cases"],
    ids=lambda case: case["address"],
)
def test_recorded_address_regressions_without_network(case):
    result = parse_photon(case["address"], case["payload"], case["query"])
    assert result.status == case["status"]
    assert (result.point.source if result.point else None) == case["source"]


@pytest.mark.parametrize("mode", ["--retry-unresolved", "--improve-unresolved"])
def test_retry_unresolved_skips_accepted_and_manual_addresses(tmp_path, monkeypatch, mode):
    database = tmp_path / "plans.sqlite"
    catalog = GeographyStore(PlanStore(database))
    addresses = [f"Москва, улица Тестовая, д. {n}" for n in range(1, 4)]
    catalog.edit(location_id(addresses[0]), edit(addresses[0]))
    accepted = parse_photon(addresses[1], {"features": [feature(housenumber="2")]}, "old query")
    catalog.save(accepted, 0)
    catalog.save(GeoLocation(location_id=location_id(addresses[2]), address=addresses[2]), 0)
    before = catalog.collect(addresses)
    monkeypatch.setattr(
        "dispatch.geocode.import_sources",
        lambda *args: [
            {
                "id": "Восток",
                "office_address": addresses[0],
                "requests": [type("Job", (), {"address": a})() for a in addresses[1:]],
            }
        ],
    )
    calls = []

    def lookup(self, address, **kwargs):
        calls.append(address)
        return parse_photon(
            address, {"features": [feature(housenumber="3")]}, normalize_query(address)
        )

    monkeypatch.setattr(PhotonClient, "lookup", lookup)
    monkeypatch.setattr(
        "sys.argv",
        [
            "geocode",
            "--url",
            "https://example.test",
            "--database",
            str(database),
            mode,
        ],
    )
    geocode_main()
    after = catalog.collect(addresses)
    assert calls == [addresses[2]]
    for address in addresses[:2]:
        assert after[location_id(address)] == before[location_id(address)]
    assert after[location_id(addresses[2])].status == "matched"


def test_cache_recheck_uses_original_query_and_never_network(tmp_path, monkeypatch):
    catalog = GeographyStore(PlanStore(tmp_path / "plans.sqlite"))
    query = "Москва, улица Тестовая, 12 к 1"
    params = {
        "q": query,
        "limit": 5,
        "lang": "default",
        "lat": 55.6,
        "lon": 37.6,
        "bbox": "35,53,41,57.5",
    }
    key = hashlib.sha256(
        json.dumps(["https://example.test/api/", params], sort_keys=True).encode()
    ).hexdigest()
    catalog.cache_put(key, {"features": [feature()]})

    def unexpected(*args, **kwargs):
        pytest.fail("Cached recheck must not make HTTP requests")

    monkeypatch.setattr("dispatch.geocode.urlopen", unexpected)
    client = PhotonClient(catalog, "https://example.test")
    assert client.lookup(ADDRESS, cached_only=True, query=query).status == "matched"
    with pytest.raises(GeocodingError):
        client.lookup(ADDRESS, cached_only=True, query="missing cache key")


def test_provider_requires_unique_house_match_and_never_uses_city_as_address():
    found = parse_photon(ADDRESS, {"features": [feature()]}, ADDRESS)
    assert found.status == "matched" and found.point.lat == 55.7
    other = feature()
    other["geometry"]["coordinates"][0] += 0.001
    ambiguous = parse_photon(ADDRESS, {"features": [feature(), other]}, ADDRESS)
    assert ambiguous.status == "review" and ambiguous.point is None
    approximate = parse_photon(ADDRESS, {"features": [feature(housenumber=None)]}, ADDRESS)
    assert approximate.status == "review" and approximate.point is None
    foreign = parse_photon(ADDRESS, {"features": [feature(countrycode="DE")]}, ADDRESS)
    assert foreign.status == "missing" and not foreign.candidates
    with pytest.raises(GeocodingError):
        parse_photon(ADDRESS, {"features": [{"geometry": None, "properties": {}}]}, ADDRESS)
    with pytest.raises(GeocodingError):
        parse_photon(ADDRESS, {"error": "unavailable"}, ADDRESS)


def test_cached_lookup_and_provider_failure_do_not_erase_coordinates(tmp_path, monkeypatch):
    catalog = GeographyStore(PlanStore(tmp_path / "plans.sqlite"))
    payload = {"features": [feature()]}
    calls = []

    def fetch(request, timeout):
        calls.append((request.full_url, timeout, request.headers))
        return BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("dispatch.geocode.urlopen", fetch)
    client = PhotonClient(catalog, "https://example.test")
    first = client.lookup(ADDRESS)
    assert client.lookup(ADDRESS) == first
    assert len(calls) == 1
    assert "q=" in calls[0][0] and calls[0][2]["User-agent"].startswith("Vyezd")
    saved = catalog.save(first, 0)

    def fail(*args, **kwargs):
        raise HTTPError("https://example.test", 429, "rate limited", {}, None)

    monkeypatch.setattr("dispatch.geocode.urlopen", fail)
    another = PhotonClient(catalog, "https://different.example.test")
    with pytest.raises(GeocodingError):
        another.lookup(ADDRESS)
    assert catalog.get(ADDRESS) == saved


def test_catalog_revision_conflict_and_idempotent_manual_save(tmp_path):
    catalog = GeographyStore(PlanStore(tmp_path / "plans.sqlite"))
    key = location_id(ADDRESS)
    first = catalog.edit(key, edit())
    assert first.status == "manual" and first.revision == 1
    assert catalog.edit(key, edit()) == first
    with pytest.raises(VersionConflict):
        catalog.edit(key, edit(note="Another edit"))
    second = catalog.edit(key, edit(revision=1, note="Проверен корпус"))
    assert second.revision == 2
    with pytest.raises(ValueError):
        catalog.edit("wrong-id", edit())
    with pytest.raises(ValidationError):
        CoordinateEdit(address=ADDRESS, expected_revision=0, lat=float("nan"), lon=37.6)


def test_geography_keeps_legacy_hash_and_never_changes_matrices(tmp_path):
    case = scenario()
    old_id = case.requests[0].location_id
    case.requests[0].location_id = location_id(case.requests[0].address)
    for matrix in case.matrices.values():
        matrix.locations = [
            case.requests[0].location_id if loc == old_id else loc for loc in matrix.locations
        ]
    values = case.model_dump(
        exclude={
            "geography",
            "roster",
            "objective_policy",
            "equipment_policy",
            "equipment_issued",
            "schedule_policy",
        }
    )
    for value in values["requests"]:
        value.pop("work_type")
    for engineer in values["engineers"]:
        engineer.pop("name")
    legacy = hashlib.sha256(
        json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    assert case.fingerprint() == legacy
    catalog = GeographyStore(PlanStore(tmp_path / "plans.sqlite"))
    catalog.edit(location_id(case.requests[0].address), edit(case.requests[0].address))
    updated = attach_geography(case, catalog)
    assert updated.fingerprint() != legacy
    assert updated.matrices == case.matrices
    assert solve_baseline(updated).routes == solve_baseline(case).routes
    assert case.geography == {}
    with pytest.raises(ValidationError):
        Scenario.model_validate(
            {**case.model_dump(), "geography": {"wrong": catalog.get(ADDRESS).model_dump()}}
        )
    with pytest.raises(ValidationError):
        GeoLocation(location_id="x", address="x", status="matched")


def test_api_coordinate_edit_refresh_history_replay_and_export(tmp_path):
    path = tmp_path / "plans.sqlite"
    with TestClient(create_app(storage_path=path)) as client:
        first = client.post("/api/scenarios/Восток/baseline", json={"engineer_count": 12}).json()
        url = "/api/sessions/" + first["session_id"]
        snapshot_before = client.get(url + "/snapshot").json()
        catalog = client.get(url + "/geography").json()
        row = next(iter(catalog.values()))
        response = client.put(
            "/api/geography/" + row["location_id"], json=edit(row["address"]).model_dump()
        )
        assert response.status_code == 200, response.text
        assert client.get(url).json() == first
        assert client.get(url + "/snapshot").json() == snapshot_before
        refreshed = client.post(
            url + "/geography/refresh", json={"operation_id": "coordinates", "expected_version": 1}
        )
        assert refreshed.status_code == 200, refreshed.text
        second = refreshed.json()
        assert second["version"] == 2 and second["geography_updated"]
        assert second["plan"]["routes"] == first["plan"]["routes"]
        assert second["plan"]["metrics"] == first["plan"]["metrics"]
        assert second["plan"]["input_hash"] != first["plan"]["input_hash"]
        assert len(second["scenario"]["geography"]) == 1
        assert client.get(url + "?version=1").json() == first
        assert (
            client.post(
                url + "/geography/refresh",
                json={"operation_id": "coordinates", "expected_version": 1},
            ).json()
            == second
        )
        assert (
            client.post(
                url + "/geography/refresh", json={"operation_id": "stale", "expected_version": 1}
            ).status_code
            == 409
        )
        assert (
            client.post(
                url + "/geography/refresh",
                json={"operation_id": "unchanged", "expected_version": 2},
            ).status_code
            == 409
        )
        exported = client.get(url + "/snapshot").json()
        require_valid_plan(
            Scenario.model_validate(exported["scenario"]), Plan.model_validate(exported["plan"])
        )
        assert exported["scenario"]["matrices"] == snapshot_before["scenario"]["matrices"]
        assert client.get("/api/map-config").status_code == 200
    with TestClient(create_app(storage_path=path)) as restarted:
        assert restarted.get(url).json() == second


def test_cancel_prunes_unused_coordinates_but_keeps_shared_addresses(tmp_path, imported, policy):
    case = make_scenario(imported[0], policy, 0)
    counts = Counter(r.location_id for r in case.requests)
    unique = next(r for r in case.requests if counts[r.location_id] == 1)
    shared = next(r for r in case.requests if counts[r.location_id] > 1)
    catalog = GeographyStore(PlanStore(tmp_path / "plans.sqlite"))
    for job in (unique, shared):
        catalog.edit(
            job.location_id,
            edit(
                job.address,
                selected_point={
                    "lat": 55.7,
                    "lon": 37.6,
                    "label": "Уточнённый адрес " + job.id,
                    "precision": "manual",
                    "source": "dispatcher",
                },
            ),
        )
    case = attach_geography(case, catalog)
    case, _ = publish(
        case,
        solve_baseline(case),
        PublishRequest(operation_id="publish", expected_version=1, engineer_ids=[]),
    )
    for job in (unique, shared):
        previous = case
        case = apply_event(
            case,
            solve_baseline(case),
            DayEvent(
                event_id=job.location_id,
                expected_version=1,
                at_s=0,
                type="cancel",
                request_id=job.id,
            ),
        )
        assert job.location_id in previous.geography
        assert unique.location_id not in case.geography
        assert shared.location_id in case.geography
        changes = plan_changes(solve_baseline(previous), solve_baseline(case), previous, case)
        assert changes[0]["request_id"] == job.id
        assert changes[0]["address"] == "Уточнённый адрес " + job.id
    # A later refresh must not resurrect coordinates for the cancelled unique address.
    assert attach_geography(case, catalog).geography == case.geography


def test_coordinate_refresh_preserves_published_roster_and_frozen_visits(
    tmp_path, imported, policy
):
    case = make_scenario(imported[0], policy, 12)
    case, published = publish(
        case,
        solve_baseline(case),
        PublishRequest(
            operation_id="publish", expected_version=1, engineer_ids=[e.id for e in case.engineers]
        ),
    )
    case, previous = replan(
        case,
        published["plan"],
        DayEvent(
            event_id="unavailable",
            expected_version=2,
            at_s=43200,
            type="engineer_unavailable",
            engineer_id=case.engineers[0].id,
            search=SearchOptions(time_limit_ms=0, rounds=2),
        ),
    )
    assert any(case.planning_state.frozen_routes.values())
    catalog = GeographyStore(PlanStore(tmp_path / "plans.sqlite"))
    job = case.requests[0]
    catalog.edit(job.location_id, edit(job.address))
    updated, refreshed = refresh_snapshot(case, previous, catalog)
    assert updated.roster == case.roster
    assert updated.planning_state == case.planning_state
    assert updated.matrices == case.matrices
    assert refreshed["frozen_request_ids"] == previous["frozen_request_ids"]
    assert refreshed["event_at_s"] == previous["event_at_s"]
    assert refreshed["plan"].routes == previous["plan"].routes
    require_valid_plan(updated, refreshed["plan"])
