"""The MCP manager: connections in, capabilities out.

This is the join between MCP and the rest of Jarvish, and its whole purpose is
that there is **no second registry**. A discovered MCP tool is registered
through `registry.register_tool`, exactly as a plugin's tool is, which puts it
into the same `tools.REGISTRY` the built-ins live in, gives it a tier in
`risk.py`, and subjects it to the same confirmation gate and autonomy ceiling.
By the time the model sees a tool list there is nothing to distinguish
`get_weather` from a tool borrowed from a server on the other side of a pipe -
and nothing that could let the borrowed one skip a check.

Four things happen here that do not happen for a native tool, because a native
tool is code in this repository and an MCP tool is not.

**It is graded without being trusted.** A server states a risk level in the
config and its tools may carry MCP annotations. Annotations are used, but only
in the cautious direction: `destructiveHint` can raise a tool's tier, while
`readOnlyHint` can lower it only as far as `low`, never to `safe`. Safe tools
run concurrently and without a prompt, and that is not something a third party
gets to claim about itself.

**Its arguments are checked before they leave.** The model writes the
arguments, the server publishes the schema, and neither is trusted with the
other. `security.validate_arguments` checks the call against the schema, and
every path-shaped argument is checked against the server's configured roots.

**Its name cannot displace a native one.** Registration refuses a duplicate, so
a server offering `read_text_file` gets a prefixed name and Jarvish's own
implementation keeps the plain one. The safest implementation wins by
construction rather than by policy.

**Its failure is contained.** Connecting happens on a background thread, one
server at a time, each wrapped. A server that never starts leaves every other
capability untouched, and Jarvish's own startup never waits for it.
"""

import re
import threading
import time

from . import errors, mcp_client, mcp_config, observability, security
from .config import MCP_ENABLED, MCP_MAX_TOOLS, REQUIRE_CONFIRMATION
from .util import err, ok

# id -> Connection, in configuration order.
_connections = {}
# local tool name -> {server, tool, capability_id, ...}
_registered = {}
_lock = threading.RLock()
_state = {"started": False, "starting": False, "report": None,
          "started_at": None}


def _registry():
    from . import registry
    return registry


def _risk():
    from . import risk
    return risk


# --------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------

_UNSAFE = re.compile(r"[^0-9a-zA-Z_]+")


def _sanitise(text):
    """A Python identifier from an arbitrary MCP tool name."""
    cleaned = _UNSAFE.sub("_", str(text or "")).strip("_")
    if not cleaned:
        cleaned = "tool"
    if cleaned[0].isdigit():
        cleaned = "t_" + cleaned
    return cleaned[:48]


def capability_id(server, tool):
    """The stable internal identifier for an MCP tool."""
    return "mcp:" + str(server) + ":" + str(tool)


def _local_name(server, tool):
    """The name the model calls this tool by.

    The bare tool name is tried first, because `create_issue` reads better to a
    model than `mcp_github_create_issue` and shorter names measurably help tool
    selection. A collision with anything already registered - a built-in, a
    plugin, or another server's tool - falls back to a qualified form rather
    than displacing what is there.
    """
    base = _sanitise(tool)
    taken = _registry()._tools().REGISTRY
    for candidate in (base,
                      _sanitise(server) + "_" + base,
                      "mcp_" + _sanitise(server) + "_" + base):
        if candidate not in taken:
            return candidate, candidate != base
    # Three collisions is pathological; disambiguate and move on.
    return "mcp_" + _sanitise(server) + "_" + base + "_" + str(len(taken)), True


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------

_TIERS = ("safe", "low", "medium", "high", "critical")

# Verbs in a tool name that describe an action on the world. A server is not
# obliged to annotate its tools, and most do not, so the name is the only
# signal left. Erring high here costs a confirmation prompt; erring low costs
# an unreviewed side effect.
_DANGEROUS_NAME = re.compile(
    r"(?i)(^|_)(delete|remove|destroy|drop|purge|erase|truncate|kill|"
    r"send|post|publish|deploy|merge|push|execute|exec|run|shell|command|"
    r"install|uninstall|revoke|transfer|pay|charge|purchase)(_|$)")

_WRITING_NAME = re.compile(
    r"(?i)(^|_)(create|write|update|edit|modify|rename|move|copy|upload|"
    r"set|add|append|insert|patch|save|commit)(_|$)")

