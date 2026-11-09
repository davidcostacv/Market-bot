"""Turn scored results into an ASR matrix plus JSON and Markdown reports."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .scenarios import LOCATIONS


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "-"


def is_hard_error(r: dict[str, Any]) -> bool:
    """A run that never really happened (network down, API error). Hitting max_steps is a valid result."""
    err = r["trace"]["error"]
    return bool(err) and not err.startswith("max_")


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    # Budget-skipped and errored runs are excluded from every rate: counting them
    # as "attack failed" would make a broken target look secure.
    scored = [r for r in results if r["status"] != "skipped_budget" and not is_hard_error(r)]
    attacks = [r for r in scored if r["template"] is not None]
    baselines = [r for r in scored if r["template"] is None]
    hits = sum(r["oracle"]["attack_success"] for r in attacks)
    util_attack = sum(r["oracle"]["utility"] for r in attacks)
    util_clean = sum(r["oracle"]["utility"] for r in baselines)

    matrix: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in attacks:
        cell = matrix[r["category"]][r["location"]]
        cell[0] += int(r["oracle"]["attack_success"])
        cell[1] += 1
    return {
        "cases_total": len(results),
        "cases_skipped": sum(r["status"] == "skipped_budget" for r in results),
        "cases_errored": sum(r["status"] != "skipped_budget" and is_hard_error(r) for r in results),
        "attack_cases": len(attacks),
        "attack_successes": hits,
        "asr": hits / len(attacks) if attacks else 0.0,
        "utility_under_attack": util_attack / len(attacks) if attacks else 0.0,
        "utility_clean": util_clean / len(baselines) if baselines else None,
        "refusals": sum(r["trace"]["refused"] for r in scored),
        "cost_usd_this_run": round(sum(r["cost_usd"] for r in results), 6),
        "matrix": {cat: {loc: v for loc, v in locs.items()} for cat, locs in matrix.items()},
    }


def render_markdown(summary: dict[str, Any], results: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    out: list[str] = []
    out.append("# AgentBreaker report\n")
    out.append(f"- **Target:** `{meta.get('target')}`  **Model:** `{meta.get('model') or '-'}`  "
               f"**Seed:** `{meta.get('seed')}`")
    out.append(f"- **Cases:** {summary['cases_total']} ({summary['attack_cases']} attacks, "
               f"{summary['cases_skipped']} skipped for budget, {summary['cases_errored']} errored; "
               f"both are excluded from the rates)")
    out.append(f"- **Attack success rate (ASR):** {summary['asr']:.1%} "
               f"({summary['attack_successes']}/{summary['attack_cases']}). Lower is better.")
    out.append(f"- **Utility under attack:** {summary['utility_under_attack']:.1%}. Higher is better.")
    if summary["utility_clean"] is not None:
        out.append(f"- **Utility without attack (baseline):** {summary['utility_clean']:.1%}")
    out.append(f"- **Refusals:** {summary['refusals']}  **Errored:** {summary['cases_errored']}  "
               f"**Cost this run:** ${summary['cost_usd_this_run']:.4f}\n")

    locs = [loc for loc in LOCATIONS if any(loc in row for row in summary["matrix"].values())]
    out.append("## ASR matrix (attack category x injection location)\n")
    out.append("| Category | " + " | ".join(locs) + " | Total |")
    out.append("|---|" + "---|" * (len(locs) + 1))
    for cat in sorted(summary["matrix"]):
        row = summary["matrix"][cat]
        hits = sum(v[0] for v in row.values())
        total = sum(v[1] for v in row.values())
        cells = [_pct(*row[loc]) if loc in row else "-" for loc in locs]
        out.append(f"| {cat} | " + " | ".join(cells) + f" | {_pct(hits, total)} |")
    out.append("")

    out.append("## Per-case results\n")
    out.append("| Case | Attack success | Sinks | Answer leak | Utility | Status |")
    out.append("|---|---|---|---|---|---|")
    for r in results:
        o = r["oracle"]
        out.append(
            f"| `{r['case_id']}` | {'**YES**' if o['attack_success'] else 'no'} | "
            f"{', '.join(o['canary_sinks']) or '-'} | {'yes' if o['answer_leak'] else 'no'} | "
            f"{'ok' if o['utility'] else 'FAIL'} | {r['status']} |"
        )
    out.append("")

    out.append("## Traces\n")
    for r in results:
        t = r["trace"]
        out.append(f"<details><summary><code>{r['case_id']}</code> canary=<code>{r['canary']}</code></summary>\n")
        out.append("```text")
        for e in t["events"]:
            kind = e["type"]
            if kind == "tool_call":
                out.append(f"TOOL CALL   {e['name']}({json.dumps(e['args'], ensure_ascii=False)})")
            elif kind == "tool_result":
                text = e["output"].replace("\n", " ")
                out.append(f"TOOL RESULT {text[:300]}{'...' if len(text) > 300 else ''}")
            else:
                out.append(f"{kind.upper():<11} {e.get('text', e.get('category', ''))}")
        if t["error"]:
            out.append(f"ERROR       {t['error']}")
        out.append("```\n</details>\n")
    return "\n".join(out)


def write_reports(out_dir: str | Path, summary: dict[str, Any], results: list[dict[str, Any]],
                  meta: dict[str, Any]) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    jpath = out / "report.json"
    mpath = out / "report.md"
    jpath.write_text(json.dumps({"meta": meta, "summary": summary, "results": results}, indent=2,
                                ensure_ascii=False), encoding="utf-8")
    mpath.write_text(render_markdown(summary, results, meta), encoding="utf-8")
    return jpath, mpath
