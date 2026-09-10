"""FastAPI application: serves the Jarvish HUD and streams everything it shows."""

import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (browser, capabilities, cognition, kb, llm, mcp_manager,
               missions, models, observability, voice,
               personal, proactive, registry, risk, session as sessions,
               tasks, telemetry, tools, vision)
from .config import MODEL, OLLAMA_HOST, WAKE_WORD
from .util import DATA_DIR

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Jarvish", version="2.0.0")


def sse(event):
    """One server-sent event frame."""
    return "data: " + json.dumps(event, default=str) + "\n\n"


class Message(BaseModel):
    role: str
    content: str = ""


class ChatRequest(BaseModel):
    messages: list[Message] = Field(default_factory=list)
    session: str | None = None


class ConfirmRequest(BaseModel):
    session: str
    id: str
    approved: bool


class StopRequest(BaseModel):
    session: str | None = None


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------

@app.get("/api/health")
async def api_health():
    status = await llm.health()
    status["tools"] = sorted(tools.REGISTRY)
    status["tool_count"] = len(tools.REGISTRY)
    status["shell_enabled"] = tools.ALLOW_SHELL
    status["wake_word"] = WAKE_WORD
    status["sessions"] = sessions.count()
    status["vision"] = vision.available()
    status["browser"] = await asyncio.to_thread(browser.status)
    status["proactive"] = await asyncio.to_thread(proactive.monitor_status)
    status["tasks"] = (await asyncio.to_thread(tasks.listing, None))["states"]
    manifest = await asyncio.to_thread(registry.manifest)
    mission_state = await asyncio.to_thread(missions.listing, None)
    status["missions"] = {"states": mission_state["states"],
                          "runner": mission_state["runner"],
                          "agents": missions.agent_names()}
    status["autonomy"] = await asyncio.to_thread(cognition.autonomy_status)
    status["registry"] = {"capabilities": manifest["count"],
                          "kinds": manifest["kinds"],
                          "plugins": len(manifest["plugins"]),
                          "version": manifest["jarvish_version"]}
    mcp_state = await asyncio.to_thread(mcp_manager.status)
    status["mcp"] = {"enabled": mcp_state["enabled"],
                     "configured": mcp_state["configured"],
                     "servers": mcp_state["server_count"],
                     "ready": mcp_state["ready_count"],
                     "capabilities": mcp_state["tool_count"],
                     "health": await asyncio.to_thread(mcp_manager.health)}
    return JSONResponse(status)


@app.get("/api/tools")
async def api_tools():
    """Every tool with its risk grading, for the permission panel."""
    manifest = risk.manifest()
    tiers = {}
    for entry in manifest.values():
        tiers[entry["risk"]] = tiers.get(entry["risk"], 0) + 1
    return JSONResponse({
        "tools": manifest,
        "count": len(manifest),
        "tiers": tiers,
        "order": list(risk.ORDER),
        "labels": risk.LABELS,
        "gate": risk.GATE_FROM,
    })


@app.get("/api/capabilities")
async def api_capabilities(q: str = ""):
    """The capability registry, and what would be offered for a given request.

    Passing `q` shows the selection the agent would make for that request,
    which is how the tool-list decision stays inspectable rather than magic.
    """
    payload = capabilities.describe()
    if q:
        _schemas, report = capabilities.select(q)
        payload["selection"] = report
    return JSONResponse(payload)


@app.get("/api/context")
async def api_context(q: str = "", budget: int = 1400):
    """Assemble the unified context for a request. On demand - never polled."""
    return JSONResponse(await asyncio.to_thread(cognition.build_context, q, None, budget))


@app.get("/api/autonomy")
async def api_autonomy():
    """The current autonomy ceiling and what each level permits."""
    return JSONResponse(await asyncio.to_thread(cognition.autonomy_status))


