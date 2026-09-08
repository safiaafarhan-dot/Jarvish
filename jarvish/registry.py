"""The capability registry: what Jarvish can do, and how to add more at runtime.

`capabilities.py` decides *which* capabilities to offer the model each turn.
This module decides *what exists at all* — and lets that set grow without
touching the agent loop.

Every capability, built in or added by a plugin, carries the same record:

    id  name  version  description  module  type  inputs  outputs
    permissions  risk  dependencies  availability  health  enabled

Four rules hold the design together.

**Availability is verified, not assumed.** A capability declares what it needs —
a Python module, an executable on PATH, an environment variable, another
capability — and the registry checks. Something missing is registered as
*discoverable but unavailable*, with the reason, rather than as a tool that
fails the first time it is called.

**Permissions set a floor on risk.** A capability that asks for `system` or
`filesystem` access cannot register itself as `safe`. The declared tier and the
tier implied by its permissions are compared, and the stricter wins. So a
plugin cannot make itself ungated by understating what it does.

**A broken plugin is contained.** Import errors, bad metadata, duplicate ids,
incompatible versions and exceptions during registration are caught per file.
One bad plugin is reported and skipped; the rest load, and the server does not
notice.

**Nothing bypasses the gate.** A registered tool joins the same
`tools.REGISTRY` the built-ins live in, and its tier is enforced by `risk.py`
exactly as theirs is. Emergency stop, telemetry and the confirmation gate all
apply unchanged, because there is no second execution path.

Two kinds are executable: **tool** (a callable the model can invoke) and
**skill** (a named procedure run through the ordinary agent loop). The provider
kinds — `model`, `vision`, `voice`, `memory`, `embedding`, `agent` — are
declarations that subsystems query through `providers()`. That is discovery,
not execution, and this module does not pretend otherwise.
"""

import importlib.util
import os
import re
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path

from .util import as_bool, boolean, err, ok, string, tool as tool_schema

# `tools` imports this module to pick up its schemas, and `risk` imports
# `tools` — so importing either at module level would close a cycle. Both are
# fetched lazily instead; by the time any function here runs, both are loaded.


def _tools():
    from . import tools
    return tools


def _risk():
    from . import risk
    return risk


# Mirrors risk.ORDER. Declared literally so this module has no import-time
# dependency on `risk`.
TIERS = ("safe", "low", "medium", "high", "critical")
TIER_LABELS = {
    "safe": "Read-only", "low": "Low risk", "medium": "Changes state",
    "high": "High risk", "critical": "Irreversible",
}

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"

# What this build of Jarvish is, for plugin compatibility checks.
VERSION = "2.1.0"

KINDS = ("tool", "skill", "agent", "model", "vision", "voice", "memory", "embedding")
EXECUTABLE_KINDS = ("tool", "skill")
PROVIDER_KINDS = tuple(k for k in KINDS if k not in EXECUTABLE_KINDS)

# What a capability is allowed to touch, and the least risk that implies. A
# capability cannot claim a scope and then grade itself below this.
PERMISSIONS = ("read", "network", "browser", "execute", "write", "filesystem", "system")
PERMISSION_FLOOR = {
    "read": "safe",
    "network": "low",
    "browser": "low",
    "execute": "medium",
    "write": "medium",
    "filesystem": "medium",
    "system": "high",
}

# A plugin that hangs during registration or a health check must not hang the
# server, so both are run with a deadline.
PLUGIN_TIMEOUT = 10.0
HEALTH_TIMEOUT = 5.0

_lock = threading.RLock()
_capabilities = {}      # id -> record
_handlers = {}          # id -> callable (tools only)
_skills = {}            # id -> skill definition
_health_checks = {}     # id -> callable
_plugins = {}           # plugin name -> load report


def _identifier(module, name):
    return str(module) + ":" + str(name)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

_VERSION = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


def _version_tuple(text):
    match = _VERSION.match(str(text or ""))
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def _compatible(requires_jarvish):
    """Whether this build satisfies a plugin's minimum version."""
    if not requires_jarvish:
        return True, None
    wanted = _version_tuple(requires_jarvish)
    if wanted is None:
        return False, "Unreadable version requirement: " + str(requires_jarvish)
    if _version_tuple(VERSION) < wanted:
        return False, ("Needs Jarvish " + str(requires_jarvish) +
                       "; this build is " + VERSION + ".")
    return True, None