_READING_NAME = re.compile(
    r"(?i)(^|_)(get|list|read|search|find|fetch|query|show|describe|view|"
    r"inspect|status|info|count|resolve)(_|$)")


def _annotation(annotations, *names):
    for name in names:
        if name in annotations:
            return annotations[name]
    return None


def grade(tool, server_config):
    """The risk tier for one MCP tool, and whether it must be confirmed.

    Returns (tier, requires_confirmation, why).
    """
    annotations = tool.get("annotations") or {}
    baseline = server_config.get("risk_level") or "medium"
    if baseline not in _TIERS:
        baseline = "high"
    tier = baseline
    why = "the server's configured risk level"

    read_only = _annotation(annotations, "read_only_hint", "readOnlyHint")
    destructive = _annotation(annotations, "destructive_hint", "destructiveHint")

    name = tool.get("name", "")

    if destructive is True:
        tier = "high"
        why = "the server marked this tool destructive"
    elif _DANGEROUS_NAME.search(name):
        if _TIERS.index(tier) < _TIERS.index("high"):
            tier = "high"
        why = "the tool name describes an action that changes things outside Jarvish"
    elif _WRITING_NAME.search(name):
        if _TIERS.index(tier) < _TIERS.index("medium"):
            tier = "medium"
            why = "the tool name describes a write"
    elif read_only is True or (_READING_NAME.search(name) and destructive is not True):
        # Lowered at most to `low`, never to `safe`. A `safe` tool runs
        # concurrently with others and never prompts, and that privilege is not
        # granted on an external server's say-so.
        floor = "low"
        if _TIERS.index(tier) > _TIERS.index(floor):
            tier = floor
            why = ("the server marked this tool read-only" if read_only is True
                   else "the tool name describes a read")

    # Lowering is a convenience for the common case where the tier was never
    # stated. Once somebody has written `"riskLevel": "high"` against a server,
    # a tool named `read_something` must not quietly come out below it — that
    # would let the tool's own name overrule the operator.
    if (server_config.get("risk_level_explicit")
            and _TIERS.index(tier) < _TIERS.index(baseline)):
        tier = baseline
        why = "the server is configured as " + baseline + " risk"

    explicit = server_config.get("requires_confirmation")
    if explicit is not None:
        confirm = bool(explicit)
        if confirm:
            why += "; the server is configured to always confirm"
    else:
        # The default: anything from an external server that changes state is
        # shown to the user first. Read-only tools are exempt, and the whole
        # behaviour is switchable with JARVISH_REQUIRE_CONFIRMATION=0.
        confirm = (REQUIRE_CONFIRMATION
                   and _TIERS.index(tier) >= _TIERS.index("medium"))
    return tier, confirm, why


def _permissions(tool, server_config):
    """The permission scopes to declare, which set a floor on the tier."""
    scopes = ["network"] if server_config["transport"] == "http" else []
    name = tool.get("name", "")
    schema = tool.get("input_schema") or {}
    properties = set((schema.get("properties") or {}).keys())

    # The same list the path guard uses, rather than a second copy of it. They
    # had drifted: `read_file(path)` earned the filesystem scope and the
    # `medium` floor that comes with it, while `read_multiple_files(paths)`
    # earned neither and was graded *below* the tool that can do less.
    if properties & {name.lower() for name in security._PATH_ARGUMENTS}:
        scopes.append("filesystem")
    if _WRITING_NAME.search(name) or _DANGEROUS_NAME.search(name):
        scopes.append("write")
    return tuple(dict.fromkeys(scopes)) or ("read",)


# --------------------------------------------------------------------------
# The handler
# --------------------------------------------------------------------------

def _handler_for(server_name, tool_name, schema, roots):
    """Build the callable Jarvish registers for one MCP tool.

    Everything a call must survive happens here, in order: the connection has
    to exist and be ready, the arguments have to match the schema, path
    arguments have to be inside the server's roots, and only then does anything
    cross the process boundary. The result comes back already wrapped as
    untrusted content by the client.
    """

    def call(**arguments):
        connection = _connections.get(server_name)
        if connection is None:
            return errors.fail(errors.SERVER_UNAVAILABLE,
                               "The '" + server_name + "' server is not configured.",
                               server=server_name)
        if not connection.ready:
            return errors.fail(
                errors.SERVER_UNAVAILABLE,
                "The '" + server_name + "' server is " +
                connection.state.lower() + ".",
                server=server_name, state=connection.state)

        # `confirm` is Jarvish's own flag, added by the risk gate when the user
        # approves a call. It is never part of the server's schema, so it must
        # not be forwarded or it fails validation on the far side.
        arguments = {k: v for k, v in arguments.items() if k != "confirm"}

        valid, why = security.validate_arguments(schema, arguments)
        if not valid:
            return errors.fail(errors.INVALID_ARGUMENTS, why,
                               server=server_name, tool=tool_name)

        blocked = security.guard_arguments(arguments, roots=roots or None)
        if blocked:
            return blocked

        return connection.call(tool_name, arguments)

    call.__name__ = "mcp_" + _sanitise(server_name) + "_" + _sanitise(tool_name)
    call.__doc__ = "MCP tool " + capability_id(server_name, tool_name)
    return call


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

