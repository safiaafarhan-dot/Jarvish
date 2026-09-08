"""WhatsApp and email composing, plus phone pairing.

Jarvish never sends anything by itself. Every tool here opens the message
already written and addressed, and you press send. That is deliberate:

- WhatsApp has no API for personal accounts. Driving web.whatsapp.com with a
  script breaks their terms of service and gets numbers banned. The wa.me deep
  link used here is WhatsApp's own supported way to hand off a drafted message.
- Email could be sent outright through Gmail's API, but that needs an OAuth
  consent flow, and silently sending mail on someone's behalf is not a good
  default. The compose window is instant and needs no setup.
"""

import re
import webbrowser
from urllib.parse import quote, urlencode

from .personal import recall
from .util import err, ok, string, tool

_DIGITS = re.compile(r"\D+")


def _phone_number(value):
    """Reduce a phone number to digits, keeping any country code."""
    digits = _DIGITS.sub("", str(value or ""))
    return digits or None


def whatsapp_message(text, to=None):
    """Open WhatsApp with a message typed out, ready for you to send."""
    body = str(text or "").strip()
    if not body:
        return err("No message text given.")

    number = _phone_number(to)
    # A saved contact can be referenced by name, e.g. "remember mum_phone".
    if to and not number:
        looked_up = recall(str(to).strip().lower().replace(" ", "_") + "_phone")
        if looked_up.get("ok"):
            number = _phone_number(looked_up["value"])

    if number:
        url = "https://wa.me/" + number + "?text=" + quote(body)
        target = "+" + number
    else:
        url = "https://web.whatsapp.com/send?text=" + quote(body)
        target = "whichever chat you pick"

    try:
        webbrowser.open(url)
    except Exception as exc:
        return err("Could not open WhatsApp: " + str(exc))

    return ok(
        drafted_to=target,
        message=body,
        sent=False,
        note="WhatsApp is open with the message typed in. Tell the user to press send - "
             "Jarvish cannot send it for them.",
    )


def compose_email(to=None, subject=None, body=None, provider="gmail"):
    """Open an email compose window with everything filled in."""
    recipient = str(to or "").strip()
    # Allow "email mum" when mum_email is stored in the profile.
    if recipient and "@" not in recipient:
        looked_up = recall(recipient.lower().replace(" ", "_") + "_email")
        if looked_up.get("ok"):
            recipient = str(looked_up["value"]).strip()

    subject_text = str(subject or "").strip()
    body_text = str(body or "").strip()
    choice = str(provider or "gmail").strip().lower()

    if choice in ("gmail", "google"):
        params = {"view": "cm", "fs": "1"}
        if recipient:
            params["to"] = recipient
        if subject_text:
            params["su"] = subject_text
        if body_text:
            params["body"] = body_text
        url = "https://mail.google.com/mail/?" + urlencode(params)
    elif choice in ("outlook", "hotmail"):
        params = {}
        if recipient:
            params["to"] = recipient
        if subject_text:
            params["subject"] = subject_text
        if body_text:
            params["body"] = body_text
        url = "https://outlook.live.com/mail/0/deeplink/compose?" + urlencode(params)
    else:
        params = {}
        if subject_text:
            params["subject"] = subject_text
        if body_text:
            params["body"] = body_text
        url = "mailto:" + quote(recipient)
        if params:
            url += "?" + urlencode(params)

    try:
        webbrowser.open(url)
    except Exception as exc:
        return err("Could not open the mail composer: " + str(exc))

    return ok(
        drafted_to=recipient or "(no recipient yet)",
        subject=subject_text or None,
        provider=choice,
        sent=False,
        note="The compose window is open with the draft filled in. Tell the user to "
             "review it and press send - Jarvish cannot send it for them.",
    )


def open_inbox(provider="gmail"):
    """Open your email inbox."""
    choice = str(provider or "gmail").strip().lower()
    urls = {
        "gmail": "https://mail.google.com/mail/u/0/#inbox",
        "google": "https://mail.google.com/mail/u/0/#inbox",
        "outlook": "https://outlook.live.com/mail/0/",
        "hotmail": "https://outlook.live.com/mail/0/",
        "yahoo": "https://mail.yahoo.com",
    }
    url = urls.get(choice, urls["gmail"])
    try:
        webbrowser.open(url)
    except Exception as exc:
        return err("Could not open the inbox: " + str(exc))
    return ok(opened=url, provider=choice)


# --------------------------------------------------------------------------
# Phone
# --------------------------------------------------------------------------

def open_phone_link():
    """Open Windows Phone Link, which mirrors your phone's texts and calls."""
    import subprocess

    try:
        subprocess.Popen(["cmd", "/c", "start", "", "ms-phone:"], shell=False)
    except Exception as exc:
        return err("Could not open Phone Link: " + str(exc))
    return ok(
        opened="Phone Link",
        note="Phone Link shows phone messages, calls, photos and notifications on the PC. "
             "If it is not set up yet, it will walk the user through pairing. Windows "
             "exposes no API for it, so Jarvish can open it but cannot read from it.",
    )


def phone_access():
    """Get the address for using Jarvish from your phone on the same WiFi."""
    import socket

    from .config import PORT

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        lan_ip = probe.getsockname()[0]
        probe.close()
    except Exception:
        lan_ip = None

    if not lan_ip:
        return err("Could not work out this PC's network address.")

    return ok(
        url="http://" + lan_ip + ":" + str(PORT),
        note="Open that address in the phone's browser while it is on the same WiFi. "
             "The server must be started with JARVISH_HOST=0.0.0.0 (or 'npm run phone') "
             "so it accepts connections from other devices. Voice input needs Chrome.",
    )


SCHEMAS = [
    tool("whatsapp_message",
         "Draft a WhatsApp message and open the chat with it typed in, ready to send. "
         "Jarvish cannot press send - always tell the user to do that. Use this when "
         "the user asks to reply to or message someone on WhatsApp.",
         {"text": string("The message to write."),
          "to": string("Phone number with country code, or a contact name saved in the "
                       "profile as <name>_phone. Optional - without it the user picks the chat.")},
         ["text"]),
    tool("compose_email",
         "Draft an email and open the compose window with it filled in, ready to send. "
         "Jarvish cannot press send - always tell the user to do that.",
         {"to": string("Recipient address, or a name saved in the profile as <name>_email."),
          "subject": string("The subject line."),
          "body": string("The message body."),
          "provider": string("gmail, outlook, or default for the desktop mail app.")}),
    tool("open_inbox", "Open the user's email inbox.",
         {"provider": string("gmail, outlook or yahoo.")}),
    tool("open_phone_link",
         "Open Windows Phone Link, which shows the user's phone messages, calls and "
         "photos on this PC."),
    tool("phone_access",
         "Get the web address for opening Jarvish from the user's phone on the same WiFi."),
]

REGISTRY = {
    "whatsapp_message": whatsapp_message,
    "compose_email": compose_email,
    "open_inbox": open_inbox,
    "open_phone_link": open_phone_link,
    "phone_access": phone_access,
}
