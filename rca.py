#!/usr/bin/env python3
"""RCA experiment: does the agent change direction when evidence contradicts
its first theory?

An agent investigates one frozen incident by choosing checks from a fixed menu.
The incident opens with a fresh deploy that looks guilty. The version check
(errors split by application version) contradicts that theory. We record what
the agent believes after every observation and score the final answer against
a ground-truth file the agent never sees.

Two setups, same model, same checks, same prompts:

    baseline  a reasoning model picks checks, reports its own beliefs, and
              writes the final answer.
    jev       the same, but after every observation Jev re-scores every
              hypothesis (which one best explains all the evidence, and which
              ones any observation contradicts). The model sees those scores.
              Jev's distribution is the belief we measure.

Usage:
    export ANTHROPIC_API_KEY=...          # or OPENAI_API_KEY with --llm openai
    export TYPESAFE_API_KEY=...           # only for --setup jev / both
    python3 rca.py run --setup both --trials 5
    python3 rca.py run --setup both --trials 5 --force-version-check
    python3 rca.py report rca_results.jsonl
    python3 rca.py run --setup jev --dry-run   # print the first prompt and Jev payload

guide/rca-experiment.md has the design and the metrics. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

import triage

BASE = os.path.dirname(os.path.abspath(__file__))
SCENARIO_DIR = os.path.join(BASE, "rca_scenarios")
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5", "openai": None}
MAX_CHECKS = 8


class AgentError(Exception):
    """The model's reply could not be used."""


# ---------------------------------------------------------------- scenarios

def load_scenario(name):
    """Return (scenario, truth). The truth file is for scoring only."""
    with open(os.path.join(SCENARIO_DIR, f"{name}.json"), encoding="utf-8") as f:
        scenario = json.load(f)
    with open(os.path.join(SCENARIO_DIR, f"{name}.truth.json"), encoding="utf-8") as f:
        truth = json.load(f)
    for check in scenario["initial_checks"]:
        if check not in scenario["checks"]:
            raise ValueError(f"initial check {check!r} is not in the scenario's checks")
    return scenario, truth


# ---------------------------------------------------------------- LLM clients

def _post_json(url, body, headers, timeout, retries=2):
    data = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:300]!r}"
            if e.code != 429 and e.code < 500:
                break
        except (urllib.error.URLError, TimeoutError) as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(2 * 2 ** attempt)
    raise AgentError(f"LLM request failed: {last}")


def anthropic_client(model, api_key, temperature=None, timeout=180):
    def ask(system, prompt):
        body = {"model": model, "max_tokens": 1500, "system": system,
                "messages": [{"role": "user", "content": prompt}]}
        if temperature is not None:
            body["temperature"] = temperature
        resp = _post_json("https://api.anthropic.com/v1/messages", body, {
            "content-type": "application/json", "x-api-key": api_key,
            "anthropic-version": "2023-06-01"}, timeout)
        return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    return ask