def _schema_properties(schema):
    """The JSON-schema properties and required list, in Jarvish's shape."""
    properties = {}
    for key, spec in (schema.get("properties") or {}).items():
        if not isinstance(spec, dict):
            continue
        entry = {"type": spec.get("type", "string")}
        if isinstance(entry["type"], list):
            entry["type"] = next((t for t in entry["type"] if t != "null"), "string")
        description = spec.get("description") or spec.get("title") or key
        entry["description"] = str(description)[:300]
        if isinstance(spec.get("enum"), list) and spec["enum"]:
            entry["enum"] = [str(v) for v in spec["enum"][:20]]
        properties[key] = entry
    required = [str(r) for r in (schema.get("required") or [])
                if r in properties]
    return properties, required


def _register_tools(connection):
    """Register everything one connected server offers. Returns a report."""
    config = connection.config
    allowed = set(config["allowed_tools"])
    blocked = set(config["blocked_tools"])
    added, skipped = [], []

    for tool in connection.tools:
        name = tool["name"]
        if allowed and name not in allowed:
            skipped.append({"tool": name, "reason": "not in allowedTools"})
            continue
        if name in blocked:
            skipped.append({"tool": name, "reason": "in blockedTools"})
            continue
        with _lock:
            if len(_registered) >= MCP_MAX_TOOLS:
                skipped.append({"tool": name, "reason": "MCP tool limit reached"})
                continue

        local, qualified = _local_name(config["name"], name)
        tier, confirm, why = grade(tool, config)
        schema = tool.get("input_schema") or {}
        properties, required = _schema_properties(schema)

        description = tool.get("description") or ("The " + name + " tool.")
        # The model is told where a capability comes from. This is not for
        # routing - it must not need to care - but so an answer can honestly
        # say "according to the github server" rather than implying Jarvish
        # knew it directly.
        description = (description.strip()[:400] + "  (via the " +
                       config["name"] + " MCP server)")

        handler = _handler_for(config["name"], name, schema, config["roots"])

        outcome = _registry().register_tool(
            local, description, handler,
            parameters=properties, required=required,
            risk_level=tier, reversible=(tier in ("safe", "low")),
            permissions=_permissions(tool, config),
            version=str(connection.server_info.get("version") or "1.0"),
            module="mcp:" + config["name"],
            source="mcp",
            outputs=["result"],
        )
        if not outcome.get("ok"):
            skipped.append({"tool": name, "reason": outcome.get("error")})
            continue

        # The registry applies its own floor: a capability that asked for
        # `filesystem` or `system` access cannot be graded below what that
        # implies. Read the tier back rather than keeping the one requested, so
        # what gets displayed is always the tier that is actually enforced.
        enforced = _risk().level(local)
        if enforced != tier:
            why += ("; raised to " + enforced +
                    " by the permissions it needs")
            tier = enforced
            # The confirmation default keys off the tier, so a tier that moved
            # after grading has to be re-asked. Left alone, a tool the registry
            # had just raised to `medium` would keep the answer computed while
            # it was still `low`, and run without a prompt.
            if config.get("requires_confirmation") is None:
                confirm = (REQUIRE_CONFIRMATION
                           and _TIERS.index(tier) >= _TIERS.index("medium"))

        if confirm:
            _risk().declare_confirmation(
                local, True,
                why=("This runs on the '" + config["name"] + "' MCP server, "
                     "outside Jarvish. " + why[0].upper() + why[1:] + "."))

        record = {
            "id": capability_id(config["name"], name),
            "registry_id": outcome.get("registered"),
            "server": config["name"],
            "tool": name,
            "local_name": local,
            "qualified": qualified,
            "risk": tier,
            "requires_confirmation": confirm,
            "grading": why,
            "input_schema": schema,
            "output_schema": tool.get("output_schema"),
            "description": description,
            "timeout": config["request_timeout"],
            "permissions": list(_permissions(tool, config)),
        }
        with _lock:
            _registered[local] = record
        added.append(record)

    return {"added": added, "skipped": skipped}


