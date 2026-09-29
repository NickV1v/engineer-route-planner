"""Versioned BK/HD classification and lexicographic coverage priorities."""

from dataclasses import dataclass
from typing import Literal

WorkType = Literal["emergency", "installation", "repair", "additional"]
CLASSIFICATION_VERSION = "bk_hd_work_types_v1"
PRIORITY_POLICY = "work_type_priority_v1"
# Unconfirmed types remain visible and use ordinary priority pending clarification.
PRIORITY_GROUPS = (("emergency",), ("installation",), ("repair", "additional", None))


@dataclass(frozen=True)
class Classification:
    work_type: WorkType | None
    reason: str = ""


def classify_work(bk: str, hd: str) -> Classification:
    """Preserve ambiguous source classifications instead of inventing an emergency."""
    bk, hd = bk.strip(), hd.strip()
    if hd == "Авария":
        if bk == "Глобальная проблема":
            return Classification("emergency")
        return Classification(None, "HD указывает аварию, а BK — другой вид работы")
    if bk == "Глобальная проблема":
        return Classification(None, "Глобальная проблема без подтверждённого типа HD=Авария")
    mapping: dict[str, WorkType] = {
        "Подключение": "installation",
        "Локальная заявка": "repair",
        "Дозаказ": "additional",
    }
    if bk in mapping:
        return Classification(mapping[bk])
    return Classification(None, "Неизвестный или отсутствующий тип BK")


def unassigned_by_priority(
    work_types: dict[str, WorkType | None], assigned_ids: set[str]
) -> tuple[int, int, int]:
    """Missing emergencies, installations, then other work (including unknown types)."""
    if not assigned_ids.issubset(work_types):
        raise ValueError("Назначена неизвестная заявка")
    if any(
        kind not in {k for group in PRIORITY_GROUPS for k in group} for kind in work_types.values()
    ):
        raise ValueError("Неизвестный вид работы")
    return tuple(
        sum(kind in group and rid not in assigned_ids for rid, kind in work_types.items())
        for group in PRIORITY_GROUPS
    )
