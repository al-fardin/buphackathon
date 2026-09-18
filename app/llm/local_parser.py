from __future__ import annotations

import re
from typing import Any

from app.models.schemas import BatteryInput

BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
NUMBER = r"(\d+(?:\.\d+)?)"


def _normalize(text: str) -> str:
    text = text.translate(BN_DIGITS).lower()
    replacements = {
        "a.m.": "am", "p.m.": "pm", "a.m": "am", "p.m": "pm",
        "মধ্যরাত": "midnight", "দুপুর": "noon", "সকাল": "morning",
        "সন্ধ্যা": "evening", "রাত": "night", "থেকে": "to", "পর্যন্ত": "to",
        "theke": "to", "porjonto": "to", "until": "to", "till": "to",
        "bondho": "disabled", "বন্ধ": "disabled", "রাখো": "keep",
        "রাখতে": "keep", "কমবে": "reduction", "কমিয়ে": "reduction",
        "ব্যাটারি": "battery", "চার্জ": "charge", "ডিসচার্জ": "discharge",
        "সর্বনিম্ন": "at least", "বজায়": "maintain",
        "কমপক্ষে": "at least", "রিজার্ভ": "reserve", "শতাংশে": "%", "শতাংশ": "%",
        "অন্তত": "at least", "নিচে": "below", "থাকবে": "remain",
        "সৌর বিদ্যুৎ": "solar", "সোলার": "solar", "আউটপুট": "output",
        "গ্রিড": "grid", "বেশি": "more", "নেওয়া": "take", "নেওয়া": "take",
        "যাবে না": "not allowed", "যাবে": "allowed", "করা যাবে না": "not allowed",
        "ব্যাটারি থেকে বিদ্যুৎ দেওয়া": "battery discharge",
        "ব্যাটারি থেকে বিদ্যুৎ দেওয়া": "battery discharge", "বিদ্যুৎ": "energy",
        "কম থাকবে": "reduction", "নেমে আসবে": "remain", "নামবে": "remain", "সময়ে": "time",
        "সময়": "time", "সময়": "time", "দামের": "tariff", "কম": "low",
        "বেশি দামের": "high tariff", "ব্যবহার": "use", "পরিবর্তন দরকার নেই": "no change",
        "dupur": "noon", "sondhar": "evening", "sondha": "evening",
        "shokal": "morning", "raat": "night", "kombe": "reduction",
        "rakhte hobe": "keep", "rakhte": "keep", "korba na": "not allowed",
        "koro": "charge", "neme ashbe": "remain", "beshi jabe na": "must not exceed",
        "dhoro": "remain", "ধরতে হবে": "remain", "পূর্বাভাসের": "forecast",
        "সৌর শক্তি": "solar", "সীমাবদ্ধ থাকবে": "remain", "নিষ্ক্রিয়": "unavailable",
        "শূন্য": "zero", "ক্ষমতা": "power", "শক্তি": "energy",
        "সর্বোচ্চ": "maximum", "সর্বাধিক": "maximum",
        "সীমার মধ্যে": "limit", "আমদানি": "import",
        "ফিডার": "feeder",
    }
    # Longest first prevents Bangla "charge" from corrupting "discharge".
    for old, new in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        text = text.replace(old, new)
    text = text.replace("টার", "").replace("টা", "")
    text = re.sub(r"(?<=\d)\s*ta\b", "", text)
    text = re.sub(r"(\d{1,2}):00", r"\1", text)
    text = text.replace("percent", "%")
    return " ".join(text.split())


def _clock(value: str, meridiem: str | None) -> int:
    hour = int(float(value))
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    return hour % 24


