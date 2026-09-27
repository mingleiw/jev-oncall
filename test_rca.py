#!/usr/bin/env python3
"""Offline tests for the RCA experiment: parsing, trial flow, scoring,
forced mode, error handling, and the ground-truth leak test.

No API key, no network.

    python3 -m unittest test_rca -v
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import rca_experiment
import triage

BASE = os.path.dirname(os.path.abspath(__file__))
SCENARIO = rca_experiment.load_scenario()
GROUND_TRUTH = rca_experiment.load_ground_truth()


# --------------------------------------------------------------------------
# Helpers

def fake_model_reply(beliefs, next_check=None, done=False, final=None):
    """Build a model reply string that the parser can extract."""
    parts = []
    if beliefs:
        parts.append(f'```json\n{{"beliefs": {json.dumps(beliefs)}}}\n```')
    if done and final:
        parts.append(f'DONE\n```json\n{json.dumps(final)}\n```')
    elif next_check:
        parts.append(f"NEXT: `{next_check}`")
    return "\n".join(parts)


def fake_jev_response(hypothesis_ids, top_id="deploy", contra_id="none"):
    """Build a Jev-shaped response for RCA scoring."""
    n = len(hypothesis_ids)
    probs = {h: 0.05 for h in hypothesis_ids}
    probs[top_id] = 1.0 - 0.05 * (n - 1)
    contra_probs = {h: 0.02 for h in hypothesis_ids}
    contra_probs["none"] = 1.0 - 0.02 * n
    if contra_id != "none":
        contra_probs[contra_id] = contra_probs["none"]
        contra_probs["none"] = 0.02
    return {
        "model": triage.MODEL,
        "answers": {
            "best_explanation": {"type": "choice", "probabilities": probs},
            "contradicted": {"type": "choice", "probabilities": contra_probs},
        },
        "usage": {"input_tokens": 500, "output_tokens": 80},
    }


class ScriptedModel:
    """A fake model that returns scripted replies in sequence."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, system=None, **kwargs):
        self.calls.append({"messages": messages, "system": system})
        if not self.replies:
            return fake_model_reply(None, done=True, final={
                "hypothesis": "etl-cron", "component": "analytics-etl",
                "mechanism": "fallback", "evidence": []})
        return self.replies.pop(0)


# --------------------------------------------------------------------------
# Parsing tests

class TestParseBeliefs(unittest.TestCase):
    def test_parses_beliefs_from_json_block(self):
        text = 'Looking at the data:\n```json\n{"beliefs": {"deploy": 0.6, "etl-cron": 0.2, "conn-leak": 0.1, "db-config": 0.05, "traffic": 0.03, "dns": 0.02}}\n```\nNEXT: `pool-metrics`'
        beliefs = rca_experiment.parse_beliefs(text)
        self.assertIsNotNone(beliefs)
        self.assertAlmostEqual(beliefs["deploy"], 0.6)

    def test_parses_beliefs_inline(self):
        text = 'My assessment: {"deploy": 0.5, "etl-cron": 0.3, "conn-leak": 0.2}\nNEXT: `pool-metrics`'
        beliefs = rca_experiment.parse_beliefs(text)
        self.assertIsNotNone(beliefs)
        self.assertEqual(len(beliefs), 3)

    def test_returns_none_on_garbage(self):
        self.assertIsNone(rca_experiment.parse_beliefs("I'm not sure what to do"))

    def test_rejects_non_numeric_values(self):
        text = '{"deploy": "high", "etl-cron": "low"}'
        beliefs = rca_experiment.parse_beliefs(text)
        self.assertIsNone(beliefs)