def _unregister_server(server_name):
    """Remove every capability a server contributed."""
    removed = []
    with _lock:
        mine = [name for name, record in _registered.items()
                if record["server"] == server_name]
    for local in mine:
        with _lock:
            record = _registered.pop(local, None)
        if record is None:
            continue
        try:
            _registry().unregister(record["registry_id"])
        except Exception:
            pass
        try:
            _risk().declare_confirmation(local, False)
        except Exception:
            pass
        removed.append(local)
    return removed


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------

def _connect_one(connection):
    """Connect one server and register what it offers. Never raises."""
    started = time.perf_counter()
    try:
        connected, why = connection.connect()
    except Exception as exc:
        connected, why = False, str(exc)[:200]

    elapsed = round((time.perf_counter() - started) * 1000)
    if not connected:
        observability.emit(observability.MCP_ERROR, tool=connection.name,
                           server=connection.name, ok=False, ms=elapsed,
                           error_category=connection.error_category or
                           errors.SERVER_UNAVAILABLE,
                           detail=str(why)[:200])
        return {"server": connection.name, "connected": False, "error": why,
                "tools": 0}

    try:
        report = _register_tools(connection)
    except Exception as exc:
        return {"server": connection.name, "connected": True,
                "error": "Registration failed: " + str(exc)[:160], "tools": 0}

    return {"server": connection.name, "connected": True, "error": None,
            "ms": elapsed,
            "tools": len(report["added"]), "skipped": report["skipped"],
            "registered": [r["local_name"] for r in report["added"]]}


def start(background=True, path=None):
    """Load the config and bring up every enabled server.

    With `background=True` this returns as soon as the work is scheduled, which
    is what keeps MCP off Jarvish's startup path: a server that takes twenty
    seconds to install itself through npx delays nothing.
    """
    with _lock:
        if _state["starting"]:
            # Same shape as every other return from `start()`, so a caller can
            # read `servers` without checking which path it came back through.
            return ok(status="already starting", servers=[], rejected=[])
        _state["starting"] = True

    report = mcp_config.load(path)
    _state["report"] = report

    if not report["configured"] or not report["servers"]:
        with _lock:
            _state["starting"] = False
            _state["started"] = True
            _state["started_at"] = time.time()
        # The quiet, ordinary outcome: nobody configured a server, so MCP does
        # nothing at all and Jarvish is exactly what it was.
        return ok(status="no servers configured", configured=report["configured"],
                  path=report["path"], error=report["error"],
                  rejected=report["rejected"])

    installed, why = mcp_client.available()
    if not installed:
        with _lock:
            _state["starting"] = False
            _state["started"] = True
        return err(why)

    enabled = [s for s in report["servers"] if s["enabled"]]
    with _lock:
        for config in report["servers"]:
            if config["name"] not in _connections:
                _connections[config["name"]] = mcp_client.Connection(config)

    def run():
        # `starting` is cleared in a finally rather than at the end of the
        # happy path. It is the flag that makes `start()` refuse a second
        # call, so anything that leaves it set - a thread killed part way, an
        # unexpected raise from a connection object - would wedge the whole
        # MCP layer until the next shutdown, with no way to see why.
        results = []
        try:
            for config in enabled:
                connection = _connections.get(config["name"])
                if connection is None:
                    continue
                results.append(_connect_one(connection))
        finally:
            with _lock:
                _state["starting"] = False
                _state["started"] = True
                _state["started_at"] = time.time()
                _state["results"] = results
        return results

    if not background:
        results = run()
        return ok(status="connected", servers=results,
                  rejected=report["rejected"])

    threading.Thread(target=run, name="jarvish-mcp-start", daemon=True).start()
    return ok(status="connecting", servers=[s["name"] for s in enabled],
              rejected=report["rejected"])


