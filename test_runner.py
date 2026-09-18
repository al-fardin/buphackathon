from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.llm.guardrails import ALLOWED, DirectiveValidationError, validate_directive
from app.llm.local_parser import _split_clauses, extract_hours, parse_note_all
from app.models.schemas import BatteryInput, DirectiveInterpretation, ScenarioInput
from app.optimizer.constraint_engine import compile_constraints
from app.optimizer.linear_optimizer import OptimizationError, optimize_schedule
from app.validator.schedule_validator import validate_schedule
from tests.reference_oracle import ReferenceInfeasible, reference_optimal_metrics

ROOT = Path(__file__).resolve().parent
METRIC_TOLERANCE = 0.01


def default_battery() -> BatteryInput:
    return BatteryInput(capacity_kwh=250, initial_energy_kwh=120, minimum_energy_kwh=40,
                        max_charge_kwh_per_hour=50, max_discharge_kwh_per_hour=50)


def discover_json(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths or [str(ROOT/"tests"/"test_cases")]:
        path = Path(raw).expanduser().resolve()
        if path.is_file() and path.suffix.lower() == ".json":
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.glob("*.json")))
    return list(dict.fromkeys(files))


def load_cases(files: list[Path]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            result.append({"source": str(path), "load_error": f"{type(exc).__name__}: {exc}"})
            continue
        raw_cases = data.get("cases", [data]) if isinstance(data, dict) else data
        if not isinstance(raw_cases, list):
            result.append({"source": str(path), "load_error": "JSON must contain a cases array"})
            continue
        for index, case in enumerate(raw_cases):
            result.append({"source": str(path), "index": index, "case": case})
    return result


def compact_directive(item: DirectiveInterpretation) -> dict[str, Any]:
    return {"note_index": item.note_index, "applies": item.applies,
            "directive_type": item.directive_type,
            "structured_adjustment": item.structured_adjustment}


def expectation_conflicts(expected: list[dict[str, Any]], scenario: ScenarioInput) -> list[str]:
    errors: list[str] = []
    for position, item in enumerate(expected):
        kind = item.get("directive_type")
        if kind not in ALLOWED:
            errors.append(f"entry {position}: unsupported directive_type {kind!r}")
            continue
        if kind == "no_op":
            if item.get("applies") is not False or item.get("structured_adjustment") is not None:
                errors.append(f"entry {position}: invalid no_op semantics")
            continue
        if item.get("applies") is not True:
            errors.append(f"entry {position}: non-no_op must have applies=true")
            continue
        try:
            validate_directive(item, int(item.get("note_index", -1)), scenario.battery)
        except (DirectiveValidationError, ValidationError, ValueError) as exc:
            errors.append(f"entry {position}: {exc}")
            continue
        adjustment = item.get("structured_adjustment") or {}
        expected_hours = adjustment.get("hours")
        note_index = item.get("note_index")
        if expected_hours and isinstance(note_index, int) and 0 <= note_index < len(scenario.operator_notes):
            note = scenario.operator_notes[note_index]
            grounded = {tuple(sorted(extract_hours(part))) for part in _split_clauses(note) if extract_hours(part)}
            whole = extract_hours(note)
            if whole:
                grounded.add(tuple(sorted(whole)))
            if tuple(expected_hours) not in grounded:
                errors.append(
                    f"entry {position}: expected hours {expected_hours} are not stated or implied by the note"
                )
    return errors


def interpret_locally(scenario: ScenarioInput) -> list[DirectiveInterpretation]:
    directives: list[DirectiveInterpretation] = []
    for note_index, note in enumerate(scenario.operator_notes):
        for raw in parse_note_all(note, scenario.battery):
            directives.append(validate_directive(raw, note_index, scenario.battery))
    return directives


def run_schedule(scenario: ScenarioInput, directives: list[DirectiveInterpretation]) -> tuple[str, str, dict[str, float] | None]:
    try:
        plan = optimize_schedule(scenario, compile_constraints(scenario, directives))
        result = validate_schedule(scenario, directives, plan)
        if not result.valid:
            return "failed", "; ".join(result.errors), None
        metrics = {
            "total_grid_kwh": round(sum(row.grid_kwh for row in plan), 6),
            "total_cost_bdt": round(sum(
                row.grid_kwh*scenario.hours[row.hour].tariff_bdt_per_kwh for row in plan
            ), 6),
        }
        return "passed", "", metrics
    except OptimizationError as exc:
        return "controlled_infeasible", str(exc), None


def expected_schedule_metrics(case: dict[str, Any]) -> dict[str, float | None]:
    """Read supported expected-output layouts without inventing missing truth."""
    containers = []
    for key in ("expected_output", "expected"):
        value = case.get(key)
        if isinstance(value, dict):
            containers.append(value)
    containers.append(case)

    result: dict[str, float | None] = {"total_grid_kwh": None, "total_cost_bdt": None}
    for field in result:
        for container in containers:
            value = container.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
                result[field] = float(value)
                break
    return result


def compare_schedule_metrics(case: dict[str, Any], actual: dict[str, float] | None) -> tuple[dict[str, Any], list[str]]:
    expected = expected_schedule_metrics(case)
    row: dict[str, Any] = {"tolerance": METRIC_TOLERANCE}
    errors: list[str] = []
    available = False
    compared = False
    for field in ("total_grid_kwh", "total_cost_bdt"):
        expected_value = expected[field]
        actual_value = actual.get(field) if actual is not None else None
        difference = round(abs(actual_value-expected_value), 6) if (
            expected_value is not None and actual_value is not None
        ) else None
        row[f"expected_{field}"] = expected_value
        row[f"output_{field}"] = actual_value
        row[f"{field}_diff"] = difference
        if expected_value is not None:
            available = True
            if actual_value is None:
                errors.append(f"{field}: expected {expected_value}, but no schedule output was produced")
            else:
                compared = True
                if difference is not None and difference > METRIC_TOLERANCE:
                    errors.append(
                        f"{field}: expected {expected_value}, got {actual_value}, "
                        f"absolute diff {difference} > {METRIC_TOLERANCE}"
                    )
    if errors:
        row["metric_status"] = "failed"
    elif compared:
        row["metric_status"] = "passed"
    elif available:
        row["metric_status"] = "not_run"
    else:
        row["metric_status"] = "not_available"
    return row, errors


def run_loaded(entry: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    if "load_error" in entry:
        return "failed", "json_load", entry["load_error"], {"metric_status": "not_run"}
    case = entry.get("case")
    if not isinstance(case, dict):
        return "failed", "case_schema", "case must be an object", {"metric_status": "not_run"}

    if "note" in case:
        expected_type = case.get("type")
        expected_adjustment = case.get("adjustment")
        raw = parse_note_all(str(case["note"]), default_battery())
        got = validate_directive(raw[0], 0, default_battery())
        ok = got.directive_type == expected_type and got.structured_adjustment == expected_adjustment
        metrics, _ = compare_schedule_metrics(case, None)
        return ("passed", "compact_language", "", metrics) if ok else (
            "failed", "compact_language", f"expected {expected_type} {expected_adjustment}; got {compact_directive(got)}", metrics)

    if "input" not in case:
        metrics, _ = compare_schedule_metrics(case, None)
        return "failed", "case_schema", "missing input object", metrics
    try:
        scenario = ScenarioInput.model_validate(case["input"])
    except ValidationError as exc:
        metrics, metric_errors = compare_schedule_metrics(case, None)
        detail = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        if metric_errors:
            return "failed", "metric_comparison", "; ".join(metric_errors+[detail]), metrics
        return "controlled_rejection", "request_validation", detail, metrics

    expected = case.get("expected_directive_interpretation")
    if expected is None and isinstance(case.get("expected_output"), dict):
        expected = case["expected_output"].get("directive_interpretation")

    directives = interpret_locally(scenario)
    if expected is not None:
        if not isinstance(expected, list):
            metrics, _ = compare_schedule_metrics(case, None)
            return "dataset_conflict", "fixture_contract", "expected directives must be an array", metrics
        conflicts = expectation_conflicts(expected, scenario)
        if conflicts:
            metrics, _ = compare_schedule_metrics(case, None)
            return "dataset_conflict", "fixture_contract", "; ".join(conflicts), metrics
        got = [compact_directive(item) for item in directives]
        normalized_expected = [{
            "note_index": item["note_index"], "applies": item["applies"],
            "directive_type": item["directive_type"],
            "structured_adjustment": item.get("structured_adjustment"),
        } for item in expected]
        if got != normalized_expected:
            metrics, _ = compare_schedule_metrics(case, None)
            return "failed", "directive_interpretation", f"expected {normalized_expected}; got {got}", metrics

    fixture_metrics = expected_schedule_metrics(case)
    comparison_case = case
    expected_source = "fixture" if any(value is not None for value in fixture_metrics.values()) else None
    oracle_infeasible = False
    if any(value is None for value in fixture_metrics.values()):
        try:
            oracle_metrics = reference_optimal_metrics(scenario, directives)
            merged = {
                field: fixture_metrics[field] if fixture_metrics[field] is not None else oracle_metrics[field]
                for field in ("total_grid_kwh", "total_cost_bdt")
            }
            expected_output = dict(case.get("expected_output", {})) if isinstance(case.get("expected_output"), dict) else {}
            expected_output.update(merged)
            comparison_case = {**case, "expected_output": expected_output}
            expected_source = "fixture+independent_reference_lp" if expected_source else "independent_reference_lp"
        except ReferenceInfeasible:
            oracle_infeasible = True
            expected_source = "independent_reference_lp"

    status, detail, actual = run_schedule(scenario, directives)
    metrics, metric_errors = compare_schedule_metrics(comparison_case, actual)
    metrics["expected_source"] = expected_source
    if oracle_infeasible:
        if status == "controlled_infeasible":
            metrics["metric_status"] = "expected_infeasible"
            metric_errors = []
        else:
            metrics["metric_status"] = "failed"
            metric_errors = ["independent reference LP expected an infeasible scenario, but output produced a schedule"]
    if metric_errors:
        return "failed", "metric_comparison", "; ".join(metric_errors), metrics
    category = "optimization_focus" if "expected_focus" in case else "full_case"
    if status == "controlled_infeasible" and "expected_focus" in case:
        return "controlled_infeasible", category, detail, metrics
    return status, category, detail, metrics


def synthetic_scenario(index: int) -> ScenarioInput:
    rng = random.Random(index)
    capacity = rng.uniform(80, 300)
    minimum = rng.uniform(0, capacity*0.3)
    initial = rng.uniform(minimum, capacity)
    return ScenarioInput.model_validate({
        "scenario_id": f"GENERATED-{index}", "operator_notes": ["Routine memo; no energy change."],
        "hours": [{"hour": h, "demand_kwh": rng.uniform(50, 250),
                   "solar_kwh": rng.uniform(0, 100) if 6 <= h <= 17 else 0,
                   "tariff_bdt_per_kwh": rng.uniform(3, 10) if h < 7 else rng.uniform(10, 30)}
                  for h in range(24)],
        "battery": {"capacity_kwh": capacity, "initial_energy_kwh": initial,
                    "minimum_energy_kwh": minimum, "max_charge_kwh_per_hour": rng.uniform(10, 70),
                    "max_discharge_kwh_per_hour": rng.uniform(10, 70)},
    })


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run GridWise JSON and generated validation cases")
    parser.add_argument("--cases-dir", action="append", default=[],
                        help="JSON file or directory; repeat to combine sources")
    parser.add_argument("--generated", type=int, default=1000,
                        help="number of deterministic generated cases (default: 1000)")
    parser.add_argument("--output", default=str(ROOT/"test_report.json"), help="report JSON path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started = time.perf_counter()
    files = discover_json(args.cases_dir)
    entries = load_cases(files)
    details: list[dict[str, Any]] = []
    counts = {"passed": 0, "failed": 0, "dataset_conflict": 0,
              "controlled_infeasible": 0, "controlled_rejection": 0}
    categories: dict[str, dict[str, int]] = {}
    comparison_table: list[dict[str, Any]] = []
    comparison_by_file: dict[str, dict[str, int]] = {}

    for ordinal, entry in enumerate(entries):
        try:
            status, category, detail, metrics = run_loaded(entry)
        except Exception as exc:
            status, category, detail = "failed", "unexpected_error", f"{type(exc).__name__}: {exc}"
            metrics = {"metric_status": "not_run"}
        counts[status] += 1
        categories.setdefault(category, {key: 0 for key in counts})[status] += 1
        case = entry.get("case", {})
        test_id = case.get("id", f"json-{ordinal}") if isinstance(case, dict) else f"json-{ordinal}"
        source = entry.get("source")
        source_file = Path(source).name if source else None
        row = {
            "test": test_id,
            "source_file": source_file,
            "test_status": status,
            **metrics,
        }
        comparison_table.append(row)
        if source_file:
            file_counts = comparison_by_file.setdefault(source_file, {
                "cases": 0, "metric_passed": 0, "metric_failed": 0,
                "metric_not_available": 0, "metric_not_run": 0,
                "metric_expected_infeasible": 0,
            })
            file_counts["cases"] += 1
            metric_key = f"metric_{metrics.get('metric_status', 'not_run')}"
            if metric_key in file_counts:
                file_counts[metric_key] += 1
        if status != "passed":
            details.append({"test": test_id, "status": status,
                            "category": category, "source": source, "detail": detail})

    for index in range(max(0, args.generated)):
        try:
            scenario = synthetic_scenario(index)
            status, detail, _ = run_schedule(scenario, interpret_locally(scenario))
        except Exception as exc:
            status, detail = "failed", f"{type(exc).__name__}: {exc}"
        category = "generated_optimization"
        counts[status] += 1
        categories.setdefault(category, {key: 0 for key in counts})[status] += 1
        if status != "passed":
            details.append({"test": f"generated-{index}", "status": status,
                            "category": category, "detail": detail})

    total = len(entries)+max(0, args.generated)
    executable = total-counts["dataset_conflict"]
    acceptable = counts["passed"]+counts["controlled_infeasible"]+counts["controlled_rejection"]
    report = {
        "total_tests": total, "json_files": len(files), **counts,
        "accuracy_metrics": {
            "strict_pass_rate": round(counts["passed"]/total, 6) if total else 0,
            "executable_success_rate": round(acceptable/executable, 6) if executable else 0,
        },
        "category_metrics": categories,
        "metric_tolerance": METRIC_TOLERANCE,
        "metric_comparison_summary_by_file": comparison_by_file,
        "metric_comparison_table": comparison_table,
        "duration_seconds": round(time.perf_counter()-started, 3),
        "case_details": details,
    }
    output = Path(args.output).expanduser().resolve()
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    csv_output = output.with_name(f"{output.stem}_metric_comparison.csv")
    csv_fields = [
        "test", "source_file", "test_status", "metric_status", "expected_source", "tolerance",
        "expected_total_grid_kwh", "output_total_grid_kwh", "total_grid_kwh_diff",
        "expected_total_cost_bdt", "output_total_cost_bdt", "total_cost_bdt_diff",
    ]
    with csv_output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(comparison_table)
    print(json.dumps({key: report[key] for key in (
        "total_tests", "json_files", "passed", "failed", "dataset_conflict",
        "controlled_infeasible", "controlled_rejection", "accuracy_metrics", "duration_seconds"
    )}, indent=2, ensure_ascii=False))
    print(f"Full report: {output}")
    print(f"Metric comparison CSV: {csv_output}")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
