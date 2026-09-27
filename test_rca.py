#!/usr/bin/env python3
"""Offline tests for the RCA experiment: fake model, fake Jev, no network.

    python3 -m unittest test_rca -v
"""
import io
import json
import unittest

import rca
import triage

SCENARIO, TRUTH = rca.load_scenario("pool_exhaustion_v1")
HYPS = list(SCENARIO["hypotheses"])


def beliefs(top, p=0.7):
    rest = (1 - p) / (len(HYPS) - 1)
    return {h: (p if h == top else rest) for h in HYPS}


def reply(top, next_check=None, final=None, p=0.7):
    return json.dumps({"beliefs": beliefs(top, p), "reasoning": "r",
                       "next_check": next_check, "final": final})


GOOD_FINAL = {"hypothesis": "slow_queries", "component": "orders-db",
              "mechanism": "The orders-archive batch job holds row locks, so queries wait.",
              "evidence": ["errors_by_version", "db_lock_waits", "batch_jobs"]}


class ScriptedLLM:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, system, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def fake_judge(scenario, observed):
    """Deploy leads until the version check is seen, then slow_queries leads."""
    top = "slow_queries" if "errors_by_version" in observed else "deploy"
    contradicted = {h: 0.1 for h in HYPS}
    if "errors_by_version" in observed:
        contradicted["deploy"] = 0.9
    return {"best": beliefs(top), "contradicted": contradicted}, 120.0


class ScenarioTests(unittest.TestCase):
    def test_truth_refers_to_real_checks_and_hypotheses(self):
        checks = set(SCENARIO["checks"])
        self.assertIn(TRUTH["hypothesis"], SCENARIO["hypotheses"])
        self.assertIn(TRUTH["decoy"], SCENARIO["hypotheses"])
        self.assertIn(TRUTH["version_check"], checks)
        for group in (TRUTH["mechanism_checks"], TRUTH["noise"], list(TRUTH["traps"])):
            self.assertTrue(set(group) <= checks, group)
        for ids in TRUTH["contradicts"].values():
            self.assertTrue(set(ids) <= checks, ids)

    def test_agent_input_never_contains_the_truth(self):
        prompt = rca.render_state(SCENARIO, list(SCENARIO["checks"]))
        payload = json.dumps(rca.build_judge_payload(SCENARIO, list(SCENARIO["checks"])))
        for text in (prompt, payload, rca.SYSTEM):
            self.assertNotIn("orders-archive batch job, manually triggered", text)
            self.assertNotIn("decoy", text.lower())


class ParseTests(unittest.TestCase):
    observed = ["deploys"]

    def test_accepts_fenced_json_and_normalizes_beliefs(self):
        text = "```json\n" + json.dumps({"beliefs": {"deploy": 2, "leak": 2}, "next_check": "pool_config",
                                          "final": None}) + "\n```"
        with self.assertRaises(rca.AgentError):  # 2 is out of range
            rca.parse_reply(text, SCENARIO, self.observed)
        text = text.replace("2", "0.5")
        r = rca.parse_reply(text, SCENARIO, self.observed)
        self.assertAlmostEqual(r["beliefs"]["deploy"], 0.5)
        self.assertEqual(r["beliefs"]["traffic"], 0.0)

    def test_rejects_bad_replies(self):
        bad = [
            "no json here",
            reply("deploy"),                                   # neither next_check nor final
            reply("deploy", "not_a_check"),
            reply("deploy", "deploys"),                        # already observed
            reply("deploy", "pool_config", GOOD_FINAL),        # both set
            reply("deploy", final={**GOOD_FINAL, "hypothesis": "aliens"}),
        ]
        for text in bad:
            with self.assertRaises(rca.AgentError, msg=text):
                rca.parse_reply(text, SCENARIO, self.observed)

    def test_must_finish_rejects_another_check(self):
        with self.assertRaises(rca.AgentError):
            rca.parse_reply(reply("deploy", "pool_config"), SCENARIO, self.observed, must_finish=True)


class JudgeTests(unittest.TestCase):
    def response(self, best, contradicted):
        answers = {"best_explanation": {"type": "choice", "probabilities": best}}
        for h, p in contradicted.items():
            answers[f"contradicted_{h}"] = {"type": "noul", "noul": p}
        return {"model": triage.MODEL, "answers": answers}

    def test_payload_asks_one_choice_and_one_noul_per_hypothesis(self):
        p = rca.build_judge_payload(SCENARIO, ["deploys", "errors_by_version"])
        self.assertEqual(p["questions"]["best_explanation"]["type"], "choice")
        self.assertEqual(set(p["questions"]["best_explanation"]["criteria"]), set(HYPS))
        self.assertEqual(sum(q["type"] == "noul" for q in p["questions"].values()), len(HYPS))
        self.assertEqual([o["check"] for o in p["state"]["observations"]], ["deploys", "errors_by_version"])

    def test_parses_and_validates(self):
        ok = self.response(beliefs("slow_queries"), {h: 0.2 for h in HYPS})
        scores = rca.parse_judge(ok, SCENARIO)
        self.assertEqual(rca._top(scores["best"]), "slow_queries")
        missing = self.response(beliefs("deploy"), {"deploy": 0.5})
        with self.assertRaises(triage.JevError):
            rca.parse_judge(missing, SCENARIO)
        bad_sum = self.response({"deploy": 0.2}, {h: 0.2 for h in HYPS})
        with self.assertRaises(triage.JevError):
            rca.parse_judge(bad_sum, SCENARIO)


