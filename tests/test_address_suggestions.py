import copy
import hashlib
import json
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

from dispatch.address_suggestions import (
    AddressSuggestions,
    SuggestionError,
    SuggestionSearch,
    dadata_suggestion,
)
from dispatch.api import create_app
from dispatch.baseline import solve_baseline
from dispatch.events import DayEvent, apply_event
from dispatch.geography import CoordinateEdit, GeographyStore, attach_geography
from dispatch.importer import location_id
from dispatch.models import GeoLocation, GeoPoint
from dispatch.publication import PublishRequest, publish
from dispatch.scenarios import make_scenario
from dispatch.store import PlanStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((ROOT / "tests/fixtures/geocode_audit_responses.json").read_text())["cases"]


def raw_house():
    raw = copy.deepcopy(FIXTURES["fraction_in_registry"]["result"])
    raw["unrestricted_value"] = "142000, " + raw["value"]
    return raw


@pytest.fixture
def service(tmp_path):
    return AddressSuggestions(GeographyStore(PlanStore(tmp_path / "geo.sqlite")))


def test_private_config_is_loaded_without_executing_content_or_exposing_secret(
    service, tmp_path, monkeypatch
):
    path = tmp_path / ".env.local"
    path.write_text("# settings\nDADATA_API_KEY='fakePrivateKey42'\n")
    service.config_file = path
    assert not service.live_available  # explicit empty test environment disables file fallback
    monkeypatch.delenv("DADATA_API_KEY")
    assert service.live_available
    path.write_text("DADATA_API_KEY=$(do-not-execute)\n")
    assert not service.live_available
    monkeypatch.setenv("DADATA_API_KEY", "environmentKey")
    assert service._api_key() == "environmentKey"


def test_local_choices_work_without_a_key_and_never_accept_themselves(service, monkeypatch):
    address = "Москва, улица Тестовая, 12к1"
    service.geography.edit(
        location_id(address),
        CoordinateEdit(address=address, expected_revision=0, lat=55.7, lon=37.6),
    )
    before = service.geography.get(address)
    monkeypatch.setattr(service, "_call", lambda *args: pytest.fail("Unexpected live API call"))
    result = service.search(SuggestionSearch(query="Москва Тестовая 12"))
    assert not result.live_available and result.items
    chosen = service.resolve(result.items[0].id)
    assert chosen.point.lat == 55.7 and chosen.provider == "catalog"
    assert service.geography.get(address) == before


def test_resolve_uses_prior_canonical_value_and_does_not_accept_unregistered_house(
    service, monkeypatch
):
    monkeypatch.setenv("DADATA_API_KEY", "fakeKey")
    raw = raw_house()
    incomplete = copy.deepcopy(raw)
    incomplete["data"]["geo_lat"] = None
    incomplete["data"]["geo_lon"] = None
    calls = []

    def call(query, count):
        calls.append((query, count))
        return [incomplete if count == 5 else raw]

    monkeypatch.setattr(service, "_call", call)
    result = service.search(SuggestionSearch(query="Домодедово Гагарина 55/2"))
    assert result.items[0].point is None
    selected = service.resolve(result.items[0].id)
    assert selected.point and selected.point.precision == "house"
    assert calls[-1] == (raw["unrestricted_value"], 1)
    service.resolve(selected.id)  # already resolved: no further charge
    assert len(calls) == 2
    street = dadata_suggestion(FIXTURES["unregistered_house"]["result"])
    assert street.precision == "street" and street.point is None


