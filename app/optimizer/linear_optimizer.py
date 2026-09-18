from __future__ import annotations

import numpy as np
from scipy.optimize import linprog

from app.models.schemas import HourlyPlan, ScenarioInput
from app.optimizer.constraint_engine import HourConstraints


class OptimizationError(RuntimeError):
    pass


def optimize_schedule(scenario: ScenarioInput, constraints: HourConstraints) -> list[HourlyPlan]:
    # Variable blocks: grid, solar-used, charge, discharge, battery-energy-after,
    # and a scalar peak-grid variable. Objectives are solved lexicographically:
    # cost -> peak -> total grid -> battery throughput.
    size = 24
    grid, solar, charge, discharge, energy = (0, size, 2*size, 3*size, 4*size)
    peak = 5*size
    n = peak+1

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
    bounds.append((0, None))

    # Every hourly grid import must be no larger than the peak variable.
    base_inequalities: list[np.ndarray] = []
    base_limits: list[float] = []
    for hour in range(size):
        row = np.zeros(n)
        row[grid+hour] = 1
        row[peak] = -1
        base_inequalities.append(row)
        base_limits.append(0)

    equality_matrix = np.asarray(equalities)
    equality_targets = np.asarray(targets)

    def solve(objective: np.ndarray, extra_rows: list[np.ndarray] | None = None,
              extra_limits: list[float] | None = None):
        rows = base_inequalities+list(extra_rows or [])
        limits = base_limits+list(extra_limits or [])
        result = linprog(
            objective,
            A_ub=np.asarray(rows),
            b_ub=np.asarray(limits),
            A_eq=equality_matrix,
            b_eq=equality_targets,
            bounds=bounds,
            method="highs",
            options={"presolve": True},
        )
        if not result.success:
            raise OptimizationError(f"No feasible energy schedule: {result.message}")
        return result

    cost_row = np.zeros(n)
    cost_row[grid:grid+size] = [h.tariff_bdt_per_kwh for h in scenario.hours]

    # Stage 1: globally minimum bill.
    cost_result = solve(cost_row)
    minimum_cost = float(cost_row@cost_result.x)
    cost_limit = minimum_cost+_lex_tolerance(minimum_cost)

    # Stage 2: preserve the minimum bill and shave the maximum grid draw.
    peak_objective = np.zeros(n)
    peak_objective[peak] = 1
    peak_result = solve(peak_objective, [cost_row], [cost_limit])
    minimum_peak = float(peak_result.x[peak])

    peak_row = np.zeros(n)
    peak_row[peak] = 1
    locked_rows = [cost_row, peak_row]
    locked_limits = [cost_limit, minimum_peak+_lex_tolerance(minimum_peak)]

    # Stage 3: among cost/peak-equivalent plans, minimize total grid energy.
    grid_objective = np.zeros(n)
    grid_objective[grid:grid+size] = 1
    grid_result = solve(grid_objective, locked_rows, locked_limits)
    minimum_grid = float(grid_objective@grid_result.x)

    # Stage 4: avoid charge/discharge cycles without sacrificing prior goals.
    throughput_objective = np.zeros(n)
    throughput_objective[charge:charge+size] = 1
    throughput_objective[discharge:discharge+size] = 1
    result = solve(
        throughput_objective,
        locked_rows+[grid_objective],
        locked_limits+[minimum_grid+_lex_tolerance(minimum_grid)],
    )

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


def _lex_tolerance(value: float) -> float:
    """Numerical allowance small enough to preserve the preceding objective."""
    return max(1e-5, abs(value)*1e-8)
