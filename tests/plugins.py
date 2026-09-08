"""P7 tests: the capability registry, plugin isolation and permission floors.

These register real capabilities into the live registry, call them, and check
the security model actually holds — including that a plugin cannot grade itself
below what its declared permissions imply.
"""

import os
import sys
import tempfile
import textwrap
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvish import capabilities as selector   # noqa: E402
from jarvish import registry, risk, tools      # noqa: E402

P = F = 0


def ok(label, cond, extra=""):
    global P, F
    if cond:
        P += 1
    else:
        F += 1
    print("  [%s] %-48s %s" % ("PASS" if cond else "FAIL", label, str(extra)[:44]))


def cleanup(*identifiers):
    for identifier in identifiers:
        registry.unregister(identifier)


# ── the built-in tools still work through the registry ───────────────────
print("--- existing tools remain intact ---")
builtin_count = len(tools.REGISTRY)
manifest = registry.manifest()
ok("registry sees every tool", manifest["count"] >= builtin_count,
   "%s capabilities / %s tools" % (manifest["count"], builtin_count))
ok("built-ins marked as built in", manifest["sources"].get("builtin", 0) > 100,
   manifest["sources"])
ok("built-ins carry ids", all(":" in c["id"] for c in manifest["capabilities"]))
ok("built-ins carry versions", all(c["version"] for c in manifest["capabilities"]))
ok("built-ins carry permissions",
   all(c["permissions"] for c in manifest["capabilities"]))
ok("a known built-in still callable", tools.call("get_time", {})["ok"])

# ── registration ─────────────────────────────────────────────────────────
print("--- registration ---")
calls = {"n": 0}


def adder(a, b):
    calls["n"] += 1
    return {"ok": True, "sum": float(a) + float(b)}


r = registry.register_tool(
    name="test_add", description="Add two numbers.", handler=adder,
    parameters={"a": {"type": "number", "description": "first"},
                "b": {"type": "number", "description": "second"}},
    required=["a", "b"], risk_level="safe", permissions=("read",),
    version="1.2.3", module="testkit", outputs=["sum"])
ok("tool registered", r["ok"], r.get("registered"))
ok("id is module:name", r.get("registered") == "testkit:test_add")
ok("joined the live tool registry", "test_add" in tools.REGISTRY)
ok("schema published", any(s["function"]["name"] == "test_add"
                           for s in tools.SCHEMAS))
ok("risk tier declared", risk.level("test_add") == "safe", risk.level("test_add"))

# register -> discover -> select -> permission -> execute -> verify
found = registry.describe_one("test_add")
ok("discoverable by name", found["ok"] and found["version"] == "1.2.3")
ok("metadata complete",
   all(k in found for k in ("id", "name", "version", "description", "module",
                            "type", "inputs", "outputs", "permissions", "risk",
                            "dependencies", "available", "health")))
_schemas, report = selector.select("add two numbers with test_add")
ok("selectable by the agent", "test_add" in report["offered"], report["count"])
ok("permission checked before execution", not risk.gated("test_add", {}))
executed = tools.call("test_add", {"a": 2, "b": 3})
ok("executes for real", executed["ok"] and executed["sum"] == 5.0, executed)
ok("verified by side effect", calls["n"] == 1)

# ── metadata validation ──────────────────────────────────────────────────
print("--- validation ---")
ok("bad name refused",
   not registry.register_tool("not a name", "x", adder, module="testkit")["ok"])
ok("non-callable handler refused",
   not registry.register_tool("bad_handler", "x", "nope", module="testkit")["ok"])
ok("unknown permission refused",
   not registry.register_tool("bad_perm", "x", adder, permissions=("god",),
                              module="testkit")["ok"])
dup = registry.register_tool("test_add", "duplicate", adder, module="testkit")
ok("duplicate id refused", not dup["ok"], dup.get("error", "")[:40])
clash = registry.register_tool("get_time", "clashes with a built-in", adder,
                               module="other")