def _check_requires(requires):
    """(available, reason). Every requirement is actually checked."""
    if not requires:
        return True, None
    missing = []

    for module in requires.get("python", ()) or ():
        try:
            if importlib.util.find_spec(module) is None:
                missing.append("Python module '" + module + "'")
        except (ImportError, ValueError):
            missing.append("Python module '" + module + "'")
    for binary in requires.get("executable", ()) or ():
        if shutil.which(binary) is None:
            missing.append("executable '" + binary + "'")
    for variable in requires.get("env", ()) or ():
        if not os.environ.get(variable):
            missing.append("environment variable " + variable)
    for other in requires.get("capability", ()) or ():
        known = other in _tools().REGISTRY or any(
            entry["name"] == other for entry in _capabilities.values())
        if not known:
            missing.append("capability '" + other + "'")

    if missing:
        return False, "Needs " + ", ".join(missing) + "."
    return True, None


def _effective_tier(declared, permissions):
    """The stricter of what was declared and what the permissions imply."""
    tier = declared if declared in TIERS else "high"
    for scope in permissions or ():
        floor = PERMISSION_FLOOR.get(scope)
        if floor and TIERS.index(floor) > TIERS.index(tier):
            tier = floor
    return tier


def _validate_permissions(permissions):
    unknown = [p for p in (permissions or ()) if p not in PERMISSIONS]
    if unknown:
        return False, ("Unknown permission(s): " + ", ".join(unknown) +
                       ". Valid: " + ", ".join(PERMISSIONS) + ".")
    return True, None


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

def _record(name, kind, description, module, version, inputs, outputs,
            permissions, tier, reversible, requires, source, available, reason):
    return {
        "id": _identifier(module, name),
        "name": name,
        "kind": kind,
        "type": kind,
        "version": str(version),
        "description": description,
        "module": module,
        "inputs": inputs,
        "outputs": outputs or [],
        "permissions": list(permissions or []),
        "risk": tier,
        "risk_label": TIER_LABELS.get(tier, tier),
        "reversible": bool(reversible),
        "dependencies": requires or {},
        "available": available,
        "unavailable_reason": reason,
        "health": {"status": "unknown" if available else "unavailable",
                   "checked_at": None, "detail": reason},
        "enabled": available,
        "source": source,
        "registered_at": time.time(),
    }


def _activate(record, handler, parameters, required):
    """Put a tool into the live registry the built-ins share."""
    name = record["name"]
    schema = tool_schema(name, record["description"], parameters or {}, required)
    _tools().SCHEMAS[:] = [s for s in _tools().SCHEMAS
                        if s["function"]["name"] != name] + [schema]
    _tools().REGISTRY[name] = handler
    _risk().declare(name, record["risk"], reversible=record["reversible"])


def _deactivate(name):
    _tools().REGISTRY.pop(name, None)
    _tools().SCHEMAS[:] = [s for s in _tools().SCHEMAS if s["function"]["name"] != name]
    _risk().undeclare(name)


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

def register_tool(name, description, handler, parameters=None, required=(),
                  risk_level="high", reversible=True, requires=None,
                  permissions=(), version="1.0", module="plugin", outputs=None,
                  source="plugin", health=None, requires_jarvish=None):
    """Add a callable tool at runtime. It becomes a first-class capability."""
    name = str(name or "").strip()
    if not name.isidentifier():
        return err("'" + str(name) + "' is not a valid tool name.")
    if not callable(handler):
        return err("Tool '" + name + "' has no callable handler.")

    valid, why = _validate_permissions(permissions)
    if not valid:
        return err(why)
    compatible, incompatible_reason = _compatible(requires_jarvish)
    if not compatible:
        return err("Tool '" + name + "' is incompatible: " + incompatible_reason)

    with _lock:
        identifier = _identifier(module, name)
        if identifier in _capabilities:
            return err("Duplicate capability id '" + identifier + "'.")
        if name in _tools().REGISTRY:
            return err("'" + name + "' is already registered as a tool. "
                       "Capability names must be unique.")

        tier = _effective_tier(risk_level, permissions)
        available, reason = _check_requires(requires)

        record = _record(name, "tool", str(description or "").strip() or name,
                         module, version, sorted((parameters or {}).keys()),
                         outputs, permissions, tier, reversible, requires,
                         source, available, reason)
        _capabilities[identifier] = record
        _handlers[identifier] = handler
        if health:
            _health_checks[identifier] = health

        if not available:
            # Discoverable, with the reason — but never callable.
            return ok(registered=identifier, available=False, reason=reason,
                      risk=tier)

        _activate(record, handler, parameters, required)
        record["_parameters"] = parameters or {}
        record["_required"] = list(required)
        return ok(registered=identifier, name=name, available=True, risk=tier,
                  permissions=list(permissions))