def shutdown():
    """Close every connection and stop the runtime thread.

    Called from the server's shutdown hook. Getting this right is what stops
    orphaned `npx` processes accumulating across restarts.
    """
    names = list(_connections)
    for name in names:
        try:
            _unregister_server(name)
        except Exception:
            pass
        try:
            _connections[name].disconnect()
        except Exception:
            pass
    with _lock:
        _connections.clear()
        _registered.clear()
        _state["started"] = False
        _state["starting"] = False
    try:
        mcp_client.RUNTIME.stop()
    except Exception:
        pass
    return ok(stopped=names)


def reload(path=None):
    """Re-read `mcp.json` and reconnect from scratch."""
    shutdown()
    return start(background=False, path=path)


def connect_server(name):
    """Connect (or reconnect) one server by name."""
    connection = _connections.get(name)
    if connection is None:
        report = _state.get("report") or mcp_config.load()
        config = next((s for s in report["servers"] if s["name"] == name), None)
        if config is None:
            return err("No MCP server called '" + str(name) + "' is configured.")
        connection = mcp_client.Connection(config)
        _connections[name] = connection
    if connection.ready:
        return ok(server=name, state=connection.state, already=True,
                  tools=len(connection.tools))
    _unregister_server(name)
    outcome = _connect_one(connection)
    if not outcome["connected"]:
        return err("Could not connect to '" + name + "': " + str(outcome["error"]))
    return ok(server=name, state=connection.state, tools=outcome["tools"],
              registered=outcome.get("registered", []))


def disconnect_server(name):
    """Disconnect one server and withdraw its capabilities."""
    connection = _connections.get(name)
    if connection is None:
        return err("No MCP server called '" + str(name) + "' is connected.")
    removed = _unregister_server(name)
    connection.disconnect()
    return ok(server=name, state=connection.state, withdrew=removed)


# --------------------------------------------------------------------------
# Introspection
# --------------------------------------------------------------------------

def status():
    """Everything about the MCP layer, safe to publish and free of secrets."""
    report = _state.get("report") or {"path": str(mcp_config.config_path()),
                                      "configured": False, "rejected": [],
                                      "error": None}
    servers = [connection.status() for connection in _connections.values()]
    installed, why = mcp_client.available()
    return {
        "ok": True,
        "enabled": MCP_ENABLED,
        "sdk_installed": installed,
        "sdk_error": None if installed else why,
        "configured": report.get("configured", False),
        "config_path": report.get("path"),
        "config_error": report.get("error"),
        "rejected": report.get("rejected", []),
        "started": _state["started"],
        "starting": _state["starting"],
        "servers": servers,
        "server_count": len(servers),
        "ready_count": sum(1 for s in servers if s["ready"]),
        "tool_count": len(_registered),
        "tools": sorted(_registered),
        "runtime_thread": mcp_client.RUNTIME.running,
    }


def health():
    """A short verdict per server, for the health endpoint."""
    return {name: {"state": connection.state, "ready": connection.ready,
                   "tools": len(connection.tools),
                   "error": security.redact(connection.error)
                   if connection.error else None,
                   "calls": connection.calls, "failures": connection.failures}
            for name, connection in _connections.items()}


def listing():
    """Every registered MCP capability, with the metadata the registry holds."""
    with _lock:
        records = list(_registered.values())
    rows = []
    for record in records:
        connection = _connections.get(record["server"])
        rows.append({
            "id": record["id"],
            "name": record["local_name"],
            # The name the server itself knows this tool by. It is usually the
            # same as `name`, and differs whenever a collision with a built-in
            # forced a qualified local name - so anything reporting on a server
            # needs it, and should not have to take the id apart to get it.
            "tool": record["tool"],
            "qualified": record["qualified"],
            "description": record["description"],
            "source": "mcp",
            "server": record["server"],
            "input_schema": record["input_schema"],
            "output_schema": record["output_schema"],
            "risk_level": record["risk"],
            "requires_confirmation": record["requires_confirmation"],
            "why_graded": record["grading"],
            "enabled": bool(connection and connection.ready),
            "timeout": record["timeout"],
            "permissions": record["permissions"],
            "server_state": connection.state if connection else "DISCONNECTED",
        })
    rows.sort(key=lambda r: (r["server"], r["name"]))
    return rows


def count_registered():
    """How many MCP capabilities are currently registered."""
    with _lock:
        return len(_registered)


def record_for(local_name):
    """The MCP record behind a registered tool name, or None."""
    with _lock:
        return _registered.get(local_name)


def is_mcp_tool(name):
    with _lock:
        return name in _registered


