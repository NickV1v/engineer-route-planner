import copy

import pytest

from dispatch.address_suggestions import AddressSuggestions, SuggestionError, SuggestionSearch
from dispatch.geography import GeographyStore
from dispatch.importer import location_id
from dispatch.models import GeoLocation, GeoPoint
from dispatch.store import PlanStore


def house(city="Москва", street="Профсоюзная", number="8", block="2", street_type="улица"):
    label = f"г {city}, {street_type} {street}, д {number}" + (f" к {block}" if block else "")
    return {
        "value": label,
        "unrestricted_value": "123456, " + label,
        "data": {
            "country_iso_code": "RU",
            "region": "Москва" if city == "Москва" else "Московская",
            "region_type_full": "город" if city == "Москва" else "область",
            "city": city,
            "street_type_full": street_type,
            "street": street,
            "house_type_full": "дом",
            "house": number,
            "block_type_full": "корпус" if block else None,
            "block": block,
            "house_fias_id": "fixture-house",
            "fias_level": "8",
            "fias_actuality_state": "0",
            "qc_geo": "0",
            "geo_lat": "55.686426",
            "geo_lon": "37.569387",
        },
    }


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("DADATA_API_KEY", "fakeKey")
    return AddressSuggestions(GeographyStore(PlanStore(tmp_path / "geography.sqlite")))


def search(service, address, **kwargs):
    return service.search(SuggestionSearch(query=address, auto_resolve=True, **kwargs))


@pytest.mark.parametrize(
    "address,candidate",
    [
        ("Город Москва, ул.Профсоюзная, д. 8 к 2", house()),
        (
            "Город Москва, проезд.Загорьевский, д. 9",
            house(street="Загорьевский", number="9", block=None, street_type="проезд"),
        ),
        (
            "Домодедово, ул.Гагарина, д. 55/2",
            house(city="Домодедово", street="Гагарина", number="55/2", block=None),
        ),
        (
            "Город Москва, ул.Бирюлёвская, д. 58 к 3",
            house(street="Бирюлёвская", number="58", block="3"),
        ),
    ],
)
def test_unique_exact_house_is_resolved_without_selecting_a_suggestion(
    service, monkeypatch, address, candidate
):
    offered = copy.deepcopy(candidate)
    offered["data"]["geo_lat"] = None
    offered["data"]["geo_lon"] = None
    neighbour = house(number="10")
    calls = []

    def call(query, count):
        calls.append((query, count))
        return [candidate] if count == 1 else [offered, neighbour]

    monkeypatch.setattr(service, "_call", call)
    before = service.geography.get(address)
    result = search(service, address, original_address=address)
    assert result.automatic is not None
    assert result.automatic.point.precision == "house"
    assert result.automatic.label == candidate["value"]
    assert calls == [(address, 20), (candidate["unrestricted_value"], 1)]
    assert service.geography.get(address) == before  # only the editor draft changes