class TestParseNextCheck(unittest.TestCase):
    def test_parses_next_check(self):
        text = "Based on this, I'll check the pool metrics.\nNEXT: `pool-metrics`"
        check = rca_experiment.parse_next_check(text, {"pool-metrics", "active-queries"})
        self.assertEqual(check, "pool-metrics")

    def test_returns_none_for_invalid_check(self):
        text = "NEXT: `nonexistent-check`"
        check = rca_experiment.parse_next_check(text, {"pool-metrics"})
        self.assertIsNone(check)

    def test_finds_check_name_in_backticks(self):
        text = "Let me look at `pool-metrics` next."
        check = rca_experiment.parse_next_check(text, {"pool-metrics", "active-queries"})
        self.assertEqual(check, "pool-metrics")

    def test_ignores_check_after_done(self):
        text = "NEXT: `pool-metrics`\nDONE\nI mentioned `active-queries` earlier"
        check = rca_experiment.parse_next_check(text, {"pool-metrics", "active-queries"})
        self.assertEqual(check, "pool-metrics")


class TestParseFinalAnswer(unittest.TestCase):
    def test_parses_final_answer(self):
        text = ('DONE\n```json\n{"hypothesis": "etl-cron", "component": "analytics-etl", '
                '"mechanism": "cron rescheduled", "evidence": ["pool-metrics"]}\n```')
        answer = rca_experiment.parse_final_answer(text)
        self.assertEqual(answer["hypothesis"], "etl-cron")

    def test_returns_none_without_done(self):
        text = '```json\n{"hypothesis": "deploy"}\n```'
        self.assertIsNone(rca_experiment.parse_final_answer(text))

    def test_parses_inline_json_after_done(self):
        text = 'DONE {"hypothesis": "etl-cron", "component": "etl", "mechanism": "x", "evidence": []}'
        answer = rca_experiment.parse_final_answer(text)
        self.assertEqual(answer["hypothesis"], "etl-cron")


# --------------------------------------------------------------------------
# Jev payload and parsing

class TestJevPayload(unittest.TestCase):
    def test_payload_has_all_hypotheses(self):
        evidence = [{"check": "pool-metrics", "result": "some data"}]
        payload = rca_experiment.build_jev_payload(SCENARIO, evidence)
        criteria = payload["questions"]["best_explanation"]["criteria"]
        for hid in SCENARIO["hypotheses"]:
            self.assertIn(hid, criteria)

    def test_contradicted_question_includes_none(self):
        payload = rca_experiment.build_jev_payload(SCENARIO, [])
        criteria = payload["questions"]["contradicted"]["criteria"]
        self.assertIn("none", criteria)

    def test_evidence_appears_in_state(self):
        evidence = [{"check": "pool-metrics", "result": "pool at 100%"}]
        payload = rca_experiment.build_jev_payload(SCENARIO, evidence)
        self.assertIn("pool at 100%", payload["state"]["incident_rca"])

    def test_parse_jev_rca_response(self):
        hypothesis_ids = list(SCENARIO["hypotheses"].keys())
        resp = fake_jev_response(hypothesis_ids, top_id="etl-cron")
        scores = rca_experiment.parse_jev_rca(resp, hypothesis_ids)
        self.assertIn("beliefs", scores)
        self.assertEqual(max(scores["beliefs"], key=scores["beliefs"].get), "etl-cron")


# --------------------------------------------------------------------------
# Scoring

