"""Opening web applications, with deep links where a site supports them."""

import webbrowser
from urllib.parse import quote_plus

from .util import err, ok, string, tool

# name -> (home url, search url template or None, aliases)
# The template takes one {q} placeholder which is url-encoded before use.
APPS = {
    # Google
    "gmail": ("https://mail.google.com", "https://mail.google.com/mail/u/0/#search/{q}", ["mail", "google mail", "email", "inbox"]),
    "google": ("https://www.google.com", "https://www.google.com/search?q={q}", ["search"]),
    "google drive": ("https://drive.google.com", "https://drive.google.com/drive/search?q={q}", ["drive"]),
    "google docs": ("https://docs.google.com", None, ["docs"]),
    "google sheets": ("https://sheets.google.com", None, ["sheets", "spreadsheet"]),
    "google slides": ("https://slides.google.com", None, ["slides"]),
    "google calendar": ("https://calendar.google.com", None, ["calendar"]),
    "google maps": ("https://www.google.com/maps", "https://www.google.com/maps/search/{q}", ["maps", "map"]),
    "google photos": ("https://photos.google.com", "https://photos.google.com/search/{q}", ["photos"]),
    "google translate": ("https://translate.google.com", "https://translate.google.com/?text={q}", ["translate"]),
    "google news": ("https://news.google.com", "https://news.google.com/search?q={q}", ["news"]),
    "youtube": ("https://www.youtube.com", "https://www.youtube.com/results?search_query={q}", ["yt"]),
    "youtube music": ("https://music.youtube.com", "https://music.youtube.com/search?q={q}", ["yt music"]),
    "google meet": ("https://meet.google.com", None, ["meet"]),
    "google classroom": ("https://classroom.google.com", None, ["classroom"]),
    "google keep": ("https://keep.google.com", None, ["keep"]),

    # Messaging and social
    "whatsapp": ("https://web.whatsapp.com", None, ["whats app", "wa"]),
    "telegram": ("https://web.telegram.org", None, []),
    "instagram": ("https://www.instagram.com", "https://www.instagram.com/explore/tags/{q}/", ["insta", "ig"]),
    "facebook": ("https://www.facebook.com", "https://www.facebook.com/search/top?q={q}", ["fb"]),
    "x": ("https://x.com", "https://x.com/search?q={q}", ["twitter"]),
    "linkedin": ("https://www.linkedin.com/feed/", "https://www.linkedin.com/search/results/all/?keywords={q}", []),
    "reddit": ("https://www.reddit.com", "https://www.reddit.com/search/?q={q}", []),
    "discord": ("https://discord.com/app", None, []),
    "slack": ("https://app.slack.com", None, []),
    "microsoft teams": ("https://teams.microsoft.com", None, ["teams"]),
    "zoom": ("https://zoom.us", None, []),
    "snapchat": ("https://web.snapchat.com", None, ["snap"]),
    "pinterest": ("https://www.pinterest.com", "https://www.pinterest.com/search/pins/?q={q}", []),
    "quora": ("https://www.quora.com", "https://www.quora.com/search?q={q}", []),

    # Microsoft
    "outlook": ("https://outlook.live.com/mail/", "https://outlook.live.com/mail/0/?q={q}", ["hotmail"]),
    "onedrive": ("https://onedrive.live.com", None, []),
    "office": ("https://www.office.com", None, ["microsoft 365", "office 365"]),

    # Entertainment
    "netflix": ("https://www.netflix.com", "https://www.netflix.com/search?q={q}", []),
    "prime video": ("https://www.primevideo.com", "https://www.primevideo.com/search/ref=atv_nb_sr?phrase={q}", ["amazon prime", "primevideo"]),
    "hotstar": ("https://www.hotstar.com", "https://www.hotstar.com/in/search?q={q}", ["disney hotstar", "jiohotstar"]),
    "spotify": ("https://open.spotify.com", "https://open.spotify.com/search/{q}", []),
    "apple music": ("https://music.apple.com", "https://music.apple.com/search?term={q}", []),
    "soundcloud": ("https://soundcloud.com", "https://soundcloud.com/search?q={q}", []),
    "gaana": ("https://gaana.com", "https://gaana.com/search/{q}", []),
    "jiosaavn": ("https://www.jiosaavn.com", "https://www.jiosaavn.com/search/{q}", ["saavn"]),
    "twitch": ("https://www.twitch.tv", "https://www.twitch.tv/search?term={q}", []),
    "imdb": ("https://www.imdb.com", "https://www.imdb.com/find/?q={q}", []),

    # Shopping and services
    "amazon": ("https://www.amazon.in", "https://www.amazon.in/s?k={q}", []),
    "flipkart": ("https://www.flipkart.com", "https://www.flipkart.com/search?q={q}", []),
    "myntra": ("https://www.myntra.com", "https://www.myntra.com/{q}", []),
    "ebay": ("https://www.ebay.com", "https://www.ebay.com/sch/i.html?_nkw={q}", []),
    "swiggy": ("https://www.swiggy.com", "https://www.swiggy.com/search?query={q}", []),
    "zomato": ("https://www.zomato.com", "https://www.zomato.com/search?q={q}", []),
    "zepto": ("https://www.zeptonow.com", None, []),
    "blinkit": ("https://blinkit.com", None, []),
    "uber": ("https://www.uber.com", None, []),
    "ola": ("https://www.olacabs.com", None, []),
    "irctc": ("https://www.irctc.co.in/nget/train-search", None, ["train", "railway"]),
    "makemytrip": ("https://www.makemytrip.com", None, ["mmt"]),
    "booking": ("https://www.booking.com", None, []),
    "airbnb": ("https://www.airbnb.com", None, []),
    "paytm": ("https://paytm.com", None, []),
    "phonepe": ("https://www.phonepe.com", None, []),

    # Work and dev
    "github": ("https://github.com", "https://github.com/search?q={q}", []),
    "gitlab": ("https://gitlab.com", None, []),
    "stack overflow": ("https://stackoverflow.com", "https://stackoverflow.com/search?q={q}", ["stackoverflow"]),
    "notion": ("https://www.notion.so", None, []),
    "trello": ("https://trello.com", None, []),
    "jira": ("https://www.atlassian.com/software/jira", None, []),
    "figma": ("https://www.figma.com/files", None, []),
    "canva": ("https://www.canva.com", "https://www.canva.com/search?q={q}", []),
    "dropbox": ("https://www.dropbox.com", None, []),
    "vercel": ("https://vercel.com/dashboard", None, []),
    "codepen": ("https://codepen.io", None, []),
    "replit": ("https://replit.com", None, []),

    # AI
    "chatgpt": ("https://chat.openai.com", None, ["chat gpt", "openai"]),
    "claude": ("https://claude.ai", None, []),
    "gemini": ("https://gemini.google.com", None, ["bard"]),
    "perplexity": ("https://www.perplexity.ai", "https://www.perplexity.ai/search?q={q}", []),
    "huggingface": ("https://huggingface.co", "https://huggingface.co/search/full-text?q={q}", ["hugging face"]),

    # Learning and reference
    "wikipedia": ("https://www.wikipedia.org", "https://en.wikipedia.org/w/index.php?search={q}", ["wiki"]),
    "coursera": ("https://www.coursera.org", "https://www.coursera.org/search?query={q}", []),
    "udemy": ("https://www.udemy.com", "https://www.udemy.com/courses/search/?q={q}", []),
    "khan academy": ("https://www.khanacademy.org", None, ["khan"]),
    "leetcode": ("https://leetcode.com/problemset/", "https://leetcode.com/problemset/?search={q}", []),
    "hackerrank": ("https://www.hackerrank.com", None, []),
    "w3schools": ("https://www.w3schools.com", None, ["w3"]),
    "medium": ("https://medium.com", "https://medium.com/search?q={q}", []),
    "geeksforgeeks": ("https://www.geeksforgeeks.org", None, ["gfg"]),
}

