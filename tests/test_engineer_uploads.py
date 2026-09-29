import csv
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_uploads import csv_file

from dispatch.api import create_app
from dispatch.checker import require_valid_plan
from dispatch.engineer_uploads import COLUMNS, validate_engineers_csv
from dispatch.models import Plan
from dispatch.store import PlanStore


def team_csv(rows=None, *, delimiter=";", encoding="utf-8-sig"):
    default = {
        "ID": "007-А",
        "Имя": "Анна Иванова",
        "Навыки": "installation | local",
        "Транспорт": "Автомобиль",
        "Начало смены": "09:00",
        "Конец смены": "18:00",
    }
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=delimiter)
    writer.writerow(COLUMNS)
    for changes in rows if rows is not None else [{}]:
        row = {**default, **changes}
        writer.writerow([row[column] for column in COLUMNS])
    return output.getvalue().encode(encoding)


@pytest.mark.parametrize(
    "delimiter,encoding", [(";", "utf-8-sig"), (",", "utf-8"), ("\t", "cp1251")]
)
def test_engineers_csv_encodings_and_values(delimiter, encoding):
    result, errors = validate_engineers_csv(
        team_csv(delimiter=delimiter, encoding=encoding), "Сотрудники.csv"
    )
    assert not errors
    e = result["engineers"][0]
    assert (e.id, e.name) == ("007-А", "Анна Иванова")
    assert e.skills == ["installation", "local"]
    assert e.profile == "car"
    assert (e.shift_start_s, e.shift_end_s) == (32400, 64800)
    assert result["report"]["accepted"] == 1


@pytest.mark.parametrize(
    "changes,column",
    [
        ({"ID": " "}, "ID"),
        ({"ID": "a" * 151}, "ID"),
        ({"ID": "a\nb"}, "ID"),
        ({"Имя": "x" * 151}, "Имя"),
        ({"Навыки": ""}, "Навыки"),
        ({"Навыки": "installation | installation"}, "Навыки"),
        ({"Навыки": "local | Локальные работы"}, "Навыки"),
        ({"Навыки": "монтаж спутников"}, "Навыки"),
        ({"Навыки": "local |"}, "Навыки"),
        ({"Транспорт": "самолёт"}, "Транспорт"),
        ({"Начало смены": "24:00"}, "Начало смены"),
        ({"Начало смены": "9 утра"}, "Начало смены"),
        ({"Конец смены": "18:60"}, "Конец смены"),
        ({"Конец смены": "09:00"}, "Начало смены / Конец смены"),
        ({"Конец смены": "08:00"}, "Начало смены / Конец смены"),
    ],
)
def test_invalid_engineer_reports_row_and_field(changes, column):
    result, errors = validate_engineers_csv(team_csv([{}, {"ID": "other", **changes}]), "team.csv")
    assert result is None
    assert any(e.line == 3 and e.column == column and len(e.message) > 15 for e in errors)


def test_engineer_limits_duplicate_ids_and_headers():
    for data, name, fragment in [
        (b"", "team.csv", "пуст"),
        (b"\xef\xbb\xbf", "team.csv", "пуст"),
        (team_csv(), "team.xlsx", "CSV"),
        (b"x" * (2 * 1024 * 1024 + 1), "team.csv", "2 МБ"),
        (b"\xff\xfe\x00a", "team.csv", "текстовым"),
        (team_csv([]), "team.csv", "нет инженеров"),
        (team_csv([{}, {}]), "team.csv", "повторяется"),
        (team_csv([{"ID": str(i)} for i in range(31)]), "team.csv", "30 инженеров"),
        (b"ID;ID\n", "team.csv", "не повторяться"),
        (team_csv().replace("Имя".encode(), "Адрес".encode()), "team.csv", "Все начинают из офиса"),
        (team_csv().replace(b"007-", b'"007-') + b'"', "team.csv", "кавычки"),
    ]:
        result, errors = validate_engineers_csv(data, name)
        assert result is None and any(fragment in e.message for e in errors), (fragment, errors)