def openai_client(model, api_key, base_url, temperature=None, timeout=180):
    def ask(system, prompt):
        body = {"model": model, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        if temperature is not None:
            body["temperature"] = temperature
        resp = _post_json(base_url.rstrip("/") + "/chat/completions", body, {
            "Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}, timeout)
        return resp["choices"][0]["message"]["content"] or ""
    return ask


# ---------------------------------------------------------------- the agent

SYSTEM = """You are the on-call engineer investigating a production incident.
You can run checks from a fixed menu. Each check returns what the telemetry
showed at a fixed moment. Work like a careful SRE: form hypotheses, pick the
check most likely to prove the leading theory wrong, and revise when evidence
contradicts you. A failed query, or an empty result from a service that may not
exist, is not evidence that nothing happened.

Reply with one JSON object and nothing else:
{
  "beliefs": {"<hypothesis id>": <probability>, ...},   // every hypothesis, sums to 1
  "reasoning": "<one or two sentences>",
  "next_check": "<check id>" or null,
  "final": null or {
    "hypothesis": "<hypothesis id>",
    "component": "<the component where the cause lives>",
    "mechanism": "<what actually happened, in one or two sentences>",
    "evidence": ["<check id>", ...]                      // checks you ran that support it
  }
}
Set exactly one of next_check and final."""


def render_state(scenario, observed, jev_scores=None, checks_left=MAX_CHECKS):
    checks = scenario["checks"]
    lines = ["# Alert", json.dumps(scenario["alert"], indent=2), "",
             "# Hypotheses"]
    lines += [f"- {hid}: {text}" for hid, text in scenario["hypotheses"].items()]
    lines += ["", "# Observations so far"]
    for i, cid in enumerate(observed, 1):
        lines.append(f"{i}. [{cid}] {checks[cid]['describe']}\n   {checks[cid]['result']}")
    if jev_scores:
        lines += ["", "# Evidence scores from a separate judge (re-computed after every observation)",
                  "Which hypothesis best explains all the evidence so far:"]
        lines += [f"- {h}: {p:.2f}" for h, p in sorted(jev_scores["best"].items(), key=lambda kv: -kv[1])]
        lines.append("Probability that some observation contradicts the hypothesis:")
        lines += [f"- {h}: {p:.2f}" for h, p in jev_scores["contradicted"].items()]
    remaining = [c for c in checks if c not in observed]
    lines += ["", "# Checks you can still run"]
    lines += [f"- {cid}: {checks[cid]['describe']}" for cid in remaining]
    if checks_left <= 0 or not remaining:
        lines += ["", "No checks left. Give your final answer now (next_check must be null)."]
    else:
        lines += ["", f"You may run {checks_left} more check(s)."]
    return "\n".join(lines)


def _extract_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise AgentError("no JSON object in reply")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise AgentError(f"invalid JSON: {e}") from e


def parse_reply(text, scenario, observed, must_finish=False):
    data = _extract_json(text)
    hyps = list(scenario["hypotheses"])
    raw = data.get("beliefs")
    if not isinstance(raw, dict):
        raise AgentError("missing beliefs")
    beliefs = {}
    for h in hyps:
        try:
            p = float(raw.get(h, 0.0))
        except (TypeError, ValueError) as e:
            raise AgentError(f"belief for {h!r} is not a number") from e
        if not 0.0 <= p <= 1.0:
            raise AgentError(f"belief for {h!r} out of range: {p}")
        beliefs[h] = p
    total = sum(beliefs.values())
    if total <= 0:
        raise AgentError("beliefs sum to zero")
    beliefs = {h: p / total for h, p in beliefs.items()}

    next_check, final = data.get("next_check"), data.get("final")
    if (next_check is None) == (final is None):
        raise AgentError("set exactly one of next_check and final")
    if next_check is not None:
        if must_finish:
            raise AgentError("no checks left; a final answer is required")
        if next_check not in scenario["checks"]:
            raise AgentError(f"unknown check {next_check!r}")
        if next_check in observed:
            raise AgentError(f"check {next_check!r} was already run")
    if final is not None:
        if not isinstance(final, dict) or final.get("hypothesis") not in hyps:
            raise AgentError("final.hypothesis must be one of the hypothesis ids")
        ev = final.get("evidence") or []
        if not isinstance(ev, list):
            raise AgentError("final.evidence must be a list")
        final = {"hypothesis": final["hypothesis"],
                 "component": str(final.get("component", "")),
                 "mechanism": str(final.get("mechanism", "")),
                 "evidence": [str(e) for e in ev]}
    return {"beliefs": beliefs, "reasoning": str(data.get("reasoning", "")),
            "next_check": next_check, "final": final}


# ---------------------------------------------------------------- the Jev judge

BEST_Q = ("Given the incident alert and every observation so far, which hypothesis best "
          "explains all of the evidence? Weigh observations that contradict a hypothesis, "
          "not only the ones that fit it. A failed query, or an empty result from a service "
          "that may not exist, is not evidence either way.")
CONTRA_Q = ("Does any single observation so far directly contradict this hypothesis: {h} "
            "Answer yes only if a specific observation is inconsistent with it.")


def build_judge_payload(scenario, observed, model=triage.MODEL):
    checks = scenario["checks"]
    questions = {"best_explanation": {"type": "choice", "instructions": BEST_Q,
                                      "criteria": dict(scenario["hypotheses"])}}
    for hid, text in scenario["hypotheses"].items():
        questions[f"contradicted_{hid}"] = {"type": "noul", "instructions": CONTRA_Q.format(h=text)}
    state = {"alert": scenario["alert"],
             "observations": [{"check": c, "what": checks[c]["describe"], "result": checks[c]["result"]}
                              for c in observed]}
    return {"model": model, "state": state, "questions": questions}


def parse_judge(resp, scenario):
    hyps = list(scenario["hypotheses"])
    try:
        answers = resp["answers"]
        best = triage._distribution(answers["best_explanation"]["probabilities"], hyps, "best_explanation")
        contradicted = {}
        for h in hyps:
            p = triage._validate_finite(answers[f"contradicted_{h}"]["noul"], f"contradicted_{h}")
            if not 0.0 <= p <= 1.0:
                raise triage.JevError(f"contradicted_{h}: probability {p} out of range")
            contradicted[h] = p
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise triage.JevError(f"malformed response ({type(e).__name__}: {e})") from e
    return {"best": best, "contradicted": contradicted}


def jev_judge(api_key, model=triage.MODEL, timeout_s=10.0, retries=2):
    def judge(scenario, observed):
        payload = build_judge_payload(scenario, observed, model)
        resp, ms = triage.call_jev(payload, api_key, timeout_s, retries, max_wait_s=10.0)
        return parse_judge(resp, scenario), ms
    return judge


# ---------------------------------------------------------------- one trial

def run_trial(scenario, setup, ask, judge=None, max_checks=MAX_CHECKS,
              force_version_check=None):
    """Run one investigation. force_version_check, if set, is the check id that
    replaces the agent's first choice, so every trial sees the contradiction."""
    observed = list(scenario["initial_checks"])
    steps, final, status = [], None, "ok"
    t0 = time.monotonic()
    while True:
        checks_left = max_checks - (len(observed) - len(scenario["initial_checks"]))
        remaining = [c for c in scenario["checks"] if c not in observed]
        must_finish = checks_left <= 0 or not remaining
        step = {"observed": list(observed)}
        scores = None
        if setup == "jev":
            try:
                scores, ms = judge(scenario, observed)
                step["jev"], step["jev_ms"] = scores, round(ms, 1)
            except triage.JevError as e:
                status = f"jev_error: {e}"
                steps.append(step)
                break
        prompt = render_state(scenario, observed, scores, checks_left)
        reply, error = None, None
        for attempt in range(2):
            text = ask(SYSTEM, prompt if attempt == 0 else
                       prompt + f"\n\nYour last reply was rejected: {error}. Reply with valid JSON only.")
            try:
                reply = parse_reply(text, scenario, observed, must_finish)
                break
            except AgentError as e:
                error = str(e)
        if reply is None:
            status = f"invalid_reply: {error}"
            steps.append(step)
            break
        step.update(llm=reply["beliefs"], reasoning=reply["reasoning"],
                    next_check=reply["next_check"], final=reply["final"])
        steps.append(step)
        if reply["final"]:
            final = reply["final"]
            break
        check = reply["next_check"]
        if (force_version_check and len(steps) == 1 and force_version_check not in observed
                and check != force_version_check):
            step["forced"] = force_version_check
            check = force_version_check
        observed.append(check)
    return {"scenario": scenario["id"], "setup": setup, "status": status,
            "forced_version_check": bool(force_version_check),
            "steps": steps, "observed": observed, "final": final,
            "wall_s": round(time.monotonic() - t0, 1)}


# ---------------------------------------------------------------- scoring

def _top(dist):
    return max(dist, key=dist.get) if dist else None


def score_trial(trial, truth):
    """Score one trial. The belief we measure is Jev's in the jev setup and the
    model's own in the baseline."""
    if trial["setup"] == "jev":
        beliefs = [(s.get("jev") or {}).get("best") for s in trial["steps"]]
    else:
        beliefs = [s.get("llm") for s in trial["steps"]]
    decoy, vcheck = truth["decoy"], truth["version_check"]
    out = {"valid": trial["status"] == "ok" and trial["final"] is not None}
    first = beliefs[0] if beliefs else None
    out["decoy_top_first"] = bool(first) and _top(first) == decoy
    out["p_decoy_first"] = round(first[decoy], 3) if first else None

    # The step whose observations first include the version check.
    k = next((i for i, s in enumerate(trial["steps"]) if vcheck in s["observed"]), None)
    out["ran_version_check"] = k is not None
    if k is not None and k > 0 and beliefs[k] and beliefs[k - 1]:
        before, after = beliefs[k - 1], beliefs[k]
        out["p_decoy_before"] = round(before[decoy], 3)
        out["p_decoy_after"] = round(after[decoy], 3)
        out["p_decoy_drop"] = round(before[decoy] - after[decoy], 3)
        out["redirected"] = _top(after) != decoy
    else:
        out["redirected"] = None

    final, observed = trial["final"], set(trial["observed"])
    if final:
        text = f"{final['component']} {final['mechanism']}".lower()
        out["correct_hypothesis"] = final["hypothesis"] == truth["hypothesis"]
        out["blamed_decoy"] = final["hypothesis"] == decoy
        out["localized"] = truth["component"].lower() in text
        out["mechanism_found"] = (any(c in observed for c in truth["mechanism_checks"])
                                  and any(w in text for w in truth["mechanism_keywords"]))
        out["explained"] = out["correct_hypothesis"] and out["mechanism_found"]
        cited = set(final["evidence"])
        out["grounded"] = bool(cited) and cited <= observed
        out["cited_trap"] = bool(cited & set(truth["traps"]))
    out["checks_used"] = len(trial["observed"]) - len(trial["steps"][0]["observed"]) if trial["steps"] else 0
    out["noise_checks"] = len(observed & set(truth["noise"]))
    return out


def report(rows, truth_by_scenario, out=sys.stdout):
    groups = {}
    for r in rows:
        groups.setdefault((r["scenario"], r["setup"], r.get("forced_version_check", False)), []).append(r)

    def rate(scores, field, among=None):
        pool = [s for s in scores if (among is None or s.get(among))]
        vals = [s[field] for s in pool if s.get(field) is not None]
        return f"{sum(vals)}/{len(vals)}" if vals else "-"

    def mean(scores, field):
        vals = [s[field] for s in scores if s.get(field) is not None]
        return f"{sum(vals) / len(vals):.2f}" if vals else "-"

    metrics = [
        ("valid trials", lambda s: rate(s, "valid")),
        ("decoy on top at start", lambda s: rate(s, "decoy_top_first")),
        ("ran the version check", lambda s: rate(s, "ran_version_check")),
        ("changed direction after it", lambda s: rate(s, "redirected")),
        ("mean drop in P(decoy)", lambda s: mean(s, "p_decoy_drop")),
        ("right hypothesis", lambda s: rate(s, "correct_hypothesis")),
        ("found the mechanism", lambda s: rate(s, "explained")),
        ("named the component", lambda s: rate(s, "localized")),
        ("blamed the decoy", lambda s: rate(s, "blamed_decoy")),
        ("evidence all observed", lambda s: rate(s, "grounded")),
        ("cited a failed/empty query", lambda s: rate(s, "cited_trap")),
        ("mean checks used", lambda s: mean(s, "checks_used")),
        ("mean noise checks", lambda s: mean(s, "noise_checks")),
    ]
    for (scenario, setup, forced), trials in sorted(groups.items()):
        scores = [score_trial(t, truth_by_scenario[scenario]) for t in trials]
        mode = "forced version check" if forced else "free"
        print(f"\n{scenario} | {setup} | {mode} | {len(trials)} trials", file=out)
        for name, fn in metrics:
            print(f"  {name:<30} {fn(scores)}", file=out)


# ---------------------------------------------------------------- CLI

def _make_ask(args):
    if args.llm == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            sys.exit("Set ANTHROPIC_API_KEY (or use --llm openai).")
        return anthropic_client(args.model, key, args.temperature)
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("Set OPENAI_API_KEY.")
    return openai_client(args.model, key, os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                         args.temperature)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run trials and append them to a JSONL file")
    run.add_argument("--scenario", default="pool_exhaustion_v1")
    run.add_argument("--setup", choices=["baseline", "jev", "both"], default="both")
    run.add_argument("--trials", type=int, default=5, help="trials per setup")
    run.add_argument("--force-version-check", action="store_true",
                     help="replace the agent's first check with the version check")
    run.add_argument("--max-checks", type=int, default=MAX_CHECKS)
    run.add_argument("--llm", choices=["anthropic", "openai"], default="anthropic")
    run.add_argument("--model", help="LLM model id (default: claude-sonnet-5 for anthropic)")
    run.add_argument("--temperature", type=float)
    run.add_argument("--jev-model", default=triage.MODEL)
    run.add_argument("--out", default="rca_results.jsonl")
    run.add_argument("--dry-run", action="store_true", help="print the first prompt and Jev payload, then exit")
    rep = sub.add_parser("report", help="summarize a results file")
    rep.add_argument("results", nargs="?", default="rca_results.jsonl")
    args = ap.parse_args(argv)

    if args.cmd == "report":
        with open(args.results, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        truths = {name: load_scenario(name)[1] for name in {r["scenario"] for r in rows}}
        report(rows, truths)
        return 0

    scenario, truth = load_scenario(args.scenario)
    if args.dry_run:
        print("=== system ===\n" + SYSTEM)
        print("\n=== first prompt ===\n" + render_state(scenario, scenario["initial_checks"]))
        print("\n=== Jev payload ===\n" + json.dumps(build_judge_payload(scenario, scenario["initial_checks"],
                                                                        args.jev_model), indent=2))
        return 0

    args.model = args.model or DEFAULT_MODELS[args.llm]
    if not args.model:
        sys.exit("--model is required with --llm openai")
    ask = _make_ask(args)
    setups = ["baseline", "jev"] if args.setup == "both" else [args.setup]
    judge = None
    if "jev" in setups:
        key = os.environ.get("TYPESAFE_API_KEY")
        if not key:
            sys.exit("Set TYPESAFE_API_KEY for the jev setup.")
        judge = jev_judge(key, args.jev_model)
    force = truth["version_check"] if args.force_version_check else None

    rows = []
    with open(args.out, "a", encoding="utf-8") as f:
        for i in range(args.trials):
            for setup in setups:  # interleave so drift in the API hits both setups alike
                trial = run_trial(scenario, setup, ask, judge, args.max_checks, force)
                trial.update(trial_index=i, llm=args.llm, model=args.model,
                             jev_model=args.jev_model if setup == "jev" else None)
                f.write(json.dumps(trial) + "\n")
                f.flush()
                rows.append(trial)
                s = score_trial(trial, truth)
                print(f"trial {i + 1} {setup:<8} status={trial['status']} "
                      f"final={trial['final']['hypothesis'] if trial['final'] else '-'} "
                      f"redirected={s['redirected']} checks={s['checks_used']}")
    report(rows, {scenario["id"]: truth})
    print(f"\nAppended {len(rows)} trials to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
