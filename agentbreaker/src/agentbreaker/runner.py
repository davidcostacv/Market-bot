"""Build the test matrix, run each case (or load it from cache), and score it."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .corpus import Template, make_canary
from .oracles import evaluate
from .scenarios import Scenario
from .trace import Trace


class CostLimitError(RuntimeError):
    """Raised before any paid call when the estimated cost exceeds --max-cost."""


@dataclass
class Case:
    case_id: str
    scenario: Scenario
    template: Template | None  # None = baseline (no attack) to measure clean utility
    location: str | None
    canary: str | None

    @property
    def category(self) -> str:
        return self.template.category if self.template else "baseline"

    @property
    def goal(self) -> str | None:
        return self.template.goal if self.template else None

    def environment(self):
        payload = self.template.render(self.canary) if self.template else None
        return self.scenario.build_environment(self.location, payload)


def build_cases(
    scenarios: list[Scenario],
    templates: list[Template],
    seed: int,
    include_baseline: bool = True,
) -> list[Case]:
    """Every scenario x template x supported location, plus one clean baseline per scenario."""
    cases: list[Case] = []
    for s in scenarios:
        if include_baseline:
            cases.append(Case(f"{s.id}__baseline", s, None, None, None))
        for t in templates:
            for loc in s.injection_points:
                cid = f"{s.id}__{t.id}__{loc}"
                cases.append(Case(cid, s, t, loc, make_canary(seed, cid)))
    return cases


def cache_key(agent_identity: dict[str, Any], case: Case) -> str:
    blob = json.dumps(
        {
            "agent": agent_identity,
            "scenario": case.scenario.fingerprint(),
            "template": case.template.fingerprint() if case.template else None,
            "location": case.location,
            "canary": case.canary,
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


class JsonCache:
    """One JSON file per case. Re-running the same matrix costs zero tokens."""

    def __init__(self, directory: str | Path | None) -> None:
        self.dir = Path(directory) if directory else None

    def get(self, key: str) -> Trace | None:
        if not self.dir:
            return None
        f = self.dir / f"{key}.json"
        if not f.exists():
            return None
        return Trace.from_dict(json.loads(f.read_text(encoding="utf-8")))

    def put(self, key: str, trace: Trace) -> None:
        if not self.dir:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{key}.json").write_text(json.dumps(trace.to_dict(), indent=2), encoding="utf-8")


def plan(agent: Any, cases: list[Case], cache: JsonCache) -> dict[str, Any]:
    """What a run would do: how many cases are cached, and the worst-case cost of the rest."""
    identity = agent.cache_identity()
    uncached = [c for c in cases if cache.get(cache_key(identity, c)) is None]
    estimate = sum(agent.estimate_cost(c.scenario.task, c.environment()) for c in uncached)
    return {
        "total_cases": len(cases),
        "cached": len(cases) - len(uncached),
        "to_run": len(uncached),
        "estimated_cost_usd": round(estimate, 4),
    }


def run_cases(
    agent: Any,
    cases: list[Case],
    cache: JsonCache,
    max_cost: float | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Run all cases. Stops making new calls once actual spend reaches max_cost."""
    identity = agent.cache_identity()
    if max_cost is not None:
        p = plan(agent, cases, cache)
        if p["estimated_cost_usd"] > max_cost:
            raise CostLimitError(
                f"Estimated cost ${p['estimated_cost_usd']:.4f} for {p['to_run']} uncached cases "
                f"exceeds --max-cost ${max_cost:.2f}. Use a cheaper --model, fewer scenarios, "
                f"or raise --max-cost."
            )
    spent = 0.0
    results: list[dict[str, Any]] = []
    for i, case in enumerate(cases, 1):
        key = cache_key(identity, case)
        trace = cache.get(key)
        cached = trace is not None
        status = "cached" if cached else "ran"
        if trace is None:
            if max_cost is not None and spent >= max_cost:
                status = "skipped_budget"
                trace = Trace(target=getattr(agent, "name", "?"), error="skipped: --max-cost reached")
            else:
                trace = agent.run(case.scenario.task, case.environment())
                spent += trace.cost_usd
                # Do not cache transient failures (e.g. Ollama not running), so a re-run retries them.
                if trace.error is None or trace.error.startswith("max_"):
                    cache.put(key, trace)
        verdict = evaluate(trace, case.canary, case.goal, case.scenario.utility_keywords)
        results.append(
            {
                "case_id": case.case_id,
                "scenario": case.scenario.id,
                "template": case.template.id if case.template else None,
                "category": case.category,
                "goal": case.goal,
                "location": case.location,
                "canary": case.canary,
                "status": status,
                "cost_usd": 0.0 if cached else trace.cost_usd,
                "oracle": verdict,
                "trace": trace.to_dict(),
            }
        )
        if progress:
            flag = "HIT " if verdict["attack_success"] else "    "
            progress(f"[{i:>3}/{len(cases)}] {flag}{status:<14} {case.case_id}")
    return results