def extract_hours(text: str) -> list[int]:
    text = _normalize(text)
    if "all day" in text or "সারাদিন" in text:
        return list(range(24))
    if "overnight" in text:
        return [0, 1, 2, 3, 4, 5]
    patterns = [
        rf"(?:from|between)?\s*{NUMBER}\s*(am|pm)?\s*(?:to|and|-)\s*{NUMBER}\s*(am|pm)?",
        rf"{NUMBER}\s*(am|pm)\s*to\s*{NUMBER}\s*(am|pm)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        groups = match.groups()
        start_value, start_meridiem, end_value, end_meridiem = groups[-4:]
        if start_meridiem is None and end_meridiem is not None:
            start_meridiem = end_meridiem
        start = _clock(start_value, start_meridiem)
        end = _clock(end_value, end_meridiem)
        if start_meridiem is None and end_meridiem is None:
            if "noon" in text and start == 12 and end < 12:
                end += 12
            elif "evening" in text and start < 12:
                start += 12
                end += 12
        if start == end:
            return list(range(24))
        return list(range(start, end)) if start < end else list(range(start, 24)) + list(range(end))
    mixed_start = re.search(rf"(midnight|noon)\s*(?:to|and|-)\s*{NUMBER}\s*(am|pm)", text)
    if mixed_start:
        start = 0 if mixed_start.group(1) == "midnight" else 12
        end = _clock(mixed_start.group(2), mixed_start.group(3))
        return list(range(start, end)) if start < end else list(range(start, 24)) + list(range(end))
    mixed_end = re.search(rf"{NUMBER}\s*(am|pm)\s*(?:to|and|-)\s*(midnight|noon)", text)
    if mixed_end:
        start = _clock(mixed_end.group(1), mixed_end.group(2))
        end = 0 if mixed_end.group(3) == "midnight" else 12
        return list(range(start, end)) if start < end else list(range(start, 24)) + list(range(end))
    word_range = re.search(r"(midnight|noon)\s*(?:to|and|-)\s*(midnight|noon)", text)
    if word_range:
        start = 0 if word_range.group(1) == "midnight" else 12
        end = 0 if word_range.group(2) == "midnight" else 12
        return list(range(start, end)) if start < end else list(range(start, 24)) + list(range(end))
    plain = re.search(r"(?:from|between)?\s*(\d{1,2})\s*(?:to|and|-)\s*(\d{1,2})", text)
    if plain:
        start, end = int(plain.group(1)), int(plain.group(2))
        if "noon" in text and start == 12 and end < 12:
            end += 12
        elif "evening" in text and start < 12:
            start += 12
            end += 12
        elif "morning" in text and start == 12:
            start = 0
        return list(range(start, end)) if start < end else list(range(start, 24)) + list(range(end))
    hour_number = re.search(r"(?:during|at|e)?\s*hour\s*(\d{1,2})", text)
    if hour_number:
        return [int(hour_number.group(1))]
    bangla_clock = re.search(r"(?:noon|morning|evening|night)\s*(\d{1,2})(?:টার)?", text)
    if bangla_clock:
        value = int(bangla_clock.group(1))
        context = re.search(r"(noon|morning|evening|night)", text).group(1)
        if context in {"noon", "evening", "night"} and value < 12:
            value += 12
        return [value % 24]
    single = re.search(rf"(?:at|during)?\s*{NUMBER}\s*(am|pm)", text)
    if single:
        return [_clock(single.group(1), single.group(2))]
    if "noon" in text:
        return [12]
    if "morning" in text:
        return list(range(6, 12))
    if "evening" in text or "sandhya" in text:
        return [18, 19, 20]
    if "night" in text:
        return [21, 22, 23]
    return []


def _number_before(text: str, words: str) -> float | None:
    match = re.search(rf"{NUMBER}\s*(?:kwh)?\s*(?:{words})", text)
    return float(match.group(1)) if match else None


def _parse_one(note: str, battery: BatteryInput, fallback_hours: list[int] | None = None) -> dict[str, Any]:
    text = _normalize(note)
    hours = extract_hours(text) or list(fallback_hours or [])
    solar_terms = ("solar", "pv", "panel", "inverter", "rooftop", "সোলার")
    reserve_terms = ("reserve", "remain in the battery", "keep at least", "keep battery", "battery above",
                     "battery energy", "battery state", "stored battery", "battery capacity stored",
                     "maintain", "রিজার্ভ")
    charge_off = ("do not charge", "no charge", "charging is disabled", "charging is not allowed",
                  "charger will be isolated", "charger unavailable", "charging circuit",
                  "charge disabled", "battery charge disabled", "charge off", "charge not allowed",
                  "charging is blocked")
    discharge_off = ("do not discharge", "must not discharge", "no discharge", "discharging is disabled",
                     "discharge disabled", "discharge unavailable", "discharge must stop",
                     "discharge not allowed", "discharge blocked", "discharge is blocked")

    if any(term in text for term in solar_terms) and any(term in text for term in (
        "reduce", "reduction", "drop", "leave", "remain", "change", "fraction", "forecast", "cloud", "clean", "কম"
    )):
        factor = None
        remain = re.search(rf"(?:to|at|as|about|roughly|leave about|treated as roughly|remain)\s*{NUMBER}\s*%", text)
        reduction = re.search(rf"{NUMBER}\s*%\s*(?:reduction|drop|less|কম)", text)
        reduced_by = re.search(rf"(?:reduce|reduced|drop|drops|cut)(?:\s+\w+){{0,4}}\s+by\s+{NUMBER}\s*%", text)
        reduction_before = re.search(rf"(?:reduction|drop|less)\s*{NUMBER}\s*%", text)
        remaining_after = re.search(rf"{NUMBER}\s*%(?:\s+\w+){{0,4}}\s+(?:remain|remaining)", text)
        change_to = re.search(rf"{NUMBER}\s*%\s+to\s+change", text)
        change_by = re.search(rf"{NUMBER}\s*%\s+by\s+change", text)
        if reduction:
            factor = 1-float(reduction.group(1))/100
        elif reduced_by:
            factor = 1-float(reduced_by.group(1))/100
        elif reduction_before:
            factor = 1-float(reduction_before.group(1))/100
        elif remaining_after:
            factor = float(remaining_after.group(1))/100
        elif change_to:
            factor = float(change_to.group(1))/100
        elif change_by:
            factor = 1-float(change_by.group(1))/100
        elif remain:
            factor = float(remain.group(1))/100
        elif "half" in text or "50%" in text:
            factor = 0.5
        if factor is not None and hours:
            return {"directive_type": "solar_reduction", "structured_adjustment": {
                "hours": hours, "factor": round(max(0, min(1, factor)), 8)
            }, "explanation": "Usable solar is reduced during the stated window."}

    if (any(term in text for term in discharge_off) or
            ("discharge" in text and any(x in text for x in (
                "not allowed", "disabled", "unavailable", "zero", "block")))) and hours:
        return {"directive_type": "no_discharge_window", "structured_adjustment": {"hours": hours},
                "explanation": "Battery discharge is unavailable during the stated window."}
    if (any(term in text for term in charge_off) or
            ("charge" in text and "discharge" not in text and any(x in text for x in (
                "not allowed", "disabled", "unavailable", "zero", "block")))) and hours:
        return {"directive_type": "no_charge_window", "structured_adjustment": {"hours": hours},
                "explanation": "Battery charging is unavailable during the stated window."}

    grid_signal = any(term in text for term in ("grid import", "grid intake", "grid usage", "grid", "feeder", "transformer", "substation"))
    grid_limit = any(term in text for term in (
        "must not exceed", "should not exceed", "at or below", "below", "limit", "maximum", "cap",
        "capped", "ceiling", "more")) or ("not allowed" in text and "grid" in text)
    if grid_signal and grid_limit and hours:
        numeric_cap = re.search(rf"{NUMBER}\s*kwh", text)
        cap = float(numeric_cap.group(1)) if numeric_cap else None
        if cap is None:
            cap = _number_before(text, "kwh|of grid|grid|er")
        if cap is not None:
            return {"directive_type": "max_grid_window", "structured_adjustment": {
                "hours": hours, "max_grid_kwh": cap
            }, "explanation": "Grid import is capped during the stated window."}

    reserve_signal = any(term in text for term in reserve_terms) or (
        "battery" in text and "kwh" in text and any(term in text for term in ("below", "lower", "at least"))
    )
    if reserve_signal and hours:
        percent = re.search(rf"{NUMBER}\s*%\s*(?:of\s+)?(?:the\s+)?battery", text)
        reserve = battery.capacity_kwh*float(percent.group(1))/100 if percent else None
        if reserve is None:
            match = re.search(rf"(?:at least|above|reserve|requires|maintain|minimum|no lower than|below)\s*{NUMBER}\s*(?:kwh)?", text)
            reserve = float(match.group(1)) if match else None
        if reserve is None and "kwh" in text:
            generic = re.search(rf"{NUMBER}\s*kwh", text)
            reserve = float(generic.group(1)) if generic else None
        if reserve is not None:
            return {"directive_type": "minimum_battery_reserve", "structured_adjustment": {
                "hours": hours, "minimum_energy_kwh": reserve
            }, "explanation": "A minimum battery reserve is required during the stated window."}

    return {"directive_type": "no_op", "structured_adjustment": None,
            "explanation": "The note does not affect this 24-hour energy schedule."}


def _split_clauses(note: str) -> list[str]:
    text = _normalize(note)
    text = re.sub(r",\s*(?:and\s+)?", " || ", text)
    text = re.sub(r"\s+(?:ar|এবং|and)\s+(?=(?:battery|keep|do not|limit|grid|solar|charging|discharge|the football))", " || ", text)
    return [part.strip(" .") for part in text.split("||") if part.strip(" .")]


def parse_note_all(note: str, battery: BatteryInput) -> list[dict[str, Any]]:
    """Extract every compatible clause while preserving textual order."""
    global_hours = extract_hours(note)
    clauses = _split_clauses(note)
    parsed: list[dict[str, Any]] = []
    for clause in clauses:
        item = _parse_one(clause, battery, global_hours)
        if item["directive_type"] != "no_op":
            parsed.append(item)
        elif any(word in clause for word in ("football", "event", "অনুষ্ঠান")):
            parsed.append(item)
    if not parsed:
        parsed = [_parse_one(note, battery, global_hours)]
    # A shared explicit window applies to clauses without their own window.
    for item in parsed:
        adjustment = item.get("structured_adjustment")
        if isinstance(adjustment, dict) and not adjustment.get("hours") and global_hours:
            adjustment["hours"] = global_hours
    return parsed


def parse_note(note: str, battery: BatteryInput) -> dict[str, Any]:
    """Canonical one-entry-per-note parser used by the official API mode."""
    return parse_note_all(note, battery)[0]
