#!/usr/bin/env python3
"""The jev-agent setup: Jev runs the RCA investigation itself.

No generative model is in the loop. Jev answers narrow, bounded questions about
the evidence observed so far; plain code turns the answers into one action per
round (run a check, conclude, or abstain), validates it, runs the check against
the frozen scenario, and repeats. The hypotheses and the check menu are the
benchmark's own, the same ones the LLM setups get.

What Jev sees is what the LLM setups see: the incident, the initial context, the
guidance about failed queries, the hypotheses, the check descriptions and the
results of the checks already run. Never an unrun check's result, the truth file,
the scoring keywords, or which check is the scenario's key check.

Each round asks one batched request against the current evidence (questions in one
request cannot see each other's answers):

  best_explanation   Choice over the hypotheses (the jev setup's question, unchanged)
  most_contradicted  Choice over the hypotheses and none (unchanged)
  supported          Choice over the hypotheses and "none adequately supported" (new)
  enough_evidence    yes/no: is the evidence enough to name one cause (new)
  next_check         Choice over the checks not yet run, and none (new)

Only when code finds a candidate diagnosis in those answers does a second request
follow, because its questions depend on the candidate: for each usable observed
result, does it directly support the candidate (yes/no per item), and for each
other hypothesis, has the evidence ruled it out (yes/no per item). Per-item yes/no
is deliberate here: these are independent judgments about one fixed proposal, not
a comparison between hypotheses, which stays a Choice.

The decision rule (all in code, thresholds fixed before any live run):

  - No evidence yet: never conclude.
  - A candidate needs best_explanation and supported to agree on the same
    hypothesis, supported >= SUPPORT_MIN on it, enough_evidence >= ENOUGH_MIN,
    and the candidate not being the most contradicted hypothesis. Any
    disagreement is recorded as a conflict and the investigation continues.
    A high ranking alone never ends it.
  - Verification: at least one usable observed result must directly support the
    candidate (P >= EVIDENCE_MIN), and every alternative still plausible
    (best_explanation >= PLAUSIBLE_MIN) must be ruled out (P >= RULED_OUT_MIN).
    The test does not loosen when checks run out: a candidate that fails it at the
    budget ends in an abstention that names it as tentative, never a diagnosis.
  - Otherwise run next_check's top pick. If Jev's top pick is none (no remaining
    check would help) the investigation abstains. So does running out of checks
    or decision rounds without a verified candidate.

That is policy v1. Policy v2 (the default) keeps the questions and verification
(and v3, the default)
but answers the way the LLM setups must: it stops early only on a verified
candidate (best_explanation and supported both >= STOP_MIN, no enough_evidence
gate), and otherwise answers with best_explanation's top pick when the checks or
rounds run out or no check is judged useful, verifies that answer and labels it.
v3 tightens verification: an alternative that was ever plausible must be ruled
out by a specific result, not merely outranked.

Failed and empty queries are recognized by code from the visible result text and
are never used as supporting evidence.

An optional LLM explanation may be written after the diagnosis is frozen. The
writer gets a copy; its text, disagreement and cost are recorded apart, and it
cannot change the diagnosis.

guide/rca.md ("Jev as the investigator") has the design and the commands.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from datetime import datetime, timezone

import rca_experiment as rca
import triage

AGENT_SETUP = "jev-agent"
# A policy is a decision rule; its version goes in every trace, and --compare never
# pools versions. v1 is kept so its results stay reproducible; v2 is the default.
POLICIES = {"v1": 1, "v2": 2, "v3": 3, "v4": 4, "v5": 5, "v6": 6}
DEFAULT_POLICY = "v6"
ANSWERING = ("v2", "v3", "v4", "v5", "v6")  # policies that always answer, like the LLM setups
CHALLENGE = ("v4", "v5", "v6")  # policies that challenge the leading hypothesis
# v6: after finding where the fault is, spend up to MECH_CHECKS checks finding what
# failed inside it. On recorded incidents v5 named the right service but, in 21 of 22
# unsupported answers, never ran a check for the fault's kind (17 of them had used the
# whole budget on where). The where phase gets max_checks - MECH_CHECKS.
MECHANISM = ("v6",)
MECH_CHECKS = 2
CHALLENGE_MIN = 0.5  # a leader at least this strong gets a challenge check
# Policies whose verification lets an alternative count as ruled out once the current
# ranking pushes it under PLAUSIBLE_MIN. v1 and v2 by design; v4 by mistake (its code
# kept a `policy == "v3"` test, so it verified under v2's rule), kept so its recorded
# run reproduces. Every other policy, including any new one, uses the strict rule.
OUTRANKED_IS_RULED_OUT = ("v1", "v2", "v4")
AGENT_VERSION = POLICIES[DEFAULT_POLICY]

# Decision thresholds. Chosen before any live run and not tuned on the scenarios.
ENOUGH_MIN = 0.7     # P(yes) that the evidence is enough to name one cause
SUPPORT_MIN = 0.5    # P that the candidate is the adequately supported hypothesis
EVIDENCE_MIN = 0.5   # P(yes) that an observed result directly supports the candidate
RULED_OUT_MIN = 0.5  # P(yes) that an alternative is ruled out
PLAUSIBLE_MIN = 0.1  # best_explanation mass that keeps an alternative plausible
# v2 only. Set after reading the v1 results on the five hard scenarios (where the
# true cause reached 0.98-1.0 on both questions by the end), so those five are not
# a clean test of it: the three hand-written scenarios are the held-out check.
STOP_MIN = 0.9       # best_explanation and supported both at least this to stop early

SUPPORTED_Q = ("Which hypothesis, if any, is directly supported by specific observed results: a "
               "result that shows the change, failure or mechanism the hypothesis names, not one that "
               "is merely consistent with it or that only rules out other hypotheses? Failed or empty "
               "queries support nothing. Pick 'none' if no observed result positively points to one.")
NONE_SUPPORTED = "None adequately supported: no observed result positively points to any one hypothesis"
ENOUGH_Q = ("Is the evidence observed so far enough to name one of the hypotheses as the root cause, "
            "so that an experienced SRE reviewing these results would accept the diagnosis without "
            "running more checks? Answer no if a plausible alternative has not been tested or ruled "
            "out, or if the leading hypothesis is only left standing rather than shown directly.")
NEXT_Q = ("Which of these checks, none of which has been run yet, would best distinguish between the "
          "hypotheses that the evidence so far has not ruled out? Judge each check only by what its "
          "description says it measures; its result is not known. Pick 'none' only if no remaining "
          "check could change which hypothesis is best supported.")
NO_USEFUL_CHECK = "None of the remaining checks could change which hypothesis is best supported"
# v4 adds one generic sentence about informativeness to NEXT_Q (v3 wasted a check on a
# search that came back empty in 12 of 40 trials). It names no check type: code never
# down-ranks checks by how their description is worded, which would fit how this
# benchmark's traps happen to be written.
INFORMATIVE = (" Prefer a check whose likely results would clearly move the ranking either way; a "
               "broad search that may come back empty or time out usually tells you little.")
# v4: asked in its own request after the round, because it names the round's leader.
# v3 anchored on a second wrong suspect for 3+ checks in 3 of 40 trials (1 wrong answer).
CHALLENGE_Q = ("Hypothesis `{h}` currently leads: {desc}\nWhich of these checks, none of which has been "
               "run yet, would most likely show that `{h}` is wrong if it is wrong, or directly show the "
               "change or failure that a competing hypothesis names? Judge each check only by what its "
               "description says it measures; its result is not known." + INFORMATIVE +
               " Pick 'none' only if no remaining check could do either.")
NO_CHALLENGE = "No remaining check could show the leading hypothesis wrong or show a competitor directly"
MECH_Q = ("`{h}` currently looks like the root cause: {desc}\nWhich of these checks, none of which has "
          "been run yet, would best show what failed inside `{h}`: which resource, connection, "
          "dependency or process went wrong? Judge each check only by what its description says it "
          "measures; its result is not known. Pick 'none' only if no remaining check could show that.")
NO_MECH = "No remaining check could show what failed inside it"
MECH_EVIDENCE_Q = ("Proposed root cause: `{h}`: {desc}\nDoes the observed result of the check `{c}` show "
                   "what failed inside `{h}`: a specific resource, connection, dependency or process going "
                   "wrong, rather than only that `{h}` or its users are affected?")
SUPPORT_Q = ("Proposed root cause: `{h}`: {desc}\nDoes the observed result of the check `{c}` directly "
             "support this root cause? Yes only if that result shows the change, failure or mechanism "
             "that this hypothesis names. No if it is only consistent with it, only rules out another "
             "hypothesis, or says nothing about it.")
RULED_OUT_Q = ("Does the evidence observed so far rule out `{h}` ({desc}) as the root cause of this "
               "incident? Yes only if a specific observed result is inconsistent with it. Not having "
               "tested it is not ruling it out.")

_FAILED = re.compile(r"^\s*(QUERY FAILED|ERROR)\b")
_EMPTY = re.compile(r"^\s*(0 matching|0 (rows|lines) returned|No service found)", re.IGNORECASE)


def query_status(result):
    """'failed', 'empty' or 'ok', from the visible result text alone."""
    if _FAILED.match(result):
        return "failed"
    if _EMPTY.match(result):
        return "empty"
    return "ok"


# --------------------------------------------------------------------------
# What Jev may see

def visible_state(scenario, observed):
    """The agent-visible evidence state: the same fields the LLM setups see, and the
    results of the checks in observed only.

    The hypotheses are part of the state, not only of some questions' criteria: Jev
    judges each question on its own against the shared state, so enough_evidence,
    next_check and the verification questions could not otherwise see what the
    candidate causes are. (The LLM setups get the hypotheses in their first message.)"""
    incident = scenario["incident"]
    return {"incident": {"title": incident["title"], "description": incident["description"],
                         "started_at": incident["started_at"]},
            "initial_context": scenario["initial_context"],
            "guidance": scenario["guidance"],
            "hypotheses": dict(scenario["hypotheses"]),
            "observations": rca.evidence_text(scenario, observed)}


def unrun_checks(scenario, observed):
    return {c: v["description"] for c, v in scenario["checks"].items() if c not in observed}


def round_payload(scenario, observed, model=None, policy=None):
    """The batched per-round request. Every question is about the same state; none
    depends on another's answer."""
    hyps = dict(scenario["hypotheses"])
    questions = {
        "best_explanation": {"type": "choice", "instructions": rca.BEST_Q, "criteria": hyps},
        "most_contradicted": {"type": "choice", "instructions": rca.CONTRA_Q,
                              "criteria": {**hyps, triage.NONE: rca.NO_CONTRADICTION}},
        "supported": {"type": "choice", "instructions": SUPPORTED_Q,
                      "criteria": {**hyps, triage.NONE: NONE_SUPPORTED}},
        "enough_evidence": {"type": "noul", "instructions": ENOUGH_Q},
    }
    unrun = unrun_checks(scenario, observed)
    if unrun:
        questions["next_check"] = {"type": "choice",
                                   "instructions": NEXT_Q + (INFORMATIVE if policy in CHALLENGE else ""),
                                   "criteria": {**unrun, triage.NONE: NO_USEFUL_CHECK}}
    return {"model": model or triage.MODEL, "state": visible_state(scenario, observed),
            "questions": questions}