def test_search_only_offers_active_registered_houses(service, monkeypatch):
    monkeypatch.setenv("DADATA_API_KEY", "fakeKey")
    house = raw_house()
    rows = [house]
    for changes in (
        {"fias_level": "7"},
        {"house_fias_id": None},
        {"fias_actuality_state": "1"},
        {"house": None},
        {"flat": "15"},
        {"room": "2"},
        {"country_iso_code": "BY"},
    ):
        row = copy.deepcopy(house)
        row["data"].update(changes)
        rows.append(row)
    monkeypatch.setattr(service, "_call", lambda *args: rows)
    result = service.search(SuggestionSearch(query="Домодедово Гагарина 55/2"))
    assert len(result.items) == 1
    assert result.items[0].precision == "house"
    assert len(service._issued) == 1


def test_catalog_excludes_approximate_and_unconfirmed_manual_candidates(service):
    address = "Москва, улица Тестовая, 12"
    candidates = [
        GeoPoint(lat=55.7, lon=37.6, label=address, precision=precision, source="test")
        for precision in ("street", "locality", "unknown", "manual", "house")
    ]
    service.geography.save(
        GeoLocation(
            location_id=location_id(address),
            address=address,
            status="review",
            candidates=candidates,
        ),
        0,
    )
    result = service.search(SuggestionSearch(query=address))
    assert len(result.items) == 1
    assert result.items[0].point == candidates[-1]
    # A manually placed point without a house address must not be called a house.
    service.geography.edit(
        location_id("Москва, улица Тестовая"),
        CoordinateEdit(address="Москва, улица Тестовая", expected_revision=0, lat=55.8, lon=37.7),
    )
    assert len(service.search(SuggestionSearch(query="Москва Тестовая")).items) == 1


def test_changed_house_or_imprecise_coordinates_never_resolve(service, monkeypatch):
    raw = raw_house()
    item = service._remember(dadata_suggestion(raw), raw)
    changed = copy.deepcopy(raw)
    changed["data"]["house"] = "56"
    monkeypatch.setattr(service, "_call", lambda *args: [changed])
    with pytest.raises(SuggestionError, match="не подтверждены"):
        service.resolve(item.id)
    changed["data"]["house"] = raw["data"]["house"]
    changed["data"]["qc_geo"] = "1"
    with pytest.raises(SuggestionError, match="не подтверждены"):
        service.resolve(item.id)


def test_expired_or_unknown_choice_requires_a_new_search(service):
    raw = raw_house()
    item = service._remember(dadata_suggestion(raw), raw)
    service._issued[item.id] = (0, item, raw)
    for key in (item.id, "0" * 32):
        with pytest.raises(SuggestionError, match="устарели"):
            service.resolve(key)


def test_provider_failure_preserves_local_choices_and_never_leaks_key(service, monkeypatch):
    address = "Москва, улица Тестовая, 12"
    service.geography.edit(
        location_id(address),
        CoordinateEdit(address=address, expected_revision=0, lat=55.7, lon=37.6),
    )
    token = "fakePrivateKeyNeverEcho42"
    monkeypatch.setenv("DADATA_API_KEY", token)

    def failed(*args, **kwargs):
        raise HTTPError("https://example.invalid", 403, token, {}, None)

    monkeypatch.setattr("dispatch.address_suggestions.urlopen", failed)
    result = service.search(SuggestionSearch(query=address))
    assert result.items and "недоступен" in result.notice
    assert token not in result.model_dump_json()


def test_outbound_request_is_bounded_post_and_only_uses_server_credential(service, monkeypatch):
    monkeypatch.setenv("DADATA_API_KEY", "fakeKey")

    def response(request, *, timeout):
        assert request.method == "POST"
        assert timeout == 8
        assert request.get_header("Authorization") == "Token fakeKey"
        assert json.loads(request.data)["to_bound"] == {"value": "house"}
        return BytesIO(json.dumps({"suggestions": [raw_house()]}).encode())

    monkeypatch.setattr("dispatch.address_suggestions.urlopen", response)
    assert len(service._call("Москва", 5)) == 1


