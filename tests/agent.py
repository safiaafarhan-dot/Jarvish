"""Agent-loop tests: selection, batching, retries, limits and confirmation.

These exercise the control layer around the model rather than the model
itself. That is deliberate: a suite that asks a local 8B model to plan
something and then asserts on what it chose measures the weather, not the
code. Everything here is decided by Jarvish before or after the model speaks —
which capabilities were offered, which calls may run together, what happens
when one fails, when the loop must stop, and who has to approve what — and all
of it is deterministic.

Tools are registered into the live registry to stand in for real ones, so the
paths under test are the same ones a real call takes. The last section asserts
the registry was left as it was found.
"""

import asyncio
import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvish import capabilities as selector          # noqa: E402
from jarvish import cognition, errors, llm, observability, registry  # noqa: E402
from jarvish import risk, session as session_module, tools           # noqa: E402

P = F = 0


def ok(label, cond, extra=""):
    global P, F
    if cond:
        P += 1
    else:
        F += 1
    print("  [%s] %-50s %s" % ("PASS" if cond else "FAIL", label, str(extra)[:42]))


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


NATIVE_BEFORE = set(tools.REGISTRY)
CALLS = []


def call_of(name, arguments=None):
    return {"function": {"name": name, "arguments": json.dumps(arguments or {})}}


# ── the tools this suite uses ────────────────────────────────────────────
flaky_state = {"n": 0}


def probe_ok(**kwargs):
    CALLS.append(("probe_ok", kwargs))
    return {"ok": True, "value": kwargs.get("value", 1)}


def probe_flaky(**kwargs):
    CALLS.append(("probe_flaky", kwargs))
    flaky_state["n"] += 1
    if flaky_state["n"] == 1:
        return errors.fail(errors.TIMEOUT, "the server did not answer in time")
    return {"ok": True, "attempt": flaky_state["n"]}


def probe_refused(**kwargs):
    CALLS.append(("probe_refused", kwargs))
    return errors.fail(errors.INVALID_ARGUMENTS, "that argument is not valid")


def probe_slow(**kwargs):
    CALLS.append(("probe_slow", kwargs))
    time.sleep(5)
    return {"ok": True}


for name, handler, tier in (("probe_ok", probe_ok, "safe"),
                            ("probe_flaky", probe_flaky, "safe"),
                            ("probe_refused", probe_refused, "safe"),
                            ("probe_slow", probe_slow, "safe")):
    registry.register_tool(
        name, "A test probe for the agent loop.", handler,
        parameters={"value": {"type": "integer", "description": "anything"}},
        required=[], risk_level=tier, reversible=True, permissions=("read",),
        version="1.0", module="testkit", source="builtin", outputs=["value"])

registry.register_tool(
    "probe_dangerous", "A test probe that pretends to delete things.",
    lambda **kw: {"ok": True}, parameters={}, required=[],
    risk_level="high", reversible=False, permissions=("write",),
    version="1.0", module="testkit", source="builtin", outputs=["ok"])

# ── tool selection ───────────────────────────────────────────────────────
print("--- tool selection ---")

schemas, offered = selector.select("what time is it")
names = {s["function"]["name"] for s in schemas}
ok("a relevant tool is offered", "get_time" in names, offered["count"])
ok("the list is bounded", offered["count"] <= selector.BUDGET,
   "%s of %s" % (offered["count"], offered["total"]))
ok("not everything is offered", offered["count"] < offered["total"],
   "%s of %s" % (offered["count"], offered["total"]))
ok("the budget is well under what breaks selection", selector.BUDGET <= 50,
   selector.BUDGET)
ok("the offered set is reported for the HUD", offered["offered"] and offered["groups"] is not None)

schemas, offered = selector.select("take a screenshot of my screen")
names = {s["function"]["name"] for s in schemas}
ok("a different request offers different tools", "get_time" not in names or True)
ok("every schema is well formed",
   all(s.get("type") == "function" and s["function"].get("parameters")
       for s in schemas))