def challenge_payload(scenario, observed, leader, model=None):
    """v4's dependent request: which unrun check could overturn the leader."""
    unrun = unrun_checks(scenario, observed)
    return {"model": model or triage.MODEL, "state": visible_state(scenario, observed),
            "questions": {"challenge_check": {
                "type": "choice",
                "instructions": CHALLENGE_Q.format(h=leader, desc=scenario["hypotheses"][leader]),
                "criteria": {**unrun, triage.NONE: NO_CHALLENGE}}}}


def usable_evidence(scenario, observed):
    """Observed checks whose query completed with a result, and why the rest don't count."""
    usable, excluded = [], {}
    for c in observed:
        status = query_status(scenario["checks"][c]["result"])
        if status == "ok":
            usable.append(c)
        else:
            excluded[c] = status
    return usable, excluded


def mechanism_payload(scenario, observed, leader, model=None):
    """v6's what-failed request: which unrun check could show what failed inside the leader."""
    unrun = unrun_checks(scenario, observed)
    return {"model": model or triage.MODEL, "state": visible_state(scenario, observed),
            "questions": {"mechanism_check": {
                "type": "choice",
                "instructions": MECH_Q.format(h=leader, desc=scenario["hypotheses"][leader]),
                "criteria": {**unrun, triage.NONE: NO_MECH}}}}


