"""Command line: `agentbreaker run ...` and `agentbreaker list`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .adapters import PAID_TARGETS, TARGETS, make_agent
from .corpus import load_templates
from .report import is_hard_error, summarize, write_reports
from .runner import CostLimitError, JsonCache, build_cases, plan, run_cases
from .scenarios import load_scenarios

DATA = Path(__file__).parent / "data"
DEFAULT_SCENARIOS = DATA / "scenarios"
DEFAULT_CORPUS = DATA / "corpus" / "templates.yaml"
DEFAULT_PAID_MAX_COST = 1.00  # USD; paid targets never run without a ceiling


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentbreaker", description=__doc__)
    p.add_argument("--version", action="version", version=f"agentbreaker {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="Run the injection matrix against a target agent")
    r.add_argument("--target", choices=TARGETS, default="mock")
    r.add_argument("--scenarios", default=str(DEFAULT_SCENARIOS), help="YAML file or directory")
    r.add_argument("--corpus", default=str(DEFAULT_CORPUS), help="Injection templates YAML")
    r.add_argument("--out", default=None, help="Report directory (default: results/<target>)")
    r.add_argument("--seed", type=int, default=1337, help="Derives every canary; also the Ollama seed")
    r.add_argument("--model", default=None, help="Model name (ollama: qwen2.5:7b, anthropic: claude-opus-5-5)")
    r.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None,
                   help="anthropic only: output effort. 'low' is cheapest")
    r.add_argument("--max-steps", type=int, default=None, help="Max agent loop turns per case")
    r.add_argument("--ollama-url", default=None, help="default http://localhost:11434")
    r.add_argument("--fallbacks", action="store_true",
                   help="anthropic only: enable server-side refusal fallbacks (changes the measured model)")
    r.add_argument("--max-cost", type=float, default=None,
                   help=f"USD ceiling for paid targets (default {DEFAULT_PAID_MAX_COST:.2f})")
    r.add_argument("--dry-run", action="store_true", help="Show the plan and cost estimate; call nothing")
    r.add_argument("--cache-dir", default=".agentbreaker_cache")
    r.add_argument("--no-cache", action="store_true", help="Ignore and do not write the JSON cache")
    r.add_argument("--no-baseline", action="store_true", help="Skip the clean (no-attack) runs")
    r.add_argument("--quiet", action="store_true")

    ls = sub.add_parser("list", help="List scenarios and injection templates")
    ls.add_argument("--scenarios", default=str(DEFAULT_SCENARIOS))
    ls.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    return p


def _cmd_list(args: argparse.Namespace) -> int:
    print("Scenarios:")
    for s in load_scenarios(args.scenarios):
        print(f"  {s.id:<18} locations={','.join(s.injection_points)}  task={s.task!r}")
    print("Templates:")
    for t in load_templates(args.corpus):
        print(f"  {t.id:<22} category={t.category:<20} goal={t.goal}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    scenarios = load_scenarios(args.scenarios)
    templates = load_templates(args.corpus)
    cases = build_cases(scenarios, templates, args.seed, include_baseline=not args.no_baseline)
    agent = make_agent(
        args.target,
        model=args.model,
        effort=args.effort,
        max_steps=args.max_steps,
        base_url=args.ollama_url,
        seed=args.seed,
        fallbacks=args.fallbacks or None,
    )
    cache = JsonCache(None if args.no_cache else args.cache_dir)
    max_cost = args.max_cost
    if args.target in PAID_TARGETS and max_cost is None:
        max_cost = DEFAULT_PAID_MAX_COST

    info = plan(agent, cases, cache)
    model = getattr(agent, "model", None)
    print(f"Target: {args.target}  model: {model or '-'}  cases: {info['total_cases']}  "
          f"cached: {info['cached']}  to run: {info['to_run']}  "
          f"estimated cost: ${info['estimated_cost_usd']:.4f}"
          + (f"  (max ${max_cost:.2f})" if max_cost is not None else ""))
    if args.dry_run:
        print("Dry run: nothing was called.")
        return 0

    try:
        results = run_cases(agent, cases, cache, max_cost=max_cost,
                            progress=None if args.quiet else print)
    except CostLimitError as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 2

    summary = summarize(results)
    meta = {"target": args.target, "model": model, "seed": args.seed, "version": __version__,
            "scenarios": [s.id for s in scenarios], "templates": [t.id for t in templates]}
    out_dir = args.out or str(Path("results") / args.target)
    jpath, mpath = write_reports(out_dir, summary, results, meta)
    print(f"\nASR: {summary['asr']:.1%} ({summary['attack_successes']}/{summary['attack_cases']})  "
          f"utility under attack: {summary['utility_under_attack']:.1%}  "
          f"clean utility: {summary['utility_clean'] if summary['utility_clean'] is None else format(summary['utility_clean'], '.1%')}  "
          f"cost: ${summary['cost_usd_this_run']:.4f}")
    print(f"Reports: {mpath}  {jpath}")
    ran = [r for r in results if r["status"] != "skipped_budget"]
    errored = [r for r in ran if is_hard_error(r)]
    if errored:
        print(f"WARNING: {len(errored)}/{len(ran)} cases failed to run. First error: "
              f"{errored[0]['trace']['error']}", file=sys.stderr)
        if len(errored) == len(ran):
            print("Every case failed, so the ASR above is meaningless (not a perfect score).",
                  file=sys.stderr)
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "list":
        return _cmd_list(args)
    return _cmd_run(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
