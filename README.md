# GridWise AI Energy Optimizer

Production-ready FastAPI service for the BUP CSE Fest 2026 GridWise LLM-Assisted Energy Optimization Challenge. It interprets English, Bangla, Banglish, and mixed-language operator notes, validates the resulting directives, solves a 24-hour linear program, and independently replays the returned schedule before responding.

## Architecture

```text
Operator notes
    -> hosted/local LLM interpreter
    -> deterministic directive guardrails
    -> constraint compiler
    -> SciPy HiGHS linear optimizer
    -> 24-hour schedule
    -> independent replay validator
    -> FastAPI JSON response
```

The model output is untrusted. `app/llm/guardrails.py` enforces directive types, note mapping, hour ranges, adjustment shapes, and numeric bounds before any output reaches the optimizer. If a configured provider is temporarily unavailable, a multilingual deterministic parser provides a controlled availability fallback. For official judging, configure OpenAI or Ollama so a language-capable generative model remains in the interpretation path.

The LP minimizes `sum(grid_kwh[h] * tariff[h])` subject to hourly energy balance, available/effective solar, battery state transitions, capacity/reserve bounds, charge/discharge rates, operator windows, grid caps, and exact end-of-day battery neutrality. A tiny cycle penalty breaks equal-cost ties and avoids unnecessary cycling.

## Project layout

```text
app/
  api/routes.py                    API orchestration
  llm/llm_interpreter.py          OpenAI/Ollama adapters and fallback
  llm/local_parser.py             multilingual availability fallback
  llm/guardrails.py               deterministic structured-data checks
  models/schemas.py               strict Pydantic contracts
  optimizer/constraint_engine.py  directive-to-math compilation
  optimizer/linear_optimizer.py   HiGHS linear program
  validator/schedule_validator.py independent schedule replay
  main.py                          FastAPI app and safe errors
tests/test_cases/                  JSON test corpus
test_runner.py                     1000+ case test harness
```

## Local quickstart

Python 3.11 or 3.12 is recommended.

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Configure one LLM route in `.env`:

- OpenAI: set `LLM_PROVIDER=openai`, `OPENAI_API_KEY`, and optionally `OPENAI_MODEL`.
- Local model: run Ollama, pull `qwen2.5:3b`, and set `LLM_PROVIDER=ollama`.
- `auto`: uses OpenAI when a key is set, otherwise checks Ollama, then uses the controlled parser fallback if the provider is unavailable.

Never commit `.env`.

## Docker

```bash
cp .env.example .env
docker compose up --build
```

The API is available at `http://localhost:8000`. The container runs as a non-root user, drops Linux capabilities, has a read-only root filesystem, and contains no secrets.

## API

Health check:

```bash
curl http://localhost:8000/health
```

Response:

```json
{"status":"ok"}
```

Optimization request:

```bash
curl -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  --data @scenario.json
```

`scenario.json` must contain a non-empty `scenario_id`, 1-3 notes, exactly 24 unique hour entries numbered 0-23, and a valid battery configuration. Example battery object:

```json
{
  "capacity_kwh": 200,
  "initial_energy_kwh": 100,
  "minimum_energy_kwh": 20,
  "max_charge_kwh_per_hour": 40,
  "max_discharge_kwh_per_hour": 40
}
```

The success response follows the canonical challenge schema: `scenario_id`, one ordered `directive_interpretation` per note, 24 `hourly_plan` entries using `grid_kwh`, `solar_used_kwh`, `battery_action`, `battery_kwh`, and `battery_energy_after_kwh`, plus `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`, `plan_summary`, and a replay `validation` object.

## Testing

Run the required framework:

```bash
python test_runner.py
```

It discovers JSON files in `tests/test_cases/`, supports both compact language cases and the organizer's public case-pack shape, runs 1,000 deterministic generated optimization/replay cases, and writes `test_report.json` with totals, accuracy, duration, and failure details.

Run unit/API tests separately:

```bash
pytest -q
```

To test the organizer pack, place its JSON file in `tests/test_cases/` and rerun `python test_runner.py`.

Alternative test-pack wrappers are also supported. You can point the runner at one or more files/directories without copying them:

```bash
python test_runner.py --cases-dir "C:\path\to\gridwise-cases" --generated 0
```

For the separately distributed 10,000-case adversarial pack:

```powershell
$env:LLM_PROVIDER="rules"
python test_runner.py `
  --cases-dir "F:\gridwise-ai\tests\test_cases\gridwise_adversarial_schema_cases_10000.json" `
  --generated 0
```

That pack contains 8,000 executable scenarios, 1,000 deliberately malformed
requests, and 1,000 deliberately infeasible scenarios. A correct run therefore
reports `failed: 0`, `controlled_rejection: 1000`,
`controlled_infeasible: 1000`, and `executable_success_rate: 1.0`. The
`strict_pass_rate` is intentionally `0.8` because controlled outcomes are not
mislabelled as ordinary passes.

The report separates real failures from `dataset_conflict` (an expectation that violates the official directive schema) and `controlled_infeasible` (a structurally valid scenario with no feasible schedule). `expected_output.directive_interpretation`, top-level `expected_directive_interpretation`, `expected_focus`, compact language cases, JSON arrays, and `{"cases": [...]}` packs are accepted.

## Deployment

Deploy the container to any platform that exposes port 8000 and permits outbound access to the selected LLM provider. Set secrets through the platform's secret manager. Keep `/health` and `/optimize-energy` public for the judge, use HTTPS at the ingress, set request timeouts above `LLM_TIMEOUT_SECONDS`, and keep at least one warm instance during evaluation.

Recommended production settings:

- Use a low-latency model with JSON output and temperature 0.
- Set provider quotas and alerts before the judging window.
- Keep application logs free of request notes and credentials.
- Restrict CORS at the gateway if a browser client is later added.
- Use horizontal replicas rather than multiple Uvicorn workers per small container.

## Error behavior

- `400`: malformed or structurally invalid input.
- `422`: well-formed scenario infeasible under hard constraints.
- `500`: controlled internal/provider failure; no stack trace or secret is returned.

## Dependencies and credits

FastAPI/Pydantic provide the API and schema layer. SciPy HiGHS solves the linear program. Uvicorn serves ASGI. OpenAI-compatible chat completions and Ollama are supported model providers. No external data is used.

## Contract mode

The canonical challenge contract requires exactly one interpretation entry per operator note, so `ALLOW_MULTIPLE_DIRECTIVES_PER_NOTE=false` is the safe submission default. Custom test packs that explicitly expect several entries with the same `note_index` can use `ALLOW_MULTIPLE_DIRECTIVES_PER_NOTE=true`; the clause-aware interpreter then preserves directive order and applies all extracted hard constraints.