@pytest.mark.parametrize(
    "address,changes",
    [
        ("Москва, Профсоюзная улица, 8/2", {}),
        ("Москва, Профсоюзная улица, 8к2", {"house": "8/2", "block": None}),
        (
            "Домодедово, ул.Гагарина, д. 55/2",
            {"city": "Домодедово", "street": "Гагарина", "house": "55", "block": "2"},
        ),
        (
            "Домодедово, ул.Гагарина, д. 55к2",
            {"city": "Домодедово", "street": "Гагарина", "house": "55/2", "block": None},
        ),
        ("Москва, ул.Профсоюзная, д. 8к2", {"block": "2 стр 1"}),
        ("Москва, ул.Профсоюзная, д. 8к2с1", {}),
        ("Москва, ул.Профсоюзная, д. 8к2", {"city": "Кашира"}),
        ("Москва, ул.Профсоюзная, д. 8к2", {"street": "Новая Профсоюзная"}),
        ("Москва, проезд.Профсоюзный, д. 8к2", {"street": "Профсоюзный"}),
        ("Москва, ул.Профсоюзная, д. 8к2", {"country_iso_code": "BY"}),
        ("Москва, ул.Профсоюзная, д. 8к2", {"house_type_full": "владение"}),
        ("Москва, ул.Профсоюзная, д. 8к2", {"house_fias_id": None}),
        ("Москва, ул.Профсоюзная, д. 8к2", {"fias_level": "7"}),
        ("Москва, ул.Профсоюзная, д. 8к2", {"fias_actuality_state": "1"}),
        ("Москва, ул.Профсоюзная, д. 8к2", {"qc_geo": "1"}),
        ("Москва, ул.Профсоюзная, д. 8к2", {"geo_lat": None}),
        ("Москва, ул.Профсоюзная, д. 8к2", {"flat": "12"}),
        ("Москва, ул.Профсоюзная, д. 8к2", {"city": 42}),
        ("Москва, неизвестный поселок, ул.Профсоюзная, д. 8к2", {}),
    ],
)
def test_fallback_never_guesses_address_components_or_uses_approximate_points(
    service, monkeypatch, address, changes
):
    candidate = house()
    candidate["data"].update(changes)
    monkeypatch.setattr(service, "_call", lambda *args: [candidate])
    assert search(service, address).automatic is None


@pytest.mark.parametrize("kind", ["two_houses", "full_page", "malformed_row"])
def test_ambiguous_or_incomplete_result_set_does_not_select_first_house(service, monkeypatch, kind):
    candidates = [house()]
    if kind == "two_houses":
        second = house()
        second["data"]["house_fias_id"] = "another-house"
        second["data"]["geo_lat"] = "55.7"
        candidates.append(second)
    elif kind == "full_page":
        candidates.extend(house(number=str(n)) for n in range(10, 29))
    else:
        candidates.append({"data": None})

    def call(query, count):
        assert count == 20  # no extra request to choose an arbitrary first result
        return candidates

    monkeypatch.setattr(service, "_call", call)
    result = search(service, "Москва, ул.Профсоюзная, д. 8к2")
    assert result.items and result.automatic is None


@pytest.mark.parametrize("status", ["matched", "manual"])
def test_existing_photon_and_manual_coordinates_prevent_fallback(service, monkeypatch, status):
    address = "Москва, ул.Профсоюзная, д. 8к2"
    record = service.geography.save(
        GeoLocation(
            location_id=location_id(address),
            address=address,
            status=status,
            point=GeoPoint(lat=55.7, lon=37.5, label=address, precision="house", source="test"),
        ),
        expected_revision=0,
    )
    monkeypatch.setattr(service, "_call", lambda *args: pytest.fail("Fallback for accepted point"))
    assert search(service, address).automatic is None
    assert service.geography.get(address) == record


@pytest.mark.parametrize(
    "changes", [{"city": "Кашира"}, {"block": "3"}, {"house_fias_id": "other"}, {"qc_geo": "2"}]
)
def test_resolution_revalidates_address_and_precision(service, monkeypatch, changes):
    changed = house()
    changed["data"].update(changes)
    monkeypatch.setattr(service, "_call", lambda query, count: [changed if count == 1 else house()])
    result = search(service, "Москва, ул.Профсоюзная, д. 8к2")
    assert result.items and result.automatic is None
    assert "не подтверждены" in result.notice


def test_corrected_query_cannot_automatically_relocate_original_address(service, monkeypatch):
    monkeypatch.setattr(service, "_call", lambda *args: [house()])
    result = search(
        service, "Москва, ул.Профсоюзная, д. 8к2", original_address="Москва, ул.Профсоюзная, д. 8/2"
    )
    assert result.items and result.automatic is None


def test_service_failure_during_resolution_keeps_suggestions(service, monkeypatch):
    def call(query, count):
        if count == 1:
            raise SuggestionError("Сервис временно недоступен")
        return [house()]

    monkeypatch.setattr(service, "_call", call)
    result = search(service, "Москва, ул.Профсоюзная, д. 8к2")
    assert result.items and result.automatic is None
    assert "недоступен" in result.notice
