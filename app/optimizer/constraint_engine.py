from __future__ import annotations

from dataclasses import dataclass

from app.models.schemas import DirectiveInterpretation, ScenarioInput


@dataclass(frozen=True)
class HourConstraints:
    effective_solar: list[float]
    minimum_energy: list[float]
    can_charge: list[bool]
    can_discharge: list[bool]
    max_grid: list[float | None]


def compile_constraints(scenario: ScenarioInput,
                        directives: list[DirectiveInterpretation]) -> HourConstraints:
    solar = [h.solar_kwh for h in scenario.hours]
    reserve = [scenario.battery.minimum_energy_kwh]*24
    can_charge = [True]*24
    can_discharge = [True]*24
    max_grid: list[float | None] = [None]*24
    for directive in directives:
        if not directive.applies or directive.structured_adjustment is None:
            continue
        adjustment = directive.structured_adjustment
        hours = adjustment["hours"]
        if directive.directive_type == "solar_reduction":
            for hour in hours:
                solar[hour] = min(solar[hour], scenario.hours[hour].solar_kwh*adjustment["factor"])
        elif directive.directive_type == "minimum_battery_reserve":
            for hour in hours:
                reserve[hour] = max(reserve[hour], adjustment["minimum_energy_kwh"])
        elif directive.directive_type == "no_charge_window":
            for hour in hours:
                can_charge[hour] = False
        elif directive.directive_type == "no_discharge_window":
            for hour in hours:
                can_discharge[hour] = False
        elif directive.directive_type == "max_grid_window":
            for hour in hours:
                value = adjustment["max_grid_kwh"]
                max_grid[hour] = value if max_grid[hour] is None else min(max_grid[hour], value)
    return HourConstraints(solar, reserve, can_charge, can_discharge, max_grid)
