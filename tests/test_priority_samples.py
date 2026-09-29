import copy
import json
import subprocess
import sys

import pytest
from conftest import ROOT
from oracle import optimum, route_score
from pydantic import ValidationError

from dispatch.models import Scenario
from dispatch.priority_samples import (
    PriorityCase,
    PriorityCases,
    audit_sources,
    compare_current,
    load_cases,
)
from dispatch.scenarios import make_scenario
from dispatch.work_types import classify_work, unassigned_by_priority

CASES = load_cases().cases


@pytest.mark.parametrize("sample", CASES, ids=lambda c: c.id)
def test_hand_calculated_priority_result_is_feasible_and_globally_optimal(sample):
    case = sample.scenario()
    indices = {r.id: i for i, r in enumerate(case.requests)}
    routes = [[indices[rid] for rid in sample.expected_routes[e.id]] for e in case.engineers]
    score = route_score(case, routes)
    assert score is not None
    assert score[:3] == tuple(sample.expected_missing)
    assert score == optimum(case)
    serialized = case.model_dump_json()
    assert Scenario.model_validate_json(serialized).fingerprint() == case.fingerprint()
    assert sample.scenario().model_dump_json() == serialized


@pytest.mark.parametrize(
    "bk,hd,expected",
    [
        ("Глобальная проблема", "Авария", "emergency"),
        (" Подключение ", "Конвергенция абонента", "installation"),
        ("Дозаказ", "Конвергенция абонента", "additional"),
        ("Дозаказ", "Заказ подключения/Дозаказ оборудования", "additional"),
        ("Подключение", "Заказ подключения/Дозаказ оборудования", "installation"),
        ("Локальная заявка", "Роутер. Замена техническим специалистом", "repair"),
        ("Глобальная проблема", "Информация", None),
        ("Подключение", "Авария", None),
        ("Неизвестно", "Заявка на подключение", None),
        ("", "", None),
    ],
)
def test_work_type_depends_on_classification_not_skill_or_urgent_flag(bk, hd, expected):
    result = classify_work(bk, hd)
    assert result.work_type == expected
    assert bool(result.reason) == (expected is None)


def test_target_counts_keep_repairs_and_additional_jobs_in_same_priority():
    types = {"e": "emergency", "i": "installation", "r": "repair", "a": "additional"}
    assert unassigned_by_priority(types, set()) == (1, 1, 2)
    assert unassigned_by_priority(types, {"e"}) < unassigned_by_priority(types, {"i", "r", "a"})
    assert unassigned_by_priority(types, {"r"}) == unassigned_by_priority(types, {"a"})
    with pytest.raises(ValueError, match="неизвестная заявка"):
        unassigned_by_priority(types, {"unknown"})
    with pytest.raises(ValueError, match="Неизвестный вид"):
        unassigned_by_priority({"x": "unknown"}, set())


@pytest.mark.parametrize(
    "change",
    [
        {"expected_routes": {"unknown_engineer": []}},
        {"expected_routes": {"E1": ["emergency", "emergency"]}},
        {"expected_routes": {"E1": ["unknown_job"]}},
        {"expected_missing": [0, 0, 0]},
        {"roster": ["unknown_engineer"]},
        {"travel_s": -1},
        {"id": "../outside"},
    ],
)
def test_invalid_case_contract_is_rejected(change):
    values = CASES[0].model_dump()
    values.update(change)
    with pytest.raises(ValidationError):
        PriorityCase.model_validate(values)


def test_duplicate_case_and_unknown_work_type_are_rejected():
    payload = load_cases().model_dump()
    payload["cases"].append(copy.deepcopy(payload["cases"][0]))
    with pytest.raises(ValidationError, match="Повтор ID"):
        PriorityCases.model_validate(payload)
    payload["cases"].pop()
    payload["cases"][0]["requests"][0]["work_type"] = "information"
    with pytest.raises(ValidationError):
        PriorityCases.model_validate(payload)


def test_export_is_reproducible_without_input_archive_or_live_api(tmp_path):
    output = tmp_path / "samples"
    command = [sys.executable, "-m", "dispatch.priority_samples", "--output", str(output)]
    subprocess.run(command, check=True, cwd=tmp_path, capture_output=True)
    first = {p.name: p.read_bytes() for p in output.iterdir()}
    assert len(first) == 2 * len(CASES) + 1
    for sample in CASES:
        scenario = Scenario.model_validate_json(first[sample.id + ".scenario.json"])
        expected = json.loads(first[sample.id + ".expected.json"])
        assert scenario.fingerprint() == expected["input_hash"]
        assert expected["routes"] == sample.expected_routes
    subprocess.run(command, check=True, cwd=tmp_path, capture_output=True)
    assert {p.name: p.read_bytes() for p in output.iterdir()} == first


def test_audit_keeps_provided_requests_and_norms_unchanged(imported, policy):
    before = [make_scenario(item, policy).fingerprint() for item in imported]
    audit = audit_sources(ROOT)
    assert sum(row["total"] for row in audit["zones"]) == 205
    assert sum(len(row["unresolved"]) for row in audit["zones"]) == 6
    for row, source in zip(audit["zones"], imported):
        assert row["source_sha256"] == source["report"]["source_sha256"]
        assert sum(row["counts"].values()) == len(source["requests"])
        assert all(r["hd"] == "Информация" for r in row["unresolved"])
    assert [make_scenario(item, policy).fingerprint() for item in imported] == before


def test_current_solver_report_meets_all_nine_priority_targets():
    report = compare_current(load_cases())
    assert len(report["cases"]) == len(CASES)
    for row, sample in zip(report["cases"], CASES):
        assert row["input_hash"] == sample.scenario().fingerprint()
        for name in ("baseline", "plan"):
            assigned = set(row[name]["assigned_ids"])
            assert row[name]["missing_by_priority"] == list(
                unassigned_by_priority(sample.work_types(), assigned)
            )
        assert row["matches_target_coverage"] == (
            row["plan"]["missing_by_priority"] == sample.expected_missing
        )
        assert row["matches_target_coverage"], sample.id
