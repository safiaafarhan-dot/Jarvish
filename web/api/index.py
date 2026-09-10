"""The API of the deployed Jarvish interface.

This is not the Jarvish backend. The backend is `jarvish/server.py`, it runs on
the user's own Windows PC, and it is what makes Jarvish Jarvish: Ollama, the
desktop tools, screen vision, the browser driver, voice, MCP subprocesses, the
task and mission runners, and SQLite databases that have to survive between
requests. None of that can exist inside a serverless function - no persistent
process, no local model, no desktop, no microphone, no filesystem that lasts
past the response. Making the deployment *look* like it had those would be the
one thing worse than not having them.

So this file is deliberately small. It answers two questions honestly and
refuses everything else in a way the interface already knows how to draw:

  GET  /api/health   what this deployment actually is, and what it can do
  POST /api/chat     a reply, if a cloud model is configured; a plain
                     statement that none is, if it is not

Everything else returns 503 with a reason. That is not a stub - it is the
correct answer. Every panel in web/app.js already wraps its fetch in a
try/catch that marks the subsystem "offline" when the call fails, so the HUD
renders what is true here without one line of the interface changing. The same
applies to the telemetry EventSource: a non-200 makes the browser fail the
stream permanently rather than reconnect forever, so a deployment with no
telemetry stays quiet instead of retrying every three seconds.

The cloud model is optional and off by default. With no ANTHROPIC_API_KEY set,
the deployment still builds, still serves and still answers - it simply says it
has no model. Nothing here ever reaches for Ollama: 127.0.0.1 on a Vercel
function is the function itself.
"""

import json
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# The HUD, one directory up from this file. A FastAPI app on Vercel is a single
# function that serves every route, so the interface is served from here rather
# than as a separate static build - the same arrangement jarvish/server.py uses
# locally, which is why index.html's /static/... references work unchanged.
WEB_DIR = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# Configuration. Every value comes from the environment, and the only one that
# is ever a secret is the API key - which is never returned, logged or echoed
# anywhere in this file.
# --------------------------------------------------------------------------

# ANTHROPIC_MODEL is the name to set in Vercel; JARVISH_CLOUD_MODEL is still
# read so an existing deployment configured under the older name keeps working.
# Verified against the SDK's own model list rather than assumed: anthropic
# 1.5.0 recognises claude-opus-5. Set ANTHROPIC_MODEL=claude-haiku-4-5 for a
# cheaper public demo — it is a configuration change, not a code change.
CLOUD_MODEL = (os.environ.get("ANTHROPIC_MODEL", "").strip()
               or os.environ.get("JARVISH_CLOUD_MODEL", "").strip()
               or "claude-opus-5")

# Jarvish replies are read aloud by the browser's speech synthesis, so they are
# meant to be short. A ceiling in the low thousands is the shape of the product
# rather than a cost saving.
MAX_TOKENS = int(os.environ.get("JARVISH_CLOUD_MAX_TOKENS", "").strip() or "8192")

WAKE_WORD = os.environ.get("JARVISH_WAKE_WORD", "jarvis").strip().lower()

