"""The capability registry, and the selector that keeps the tool list usable.

Jarvish has 80 tools. A local model cannot choose well from 80 schemas — this
was measured against qwen3:8b, which selects correctly with 40 tools in the
list and stops calling tools at all by 60. The failure is silent: the model
simply answers "I cannot access that" instead of reaching for the tool sitting
right in front of it.

So the whole registry is never sent at once. Each turn, this module scores every
capability against what the user actually asked for and sends a focused subset:

    request  ->  capability matching  ->  a short, relevant tool list

A small core is always present so the assistant is never left unable to do the
obvious thing, and tools already used this session stay available so follow-ups
like "do that again" still resolve.

`describe()` exposes the same registry as structured metadata — name, module,
risk, reversibility, arguments — which is what makes new capabilities
discoverable without touching the agent loop.
"""

import re

from . import risk, tools

# How many schemas may go to the model in one turn. Comfortably under the point
# where selection quality collapses, with headroom for weaker models.
BUDGET = 30

# Always available, whatever was asked. These are the things a user expects to
# work without preamble, plus the memory tools the system prompt relies on.
CORE = (
    "get_time", "system_info", "help_overview",
    "recall", "remember", "add_instruction",
    "open_app", "open_url", "web_search",
)

# Which module a tool belongs to, and the words that should summon that module.
# Matching any trigger pulls in the whole group, because a request about tabs
# usually needs several browser tools rather than one.
GROUPS = {
    "browser": {
        "tools": ("browser_status", "browser_launch", "browser_tabs", "browser_open",
                  "browser_read", "browser_find", "browser_search_page", "browser_click",
                  "browser_type", "browser_clear", "browser_check", "browser_select",
                  "browser_submit", "browser_scroll", "browser_new_tab",
                  "browser_close_tab", "browser_switch_tab", "browser_download"),
        "triggers": ("browser", "tab", "tabs", "page", "website", "site", "url", "link",
                     "form", "click", "navigate", "chrome", "web page", "webpage",
                     "fill", "submit", "download", "scroll", "login", "log in",
                     "sign in", "portfolio", "deployment", "deploy"),
    },
    "vision": {
        "tools": ("look_at_screen", "read_screen_text", "find_on_screen",
                  "click_on_screen", "describe_active_window", "vision_status"),
        "triggers": ("screen", "see", "look", "looking", "visible", "what am i",
                     "this error", "read this", "what's wrong", "whats wrong",
                     "show me", "on my display", "window", "screenshot", "ocr",
                     "point", "image", "picture"),
    },
    "knowledge": {
        "tools": ("search_knowledge", "index_folder", "knowledge_status", "find_symbol",
                  "related_files", "project_overview", "forget_folder"),
        "triggers": ("file", "files", "code", "codebase", "project", "function",
                     "class", "module", "import", "repository", "repo", "document",
                     "documents", "pdf", "note", "notes", "indexed", "index",
                     "search my", "find everything", "where is", "wrote", "written",
                     "symbol", "defined", "implementation", "architecture"),
    },
    "files": {
        "tools": ("find_files", "list_directory", "read_text_file"),
        "triggers": ("folder", "directory", "downloads", "desktop", "file",
                     "files", "pdf", "read", "path", "disk"),
    },
    "network": {
        "tools": ("wifi_status", "wifi_networks", "wifi_saved_networks", "wifi_connect",
                  "wifi_disconnect", "wifi_power", "bluetooth_devices", "bluetooth_power",
                  "network_info"),
        "triggers": ("wifi", "wi-fi", "network", "internet", "bluetooth", "connection",
                     "connected", "offline", "online", "signal", "router"),
    },
    "messaging": {
        "tools": ("whatsapp_message", "compose_email", "open_inbox", "open_phone_link",
                  "phone_access"),
        "triggers": ("whatsapp", "email", "mail", "inbox", "message", "text",
                     "send", "reply", "phone", "contact"),
    },
    "live": {
        "tools": ("get_weather", "get_news", "read_web_page", "convert_currency",
                  "daily_briefing"),
        "triggers": ("weather", "news", "today", "forecast", "temperature", "rain",
                     "currency", "exchange", "rate", "brief", "briefing", "headlines"),
    },
    "desktop": {
        "tools": ("open_settings", "power_action", "set_brightness", "get_brightness",
                  "take_screenshot", "type_text", "list_windows", "focus_window",
                  "close_app", "lock_screen", "media_control", "list_processes"),
        "triggers": ("brightness", "volume", "mute", "screenshot", "lock", "shutdown",
                     "restart", "sleep", "window", "windows", "close", "settings",
                     "play", "pause", "music", "process", "processes", "task manager",
                     "cpu", "memory", "ram", "battery"),
    },
    "webapps": {
        "tools": ("open_web_app", "list_web_apps"),
        "triggers": ("youtube", "netflix", "gmail", "maps", "spotify", "instagram",
                     "twitter", "github", "open", "app", "apps", "website"),
    },
    "personal": {
        "tools": ("remember", "recall", "forget", "add_instruction",
                  "list_instructions", "remove_instruction"),
        "triggers": ("remember", "forget", "my name", "favourite", "favorite",
                     "call me", "always", "prefer", "profile", "about me"),
    },
    "tasks": {
        "tools": ("schedule_task", "list_tasks", "task_status", "pause_task",
                  "resume_task", "cancel_task", "retry_task"),
        "triggers": ("task", "tasks", "background", "schedule", "later", "remind",
                     "reminder", "every", "keep an eye", "monitor", "watch",
                     "recurring", "repeat", "in minutes", "in an hour", "queue",
                     "pause", "resume", "cancel"),
    },
    "proactive": {
        "tools": ("system_insights", "notifications", "proactive_settings",
                  "dismiss_notifications"),
        "triggers": ("notification", "notifications", "alert", "alerts", "quiet",
                     "do not disturb", "disturb", "insight", "insights", "warn",
                     "warnings", "interrupt", "notify", "anything wrong",
                     "needs attention"),
    },
    "dev": {
        "tools": ("project_info", "project_architecture", "find_callers",
                  "find_dependents", "diagnose_error", "propose_change",
                  "apply_change", "revert_change", "run_dev_command",
                  "run_tests", "run_build", "git_status", "git_diff", "git_log",
                  "git_branches", "propose_commit", "remember_project",
                  "project_notes"),
        "triggers": ("error", "traceback", "exception", "stack trace", "bug",
                     "failing", "fails", "crash", "crashed", "broken", "fix",
                     "test", "tests", "pytest", "build", "lint", "compile",
                     "git", "commit", "branch", "diff", "merge", "repository",
                     "refactor", "dependency", "dependencies", "install",
                     "npm", "pip", "calls", "callers", "imports", "architecture",
                     "codebase", "project", "implemented", "why is this",
                     "not working", "debug"),
    },
    "missions": {
        "tools": ("start_mission", "mission_status", "list_missions",
                  "pause_mission", "resume_mission", "cancel_mission",
                  "retry_mission_task"),
        "triggers": ("mission", "missions", "autonomous", "agents", "orchestrate",
                     "figure out and fix", "investigate", "work on this",
                     "what's running", "whats running", "what are you waiting for",
                     "why did it fail", "in the background", "long running"),
    },
    "cognition": {
        "tools": ("current_context", "autonomy_level", "set_autonomy",
                  "recall_strategy", "remember_strategy", "forget_strategies",
                  "tool_reliability"),
        "triggers": ("context", "autonomy", "permission level", "how much",
                     "strategy", "strategies", "before", "last time", "previously",
                     "reliability", "unreliable", "slow tools", "what is going on",
                     "whats going on", "continue", "unfinished", "this", "here"),
    },
    "registry": {
        "tools": ("list_capabilities", "capability_info", "run_skill",
                  "reload_plugins", "capability_health", "set_capability"),
        "triggers": ("capability", "capabilities", "plugin", "plugins", "skill",
                     "skills", "what can you do", "extension", "extensions",
                     "provider", "providers", "registry", "installed",
                     "enable", "disable", "health"),
    },
    "mcp": {
        "tools": ("mcp_status", "mcp_tools"),
        "triggers": ("mcp", "server", "servers", "connected", "external",
                     "integration", "integrations", "protocol"),
    },
    "shell": {
        "tools": ("run_powershell",),
        "triggers": ("powershell", "command line", "terminal", "shell", "script"),
    },
}

