from __future__ import annotations

import numpy as np
from scipy.optimize import linprog

from app.models.schemas import DirectiveInterpretation, ScenarioInput
from app.optimizer.constraint_engine import compile_constraints


class ReferenceInfeasible(RuntimeError):
    pass


def reference_optimal_metrics(
    scenario: ScenarioInput,
    directives: list[DirectiveInterpretation],
) -> dict[str, float]:
    """Independent cost-first LP oracle used only by the test framework."""
    size = 24
    grid, solar, charge, discharge, energy = 0, size, 2*size, 3*size, 4*size
    n = 5*size
    constraints = compile_constraints(scenario, directives)

    a_eq: list[np.ndarray] = []
    b_eq: list[float] = []
    for hour, source in enumerate(scenario.hours):
        balance = np.zeros(n)
        balance[grid+hour] = 1
        balance[solar+hour] = 1
        balance[charge+hour] = -1
        balance[discharge+hour] = 1
        a_eq.append(balance)
        b_eq.append(source.demand_kwh)

        transition = np.zeros(n)
        transition[energy+hour] = 1
        transition[charge+hour] = -1
        transition[discharge+hour] = 1
        if hour:
            transition[energy+hour-1] = -1
            b_eq.append(0)
        else:
            b_eq.append(scenario.battery.initial_energy_kwh)
        a_eq.append(transition)

    final = np.zeros(n)
    final[energy+23] = 1
    a_eq.append(final)
    b_eq.append(scenario.battery.initial_energy_kwh)

    battery = scenario.battery
    bounds: list[tuple[float, float | None]] = []
    bounds.extend((0, constraints.max_grid[h]) for h in range(size))
    bounds.extend((0, constraints.effective_solar[h]) for h in range(size))
    bounds.extend(
        (0, battery.max_charge_kwh_per_hour if constraints.can_charge[h] else 0)
        for h in range(size)
    )
    bounds.extend(
        (0, battery.max_discharge_kwh_per_hour if constraints.can_discharge[h] else 0)
        for h in range(size)
    )
    bounds.extend((constraints.minimum_energy[h], battery.capacity_kwh) for h in range(size))

    cost = np.zeros(n)
    cost[grid:grid+size] = [row.tariff_bdt_per_kwh for row in scenario.hours]
    first = linprog(
        cost,
        A_eq=np.asarray(a_eq),
        b_eq=np.asarray(b_eq),
        bounds=bounds,
        method="highs",
        options={"presolve": True},
    )
    if not first.success:
        raise ReferenceInfeasible(first.message)

    minimum_cost = float(cost@first.x)
    total_grid = np.zeros(n)
    total_grid[grid:grid+size] = 1
    cost_tolerance = max(1e-5, abs(minimum_cost)*1e-8)
    second = linprog(
        total_grid,
        A_ub=np.asarray([cost]),
        b_ub=np.asarray([minimum_cost+cost_tolerance]),
        A_eq=np.asarray(a_eq),
        b_eq=np.asarray(b_eq),
        bounds=bounds,
        method="highs",
        options={"presolve": True},
    )
    if not second.success:
        raise ReferenceInfeasible(second.message)

    return {
        "total_grid_kwh": round(float(total_grid@second.x), 6),
        "total_cost_bdt": round(float(cost@second.x), 6),
    }