ok("clash with a built-in refused", not clash["ok"], clash.get("error", "")[:40])
ok("built-in tier not overwritten", risk.level("get_time") == "safe")

incompatible = registry.register_tool(
    "future_tool", "needs a newer Jarvish", adder, module="testkit",
    requires_jarvish="99.0")
ok("incompatible version refused", not incompatible["ok"],
   incompatible.get("error", "")[:44])

# ── permissions set a floor on risk ──────────────────────────────────────
print("--- permission floor ---")
sneaky = registry.register_tool(
    name="test_sneaky", description="Claims to be safe but wants system access.",
    handler=lambda: {"ok": True}, risk_level="safe",
    permissions=("system",), module="testkit")
ok("registered with escalated tier", sneaky["ok"] and sneaky["risk"] == "high",
   sneaky.get("risk"))
ok("cannot self-declare below its permissions", risk.level("test_sneaky") == "high")
ok("and is therefore gated", risk.gated("test_sneaky", {}))

writer = registry.register_tool(
    "test_writer", "Writes files.", lambda: {"ok": True}, risk_level="safe",
    permissions=("filesystem",), module="testkit")
ok("filesystem implies at least medium", risk.level("test_writer") == "medium",
   risk.level("test_writer"))

reader = registry.register_tool(
    "test_reader", "Reads only.", lambda: {"ok": True}, risk_level="safe",
    permissions=("read",), module="testkit")
ok("read stays safe", risk.level("test_reader") == "safe")

# ── dependencies ─────────────────────────────────────────────────────────
print("--- dependency resolution ---")
missing = registry.register_tool(
    "test_needs_module", "Needs something absent.", lambda: {"ok": True},
    requires={"python": ["a_module_that_is_not_installed"]},
    risk_level="safe", permissions=("read",), module="testkit")
ok("unmet dependency registers as unavailable",
   missing["ok"] and missing["available"] is False, missing.get("reason", "")[:40])
ok("unavailable tool is not callable", "test_needs_module" not in tools.REGISTRY)
ok("but is still discoverable",
   registry.describe_one("test_needs_module")["ok"])

satisfied = registry.register_tool(
    "test_needs_present", "Needs something present.", lambda: {"ok": True},
    requires={"python": ["json"], "capability": ["get_time"]},
    risk_level="safe", permissions=("read",), module="testkit")
ok("met dependency registers available", satisfied["ok"] and satisfied["available"])
ok("and becomes callable", "test_needs_present" in tools.REGISTRY)

exe = registry.register_tool(
    "test_needs_exe", "Needs a missing executable.", lambda: {"ok": True},
    requires={"executable": ["definitely_not_a_real_binary_xyz"]},
    risk_level="safe", permissions=("read",), module="testkit")
ok("missing executable detected", exe["ok"] and not exe["available"],
   exe.get("reason", "")[:40])

env = registry.register_tool(
    "test_needs_env", "Needs an env var.", lambda: {"ok": True},
    requires={"env": ["JARVISH_A_VAR_THAT_IS_UNSET"]},
    risk_level="safe", permissions=("read",), module="testkit")
ok("missing env var detected", env["ok"] and not env["available"])

# ── enable / disable ─────────────────────────────────────────────────────
print("--- enable and disable ---")
ok("disable removes it from the tool list",
   registry.set_enabled("testkit:test_add", False)["ok"] and
   "test_add" not in tools.REGISTRY)
ok("still discoverable while disabled",
   registry.describe_one("test_add")["ok"])
ok("enable restores it",
   registry.set_enabled("testkit:test_add", True)["ok"] and
   "test_add" in tools.REGISTRY)
ok("still executes after re-enabling", tools.call("test_add", {"a": 1, "b": 1})["sum"] == 2)
ok("cannot enable an unavailable capability",
   not registry.set_enabled("testkit:test_needs_module", True)["ok"])
ok("unknown capability refused", not registry.set_enabled("nope:nope", True)["ok"])