def test_malformed_rows_are_ignored_and_blank_query_is_rejected(service, monkeypatch):
    monkeypatch.setenv("DADATA_API_KEY", "fakeKey")
    monkeypatch.setattr(service, "_call", lambda *args: [None, {"data": None}, raw_house()])
    assert len(service.search(SuggestionSearch(query="Москва")).items) == 1
    with pytest.raises(ValueError, match="три символа"):
        service.search(SuggestionSearch(query="   "))


def test_api_confirmation_retains_original_address_and_snapshot(tmp_path, monkeypatch):
    app = create_app(ROOT, storage_path=tmp_path / "api.sqlite")
    service = app.state.address_suggestions
    monkeypatch.setenv("DADATA_API_KEY", "fakeKey")
    monkeypatch.setattr(service, "_call", lambda *args: [raw_house()])
    with TestClient(app) as client:
        first = client.post("/api/scenarios/Восток/baseline", json={"engineer_count": 1}).json()
        original = first["scenario"]["requests"][0]["address"]
        key = location_id(original)
        choices = client.post(
            "/api/address-suggestions", json={"query": "Домодедово Гагарина 55/2"}
        ).json()
        selected = client.post(
            "/api/address-suggestions/resolve", json={"suggestion_id": choices["items"][0]["id"]}
        ).json()
        point = selected["point"]
        payload = dict(
            address=original,
            expected_revision=0,
            lat=point["lat"],
            lon=point["lon"],
            selected_point=point,
        )
        response = client.put(f"/api/geography/{key}", json=payload)
        assert response.status_code == 200
        saved = response.json()
        assert saved["address"] == original and saved["status"] == "manual"
        assert saved["point"]["label"] == selected["label"]
        assert saved["point"]["source"].startswith("DaData:")
        assert client.get(f"/api/sessions/{first['session_id']}").json() == first
        assert client.put(f"/api/geography/{key}", json={**payload, "lat": 55.8}).status_code == 422
        assert (
            client.put(f"/api/geography/{key}", json={**payload, "note": "stale"}).status_code
            == 409
        )


def urgent_event(**request_changes):
    return DayEvent(
        event_id="suggestion-test",
        expected_version=1,
        at_s=32400,
        type="add_urgent",
        request={
            "id": "new-geocoded",
            "address": "Москва, улица Тестовая, 123",
            "kind": "Дозаказ",
            "window_start_s": 36000,
            "window_end_s": 72000,
            **request_changes,
        },
    )


def test_new_request_carries_selected_point_without_changing_previous_geography(
    imported, policy, tmp_path
):
    case = make_scenario(imported[0], policy, 3)
    plan = solve_baseline(case)
    case, response = publish(
        case,
        plan,
        PublishRequest(
            operation_id="publish", expected_version=1, engineer_ids=[e.id for e in case.engineers]
        ),
    )
    before = case.model_dump()
    point = GeoPoint(
        lat=55.7, lon=37.6, label="Москва, улица Тестовая, 123", precision="house", source="test"
    )
    event = urgent_event(coordinates=point, original_address="Москва Тестовая 123")
    updated = apply_event(case, response["plan"], event)
    job = updated.requests[-1]
    assert updated.geography[job.location_id].point == point
    assert job.raw["original_address"] == "Москва Тестовая 123"
    assert case.model_dump() == before
    catalog = GeographyStore(PlanStore(tmp_path / "geography.sqlite"))
    assert attach_geography(updated, catalog).geography == updated.geography
    # A duplicate request must not silently move existing requests with this address.
    existing = case.requests[0]
    with pytest.raises(ValueError, match="уже используется"):
        apply_event(
            case, response["plan"], urgent_event(address=existing.address, coordinates=point)
        )


def test_event_fingerprint_remains_compatible_without_selection():
    event = urgent_event()
    old = event.model_dump()
    old["request"].pop("coordinates")
    old["request"].pop("original_address")
    old.pop("mode")
    old.pop("max_delay_s")
    assert (
        event.fingerprint()
        == hashlib.sha256(
            json.dumps(old, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
    )
