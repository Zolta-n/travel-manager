"""Claude-based itinerary extraction with deterministic date / time-zone checks.

Claude reads the original document (PDF, photo or email text) and returns
structured booking segments. Nothing the model returns is trusted as-is:
every date, time and IANA time zone is re-validated here with zoneinfo, UTC
instants are computed locally, and anything uncertain is marked for review.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
import re
import threading
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOGGER = logging.getLogger("travel_manager.ai")

DEFAULT_MODEL = "claude-opus-5-5"
MAX_PDF_BYTES = 12 * 1024 * 1024
MAX_IMAGE_BYTES = 3_700_000  # base64 must stay under the API's 5 MB image limit
MAX_TEXT_CHARS = 100_000
JOB_TTL_SECONDS = 30 * 60
MAX_QUEUED_JOBS = 20

CATEGORIES = ["Flight", "Rail / travel", "Rental car", "Hotel / accommodation",
              "Appointment", "Meal", "Transfer", "Other"]
STATUSES = ["Confirmed", "Planned", "Cancelled", "Needs verification"]
DOC_TYPES = ["booking_confirmation", "itinerary", "boarding_pass", "e_ticket", "invoice",
             "receipt", "cancellation", "change_notice", "other"]
DATE_RX = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
TIME_RX = re.compile(r"([01]\d|2[0-3]):[0-5]\d\Z")
OFFSET_RX = re.compile(r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?\Z", re.I)
CURRENCY_RX = re.compile(r"[A-Z]{3}\Z")


class AIError(Exception):
    def __init__(self, message, code="ai_error", status=502):
        super().__init__(message)
        self.code, self.status = code, status


# ---------------------------------------------------------------- configuration

@dataclass(frozen=True)
class AIConfig:
    api_key: str
    model: str = DEFAULT_MODEL
    timeout: float = 240.0

    @property
    def enabled(self):
        return bool(self.api_key)

    @classmethod
    def from_env(cls):
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if key and not re.fullmatch(r"sk-ant-[A-Za-z0-9_-]{20,300}", key):
            LOGGER.warning("ANTHROPIC_API_KEY has an unexpected format; AI reading is disabled")
            key = ""
        model = os.environ.get("TRAVEL_AI_MODEL", "").strip() or DEFAULT_MODEL
        if not re.fullmatch(r"claude-[a-z0-9.-]{1,60}", model):
            model = DEFAULT_MODEL
        return cls(api_key=key, model=model)


# ---------------------------------------------------------------- output schema

def _string():
    return {"type": "string"}


ENDPOINT_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string", "description": "Local date YYYY-MM-DD, or empty if not stated."},
        "time": {"type": "string", "description": "Local 24-hour time HH:MM, or empty if not stated."},
        "timeZone": {"type": "string", "description": "IANA zone of the place this time applies to, e.g. Asia/Shanghai."},
        "printedUtcOffset": {"type": "string", "description": "UTC offset printed in the document for this time, e.g. +08:00, else empty."},
        "location": {"type": "string", "description": "Airport/station/hotel/branch/venue name with city."},
        "code": {"type": "string", "description": "IATA airport or station code if printed, else empty."},
    },
    "required": ["date", "time", "timeZone", "printedUtcOffset", "location", "code"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "documentType": {"type": "string", "enum": DOC_TYPES},
        "summary": {"type": "string", "description": "One sentence describing the document."},
        "documentDate": {"type": "string", "description": "Issue/booking date YYYY-MM-DD if printed, else empty."},
        "segments": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": CATEGORIES},
                "title": {"type": "string", "description": "Short title, e.g. 'MU587 Shanghai PVG → Detroit DTW' or 'Stay · Hilton Garden Inn'."},
                "status": {"type": "string", "enum": STATUSES},
                "start": ENDPOINT_SCHEMA,
                "end": ENDPOINT_SCHEMA,
                "provider": {"type": "string", "description": "Airline, hotel, rental company, restaurant or organiser."},
                "confirmation": {"type": "string", "description": "Booking reference / PNR / reservation number."},
                "serviceNumber": {"type": "string", "description": "Flight or train number, room type, car class, if any."},
                "address": {"type": "string"},
                "notes": {"type": "string", "description": "Seat, baggage, terminal, cabin, contact, cancellation policy - only if printed."},
                "yearInferred": {"type": "boolean", "description": "True if the year was not printed and you inferred it."},
                "timeZoneInferred": {"type": "boolean", "description": "True if no zone/offset was printed and you derived it from the place."},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "evidence": {"type": "string", "description": "Short verbatim quote showing the date/time you used."},
                "issues": {"type": "array", "items": _string(), "description": "Anything ambiguous or missing for this segment."},
            },
            "required": ["category", "title", "status", "start", "end", "provider", "confirmation",
                         "serviceNumber", "address", "notes", "yearInferred", "timeZoneInferred",
                         "confidence", "evidence", "issues"],
            "additionalProperties": False,
        }},
        "costs": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "description": _string(),
                "amount": {"type": "number"},
                "currency": {"type": "string", "description": "ISO 4217 code."},
                "date": {"type": "string", "description": "YYYY-MM-DD or empty."},
                "kind": {"type": "string", "enum": ["Invoice", "Receipt", "Estimate"]},
                "segmentIndex": {"type": "integer", "description": "Index into segments this cost belongs to, or -1."},
            },
            "required": ["description", "amount", "currency", "date", "kind", "segmentIndex"],
            "additionalProperties": False,
        }},
        "trip": {"type": "object", "properties": {
            "matchedTripId": {"type": "string", "description": "tripId of the existing trip this belongs to, or empty."},
            "reason": _string(),
            "newTripName": {"type": "string", "description": "Suggested trip name if no existing trip fits, else empty."},
            "newTripDestination": _string(),
            "newTripType": {"type": "string", "enum": ["Business", "Private"],
                            "description": "For a suggested new trip: Business (work travel, company booking, conference, corporate rate) or Private."},
        }, "required": ["matchedTripId", "reason", "newTripName", "newTripDestination", "newTripType"],
            "additionalProperties": False},
        "warnings": {"type": "array", "items": _string()},
    },
    "required": ["documentType", "summary", "documentDate", "segments", "costs", "trip", "warnings"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You extract travel itinerary data from one document (booking confirmation, e-ticket, boarding pass, hotel or car-rental voucher, restaurant or meeting confirmation, invoice, receipt, or an email containing any of these). The output becomes the traveller's itinerary, so accuracy matters more than completeness: never invent a value that is not supported by the document; use an empty string when something is not stated.

Segments
- One segment per bookable item: every flight or train leg separately (a connection is two legs), one hotel stay (start = check-in, end = check-out), one car rental (start = pick-up, end = return), one transfer, meal, meeting or appointment.
- A round trip in one booking is two or more segments. Do not merge legs.
- For cancellations or schedule changes, still output the affected segments, set status "Cancelled" for cancelled ones, and say what changed in issues.
- status is "Confirmed" only when the document confirms the booking; otherwise "Planned" or "Needs verification".

Dates, times and time zones
- date is the LOCAL calendar date and time is the LOCAL 24-hour clock time at the place where that event happens: departure time at the departure airport, arrival time at the arrival airport, check-in at the hotel.
- Watch for next-day markers such as "+1", "(+1)", "next day", or an arrival date printed separately. Overnight flights must arrive on the correct local date.
- timeZone must be a canonical IANA zone name for that place (e.g. Asia/Shanghai, America/Detroit, Europe/Berlin). Derive it from the airport code, station, city or address. Set timeZoneInferred true unless a zone or UTC offset was printed.
- If the document prints a UTC offset or zone abbreviation for a time, copy the offset into printedUtcOffset (e.g. "+08:00").
- If no time is printed (common for hotel check-in/out or rental returns), leave time empty. Do not use policy times like "check-in from 15:00" unless they are the booked time; mention them in notes instead.
- If the year is not printed, infer it from the document date, the trip list and today's date (the next future occurrence is usually right), and set yearInferred true.
- Numeric dates such as 03/04/2026 are ambiguous. Decide day/month order from the issuer's country, language, currency and other unambiguous dates in the same document. If it is still ambiguous, choose the most likely reading, set confidence "low" and explain in issues.

Other fields
- confidence: "high" only when the date, time and place of both ends are clearly printed (or the end is legitimately absent, e.g. a meal); "medium" when something was inferred; "low" when you are unsure.
- evidence: a short verbatim quote containing the main date/time you used.
- costs: only totals that are explicitly printed with a currency (grand total, amount paid, invoice total). Never sum or estimate. Use kind "Estimate" for quoted prices not yet paid.
- trip: you receive the traveller's existing trips. Set matchedTripId only when the segments clearly fall inside that trip's dates (allow one day either side) and destination. Business and private trips can overlap; if more than one could fit, leave it empty and explain in reason. If none fits, suggest newTripName (short, e.g. "Detroit business trip" or "Kyoto spring holiday"), newTripDestination and newTripType.
- Write all output text in English, but copy names, addresses and references exactly as printed."""


# ---------------------------------------------------------------- request building

def build_content(binary, mime, name, text=None, image=None):
    """Return Claude content blocks for a document. `image` is (bytes, media_type) when pre-scaled."""
    mime = (mime or "").lower()
    suffix = os.path.splitext(name or "")[1].lower()
    if binary[:5] == b"%PDF-" or mime == "application/pdf" or suffix == ".pdf":
        if len(binary) > MAX_PDF_BYTES:
            raise AIError("This PDF exceeds the 12 MB AI reading limit.", "too_large", 413)
        return [{"type": "document", "title": name[:200] or "document.pdf",
                 "source": {"type": "base64", "media_type": "application/pdf",
                            "data": base64.standard_b64encode(binary).decode("ascii")}}]
    if image is not None:
        data, media = image
        return [{"type": "image", "source": {"type": "base64", "media_type": media,
                                              "data": base64.standard_b64encode(data).decode("ascii")}}]
    if text is not None:
        if not text.strip():
            raise AIError("The document contains no readable text.", "no_text", 422)
        return [{"type": "text", "text": "Document file: " + (name or "document") +
                 "\n----- DOCUMENT TEXT START -----\n" + text[:MAX_TEXT_CHARS] + "\n----- DOCUMENT TEXT END -----"}]
    raise AIError("This format cannot be read by AI. Use PDF, a photo (JPEG/PNG/WebP/GIF), an .eml email or text.",
                  "unsupported_type", 422)


