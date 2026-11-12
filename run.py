#!/usr/bin/env python3
"""Runs every enabled agent once, writes the dashboard data, sends alerts.

    python run.py                       normal run (needs secrets in the environment)
    python run.py --dry-run             print results, send and save nothing
    python run.py --only flights        run a single agent
    python run.py --mock-serpapi FILE   answer every SerpApi call from a saved JSON file
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents import discover
from core.base import Alert, Context
from core.http import get_json
from core.notify import send_ntfy
from core.store import load_json, save_json

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state" / "state.json"
HUB_PATH = ROOT / "docs" / "data" / "hub.json"


def run_hub(
    config: dict[str, Any],
    state: dict[str, Any],
    prev_hub: dict[str, Any],
    ctx: Context,
    registry: dict[str, type],
    only: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[Alert]]:
    state = copy.deepcopy(state)
    usage = state.setdefault("_usage", {})
    agent_states = state.setdefault("agents", {})
    now_iso = ctx.now.isoformat(timespec="seconds")
    month = ctx.now.strftime("%Y-%m")
    prev_agents = prev_hub.get("agents", {})
    hub: dict[str, Any] = {"updated": now_iso, "agents": {}}
    alerts: list[Alert] = []

    for key, settings in config.get("agents", {}).items():
        if not settings.get("enabled", True):
            continue
        if only and key != only:
            if key in prev_agents:
                hub["agents"][key] = prev_agents[key]
            continue

        prev = prev_agents.get(key, {})
        cls = registry.get(key)
        limit = settings.get("monthly_call_budget")
        used = usage.get(key, {}).get(month, 0)
        remaining = None if limit is None else max(int(limit) - used, 0)
        entry: dict[str, Any] = {
            "title": getattr(cls, "title", "") or key,
            "summary": "",
            "items": [],
            "updated": now_iso,
            "error": None,
        }

        try:
            if cls is None:
                raise RuntimeError(f"no agent module registered for '{key}'")
            result = cls(settings, agent_states.get(key, {}), ctx, remaining).run()
        except Exception as exc:  # one broken agent must not stop the others
            message = str(exc) if type(exc).__name__ == "AgentError" else f"{type(exc).__name__}: {str(exc)[:200]}"
            entry["error"] = message
            entry["summary"] = prev.get("summary", "")
            entry["items"] = prev.get("items", [])
            entry["budget"] = prev.get("budget")
            if not prev.get("error"):
                alerts.append(Alert(title=f"{entry['title']} agent failed", message=message, priority=2))
        else:
            agent_states[key] = result.state
            usage[key] = {month: used + result.calls_used}
            entry["summary"] = result.summary
            entry["items"] = result.items
            alerts.extend(result.alerts)
            if limit is not None:
                entry["budget"] = {"used": used + result.calls_used, "limit": int(limit), "month": month}
        hub["agents"][key] = entry

    return hub, state, alerts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only")
    parser.add_argument("--mock-serpapi", metavar="FILE")
    parser.add_argument("--plan", action="store_true", help="show API calls each search needs, make none")
    args = parser.parse_args(argv)

    if args.plan:
        from agents.flights import FlightsAgent

        settings = load_json(CONFIG_PATH, {}).get("agents", {}).get("flights", {})
        rows = FlightsAgent.plan(settings, datetime.now(timezone.utc).date())
        for row in rows:
            print(f"{row['calls']:>3} calls per check, every {row['every_days']:g} days, about {row['per_month']} per month: {row['name']}")
        total = round(sum(r["per_month"] for r in rows), 1)
        print(f"Total about {total} calls per month (budget {settings.get('monthly_call_budget', 'none')})")
        return 0

    env = dict(os.environ)
    http_get = get_json
    if args.mock_serpapi:
        fixture = json.loads(Path(args.mock_serpapi).read_text(encoding="utf-8"))
        http_get = lambda url, timeout: fixture  # noqa: E731
        env.setdefault("SERPAPI_API_KEY", "mock")

    ctx = Context(env=env, now=datetime.now(timezone.utc), http_get=http_get)
    config = load_json(CONFIG_PATH, {"agents": {}})
    state = load_json(STATE_PATH, {})
    prev_hub = load_json(HUB_PATH, {})

    hub, new_state, alerts = run_hub(config, state, prev_hub, ctx, discover(), args.only)

    topic = env.get("NTFY_TOPIC")
    server = env.get("NTFY_SERVER", "https://ntfy.sh")
    for alert in alerts:
        print(f"ALERT [{alert.priority}] {alert.title}\n{alert.message}\n")
        if args.dry_run or not topic:
            continue
        try:
            send_ntfy(alert, topic, server)
        except Exception as exc:
            print(f"ntfy delivery failed ({type(exc).__name__})", file=sys.stderr)

    if args.dry_run:
        print(json.dumps(hub, indent=2, ensure_ascii=False))
        return 0

    save_json(HUB_PATH, hub)
    save_json(STATE_PATH, new_state)
    for key, entry in hub["agents"].items():
        status = f"error: {entry['error']}" if entry.get("error") else entry["summary"]
        print(f"{key}: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
