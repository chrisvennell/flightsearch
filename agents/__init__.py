"""Agent registry. Every module in this package is imported and any Agent
subclass with a `key` is registered, so adding an agent means adding a file."""
from __future__ import annotations

import importlib
import pkgutil

from core.base import Agent


def discover() -> dict[str, type[Agent]]:
    found: dict[str, type[Agent]] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, Agent) and obj is not Agent and obj.key:
                found[obj.key] = obj
    return found