class TestScoring(unittest.TestCase):
    def _trace(self, setup="baseline", steps=None, final=None, forced=False):
        steps = steps or []
        checks_used = [s["check"] for s in steps]
        return {
            "setup": setup,
            "forced": forced,
            "steps": steps,
            "final_answer": final,
            "checks_used": checks_used,
        }

    def test_correct_answer_scores_all_true(self):
        final = {"hypothesis": "etl-cron", "component": "analytics-etl",
                 "mechanism": "analytics ETL cron rescheduled to peak hours, holding connections",
                 "evidence": ["pool-metrics", "active-queries", "cron-history"]}
        steps = [
            {"check": "deployment-log", "beliefs": {"deploy": 0.6, "etl-cron": 0.2, "conn-leak": 0.1, "db-config": 0.05, "traffic": 0.03, "dns": 0.02}, "jev_scores": None},
            {"check": "error-by-version", "beliefs": {"deploy": 0.2, "etl-cron": 0.4, "conn-leak": 0.2, "db-config": 0.1, "traffic": 0.05, "dns": 0.05}, "jev_scores": None},
            {"check": "pool-metrics", "beliefs": None, "jev_scores": None},
            {"check": "active-queries", "beliefs": None, "jev_scores": None},
            {"check": "cron-history", "beliefs": None, "jev_scores": None},
        ]
        trace = self._trace(steps=steps, final=final)
        score = rca_experiment.score_trial(trace, GROUND_TRUTH, SCENARIO)
        self.assertTrue(score["correct_hypothesis"])
        self.assertTrue(score["correct_mechanism"])
        self.assertTrue(score["correct_component"])
        self.assertFalse(score["blamed_decoy"])
        self.assertTrue(score["all_evidence_observed"])
        self.assertFalse(score["cited_trap"])

    def test_decoy_blame_detected(self):
        final = {"hypothesis": "deploy", "component": "checkout-api",
                 "mechanism": "deploy changed timeouts", "evidence": ["deployment-log"]}
        steps = [{"check": "deployment-log", "beliefs": {"deploy": 0.8}, "jev_scores": None}]
        score = rca_experiment.score_trial(self._trace(steps=steps, final=final),
                                           GROUND_TRUTH, SCENARIO)
        self.assertTrue(score["blamed_decoy"])
        self.assertFalse(score["correct_hypothesis"])

    def test_direction_change_detected(self):
        steps = [
            {"check": "deployment-log", "beliefs": {"deploy": 0.7, "etl-cron": 0.1, "conn-leak": 0.1, "db-config": 0.05, "traffic": 0.03, "dns": 0.02}, "jev_scores": None},
            {"check": "error-by-version", "beliefs": {"deploy": 0.15, "etl-cron": 0.4, "conn-leak": 0.2, "db-config": 0.1, "traffic": 0.1, "dns": 0.05}, "jev_scores": None},
        ]
        final = {"hypothesis": "etl-cron", "component": "analytics-etl",
                 "mechanism": "cron job rescheduled to peak, holding connections",
                 "evidence": ["error-by-version"]}
        score = rca_experiment.score_trial(self._trace(steps=steps, final=final),
                                           GROUND_TRUTH, SCENARIO)
        self.assertTrue(score["changed_direction"])
        self.assertGreater(score["p_deploy_drop"], 0.4)

    def test_no_direction_change_when_agent_sticks(self):
        steps = [
            {"check": "deployment-log", "beliefs": {"deploy": 0.7, "etl-cron": 0.1, "conn-leak": 0.1, "db-config": 0.05, "traffic": 0.03, "dns": 0.02}, "jev_scores": None},
            {"check": "error-by-version", "beliefs": {"deploy": 0.5, "etl-cron": 0.2, "conn-leak": 0.15, "db-config": 0.05, "traffic": 0.05, "dns": 0.05}, "jev_scores": None},
        ]
        final = {"hypothesis": "deploy", "component": "checkout-api",
                 "mechanism": "deploy caused issues", "evidence": ["deployment-log"]}
        score = rca_experiment.score_trial(self._trace(steps=steps, final=final),
                                           GROUND_TRUTH, SCENARIO)
        self.assertFalse(score["changed_direction"])

    def test_jev_beliefs_used_for_scoring_in_jev_mode(self):
        jev_before = {"beliefs": {"deploy": 0.7, "etl-cron": 0.1, "conn-leak": 0.1,
                                  "db-config": 0.05, "traffic": 0.03, "dns": 0.02},
                      "contradicted": {}}
        jev_after = {"beliefs": {"deploy": 0.1, "etl-cron": 0.5, "conn-leak": 0.2,
                                 "db-config": 0.1, "traffic": 0.05, "dns": 0.05},
                     "contradicted": {"deploy": 0.8, "none": 0.2}}
        steps = [
            {"check": "deployment-log", "beliefs": {"deploy": 0.7}, "jev_scores": jev_before},
            {"check": "error-by-version", "beliefs": {"deploy": 0.5}, "jev_scores": jev_after},
        ]
        final = {"hypothesis": "etl-cron", "component": "analytics-etl",
                 "mechanism": "cron rescheduled", "evidence": ["error-by-version"]}
        score = rca_experiment.score_trial(self._trace(setup="jev", steps=steps, final=final),
                                           GROUND_TRUTH, SCENARIO)
        self.assertTrue(score["changed_direction"])
        self.assertAlmostEqual(score["p_deploy_drop"], 0.6, places=1)

    def test_unobserved_evidence_flagged(self):
        final = {"hypothesis": "etl-cron", "component": "analytics-etl",
                 "mechanism": "cron rescheduled", "evidence": ["pool-metrics", "never-ran"]}
        steps = [{"check": "pool-metrics", "beliefs": None, "jev_scores": None}]
        score = rca_experiment.score_trial(self._trace(steps=steps, final=final),
                                           GROUND_TRUTH, SCENARIO)
        self.assertFalse(score["all_evidence_observed"])

    def test_trap_citation_detected(self):
        final = {"hypothesis": "etl-cron", "component": "analytics-etl",
                 "mechanism": "cron rescheduled", "evidence": ["slow-query-log"]}
        steps = [{"check": "slow-query-log", "beliefs": None, "jev_scores": None}]
        score = rca_experiment.score_trial(self._trace(steps=steps, final=final),
                                           GROUND_TRUTH, SCENARIO)
        self.assertTrue(score["cited_trap"])

    def test_noise_checks_counted(self):
        steps = [
            {"check": "cdn-cache", "beliefs": None, "jev_scores": None},
            {"check": "cert-expiry", "beliefs": None, "jev_scores": None},
            {"check": "pool-metrics", "beliefs": None, "jev_scores": None},
        ]
        final = {"hypothesis": "etl-cron", "component": "analytics-etl",
                 "mechanism": "cron rescheduled", "evidence": ["pool-metrics"]}
        score = rca_experiment.score_trial(self._trace(steps=steps, final=final),
                                           GROUND_TRUTH, SCENARIO)
        self.assertEqual(score["num_noise"], 2)
        self.assertIn("cdn-cache", score["noise_checks"])

    def test_missing_final_answer_scores_none(self):
        score = rca_experiment.score_trial(self._trace(), GROUND_TRUTH, SCENARIO)
        self.assertIsNone(score["correct_hypothesis"])
        self.assertIsNone(score["blamed_decoy"])


