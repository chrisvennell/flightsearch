"""Flight price agent.

Each search in config.json becomes one or more SerpApi Google Flights queries
(a "sweep"). A sweep runs only every `check_every_days` days so the monthly
call budget lasts. The results are filtered by the search's rules, priced
against its caps, and an alert is raised when something qualifies.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from agents.flight_rules import (
    DEFAULT_MIDDLE_EAST,
    airport_list,
    is_valid,
    live,
    minutes_text,
    normalize,
    price_cap,
    rules_of,
    search_plan,
    decide_reasons,
    stitch,
    stops_text,
    sweep_calls,
)
from core.base import Agent, AgentError, AgentResult, Alert
from core.http import HttpError

SERPAPI_URL = "https://serpapi.com/search.json"
HISTORY_LIMIT = 120
DEFAULT_EVERY_DAYS = 7


def fallback_link(search: dict[str, Any]) -> str:
    return "https://www.google.com/travel/flights?q=" + quote(f"Flights for {search['name']}")


def path_text(path: list[str]) -> str:
    out, run = [], []
    for code in path:
        if code == "|":
            out.append("-".join(run))
            run = []
        else:
            run.append(code)
    out.append("-".join(run))
    return " / ".join(out)


def when_text(stamp: Any) -> str:
    try:
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M").strftime("%d %b %H:%M").lstrip("0")
    except (TypeError, ValueError):
        return "?"


def describe(it: dict[str, Any]) -> str:
    parts = [
        path_text(it["path"]),
        f"leaves {when_text(it['departs'])}, arrives {when_text(it['arrives'])}",
        minutes_text(it["duration"]) + " total" if it["duration"] else "",
        " + ".join(it["airlines"]),
    ]
    if it["overnights"]:
        parts.append("overnight at " + ", ".join(it["overnights"]))
    if it.get("nights_extra"):
        parts.append(f"{it['nights_extra']} night(s) between tickets")
    if it.get("transfer"):
        parts.append("two tickets: " + it["transfer"])
    return ". ".join(p for p in parts if p)


def choose(items: list[dict[str, Any]], prefer: str) -> dict[str, Any]:
    if prefer == "shortest":
        return min(items, key=lambda it: (it["duration"], it["price"]))
    return min(items, key=lambda it: (it["price"], it["duration"]))


class FlightsAgent(Agent):
    key = "flights"
    title = "Flights"

    # ------------------------------------------------------------ planning

    @staticmethod
    def plan(settings: dict[str, Any], today: date) -> list[dict[str, Any]]:
        sets = settings.get("airport_sets", {})
        rows = []
        for search in settings.get("searches", []):
            if search.get("enabled", True) is False:
                continue
            every = float(search.get("check_every_days", settings.get("check_every_days", DEFAULT_EVERY_DAYS)))
            calls = sweep_calls(search, sets, today)
            rows.append(
                {"name": search["name"], "calls": calls, "every_days": every, "per_month": 0.0 if search.get("one_off") else round(calls * 30 / every, 1)}
            )
        return rows

    @staticmethod
    def
