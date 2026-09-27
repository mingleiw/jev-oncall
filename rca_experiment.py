#!/usr/bin/env python3
"""RCA experiment: does a Jev re-score after each observation help an agent
change direction when evidence contradicts the leading hypothesis?

Two setups, same model, same scenario, same prompt wording:

  baseline  The model picks checks, reports beliefs, and gives a final answer.
  jev       Same, but after every observation Jev re-scores every hypothesis
            and the model sees those scores. Jev's distribution is the belief
            being measured.

Usage:
    export ANTHROPIC_API_KEY=...   # or OPENAI_API_KEY + OPENAI_BASE_URL
    python3 rca_experiment.py --trials 5
    python3 rca_experiment.py --trials 5 --forced   # version check first
    python3 rca_experiment.py --dry-run              # no API calls

Standard library only.
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import ssl
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import triage

BASE = os.path.dirname(os.path.abspath(__file__))
SCENARIO_PATH = os.path.join(BASE, "rca_scenario.json")
GROUND_TRUTH_PATH = os.path.join(BASE, "rca_ground_truth.json")
MAX_CHECKS = 12


def load_scenario(path=None):
    with open(path or SCENARIO_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_ground_truth(path=None):
    with open(path or GROUND_TRUTH_PATH, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# Model calls: Anthropic and OpenAI-compatible, standard-library HTTP

def _ssl_context():
    ctx = ssl.create_default_context()
    ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if ca and os.path.exists(ca):
        ctx.load_verify_locations(ca)
    return ctx


def _model_request(host, port, path, headers, body, timeout=60):
    ctx = _ssl_context()
    proxy = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
             or os.environ.get("all_proxy") or os.environ.get("ALL_PROXY"))
    if proxy:
        px = urlparse(proxy)
        conn = http.client.HTTPSConnection(px.hostname, px.port or 8080,
                                           timeout=timeout, context=ctx)
        tunnel_headers = {}
        if px.username:
            import base64
            auth = base64.b64encode(f"{px.username}:{px.password or ''}".encode()).decode()
            tunnel_headers["Proxy-Authorization"] = f"Basic {auth}"
        conn.set_tunnel(host, port or 443, tunnel_headers)
    else:
        conn = http.client.HTTPSConnection(host, port or 443, timeout=timeout, context=ctx)
    data = json.dumps(body).encode("utf-8")
    conn.request("POST", path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    if resp.status >= 400:
        raise RuntimeError(f"HTTP {resp.status}: {raw[:500].decode('utf-8', errors='replace')}")
    return json.loads(raw.decode("utf-8"))


def call_anthropic(messages, model="claude-sonnet-4-20250514", system=None, api_key=None):
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    headers = {
        "Content-Type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    }
    body = {"model": model, "max_tokens": 4096, "messages": messages}
    if system:
        body["system"] = system
    resp = _model_request("api.anthropic.com", 443, "/v1/messages", headers, body)
    return resp["content"][0]["text"]


def call_openai(messages, model="gpt-4o", api_key=None, base_url=None):
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    url = base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")
    parsed = urlparse(url)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }
    body = {"model": model, "max_tokens": 4096, "messages": messages}
    path = (parsed.path.rstrip("/") or "") + "/v1/chat/completions"
    if "/v1/" in parsed.path:
        path = parsed.path.rstrip("/") + "/chat/completions"
    resp = _model_request(parsed.hostname, parsed.port, path, headers, body)
    return resp["choices"][0]["message"]["content"]


def call_model(messages, system=None, provider="anthropic", model=None, api_key=None,
               base_url=None):
    if provider == "anthropic":
        return call_anthropic(messages, model=model or "claude-sonnet-4-20250514",
                              system=system, api_key=api_key)
    else:
        msgs = messages
        if system:
            msgs = [{"role": "system", "content": system}] + messages
        return call_openai(msgs, model=model or "gpt-4o", api_key=api_key, base_url=base_url)


# --------------------------------------------------------------------------
# Jev hypothesis scoring

def build_jev_payload(scenario, evidence_so_far, model=None):
    """Build a Jev payload to re-score hypotheses given the evidence.

    Uses a Choice question for the best-explaining hypothesis and a second
    Choice for the most-contradicted hypothesis."""
    incident = scenario["incident"]
    hypotheses = scenario["hypotheses"]

    state_text = f"Incident: {incident['title']}\n{incident['description']}\n\nEvidence observed so far:\n"
    for step in evidence_so_far:
        state_text += f"\n[{step['check']}]: {step['result']}\n"

    guidance = scenario.get("guidance", "")
    if guidance:
        state_text += f"\n{guidance}\n"

    criteria_best = {hid: desc for hid, desc in hypotheses.items()}
    criteria_contra = dict(criteria_best)
    criteria_contra["none"] = "No hypothesis is clearly contradicted by the evidence so far"

    return {
        "model": model or triage.MODEL,
        "state": {"incident_rca": state_text},
        "questions": {
            "best_explanation": {
                "type": "choice",
                "instructions": ("Which hypothesis best explains all the evidence observed so "
                                 "far? Consider how well each hypothesis accounts for every "
                                 "observation, including any that contradict it."),
                "criteria": criteria_best,
            },
            "contradicted": {
                "type": "choice",
                "instructions": ("Which hypothesis is most clearly contradicted by the evidence? "
                                 "A hypothesis is contradicted when a specific observation is "
                                 "inconsistent with its predicted outcome."),
                "criteria": criteria_contra,
            },
        },
    }


def parse_jev_rca(resp, hypothesis_ids):
    """Parse a Jev RCA response into belief and contradiction distributions."""
    answers = resp["answers"]

    best_raw = answers["best_explanation"]["probabilities"]
    best = triage._distribution(best_raw, hypothesis_ids, "best_explanation")

    contra_ids = list(hypothesis_ids) + ["none"]
    contra_raw = answers["contradicted"]["probabilities"]
    contra = triage._distribution(contra_raw, contra_ids, "contradicted")

    return {"beliefs": best, "contradicted": contra, "model": resp.get("model", "unknown")}


def call_jev_rca(scenario, evidence, api_key, model=None, timeout=5.0):
    payload = build_jev_payload(scenario, evidence, model)
    hypothesis_ids = list(scenario["hypotheses"].keys())
    resp, ms = triage.call_jev(payload, api_key, timeout_s=timeout, retries=1, max_wait_s=3.0)
    scores = parse_jev_rca(resp, hypothesis_ids)
    scores["latency_ms"] = round(ms)
    return scores


# --------------------------------------------------------------------------
# Prompt construction

SYSTEM_PROMPT = """You are an SRE investigating a production incident. You will be given an incident description and a list of hypotheses about its root cause. You have access to a menu of diagnostic checks.

