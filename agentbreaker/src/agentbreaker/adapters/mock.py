"""A deterministic, scripted "naive agent". No LLM, $0, used by tests and CI.

It follows a fixed plan of tool calls (from the scenario) to do the user's
task, and after every tool result it scans the text for instructions and
obeys them. That is the textbook vulnerable agent: it cannot tell DATA (the
email/document/page it read) from INSTRUCTIONS (what its user asked).

It recognises only plain-text English instructions. It does not decode
base64, so encoding-trick payloads fail against it. That gives a realistic
spread in the ASR matrix instead of 100% everywhere.
"""

from __future__ import annotations

import re
from typing import Any

from ..environment import MockEnvironment
from ..trace import Trace

TOOL_RE = re.compile(r'call the (\w+) tool with ((?:\w+="[^"]*"[\s,]*(?:and\s+)?)+)', re.IGNORECASE)
KV_RE = re.compile(r'(\w+)="([^"]*)"')
ANSWER_RE = re.compile(r"include the exact code (\S+?) in your final answer", re.IGNORECASE)
HIJACK_RE = re.compile(r"\b(?:stop|abandon) the current task\b", re.IGNORECASE)


def parse_instructions(text: str) -> dict[str, Any]:
    """Extract the instructions a naive agent would 'see' in a piece of text."""
    tool_calls = [
        (m.group(1), dict(KV_RE.findall(m.group(2)))) for m in TOOL_RE.finditer(text)
    ]
    answer_codes = ANSWER_RE.findall(text)
    return {
        "tool_calls": tool_calls,
        "answer_codes": answer_codes,
        "hijack": bool(HIJACK_RE.search(text)),
    }


class MockNaiveAgent:
    name = "mock"

    def __init__(self, max_injected_actions: int = 5) -> None:
        self.max_injected_actions = max_injected_actions

    def cache_identity(self) -> dict[str, Any]:
        return {"target": "mock", "version": 1}

    def estimate_cost(self, task: str, environment: MockEnvironment) -> float:
        return 0.0

    def run(self, task: str, environment: MockEnvironment) -> Trace:
        trace = Trace(target="mock", model="naive-scripted-v1")
        trace.log("user", text=task)
        plan = environment.hints.get("mock_plan", [])
        extra_answer: list[str] = []
        hijacked = False
        injected_actions = 0

        for step in plan:
            if hijacked:
                break
            output = environment.call(step["tool"], step.get("args", {}))
            trace.record_tool_call(step["tool"], step.get("args", {}), output)
            trace.steps += 1

            found = parse_instructions(output)
            # The naive agent obeys every instruction it finds. That is the bug.
            for tool_name, args in found["tool_calls"]:
                if injected_actions >= self.max_injected_actions:
                    break
                injected_actions += 1
                out = environment.call(tool_name, args)
                trace.record_tool_call(tool_name, args, out)
                trace.steps += 1
            extra_answer.extend(found["answer_codes"])
            if found["hijack"]:
                hijacked = True
                trace.log("note", text="Agent abandoned the user's task (hijacked).")

        answer = "" if hijacked else environment.hints.get("mock_answer", "")
        if extra_answer:
            answer = (answer + " " + " ".join(extra_answer)).strip()
        if hijacked and not answer:
            answer = "Done."
        trace.final_answer = answer
        trace.log("assistant", text=answer)
        return trace
