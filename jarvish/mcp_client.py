"""The MCP client: one connection to one server, and the thread they all live on.

This talks the real Model Context Protocol through the official Python SDK.
There is no bespoke protocol here, and deliberately so - the point of MCP is
that a server written for any host works with this one.

The interesting design problem is not the protocol, it is where it runs.

Jarvish's tools are **synchronous functions**. `tools.REGISTRY` maps a name to
a plain callable, `llm.run_agent` executes them with `asyncio.to_thread`, and
the voice worker, the task runner and the mission runner each drive the agent
from their own event loop on their own thread. An MCP session, by contrast, is
a long-lived async object built on anyio task groups: it must be opened,
awaited and closed on one loop, by one task, and it holds a subprocess open
between calls.

Those two facts do not fit together directly. Opening a session per call would
pay process startup - seconds, for an npx server - on every tool call and
defeat the persistent connection MCP exists to provide. Running sessions on
whichever loop happens to be calling would tear a session across loops the
moment the voice thread and the HUD both used it.

So every MCP session lives on **one dedicated thread with one dedicated event
loop**, owned by this module, and the outside world reaches it only through
`submit()`, which hands work across with `run_coroutine_threadsafe`. A caller
on any thread, on any loop or on none, gets a synchronous result. Nothing here
ever runs on a caller's loop, which is what makes it impossible for an MCP call
to re-enter the agent's own loop.

Each connection runs a **supervisor task** that owns the session for its whole
life: it connects, initialises, discovers, then parks on an event until asked
to shut down, reconnecting with backoff if the server drops. Requests are
separate tasks on the same loop that use the session the supervisor is holding
open. Cancelling a request never touches the connection; cancelling the
supervisor closes the subprocess with it.
"""

import asyncio
import concurrent.futures
import threading
import time

from . import errors, observability, security
from .config import MCP_REQUEST_TIMEOUT

# Three different classes on Python 3.10, and which one is raised depends on
# who timed out: `concurrent.futures.Future.result(timeout)` raises its own,
# `asyncio.wait_for` raises asyncio's, and the builtin turns up from the OS.
# They were only unified in 3.11. Catching the wrong one here is silent and
# expensive - a request that timed out gets reported as EXECUTION_FAILED,
# which is marked retryable, so the agent calmly runs the slow call a second
# time. Every timeout catch in this module uses this tuple.
_TIMEOUTS = (TimeoutError, asyncio.TimeoutError, concurrent.futures.TimeoutError)

# --------------------------------------------------------------------------
# Lifecycle states
# --------------------------------------------------------------------------

STARTING = "STARTING"
CONNECTING = "CONNECTING"
INITIALIZING = "INITIALIZING"
DISCOVERING = "DISCOVERING"
READY = "READY"
RECONNECTING = "RECONNECTING"
DISCONNECTED = "DISCONNECTED"

STATES = (STARTING, CONNECTING, INITIALIZING, DISCOVERING, READY,
          RECONNECTING, DISCONNECTED)

# How long to wait between reconnect attempts, by attempt number. Backing off
# matters: a server that crashes on startup would otherwise be relaunched in a
# tight loop for as long as Jarvish runs.
_BACKOFF = (1.0, 3.0, 8.0, 20.0, 45.0)


def available():
    """Whether the MCP SDK is installed. Returns (ok, reason)."""
    try:
        import mcp  # noqa: F401
    except ImportError as exc:
        return False, ("The MCP SDK is not installed. Add it with: "
                       "python -m pip install \"mcp>=2,<3\"  (" + str(exc) + ")")
    return True, None


# --------------------------------------------------------------------------
# The runtime thread
# --------------------------------------------------------------------------

