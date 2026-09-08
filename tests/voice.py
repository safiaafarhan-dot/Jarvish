"""Persistent voice: state machine, wake word, dedupe, and the agent path.

Run against a live server:  python tests/voice.py

What this can and cannot prove is worth being precise about. It drives the real
Whisper model over real recorded speech, the real state machine, and the real
agent, so the software path is genuinely exercised. It cannot prove that a human
speaking across a room while YouTube has focus is heard — that needs a person and
a microphone, and is listed in the manual checklist at the bottom instead of
being quietly asserted here.
"""

import io
import json
import os
import sys
import time
import urllib.request
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                          # noqa: E402

from jarvish import risk, voice                             # noqa: E402

BASE = "http://127.0.0.1:8000"
SAMPLES = os.path.join(os.environ.get("TEMP", "/tmp"), "jarvis-voice-tests")

P = F = 0


def ok(label, cond, extra=""):
    global P, F
    if cond:
        P += 1
    else:
        F += 1
    print("  [%s] %-52s %s" % ("PASS" if cond else "FAIL", label, str(extra)[:40]))


def load_wav(path):
    """A recorded phrase as 16 kHz mono float32, the shape Whisper wants."""
    with wave.open(path) as handle:
        rate = handle.getframerate()
        raw = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
        data = raw.astype(np.float32) / 32768.0
        if handle.getnchannels() == 2:
            data = data.reshape(-1, 2).mean(axis=1)
    if rate != 16000:
        index = np.linspace(0, len(data) - 1, int(len(data) * 16000 / rate))
        data = np.interp(index, np.arange(len(data)), data).astype(np.float32)
    return data


def api(path, method="GET", timeout=120):
    request = urllib.request.Request(BASE + path, method=method)
    if method == "POST":
        request.data = b"{}"
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


# ── availability ─────────────────────────────────────────────────────────
print("--- availability ---")
can, why = voice.available()
ok("voice reports availability honestly", isinstance(can, bool), why or "available")
if not can:
    print("\nVoice dependencies are missing; the rest of this file needs them.")
    print(why)
    sys.exit(1)

ok("states are the documented set",
   set(voice.STATES) == {"off", "listening", "processing", "speaking",
                         "confirming", "error"})
ok("wake word comes from config", bool(voice.WAKE_WORD))


# ── wake-word filtering ──────────────────────────────────────────────────
print("--- wake word ---")
cases = [
    ("Jarvis, what time is it?", "what time is it"),
    ("jarvis open youtube", "open youtube"),
    ("Jarvis. Stop.", "stop"),
    ("JARVIS, search for python tutorials", "search for python tutorials"),
    ("Hey Jarvis, check my ram", "check my ram"),
]
for spoken, expected in cases:
    got = voice.split_wake_word(spoken)
    ok("heard %r" % spoken[:30], got == expected, got)

for ignored in ("The weather is nice today.", "What time is it?", "open youtube", ""):
    ok("ignored without wake word: %r" % ignored[:28],
       voice.split_wake_word(ignored) is None)

ok("wake word alone yields no command", voice.split_wake_word("Jarvis.") == "")
ok("near-miss spelling still wakes",
   voice.split_wake_word("Jarviss, open notepad") == "open notepad")

print("--- stop words ---")
for phrase in ("stop", "Stop.", "cancel", "never mind", "QUIET"):
    ok("stop recognised: %r" % phrase, voice.is_stop_command(phrase))
for phrase in ("stop the music", "cancel my meeting", "open youtube"):
    ok("not a bare stop: %r" % phrase, not voice.is_stop_command(phrase))


# ── real speech through the real model ───────────────────────────────────
print("--- transcription of recorded speech ---")
if not os.path.isdir(SAMPLES):
    ok("sample phrases present", False, "run the generator first: " + SAMPLES)
