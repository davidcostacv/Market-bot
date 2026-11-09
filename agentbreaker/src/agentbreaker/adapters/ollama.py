"""Local model via the Ollama HTTP API. Free: runs on your own GPU.

Uses only the standard library (urllib), so no extra dependency. It talks to
http://localhost:11434 by default, i.e. a model on YOUR machine. Nothing leaves it.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable

from ..environment import MockEnvironment
from ..trace import Trace
from .base import SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION

PostFn = Callable[[str, dict[str, Any]], dict[str, Any]]


def _http_post(url: str, payload: dict[str, Any], timeout: float = 300.0) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local URL by design
        return json.loads(resp.read().decode("utf-8"))


class OllamaAgent:
    name = "ollama"

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        base_url: str = "http://localhost:11434",
        seed: int = 0,
        max_steps: int = 8,
        post: PostFn | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.seed = seed
        self.max_steps = max_steps
        self._post = post or _http_post

    def cache_identity(self) -> dict[str, Any]:
        return {
            "target": "ollama",
            "model": self.model,
            "seed": self.seed,
            "max_steps": self.max_steps,
            "system": SYSTEM_PROMPT_VERSION,
        }

    def estimate_cost(self, task: str, environment: MockEnvironment) -> float:
        return 0.0  # local GPU: only electricity

    @staticmethod
    def _tools(environment: MockEnvironment) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in environment.tool_specs()
        ]

    def run(self, task: str, environment: MockEnvironment) -> Trace:
        trace = Trace(target="ollama", model=self.model)
        trace.log("user", text=task)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        tools = self._tools(environment)
        try:
            for _ in range(self.max_steps):
                resp = self._post(
                    f"{self.base_url}/api/chat",
                    {
                        "model": self.model,
                        "messages": messages,
                        "tools": tools,
                        "stream": False,
                        # temperature 0 + fixed seed: as repeatable as a local model gets.
                        "options": {"temperature": 0, "seed": self.seed},
                    },
                )
                trace.steps += 1
                trace.usage["input_tokens"] += int(resp.get("prompt_eval_count", 0) or 0)
                trace.usage["output_tokens"] += int(resp.get("eval_count", 0) or 0)
                msg = resp.get("message", {}) or {}
                content = msg.get("content", "") or ""
                calls = msg.get("tool_calls") or []
                if content:
                    trace.log("assistant", text=content)
                messages.append({"role": "assistant", "content": content, "tool_calls": calls})
                if not calls:
                    trace.final_answer = content
                    break
                for call in calls:
                    fn = call.get("function", {})
                    name = fn.get("name", "")
                    args = fn.get("arguments", {})
                    if isinstance(args, str):  # some models return a JSON string
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    output = environment.call(name, args)
                    trace.record_tool_call(name, args, output)
                    # [VERIFICAR] Newer Ollama versions read "tool_name"; older ones ignore it.
                    messages.append({"role": "tool", "content": output, "tool_name": name})
            else:
                trace.error = f"max_steps ({self.max_steps}) reached"
        except OSError as exc:  # URLError is a subclass of OSError
            trace.error = f"Ollama request failed: {exc}. Is `ollama serve` running?"
        return trace
