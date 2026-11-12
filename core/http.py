"""Minimal JSON-over-HTTP helper using only the standard library.

Error messages never include the request URL, because API keys travel in it
and the dashboard data is published.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


class HttpError(Exception):
    pass


def _error_text(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"])[:200]
    return ""


def get_json(url: str, timeout: float = 30.0, retries: int = 1) -> dict[str, Any]:
    last: HttpError | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url, headers={"User-Agent": "agent-hub/1.0", "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            message = f"HTTP {exc.code}: {_error_text(body) or exc.reason}"
            if exc.code < 500:
                raise HttpError(message) from None
            last = HttpError(message)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last = HttpError(f"request failed ({type(exc).__name__})")
        if attempt < retries:
            time.sleep(2)
    assert last is not None
    raise last
