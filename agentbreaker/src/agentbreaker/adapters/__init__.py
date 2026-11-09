"""Target adapters. Each wraps one kind of agent behind Agent.run(task, environment)."""

from __future__ import annotations

from typing import Any

from .anthropic_adapter import AnthropicAgent
from .mock import MockNaiveAgent
from .ollama import OllamaAgent

TARGETS = ("mock", "ollama", "anthropic")
PAID_TARGETS = {"anthropic"}


def make_agent(target: str, **opts: Any):
    if target == "mock":
        return MockNaiveAgent()
    if target == "ollama":
        kwargs = {k: v for k, v in opts.items() if k in ("model", "base_url", "seed", "max_steps") and v is not None}
        return OllamaAgent(**kwargs)
    if target == "anthropic":
        kwargs = {k: v for k, v in opts.items()
                  if k in ("model", "effort", "max_steps", "fallbacks") and v is not None}
        return AnthropicAgent(**kwargs)
    raise ValueError(f"Unknown target '{target}'. Choose from {TARGETS}.")


__all__ = ["AnthropicAgent", "MockNaiveAgent", "OllamaAgent", "TARGETS", "PAID_TARGETS", "make_agent"]