def build_context(state, today=None):
    trips = [{"tripId": t.get("tripId", ""), "name": t.get("name", ""), "type": t.get("type", ""),
              "destination": t.get("destination", ""), "startDate": t.get("startDate", ""),
              "endDate": t.get("endDate", "")} for t in (state or {}).get("trips", [])][:200]
    home = str((state or {}).get("settings", {}).get("homeZone") or "Asia/Shanghai")
    return {"today": (today or date.today()).isoformat(), "homeZone": home, "trips": trips}


def build_request(content, context, name, model):
    instruction = ("Extract the itinerary from the document above.\n"
                   "Document filename: " + (name or "unknown") + "\n"
                   "Today's date: " + context["today"] + "\n"
                   "Traveller's home time zone: " + context["homeZone"] + "\n"
                   "Existing trips (JSON): " + json.dumps(context["trips"], ensure_ascii=False))
    return {
        "model": model,
        "max_tokens": 32000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": [*content, {"type": "text", "text": instruction}]}],
        "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        "betas": ["server-side-fallback-2026-07-01"],
        "fallbacks": "default",
    }


def _stream(client, request):
    import anthropic
    try:
        with client.beta.messages.stream(**request) as stream:
            return stream.get_final_message()
    except anthropic.BadRequestError as exc:
        # Never lose AI reading just because the optional refusal fallback is unavailable.
        if "fallbacks" not in request or "fallback" not in str(getattr(exc, "message", "")).lower():
            raise
        LOGGER.warning("Refusal fallback rejected by the API; retrying without it")
        plain = {k: v for k, v in request.items() if k not in ("fallbacks", "betas")}
        with client.beta.messages.stream(**plain) as stream:
            return stream.get_final_message()


