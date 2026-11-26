# PITCH.md

Six lines and a lever. Your words. The last two are scored.

Built: A Larkspur disruption-care chat agent with 19 tools (9 given, 8 built by the team, 2 over MCP) that looks up the booking and flight, applies the policy table and hands people the cases it must not handle.
Does: Tells a disrupted customer what happened and what Larkspur owes them, holds a seat for 15 minutes, and escalates refunds, groups and threats to a person.
Number: $0.1096 model cost per contact (31,696 tokens in, 968 out), 5 Stage 1 ticket types x 3 runs each.
Safety check: 8 eval cases, 5 of them hard gates; nothing is rebooked without the customer's own Confirm click, and vouchers are checked against the policy row.
Next: Cut cost per contact by caching the system prompt and the 19 tool schemas that are re-sent on every turn, then re-measure against this baseline.
Still broken: On a legal threat (R8KD3F) the agent never escalates; it gives a normal entitlements rundown, so the tone_safety hard gate blocks the release.
Lever: cost

## Priya asked

Costs:
Wrong:
Runs it:
Left out:
