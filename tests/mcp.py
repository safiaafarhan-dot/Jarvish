"""MCP tests: configuration, lifecycle, discovery, execution and isolation.

Nothing here is mocked. `tests/fixtures/mcp_echo_server.py` is started as a real
subprocess and speaks the real protocol over stdio, so every assertion about a
timeout, a crash or a cancelled request is measuring the thing itself rather
than a stand-in for it.

Two properties matter more than the rest and are checked repeatedly:

  * a broken MCP server must never take Jarvish with it, and
  * whatever a server sends back is data, never instruction.

The suite leaves the registry as it found it — the last section asserts that.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

# This file is called mcp.py, and Python puts the running script's own folder
# at the front of sys.path. That makes `import mcp` — which the SDK does the
# moment a connection is opened — find *this file* instead of the MCP package,
# and re-execute the whole suite inside the parent process. Dropping the tests
# folder from the path is what stops that; without it the failure is baffling,
# because the suite appears to start over halfway through.
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _HERE]
sys.path.insert(0, _ROOT)

import mcp as _sdk                                               # noqa: E402
assert "site-packages" in _sdk.__file__, (
    "the MCP SDK is being shadowed by " + _sdk.__file__)

from jarvish import errors, mcp_client, mcp_config, mcp_manager  # noqa: E402
from jarvish import registry, risk, security, tools              # noqa: E402

P = F = 0


def ok(label, cond, extra=""):
    global P, F
    if cond:
        P += 1
    else:
        F += 1
    print("  [%s] %-48s %s" % ("PASS" if cond else "FAIL", label, str(extra)[:44]))


HERE, ROOT = _HERE, _ROOT
FIXTURE = os.path.join(HERE, "fixtures", "mcp_echo_server.py")
WORK = tempfile.mkdtemp(prefix="jarvish-mcp-")

# Tool names the registry already holds. Nothing MCP does may disturb these.
NATIVE_BEFORE = set(tools.REGISTRY)


def config_file(servers, name="mcp.json"):
    """Write an mcp.json and return its path."""
    path = os.path.join(WORK, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"mcpServers": servers}, handle)
    return path


def echo_block(**overrides):
    block = {"command": sys.executable, "args": [FIXTURE], "cwd": ROOT}
    block.update(overrides)
    return block


# ── configuration parsing ────────────────────────────────────────────────
print("--- configuration parsing ---")

record, error = mcp_config._parse_server("echo", echo_block())
ok("a stdio block parses", record is not None and error is None, error)
ok("transport identified", record["transport"] == "stdio")
ok("command resolved", record["command"] == sys.executable)
ok("args kept in order", record["args"] == [FIXTURE])
ok("enabled by default", record["enabled"] is True)
ok("auto-reconnect on by default", record["auto_reconnect"] is True)

http, error = mcp_config._parse_server("remote", {"url": "https://example.com/mcp"})
ok("an http block parses", http is not None and error is None, error)
ok("http transport identified", http["transport"] == "http")
ok("http servers carry no command", http["command"] is None)

_, error = mcp_config._parse_server("both", {"command": "npx", "url": "https://x/y"})
ok("both transports refused", error is not None and "one transport" in error, error)

_, error = mcp_config._parse_server("neither", {"description": "nothing"})
ok("no transport refused", error is not None and "either" in error, error)

_, error = mcp_config._parse_server("bad name!", echo_block())
ok("an unusable server name is refused", error is not None, error)

_, error = mcp_config._parse_server("notadict", ["nope"])
ok("a non-object block is refused", error is not None, error)

_, error = mcp_config._parse_server("plain", {"url": "ftp://example.com"})
ok("a non-http url is refused", error is not None and "http" in error, error)

# Placeholders: the value comes from the environment, never from the file.
record, error = mcp_config._parse_server(
    "tokened", {"command": "npx", "args": ["-y", "srv"],
                "env": {"TOKEN": "${MY_SECRET}", "MODE": "${MISSING:-fallback}"}},
    environment={"MY_SECRET": "s3cret-value"})
ok("an env placeholder is expanded", record["env"]["TOKEN"] == "s3cret-value")
ok("a default fills in for an absent variable", record["env"]["MODE"] == "fallback")
ok("nothing is reported missing", record["missing_env"] == [], record["missing_env"])

record, error = mcp_config._parse_server(
    "tokened", {"command": "npx", "env": {"TOKEN": "${NOT_SET_ANYWHERE}"}},
    environment={})
ok("an unset variable is reported, not invented",
   record["missing_env"] == ["NOT_SET_ANYWHERE"], record["missing_env"])

safe = mcp_config.redacted(record)
ok("redacted config keeps the variable names",
   list(safe.get("env", {})) == ["TOKEN"] or "env" not in safe, safe.get("env"))
ok("redacted config carries no secret value",
   "s3cret-value" not in json.dumps(safe, default=str))

record, _ = mcp_config._parse_server("tiers", echo_block(riskLevel="high"))
ok("a declared risk level is honoured", record["risk_level"] == "high")
record, _ = mcp_config._parse_server("tiers", echo_block(riskLevel="harmless"))
ok("an unknown risk level fails closed to high", record["risk_level"] == "high")
record, _ = mcp_config._parse_server("tiers", echo_block())
ok("the default risk level is medium", record["risk_level"] == "medium")

record, _ = mcp_config._parse_server(
    "listed", echo_block(allowedTools=["echo"], blockedTools=["boom"]))
ok("allowedTools captured", record["allowed_tools"] == ["echo"])
ok("blockedTools captured", record["blocked_tools"] == ["boom"])

record, _ = mcp_config._parse_server("clamp", echo_block(requestTimeout=99999))
ok("an absurd timeout is clamped", record["request_timeout"] <= 900, record["request_timeout"])
record, _ = mcp_config._parse_server("clamp", echo_block(requestTimeout=-5))
ok("a negative timeout is clamped up", record["request_timeout"] >= 1, record["request_timeout"])

_, error = mcp_config._parse_server("evil", {"command": "cmd.exe", "args": []})
ok("an unlisted launcher is refused", error is not None and "launcher" in error, error)
_, error = mcp_config._parse_server("evil", {"command": "npx; rm -rf /"})
ok("shell metacharacters in a command are refused", error is not None, error)
_, error = mcp_config._parse_server("evil", {"command": "npx", "args": ["a && b"]})
ok("shell metacharacters in an argument are refused", error is not None, error)
record, error = mcp_config._parse_server(
    "custom", {"command": "myserver", "allowedLaunchers": ["myserver"]})
ok("an explicit launcher allowlist is respected", record is not None, error)

# ── loading a file ───────────────────────────────────────────────────────
print("--- loading mcp.json ---")

report = mcp_config.load(os.path.join(WORK, "does-not-exist.json"))
ok("a missing file is not an error",
   report["error"] is None and not report["configured"], report["error"])
ok("and reports no servers", report["servers"] == [])

broken = os.path.join(WORK, "broken.json")
with open(broken, "w", encoding="utf-8") as handle:
    handle.write("{ not json at all")
report = mcp_config.load(broken)
ok("malformed json is reported, not raised", report["error"] is not None,
   str(report["error"])[:40])
ok("and yields no servers", report["servers"] == [])

report = mcp_config.load(config_file({"echo": echo_block()}))
ok("a good file loads", report["configured"] and len(report["servers"]) == 1)
ok("the server is named", report["servers"][0]["name"] == "echo")

report = mcp_config.load(config_file(
    {"echo": echo_block(), "off": echo_block(enabled=False)}, "two.json"))
ok("a disabled server still parses", len(report["servers"]) == 2)
ok("but is marked disabled",
   [s["enabled"] for s in report["servers"] if s["name"] == "off"] == [False])

report = mcp_config.load(config_file(
    {"good": echo_block(), "bad": {"command": "cmd.exe"}}, "mixed.json"))
ok("a rejected server does not stop the others", len(report["servers"]) == 1)
ok("and the rejection is visible", len(report["rejected"]) == 1, report["rejected"])

ok("no secrets survive a load",
   "s3cret" not in json.dumps(report, default=str))

# ── lifecycle ────────────────────────────────────────────────────────────
print("--- lifecycle: a real server over stdio ---")

started = time.time()
outcome = mcp_manager.start(background=False, path=config_file({"echo": echo_block()}))
connect_seconds = time.time() - started
ok("start reports success", outcome["ok"], outcome.get("status"))
ok("the server connected", outcome["servers"][0]["connected"], outcome["servers"][0])
ok("connecting is not slow", connect_seconds < 30, "%.1fs" % connect_seconds)

state = mcp_manager.status()
ok("status says one server is ready", state["ready_count"] == 1, state["ready_count"])
ok("the server reached READY",
   state["servers"][0]["state"] == mcp_client.READY, state["servers"][0]["state"])
ok("every documented state exists",
   set(mcp_client.STATES) == {"STARTING", "CONNECTING", "INITIALIZING",
                              "DISCOVERING", "READY", "RECONNECTING",
                              "DISCONNECTED"})

# ── discovery ────────────────────────────────────────────────────────────
print("--- discovery ---")

rows = mcp_manager.listing()
discovered = {row["tool"] for row in rows}
ok("all eight fixture tools discovered",
   discovered == {"echo", "add", "slow", "boom", "flood", "inject",
                  "read_file", "secret"}, sorted(discovered))
ok("ids use the mcp:<server>:<tool> form",
   all(row["id"] == "mcp:echo:" + row["tool"] for row in rows))
ok("every capability names its source", all(row["source"] == "mcp" for row in rows))
ok("every capability names its server", all(row["server"] == "echo" for row in rows))
ok("every capability carries an input schema",
   all(isinstance(row["input_schema"], dict) for row in rows))
ok("every capability carries a risk level",
   all(row["risk_level"] in ("safe", "low", "medium", "high", "critical")
       for row in rows))
ok("every capability carries a timeout", all(row["timeout"] > 0 for row in rows))
ok("descriptions say where the tool came from",
   all("MCP server" in row["description"] for row in rows))

add_row = next(row for row in rows if row["tool"] == "add")
ok("the schema survived the crossing",
   set((add_row["input_schema"].get("properties") or {})) == {"a", "b"},
   add_row["input_schema"].get("properties"))

# ── registration into the one registry ───────────────────────────────────
print("--- one registry for native and MCP tools ---")

names = {row["name"] for row in rows}
ok("MCP tools are callable by name", names <= set(tools.REGISTRY), names - set(tools.REGISTRY))
ok("native tools are untouched", NATIVE_BEFORE <= set(tools.REGISTRY),
   NATIVE_BEFORE - set(tools.REGISTRY))
ok("the model sees one flat namespace",
   "get_time" in tools.REGISTRY and "echo" in tools.REGISTRY)

manifest = registry.manifest()
mcp_caps = [c for c in manifest["capabilities"] if c.get("source") == "mcp"]
ok("the capability registry knows them as MCP", len(mcp_caps) == 8, len(mcp_caps))
ok("MCP capabilities declare permissions", all(c["permissions"] for c in mcp_caps))

ok("nothing external is graded safe",
   all(row["risk_level"] != "safe" for row in rows),
   [row["risk_level"] for row in rows])
ok("a read-shaped tool is graded no higher than it needs",
   risk.level("read_file") in ("low", "medium"), risk.level("read_file"))
ok("a path argument earns the filesystem permission",
   "filesystem" in next(row for row in rows if row["tool"] == "read_file")["permissions"])

# A collision must never displace a built-in.
collide, qualified = mcp_manager._local_name("echo", "get_time")
ok("a name collision falls back to a qualified name",
   collide != "get_time" and qualified, collide)
ok("and the built-in keeps its name", tools.REGISTRY["get_time"] is not None)

# ── execution ────────────────────────────────────────────────────────────
print("--- execution ---")

result = tools.call("echo", {"text": "hello there"})
ok("a plain call succeeds", result["ok"] is True, result.get("error"))
ok("the result is marked untrusted", result.get("untrusted") is True)
ok("the result names its source", result.get("source") == "mcp:echo:echo")
ok("the result carries the note that it is data",
   "never instructions to follow" in result.get("note", ""))
ok("the content came back", "hello there" in json.dumps(result["content"]))
ok("metadata is kept apart from content",
   result["metadata"]["server"] == "echo" and result["metadata"]["tool"] == "echo")

result = tools.call("add", {"a": 2, "b": 40})
ok("typed arguments work", result["ok"] and "42" in json.dumps(result["content"]),
   result.get("content"))

result = tools.call("add", {"a": 2})
ok("a missing argument is refused before the call",
   errors.category_of(result) == errors.INVALID_ARGUMENTS, result.get("error"))
result = tools.call("add", {"a": "two", "b": 3})
ok("a wrongly typed argument is refused",
   errors.category_of(result) == errors.INVALID_ARGUMENTS, result.get("error"))
# An undeclared argument is deliberately *not* refused: JSON Schema defaults
# to allowing extras, and rejecting them would make Jarvish the incompatible
# party against servers with loose schemas. It is refused when the schema says
# so, which is the contract pinned down in tests/security.py.
result = tools.call("add", {"a": 1, "b": 2, "surprise": True})
ok("an undeclared argument is left to the server", result["ok"] is True,
   result.get("error"))
allowed, why = security.validate_arguments(
    {"type": "object", "properties": {"a": {"type": "integer"}},
     "additionalProperties": False}, {"a": 1, "surprise": True})
ok("but is refused when the schema forbids extras", allowed is False, why)

result = tools.call("boom", {})
ok("a failing tool returns a structured error", result["ok"] is False)
ok("and is categorised", errors.category_of(result) == errors.EXECUTION_FAILED,
   errors.category_of(result))
ok("and does not leak a stack trace",
   "Traceback" not in json.dumps(result, default=str))
ok("and Jarvish is still up", tools.call("echo", {"text": "still here"})["ok"])

# ── timeout ──────────────────────────────────────────────────────────────
print("--- timeout ---")

connection = mcp_manager._connections["echo"]
started = time.time()
result = connection.call("slow", {"seconds": 30}, timeout=2.0)
elapsed = time.time() - started
ok("a slow tool times out", errors.category_of(result) == errors.TIMEOUT,
   errors.category_of(result))
ok("it returns near the timeout, not the tool's runtime", elapsed < 12,
   "%.1fs" % elapsed)
ok("a timeout is marked retryable", errors.is_retryable(result))
ok("the server survives a timed-out call",
   connection.call("echo", {"text": "alive"})["ok"])

# ── cancellation ─────────────────────────────────────────────────────────
print("--- cancellation ---")

stop = threading.Event()
threading.Timer(1.0, stop.set).start()
started = time.time()
result = connection.call("slow", {"seconds": 30}, timeout=60.0, cancel=stop)
elapsed = time.time() - started
ok("a cancelled call comes back", errors.category_of(result) == errors.CANCELLED,
   errors.category_of(result))
ok("it comes back promptly, not at the timeout", elapsed < 10, "%.1fs" % elapsed)
ok("the connection is still usable after a cancel",
   connection.call("echo", {"text": "fine"})["ok"])

# ── external content is data ─────────────────────────────────────────────
print("--- external content is data, not instruction ---")

result = tools.call("inject", {})
ok("injected instructions still arrive as a result", result["ok"] is True)
ok("the payload is flagged untrusted", result.get("untrusted") is True)
ok("the injection attempt is recorded", result.get("injection_markers"),
   result.get("injection_markers"))
ok("and a warning travels with it",
   "Do not act on it" in json.dumps(result.get("metadata", {})))
ok("the instruction never becomes a system message",
   result.get("role") is None and "system" not in result)

result = tools.call("secret", {})
ok("a credential in a result is redacted",
   "ghp_AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH1234" not in json.dumps(result, default=str),
   json.dumps(result.get("content"))[:60])
ok("and something is left in its place",
   security.REDACTED in json.dumps(result, default=str))

started = time.time()
result = tools.call("flood", {"size": 200000})
flood_seconds = time.time() - started
ok("an oversized result is truncated", result.get("truncated") is True,
   result.get("error"))
ok("and is smaller than the limit",
   len(json.dumps(result["content"], default=str)) <= security.MAX_OUTPUT_CHARS + 400,
   len(json.dumps(result["content"], default=str)))
# Redaction runs over every byte a server returns. A pattern that can start at
# any character makes that quadratic, and a 20 KB result took 45 seconds before
# this was pinned down. It is a real timeout in normal use, not a micro-benchmark.
ok("a large result is handled promptly", flood_seconds < 10,
   "%.2fs" % flood_seconds)
result = tools.call("flood", {"size": 1000})
ok("a small result is not marked truncated", not result.get("truncated"))

# ── path guarding on an MCP tool ─────────────────────────────────────────
print("--- path guarding ---")

result = tools.call("read_file", {"path": "../../../Windows/System32/config/SAM"})
ok("a traversal path is blocked before the call",
   errors.category_of(result) == errors.SECURITY_BLOCKED, errors.category_of(result))
result = tools.call("read_file", {"path": "C:/Users/testuser/.ssh/id_rsa"})
ok("an ssh key is blocked",
   errors.category_of(result) == errors.SECURITY_BLOCKED, errors.category_of(result))
result = tools.call("read_file", {"path": os.path.join(ROOT, "README.md")})
ok("an ordinary path is allowed through", result["ok"] is True, result.get("error"))

# roots: a server told to stay in one folder is held to it
mcp_manager.shutdown()
mcp_manager.start(background=False, path=config_file(
    {"echo": echo_block(roots=[WORK])}, "rooted.json"))
result = tools.call("read_file", {"path": os.path.join(WORK, "inside.txt")})
ok("a path inside the configured roots is allowed", result["ok"] is True,
   result.get("error"))
result = tools.call("read_file", {"path": os.path.join(ROOT, "README.md")})
ok("a path outside the configured roots is blocked",
   errors.category_of(result) == errors.SECURITY_BLOCKED, errors.category_of(result))

# ── allowlists ───────────────────────────────────────────────────────────
print("--- tool allow and block lists ---")

mcp_manager.shutdown()
mcp_manager.start(background=False, path=config_file(
    {"echo": echo_block(allowedTools=["echo", "add"])}, "allowed.json"))
offered = {row["tool"] for row in mcp_manager.listing()}
ok("only allowed tools are registered", offered == {"echo", "add"}, sorted(offered))
ok("a tool outside the allowlist is not callable", "boom" not in tools.REGISTRY)

mcp_manager.shutdown()
mcp_manager.start(background=False, path=config_file(
    {"echo": echo_block(blockedTools=["boom", "secret"])}, "blocked.json"))
offered = {row["tool"] for row in mcp_manager.listing()}
ok("blocked tools are withheld", "boom" not in offered and "secret" not in offered)
ok("everything else is still offered", len(offered) == 6, sorted(offered))

# ── confirmation ─────────────────────────────────────────────────────────
print("--- confirmation ---")

mcp_manager.shutdown()
mcp_manager.start(background=False, path=config_file(
    {"echo": echo_block(riskLevel="high")}, "risky.json"))
ok("a high-risk server grades its tools high",
   all(row["risk_level"] == "high" for row in mcp_manager.listing()),
   [row["risk_level"] for row in mcp_manager.listing()])
ok("and they require confirmation",
   all(row["requires_confirmation"] for row in mcp_manager.listing()))
ok("the risk gate agrees", risk.gated("echo", {"text": "x"}))
ok("the reason names the server",
   "MCP server" in str(risk.reason("echo")), risk.reason("echo"))
ok("an explicit high level is not lowered by a read-shaped name",
   risk.level("read_file") == "high", risk.level("read_file"))

mcp_manager.shutdown()
mcp_manager.start(background=False, path=config_file(
    {"echo": echo_block(requiresConfirmation=True, riskLevel="low")}, "always.json"))
ok("a server may demand confirmation it would not otherwise need",
   all(row["requires_confirmation"] for row in mcp_manager.listing()))

# ── one broken server does not affect the others ─────────────────────────
print("--- failure isolation ---")

mcp_manager.shutdown()
outcome = mcp_manager.start(background=False, path=config_file({
    "broken": echo_block(args=[FIXTURE, "--fail-on-start"]),
    "echo": echo_block(),
}, "isolation.json"))
results = {row["server"]: row for row in outcome["servers"]}
ok("the broken server is reported as failed", results["broken"]["connected"] is False)
ok("with a reason", results["broken"]["error"], str(results["broken"]["error"])[:40])
ok("the healthy server still connected", results["echo"]["connected"] is True)
ok("and its tools are usable", tools.call("echo", {"text": "unaffected"})["ok"])
ok("Jarvish is not down", tools.call("get_time", {})["ok"])
verdicts = mcp_manager.health()
ok("health reports the failure",
   verdicts["broken"]["ready"] is False and verdicts["broken"]["error"],
   verdicts["broken"])
ok("and reports the healthy one as ready", verdicts["echo"]["ready"] is True)
ok("no tools were registered for the broken server",
   all(row["server"] != "broken" for row in mcp_manager.listing()))

# ── disconnect withdraws capabilities ────────────────────────────────────
print("--- disconnect and reconnect ---")

before = len(mcp_manager.listing())
outcome = mcp_manager.disconnect_server("echo")
ok("disconnect succeeds", outcome["ok"], outcome.get("error"))
ok("its tools are withdrawn from the registry", "echo" not in tools.REGISTRY)
ok("nothing MCP remains listed", mcp_manager.count_registered() == 0,
   mcp_manager.count_registered())
ok("native tools are still there", "get_time" in tools.REGISTRY)

result = mcp_manager._connections["echo"].call("echo", {"text": "x"})
ok("calling a disconnected server is refused",
   errors.category_of(result) == errors.SERVER_UNAVAILABLE, errors.category_of(result))

outcome = mcp_manager.connect_server("echo")
ok("reconnect succeeds", outcome["ok"], outcome.get("error"))
ok("its tools come back", len(mcp_manager.listing()) == before, len(mcp_manager.listing()))
ok("and work again", tools.call("echo", {"text": "back"})["ok"])

outcome = mcp_manager.connect_server("nonexistent")
ok("connecting an unknown server is refused, not raised", outcome["ok"] is False,
   outcome.get("error"))

# ── developer commands ───────────────────────────────────────────────────
print("--- developer commands ---")

state = mcp_manager.mcp_status()
ok("mcp status answers", state["ok"], state)
ok("it counts servers", state["total"] >= 1 and len(state["servers"]) >= 1,
   state.get("total"))
ok("it names each server's state",
   all(row["state"] in mcp_client.STATES for row in state["servers"]),
   [row["state"] for row in state["servers"]])
ok("it carries no secrets", "s3cret" not in json.dumps(state, default=str))

listed = mcp_manager.mcp_tools()
ok("mcp tools lists them", listed["ok"] and listed["count"] > 0, listed.get("count"))
listed = mcp_manager.mcp_tools(server="echo")
ok("mcp tools filters by server", all(t["server"] == "echo" for t in listed["tools"]))
listed = mcp_manager.mcp_tools(server="nope")
ok("an unknown server yields nothing", listed["count"] == 0)

reloaded = mcp_manager.mcp_reload()
ok("mcp reload runs", reloaded["ok"], reloaded.get("error"))

# ── capability selection ─────────────────────────────────────────────────
print("--- capability selection ---")

mcp_manager.shutdown()
mcp_manager.start(background=False, path=config_file({"echo": echo_block()}, "sel.json"))
groups = mcp_manager.capability_groups()
ok("each server becomes a capability group", "mcp:echo" in groups, list(groups))
ok("the group lists its tools", set(groups["mcp:echo"]["tools"]) >= {"echo", "add"})
ok("the group has trigger words", len(groups["mcp:echo"]["triggers"]) > 0)

from jarvish import capabilities as selector  # noqa: E402
schemas, offered = selector.select("please echo this back to me")
ok("MCP tools can be selected for a relevant request",
   any(s["function"]["name"] == "echo" for s in schemas), offered["count"])
ok("the offered list stays inside the budget",
   offered["count"] <= selector.BUDGET, offered["count"])

schemas, offered = selector.select("what time is it")
ok("an unrelated request still gets its native tool",
   any(s["function"]["name"] == "get_time" for s in schemas), offered["count"])

# ── MCP is optional ──────────────────────────────────────────────────────
print("--- MCP is optional ---")

mcp_manager.shutdown()
ok("shutdown withdraws everything", mcp_manager.count_registered() == 0)
ok("the registry is exactly as it started", set(tools.REGISTRY) == NATIVE_BEFORE,
   set(tools.REGISTRY) ^ NATIVE_BEFORE)
ok("no MCP capabilities remain",
   not [c for c in registry.manifest()["capabilities"] if c.get("source") == "mcp"])
ok("native tools still run", tools.call("get_time", {})["ok"])

outcome = mcp_manager.start(background=False,
                            path=os.path.join(WORK, "absent.json"))
ok("starting with no config is a quiet success", outcome["ok"],
   outcome.get("status"))
ok("and registers nothing", mcp_manager.count_registered() == 0)
ok("and Jarvish is unchanged", set(tools.REGISTRY) == NATIVE_BEFORE)

state = mcp_manager.status()
ok("status says so plainly", state["configured"] is False and state["tool_count"] == 0)

mcp_manager.shutdown()
shutil.rmtree(WORK, ignore_errors=True)

print("\n%d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
