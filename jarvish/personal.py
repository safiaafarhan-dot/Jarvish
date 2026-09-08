"""Jarvish's memory of you: personal facts and standing instructions.

Everything lives in one JSON file under data/ and is injected into the system
prompt on every turn, so the model always knows who it is talking to and what
rules you have set.
"""

import json
import threading
from datetime import datetime

from .util import DATA_DIR, boolean, err, ok, string, tool

PROFILE_PATH = DATA_DIR / "profile.json"

_lock = threading.Lock()

# Suggested keys. Nothing is restricted to this list - it just gives the model
# consistent names to reach for instead of inventing a new one every time.
KNOWN_KEYS = [
    "name", "nickname", "pronouns", "birthday", "city", "country", "timezone",
    "occupation", "employer", "email", "phone",
    "favourite_food", "favourite_drink", "favourite_place", "favourite_song",
    "favourite_artist", "favourite_movie", "favourite_show", "favourite_colour",
    "favourite_sport", "favourite_book", "hobbies", "pets", "allergies",
    "dietary_preference", "wake_time", "sleep_time", "work_hours",
]

_EMPTY = {"facts": {}, "instructions": [], "updated": None}


def _read():
    if not PROFILE_PATH.exists():
        return dict(_EMPTY, facts={}, instructions=[])
    try:
        data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(_EMPTY, facts={}, instructions=[])
    return {
        "facts": data.get("facts") or {},
        "instructions": data.get("instructions") or [],
        "updated": data.get("updated"),
    }


def _write(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    PROFILE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _normalise(key):
    return str(key).strip().lower().replace(" ", "_").replace("-", "_")


# --------------------------------------------------------------------------
# Prompt injection
# --------------------------------------------------------------------------

def prompt_block():
    """The profile rendered for the system prompt. Empty string when unset."""
    data = _read()
    facts, instructions = data["facts"], data["instructions"]
    if not facts and not instructions:
        return ""

    lines = []
    if facts:
        lines.append("What you know about the user:")
        for key, value in facts.items():
            lines.append("- " + key.replace("_", " ") + ": " + str(value))
    if instructions:
        lines.append("")
        lines.append("Standing instructions from the user. These override your defaults "
                     "and you must follow them in every reply:")
        for index, rule in enumerate(instructions, 1):
            lines.append(str(index) + ". " + rule)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def remember(key, value):
    """Store or update one personal fact."""
    key = _normalise(key)
    if not key:
        return err("No key given.")
    if value is None or str(value).strip() == "":
        return err("No value given.")
    with _lock:
        data = _read()
        previous = data["facts"].get(key)
        data["facts"][key] = str(value).strip()
        _write(data)
    return ok(remembered={key: str(value).strip()}, replaced=previous)


def recall(key=None):
    """Look up one fact, or return the whole profile when no key is given."""
    data = _read()
    if key:
        normalised = _normalise(key)
        if normalised in data["facts"]:
            return ok(key=normalised, value=data["facts"][normalised])
        # Fall back to a loose match so "favourite food" finds "favourite_food".
        for name, value in data["facts"].items():
            if normalised in name or name in normalised:
                return ok(key=name, value=value)
        return err("Nothing stored for '" + str(key) + "'. Ask the user and then remember it.")
    return ok(
        facts=data["facts"],
        instructions=data["instructions"],
        count=len(data["facts"]),
    )


def forget(key):
    """Delete one stored fact."""
    normalised = _normalise(key)
    with _lock:
        data = _read()
        if normalised not in data["facts"]:
            return err("Nothing stored for '" + str(key) + "'.")
        removed = data["facts"].pop(normalised)
        _write(data)
    return ok(forgot=normalised, was=removed)


def add_instruction(instruction):
    """Save a standing rule that applies to every future reply."""
    text = str(instruction).strip()
    if not text:
        return err("No instruction given.")
    with _lock:
        data = _read()
        if text in data["instructions"]:
            return ok(already_set=text, instructions=data["instructions"])
        data["instructions"].append(text)
        _write(data)
    return ok(added=text, instructions=data["instructions"])


def list_instructions():
    """Show every standing instruction currently in force."""
    data = _read()
    return ok(instructions=data["instructions"], count=len(data["instructions"]))


def remove_instruction(number=None, text=None):
    """Drop a standing instruction by its number or its exact text."""
    with _lock:
        data = _read()
        rules = data["instructions"]
        if not rules:
            return err("There are no standing instructions.")

        index = None
        if number is not None:
            try:
                index = int(float(number)) - 1
            except (TypeError, ValueError):
                index = None
        if index is None and text:
            needle = str(text).strip().lower()
            for position, rule in enumerate(rules):
                if needle in rule.lower():
                    index = position
                    break
        if index is None or not (0 <= index < len(rules)):
            return err("Could not find that instruction. Use list_instructions first.")

        removed = rules.pop(index)
        _write(data)
    return ok(removed=removed, instructions=rules)


SCHEMAS = [
    tool("remember",
         "Save a personal detail about the user so it is available in every future "
         "conversation. Use this whenever the user shares a preference or fact about "
         "themselves, even in passing.",
         {"key": string("Short snake_case name, for example favourite_food, city, birthday."),
          "value": string("The value to store.")},
         ["key", "value"]),
    tool("recall",
         "Look up something the user told you earlier. Call this before saying you do "
         "not know something personal about them.",
         {"key": string("The fact to look up. Omit to get the whole profile.")}),
    tool("forget",
         "Delete a stored personal detail.",
         {"key": string("The fact to delete.")}, ["key"]),
    tool("add_instruction",
         "Save a standing rule the user wants followed in every future reply, for "
         "example 'always call me boss' or 'always answer in Hindi'.",
         {"instruction": string("The rule, written as an instruction.")}, ["instruction"]),
    tool("list_instructions", "Show all standing instructions currently in force."),
    tool("remove_instruction",
         "Remove a standing instruction.",
         {"number": string("Its position in the list."),
          "text": string("Part of its wording, if the number is unknown.")}),
]

REGISTRY = {
    "remember": remember,
    "recall": recall,
    "forget": forget,
    "add_instruction": add_instruction,
    "list_instructions": list_instructions,
    "remove_instruction": remove_instruction,
}
