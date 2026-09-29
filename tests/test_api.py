from fastapi.testclient import TestClient

from dispatch.api import create_app
from dispatch.optimizer import SolverError


def test_map_config_vector_default_and_explicit_tile_override(monkeypatch, tmp_path):
    monkeypatch.delenv("DISPATCH_MAP_STYLE_URL", raising=False)
    monkeypatch.delenv("DISPATCH_TILE_URL", raising=False)
    with TestClient(create_app(storage_path=tmp_path / "plans.sqlite")) as client:
        config = client.get("/api/map-config").json()
        assert config["style_url"] == "https://tiles.openfreemap.org/styles/bright"
        assert config["tile_url"].startswith("https://tile.openstreetmap.org/")
        # Existing offline/test configuration must disable all external map requests.
        monkeypatch.setenv("DISPATCH_TILE_URL", "")
        config = client.get("/api/map-config").json()
        assert config["style_url"] == config["tile_url"] == ""
        monkeypatch.setenv("DISPATCH_MAP_STYLE_URL", "https://example.test/style.json")
        assert client.get("/api/map-config").json()["style_url"].endswith("/style.json")


def test_api_import_plan_validation_and_isolation(tmp_path):
    with TestClient(create_app(storage_path=tmp_path / "plans.sqlite")) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        zones = client.get("/api/scenarios").json()
        assert [z["requests"] for z in zones] == [66, 83, 56]
        assert len(client.get("/api/scenarios/Восток").json()["requests"]) == 66
        first = client.post("/api/scenarios/Восток/baseline", json={"engineer_count": 12})
        assert first.status_code == 200
        assert first.json()["validation"]["valid"]
        zero = client.post("/api/scenarios/Восток/baseline", json={"engineer_count": 0}).json()
        assert zero["plan"]["metrics"]["assigned"] == 0
        assert zero["plan"]["metrics"]["unassigned"] == 66
        again = client.post("/api/scenarios/Восток/baseline", json={"engineer_count": 12})
        assert first.json()["session_id"] != again.json()["session_id"]
        assert {k: v for k, v in first.json().items() if k != "session_id"} == {
            k: v for k, v in again.json().items() if k != "session_id"
        }
        for count in [-1, 31, 1.5, "12"]:
            assert (
                client.post(
                    "/api/scenarios/Восток/baseline", json={"engineer_count": count}
                ).status_code
                == 422
            )
        assert client.get("/api/scenarios/missing").status_code == 404


def test_optimized_endpoint_and_failure_preserves_baseline(monkeypatch, tmp_path):
    with TestClient(create_app(storage_path=tmp_path / "plans.sqlite")) as client:
        url = "/api/scenarios/Восток"
        baseline = client.post(url + "/baseline", json={"engineer_count": 12}).json()["plan"]
        response = client.post(
            url + "/optimize", json={"engineer_count": 12, "search": {"time_limit_ms": 0}}
        )
        assert response.status_code == 200
        data = response.json()
        assert {k: v for k, v in data["baseline"].items() if k != "input_hash"} == {
            k: v for k, v in baseline.items() if k != "input_hash"
        }
        assert data["comparison"]["scope"] == "morning"
        assert data["baseline"]["input_hash"] == data["comparison"]["input_hash"]
        assert data["staffing"]["status"] == "active"
        assert data["staffing"]["scheduled"] == data["plan"]["metrics"]["active_engineers"]
        assert data["comparison"]["optimized_score"] <= data["comparison"]["baseline_score"]
        assert data["validation"]["valid"]

        def fail(*args, **kwargs):
            raise SolverError("Тестовая ошибка C++")

        monkeypatch.setattr("dispatch.api.optimize", fail)
        assert client.post(url + "/optimize", json={"engineer_count": 12}).status_code == 503
        assert (
            client.post(url + "/baseline", json={"engineer_count": 12}).json()["plan"] == baseline
        )