# ── health checks ────────────────────────────────────────────────────────
print("--- health ---")
registry.register_tool("test_healthy", "Fine.", lambda: {"ok": True},
                       risk_level="safe", permissions=("read",), module="testkit",
                       health=lambda: True)
registry.register_tool("test_sick", "Broken.", lambda: {"ok": True},
                       risk_level="safe", permissions=("read",), module="testkit",
                       health=lambda: {"ok": False, "detail": "backend is down"})
registry.register_tool("test_raises", "Health check explodes.", lambda: {"ok": True},
                       risk_level="safe", permissions=("read",), module="testkit",
                       health=lambda: 1 / 0)
registry.register_tool("test_hangs", "Health check hangs.", lambda: {"ok": True},
                       risk_level="safe", permissions=("read",), module="testkit",
                       health=lambda: time.sleep(30))

ok("healthy reported healthy",
   registry.check_health("testkit:test_healthy")["status"] == "healthy")
sick = registry.check_health("testkit:test_sick")
ok("unhealthy reported with reason",
   sick["status"] == "unhealthy" and sick["detail"] == "backend is down")
ok("unhealthy is disabled automatically", "test_sick" not in tools.REGISTRY)
ok("raising check is unhealthy, not fatal",
   registry.check_health("testkit:test_raises")["status"] == "unhealthy")
started = time.time()
hung = registry.check_health("testkit:test_hangs")
ok("hanging check times out", hung["status"] == "unhealthy" and
   time.time() - started < registry.HEALTH_TIMEOUT + 3, "%.1fs" % (time.time() - started))
ok("no health check -> unknown, not failed",
   registry.check_health("testkit:test_reader")["status"] == "unknown")

summary = registry.check_all_health()
ok("health sweep runs", summary["ok"] and summary["checked"] > 0, summary["summary"])

# ── skills ───────────────────────────────────────────────────────────────
print("--- skills ---")
sk = registry.register_skill(
    "test_skill", "A test procedure.",
    "Use get_time and report the time in one short sentence.",
    uses=("get_time",), module="testkit")
ok("skill registered", sk["ok"], sk.get("registered"))
ok("skill discoverable", registry.describe_one("test_skill")["ok"])
ok("skill exposes its instructions",
   "get_time" in registry.describe_one("test_skill")["instructions"])
ok("empty instructions refused",
   not registry.register_skill("empty_skill", "x", "", module="testkit")["ok"])
ok("unknown skill refused", not registry.run_skill("no_such_skill")["ok"])

# ── providers ────────────────────────────────────────────────────────────
print("--- providers ---")
pr = registry.register_provider(
    "vision", "test_vision_backend", "A declared vision provider.",
    version="0.9", module="testkit")
ok("provider registered", pr["ok"], pr.get("registered"))
ok("provider discoverable by kind",
   any(p["name"] == "test_vision_backend" for p in registry.providers("vision")))
ok("bad kind refused",
   not registry.register_provider("nonsense", "x", "y", module="testkit")["ok"])
ok("tool kind refused as a provider",
   not registry.register_provider("tool", "x", "y", module="testkit")["ok"])
ok("provider is not executable", "test_vision_backend" not in tools.REGISTRY)

# ── plugin isolation ─────────────────────────────────────────────────────
print("--- plugin isolation ---")
folder = Path(tempfile.mkdtemp(prefix="jarvish-plugins-"))

(folder / "good.py").write_text(textwrap.dedent('''
    def shout(text):
        return {"ok": True, "shouted": str(text).upper()}

    def register(api):
        api.tool(name="test_shout", description="Uppercase some text.",
                 handler=shout,
                 parameters={"text": {"type": "string", "description": "words"}},
                 required=["text"], risk_level="safe", permissions=("read",))
'''), encoding="utf-8")

(folder / "importfail.py").write_text(
    "import a_module_that_does_not_exist\n\ndef register(api):\n    pass\n",
    encoding="utf-8")

(folder / "noregister.py").write_text("X = 1\n", encoding="utf-8")

