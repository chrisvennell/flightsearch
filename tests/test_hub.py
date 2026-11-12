import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.base import Agent, AgentResult, Alert, Context  # noqa: E402
from core.notify import build_request  # noqa: E402
from run import run_hub  # noqa: E402

NOW = datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)


def make_ctx():
    return Context(env={}, now=NOW, http_get=lambda url, timeout: {})


class Boom(Agent):
    key = "boom"
    title = "Boom"

    def run(self):
        raise ValueError("kaboom")


class Counter(Agent):
    key = "counter"
    title = "Counter"

    def run(self):
        n = self.state.get("n", 0) + 1
        return AgentResult(
            summary=f"run {n}",
            items=[{"title": "n", "value": str(n)}],
            alerts=[Alert(title="hi", message="there")],
            state={"n": n},
            calls_used=3,
        )


class Hub(unittest.TestCase):
    REGISTRY = {"boom": Boom, "counter": Counter}
    CONFIG = {
        "agents": {
            "counter": {"monthly_call_budget": 10},
            "boom": {},
            "ghost": {},
            "off": {"enabled": False},
        }
    }

    def run_once(self, state=None, prev=None, only=None):
        return run_hub(self.CONFIG, state or {}, prev or {}, make_ctx(), self.REGISTRY, only)

    def test_failing_agent_does_not_stop_others(self):
        hub, state, alerts = self.run_once()
        self.assertEqual(hub["agents"]["counter"]["summary"], "run 1")
        self.assertIn("ValueError", hub["agents"]["boom"]["error"])
        self.assertIn("no agent module", hub["agents"]["ghost"]["error"])
        self.assertNotIn("off", hub["agents"])

    def test_usage_accumulates_within_a_month(self):
        _, state, _ = self.run_once()
        _, state, _ = self.run_once(state=state)
        self.assertEqual(state["_usage"]["counter"]["2026-11"], 6)
        hub, _, _ = self.run_once(state=state)
        self.assertEqual(hub["agents"]["counter"]["budget"], {"used": 9, "limit": 10, "month": "2026-11"})

    def test_error_alert_only_on_first_failure(self):
        hub1, _, alerts1 = self.run_once()
        self.assertEqual(len([a for a in alerts1 if "failed" in a.title]), 2)
        _, _, alerts2 = self.run_once(prev=hub1)
        self.assertEqual([a for a in alerts2 if "failed" in a.title], [])

    def test_failed_agent_keeps_previous_items(self):
        prev = {"agents": {"boom": {"summary": "old", "items": [{"title": "kept"}]}}}
        hub, _, _ = self.run_once(prev=prev)
        self.assertEqual(hub["agents"]["boom"]["items"], [{"title": "kept"}])

    def test_only_flag_runs_one_agent_and_carries_the_rest(self):
        prev = {"agents": {"boom": {"title": "Boom", "summary": "carried", "items": []}}}
        hub, _, _ = self.run_once(prev=prev, only="counter")
        self.assertEqual(hub["agents"]["boom"]["summary"], "carried")
        self.assertEqual(hub["agents"]["counter"]["summary"], "run 1")


class Notify(unittest.TestCase):
    def test_title_is_ascii_and_click_must_be_https(self):
        alert = Alert(title="Flight deal ✈", message="Sydney\n$450", priority=4, click="javascript:alert(1)")
        url, body, headers = build_request(alert, "topic123")
        self.assertEqual(url, "https://ntfy.sh/topic123")
        self.assertEqual(headers["Title"], "Flight deal")
        self.assertEqual(headers["Priority"], "4")
        self.assertNotIn("Click", headers)
        self.assertEqual(body.decode("utf-8"), "Sydney\n$450")

    def test_https_click_is_kept(self):
        alert = Alert(title="x", message="y", click="https://example.com/a")
        self.assertEqual(build_request(alert, "t")[2]["Click"], "https://example.com/a")


if __name__ == "__main__":
    unittest.main()