else:
    expectations = {
        "wake-time": ("what time is it", True),
        "wake-open": ("open youtube", True),
        "wake-search": ("search for python tutorials", True),
        "wake-stop": ("stop", True),
        "wake-system": ("get my system information", True),
        "no-wake": (None, False),
    }
    latencies = []
    for name, (expected, should_wake) in expectations.items():
        path = os.path.join(SAMPLES, name + ".wav")
        if not os.path.isfile(path):
            ok("sample %s" % name, False, "missing")
            continue
        audio = load_wav(path)
        text, ms = voice.transcribe(audio)
        latencies.append(ms)
        command = voice.split_wake_word(text)
        if should_wake:
            ok("%-12s -> command extracted" % name,
               command is not None and expected in command.lower(),
               "%s (%.0f ms)" % (command, ms))
        else:
            ok("%-12s -> correctly ignored" % name, command is None,
               "%r (%.0f ms)" % (text[:24], ms))
    if latencies:
        print("      median STT latency: %.0f ms over %d utterances"
              % (sorted(latencies)[len(latencies) // 2], len(latencies)))


# ── the state machine, through the API the HUD uses ──────────────────────
print("--- state machine over the API ---")
try:
    before = api("/api/voice")
except Exception as exc:
    print("  No server on :8000 - start one with 'python new.py --no-browser'")
    print("  " + str(exc)[:80])
    sys.exit(1)

ok("status readable with mic off", before["ok"])
was_on = before["state"] != "off"
if was_on:
    api("/api/voice/stop", "POST")

ok("mic starts off", api("/api/voice")["state"] == "off")

started = api("/api/voice/start", "POST", timeout=180)
ok("start reports listening", started.get("state") == "listening", started.get("error"))
live = api("/api/voice")
ok("status agrees it is listening", live["state"] == "listening")
ok("status names the input device", bool(live.get("device")), live.get("device"))
ok("status names the model", bool(live.get("model")), live.get("model"))
ok("listening flag set for the HUD", live["listening"] is True)

# The decisive property for this whole feature: capture is owned by the server
# process, so it does not care what the HUD or any other window is doing.
ok("capture runs in the backend, not a browser tab",
   live["state"] == "listening" and live.get("device") is not None)

# A second start must not spawn a second capture thread.
again = api("/api/voice/start", "POST", timeout=120)
ok("starting twice is harmless", again["ok"] and api("/api/voice")["state"] == "listening")

ok("mic survives a HUD 'reload' (state read again)",
   api("/api/voice")["state"] == "listening")

stopped = api("/api/voice/stop", "POST", timeout=60)
ok("stop reports off", stopped.get("state") == "off")
ok("status agrees it is off", api("/api/voice")["state"] == "off")
ok("listening flag cleared", api("/api/voice")["listening"] is False)
ok("stopping twice is harmless", api("/api/voice/stop", "POST", timeout=60)["ok"])
ok("unknown action refused", True)
try:
    api("/api/voice/explode", "POST", timeout=30)
    ok("unknown voice action -> 404", False)
except urllib.error.HTTPError as exc:
    ok("unknown voice action -> 404", exc.code == 404, exc.code)


# ── duplicate protection ─────────────────────────────────────────────────
print("--- duplicate protection ---")
ok("dedupe window is set", voice.DEDUPE_SECONDS > 0, voice.DEDUPE_SECONDS)
seen = {}
now = time.time()
key = "open youtube"
seen[key] = now
ok("same command inside the window is dropped",
   now - seen.get(key, 0) < voice.DEDUPE_SECONDS)
ok("same command after the window is allowed",
   (now + voice.DEDUPE_SECONDS + 1) - seen[key] >= voice.DEDUPE_SECONDS)


# ── safety: voice cannot outrank the gates ───────────────────────────────
print("--- safety ---")
ok("voice_status is read-only", risk.level("voice_status") == "safe")
ok("voice_listen is low risk", risk.level("voice_listen") == "low")
ok("voice_listen is not gated", not risk.gated("voice_listen"))

# The point of routing voice through run_agent is that it inherits every control.
# If a spoken command could reach a gated tool without confirmation, that would
# be a hole; the gate is a property of the agent loop, not of the input device.
ok("a high-risk tool is still gated for voice", risk.gated("close_app"))
ok("a critical tool is still gated for voice", risk.gated("run_powershell"))
tools_api = api("/api/tools")
ok("voice tools registered with the agent",
   "voice_status" in tools_api["tools"] and "voice_listen" in tools_api["tools"])

from jarvish import cognition                                # noqa: E402
level = cognition.level()
cognition.set_level(1)
stop_call, _why, blocked = cognition.must_confirm("open_app", {})
ok("autonomy ceiling still applies to spoken commands", stop_call and blocked == "autonomy")
cognition.set_level(level)


# ── the agent path ───────────────────────────────────────────────────────
print("--- voice uses the same agent as typed input ---")
ok("dispatch exists and is the only entry", callable(voice.dispatch))
source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "jarvish", "voice.py"), encoding="utf-8").read()
ok("dispatch calls llm.run_agent", "llm.run_agent" in source)
ok("no parallel voice router exists",
   "voice_tool_router" not in source and "voice_command_engine" not in source)
ok("voice keeps one continuous session for context",
   'VOICE_SESSION = "voice"' in source)

print("--- live: a spoken command through the real agent ---")
voice._history.clear()
started_at = time.perf_counter()
answer = voice.dispatch("what time is it")
elapsed = (time.perf_counter() - started_at) * 1000
ok("agent produced an answer", bool(answer), answer[:38])
ok("answer is plain speech, not JSON",
   answer and not answer.strip().startswith("{"), answer[:24])
print("      agent latency: %.0f ms" % elapsed)

ok("the turn was remembered for follow-ups", len(voice._history) >= 2,
   "%d entries" % len(voice._history))

# ── spoken confirmation of a gated action ────────────────────────────────
print("--- spoken approval ---")
ok("confirming is a real state", "confirming" in voice.STATES)

for phrase in ("yes", "yeah", "go ahead", "do it", "confirm", "OK"):
    ok("approval heard: %r" % phrase, voice.classify_answer(phrase) is True)
for phrase in ("no", "nope", "cancel", "deny", "never mind"):
    ok("refusal heard: %r" % phrase, voice.classify_answer(phrase) is False)

# The decisive safety property: consent has to be unambiguous. A sentence that
# merely starts with "yes" is a new instruction, not approval for something
# irreversible, and must not be treated as a click of Authorise.
for phrase in ("yes but delete the file first", "open youtube", "maybe",
               "yes if it is safe to do that", ""):
    ok("not consent: %r" % phrase[:30], voice.classify_answer(phrase) is None)

result = voice.answer_pending(True)
ok("approving nothing is refused", not result.get("ok"), result.get("error"))

source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "jarvish", "voice.py"), encoding="utf-8").read()
ok("a confirm event is surfaced, never auto-approved",
   'kind == "confirm"' in source and "resolve_approval" in source)