@app.post("/api/autonomy")
async def api_set_autonomy(body: dict | None = None):
    level = (body or {}).get("level")
    if level is None:
        return JSONResponse({"ok": False, "error": "No level given."}, status_code=400)
    return JSONResponse(await asyncio.to_thread(cognition.set_level, level))


@app.get("/api/voice")
async def api_voice():
    """Microphone state. The backend owns it, so this survives a HUD reload."""
    return JSONResponse(await asyncio.to_thread(voice.status))


@app.post("/api/voice/{action}")
async def api_voice_action(action: str):
    """start / stop the microphone, or report that the HUD finished speaking.

    Capture lives in the server process, not the page, so turning the microphone
    on here keeps it on while any other application is in the foreground — and
    closing the HUD does not turn it off.
    """
    if action == "start":
        return JSONResponse(await asyncio.to_thread(voice.start))
    if action == "stop":
        return JSONResponse(await asyncio.to_thread(voice.stop))
    if action == "spoken":
        return JSONResponse(await asyncio.to_thread(voice.speaking_finished))
    return JSONResponse({"ok": False, "error": "Unknown voice action."},
                        status_code=404)


@app.get("/api/strategies")
async def api_strategies(goal: str = "", limit: int = 5):
    """Strategies that worked for a similar goal, or tool reliability."""
    if not goal:
        return JSONResponse(await asyncio.to_thread(cognition.reliability))
    return JSONResponse(await asyncio.to_thread(cognition.recall_strategies, goal, limit))


@app.get("/api/missions")
async def api_missions(state: str = ""):
    """Active and recent missions with their progress."""
    return JSONResponse(await asyncio.to_thread(missions.listing, state or None))


@app.get("/api/missions/{mission_id}")
async def api_mission(mission_id: str, log: bool = False):
    """One mission: its task graph, agents, results and anything blocking it."""
    return JSONResponse(await asyncio.to_thread(missions.status, mission_id, log))


@app.post("/api/missions/action/{action}")
async def api_mission_action(action: str, body: dict | None = None):
    """start / pause / resume / cancel / retry."""
    body = body or {}
    if action == "start":
        goal = body.get("goal")
        if not goal:
            return JSONResponse({"ok": False, "error": "No goal given."},
                                status_code=400)
        return JSONResponse(await asyncio.to_thread(
            missions.create, goal, body.get("plan"), True,
            body.get("plan_it", True)))
    if action == "retry":
        task = body.get("task")
        if not task:
            return JSONResponse({"ok": False, "error": "No task given."},
                                status_code=400)
        return JSONResponse(await asyncio.to_thread(missions.retry_task, task))

    handlers = {"pause": missions.pause, "resume": missions.resume,
                "cancel": missions.cancel}
    if action not in handlers:
        return JSONResponse({"ok": False, "error": "Unknown action."}, status_code=404)
    mission_id = body.get("mission")
    if not mission_id:
        return JSONResponse({"ok": False, "error": "No mission given."},
                            status_code=400)
    return JSONResponse(await asyncio.to_thread(handlers[action], mission_id))


@app.get("/api/registry")
async def api_registry(kind: str = "", available_only: bool = False,
                       source: str = ""):
    """Every capability Jarvish has, with version, permissions and health."""
    return JSONResponse(await asyncio.to_thread(
        registry.manifest, kind or None, available_only, source or None))


@app.get("/api/registry/{name}")
async def api_capability(name: str):
    """One capability in full."""
    return JSONResponse(await asyncio.to_thread(registry.describe_one, name))


@app.post("/api/registry/action/{action}")
async def api_registry_action(action: str, body: dict | None = None):
    """reload / health / enable / disable."""
    body = body or {}
    if action == "reload":
        return JSONResponse(await asyncio.to_thread(registry.reload_plugins))
    if action == "health":
        return JSONResponse(await asyncio.to_thread(registry.check_all_health))
    if action in ("enable", "disable"):
        target = body.get("capability")
        if not target:
            return JSONResponse({"ok": False, "error": "No capability given."},
                                status_code=400)
        return JSONResponse(await asyncio.to_thread(
            registry.set_enabled, target, action == "enable"))
    return JSONResponse({"ok": False, "error": "Unknown action."}, status_code=404)