def register_skill(name, description, instructions, uses=(), version="1.0",
                   requires=None, permissions=("read",), module="plugin",
                   source="plugin", requires_jarvish=None):
    """Add a named procedure the agent can run through the ordinary loop."""
    name = str(name or "").strip()
    if not name:
        return err("A skill needs a name.")
    if not str(instructions or "").strip():
        return err("Skill '" + name + "' has no instructions.")

    valid, why = _validate_permissions(permissions)
    if not valid:
        return err(why)
    compatible, incompatible_reason = _compatible(requires_jarvish)
    if not compatible:
        return err("Skill '" + name + "' is incompatible: " + incompatible_reason)

    with _lock:
        identifier = _identifier(module, name)
        if identifier in _capabilities:
            return err("Duplicate capability id '" + identifier + "'.")

        available, reason = _check_requires(requires)
        tier = _effective_tier("safe", permissions)
        _skills[identifier] = {
            "name": name,
            "description": str(description or "").strip() or name,
            "instructions": str(instructions).strip(),
            "uses": list(uses),
        }
        _capabilities[identifier] = _record(
            name, "skill", _skills[identifier]["description"], module, version,
            ["input"], ["answer"], permissions, tier, True, requires, source,
            available, reason)
    return ok(registered=identifier, kind="skill", available=available,
              reason=reason, risk=tier)


def register_provider(kind, name, description, version="1.0", requires=None,
                      permissions=("read",), module="plugin", source="plugin",
                      outputs=None, health=None, requires_jarvish=None):
    """Declare a provider — a model, vision, voice, memory or embedding backend.

    Discovery, not execution: subsystems ask `providers(kind)` what exists.
    Registering one does not make anything call it.
    """
    kind = str(kind or "").strip().lower()
    if kind not in PROVIDER_KINDS:
        return err("Provider kind must be one of: " + ", ".join(PROVIDER_KINDS) + ".")
    name = str(name or "").strip()
    if not name:
        return err("A provider needs a name.")

    valid, why = _validate_permissions(permissions)
    if not valid:
        return err(why)
    compatible, incompatible_reason = _compatible(requires_jarvish)
    if not compatible:
        return err("Provider '" + name + "' is incompatible: " + incompatible_reason)

    with _lock:
        identifier = _identifier(module, name)
        if identifier in _capabilities:
            return err("Duplicate capability id '" + identifier + "'.")
        available, reason = _check_requires(requires)
        _capabilities[identifier] = _record(
            name, kind, str(description or "").strip() or name, module, version,
            [], outputs, permissions, _effective_tier("safe", permissions),
            True, requires, source, available, reason)
        if health:
            _health_checks[identifier] = health
    return ok(registered=identifier, kind=kind, available=available, reason=reason)


def unregister(identifier):
    """Remove a runtime capability. Built-ins cannot be removed."""
    with _lock:
        record = _capabilities.get(identifier)
        if record is None:
            return err("'" + str(identifier) + "' is not a registered capability.")
        _capabilities.pop(identifier, None)
        _handlers.pop(identifier, None)
        _skills.pop(identifier, None)
        _health_checks.pop(identifier, None)
        if record["kind"] == "tool":
            _deactivate(record["name"])
    return ok(unregistered=identifier)


def set_enabled(identifier, enabled=True):
    """Turn a capability on or off without unregistering it."""
    with _lock:
        record = _capabilities.get(identifier)
        if record is None:
            return err("'" + str(identifier) + "' is not a registered capability.")
        wanted = as_bool(enabled, True)

        if wanted and not record["available"]:
            return err("'" + identifier + "' cannot be enabled: " +
                       str(record["unavailable_reason"]))

        record["enabled"] = wanted
        if record["kind"] == "tool":
            if wanted:
                _activate(record, _handlers[identifier],
                          record.get("_parameters"), record.get("_required", ()))
            else:
                _deactivate(record["name"])
    return ok(capability=identifier, enabled=wanted)


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------