After each check, you must report your beliefs about the probability of each hypothesis in valid JSON, then pick the next check. When you are confident, say DONE and give your final answer.

IMPORTANT:
- A failed query (like a timeout or a locked table) means the check itself could not complete. It is NOT evidence that nothing happened.
- An empty result from a wrong service name means the query was malformed, not that there is nothing to find. Neither should be cited as evidence.
- Report beliefs as a JSON object mapping hypothesis IDs to probabilities that sum to 1.0.
- Pick checks strategically: the most informative check is one that would change your beliefs the most.

Response format after each observation:
```json
{"beliefs": {"hypothesis_id": probability, ...}}
```
NEXT: <check_name>

When done:
DONE
```json
{"hypothesis": "<id>", "component": "<service or system>", "mechanism": "<one sentence>", "evidence": ["<check_name>", ...]}
```"""


def build_initial_prompt(scenario, forced=False):
    incident = scenario["incident"]
    decoy = scenario["decoy"]
    hypotheses = scenario["hypotheses"]
    checks = scenario["checks"]

    prompt = f"""## Incident
{incident['title']}
{incident['description']}

## Initial Context
{decoy['summary']}

## Hypotheses
"""
    for hid, desc in hypotheses.items():
        prompt += f"- **{hid}**: {desc}\n"

    prompt += "\n## Available Checks\n"
    for cname, cdata in checks.items():
        prompt += f"- `{cname}`: {cdata['description']}\n"

    if forced:
        prompt += (f"\nYour first check has been run for you: `error-by-version`.\n\n"
                   f"Result:\n{checks['error-by-version']['result']}\n\n"
                   "Report your beliefs and pick the next check.")
    else:
        prompt += "\nReport your initial beliefs based on the incident description and context, then pick your first check."

    return prompt


def format_observation(check_name, result, jev_scores=None):
    text = f"## Check result: `{check_name}`\n{result}\n"
    if jev_scores:
        beliefs = jev_scores["beliefs"]
        contra = jev_scores["contradicted"]
        text += "\n## Jev hypothesis scores (updated with this evidence)\n"
        for hid in sorted(beliefs, key=lambda h: -beliefs[h]):
            text += f"  {hid}: P={beliefs[hid]:.3f}"
            if contra.get(hid, 0) > 0.1:
                text += f"  (P(contradicted)={contra[hid]:.3f})"
            text += "\n"
        top_contra = max((h for h in contra if h != "none"), key=lambda h: contra[h], default=None)
        if top_contra and contra[top_contra] > contra.get("none", 0):
            text += f"  Most contradicted: {top_contra} ({contra[top_contra]:.3f})\n"
    text += "\nReport your updated beliefs and pick the next check, or say DONE."
    return text


# --------------------------------------------------------------------------
# Response parsing

def parse_beliefs(text):
    """Extract a beliefs JSON object from model output."""
    import re
    patterns = [
        r'```json\s*(\{[^}]*"beliefs"[^}]*\{[^}]*\}[^}]*\})\s*```',
        r'```json\s*(\{"beliefs":\s*\{[^}]+\}\s*\})\s*```',
        r'(\{"beliefs":\s*\{[^}]+\})',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(1))
                if "beliefs" in obj:
                    return obj["beliefs"]
            except json.JSONDecodeError:
                continue
    # Try to find any JSON object with hypothesis-like keys
    for m in re.finditer(r'\{[^{}]+\}', text):
        try:
            obj = json.loads(m.group())
            if isinstance(obj, dict) and all(isinstance(v, (int, float)) for v in obj.values()):
                if len(obj) >= 3:
                    return obj
        except json.JSONDecodeError:
            continue
    return None


def parse_next_check(text, available_checks):
    """Extract the next check name from model output."""
    import re
    m = re.search(r'NEXT:\s*`?([a-z][\w-]*)`?', text)
    if m and m.group(1) in available_checks:
        return m.group(1)
    for name in available_checks:
        if f"`{name}`" in text.split("DONE")[0]:
            return name
    return None


def parse_final_answer(text):
    """Extract the final answer JSON from model output after DONE."""
    import re
    done_pos = text.find("DONE")
    if done_pos < 0:
        return None
    after = text[done_pos:]
    for m in re.finditer(r'\{[^{}]*\}', after, re.DOTALL):
        try:
            obj = json.loads(m.group())
            if "hypothesis" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    # Try multiline JSON
    m = re.search(r'```json\s*(\{.*?\})\s*```', after, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


# --------------------------------------------------------------------------
# Trial runner

def run_trial(scenario, setup, provider="anthropic", model=None, api_key=None,
              base_url=None, jev_api_key=None, jev_model=None, forced=False,
              dry_run=False, max_checks=MAX_CHECKS):
    """Run one RCA trial. Returns a trace dict."""
    checks = scenario["checks"]
    available = set(checks.keys())
    hypothesis_ids = list(scenario["hypotheses"].keys())

    trace = {
        "setup": setup,
        "provider": provider,
        "model": model,
        "forced": forced,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "steps": [],
        "final_answer": None,
        "error": None,
    }

    system = SYSTEM_PROMPT
    messages = []

    initial = build_initial_prompt(scenario, forced=forced)
    messages.append({"role": "user", "content": initial})

    if forced:
        # Record the forced version check as step 0
        jev_scores = None
        if setup == "jev" and not dry_run:
            evidence = [{"check": "error-by-version",
                         "result": checks["error-by-version"]["result"]}]
            try:
                jev_scores = call_jev_rca(scenario, evidence, jev_api_key, jev_model)
            except Exception as e:
                jev_scores = {"error": str(e)}
        trace["steps"].append({
            "check": "error-by-version",
            "result": checks["error-by-version"]["result"],
            "jev_scores": jev_scores,
            "beliefs": None,
        })
        used = {"error-by-version"}
    else:
        used = set()

    if dry_run:
        trace["dry_run"] = True
        trace["prompt_system"] = system
        trace["prompt_initial"] = initial
        if setup == "jev":
            evidence = [{"check": "error-by-version",
                         "result": checks["error-by-version"]["result"]}]
            trace["jev_payload"] = build_jev_payload(scenario, evidence, jev_model)
        return trace

    for step_i in range(max_checks):
        try:
            reply = call_model(messages, system=system, provider=provider,
                               model=model, api_key=api_key, base_url=base_url)
        except Exception as e:
            trace["error"] = f"model call failed: {e}"
            break

        messages.append({"role": "assistant", "content": reply})

        if "DONE" in reply:
            final = parse_final_answer(reply)
            beliefs = parse_beliefs(reply)
            trace["final_answer"] = final
            trace["final_beliefs"] = beliefs
            break

        beliefs = parse_beliefs(reply)
        next_check = parse_next_check(reply, available - used)
        if not next_check:
            # Model didn't pick a valid check; try once more
            messages.append({"role": "user", "content":
                             f"Please pick one of the remaining checks: {', '.join(sorted(available - used))}"})
            continue

        result = checks[next_check]["result"]
        used.add(next_check)

        jev_scores = None
        if setup == "jev":
            evidence = [{"check": c, "result": checks[c]["result"]}
                        for s in trace["steps"] for c in [s["check"]]]
            evidence.append({"check": next_check, "result": result})
            try:
                jev_scores = call_jev_rca(scenario, evidence, jev_api_key, jev_model)
            except Exception as e:
                jev_scores = {"error": str(e)}

        trace["steps"].append({
            "check": next_check,
            "result": result,
            "beliefs": beliefs,
            "jev_scores": jev_scores,
        })

        obs = format_observation(next_check, result,
                                 jev_scores if setup == "jev" and isinstance(jev_scores, dict)
                                 and "beliefs" in jev_scores else None)
        messages.append({"role": "user", "content": obs})

    trace["checks_used"] = [s["check"] for s in trace["steps"]]
    trace["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return trace


# --------------------------------------------------------------------------
# Scoring

def score_trial(trace, ground_truth, scenario):
    """Score one trial against ground truth. Returns a dict of metrics."""
    gt = ground_truth
    steps = trace.get("steps", [])
    final = trace.get("final_answer")
    checks_used = trace.get("checks_used", [])
    hypothesis_ids = list(scenario["hypotheses"].keys())

    scores = {
        "setup": trace["setup"],
        "forced": trace.get("forced", False),
    }

    # 1. Did the decoy lead at the start?
    first_beliefs = None
    for s in steps:
        if s.get("beliefs"):
            first_beliefs = s["beliefs"]
            break
    if first_beliefs:
        decoy = gt["decoy_id"]
        scores["decoy_led_initially"] = (
            max(first_beliefs, key=first_beliefs.get) == decoy
            if isinstance(first_beliefs, dict) else None
        )
    else:
        scores["decoy_led_initially"] = None

    # 2. Did the agent run the version check?
    scores["ran_version_check"] = gt["version_check"] in checks_used

    # 3. Did it change direction after the version check?
    scores["changed_direction"] = None
    scores["p_deploy_drop"] = None
    if gt["version_check"] in checks_used:
        vc_idx = checks_used.index(gt["version_check"])
        beliefs_before = None
        beliefs_after = None

        # In jev mode, use Jev's beliefs
        if trace["setup"] == "jev":
            if vc_idx > 0 and steps[vc_idx - 1].get("jev_scores") and "beliefs" in (steps[vc_idx - 1].get("jev_scores") or {}):
                beliefs_before = steps[vc_idx - 1]["jev_scores"]["beliefs"]
            if steps[vc_idx].get("jev_scores") and "beliefs" in (steps[vc_idx].get("jev_scores") or {}):
                beliefs_after = steps[vc_idx]["jev_scores"]["beliefs"]
        # In baseline mode, use model's self-reported beliefs
        else:
            for i in range(vc_idx - 1, -1, -1):
                if steps[i].get("beliefs"):
                    beliefs_before = steps[i]["beliefs"]
                    break
            if vc_idx + 1 < len(steps) and steps[vc_idx + 1].get("beliefs"):
                beliefs_after = steps[vc_idx + 1]["beliefs"]
            elif steps[vc_idx].get("beliefs"):
                beliefs_after = steps[vc_idx]["beliefs"]

        if beliefs_before and beliefs_after:
            decoy = gt["decoy_id"]
            was_top = max(beliefs_before, key=beliefs_before.get) == decoy
            is_top = max(beliefs_after, key=beliefs_after.get) == decoy
            scores["changed_direction"] = was_top and not is_top
            p_before = beliefs_before.get(decoy, 0)
            p_after = beliefs_after.get(decoy, 0)
            scores["p_deploy_drop"] = round(p_before - p_after, 3)

    # 4-6. Final answer correctness
    if final:
        scores["correct_hypothesis"] = final.get("hypothesis") == gt["hypothesis"]
        scores["correct_mechanism"] = False
        if final.get("mechanism"):
            mechanism = final["mechanism"].lower()
            scores["correct_mechanism"] = (
                ("cron" in mechanism or "etl" in mechanism or "analytics" in mechanism
                 or "schedul" in mechanism or "rescheduled" in mechanism)
                and ("connection" in mechanism or "pool" in mechanism or "long" in mechanism
                     or "hold" in mechanism or "peak" in mechanism)
            )
        scores["correct_component"] = (
            final.get("component", "").lower().replace("-", "").replace("_", "")
            in ("analyticsetl", "analytics", "etl", "cron")
        )

        # 7. Did it blame the decoy?
        scores["blamed_decoy"] = final.get("hypothesis") == gt["decoy_id"]

        # 8. Evidence integrity: every cited piece was actually observed
        cited = final.get("evidence", [])
        scores["all_evidence_observed"] = all(e in checks_used for e in cited)
        scores["cited_trap"] = any(e in gt["traps"] for e in cited)
        scores["evidence_cited"] = cited
    else:
        scores["correct_hypothesis"] = None
        scores["correct_mechanism"] = None
        scores["correct_component"] = None
        scores["blamed_decoy"] = None
        scores["all_evidence_observed"] = None
        scores["cited_trap"] = None
        scores["evidence_cited"] = []

    # 9. Checks used and noise checks
    scores["checks_used"] = checks_used
    scores["num_checks"] = len(checks_used)
    scores["noise_checks"] = [c for c in checks_used if c in gt["noise"]]
    scores["num_noise"] = len(scores["noise_checks"])

    return scores


def print_report(all_scores):
    """Print a summary report from scored trials."""
    baseline = [s for s in all_scores if s["setup"] == "baseline"]
    jev = [s for s in all_scores if s["setup"] == "jev"]

    def count(trials, key):
        yes = sum(1 for t in trials if t.get(key) is True)
        total = sum(1 for t in trials if t.get(key) is not None)
        return f"{yes}/{total}"

    def avg(trials, key):
        vals = [t[key] for t in trials if t.get(key) is not None]
        return f"{sum(vals) / len(vals):.3f}" if vals else "-"

    print("\n=== RCA Experiment Report ===\n")
    print(f"{'Metric':<35} {'Baseline':>10} {'Jev':>10}")
    print("-" * 57)
    print(f"{'Trials':<35} {len(baseline):>10} {len(jev):>10}")
    print(f"{'Decoy led initially':<35} {count(baseline, 'decoy_led_initially'):>10} {count(jev, 'decoy_led_initially'):>10}")
    print(f"{'Ran version check':<35} {count(baseline, 'ran_version_check'):>10} {count(jev, 'ran_version_check'):>10}")
    print(f"{'Changed direction':<35} {count(baseline, 'changed_direction'):>10} {count(jev, 'changed_direction'):>10}")
    print(f"{'Avg P(deploy) drop':<35} {avg(baseline, 'p_deploy_drop'):>10} {avg(jev, 'p_deploy_drop'):>10}")
    print(f"{'Correct hypothesis':<35} {count(baseline, 'correct_hypothesis'):>10} {count(jev, 'correct_hypothesis'):>10}")
    print(f"{'Correct mechanism':<35} {count(baseline, 'correct_mechanism'):>10} {count(jev, 'correct_mechanism'):>10}")
    print(f"{'Correct component':<35} {count(baseline, 'correct_component'):>10} {count(jev, 'correct_component'):>10}")
    print(f"{'Blamed decoy':<35} {count(baseline, 'blamed_decoy'):>10} {count(jev, 'blamed_decoy'):>10}")
    print(f"{'All evidence observed':<35} {count(baseline, 'all_evidence_observed'):>10} {count(jev, 'all_evidence_observed'):>10}")
    print(f"{'Cited a trap':<35} {count(baseline, 'cited_trap'):>10} {count(jev, 'cited_trap'):>10}")
    print(f"{'Avg checks used':<35} {avg(baseline, 'num_checks'):>10} {avg(jev, 'num_checks'):>10}")
    print(f"{'Avg noise checks':<35} {avg(baseline, 'num_noise'):>10} {avg(jev, 'num_noise'):>10}")


# --------------------------------------------------------------------------
# CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="RCA experiment: does Jev help an agent change direction?")
    ap.add_argument("--trials", type=int, default=1, help="Number of trials per setup (default: 1)")
    ap.add_argument("--setup", choices=["baseline", "jev", "both"], default="both",
                    help="Which setup to run (default: both)")
    ap.add_argument("--forced", action="store_true",
                    help="Force the version check as the first check in every trial")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print prompts and Jev payload without making any API calls")
    ap.add_argument("--provider", choices=["anthropic", "openai"], default="anthropic")
    ap.add_argument("--model", help="Model name for the reasoning agent")
    ap.add_argument("--jev-model", help="Jev model (default: triage.MODEL)")
    ap.add_argument("--out", default=os.path.join(BASE, "rca_traces.jsonl"),
                    help="JSONL file for trial traces (default: rca_traces.jsonl)")
    ap.add_argument("--scenario", default=SCENARIO_PATH)
    ap.add_argument("--ground-truth", default=GROUND_TRUTH_PATH)
    ap.add_argument("--max-checks", type=int, default=MAX_CHECKS)
    ap.add_argument("--report", metavar="JSONL",
                    help="Score and report from an existing trace file instead of running")
    args = ap.parse_args(argv)

    if args.report:
        scenario = load_scenario(args.scenario)
        gt = load_ground_truth(args.ground_truth)
        all_scores = []
        with open(args.report, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    trace = json.loads(line)
                    all_scores.append(score_trial(trace, gt, scenario))
        print_report(all_scores)
        return 0

    scenario = load_scenario(args.scenario)
    gt = load_ground_truth(args.ground_truth)

    api_key = None
    base_url = None
    if args.provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
    else:
        api_key = os.environ.get("OPENAI_API_KEY")
        base_url = os.environ.get("OPENAI_BASE_URL")

    jev_api_key = os.environ.get("TYPESAFE_API_KEY")
    if not args.dry_run and not api_key:
        key_name = "ANTHROPIC_API_KEY" if args.provider == "anthropic" else "OPENAI_API_KEY"
        sys.exit(f"{key_name} not set")

    setups = []
    if args.setup in ("baseline", "both"):
        setups.append("baseline")
    if args.setup in ("jev", "both"):
        setups.append("jev")

    all_scores = []
    trial_num = 0

    for i in range(args.trials):
        for setup in setups:
            trial_num += 1
            label = f"trial {trial_num}: {setup}"
            if args.forced:
                label += " (forced)"
            print(f"\n--- {label} ---")

            trace = run_trial(
                scenario, setup,
                provider=args.provider, model=args.model,
                api_key=api_key, base_url=base_url,
                jev_api_key=jev_api_key, jev_model=args.jev_model,
                forced=args.forced, dry_run=args.dry_run,
                max_checks=args.max_checks,
            )
            trace["trial"] = trial_num

            if args.dry_run:
                print(json.dumps(trace, indent=2))
                continue

            with open(args.out, "a", encoding="utf-8") as f:
                f.write(json.dumps(trace) + "\n")

            score = score_trial(trace, gt, scenario)
            all_scores.append(score)

            print(f"  checks: {', '.join(trace.get('checks_used', []))}")
            if trace.get("final_answer"):
                fa = trace["final_answer"]
                print(f"  answer: {fa.get('hypothesis')} (correct: {score['correct_hypothesis']})")
                print(f"  mechanism: {score['correct_mechanism']}, component: {score['correct_component']}")
            if score.get("changed_direction") is not None:
                print(f"  changed direction: {score['changed_direction']}, "
                      f"P(deploy) drop: {score.get('p_deploy_drop')}")

    if all_scores:
        print_report(all_scores)
        print(f"\nTraces written to {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