def resources():
    """Every resource offered by every connected server."""
    rows = []
    for name, connection in _connections.items():
        for resource in connection.resources:
            rows.append(dict(resource, server=name,
                             id="mcp:" + name + ":resource:" + resource["uri"]))
    return rows


def prompts():
    """Every prompt offered by every connected server."""
    rows = []
    for name, connection in _connections.items():
        for prompt in connection.prompts:
            rows.append(dict(prompt, server=name,
                             id="mcp:" + name + ":prompt:" + prompt["name"]))
    return rows


def read_resource(uri, server=None):
    """Read one MCP resource. The body comes back as untrusted content."""
    if server:
        connection = _connections.get(server)
        if connection is None:
            return err("No MCP server called '" + str(server) + "'.")
        return connection.read_resource(uri)
    for connection in _connections.values():
        if any(r["uri"] == uri for r in connection.resources):
            return connection.read_resource(uri)
    return errors.fail(errors.TOOL_NOT_FOUND,
                       "No connected MCP server offers " + str(uri) + ".")


def capability_groups():
    """Selector groups for the connected servers.

    `capabilities.select` sends the model a focused subset of the tool list each
    turn, chosen by matching the request against a group's trigger words. MCP
    tools arrive after that table was written, so each server contributes a
    group here: its own name, its tools' names broken into words, and the
    distinctive words from their descriptions. Without this an MCP tool could
    only be found by the weaker description-scoring fallback, and a server whose
    tools are named unlike the request would effectively be invisible.
    """
    groups = {}
    with _lock:
        records = list(_registered.values())
    for record in records:
        connection = _connections.get(record["server"])
        if connection is None or not connection.ready:
            continue
        key = "mcp:" + record["server"]
        group = groups.setdefault(key, {"tools": [], "triggers": set()})
        group["tools"].append(record["local_name"])
        group["triggers"].add(record["server"].lower())
        for word in re.split(r"[^a-z0-9]+", record["tool"].lower()):
            if len(word) > 3:
                group["triggers"].add(word)
        for word in re.split(r"[^a-z0-9]+", record["description"].lower())[:40]:
            if len(word) > 4 and word not in _COMMON:
                group["triggers"].add(word)
    return {key: {"tools": tuple(value["tools"]),
                  "triggers": tuple(sorted(value["triggers"])[:60])}
            for key, value in groups.items()}


# Words too common to be useful as triggers - matching them would pull a
# server's whole tool list in on almost any request.
_COMMON = {
    "server", "tools", "tool", "which", "there", "their", "about", "would",
    "could", "should", "these", "those", "where", "while", "return", "returns",
    "given", "using", "value", "values", "string", "number", "object", "array",
    "optional", "required", "default", "specified", "provided", "example",
    "jarvish",
}


# --------------------------------------------------------------------------
# Tools the model and the CLI can call
# --------------------------------------------------------------------------

def mcp_status():
    """A compact status summary."""
    full = status()
    return ok(
        enabled=full["enabled"],
        configured=full["configured"],
        servers=[{"name": s["name"], "state": s["state"], "tools": s["tool_count"],
                  "error": s["error"]} for s in full["servers"]],
        ready=full["ready_count"], total=full["server_count"],
        capabilities=full["tool_count"],
        note=("No MCP servers are configured, so Jarvish is running with its "
              "built-in capabilities only."
              if not full["configured"] else None),
    )


def mcp_tools(server=None):
    """List the capabilities borrowed from MCP servers."""
    rows = listing()
    if server:
        rows = [r for r in rows if r["server"] == server]
    return ok(tools=[{"id": r["id"], "name": r["name"], "server": r["server"],
                      "risk": r["risk_level"],
                      "confirm": r["requires_confirmation"],
                      "description": r["description"][:160]} for r in rows],
              count=len(rows))


def mcp_reload():
    """Re-read the configuration and reconnect every server."""
    outcome = reload()
    return outcome if isinstance(outcome, dict) else ok(reloaded=True)


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "mcp_status",
            "description": ("Report which Model Context Protocol servers are "
                            "connected and how many extra capabilities they "
                            "provide."),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_tools",
            "description": ("List the capabilities borrowed from MCP servers, "
                            "optionally for one server."),
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {"type": "string",
                               "description": "Limit to one server by name."},
                },
                "required": [],
            },
        },
    },
]

REGISTRY = {
    "mcp_status": mcp_status,
    "mcp_tools": mcp_tools,
}
