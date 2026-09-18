from __future__ import annotations

import math
from typing import Any

from app.models.schemas import BatteryInput, DirectiveInterpretation

ALLOWED = {
    "solar_reduction", "minimum_battery_reserve", "no_charge_window",
    "no_discharge_window", "max_grid_window", "no_op"
}


class DirectiveValidationError(ValueError):
    pass


def _hours(value: Any) -> list[int]:
    if not isinstance(value, list) or not value:
        raise DirectiveValidationError("hours must be a non-empty array")
    if any(isinstance(h, bool) or not isinstance(h, int) or not 0 <= h <= 23 for h in value):
        raise DirectiveValidationError("hours must contain integers from 0 through 23")
    return sorted(set(value))


def validate_directive(raw: dict[str, Any], note_index: int,
                       battery: BatteryInput) -> DirectiveInterpretation:
    kind = raw.get("directive_type") or raw.get("type")
    if kind not in ALLOWED:
        raise DirectiveValidationError("unsupported directive type")
    if kind == "no_op":
        return DirectiveInterpretation(
            note_index=note_index, applies=False, directive_type="no_op",
            structured_adjustment=None,
            explanation=str(raw.get("explanation") or "The note does not affect this energy schedule."),
        )

    adjustment = raw.get("structured_adjustment", raw.get("adjustment"))
    if adjustment is None:
        adjustment = {k: v for k, v in raw.items() if k not in {
            "type", "directive_type", "applies", "explanation", "note_index"
        }}
    if not isinstance(adjustment, dict):
        raise DirectiveValidationError("structured_adjustment must be an object")
    normalized: dict[str, Any] = {"hours": _hours(adjustment.get("hours"))}
    if kind == "solar_reduction":
        factor = adjustment.get("factor")
        if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not math.isfinite(factor) or not 0 <= factor <= 1:
            raise DirectiveValidationError("solar factor must be finite and between 0 and 1")
        normalized["factor"] = float(factor)
    elif kind == "minimum_battery_reserve":
        reserve = adjustment.get("minimum_energy_kwh")
        if isinstance(reserve, bool) or not isinstance(reserve, (int, float)) or not math.isfinite(reserve) or not 0 <= reserve <= battery.capacity_kwh:
            raise DirectiveValidationError("battery reserve must be within capacity")
        normalized["minimum_energy_kwh"] = float(reserve)
    elif kind == "max_grid_window":
        cap = adjustment.get("max_grid_kwh")
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) or cap < 0:
            raise DirectiveValidationError("grid cap must be finite and non-negative")
        normalized["max_grid_kwh"] = float(cap)
    return DirectiveInterpretation(
        note_index=note_index, applies=True, directive_type=kind,
        structured_adjustment=normalized,
        explanation=str(raw.get("explanation") or f"Applied {kind.replace('_', ' ')} directive."),
    )