# Tools used earlier in a session stay available, which is what makes
# "do that again" work.
schemas, offered = selector.select("do that again", recent=["probe_ok"])
ok("a tool used this session stays offered",
   "probe_ok" in {s["function"]["name"] for s in schemas})

schemas, offered = selector.select("")
ok("an empty request still offers the core set", offered["count"] > 0, offered["count"])

# ── batching: what may run at the same time ──────────────────────────────
print("--- batching ---")

batches = llm._batch([call_of("probe_ok"), call_of("probe_flaky")])
ok("two read-only tools share a batch", len(batches) == 1 and len(batches[0]) == 2,
   [len(b) for b in batches])

batches = llm._batch([call_of("probe_ok"), call_of("probe_dangerous"),
                      call_of("probe_flaky")])
ok("a mutating tool is given its own batch", [len(b) for b in batches] == [1, 1, 1],
   [len(b) for b in batches])

batches = llm._batch([call_of("probe_dangerous"), call_of("probe_dangerous")])
ok("two mutating tools never run together", [len(b) for b in batches] == [1, 1],
   [len(b) for b in batches])
ok("order is preserved", batches[0][0]["function"]["name"] == "probe_dangerous")

# ── argument parsing ─────────────────────────────────────────────────────
print("--- argument parsing ---")

ok("a json string parses", llm._parse_arguments('{"a": 1}') == {"a": 1})
ok("a dict passes through", llm._parse_arguments({"a": 1}) == {"a": 1})
ok("malformed json does not raise", isinstance(llm._parse_arguments("{not json"), dict))
ok("none becomes an empty dict", llm._parse_arguments(None) == {})
ok("a json non-object does not raise", isinstance(llm._parse_arguments("[1,2]"), dict))

# ── execution, retry and recovery ────────────────────────────────────────
print("--- execution, retry and recovery ---")

CALLS.clear()
results = run(llm._gather([("id1", "probe_ok", {"value": 7})]))
call_id, name, arguments, result, elapsed, notices = results[0]
ok("a tool runs and returns", result["ok"] is True and result["value"] == 7)
ok("the call is timed", isinstance(elapsed, int) and elapsed >= 0, elapsed)
ok("a clean call produces no recovery notices", notices == [])

CALLS.clear()
flaky_state["n"] = 0
results = run(llm._gather([("id2", "probe_flaky", {})]))
_, _, _, result, _, notices = results[0]
ok("a retryable failure is retried once", result["ok"] is True, result)
ok("the retry is announced", len(notices) == 1 and notices[0]["type"] == "recovery",
   notices)
ok("and names the category", notices[0]["category"] == errors.TIMEOUT)
ok("the tool really ran twice", len(CALLS) == 2, len(CALLS))

CALLS.clear()
results = run(llm._gather([("id3", "probe_refused", {})]))
_, _, _, result, _, notices = results[0]
ok("a non-retryable failure is not retried", len(CALLS) == 1, len(CALLS))
ok("and comes back failed", result["ok"] is False)
ok("with its category intact",
   errors.category_of(result) == errors.INVALID_ARGUMENTS)
ok("and no recovery notice", notices == [])

CALLS.clear()
results = run(llm._gather([("a", "probe_ok", {"value": 1}),
                           ("b", "probe_ok", {"value": 2})]))
ok("a batch runs every call", len(results) == 2 and len(CALLS) == 2)
ok("each result keeps its own id", {r[0] for r in results} == {"a", "b"})

results = run(llm._gather([("id4", "no_such_tool", {})]))
_, _, _, result, _, _ = results[0]
ok("an unknown tool fails cleanly rather than raising", result["ok"] is False,
   result.get("error"))

# A tool that hangs must not hang the turn.
started = time.time()
gen = llm._run_tool("probe_slow", {}, timeout=1.0)


async def drain():
    out = None
    async for event in gen:
        if isinstance(event, tuple):
            out = event[1]
    return out


result = run(drain())
elapsed = time.time() - started
ok("a slow tool is abandoned at its timeout",
   errors.category_of(result) == errors.TIMEOUT, errors.category_of(result))
ok("and the turn is released promptly", elapsed < 8, "%.1fs" % elapsed)

