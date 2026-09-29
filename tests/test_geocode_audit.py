import copy
import json
from pathlib import Path

import pytest

from dispatch.geocode_audit import (
    audit,
    audit_candidate,
    audit_dadata_candidate,
    provider_house,
)

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "geocode_audit_responses.json").read_text()
)["cases"]


@pytest.mark.parametrize(
    ("case", "flag"),
    [
        ("wrong_corpus", "address_mismatch"),
        ("invented_house", "building_position_not_found"),
        ("outside_bbox", "point_outside_bbox"),
        ("old_source", "source_freshness_not_verified"),
    ],
)
def test_geoapify_confidence_does_not_override_identity_or_geometry(case, flag):
    fixture = CASES[case]
    result = audit_candidate(fixture["address"], fixture["result"])
    assert flag in result["flags"]
    assert not result["passes_screening"]


def test_openaddresses_parts_keep_corpus_and_structure_distinct():
    assert provider_house("дом 59, корпус 2, строение 1") == "59к2с1"
    assert provider_house("дом 55/2") != provider_house("дом 55, корпус 2")


def test_dadata_qc_zero_without_registered_house_is_insufficient():
    fixture = CASES["unregistered_house"]
    result = audit_dadata_candidate(fixture["address"], fixture["result"])
    assert result["address_match"]
    assert result["rank"]["qc_geo"] == "0"
    assert "house_not_in_registry" in result["flags"]
    assert not result["passes_screening"]


@pytest.mark.parametrize("name", ["fraction_in_registry", "property_in_registry"])
def test_dadata_preserves_full_number_and_registry_identifier(name):
    fixture = CASES[name]
    result = audit_dadata_candidate(fixture["address"], fixture["result"])
    assert result["passes_screening"]
    assert result["house_fias_id"]
    assert result["components"]["housenumber"] in {"55/2", "9бс1"}
    assert result["house_type"] in {"дом", "владение"}


def test_dadata_distinguishes_utility_structure_from_main_house():
    fixture = CASES["utility_building"]
    result = audit_dadata_candidate(fixture["address"], fixture["result"])
    assert result["components"]["housenumber"] == "8к2с1"
    assert not result["address_match"]


@pytest.mark.parametrize(
    "change", [{"flat": "1"}, {"fias_actuality_state": "99"}, {"geo_lat": "nan"}]
)
def test_dadata_rejects_unrequested_flat_historical_house_and_invalid_point(change):
    fixture = copy.deepcopy(CASES["fraction_in_registry"])
    fixture["result"]["data"].update(change)
    assert not audit_dadata_candidate(fixture["address"], fixture["result"])["passes_screening"]


def test_audit_keeps_reference_status_and_counts_unique_points():
    fixture = CASES["valid_building"]
    baseline = [
        {"location_id": "id", "address": fixture["address"], "status": "review", "point": None}
    ]
    original = copy.deepcopy(baseline)
    result = audit(
        baseline,
        [
            {
                "location_id": "id",
                "address": fixture["address"],
                "payload": {"results": [fixture["result"], fixture["result"]]},
            }
        ],
    )
    assert result["unresolved"]["attempted"] == 1
    assert result["unresolved"]["one_screened_point"] == 1
    assert result["rows"][0]["baseline_status"] == "review"
    assert baseline == original


def test_audit_does_not_mix_denominators_or_accept_address_substitution():
    baseline = [
        {"location_id": "id", "address": "Москва, улица Тестовая, 1", "status": "review"},
        {"location_id": "other", "address": "Москва, улица Тестовая, 2", "status": "matched"},
    ]
    search = {"location_id": "id", "address": baseline[0]["address"], "payload": {"results": []}}
    result = audit(baseline, [search])
    assert result["overall"]["attempted"] == 1
    assert result["accepted_control"]["attempted"] == 0
    assert result["unresolved"]["any_candidate"] == 0
    with pytest.raises(ValueError, match="Duplicate search"):
        audit(baseline, [search, search])
    with pytest.raises(ValueError, match="differs"):
        audit(baseline, [{**search, "address": baseline[1]["address"]}])