def check_health(identifier):
    """Run a capability's health check, with a deadline.

    An unhealthy capability is disabled rather than left in the tool list to
    fail when the model reaches for it.
    """
    with _lock:
        record = _capabilities.get(identifier)
        check = _health_checks.get(identifier)
    if record is None:
        return err("'" + str(identifier) + "' is not a registered capability.")

    if not record["available"]:
        status, detail = "unavailable", record["unavailable_reason"]
    elif check is None:
        status, detail = "unknown", "No health check declared."
    else:
        outcome = {}

        def run():
            try:
                outcome["value"] = check()
            except Exception as exc:
                outcome["error"] = str(exc)[:200]

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(HEALTH_TIMEOUT)

        if worker.is_alive():
            status = "unhealthy"
            detail = "Health check timed out after " + str(HEALTH_TIMEOUT) + "s."
        elif "error" in outcome:
            status, detail = "unhealthy", outcome["error"]
        else:
            value = outcome.get("value")
            if isinstance(value, dict):
                status = "healthy" if value.get("ok", True) else "unhealthy"
                detail = value.get("detail")
            else:
                status = "healthy" if value or value is None else "unhealthy"
                detail = None

    with _lock:
        record["health"] = {"status": status, "checked_at": time.time(),
                            "detail": detail}
        # An unhealthy capability stops being offered, but stays discoverable.
        if status == "unhealthy" and record["enabled"] and record["kind"] == "tool":
            record["enabled"] = False
            _deactivate(record["name"])
    return ok(capability=identifier, status=status, detail=detail)


def check_all_health():
    with _lock:
        identifiers = list(_capabilities)
    results = [check_health(identifier) for identifier in identifiers]
    summary = {}
    for result in results:
        if result["ok"]:
            summary[result["status"]] = summary.get(result["status"], 0) + 1
    return ok(checked=len(results), summary=summary,
              unhealthy=[r["capability"] for r in results
                         if r["ok"] and r["status"] == "unhealthy"])


# --------------------------------------------------------------------------
# The plugin API and loader
# --------------------------------------------------------------------------

class PluginAPI:
    """What a plugin file is handed. Deliberately small."""

    def __init__(self, source):
        self.source = source
        self.registered = []
        self.rejected = []

    def _note(self, result):
        if result["ok"]:
            self.registered.append(result.get("registered"))
        else:
            self.rejected.append(result.get("error"))
        return result

    def tool(self, name, description, handler, parameters=None, required=(),
             risk_level="high", reversible=True, requires=None, permissions=(),
             version="1.0", outputs=None, health=None, requires_jarvish=None):
        return self._note(register_tool(
            name, description, handler, parameters, required, risk_level,
            reversible, requires, permissions, version, module=self.source,
            outputs=outputs, source="plugin", health=health,
            requires_jarvish=requires_jarvish))

    def skill(self, name, description, instructions, uses=(), version="1.0",
              requires=None, permissions=("read",), requires_jarvish=None):
        return self._note(register_skill(
            name, description, instructions, uses, version, requires,
            permissions, module=self.source, source="plugin",
            requires_jarvish=requires_jarvish))

    def provider(self, kind, name, description, version="1.0", requires=None,
                 permissions=("read",), outputs=None, health=None,
                 requires_jarvish=None):
        return self._note(register_provider(
            kind, name, description, version, requires, permissions,
            module=self.source, source="plugin", outputs=outputs, health=health,
            requires_jarvish=requires_jarvish))