# --------------------------------------------------------------------------
# Trial flow with scripted model

class TestTrialFlow(unittest.TestCase):
    def _run_with_replies(self, replies, setup="baseline", forced=False):
        model = ScriptedModel(replies)
        with mock.patch.object(rca_experiment, "call_model", side_effect=model):
            return rca_experiment.run_trial(
                SCENARIO, setup, forced=forced, max_checks=5,
                api_key="fake", jev_api_key="fake")

    def test_baseline_trial_runs_checks_in_order(self):
        replies = [
            fake_model_reply({"deploy": 0.6, "etl-cron": 0.2, "conn-leak": 0.1,
                              "db-config": 0.05, "traffic": 0.03, "dns": 0.02},
                             next_check="pool-metrics"),
            fake_model_reply({"deploy": 0.3, "etl-cron": 0.5, "conn-leak": 0.1,
                              "db-config": 0.05, "traffic": 0.03, "dns": 0.02},
                             next_check="active-queries"),
            fake_model_reply(None, done=True, final={
                "hypothesis": "etl-cron", "component": "analytics-etl",
                "mechanism": "cron job rescheduled to peak, holding pool connections",
                "evidence": ["pool-metrics", "active-queries"]}),
        ]
        trace = self._run_with_replies(replies)
        self.assertEqual(trace["checks_used"], ["pool-metrics", "active-queries"])
        self.assertEqual(trace["final_answer"]["hypothesis"], "etl-cron")

    def test_forced_mode_starts_with_version_check(self):
        replies = [
            fake_model_reply({"deploy": 0.2, "etl-cron": 0.4, "conn-leak": 0.2,
                              "db-config": 0.1, "traffic": 0.05, "dns": 0.05},
                             next_check="pool-metrics"),
            fake_model_reply(None, done=True, final={
                "hypothesis": "etl-cron", "component": "analytics-etl",
                "mechanism": "cron rescheduled", "evidence": ["error-by-version", "pool-metrics"]}),
        ]
        trace = self._run_with_replies(replies, forced=True)
        self.assertEqual(trace["steps"][0]["check"], "error-by-version")
        self.assertIn("error-by-version", trace["checks_used"])

    def test_jev_mode_includes_jev_scores(self):
        hypothesis_ids = list(SCENARIO["hypotheses"].keys())
        jev_resp = fake_jev_response(hypothesis_ids, top_id="etl-cron", contra_id="deploy")

        replies = [
            fake_model_reply({"deploy": 0.3, "etl-cron": 0.4, "conn-leak": 0.15,
                              "db-config": 0.05, "traffic": 0.05, "dns": 0.05},
                             next_check="pool-metrics"),
            fake_model_reply(None, done=True, final={
                "hypothesis": "etl-cron", "component": "analytics-etl",
                "mechanism": "cron rescheduled", "evidence": ["pool-metrics"]}),
        ]
        model = ScriptedModel(replies)
        with mock.patch.object(rca_experiment, "call_model", side_effect=model), \
             mock.patch.object(triage, "call_jev", return_value=(jev_resp, 50.0)):
            trace = rca_experiment.run_trial(
                SCENARIO, "jev", api_key="fake", jev_api_key="fake", max_checks=5)

        self.assertEqual(trace["setup"], "jev")
        jev_step = trace["steps"][0]
        self.assertIn("beliefs", jev_step["jev_scores"])

    def test_model_error_recorded(self):
        def fail(*a, **kw):
            raise RuntimeError("connection refused")
        with mock.patch.object(rca_experiment, "call_model", side_effect=fail):
            trace = rca_experiment.run_trial(
                SCENARIO, "baseline", api_key="fake", max_checks=5)
        self.assertIn("model call failed", trace["error"])

    def test_check_not_reused(self):
        replies = [
            fake_model_reply({"deploy": 0.6, "etl-cron": 0.2, "conn-leak": 0.1,
                              "db-config": 0.05, "traffic": 0.03, "dns": 0.02},
                             next_check="pool-metrics"),
            fake_model_reply({"deploy": 0.3, "etl-cron": 0.5, "conn-leak": 0.1,
                              "db-config": 0.05, "traffic": 0.03, "dns": 0.02},
                             next_check="pool-metrics"),  # repeat!
            fake_model_reply(None, done=True, final={
                "hypothesis": "etl-cron", "component": "analytics-etl",
                "mechanism": "cron rescheduled", "evidence": ["pool-metrics"]}),
        ]
        trace = self._run_with_replies(replies)
        # pool-metrics should appear only once
        self.assertEqual(trace["checks_used"].count("pool-metrics"), 1)


