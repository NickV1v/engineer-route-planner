"""Initial calculations fix staffing automatically, while comparisons retain morning inputs."""

import pytest
from conftest import engineer, request, scenario
from fastapi.testclient import TestClient

from dispatch.api import create_app, with_scenario
from dispatch.baseline import solve_baseline
from dispatch.checker import check_plan, require_valid_plan
from dispatch.geography import CoordinateEdit, GeographyStore, refresh_snapshot
from dispatch.importer import location_id
from dispatch.models import Plan, Scenario
from dispatch.optimizer import SearchOptions, optimize
from dispatch.publication import activate_plan, comparison_scenario, start_day
from dispatch.store import PlanStore

SEARCH = SearchOptions(time_limit_ms=0, rounds=8)


def morning():
    case = scenario(
        [
            request(
                "late",
                location_id=location_id("Тестовый адрес late"),
                service_s=600,
                window_start_s=50000,
                window_end_s=55000,
                work_type="installation",
            ),
            request(
                "early",
                location_id=location_id("Тестовый адрес early"),
                source_order=1,
                service_s=600,
                window_end_s=40000,
                work_type="installation",
            ),
        ],
        [engineer(), engineer("E2")],
    )
    case.equipment_policy = "client_devices_v1"
    case.objective_policy = "work_type_priority_v1"
    return case


def test_morning_comparison_keeps_original_staff_and_equipment_inputs():
    case = morning()
    before = case.model_dump_json()
    result = optimize(case, SEARCH)
    assert result["baseline"].metrics.active_engineers == 2
    assert result["plan"].metrics.active_engineers == 1
    active, saved = start_day(case, result)
    assert len(active.roster) == 1
    assert saved["plan"].routes == result["plan"].routes
    assert saved["baseline"] == result["baseline"]
    assert saved["comparison"]["objective_version"] == "work_type_priority_v1"
    assert saved["comparison"]["engineers_delta"] == -1
    assert saved["comparison"]["optimized_score"][3] == 1
    assert active.equipment_issued[active.roster[0]].routers == 2
    assert all(
        s.routers == 0 for eid, s in active.equipment_issued.items() if eid not in active.roster
    )
    draft = comparison_scenario(active, saved)
    assert draft == case
    require_valid_plan(active, saved["plan"])
    require_valid_plan(draft, saved["baseline"])
    # This baseline uses an engineer outside the accepted roster; it is a morning comparison.
    assert check_plan(active, saved["baseline"])
    assert case.model_dump_json() == before
    assert activate_plan(active, saved["plan"]) == (active, saved["plan"])


@pytest.mark.parametrize("endpoint", ["baseline", "optimize"])
@pytest.mark.parametrize("device_free", [False, True])
def test_api_creates_active_version_one_and_replans_without_calling_extra_staff(
    endpoint, device_free, tmp_path, monkeypatch
):
    case = morning()
    if device_free:
        for job in case.requests:
            job.kind, job.work_type, job.skill = "Локальная заявка", "repair", "local"
        for person in case.engineers:
            person.skills = ["local"]
    monkeypatch.setattr("dispatch.api.make_scenario", lambda *args: case.model_copy(deep=True))
    path = tmp_path / "plans.sqlite"
    with TestClient(create_app(storage_path=path)) as client:
        response = client.post(f"/api/scenarios/Восток/{endpoint}", json={"engineer_count": 2})
        assert response.status_code == 200, response.text
        initial = response.json()
        assert initial["version"] == 1
        roster = [r["engineer_id"] for r in initial["plan"]["routes"] if r["visits"]]
        assert initial["staffing"]["engineer_ids"] == roster
        assert initial["staffing"]["status"] == "active"
        url = "/api/sessions/" + initial["session_id"]
        snapshot = client.get(url + "/snapshot").json()
        active = Scenario.model_validate(snapshot["scenario"])
        require_valid_plan(active, Plan.model_validate(initial["plan"]))
        assert active.roster == roster
        if endpoint == "optimize":
            comparison = Scenario.model_validate(snapshot["comparison_scenario"])
            require_valid_plan(comparison, Plan.model_validate(initial["baseline"]))
            assert comparison.roster is None and len(roster) == 1
        payload = {
            "event_id": "stop",
            "expected_version": 1,
            "at_s": 0,
            "type": "engineer_unavailable",
            "engineer_id": roster[0],
            "mode": "flexible",
            "search": SEARCH.model_dump(),
        }
        preview = client.post(url + "/events/preview", json=payload)
        assert preview.status_code == 200, preview.text
        preview = preview.json()
        assert preview["result"]["staffing"]["engineer_ids"] == roster
        assert (
            preview["result"]["scenario"]["equipment_issued"]
            == initial["scenario"]["equipment_issued"]
        )
        for route in preview["result"]["plan"]["routes"]:
            if route["engineer_id"] not in roster or route["engineer_id"] == roster[0]:
                assert route["visits"] == []
        if len(roster) == 1:
            assert preview["result"]["plan"]["metrics"]["assigned"] == 0
        assert client.get(url).json() == initial
        committed = client.post(url + "/events/commit", json={"preview_id": preview["preview_id"]})
        assert committed.status_code == 200, committed.text
        assert committed.json()["version"] == 2
        assert committed.json()["staffing"]["engineer_ids"] == roster
        assert client.get(url + "?version=1").json() == initial
    with TestClient(create_app(storage_path=path)) as client:
        assert client.get(url).json() == committed.json()


