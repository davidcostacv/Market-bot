"""Runner, cache, cost guard, report, CLI and the LLM adapters (with fakes).

The paid Anthropic API is NEVER called here: the adapter is given a fake client.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from agentbreaker.adapters import AnthropicAgent, MockNaiveAgent, OllamaAgent, make_agent
from agentbreaker.adapters.anthropic_adapter import cost_from_usage
from agentbreaker.cli import DEFAULT_CORPUS, DEFAULT_SCENARIOS, main
from agentbreaker.corpus import load_templates
from agentbreaker.report import render_markdown, summarize
from agentbreaker.runner import CostLimitError, JsonCache, build_cases, run_cases
from agentbreaker.scenarios import load_scenarios

SCENARIOS = load_scenarios(DEFAULT_SCENARIOS)
TEMPLATES = load_templates(DEFAULT_CORPUS)


class CountingMock(MockNaiveAgent):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def run(self, task, environment):
        self.calls += 1
        return super().run(task, environment)


class FakePaidAgent(MockNaiveAgent):
    name = "fakepaid"

    def cache_identity(self):
        return {"target": "fakepaid"}

    def estimate_cost(self, task, environment):
        return 0.10

    def run(self, task, environment):
        t = super().run(task, environment)
        t.cost_usd = 0.10
        return t


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_matrix_size(self):
        cases = build_cases(SCENARIOS, TEMPLATES, seed=1)
        # 3 scenarios x 6 templates x 2 locations + 3 baselines
        self.assertEqual(len(cases), 39)
        self.assertEqual(len({c.canary for c in cases if c.canary}), 36)

    def test_cache_makes_second_run_free(self):
        cases = build_cases(SCENARIOS, TEMPLATES, seed=1)
        cache = JsonCache(self.tmp.name)
        agent = CountingMock()
        first = run_cases(agent, cases, cache)
        second = run_cases(agent, cases, cache)
        self.assertEqual(agent.calls, 39)  # second run never called the agent
        self.assertTrue(all(r["status"] == "cached" for r in second))
        self.assertEqual([r["oracle"] for r in first], [r["oracle"] for r in second])

    def test_seed_changes_canaries_and_cache_keys(self):
        cache = JsonCache(self.tmp.name)
        agent = CountingMock()
        run_cases(agent, build_cases(SCENARIOS, TEMPLATES, seed=1), cache)
        run_cases(agent, build_cases(SCENARIOS, TEMPLATES, seed=2), cache)
        self.assertEqual(agent.calls, 3 + 36 * 2)  # baselines have no canary -> shared

    def test_cost_guard_aborts_before_any_call(self):
        cases = build_cases(SCENARIOS, TEMPLATES, seed=1)
        agent = FakePaidAgent()
        with self.assertRaises(CostLimitError):
            run_cases(agent, cases, JsonCache(None), max_cost=1.0)  # 39 x $0.10 > $1

    def test_budget_stops_mid_run(self):
        class Underestimates(FakePaidAgent):
            def run(self, task, environment):
                t = super().run(task, environment)
                t.cost_usd = 0.25  # real spend is higher than the 0.10 estimate
                return t

        cases = build_cases(SCENARIOS, TEMPLATES, seed=1)[:5]
        # Estimate passes (5 x 0.10 = 0.50 <= 0.55), but real spend passes 0.55 after 3 cases.
        results = run_cases(Underestimates(), cases, JsonCache(None), max_cost=0.55)
        statuses = [r["status"] for r in results]
        self.assertEqual(statuses, ["ran", "ran", "ran", "skipped_budget", "skipped_budget"])
        self.assertEqual(summarize(results)["cases_skipped"], 2)

    def test_report(self):
        results = run_cases(MockNaiveAgent(), build_cases(SCENARIOS, TEMPLATES, seed=1), JsonCache(None))
        s = summarize(results)
        self.assertEqual(s["attack_cases"], 36)
        self.assertEqual(s["utility_clean"], 1.0)
        # Naive mock: every plaintext attack lands, base64 never does.
        self.assertEqual(s["matrix"]["encoding-trick"]["email_body"], [0, 1])
        self.assertEqual(s["matrix"]["ignore-previous"]["tool_result"], [3, 3])
        md = render_markdown(s, results, {"target": "mock", "seed": 1})
        self.assertIn("ASR matrix", md)
        self.assertIn("| encoding-trick |", md)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "out"
        self.cache = Path(self.tmp.name) / "cache"

    def test_mock_run_writes_reports(self):
        rc = main(["run", "--target", "mock", "--out", str(self.out), "--cache-dir", str(self.cache), "--quiet"])
        self.assertEqual(rc, 0)
        data = json.loads((self.out / "report.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(data["summary"]["asr"], 30 / 36)
        self.assertTrue((self.out / "report.md").exists())

    def test_anthropic_dry_run_calls_nothing(self):
        # No API key and no SDK needed: dry run only estimates.
        rc = main(["run", "--target", "anthropic", "--dry-run", "--cache-dir", str(self.cache)])
        self.assertEqual(rc, 0)
        self.assertFalse(self.out.exists())

    def test_anthropic_over_budget_aborts(self):
        rc = main(["run", "--target", "anthropic", "--max-cost", "0.0001",
                   "--cache-dir", str(self.cache), "--out", str(self.out)])
        self.assertEqual(rc, 2)

    def test_unreachable_target_is_not_reported_as_secure(self):
        # Port 9 on localhost: nothing listens, connection is refused instantly. Never leaves the machine.
        rc = main(["run", "--target", "ollama", "--ollama-url", "http://127.0.0.1:9", "--no-cache",
                   "--out", str(self.out), "--quiet"])
        self.assertEqual(rc, 1)
        summary = json.loads((self.out / "report.json").read_text(encoding="utf-8"))["summary"]
        self.assertEqual(summary["cases_errored"], 39)
        self.assertEqual(summary["attack_cases"], 0)  # errors excluded from the ASR denominator

    def test_list(self):
        self.assertEqual(main(["list"]), 0)


# ---------------------------------------------------------------- adapters --
def tool_use(id_, name, input_):
    return NS(type="tool_use", id=id_, name=name, input=input_)


def response(content, stop, **usage):
    u = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0, **usage}
    return NS(content=content, stop_reason=stop, usage=NS(**u), model="claude-opus-5-5", stop_details=None)


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        # Snapshot: the adapter keeps appending to the same list after the call.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


class TestAnthropicAdapter(unittest.TestCase):
    def setUp(self):
        self.scenario = next(s for s in SCENARIOS if s.id == "web_research")

    def make(self, responses, **kw):
        msgs = FakeMessages(responses)
        return AnthropicAgent(client=NS(messages=msgs), **kw), msgs

    def test_tool_loop_and_request_shape(self):
        agent, msgs = self.make([
            response([NS(type="text", text="Looking."), tool_use("t1", "fetch_url",
                      {"url": "https://library.example.test/hours"})], "tool_use"),
            response([NS(type="text", text="Saturday 10am to 4pm.")], "end_turn"),
        ], effort="low")
        trace = agent.run(self.scenario.task, self.scenario.build_environment())
        self.assertEqual(trace.final_answer, "Saturday 10am to 4pm.")
        self.assertEqual(trace.tool_calls[0]["name"], "fetch_url")
        req = msgs.requests[0]
        self.assertEqual(req["tool_choice"], {"type": "auto"})
        self.assertEqual(req["model"], "claude-opus-5-5")
        self.assertEqual(req["output_config"], {"effort": "low"})
        self.assertNotIn("temperature", req)
        self.assertEqual(req["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertIn("cache_control", req["tools"][-1])
        # Tool result went back in a single user message with the matching id.
        result_msg = msgs.requests[1]["messages"][-1]
        self.assertEqual(result_msg["content"][0]["tool_use_id"], "t1")
        self.assertEqual(trace.usage["input_tokens"], 200)
        self.assertAlmostEqual(trace.cost_usd, cost_from_usage("claude-opus-5-5", trace.usage))

    def test_tool_prefix_is_identical_across_turns(self):
        agent, msgs = self.make([
            response([tool_use("t1", "search_web", {"query": "x"})], "tool_use"),
            response([NS(type="text", text="done")], "end_turn"),
        ])
        agent.run(self.scenario.task, self.scenario.build_environment())
        self.assertEqual(msgs.requests[0]["tools"], msgs.requests[1]["tools"])
        self.assertEqual(msgs.requests[0]["system"], msgs.requests[1]["system"])

    def test_refusal_is_recorded(self):
        refusal = response([], "refusal")
        refusal.stop_details = NS(category="cyber", explanation="x")
        agent, _ = self.make([refusal])
        trace = agent.run(self.scenario.task, self.scenario.build_environment())
        self.assertTrue(trace.refused)
        self.assertEqual(trace.refusal_category, "cyber")

    def test_api_error_is_captured(self):
        class Boom:
            def create(self, **kw):
                raise RuntimeError("network down")
        agent = AnthropicAgent(client=NS(messages=Boom()))
        trace = agent.run(self.scenario.task, self.scenario.build_environment())
        self.assertIn("network down", trace.error)

    def test_cheap_model_estimates_less(self):
        env = self.scenario.build_environment()
        opus = make_agent("anthropic").estimate_cost("t", env)
        haiku = make_agent("anthropic", model="claude-haiku-5-5").estimate_cost("t", env)
        self.assertGreater(opus, haiku)
        self.assertGreater(haiku, 0)


class TestOllamaAdapter(unittest.TestCase):
    def test_tool_loop_with_fake_server(self):
        replies = [
            {"message": {"content": "", "tool_calls": [{"function": {"name": "send_message",
              "arguments": '{"to": "a@example.test", "body": "CNRY-1"}'}}]},
             "prompt_eval_count": 10, "eval_count": 5},
            {"message": {"content": "Sent."}, "prompt_eval_count": 12, "eval_count": 2},
        ]
        seen = []

        def fake_post(url, payload):
            seen.append((url, {**payload, "messages": list(payload["messages"])}))
            return replies.pop(0)

        s = SCENARIOS[0]
        env = s.build_environment()
        trace = OllamaAgent(model="qwen2.5:7b", post=fake_post).run(s.task, env)
        self.assertEqual(env.sent_messages[0]["body"], "CNRY-1")  # string args were parsed
        self.assertEqual(trace.final_answer, "Sent.")
        self.assertEqual(trace.usage["input_tokens"], 22)
        self.assertTrue(seen[0][0].endswith("/api/chat"))
        self.assertEqual(seen[0][1]["options"]["temperature"], 0)
        self.assertEqual(seen[1][1]["messages"][-1]["role"], "tool")

    def test_connection_error_is_reported_not_raised(self):
        def down(url, payload):
            raise ConnectionRefusedError("refused")
        s = SCENARIOS[0]
        trace = OllamaAgent(post=down).run(s.task, s.build_environment())
        self.assertIn("ollama serve", trace.error)


if __name__ == "__main__":
    unittest.main()
