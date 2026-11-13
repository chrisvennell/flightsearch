"""Pure logic for the flights agent. No network access happens here.

Covers: turning SerpApi itineraries into a simple shape, expanding search
definitions into API queries, price limits that depend on overnights and on
Middle East routing, and joining two separate tickets into one trip.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from itertools import product
from typing import Any

TIME_FORMAT = "%Y-%m-%d %H:%M"
BLOCK_KEYS = ("from", "to", "depart", "return", "outbound_times", "return_times")

# Assumption, editable in config.json under airport_sets.middle_east.
DEFAULT_MIDDLE_EAST = [
    "DXB", "DWC", "SHJ", "AUH", "DOH", "BAH", "KWI", "MCT",
    "RUH", "JED", "DMM", "MED", "AMM", "TLV", "BEY",
]


# ---------------------------------------------------------------- parsing

def parse_time(text: Any) -> datetime | None:
    try:
        return datetime.strptime(text, TIME_FORMAT)
    except (TypeError, ValueError):
        return None


def expand_dates(spec: Any) -> list[str]:
    """A date, a list of dates, or {"from": d1, "to": d2} (inclusive) as ISO dates."""
    if spec is None:
        return []
    if isinstance(spec, str):
        date.fromisoformat(spec)
        return [spec]
    if isinstance(spec, dict):
        start, end = date.fromisoformat(spec["from"]), date.fromisoformat(spec["to"])
        if end < start:
            raise ValueError(f"date range ends before it starts: {spec}")
        return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    out: list[str] = []
    for item in spec:
        out.extend(expand_dates(item))
    return out


def airport_list(value: Any, sets: dict[str, list[str]]) -> list[str]:
    """A code, a list of codes, or "@name" references to airport_sets, as unique codes."""
    values = [value] if isinstance(value, str) else list(value)
    out: list[str] = []
    for item in values:
        if item.startswith("@"):
            if item[1:] not in sets:
                raise ValueError(f"unknown airport set {item}")
            out.extend(airport_list(sets[item[1:]], sets))
        else:
            out.append(item.upper())
    return list(dict.fromkeys(out))


def minutes_text(minutes: Any) -> str:
    if not isinstance(minutes, (int, float)) or minutes <= 0:
        return ""
    return f"{int(minutes) // 60}h {int(minutes) % 60:02d}m"


def stops_text(stops: int) -> str:
    return "nonstop" if stops == 0 else f"{stops} stop" + ("" if stops == 1 else "s")


# ----------------------------------------------------------- itineraries

def normalize(raw: dict[str, Any]) -> dict[str, Any] | None:
    """One SerpApi itinerary as a flat dict, or None when it has no usable price or legs."""
    price = raw.get("price")
    if not isinstance(price, (int, float)) or isinstance(price, bool):
        return None
    legs = raw.get("flights") or []
    if not legs:
        return None
    first_dep = legs[0].get("departure_airport") or {}
    last_arr = legs[-1].get("arrival_airport") or {}
    layovers = raw.get("layovers") or []

    path = [first_dep.get("id") or "?"]
    airports = {path[0]}
    airlines: list[str] = []
    for leg in legs:
        arrival_id = (leg.get("arrival_airport") or {}).get("id") or "?"
        path.append(arrival_id)
        airports.add(arrival_id)
        name = leg.get("airline")
        if name and name not in airlines:
            airlines.append(name)

    duration = raw.get("total_duration")
    if not isinstance(duration, (int, float)):
        duration = sum(leg.get("duration") or 0 for leg in legs) + sum(
            lo.get("duration") or 0 for lo in layovers
        )

    return {
        "price": int(price),
        "duration": int(duration),
        "departs": first_dep.get("time"),
        "arrives": last_arr.get("time"),
        "from": path[0],
        "to": path[-1],
        "path": path,
        "stops": max(len(legs) - 1, 0),
        "airlines": airlines,
        "overnights": [lo.get("id") or "?" for lo in layovers if lo.get("overnight")],
        "nights_extra": 0,
        "airports": sorted(airports),
        "stitched": False,
    }


def stitch(
    first: list[dict[str, Any]],
    second: list[dict[str, Any]],
    min_gap_hours: float,
    max_gap_hours: float,
    count_gap_nights: bool = True,
) -> list[dict[str, Any]]:
    """Pair two separate tickets where the second leaves a suitable time after the first lands.

    Both airports must share a time zone for the gap to be right (OOL and BNE, SYD and WSI).
    """
    joined: list[dict[str, Any]] = []
    for a in first:
        landed = parse_time(a["arrives"])
        if landed is None:
            continue
        for b in second:
            leaves = parse_time(b["departs"])
            if leaves is None:
                continue
            gap = leaves - landed
            hours = gap.total_seconds() / 3600
            if hours < min_gap_hours or hours > max_gap_hours:
                continue
            nights = (leaves.date() - landed.date()).days if count_gap_nights else 0
            joined.append(
                {
                    "price": a["price"] + b["price"],
                    "duration": a["duration"] + int(gap.total_seconds() // 60) + b["duration"],
                    "departs": a["departs"],
                    "arrives": b["arrives"],
                    "from": a["from"],
                    "to": b["to"],
                    "path": a["path"] + ["|"] + b["path"],
                    "stops": a["stops"] + b["stops"] + 1,
                    "airlines": list(dict.fromkeys(a["airlines"] + b["airlines"])),
                    "overnights": a["overnights"] + b["overnights"],
                    "nights_extra": max(nights, 0),
                    "airports": sorted(set(a["airports"]) | set(b["airports"])),
                    "stitched": True,
                    "link": a.get("link"),
                    "link2": b.get("link"),
                    "transfer": f"{a['to']} to {b['from']}, {hours:.0f}h between tickets",
                }
            )
    return joined


# ----------------------------------------------------------------- rules

def rules_of(search: dict[str, Any]) -> dict[str, Any]:
    rules = dict(search.get("rules") or {})
    if "base_cap" not in rules and search.get("max_price") is not None:
        rules["base_cap"] = search["max_price"]  # older, simpler config
    return rules


def price_cap(it: dict[str, Any], rules: dict[str, Any], middle_east: set[str]) -> float | None:
    """Highest acceptable price for this itinerary, or None when the search has no cap."""
    base = rules.get("base_cap")
    if base is None:
        return None
    by_destination = rules.get("cap_by_destination") or {}
    if it["to"] in by_destination:
        base = by_destination[it["to"]]
    alternative = rules.get("no_middle_east_cap")
    if alternative is not None and not (set(it["airports"]) & middle_east):
        base = max(base, alternative)
    nights = len(it["overnights"]) + it.get("nights_extra", 0)
    return base - rules.get("per_overnight_drop", 0) * nights


def api_max_price(rules: dict[str, Any]) -> int | None:
    caps = [rules[k] for k in ("base_cap", "no_middle_east_cap") if rules.get(k) is not None]
    return int(max(caps)) if caps else None


def is_valid(
    it: dict[str, Any],
    search: dict[str, Any],
    overnight_ok: set[str] | None,
) -> bool:
    max_stops = search.get("max_stops")
    if max_stops is not None and it["stops"] > max_stops:
        return False
    window = search.get("arrive_between")
    if window:
        day = (it.get("arrives") or "")[:10]
        if not day or not (window[0] <= day <= window[1]):
            return False
    if overnight_ok is not None and any(code not in overnight_ok for code in it["overnights"]):
        return False
    return True


def decide_reasons(
    st: dict[str, Any],
    qualifying_price: int | None,
    cheapest_price: int | None,
    drop_pct: float,
    has_cap: bool,
) -> list[str]:
    """Why a notification is due. Empty list means stay quiet.

    With a cap: alert once when something qualifies (and again after a further
    drop of drop_pct). Without a cap: alert on a drop of drop_pct between checks.
    """
    factor = 1 - drop_pct / 100
    if has_cap:
        if qualifying_price is None:
            return []
        last_alert = st.get("last_alert_price")
        if last_alert is None:
            return ["within your limits"]
        if qualifying_price <= last_alert * factor:
            return [f"down {drop_pct:g}% or more since the last alert"]
        return []
    previous = st.get("last")
    if cheapest_price is not None and previous is not None and cheapest_price <= previous * factor:
        return [f"down {drop_pct:g}% or more since the last check"]
    return []


# ----------------------------------------------------------------- plans

def expand_queries(block: dict[str, Any], defaults: dict[str, Any], sets: dict[str, list[str]]) -> list[dict[str, Any]]:
    merged = {**defaults, **block}
    departs = expand_dates(merged.get("depart"))
    if not departs:
        raise ValueError("a search needs a depart date")
    returns: list[str | None] = list(expand_dates(merged.get("return"))) or [None]
    origin = ",".join(airport_list(merged["from"], sets))
    destination = ",".join(airport_list(merged["to"], sets))
    queries = []
    for d, r in product(departs, returns):
        if r is not None and r < d:
            continue
        queries.append(
            {
                "from": origin,
                "to": destination,
                "depart": d,
                "return": r,
                "outbound_times": merged.get("outbound_times"),
                "return_times": merged.get("return_times"),
            }
        )
    return queries


def search_plan(search: dict[str, Any], sets: dict[str, list[str]]) -> dict[str, list[dict[str, Any]]]:
    """Queries a search needs. Keys: "main", or "first" and "second" for two-ticket searches."""
    if "stitch" in search:
        spec = search["stitch"]
        return {
            "first": expand_queries(spec["first"], {}, sets),
            "second": expand_queries(spec["second"], {}, sets),
        }
    base = {k: search[k] for k in BLOCK_KEYS if k in search}
    queries: list[dict[str, Any]] = []
    for block in search.get("queries") or [{}]:
        queries.extend(expand_queries(block, base, sets))
    return {"main": queries}


def live(queries: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    return [q for q in queries if date.fromisoformat(q["depart"]) >= today]


def sweep_calls(search: dict[str, Any], sets: dict[str, list[str]], today: date) -> int:
    plan = search_plan(search, sets)
    return sum(len(live(queries, today)) for queries in plan.values())
