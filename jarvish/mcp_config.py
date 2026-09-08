"""Reading `mcp.json`: which MCP servers exist, and on what terms.

The file format is the one every other MCP host uses, so a server block copied
from a project's README works here unchanged:

    {
      "mcpServers": {
        "filesystem": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/me/Documents"],
          "env": {}
        }
      }
    }

Jarvish adds optional keys alongside those - `enabled`, the three timeouts,
`riskLevel`, `requiresConfirmation`, `allowedTools`, `roots` - and ignores keys
it does not know, because a config written for another host must not be an
error here.

Three decisions are worth stating.

**Secrets are named, never written.** `"env": {"GITHUB_TOKEN": "ghp_..."}` in a
file that lives beside the source is how tokens end up in a repository, so a
value of the form `${NAME}` is read from the process environment at connect
time. A literal-looking secret is still accepted - it is the user's file - but
it is redacted everywhere it is subsequently reported.

**A bad block is skipped, not fatal.** One server with a typo must not stop the
other four loading, and must not stop Jarvish starting. Every rejection is
kept with its reason and surfaced by `mcp status`.

**Absent means off.** No file, or a file with no servers, and MCP does not
start at all. That is the guarantee that installing this changed nothing for
anyone who does not configure a server.
"""

import json
import os
import re
from pathlib import Path

from . import security
from .config import (MCP_CONFIG_PATH, MCP_CONNECTION_TIMEOUT, MCP_ENABLED,
                     MCP_MAX_SERVERS, MCP_REQUEST_TIMEOUT, MCP_STARTUP_TIMEOUT)

# A server name has to survive being pasted into a capability id, a log line
# and a tool name, so it is kept to the characters that are safe in all three.
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$", re.IGNORECASE)

# ${VAR} and ${VAR:-default}, the two forms people actually write.
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# Risk tiers a server block may claim for its tools. Mirrors risk.ORDER; a
# value outside it is corrected upwards rather than rejected, because the safe
# failure for an unrecognised risk level is to treat it as dangerous.
_TIERS = ("safe", "low", "medium", "high", "critical")

DEFAULT_RISK = "medium"


def config_path():
    """Where `mcp.json` is looked for."""
    if MCP_CONFIG_PATH:
        return Path(os.path.expandvars(os.path.expanduser(MCP_CONFIG_PATH)))
    return Path(__file__).resolve().parent.parent / "mcp.json"


def _expand(value, environment=None):
    """Resolve ${VAR} placeholders from the real environment.

    Returns (text, missing). An unresolvable placeholder is reported rather
    than silently becoming an empty string, because an MCP server handed an
    empty token fails much later and much less clearly.
    """
    missing = []
    source = environment if environment is not None else os.environ

    def replace(match):
        name, fallback = match.group(1), match.group(2)
        if name in source:
            return source[name]
        if fallback is not None:
            return fallback
        missing.append(name)
        return ""

    return _PLACEHOLDER.sub(replace, str(value)), missing


