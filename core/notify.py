"""Push notifications to the phone through ntfy (https://ntfy.sh)."""
from __future__ import annotations

import urllib.request
from typing import Callable

from core.base import Alert


def _ascii(text: str) -> str:
    # HTTP header values must be ASCII-safe; the message body is sent as UTF-8.
    return text.encode("ascii", "ignore").decode().strip()


def build_request(alert: Alert, topic: str, server: str = "https://ntfy.sh"):
    headers = {
        "Title": _ascii(alert.title) or "Agent Hub",
        "Priority": str(alert.priority),
        "User-Agent": "agent-hub/1.0",
    }
    if alert.click and alert.click.startswith("https://"):
        headers["Click"] = alert.click
    buttons = []
    for label, link in alert.actions[:3]:
        if link and link.startswith("https://"):
            # commas and semicolons separate fields in the Actions header
            safe = link.replace(",", "%2C").replace(";", "%3B")
            buttons.append(f"view, {_ascii(label).replace(',', '')}, {safe}")
    if buttons:
        headers["Actions"] = "; ".join(buttons)
    url = f"{server.rstrip('/')}/{topic}"
    return url, alert.message.encode("utf-8"), headers


def send_ntfy(
    alert: Alert,
    topic: str,
    server: str = "https://ntfy.sh",
    opener: Callable = urllib.request.urlopen,
) -> None:
    url, body, headers = build_request(alert, topic, server)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with opener(req, timeout=20) as resp:
        resp.read()
