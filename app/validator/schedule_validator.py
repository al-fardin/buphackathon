from __future__ import annotations

import math

from app.models.schemas import DirectiveInterpretation, HourlyPlan, ScenarioInput, ValidationResult
from app.optimizer.constraint_engine import compile_constraints

TOLERANCE = 0.01


def validate_schedule(scenario: ScenarioInput, directives: list[DirectiveInterpretation],
                      plan: list[HourlyPlan]) -> ValidationResult:
    errors: list[str] = []
    if len(plan) != 24 or sorted(p.hour for p in plan) != list(range(24)):
        return ValidationResult(valid=False, errors=["hourly_plan must contain each hour 0-23 exactly once"])
    plan = sorted(plan, key=lambda p: p.hour)
    constraints = compile_constraints(scenario, directives)
    previous = scenario.battery.initial_energy_kwh
    for hour, (source, row) in enumerate(zip(scenario.hours, plan)):
        values = [row.grid_kwh, row.solar_used_kwh, row.battery_kwh, row.battery_energy_after_kwh]
        if any(not math.isfinite(v) or v < -TOLERANCE for v in values):
            errors.append(f"hour {hour}: values must be finite and non-negative")
            continue
        charge = row.battery_kwh if row.battery_action == "charge" else 0.0
        discharge = row.battery_kwh if row.battery_action == "discharge" else 0.0
        if row.battery_action == "idle" and row.battery_kwh > TOLERANCE:
            errors.append(f"hour {hour}: idle battery_kwh must be zero")
        expected_energy = previous+charge-discharge
        if abs(row.battery_energy_after_kwh-expected_energy) > TOLERANCE:
            errors.append(f"hour {hour}: invalid battery transition")
        if not constraints.minimum_energy[hour]-TOLERANCE <= row.battery_energy_after_kwh <= scenario.battery.capacity_kwh+TOLERANCE:
            errors.append(f"hour {hour}: battery energy outside active bounds")
        if charge > scenario.battery.max_charge_kwh_per_hour+TOLERANCE:
            errors.append(f"hour {hour}: charge rate exceeded")
        if discharge > scenario.battery.max_discharge_kwh_per_hour+TOLERANCE:
            errors.append(f"hour {hour}: discharge rate exceeded")
        if not constraints.can_charge[hour] and charge > TOLERANCE:
            errors.append(f"hour {hour}: no-charge directive violated")
        if not constraints.can_discharge[hour] and discharge > TOLERANCE:
            errors.append(f"hour {hour}: no-discharge directive violated")
        if row.solar_used_kwh > constraints.effective_solar[hour]+TOLERANCE:
            errors.append(f"hour {hour}: effective solar exceeded")
        if constraints.max_grid[hour] is not None and row.grid_kwh > constraints.max_grid[hour]+TOLERANCE:
            errors.append(f"hour {hour}: grid cap exceeded")
        balance = row.grid_kwh+row.solar_used_kwh+discharge-source.demand_kwh-charge
        if abs(balance) > TOLERANCE:
            errors.append(f"hour {hour}: energy balance error {balance:.6f}")
        previous = row.battery_energy_after_kwh
    if abs(previous-scenario.battery.initial_energy_kwh) > TOLERANCE:
        errors.append("end-of-day battery neutrality violated")
    return ValidationResult(valid=not errors, errors=errors)
