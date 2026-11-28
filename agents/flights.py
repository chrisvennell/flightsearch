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
    query_key,
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
        seen: set[tuple] = set()  # searches that ask the same query in one run share its call
        for search in settings.get("searches", []):
            if search.get("enabled", True) is False:
                continue
            every = float(search.get("check_every_days", settings.get("check_every_days", DEFAULT_EVERY_DAYS)))
            calls = 0
            for queries in search_plan(search, sets).values():
                for query in live(queries, today):
                    if query_key(query) not in seen:
                        seen.add(query_key(query))
                        calls += 1
            rows.append(
                {"name": search["name"], "calls": calls, "every_days": every, "per_month": 0.0 if search.get("one_off") else round(calls * 30 / every, 1)}
            )
        return rows

    # ----------------------------------------------------------------- run

    def run(self) -> AgentResult:
        api_key = self.ctx.env.get("SERPAPI_API_KEY")
        if not api_key:
            raise AgentError("SERPAPI_API_KEY is not set")

        sets = self.settings.get("airport_sets", {})
        middle_east = set(airport_list(sets.get("middle_east", DEFAULT_MIDDLE_EAST), sets))
        drop_pct = float(self.settings.get("drop_alert_percent", 10))
        currency = self.settings.get("currency", "USD")
        today = self.ctx.now.date()
        state = {name: dict(val) for name, val in self.state.items()}
        items: list[dict[str, Any]] = []
        alerts: list[Alert] = []
        calls = 0
        swept = 0
        cache: dict[tuple, list[dict[str, Any]] | None] = {}  # answers fetched earlier in this run
        searches = [s for s in self.settings.get("searches", []) if s.get("enabled", True) is not False]

        for search in searches:
            name = search["name"]
            st = state.setdefault(name, {})
            rules = rules_of(search)
            every = float(search.get("check_every_days", self.settings.get("check_every_days", DEFAULT_EVERY_DAYS)))

            try:
                plan = search_plan(search, sets)
                overnight_ok = (
                    set(airport_list(search["overnight_ok"], sets)) if "overnight_ok" in search else None
                )
            except (ValueError, KeyError, TypeError) as exc:
                items.append(self._card(search, st, currency, "error", "bad", note=f"config problem: {exc}"))
                continue

            if not self._due(st, every):
                items.append(self._card(search, st, currency))
                continue

            todo = {key: live(queries, today) for key, queries in plan.items()}
            if sum(len(q) for q in todo.values()) == 0 or any(not q for q in todo.values()):
                items.append(self._card(search, st, currency, "dates passed", "warn"))
                continue
            needed = len({query_key(q) for queries in todo.values() for q in queries} - cache.keys())
            if self.budget_remaining is not None and calls + needed > self.budget_remaining:
                items.append(self._card(search, st, currency, "budget reached", "warn"))
                continue

            results: dict[str, list[dict[str, Any]]] = {key: [] for key in todo}
            try:
                for key, queries in todo.items():
                    for query in queries:
                        qk = query_key(query)
                        if qk not in cache:
                            cache[qk] = self._fetch(search, query, currency, api_key)
                            if cache[qk] is not None:
                                calls += 1
                        if cache[qk] is not None:
                            results[key].extend(dict(it) for it in cache[qk])
            except AgentError as exc:
                items.append(self._card(search, st, currency, "error", "bad", note=str(exc)))
                continue

            if "stitch" in search:
                spec = search["stitch"]
                candidates = stitch(
                    results["first"],
                    results["second"],
                    float(spec.get("min_gap_hours", 3)),
                    float(spec.get("max_gap_hours", 24)),
                    bool(spec.get("count_gap_nights", True)),
                    min_gap_by_airport=spec.get("min_gap_hours_by_airport"),
                    max_gap_nights=spec.get("max_gap_nights"),
                    same_airport=bool(spec.get("same_airport", False)),
                )
            else:
                candidates = results["main"]

            swept += 1
            st["last_sweep"] = today.isoformat()
            st["checked"] = self.ctx.now.isoformat(timespec="seconds")

            valid = [it for it in candidates if is_valid(it, search, overnight_ok)]
            for it in valid:
                it["cap"] = price_cap(it, rules, middle_east)
            qualifying = [it for it in valid if it["cap"] is None or it["price"] <= it["cap"]]
            prefer = rules.get("prefer", "cheapest")

            if not valid:
                st["card"] = None
                items.append(self._card(search, st, currency, "no matching flights", "warn"))
                st["last_alert_price"] = None
                continue

            pool = qualifying or valid
            shown = choose(pool, prefer) if qualifying else min(valid, key=lambda it: (it["price"], it["duration"]))
            cheapest_q = min((it["price"] for it in qualifying), default=None)
            cheapest = min(it["price"] for it in valid)
            has_cap = rules.get("base_cap") is not None

            reasons = decide_reasons(st, cheapest_q, cheapest, drop_pct, has_cap)

            tracked = shown["price"]
            history = list(st.get("history", []))
            history.append([today.isoformat(), tracked])
            st["history"] = history[-HISTORY_LIMIT:]
            st["best"] = min(tracked, st.get("best", tracked))
            previous = st.get("last")
            st["last"] = tracked
            if has_cap and cheapest_q is None:
                st["last_alert_price"] = None
            if reasons:
                st["last_alert_price"] = cheapest_q if has_cap else cheapest

            if qualifying:
                badge, tone = ("within limits", "good") if has_cap else ("", "")
            else:
                over = shown["price"] - shown["cap"] if shown.get("cap") is not None else 0
                badge, tone = (f"{currency} {over:,} over", "") if shown.get("cap") is not None else ("", "")
            link = shown.get("link") or fallback_link(search)
            detail = describe(shown)
            if shown.get("cap") is not None:
                detail += f". Limit for this routing {currency} {int(shown['cap']):,}"
            card = self._item(search, st, currency, link, badge, tone, detail)
            if shown.get("link2"):
                card["link2"] = shown["link2"]
            st["card"] = {k: v for k, v in card.items() if k != "history"}
            items.append(card)

            if reasons:
                two_tickets = bool(shown.get("link2"))
                lines = [f"{name}: {currency} {shown['price']:,}", detail, reasons[0].capitalize()]
                if two_tickets:
                    lines.append("Tap the notification for ticket 1, or the button for ticket 2.")
                if previous is not None and previous != shown["price"]:
                    lines.append(f"Previous check: {currency} {previous:,}")
                extra = [("Ticket 2", shown["link2"])] if two_tickets else []
                alerts.append(
                    Alert(title=f"Flight deal: {name}"[:60], message="\n".join(lines), priority=4, click=link, actions=extra)
                    if has_cap
                    else Alert(title=f"Price drop: {name}"[:60], message="\n".join(lines), priority=3, click=link, actions=extra)
                )

        summary = f"{swept} of {len(searches)} searches checked this run, {calls} calls"
        if alerts:
            summary += f", {len(alerts)} alert" + ("" if len(alerts) == 1 else "s")
        return AgentResult(summary=summary, items=items, alerts=alerts, state=state, calls_used=calls)

    # ------------------------------------------------------------- helpers

    def _due(self, st: dict[str, Any], every_days: float) -> bool:
        """True when the last completed sweep is at least `every_days` old.

        Runs happen twice a day, so the interval is met to within a couple of
        hours of tolerance, which also absorbs late scheduler starts.
        """
        try:
            last = datetime.fromisoformat(st["checked"])
        except (KeyError, TypeError, ValueError):
            return True
        return self.ctx.now - last >= timedelta(days=every_days) - timedelta(hours=2)

    def _fetch(
        self, search: dict[str, Any], query: dict[str, Any], currency: str, api_key: str
    ) -> list[dict[str, Any]] | None:
        """Normalised itineraries for one query, [] for none, None when the API reports no results."""
        round_trip = bool(query.get("return"))
        params: dict[str, Any] = {
            "engine": "google_flights",
            "departure_id": query["from"],
            "arrival_id": query["to"],
            "outbound_date": query["depart"],
            "currency": currency,
            "hl": "en",
            "type": "1" if round_trip else "2",
            "sort_by": str(self.settings.get("sort_by", 2)),
            "api_key": api_key,
        }
        if self.settings.get("gl"):
            params["gl"] = self.settings["gl"]
        if round_trip:
            params["return_date"] = query["return"]
        if query.get("outbound_times"):
            params["outbound_times"] = query["outbound_times"]
        if round_trip and query.get("return_times"):
            params["return_times"] = query["return_times"]
        if int(self.settings.get("adults", 1)) > 1:
            params["adults"] = int(self.settings["adults"])
        try:
            data = self.ctx.http_get(SERPAPI_URL + "?" + urlencode(params), 30.0)
        except HttpError as exc:
            raise AgentError(str(exc)) from None
        if not isinstance(data, dict):
            raise AgentError("unexpected response")
        if data.get("error"):
            return None
        link = (data.get("search_metadata") or {}).get("google_flights_url")
        out = []
        for group in ("best_flights", "other_flights"):
            for raw in data.get(group) or []:
                it = normalize(raw)
                if it is None:
                    continue
                if isinstance(link, str) and link.startswith("https://"):
                    it["link"] = link
                out.append(it)
        return out

    def _card(
        self,
        search: dict[str, Any],
        st: dict[str, Any],
        currency: str,
        badge: str = "",
        tone: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """Card built from saved state, optionally overriding the badge or showing a note."""
        card = dict(st.get("card") or {})
        card.setdefault("title", search["name"])
        card.setdefault("subtitle", "No result yet")
        card.setdefault("value", f"{currency} {st['last']:,}" if st.get("last") is not None else "")
        card.setdefault("link", fallback_link(search))
        if note:
            card["subtitle"] = note
        if badge:
            card["badge"], card["tone"] = badge, tone
        card["history"] = [p for _, p in st.get("history", [])]
        return card

    @staticmethod
    def _item(
        search: dict[str, Any],
        st: dict[str, Any],
        currency: str,
        link: str,
        badge: str,
        tone: str,
        detail: str,
    ) -> dict[str, Any]:
        return {
            "title": search["name"],
            "subtitle": detail,
            "value": f"{currency} {st['last']:,}",
            "badge": badge,
            "tone": tone,
            "link": link,
            "history": [p for _, p in st.get("history", [])],
        }