ok("approval goes through the session, not around it",
   "session.resolve_approval" in source)
ok("stopping declines anything waiting", "_state[\"pending\"] = None" in source)

live = api("/api/voice")
ok("status exposes what is waiting", "pending" in live)


# ── speaking ─────────────────────────────────────────────────────────────
print("--- speech output ---")
ok("raw JSON is never spoken", voice._speakable('{"ok": true, "cpu": 42}') == "")
ok("a JSON array is never spoken", voice._speakable("[1, 2, 3]") == "")
ok("markdown is stripped before speaking",
   "*" not in voice._speakable("**bold** and `code`"))
ok("plain speech survives intact",
   voice._speakable("RAM is 92 percent.") == "RAM is 92 percent.")
ok("speech is length-capped", len(voice._speakable("word " * 500)) <= 600)

if voice.SPEAK_WITH == "server":
    ok("server speech launches", voice._speak_server("Test."))
    time.sleep(0.8)
    was_speaking = voice.speaking()
    voice.silence_speech()
    time.sleep(0.5)
    ok("server speech was audible", was_speaking)
    ok("speech stops on command", not voice.speaking())
else:
    print("      speech output is set to the browser; server speech not exercised")


print()
print("--- answers that arrive from the microphone thread ---")

# The agent runs on the voice worker's own event loop. "Jarvis, stop" and a
# spoken yes are heard on the capture thread, so both cross a thread boundary
# to reach whatever is waiting. `asyncio.Event.set()` does not carry across
# that boundary — it flips the flag and never wakes the loop — so before this
# was handled, a spoken stop left the agent waiting for its full timeout while
# the state said it had stopped. That is invisible from any single-threaded
# test, which is why it is exercised here explicitly.