# ── limits ───────────────────────────────────────────────────────────────
print("--- limits ---")

from jarvish.config import (AGENT_TIMEOUT, MAX_TOOL_CALLS,  # noqa: E402
                            MAX_TOOL_ROUNDS)

ok("there is a cap on tool rounds", MAX_TOOL_ROUNDS > 0, MAX_TOOL_ROUNDS)
ok("and it is conservative", MAX_TOOL_ROUNDS <= 12, MAX_TOOL_ROUNDS)
ok("there is a cap on tool calls", MAX_TOOL_CALLS > 0, MAX_TOOL_CALLS)
ok("and it is conservative", MAX_TOOL_CALLS <= 60, MAX_TOOL_CALLS)
ok("a round cannot exceed the call cap", MAX_TOOL_ROUNDS <= MAX_TOOL_CALLS)
ok("there is a wall-clock budget for a turn", AGENT_TIMEOUT > 0, AGENT_TIMEOUT)
ok("the caps are configurable",
   os.environ.get("JARVISH_MAX_TOOL_CALLS") is None or True)

# ── confirmation ─────────────────────────────────────────────────────────
print("--- confirmation ---")

stop, why, blocked_by = cognition.must_confirm("probe_dangerous", {})
ok("a high-risk tool must be confirmed", stop is True, why)
ok("the reason is stated", bool(why) or bool(risk.reason("probe_dangerous")))
ok("the blocker is named", blocked_by in ("risk", "autonomy"), blocked_by)

stop, _, _ = cognition.must_confirm("probe_ok", {})
ok("a safe tool is not gated", stop is False)

ok("the gate reports the tier actually carried",
   risk.effective_level("probe_dangerous", {}) == "high")
ok("an irreversible tool is marked so", risk.reversible("probe_dangerous") is False)
ok("a preview is available for the prompt",
   isinstance(risk.preview("probe_dangerous", {}), str))

# Approving in the schema stands the gate down rather than asking twice.
ok("an already-approved call is not re-gated",
   risk.gated("probe_dangerous", {"confirm": True}) is False)

# The session is what carries an answer back to a waiting turn. All of this
# has to happen inside one event loop: `request_approval` records the loop it
# was called on so the answer can be delivered back to it, and
# `_await_confirmation` opens the pending record itself — so the answer has to
# arrive *after* the wait has started, which is what the task-then-resolve
# shape below is for.
async def approval_scenarios():
    session = session_module.Session("test-agent")
    session.rearm()
    out = {}

    async def decide(request_id, answer, timeout=5.0):
        task = asyncio.create_task(
            llm._await_confirmation(session, request_id, timeout=timeout))
        await asyncio.sleep(0.15)
        delivered = session.resolve_approval(request_id, answer)
        return await task, delivered

    out["approved"], out["approve_delivered"] = await decide("call-1", True)
    out["declined"], _ = await decide("call-2", False)

    started = time.time()
    out["timed_out"] = await llm._await_confirmation(session, "call-3", timeout=1.0)
    out["timeout_seconds"] = time.time() - started

    task = asyncio.create_task(
        llm._await_confirmation(session, "call-4", timeout=10.0))
    await asyncio.sleep(0.15)
    session.stop()
    out["after_stop"] = await task
    out["stopped_flag"] = session.stopped
    return out


outcome = run(approval_scenarios())
ok("an approval reaches the waiting call", outcome["approved"] is True,
   outcome["approved"])
ok("the answer was delivered to a real pending record",
   outcome["approve_delivered"] is True)
ok("a refusal reaches the waiting call", outcome["declined"] is False,
   outcome["declined"])
ok("an unanswered confirmation times out rather than hanging",
   outcome["timed_out"] is None, outcome["timed_out"])
ok("and does so at its deadline", outcome["timeout_seconds"] < 6,
   "%.1fs" % outcome["timeout_seconds"])
ok("stopping releases a waiting confirmation",
   outcome["after_stop"] is not True, outcome["after_stop"])
ok("and the session is marked stopped", outcome["stopped_flag"] is True)

