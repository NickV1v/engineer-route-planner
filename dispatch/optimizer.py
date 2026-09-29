"""Bounded subprocess bridge; C++ schedules and scores are independently verified."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from threading import BoundedSemaphore
from typing import Literal

from pydantic import Field, ValidationError

from dispatch.baseline import solve_baseline
from dispatch.checker import require_valid_plan
from dispatch.models import Model, Plan, Scenario
from dispatch.objective import score_plan

ROOT = Path(__file__).resolve().parents[1]
SLOTS = BoundedSemaphore(2)


class SolverError(RuntimeError):
    """A solver failure must never be presented as an optimized solution."""


class SearchOptions(Model):
    rounds: int = Field(default=2048, ge=0, le=4096)
    max_evaluations: int = Field(default=32_000_000, ge=0, le=100_000_000)
    time_limit_ms: int = Field(default=60_000, ge=0, le=120_000)
    seed: int = Field(default=20260917, ge=0, le=4_294_967_295)
    use_local_search: bool = True
    use_eliminate: bool = False
    use_destroy: bool = True


class SearchStatistics(Model):
    status: Literal["iteration_limit", "evaluation_limit", "time_limit", "evaluated"]
    evaluations: int = Field(ge=0)
    improvements: int = Field(ge=0)
    rounds_completed: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0)


class SolverOutput(Model):
    protocol_version: Literal["1.0"]
    plan: Plan
    score: list[int] = Field(min_length=6, max_length=11)
    search: SearchStatistics


def solver_binary() -> Path:
    binary = Path(os.environ.get("DISPATCH_SOLVER_BINARY", ROOT / "build/dispatch-solver"))
    if not binary.is_file():
        raise SolverError("C++-солвер не собран. Выполните make solver в корне проекта.")
    sources = [ROOT / "solver/main.cpp", ROOT / "solver/solver.cpp", ROOT / "solver/solver.hpp"]
    if any(source.stat().st_mtime_ns > binary.stat().st_mtime_ns for source in sources):
        raise SolverError("Исходники солвера изменились. Выполните make solver заново.")
    return binary


def invoke_solver(
    scenario: Scenario,
    initial: Plan,
    options: SearchOptions,
    *,
    mode: Literal["optimize", "evaluate"] = "optimize",
) -> SolverOutput:
    require_valid_plan(scenario, initial)
    binary = solver_binary()
    by_engineer = {route.engineer_id: route for route in initial.routes}
    envelope = {
        "protocol_version": "1.0",
        "mode": mode,
        "scenario": scenario.model_dump(),
        "input_hash": scenario.fingerprint(),
        "initial_routes": [
            [v.request_id for v in by_engineer[e.id].visits] for e in scenario.engineers
        ],
        "options": options.model_dump(),
    }
    if not SLOTS.acquire(blocking=False):
        raise SolverError("Два расчёта уже выполняются. Повторите запуск после их завершения.")
    try:
        try:
            result = subprocess.run(
                [str(binary)],
                input=json.dumps(envelope, ensure_ascii=False),
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=options.time_limit_ms / 1000 + 5 if options.time_limit_ms else 180,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SolverError("Процесс C++ превысил лимит времени; результат не принят.") from exc
        except OSError as exc:
            raise SolverError("Не удалось запустить C++-солвер.") from exc
    finally:
        SLOTS.release()
    if result.returncode:
        raise SolverError(
            f"C++-солвер завершился с ошибкой {result.returncode}; результат не принят."
        )
    if len(result.stdout) > 4 * 1024 * 1024:
        raise SolverError("Ответ солвера превышает допустимый размер.")
    try:
        output = SolverOutput.model_validate_json(result.stdout)
        require_valid_plan(scenario, output.plan)
        actual_score = score_plan(scenario, output.plan)
        if tuple(output.score) != actual_score:
            raise ValueError("Score C++ расходится с Python")
        if mode == "optimize" and actual_score > score_plan(scenario, initial):
            raise ValueError("Результат хуже исходного плана")
        if output.plan.algorithm != "cpp_insertion_v1":
            raise ValueError("Неверный алгоритм в ответе")
    except (ValueError, ValidationError) as exc:
        raise SolverError("Ответ C++ не прошёл независимую проверку; результат не принят.") from exc
    return output


def optimize(
    scenario: Scenario, options: SearchOptions | None = None, *, warm_start: Plan | None = None
) -> dict:
    options = options or SearchOptions()
    baseline = require_valid_plan(scenario, solve_baseline(scenario))
    initial = baseline
    if warm_start is not None:
        require_valid_plan(scenario, warm_start)
        if score_plan(scenario, warm_start) <= score_plan(scenario, baseline):
            initial = warm_start
    result = invoke_solver(scenario, initial, options)
    return {
        "baseline": baseline,
        "plan": result.plan,
        "search": result.search,
        "options": options,
        "binary_sha256": hashlib.sha256(solver_binary().read_bytes()).hexdigest(),
    }
