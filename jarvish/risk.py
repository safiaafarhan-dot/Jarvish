"""Risk classification for every tool, and the confirmation gate built on it.

Jarvish drives a real computer, so before anything runs the agent needs to know
what class of action it is about to take: whether it only reads, whether it can
be undone, and whether the user should be asked first. Every tool is graded
here, and `llm.run_agent` refuses to execute a gated tool until the UI sends
back an explicit confirmation.
"""

import re

from . import tools

SAFE = "safe"
LOW = "low"
MEDIUM = "medium"
HIGH = "high"
CRITICAL = "critical"

ORDER = (SAFE, LOW, MEDIUM, HIGH, CRITICAL)

LABELS = {
    SAFE: "Read-only",
    LOW: "Low risk",
    MEDIUM: "Changes state",
    HIGH: "High risk",
    CRITICAL: "Irreversible",
}

# Anything at or above this tier stops and asks the user before running.
GATE_FROM = HIGH

# Read-only lookups. These cannot change the machine or the user's data.
_SAFE = {
    "get_time", "system_info", "list_processes", "help_overview",
    "recall", "list_instructions",
    "web_search", "read_web_page", "get_weather", "get_news",
    "convert_currency", "daily_briefing",
    "find_files", "list_directory", "read_text_file",
    "list_web_apps", "phone_access",
    "wifi_status", "wifi_networks", "wifi_saved_networks",
    "bluetooth_devices", "network_info", "get_brightness",
    "list_windows", "take_screenshot",
    "look_at_screen", "read_screen_text", "find_on_screen",
    "describe_active_window", "vision_status",
    "browser_status", "browser_tabs", "browser_read", "browser_find",
    "browser_search_page",
    # Knowledge retrieval only reads what has already been indexed.
    "search_knowledge", "knowledge_status", "find_symbol",
    "related_files", "project_overview",
    # Reading task state and insights changes nothing.
    "list_tasks", "task_status", "system_insights", "notifications",
    "voice_status",
    # The registry itself only reports what exists.
    "list_capabilities", "capability_info", "capability_health",
    # Reporting which MCP servers are connected reads state and nothing else.
    "mcp_status", "mcp_tools",
    # Reading mission state changes nothing.
    "mission_status", "list_missions",
    # The cognitive layer only reads what other subsystems already hold.
    "current_context", "autonomy_level", "recall_strategy",
    "tool_reliability",
    # Developer inspection: reads the index, the repo or the files.
    "project_info", "project_architecture", "find_callers",
    "find_dependents", "diagnose_error", "propose_change",
    "git_status", "git_diff", "git_log", "git_branches",
    "propose_commit", "project_notes",
}

# Visible but harmless: something opens or a setting nudges. Trivially undone.
_LOW = {
    "voice_listen",
    "open_app", "open_url", "open_web_app", "open_inbox",
    "open_phone_link", "open_settings",
    "media_control", "set_brightness",
    "browser_launch", "browser_open", "browser_new_tab",
    "browser_scroll", "browser_switch_tab",
    # The general interaction primitive. Clicking "Next" is harmless; the
    # intent patterns raise it when the target reads as consequential.
    "browser_click",
    # Controlling a task the user already created, and tidying alerts.
    "pause_task", "resume_task", "retry_task", "dismiss_notifications",
    # Running the project's own tests or build changes nothing outward.
    "run_tests", "run_build", "remember_project",
    "remember_strategy",
    # Turning a capability on or off, and re-reading the plugins folder.
    "set_capability", "reload_plugins",
    # Controlling a mission the user already started.
    "pause_mission", "resume_mission", "retry_mission_task",
}

# Writes to disk, types into windows, or drafts a message to a real person.
_MEDIUM = {
    "remember", "forget", "add_instruction", "remove_instruction",
    "whatsapp_message", "compose_email",
    "type_text", "focus_window",
    "wifi_connect", "bluetooth_power",
    # Filling a field or closing a tab changes state but sends nothing.
    "browser_type", "browser_clear", "browser_check", "browser_select",
    "browser_close_tab", "browser_download",
    # Indexing reads a whole folder and writes a local database.
    "index_folder", "forget_folder",
    # Scheduling commits Jarvish to future work; cancelling destroys a
    # task; settings change when it is allowed to speak up at all.
    "schedule_task", "cancel_task", "proactive_settings",
    # Starting a mission commits Jarvish to autonomous work; the tools
    # inside it are still gated individually. Cancelling destroys state.
    "start_mission", "cancel_mission",
    # Changing the autonomy ceiling, and deleting learned strategies.
    "set_autonomy", "forget_strategies",
    # Editing source, and restoring a backup over the current file.
    "apply_change", "revert_change",
    # A shell command in the project. The tier is recomputed from the
    # command itself in `escalate`, so destructive ones are gated.
    "run_dev_command",
    # A skill runs a whole agent turn, so it inherits whatever that turn
    # does; the tools inside it are gated individually.
    "run_skill",
}

