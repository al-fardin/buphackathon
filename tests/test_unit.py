from __future__ import annotations

from fastapi.testclient import TestClient

from app.llm.local_parser import parse_note, parse_note_all
from app.main import app
from app.models.schemas import BatteryInput
from test_runner import compare_schedule_metrics


def scenario(note: str = "The cafeteria menu changes tomorrow.") -> dict:
    return {
        "scenario_id": "TEST-1", "operator_notes": [note],
        "hours": [{"hour": h, "demand_kwh": 100, "solar_kwh": 20 if 7 <= h <= 17 else 0,
                   "tariff_bdt_per_kwh": 5 if h < 6 else (20 if 17 <= h <= 21 else 10)} for h in range(24)],
        "battery": {"capacity_kwh": 200, "initial_energy_kwh": 100,
                    "minimum_energy_kwh": 20, "max_charge_kwh_per_hour": 40,
                    "max_discharge_kwh_per_hour": 40},
    }


def test_health() -> None:
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def test_optimize_valid() -> None:
    response = TestClient(app).post("/optimize-energy", json=scenario())
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["hourly_plan"]) == 24
    assert body["validation"]["valid"] is True
    assert abs(body["hourly_plan"][-1]["battery_energy_after_kwh"]-100) <= 0.01


def test_bad_hours_rejected() -> None:
    body = scenario()
    body["hours"] = body["hours"][:-1]
    assert TestClient(app).post("/optimize-energy", json=body).status_code == 400


def test_banglish() -> None:
    battery = BatteryInput(capacity_kwh=200, initial_energy_kwh=100, minimum_energy_kwh=20,
                           max_charge_kwh_per_hour=40, max_discharge_kwh_per_hour=40)
    parsed = parse_note("Battery charge bondho rakho 6 PM theke 8 PM", battery)
    assert parsed["directive_type"] == "no_charge_window"
    assert parsed["structured_adjustment"]["hours"] == [18, 19]


def test_bangla_reserve() -> None:
    battery = BatteryInput(capacity_kwh=200, initial_energy_kwh=100, minimum_energy_kwh=20,
                           max_charge_kwh_per_hour=40, max_discharge_kwh_per_hour=40)
    parsed = parse_note("সন্ধ্যার সময় battery reserve maintain korte hobe at least 100 kWh", battery)
    assert parsed["directive_type"] == "minimum_battery_reserve"


def test_multiple_directives() -> None:
    battery = BatteryInput(capacity_kwh=250, initial_energy_kwh=120, minimum_energy_kwh=40,
                           max_charge_kwh_per_hour=50, max_discharge_kwh_per_hour=50)
    parsed = parse_note_all(
        "Do not charge the battery from 8 AM to 10 AM, do not discharge it from 6 PM to 8 PM, "
        "and limit grid import to 80 kWh at hour 18.", battery
    )
    assert [item["directive_type"] for item in parsed] == [
        "no_charge_window", "no_discharge_window", "max_grid_window"
    ]


def test_flat_tariff_avoids_unnecessary_cycles() -> None:
    body = scenario()
    for row in body["hours"]:
        row["demand_kwh"] = 100
        row["solar_kwh"] = 0
        row["tariff_bdt_per_kwh"] = 10
    response = TestClient(app).post("/optimize-energy", json=body)
    assert response.status_code == 200, response.text
    plan = response.json()["hourly_plan"]
    assert all(row["battery_action"] == "idle" for row in plan)
    assert all(abs(row["grid_kwh"]-100) <= 0.001 for row in plan)


def test_peak_is_minimized_after_cost() -> None:
    body = scenario()
    for row in body["hours"]:
        row["demand_kwh"] = 100
        row["solar_kwh"] = 0
        row["tariff_bdt_per_kwh"] = 10
    body["hours"][18]["demand_kwh"] = 200
    body["battery"] = {
        "capacity_kwh": 200,
        "initial_energy_kwh": 100,
        "minimum_energy_kwh": 0,
        "max_charge_kwh_per_hour": 100,
        "max_discharge_kwh_per_hour": 100,
    }
    response = TestClient(app).post("/optimize-energy", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["peak_grid_kwh"] < 110
    assert abs(result["total_cost_bdt"]-25000) <= 0.01
    assert result["validation"]["valid"] is True


def test_metric_comparison_uses_point_zero_one_tolerance() -> None:
    case = {"expected_output": {"total_grid_kwh": 100, "total_cost_bdt": 500}}
    row, errors = compare_schedule_metrics(
        case, {"total_grid_kwh": 100.01, "total_cost_bdt": 499.99}
    )
    assert errors == []
    assert row["metric_status"] == "passed"

    row, errors = compare_schedule_metrics(
        case, {"total_grid_kwh": 100.010001, "total_cost_bdt": 500}
    )
    assert errors
    assert row["metric_status"] == "failed"