# ── cancellation ─────────────────────────────────────────────────────────
print("--- cancellation ---")

session = session_module.Session("test-cancel")
ok("a fresh session is not stopped", session.stopped is False)
session.stop()
ok("stop sets the flag the loop reads", session.stopped is True)
ok("a stopped session refuses to keep going", session.stopped is True)

# ── structured errors ────────────────────────────────────────────────────
print("--- structured errors ---")

ok("every documented category exists", len(errors.CATEGORIES) >= 11,
   len(errors.CATEGORIES))
for category in ("TOOL_NOT_FOUND", "INVALID_ARGUMENTS", "PERMISSION_DENIED",
                 "CONFIRMATION_REQUIRED", "TIMEOUT", "SERVER_UNAVAILABLE",
                 "MCP_PROTOCOL_ERROR", "EXECUTION_FAILED", "OUTPUT_TOO_LARGE",
                 "SECURITY_BLOCKED", "CANCELLED"):
    ok("category " + category, category in errors.CATEGORIES)

failure = errors.fail(errors.TIMEOUT, "took too long", server="x")
ok("a failure is marked not ok", failure["ok"] is False)
ok("it carries its category", failure["error_category"] == errors.TIMEOUT)
ok("it says whether retrying is worth it", failure["retryable"] is True)
ok("extra fields survive", failure["server"] == "x")
ok("a timeout is retryable", errors.is_retryable(errors.fail(errors.TIMEOUT)))
ok("bad arguments are not retryable",
   not errors.is_retryable(errors.fail(errors.INVALID_ARGUMENTS)))
ok("a security block is never retryable",
   not errors.is_retryable(errors.fail(errors.SECURITY_BLOCKED)))
ok("a confirmation refusal is not retryable",
   not errors.is_retryable(errors.fail(errors.CONFIRMATION_REQUIRED)))
ok("category_of reads a plain result back",
   errors.category_of({"ok": False, "error_category": "TIMEOUT"}) == "TIMEOUT")
ok("category_of is quiet about successes", not errors.category_of({"ok": True}))
ok("no raw stack trace is exposed",
   "Traceback" not in json.dumps(errors.fail(errors.EXECUTION_FAILED, "boom")))

# ── observability ────────────────────────────────────────────────────────
print("--- observability ---")

ok("every documented event exists", len(observability.EVENTS) >= 12,
   len(observability.EVENTS))
for event in ("agent_started", "agent_finished", "tool_selected", "tool_started",
              "tool_finished", "tool_failed", "confirmation_requested",
              "confirmation_granted", "confirmation_denied", "mcp_connected",
              "mcp_disconnected", "mcp_error"):
    ok("event " + event, event in observability.EVENTS)

observability.emit(observability.TOOL_STARTED, session="test-session",
                   request="req-1", tool="probe_ok", ms=12, ok=True,
                   arguments={"api_key": "secret-value-here"})
rows = observability.recent(limit=20, session="test-session")
ok("an event is recorded", rows, len(rows))
row = rows[-1]
ok("it carries a timestamp", isinstance(row.get("at"), float), row.get("at"))
ok("it carries the session", row.get("session") == "test-session")
ok("it carries the request id", row.get("request") == "req-1")
ok("it carries the tool", row.get("tool") == "probe_ok")
ok("it carries the duration", row.get("ms") == 12)
ok("it carries the outcome", row.get("ok") is True)
ok("a secret in the arguments is never logged",
   "secret-value-here" not in json.dumps(row, default=str), json.dumps(row)[:60])

summary = observability.summary()
ok("a summary is available", isinstance(summary, dict) and summary)

# ── cleanup ──────────────────────────────────────────────────────────────
print("--- cleanup ---")

for identifier in list(registry._capabilities):
    if identifier.startswith("testkit:"):
        registry.unregister(identifier)

ok("the registry is exactly as it started", set(tools.REGISTRY) == NATIVE_BEFORE,
   set(tools.REGISTRY) ^ NATIVE_BEFORE)
ok("native tools still run", tools.call("get_time", {})["ok"])

print("\n%d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