def call_claude(request, config, client=None):
    import anthropic
    client = client or anthropic.Anthropic(api_key=config.api_key, timeout=config.timeout, max_retries=2)
    try:
        message = _stream(client, request)
    except anthropic.AuthenticationError as exc:
        raise AIError("The Anthropic API key was rejected. Set a new key with install.sh --set-ai-key.", "ai_auth", 502) from exc
    except anthropic.PermissionDeniedError as exc:
        raise AIError("The Anthropic API key is not allowed to use this model.", "ai_permission", 502) from exc
    except anthropic.RateLimitError as exc:
        raise AIError("Claude is rate-limited or out of credit right now. Retry in a few minutes.", "ai_rate_limited", 429) from exc
    except anthropic.BadRequestError as exc:
        raise AIError("Claude could not process this document (" + str(getattr(exc, "message", "bad request"))[:300] + ").", "ai_bad_request", 422) from exc
    except anthropic.APIStatusError as exc:
        raise AIError("The Claude API returned an error (" + str(exc.status_code) + "). Retry later.", "ai_unavailable", 502) from exc
    except anthropic.APIConnectionError as exc:
        raise AIError("The NAS could not reach api.anthropic.com. Check the NAS internet connection.", "ai_unreachable", 502) from exc
    if message.stop_reason == "refusal":
        raise AIError("Claude declined to read this document. Enter the details manually.", "ai_refusal", 422)
    if message.stop_reason == "max_tokens":
        raise AIError("The document is too long for one AI read. Split it into smaller files.", "ai_truncated", 422)
    text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise AIError("Claude returned an unreadable answer. Retry reading.", "ai_bad_output", 502) from exc
    usage = getattr(message, "usage", None)
    return raw, {"model": getattr(message, "model", request["model"]),
                 "inputTokens": getattr(usage, "input_tokens", 0) or 0,
                 "outputTokens": getattr(usage, "output_tokens", 0) or 0}


# ---------------------------------------------------------------- deterministic checks

def _text(value, limit=500):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or "")).strip()[:limit]


def valid_date(value):
    if not isinstance(value, str) or not DATE_RX.match(value):
        return False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return date(1990, 1, 1) <= parsed <= date(2200, 12, 31)


def zone_for(name):
    if not isinstance(name, str) or not name or len(name) > 64 or not re.fullmatch(r"[A-Za-z0-9_+\-/]+", name):
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def parse_offset(value):
    match = OFFSET_RX.fullmatch((value or "").strip())
    if not match:
        return None
    minutes = int(match[2]) * 60 + int(match[3] or 0)
    if minutes > 14 * 60:
        return None
    return minutes if match[1] == "+" else -minutes


def offset_zone(minutes):
    """Etc/GMT zones only exist for whole hours and use inverted signs."""
    if minutes is None or minutes % 60:
        return ""
    hours = minutes // 60
    return "UTC" if hours == 0 else "Etc/GMT" + ("-" if hours > 0 else "+") + str(abs(hours))


def local_instant(day, clock, zone_name):
    """Return (utc_datetime, problem). Detects times skipped or repeated by DST changes."""
    zone = zone_for(zone_name)
    if zone is None:
        return None, "unknown time zone"
    naive = datetime.fromisoformat(day + "T" + (clock or "12:00"))
    first, second = naive.replace(tzinfo=zone, fold=0), naive.replace(tzinfo=zone, fold=1)
    utc = first.astimezone(timezone.utc)
    if utc.astimezone(zone).replace(tzinfo=None) != naive:
        return None, "this local time does not exist (daylight-saving change)"
    if clock and first.utcoffset() != second.utcoffset():
        return utc, "this local time occurs twice (daylight-saving change); check which one is meant"
    return utc, ""


def _endpoint(raw):
    raw = raw if isinstance(raw, dict) else {}
    out = {key: _text(raw.get(key), 300) for key in ("date", "time", "timeZone", "printedUtcOffset", "location", "code")}
    out["code"] = out["code"].upper()[:8]
    return out