@pytest.mark.parametrize("people", [[], [engineer(skills=["local"])]])
def test_zero_assignments_start_an_empty_shift(people):
    case = scenario(engineers=people)
    case.equipment_policy = "client_devices_v1"
    active, result = start_day(case, {"plan": solve_baseline(case)})
    assert active.roster == []
    assert all(s.routers == s.set_top_boxes == 0 for s in active.equipment_issued.values())
    require_valid_plan(active, result["plan"])


def test_legacy_draft_activates_inside_preview_without_rewriting_history(tmp_path):
    case = morning()
    path = tmp_path / "plans.sqlite"
    store = PlanStore(path)
    saved = store.create(case, with_scenario(case, {"plan": optimize(case, SEARCH)["plan"]}))
    url = "/api/sessions/" + saved["session_id"]
    with TestClient(create_app(storage_path=path)) as client:
        response = client.post(
            url + "/events/preview",
            json={
                "event_id": "cancel",
                "expected_version": 1,
                "at_s": 0,
                "type": "cancel",
                "request_id": "late",
                "search": SEARCH.model_dump(),
            },
        )
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["result"]["staffing"]["scheduled"] == 1
        assert (
            sum(s["routers"] for s in preview["result"]["scenario"]["equipment_issued"].values())
            == 2
        )
        assert client.get(url).json() == saved
        assert store.get(saved["session_id"])[0].roster is None
        committed = client.post(url + "/events/commit", json={"preview_id": preview["preview_id"]})
        assert committed.status_code == 200, committed.text
        assert committed.json()["version"] == 2
        assert client.get(url + "?version=1").json() == saved
        # Cancelling the last future job keeps the same engineer on the existing shift.
        last = client.post(
            url + "/events/preview",
            json={
                "event_id": "last",
                "expected_version": 2,
                "at_s": 0,
                "type": "cancel",
                "request_id": "early",
                "search": SEARCH.model_dump(),
            },
        )
        assert last.status_code == 200, last.text
        assert last.json()["result"]["plan"]["metrics"]["assigned"] == 0
        assert (
            last.json()["result"]["staffing"]["engineer_ids"]
            == committed.json()["staffing"]["engineer_ids"]
        )


def test_coordinate_refresh_preserves_roster_and_both_comparison_inputs(tmp_path, monkeypatch):
    case = morning()
    case.manifest["transport"] = "synthetic_fixture_v1"
    active, result = start_day(case, optimize(case, SEARCH))
    catalog = GeographyStore(PlanStore(tmp_path / "geo.sqlite"))
    job = case.requests[0]
    catalog.edit(
        job.location_id,
        CoordinateEdit(
            address=job.address,
            expected_revision=0,
            lat=55.7,
            lon=37.6,
        ),
    )
    collect = catalog.collect

    def concurrent_edit(addresses):
        records = collect(addresses)
        if records[job.location_id].revision == 1:
            catalog.edit(
                job.location_id,
                CoordinateEdit(
                    address=job.address,
                    expected_revision=1,
                    lat=55.8,
                    lon=37.7,
                ),
            )
        return records

    monkeypatch.setattr(catalog, "collect", concurrent_edit)
    updated, refreshed = refresh_snapshot(active, result, catalog)
    assert updated.geography[job.location_id].point.lat == 55.7
    assert catalog.get(job.address).point.lat == 55.8
    assert updated.roster == active.roster
    assert updated.equipment_issued == active.equipment_issued
    require_valid_plan(updated, refreshed["plan"])
    require_valid_plan(comparison_scenario(updated, refreshed), refreshed["baseline"])
    assert refreshed["comparison"]["optimized_score"] == result["comparison"]["optimized_score"]
    assert refreshed["comparison"]["input_hash"] != result["comparison"]["input_hash"]
