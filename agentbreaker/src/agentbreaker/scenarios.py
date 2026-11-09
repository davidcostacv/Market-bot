"""Scenarios: a user task + a mock world + where an attacker can plant text."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .environment import MockEnvironment

# Where injected text can live. Each maps to one "untrusted input channel".
LOCATIONS = ("email_body", "document", "web_page", "tool_result")


@dataclass
class Scenario:
    id: str
    description: str
    task: str
    tools: list[str]
    environment: dict[str, Any]
    # location -> where exactly to put the payload, e.g. {"email_body": {"target": "e2"}}
    injection_points: dict[str, dict[str, str]]
    # Keywords that a correct final answer must contain (case-insensitive).
    utility_keywords: list[str]
    mock_plan: list[dict[str, Any]] = field(default_factory=list)
    mock_answer: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def fingerprint(self) -> str:
        blob = json.dumps(self.raw, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def build_environment(self, location: str | None = None, payload: str | None = None) -> MockEnvironment:
        """Create a fresh sandbox, optionally with `payload` planted at `location`."""
        data = json.loads(json.dumps(self.environment))  # deep copy, JSON-safe
        injection: tuple[str, str] | None = None
        if location is not None:
            if location not in self.injection_points:
                raise ValueError(f"{self.id}: location '{location}' not supported")
            spec = self.injection_points[location]
            if location == "email_body":
                email = next(e for e in data["inbox"] if e["id"] == spec["target"])
                email["body"] = f"{email['body']}\n\n{payload}"
            elif location == "document":
                data["documents"][spec["target"]] += f"\n\n{payload}"
            elif location == "web_page":
                data["web_pages"][spec["target"]] += f"\n\n{payload}"
            elif location == "tool_result":
                injection = (spec["tool"], payload or "")
        return MockEnvironment(
            data,
            allowed_tools=self.tools,
            tool_result_injection=injection,
            hints={"mock_plan": self.mock_plan, "mock_answer": self.mock_answer},
        )


def _parse(raw: dict[str, Any], source: str) -> Scenario:
    for key in ("id", "task", "tools", "environment", "injection_points", "utility"):
        if key not in raw:
            raise ValueError(f"{source}: missing key '{key}'")
    bad = [loc for loc in raw["injection_points"] if loc not in LOCATIONS]
    if bad:
        raise ValueError(f"{source}: unknown injection locations {bad}")
    s = Scenario(
        id=raw["id"],
        description=raw.get("description", ""),
        task=raw["task"],
        tools=list(raw["tools"]),
        environment=raw["environment"],
        injection_points=raw["injection_points"],
        utility_keywords=list(raw["utility"].get("answer_contains_all", [])),
        mock_plan=list(raw.get("mock_plan", [])),
        mock_answer=raw.get("mock_answer", ""),
        raw=raw,
    )
    # Validate injection targets exist, so typos fail loudly at load time.
    env = s.environment
    for loc, spec in s.injection_points.items():
        if loc == "email_body" and not any(e["id"] == spec["target"] for e in env.get("inbox", [])):
            raise ValueError(f"{source}: email target '{spec['target']}' not in inbox")
        if loc == "document" and spec["target"] not in env.get("documents", {}):
            raise ValueError(f"{source}: document '{spec['target']}' not found")
        if loc == "web_page" and spec["target"] not in env.get("web_pages", {}):
            raise ValueError(f"{source}: web page '{spec['target']}' not found")
        if loc == "tool_result" and spec["tool"] not in s.tools:
            raise ValueError(f"{source}: tool_result tool '{spec['tool']}' not in tools")
    return s


def load_scenarios(path: str | Path) -> list[Scenario]:
    """Load one YAML file or every *.yaml file in a directory (sorted by name)."""
    p = Path(path)
    files = sorted(p.glob("*.yaml")) if p.is_dir() else [p]
    if not files:
        raise ValueError(f"No scenario files found at {p}")
    scenarios = [_parse(yaml.safe_load(f.read_text(encoding="utf-8")), f.name) for f in files]
    ids = [s.id for s in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate scenario ids")
    return scenarios