# Interrupts the user's work or drops connectivity. Recoverable, but disruptive.
_HIGH = {
    "close_app", "lock_screen", "wifi_disconnect", "wifi_power",
    # A click lands on whatever the model believes it identified, and the
    # thing under it may well be "Delete". Grounded, but still unbounded.
    "click_on_screen",
}

# Ends the session or runs model-authored code with the user's privileges.
_CRITICAL = {
    "power_action", "run_powershell",
}

# Submitting is the act that actually sends something into the world.
_HIGH.add("browser_submit")

# Tools whose effect cannot be walked back by calling something else.
_IRREVERSIBLE = {
    "forget", "remove_instruction", "close_app", "power_action",
    "run_powershell", "lock_screen",
    # An edit is reversible via revert_change, but only until the next one.
    "revert_change",
}

# Why a gated tool is being stopped, phrased for a confirmation prompt.
_REASONS = {
    "close_app": "This force-closes the application. Unsaved work will be lost.",
    "lock_screen": "This locks the workstation immediately.",
    "wifi_disconnect": "This drops the current Wi-Fi connection.",
    "wifi_power": "This switches the Wi-Fi radio off, ending all connectivity.",
    "power_action": "This shuts down, restarts or signs out of the machine.",
    "run_powershell": "This runs a command the model wrote, with your full privileges.",
    "click_on_screen": "This clicks a real control on your screen. Check the element "
                       "below is the one you meant.",
    "browser_submit": "This submits a form in the browser — the step that actually "
                      "sends, posts or confirms.",
    "apply_change": "This rewrites source code on disk. A backup is kept and the "
                    "result is parse-checked, but review the change below first.",
    "run_dev_command": "This runs a real command in the project directory.",
}


# Tiers declared at runtime by the capability registry. Kept separate from the
# built-in sets so a plugin can never quietly re-grade a built-in tool.
_DECLARED = {}
_DECLARED_REVERSIBLE = {}


def declare(name, tier, reversible=True):
    """Record the tier of a capability registered at runtime.

    A tier that is not recognised becomes `high`, and a built-in name is never
    overwritten — a plugin cannot lower the grade of something already graded.
    """
    if name in _SAFE or name in _LOW or name in _MEDIUM or name in _HIGH \
            or name in _CRITICAL:
        return False
    _DECLARED[name] = tier if tier in ORDER else HIGH
    _DECLARED_REVERSIBLE[name] = bool(reversible)
    return True


def undeclare(name):
    _DECLARED.pop(name, None)
    _DECLARED_REVERSIBLE.pop(name, None)
    _ALWAYS_CONFIRM.discard(name)
    _CONFIRM_REASON.pop(name, None)


# Capabilities that must be confirmed whatever tier they carry.
#
# The tier answers "how dangerous is this class of action", and for built-ins
# that is the whole story. A capability borrowed from an MCP server has a
# second, independent question attached: whether its *provider* is trusted to
# act unattended. A `medium` tool from a server the user marked
# `requiresConfirmation` should stop and ask even though `medium` is below the
# gate — not because the action got more dangerous, but because the user said
# so about that server.
#
# This can only ever add a prompt. There is no matching set that removes one,
# because that would be a way to route around the gate, and the gate has
# exactly one door.
_ALWAYS_CONFIRM = set()
_CONFIRM_REASON = {}


def declare_confirmation(name, required=True, why=None):
    """Demand confirmation for a capability regardless of its tier."""
    if required:
        _ALWAYS_CONFIRM.add(name)
        if why:
            _CONFIRM_REASON[name] = str(why)
    else:
        _ALWAYS_CONFIRM.discard(name)
        _CONFIRM_REASON.pop(name, None)
    return True


def always_confirms(name):
    return name in _ALWAYS_CONFIRM


def level(name):
    """The risk tier for a tool. Unknown tools are treated as high risk."""
    if name in _SAFE:
        return SAFE
    if name in _LOW:
        return LOW
    if name in _MEDIUM:
        return MEDIUM
    if name in _HIGH:
        return HIGH
    if name in _CRITICAL:
        return CRITICAL
    if name in _DECLARED:
        return _DECLARED[name]
    return HIGH


def reversible(name):
    if name in _DECLARED_REVERSIBLE:
        return _DECLARED_REVERSIBLE[name]
    return name not in _IRREVERSIBLE