def test_midnight_end_optional_name_and_all_transport_profiles():
    result, errors = validate_engineers_csv(
        team_csv(
            [
                {"ID": str(i), "Имя": "", "Конец смены": "24:00", "Транспорт": profile}
                for i, profile in enumerate(
                    ["car", "Пешком", "Велосипед", "Общественный транспорт"]
                )
            ]
        ),
        "team.csv",
    )
    assert not errors
    assert [e.profile for e in result["engineers"]] == [
        "car",
        "walk",
        "bicycle",
        "public_transport_approx",
    ]
    assert all(e.name is None and e.shift_end_s == 86400 for e in result["engineers"])
    without_name = team_csv().decode("utf-8-sig").replace("Имя;", "").replace("Анна Иванова;", "")
    assert not validate_engineers_csv(without_name.encode(), "team.csv")[1]


def test_prepared_engineer_file():
    sample = Path(__file__).resolve().parents[1] / "data/samples/engineers/Инженеры.csv"
    result, errors = validate_engineers_csv(sample.read_bytes(), sample.name)
    assert not errors
    assert len(result["engineers"]) == 12
    assert {e.profile for e in result["engineers"]} == {
        "car",
        "walk",
        "bicycle",
        "public_transport_approx",
    }


def test_engineer_validation_is_isolated_and_constraints_reach_plan(tmp_path, monkeypatch):
    path = tmp_path / "plans.sqlite"
    app = create_app(storage_path=path)
    store = PlanStore(path)
    with TestClient(app) as client:

        def validate():
            response = client.post("/api/engineers/validate?filename=team.csv", content=team_csv())
            assert response.status_code == 200
            return response.json()

        def forbidden(*args, **kwargs):
            pytest.fail("Engineer validation must not run planning or mapping")

        with monkeypatch.context() as guard:
            guard.setattr(app.state.routing, "calculation", forbidden)
            guard.setattr(app.state.upload_geocoder, "prepare", forbidden)
            guard.setattr(app.state.address_suggestions, "search", forbidden)
            guard.setattr("dispatch.api.optimize", forbidden)
            data = validate()
        assert data["valid"]
        with store.connection() as db:
            assert db.execute("SELECT count(*) FROM uploads").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        uploaded = client.post("/api/uploads/validate?filename=work.csv", content=csv_file()).json()
        payload = {
            "engineers": data["engineers"],
            "search": {"rounds": 8, "max_evaluations": 100000, "time_limit_ms": 0},
        }
        response = client.post(f"/api/uploads/{uploaded['upload_id']}/optimize", json=payload)
        assert response.status_code == 200, response.text
        run = response.json()
        assert run["plan"]["metrics"]["assigned"] == 1
        assert run["engineers"][0]["id"] == "007-А"
        assert run["engineers"][0]["name"] == "Анна Иванова"
        assert run["engineers"][0]["start_location_id"] == run["scenario"]["office_location_id"]
        scenario, _ = store.get(run["session_id"])
        require_valid_plan(scenario, Plan.model_validate(run["plan"]))
        assert scenario.manifest["engineer_inputs"] == data["engineers"]
        assert client.get(f"/api/sessions/{run['session_id']}").json() == run
        assert validate()["valid"]
        invalid = client.post(
            "/api/engineers/validate?filename=team.csv", content=team_csv([{"Транспорт": "плохой"}])
        ).json()
        assert not invalid["valid"]
        assert client.get(f"/api/sessions/{run['session_id']}").json() == run
        payload["engineers"][0]["skills"] = ["emergency"]
        rejected = client.post(
            f"/api/uploads/{uploaded['upload_id']}/baseline",
            json={"engineers": payload["engineers"]},
        ).json()
        assert rejected["plan"]["metrics"]["assigned"] == 0