# --------------------------------------------------------------------------
# Dry run

class TestDryRun(unittest.TestCase):
    def test_dry_run_produces_prompts_without_api_calls(self):
        trace = rca_experiment.run_trial(SCENARIO, "baseline", dry_run=True)
        self.assertTrue(trace["dry_run"])
        self.assertIn("prompt_system", trace)
        self.assertIn("prompt_initial", trace)
        self.assertIn("Incident", trace["prompt_initial"])

    def test_dry_run_jev_includes_payload(self):
        trace = rca_experiment.run_trial(SCENARIO, "jev", dry_run=True)
        self.assertIn("jev_payload", trace)
        self.assertIn("best_explanation", trace["jev_payload"]["questions"])

    def test_dry_run_forced_includes_version_check(self):
        trace = rca_experiment.run_trial(SCENARIO, "baseline", dry_run=True, forced=True)
        self.assertIn("error-by-version", trace["prompt_initial"])


# --------------------------------------------------------------------------
# Ground truth leak test

class TestGroundTruthLeak(unittest.TestCase):
    """The ground truth must not appear in any agent input."""

    def test_scenario_does_not_contain_ground_truth_answer(self):
        gt = GROUND_TRUTH
        scenario_text = json.dumps(SCENARIO)

        # The ground truth hypothesis name appears in the scenario (it's a hypothesis),
        # but the mechanism text should not.
        mechanism = gt["mechanism"]
        self.assertNotIn(mechanism, scenario_text,
                         "Ground truth mechanism text appears verbatim in scenario")

    def test_system_prompt_does_not_leak_answer(self):
        prompt = rca_experiment.SYSTEM_PROMPT
        gt = GROUND_TRUTH
        self.assertNotIn(gt["hypothesis"], prompt)
        self.assertNotIn(gt["component"], prompt)

    def test_initial_prompt_does_not_leak_answer(self):
        prompt = rca_experiment.build_initial_prompt(SCENARIO)
        gt = GROUND_TRUTH
        self.assertNotIn(gt["mechanism"], prompt)

    def test_no_check_result_contains_ground_truth_mechanism(self):
        gt = GROUND_TRUTH
        mechanism = gt["mechanism"]
        for name, check in SCENARIO["checks"].items():
            self.assertNotIn(mechanism, check["result"],
                             f"Check {name} contains the ground truth mechanism")

    def test_ground_truth_file_is_separate(self):
        """Ground truth is not embedded in the scenario file."""
        scenario_raw = json.dumps(SCENARIO)
        self.assertNotIn("ground_truth", scenario_raw.lower().replace("_", "").replace("-", ""))
        for check in SCENARIO["checks"].values():
            self.assertNotIn("etl-cron", check["result"],
                             "A check result names the correct hypothesis")