# Clicking "Next" and clicking "Delete account" are the same tool call with a
# different argument, so the tier cannot come from the tool name alone. These
# patterns read the *target the model named* — its stated intent — and are
# checked most-severe first, so "delete my account" outranks plain "delete".
_INTENT_TIERS = (
    (CRITICAL, re.compile(
        r"\b(delete|close|deactivate|terminate|permanently remove)\s+"
        r"(my\s+|the\s+|this\s+)?(account|profile|subscription|workspace|"
        r"organisation|organization|repository|repo|database)\b"
        r"|\b(erase|wipe|destroy)\s+(all|everything)\b"
        r"|\bdelete\s+(all|everything|permanently)\b",
        re.IGNORECASE)),
    (HIGH, re.compile(
        r"\b(send|post|publish|tweet|submit|confirm|buy|purchase|pay|order|"
        r"checkout|transfer|withdraw|deposit|delete|remove|discard|erase|"
        r"unsubscribe|sign ?out|log ?out)\b",
        re.IGNORECASE)),
    (MEDIUM, re.compile(
        r"\b(download|upload|save|export|import|attach|install|apply)\b",
        re.IGNORECASE)),
)

# Tools whose risk depends on what they are pointed at.
_ESCALATES = {"browser_click", "click_on_screen", "browser_type", "browser_find",
              "run_dev_command", "apply_change"}


def escalate(name, arguments):
    """(tier, reason) when this specific call outranks the tool's base tier.

    Returns None when the call is no riskier than usual.
    """
    if name not in _ESCALATES or not arguments:
        return None

    if name == "browser_type" and arguments.get("submit"):
        return (HIGH, "This fills the field and then submits the form, which sends it.")

    # A shell command is only as safe as the command. `git status` and
    # `rm -rf` arrive through the same tool, so the tier comes from parsing it.
    if name == "run_dev_command":
        from . import dev
        tier, why = dev.classify_command(arguments.get("command"))
        if ORDER.index(tier) > ORDER.index(level(name)):
            return (tier, why)
        return None

    # Editing a file the project depends on widely deserves a closer look.
    if name == "apply_change":
        target = str(arguments.get("file") or "")
        if re.search(r"(settings|config|secret|credential|\.env)", target, re.IGNORECASE):
            return (HIGH, "This edits configuration or an environment file.")
        return None

    haystack = " ".join(
        str(value) for key, value in arguments.items()
        if key in ("target", "option") and value)
    if not haystack.strip():
        return None

    base = ORDER.index(level(name))
    for tier, pattern in _INTENT_TIERS:
        match = pattern.search(haystack)
        if match and ORDER.index(tier) > base:
            return (tier, 'The target is "' + str(arguments.get("target", ""))[:60] +
                          '", which reads as a ' + match.group(0).lower().strip() +
                          " action.")
    return None


def effective_level(name, arguments=None):
    """The tier this call actually carries, after reading its arguments."""
    found = escalate(name, arguments)
    return found[0] if found else level(name)


def escalated(name, arguments):
    """The reason this call was escalated, or None."""
    found = escalate(name, arguments)
    return found[1] if found else None


def gated(name, arguments=None):
    """Whether this call must be confirmed by the user before it runs.

    Some tools carry their own `confirm` flag in the schema. When the model has
    already set it the user has, by definition, been asked once already, so the
    gate stands down rather than asking twice.
    """
    if arguments and arguments.get("confirm") is True:
        return False
    if name in _ALWAYS_CONFIRM:
        return True
    return ORDER.index(effective_level(name, arguments)) >= ORDER.index(GATE_FROM)


def reason(name, arguments=None):
    why = escalated(name, arguments)
    if why:
        return why
    if name in _REASONS:
        return _REASONS[name]
    if name in _CONFIRM_REASON:
        return _CONFIRM_REASON[name]
    return "This action affects the system and cannot be undone."


def preview(name, arguments):
    """A one-line, human-readable statement of what is about to happen."""
    arguments = arguments or {}
    detail = ", ".join(
        str(key) + ": " + str(value)
        for key, value in arguments.items()
        if key != "confirm" and value not in (None, "")
    )
    return name + ("  (" + detail + ")" if detail else "")


def describe(name):
    """Full risk metadata for one tool, for the UI's permission panel."""
    tier = level(name)
    return {
        "name": name,
        "risk": tier,
        "risk_label": LABELS[tier],
        "reversible": reversible(name),
        "confirm": gated(name),
        "always_confirms": name in _ALWAYS_CONFIRM,
    }


def manifest():
    """Risk metadata for every registered tool, grouped by module ownership."""
    return {name: describe(name) for name in sorted(tools.REGISTRY)}