@app.get("/api/models")
async def api_models():
    """The model inventory and both routing decisions, for the HUD."""
    return JSONResponse(await models.overview())


@app.get("/api/vision/status")
async def api_vision_status():
    """What the visual layer can do here, and what is missing."""
    route = await models.route_vision()
    return JSONResponse({
        "capabilities": vision.available(),
        "vision_model": route.model,
        "vision_available": not route.degraded,
        "limitation": route.explanation,
        "suggested": [
            {"name": n, "size": s, "note": note}
            for n, s, note in models.SUGGESTED_VISION
        ],
        # Providers registered through the capability registry, so a plugin can
        # advertise a vision backend and have it appear here.
        "registered_providers": await asyncio.to_thread(registry.providers, "vision"),
    })


@app.post("/api/vision/observe")
async def api_vision_observe(body: dict | None = None):
    """One perception pass: capture, OCR, UI tree, merged into a scene.

    The HUD calls this to fill the vision panel without going through the model.
    """
    region = (body or {}).get("region")
    scene = await asyncio.to_thread(vision.observe, region)
    return JSONResponse(scene)


@app.get("/api/knowledge/status")
async def api_knowledge_status():
    """What is indexed, and which retrieval methods actually work here."""
    return JSONResponse(await asyncio.to_thread(kb.status))


@app.post("/api/knowledge/search")
async def api_knowledge_search(body: dict | None = None):
    """Hybrid retrieval, for the HUD's knowledge panel."""
    body = body or {}
    return JSONResponse(await asyncio.to_thread(
        kb.search, body.get("query", ""), body.get("limit", 6)))


@app.post("/api/knowledge/index")
async def api_knowledge_index(body: dict | None = None):
    """Index a folder. Runs in the background unless told otherwise."""
    body = body or {}
    return JSONResponse(await asyncio.to_thread(
        kb.index_folder, body.get("path", ""), body.get("background", True)))


@app.get("/api/browser/status")
async def api_browser_status():
    """Whether a controllable browser is attached."""
    return JSONResponse(await asyncio.to_thread(browser.status))


@app.get("/api/tasks")
async def api_tasks(state: str = ""):
    """Background tasks and their state."""
    return JSONResponse(await asyncio.to_thread(tasks.listing, state or None))


@app.post("/api/tasks/{action}")
async def api_task_action(action: str, body: dict | None = None):
    """pause / resume / cancel / retry / clear one background task."""
    body = body or {}
    handlers = {"pause": tasks.pause, "resume": tasks.resume,
                "cancel": tasks.cancel, "retry": tasks.retry}
    if action == "clear":
        return JSONResponse(await asyncio.to_thread(tasks.clear_finished))
    if action not in handlers:
        return JSONResponse({"ok": False, "error": "Unknown action."}, status_code=404)
    task_id = body.get("task")
    if not task_id:
        return JSONResponse({"ok": False, "error": "No task given."}, status_code=400)
    return JSONResponse(await asyncio.to_thread(handlers[action], task_id))


@app.get("/api/proactive")
async def api_proactive():
    """Live notifications plus the monitor's own state."""
    payload = await asyncio.to_thread(proactive.notifications)
    payload["monitor"] = await asyncio.to_thread(proactive.monitor_status)
    return JSONResponse(payload)


@app.post("/api/proactive/settings")
async def api_proactive_settings(body: dict | None = None):
    """Quiet mode, categories, minimum level, quiet hours."""
    return JSONResponse(await asyncio.to_thread(proactive.configure, **(body or {})))