# Same-origin by default. On Vercel the interface and these functions are served
# from one domain, so no CORS header is needed and none is sent - an empty
# allowlist is the correct production setting, not an unfinished one. Fill
# JARVISH_ALLOWED_ORIGINS in only to host the interface somewhere else.
ALLOWED_ORIGINS = tuple(
    origin.strip().rstrip("/")
    for origin in os.environ.get("JARVISH_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
)

SYSTEM_PROMPT = (
    "You are Jarvish, running as a cloud deployment of a desktop assistant. "
    "Bearing is terse and operational - 'Understood.', 'On it.' - never "
    "'Sure! I'd be happy to help!'. Answer in a few sentences; the reply is "
    "read aloud.\n\n"
    "You have no tools here. The desktop machine, its applications, its files, "
    "its screen, its microphone, its Wi-Fi and its local models belong to the "
    "local build of Jarvish that runs on the user's own PC. If asked to do "
    "something on that machine, say plainly that this deployment cannot reach "
    "it and that the local Jarvish can. Never claim to have taken an action."
)


def _configured():
    """Whether a cloud model is available to this deployment."""
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _safe(text):
    """An error message with the API key scrubbed out of it.

    The SDK does not put the key in exception text, but this costs nothing and
    the one thing a public endpoint must never do is echo a credential back.
    """
    message = str(text)
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key and key in message:
        message = message.replace(key, "<redacted>")
    return message[:400]


def sse(event):
    """One server-sent event frame, in the shape web/app.js already reads."""
    return "data: " + json.dumps(event, default=str) + "\n\n"


app = FastAPI(title="Jarvish (cloud)", version="2.0.0", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------
# What each family of local-only endpoints needs, and why it cannot be here.
# The interface draws these as offline; this is the text behind that.
# --------------------------------------------------------------------------

LOCAL_ONLY = {
    "voice": "the microphone and speech-to-text belong to the desktop process",
    "vision": "screen capture and UI Automation need a real desktop session",
    "browser": "driving Chrome needs a browser on the user's own machine",
    "knowledge": "the index is a SQLite database on the local disk",
    "tasks": "the task runner is a long-lived thread with a local database",
    "missions": "the mission runner is a long-lived thread with a local database",
    "proactive": "the monitor samples this machine's CPU, memory and battery",
    "mcp": "MCP servers are subprocesses started by the desktop process",
    "registry": "capabilities are registered by the running desktop process",
    "capabilities": "capabilities are registered by the running desktop process",
    "telemetry": "the telemetry stream reports the desktop process's own state",
    "tool": "every tool acts on the user's PC",
    "tools": "every tool acts on the user's PC",
    "profile": "personal memory is a file on the local disk",
    "activity": "the event log is a file on the local disk",
    "events": "the event log is a file on the local disk",
    "autonomy": "the autonomy ceiling governs local tool calls, of which there are none here",
    "context": "unified context is assembled from local subsystems",
    "strategies": "strategy memory is a local database",
    "models": "the model inventory comes from Ollama on the user's machine",
    "confirm": "there is nothing to confirm without tools",
    "stop": "there is no running tool chain to stop",
}


def _reason(endpoint):
    return LOCAL_ONLY.get(
        endpoint.split("/")[0],
        "this endpoint is served by the Jarvish process on the user's PC",
    )


def _cors(response, request):
    """Echo an allowlisted origin, and nothing else. No wildcard, ever."""
    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin and origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


def _endpoint(request):
    """The endpoint name, however the platform presented the request.

    vercel.json rewrites `/api/<name>` to this function and passes the original
    name through as `?endpoint=`, because a rewrite destination replaces the
    path the function sees. The path is still read as a fallback so the module
    routes correctly when it is served directly - which is how the local test
    below exercises it.
    """
    name = (request.query_params.get("endpoint") or "").strip()
    if not name:
        path = request.url.path
        marker = "/api/"
        if marker in path:
            path = path[path.rindex(marker) + len(marker):]
        name = path
    return name.strip("/").lower()


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------

def _health():
    """What this deployment is. Every field is the truth about the cloud build.

    `online` keeps the meaning the interface already gives it - "a model can
    answer". With no key configured that is false and the HUD's model indicator
    reads offline, which is correct rather than a failure.
    """
    configured = _configured()
    return {
        "deployment": "vercel",
        "local_only": True,
        "online": configured,
        "model": CLOUD_MODEL if configured else "no model configured",
        "model_installed": configured,
        "models": [CLOUD_MODEL] if configured else [],
        "provider": "anthropic" if configured else None,
        "wake_word": WAKE_WORD,
        "tools": [],
        "tool_count": 0,
        "shell_enabled": False,
        "sessions": 0,
        "note": (
            "This is the Jarvish interface deployed to Vercel. Desktop control, "
            "Ollama, voice, vision, the browser driver, MCP and the task and "
            "mission runners all run in the local Jarvish on your own PC and are "
            "not reachable from here."
        ),
    }


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------

NO_MODEL = (
    "This is the deployed interface, not the assistant. No cloud model is "
    "configured for it, so there is nothing here to answer with - and nothing "
    "here can reach your PC. Run Jarvish locally for the model, the tools and "
    "desktop control. To enable cloud replies instead, set ANTHROPIC_API_KEY in "
    "this Vercel project's environment variables."
)


async def _unconfigured_stream(session_id):
    yield sse({"type": "session", "id": session_id})
    yield sse({"type": "model", "model": "none", "degraded": True,
               "reason": "no cloud model configured for this deployment"})
    yield sse({"type": "token", "text": NO_MODEL})
    yield sse({"type": "done"})


async def _claude_stream(history, session_id):
    yield sse({"type": "session", "id": session_id})

    try:
        from anthropic import AsyncAnthropic
    except Exception as exc:  # the dependency is missing from the build
        yield sse({"type": "error",
                   "message": "The cloud provider is not installed: " + _safe(exc)})
        yield sse({"type": "done"})
        return

    # The key is read from the environment by the SDK. It is never accepted
    # from a request and never appears in a response.
    #
    # Constructing the client is inside the guard because it can raise on its
    # own - a malformed key is rejected here, before any request is made. Left
    # outside, that exception escapes mid-stream, and the client gets a broken
    # connection while the platform logs a traceback carrying the key.
    try:
        client = AsyncAnthropic()
    except Exception as exc:
        yield sse({"type": "error",
                   "message": "The cloud model could not be reached: " + _safe(exc)})
        yield sse({"type": "done"})
        return

    yield sse({"type": "model", "model": CLOUD_MODEL, "degraded": True,
               "reason": "cloud model - no desktop tools in this deployment"})

    try:
        # Server-side fallbacks: if a safety classifier declines, the same
        # request is re-run on a fallback model inside the same call rather
        # than the turn stopping with nothing shown.
        async with client.beta.messages.stream(
            model=CLOUD_MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=history,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            async for text in stream.text_stream:
                yield sse({"type": "token", "text": text})
            final = await stream.get_final_message()

        if final.stop_reason == "refusal":
            yield sse({"type": "token", "text": "\n\nI can't answer that one."})
    except Exception as exc:
        yield sse({"type": "error", "message": _safe(exc)})

    yield sse({"type": "done"})


async def _chat(request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Expected a JSON body."}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Expected a JSON object."}, status_code=400)

    history = [
        {"role": message.get("role"), "content": message.get("content") or ""}
        for message in (body.get("messages") or [])
        if isinstance(message, dict)
        and message.get("role") in ("user", "assistant")
        and str(message.get("content") or "").strip()
    ]

    if not history or history[-1]["role"] != "user":
        return JSONResponse({"error": "The last message must be from the user."},
                            status_code=400)

    session_id = str(body.get("session") or "") or ("cloud-" + uuid.uuid4().hex[:12])

    stream = (_claude_stream(history, session_id) if _configured()
              else _unconfigured_stream(session_id))

    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------
# Router
#
# Declaration order is load-bearing. Vercel gives a route declared before a
# static mount priority over the files under it, so the API is registered
# first and the HUD is mounted last - exactly the precedence the local server
# has, where /api/* is routed and /static/* falls through to disk.
# --------------------------------------------------------------------------

@app.get("/")
async def index():
    """The HUD itself, unchanged from the file the local server serves."""
    return FileResponse(WEB_DIR / "index.html")


@app.api_route("/api/{_path:path}", methods=["GET", "POST", "OPTIONS"])
async def route(request: Request, _path: str = ""):
    endpoint = _endpoint(request)

    if request.method == "OPTIONS":
        return _cors(JSONResponse(None, status_code=204), request)

    if endpoint == "health":
        return _cors(JSONResponse(_health()), request)

    if endpoint == "chat" and request.method == "POST":
        return _cors(await _chat(request), request)

    return _cors(JSONResponse({
        "ok": False,
        "local_only": True,
        "endpoint": "/api/" + endpoint if endpoint else "/api",
        "error": "Not available in this deployment - " + _reason(endpoint) + ".",
        "hint": "Run Jarvish on your own machine for this: python new.py",
    }, status_code=503), request)


# Mounted last, so every route above wins over a file of the same name. Vercel
# promotes these to the CDN at build time; the HUD asks for /static/app.js and
# gets web/app.js, which is why no interface file needed changing. The two
# files here that belong to the build rather than the interface - this module
# and requirements.txt - are excluded, matching the local server's guard.
class _BuildFilesHidden(StaticFiles):
    """The HUD's own assets, without the deployment's plumbing."""

    async def get_response(self, path, scope):
        if path.replace("\\", "/").lstrip("/").startswith(("api/", "requirements.txt")):
            return await super().get_response("__missing__", scope)
        return await super().get_response(path, scope)


app.mount("/static", _BuildFilesHidden(directory=WEB_DIR), name="static")
