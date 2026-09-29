#!/usr/bin/env python3
"""Offline tests for the jev-agent setup (Jev investigates, code decides) with a
scripted fake Jev: action validation, evidence visibility, no truth leakage,
stopping and abstention, budgets, API failures, the explanation writer, the
comparison report and the CLI. No API key, no network.

    python3 -m unittest test_rca_agent -v
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import rca_experiment as rca
import rca_jev_agent as agent
import triage

SCENARIO = rca.load_scenario()
TRUTH = rca.load_ground_truth()
HYPS = list(SCENARIO["hypotheses"])
VCHECK = SCENARIO["version_check"]
CAUSE = TRUTH["hypothesis"]  # batch-job


def choice(probs):
    top = max(sorted(probs), key=probs.get)
    return {"type": "choice", "choice": top, "probabilities": probs}


def peaked(options, top, p=0.8):
    rest = (1 - p) / (len(options) - 1)
    return {o: (p if o == top else rest) for o in options}


class FakeJev:
    """Answers from the payload alone, as Jev would: it only knows what it is sent.

    picks: checks to ask for, in order (the first not yet run and on offer).
    lead_after: once this check is observed, the cause leads best_explanation.
    supported_after / enough_after: once observed, the cause is supported / the
    evidence is judged enough. support_yes: observed checks judged to support the
    candidate. ruled_out: alternatives judged ruled out ("all" for every one)."""

    def __init__(self, picks=(VCHECK, "cron-history", "active-queries"), lead_after=VCHECK,
                 supported_after="cron-history", enough_after="cron-history",
                 support_yes=("cron-history", "active-queries"), ruled_out="all", enough_p=0.85, lead_p=0.8):
        self.picks, self.lead_after = list(picks), lead_after
        self.supported_after, self.enough_after = supported_after, enough_after
        self.support_yes, self.ruled_out, self.enough_p = set(support_yes), ruled_out, enough_p
        self.lead_p = lead_p
        self.payloads = []

    def observed(self, payload):
        return [o["check"] for o in payload["state"]["observations"]]

    def __call__(self, payload):
        self.payloads.append(json.loads(json.dumps(payload)))
        seen, q = self.observed(payload), payload["questions"]
        if "best_explanation" in q:
            lead = CAUSE if self.lead_after in seen else "deploy"
            answers = {"best_explanation": choice(peaked(HYPS, lead, self.lead_p)),
                       "most_contradicted": choice(peaked([*HYPS, triage.NONE],
                                                          "deploy" if VCHECK in seen else triage.NONE)),
                       "supported": choice(peaked([*HYPS, triage.NONE],
                                                  CAUSE if self.supported_after in seen else triage.NONE)),
                       "enough_evidence": {"type": "noul",
                                           "noul": self.enough_p if self.enough_after in seen else 0.2}}
            if "next_check" in q:
                offered = list(q["next_check"]["criteria"])
                pick = next((c for c in self.picks if c in offered), triage.NONE)
                answers["next_check"] = choice(peaked(offered, pick))
        else:
            answers = {}
            for key, question in q.items():
                text = question["instructions"]
                if key.startswith("support_"):
                    yes = any(f"`{c}`" in text for c in self.support_yes)
                else:
                    yes = self.ruled_out == "all" or any(f"`{h}`" in text for h in self.ruled_out)
                answers[key] = {"type": "noul", "noul": 0.9 if yes else 0.1}
        return {"model": triage.MODEL, "answers": answers,
                "usage": {"input_tokens": 1000, "output_tokens": 50}}, 12.0


def asked(jev):
    return lambda payload: jev(payload)


def run(jev=None, **kw):
    # The tests below pin policy v1 (abstains without a verified candidate); class V2
    # tests the default, v2, which always answers.
    kw.setdefault("policy", "v1")
    return agent.run_agent_trial(SCENARIO, jev or FakeJev(), **kw)


class Investigation(unittest.TestCase):
    def test_concludes_only_after_verification(self):
        jev = FakeJev()
        t = run(jev)
        self.assertEqual(t["status"], "ok")
        self.assertEqual(t["observed"], [VCHECK, "cron-history"])
        d = t["diagnosis"]
        self.assertEqual((d["decision"], d["hypothesis"], d["stop_reason"]), ("diagnosis", CAUSE, "concluded"))
        self.assertEqual(d["evidence"], ["cron-history"])  # only what was judged to support it
        self.assertEqual(d["unresolved_alternatives"], [])
        self.assertIn("best_explanation", d["probabilities"])
        self.assertEqual(t["final"]["hypothesis"], CAUSE)
        # Round 1 and 2 ask one request each; round 3 adds the dependent verification.
        self.assertEqual([[c["kind"] for c in r["calls"]] for r in t["rounds"]],
                         [["round"], ["round"], ["round", "verify"]])
        self.assertEqual(t["usage"]["jev_calls"], 4)
        self.assertEqual(t["usage"]["jev_input_tokens"], 4000)
        self.assertEqual((t["usage"]["llm_calls"], t["usage"]["input_tokens"]), (0, 0))
        self.assertTrue(all(c["started_at"] for r in t["rounds"] for c in r["calls"]))

    def test_scored_on_the_shared_rubric_without_text_credit(self):
        s = rca.score_trial(run(), TRUTH)
        self.assertTrue(s["correct_hypothesis"])
        self.assertTrue(s["evidence_valid"])  # cited a mechanism check it ran, no trap
        self.assertIsNone(s["found_mechanism"])  # no text: not scored, never earned
        self.assertIsNone(s["named_component"])
        self.assertFalse(s["abstained"])
        self.assertTrue(s["jev_changed_direction"])

    def test_a_high_ranking_alone_never_ends_it(self):
        # The cause leads from the first check, but support and sufficiency never come.
        jev = FakeJev(picks=list(SCENARIO["checks"]), supported_after="never", enough_after="never")
        t = run(jev)
        self.assertEqual(t["diagnosis"]["decision"], "abstain")
        self.assertEqual(t["diagnosis"]["stop_reason"], "check_budget_exhausted")
        self.assertEqual(len(t["observed"]), rca.MAX_CHECKS)
        self.assertFalse(any(c["kind"] == "verify" for r in t["rounds"] for c in r["calls"]))
        self.assertIsNone(t["final"])
        self.assertTrue(t["abstained"])
        s = rca.score_trial(t, TRUTH)
        self.assertTrue(s["abstained"])
        self.assertIsNone(s["correct_hypothesis"])

    def test_disagreeing_answers_are_a_conflict_not_a_conclusion(self):
        answers = {"best_explanation": peaked(HYPS, CAUSE), "supported": peaked([*HYPS, "none"], "dns"),
                   "most_contradicted": peaked([*HYPS, "none"], "none"), "enough_evidence": 0.9}
        cand, reason, conflicts = agent.candidate_from(answers, 2)
        self.assertIsNone(cand)
        self.assertIn("disagree", reason)
        self.assertTrue(conflicts)
        answers["supported"] = peaked([*HYPS, "none"], "none")
        cand, _, conflicts = agent.candidate_from(answers, 2)
        self.assertIsNone(cand)
        self.assertIn("no hypothesis is adequately supported", conflicts[0])
        answers["supported"] = peaked([*HYPS, "none"], CAUSE)
        answers["most_contradicted"] = peaked([*HYPS, "none"], CAUSE)
        self.assertIsNone(agent.candidate_from(answers, 2)[0])  # also the most contradicted
        answers["most_contradicted"] = peaked([*HYPS, "none"], "deploy")
        self.assertEqual(agent.candidate_from(answers, 2)[0], CAUSE)
        self.assertIsNone(agent.candidate_from(answers, 0)[0])  # no evidence, no conclusion

    def test_verification_blocks_until_support_and_alternatives(self):
        # Nothing supports the candidate: keep investigating, then abstain at the budget.
        t = run(FakeJev(picks=list(SCENARIO["checks"]), support_yes=()))
        self.assertEqual(t["diagnosis"]["decision"], "abstain")
        reasons = [r["reason"] for r in t["rounds"] if r["verification"]]
        self.assertTrue(reasons and all("directly supports" in r for r in reasons))
        # A plausible alternative not ruled out blocks the conclusion.
        verdict = agent.verdict(CAUSE, {"cron-history": 0.9}, {"deploy": 0.2, "dns": 0.9},
                                {CAUSE: 0.6, "deploy": 0.3, "dns": 0.1})
        self.assertFalse(verdict[0])
        self.assertEqual(verdict[2], ["deploy"])
        # An implausible alternative (best_explanation under PLAUSIBLE_MIN) does not block.
        self.assertTrue(agent.verdict(CAUSE, {"cron-history": 0.9}, {"dns": 0.1},
                                      {CAUSE: 0.95, "dns": 0.05})[0])

    def test_running_out_of_checks_never_verifies_a_diagnosis(self):
        # Regression: the same answers used to fail with checks left and pass with none.
        # Now the budget runs out into an abstention that names the candidate as tentative.
        def trial(max_checks):
            jev = FakeJev(picks=[VCHECK, "pool-metrics", "cron-history", "active-queries"],
                          supported_after=VCHECK, enough_after=VCHECK, support_yes=("cron-history",),
                          ruled_out=("dns",), lead_p=0.45)  # every alternative keeps 0.11: plausible
            return run(jev, max_checks=max_checks)
        for max_checks in (3, 4):
            t = trial(max_checks)
            d = t["diagnosis"]
            self.assertEqual((d["decision"], d["hypothesis"]), ("abstain", None), max_checks)
            self.assertEqual(d["stop_reason"], "check_budget_exhausted_unverified")
            self.assertEqual(d["tentative_hypothesis"], CAUSE)
            self.assertIn("deploy", d["unresolved_alternatives"])
            self.assertEqual(d["evidence"], ["cron-history"])
            self.assertIsNone(t["final"])
            s = rca.score_trial(t, TRUTH)
            self.assertTrue(s["abstained"])
            self.assertIsNone(s["correct_hypothesis"])  # never credited as a diagnosis
            self.assertTrue(s["tentative_correct"])  # reported apart

    def test_no_useful_check_means_abstain(self):
        t = run(FakeJev(picks=[VCHECK], supported_after="never"))
        self.assertEqual(t["observed"], [VCHECK])
        self.assertEqual(t["diagnosis"]["stop_reason"], "no_useful_check")

    def test_forced_mode_runs_the_key_check_first_after_asking(self):
        jev = FakeJev(picks=["cron-history", VCHECK])
        t = run(jev, forced=True)
        self.assertEqual(t["observed"][0], VCHECK)
        self.assertTrue(t["rounds"][0]["action"]["forced"])
        self.assertEqual(t["states"][0]["check"], None)  # Jev's prior before any check is kept


class V2(unittest.TestCase):
    """Policy v2: answers the way the LLM setups must, and says whether it verified."""

    def v2(self, jev=None, **kw):
        return agent.run_agent_trial(SCENARIO, jev or FakeJev(), policy="v2", **kw)

    def test_policy_is_recorded(self):
        t = self.v2(FakeJev(lead_p=0.95))
        self.assertEqual((t["policy"], t["agent_version"]), ("v2", 2))
        self.assertEqual(agent.run_agent_trial(SCENARIO, FakeJev(), policy="v1")["agent_version"], 1)
        with self.assertRaises(ValueError):
            agent.run_agent_trial(SCENARIO, FakeJev(), policy="v9")

    def test_early_stop_needs_high_agreement_and_verification_not_enough_evidence(self):
        # enough_evidence stays low (the v1 blocker); v2 stops once both rankings pass
        # STOP_MIN and verification holds.
        jev = FakeJev(lead_p=0.95, enough_after="never")
        jev_sup = jev.__call__

        def sure(payload):
            resp, ms = jev_sup(payload)
            if "supported" in resp["answers"] and "cron-history" in jev.observed(payload):
                resp["answers"]["supported"] = choice(peaked([*HYPS, triage.NONE], CAUSE, 0.95))
            return resp, ms
        t = self.v2(sure)
        d = t["diagnosis"]
        self.assertEqual((d["hypothesis"], d["stop_reason"], d["verified"]), (CAUSE, "verified", True))
        self.assertEqual(t["observed"], [VCHECK, "cron-history"])
        # At 0.8 agreement (below STOP_MIN) it does not stop early.
        answers = {"best_explanation": peaked(HYPS, CAUSE, 0.8), "supported": peaked([*HYPS, "none"], CAUSE, 0.95),
                   "most_contradicted": peaked([*HYPS, "none"], "deploy"), "enough_evidence": 0.1}
        self.assertIsNone(agent.candidate_from(answers, 2, "v2")[0])
        answers["best_explanation"] = peaked(HYPS, CAUSE, 0.95)
        self.assertEqual(agent.candidate_from(answers, 2, "v2")[0], CAUSE)
        self.assertIsNone(agent.candidate_from(answers, 2, "v1")[0])  # v1 still wants enough_evidence

    def test_out_of_checks_it_answers_anyway_and_labels_it_unverified(self):
        jev = FakeJev(picks=list(SCENARIO["checks"]), supported_after="never", support_yes=())
        t = self.v2(jev, max_checks=3)
        d = t["diagnosis"]
        self.assertEqual((d["decision"], d["hypothesis"]), ("diagnosis", CAUSE))
        self.assertEqual((d["stop_reason"], d["verified"], d["evidence"]), ("check_budget_exhausted", False, []))
        self.assertFalse(t["abstained"])
        self.assertEqual(t["rounds"][-1]["calls"][-1]["kind"], "verify")  # the answer was checked
        s = rca.score_trial(t, TRUTH)
        self.assertTrue(s["finished"])
        self.assertTrue(s["correct_hypothesis"])  # comparable with the LLMs' forced answer
        self.assertFalse(s["verified"])
        self.assertFalse(s["evidence_valid"])  # but it cited nothing, so no supported diagnosis

    def test_no_useful_check_leads_to_a_verified_or_labelled_answer(self):
        t = self.v2(FakeJev(picks=[VCHECK, "cron-history"], supported_after="never"))
        d = t["diagnosis"]
        self.assertEqual((d["stop_reason"], d["hypothesis"]), ("no_useful_check", CAUSE))
        self.assertTrue(d["verified"])  # cron-history supports it and every alternative is ruled out
        self.assertEqual(d["evidence"], ["cron-history"])
        self.assertTrue(rca.score_trial(t, TRUTH)["evidence_valid"])

    def test_a_wrong_answer_is_an_incorrect_conclusion(self):
        t = self.v2(FakeJev(picks=[VCHECK], lead_after="never", supported_after="never"))
        s = rca.score_trial(t, TRUTH)
        self.assertEqual(t["final"]["hypothesis"], "deploy")
        self.assertTrue(s["finished"])
        self.assertFalse(s["correct_hypothesis"])

    def test_a_choice_field_that_disagrees_is_recorded_not_fatal(self):
        def odd(payload):
            resp, ms = FakeJev()(payload)
            nc = resp["answers"].get("next_check")
            if nc:
                nc["choice"] = next(k for k in nc["probabilities"] if k != nc["choice"])
            return resp, ms
        t = self.v2(odd)
        self.assertEqual(t["status"], "ok")
        self.assertTrue(t["warnings"] and "not the most probable" in t["warnings"][0])
        self.assertEqual(run(odd)["status"], "jev_error")  # v1 unchanged

    def test_round_budget_answers_from_the_last_evaluated_state(self):
        t = self.v2(FakeJev(supported_after="never"), max_rounds=2)
        self.assertEqual(t["diagnosis"]["stop_reason"], "round_budget_exhausted")
        self.assertIsNotNone(t["final"])
        self.assertEqual(len(t["rounds"]), 2)

    def test_a_failed_final_verification_is_infrastructure(self):
        good = FakeJev(picks=[VCHECK], supported_after="never")

        def verify_down(payload):
            if "best_explanation" not in payload["questions"]:
                raise triage.JevError("timeout")
            return good(payload)
        t = self.v2(verify_down)
        self.assertEqual(t["status"], "jev_error")
        self.assertTrue(rca.score_trial(t, TRUTH)["infra_failure"])

    def test_v1_and_v2_land_in_separate_columns(self):
        base = {"model": f"jev:{triage.MODEL}", "scenario": "pool_etl_cron",
                "scenario_digest": rca.scenario_digest(SCENARIO), "jev_model": triage.MODEL}
        traces = [dict(run(), **base), dict(self.v2(), **base)]
        table, _, _ = agent.compare(traces, {})
        self.assertEqual(sorted(k.split("agent ")[-1] for k in table), ["v1]", "v2]"])


class V3(unittest.TestCase):
    """Policy v3: an alternative that was ever plausible must be ruled out, not outranked."""

    def jev(self, **kw):
        # Before any check the deploy leads (0.8); after the first check the cause
        # takes everything, so the deploy's current best_explanation is ~0.
        kw.setdefault("picks", ["cron-history", VCHECK, "active-queries"])
        return FakeJev(lead_after="cron-history", supported_after="cron-history", lead_p=0.95, **kw)

    def sure(self, jev):
        def ask(payload):
            resp, ms = jev(payload)
            if "supported" in resp["answers"] and "cron-history" in jev.observed(payload):
                resp["answers"]["supported"] = choice(peaked([*HYPS, triage.NONE], CAUSE, 0.95))
            return resp, ms
        return ask

    def test_default_policy_is_v3(self):
        t = agent.run_agent_trial(SCENARIO, FakeJev())
        self.assertEqual((t["policy"], t["agent_version"]), ("v3", 3))
        self.assertEqual(agent.DEFAULT_POLICY, "v3")

    def test_outranking_the_initial_suspect_is_not_ruling_it_out(self):
        # Regression for the v2 run: after one check the ranking put ~1.0 on the cause,
        # nothing was ruled out, and v2 still called the answer verified.
        v2 = agent.run_agent_trial(SCENARIO, self.sure(self.jev(ruled_out=())), policy="v2")
        self.assertEqual(v2["observed"], ["cron-history"])
        self.assertTrue(v2["diagnosis"]["verified"])
        v3 = agent.run_agent_trial(SCENARIO, self.sure(self.jev(ruled_out=())), policy="v3")
        self.assertNotEqual(v3["observed"], ["cron-history"])  # it kept investigating
        first = v3["rounds"][1]["verification"]
        self.assertIn("deploy", first["unresolved"])  # the suspect before any evidence
        self.assertFalse(v3["diagnosis"]["verified"])  # never ruled out, so never verified
        self.assertEqual(v3["diagnosis"]["hypothesis"], CAUSE)  # but it still answers, like an LLM

    def test_ruling_out_the_once_plausible_alternatives_verifies(self):
        t = agent.run_agent_trial(SCENARIO, self.sure(self.jev(ruled_out=("deploy",))), policy="v3")
        d = t["diagnosis"]
        self.assertEqual((d["verified"], d["stop_reason"], t["observed"]), (True, "verified", ["cron-history"]))
        # Alternatives never plausible (peak under PLAUSIBLE_MIN) need no ruling out.
        self.assertEqual(d["unresolved_alternatives"], [])

    def test_verdict_uses_the_peak_when_given(self):
        args = (CAUSE, {"cron-history": 0.9}, {"deploy": 0.2}, {CAUSE: 0.99, "deploy": 0.01})
        self.assertTrue(agent.verdict(*args)[0])  # v1/v2: outranked, so exempt
        self.assertFalse(agent.verdict(*args, peak={CAUSE: 0.99, "deploy": 0.8})[0])


class Budgets(unittest.TestCase):
    def test_check_budget_and_no_duplicates(self):
        jev = FakeJev(picks=list(SCENARIO["checks"]) * 2, supported_after="never")
        t = run(jev, max_checks=3)
        self.assertEqual(len(t["observed"]), 3)
        self.assertEqual(len(set(t["observed"])), 3)
        for p in jev.payloads:  # a run check is never offered again
            offered = set(p["questions"].get("next_check", {}).get("criteria", {}))
            self.assertFalse(offered & set(jev.observed(p)))

    def test_round_budget(self):
        t = run(FakeJev(supported_after="never"), max_rounds=2)
        self.assertEqual(len(t["rounds"]), 2)
        self.assertEqual(t["diagnosis"]["stop_reason"], "round_budget_exhausted")
        self.assertTrue(t["abstained"])

    def test_a_check_off_the_menu_is_rejected(self):
        def rogue(payload):
            resp, ms = FakeJev()(payload)
            if "next_check" in resp["answers"]:
                resp["answers"]["next_check"] = choice({"rm -rf /": 0.9, triage.NONE: 0.1})
            return resp, ms
        t = run(rogue)
        self.assertEqual(t["status"], "jev_error")
        self.assertEqual(t["observed"], [])
        self.assertIn("unexpected option", t["error"])


class Failures(unittest.TestCase):
    def test_api_failure_is_infrastructure_not_diagnosis(self):
        def down(payload):
            raise triage.JevError("HTTP 503")
        t = run(down)
        self.assertEqual(t["status"], "jev_error")
        self.assertIsNone(t["diagnosis"])
        s = rca.score_trial(t, TRUTH)
        self.assertTrue(s["infra_failure"])
        self.assertFalse(s["abstained"])
        self.assertEqual(t["usage"]["jev_calls"], 1)

    def test_malformed_answers_and_a_failed_verification_call(self):
        def missing(payload):
            resp, ms = FakeJev()(payload)
            resp["answers"].pop("supported", None)
            return resp, ms
        self.assertEqual(run(missing)["status"], "jev_error")
        good = FakeJev()

        def verify_down(payload):
            if "best_explanation" not in payload["questions"]:
                raise triage.JevError("timeout")
            return good(payload)
        t = run(verify_down)
        self.assertEqual(t["status"], "jev_error")
        self.assertIn("timeout", t["error"])

    def test_the_cli_replaces_an_infra_failure_on_resume(self):
        base = {"scenario": "pool_etl_cron", "scenario_digest": "d", "model": "jev:j", "setup": "jev-agent",
                "forced": False, "trial": 1, "harness_version": 4}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.jsonl")
            with open(path, "w") as f:
                f.write(json.dumps(dict(base, status="jev_error")) + "\n")
                f.write(json.dumps(dict(base, status="ok")) + "\n")
            self.assertEqual([t["status"] for t in rca.read_traces(path)], ["ok"])


class Evidence(unittest.TestCase):
    def test_query_status_matches_every_trap_and_nothing_else(self):
        for name in rca.scenario_names():
            scenario, truth = rca.load_scenario(name), rca.load_ground_truth(name)
            for c, check in scenario["checks"].items():
                status = agent.query_status(check["result"])
                self.assertEqual(status != "ok", c in truth["traps"], f"{name}: {c} -> {status}")

    def test_failed_and_empty_queries_are_never_evidence(self):
        traps = TRUTH["traps"]
        jev = FakeJev(picks=[*traps, VCHECK, "cron-history"], support_yes=(*traps, "cron-history"))
        t = run(jev)
        d = t["diagnosis"]
        self.assertEqual(d["decision"], "diagnosis")
        self.assertFalse(set(d["evidence"]) & set(traps))
        self.assertEqual(set(d["excluded_evidence"]), set(traps))
        verify = [p for p in jev.payloads if "best_explanation" not in p["questions"]]
        for p in verify:
            text = json.dumps(p["questions"])
            for trap in traps:
                self.assertNotIn(f"`{trap}`", text)  # never even asked about
        self.assertFalse(rca.score_trial(t, TRUTH)["cited_trap"])
        self.assertEqual(rca.score_trial(t, TRUTH)["traps_run"], len(traps))

    def test_jev_sees_only_observed_results(self):
        for name in rca.scenario_names():
            scenario = rca.load_scenario(name)
            order = list(scenario["checks"])
            jev = FakeJev(picks=order, supported_after="never")
            agent.run_agent_trial(scenario, jev)
            for p in jev.payloads:
                seen = jev.observed(p)
                blob = json.dumps(p)
                for c in order:
                    result = scenario["checks"][c]["result"]
                    self.assertEqual(json.dumps(result)[1:-1] in blob, c in seen, f"{name}: {c}")


class GroundTruthLeak(unittest.TestCase):
    """Nothing from a truth file, and not the key-check designation, may reach Jev."""

    def test_no_payload_contains_truth_or_key_check_metadata(self):
        for name in rca.scenario_names():
            scenario, truth = rca.load_scenario(name), rca.load_ground_truth(name)
            secrets = [truth["mechanism"], *[n["notes"] for n in truth["check_notes"].values() if "notes" in n]]
            everything = list(scenario["checks"])
            payloads = [agent.round_payload(scenario, everything[:k]) for k in range(len(everything) + 1)]
            payloads += [agent.verify_payload(scenario, everything, h)[0] for h in scenario["hypotheses"]]
            for p in payloads:
                blob = json.dumps(p)
                for secret in secrets:
                    self.assertNotIn(secret, blob, name)
                for word in ("version_check", "decoy", "TRAP", "NOISE", "ground truth", "mechanism_keywords",
                             "_origin", '"_source"', '"traps"'):
                    self.assertNotIn(word, blob, f"{name}: {word}")
                self.assertEqual(set(p["state"]),
                                 {"incident", "initial_context", "guidance", "hypotheses", "observations"})

    def test_a_trial_never_reads_a_truth_file(self):
        real_open = open

        def guarded(path, *a, **k):
            if str(path).endswith(".truth.json"):
                raise AssertionError(f"the trial read {path}")
            return real_open(path, *a, **k)
        scenario = rca.load_scenario()
        with mock.patch("builtins.open", guarded):
            agent.run_agent_trial(scenario, FakeJev())

    def test_the_tested_questions_are_kept(self):
        q = agent.round_payload(SCENARIO, [VCHECK])["questions"]
        self.assertEqual(q["best_explanation"]["instructions"], rca.BEST_Q)
        self.assertEqual(set(q["best_explanation"]["criteria"]), set(HYPS))  # no "none": unchanged
        self.assertEqual(q["most_contradicted"]["instructions"], rca.CONTRA_Q)
        self.assertEqual(q["supported"]["type"], "choice")
        self.assertIn(triage.NONE, q["supported"]["criteria"])
        self.assertEqual(q["enough_evidence"]["type"], "noul")
        self.assertNotIn(VCHECK, q["next_check"]["criteria"])
        # Same observations as the jev setup's payload: comparable evidence.
        self.assertEqual(agent.round_payload(SCENARIO, [VCHECK])["state"]["observations"],
                         rca.build_jev_payload(SCENARIO, [VCHECK])["state"]["observations"])

    def test_every_question_can_see_the_hypotheses(self):
        # Regression: Jev judges each question alone against the shared state, and the
        # hypotheses used to live only in some questions' criteria. Replacing them left
        # enough_evidence's and next_check's inputs unchanged.
        other = dict(SCENARIO, hypotheses={f"h{i}": f"Something else entirely, number {i}." for i in range(6)})
        cand = {"real": list(SCENARIO["hypotheses"])[1], "other": "h1"}
        for build in (lambda sc, k: agent.round_payload(sc, [VCHECK]),
                      lambda sc, k: agent.verify_payload(sc, [VCHECK, "cron-history"], cand[k])[0]):
            real, fake = build(SCENARIO, "real"), build(other, "other")
            for key in real["questions"]:
                if key not in fake["questions"]:
                    continue
                seen = lambda p: json.dumps([p["state"], p["questions"][key]], sort_keys=True)
                self.assertNotEqual(seen(real), seen(fake), key)
            self.assertEqual(real["state"]["hypotheses"], SCENARIO["hypotheses"])


class ExplanationWriter(unittest.TestCase):
    def writer(self, obj=None, fail=False):
        calls = []

        def ask(system, messages):
            calls.append(messages[0]["content"])
            if fail:
                raise rca.ModelError("HTTP 429")
            return "```json\n" + json.dumps(obj) + "\n```", {}, {"input_tokens": 300, "output_tokens": 60}
        return ask, calls

    def test_writer_cannot_change_the_diagnosis_and_disagreement_is_recorded(self):
        ask, calls = self.writer({"component": "checkout-api", "mechanism": "The deploy did it.",
                                  "agrees": True, "preferred_hypothesis": "deploy", "reason": "x",
                                  "hypothesis": "deploy", "evidence": ["deployment-log"]})
        t = run(explainer=ask)
        self.assertEqual(t["final"]["hypothesis"], CAUSE)
        self.assertEqual(t["diagnosis"]["hypothesis"], CAUSE)
        self.assertEqual(t["final"]["evidence"], ["cron-history"])
        self.assertEqual(agent._digest(t["diagnosis"]), t["diagnosis_digest"])
        self.assertFalse(t["explanation"]["agrees"])  # named another cause: a disagreement
        self.assertEqual(t["explanation"]["preferred_hypothesis"], "deploy")
        self.assertEqual(t["usage"]["llm_calls"], 1)
        self.assertIn("Frozen diagnosis", calls[0])
        for c in SCENARIO["checks"]:
            if c not in t["observed"]:
                self.assertNotIn(SCENARIO["checks"][c]["result"], calls[0])

    def test_explanation_is_scored_apart(self):
        ask, _ = self.writer({"component": "analytics-etl", "agrees": True, "preferred_hypothesis": None,
                              "mechanism": "The ETL cron job moved to peak hours and held the pool's connections."})
        t = run(explainer=ask)
        s = rca.score_trial(t, TRUTH)
        self.assertIsNone(s["found_mechanism"])
        self.assertTrue(s["explanation_mechanism"])
        self.assertTrue(t["explanation"]["agrees"])

    def test_a_failed_writer_leaves_the_diagnosis(self):
        ask, _ = self.writer(fail=True)
        t = run(explainer=ask)
        self.assertEqual(t["status"], "ok")
        self.assertEqual(t["final"]["hypothesis"], CAUSE)
        self.assertIn("429", t["explanation"]["error"])

    def test_a_writer_that_edits_the_trace_is_caught(self):
        t = run()

        def sneaky(system, messages):
            t["diagnosis"]["hypothesis"] = "deploy"
            return "{}", {}, {}
        with self.assertRaises(AssertionError):
            agent.explain(SCENARIO, t, sneaky)


class Comparison(unittest.TestCase):
    def llm_trace(self, setup="alone", status="ok", correct=True):
        final = {"hypothesis": CAUSE if correct else "deploy", "component": "analytics-etl",
                 "mechanism": "cron job held connections", "evidence": ["cron-history"]}
        states = [{"check": None, "model_beliefs": None, "jev": None, "jev_error": None},
                  {"check": "cron-history", "model_beliefs": None,
                   "jev": {"usage": {"input_tokens": 1200}} if setup == "jev" else None, "jev_error": None}]
        return {"setup": setup, "status": status, "states": states, "observed": ["cron-history"],
                "final": final if status == "ok" else None, "model": "openai:x/y", "scenario": "pool_etl_cron",
                "scenario_digest": rca.scenario_digest(SCENARIO), "seconds": 80.0, "jev_model": triage.MODEL,
                "usage": {"input_tokens": 8000, "output_tokens": 2000, "jev_calls": 1 if setup == "jev" else 0}}

    def agent_trace(self):
        return dict(run(), model=f"jev:{triage.MODEL}", scenario="pool_etl_cron",
                    scenario_digest=rca.scenario_digest(SCENARIO), jev_model=triage.MODEL)

    def test_three_systems_with_infra_apart_and_costs(self):
        pricing = {f"jev:{triage.MODEL}": {"input_per_m": 0.042, "output_per_m": 0.0},
                   "openai:x/y": {"input_per_m": 1.0, "output_per_m": 2.0}}
        traces = [self.llm_trace(), self.llm_trace(status="model_error"), self.llm_trace("jev", correct=False),
                  self.agent_trace()]
        table, digests, _ = agent.compare(traces, pricing)
        col = lambda prefix: next(v for k, v in table.items() if k.startswith(prefix + " ["))
        alone, withjev = col("x/y"), col("x/y + Jev")
        jev = col(f"Jev investigates ({triage.MODEL})")
        self.assertEqual((alone["trials"], alone["infra"], alone["correct"]), ("2", "1", "1/1"))
        self.assertEqual(withjev["incorrect"], "1/1")
        self.assertEqual(alone["cost"], f"{(8000 * 1 + 2000 * 2) / 1e6:.5f}")
        self.assertEqual(withjev["cost"], f"{(8000 + 4000 + 1200 * 0.042) / 1e6:.5f}")
        self.assertEqual(alone["llm_calls"], ">=2.0")  # an old trace: calls estimated from states
        self.assertEqual(jev["llm_calls"], "0.0")
        self.assertEqual(jev["cost"], f"{4000 * 0.042 / 1e6:.5f}")
        self.assertEqual(jev["correct"], "1/1")
        self.assertEqual(jev["mechanism"], "-")  # no text, nothing to score
        self.assertEqual(len({frozenset(d) for d in digests.values()}), 1)
        table, _, _ = agent.compare(traces, {})
        self.assertEqual(next(v for k, v in table.items() if k.startswith("x/y ["))["cost"],
                         "unpriced")  # a missing price is never zero

    def test_the_pricing_file_prices_jev_and_leaves_unknowns_unpriced(self):
        pricing = agent.load_pricing()
        self.assertEqual(pricing[f"jev:{triage.MODEL}"]["input_per_m"], triage.USD_PER_M_INPUT_TOKENS)
        self.assertTrue(all("source" in v for v in pricing.values()))

    def test_different_run_conditions_are_never_pooled(self):
        # Regression: forced and free trials with different budgets used to merge into
        # one column with no warning.
        free = self.llm_trace()
        forced = dict(self.llm_trace(), forced=True, budgets={"max_checks": 4})
        table, digests, conds = agent.compare([free, forced], {})
        self.assertEqual(len(table), 2)
        self.assertIn("x/y [free, 6* checks]", table)  # an older trace: budget assumed, marked
        self.assertIn("x/y [forced, 4 checks]", table)
        warnings = agent.mismatches(digests, conds)
        self.assertTrue(any("different conditions" in w for w in warnings))
        buf = io.StringIO()
        agent.print_compare([free, forced], {}, out=buf)
        self.assertIn("WARNING: the columns ran under different conditions", buf.getvalue())
        agent_trace = self.agent_trace()
        _, digests, conds = agent.compare([dict(free, budgets={"max_checks": 6}), agent_trace], {})
        self.assertFalse([w for w in agent.mismatches(digests, conds) if not w.startswith("*")])
        page = __import__("rca_report").render([free, forced, agent_trace])
        self.assertIn("Mismatch", page)

    def test_llm_traces_record_their_budget(self):
        model = lambda system, messages: ("DONE {}", {"role": "assistant", "content": ""}, {})
        self.assertEqual(rca.run_trial(SCENARIO, "alone", model, max_checks=4)["budgets"], {"max_checks": 4})

    def test_digest_mismatch_is_flagged(self):
        other = dict(self.llm_trace(), scenario_digest="old")
        buf = io.StringIO()
        agent.print_compare([other, self.agent_trace()], {}, out=buf)
        self.assertIn("WARNING", buf.getvalue())

    def test_html_section_names_jev_as_the_investigator(self):
        import rca_report
        page = rca_report.render([self.llm_trace(), self.agent_trace()])
        self.assertIn("Jev as the investigator", page)
        self.assertIn("Jev investigates", page)
        self.assertIn("TypeSafe", page)
        (_, s, _), = rca_report.scored([self.agent_trace()])
        self.assertIsNone(s["resolved"])  # not comparable to the text-scored resolved


class CLI(unittest.TestCase):
    def run_main(self, *argv, env=None):
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.dict(os.environ, env or {}, clear=True):
            code = rca.main(list(argv))
        return code, buf.getvalue()

    def test_dry_run_plans_jev_agent_once_per_scenario_not_per_model(self):
        code, out = self.run_main("--dry-run", "--models", "anthropic,openai:x", "--setup", "alone,jev-agent")
        self.assertEqual(code, 0)
        plan = [l.split() for l in out.splitlines() if l.startswith("  trial ")]
        n = len(rca.scenario_names())
        self.assertEqual(sum(1 for p in plan if p[-1] == "jev-agent"), n)
        self.assertEqual(sum(1 for p in plan if p[-1] == "alone"), 2 * n)
        self.assertTrue(all(p[-2] == f"jev:{triage.MODEL}" for p in plan if p[-1] == "jev-agent"))
        self.assertIn("jev-agent round request", out)
        self.assertIn('"enough_evidence"', out)

    def test_run_writes_traces_credited_to_jev_and_compares(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "t.jsonl")
            with mock.patch.object(agent, "jev_asker", return_value=FakeJev()):
                code, text = self.run_main("--setup", "jev-agent", "--scenarios", "pool_etl_cron", "--out", out,
                                           env={"TYPESAFE_API_KEY": "t"})
            self.assertEqual(code, 0)
            self.assertIn("answer=batch-job", text)
            (t,) = rca.read_traces(out)
            self.assertEqual((t["setup"], t["model"]), ("jev-agent", f"jev:{triage.MODEL}"))
            self.assertEqual(t["scenario_digest"], rca.scenario_digest(SCENARIO))
            self.assertEqual(t["budgets"]["max_checks"], rca.MAX_CHECKS)
            code, text = self.run_main("--compare", out, os.path.join(rca.BASE, "rca_results",
                                                                      "2026-09-28-v4-deepseek-hard", "free.jsonl"))
            self.assertEqual(code, 0)
            self.assertIn("Investigator comparison", text)
            self.assertIn("Jev investigates", text)
            self.assertIn("unpriced", text)

    def test_jev_agent_needs_only_the_jev_key(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_main("--setup", "jev-agent", "--scenarios", "pool_etl_cron")
        self.assertIn("TYPESAFE_API_KEY", str(cm.exception.code))

    def test_explain_model_needs_its_key(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_main("--setup", "jev-agent", "--explain-model", "anthropic", env={"TYPESAFE_API_KEY": "t"})
        self.assertIn("ANTHROPIC_API_KEY", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