def _number(block, key, default, low, high):
    try:
        value = float(block.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


def _parse_server(name, block, environment=None):
    """Turn one config block into a server record. Returns (record, error)."""
    if not _NAME.match(str(name)):
        return None, ("Server name '" + str(name)[:40] + "' is not usable. Use "
                      "letters, digits, dashes and underscores.")
    if not isinstance(block, dict):
        return None, "Server '" + name + "' must be an object."

    url = str(block.get("url") or block.get("httpUrl") or "").strip()
    command = str(block.get("command") or "").strip()

    if url and command:
        return None, ("Server '" + name + "' sets both 'command' and 'url'. "
                      "Pick one transport.")
    if not url and not command:
        return None, ("Server '" + name + "' needs either a 'command' (stdio) "
                      "or a 'url' (streamable HTTP).")

    missing = []
    transport = "http" if url else "stdio"

    if transport == "http":
        resolved_url, gaps = _expand(url, environment)
        missing.extend(gaps)
        if not re.match(r"^https?://", resolved_url):
            return None, ("Server '" + name + "' has a url that is not http "
                          "or https.")
        args, env, cwd = [], {}, None
        resolved_command = None
    else:
        resolved_command, gaps = _expand(command, environment)
        missing.extend(gaps)
        allowed, why = security.check_launcher(
            resolved_command, block.get("allowedLaunchers"))
        if not allowed:
            return None, "Server '" + name + "': " + why

        args = []
        for argument in block.get("args") or []:
            text, gaps = _expand(argument, environment)
            missing.extend(gaps)
            args.append(text)
        safe, why = security.check_arguments_safe(args)
        if not safe:
            return None, "Server '" + name + "': " + why

        env = {}
        for key, value in (block.get("env") or {}).items():
            text, gaps = _expand(value, environment)
            missing.extend(gaps)
            env[str(key)] = text

        cwd = block.get("cwd")
        if cwd:
            cwd, gaps = _expand(cwd, environment)
            missing.extend(gaps)
        resolved_url = None

    declared_tier = block.get("riskLevel")
    tier = str(declared_tier or DEFAULT_RISK).strip().lower()
    if tier not in _TIERS:
        tier = "high"

    # `requiresConfirmation` may be omitted, in which case the risk gate alone
    # decides. Setting it true is a way to demand a prompt for tools the gate
    # would otherwise let through - it can add caution, never remove it.
    confirmation = block.get("requiresConfirmation")

    record = {
        "name": str(name),
        "transport": transport,
        "command": resolved_command,
        "args": args,
        "env": env,
        "cwd": str(cwd) if cwd else None,
        "url": resolved_url,
        "headers": {k: _expand(v, environment)[0]
                    for k, v in (block.get("headers") or {}).items()},
        "enabled": block.get("enabled", True) is not False,
        "description": str(block.get("description") or "").strip(),
        "risk_level": tier,
        # Whether a person wrote that tier down, as opposed to it being the
        # default. Grading may lower a read-only tool below the default, but
        # never below a level somebody chose on purpose.
        "risk_level_explicit": bool(declared_tier),
        "requires_confirmation": (None if confirmation is None
                                  else bool(confirmation)),
        # An empty list means "every tool this server offers". A populated one
        # is an allowlist, and anything outside it is never registered.
        "allowed_tools": [str(t) for t in (block.get("allowedTools") or [])],
        "blocked_tools": [str(t) for t in (block.get("blockedTools") or [])],
        # Folders a filesystem-shaped server may touch. Enforced by Jarvish on
        # every path-shaped argument, whatever the server itself believes.
        "roots": [str(r) for r in (block.get("roots") or [])],
        "startup_timeout": _number(block, "startupTimeout",
                                   MCP_STARTUP_TIMEOUT, 1.0, 300.0),
        "connection_timeout": _number(block, "connectionTimeout",
                                      MCP_CONNECTION_TIMEOUT, 1.0, 300.0),
        "request_timeout": _number(block, "requestTimeout",
                                   MCP_REQUEST_TIMEOUT, 1.0, 900.0),
        "auto_reconnect": block.get("autoReconnect", True) is not False,
        "max_reconnect_attempts": int(_number(block, "maxReconnectAttempts",
                                              3, 0, 20)),
        "missing_env": sorted(set(missing)),
    }
    return record, None


def redacted(record):
    """A server record safe to log, return over the API or show the model."""
    return dict(
        record,
        env=security.redact_env(record.get("env")),
        headers=security.redact_env(record.get("headers")),
        url=security.redact(record.get("url")),
    )


def load(path=None, environment=None):
    """Read and validate the configuration.

    Never raises. A missing file is a normal, quiet outcome - it is what most
    installations will have - and is reported as `configured: False` rather
    than as an error anybody needs to read.
    """
    target = Path(path) if path else config_path()
    report = {
        "path": str(target),
        "configured": False,
        "enabled": MCP_ENABLED,
        "servers": [],
        "rejected": [],
        "error": None,
    }

    if not MCP_ENABLED:
        report["error"] = "MCP is switched off (JARVISH_MCP_ENABLED=0)."
        return report

    if not target.exists():
        return report

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report["error"] = ("mcp.json is not valid JSON: line " +
                           str(exc.lineno) + ", " + exc.msg)
        return report
    except Exception as exc:
        report["error"] = "Could not read mcp.json: " + str(exc)
        return report

    if not isinstance(raw, dict):
        report["error"] = "mcp.json must contain a JSON object."
        return report

    blocks = raw.get("mcpServers")
    if blocks is None:
        blocks = raw.get("servers")
    if not isinstance(blocks, dict):
        report["error"] = "mcp.json has no 'mcpServers' object."
        return report

    report["configured"] = True

    for name, block in blocks.items():
        if len(report["servers"]) >= MCP_MAX_SERVERS:
            report["rejected"].append({
                "name": str(name),
                "error": ("Server limit reached (" + str(MCP_MAX_SERVERS) +
                          "). Raise JARVISH_MCP_MAX_SERVERS to add more."),
            })
            continue
        record, why = _parse_server(name, block, environment)
        if why:
            report["rejected"].append({"name": str(name), "error": why})
            continue
        report["servers"].append(record)

    return report


EXAMPLE = {
    "mcpServers": {
        "filesystem": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem",
                     "${USERPROFILE}/Documents"],
            "description": "Read and write files in Documents.",
            "riskLevel": "medium",
            "roots": ["${USERPROFILE}/Documents"],
            "enabled": True,
        },
        "github": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"},
            "description": "Issues and pull requests.",
            "riskLevel": "high",
            "requiresConfirmation": True,
            "enabled": False,
        },
    }
}