def _check_endpoint(point, label, problems, flags):
    if point["date"] and not valid_date(point["date"]):
        problems.append(label + " date is not a valid calendar date.")
        point["date"] = ""
    if point["time"] and not TIME_RX.match(point["time"]):
        problems.append(label + " time is not a valid 24-hour time.")
        point["time"] = ""
    printed = parse_offset(point["printedUtcOffset"])
    if point["timeZone"] and zone_for(point["timeZone"]) is None:
        fallback = offset_zone(printed)
        problems.append(label + " time zone '" + point["timeZone"] + "' is not a valid IANA zone" +
                        ("; used the printed UTC offset instead." if fallback else "."))
        point["timeZone"] = fallback
    if not point["timeZone"] and printed is not None:
        point["timeZone"] = offset_zone(printed)
    if point["time"] and not point["timeZone"]:
        problems.append(label + " time was read, but its time zone is unknown.")
    instant = None
    if point["date"] and point["timeZone"]:
        instant, issue = local_instant(point["date"], point["time"], point["timeZone"])
        if issue:
            (problems if instant is None else flags).append(label + " " + issue + ".")
        if instant is not None and printed is not None and point["time"]:
            actual = instant.astimezone(zone_for(point["timeZone"])).utcoffset()
            if actual is not None and int(actual.total_seconds() // 60) != printed:
                problems.append(label + " printed UTC offset " + point["printedUtcOffset"] +
                                " does not match " + point["timeZone"] + " on that date.")
    return instant


def _trip_for(trip_id, trips):
    return next((t for t in trips if t.get("tripId") == trip_id), None)


def _inside(day, trip, slack=1):
    if not (valid_date(day) and valid_date(trip.get("startDate", "")) and valid_date(trip.get("endDate", ""))):
        return False
    d = date.fromisoformat(day)
    return date.fromisoformat(trip["startDate"]) - timedelta(days=slack) <= d <= date.fromisoformat(trip["endDate"]) + timedelta(days=slack)


def normalize_segment(raw, trips, matched_trip):
    problems, flags = [], []
    category = raw.get("category") if raw.get("category") in CATEGORIES else "Other"
    status = raw.get("status") if raw.get("status") in STATUSES else "Needs verification"
    start, end = _endpoint(raw.get("start")), _endpoint(raw.get("end"))
    start_utc = _check_endpoint(start, "Start", problems, flags)
    end_utc = _check_endpoint(end, "End", problems, flags)
    if not start["date"]:
        problems.append("No start date could be read.")
    if end["time"] and not end["date"]:
        problems.append("End time has no end date.")
    duration = None
    if start_utc and end_utc and start["time"] and end["time"]:
        duration = int((end_utc - start_utc).total_seconds() // 60)
        if duration < 0:
            problems.append("End is before start once time zones are applied; check the dates and +1 day markers.")
        elif category in ("Flight", "Rail / travel") and duration > 22 * 60:
            problems.append("Travel time of " + str(duration // 60) + " h is implausible; check the arrival date.")
        elif category == "Flight" and duration < 20:
            problems.append("Flight shorter than 20 minutes; check the times and zones.")
    elif start["date"] and end["date"] and end["date"] < start["date"]:
        problems.append("End date precedes start date.")
    if category == "Hotel / accommodation" and start["date"] and end["date"] and start["date"] < end["date"]:
        nights = (date.fromisoformat(end["date"]) - date.fromisoformat(start["date"])).days
        if nights > 60:
            problems.append("Stay of " + str(nights) + " nights looks implausible.")
    if category in ("Flight", "Rail / travel", "Hotel / accommodation", "Rental car") and not end["date"]:
        problems.append("No end / arrival / check-out / return date was read.")
    if raw.get("yearInferred"):
        flags.append("Year was not printed and was inferred.")
        if not any(_inside(start["date"], t) for t in ([matched_trip] if matched_trip else trips)):
            problems.append("The inferred year does not place this inside a known trip; confirm the year.")
    if raw.get("timeZoneInferred"):
        flags.append("Time zone derived from the location.")
    confidence = raw.get("confidence") if raw.get("confidence") in ("high", "medium", "low") else "low"
    if confidence == "low":
        problems.append("Claude marked this segment as low confidence.")
    issues = [_text(x, 300) for x in (raw.get("issues") or [])[:8] if _text(x)]
    origin = " ".join(x for x in (start["location"], "(" + start["code"] + ")" if start["code"] else "") if x)
    target = " ".join(x for x in (end["location"], "(" + end["code"] + ")" if end["code"] else "") if x)
    notes = "; ".join(x for x in (_text(raw.get("serviceNumber"), 120), _text(raw.get("notes"), 1500)) if x)
    activity = {
        "category": category, "title": _text(raw.get("title"), 200) or category, "status": status,
        "startDate": start["date"], "startTime": start["time"], "startZone": start["timeZone"],
        "endDate": end["date"], "endTime": end["time"], "endZone": end["timeZone"] if end["date"] else "",
        "from": origin, "to": target if target.lower() != origin.lower() else "",
        "address": _text(raw.get("address"), 500), "provider": _text(raw.get("provider"), 200),
        "confirmation": _text(raw.get("confirmation"), 60), "notes": notes,
    }
    problems += issues
    if status == "Cancelled":
        problems.append("The document marks this booking as cancelled; review before changing the itinerary.")
    return {
        "activity": activity, "confidence": confidence, "evidence": _text(raw.get("evidence"), 300),
        "problems": problems, "flags": flags,
        "checks": {"startUtc": start_utc.strftime("%Y-%m-%dT%H:%MZ") if start_utc else "",
                   "endUtc": end_utc.strftime("%Y-%m-%dT%H:%MZ") if end_utc else "",
                   "durationMinutes": duration},
        "autoOk": not problems,
    }


def _segment_key(seg):
    a = seg["activity"]
    return (a["category"], a["startDate"], a["startTime"], a["from"].lower(), a["to"].lower(), a["confirmation"].lower())


def normalize_result(raw, trips):
    raw = raw if isinstance(raw, dict) else {}
    trip_raw = raw.get("trip") if isinstance(raw.get("trip"), dict) else {}
    matched_id = _text(trip_raw.get("matchedTripId"), 256)
    matched = _trip_for(matched_id, trips)
    segments, seen = [], set()
    for item in (raw.get("segments") or [])[:40]:
        if not isinstance(item, dict):
            continue
        seg = normalize_segment(item, trips, matched)
        key = _segment_key(seg)
        if key in seen:
            continue
        seen.add(key)
        segments.append(seg)
    segments.sort(key=lambda s: s["checks"]["startUtc"] or s["activity"]["startDate"] or "9999")
    if matched and segments and not all(_inside(s["activity"]["startDate"], matched) for s in segments if s["activity"]["startDate"]):
        matched = None
        matched_id = ""
    costs = []
    for item in (raw.get("costs") or [])[:20]:
        if not isinstance(item, dict):
            continue
        amount, currency = item.get("amount"), _text(item.get("currency"), 3).upper()
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not 0 <= amount < 10**9 or not CURRENCY_RX.match(currency):
            continue
        index = item.get("segmentIndex")
        costs.append({"description": _text(item.get("description"), 200) or "Total",
                      "amount": round(float(amount), 2), "currency": currency,
                      "date": item.get("date") if valid_date(item.get("date")) else "",
                      "kind": item.get("kind") if item.get("kind") in ("Invoice", "Receipt", "Estimate") else "Estimate",
                      "segmentIndex": index if isinstance(index, int) and 0 <= index < len(segments) else -1})
    doc_type = raw.get("documentType") if raw.get("documentType") in DOC_TYPES else "other"
    return {
        "version": "ai-1", "documentType": doc_type, "summary": _text(raw.get("summary"), 400),
        "documentDate": raw.get("documentDate") if valid_date(raw.get("documentDate")) else "",
        "segments": segments, "costs": costs,
        "trip": {"matchedTripId": matched_id if matched else "", "reason": _text(trip_raw.get("reason"), 400),
                 "newTripName": _text(trip_raw.get("newTripName"), 120),
                 "newTripDestination": _text(trip_raw.get("newTripDestination"), 200),
                 "newTripType": trip_raw.get("newTripType") if trip_raw.get("newTripType") in ("Business", "Private") else "Business"},
        "warnings": [_text(x, 300) for x in (raw.get("warnings") or [])[:10] if _text(x)],
    }


# ---------------------------------------------------------------- background jobs

class JobBoard:
    """Runs one AI read at a time in the background so slow reads never hit
    proxy timeouts (Cloudflare closes requests after 100 seconds)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}
        self.slot = threading.BoundedSemaphore(1)

    def _expire(self):
        cutoff = time.time() - JOB_TTL_SECONDS
        for key in [k for k, v in self.jobs.items() if v["updated"] < cutoff]:
            del self.jobs[key]

    def submit(self, document_id, work):
        with self.lock:
            self._expire()
            for key, job in self.jobs.items():
                if job["documentId"] == document_id and job["status"] in ("queued", "running"):
                    return key
            if sum(j["status"] in ("queued", "running") for j in self.jobs.values()) >= MAX_QUEUED_JOBS:
                raise AIError("Too many documents are waiting for AI reading. Try again shortly.", "ai_busy", 429)
            key = uuid.uuid4().hex
            self.jobs[key] = {"documentId": document_id, "status": "queued", "updated": time.time()}
        threading.Thread(target=self._run, args=(key, work), daemon=True, name="ai-read").start()
        return key

    def _set(self, key, **values):
        with self.lock:
            if key in self.jobs:
                self.jobs[key].update(values, updated=time.time())

    def _run(self, key, work):
        with self.slot:
            self._set(key, status="running")
            try:
                self._set(key, status="done", result=work())
            except AIError as exc:
                self._set(key, status="error", error=str(exc), code=exc.code)
            except Exception:
                LOGGER.exception("AI reading failed; document contents are never logged")
                self._set(key, status="error", error="AI reading failed on the NAS. Check the server log.", code="ai_error")

    def get(self, key):
        with self.lock:
            self._expire()
            job = self.jobs.get(key)
            return None if job is None else {k: v for k, v in job.items() if k != "updated"}


SELFTEST_TEXT = """Booking confirmation ABC123
Flight MU587 Shanghai Pudong (PVG) 11 Nov 2026 13:10 -> Detroit (DTW) 11 Nov 2026 14:35
Flight MU588 Detroit (DTW) 20 Nov 2026 16:00 -> Shanghai Pudong (PVG) 21 Nov 2026 19:30
Total paid: USD 1,234.50"""


def selftest():
    """Run from the container terminal: python /app/ai_reader.py --selftest"""
    config = AIConfig.from_env()
    if not config.enabled:
        print("AI reading is not configured: no valid ANTHROPIC_API_KEY in the container.")
        return 1
    try:
        content = build_content(b"", "text/plain", "selftest.txt", text=SELFTEST_TEXT)
        raw, usage = call_claude(build_request(content, build_context({}), "selftest.txt", config.model), config)
    except AIError as exc:
        print("AI self-test failed: " + str(exc) + " (" + exc.code + ")")
        return 1
    result = normalize_result(raw, [])
    print("AI self-test OK · model " + usage["model"] + " · " + str(usage["inputTokens"]) + " in / " + str(usage["outputTokens"]) + " out tokens")
    for seg in result["segments"]:
        a = seg["activity"]
        print("- " + a["title"] + ": " + a["startDate"] + " " + a["startTime"] + " " + a["startZone"] + " -> " +
              a["endDate"] + " " + a["endTime"] + " " + a["endZone"] + ("" if seg["autoOk"] else "  [review: " + "; ".join(seg["problems"]) + "]"))
    return 0


if __name__ == "__main__":
    import sys
    if sys.argv[1:] != ["--selftest"]:
        print("Usage: python ai_reader.py --selftest")
        raise SystemExit(2)
    raise SystemExit(selftest())
