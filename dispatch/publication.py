"""Fix the calculated roster and equipment; retain the legacy publication API."""

import hashlib
import json

from pydantic import Field

from dispatch.baseline import solve_baseline
from dispatch.checker import require_valid_plan
from dispatch.equipment import equipment_required
from dispatch.models import Model, Plan, Scenario
from dispatch.objective import compare_plans
from dispatch.store import VersionConflict


class PublishRequest(Model):
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    expected_version: int = Field(ge=1)
    engineer_ids: list[str] = Field(max_length=100)

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()


def publish(scenario: Scenario, plan: Plan, request: PublishRequest) -> tuple[Scenario, dict]:
    require_valid_plan(scenario, plan)
    if scenario.roster is not None:
        raise VersionConflict("Состав смены уже утверждён")
    ids = set(request.engineer_ids)
    if len(ids) != len(request.engineer_ids) or not ids.issubset(
        {e.id for e in scenario.engineers}
    ):
        raise ValueError("Некорректный состав смены")
    needed = {r.engineer_id for r in plan.routes if r.visits}
    if not needed.issubset(ids):
        raise ValueError(
            "Нельзя исключить инженера с назначенными работами. Сначала измените начальный план."
        )
    updated, accepted = _fix_roster(scenario, plan, ids)
    baseline = require_valid_plan(updated, solve_baseline(updated))
    return updated, {
        "plan": accepted,
        "baseline": baseline,
        "comparison": compare_plans(updated, baseline, accepted),
        "publication": True,
        "frozen_request_ids": [
            v.request_id for route in updated.planning_state.frozen_routes.values() for v in route
        ]
        if updated.planning_state
        else [],
        **({"event_at_s": updated.planning_state.at_s} if updated.planning_state else {}),
    }


def _fix_roster(scenario: Scenario, plan: Plan, ids: set[str]) -> tuple[Scenario, Plan]:
    updated = scenario.model_copy(deep=True)
    updated.roster = [e.id for e in scenario.engineers if e.id in ids]
    if updated.equipment_policy is not None:
        requests = {r.id: r for r in updated.requests}
        updated.equipment_issued = {
            route.engineer_id: equipment_required(requests[v.request_id] for v in route.visits)
            for route in plan.routes
        }
    updated = Scenario.model_validate(updated.model_dump())
    accepted = plan.model_copy(update={"input_hash": updated.fingerprint()}, deep=True)
    require_valid_plan(updated, accepted)
    return updated, accepted


def activate_plan(scenario: Scenario, plan: Plan) -> tuple[Scenario, Plan]:
    """Fix exactly the assigned staff and equipment; never shrink an existing day's roster."""
    require_valid_plan(scenario, plan)
    if scenario.roster is not None:
        return scenario, plan
    return _fix_roster(scenario, plan, {r.engineer_id for r in plan.routes if r.visits})


def start_day(scenario: Scenario, response: dict) -> tuple[Scenario, dict]:
    """Keep the morning comparison on its original inputs, then freeze the accepted roster."""
    if scenario.roster is not None or scenario.planning_state is not None:
        raise ValueError("Исходный расчёт требует нового дня")
    plan = require_valid_plan(scenario, Plan.model_validate(response["plan"]))
    result = dict(response)
    if "baseline" in response:
        baseline = require_valid_plan(scenario, Plan.model_validate(response["baseline"]))
        result["comparison"] = {
            **compare_plans(scenario, baseline, plan),
            "scope": "morning",
            "input_hash": scenario.fingerprint(),
        }
    active, result["plan"] = activate_plan(scenario, plan)
    return active, result


def comparison_scenario(scenario: Scenario, response: dict) -> Scenario:
    """Reconstruct the morning input without duplicating large matrices in stored responses."""
    comparison = response.get("comparison") or {}
    if comparison.get("scope") != "morning":
        return scenario
    if scenario.planning_state is not None:
        raise ValueError("Утреннее сравнение не может использовать состояние перепланирования")
    draft = scenario.model_copy(update={"roster": None, "equipment_issued": None}, deep=True)
    if draft.fingerprint() != comparison["input_hash"]:
        raise ValueError("Вход утреннего сравнения не соответствует сохранённому хешу")
    return draft


def staffing(scenario: Scenario, plan: Plan) -> dict:
    scheduled = scenario.roster
    if scheduled is None:
        return {
            "status": "draft",
            "engineer_ids": [],
            "scheduled": 0,
            "unavailable": 0,
            "with_work": plan.metrics.active_engineers,
            "reserve_ids": [],
        }
    state = scenario.planning_state
    unavailable = set(state.unavailable_engineers) if state else set()
    at = state.at_s if state else min((e.shift_start_s for e in scenario.engineers), default=0)
    by_id = {e.id: e for e in scenario.engineers}
    reserves = [
        r.engineer_id
        for r in plan.routes
        if r.engineer_id in scheduled
        and r.engineer_id not in unavailable
        and by_id[r.engineer_id].shift_start_s <= at < by_id[r.engineer_id].shift_end_s
        and not any(v.finish_s > at for v in r.visits)
    ]
    return {
        "status": "active",
        "engineer_ids": scheduled,
        "scheduled": len(scheduled),
        "unavailable": len(unavailable & set(scheduled)),
        "with_work": sum(
            r.engineer_id in scheduled
            and r.engineer_id not in unavailable
            and any(v.finish_s > at for v in r.visits)
            for r in plan.routes
        ),
        "reserve_ids": reserves,
    }