# --------------------------------------------------------------------------
# Report

class TestReport(unittest.TestCase):
    def test_report_does_not_crash_on_empty(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rca_experiment.print_report([])
        self.assertIn("RCA Experiment Report", buf.getvalue())

    def test_report_counts_are_fractions(self):
        import io
        from contextlib import redirect_stdout
        scores = [
            {"setup": "baseline", "forced": False, "decoy_led_initially": True,
             "ran_version_check": True, "changed_direction": True, "p_deploy_drop": 0.5,
             "correct_hypothesis": True, "correct_mechanism": True, "correct_component": True,
             "blamed_decoy": False, "all_evidence_observed": True, "cited_trap": False,
             "evidence_cited": [], "checks_used": [], "num_checks": 3, "noise_checks": [],
             "num_noise": 0},
            {"setup": "jev", "forced": False, "decoy_led_initially": True,
             "ran_version_check": True, "changed_direction": True, "p_deploy_drop": 0.6,
             "correct_hypothesis": True, "correct_mechanism": False, "correct_component": True,
             "blamed_decoy": False, "all_evidence_observed": True, "cited_trap": False,
             "evidence_cited": [], "checks_used": [], "num_checks": 4, "noise_checks": [],
             "num_noise": 1},
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            rca_experiment.print_report(scores)
        output = buf.getvalue()
        self.assertIn("1/1", output)


# --------------------------------------------------------------------------
# CLI

class TestCLI(unittest.TestCase):
    def test_report_from_existing_traces(self):
        import io
        from contextlib import redirect_stdout

        trace = {
            "setup": "baseline", "forced": False, "trial": 1,
            "steps": [
                {"check": "deployment-log",
                 "beliefs": {"deploy": 0.6, "etl-cron": 0.2, "conn-leak": 0.1,
                             "db-config": 0.05, "traffic": 0.03, "dns": 0.02},
                 "jev_scores": None},
            ],
            "final_answer": {"hypothesis": "etl-cron", "component": "analytics-etl",
                             "mechanism": "cron rescheduled to peak, holding connections",
                             "evidence": ["deployment-log"]},
            "checks_used": ["deployment-log"],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(trace) + "\n")
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rca_experiment.main(["--report", f.name])
            self.assertIn("Correct hypothesis", buf.getvalue())
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
