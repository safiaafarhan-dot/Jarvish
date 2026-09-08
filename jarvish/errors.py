"""Structured error categories for tool execution.

Every failure a tool can suffer is one of a small, closed set of categories.
Naming them is worth a module for three reasons.

The **model** reacts differently to each. `TOOL_NOT_FOUND` means pick another
tool; `INVALID_ARGUMENTS` means call the same one again, differently;
`PERMISSION_DENIED` means stop asking. A single opaque "it failed" string gives
it nothing to steer by, and it retries the identical call until the round
budget runs out.

The **user** should never see a stack trace. A category carries a sentence
written for a person, and the raw exception stays in the log.

The **operator** wants to count them. `mcp_error` with a category is a metric;
`mcp_error` with a free-text message is not.

Results keep Jarvish's existing shape — `{"ok": False, "error": "..."}` — so
nothing that already reads tool results needs to change. The category rides
alongside in `error_category`, and older code that ignores it is unaffected.
"""

TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
PERMISSION_DENIED = "PERMISSION_DENIED"
CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
TIMEOUT = "TIMEOUT"
SERVER_UNAVAILABLE = "SERVER_UNAVAILABLE"
MCP_PROTOCOL_ERROR = "MCP_PROTOCOL_ERROR"
EXECUTION_FAILED = "EXECUTION_FAILED"
OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
SECURITY_BLOCKED = "SECURITY_BLOCKED"
CANCELLED = "CANCELLED"

CATEGORIES = (
    TOOL_NOT_FOUND, INVALID_ARGUMENTS, PERMISSION_DENIED,
    CONFIRMATION_REQUIRED, TIMEOUT, SERVER_UNAVAILABLE, MCP_PROTOCOL_ERROR,
    EXECUTION_FAILED, OUTPUT_TOO_LARGE, SECURITY_BLOCKED, CANCELLED,
)

# Whether calling the same thing again, unchanged, could plausibly work. The
# agent loop uses this to decide between retrying and re-planning.
RETRYABLE = {
    TIMEOUT: True,
    SERVER_UNAVAILABLE: True,
    MCP_PROTOCOL_ERROR: True,
    EXECUTION_FAILED: True,
    TOOL_NOT_FOUND: False,
    INVALID_ARGUMENTS: False,
    PERMISSION_DENIED: False,
    CONFIRMATION_REQUIRED: False,
    OUTPUT_TOO_LARGE: False,
    SECURITY_BLOCKED: False,
    CANCELLED: False,
}

# What to say to a person when there is nothing more specific to say.
PHRASING = {
    TOOL_NOT_FOUND: "That capability is not available.",
    INVALID_ARGUMENTS: "The arguments for that call were not valid.",
    PERMISSION_DENIED: "That action is not permitted at the current settings.",
    CONFIRMATION_REQUIRED: "That action needs to be confirmed first.",
    TIMEOUT: "That took too long and was stopped.",
    SERVER_UNAVAILABLE: "The service backing that capability is not reachable.",
    MCP_PROTOCOL_ERROR: "The external server replied in a way that could not be read.",
    EXECUTION_FAILED: "The action ran but did not succeed.",
    OUTPUT_TOO_LARGE: "The result was too large to return.",
    SECURITY_BLOCKED: "That was blocked by a security rule.",
    CANCELLED: "That was cancelled.",
}


def fail(category, message=None, **fields):
    """A failed tool result carrying a category.

    Shape-compatible with `util.err`, so every existing caller — the agent
    loop, the HUD, the task runner — keeps working without knowing categories
    exist at all.
    """
    if category not in CATEGORIES:
        category = EXECUTION_FAILED
    return dict(
        ok=False,
        error=str(message or PHRASING[category]),
        error_category=category,
        retryable=RETRYABLE[category],
        **fields,
    )


def category_of(result):
    """The category of a failed result, or None if it is not a categorised failure."""
    if not isinstance(result, dict) or result.get("ok") is not False:
        return None
    found = result.get("error_category")
    return found if found in CATEGORIES else None


def is_retryable(result):
    found = category_of(result)
    return RETRYABLE.get(found, False) if found else False