@app.post("/api/proactive/{action}")
async def api_proactive_action(action: str, body: dict | None = None):
    body = body or {}
    if action == "acknowledge":
        return JSONResponse(await asyncio.to_thread(proactive.acknowledge, body.get("key")))
    if action == "dismiss":
        return JSONResponse(await asyncio.to_thread(proactive.dismiss, body.get("key")))
    return JSONResponse({"ok": False, "error": "Unknown action."}, status_code=404)


@app.get("/api/profile")
async def api_profile():
    """What Jarvish remembers about the user."""
    return JSONResponse(personal.recall())


@app.get("/api/activity")
async def api_activity(session: str = ""):
    """The audit trail for one session."""
    found = sessions.peek(session) if session else None
    if found is None:
        return JSONResponse({"activity": [], "state": "idle"})
    return JSONResponse({
        "activity": found.trail(),
        "state": found.state,
        "tools_used": list(found.tool_history),
    })


# --------------------------------------------------------------------------
# Live telemetry
# --------------------------------------------------------------------------

@app.get("/api/telemetry")
async def api_telemetry():
    """One frame, for clients that would rather poll than stream."""
    frame = telemetry.snapshot()
    frame["insights"] = telemetry.insights(frame)
    return JSONResponse(frame)


@app.get("/api/telemetry/stream")
async def api_telemetry_stream():
    """A frame a second, forever, so the HUD readouts move on their own."""

    async def frames():
        # The process scan is the expensive one and is cached for 30s in
        # `telemetry`; asking for it more often than that just burns CPU.
        tick = 0
        try:
            while True:
                frame = telemetry.snapshot()
                frame["insights"] = telemetry.insights(frame)
                if tick % 30 == 0:
                    frame["top"] = telemetry.top_processes(5)
                yield sse(frame)
                tick += 1
                # Two seconds is smooth enough for gauges and halves the
                # sampling cost on a machine that is already short of room.
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:  # the tab closed; nothing to clean up
            return

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------