_WORD = re.compile(r"[a-z0-9_]+")


def _tokens(text):
    return set(_WORD.findall(str(text or "").lower()))


def groups_now():
    """`GROUPS`, plus one group per connected MCP server.

    The table above is written by hand because the built-in tools are known
    when this file is written. MCP tools are not: they arrive at runtime from
    servers this machine may never have seen before, and a capability the
    selector cannot find is a capability the model never gets offered. So each
    connected server contributes a group of its own, keyed `mcp:<server>`, with
    triggers derived from its name and its tools.

    Read through a function rather than mutating `GROUPS` so the built-in table
    stays exactly what it says it is, and so a server disconnecting removes its
    triggers again without leaving anything behind.
    """
    try:
        from . import mcp_manager
        dynamic = mcp_manager.capability_groups()
    except Exception:
        return GROUPS
    if not dynamic:
        return GROUPS
    merged = dict(GROUPS)
    merged.update(dynamic)
    return merged


def _schema_for(name):
    for schema in tools.SCHEMAS:
        if schema["function"]["name"] == name:
            return schema
    return None


def module_of(name):
    for module, spec in groups_now().items():
        if name in spec["tools"]:
            return module
    return "core"


def select(query, recent=(), budget=BUDGET, groups=None):
    """The tool schemas to offer the model for this request.

    Returns (schemas, report). The report says what was chosen and why, so the
    decision is visible in the HUD rather than being invisible magic.

    `groups` forces particular capability groups in — this is how a mission
    agent is scoped to its own tools. Scoping belongs here, in the tool list;
    an earlier version smuggled trigger words into the prompt instead, and the
    model dutifully treated them as instructions and wandered off task.
    """
    text = str(query or "").lower()
    words = _tokens(text)
    table = groups_now()

    chosen, why = [], {}

    def add(name, reason):
        if name in why or _schema_for(name) is None:
            return
        why[name] = reason
        chosen.append(name)

    # 0. Groups the caller insists on — an agent's own scope.
    forced = [g for g in (groups or ()) if g in table]
    for module in forced:
        for name in table[module]["tools"]:
            add(name, "agent scope:" + module)

    # 1. Groups whose triggers appear in the request. Phrase triggers are
    #    checked against the raw text so "sign in" matches as a phrase.
    hit_groups = [(module, "forced") for module in forced]
    for module, spec in table.items():
        if module in forced:
            continue
        for trigger in spec["triggers"]:
            if (" " in trigger and trigger in text) or (" " not in trigger and trigger in words):
                hit_groups.append((module, trigger))
                break

    for module, trigger in hit_groups:
        if trigger == "forced":
            continue
        for name in table[module]["tools"]:
            add(name, "group:" + module + " (" + trigger + ")")

    # 2. Tools still in play from earlier in this session.
    for name in recent:
        add(name, "used earlier this session")

    # 3. The core set, always.
    for name in CORE:
        add(name, "core")

    # 4. If there is still room, the best remaining matches on name and
    #    description, so an unusual phrasing can still find its tool.
    if len(chosen) < budget:
        scored = []
        for schema in tools.SCHEMAS:
            name = schema["function"]["name"]
            if name in why:
                continue
            description = schema["function"].get("description", "")
            score = 3 * len(words & _tokens(name)) + len(words & _tokens(description))
            if score:
                scored.append((score, name))
        scored.sort(reverse=True)
        for score, name in scored:
            if len(chosen) >= budget:
                break
            add(name, "matched description")

    # 5. Nothing matched at all: give a broad, useful default rather than
    #    only the core, so an unanticipated request still has somewhere to go.
    if len(chosen) <= len(CORE):
        for module in ("knowledge", "desktop", "live", "vision"):
            for name in table[module]["tools"]:
                if len(chosen) >= budget:
                    break
                add(name, "default set")

    chosen = chosen[:budget]
    # Schemas go out in the fixed order of tools.SCHEMAS, not selection order.
    #
    # Reordering these to put the always-present core tools first was tried, on
    # the theory that a stable prompt prefix would let Ollama reuse its cache.
    # It was measured and it does not help: offering 0 schemas instead of 30
    # (1063 vs 3481 prompt tokens) changes latency by under 60 ms, so the tool
    # list is not what a turn spends its time on. Left alone deliberately.
    schemas = [s for s in tools.SCHEMAS if s["function"]["name"] in set(chosen)]

    return schemas, {
        "offered": chosen,
        "count": len(chosen),
        "total": len(tools.SCHEMAS),
        "groups": [module for module, _t in hit_groups],
        "why": why,
        "budget": budget,
    }


def describe():
    """Structured metadata for every capability, for discovery and the HUD."""
    entries = []
    for schema in tools.SCHEMAS:
        function = schema["function"]
        name = function["name"]
        parameters = function.get("parameters") or {}
        graded = risk.describe(name)
        entries.append({
            "name": name,
            "module": module_of(name),
            "description": function.get("description", ""),
            "inputs": sorted((parameters.get("properties") or {}).keys()),
            "required": list(parameters.get("required") or ()),
            "risk": graded["risk"],
            "risk_label": graded["risk_label"],
            "reversible": graded["reversible"],
            "confirm": graded["confirm"],
            "available": name in tools.REGISTRY,
        })
    entries.sort(key=lambda e: (e["module"], e["name"]))
    modules = {}
    for entry in entries:
        modules[entry["module"]] = modules.get(entry["module"], 0) + 1
    return {"capabilities": entries, "count": len(entries), "modules": modules,
            "budget": BUDGET}
