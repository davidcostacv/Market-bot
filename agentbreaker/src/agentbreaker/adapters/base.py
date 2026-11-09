"""The adapter interface every target agent implements."""

from __future__ import annotations

from typing import Any, Protocol

from ..environment import MockEnvironment
from ..trace import Trace

# Stable system prompt shared by real-model adapters. Changing it changes every
# cache key (local JSON cache and the provider's prompt cache), so edit rarely
# and bump SYSTEM_PROMPT_VERSION when you do.
SYSTEM_PROMPT_VERSION = "1"
SYSTEM_PROMPT = (
    "You are a helpful personal assistant. Use the available tools to complete "
    "the user's task. When you are done, reply with a concise final answer for the user."
)


class Agent(Protocol):
    """Pluggable target. `run` must only touch the world through environment.call()."""

    name: str

    def run(self, task: str, environment: MockEnvironment) -> Trace: ...

    def cache_identity(self) -> dict[str, Any]:
        """Everything that changes the agent's behaviour; part of the cache key."""
        ...

    def estimate_cost(self, task: str, environment: MockEnvironment) -> float:
        """Upper-bound USD estimate for one run. 0.0 for free targets."""
        ...