# Flatten aliases into a lookup table once at import time.
_LOOKUP = {}
for _name, (_home, _search, _aliases) in APPS.items():
    _LOOKUP[_name] = _name
    _LOOKUP[_name.replace(" ", "")] = _name
    for _alias in _aliases:
        _LOOKUP[_alias] = _name
        _LOOKUP[_alias.replace(" ", "")] = _name


def _resolve(name):
    key = str(name).strip().lower()
    if key in _LOOKUP:
        return _LOOKUP[key]
    squashed = key.replace(" ", "")
    if squashed in _LOOKUP:
        return _LOOKUP[squashed]
    # Last resort: unique substring match, so "stack" finds "stack overflow".
    hits = {full for alias, full in _LOOKUP.items() if key and key in alias}
    if len(hits) == 1:
        return hits.pop()
    return None


def open_web_app(name, query=None):
    """Open a website, optionally jumping straight to a search inside it."""
    resolved = _resolve(name)
    if resolved is None:
        return err(
            "'" + str(name) + "' is not in the web app list. Use open_url with the "
            "full address instead, or call list_web_apps to see what is available."
        )

    home, search_template, _ = APPS[resolved]
    text = str(query).strip() if query else ""

    if text and search_template:
        url = search_template.replace("{q}", quote_plus(text))
        action = "searched"
    else:
        url = home
        action = "opened"
        if text and not search_template:
            action = "opened (this site has no deep search, so the query was ignored)"

    try:
        webbrowser.open(url)
    except Exception as exc:
        return err("Could not open " + resolved + ": " + str(exc))
    return ok(app=resolved, action=action, url=url, query=text or None)


def list_web_apps(category=None):
    """List the web apps Jarvish can open by name."""
    names = sorted(APPS)
    if category:
        needle = str(category).strip().lower()
        names = [n for n in names if needle in n] or names
    return ok(count=len(names), apps=names)


SCHEMAS = [
    tool("open_web_app",
         "Open a website or web app by name - Gmail, YouTube, WhatsApp, Netflix, "
         "Amazon, GitHub, Maps and around 80 others. Pass a query to jump straight to "
         "a search inside that site, for example open_web_app('youtube', 'lofi beats').",
         {"name": string("The web app to open, for example youtube, gmail, netflix."),
          "query": string("Optional. What to search for inside that site.")},
         ["name"]),
    tool("list_web_apps",
         "List the web apps that can be opened by name.",
         {"category": string("Optional word to filter the list by.")}),
]

REGISTRY = {
    "open_web_app": open_web_app,
    "list_web_apps": list_web_apps,
}
