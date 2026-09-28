#!/usr/bin/env python3
"""Offline tests for the RCA experiment with a scripted model and a fake Jev:
reply parsing, trial flow, forced mode, scoring, errors, the report, and the
ground-truth leak test. No API key, no network.

    python3 -m unittest test_rca -v
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import rca_experiment as rca
import triage

SCENARIO = rca.load_scenario()
TRUTH = rca.load_ground_truth()
HYPS = list(SCENARIO["hypotheses"])
VCHECK = SCENARIO["version_check"]


def dist(top, p=0.7):
    rest = (1 - p) / (len(HYPS) - 1)
    return {h: (p if h == top else rest) for h in HYPS}


def reply(beliefs=None, next_check=None, final=None):
    parts = []
    if beliefs:
        parts.append("```json\n" + json.dumps({"beliefs": beliefs}) + "\n```")
    if final:
        parts.append("DONE\n```json\n" + json.dumps(final) + "\n```")
    elif next_check:
        parts.append(f"NEXT: `{next_check}`")
    return "\n".join(parts)


RIGHT = {"hypothesis": "batch-job", "component": "analytics-etl",
         "mechanism": "The analytics ETL cron job was rescheduled to peak hours and its long "
                      "queries held most of the pool's connections.",
         "evidence": ["cron-history", "active-queries"]}
DECOY = {"hypothesis": "deploy", "component": "checkout-api",
         "mechanism": "The deploy raised retries and timeouts.", "evidence": ["deployment-log"]}


class Scripted:
    """A fake model: returns scripted replies in order and records what it saw."""

    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def __call__(self, system, messages):
        self.seen.append((system, [dict(m) for m in messages]))
        text = self.replies.pop(0) if self.replies else "I am not sure."
        return text, {"role": "assistant", "content": text}, {"input_tokens": 10, "output_tokens": 5}


def jev_response(top, contradicted=()):
    """A Jev-shaped reply. contradicted names the hypothesis the evidence most
    contradicts (the first given), or none."""
    probs = dist(top, 0.75)
    most = contradicted[0] if contradicted else triage.NONE
    options = [*HYPS, triage.NONE]
    contra = {h: (0.9 if h == most else 0.1 / (len(options) - 1)) for h in options}
    answers = {"best_explanation": {"type": "choice", "choice": top, "probabilities": probs},
               "most_contradicted": {"type": "choice", "choice": most, "probabilities": contra}}
    return {"model": triage.MODEL, "answers": answers, "usage": {"input_tokens": 700}}


class FakeJev:
    """Scores the deploy on top until the version check is observed, then batch-job."""

    def __init__(self):
        self.calls = []

    def __call__(self, scenario, observed):
        self.calls.append(list(observed))
        if VCHECK in observed:
            resp = jev_response("batch-job", contradicted=("deploy",))
        else:
            resp = jev_response("deploy")
        return rca.parse_jev_rca(resp, HYPS)


# --------------------------------------------------------------------------

class ReplyParsing(unittest.TestCase):
    def test_beliefs_are_read_and_normalized(self):
        b = rca.parse_beliefs(reply({"deploy": 6, "batch-job": 2, "traffic": 2}), HYPS)
        self.assertAlmostEqual(b["deploy"], 0.6)
        self.assertEqual(b["dns"], 0.0)
        self.assertAlmostEqual(sum(b.values()), 1.0)

    def test_bare_belief_object_is_accepted(self):
        b = rca.parse_beliefs('Beliefs: {"deploy": 0.6, "batch-job": 0.4}', HYPS)
        self.assertEqual(rca._top(b), "deploy")

    def test_bad_beliefs_are_rejected(self):
        for text in ("no json here",
                     '{"beliefs": {"deploy": "high", "batch-job": "low"}}',
                     '{"beliefs": {"deploy": 0.5, "martians": 0.5}}',  # unknown hypothesis
                     '{"beliefs": {"deploy": 0, "batch-job": 0}}',
                     '{"beliefs": {"deploy": -1, "batch-job": 2}}',
                     '{"beliefs": {"deploy": 0.5'):  # truncated
            self.assertIsNone(rca.parse_beliefs(text, HYPS), text)

    def test_next_check(self):
        avail = ["pool-metrics", "active-queries"]
        self.assertEqual(rca.parse_next_check("NEXT: `pool-metrics`", avail), "pool-metrics")
        self.assertEqual(rca.parse_next_check("**NEXT: active-queries**", avail), "active-queries")
        self.assertIsNone(rca.parse_next_check("NEXT: `made-up`", avail))
        self.assertIsNone(rca.parse_next_check("I would look at `pool-metrics`.", avail))

    def test_final_answer(self):
        self.assertEqual(rca.parse_final_answer(reply(final=RIGHT), HYPS)["hypothesis"], "batch-job")
        self.assertIsNone(rca.parse_final_answer(json.dumps(RIGHT), HYPS))  # no DONE
        bad = dict(RIGHT, hypothesis="aliens")
        self.assertIsNone(rca.parse_final_answer(reply(final=bad), HYPS))
        loose = rca.parse_final_answer('DONE {"hypothesis": "dns", "evidence": "dns-log"}', HYPS)
        self.assertEqual(loose["evidence"], [])  # not a list: nothing counts as cited


class JevPayload(unittest.TestCase):
    def test_every_hypothesis_is_scored_and_checked_for_contradiction(self):
        p = rca.build_jev_payload(SCENARIO, [VCHECK])
        self.assertEqual(set(p["questions"]["best_explanation"]["criteria"]), set(HYPS))
        contra = p["questions"]["most_contradicted"]
        self.assertEqual(contra["type"], "choice")  # one comparison, not a yes/no per hypothesis
        self.assertEqual(set(contra["criteria"]), {*HYPS, triage.NONE})
        self.assertEqual(p["state"]["observations"][0]["result"], SCENARIO["checks"][VCHECK]["result"])
        self.assertEqual(p["model"], triage.MODEL)

    def test_guidance_reaches_both_setups(self):
        # Fairness: what Jev is told about failed queries, the model is told too.
        p = rca.build_jev_payload(SCENARIO, [])
        self.assertEqual(p["state"]["guidance"], SCENARIO["guidance"])
        self.assertIn("not evidence that nothing happened", rca.SYSTEM_PROMPT)
        self.assertIn("malformed", rca.SYSTEM_PROMPT)
        self.assertIn(SCENARIO["initial_context"], json.dumps(p))
        self.assertIn(SCENARIO["initial_context"], rca.build_initial_prompt(SCENARIO))

    def test_malformed_jev_answers_raise(self):
        good = jev_response("deploy")
        self.assertEqual(rca._top(rca.parse_jev_rca(good, HYPS)["beliefs"]), "deploy")
        for mutate in (lambda a: a.pop("most_contradicted"),
                       lambda a: a["most_contradicted"]["probabilities"].update(dns=1.5),
                       lambda a: a["most_contradicted"]["probabilities"].update(dns=float("nan")),
                       lambda a: a["most_contradicted"].update(choice="dns"),  # not the likeliest
                       lambda a: a["best_explanation"]["probabilities"].update(aliens=0.1)):
            resp = json.loads(json.dumps(good))
            mutate(resp["answers"])
            with self.assertRaises(triage.JevError):
                rca.parse_jev_rca(resp, HYPS)

    def test_scorer_reuses_triage_call_jev(self):
        with mock.patch.object(triage, "call_jev", return_value=(jev_response("batch-job"), 42.0)) as call:
            scores = rca.jev_scorer("k")(SCENARIO, ["pool-metrics"])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(rca._top(scores["beliefs"]), "batch-job")
        self.assertEqual(scores["latency_ms"], 42)
        with self.assertRaises(rca.ModelError):
            rca.jev_scorer(None)


class TrialFlow(unittest.TestCase):
    def test_alone_records_a_belief_for_every_state(self):
        model = Scripted(reply(dist("deploy"), "deployment-log"),
                         reply(dist("deploy"), VCHECK),
                         reply(dist("batch-job"), "cron-history"),
                         reply(dist("batch-job"), final=RIGHT))
        t = rca.run_trial(SCENARIO, "alone", model)
        self.assertEqual(t["status"], "ok")
        self.assertEqual(t["observed"], ["deployment-log", VCHECK, "cron-history"])
        self.assertEqual([s["check"] for s in t["states"]], [None, *t["observed"]])
        self.assertTrue(all(s["model_beliefs"] for s in t["states"]))
        self.assertEqual(t["final"]["hypothesis"], "batch-job")
        self.assertEqual(t["usage"]["input_tokens"], 40)

    def test_the_model_sees_each_result_and_never_its_scoring_notes(self):
        model = Scripted(reply(dist("deploy"), VCHECK), reply(dist("batch-job"), final=RIGHT))
        rca.run_trial(SCENARIO, "alone", model)
        last = model.seen[-1][1][-1]["content"]
        self.assertIn(SCENARIO["checks"][VCHECK]["result"], last)
        self.assertNotIn("Jev", last)

    def test_forced_mode_runs_the_version_check_first_and_keeps_the_prior(self):
        model = Scripted(reply(dist("deploy"), "deployment-log"),
                         reply(dist("batch-job"), final=RIGHT))
        t = rca.run_trial(SCENARIO, "alone", model, forced=True)
        self.assertEqual(t["observed"], [VCHECK])
        self.assertEqual(rca._top(t["states"][0]["model_beliefs"]), "deploy")  # before
        self.assertEqual(rca._top(t["states"][1]["model_beliefs"]), "batch-job")  # after
        self.assertIn("in place of your pick", model.seen[1][1][-1]["content"])
        self.assertTrue(rca.score_trial(t, TRUTH)["changed_direction"])

    def test_forced_mode_ignores_an_immediate_done(self):
        model = Scripted(reply(dist("deploy"), final=DECOY), reply(dist("deploy"), final=DECOY))
        t = rca.run_trial(SCENARIO, "alone", model, forced=True)
        self.assertEqual(t["observed"], [VCHECK])

    def test_jev_scores_every_state_and_the_model_sees_them(self):
        jev = FakeJev()
        model = Scripted(reply(dist("deploy"), VCHECK), reply(dist("deploy"), final=DECOY))
        t = rca.run_trial(SCENARIO, "jev", model, jev)
        self.assertEqual(jev.calls, [[], [VCHECK]])
        self.assertIn("Jev's scores", model.seen[0][1][0]["content"])  # starting scores
        self.assertIn("Jev's scores", model.seen[1][1][-1]["content"])
        s = rca.score_trial(t, TRUTH)
        # The model stayed on the deploy even though Jev moved off it.
        self.assertFalse(s["changed_direction"])
        self.assertTrue(s["jev_changed_direction"])
        self.assertTrue(s["blamed_decoy"])

    def test_a_failing_jev_is_recorded_and_the_trial_goes_on(self):
        def broken(scenario, observed):
            raise triage.JevError("HTTP 503")
        model = Scripted(reply(dist("deploy"), VCHECK), reply(dist("batch-job"), final=RIGHT))
        t = rca.run_trial(SCENARIO, "jev", model, broken)
        self.assertEqual(t["status"], "ok")
        s = rca.score_trial(t, TRUTH)
        self.assertEqual(s["jev_errors"], 2)
        self.assertTrue(s["changed_direction"])  # the model's own belief still counts
        self.assertIsNone(s["jev_changed_direction"])

    def test_jev_contra_shows_only_contradictions_and_nothing_before_evidence(self):
        jev = FakeJev()  # deploy tops the ranking until the version check; then it is contradicted
        model = Scripted(reply(dist("deploy"), VCHECK), reply(dist("batch-job"), "cron-history"),
                         reply(dist("batch-job"), final=RIGHT))
        t = rca.run_trial(SCENARIO, "jev-contra", model, jev)
        first = model.seen[0][1][0]["content"]
        self.assertNotIn("Jev", first)  # no scores before any evidence
        after_vc = model.seen[1][1][-1]["content"]
        self.assertIn("The evidence most contradicts `deploy` (P = 0.90)", after_vc)
        for ranking in ("P(best explanation)", "Jev's scores", "batch-job`: 0."):
            self.assertNotIn(ranking, after_vc)  # never the ranking: nothing to copy
        # Jev's full scores are still recorded at every state, including the first.
        self.assertEqual(jev.calls, [[], [VCHECK], [VCHECK, "cron-history"]])
        self.assertTrue(all(st["jev"] for st in t["states"]))
        s = rca.score_trial(t, TRUTH)
        self.assertTrue(s["changed_direction"])  # the model's own belief
        self.assertTrue(s["jev_changed_direction"])  # Jev's, reported apart
        with self.assertRaises(ValueError):
            rca.run_trial(SCENARIO, "jev-contra", Scripted())

    def test_contradiction_list_wording(self):
        none_top = {**{h: 0.05 for h in HYPS}, triage.NONE: 0.7}
        text = rca.format_jev_contradictions({"beliefs": dist("deploy"), "contradicted": none_top})
        self.assertIn("No hypothesis is clearly contradicted", text)
        deploy_top = {**{h: 0.05 for h in HYPS}, "deploy": 0.6, triage.NONE: 0.15}
        text = rca.format_jev_contradictions({"beliefs": dist("deploy"), "contradicted": deploy_top})
        self.assertIn("most contradicts `deploy` (P = 0.60)", text)
        self.assertEqual(text.count("`"), 2)  # one hypothesis named, never a list

    def test_jev_setup_needs_a_scorer(self):
        with self.assertRaises(ValueError):
            rca.run_trial(SCENARIO, "jev", Scripted())

    def test_invalid_picks_are_retried_then_give_up(self):
        t = rca.run_trial(SCENARIO, "alone", Scripted("hmm", "hmm", "hmm", "hmm"))
        self.assertEqual(t["status"], "invalid_reply")
        model = Scripted("hmm", reply(dist("deploy"), "pool-metrics"), reply(dist("batch-job"), final=RIGHT))
        t = rca.run_trial(SCENARIO, "alone", model)
        self.assertEqual((t["status"], t["observed"]), ("ok", ["pool-metrics"]))

    def test_a_check_is_never_run_twice(self):
        model = Scripted(reply(dist("deploy"), "pool-metrics"), reply(dist("deploy"), "pool-metrics"),
                         reply(dist("deploy"), "cron-history"), reply(dist("batch-job"), final=RIGHT))
        t = rca.run_trial(SCENARIO, "alone", model)
        self.assertEqual(t["observed"], ["pool-metrics", "cron-history"])

    def test_out_of_checks_forces_a_final_answer_or_counts_unfinished(self):
        picks = [reply(dist("deploy"), c) for c in ("pool-metrics", "cron-history")]
        t = rca.run_trial(SCENARIO, "alone", Scripted(*picks, reply(dist("batch-job"), final=RIGHT)),
                          max_checks=2)
        self.assertEqual((t["status"], t["final"]["hypothesis"]), ("ok", "batch-job"))
        t = rca.run_trial(SCENARIO, "alone", Scripted(*picks, "still thinking"), max_checks=2)
        self.assertEqual(t["status"], "unfinished")
        self.assertFalse(rca.score_trial(t, TRUTH)["finished"])

    def test_model_errors_end_the_trial(self):
        def down(system, messages):
            raise rca.ModelError("HTTP 529: overloaded")
        t = rca.run_trial(SCENARIO, "alone", down)
        self.assertEqual(t["status"], "model_error")
        self.assertIn("529", t["error"])


class Scoring(unittest.TestCase):
    def trace(self, setup="alone", beliefs=(), observed=(), final=None, jev=()):
        observed = list(observed)
        states = [{"check": c, "model_beliefs": b, "jev": {"beliefs": j} if j else None, "jev_error": None}
                  for c, b, j in zip([None, *observed], list(beliefs) + [None] * 20, list(jev) + [None] * 20)]
        return {"setup": setup, "forced": False, "status": "ok", "states": states,
                "observed": observed, "final": final}

    def test_agent_that_changes_direction(self):
        t = self.trace(beliefs=[dist("deploy", 0.7), dist("deploy", 0.7), dist("batch-job", 0.5),
                                dist("batch-job", 0.8)],
                       observed=["deployment-log", VCHECK, "cron-history", "active-queries"], final=RIGHT)
        s = rca.score_trial(t, TRUTH)
        self.assertTrue(s["decoy_led_initially"])
        self.assertTrue(s["changed_direction"])
        self.assertAlmostEqual(s["p_decoy_drop"], 0.7 - 0.1, places=3)
        for key in ("correct_hypothesis", "found_mechanism", "named_component", "evidence_all_observed"):
            self.assertTrue(s[key], key)
        self.assertFalse(s["blamed_decoy"])
        self.assertFalse(s["cited_trap"])

    def test_agent_that_sticks_with_the_decoy(self):
        t = self.trace(beliefs=[dist("deploy", 0.7), dist("deploy", 0.6)],
                       observed=[VCHECK], final=DECOY)
        s = rca.score_trial(t, TRUTH)
        self.assertFalse(s["changed_direction"])
        self.assertAlmostEqual(s["p_decoy_drop"], 0.1, places=3)
        self.assertTrue(s["blamed_decoy"])
        self.assertFalse(s["correct_hypothesis"])
        self.assertFalse(s["evidence_all_observed"])  # cited deployment-log, never ran it

    def test_no_direction_to_change_when_the_decoy_did_not_lead(self):
        t = self.trace(beliefs=[dist("traffic"), dist("batch-job")], observed=[VCHECK], final=RIGHT)
        self.assertIsNone(rca.score_trial(t, TRUTH)["changed_direction"])

    def test_mechanism_is_scored_separately_from_the_hypothesis(self):
        vague = dict(RIGHT, component="database", mechanism="Something on the database.")
        t = self.trace(beliefs=[dist("deploy")], observed=["cron-history"], final=vague)
        s = rca.score_trial(t, TRUTH)
        self.assertTrue(s["correct_hypothesis"])
        self.assertFalse(s["found_mechanism"])
        self.assertFalse(s["named_component"])
        # The right words without having run a check that shows them is not finding it.
        t = self.trace(beliefs=[dist("deploy")], observed=["dns-log"], final=RIGHT)
        self.assertFalse(rca.score_trial(t, TRUTH)["found_mechanism"])

    def test_citing_a_trap(self):
        final = dict(RIGHT, evidence=["slow-query-log", "cron-history"])
        t = self.trace(beliefs=[dist("deploy")], observed=["slow-query-log", "cron-history"], final=final)
        s = rca.score_trial(t, TRUTH)
        self.assertTrue(s["cited_trap"])
        self.assertTrue(s["evidence_all_observed"])

    def test_noise_and_unfinished(self):
        t = self.trace(beliefs=[dist("deploy")], observed=["cdn-cache", "cert-expiry", "pool-metrics"])
        s = rca.score_trial(t, TRUTH)
        self.assertEqual((s["checks_used"], s["noise_checks"]), (3, 2))
        self.assertFalse(s["finished"])
        self.assertIsNone(s["correct_hypothesis"])

    def test_both_setups_measure_the_model_and_jev_is_reported_apart(self):
        # The model stays on the deploy; Jev moves off it. The headline metrics are the
        # model's in both setups, so alone and + Jev compare the same believer.
        t = self.trace(setup="jev", beliefs=[dist("deploy", 0.7), dist("deploy", 0.6)],
                       jev=[dist("deploy", 0.8), dist("batch-job", 0.6)], observed=[VCHECK], final=RIGHT)
        s = rca.score_trial(t, TRUTH)
        self.assertFalse(s["changed_direction"])
        self.assertAlmostEqual(s["p_decoy_drop"], 0.1, places=3)
        self.assertTrue(s["jev_changed_direction"])
        self.assertAlmostEqual(s["jev_p_decoy_start"], 0.8)
        self.assertAlmostEqual(s["jev_p_decoy_drop"], 0.8 - 0.08, places=3)

    def test_jev_can_stay_anchored_while_the_model_moves(self):
        # The pilot's case: Jev starts sure of the deploy and still ranks it first after
        # the version check, while the model drops it.
        jev_after = {**dist("deploy", 0.51)}
        t = self.trace(setup="jev", beliefs=[dist("deploy", 0.45), dist("batch-job", 0.5)],
                       jev=[dist("deploy", 1.0), jev_after], observed=[VCHECK], final=RIGHT)
        s = rca.score_trial(t, TRUTH)
        self.assertTrue(s["changed_direction"])
        self.assertFalse(s["jev_changed_direction"])
        self.assertAlmostEqual(s["jev_p_decoy_drop"], 0.49, places=3)

    def test_a_tie_at_the_top_is_not_a_change_of_course(self):
        # From the pilot: with Jev, the model went deploy 0.45 -> 0.25, tied with traffic.
        before = {"deploy": 0.45, "batch-job": 0.15, "conn-leak": 0.15, "db-config": 0.10,
                  "traffic": 0.10, "dns": 0.05}
        after = {"deploy": 0.25, "traffic": 0.25, "batch-job": 0.20, "conn-leak": 0.15,
                 "db-config": 0.10, "dns": 0.05}
        s = rca.score_trial(self.trace(beliefs=[before, after], observed=[VCHECK], final=RIGHT), TRUTH)
        self.assertFalse(s["changed_direction"])
        tied_before = dict(before, **{"deploy": 0.3, "batch-job": 0.3})
        moved = dict(after, **{"deploy": 0.1, "traffic": 0.4})
        s = rca.score_trial(self.trace(beliefs=[tied_before, moved], observed=[VCHECK], final=RIGHT), TRUTH)
        self.assertTrue(s["changed_direction"])  # tied for the lead counts as leading

    def test_checks_to_cause_and_how_long_the_decoy_held(self):
        t = self.trace(beliefs=[dist("deploy"), dist("deploy", 0.5), dist("traffic"), dist("batch-job"),
                                dist("batch-job", 0.9)],
                       observed=[VCHECK, "traffic-stats", "pool-metrics", "cron-history"], final=RIGHT)
        s = rca.score_trial(t, TRUTH)
        self.assertEqual(s["decoy_held"], 1)  # still led right after the version check, then dropped
        self.assertEqual(s["checks_to_cause"], 3)  # the true cause first led after the third check
        quick = self.trace(beliefs=[dist("deploy"), dist("batch-job")], observed=[VCHECK], final=RIGHT)
        s = rca.score_trial(quick, TRUTH)
        self.assertEqual((s["decoy_held"], s["checks_to_cause"]), (0, 1))
        never = self.trace(beliefs=[dist("deploy"), dist("deploy")], observed=[VCHECK], final=DECOY)
        self.assertIsNone(rca.score_trial(never, TRUTH)["checks_to_cause"])

    def test_alone_has_no_jev_metrics(self):
        s = rca.score_trial(self.trace(beliefs=[dist("deploy"), dist("batch-job")], observed=[VCHECK],
                                       final=RIGHT), TRUTH)
        for key in ("jev_changed_direction", "jev_p_decoy_drop", "jev_decoy_led_initially", "jev_p_decoy_start"):
            self.assertIsNone(s[key], key)


class Report(unittest.TestCase):
    def test_counts_by_model_and_setup(self):
        alone = Scripted(reply(dist("deploy"), VCHECK), reply(dist("deploy"), final=DECOY))
        with_jev = Scripted(reply(dist("deploy"), VCHECK), reply(dist("batch-job"), final=RIGHT))
        traces = [dict(rca.run_trial(SCENARIO, "alone", alone), model="anthropic:m"),
                  dict(rca.run_trial(SCENARIO, "jev", with_jev, FakeJev()), model="anthropic:m")]
        buf = io.StringIO()
        rca.report(traces, out=buf)
        rows = {line[:32].strip(): line[32:].split() for line in buf.getvalue().splitlines()[2:]}
        self.assertEqual(rows["right hypothesis"], ["0/1", "1/1", "-"])  # no jev-contra trials
        self.assertEqual(rows["blamed the decoy"], ["1/1", "0/1", "-"])
        self.assertNotIn("%", buf.getvalue())


class CLI(unittest.TestCase):
    def run_main(self, *argv, env=None):
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.dict(os.environ, env or {}, clear=True):
            code = rca.main(list(argv))
        return code, buf.getvalue()

    def test_dry_run_needs_no_keys(self):
        code, out = self.run_main("--dry-run", "--models", "anthropic,openai:gpt-x", "--trials", "2",
                                  "--forced")
        self.assertEqual(code, 0)
        self.assertIn(rca.SYSTEM_PROMPT, out)
        self.assertIn('"most_contradicted"', out)
        self.assertIn(f"{len(rca.scenario_names()) * 12} trials, forced mode", out)  # scenarios x 2 models x 3 setups x 2
        self.assertIn("openai:gpt-x", out)
        # Setups alternate within each trial, so API drift hits both alike.
        plan = [l.split()[-1] for l in out.splitlines() if l.startswith("  trial ")]
        self.assertEqual(plan[:6], ["alone", "jev", "jev-contra", "alone", "jev", "jev-contra"])

    def test_missing_keys_stop_before_any_trial(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_main("--models", "anthropic")
        self.assertIn("ANTHROPIC_API_KEY", str(cm.exception.code))
        with self.assertRaises(SystemExit) as cm:
            self.run_main("--models", "anthropic", env={"ANTHROPIC_API_KEY": "k"})
        self.assertIn("TYPESAFE_API_KEY", str(cm.exception.code))

    def test_setup_choices(self):
        code, out = self.run_main("--dry-run", "--setup", "alone,jev-contra", "--trials", "1")
        plan = [l.split()[-1] for l in out.splitlines() if l.startswith("  trial ")]
        self.assertEqual(set(plan), {"alone", "jev-contra"})
        self.assertIn("Jev payload", out)  # jev-contra calls Jev too
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            self.run_main("--dry-run", "--setup", "both")

    def test_bad_model_spec(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            self.run_main("--dry-run", "--models", "gemini:x")
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            self.run_main("--dry-run", "--scenarios", "nope")

    def test_check_reports_each_call(self):
        ok = lambda *a, **k: ("OK", {}, {})
        with mock.patch.object(rca, "call_anthropic", side_effect=ok), \
             mock.patch.object(rca, "call_openai", side_effect=rca.ModelError("HTTP 401")), \
             mock.patch.object(triage, "call_jev", return_value=(jev_response("deploy"), 9.0)):
            code, out = self.run_main("--check", "--models", "anthropic,openai",
                                      env={"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o",
                                           "TYPESAFE_API_KEY": "t"})
        self.assertEqual(code, 1)
        self.assertIn("ok    anthropic:claude-opus-5", out)
        self.assertIn("FAIL  openai:gpt-5: HTTP 401", out)
        self.assertIn("top at start is 'deploy'", out)

    def test_a_run_appends_traces_and_reports(self):
        fake = Scripted(reply(dist("deploy"), VCHECK), reply(dist("batch-job"), final=RIGHT),
                        reply(dist("deploy"), VCHECK), reply(dist("batch-job"), final=RIGHT))
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "t.jsonl")
            with mock.patch.object(rca, "model_client", return_value=fake), \
                 mock.patch.object(rca, "jev_scorer", return_value=FakeJev()):
                code, text = self.run_main("--models", "anthropic", "--scenarios", "pool_etl_cron",
                                           "--setup", "alone,jev", "--out", out)
            traces = rca.read_traces(out)
            self.assertEqual(code, 0)
            self.assertEqual([t["setup"] for t in traces], ["alone", "jev"])
            self.assertEqual({t["model"] for t in traces}, {"anthropic:claude-opus-5"})
            self.assertEqual(traces[0]["scenario_digest"], rca.scenario_digest(SCENARIO))
            self.assertIn("right hypothesis", text)
            _, again = self.run_main("--report", out)
            self.assertIn("right hypothesis", again)

    def test_resume_skips_finished_trials_and_retries_errors(self):
        def fake():
            return Scripted(*[r for _ in range(4) for r in
                              (reply(dist("deploy"), VCHECK), reply(dist("batch-job"), final=RIGHT))])
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "t.jsonl")
            args = ["--models", "anthropic", "--scenarios", "pool_etl_cron", "--setup", "alone,jev",
                    "--forced", "--out", out]
            with mock.patch.object(rca, "jev_scorer", return_value=FakeJev()):
                with mock.patch.object(rca, "model_client", return_value=fake()):
                    self.run_main(*args, "--trials", "1")
                with open(out) as f:
                    first = [json.loads(l) for l in f]
                first[1]["status"] = "model_error"  # pretend the jev trial hit a rate limit
                with open(out, "w") as f:
                    f.writelines(json.dumps(t) + "\n" for t in first)
                with mock.patch.object(rca, "model_client", return_value=fake()):
                    _, text = self.run_main(*args, "--trials", "2", "--resume")
            self.assertIn("3 of 4 trials to run", text)  # trial 1 alone was done; its jev errored
            with open(out) as f:
                self.assertEqual(len(f.readlines()), 5)  # append-only: the errored attempt stays on record
            traces = rca.read_traces(out)
            self.assertEqual(len(traces), 4)  # but only the latest attempt of each trial counts
            self.assertTrue(all(t["status"] == "ok" for t in traces))

    def test_a_second_run_into_the_same_file_adds_trials(self):
        def fake():
            return Scripted(*[r for _ in range(4) for r in
                              (reply(dist("deploy"), VCHECK), reply(dist("batch-job"), final=RIGHT))])
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "t.jsonl")
            args = ["--models", "anthropic", "--scenarios", "pool_etl_cron", "--setup", "alone,jev",
                    "--forced", "--out", out]
            with mock.patch.object(rca, "jev_scorer", return_value=FakeJev()):
                for _ in range(2):  # the same command twice, without --resume
                    with mock.patch.object(rca, "model_client", return_value=fake()):
                        self.run_main(*args)
            traces = rca.read_traces(out)
            self.assertEqual(len(traces), 4)  # nothing replaced
            self.assertEqual(sorted(t["trial"] for t in traces if t["setup"] == "alone"), [1, 2])

    def test_an_older_file_with_repeated_trial_numbers_keeps_every_trial(self):
        # The pilot file: a second run reused trial 1 in every cell. Only an errored
        # attempt may be replaced by a later one; finished trials all count.
        base = {"scenario": "pool_etl_cron", "scenario_digest": "d", "model": "m", "setup": "alone",
                "forced": True, "trial": 1, "harness_version": 3}
        rows = [dict(base, status="ok", run_id="a"), dict(base, status="invalid_reply", run_id="b"),
                dict(base, setup="jev", status="model_error", run_id="a"),
                dict(base, setup="jev", status="ok", run_id="b")]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.jsonl")
            with open(path, "w") as f:
                f.writelines(json.dumps(r) + "\n" for r in rows)
            got = sorted((t["setup"], t["run_id"]) for t in rca.read_traces(path))
        self.assertEqual(got, [("alone", "a"), ("alone", "b"), ("jev", "b")])

    def test_local_server_needs_no_key(self):
        rca.model_client("openai", "m", env={"OPENAI_BASE_URL": "http://localhost:11434/v1"})
        with self.assertRaises(rca.ModelError):
            rca.model_client("openai", "m", env={})
        with mock.patch.object(rca, "post_json", return_value={"choices": [{"message": {"content": "x"}}]}) as post:
            rca.call_openai("m", "s", [], None, "http://localhost:11434/v1")
        self.assertEqual(post.call_args[0][2], {})  # no Authorization header

    def test_openai_url(self):
        self.assertEqual(rca.openai_url(None), "https://api.openai.com/v1/chat/completions")
        self.assertEqual(rca.openai_url("https://x.ai/v1/"), "https://x.ai/v1/chat/completions")
        self.assertEqual(rca.openai_url("http://localhost:8000"), "http://localhost:8000/v1/chat/completions")
        self.assertEqual(rca.openai_url("https://generativelanguage.googleapis.com/v1beta/openai/"),
                         "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
        self.assertEqual(rca.openai_url("https://openrouter.ai/api/v1"), "https://openrouter.ai/api/v1/chat/completions")


class ModelResponses(unittest.TestCase):
    def test_anthropic_text_comes_from_text_blocks_not_thinking(self):
        resp = {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "NEXT: x"}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 4}}
        with mock.patch.object(rca, "post_json", return_value=resp):
            text, msg, usage = rca.call_anthropic("m", "s", [], "k")
        self.assertEqual(text, "NEXT: x")
        self.assertEqual(msg["content"], resp["content"])  # thinking blocks go back unchanged
        self.assertEqual(usage, {"input_tokens": 3, "output_tokens": 4})

    def test_anthropic_refusal_is_an_error(self):
        with mock.patch.object(rca, "post_json", return_value={"stop_reason": "refusal", "content": []}):
            with self.assertRaises(rca.ModelError):
                rca.call_anthropic("m", "s", [], "k")

    def test_max_tokens_is_opt_in(self):
        resp = {"choices": [{"message": {"content": "x"}}]}
        with mock.patch.object(rca, "post_json", return_value=resp) as post:
            rca.call_openai("m", "s", [], "k")
            self.assertNotIn("max_tokens", post.call_args[0][1])
            rca.call_openai("m", "s", [], "k", max_tokens=2000)
            self.assertEqual(post.call_args[0][1]["max_tokens"], 2000)

    def test_an_error_in_a_200_body_is_reported(self):
        resp = {"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day"}}
        with mock.patch.object(rca, "post_json", return_value=resp):
            with self.assertRaises(rca.ModelError) as cm:
                rca.call_openai("m", "s", [], "k")
        self.assertIn("free-models-per-day", str(cm.exception))

    def test_openai_shape(self):
        resp = {"choices": [{"message": {"content": "DONE"}}], "usage": {"prompt_tokens": 1}}
        with mock.patch.object(rca, "post_json", return_value=resp) as post:
            text, _, usage = rca.call_openai("m", "sys", [{"role": "user", "content": "hi"}], "k")
        self.assertEqual(text, "DONE")
        self.assertEqual(post.call_args[0][1]["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(usage["input_tokens"], 1)


class GroundTruthLeak(unittest.TestCase):
    """Nothing from a scenario's truth file may reach the model or Jev."""

    def agent_inputs(self, scenario):
        yield "system prompt", rca.SYSTEM_PROMPT
        yield "first message", rca.build_initial_prompt(scenario)
        yield "jev payload", json.dumps(rca.build_jev_payload(scenario, list(scenario["checks"])))
        for name, check in scenario["checks"].items():
            yield name, rca.format_observation(name, check["result"])

    def test_no_agent_input_contains_ground_truth(self):
        for name in rca.scenario_names():
            scenario, truth = rca.load_scenario(name), rca.load_ground_truth(name)
            secrets = [truth["mechanism"], *[n["notes"] for n in truth["check_notes"].values() if "notes" in n]]
            for where, text in self.agent_inputs(scenario):
                for secret in secrets:
                    self.assertNotIn(secret, text, f"{name}: {where}")
                for word in ("TRAP", "NOISE", "decoy", "ground truth"):
                    self.assertNotIn(word, text, f"{name}: {where} contains {word!r}")

    def test_scenario_files_hold_no_scoring_fields(self):
        for name in rca.scenario_names():
            with open(os.path.join(rca.SCENARIO_DIR, f"{name}.json"), encoding="utf-8") as f:
                raw = f.read()
            for key in ("rules_out", "supports", "notes", "decoy", "hypothesis\"", "mechanism", "traps"):
                self.assertNotIn(key, raw, f"{name}: {key}")

    def test_running_a_trial_never_reads_a_truth_file(self):
        real_open = open

        def guarded(path, *a, **k):
            if str(path).endswith(".truth.json"):
                raise AssertionError(f"the trial read {path}")
            return real_open(path, *a, **k)
        model = Scripted(reply(dist("deploy"), VCHECK), reply(dist("batch-job"), final=RIGHT))
        with mock.patch("builtins.open", guarded):
            rca.run_trial(rca.load_scenario(), "jev", model, FakeJev(), forced=True)