class _Runtime:
    """The single background loop every MCP session runs on."""

    def __init__(self):
        self._loop = None
        self._thread = None
        self._lock = threading.RLock()

    def loop(self):
        """The running loop, starting the thread on first use."""
        with self._lock:
            if self._loop is not None and not self._loop.is_closed():
                return self._loop

            ready = threading.Event()

            def run():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    # Let anything still pending unwind before the loop closes,
                    # so a subprocess transport gets its chance to terminate the
                    # child rather than leaking it.
                    try:
                        pending = asyncio.all_tasks(loop)
                        for task in pending:
                            task.cancel()
                        if pending:
                            loop.run_until_complete(
                                asyncio.gather(*pending, return_exceptions=True))
                        loop.run_until_complete(loop.shutdown_asyncgens())
                    except Exception:
                        pass
                    loop.close()

            self._thread = threading.Thread(
                target=run, name="jarvish-mcp", daemon=True)
            self._thread.start()
            ready.wait(10.0)
            return self._loop

    @property
    def running(self):
        return self._loop is not None and not self._loop.is_closed()

    def submit(self, coroutine, timeout=None):
        """Run a coroutine on the MCP loop and wait for it from this thread."""
        loop = self.loop()
        if loop is None:
            raise RuntimeError("The MCP runtime could not be started.")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout)
        except _TIMEOUTS:
            future.cancel()
            raise

    def spawn(self, coroutine):
        """Start a coroutine on the MCP loop without waiting for it."""
        loop = self.loop()
        return asyncio.run_coroutine_threadsafe(coroutine, loop)

    def stop(self):
        with self._lock:
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            loop.call_soon_threadsafe(loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=10.0)
            self._loop = None
            self._thread = None


RUNTIME = _Runtime()


# --------------------------------------------------------------------------
# One connection
# --------------------------------------------------------------------------