(folder / "raises.py").write_text(textwrap.dedent('''
    def register(api):
        api.tool(name="test_partial", description="Registers before failing.",
                 handler=lambda: {"ok": True}, risk_level="safe",
                 permissions=("read",))
        raise RuntimeError("deliberate failure after one registration")
'''), encoding="utf-8")

result = registry.load_plugins(folder)
ok("good plugin loaded", any(e["plugin"] == "good" for e in result["loaded"]))
ok("its tool is callable",
   tools.call("test_shout", {"text": "hi"})["shouted"] == "HI")
failed = {e["plugin"]: e["error"] for e in result["failed"]}
ok("import failure isolated", "importfail" in failed, failed.get("importfail", "")[:40])
ok("missing register() reported", "noregister" in failed,
   failed.get("noregister", "")[:44])
ok("exception during register isolated", "raises" in failed)
ok("partial registration kept", "test_partial" in tools.REGISTRY)
ok("one bad plugin did not stop the others", "test_shout" in tools.REGISTRY)
ok("server still healthy", tools.call("get_time", {})["ok"])

# ── hot reload ───────────────────────────────────────────────────────────
print("--- hot reload ---")
(folder / "good.py").write_text(textwrap.dedent('''
    def whisper(text):
        return {"ok": True, "whispered": str(text).lower()}

    def register(api):
        api.tool(name="test_whisper", description="Lowercase some text.",
                 handler=whisper,
                 parameters={"text": {"type": "string", "description": "words"}},
                 required=["text"], risk_level="safe", permissions=("read",))
'''), encoding="utf-8")

before = len(tools.REGISTRY)
reloaded = registry.load_plugins(folder)
ok("reload picked up the change", "test_whisper" in tools.REGISTRY)
ok("new tool works", tools.call("test_whisper", {"text": "HI"})["whispered"] == "hi")
ok("built-ins survived the reload", tools.call("get_time", {})["ok"])
ok("registry still consistent", len(tools.REGISTRY) >= before - 5)

# ── security ─────────────────────────────────────────────────────────────
print("--- security ---")
danger = registry.register_tool(
    "test_dangerous", "Wants system access.", lambda: {"ok": True},
    risk_level="critical", permissions=("system", "execute"), module="testkit")
ok("critical plugin tool stays critical", risk.level("test_dangerous") == "critical")
ok("and is gated", risk.gated("test_dangerous", {}))
ok("gate reason available", bool(risk.reason("test_dangerous")))
ok("registry tools are graded, never ungraded",
   all(risk.level(c["name"]) in risk.ORDER
       for c in registry.manifest(kind="tool")["capabilities"]))

unknown = registry.register_tool(
    "test_unknown_tier", "Bad tier string.", lambda: {"ok": True},
    risk_level="totally_made_up", permissions=("read",), module="testkit")
ok("unrecognised tier falls back to high",
   unknown["ok"] and risk.level("test_unknown_tier") == "high")
ok("and is gated", risk.gated("test_unknown_tier", {}))

ok("built-in cannot be unregistered",
   not registry.unregister("core:get_time")["ok"] or "get_time" in tools.REGISTRY)

# ── cleanup ──────────────────────────────────────────────────────────────
print("--- unregister ---")
ok("unregister removes the tool",
   registry.unregister("testkit:test_add")["ok"] and "test_add" not in tools.REGISTRY)
ok("and its risk declaration", risk.level("test_add") == "high")
ok("unknown id refused", not registry.unregister("nope:nope")["ok"])

for identifier in list(registry._capabilities):
    if identifier.startswith("testkit:") or identifier.startswith("good:") \
            or identifier.startswith("raises:"):
        registry.unregister(identifier)

import shutil   # noqa: E402
shutil.rmtree(folder, ignore_errors=True)
registry.reload_plugins()

ok("real plugin still loads after all that", "convert_units" in tools.REGISTRY)
ok("and works", tools.call("convert_units",
                           {"value": 1, "from_unit": "m", "to_unit": "cm"})["value"] == 100)

print("\n%d passed, %d failed" % (P, F))
