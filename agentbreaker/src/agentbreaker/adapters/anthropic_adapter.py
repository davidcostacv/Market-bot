"""Claude via the official `anthropic` Python SDK. PAID: every run costs tokens.

Cost controls built in:
- The runner's JSON cache means a case is only ever paid for once per seed.
- The system prompt and tool list are byte-stable (tools sorted by name) and
  carry cache_control, so the provider's prompt cache can serve repeated
  prefixes at ~0.1x price (only once the prefix exceeds the model's minimum
  cacheable length).
- `estimate_cost` feeds the runner's --max-cost / --dry-run guard.

API notes for current models:
- tool_choice is always {"type": "auto"}; forced tool choice returns HTTP 400.
- stop_reason "refusal" is handled: the run stops and is recorded as refused.
- No temperature/top_p: current models reject sampling parameters.
- The key is read by the SDK from the ANTHROPIC_API_KEY environment variable.
"""

from __future__ import annotations

import json
from typing import Any

from ..environment import MockEnvironment
from ..trace import Trace
from .base import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION

# USD per million tokens (input, output). Cache reads bill ~0.1x input, writes ~1.25x.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
DEFAULT_MODEL = "claude-opus-5-5"
CHEAP_MODEL = "claude-haiku-5-5"


def price_for(model: str) -> tuple[float, float]:
    # Unknown model: assume the most expensive known price, so guards stay conservative.
    return PRICES.get(model, max(PRICES.values()))


def cost_from_usage(model: str, usage: dict[str, int]) -> float:
    p_in, p_out = price_for(model)
    return (
        usage.get("input_tokens", 0) * p_in
        + usage.get("cache_creation_input_tokens", 0) * p_in * 1.25
        + usage.get("cache_read_input_tokens", 0) * p_in * 0.10
        + usage.get("output_tokens", 0) * p_out
    ) / 1_000_000


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from an SDK object or a plain dict (tests use dicts/namespaces)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class AnthropicAgent:
    name = "anthropic"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        effort: str | None = None,
        max_steps: int = 8,
        max_tokens: int = 16000,
        fallbacks: bool = False,
        client: Any = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.fallbacks = fallbacks
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic  # imported lazily: the mock target never needs it
            except ImportError as exc:  # pragma: no cover - depends on install
                raise RuntimeError(
                    'The anthropic SDK is not installed. Run: pip install -e ".[anthropic]"'
                ) from exc
            self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        return self._client

    def cache_identity(self) -> dict[str, Any]:
        return {
            "target": "anthropic",
            "model": self.model,
            "effort": self.effort,
            "max_steps": self.max_steps,
            "fallbacks": self.fallbacks,
            "system": SYSTEM_PROMPT_VERSION,
        }

    def estimate_cost(self, task: str, environment: MockEnvironment) -> float:
        """Rough upper bound: assume every turn re-reads the prompt plus all world content."""
        world = json.dumps(
            [environment.inbox, environment.documents, environment.web_pages,
             environment.files, environment.search_results],
            ensure_ascii=False,
        )
        base = len(SYSTEM_PROMPT) + len(json.dumps(environment.tool_specs())) + len(task)
        turns = min(self.max_steps, 6)
        input_tokens = turns * (base + len(world)) / 4  # ~4 chars per token
        output_tokens = turns * 800  # includes adaptive thinking
        p_in, p_out = price_for(self.model)
        return (input_tokens * p_in + output_tokens * p_out) / 1_000_000

    def _tools(self, environment: MockEnvironment) -> list[dict[str, Any]]:
        tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
            for t in environment.tool_specs()
        ]
        if tools:
            # Breakpoint after the tools: tools + system form the stable cached prefix.
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        return tools

    def _create(self, **kwargs: Any) -> Any:
        if self.fallbacks:
            # Opt-in: on a refusal the API retries on a fallback model, which changes
            # WHICH model you are measuring. Off by default for clean results.
            return self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        return self.client.messages.create(**kwargs)

    def run(self, task: str, environment: MockEnvironment) -> Trace:
        trace = Trace(target="anthropic", model=self.model)
        trace.log("user", text=task)
        messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        tools = self._tools(environment)
        system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
        try:
            for _ in range(self.max_steps):
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "system": system,
                    "tools": tools,
                    "tool_choice": {"type": "auto"},
                    "messages": messages,
                }
                if self.effort:
                    kwargs["output_config"] = {"effort": self.effort}
                resp = self._create(**kwargs)
                trace.steps += 1
                usage = _get(resp, "usage")
                for k in trace.usage:
                    trace.usage[k] += int(_get(usage, k, 0) or 0)
                served = _get(resp, "model")
                if served and served != self.model:
                    trace.log("note", text=f"served by {served}")

                stop = _get(resp, "stop_reason")
                if stop == "refusal":
                    details = _get(resp, "stop_details")
                    trace.refused = True
                    trace.refusal_category = _get(details, "category") if details else None
                    trace.log("refusal", category=trace.refusal_category)
                    break

                content = list(_get(resp, "content", []) or [])
                texts = [_get(b, "text", "") for b in content if _get(b, "type") == "text"]
                tool_uses = [b for b in content if _get(b, "type") == "tool_use"]
                for t in texts:
                    if t:
                        trace.log("assistant", text=t)
                # Append the full content (incl. thinking blocks) unchanged.
                messages.append({"role": "assistant", "content": content})

                if stop == "pause_turn":
                    continue
                if not tool_uses:
                    trace.final_answer = "\n".join(t for t in texts if t)
                    if stop == "max_tokens":
                        trace.error = "max_tokens reached"
                    break

                results = []
                for block in tool_uses:
                    name = _get(block, "name")
                    args = _get(block, "input", {}) or {}
                    output = environment.call(name, args)
                    trace.record_tool_call(name, args, output)
                    results.append(
                        {"type": "tool_result", "tool_use_id": _get(block, "id"), "content": output}
                    )
                # All results for one turn go back in ONE user message.
                messages.append({"role": "user", "content": results})
            else:
                trace.error = f"max_steps ({self.max_steps}) reached"
        except Exception as exc:  # noqa: BLE001 - record any API/network failure in the trace
            trace.error = f"{type(exc).__name__}: {exc}"
        trace.cost_usd = cost_from_usage(self.model, trace.usage)
        return trace
