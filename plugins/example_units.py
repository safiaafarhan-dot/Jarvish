"""A worked example of a Jarvish plugin.

Drop a file like this into `plugins/` and its capabilities become available
without touching the agent loop. Every plugin defines one function:

    def register(api): ...

`api` offers three things — `api.tool`, `api.skill` and `api.provider` — and
every one of them goes through the same validation, permission and risk
machinery the built-ins use. Declaring `risk_level="safe"` while asking for
`system` permission will not work: the stricter of the two wins.
"""

LENGTH = {
    "mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0,
    "in": 0.0254, "ft": 0.3048, "yd": 0.9144, "mi": 1609.344,
}

WEIGHT = {"mg": 1e-6, "g": 0.001, "kg": 1.0, "t": 1000.0,
          "oz": 0.0283495, "lb": 0.453592, "st": 6.35029}

# A model asked to convert "5 kilometres to miles" says exactly that — not
# "km to mi". Accepting only the abbreviations made this tool impossible to
# call, so every spelling anyone would actually use maps to a canonical unit.
ALIASES = {
    "millimetre": "mm", "millimeter": "mm",
    "centimetre": "cm", "centimeter": "cm",
    "metre": "m", "meter": "m",
    "kilometre": "km", "kilometer": "km", "klick": "km",
    "inch": "in", "inche": "in",
    "foot": "ft", "feet": "ft",
    "yard": "yd",
    "mile": "mi",
    "milligram": "mg", "milligramme": "mg",
    "gram": "g", "gramme": "g",
    "kilogram": "kg", "kilogramme": "kg", "kilo": "kg",
    "tonne": "t", "ton": "t", "metric ton": "t",
    "ounce": "oz",
    "pound": "lb", "lbs": "lb",
    "stone": "st",
}


def _canonical(unit):
    """Normalise however a unit was written to the canonical abbreviation."""
    text = str(unit or "").strip().lower().replace(".", "")
    if text in LENGTH or text in WEIGHT:
        return text
    if text in ALIASES:
        return ALIASES[text]
    # Plurals: "miles", "kilometres", "pounds".
    if text.endswith("s"):
        singular = text[:-1]
        if singular in LENGTH or singular in WEIGHT:
            return singular
        if singular in ALIASES:
            return ALIASES[singular]
    return text


def _convert(value, source, target):
    for table in (LENGTH, WEIGHT):
        if source in table and target in table:
            return value * table[source] / table[target]
    return None


def convert_units(value, from_unit, to_unit):
    """Convert a length or a weight between units, offline."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return {"ok": False, "error": "'" + str(value) + "' is not a number."}

    source = _canonical(from_unit)
    target = _canonical(to_unit)
    result = _convert(amount, source, target)

    if result is None:
        known = sorted(set(LENGTH) | set(WEIGHT))
        return {"ok": False,
                "error": "Cannot convert " + repr(str(from_unit)) + " to " +
                         repr(str(to_unit)) + ". Units understood: " +
                         ", ".join(known) + ", plus their full names and plurals "
                         "(metres, feet, pounds...). Length and weight only."}

    return {"ok": True, "value": round(result, 6),
            "from": {"value": amount, "unit": source},
            "to": {"value": round(result, 6), "unit": target},
            "said": str(amount) + " " + source + " is " +
                    str(round(result, 4)) + " " + target}


def _healthy():
    """Proof the conversion table is intact, used as the health check."""
    metres = _convert(1.0, "km", "m")
    return {"ok": metres == 1000.0,
            "detail": None if metres == 1000.0 else "Conversion table is wrong."}


def register(api):
    api.tool(
        name="convert_units",
        description=("Convert a length or weight between units — metres, feet, "
                     "miles, kilograms, pounds and so on. Works offline."),
        handler=convert_units,
        parameters={
            "value": {"type": "number", "description": "The amount to convert."},
            "from_unit": {"type": "string", "description": "The unit it is in."},
            "to_unit": {"type": "string", "description": "The unit to convert to."},
        },
        required=["value", "from_unit", "to_unit"],
        risk_level="safe",
        permissions=("read",),
        version="1.0.0",
        outputs=["value", "said"],
        health=_healthy,
        requires_jarvish="2.0",
    )

    api.skill(
        name="unit_report",
        description="Convert a measurement and explain it in plain language.",
        instructions=(
            "Convert the measurement the user gives you using convert_units, then "
            "say the result in one short sentence. If the units are not "
            "convertible, say so plainly and list what is supported."),
        uses=("convert_units",),
        version="1.0.0",
        permissions=("read",),
    )

    # A provider declaration. This is discovery only — registering it does not
    # make anything call it, and the registry does not pretend otherwise.
    api.provider(
        kind="embedding",
        name="offline_hash_embedding",
        description=("A placeholder embedding provider declaration, to show how "
                     "a backend advertises itself to the registry."),
        version="0.1.0",
        permissions=("read",),
    )
