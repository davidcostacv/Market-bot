"""The Trace: everything an agent did during one run, in a JSON-friendly shape."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _empty_usage() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


@dataclass
class Trace:
    """Record of one agent run.

    events is the ordered log (assistant text, tool calls, tool results, refusals).
    tool_calls is the subset the oracles care about: every tool the agent invoked,
    with its arguments and the (mock) output it received.
    """

    target: str = ""
    model: str = ""
    final_answer: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    refused: bool = False
    refusal_category: str | None = None
    error: str | None = None
    steps: int = 0
    usage: dict[str, int] = field(default_factory=_empty_usage)
    cost_usd: float = 0.0

    def log(self, kind: str, **data: Any) -> None:
        self.events.append({"type": kind, **data})

    def record_tool_call(self, name: str, args: dict[str, Any], output: str) -> None:
        self.tool_calls.append({"name": name, "args": args, "output": output})
        self.log("tool_call", name=name, args=args)
        self.log("tool_result", name=name, output=output)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Trace":
        return cls(**data)