class Scenarios(unittest.TestCase):
    def test_every_scenario_has_what_the_experiment_needs(self):
        self.assertGreaterEqual(len(rca.scenario_names()), 3)
        for name in rca.scenario_names():
            scenario, truth = rca.load_scenario(name), rca.load_ground_truth(name)
            checks, hyps = scenario["checks"], scenario["hypotheses"]
            with self.subTest(name):
                self.assertGreaterEqual(len(hyps), 5)
                self.assertIn(truth["hypothesis"], hyps)
                # The decoy is whatever looks guilty at the start; it can be the deploy or
                # anything else, and some scenarios have a guilty deploy.
                self.assertIn(truth["decoy"], hyps)
                self.assertNotEqual(truth["decoy"], truth["hypothesis"])
                self.assertEqual(scenario["version_check"], truth["version_check"])
                self.assertGreaterEqual(len(truth["traps"]), 2)
                self.assertGreaterEqual(len(truth["noise"]), 3)
                for check in [truth["version_check"], *truth["traps"], *truth["noise"],
                              *truth["mechanism_checks"], *truth["check_notes"]]:
                    self.assertIn(check, checks)
                ruled_out = {n.get("rules_out") for n in truth["check_notes"].values()}
                for h in hyps:
                    if h != truth["hypothesis"]:
                        self.assertIn(h, ruled_out, f"no check rules out {h}")

    def test_the_partial_decoy_scenario(self):
        # The version check only weakens the deploy: the new version is worse, but the
        # old one fails too. Blaming the deploy is wrong; naming the cache is right.
        name = "product_cache_partial"
        scenario, truth = rca.load_scenario(name), rca.load_ground_truth(name)
        vc = scenario["checks"][scenario["version_check"]]["result"]
        self.assertIn("v5.3.0 (6 instances): 5xx 11.4%", vc)  # the new version really is worse
        self.assertIn("v5.2.9 (6 instances): 5xx 7.1%", vc)  # but the old one fails too
        hyps = list(scenario["hypotheses"])
        b = lambda top: {h: (0.8 if h == top else 0.04) for h in hyps}
        right = {"hypothesis": "cache", "component": "product-cache (Redis)",
                 "mechanism": "The cache primary was replaced during maintenance and came back empty, so "
                              "lookups fell through to the database; the new TTL made it worse.",
                 "evidence": ["cache_events", "cache_hit_rate"]}
        model = Scripted(reply(b("deploy"), "errors_by_version"), reply(b("deploy"), "cache_hit_rate"),
                         reply(b("cache"), "cache_events"), reply(b("cache"), final=right))
        s = rca.score_trial(rca.run_trial(scenario, "alone", model, forced=True), truth)
        for key in ("correct_hypothesis", "found_mechanism", "named_component", "evidence_all_observed"):
            self.assertTrue(s[key], key)
        self.assertFalse(s["changed_direction"])  # the deploy survived the version check
        self.assertEqual((s["decoy_held"], s["checks_to_cause"]), (1, 2))
        blame = {"hypothesis": "deploy", "component": "product-api v5.3.0",
                 "mechanism": "The shorter cache TTL caused more database load.", "evidence": ["errors_by_version"]}
        s = rca.score_trial(rca.run_trial(scenario, "alone", Scripted(
            reply(b("deploy"), "errors_by_version"), reply(b("deploy"), final=blame)), forced=True), truth)
        self.assertTrue(s["blamed_decoy"])
        self.assertFalse(s["named_component"])  # "cache TTL" in a deploy blame doesn't count

    def test_each_trace_is_scored_against_its_own_scenario(self):
        name = "pool_lock_batch"
        scenario, truth = rca.load_scenario(name), rca.load_ground_truth(name)
        hyps = list(scenario["hypotheses"])
        right = {"hypothesis": truth["hypothesis"], "component": "orders-archive job",
                 "mechanism": "The manually run archive batch holds row locks, so checkout queries "
                              "wait and hold pool connections.",
                 "evidence": ["db_lock_waits", "batch_jobs"]}
        b = lambda top: {h: (0.8 if h == top else 0.05) for h in hyps}
        model = Scripted(reply(b("deploy"), "errors_by_version"),
                         reply(b("slow_queries"), "db_lock_waits"),
                         reply(b("slow_queries"), "batch_jobs"),
                         reply(b("slow_queries"), final=right))
        trace = dict(rca.run_trial(scenario, "alone", model, forced=True), scenario=name, model="m")
        s = rca.score_trial(trace, truth)
        for key in ("changed_direction", "correct_hypothesis", "found_mechanism", "named_component",
                    "evidence_all_observed"):
            self.assertTrue(s[key], key)
        buf = io.StringIO()
        rca.report([trace, dict(trace, scenario="pool_etl_cron")], out=buf)
        self.assertIn("pool_lock_batch | m | forced version check", buf.getvalue())
        self.assertIn("pool_etl_cron | m | forced version check", buf.getvalue())


