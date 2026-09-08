"""Live, day-to-day information: weather, news, currency, and page reading.

Every source here is key-free, so nothing needs an account or an API token.
"""

import html
import re
from urllib.parse import quote, quote_plus

import httpx

from .util import as_int, err, ok, string, tool

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>", re.S | re.I)


def _text(fragment):
    return html.unescape(_TAG_RE.sub(" ", fragment)).strip()


def _get(url, timeout=20.0):
    return httpx.get(url, headers=_UA, timeout=timeout, follow_redirects=True)


# --------------------------------------------------------------------------
# Weather
# --------------------------------------------------------------------------

def get_weather(city=None, days=1):
    """Current conditions and a short forecast, from wttr.in."""
    place = str(city).strip() if city else ""
    days = as_int(days, 1, 1, 3)
    try:
        response = _get("https://wttr.in/" + quote(place) + "?format=j1")
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return err("Weather lookup failed: " + str(exc))

    try:
        current = data["current_condition"][0]
        area = data.get("nearest_area", [{}])[0]
        location = ", ".join(
            part[0]["value"]
            for part in (area.get("areaName"), area.get("region"), area.get("country"))
            if part and part[0].get("value")
        )
        result = {
            "location": location or place or "your location",
            "description": current["weatherDesc"][0]["value"],
            "temp_c": current["temp_C"],
            "feels_like_c": current["FeelsLikeC"],
            "humidity_percent": current["humidity"],
            "wind_kmph": current["windspeedKmph"],
            "precip_mm": current.get("precipMM"),
            "observed": current.get("localObsDateTime"),
        }
        forecast = []
        for day in data.get("weather", [])[:days]:
            forecast.append({
                "date": day["date"],
                "min_c": day["mintempC"],
                "max_c": day["maxtempC"],
                "sunrise": day["astronomy"][0]["sunrise"],
                "sunset": day["astronomy"][0]["sunset"],
                "description": day["hourly"][4]["weatherDesc"][0]["value"] if day.get("hourly") else None,
            })
        result["forecast"] = forecast
    except (KeyError, IndexError, TypeError) as exc:
        return err("Weather data was in an unexpected shape: " + str(exc))

    return ok(**result)


# --------------------------------------------------------------------------
# News
# --------------------------------------------------------------------------

_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
_FIELD_RE = {
    "title": re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S),
    "date": re.compile(r"<pubDate>(.*?)</pubDate>", re.S),
    "source": re.compile(r"<source[^>]*>(.*?)</source>", re.S),
}


def get_news(topic=None, limit=6):
    """Latest headlines from Google News. Omit the topic for the top stories."""
    limit = as_int(limit, 6, 1, 15)
    topic = str(topic).strip() if topic else ""
    if topic:
        url = "https://news.google.com/rss/search?q=" + quote_plus(topic) + "&hl=en&gl=IN&ceid=IN:en"
    else:
        url = "https://news.google.com/rss?hl=en&gl=IN&ceid=IN:en"

    try:
        response = _get(url)
        response.raise_for_status()
    except Exception as exc:
        return err("News lookup failed: " + str(exc))

    stories = []
    for block in _ITEM_RE.finditer(response.text):
        chunk = block.group(1)
        story = {}
        for field, pattern in _FIELD_RE.items():
            match = pattern.search(chunk)
            story[field] = _text(match.group(1)) if match else None
        if story.get("title"):
            stories.append(story)
        if len(stories) >= limit:
            break

    if not stories:
        return err("No headlines came back.")
    return ok(topic=topic or "top stories", count=len(stories), stories=stories)


# --------------------------------------------------------------------------
# Reading a page
# --------------------------------------------------------------------------

def read_web_page(url, max_chars=3000):
    """Fetch a web page and return its readable text."""
    address = str(url).strip()
    if not address:
        return err("No URL given.")
    if not re.match(r"^https?://", address):
        address = "https://" + address
    max_chars = as_int(max_chars, 3000, 500, 12000)

    try:
        response = _get(address, timeout=25.0)
        response.raise_for_status()
    except Exception as exc:
        return err("Could not fetch that page: " + str(exc))

    body = _SCRIPT_RE.sub(" ", response.text)
    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    text = re.sub(r"\s+", " ", _text(body)).strip()

    return ok(
        url=address,
        title=_text(title_match.group(1)) if title_match else None,
        content=text[:max_chars],
        truncated=len(text) > max_chars,
    )


# --------------------------------------------------------------------------
# Currency
# --------------------------------------------------------------------------

def convert_currency(amount, source="USD", target="INR"):
    """Convert between currencies at today's rate."""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return err("Amount must be a number.")
    source = str(source).strip().upper()
    target = str(target).strip().upper()

    try:
        response = _get("https://api.frankfurter.app/latest?amount=" + str(value)
                        + "&from=" + source + "&to=" + target)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return err("Currency lookup failed: " + str(exc))

    rate = (data.get("rates") or {}).get(target)
    if rate is None:
        return err("No rate available for " + source + " to " + target + ".")
    return ok(amount=value, source=source, target=target,
              converted=round(rate, 2), date=data.get("date"))


# --------------------------------------------------------------------------
# Daily briefing
# --------------------------------------------------------------------------

def daily_briefing(city=None):
    """Time, weather and headlines in one call - a morning catch-up."""
    from .tools import get_time  # imported here to avoid a circular import

    briefing = {"when": get_time()}
    weather = get_weather(city)
    briefing["weather"] = weather if weather.get("ok") else {"error": weather.get("error")}
    news = get_news(limit=5)
    briefing["news"] = news.get("stories") if news.get("ok") else {"error": news.get("error")}
    return ok(**briefing)


SCHEMAS = [
    tool("get_weather",
         "Current weather and short forecast for a city. Omit the city to use the "
         "user's stored home city or their approximate location.",
         {"city": string("City name. Optional."),
          "days": string("How many forecast days, 1 to 3.")}),
    tool("get_news",
         "Latest news headlines. Give a topic for focused news, or omit it for the "
         "day's top stories.",
         {"topic": string("Optional subject, for example 'technology' or 'cricket'."),
          "limit": string("How many headlines, up to 15.")}),
    tool("read_web_page",
         "Fetch a web page and read its text. Use this after web_search when you need "
         "the actual detail from a result rather than just the snippet.",
         {"url": string("The page address."),
          "max_chars": string("How much text to return.")},
         ["url"]),
    tool("convert_currency",
         "Convert an amount between two currencies at today's exchange rate.",
         {"amount": string("The amount to convert."),
          "source": string("Currency code to convert from, for example USD."),
          "target": string("Currency code to convert to, for example INR.")},
         ["amount"]),
    tool("daily_briefing",
         "A combined catch-up: the time, today's weather, and the top headlines. Use "
         "this for 'good morning', 'what's happening today' or 'brief me'.",
         {"city": string("City for the weather. Optional.")}),
]

REGISTRY = {
    "get_weather": get_weather,
    "get_news": get_news,
    "read_web_page": read_web_page,
    "convert_currency": convert_currency,
    "daily_briefing": daily_briefing,
}