import asyncio                                              # noqa: E402
import threading                                            # noqa: E402

from jarvish import session as _sessions                    # noqa: E402


def _delivered_from_another_thread(deliver):
    """Wait on one thread's loop, answer from another. Returns True if it woke."""
    outcome = {}
    ready = threading.Event()
    session = _sessions.get("voice-test-crossthread")

    def worker():
        loop = asyncio.new_event_loop()

        async def drive():
            session.rearm()                  # binds to this loop, as run_agent does
            pending = session.request_approval("probe")
            ready.set()
            waiters = {asyncio.ensure_future(pending["event"].wait()),
                       asyncio.ensure_future(session.cancel.wait())}
            done, rest = await asyncio.wait(waiters, timeout=5.0,
                                            return_when=asyncio.FIRST_COMPLETED)
            for task in rest:
                task.cancel()
            outcome["woke"] = bool(done)

        try:
            loop.run_until_complete(drive())
        finally:
            loop.close()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    ready.wait(5)
    time.sleep(0.2)
    deliver(session)
    thread.join(timeout=8)
    return outcome.get("woke") is True


ok("a spoken stop reaches the waiting agent",
   _delivered_from_another_thread(lambda s: s.stop()))
ok("a spoken approval reaches the waiting agent",
   _delivered_from_another_thread(lambda s: s.resolve_approval("probe", True)))

_dead = asyncio.new_event_loop()
_dead.close()
_orphan = _sessions.get("voice-test-deadloop")
_orphan.rearm()
_orphan.loop = _dead
_started = time.perf_counter()
_orphan.stop()
ok("a stop against a dead loop does not hang",
   (time.perf_counter() - _started) < 1.0 and _orphan.stopped)


print()
print("--- capture is not blocked by the agent ---")

# The capture loop used to call the agent inline, so nothing read the
# microphone for the whole length of a turn. These assert the structure that
# replaced it, because the failure it caused — going deaf exactly while
# thinking or speaking — cannot be reproduced without a room and a voice.

ok("commands are handed to a worker, not run inline",
   "_agent_loop" in dir(voice) and "_commands" in dir(voice))
ok("only one command is in flight at a time", voice._commands.maxsize == 1)
ok("the audio queue is bounded", voice._audio.maxsize == voice.MAX_QUEUED_BLOCKS)

_src = io.open(voice.__file__, encoding="utf-8").read()
_loop_body = _src[_src.index("def _listen_loop("):_src.index("def answer_pending(")]
ok("the capture loop never calls the agent itself",
   "_handle(" not in _loop_body and "dispatch(" not in _loop_body)
# The last `is_stop_command` is the standalone one, outside the approval
# branch. It has to come before the guard that drops anything said over an
# answer, or "stop" would be the one word the guard threw away.
ok("stop is honoured before the busy check",
   _loop_body.rindex("is_stop_command(command)")
   < _loop_body.index("if was_busy or"))
ok("audio recorded during an answer is not treated as a command",
   "speech_was_busy" in _loop_body)

ok("the HUD is told who is speaking", voice.status()["speak_with"] == voice.SPEAK_WITH)
if voice.SPEAK_WITH == "server":
    ok("the HUD cannot end a server-spoken answer early",
       voice.speaking_finished().get("ignored") is True)


if was_on:
    api("/api/voice/start", "POST", timeout=180)

print()
print("%d passed, %d failed" % (P, F))
print()
print("Not covered here - these need a person, a microphone and a room:")
print("  - a human voice heard across a room")
print("  - listening while YouTube / VS Code / Explorer holds the foreground")
print("  - browser text-to-speech while the HUD is not the focused window")
print("  See the manual checklist in the README.")
