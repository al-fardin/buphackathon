from __future__ import annotations

import numpy as np
from scipy.optimize import linprog

from app.models.schemas import HourlyPlan, ScenarioInput
from app.optimizer.constraint_engine import HourConstraints


class OptimizationError(RuntimeError):
    pass


def optimize_schedule(scenario: ScenarioInput, constraints: HourConstraints) -> list[HourlyPlan]:
    # Variable blocks: grid, solar-used, charge, discharge, battery-energy-after.
    size = 24
    grid, solar, charge, discharge, energy = (0, size, 2*size, 3*size, 4*size)
    n = 5*size
    objective = np.zeros(n)
    objective[grid:grid+size] = [h.tariff_bdt_per_kwh for h in scenario.hours]
    # Tie-break equivalent cost optima toward fewer battery cycles.
    objective[charge:charge+size] = 1e-8
    objective[discharge:discharge+size] = 1e-8

    equalities: list[np.ndarray] = []
    targets: list[float] = []
    for hour, item in enumerate(scenario.hours):
        row = np.zeros(n)
        row[grid+hour] = 1
        row[solar+hour] = 1
        row[charge+hour] = -1
        row[discharge+hour] = 1
        equalities.append(row)
        targets.append(item.demand_kwh)

        state = np.zeros(n)
        state[energy+hour] = 1
        state[charge+hour] = -1
        state[discharge+hour] = 1
        if hour == 0:
            targets.append(scenario.battery.initial_energy_kwh)
        else:
            state[energy+hour-1] = -1
            targets.append(0)
        equalities.append(state)

    final = np.zeros(n)
    final[energy+23] = 1
    equalities.append(final)
    targets.append(scenario.battery.initial_energy_kwh)

    bounds = []
    battery = scenario.battery
    for hour in range(size):
        bounds.append((0, constraints.max_grid[hour]))
    for hour in range(size):
        bounds.append((0, constraints.effective_solar[hour]))
    for hour in range(size):
        bounds.append((0, battery.max_charge_kwh_per_hour if constraints.can_charge[hour] else 0))
    for hour in range(size):
        bounds.append((0, battery.max_discharge_kwh_per_hour if constraints.can_discharge[hour] else 0))
    for hour in range(size):
        bounds.append((constraints.minimum_energy[hour], battery.capacity_kwh))

    result = linprog(objective, A_eq=np.asarray(equalities), b_eq=np.asarray(targets),
                     bounds=bounds, method="highs", options={"presolve": True})
    if not result.success:
        raise OptimizationError(f"No feasible energy schedule: {result.message}")

    x = result.x
    plan: list[HourlyPlan] = []
    for hour in range(size):
        c = max(0.0, x[charge+hour])
        d = max(0.0, x[discharge+hour])
        if c > 1e-6 and d > 1e-6:
            net = c-d
            c, d = (net, 0.0) if net >= 0 else (0.0, -net)
        if c > 1e-6:
            action, amount = "charge", c
        elif d > 1e-6:
            action, amount = "discharge", d
        else:
            action, amount = "idle", 0.0
        plan.append(HourlyPlan(
            hour=hour, grid_kwh=_clean(x[grid+hour]), solar_used_kwh=_clean(x[solar+hour]),
            battery_action=action, battery_kwh=_clean(amount),
            battery_energy_after_kwh=_clean(x[energy+hour]),
        ))
    return plan


def _clean(value: float) -> float:
    value = round(float(value), 6)
    return 0.0 if abs(value) < 1e-7 else value
