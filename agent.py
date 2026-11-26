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

TONE_ADDENDUM = ""                       # ✏️ Build 4, step 4.1, intelligence goal

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


EXTRA_TOOLS: List[Dict[str, Any]] = [    # ✏️ Build 2, step 2.1: schemas for the tools you add
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
]


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
    "care_entitlements": care_entitlements,
    "reopen_stats": reopen_stats,
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
        model=MODEL, max_tokens=4096, system=runtime_preamble() + SYSTEM_PROMPT + TONE_ADDENDUM,
        thinking={"type": "adaptive"}, tools=tools, messages=messages,
    )

    turns = 1
    while response.stop_reason == "tool_use" and turns < MAX_TOOL_CALLS:
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results(response)})
        response = client.messages.create(
            model=MODEL, max_tokens=4096, system=runtime_preamble() + SYSTEM_PROMPT + TONE_ADDENDUM,
            thinking={"type": "adaptive"}, tools=tools, messages=messages,
        )
        turns += 1

    return text_of(response)


def tool_list() -> List[Dict[str, Any]]:                   # ✏️ Build 2, step 2.2
    """Given. Exactly what Claude is offered on every turn; run.py --show-tools
    prints this list."""
    return build_tools() + EXTRA_TOOLS + mcp_client.tools()


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