def _load_one(path):
    """Import one plugin file and let it register. Never raises."""
    key = path.stem
    api = PluginAPI(key)
    outcome = {}

    def run():
        try:
            spec = importlib.util.spec_from_file_location(
                "jarvish_plugin_" + key, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            entry = getattr(module, "register", None)
            if not callable(entry):
                raise AttributeError(
                    "no register(api) function — a plugin must define one")
            entry(api)
            outcome["ok"] = True
        except Exception as exc:
            lines = traceback.format_exc(limit=3).strip().splitlines()
            outcome["error"] = (type(exc).__name__ + ": " + str(exc)[:160]) \
                if str(exc) else lines[-1][:200]

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(PLUGIN_TIMEOUT)

    if worker.is_alive():
        return {"loaded": False, "path": str(path), "capabilities": [],
                "rejected": api.rejected,
                "error": "Timed out after " + str(PLUGIN_TIMEOUT) +
                         "s while loading."}
    if "error" in outcome:
        return {"loaded": False, "path": str(path),
                "capabilities": api.registered, "rejected": api.rejected,
                "error": outcome["error"]}
    return {"loaded": True, "path": str(path), "capabilities": api.registered,
            "rejected": api.rejected, "error": None}


def load_plugins(directory=None):
    """Load every plugin file, in isolation from one another."""
    folder = Path(directory) if directory else PLUGIN_DIR
    if not folder.exists():
        return ok(directory=str(folder), loaded=[], failed=[], count=0,
                  note="No plugins directory; nothing to load.")

    loaded, failed = [], []
    # Deliberately not holding `_lock` around this. `_load_one` runs the plugin
    # on a worker thread so a hang can be abandoned, and that thread calls
    # `register_*`, which takes `_lock` itself. An RLock is reentrant for the
    # thread that owns it, not across threads — holding it here would deadlock
    # every plugin until the timeout fired.
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        report = _load_one(path)
        with _lock:
            _plugins[path.stem] = report
        if report["loaded"]:
            loaded.append({"plugin": path.stem,
                           "capabilities": report["capabilities"],
                           "rejected": report["rejected"]})
        else:
            failed.append({"plugin": path.stem, "error": report["error"],
                           "registered_before_failure": report["capabilities"]})

    return ok(directory=str(folder), loaded=loaded, failed=failed,
              count=sum(len(entry["capabilities"]) for entry in loaded),
              isolated=True)


def reload_plugins():
    """Drop everything plugins registered, then load them again."""
    with _lock:
        for identifier in [i for i, r in _capabilities.items()
                           if r["source"] == "plugin"]:
            unregister(identifier)
        _plugins.clear()
    # Outside the lock: loading spawns worker threads that need it themselves.
    return load_plugins()


# --------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------

def run_skill(name, input=None):
    """Run a registered skill as an ordinary agent turn.

    The skill supplies instructions; the agent supplies planning, capability
    selection, the risk gate and verification. A skill is a shortcut, not a
    second execution path, so it cannot escape any of that.
    """
    wanted = str(name or "").strip()
    with _lock:
        match = next(((i, s) for i, s in _skills.items()
                      if s["name"] == wanted or i == wanted), None)
    if match is None:
        available = sorted(s["name"] for s in _skills.values())
        return err("No skill called '" + wanted + "'." +
                   (" Available: " + ", ".join(available) + "." if available else
                    " None are registered."))

    identifier, skill = match
    record = _capabilities.get(identifier, {})
    if not record.get("available", True):
        return err("Skill '" + skill["name"] + "' is unavailable. " +
                   str(record.get("unavailable_reason") or ""))
    if not record.get("enabled", True):
        return err("Skill '" + skill["name"] + "' is disabled.")

    prompt = skill["instructions"]
    if input:
        prompt += "\n\nInput from the user: " + str(input)

    import asyncio
    from . import llm, session as sessions

    async def drive():
        conversation = sessions.get("skill:" + skill["name"])
        conversation.rearm()
        answer, used = "", []
        async for event in llm.run_agent([{"role": "user", "content": prompt}],
                                         session=conversation):
            if event.get("type") == "token":
                answer += event["text"]
            elif event.get("type") == "tool_start":
                used.append(event["name"])
            elif event.get("type") == "error":
                raise RuntimeError(event["message"])
        return answer.strip(), used

    try:
        answer, used = asyncio.run(drive())
    except Exception as exc:
        return err("Skill '" + skill["name"] + "' failed: " + str(exc)[:200])

    return ok(skill=skill["name"], answer=answer, tools_used=used,
              expected_tools=skill["uses"])


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def _builtin_records():
    """The built-in tools, described in the same shape as everything else."""
    from . import capabilities as selector
    records = {}
    registered_names = {r["name"] for r in _capabilities.values()}
    for entry in selector.describe()["capabilities"]:
        name = entry["name"]
        if name in registered_names:
            continue
        identifier = _identifier(entry["module"], name)
        record = _record(name, "tool", entry["description"], entry["module"],
                         VERSION, entry["inputs"], [], _implied_permissions(entry),
                         entry["risk"], entry["reversible"], None, "builtin",
                         True, None)
        record["health"] = {"status": "healthy", "checked_at": None,
                            "detail": "Built in."}
        records[identifier] = record
    return records


def _implied_permissions(entry):
    """A rough scope for a built-in, from its module and tier."""
    module = entry.get("module")
    mapping = {
        "browser": ["browser", "network"], "vision": ["read", "system"],
        "network": ["network", "system"], "knowledge": ["network"],
        "files": ["filesystem"], "dev": ["filesystem", "execute"],
        "desktop": ["system"], "shell": ["system", "execute"],
        "tasks": ["execute"], "personal": ["write"], "webapps": ["browser"],
        "messaging": ["network"], "proactive": ["read"], "core": ["read"],
    }
    return mapping.get(module, ["read"])


def manifest(kind=None, available_only=False, source=None):
    """Every capability Jarvish has, built in or registered."""
    with _lock:
        everything = dict(_builtin_records())
        everything.update(_capabilities)

    entries = [dict(e) for e in everything.values()]
    for entry in entries:
        entry.pop("_parameters", None)
        entry.pop("_required", None)
    if kind:
        entries = [e for e in entries if e["kind"] == str(kind).lower()]
    if source:
        entries = [e for e in entries if e["source"] == str(source).lower()]
    if as_bool(available_only):
        entries = [e for e in entries if e["available"]]
    entries.sort(key=lambda e: (e["kind"], e["module"], e["name"]))

    by_kind, by_source, by_health = {}, {}, {}
    for entry in entries:
        by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
        by_source[entry["source"]] = by_source.get(entry["source"], 0) + 1
        status = entry["health"]["status"]
        by_health[status] = by_health.get(status, 0) + 1

    return {
        "capabilities": entries,
        "count": len(entries),
        "kinds": by_kind,
        "sources": by_source,
        "health": by_health,
        "enabled": sum(1 for e in entries if e["enabled"]),
        "unavailable": [{"id": e["id"], "reason": e["unavailable_reason"]}
                        for e in entries if not e["available"]],
        "plugins": dict(_plugins),
        "plugin_dir": str(PLUGIN_DIR),
        "jarvish_version": VERSION,
        "permissions": list(PERMISSIONS),
        "executable_kinds": list(EXECUTABLE_KINDS),
    }


def providers(kind):
    """Registered providers of one kind, for a subsystem to consult."""
    with _lock:
        return [dict(e) for e in _capabilities.values()
                if e["kind"] == str(kind).lower() and e["enabled"]]


def describe_one(name):
    with _lock:
        everything = dict(_builtin_records())
        everything.update(_capabilities)
    record = everything.get(name)
    if record is None:
        record = next((e for e in everything.values() if e["name"] == name), None)
    if record is None:
        return err("No capability called '" + str(name) + "'.")
    payload = {k: v for k, v in record.items() if not k.startswith("_")}
    identifier = record["id"]
    if record["kind"] == "skill" and identifier in _skills:
        payload["instructions"] = _skills[identifier]["instructions"]
        payload["uses"] = _skills[identifier]["uses"]
    return ok(**payload)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def tool_list(kind=None, available_only=False):
    data = manifest(kind, available_only)
    if not kind:
        # The full list is long; summarise unless a kind was asked for.
        data["capabilities"] = [
            {"id": e["id"], "name": e["name"], "kind": e["kind"],
             "risk": e["risk"], "available": e["available"],
             "enabled": e["enabled"], "health": e["health"]["status"]}
            for e in data["capabilities"]]
    return ok(**data)


SCHEMAS = [
    tool_schema("list_capabilities",
                "List everything Jarvish can do — built-in tools, plugin tools, "
                "skills and registered providers — with risk, health and whether "
                "each is available. Use when asked what you can do or what "
                "plugins are loaded.",
                {"kind": string("Only this kind.", list(KINDS)),
                 "available_only": boolean("Hide anything unavailable.")}),
    tool_schema("capability_info",
                "Full detail on one capability: version, permissions, what it "
                "needs, its risk, its health, and why it is unavailable if it is.",
                {"name": string("The capability name or id.")},
                ["name"]),
    tool_schema("run_skill",
                "Run a registered skill — a named procedure added by a plugin.",
                {"name": string("The skill to run."),
                 "input": string("Anything the skill should work on.")},
                ["name"]),
    tool_schema("reload_plugins",
                "Re-read the plugins folder, picking up new or changed plugins "
                "without restarting."),
    tool_schema("capability_health",
                "Run health checks across registered capabilities and report "
                "which are unhealthy."),
    tool_schema("set_capability",
                "Enable or disable a registered capability by its id.",
                {"capability": string("The capability id, like 'weather:sun_times'."),
                 "enabled": boolean("True to enable, false to disable.")},
                ["capability"]),
]

REGISTRY = {
    "list_capabilities": tool_list,
    "capability_info": describe_one,
    "run_skill": run_skill,
    "reload_plugins": reload_plugins,
    "capability_health": check_all_health,
    "set_capability": lambda capability, enabled=True: set_enabled(capability, enabled),
}