class Connection:
    """One MCP server: its process, its session, and what it can do.

    Every public method here is safe to call from any thread. The async
    internals below the line are only ever touched by the runtime loop.
    """

    def __init__(self, config):
        self.config = config
        self.name = config["name"]
        self.state = STARTING
        self.error = None
        self.error_category = None
        self.tools = []          # list of {name, description, input_schema, ...}
        self.resources = []
        self.prompts = []
        self.server_info = {}
        self.capabilities = {}
        self.connected_at = None
        self.attempts = 0
        self.calls = 0
        self.failures = 0
        self.last_call_at = None

        self._client = None
        self._supervisor = None
        self._ready = None       # asyncio.Event, created on the MCP loop
        self._closing = None
        self._settled = threading.Event()
        self._state_lock = threading.RLock()

    # -- state ----------------------------------------------------------

    def _set_state(self, state, error=None, category=None):
        with self._state_lock:
            self.state = state
            self.error = error
            self.error_category = category
        if state == READY:
            self.connected_at = time.time()
            observability.emit(observability.MCP_CONNECTED, tool=self.name,
                               server=self.name, tools=len(self.tools),
                               resources=len(self.resources),
                               prompts=len(self.prompts))
        elif state == DISCONNECTED and error:
            observability.emit(observability.MCP_ERROR, tool=self.name,
                               server=self.name, ok=False,
                               error_category=category or errors.SERVER_UNAVAILABLE,
                               detail=str(error)[:300])
        elif state == DISCONNECTED:
            observability.emit(observability.MCP_DISCONNECTED, tool=self.name,
                               server=self.name)

    @property
    def ready(self):
        return self.state == READY and self._client is not None

    @property
    def healthy(self):
        return self.state in (READY, DISCOVERING, INITIALIZING)

    # -- transport ------------------------------------------------------

    def _make_client(self):
        """Build an SDK client for whichever transport this server uses."""
        from mcp import Client, StdioServerParameters

        if self.config["transport"] == "http":
            target = self.config["url"]
        else:
            target = StdioServerParameters(
                command=self.config["command"],
                args=list(self.config["args"]),
                # Never None: that gives the child an empty environment on
                # Windows, where npx then cannot find node. Never the raw
                # environment either — see `_child_environment`.
                env=_child_environment(self.config["env"]),
                cwd=self.config.get("cwd") or None,
            )
        return Client(target, read_timeout_seconds=self.config["request_timeout"])

    # -- the supervisor -------------------------------------------------

    async def _discover(self, client):
        """Ask the server what it offers. Each kind fails independently.

        A server that advertises `resources` and then errors on `list_resources`
        should still contribute its tools, so one broken capability never costs
        the others.
        """
        capabilities = getattr(client, "server_capabilities", None)

        def offers(kind):
            if capabilities is None:
                return True
            return getattr(capabilities, kind, None) is not None

        self.tools = []
        if offers("tools"):
            try:
                listed = await client.list_tools()
                for tool in listed.tools:
                    self.tools.append({
                        "name": tool.name,
                        "description": (tool.description or "").strip(),
                        "input_schema": _schema_of(tool),
                        "output_schema": getattr(tool, "output_schema", None),
                        "annotations": _annotations_of(tool),
                    })
            except Exception as exc:
                self.tools = []
                observability.emit(observability.MCP_ERROR, tool=self.name,
                                   server=self.name, ok=False, phase="list_tools",
                                   error_category=errors.MCP_PROTOCOL_ERROR,
                                   detail=str(exc)[:200])

        self.resources = []
        if offers("resources"):
            try:
                listed = await client.list_resources()
                self.resources = [
                    {"uri": str(r.uri), "name": r.name or str(r.uri),
                     "description": (r.description or "").strip(),
                     "mime_type": getattr(r, "mime_type", None)}
                    for r in listed.resources
                ]
            except Exception:
                self.resources = []

        self.prompts = []
        if offers("prompts"):
            try:
                listed = await client.list_prompts()
                self.prompts = [
                    {"name": p.name, "description": (p.description or "").strip(),
                     "arguments": [a.name for a in (p.arguments or [])]}
                    for p in listed.prompts
                ]
            except Exception:
                self.prompts = []

        info = getattr(client, "server_info", None)
        self.server_info = {
            "name": getattr(info, "name", self.name),
            "version": getattr(info, "version", ""),
            "title": getattr(info, "title", None),
        }
        self.capabilities = {
            "tools": offers("tools"), "resources": offers("resources"),
            "prompts": offers("prompts"),
            "protocol": getattr(client, "protocol_version", None),
        }

    async def _supervise(self):
        """Own the session for its entire life, reconnecting when it drops."""
        self._ready = asyncio.Event()
        self._closing = asyncio.Event()
        attempt = 0

        while not self._closing.is_set():
            self.attempts += 1
            try:
                self._set_state(CONNECTING if attempt == 0 else RECONNECTING)
                context = self._make_client()
                async with context as client:
                    # The SDK performs initialize inside __aenter__, so by the
                    # time we are here the handshake has already succeeded.
                    self._set_state(INITIALIZING)
                    self._set_state(DISCOVERING)
                    await self._discover(client)

                    self._client = client
                    attempt = 0
                    self._set_state(READY)
                    self._ready.set()
                    self._settled.set()

                    # Park. The session stays open, and requests run as
                    # separate tasks against the client held here.
                    await self._closing.wait()

                self._client = None
                self._set_state(DISCONNECTED)
                return

            except asyncio.CancelledError:
                self._client = None
                self._set_state(DISCONNECTED)
                raise
            except Exception as exc:
                self._client = None
                self._ready = asyncio.Event()
                detail = _describe(exc)
                category = _categorise(exc, detail)
                if category == errors.EXECUTION_FAILED:
                    # Nothing executed - this is the connect path, so an
                    # unrecognised failure means the server did not come up.
                    category = errors.SERVER_UNAVAILABLE

                allowed = self.config["max_reconnect_attempts"] \
                    if self.config["auto_reconnect"] else 0
                if self._closing.is_set() or attempt >= allowed:
                    self._set_state(DISCONNECTED, detail, category)
                    self._settled.set()
                    return

                self._set_state(RECONNECTING, detail, category)
                self._settled.set()
                delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
                attempt += 1
                try:
                    await asyncio.wait_for(self._closing.wait(), timeout=delay)
                    break
                except _TIMEOUTS:
                    continue

        self._client = None
        self._set_state(DISCONNECTED)

    # -- public, thread-safe --------------------------------------------

    def connect(self, timeout=None):
        """Start the server and wait until it is ready. Returns (ok, reason)."""
        installed, why = available()
        if not installed:
            self._set_state(DISCONNECTED, why, errors.SERVER_UNAVAILABLE)
            return False, why

        if self.state == READY:
            return True, None

        deadline = timeout or (self.config["startup_timeout"] +
                               self.config["connection_timeout"])
        self._settled.clear()
        self._set_state(STARTING)
        self._supervisor = RUNTIME.spawn(self._supervise())

        # Wait on a threading.Event rather than the asyncio one: this call
        # arrives from an arbitrary thread, and the asyncio event belongs to
        # the MCP loop.
        if not self._settled.wait(deadline):
            self.disconnect()
            reason = ("The server did not become ready within " +
                      str(round(deadline)) + "s.")
            self._set_state(DISCONNECTED, reason, errors.TIMEOUT)
            return False, reason

        if self.state == READY:
            return True, None
        return False, self.error or "The server did not connect."

    def disconnect(self):
        """Close the session and stop the subprocess. Safe to call twice."""
        closing, loop = self._closing, RUNTIME._loop
        if closing is not None and loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(closing.set)
            except RuntimeError:
                pass

        supervisor = self._supervisor
        if supervisor is not None:
            try:
                supervisor.result(timeout=8.0)
            except Exception:
                # It did not unwind in time, or it raised on the way out.
                # Cancelling is what actually kills the child process.
                supervisor.cancel()
        self._supervisor = None
        self._client = None
        if self.state != DISCONNECTED:
            self._set_state(DISCONNECTED)
        return True

    def call(self, tool_name, arguments=None, timeout=None, cancel=None):
        """Call one tool on this server. Never raises; returns a tool result.

        `cancel` is any object with `is_set()` - a `threading.Event`, or the
        session's stop flag. It is polled while waiting, so a user pressing
        STOP releases this call promptly and the in-flight MCP request is
        cancelled rather than left running.
        """
        if not self.ready:
            return errors.fail(
                errors.SERVER_UNAVAILABLE,
                "The '" + self.name + "' server is " + self.state.lower() + ".",
                server=self.name, state=self.state)

        limit = timeout or self.config["request_timeout"] or MCP_REQUEST_TIMEOUT
        started = time.perf_counter()
        self.calls += 1
        self.last_call_at = time.time()

        try:
            future = RUNTIME.spawn(
                self._call(tool_name, arguments or {}, limit))
        except Exception as exc:
            self.failures += 1
            return errors.fail(errors.SERVER_UNAVAILABLE, _describe(exc),
                               server=self.name)

        # Poll rather than block outright, so cancellation is honoured while
        # the request is still in flight.
        deadline = time.monotonic() + limit + 2.0
        while True:
            try:
                result = future.result(timeout=0.15)
                break
            except _TIMEOUTS:
                if cancel is not None and cancel.is_set():
                    future.cancel()
                    self.failures += 1
                    return errors.fail(
                        errors.CANCELLED,
                        "Cancelled while waiting for " + self.name + ".",
                        server=self.name, tool=tool_name)
                if time.monotonic() > deadline:
                    future.cancel()
                    self.failures += 1
                    return errors.fail(
                        errors.TIMEOUT,
                        self.name + ":" + tool_name + " did not answer within " +
                        str(round(limit)) + "s.",
                        server=self.name, tool=tool_name)
                continue
            except asyncio.CancelledError:
                self.failures += 1
                return errors.fail(errors.CANCELLED, "The call was cancelled.",
                                   server=self.name, tool=tool_name)
            except Exception as exc:
                self.failures += 1
                return errors.fail(errors.EXECUTION_FAILED, _describe(exc),
                                   server=self.name, tool=tool_name)

        elapsed = round((time.perf_counter() - started) * 1000)
        if isinstance(result, dict):
            result.setdefault("ms", elapsed)
            if result.get("ok") is False:
                self.failures += 1
        return result

    async def _call(self, tool_name, arguments, limit):
        """The request itself, on the MCP loop."""
        client = self._client
        if client is None:
            return errors.fail(errors.SERVER_UNAVAILABLE,
                               "The connection closed before the call ran.",
                               server=self.name)
        try:
            outcome = await asyncio.wait_for(
                client.call_tool(tool_name, arguments), timeout=limit)
        except _TIMEOUTS:
            return errors.fail(
                errors.TIMEOUT,
                self.name + ":" + tool_name + " did not answer within " +
                str(round(limit)) + "s.", server=self.name, tool=tool_name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = _describe(exc)
            return errors.fail(_categorise(exc, detail), detail,
                               server=self.name, tool=tool_name)

        return _normalise_result(outcome, self.name, tool_name)

    def read_resource(self, uri, timeout=None):
        """Read one MCP resource. The body is untrusted content."""
        if not self.ready:
            return errors.fail(errors.SERVER_UNAVAILABLE,
                               "The '" + self.name + "' server is not connected.",
                               server=self.name)
        limit = timeout or self.config["request_timeout"]

        async def run():
            client = self._client
            if client is None:
                return errors.fail(errors.SERVER_UNAVAILABLE, "Not connected.")
            try:
                got = await asyncio.wait_for(client.read_resource(uri), timeout=limit)
            except _TIMEOUTS:
                return errors.fail(errors.TIMEOUT, "Reading " + str(uri) +
                                   " timed out.", server=self.name)
            except Exception as exc:
                return errors.fail(errors.MCP_PROTOCOL_ERROR, _describe(exc),
                                   server=self.name)
            parts = []
            for item in getattr(got, "contents", []) or []:
                text = getattr(item, "text", None)
                if text is not None:
                    parts.append(text)
                elif getattr(item, "blob", None) is not None:
                    parts.append("[binary content omitted]")
            return {"ok": True, **security.as_untrusted(
                "\n".join(parts), "mcp:" + self.name + ":resource",
                {"uri": str(uri), "server": self.name})}

        try:
            return RUNTIME.submit(run(), timeout=limit + 5.0)
        except Exception as exc:
            return errors.fail(errors.EXECUTION_FAILED, _describe(exc),
                               server=self.name)

    def status(self):
        """Everything worth knowing about this connection, safe to publish."""
        return {
            "name": self.name,
            "state": self.state,
            "ready": self.ready,
            "transport": self.config["transport"],
            "enabled": self.config["enabled"],
            "error": security.redact(self.error) if self.error else None,
            "error_category": self.error_category,
            "tools": [t["name"] for t in self.tools],
            "tool_count": len(self.tools),
            "resources": len(self.resources),
            "prompts": len(self.prompts),
            "server_info": self.server_info,
            "capabilities": self.capabilities,
            "connected_at": self.connected_at,
            "uptime_s": (round(time.time() - self.connected_at)
                         if self.connected_at and self.ready else None),
            "attempts": self.attempts,
            "calls": self.calls,
            "failures": self.failures,
            "risk_level": self.config["risk_level"],
            "roots": self.config["roots"],
        }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _os_environ():
    import os
    return dict(os.environ)


def _child_environment(configured):
    """The environment an MCP server subprocess is started with.

    The child needs a real environment: on Windows `npx` cannot find node
    without PATH, and much of node misbehaves without SystemRoot, so this
    cannot be only the entries named in the config.

    It must not be the *whole* environment either. An MCP server is
    third-party code that Jarvish treats as untrusted everywhere else, and
    this machine's environment holds real credentials — handing every one of
    them to every server that gets configured is a larger exposure than
    anything the server could return. Names that look like credentials are
    dropped, and the ones the operator deliberately named in `env` are put
    back afterwards, which is the supported way to give one server the one
    token it actually needs.
    """
    scrubbed = {key: value for key, value in _os_environ().items()
                if not security.is_secret_name(key)}
    scrubbed.update(configured or {})
    return scrubbed


def _schema_of(tool):
    """The tool's input schema, whichever spelling this SDK version uses."""
    for attribute in ("input_schema", "inputSchema"):
        schema = getattr(tool, attribute, None)
        if isinstance(schema, dict):
            return schema
    return {"type": "object", "properties": {}}


def _annotations_of(tool):
    """A tool's MCP annotations as a plain dict.

    `readOnlyHint` and `destructiveHint` are how a server declares what its
    tool does, and they are what lets Jarvish grade an unknown tool as
    read-only rather than defaulting it to high risk.
    """
    raw = getattr(tool, "annotations", None)
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    out = {}
    for attribute in ("title", "read_only_hint", "readOnlyHint",
                      "destructive_hint", "destructiveHint",
                      "idempotent_hint", "idempotentHint",
                      "open_world_hint", "openWorldHint"):
        value = getattr(raw, attribute, None)
        if value is not None:
            out[attribute] = value
    return out


def _categorise(exc, detail):
    """Which error category a raised exception belongs to.

    The SDK signals a request timeout by raising `MCPError` with "timed out" in
    the message rather than a timeout class, so the message has to be read.
    Getting this wrong is not cosmetic: `errors.RETRYABLE` drives whether the
    agent tries again, and a timeout reported as EXECUTION_FAILED gets the slow
    call run a second time.
    """
    if isinstance(exc, _TIMEOUTS):
        return errors.TIMEOUT

    lowered = detail.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return errors.TIMEOUT
    if ("closed" in lowered or "broken pipe" in lowered or "eof" in lowered
            or "connection" in lowered or "not connected" in lowered):
        return errors.SERVER_UNAVAILABLE
    if "not found" in lowered or "unknown tool" in lowered:
        return errors.TOOL_NOT_FOUND
    if "invalid params" in lowered or "validation" in lowered:
        return errors.INVALID_ARGUMENTS

    # An MCPError that is none of the above is the protocol itself objecting.
    if type(exc).__name__ in ("MCPError", "McpError"):
        return errors.MCP_PROTOCOL_ERROR
    if "parse" in lowered or "protocol" in lowered:
        return errors.MCP_PROTOCOL_ERROR
    return errors.EXECUTION_FAILED


def _describe(exc):
    """A short, readable description of a failure, with secrets stripped."""
    text = str(exc) or type(exc).__name__
    # anyio wraps failures in exception groups, which stringify as a summary
    # with the real cause nested; dig one level for something useful.
    inner = getattr(exc, "exceptions", None)
    if inner:
        text = "; ".join(str(e) or type(e).__name__ for e in list(inner)[:2])
    return security.redact(text[:400])


def _normalise_result(outcome, server, tool_name):
    """Turn an SDK CallToolResult into Jarvish's tool-result shape.

    Everything a server returns is treated as untrusted content, whether the
    call succeeded or not: an error message is just as good a place to hide an
    instruction as a successful result.
    """
    is_error = bool(getattr(outcome, "is_error", None)
                    or getattr(outcome, "isError", False))

    text_parts, other = [], []
    for item in getattr(outcome, "content", []) or []:
        kind = getattr(item, "type", None)
        if kind == "text" or hasattr(item, "text"):
            text_parts.append(getattr(item, "text", "") or "")
        elif kind == "image":
            other.append({"type": "image",
                          "mime_type": getattr(item, "mime_type", None)
                          or getattr(item, "mimeType", None)})
        elif kind == "resource":
            other.append({"type": "resource",
                          "uri": str(getattr(getattr(item, "resource", None),
                                             "uri", ""))})
        else:
            other.append({"type": str(kind or "unknown")})

    structured = (getattr(outcome, "structured_content", None)
                  or getattr(outcome, "structuredContent", None))

    body = structured if structured is not None else "\n".join(text_parts)

    metadata = {"server": server, "tool": tool_name}
    if other:
        metadata["attachments"] = other

    payload = security.as_untrusted(body, "mcp:" + server + ":" + tool_name,
                                    metadata)

    if is_error:
        # A server reporting a tool error in-band, rather than raising. The
        # message is the only clue to what kind of failure it was, and the
        # category decides whether the agent retries or re-plans.
        message = ("\n".join(text_parts) or "The tool reported an error.")[:400]
        lowered = message.lower()
        if "unknown tool" in lowered or "tool not found" in lowered:
            category = errors.TOOL_NOT_FOUND
        elif "invalid" in lowered and ("argument" in lowered or "param" in lowered):
            category = errors.INVALID_ARGUMENTS
        elif "timed out" in lowered or "timeout" in lowered:
            category = errors.TIMEOUT
        elif ("permission" in lowered or "denied" in lowered
              or "forbidden" in lowered or "unauthor" in lowered):
            category = errors.PERMISSION_DENIED
        else:
            category = errors.EXECUTION_FAILED
        return dict(errors.fail(category, security.redact(message),
                                server=server, tool=tool_name),
                    **{"result": payload})

    return {"ok": True, **payload}