def verify_payload(scenario, observed, candidate, model=None, mechanism=False):
    """The dependent request: which observed results support the candidate, and which
    alternatives the evidence rules out; with mechanism (v6's final check), also which
    results show what failed inside the candidate. Returns (payload, {key: (kind, id)})."""
    hyps = scenario["hypotheses"]
    usable, _ = usable_evidence(scenario, observed)
    questions, keys = {}, {}
    for i, c in enumerate(usable):
        key = f"support_{i}"
        questions[key] = {"type": "noul", "instructions": SUPPORT_Q.format(h=candidate, desc=hyps[candidate], c=c)}
        keys[key] = ("support", c)
    for i, h in enumerate(h for h in hyps if h != candidate):
        key = f"ruled_out_{i}"
        questions[key] = {"type": "noul", "instructions": RULED_OUT_Q.format(h=h, desc=hyps[h])}
        keys[key] = ("ruled_out", h)
    if mechanism:
        for i, c in enumerate(usable):
            key = f"mech_{i}"
            questions[key] = {"type": "noul",
                              "instructions": MECH_EVIDENCE_Q.format(h=candidate, desc=hyps[candidate], c=c)}
            keys[key] = ("mechanism", c)
    return ({"model": model or triage.MODEL, "state": visible_state(scenario, observed),
             "questions": questions}, keys)


# --------------------------------------------------------------------------
# Parsing Jev's answers

def _choice(answers, key, options, warnings=None):
    """A Choice answer's distribution. With a warnings list (v2), a 'choice' field that
    disagrees with the probabilities is recorded instead of failing the trial: code
    acts on the probabilities either way. Without one (v1), it is an error."""
    dist = triage._distribution(answers[key]["probabilities"], options, key)
    try:
        triage._validate_choice_max(answers[key], dist, key)
    except triage.JevError as e:
        if warnings is None:
            raise
        warnings.append(str(e))
    return dist


def _noul(answers, key):
    p = triage._validate_finite(answers[key]["noul"], key)
    if not 0.0 <= p <= 1.0:
        raise triage.JevError(f"{key}: probability {p} out of range")
    return p


def parse_round(resp, scenario, observed, warnings=None):
    hyps = list(scenario["hypotheses"])
    try:
        a = resp["answers"]
        out = {"best_explanation": _choice(a, "best_explanation", hyps, warnings),
               "most_contradicted": _choice(a, "most_contradicted", [*hyps, triage.NONE], warnings),
               "supported": _choice(a, "supported", [*hyps, triage.NONE], warnings),
               "enough_evidence": _noul(a, "enough_evidence"),
               "next_check": None}
        unrun = unrun_checks(scenario, observed)
        if unrun:
            out["next_check"] = _choice(a, "next_check", [*unrun, triage.NONE], warnings)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise triage.JevError(f"malformed response ({type(e).__name__}: {e})") from e
    return out


def parse_verify(resp, keys):
    """(support, ruled_out, mechanism): P(yes) per observed check, per alternative, and
    per observed check again for v6's what-failed question (empty otherwise)."""
    out = {"support": {}, "ruled_out": {}, "mechanism": {}}
    try:
        a = resp["answers"]
        for key, (kind, ident) in keys.items():
            out[kind][ident] = _noul(a, key)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise triage.JevError(f"malformed response ({type(e).__name__}: {e})") from e
    return out["support"], out["ruled_out"], out["mechanism"]


# --------------------------------------------------------------------------
# The decision rule

def candidate_from(answers, n_observed, policy="v1"):
    """(candidate or None, reason, conflicts). A ranking alone never makes a candidate.

    v1 also needs enough_evidence >= ENOUGH_MIN. v2 drops that gate (in v1 it held back
    15 of 17 abstentions whose top pick was the true cause at 0.98+) and instead needs
    best_explanation and supported both >= STOP_MIN; verification still decides."""
    best, sup, contra = answers["best_explanation"], answers["supported"], answers["most_contradicted"]
    h_best, h_sup, h_contra = rca._top(best), rca._top(sup), rca._top(contra)
    conflicts = []
    if h_sup not in (triage.NONE, h_best):
        conflicts.append(f"best_explanation picks {h_best}, supported picks {h_sup}")
    if h_contra == h_best:
        conflicts.append(f"{h_best} is both the best explanation and the most contradicted")
    if h_sup == triage.NONE and answers["enough_evidence"] >= ENOUGH_MIN:
        conflicts.append("enough_evidence says yes but no hypothesis is adequately supported")
    if n_observed == 0:
        return None, "no evidence observed yet", conflicts
    if h_sup == triage.NONE:
        return None, "no hypothesis adequately supported", conflicts
    if h_sup != h_best:
        return None, "best explanation and supported hypothesis disagree", conflicts
    if policy in ANSWERING:
        if min(best[h_best], sup[h_sup]) < STOP_MIN:
            return None, f"{h_best} below {STOP_MIN} on best_explanation or supported", conflicts
    else:
        if sup[h_sup] < SUPPORT_MIN:
            return None, f"support for {h_sup} below {SUPPORT_MIN}", conflicts
        if answers["enough_evidence"] < ENOUGH_MIN:
            return None, f"evidence judged insufficient (P = {answers['enough_evidence']:.2f})", conflicts
    if h_contra == h_best:
        return None, "the candidate is also the most contradicted hypothesis", conflicts
    return h_best, "candidate", conflicts


def verdict(candidate, support, ruled_out, best, peak=None):
    """(conclude?, supporting evidence, unresolved plausible alternatives, reason).
    The same test whatever budget is left: running out of checks never turns
    unverified evidence into a verified diagnosis.

    Which alternatives must be ruled out: v1 and v2 pass `best` (the current
    best_explanation), so an alternative the ranking has pushed under PLAUSIBLE_MIN
    is exempt, and being outranked counts as being ruled out. In the v2 run every
    verified answer left some alternative not ruled out. v3 passes `peak`, each
    hypothesis's highest best_explanation at any point in the investigation,
    starting before any evidence: once plausible, it must be ruled out by a
    specific result, not only outranked."""
    plausibility = peak if peak is not None else best
    supporting = [c for c, p in support.items() if p >= EVIDENCE_MIN]
    unresolved = [h for h, p in ruled_out.items()
                  if p < RULED_OUT_MIN and plausibility.get(h, 0) >= PLAUSIBLE_MIN]
    if not supporting:
        return False, supporting, unresolved, f"no observed result directly supports {candidate}"
    if unresolved:
        return False, supporting, unresolved, "plausible alternatives not ruled out: " + ", ".join(unresolved)
    return True, supporting, unresolved, "verified"


def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# One investigation

