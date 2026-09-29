from collections import Counter
from zipfile import ZipFile

import pytest
from conftest import ROOT

from dispatch.baseline import solve_baseline
from dispatch.checker import check_plan
from dispatch.importer import ALLOWED, decoded_name, import_sources, parse_csv, read_norms
from dispatch.scenarios import make_scenario


def test_all_three_sources_and_norms(imported):
    assert [len(s["requests"]) for s in imported] == [66, 83, 56]
    assert [s["report"]["source_sha256"] for s in imported] == [
        "35b2e2e34ed488579ed722b55345b73989f277b9b710b66b12d01c5ec38a00b9",
        "c767a9e38f6d21568651cc4dbebe6acaf0a25f72303018a096fcf3c81fc8f331",
        "e553b5b1ad985f2ec62ffe291544a7df1c8622ef5cd80165287d95f45a53a035",
    ]
    for item in imported:
        assert item["report"]["rejected"] == 0
        assert item["report"]["empty_rows"] == 2
        assert item["office_address"]
        assert item["date"] == "2026-08-17"
        assert len(item["requests"]) == len({r.id for r in item["requests"]})
        assert len({r.address for r in item["requests"]}) < len(item["requests"])
        for r in item["requests"]:
            assert (
                r.service_s
                == {
                    "Подключение": 4200,
                    "Дозаказ": 1200,
                    "Локальная заявка": 1800,
                    "Глобальная проблема": 4800,
                }[r.kind]
            )
    assert Counter(r.window_end_s - r.window_start_s for r in imported[1]["requests"]) == {
        7200: 72,
        86280: 11,
    }
    info = [r for s in imported for r in s["requests"] if r.subtype == "Информация"]
    assert info and all(not r.urgent for r in info)


def test_control_csvs_are_never_opened(monkeypatch, policy):
    original = ZipFile.read
    read_csvs = []

    def guarded_read(self, member, *args, **kwargs):
        info = self.getinfo(member) if isinstance(member, str) else member
        name = decoded_name(info)
        if name.endswith(".csv"):
            assert name.split("/")[-1] in ALLOWED, "Control file was opened"
            read_csvs.append(name)
        return original(self, member, *args, **kwargs)

    monkeypatch.setattr(ZipFile, "read", guarded_read)
    import_sources(ROOT / "Обезличивание.zip", ROOT / "Нормативы.xlsx", policy)
    assert len(read_csvs) == 3


def test_bad_row_is_reported_and_blocks_planning(policy):
    norms = read_norms(ROOT / "Нормативы.xlsx")
    text = "Заявка;Тип заявки BK;Тип заявки HD;Начало;Окончание;Район;Адрес\n1;Подключение;FMC;17.08.2026 10:00;17.08.2026 12:00;Центр;Адрес\n2;Неизвестный;FMC;17.08.2026 10:00;17.08.2026 12:00;Центр;Адрес\nАдрес офиса;Офис;;;;;\n"
    result = parse_csv(text.encode("cp1251"), "test.csv", "Восток", norms, policy)
    assert result["report"]["candidate_rows"] == 2
    assert result["report"]["accepted"] == 1
    assert result["report"]["rejected"] == 1
    with pytest.raises(ValueError, match="Исправьте"):
        make_scenario(result, policy)


def test_reproducible_scenarios_and_valid_baseline(imported, policy):
    for item in imported:
        case = make_scenario(item, policy)
        assert case.fingerprint() == make_scenario(item, policy).fingerprint()
        assert case.fingerprint() != make_scenario(item, policy, 8).fingerprint()
        plan = solve_baseline(case)
        assert check_plan(case, plan) == []
        assert plan.metrics.total == len(item["requests"])
