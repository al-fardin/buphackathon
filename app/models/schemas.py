from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HourInput(StrictModel):
    hour: int = Field(ge=0, le=23)
    demand_kwh: float = Field(ge=0)
    solar_kwh: float = Field(ge=0)
    tariff_bdt_per_kwh: float = Field(ge=0)

    @field_validator("demand_kwh", "solar_kwh", "tariff_bdt_per_kwh")
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("must be finite")
        return value


class BatteryInput(StrictModel):
    capacity_kwh: float = Field(gt=0)
    initial_energy_kwh: float = Field(ge=0)
    minimum_energy_kwh: float = Field(ge=0)
    max_charge_kwh_per_hour: float = Field(ge=0)
    max_discharge_kwh_per_hour: float = Field(ge=0)

    @field_validator("capacity_kwh", "initial_energy_kwh", "minimum_energy_kwh",
                     "max_charge_kwh_per_hour", "max_discharge_kwh_per_hour")
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("must be finite")
        return value

    @model_validator(mode="after")
    def valid_state(self) -> "BatteryInput":
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum energy cannot exceed capacity")
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError("initial energy cannot exceed capacity")
        return self


class ScenarioInput(StrictModel):
    scenario_id: str = Field(min_length=1, max_length=200)
    operator_notes: list[str] = Field(min_length=1, max_length=3)
    hours: list[HourInput] = Field(min_length=24, max_length=24)
    battery: BatteryInput

    @field_validator("scenario_id")
    @classmethod
    def clean_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("scenario_id cannot be blank")
        return value.strip()

    @field_validator("operator_notes")
    @classmethod
    def valid_notes(cls, values: list[str]) -> list[str]:
        if any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError("operator notes must be non-empty strings")
        return [v.strip() for v in values]

    @model_validator(mode="after")
    def unique_hours(self) -> "ScenarioInput":
        values = sorted(h.hour for h in self.hours)
        if values != list(range(24)):
            raise ValueError("hours must contain each integer 0 through 23 exactly once")
        self.hours.sort(key=lambda h: h.hour)
        return self


DirectiveType = Literal[
    "solar_reduction", "minimum_battery_reserve", "no_charge_window",
    "no_discharge_window", "max_grid_window", "no_op"
]


class DirectiveInterpretation(StrictModel):
    note_index: int = Field(ge=0)
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: dict[str, Any] | None
    explanation: str = Field(min_length=1)


class HourlyPlan(StrictModel):
    hour: int = Field(ge=0, le=23)
    grid_kwh: float = Field(ge=0)
    solar_used_kwh: float = Field(ge=0)
    battery_action: Literal["charge", "discharge", "idle"]
    battery_kwh: float = Field(ge=0)
    battery_energy_after_kwh: float = Field(ge=0)


class ValidationResult(StrictModel):
    valid: bool
    errors: list[str]


class OptimizeResponse(StrictModel):
    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourlyPlan]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str
    validation: ValidationResult | None = None
