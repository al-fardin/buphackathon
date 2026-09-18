from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from app.llm.guardrails import DirectiveValidationError, validate_directive
from app.llm.local_parser import parse_note, parse_note_all
from app.models.schemas import BatteryInput, DirectiveInterpretation

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You interpret one smart-grid operator note. Return JSON only with keys:
directive_type, hours, factor, minimum_energy_kwh, max_grid_kwh, explanation.
Allowed directive_type values:
solar_reduction, minimum_battery_reserve, no_charge_window, no_discharge_window,
max_grid_window, no_op. Time windows are start-inclusive/end-exclusive. Hours are
sorted integers 0-23. solar_reduction factor is the fraction remaining, not the
fraction lost. For no_op use hours=[] and all numeric fields=0. For every other
directive, set only its relevant numeric field and set unused numeric fields=0.
Understand English, Bangla, Banglish, and mixed language. Never invent values."""


MODEL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "directive_type": {
            "type": "string",
            "enum": [
                "solar_reduction",
                "minimum_battery_reserve",
                "no_charge_window",
                "no_discharge_window",
                "max_grid_window",
                "no_op",
            ],
        },
        "hours": {
            "type": "array",
            "items": {"type": "integer"},
        },
        "factor": {"type": "number"},
        "minimum_energy_kwh": {"type": "number"},
        "max_grid_kwh": {"type": "number"},
        "explanation": {"type": "string"},
    },
    "required": [
        "directive_type",
        "hours",
        "factor",
        "minimum_energy_kwh",
        "max_grid_kwh",
        "explanation",
    ],
    "additionalProperties": False,
}


class LLMDirectiveInterpreter:
    def __init__(self) -> None:
        self.provider = os.getenv("LLM_PROVIDER", "auto").lower()
        self.timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
        self.openai_key = os.getenv("OPENAI_API_KEY", "")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        self.openai_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
        self.ollama_model = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
        self.allow_multiple = os.getenv("ALLOW_MULTIPLE_DIRECTIVES_PER_NOTE", "false").lower() in {
            "1", "true", "yes", "on"
        }

    async def interpret(self, notes: list[str], battery: BatteryInput) -> list[DirectiveInterpretation]:
        results = []
        for index, note in enumerate(notes):
            raw: dict[str, Any] | list[dict[str, Any]] | None = None
            try:
                if self.provider in {"auto", "openai"} and self.openai_key:
                    raw = await asyncio.to_thread(self._openai, note, battery)
                elif self.provider in {"auto", "ollama"}:
                    raw = await asyncio.to_thread(self._ollama, note, battery)
                if raw is not None:
                    raw_items = raw.get("directives", [raw]) if isinstance(raw, dict) else raw
                    if not self.allow_multiple:
                        raw_items = raw_items[:1]
                    results.extend(validate_directive(item, index, battery) for item in raw_items)
                    continue
            except (DirectiveValidationError, ValueError, OSError, urllib.error.URLError) as exc:
                logger.warning("Model interpretation unavailable or rejected for note %d: %s", index, type(exc).__name__)
            # Controlled deterministic fallback keeps the service available when a provider fails.
            raw_items = parse_note_all(note, battery) if self.allow_multiple else [parse_note(note, battery)]
            results.extend(validate_directive(item, index, battery) for item in raw_items)
        return results

    def _openai(self, note: str, battery: BatteryInput) -> dict[str, Any]:
        payload = {
            "model": self.openai_model,
            "store": False,
            "reasoning": {"effort": "low"},
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": SYSTEM_PROMPT}],
                },
                {
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": json.dumps({
                            "note": note,
                            "battery_capacity_kwh": battery.capacity_kwh,
                        }, ensure_ascii=False),
                    }],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "gridwise_directive",
                    "strict": True,
                    "schema": MODEL_OUTPUT_SCHEMA,
                }
            },
            "max_output_tokens": 1000,
        }
        request = urllib.request.Request(
            f"{self.openai_url}/responses", data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.openai_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.error("OpenAI Responses API returned HTTP %s: %s", exc.code, body[:1000])
            raise

        raw = json.loads(self._extract_output_text(data))
        kind = raw["directive_type"]
        if kind == "no_op":
            adjustment = None
        elif kind == "solar_reduction":
            adjustment = {"hours": raw["hours"], "factor": raw["factor"]}
        elif kind == "minimum_battery_reserve":
            adjustment = {
                "hours": raw["hours"],
                "minimum_energy_kwh": raw["minimum_energy_kwh"],
            }
        elif kind == "max_grid_window":
            adjustment = {"hours": raw["hours"], "max_grid_kwh": raw["max_grid_kwh"]}
        else:
            adjustment = {"hours": raw["hours"]}
        return {
            "directive_type": kind,
            "structured_adjustment": adjustment,
            "explanation": raw["explanation"],
        }

    @staticmethod
    def _extract_output_text(data: dict[str, Any]) -> str:
        parts: list[str] = []
        for item in data.get("output", []):
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []):
                if isinstance(content, dict) and content.get("type") == "output_text":
                    text = content.get("text")
                    if isinstance(text, str):
                        parts.append(text)
        output_text = "".join(parts).strip()
        if not output_text:
            raise ValueError("OpenAI response did not contain output_text")
        return output_text

    def _ollama(self, note: str, battery: BatteryInput) -> dict[str, Any]:
        payload = {"model": self.ollama_model, "stream": False, "format": "json",
                   "prompt": SYSTEM_PROMPT + "\nINPUT: " + json.dumps({
                       "note": note, "battery_capacity_kwh": battery.capacity_kwh
                   }, ensure_ascii=False)}
        request = urllib.request.Request(
            f"{self.ollama_url}/api/generate", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout, 1.5)) as response:
                data = json.load(response)
        except urllib.error.URLError:
            if self.provider == "ollama":
                raise
            return None  # type: ignore[return-value]
        return json.loads(data["response"])