class HtmlReport(unittest.TestCase):
    def traces(self):
        stuck = Scripted(reply(dist("deploy"), VCHECK), reply(dist("deploy"), final=DECOY))
        switched = Scripted(reply(dist("deploy"), VCHECK), reply(dist("batch-job"), "cron-history"),
                            reply(dist("batch-job"), final=RIGHT))
        return [dict(rca.run_trial(SCENARIO, "alone", stuck), model="test:m", scenario="pool_etl_cron", trial=1),
                dict(rca.run_trial(SCENARIO, "jev", switched, FakeJev()), model="test:m",
                     scenario="pool_etl_cron", trial=1)]

    def test_ranks_systems_by_resolved_with_counts_and_intervals(self):
        import rca_report
        page = rca_report.render(self.traces(), note="Test data only.")
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("<title>RCA Leaderboard</title>", page.split("</head>")[0])
        self.assertIn("Test data only.", page)
        board = rca_report.leaderboard(rca_report.scored(self.traces()))
        self.assertEqual([setup for _, setup, _ in board], ["jev", "alone"])
        self.assertIn('<span class="name">m</span><span class="tag jev">+ Jev</span>', page)
        self.assertIn("Test stand-in", page)  # the vendor, from the test: prefix
        self.assertIn("1/1 · CI 21–100", page)  # headline score with count and 95% interval
        self.assertIn("0/1 · CI 0–79", page)
        self.assertIn('role="tab"', page)
        self.assertIn("Does Jev help?", page)
        # The model's belief and Jev's are shown apart, never mixed in one column.
        self.assertIn("Belief in the decoy", page)
        self.assertIn("<td>Jev&#x27;s scores</td>", page)
        self.assertEqual(page.count('<td>model</td>'), 2)  # alone and + Jev
        self.assertIn('class="jevline"', page)

    def test_three_setups_on_the_page(self):
        import rca_report
        switched = Scripted(reply(dist("deploy"), VCHECK), reply(dist("batch-job"), "cron-history"),
                            reply(dist("batch-job"), final=RIGHT))
        traces = self.traces() + [dict(rca.run_trial(SCENARIO, "jev-contra", switched, FakeJev()),
                                       model="test:m", scenario="pool_etl_cron", trial=1)]
        page = rca_report.render(traces)
        self.assertIn('<span class="tag jev-contra">+ Jev contradictions</span>', page)
        self.assertIn('class="b-jev-contra"', page)  # its own bar in the chart
        self.assertIn("+ Jev (contradictions only)", page)  # and in the legend
        self.assertEqual(page.count("<td>Jev&#x27;s scores</td>"), 2)  # a Jev row for each Jev setup

    def test_mixed_harness_versions_are_flagged(self):
        import rca_report
        traces = self.traces()
        self.assertNotIn("Mixed</b>", rca_report.render(traces))
        traces[0]["harness_version"], traces[1]["harness_version"] = 3, 4
        self.assertIn("harness versions 3, 4", rca_report.render(traces))

    def test_wilson_interval(self):
        import rca_report
        self.assertEqual(rca_report.wilson(0, 0), (0.0, 0.0))
        lo, hi = rca_report.wilson(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=3)
        self.assertAlmostEqual(hi, 0.7634, places=3)

    def test_unfinished_counts_as_not_resolved(self):
        import rca_report
        t = self.traces()[1]
        t = dict(t, final=None, status="unfinished")
        (_, s, _), = rca_report.scored([t])
        self.assertFalse(s["resolved"])
        self.assertEqual(rca_report.rate([s], "correct_hypothesis"), (0, 1))

    def test_trace_text_is_escaped(self):
        import rca_report
        t = self.traces()
        t[0]["model"] = '<script>alert("x")</script>'
        t[0]["final"]["hypothesis"] = "<img src=x onerror=alert(1)>"
        page = rca_report.render(t)
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;script&gt;", page)

    def test_fragment_for_embedding(self):
        import rca_report
        frag = rca_report.render(self.traces(), standalone=False)
        self.assertFalse(frag.lstrip().startswith("<!doctype"))
        self.assertNotIn("<body>", frag)

    def test_cli_writes_the_page(self):
        with tempfile.TemporaryDirectory() as d:
            src, out = os.path.join(d, "t.jsonl"), os.path.join(d, "r.html")
            with open(src, "w") as f:
                f.writelines(json.dumps(t) + "\n" for t in self.traces())
            buf = io.StringIO()
            with redirect_stdout(buf):
                rca.main(["--report", src, "--html", out, "--note", "Test data only."])
            with open(out, encoding="utf-8") as f:
                self.assertIn("Test data only.", f.read())
            self.assertIn(f"wrote {out}", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