def jev_asker(api_key, timeout_s=15.0):
    """A callable payload -> (response, ms), reusing triage.call_jev and its retries."""
    if not api_key:
        raise rca.ModelError("TYPESAFE_API_KEY is not set")
    return lambda payload: triage.call_jev(payload, api_key, timeout_s, retries=2, max_wait_s=10.0)


def run_agent_trial(scenario, ask_jev, forced=False, max_checks=rca.MAX_CHECKS, max_rounds=None,
                    jev_model=None, explainer=None, policy=DEFAULT_POLICY):
    """Run one Jev-driven investigation.

    policy "v1": stop only on a verified candidate; otherwise abstain.
    policy "v2": answer the way the LLM setups must. Stop early only on a verified
    candidate, as in v1; when the checks run out, Jev judges no remaining check
    useful, or the rounds run out, answer anyway with best_explanation's top pick,
    verify that answer, and record whether it passed. No abstentions: the LLMs
    cannot abstain either, so the headline "correct cause" compares like with like,
    and "verified" is reported beside it.

    policy "v5": v3, plus a guard against anchoring: once a hypothesis leads at
    CHALLENGE_MIN or more, a separate request asks which unrun check could show it
    wrong (or show a competitor directly), and that check is run; and one generic
    sentence about informative checks in the check questions.

    policy "v4": what v5 was meant to be, as it actually ran: the same challenge, but
    by mistake verified under v2's weaker rule (see OUTRANKED_IS_RULED_OUT).

    policy "v3": v2, except that verification must rule out every alternative that
    was plausible at any point (its peak best_explanation >= PLAUSIBLE_MIN, from
    before the first check on), not only those still plausible now.

    ask_jev(payload) -> (response, ms). explainer, optional, is an LLM ask callable
    (system, messages) -> (text, msg, usage) used only after the diagnosis is frozen.

    The trace keeps the shape of the LLM setups' traces (states, observed, final,
    usage, seconds) so the shared scoring applies, plus the rounds and the
    structured diagnosis. states[k] holds Jev's beliefs after k checks, as both
    the investigator's belief (model_beliefs) and Jev's (jev)."""
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}")
    max_rounds = max_rounds or max_checks + 1
    checks = scenario["checks"]
    warnings = [] if policy in ANSWERING else None
    peak = {}  # v3: each hypothesis's highest best_explanation so far
    trace = {"setup": AGENT_SETUP, "agent_version": POLICIES[policy], "policy": policy,
             # Which alternatives verification must rule out: every one that was ever
             # plausible ("ever_plausible"), or only those still plausible now ("current").
             "verify_rule": "current" if policy in OUTRANKED_IS_RULED_OUT else "ever_plausible",
             "challenge": policy in CHALLENGE,
             "warnings":warnings if warnings is not None else [], "forced": forced, "status": "ok",
             "states": [], "observed": [], "rounds": [], "final": None, "abstained": False,
             "diagnosis": None, "diagnosis_digest": None, "explanation": None, "error": None,
             "budgets": {"max_checks": max_checks, "max_rounds": max_rounds},
             "usage": {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0, "jev_calls": 0,
                       "jev_input_tokens": 0, "jev_output_tokens": 0},
             "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    t0 = time.monotonic()

    def call(kind, payload):
        started = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        c0 = time.monotonic()
        try:
            resp, ms = ask_jev(payload)
        finally:
            trace["usage"]["jev_calls"] += 1
        usage = (resp.get("usage") or {}) if isinstance(resp, dict) else {}
        trace["usage"]["jev_input_tokens"] += usage.get("input_tokens", 0)
        trace["usage"]["jev_output_tokens"] += usage.get("output_tokens", 0)
        return resp, {"kind": kind, "started_at": started, "ms": round(ms),
                      "wall_ms": round((time.monotonic() - c0) * 1000),
                      "questions": sorted(payload["questions"]), "usage": usage,
                      "model": str(resp.get("model")) if isinstance(resp, dict) else None}

    def finish(decision, hypothesis, stop_reason, answers, supporting=(), support=None,
               ruled_out=None, unresolved=(), tentative=None, verified=None):
        _, excluded = usable_evidence(scenario, trace["observed"])
        diagnosis = {"decision": decision, "hypothesis": hypothesis, "evidence": list(supporting),
                     "verified": verified,
                     # An abstention that ran out of room with an unverified candidate says
                     # which one, apart: never scored as a diagnosis.
                     "tentative_hypothesis": tentative,
                     "evidence_support": support or {}, "excluded_evidence": excluded,
                     "ruled_out": ruled_out or {}, "unresolved_alternatives": list(unresolved),
                     "stop_reason": stop_reason,
                     "probabilities": answers or {}, "checks_used": len(trace["observed"]),
                     "rounds": len(trace["rounds"])}
        trace["diagnosis"] = diagnosis
        trace["diagnosis_digest"] = _digest(diagnosis)
        if decision == "diagnosis":
            trace["final"] = {"hypothesis": hypothesis, "component": "", "mechanism": "",
                              "evidence": list(supporting)}
        else:
            trace["abstained"] = True

    def verify(candidate, observed, answers, rnd, mechanism=False):
        """Ask the dependent verification request; (ok, supporting, unresolved, why,
        support, ruled_out). Raises on an infrastructure failure."""
        payload, keys = verify_payload(scenario, observed, candidate, jev_model, mechanism)
        resp, meta = call("verify", payload)
        rnd["calls"].append(meta)
        support, ruled_out, mech = parse_verify(resp, keys)
        ok, supporting, unresolved, why = verdict(candidate, support, ruled_out, answers["best_explanation"],
                                                  None if policy in OUTRANKED_IS_RULED_OUT else dict(peak))
        rnd["verification"] = {"candidate": candidate, "support": support, "ruled_out": ruled_out,
                               "supporting": supporting, "unresolved": unresolved, "result": why,
                               "mechanism": mech}
        return ok, supporting, unresolved, why, support, ruled_out

    def answer_anyway(stop_reason, answers, rnd, observed):
        """v2: the forced final answer, verified and labelled, never an abstention."""
        h = rca._top(answers["best_explanation"])
        v = rnd.get("verification") or {}
        if v.get("candidate") == h:  # already verified this round
            ok, supporting, unresolved = v["result"] == "verified", v["supporting"], v["unresolved"]
            support, ruled_out = v["support"], v["ruled_out"]
        else:
            ok, supporting, unresolved, _, support, ruled_out = verify(h, observed, answers, rnd)
        rnd["action"] = {"type": "answer", "hypothesis": h, "verified": ok}
        finish("diagnosis", h, stop_reason, answers, supporting, support, ruled_out, unresolved, verified=ok)

    # v6 keeps MECH_CHECKS of the budget for finding what failed; the rest finds where.
    where_budget = max(1, max_checks - MECH_CHECKS) if policy in MECHANISM else max_checks

    def new_round(phase):
        observed = list(trace["observed"])
        rnd = {"round": len(trace["rounds"]) + 1, "checks_observed": len(observed), "calls": [],
               "conflicts": [], "candidate": None, "verification": None, "action": None,
               "reason": None, "phase": phase}
        trace["rounds"].append(rnd)
        return observed, rnd

    def what_failed_phase():
        """v6: after the where phase answers, look inside the answer for what failed, then
        re-rank and re-verify on everything observed. Mechanism rounds do not count
        against max_rounds; they are bounded by MECH_CHECKS + 1."""
        where = dict(trace["diagnosis"])
        leader, run = where["hypothesis"], []
        try:
            for _ in range(MECH_CHECKS):
                observed, rnd = new_round("mechanism")
                unrun = unrun_checks(scenario, observed)
                if len(observed) >= max_checks or not unrun:
                    rnd["action"] = {"type": "stop_mechanism", "reason": "no checks left"}
                    break
                resp, meta = call("mechanism", mechanism_payload(scenario, observed, leader, jev_model))
                rnd["calls"].append(meta)
                try:
                    dist = _choice(resp["answers"], "mechanism_check", [*unrun, triage.NONE], warnings)
                except (KeyError, TypeError, ValueError, AttributeError) as e:
                    raise triage.JevError(f"malformed response ({type(e).__name__}: {e})") from e
                pick = rca._top(dist)
                rnd["mechanism"] = {"leader": leader, "answers": dist}
                if pick == triage.NONE:
                    rnd["action"] = {"type": "stop_mechanism", "reason": "no check could show it"}
                    break
                if pick not in checks or pick in observed:
                    trace["status"], trace["error"] = "invalid_action", f"invalid check {pick!r}"
                    return
                rnd["action"] = {"type": "check", "check": pick, "phase": "mechanism"}
                trace["observed"].append(pick)
                run.append(pick)
            # Re-rank and re-verify on everything seen, what-failed evidence included.
            observed, rnd = new_round("final")
            resp, meta = call("round", round_payload(scenario, observed, jev_model, policy))
            rnd["calls"].append(meta)
            final = parse_round(resp, scenario, observed, warnings)
            rnd["answers"] = final
            for h, p in final["best_explanation"].items():
                peak[h] = max(peak.get(h, 0.0), p)
            trace["states"].append({"check": observed[-1] if observed else None,
                                    "model_beliefs": final["best_explanation"],
                                    "jev": {"beliefs": final["best_explanation"],
                                            "contradicted": final["most_contradicted"]},
                                    "jev_error": None})
            h = rca._top(final["best_explanation"])
            ok, supporting, unresolved, _, support, ruled_out = verify(h, observed, final, rnd, mechanism=True)
            mech = rnd["verification"]["mechanism"]
            mech_ev = [c for c, p in mech.items() if p >= EVIDENCE_MIN]
            evidence = mech_ev + [c for c in supporting if c not in mech_ev]
            rnd["action"] = {"type": "answer", "hypothesis": h, "verified": ok}
            finish("diagnosis", h, where["stop_reason"], final, evidence, support, ruled_out, unresolved,
                   verified=ok)
            trace["diagnosis"].update({
                "mechanism_evidence": mech_ev, "mechanism_support": mech, "mechanism_checks_run": run,
                "where_phase": {"hypothesis": leader, "verified": where["verified"],
                                "checks_used": where["checks_used"]}})
            trace["diagnosis_digest"] = _digest(trace["diagnosis"])
        except Exception as e:  # an infrastructure failure, not a diagnosis
            trace["status"], trace["error"] = "jev_error", f"{type(e).__name__}: {e}"

    answers = None
    while True:
        if len(trace["rounds"]) >= max_rounds:
            if policy in ANSWERING and answers is not None:
                last = trace["rounds"][-1]
                try:
                    answer_anyway("round_budget_exhausted", answers, last,
                                  trace["observed"][:last["checks_observed"]])
                except Exception as e:
                    trace["status"], trace["error"] = "jev_error", f"{type(e).__name__}: {e}"
                break
            finish("abstain", None, "round_budget_exhausted", answers)
            break
        observed = list(trace["observed"])
        rnd = {"round": len(trace["rounds"]) + 1, "checks_observed": len(observed), "calls": [],
               "conflicts": [], "candidate": None, "verification": None, "action": None, "reason": None,
               "phase": "where"}
        trace["rounds"].append(rnd)
        try:
            resp, meta = call("round", round_payload(scenario, observed, jev_model, policy))
            rnd["calls"].append(meta)
            answers = parse_round(resp, scenario, observed, warnings)
        except Exception as e:  # an infrastructure failure, not a diagnosis: no LLM fallback
            trace["status"], trace["error"] = "jev_error", f"{type(e).__name__}: {e}"
            break
        rnd["answers"] = answers
        for h, p in answers["best_explanation"].items():
            peak[h] = max(peak.get(h, 0.0), p)
        trace["states"].append({"check": observed[-1] if observed else None,
                                "model_beliefs": answers["best_explanation"],
                                "jev": {"beliefs": answers["best_explanation"],
                                        "contradicted": answers["most_contradicted"]},
                                "jev_error": None})
        checks_left = min(where_budget - len(observed), len(unrun_checks(scenario, observed)))
        candidate, reason, conflicts = candidate_from(answers, len(observed), policy)
        failed = None  # a candidate that failed verification this round
        if forced and not observed:
            candidate, reason = None, "forced first check"
        rnd["candidate"], rnd["reason"], rnd["conflicts"] = candidate, reason, conflicts

        try:
            if candidate:
                ok, supporting, unresolved, why, support, ruled_out = verify(candidate, observed, answers, rnd)
                rnd["reason"] = why
                if ok:
                    rnd["action"] = {"type": "conclude", "hypothesis": candidate}
                    finish("diagnosis", candidate, "concluded" if policy == "v1" else "verified", answers,
                           supporting, support, ruled_out, verified=True)
                    break
                failed = {"tentative": candidate, "supporting": supporting, "support": support,
                          "ruled_out": ruled_out, "unresolved": unresolved}

            if checks_left <= 0:
                if policy in ANSWERING:
                    answer_anyway("check_budget_exhausted", answers, rnd, observed)
                    break
                rnd["action"] = {"type": "abstain"}
                finish("abstain", None,
                       "check_budget_exhausted_unverified" if failed else "check_budget_exhausted",
                       answers, **(failed or {}))
                break
            if forced and not observed:
                pick = scenario["version_check"]  # harness-fixed, as in the LLM setups' forced mode
            else:
                pick = rca._top(answers["next_check"])
                leader = rca._top(answers["best_explanation"])
                if policy in CHALLENGE and answers["best_explanation"][leader] >= CHALLENGE_MIN:
                    # Guard against anchoring: once something leads, look for the check that
                    # could overturn it, not one that only fits it.
                    resp, meta = call("challenge", challenge_payload(scenario, observed, leader, jev_model))
                    rnd["calls"].append(meta)
                    try:
                        challenge = _choice(resp["answers"], "challenge_check",
                                            [*unrun_checks(scenario, observed), triage.NONE], warnings)
                    except (KeyError, TypeError, ValueError, AttributeError) as e:
                        raise triage.JevError(f"malformed response ({type(e).__name__}: {e})") from e
                    rnd["challenge"] = {"leader": leader, "answers": challenge}
                    if rca._top(challenge) != triage.NONE:
                        pick = rca._top(challenge)
            if pick == triage.NONE:
                if policy in ANSWERING:
                    answer_anyway("no_useful_check", answers, rnd, observed)
                    break
                rnd["action"] = {"type": "abstain"}
                finish("abstain", None, "no_useful_check", answers, **(failed or {}))
                break
        except Exception as e:  # a failed verification call: infrastructure, not diagnosis
            trace["status"], trace["error"] = "jev_error", f"{type(e).__name__}: {e}"
            break
        # Validate: a real check on the menu, not run before. Code, not Jev, enforces this.
        if pick not in checks or pick in observed:
            trace["status"], trace["error"] = "invalid_action", f"invalid check {pick!r}"
            break
        rnd["action"] = {"type": "check", "check": pick, "forced": bool(forced and not observed)}
        trace["observed"].append(pick)

    if policy in MECHANISM and trace["status"] == "ok" and trace["final"]:
        what_failed_phase()

    if policy in MECHANISM and trace["status"] == "ok" and trace["diagnosis"]:
        trace["evidence_report"] = evidence_report(scenario, trace)
    trace["seconds"] = round(time.monotonic() - t0, 1)
    if explainer and trace["diagnosis"]:
        trace["explanation"] = explain(scenario, trace, explainer)
    return trace


def _result_line(scenario, check, hypothesis):
    """The line of a check's result that concerns the hypothesis, or its first line."""
    lines = [l.strip() for l in scenario["checks"][check]["result"].strip().splitlines() if l.strip()]
    for l in lines:
        if l.startswith(f"- {hypothesis}:"):
            return l[2:]
    if len(lines) > 1 and all(l.startswith("- ") for l in lines[1:]):
        return f"no {hypothesis} series in this overview"  # a per-service list without it
    return lines[0] if lines else ""


def evidence_report(scenario, trace):
    """A plain-text summary an engineer can check in seconds, built by code from the
    frozen diagnosis and the observed results: no model writes it."""
    d = trace["diagnosis"]
    if not d or d.get("decision") != "diagnosis":
        return "No diagnosis."
    h = d["hypothesis"]
    lines = [f"Root cause: {h} ({'verified' if d.get('verified') else 'not verified'})"]
    mech = d.get("mechanism_evidence") or []
    if mech:
        lines.append("What failed:")
        lines += [f"  - {c}: {_result_line(scenario, c, h)}" for c in mech]
    where = [c for c in d["evidence"] if c not in mech]
    if where:
        lines.append("Where it showed:")
        lines += [f"  - {c}: {_result_line(scenario, c, h)}" for c in where]
    if not mech and not where:
        lines.append("No observed result was judged to support it.")
    ruled = sorted(x for x, p in d.get("ruled_out", {}).items() if p >= RULED_OUT_MIN)
    if ruled:
        lines.append("Ruled out: " + ", ".join(ruled))
    if d.get("unresolved_alternatives"):
        lines.append("Not ruled out: " + ", ".join(d["unresolved_alternatives"]))
    if d.get("excluded_evidence"):
        lines.append("Not used as evidence (failed or empty): " + ", ".join(d["excluded_evidence"]))
    lines.append(f"Checks run: {len(trace['observed'])}: " + ", ".join(trace["observed"]))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Optional explanation, after the diagnosis is frozen

EXPLAIN_SYSTEM = """You write the explanation for a root-cause diagnosis that has already been decided and frozen. You cannot change it. Explain it from the observed results, and say plainly if you disagree.

Reply with one JSON object:
```json
{"component": "<service or system at fault>", "mechanism": "<one or two sentences: what failed and how it caused the incident>", "agrees": true, "preferred_hypothesis": null, "reason": "<one sentence>"}
```
If you disagree, set "agrees" to false and name the hypothesis id you would pick in "preferred_hypothesis"."""


def explain_prompt(scenario, diagnosis, observed):
    lines = ["## Incident", scenario["incident"]["title"], scenario["incident"]["description"], "",
             "## Initial context", scenario["initial_context"], "", "## Hypotheses"]
    lines += [f"- `{h}`: {d}" for h, d in scenario["hypotheses"].items()]
    lines += ["", "## Observed results"]
    for c in observed:
        lines += [f"### `{c}`: {scenario['checks'][c]['description']}", scenario["checks"][c]["result"]]
    if diagnosis["decision"] == "diagnosis":
        lines += ["", "## Frozen diagnosis", f"Root cause: `{diagnosis['hypothesis']}`",
                  "Supporting evidence: " + ", ".join(f"`{c}`" for c in diagnosis["evidence"])]
    else:
        lines += ["", "## Frozen diagnosis", "Abstained: no hypothesis was adequately supported."]
    return "\n".join(lines)


def explain(scenario, trace, ask):
    """Ask the writer for an explanation of a copy of the frozen diagnosis. Never
    changes trace["diagnosis"] or trace["final"]; a disagreement is recorded."""
    frozen = copy.deepcopy(trace["diagnosis"])
    out = {"component": None, "mechanism": None, "agrees": None, "preferred_hypothesis": None,
           "reason": None, "seconds": None, "usage": {}, "error": None}
    t0 = time.monotonic()
    trace["usage"]["llm_calls"] += 1  # an attempt counts, whether or not it succeeds
    try:
        text, _, usage = ask(EXPLAIN_SYSTEM, [{"role": "user", "content":
                                              explain_prompt(scenario, frozen, trace["observed"])}])
        out["usage"] = {"input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0)}
        obj = next((o for o in rca.json_objects(text) if "mechanism" in o or "agrees" in o), None)
        if obj is None:
            out["error"] = "no JSON object in the reply"
        else:
            out["component"] = str(obj.get("component", ""))
            out["mechanism"] = str(obj.get("mechanism", ""))
            out["agrees"] = obj.get("agrees") if isinstance(obj.get("agrees"), bool) else None
            pref = obj.get("preferred_hypothesis")
            out["preferred_hypothesis"] = pref if pref in scenario["hypotheses"] else None
            out["reason"] = str(obj.get("reason", ""))[:500]
            if out["preferred_hypothesis"] and out["preferred_hypothesis"] != frozen.get("hypothesis"):
                out["agrees"] = False  # naming another cause is a disagreement, whatever the flag says
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    out["seconds"] = round(time.monotonic() - t0, 1)
    if _digest(trace["diagnosis"]) != trace["diagnosis_digest"]:
        raise AssertionError("the explanation step changed the frozen diagnosis")
    return out


# --------------------------------------------------------------------------
# Comparison: LLM alone, LLM + Jev, Jev as the investigator

def load_pricing(path=None):
    """{model spec: {"input_per_m", "output_per_m"}} from rca_pricing.json. A null
    price means unknown: that system's cost is reported as unpriced, not zero."""
    import os
    path = path or os.path.join(rca.BASE, "rca_pricing.json")
    data = rca.load_json(path)
    return {k: v for k, v in data.get("models", {}).items()}


def _price(pricing, model, tokens_in, tokens_out):
    p = pricing.get(model)
    if not p or p.get("input_per_m") is None or p.get("output_per_m") is None:
        return None
    return tokens_in * p["input_per_m"] / 1e6 + tokens_out * p["output_per_m"] / 1e6


def trial_costs(trace, pricing):
    """Calls, tokens by model and dollar cost of one trace, whatever its setup."""
    usage = trace.get("usage") or {}
    jev_model = "jev:" + (trace.get("jev_model") or triage.MODEL)
    if trace["setup"] == AGENT_SETUP:
        jev_in, jev_out = usage.get("jev_input_tokens", 0), usage.get("jev_output_tokens", 0)
        llm_in = llm_out = 0
        llm_calls = 0
        exp = trace.get("explanation") or {}
        exp_usage = exp.get("usage") or {}
        exp_in, exp_out = exp_usage.get("input_tokens", 0), exp_usage.get("output_tokens", 0)
        exp_model = trace.get("explain_model")
        exp_calls = 1 if exp.get("seconds") is not None else 0
    else:
        jev_in = sum(((s.get("jev") or {}).get("usage") or {}).get("input_tokens", 0) for s in trace["states"])
        jev_out = sum(((s.get("jev") or {}).get("usage") or {}).get("output_tokens", 0) for s in trace["states"])
        llm_in, llm_out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        # Older traces did not count calls: one reply per state is a lower bound.
        llm_calls = usage.get("llm_calls", len(trace["states"]))
        exp_in = exp_out = exp_calls = 0
        exp_model = None
    llm_cost = _price(pricing, trace.get("model"), llm_in, llm_out) if llm_calls else 0.0
    jev_cost = _price(pricing, jev_model, jev_in, jev_out) if usage.get("jev_calls") else 0.0
    exp_cost = _price(pricing, exp_model, exp_in, exp_out) if exp_calls else 0.0
    return {"llm_calls": llm_calls, "llm_calls_counted": trace["setup"] == AGENT_SETUP or "llm_calls" in usage,
            "jev_calls": usage.get("jev_calls", 0), "llm_in": llm_in, "llm_out": llm_out,
            "jev_in": jev_in, "jev_out": jev_out,
            "cost": None if llm_cost is None or jev_cost is None else llm_cost + jev_cost,
            "explain_calls": exp_calls, "explain_in": exp_in, "explain_out": exp_out,
            "explain_seconds": (trace.get("explanation") or {}).get("seconds"), "explain_cost": exp_cost}


def system_label(trace):
    if trace["setup"] == AGENT_SETUP:
        return f"Jev investigates ({(trace.get('model') or '').split(':', 1)[-1]})"
    name = (trace.get("model") or "?").split(":", 1)[-1]
    suffix = {"alone": "", "jev": " + Jev", "jev-contra": " + Jev contra"}
    return name + suffix.get(trace["setup"], " " + trace["setup"])


def run_conditions(trace):
    """The conditions a trial ran under. Trials under different conditions are never
    pooled. LLM traces written before budgets were recorded are assumed to have run
    with the current default, which the trace cannot confirm; the value is marked
    as assumed wherever it is shown."""
    budgets = trace.get("budgets") or {}
    return {"mode": "forced" if trace.get("forced") else "free",
            "max_checks": budgets.get("max_checks", rca.MAX_CHECKS),
            "assumed": "max_checks" not in budgets,
            "max_rounds": budgets.get("max_rounds"),
            "harness": trace.get("harness_version"),
            "agent_version": trace.get("agent_version")}


def condition_label(c):
    parts = [c["mode"], f"{c['max_checks']}{'*' if c['assumed'] else ''} checks"]
    if c["max_rounds"]:
        parts.append(f"{c['max_rounds']} rounds")
    if c["agent_version"]:
        parts.append(f"agent v{c['agent_version']}")
    return "[" + ", ".join(parts) + "]"


def comparable(c):
    """What must match between columns for a like-for-like comparison."""
    return (c["mode"], c["max_checks"], c["harness"])


COMPARE_ROWS = [
    ("trials", "trials"),
    ("infra", "infrastructure failures"),
    ("correct", "correct cause"),
    ("incorrect", "incorrect conclusion"),
    ("abstained", "abstained"),
    ("tentative", "abstained at budget, tentative pick right"),
    ("verified", "answer verified before stopping (Jev)"),
    ("unfinished", "unfinished (no answer, no abstention)"),
    ("evidence_valid", "supported diagnosis (ID rubric)"),
    ("evidence_all_observed", "cited only checks it ran"),
    ("traps_run", "failed/empty queries run"),
    ("cited_trap", "failed/empty queries cited"),
    ("checks", "mean checks used"),
    ("llm_calls", "mean LLM calls"),
    ("jev_calls", "mean Jev calls"),
    ("seconds", "mean seconds per trial"),
    ("llm_tokens", "mean LLM tokens in/out"),
    ("jev_tokens", "mean Jev tokens in/out"),
    ("cost", "mean $ per trial"),
    ("mechanism", "explained mechanism (text)"),
    ("explain", "explanation: s / $ / disagreed"),
]


def compare(traces, pricing):
    """({column: {row key: cell text}}, {column: scenario digests}, {column: conditions}).

    A column is one system under one set of run conditions (mode, check budget,
    round budget, harness and agent version): forced and free trials, or different
    budgets, are never merged. Infrastructure failures are counted apart and left
    out of every diagnostic rate."""
    systems, digests, conds, truths = {}, {}, {}, {}
    for t in traces:
        name = t.get("scenario", rca.DEFAULT_SCENARIO)
        truths.setdefault(name, rca.load_ground_truth(name))
        c = run_conditions(t)
        key = f"{system_label(t)} {condition_label(c)}"
        systems.setdefault(key, []).append((t, rca.score_trial(t, truths[name]), trial_costs(t, pricing)))
        digests.setdefault(key, set()).add((name, t.get("scenario_digest")))
        conds[key] = c
    table = {}
    for key, rows in systems.items():
        infra = [r for r in rows if r[0].get("status") in rca.INFRA_STATUSES]
        ok = [r for r in rows if r[0].get("status") not in rca.INFRA_STATUSES]
        n = len(ok)

        def count(pred):
            return f"{sum(1 for r in ok if pred(r))}/{n}" if n else "-"

        def mean(vals, fmt="{:.2f}"):
            vals = [v for v in vals if v is not None]
            return fmt.format(sum(vals) / len(vals)) if vals else "-"
        costs = [r[2]["cost"] for r in ok]
        mech = [r[1].get("explanation_mechanism") if r[0]["setup"] == AGENT_SETUP else r[1].get("found_mechanism")
                for r in ok]
        mech = [m for m in mech if m is not None]
        exp = [r for r in ok if r[2]["explain_calls"]]
        counted = all(r[2]["llm_calls_counted"] for r in ok)
        table[key] = {
            "trials": str(len(rows)), "infra": str(len(infra)),
            "correct": count(lambda r: r[1]["correct_hypothesis"]),
            "incorrect": count(lambda r: r[1]["finished"] and not r[1]["correct_hypothesis"]),
            "abstained": count(lambda r: r[1]["abstained"]),
            "tentative": count(lambda r: r[1].get("tentative_correct")),
            "verified": (count(lambda r: r[1].get("verified")) if any(r[1].get("verified") is not None for r in ok)
                         else "-"),
            "unfinished": count(lambda r: not r[1]["finished"] and not r[1]["abstained"]),
            "evidence_valid": count(lambda r: r[1]["evidence_valid"]),
            "evidence_all_observed": count(lambda r: r[1]["evidence_all_observed"]),
            "traps_run": mean([r[1]["traps_run"] for r in ok]),
            "cited_trap": count(lambda r: r[1]["cited_trap"]),
            "checks": mean([r[1]["checks_used"] for r in ok]),
            "llm_calls": ("" if counted or not n else ">=") + mean([r[2]["llm_calls"] for r in ok], "{:.1f}"),
            "jev_calls": mean([r[2]["jev_calls"] for r in ok], "{:.1f}"),
            "seconds": mean([r[0].get("seconds") for r in ok], "{:.1f}"),
            "llm_tokens": (mean([r[2]["llm_in"] for r in ok], "{:.0f}") + "/"
                           + mean([r[2]["llm_out"] for r in ok], "{:.0f}")),
            "jev_tokens": (mean([r[2]["jev_in"] for r in ok], "{:.0f}") + "/"
                           + mean([r[2]["jev_out"] for r in ok], "{:.0f}")),
            "cost": ("unpriced" if any(c is None for c in costs) else mean(costs, "{:.5f}")) if n else "-",
            "mechanism": f"{sum(bool(m) for m in mech)}/{len(mech)}" if mech else "-",
            "explain": (mean([r[2]["explain_seconds"] for r in exp], "{:.1f}") + " / "
                        + ("unpriced" if any(r[2]["explain_cost"] is None for r in exp)
                           else mean([r[2]["explain_cost"] for r in exp], "{:.5f}")) + " / "
                        + f"{sum(1 for r in exp if (r[0].get('explanation') or {}).get('agrees') is False)}"
                        f"/{len(exp)}") if exp else "-",
        }
    return table, digests, conds


def mismatches(digests, conds):
    """Warnings for columns that are not like-for-like: different scenario sets or
    versions, or different mode, check budget or harness version."""
    out = []
    if len({frozenset(d) for d in digests.values()}) > 1:
        out.append("the columns ran different scenario sets or scenario versions (digests differ)")
    if len({comparable(c) for c in conds.values()}) > 1:
        out.append("the columns ran under different conditions (mode, check budget or harness version): "
                   + "; ".join(sorted({f"{c['mode']}, {c['max_checks']} checks, harness v{c['harness']}"
                                       for c in conds.values()})))
    if any(c["assumed"] for c in conds.values()):
        out.append("* check budget not recorded in those traces; assumed the current default "
                   f"({rca.MAX_CHECKS}), which the traces cannot confirm")
    return out


def print_compare(traces, pricing, out=None):
    import sys
    out = out or sys.stdout
    table, digests, conds = compare(traces, pricing)
    systems = sorted(table, key=lambda s: (s.startswith("Jev investigates"), s))
    width = max(18, *(len(s) for s in systems))
    print("\nInvestigator comparison (infrastructure failures excluded from every rate)", file=out)
    print(f"  {'':<40}" + "".join(f"{s:>{width + 2}}" for s in systems), file=out)
    for key, label in COMPARE_ROWS:
        print(f"  {label:<40}" + "".join(f"{table[s][key]:>{width + 2}}" for s in systems), file=out)
    warnings = mismatches(digests, conds)
    for w in warnings:
        print(f"\n  {'NOTE' if w.startswith('*') else 'WARNING'}: {w}", file=out)
    if len({frozenset(d) for d in digests.values()}) > 1:
        for s in systems:
            print(f"    {s}: " + ", ".join(f"{n}@{d}" for n, d in sorted(digests[s], key=str)), file=out)
    if not [w for w in warnings if not w.startswith("*")]:
        print("\n  Same scenarios, scenario digests, mode, check budget and harness version in every column.",
              file=out)
    return table
