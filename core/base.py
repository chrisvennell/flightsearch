"""Shared types for every agent.

An agent is a class with a unique `key` and a `run()` method. It receives its
own settings (from config.json), its own saved state (from the previous run)
and a Context, and returns an AgentResult.

Dashboard item schema (plain dicts, every key optional except title):
    title     main line of the card
    subtitle  smaller line under the title
    value     large text on the right (a price, a count, a status)
    badge     short label next to the value
    tone      "good", "warn" or "bad" (colours the badge)
    link      https URL opened when the card is tapped
    history   list of numbers, drawn as a small trend line
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable


class AgentError(Exception):
    """A problem an agent wants shown on the dashboard. Keep the message free of secrets."""


@dataclass
class Alert:
    title: str
    message: str
    priority: int = 3  # ntfy scale: 1 min, 3 default, 4 high, 5 max
    click: str | None = None
    actions: list[tuple[str, str]] = field(default_factory=list)  # (label, https url) buttons


@dataclass
class AgentResult:
    summary: str
    items: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    calls_used: int = 0  # paid or quota-limited API calls made this run


@dataclass
class Context:
    env: dict[str, str]
    now: datetime  # timezone-aware, UTC
    http_get: Callable[[str, float], dict[str, Any]]


class Agent:
    key: str = ""    # unique id, also the key in config.json
    title: str = ""  # heading on the dashboard

    def __init__(
        self,
        settings: dict[str, Any],
        state: dict[str, Any],
        ctx: Context,
        budget_remaining: int | None,
    ) -> None:
        self.settings = settings
        self.state = state
        self.ctx = ctx
        self.budget_remaining = budget_remaining  # None means unlimited

    def run(self) -> AgentResult:
        raise NotImplementedError