class TrialTests(unittest.TestCase):
    def test_baseline_that_changes_direction(self):
        llm = ScriptedLLM([
            reply("deploy", "errors_by_version"),
            reply("slow_queries", "db_lock_waits"),
            reply("slow_queries", "batch_jobs"),
            reply("slow_queries", final=GOOD_FINAL),
        ])
        trial = rca.run_trial(SCENARIO, "baseline", llm)
        s = rca.score_trial(trial, TRUTH)
        self.assertEqual(trial["status"], "ok")
        self.assertTrue(s["decoy_top_first"])
        self.assertTrue(s["redirected"])
        self.assertGreater(s["p_decoy_drop"], 0.5)
        self.assertTrue(s["explained"] and s["localized"] and s["grounded"])
        self.assertFalse(s["blamed_decoy"] or s["cited_trap"])
        self.assertEqual(s["checks_used"], 3)

    def test_baseline_that_sticks_with_the_decoy(self):
        llm = ScriptedLLM([
            reply("deploy", "errors_by_version"),
            reply("deploy", "orders_db_logs"),
            reply("deploy", final={"hypothesis": "deploy", "component": "checkout-api",
                                   "mechanism": "The new loyalty query is slow.",
                                   "evidence": ["deploys", "orders_db_logs"]}),
        ])
        s = rca.score_trial(rca.run_trial(SCENARIO, "baseline", llm), TRUTH)
        self.assertFalse(s["redirected"])
        self.assertTrue(s["blamed_decoy"] and s["cited_trap"])
        self.assertFalse(s["explained"])

    def test_jev_setup_measures_jev_beliefs_and_shows_scores(self):
        # The model's own beliefs never move; Jev's do. The jev setup is scored on Jev's.
        llm = ScriptedLLM([
            reply("deploy", "errors_by_version"),
            reply("deploy", "db_lock_waits"),
            reply("deploy", final=GOOD_FINAL),
        ])
        trial = rca.run_trial(SCENARIO, "jev", llm, fake_judge)
        s = rca.score_trial(trial, TRUTH)
        self.assertTrue(s["redirected"])
        self.assertIn("Evidence scores from a separate judge", llm.prompts[1])
        self.assertEqual(trial["steps"][1]["jev_ms"], 120.0)

    def test_force_version_check_overrides_first_choice(self):
        llm = ScriptedLLM([reply("deploy", "search_cache"), reply("slow_queries", final=GOOD_FINAL)])
        trial = rca.run_trial(SCENARIO, "baseline", llm, force_version_check="errors_by_version")
        self.assertEqual(trial["steps"][0]["forced"], "errors_by_version")
        self.assertEqual(trial["observed"], ["deploys", "errors_by_version"])

    def test_invalid_twice_ends_the_trial(self):
        llm = ScriptedLLM(["nope", "still nope"])
        trial = rca.run_trial(SCENARIO, "baseline", llm)
        self.assertTrue(trial["status"].startswith("invalid_reply"))
        self.assertFalse(rca.score_trial(trial, TRUTH)["valid"])

    def test_retry_after_one_bad_reply(self):
        llm = ScriptedLLM(["nope", reply("slow_queries", final=GOOD_FINAL)])
        trial = rca.run_trial(SCENARIO, "baseline", llm)
        self.assertEqual(trial["status"], "ok")
        self.assertIn("was rejected", llm.prompts[1])

    def test_jev_error_ends_the_trial(self):
        def broken(scenario, observed):
            raise triage.JevError("HTTP 500")
        trial = rca.run_trial(SCENARIO, "jev", ScriptedLLM([]), broken)
        self.assertTrue(trial["status"].startswith("jev_error"))

    def test_budget_forces_a_final_answer(self):
        llm = ScriptedLLM([reply("deploy", "pool_config"), reply("deploy", "request_rate"),
                           reply("slow_queries", final=GOOD_FINAL)])
        trial = rca.run_trial(SCENARIO, "baseline", llm, max_checks=2)
        self.assertIn("No checks left", llm.prompts[2])
        self.assertEqual(trial["status"], "ok")

    def test_report_prints_both_setups(self):
        rows = [rca.run_trial(SCENARIO, "baseline", ScriptedLLM([reply("deploy", final=GOOD_FINAL)])),
                rca.run_trial(SCENARIO, "jev", ScriptedLLM([reply("deploy", final=GOOD_FINAL)]), fake_judge)]
        buf = io.StringIO()
        rca.report(rows, {SCENARIO["id"]: TRUTH}, out=buf)
        self.assertIn("| baseline |", buf.getvalue())
        self.assertIn("| jev |", buf.getvalue())
        self.assertIn("changed direction after it", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