@app.post("/api/chat")
async def api_chat(request: ChatRequest):
    """Stream the agent's whole working process as server-sent events."""
    history = [
        {"role": m.role, "content": m.content}
        for m in request.messages
        if m.role in ("user", "assistant") and m.content
    ]

    if not history or history[-1]["role"] != "user":
        return JSONResponse({"error": "The last message must be from the user."}, status_code=400)

    session = sessions.get(request.session)
    session.log("user", history[-1]["content"][:200])

    async def event_stream():
        # The client needs the session id before anything else, because STOP and
        # confirmations are both addressed to it.
        yield sse({"type": "session", "id": session.id})
        try:
            async for event in llm.run_agent(history, session=session):
                yield sse(event)
        except asyncio.CancelledError:
            session.stop()
            raise
        except Exception as exc:  # a crash here must still close the stream cleanly
            session.log("error", str(exc))
            yield sse({"type": "error", "message": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/confirm")
async def api_confirm(request: ConfirmRequest):
    """Answer a pending risk confirmation."""
    session = sessions.peek(request.session)
    if session is None:
        return JSONResponse({"ok": False, "error": "Unknown session."}, status_code=404)
    if not session.resolve_approval(request.id, request.approved):
        return JSONResponse({"ok": False, "error": "Nothing was waiting on that."},
                            status_code=409)
    session.log("confirm", ("Approved" if request.approved else "Declined") + " an action")
    return JSONResponse({"ok": True, "approved": request.approved})


@app.post("/api/stop")
async def api_stop(request: StopRequest):
    """Emergency stop: halt this session, or every session at once."""
    if request.session:
        session = sessions.peek(request.session)
        if session is None:
            return JSONResponse({"ok": False, "error": "Unknown session."}, status_code=404)
        session.stop()
        halted = await asyncio.to_thread(tasks.stop_all)
        paused = await asyncio.to_thread(missions.stop_all)
        return JSONResponse({"ok": True, "stopped": [session.id],
                             "tasks_halted": halted, "missions_halted": paused})

    stopped = []
    for session_id in list(sessions._sessions):
        found = sessions.peek(session_id)
        if found is not None:
            found.stop()
            stopped.append(session_id)
    # Autonomous work counts as activity, so STOP halts it as well.
    halted = await asyncio.to_thread(tasks.stop_all)
    paused = await asyncio.to_thread(missions.stop_all)
    return JSONResponse({"ok": True, "stopped": stopped, "tasks_halted": halted,
                         "missions_halted": paused})


# --------------------------------------------------------------------------
# MCP
#
# Backend only. These exist so `python new.py mcp ...`, the tests and anything
# else that wants to inspect the MCP layer can do so over HTTP. The HUD is not
# changed to consume them: an MCP tool reaches the interface as an ordinary
# capability, through the tool list, the risk panel and the confirmation
# dialog that were already there.
# --------------------------------------------------------------------------

@app.get("/api/mcp")
async def api_mcp():
    """Full MCP status: servers, states, capabilities, rejected config."""
    return JSONResponse(await asyncio.to_thread(mcp_manager.status))


@app.get("/api/mcp/tools")
async def api_mcp_tools(server: str = ""):
    """Every capability borrowed from an MCP server, with its grading."""
    rows = await asyncio.to_thread(mcp_manager.listing)
    if server:
        rows = [r for r in rows if r["server"] == server]
    return JSONResponse({"ok": True, "tools": rows, "count": len(rows)})


@app.get("/api/mcp/resources")
async def api_mcp_resources():
    resources = await asyncio.to_thread(mcp_manager.resources)
    prompts = await asyncio.to_thread(mcp_manager.prompts)
    return JSONResponse({"ok": True, "resources": resources,
                         "prompts": prompts})


@app.post("/api/mcp/{action}")
async def api_mcp_action(action: str, body: dict | None = None):
    """Connect, disconnect or reload MCP servers."""
    body = body or {}
    name = str(body.get("server") or body.get("name") or "").strip()

    if action == "reload":
        return JSONResponse(await asyncio.to_thread(mcp_manager.reload))
    if action == "connect":
        if not name:
            return JSONResponse({"ok": False, "error": "Name a server."},
                                status_code=400)
        return JSONResponse(await asyncio.to_thread(
            mcp_manager.connect_server, name))
    if action == "disconnect":
        if not name:
            return JSONResponse({"ok": False, "error": "Name a server."},
                                status_code=400)
        return JSONResponse(await asyncio.to_thread(
            mcp_manager.disconnect_server, name))
    if action == "read":
        uri = str(body.get("uri") or "").strip()
        if not uri:
            return JSONResponse({"ok": False, "error": "Name a resource uri."},
                                status_code=400)
        return JSONResponse(await asyncio.to_thread(
            mcp_manager.read_resource, uri, name or None))

    return JSONResponse({"ok": False, "error": "Unknown action '" + action + "'."},
                        status_code=400)


@app.get("/api/events")
async def api_events(limit: int = 100, event: str = "", session: str = ""):
    """The structured event log: what the agent did, when, and how it went."""
    rows = observability.recent(limit=max(1, min(limit, 400)),
                                event=event or None, session=session or None)
    return JSONResponse({"ok": True, "events": rows,
                         "summary": observability.summary()})


# --------------------------------------------------------------------------
# Direct tool access
# --------------------------------------------------------------------------

@app.post("/api/tool/{name}")
async def api_tool(name: str, arguments: dict | None = None):
    """Run a tool directly. Handy for testing the PC-control layer without the model.

    This bypasses the model but not the risk gate: gated tools still refuse
    unless the caller passes `confirm: true` alongside the arguments.

    The autonomy ceiling deliberately does *not* apply here. That ceiling limits
    what Jarvish may do **unattended**; a call to this endpoint is the user
    acting, with a hand on it, so lowering the level does not disarm the HUD's
    own buttons. The risk gate — which is about the action, not about who asked
    for it — still stands.
    """
    if name not in tools.REGISTRY:
        return JSONResponse({"ok": False, "error": "Unknown tool."}, status_code=404)

    arguments = arguments or {}
    if risk.gated(name, arguments):
        return JSONResponse({
            "ok": False,
            "error": "This tool is gated (" + risk.effective_level(name, arguments) + "). "
                     "Pass \"confirm\": true to run it.",
            "risk": risk.effective_level(name, arguments),
            "reason": risk.reason(name, arguments),
        }, status_code=428)

    # Off the event loop, for two reasons: a slow tool would otherwise block
    # every other request, and the browser and vision tools call `asyncio.run`
    # internally, which raises if a loop is already running on this thread.
    started = time.perf_counter()
    result = await asyncio.to_thread(tools.call, name, arguments)
    if isinstance(result, dict):
        result = dict(result, ms=round((time.perf_counter() - started) * 1000))
    return JSONResponse(result)


@app.on_event("startup")
async def _start_background():
    """Bring up the monitor and the task runner alongside the server.

    Task recovery happens inside the runner, so anything left mid-flight by a
    previous process is requeued rather than lost.
    """
    # Plugins first: they may register tools the rest of the system offers.
    loaded = registry.load_plugins()
    for failure in loaded.get("failed", []):
        print("  plugin failed: " + failure["plugin"] + " - " +
              str(failure["error"])[:120])
    tasks.ensure_runner()
    missions.ensure_runner()
    proactive.start()

    # MCP last, and in the background. Connecting to an external server can
    # take tens of seconds — an npx server installs itself on first run — and
    # none of that may sit between the user and a working HUD. With no
    # `mcp.json` this returns immediately having done nothing at all.
    started = mcp_manager.start(background=True)
    if not started.get("ok"):
        print("  mcp: " + str(started.get("error"))[:140])
    elif started.get("rejected"):
        for bad in started["rejected"]:
            print("  mcp: skipped '" + bad["name"] + "' - " + bad["error"][:110])


@app.on_event("shutdown")
async def _stop_background():
    proactive.stop()
    tasks.shutdown()
    missions.shutdown()
    # Closes every MCP session and stops the runtime thread. Without this the
    # subprocesses outlive the server and pile up across restarts.
    await asyncio.to_thread(mcp_manager.shutdown)


# The HUD is served from disk and changes whenever Jarvish is updated. Without
# an explicit directive the browser applies heuristic caching and can keep an
# old stylesheet for hours — which once left users looking at a stale build
# whose confirmation dialog covered the whole screen and swallowed every click.
# `no-cache` does not mean "do not store": the ETag still makes revalidation a
# cheap 304. It means "never use it without asking first".
NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


# web/ is the HUD, but it is also the folder Vercel deploys, so it holds two
# files that belong to that build and not to this one: the serverless API layer
# and its Linux dependency list. Serving them here would publish their source at
# /static/api/index.py to anything that can reach this server — which is the LAN
# whenever Jarvish is started with `--host 0.0.0.0`. Neither file contains a
# secret, and this is what keeps that true rather than a matter of luck.
NOT_SERVED = ("api/", "requirements.txt")


class RevalidatedStatic(StaticFiles):
    """Static files that must be revalidated before reuse, minus the cloud build."""

    async def get_response(self, path, scope):
        if path.replace("\\", "/").lstrip("/").startswith(NOT_SERVED):
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_CACHE)
        return response


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html", headers=NO_CACHE)


app.mount("/static", RevalidatedStatic(directory=WEB_DIR), name="static")

# Screenshots and the profile file live here, so the UI can link to them.
DATA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/data", StaticFiles(directory=DATA_DIR), name="data")


def serve(host, port):
    import uvicorn

    print("\n  Jarvish is online")
    print("  HUD     http://" + host + ":" + str(port))
    print("  Ollama  " + OLLAMA_HOST + "  (model: " + MODEL + ")")
    print("  Tools   " + str(len(tools.REGISTRY)) + "  ·  wake word: \"" + WAKE_WORD + "\"")
    print("  Press Ctrl+C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
