"""Larkspur disruption agent. This is the file you build.

It runs right now, and it is wrong in four places. The trace shows each one
before the code does, so read the trace first:

    python3 run.py K7PQ2M --trace

Where you edit:   grep -n '✏' agent.py   (six marks, one per place)
Steps and gates:  https://anthropicpartnerbasecamp.bts.com/
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List
from support.tools import check_policy as check_policy_fn
from support import (MODEL, SYSTEM_PROMPT, call_local, execute_tool, mcp_client,
                     new_session, next_available_day, record_tool_result,
                     runtime_preamble)

MAX_TOOL_CALLS = 8  # Larkspur's own build capped the loop here; then a human takes over.

TONE_ADDENDUM = "Rule for Reply: Generate short replies"                       # ✏️ Build 4, step 4.1, intelligence goal

TRANSCRIPTS_PATH = Path(__file__).parent / "data" / "americas" / "transcripts_sample.jsonl"


def reopen_stats(intent_label: str = "") -> Dict[str, Any]:
    """How often did a human-handled chat reopen within 72 hours, and why?
    Reads the eight-transcript discovery sample. Optional filter on intent_label."""
    rows = []
    with open(TRANSCRIPTS_PATH, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    if intent_label:
        rows = [r for r in rows if r.get("intent_label") == intent_label]
    reopened = [r for r in rows if r.get("reopened_within_72h")]
    by_intent: Dict[str, Dict[str, int]] = {}
    for r in rows:
        bucket = by_intent.setdefault(r.get("intent_label", "unknown"), {"chats": 0, "reopened": 0})
        bucket["chats"] += 1
        bucket["reopened"] += 1 if r.get("reopened_within_72h") else 0
    return {
        "chats": len(rows),
        "reopened_within_72h": len(reopened),
        "reopen_rate": round(len(reopened) / len(rows), 2) if rows else None,
        "by_intent": by_intent,
        "reopen_reasons": [{"transcript_id": r["transcript_id"], "intent_label": r.get("intent_label"),
                            "reason": r.get("reopen_reason")} for r in reopened],
        "caveat": "Sample of human-agent chats from the January storms discovery set, "
                  "not all traffic. Treat the rate as anecdotal.",
    }


def care_entitlements(pnr: str, cause_code: str, delay_minutes: int, status: str,
                      wait_minutes_for_alternative: Any = None) -> Dict[str, Any]:
    """The care slice (meal, hotel, ground) of what check_policy resolves.
    Same policy engine, narrower answer: nothing here is decided separately."""
    full = check_policy_fn(pnr=pnr, cause_code=cause_code, delay_minutes=delay_minutes,
                           status=status, wait_minutes_for_alternative=wait_minutes_for_alternative)
    if "error" in full:
        return full
    return {
        "policy_row_id": full.get("policy_row_id"),
        "band": full.get("band"),
        "care": full.get("care"),
        "note": "Care only. Rebooking, refund, goodwill and escalation triggers are not "
                "included; check_policy has those.",
    }


HANDBOOK_PATH = Path(__file__).parent / "data" / "americas" / "fare_rules_excerpt.md"


def what_automation_will_not_do() -> Dict[str, Any]:
    """Handbook section 7, read from fare_rules_excerpt.md: what the chat is not
    allowed to do. Answers only from that file; if the section is missing it says
    so rather than listing limits from memory."""
    try:
        lines = HANDBOOK_PATH.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {"error": "Cannot read the Handbook excerpt (%s). Do not state what the chat "
                         "may or may not do; use escalate_to_human." % exc}
    body, inside = [], False
    for line in lines:
        if line.startswith("### "):
            if inside:
                break
            inside = line[4:].lstrip().startswith("7.")
            continue
        if inside and line.strip():
            body.append(line.strip())
    text = " ".join(body)
    if not text:
        return {"error": "Section 7 was not found in the Handbook excerpt. Do not state what the "
                         "chat may or may not do; use escalate_to_human."}
    out: Dict[str, Any] = {"handbook_section": "7. What chat automation will not do",
                           "text": text, "source": HANDBOOK_PATH.name}
    head, found, rest = text.partition(" are handled by people")
    if found:
        items = [i.strip() for i in head.split(",")]
        out["handled_by_people"] = [i[4:] if i.startswith("and ") else i for i in items]
        out["what_the_assistant_does_instead"] = rest.lstrip(". ").strip()
    out["note"] = ("Compare the customer's request with handled_by_people. Section 7 is the "
                   "only source: do not add limits it does not name, and do not offer to do "
                   "anything on the list.")
    return out


def cause_in_plain_words(cause_code: str, language: str = "en") -> Dict[str, Any]:
    """The customer-facing wording for a cause code, straight from the policy table's
    cause_labels_customer, the only vocabulary the agent may use for causes."""
    from support import mock_backend
    policy = mock_backend.load_policy()
    labels = policy["cause_labels_customer"]
    code = (cause_code or "").strip().upper()
    if code not in labels:
        return {"error": f"Unknown cause_code {cause_code!r}. Valid codes: {', '.join(labels)}."}
    lang = language if language in labels[code] else "en"
    return {
        "cause_code": code,
        "language": lang,
        "plain_words": labels[code][lang],
        "attribution_rule": policy["customer_wording"]["cause_attribution"],
    }


def departures_in_window(origin: str, dest: str, date: str,
                         earliest: str = "00:00", latest: str = "23:59") -> Dict[str, Any]:
    """Scheduled departures on one route and date inside a local-time window, from
    flights.csv. Cancelled and diverted flights are listed apart, never as options."""
    from support import mock_backend
    flights, unavailable = [], []
    for row in mock_backend.load_flights():
        if row["origin"] != origin.upper() or row["dest"] != dest.upper() or row["date"] != date:
            continue
        if not (earliest <= row["sched_dep_local"] <= latest):
            continue
        entry = {"flight_no": row["flight_no"], "departs_local": row["sched_dep_local"],
                 "arrives_local": row["sched_arr_local"], "status": row["status"],
                 "delay_minutes": row["delay_min"],
                 "seats_left_Y": row["seats_left_Y"], "seats_left_J": row["seats_left_J"]}
        (unavailable if row["status"] in ("CANCELLED", "DIVERTED") else flights).append(entry)
    flights.sort(key=lambda e: e["departs_local"])
    return {"origin": origin.upper(), "dest": dest.upper(), "date": date,
            "window_local": f"{earliest}-{latest}", "departures": flights,
            "not_operating": unavailable,
            "note": "Scheduled times; delay_minutes is added to the scheduled time. "
                    "A listing is not a hold or a booking."}


EXTRA_TOOLS: List[Dict[str, Any]] = [    # ✏️ Build 2, step 2.1: schemas for the tools you add
    {
        "name": "departures_in_window",
        "description": (
            "List the Larkspur departures on one route and one date that leave inside a "
            "local-time window, for example 'anything this afternoon' or 'before 9am'. "
            "Use it when the customer names a time of day, not when they want the first "
            "day with a seat (next_available_day) or the ranked rebooking options for "
            "their booking (search_alternatives). Take origin and dest from the booking's "
            "segment and the date from the customer or segment. Returns each flight's "
            "scheduled departure and arrival, status, delay and seats left, with "
            "cancelled or diverted flights listed separately. It holds and books "
            "nothing, and does not say what the customer is owed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "Departure airport IATA code, e.g. DEN."},
                "dest": {"type": "string", "description": "Arrival airport IATA code, e.g. AUS."},
                "date": {"type": "string", "description": "Local date in ISO format YYYY-MM-DD, e.g. 2025-05-08."},
                "earliest": {"type": "string", "description": "Optional. Earliest departure, 24-hour local HH:MM, e.g. 12:00. Defaults to 00:00."},
                "latest": {"type": "string", "description": "Optional. Latest departure, 24-hour local HH:MM, e.g. 18:00. Defaults to 23:59."},
            },
            "required": ["origin", "dest", "date"],
        },
    },
    {
        "name": "cause_in_plain_words",
        "description": (
            "Turn a disruption cause code (WX, ATC, MX, CREW, SEC) into the approved "
            "customer-facing wording, for example WX becomes 'weather'. Call it whenever "
            "you are about to tell the customer why their flight was disrupted, using the "
            "cause_code from get_flight_status, and say the returned plain_words rather "
            "than the code or your own phrasing. Pass the customer's language (en or es) "
            "from lookup_booking. It returns the wording and the attribution rule: name "
            "the cause, never assign blame. It says nothing about what the customer is "
            "owed; that is check_policy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cause_code": {"type": "string", "enum": ["WX", "ATC", "MX", "CREW", "SEC"],
                               "description": "Cause from get_flight_status."},
                "language": {"type": "string", "enum": ["en", "es"],
                             "description": "Customer's language from lookup_booking. Defaults to en."},
            },
            "required": ["cause_code"],
        },
    },
    {
        "name": "care_entitlements",
        "description": (
            "Answer only the care question: what meal, hotel and ground-transport help is "
            "this customer owed while they wait. Use it when the customer asks specifically "
            "about meals, a hotel or transport, after get_flight_status has given you the "
            "cause, delay and status. It reads the same policy as check_policy and returns "
            "just the care part, with the policy_row_id to pass to issue_voucher. It does "
            "not cover rebooking, refunds, goodwill or escalation triggers, so it never "
            "replaces check_policy: when the customer's disruption has not been resolved "
            "yet, call check_policy first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pnr": {"type": "string"},
                "cause_code": {"type": "string", "enum": ["WX", "ATC", "MX", "CREW", "SEC"],
                               "description": "Cause from get_flight_status."},
                "delay_minutes": {"type": "integer", "description": "Delay in minutes from get_flight_status; 0 if cancelled."},
                "status": {"type": "string", "enum": ["ON_TIME", "DELAYED", "CANCELLED", "DIVERTED"],
                           "description": "Status from get_flight_status."},
                "wait_minutes_for_alternative": {"type": "integer",
                                                 "description": "Optional. How long the customer waits for their new flight, if known."},
            },
            "required": ["pnr", "cause_code", "delay_minutes", "status"],
        },
    },
    {
        "name": "reopen_stats",
        "description": (
            "Report how often human-handled disruption chats were reopened by the customer "
            "within 72 hours, and the recorded reasons (for example a confirmation email "
            "that never arrived). Use it only when someone asks how often chats reopen or "
            "why; it says nothing about this customer's booking or entitlements. Needs no "
            "booking details. Optionally narrow it to one intent_label. Returns the chat "
            "count, reopened count, rate, a per-intent breakdown and the reasons. The data "
            "is a small sample of human-agent chats, so say that it is a sample, never "
            "present the rate as a company-wide figure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "intent_label": {
                    "type": "string",
                    "description": "Optional. Only count chats with this intent, e.g. rebook_after_cancellation. Omit for all chats.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "seats_left",
        "description": (
            "Find the remaining free seats on a flight, per cabin (economy Y and "
            "business J). Use it when the customer asks about remaining seats, room "
            "for their party, or seats left, never for booking or delay status. "
            "If the customer gave a PNR, call lookup_booking first and take "
            "flight_number and date from its segment. If they gave no PNR but did "
            "give a flight number and date, call get_flight_status first to confirm "
            "the flight exists. If you have neither a PNR nor a flight number and "
            "date, ask the customer for their PNR before calling anything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "flight_no": {"type": "string", "description": "Flight number, e.g. LK101. From lookup_booking's segment or the customer."},
                "date": {"type": "string", "description": "Local flight date in ISO format YYYY-MM-DD, e.g. 2025-05-07."},
            },
            "required": ["flight_no", "date"],
        },
    },
    {
        "name": "what_automation_will_not_do",
        "description": (
            "Tell the customer what this chat assistant is not allowed to do itself. Use "
            "it when the customer asks the chat to do something that may be outside its "
            "remit, for example cancel the whole trip, refund the ticket, collect a fare "
            "difference, approve a hotel, rebook on another airline, handle a large group "
            "or an unaccompanied minor, or trace a bag. Needs no booking details. Returns "
            "Handbook section 7: the list of things only people handle, and what the "
            "assistant does instead (explain, hold a seat for 15 minutes, hand over with a "
            "summary). It does not decide what the customer is owed (check_policy), explain "
            "why a fare rule applies (fare_rules) or hand the case over (escalate_to_human); "
            "use those after it."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "ground_stop_status",
        "description": (
            "Check whether an airport has a ground stop (flights grounded or held) "
            "recorded in flights.csv, and if the record states times, whether the stop "
            "is before, inside or past its window at a given moment. Use it only when "
            "the customer asks whether an airport is stopped or grounded, or until "
            "when, including 'until 15:50?' style questions; never for one flight's "
            "delay status or for seats. Times come only from the record: if none are "
            "stated the tool says so, and you must not guess an end time. Pass "
            "query_time when the customer names a date or time; leave it out to use "
            "the current moment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "airport": {"type": "string", "description": "Three-letter airport code, e.g. DEN. Convert a city name like Denver to its code."},
                "query_time": {"type": "string", "description": "Optional. The moment to check, in the airport's local time, as 'YYYY-MM-DD HH:MM', e.g. '2025-05-08 15:30'. Omit to use now."},
            },
            "required": ["airport"],
        },
    },
    {
        "name": "hold_time_left",
        "description": (
            "Say how long a seat hold made by hold_seat is still valid before the seat "
            "is released: minutes and seconds left, or that it has expired. Use it only "
            "when the customer asks how long they have, or when a held seat expires. "
            "Call lookup_booking first, with the customer's PNR, or with the flight "
            "number and date if they have no PNR, then use the hold_id that hold_seat "
            "returned for that booking. If no hold exists yet, tell the customer and "
            "offer to place one; never create or extend a hold from this tool, and "
            "never promise a seat after the hold has expired."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hold_id": {"type": "string", "description": "The hold_id returned by hold_seat, e.g. HOLD-3FA2C1."},
                "query_time": {"type": "string", "description": "Optional. The moment to check, in Denver local time, as 'YYYY-MM-DD HH:MM'. Omit to use now."},
            },
            "required": ["hold_id"],
        },
    },
]


def hold_time_left(hold_id: str, query_time: str = "") -> Dict[str, Any]:
    """Time left on one seat hold. Read-only: the expiry rule matches
    confirm_rebooking (expired once now is past expires_at)."""
    from datetime import datetime
    from support import mock_backend
    hold = mock_backend._holds.get(hold_id.strip().upper())
    if not hold:
        return {"hold_id": hold_id, "found": False,
                "note": "No hold with this id. Tell the customer and offer to place one; do not place it without their go-ahead."}
    if query_time:
        try:
            now = datetime.strptime(query_time.strip(), "%Y-%m-%d %H:%M").replace(
                tzinfo=mock_backend.FIXTURE_CLOCK.tzinfo)
        except ValueError:
            return {"error": "query_time must be 'YYYY-MM-DD HH:MM' in Denver local time."}
    else:
        now = mock_backend.FIXTURE_CLOCK
    expires = hold["expires_at"]
    out = {"hold_id": hold_id.strip().upper(), "found": True, "pnr": hold["pnr"],
           "option_id": hold["option_id"], "expires_at": expires.strftime("%Y-%m-%d %H:%M")}
    if now > expires:
        out.update(state="expired", note="The hold has expired; the seat may no longer be available.")
    else:
        secs = int((expires - now).total_seconds())
        out.update(state="active", minutes_left=secs // 60, seconds_left=secs % 60)
    return out


def ground_stop_status(airport: str, query_time: str = "") -> Dict[str, Any]:
    """Ground-stop records for one airport from flights.csv remarks. Times only
    when the remark states them; compared against query_time or the fixture clock."""
    import re
    from datetime import datetime
    from support import mock_backend
    ap = airport.strip().upper()
    if query_time:
        try:
            now = datetime.strptime(query_time.strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            return {"error": "query_time must be 'YYYY-MM-DD HH:MM' in airport local time."}
    else:
        now = mock_backend.FIXTURE_CLOCK.replace(tzinfo=None)
    stop = re.compile(rf"\b{re.escape(ap)}\s+(?:GDP then\s+)?ground stop", re.I)
    win = re.compile(r"ground stop (\d{2}:\d{2})-(\d{2}:\d{2})(?: ([A-Z]{3}))?")
    windows, flights = {}, []
    for row in mock_backend.load_flights():
        remark = row.get("remarks") or ""
        if not stop.search(remark):
            continue
        flights.append(row["flight_no"])
        m = win.search(remark)
        if m:
            windows[(row["date"], m.group(1), m.group(2), m.group(3) or "local")] = True
    if not flights:
        return {"airport": ap, "ground_stop_recorded": False,
                "note": f"flights.csv has no ground-stop remark for {ap}. No timings are stated."}
    out = {"airport": ap, "ground_stop_recorded": True, "flights_with_remark": flights,
           "checked_at": now.strftime("%Y-%m-%d %H:%M")}
    if not windows:
        out["note"] = "A ground stop is recorded but flights.csv states no times. Do not give an end time."
        return out
    results = []
    for (date, start, end, tz) in windows:
        s = datetime.strptime(f"{date} {start}", "%Y-%m-%d %H:%M")
        e = datetime.strptime(f"{date} {end}", "%Y-%m-%d %H:%M")
        state = "not_started" if now < s else ("active" if now <= e else "ended")
        results.append({"date": date, "start": start, "end": end, "timezone": tz, "state_at_checked_time": state})
    out["windows"] = results
    return out


def seats_left(flight_no: str, date: str) -> Dict[str, Any]:
    """Free seats per cabin for one flight, read from flights.csv."""
    from support import mock_backend
    for row in mock_backend.load_flights():
        if row["flight_no"] == flight_no and row["date"] == date:
            return {"flight_no": flight_no, "date": date,
                    "seats_left_Y": row["seats_left_Y"], "seats_left_J": row["seats_left_J"]}
    return {"error": f"No flight {flight_no} on {date} in flights.csv. Ask the customer to confirm the flight number and date, or give their PNR."}


LOCAL_TOOLS: Dict[str, Any] = {          # ✏️ Build 2, step 2.1: the functions behind them
    "seats_left": seats_left,
    "ground_stop_status": ground_stop_status,
    "hold_time_left": hold_time_left,
    "departures_in_window": departures_in_window,
    "cause_in_plain_words": cause_in_plain_words,
    "care_entitlements": care_entitlements,
    "reopen_stats": reopen_stats,
    "what_automation_will_not_do": what_automation_will_not_do,
}

def text_of(response) -> str:
    """Given. The last non-empty text block, never content[0]."""
    texts = [b.text for b in response.content if getattr(b, "type", None) == "text" and b.text]
    return texts[-1] if texts else ""


def tool_results(response) -> List[Dict[str, Any]]:
    """Given. Runs every tool_use block and packages the results the way the
    API expects them back. A tool can live in three places: the MCP server,
    LOCAL_TOOLS, or support/tools.py."""
    # three branches, no try/except in this file: mcp_client.call_remote() and
    # support.call_local() answer with an error dict instead of raising, and both
    # record what came back on the trace
    results = []
    for block in response.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        if block.name in mcp_client.tool_names:
            output = mcp_client.call_remote(block.name, block.input)
        elif block.name in LOCAL_TOOLS:
            output = call_local(LOCAL_TOOLS[block.name], block.name, block.input)
        else:
            output = execute_tool(block.name, block.input)
        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": str(output),
        })
    return results


def run_agent(pnr: str, last_name: str, message: str) -> str:            # ✏️ Build 1, step 1.2
    """Run the tool loop until Claude stops asking for tools. Return its final text."""
    client, tracer = new_session()
    tools = tool_list()
    messages = [
        {"role": "user", "content": f"PNR {pnr}, last name {last_name}. {message}"},
    ]

    response = client.messages.create(
        model=MODEL, max_tokens=4096,system=[
            {
                "type" : "text",
                "text" : runtime_preamble() + SYSTEM_PROMPT + TONE_ADDENDUM,
                "cache_control" : {"type": "ephemeral"}
            }],
        thinking={"type": "adaptive"}, tools=tools, messages=messages,
    )

    turns = 1
    while response.stop_reason == "tool_use" and turns < MAX_TOOL_CALLS:
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results(response)})
        response = client.messages.create(
            model=MODEL, max_tokens=4096,system=[
            {
                "type" : "text",
                "text" : runtime_preamble() + SYSTEM_PROMPT + TONE_ADDENDUM,
                "cache_control" : {"type": "ephemeral"}
            }],
            thinking={"type": "adaptive"}, tools=tools, messages=messages,
        )
        turns += 1

    return text_of(response)


def tool_list() -> List[Dict[str, Any]]:                   # ✏️ Build 2, step 2.2
    """Given. Exactly what Claude is offered on every turn; run.py --show-tools
    prints this list."""
    ltools =build_tools() + EXTRA_TOOLS + mcp_client.tools()
    if ltools:
        ltools[-1] = {**ltools[-1], "cache_control": {"type": "ephemeral"}}  # last tool is ephemeral
    return ltools


# ──────────────────────────────────────────────────────────────────────────────
# Below this line: what Claude is told about each tool. Step 1.3.
# The functions these describe are written and correct, in support/tools.py.
# ──────────────────────────────────────────────────────────────────────────────
def build_tools() -> List[Dict[str, Any]]:                 # ✏️ Build 1, step 1.3
    """Anthropic-shaped schemas: name, description, input_schema. What Claude is
    told about each of the nine tools, and all it is ever told."""
    return [
        {
            "name": "lookup_booking",
            "description": (
                "Retrieve a Larkspur reservation from Altura by confirmation code (PNR) "
                "and the passenger's last name. Both are required to prevent a lookup on "
                "a guessed PNR. Returns fare family, loyalty tier, the segment that needs "
                "attention, and any group/partner/minor/SSR flags relevant to scope."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"pnr": {"type": "string"}, "last_name": {"type": "string"}},
                "required": ["pnr", "last_name"],
            },
        },
        {
            "name": "get_flight_status",
            "description": (
                "Look up a Larkspur or Larkspur Link flight's current OpsFeed status for "
                "one local date: status, delay minutes, and cause. Use this before telling "
                "a customer anything about a flight's timing; never state it from memory."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "flight_no": {"type": "string"},
                    "date": {"type": "string", "description": "Local flight date in ISO format YYYY-MM-DD, e.g. 2025-05-08. Copy it from the booking's segment date."},
                },
                "required": ["flight_no", "date"],
            },
        },
        {
            "name": "search_alternatives",
            "description": (
                "Find ranked rebooking options for the disrupted segment on a booking. "
                "Call it after lookup_booking and check_policy confirm the customer is "
                "owed a rebooking, and before offering or holding any flight; never "
                "invent flights. Needs only the pnr. Returns options, each with an "
                "option_id (pass it to hold_seat), flights, date, departure/arrival "
                "times, delay, seats available and fare difference."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"pnr": {"type": "string"}},
                "required": ["pnr"],
            },
        },
        {
            "name": "check_policy",
            "description": (
                "Resolve what Larkspur owes this customer for the disruption: rebooking "
                "waiver, refund path, meal/hotel/ground care, goodwill eligibility and cap, "
                "and any escalation triggers. cause_code, delay_minutes and status describe "
                "what get_flight_status told you; fare_family, loyalty_tier and whether this "
                "is overnight are looked up from the booking, not asked of you. Every "
                "response carries a policy_row_id. Cite it if you reference this decision "
                "again."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pnr": {"type": "string"},
                    "cause_code": {"type": "string", "enum": ["WX", "ATC", "MX", "CREW", "SEC"]},
                    "delay_minutes": {"type": "integer"},
                    "status": {"type": "string", "enum": ["ON_TIME", "DELAYED", "CANCELLED", "DIVERTED"]},
                    "wait_minutes_for_alternative": {"type": "integer"},
                    "chosen_option_id": {"type": "string"},
                },
                "required": ["pnr", "cause_code", "delay_minutes", "status"],
            },
        },
        {
            "name": "hold_seat",
            "description": "Place a 15-minute hold on one alternative. Reversible. It simply expires.",
            "input_schema": {
                "type": "object",
                "properties": {"option_id": {"type": "string"}, "pnr": {"type": "string"}},
                "required": ["option_id", "pnr"],
            },
        },
        {
            "name": "confirm_rebooking",
            "description": (
                "Finalize a held seat. Irreversible. Requires a confirmation_token that "
                "only the customer's own Confirm-click can produce. You cannot supply it "
                "yourself, and 'the customer said yes' in chat does not substitute for it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"hold_id": {"type": "string"}, "confirmation_token": {"type": "string"}},
                "required": ["hold_id", "confirmation_token"],
            },
        },
        {
            "name": "issue_voucher",
            "description": (
                "Issue a meal, ground, hotel, or goodwill voucher. Auto-approves within the "
                "policy's threshold for that type; above it, returns a pending status for a "
                "human. It does not fail. Always pass the policy_row_id that made it eligible."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "voucher_type": {"type": "string", "enum": ["meal", "ground", "hotel", "goodwill"]},
                    "amount_usd": {"type": "number"},
                    "pnr": {"type": "string"},
                    "policy_row_id": {"type": "string"},
                },
                "required": ["voucher_type", "amount_usd", "pnr", "policy_row_id"],
            },
        },
        {
            "name": "escalate_to_human",
            "description": (
                "Hand this conversation to a human, with your reasoning attached. Use for "
                "groups, partner segments, unaccompanied minors, refunds, or anything else "
                "out of scope. This is the correct outcome for those cases, not a failure."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pnr": {"type": "string"}, "reason": {"type": "string"},
                    "summary_for_human": {"type": "string"}, "queue": {"type": "string"},
                },
                "required": ["pnr", "reason", "summary_for_human"],
            },
        },
        {
            "name": "send_confirmation",
            "description": "Send the customer a written confirmation of what was just done. Benign.",
            "input_schema": {
                "type": "object",
                "properties": {"pnr": {"type": "string"}, "message": {"type": "string"}},
                "required": ["pnr", "message"],
            },
        },
    ]
