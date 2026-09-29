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
    (best_explanation >= PLAUSIBLE_MIN) must be ruled out (P >= RULED_OUT_MIN)
    while checks remain. With no checks left, unresolved alternatives no longer
    block the conclusion but are reported.
  - Otherwise run next_check's top pick. If Jev's top pick is none (no remaining
    check would help) the investigation abstains. So does running out of checks
    or decision rounds without a verified candidate.

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
AGENT_VERSION = 1  # bump when the questions, thresholds or decision rule change

# Decision thresholds. Chosen before any live run and not tuned on the scenarios.
ENOUGH_MIN = 0.7     # P(yes) that the evidence is enough to name one cause
SUPPORT_MIN = 0.5    # P that the candidate is the adequately supported hypothesis
EVIDENCE_MIN = 0.5   # P(yes) that an observed result directly supports the candidate
RULED_OUT_MIN = 0.5  # P(yes) that an alternative is ruled out
PLAUSIBLE_MIN = 0.1  # best_explanation mass that keeps an alternative plausible

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
    results of the checks in observed only."""
    incident = scenario["incident"]
    return {"incident": {"title": incident["title"], "description": incident["description"],
                         "started_at": incident["started_at"]},
            "initial_context": scenario["initial_context"],
            "guidance": scenario["guidance"],
            "observations": rca.evidence_text(scenario, observed)}


def unrun_checks(scenario, observed):
    return {c: v["description"] for c, v in scenario["checks"].items() if c not in observed}


def round_payload(scenario, observed, model=None):
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
        questions["next_check"] = {"type": "choice", "instructions": NEXT_Q,
                                   "criteria": {**unrun, triage.NONE: NO_USEFUL_CHECK}}
    return {"model": model or triage.MODEL, "state": visible_state(scenario, observed),
            "questions": questions}


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


def verify_payload(scenario, observed, candidate, model=None):
    """The dependent request: which observed results support the candidate, and which
    alternatives the evidence rules out. Returns (payload, {question key: (kind, id)})."""
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
    return ({"model": model or triage.MODEL, "state": visible_state(scenario, observed),
             "questions": questions}, keys)


# --------------------------------------------------------------------------
# Parsing Jev's answers

def _choice(answers, key, options):
    dist = triage._distribution(answers[key]["probabilities"], options, key)
    triage._validate_choice_max(answers[key], dist, key)
    return dist


def _noul(answers, key):
    p = triage._validate_finite(answers[key]["noul"], key)
    if not 0.0 <= p <= 1.0:
        raise triage.JevError(f"{key}: probability {p} out of range")
    return p


def parse_round(resp, scenario, observed):
    hyps = list(scenario["hypotheses"])
    try:
        a = resp["answers"]
        out = {"best_explanation": _choice(a, "best_explanation", hyps),
               "most_contradicted": _choice(a, "most_contradicted", [*hyps, triage.NONE]),
               "supported": _choice(a, "supported", [*hyps, triage.NONE]),
               "enough_evidence": _noul(a, "enough_evidence"),
               "next_check": None}
        unrun = unrun_checks(scenario, observed)
        if unrun:
            out["next_check"] = _choice(a, "next_check", [*unrun, triage.NONE])
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise triage.JevError(f"malformed response ({type(e).__name__}: {e})") from e
    return out


def parse_verify(resp, keys):
    support, ruled_out = {}, {}
    try:
        a = resp["answers"]
        for key, (kind, ident) in keys.items():
            (support if kind == "support" else ruled_out)[ident] = _noul(a, key)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise triage.JevError(f"malformed response ({type(e).__name__}: {e})") from e
    return support, ruled_out


# --------------------------------------------------------------------------
# The decision rule

def candidate_from(answers, n_observed):
    """(candidate or None, reason, conflicts). A ranking alone never makes a candidate."""
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
    if sup[h_sup] < SUPPORT_MIN:
        return None, f"support for {h_sup} below {SUPPORT_MIN}", conflicts
    if answers["enough_evidence"] < ENOUGH_MIN:
        return None, f"evidence judged insufficient (P = {answers['enough_evidence']:.2f})", conflicts
    if h_contra == h_best:
        return None, "the candidate is also the most contradicted hypothesis", conflicts
    return h_best, "candidate", conflicts


def verdict(candidate, support, ruled_out, best, checks_left):
    """(conclude?, supporting evidence, unresolved plausible alternatives, reason)."""
    supporting = [c for c, p in support.items() if p >= EVIDENCE_MIN]
    unresolved = [h for h, p in ruled_out.items() if p < RULED_OUT_MIN and best.get(h, 0) >= PLAUSIBLE_MIN]
    if not supporting:
        return False, supporting, unresolved, f"no observed result directly supports {candidate}"
    if unresolved and checks_left > 0:
        return False, supporting, unresolved, "plausible alternatives not ruled out: " + ", ".join(unresolved)
    return True, supporting, unresolved, "verified" if not unresolved else "verified at the check budget"


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
                    jev_model=None, explainer=None):
    """Run one Jev-driven investigation.

    ask_jev(payload) -> (response, ms). explainer, optional, is an LLM ask callable
    (system, messages) -> (text, msg, usage) used only after the diagnosis is frozen.

    The trace keeps the shape of the LLM setups' traces (states, observed, final,
    usage, seconds) so the shared scoring applies, plus the rounds and the
    structured diagnosis. states[k] holds Jev's beliefs after k checks, as both
    the investigator's belief (model_beliefs) and Jev's (jev)."""
    max_rounds = max_rounds or max_checks + 1
    hyps = list(scenario["hypotheses"])
    checks = scenario["checks"]
    trace = {"setup": AGENT_SETUP, "agent_version": AGENT_VERSION, "forced": forced, "status": "ok",
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
               ruled_out=None, unresolved=()):
        _, excluded = usable_evidence(scenario, trace["observed"])
        diagnosis = {"decision": decision, "hypothesis": hypothesis, "evidence": list(supporting),
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

    answers = None
    while True:
        if len(trace["rounds"]) >= max_rounds:
            finish("abstain", None, "round_budget_exhausted", answers)
            break
        observed = list(trace["observed"])
        rnd = {"round": len(trace["rounds"]) + 1, "checks_observed": len(observed), "calls": [],
               "conflicts": [], "candidate": None, "verification": None, "action": None, "reason": None}
        trace["rounds"].append(rnd)
        try:
            resp, meta = call("round", round_payload(scenario, observed, jev_model))
            rnd["calls"].append(meta)
            answers = parse_round(resp, scenario, observed)
        except Exception as e:  # an infrastructure failure, not a diagnosis: no LLM fallback
            trace["status"], trace["error"] = "jev_error", f"{type(e).__name__}: {e}"
            break
        rnd["answers"] = answers
        trace["states"].append({"check": observed[-1] if observed else None,
                                "model_beliefs": answers["best_explanation"],
                                "jev": {"beliefs": answers["best_explanation"],
                                        "contradicted": answers["most_contradicted"]},
                                "jev_error": None})
        checks_left = min(max_checks - len(observed), len(unrun_checks(scenario, observed)))
        candidate, reason, conflicts = candidate_from(answers, len(observed))
        if forced and not observed:
            candidate, reason = None, "forced first check"
        rnd["candidate"], rnd["reason"], rnd["conflicts"] = candidate, reason, conflicts

        if candidate:
            try:
                payload, keys = verify_payload(scenario, observed, candidate, jev_model)
                resp, meta = call("verify", payload)
                rnd["calls"].append(meta)
                support, ruled_out = parse_verify(resp, keys)
            except Exception as e:
                trace["status"], trace["error"] = "jev_error", f"{type(e).__name__}: {e}"
                break
            ok, supporting, unresolved, why = verdict(candidate, support, ruled_out,
                                                      answers["best_explanation"], checks_left)
            rnd["verification"] = {"support": support, "ruled_out": ruled_out, "supporting": supporting,
                                   "unresolved": unresolved, "result": why}
            rnd["reason"] = why
            if ok:
                rnd["action"] = {"type": "conclude", "hypothesis": candidate}
                finish("diagnosis", candidate, "concluded" if not unresolved else "concluded_at_check_budget",
                       answers, supporting, support, ruled_out, unresolved)
                break

        if checks_left <= 0:
            rnd["action"] = {"type": "abstain"}
            finish("abstain", None, "check_budget_exhausted", answers)
            break
        if forced and not observed:
            pick = scenario["version_check"]  # harness-fixed, as in the LLM setups' forced mode
        else:
            pick = rca._top(answers["next_check"])
        if pick == triage.NONE:
            rnd["action"] = {"type": "abstain"}
            finish("abstain", None, "no_useful_check", answers)
            break
        # Validate: a real check on the menu, not run before. Code, not Jev, enforces this.
        if pick not in checks or pick in observed:
            trace["status"], trace["error"] = "invalid_action", f"invalid check {pick!r}"
            break
        rnd["action"] = {"type": "check", "check": pick, "forced": bool(forced and not observed)}
        trace["observed"].append(pick)

    trace["seconds"] = round(time.monotonic() - t0, 1)
    if explainer and trace["diagnosis"]:
        trace["explanation"] = explain(scenario, trace, explainer)
    return trace


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


COMPARE_ROWS = [
    ("trials", "trials"),
    ("infra", "infrastructure failures"),
    ("correct", "correct cause"),
    ("incorrect", "incorrect conclusion"),
    ("abstained", "abstained"),
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
    """{system: {row key: cell text}} and per-system scenario digests. Infrastructure
    failures are counted apart and left out of every diagnostic rate."""
    systems, digests, truths = {}, {}, {}
    for t in traces:
        name = t.get("scenario", rca.DEFAULT_SCENARIO)
        truths.setdefault(name, rca.load_ground_truth(name))
        key = system_label(t)
        systems.setdefault(key, []).append((t, rca.score_trial(t, truths[name]), trial_costs(t, pricing)))
        digests.setdefault(key, set()).add((name, t.get("scenario_digest")))
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
    return table, digests


def print_compare(traces, pricing, out=None):
    import sys
    out = out or sys.stdout
    table, digests = compare(traces, pricing)
    systems = sorted(table, key=lambda s: (s.startswith("Jev investigates"), s))
    width = max(18, *(len(s) for s in systems))
    print("\nInvestigator comparison (infrastructure failures excluded from every rate)", file=out)
    print(f"  {'':<40}" + "".join(f"{s:>{width + 2}}" for s in systems), file=out)
    for key, label in COMPARE_ROWS:
        print(f"  {label:<40}" + "".join(f"{table[s][key]:>{width + 2}}" for s in systems), file=out)
    sets = {s: frozenset(d) for s, d in digests.items()}
    if len(set(sets.values())) > 1:
        print("\n  WARNING: the systems ran different scenario sets or scenario versions (digests differ):",
              file=out)
        for s in systems:
            print(f"    {s}: " + ", ".join(f"{n}@{d}" for n, d in sorted(sets[s], key=str)), file=out)
    else:
        print("\n  Same scenarios and scenario digests for every system.", file=out)
    return table
