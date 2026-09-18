from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.llm.llm_interpreter import LLMDirectiveInterpreter
from app.models.schemas import OptimizeResponse, ScenarioInput
from app.optimizer.constraint_engine import compile_constraints
from app.optimizer.linear_optimizer import OptimizationError, optimize_schedule
from app.validator.schedule_validator import validate_schedule

logger = logging.getLogger(__name__)
router = APIRouter()
interpreter = LLMDirectiveInterpreter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/optimize-energy", response_model=OptimizeResponse)
async def optimize_energy(scenario: ScenarioInput) -> OptimizeResponse:
    try:
        directives = await interpreter.interpret(scenario.operator_notes, scenario.battery)
        constraints = compile_constraints(scenario, directives)
        plan = optimize_schedule(scenario, constraints)
        validation = validate_schedule(scenario, directives, plan)
        if not validation.valid:
            logger.error("Internal schedule validation failed for scenario %s", scenario.scenario_id)
            raise HTTPException(status_code=500, detail="Generated schedule failed safety validation")
        total_grid = round(sum(row.grid_kwh for row in plan), 6)
        total_cost = round(sum(
            row.grid_kwh*scenario.hours[row.hour].tariff_bdt_per_kwh for row in plan
        ), 6)
        peak_grid = round(max(row.grid_kwh for row in plan), 6)
        applied = sum(item.applies for item in directives)
        summary = (
            f"Cost-minimal 24-hour plan using available solar and tariff-aware battery shifting; "
            f"{applied} operator directive{'s' if applied != 1 else ''} applied with end-of-day battery neutrality."
        )
        return OptimizeResponse(
            scenario_id=scenario.scenario_id, directive_interpretation=directives,
            hourly_plan=plan, total_grid_kwh=total_grid, total_cost_bdt=total_cost,
            peak_grid_kwh=peak_grid, plan_summary=summary, validation=validation,
        )
    except OptimizationError as exc:
        raise HTTPException(status_code=422, detail="Scenario is infeasible under the supplied constraints") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Controlled optimization failure for scenario %s", scenario.scenario_id)
        raise HTTPException(status_code=500, detail="Unable to optimize the scenario") from exc
