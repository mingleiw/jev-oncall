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


RIGHT = {"hypothesis": "etl-cron", "component": "analytics-etl",
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
    probs = dist(top, 0.75)
    answers = {"best_explanation": {"type": "choice", "choice": top, "probabilities": probs}}
    for h in HYPS:
        answers[f"contradicted_{h}"] = {"type": "noul", "noul": 0.9 if h in contradicted else 0.1}
    return {"model": triage.MODEL, "answers": answers, "usage": {"input_tokens": 700}}


class FakeJev:
    """Scores the deploy on top until the version check is observed, then etl-cron."""

    def __init__(self):
        self.calls = []

    def __call__(self, scenario, observed):
        self.calls.append(list(observed))
        if VCHECK in observed:
            resp = jev_response("etl-cron", contradicted=("deploy",))
        else:
            resp = jev_response("deploy")
        return rca.parse_jev_rca(resp, HYPS)


# --------------------------------------------------------------------------

class ReplyParsing(unittest.TestCase):
    def test_beliefs_are_read_and_normalized(self):
        b = rca.parse_beliefs(reply({"deploy": 6, "etl-cron": 2, "traffic": 2}), HYPS)
        self.assertAlmostEqual(b["deploy"], 0.6)
        self.assertEqual(b["dns"], 0.0)
        self.assertAlmostEqual(sum(b.values()), 1.0)

    def test_bare_belief_object_is_accepted(self):
        b = rca.parse_beliefs('Beliefs: {"deploy": 0.5, "etl-cron": 0.5}', HYPS)
        self.assertEqual(rca._top(b), "deploy")

    def test_bad_beliefs_are_rejected(self):
        for text in ("no json here",
                     '{"beliefs": {"deploy": "high", "etl-cron": "low"}}',
                     '{"beliefs": {"deploy": 0.5, "martians": 0.5}}',  # unknown hypothesis
                     '{"beliefs": {"deploy": 0, "etl-cron": 0}}',
                     '{"beliefs": {"deploy": -1, "etl-cron": 2}}',
                     '{"beliefs": {"deploy": 0.5'):  # truncated
            self.assertIsNone(rca.parse_beliefs(text, HYPS), text)

    def test_next_check(self):
        avail = ["pool-metrics", "active-queries"]
        self.assertEqual(rca.parse_next_check("NEXT: `pool-metrics`", avail), "pool-metrics")
        self.assertEqual(rca.parse_next_check("**NEXT: active-queries**", avail), "active-queries")
        self.assertIsNone(rca.parse_next_check("NEXT: `made-up`", avail))
        self.assertIsNone(rca.parse_next_check("I would look at `pool-metrics`.", avail))

    def test_final_answer(self):
        self.assertEqual(rca.parse_final_answer(reply(final=RIGHT), HYPS)["hypothesis"], "etl-cron")
        self.assertIsNone(rca.parse_final_answer(json.dumps(RIGHT), HYPS))  # no DONE
        bad = dict(RIGHT, hypothesis="aliens")
        self.assertIsNone(rca.parse_final_answer(reply(final=bad), HYPS))
        loose = rca.parse_final_answer('DONE {"hypothesis": "dns", "evidence": "dns-log"}', HYPS)
        self.assertEqual(loose["evidence"], [])  # not a list: nothing counts as cited


class JevPayload(unittest.TestCase):
    def test_every_hypothesis_is_scored_and_checked_for_contradiction(self):
        p = rca.build_jev_payload(SCENARIO, [VCHECK])
        self.assertEqual(set(p["questions"]["best_explanation"]["criteria"]), set(HYPS))
        for h in HYPS:
            self.assertEqual(p["questions"][f"contradicted_{h}"]["type"], "noul")
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
        for mutate in (lambda a: a.pop("contradicted_dns"),
                       lambda a: a["contradicted_dns"].update(noul=1.5),
                       lambda a: a["contradicted_dns"].update(noul=float("nan")),
                       lambda a: a["best_explanation"]["probabilities"].update(aliens=0.1)):
            resp = json.loads(json.dumps(good))
            mutate(resp["answers"])
            with self.assertRaises(triage.JevError):
                rca.parse_jev_rca(resp, HYPS)

    def test_scorer_reuses_triage_call_jev(self):
        with mock.patch.object(triage, "call_jev", return_value=(jev_response("etl-cron"), 42.0)) as call:
            scores = rca.jev_scorer("k")(SCENARIO, ["pool-metrics"])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(rca._top(scores["beliefs"]), "etl-cron")
        self.assertEqual(scores["latency_ms"], 42)
        with self.assertRaises(rca.ModelError):
            rca.jev_scorer(None)


class TrialFlow(unittest.TestCase):
    def test_alone_records_a_belief_for_every_state(self):
        model = Scripted(reply(dist("deploy"), "deployment-log"),
                         reply(dist("deploy"), VCHECK),
                         reply(dist("etl-cron"), "cron-history"),
                         reply(dist("etl-cron"), final=RIGHT))
        t = rca.run_trial(SCENARIO, "alone", model)
        self.assertEqual(t["status"], "ok")
        self.assertEqual(t["observed"], ["deployment-log", VCHECK, "cron-history"])
        self.assertEqual([s["check"] for s in t["states"]], [None, *t["observed"]])
        self.assertTrue(all(s["model_beliefs"] for s in t["states"]))
        self.assertEqual(t["final"]["hypothesis"], "etl-cron")
        self.assertEqual(t["usage"]["input_tokens"], 40)

    def test_the_model_sees_each_result_and_never_its_scoring_notes(self):
        model = Scripted(reply(dist("deploy"), VCHECK), reply(dist("etl-cron"), final=RIGHT))
        rca.run_trial(SCENARIO, "alone", model)
        last = model.seen[-1][1][-1]["content"]
        self.assertIn(SCENARIO["checks"][VCHECK]["result"], last)
        self.assertNotIn("Jev", last)

    def test_forced_mode_runs_the_version_check_first_and_keeps_the_prior(self):
        model = Scripted(reply(dist("deploy"), "deployment-log"),
                         reply(dist("etl-cron"), final=RIGHT))
        t = rca.run_trial(SCENARIO, "alone", model, forced=True)
        self.assertEqual(t["observed"], [VCHECK])
        self.assertEqual(rca._top(t["states"][0]["model_beliefs"]), "deploy")  # before
        self.assertEqual(rca._top(t["states"][1]["model_beliefs"]), "etl-cron")  # after
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
        # Jev's belief is measured, not the model's, which stayed on the deploy.
        self.assertTrue(s["changed_direction"])
        self.assertTrue(s["blamed_decoy"])

    def test_a_failing_jev_is_recorded_and_the_trial_goes_on(self):
        def broken(scenario, observed):
            raise triage.JevError("HTTP 503")
        model = Scripted(reply(dist("deploy"), VCHECK), reply(dist("etl-cron"), final=RIGHT))
        t = rca.run_trial(SCENARIO, "jev", model, broken)
        self.assertEqual(t["status"], "ok")
        self.assertEqual(rca.score_trial(t, TRUTH)["jev_errors"], 2)
        self.assertIsNone(rca.score_trial(t, TRUTH)["changed_direction"])

    def test_jev_setup_needs_a_scorer(self):
        with self.assertRaises(ValueError):
            rca.run_trial(SCENARIO, "jev", Scripted())

    def test_invalid_picks_are_retried_then_give_up(self):
        t = rca.run_trial(SCENARIO, "alone", Scripted("hmm", "hmm", "hmm", "hmm"))
        self.assertEqual(t["status"], "invalid_reply")
        model = Scripted("hmm", reply(dist("deploy"), "pool-metrics"), reply(dist("etl-cron"), final=RIGHT))
        t = rca.run_trial(SCENARIO, "alone", model)
        self.assertEqual((t["status"], t["observed"]), ("ok", ["pool-metrics"]))

    def test_a_check_is_never_run_twice(self):
        model = Scripted(reply(dist("deploy"), "pool-metrics"), reply(dist("deploy"), "pool-metrics"),
                         reply(dist("deploy"), "cron-history"), reply(dist("etl-cron"), final=RIGHT))
        t = rca.run_trial(SCENARIO, "alone", model)
        self.assertEqual(t["observed"], ["pool-metrics", "cron-history"])

    def test_out_of_checks_forces_a_final_answer_or_counts_unfinished(self):
        picks = [reply(dist("deploy"), c) for c in ("pool-metrics", "cron-history")]
        t = rca.run_trial(SCENARIO, "alone", Scripted(*picks, reply(dist("etl-cron"), final=RIGHT)),
                          max_checks=2)
        self.assertEqual((t["status"], t["final"]["hypothesis"]), ("ok", "etl-cron"))
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
        t = self.trace(beliefs=[dist("deploy", 0.7), dist("deploy", 0.7), dist("etl-cron", 0.5),
                                dist("etl-cron", 0.8)],
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
        t = self.trace(beliefs=[dist("traffic"), dist("etl-cron")], observed=[VCHECK], final=RIGHT)
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

    def test_jev_setup_measures_jev_not_the_model(self):
        t = self.trace(setup="jev", beliefs=[dist("deploy"), dist("deploy")],
                       jev=[dist("deploy", 0.8), dist("etl-cron", 0.6)], observed=[VCHECK], final=RIGHT)
        s = rca.score_trial(t, TRUTH)
        self.assertTrue(s["changed_direction"])
        self.assertAlmostEqual(s["p_decoy_drop"], 0.8 - 0.08, places=3)


class Report(unittest.TestCase):
    def test_counts_by_model_and_setup(self):
        alone = Scripted(reply(dist("deploy"), VCHECK), reply(dist("deploy"), final=DECOY))
        with_jev = Scripted(reply(dist("deploy"), VCHECK), reply(dist("etl-cron"), final=RIGHT))
        traces = [dict(rca.run_trial(SCENARIO, "alone", alone), model="anthropic:m"),
                  dict(rca.run_trial(SCENARIO, "jev", with_jev, FakeJev()), model="anthropic:m")]
        buf = io.StringIO()
        rca.report(traces, out=buf)
        rows = {line[:32].strip(): line[32:].split() for line in buf.getvalue().splitlines()[2:]}
        self.assertEqual(rows["right hypothesis"], ["0/1", "1/1"])
        self.assertEqual(rows["blamed the decoy"], ["1/1", "0/1"])
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
        self.assertIn('"contradicted_deploy"', out)
        self.assertIn("16 trials, forced mode", out)  # 2 scenarios x 2 models x 2 setups x 2
        self.assertIn("openai:gpt-x", out)
        # Setups alternate within each trial, so API drift hits both alike.
        plan = [l.split()[-1] for l in out.splitlines() if l.startswith("  trial ")]
        self.assertEqual(plan[:4], ["alone", "jev", "alone", "jev"])

    def test_missing_keys_stop_before_any_trial(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_main("--models", "anthropic")
        self.assertIn("ANTHROPIC_API_KEY", str(cm.exception.code))
        with self.assertRaises(SystemExit) as cm:
            self.run_main("--models", "anthropic", env={"ANTHROPIC_API_KEY": "k"})
        self.assertIn("TYPESAFE_API_KEY", str(cm.exception.code))

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
        fake = Scripted(reply(dist("deploy"), VCHECK), reply(dist("etl-cron"), final=RIGHT),
                        reply(dist("deploy"), VCHECK), reply(dist("etl-cron"), final=RIGHT))
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "t.jsonl")
            with mock.patch.object(rca, "model_client", return_value=fake), \
                 mock.patch.object(rca, "jev_scorer", return_value=FakeJev()):
                code, text = self.run_main("--models", "anthropic", "--scenarios", "pool_etl_cron", "--out", out)
            traces = rca.read_traces(out)
            self.assertEqual(code, 0)
            self.assertEqual([t["setup"] for t in traces], ["alone", "jev"])
            self.assertEqual({t["model"] for t in traces}, {"anthropic:claude-opus-5"})
            self.assertEqual(traces[0]["scenario_digest"], rca.scenario_digest(SCENARIO))
            self.assertIn("right hypothesis", text)
            _, again = self.run_main("--report", out)
            self.assertIn("right hypothesis", again)

    def test_openai_url(self):
        self.assertEqual(rca.openai_url(None), "https://api.openai.com/v1/chat/completions")
        self.assertEqual(rca.openai_url("https://x.ai/v1/"), "https://x.ai/v1/chat/completions")
        self.assertEqual(rca.openai_url("http://localhost:8000"), "http://localhost:8000/v1/chat/completions")


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
        model = Scripted(reply(dist("deploy"), VCHECK), reply(dist("etl-cron"), final=RIGHT))
        with mock.patch("builtins.open", guarded):
            rca.run_trial(rca.load_scenario(), "jev", model, FakeJev(), forced=True)


class Scenarios(unittest.TestCase):
    def test_every_scenario_has_what_the_experiment_needs(self):
        self.assertGreaterEqual(len(rca.scenario_names()), 2)
        for name in rca.scenario_names():
            scenario, truth = rca.load_scenario(name), rca.load_ground_truth(name)
            checks, hyps = scenario["checks"], scenario["hypotheses"]
            with self.subTest(name):
                self.assertGreaterEqual(len(hyps), 5)
                self.assertIn(truth["hypothesis"], hyps)
                self.assertEqual(truth["decoy"], "deploy")
                self.assertIn("deploy", scenario["initial_context"])  # seen before any check
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
        switched = Scripted(reply(dist("deploy"), VCHECK), reply(dist("etl-cron"), "cron-history"),
                            reply(dist("etl-cron"), final=RIGHT))
        return [dict(rca.run_trial(SCENARIO, "alone", stuck), model="test:m", scenario="pool_etl_cron", trial=1),
                dict(rca.run_trial(SCENARIO, "jev", switched, FakeJev()), model="test:m",
                     scenario="pool_etl_cron", trial=1)]

    def test_ranks_models_and_compares_alone_with_jev(self):
        import rca_report
        page = rca_report.render(self.traces(), note="Test data only.")
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("<title>RCA Leaderboard</title>", page.split("</head>")[0])
        self.assertIn("Test data only.", page)
        board = rca_report.leaderboard(rca_report.scored(self.traces()))
        self.assertEqual([setup for _, setup, _ in board], ["jev", "alone"])  # right cause ranks first
        self.assertIn("better with Jev", page)
        self.assertIn('<span class="val">0/1</span><span class="arrow" aria-hidden="true">→</span>'
                      '<span class="val">1/1</span>', page)
        self.assertIn("1 trial<", page)
        self.assertNotIn("%", page.split("<main>")[1].split("<section")[1])  # counts, not percentages

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
