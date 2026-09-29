#!/usr/bin/env python3
"""RCA experiment: when evidence contradicts the leading theory, does the
agent change direction? And does a Jev re-score after every check help?

Frozen incidents in rca_scenarios/, some written by hand and some adapted
from public postmortems. In each, something looks guilty at the start (the
decoy: usually a deploy, sometimes an attack or a provider outage), the
agent picks checks from a fixed menu, reports its beliefs after every
observation, and gives a final answer. One key check (`version_check`)
contradicts the decoy. Scoring uses the scenario's .truth.json, which no
agent input contains.

Three setups per model, same scenario, menu and prompt wording; the model's
own stated beliefs are measured in all of them:

  alone       the model on its own.
  jev         after every check Jev re-scores every hypothesis, and the model
              sees Jev's ranking and the most contradicted hypothesis.
  jev-contra  the model sees only the hypothesis Jev judges most contradicted.

A fourth setup, jev-agent (rca_jev_agent.py), has no LLM in the loop: Jev
answers bounded questions and code picks the checks, concludes or abstains.

Usage:
    export TYPESAFE_API_KEY=...  ANTHROPIC_API_KEY=...  OPENAI_API_KEY=...
    python3 rca_experiment.py --dry-run                     # prompts + Jev payload, no calls
    python3 rca_experiment.py --check --models anthropic:claude-opus-5,openai:gpt-5
    python3 rca_experiment.py --models anthropic:claude-opus-5,openai:gpt-5 \\
        --trials 5 --forced                                 # the grid, alone vs + Jev
    python3 rca_experiment.py --report rca_traces.jsonl     # re-score saved traces
    python3 rca_experiment.py --report rca_traces.jsonl --html rca_report.html  # leaderboard page

guide/rca.md has the design. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import triage

BASE = os.path.dirname(os.path.abspath(__file__))
SCENARIO_DIR = os.path.join(BASE, "rca_scenarios")
DEFAULT_SCENARIO = "pool_etl_cron"
TRACES_PATH = os.path.join(BASE, "rca_traces.jsonl")
MAX_CHECKS = 6  # tight enough that wandering costs; the pilot's agents solved it in 3-7
SETUPS = ("alone", "jev", "jev-contra")  # an LLM investigates; "all" on the command line
JEV_SETUPS = ("jev", "jev-contra")  # setups that call Jev
AGENT_SETUP = "jev-agent"  # Jev investigates, no LLM in the loop: rca_jev_agent.py
ALL_SETUPS = (*SETUPS, AGENT_SETUP)
INFRA_STATUSES = ("model_error", "jev_error")  # the call failed, not the diagnosis
DEFAULT_MODELS = {"anthropic": "claude-opus-5", "openai": "gpt-5"}
HARNESS_VERSION = 4  # bump when prompts or scoring change, so old traces stay tellable


class ModelError(Exception):
    """A model call failed or its reply could not be used."""


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def scenario_names():
    return sorted(f[:-5] for f in os.listdir(SCENARIO_DIR)
                  if f.endswith(".json") and not f.endswith(".truth.json"))


def load_scenario(name=DEFAULT_SCENARIO):
    """The agent-facing half of a scenario: never the truth file."""
    return load_json(os.path.join(SCENARIO_DIR, f"{name}.json"))


def load_ground_truth(name=DEFAULT_SCENARIO):
    """Scoring only."""
    return load_json(os.path.join(SCENARIO_DIR, f"{name}.truth.json"))


def scenario_digest(scenario):
    blob = json.dumps(scenario, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


# --------------------------------------------------------------------------
# Model calls: Anthropic and OpenAI-compatible, standard-library HTTP

def _connection(url, timeout):
    """An HTTP(S) connection to url's host, through HTTPS_PROXY when set."""
    u = urlparse(url)
    if u.scheme == "http":
        return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    proxy = triage._proxy_for_host(u.hostname)
    if proxy:
        px = urlparse(proxy)
        conn = http.client.HTTPSConnection(px.hostname, px.port or 8080, timeout=timeout)
        headers = {}
        if px.username:
            import base64
            cred = base64.b64encode(f"{px.username}:{px.password or ''}".encode()).decode()
            headers["Proxy-Authorization"] = f"Basic {cred}"
        conn.set_tunnel(u.hostname, u.port or 443, headers)
        return conn
    return http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout)


def post_json(url, body, headers, timeout=600, retries=2):
    """POST JSON, retrying 429, 5xx and network errors. Raises ModelError."""
    data = json.dumps(body).encode("utf-8")
    path = urlparse(url).path or "/"
    last = "no attempt"
    for attempt in range(retries + 1):
        conn = _connection(url, timeout)
        wait = 2.0 * 2 ** attempt
        try:
            conn.request("POST", path, body=data,
                         headers={"Content-Type": "application/json", **headers})
            resp = conn.getresponse()
            raw = resp.read()
            if 200 <= resp.status < 300:
                return json.loads(raw.decode("utf-8"))
            last = f"HTTP {resp.status}: {raw[:300].decode('utf-8', 'replace')}"
            if resp.status != 429 and resp.status < 500:
                break
            retry_after = resp.getheader("retry-after")
            if retry_after and retry_after.replace(".", "", 1).isdigit():
                wait = min(float(retry_after), 60.0)
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as e:
            last = f"{type(e).__name__}: {e}"
        finally:
            conn.close()
        if attempt < retries:
            time.sleep(wait)
    raise ModelError(last)


def call_anthropic(model, system, messages, api_key, max_tokens=None):
    """Returns (text, assistant message to append, usage)."""
    resp = post_json(
        "https://api.anthropic.com/v1/messages",
        {"model": model, "max_tokens": max_tokens or 16000, "system": system, "messages": messages},
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    if resp.get("stop_reason") == "refusal":
        raise ModelError(f"refusal: {resp.get('stop_details')}")
    content = resp.get("content") or []
    # Current models think by default, so the first block is often "thinking".
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
    usage = resp.get("usage") or {}
    return text, {"role": "assistant", "content": content}, {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0)}


def openai_url(base_url):
    """Chat completions URL. A base with a path (".../v1", ".../v1beta/openai") is used
    as given; a bare host gets the standard /v1."""
    base = (base_url or "https://api.openai.com").rstrip("/")
    has_path = urlparse(base).path not in ("", "/")
    return base + ("/chat/completions" if has_path else "/v1/chat/completions")


def call_openai(model, system, messages, api_key, base_url=None, max_tokens=None):
    # No token limit: newer OpenAI models reject max_tokens, and compatible
    # servers differ on the name of its replacement.
    body = {"model": model, "messages": [{"role": "system", "content": system}, *messages]}
    if max_tokens:  # opt-in: servers disagree on this field's name, but most accept it
        body["max_tokens"] = max_tokens
    resp = post_json(openai_url(base_url), body,
                     {"Authorization": f"Bearer {api_key}"} if api_key else {})
    if isinstance(resp, dict) and resp.get("error"):
        # Some servers (OpenRouter, on rate limits) send the error in a 200 body.
        raise ModelError(f"server error in response: {json.dumps(resp['error'])[:300]}")
    try:
        text = resp["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise ModelError(f"malformed response: {e}") from e
    usage = resp.get("usage") or {}
    return text, {"role": "assistant", "content": text}, {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0)}


def parse_model_spec(spec):
    """'anthropic:claude-opus-5' -> ("anthropic", "claude-opus-5")."""
    provider, _, model = spec.strip().partition(":")
    if provider not in DEFAULT_MODELS:
        raise ValueError(f"{spec!r}: provider must be one of {', '.join(DEFAULT_MODELS)}")
    return provider, model or DEFAULT_MODELS[provider]


def model_client(provider, model, env=None, max_tokens=None):
    """A callable (system, messages) -> (text, assistant_msg, usage)."""
    env = os.environ if env is None else env
    if provider == "anthropic":
        key = env.get("ANTHROPIC_API_KEY")
        if not key:
            raise ModelError("ANTHROPIC_API_KEY is not set")
        return lambda system, messages: call_anthropic(model, system, messages, key, max_tokens)
    key, base_url = env.get("OPENAI_API_KEY"), env.get("OPENAI_BASE_URL")
    if not key and not base_url:
        raise ModelError("OPENAI_API_KEY is not set (a local server set with OPENAI_BASE_URL needs none)")
    return lambda system, messages: call_openai(model, system, messages, key, base_url, max_tokens)


# --------------------------------------------------------------------------
# Jev: re-score every hypothesis on the evidence so far

BEST_Q = ("Which hypothesis best explains all of the evidence observed so far? Weigh every "
          "observation, including any that contradict a hypothesis.")
# One Choice rather than a yes/no per hypothesis: asked separately, Jev called most
# hypotheses contradicted at once (the true cause included), which told the agent
# nothing. A single choice has to compare them. Harness version 4.
CONTRA_Q = ("Which hypothesis does the evidence observed so far most clearly contradict? Pick one "
            "only when a specific observation is inconsistent with what it predicts; otherwise "
            "pick 'none'.")
NO_CONTRADICTION = "No observation so far contradicts any hypothesis"


def evidence_text(scenario, observed):
    checks = scenario["checks"]
    return [{"check": c, "what": checks[c]["description"], "result": checks[c]["result"]}
            for c in observed]


def build_jev_payload(scenario, observed, model=None):
    hypotheses = scenario["hypotheses"]
    questions = {"best_explanation": {"type": "choice", "instructions": BEST_Q,
                                      "criteria": dict(hypotheses)}}
    questions["most_contradicted"] = {"type": "choice", "instructions": CONTRA_Q,
                                      "criteria": {**hypotheses, triage.NONE: NO_CONTRADICTION}}
    incident = scenario["incident"]
    return {
        "model": model or triage.MODEL,
        "state": {"incident": {"title": incident["title"], "description": incident["description"],
                               "started_at": incident["started_at"]},
                  "initial_context": scenario["initial_context"],
                  "guidance": scenario["guidance"],
                  "observations": evidence_text(scenario, observed)},
        "questions": questions,
    }


def parse_jev_rca(resp, hypothesis_ids):
    """Jev's answer as {"beliefs": dist over hypotheses, "contradicted": dist over
    hypotheses and "none"} (which one the evidence most contradicts); JevError if malformed."""
    try:
        answers = resp["answers"]
        beliefs = triage._distribution(answers["best_explanation"]["probabilities"],
                                       hypothesis_ids, "best_explanation")
        triage._validate_choice_max(answers["best_explanation"], beliefs, "best_explanation")
        contradicted = triage._distribution(answers["most_contradicted"]["probabilities"],
                                            [*hypothesis_ids, triage.NONE], "most_contradicted")
        triage._validate_choice_max(answers["most_contradicted"], contradicted, "most_contradicted")
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise triage.JevError(f"malformed response ({type(e).__name__}: {e})") from e
    return {"beliefs": beliefs, "contradicted": contradicted, "model": str(resp.get("model"))}


def jev_scorer(api_key, model=None, timeout_s=15.0):
    """A callable (scenario, observed) -> scores dict. Reuses triage.call_jev."""
    if not api_key:
        raise ModelError("TYPESAFE_API_KEY is not set")

    def score(scenario, observed):
        payload = build_jev_payload(scenario, observed, model)
        resp, ms = triage.call_jev(payload, api_key, timeout_s, retries=2, max_wait_s=10.0)
        scores = parse_jev_rca(resp, list(scenario["hypotheses"]))
        scores["latency_ms"] = round(ms)
        scores["usage"] = resp.get("usage", {})
        return scores
    return score


# --------------------------------------------------------------------------
# Prompts. Identical in both setups; the jev setup only adds Jev's scores.

SYSTEM_PROMPT = """You are an SRE investigating a production incident. You get the incident, some initial context, a list of hypotheses about its root cause, and a menu of diagnostic checks. Each check you ask for is run and its output shown to you.

After every message, reply with your current beliefs over ALL hypotheses as JSON probabilities that sum to 1, then either pick one check or finish.

Rules:
- A failed query (a timeout, a locked table) means the check could not complete. It is not evidence that nothing happened.
- An empty result caused by a wrong service name means the query was malformed, not that there is nothing to find.
- Cite as evidence only checks you actually ran whose output supports your conclusion.

To continue:
```json
{"beliefs": {"<hypothesis id>": <probability>, ...}}
```
NEXT: <check name>

To finish:
```json
{"beliefs": {"<hypothesis id>": <probability>, ...}}
```
DONE
```json
{"hypothesis": "<hypothesis id>", "component": "<service or system at fault>", "mechanism": "<one or two sentences: what failed and how it caused the incident>", "evidence": ["<check name>", ...]}
```"""


def build_initial_prompt(scenario, max_checks=MAX_CHECKS):
    incident, lines = scenario["incident"], []
    lines += ["## Incident", incident["title"], incident["description"], "",
              "## Initial context", scenario["initial_context"], "", "## Hypotheses"]
    lines += [f"- `{hid}`: {text}" for hid, text in scenario["hypotheses"].items()]
    lines += ["", f"## Checks (at most {max_checks})"]
    lines += [f"- `{name}`: {c['description']}" for name, c in scenario["checks"].items()]
    lines += ["", "Report your beliefs, then pick your first check."]
    return "\n".join(lines)


def format_jev_scores(scores):
    b, c = scores["beliefs"], scores["contradicted"]
    lines = ["## Jev's scores on all evidence so far",
             "hypothesis: P(best explanation), P(the evidence most contradicts it)"]
    return "\n".join(lines + [f"- `{h}`: {b[h]:.2f}, {c[h]:.2f}"
                              for h in sorted(b, key=lambda h: -b[h])])


def format_jev_contradictions(scores):
    """What the jev-contra setup shows: the one hypothesis Jev judges the evidence most
    contradicts, or that none is, never its ranking, so there is no top pick to copy."""
    c = scores["contradicted"]
    h = _top(c)
    lines = ["## Jev's check of the hypotheses against the evidence so far"]
    if h == triage.NONE:
        return "\n".join(lines + ["- No hypothesis is clearly contradicted by the evidence so far."])
    return "\n".join(lines + [f"- The evidence most contradicts `{h}` (P = {c[h]:.2f})"])


def format_jev(scores, setup):
    return format_jev_contradictions(scores) if setup == "jev-contra" else format_jev_scores(scores)


def format_observation(check, result, jev_scores=None, note=None, checks_left=None, setup="jev"):
    parts = []
    if note:
        parts.append(note)
    parts += [f"## Result of `{check}`", result]
    if jev_scores:
        parts += ["", format_jev(jev_scores, setup)]
    if checks_left is not None:
        parts += ["", f"{checks_left} checks left."]
    parts.append("Report your beliefs, then pick the next check or finish.")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Reply parsing

def json_objects(text):
    """Every top-level JSON object in text, in order."""
    decoder, i, out = json.JSONDecoder(), 0, []
    while (i := text.find("{", i)) >= 0:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        i = end
    return out


def parse_beliefs(text, hypothesis_ids):
    """The reply's belief distribution over hypothesis_ids, normalized, or None."""
    for obj in json_objects(text):
        raw = obj.get("beliefs", obj)
        if not isinstance(raw, dict) or not set(raw) & set(hypothesis_ids):
            continue
        if set(raw) - set(hypothesis_ids):
            continue
        try:
            dist = {h: float(raw.get(h, 0.0)) for h in hypothesis_ids}
        except (TypeError, ValueError):
            continue
        total = sum(dist.values())
        if total <= 0 or any(p < 0 for p in dist.values()):
            continue
        return {h: p / total for h, p in dist.items()}
    return None


def parse_next_check(text, available):
    before_done = text.split("DONE")[0]
    for line in reversed(before_done.splitlines()):
        line = line.strip().lstrip("*#> ")
        if line.upper().startswith("NEXT:"):
            name = line.split(":", 1)[1].strip().strip("`*. ")
            return name if name in available else None
    return None


def parse_final_answer(text, hypothesis_ids):
    done = text.find("DONE")
    if done < 0:
        return None
    for obj in json_objects(text[done:]):
        if obj.get("hypothesis") in hypothesis_ids:
            evidence = obj.get("evidence")
            return {"hypothesis": obj["hypothesis"],
                    "component": str(obj.get("component", "")),
                    "mechanism": str(obj.get("mechanism", "")),
                    "evidence": [str(e) for e in evidence] if isinstance(evidence, list) else []}
    return None


# --------------------------------------------------------------------------
# One trial

def run_trial(scenario, setup, ask, jev=None, forced=False, max_checks=MAX_CHECKS):
    """Run one investigation.

    ask(system, messages) -> (text, assistant_msg, usage); jev(scenario, observed)
    -> scores, required for the Jev setups. The trace has one state per number of
    checks observed: states[0] is before any check, states[k] after the k-th. Each
    state holds the model's beliefs (from its reply to that state) and, in the Jev
    setups, Jev's full scores, whatever part of them the model was shown.

    jev shows the model Jev's ranking and contradiction scores at every state.
    jev-contra shows only the one hypothesis Jev judges the evidence most contradicts
    (or that none is), and nothing before the first check: with no evidence yet,
    there is nothing to contradict.
    """
    if setup not in SETUPS:
        raise ValueError(f"unknown setup {setup!r}")
    if setup in JEV_SETUPS and jev is None:
        raise ValueError(f"the {setup} setup needs a Jev scorer")
    checks = scenario["checks"]
    hyps = list(scenario["hypotheses"])
    version_check = scenario["version_check"]
    trace = {"setup": setup, "forced": forced, "status": "ok", "states": [],
             "budgets": {"max_checks": max_checks},
             "observed": [], "final": None, "error": None,
             "usage": {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0, "jev_calls": 0},
             "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    t0 = time.monotonic()

    def new_state(check=None):
        state = {"check": check, "model_beliefs": None, "jev": None, "jev_error": None}
        if setup in JEV_SETUPS:
            try:
                state["jev"] = jev(scenario, list(trace["observed"]))
                trace["usage"]["jev_calls"] += 1
            except Exception as e:  # recorded, never fatal: the model continues without scores
                state["jev_error"] = f"{type(e).__name__}: {e}"
        trace["states"].append(state)
        return state

    messages = [{"role": "user", "content": build_initial_prompt(scenario, max_checks)}]
    state = new_state()
    if state["jev"] and setup == "jev":
        messages[0]["content"] += "\n\n" + format_jev_scores(state["jev"])
    strikes, finishing = 0, False

    while True:
        try:
            text, assistant_msg, usage = ask(SYSTEM_PROMPT, messages)
            trace["usage"]["llm_calls"] += 1
        except Exception as e:
            trace["status"], trace["error"] = "model_error", f"{type(e).__name__}: {e}"
            break
        for k in ("input_tokens", "output_tokens"):
            trace["usage"][k] += usage.get(k, 0)
        messages.append(assistant_msg)
        beliefs = parse_beliefs(text, hyps)
        if beliefs and state["model_beliefs"] is None:
            state["model_beliefs"] = beliefs

        must_check_first = forced and not trace["observed"]
        final = None if must_check_first else parse_final_answer(text, hyps)
        if final:
            trace["final"] = final
            break
        if finishing:
            trace["status"] = "unfinished"
            break

        available = [c for c in checks if c not in trace["observed"]]
        pick = parse_next_check(text, available)
        note = None
        if must_check_first:
            if pick != version_check:
                note = (f"(This run fixes the first check for every trial: `{version_check}` "
                        "was run in place of your pick.)")
            pick = version_check
        if pick is None:
            strikes += 1
            if strikes > 2:
                trace["status"] = "invalid_reply"
                break
            messages.append({"role": "user", "content":
                             "Your reply needs a beliefs JSON and either `NEXT: <check>` naming one of: "
                             + ", ".join(f"`{c}`" for c in available) + ", or DONE with the final JSON."})
            continue
        strikes = 0
        trace["observed"].append(pick)
        state = new_state(pick)
        left = max_checks - len(trace["observed"])
        prompt = format_observation(pick, checks[pick]["result"], state["jev"], note, left, setup)
        if left <= 0 or not [c for c in checks if c not in trace["observed"]]:
            finishing = True
            prompt += "\nNo checks left: finish now with DONE and the final JSON."
        messages.append({"role": "user", "content": prompt})

    trace["seconds"] = round(time.monotonic() - t0, 1)
    return trace


# --------------------------------------------------------------------------
# Scoring

def _top(dist):
    return max(sorted(dist), key=dist.get) if dist else None


def _leads(dist, h):
    """h is the top hypothesis, or tied for top: a tie is not a change of course."""
    return dist[h] >= max(dist.values()) - 1e-9


def measured_beliefs(trace):
    """The belief the metrics measure: the model's own stated belief at each state, in
    both setups, so alone and + Jev compare the same thing."""
    return [s.get("model_beliefs") for s in trace["states"]]


def jev_beliefs(trace):
    """Jev's scores at each state (jev setup only), reported beside the model's."""
    return [(s.get("jev") or {}).get("beliefs") for s in trace["states"]]


def belief_shift(beliefs, observed, truth, prefix=""):
    """How P(decoy) and the top hypothesis moved across the version check, and how
    many checks it took for the true cause to lead on its own."""
    decoy, vcheck, cause = truth["decoy"], truth["version_check"], truth["hypothesis"]
    first = beliefs[0] if beliefs else None
    out = {f"{prefix}decoy_led_initially": _top(first) == decoy if first else None,
           f"{prefix}p_decoy_start": round(first[decoy], 3) if first else None,
           f"{prefix}changed_direction": None, f"{prefix}p_decoy_drop": None,
           f"{prefix}p_decoy_before": None, f"{prefix}p_decoy_after": None,
           f"{prefix}decoy_held": None,
           # states[k] follows the k-th check, so the first k where the cause leads
           # outright is the number of checks it took.
           f"{prefix}checks_to_cause": next(
               (k for k, b in enumerate(beliefs)
                if b and cause in b and all(b[cause] > p for h, p in b.items() if h != cause)), None)}
    if vcheck in observed:
        k = observed.index(vcheck) + 1  # states[k] is right after the version check
        before = beliefs[k - 1] if k - 1 < len(beliefs) else None
        after = beliefs[k] if k < len(beliefs) else None
        if before and after:
            out[f"{prefix}p_decoy_before"] = round(before[decoy], 3)
            out[f"{prefix}p_decoy_after"] = round(after[decoy], 3)
            out[f"{prefix}p_decoy_drop"] = round(before[decoy] - after[decoy], 3)
            # Changing direction needs a direction to change from.
            out[f"{prefix}changed_direction"] = (not _leads(after, decoy)) if _leads(before, decoy) else None
            if _leads(before, decoy):
                # Checks, from the version check on, for which the decoy still led.
                held = 0
                for b in beliefs[k:]:
                    if not b or not _leads(b, decoy):
                        break
                    held += 1
                out[f"{prefix}decoy_held"] = held
    return out


def score_trial(trace, truth):
    observed = trace["observed"]
    out = {"finished": trace["final"] is not None,
           "jev_errors": sum(1 for s in trace["states"] if s.get("jev_error")),
           "ran_version_check": truth["version_check"] in observed}
    out.update(belief_shift(measured_beliefs(trace), observed, truth))
    jev = jev_beliefs(trace) if trace["setup"] in (*JEV_SETUPS, AGENT_SETUP) else []
    out.update(belief_shift(jev, observed, truth, prefix="jev_"))
    decoy = truth["decoy"]

    final = trace["final"]
    if final:
        text = f"{final['component']} {final['mechanism']}".lower()
        cited = set(final["evidence"])
        out["correct_hypothesis"] = final["hypothesis"] == truth["hypothesis"]
        out["blamed_decoy"] = final["hypothesis"] == decoy
        out["named_component"] = any(a in text for a in truth["component_aliases"])
        out["found_mechanism"] = (any(c in observed for c in truth["mechanism_checks"])
                                  and all(any(w in text for w in group)
                                          for group in truth["mechanism_keywords"]))
        out["evidence_all_observed"] = bool(cited) and cited <= set(observed)
        out["cited_trap"] = bool(cited & set(truth["traps"]))
        # A rubric on check IDs, not text, so it applies to every setup alike: the right
        # cause, every cited check actually run, no failed/empty query cited, and at least
        # one cited check that shows the mechanism.
        out["evidence_valid"] = (out["correct_hypothesis"] and out["evidence_all_observed"]
                                 and not out["cited_trap"] and bool(cited & set(truth["mechanism_checks"])))
    else:
        for key in ("correct_hypothesis", "blamed_decoy", "named_component", "found_mechanism",
                    "evidence_all_observed", "cited_trap", "evidence_valid"):
            out[key] = None
    out["abstained"] = bool(trace.get("abstained"))
    out["infra_failure"] = trace.get("status") in INFRA_STATUSES
    out["traps_run"] = len(set(observed) & set(truth["traps"]))
    out["verified"] = (trace.get("diagnosis") or {}).get("verified") if trace["setup"] == AGENT_SETUP else None
    tentative = (trace.get("diagnosis") or {}).get("tentative_hypothesis")
    out["tentative_correct"] = (tentative == truth["hypothesis"]) if tentative else None
    out["explanation_mechanism"] = None
    if trace["setup"] == AGENT_SETUP:
        # Jev writes no text: the text metrics do not apply, and are never earned by
        # pasting a hypothesis description or evidence. An optional explanation written
        # after the diagnosis froze is scored on its own line.
        out["found_mechanism"] = out["named_component"] = None
        exp = trace.get("explanation") or {}
        if final and exp.get("mechanism") is not None:
            text = f"{exp.get('component') or ''} {exp['mechanism']}".lower()
            out["explanation_mechanism"] = (any(c in observed for c in truth["mechanism_checks"])
                                            and all(any(w in text for w in group)
                                                    for group in truth["mechanism_keywords"]))
    out["checks_used"] = len(observed)
    out["noise_checks"] = len(set(observed) & set(truth["noise"]))
    return out


METRICS = [
    ("finished", "count", "finished the investigation"),
    ("decoy_led_initially", "count", "decoy on top at the start"),
    ("ran_version_check", "count", "ran the version check"),
    ("changed_direction", "count", "changed direction after it"),
    ("p_decoy_drop", "mean", "mean drop in P(decoy)"),
    ("decoy_held", "mean", "checks the decoy still led"),
    ("checks_to_cause", "mean", "checks until the cause led"),
    ("jev_decoy_led_initially", "count", "Jev: decoy on top at start"),
    ("jev_changed_direction", "count", "Jev: changed direction"),
    ("jev_p_decoy_drop", "mean", "Jev: mean drop in P(decoy)"),
    ("jev_checks_to_cause", "mean", "Jev: checks until the cause led"),
    ("correct_hypothesis", "count", "right hypothesis"),
    ("found_mechanism", "count", "found the mechanism"),
    ("named_component", "count", "named the component"),
    ("blamed_decoy", "count", "blamed the decoy"),
    ("evidence_all_observed", "count", "cited only checks it ran"),
    ("cited_trap", "count", "cited a failed/empty query"),
    ("evidence_valid", "count", "supported diagnosis (IDs)"),
    ("abstained", "count", "abstained"),
    ("infra_failure", "count", "infrastructure failures"),
    ("checks_used", "mean", "mean checks used"),
    ("noise_checks", "mean", "mean noise checks"),
    ("jev_errors", "sum", "Jev call errors"),
]


def _cell(scores, key, kind):
    vals = [s[key] for s in scores if s.get(key) is not None]
    if not vals:
        return "-"
    if kind == "count":
        return f"{sum(bool(v) for v in vals)}/{len(vals)}"
    if kind == "sum":
        return str(sum(vals))
    return f"{sum(vals) / len(vals):.2f}"


def report(traces, out=None):
    """One table per (scenario, model, mode), a column for each setup.
    Counts, not percentages. Each trace is scored against its own scenario's truth."""
    out = out or sys.stdout
    groups, truths = {}, {}
    for t in traces:
        name = t.get("scenario", DEFAULT_SCENARIO)
        if name not in truths:
            truths[name] = load_ground_truth(name)
        groups.setdefault((name, t.get("model", "?"), t.get("forced", False),
                           t.get("harness_version")), {}).setdefault(
            t["setup"], []).append(score_trial(t, truths[name]))
    if not groups:
        print("No trials.", file=out)
        return
    for (name, model, forced, version), by_setup in sorted(groups.items(), key=lambda g: str(g[0])):
        mode = "forced version check" if forced else "free choice"
        print(f"\n{name} | {model} | {mode} | harness v{version}", file=out)
        cols = SETUPS if set(by_setup) & set(SETUPS) else (AGENT_SETUP,)
        print(f"  {'':<30}" + "".join(f"{s:>12}" for s in cols), file=out)
        print(f"  {'trials':<30}" + "".join(f"{len(by_setup.get(s, [])):>12}" for s in cols),
              file=out)
        for key, kind, label in METRICS:
            print(f"  {label:<30}" + "".join(f"{_cell(by_setup.get(s, []), key, kind):>12}"
                                            for s in cols), file=out)


def trial_key(t):
    """What makes two traces the same trial: resuming it replaces the earlier one."""
    return (*cell_key(t), t.get("trial"))


def cell_key(t):
    """The scenario, model, setup, mode and harness version a trial belongs to."""
    return (t.get("scenario", DEFAULT_SCENARIO), t.get("scenario_digest"), t.get("model"), t["setup"],
            bool(t.get("forced")), t.get("harness_version"))


def read_traces(path):
    """Every trace in the file, except an attempt that failed with a model or network
    error and was later retried (what --resume does): the retry replaces it. The file
    itself stays append-only, so every attempt remains on record."""
    by_key = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                t = json.loads(line)
                kept = [p for p in by_key.get(trial_key(t), []) if p.get("status") not in INFRA_STATUSES]
                by_key[trial_key(t)] = kept + [t]
    return [t for group in by_key.values() for t in group]


# --------------------------------------------------------------------------
# CLI

def write_html(traces, path, note=None):
    if not path:
        return
    import rca_report  # imports this module
    rca_report.write(traces, path, note)
    print(f"wrote {path}")


def check_access(models, want_jev, jev_model, env=None):
    """One tiny call per model and one Jev call. Returns True if all work."""
    ok = True
    for provider, model in models:
        t0 = time.monotonic()
        try:
            ask = model_client(provider, model, env)
            text, _, _ = ask("Reply with the single word OK.", [{"role": "user", "content": "Ping"}])
            print(f"ok    {provider}:{model} ({time.monotonic() - t0:.1f}s): {text.strip()[:40]!r}")
        except Exception as e:
            ok = False
            print(f"FAIL  {provider}:{model}: {e}")
    if want_jev:
        env = os.environ if env is None else env
        t0 = time.monotonic()
        try:
            scores = jev_scorer(env.get("TYPESAFE_API_KEY"), jev_model)(load_scenario(), [])
            top = _top(scores["beliefs"])
            print(f"ok    jev {scores['model']} ({time.monotonic() - t0:.1f}s): top at start is {top!r}")
        except Exception as e:
            ok = False
            print(f"FAIL  jev: {e}")
    return ok


def main(argv=None):
    """The CLI. --scenario-dir changes the module's scenario directory for this call only."""
    global SCENARIO_DIR
    saved = SCENARIO_DIR
    try:
        return _main(argv)
    finally:
        SCENARIO_DIR = saved


def _main(argv=None):
    ap = argparse.ArgumentParser(
        description="RCA experiment: does the agent change direction, alone vs with Jev?")
    ap.add_argument("--models", default="anthropic",
                    help="comma-separated provider:model, e.g. anthropic:claude-opus-5,openai:gpt-5 "
                         f"(provider alone uses its default: {DEFAULT_MODELS})")
    ap.add_argument("--scenarios", help="comma-separated scenario names (default: all in the scenario directory)")
    ap.add_argument("--scenario-dir", metavar="DIR",
                    help="read scenarios and truth files from DIR instead of rca_scenarios/, e.g. ones "
                         "built from recorded incidents by rca_recorded.py; pass it to --report and "
                         "--compare too")
    ap.add_argument("--setup", default="all",
                    help="comma-separated setups from alone, jev (Jev's ranking and contradiction "
                         "scores), jev-contra (only the hypothesis Jev judges most contradicted), "
                         "jev-agent (Jev investigates, no LLM in the loop; run once per scenario and "
                         "trial, not per model), or all (the three LLM setups)")
    ap.add_argument("--max-rounds", type=int,
                    help="jev-agent: decision rounds allowed (default: --max-checks + 1)")
    ap.add_argument("--agent-policy", default="v5", choices=("v1", "v2", "v3", "v4", "v5", "v6"),
                    help="jev-agent decision rule: v6 adds to v5 a what-failed phase (up to 2 checks "
                         "inside the answer) and a plain-text evidence report; v5 (default) is v3 "
                         "plus a check that challenges the "
                         "leading hypothesis once one leads; v4 is v5 as first run, which by mistake "
                         "verified under v2's rule; v3 always answers, like the LLM setups, and "
                         "counts an answer verified only if every alternative that was ever plausible is "
                         "ruled out; v2 is the same but exempts alternatives the ranking has outranked; "
                         "v1 abstains without a verified candidate")
    ap.add_argument("--explain-model", metavar="PROVIDER:MODEL",
                    help="jev-agent: after the diagnosis is frozen, have this LLM write an explanation "
                         "(recorded apart; it cannot change the diagnosis)")
    ap.add_argument("--compare", metavar="JSONL", nargs="+",
                    help="compare saved traces from several files: LLM alone, LLM + Jev and jev-agent, "
                         "with calls, latency, tokens and cost")
    ap.add_argument("--pricing", help="price table for --compare (default: rca_pricing.json)")
    ap.add_argument("--trials", type=int, default=1, help="trials per scenario, model and setup")
    ap.add_argument("--forced", action="store_true",
                    help="run the version check first in every trial, so every trial sees the contradiction")
    ap.add_argument("--max-checks", type=int, default=MAX_CHECKS)
    ap.add_argument("--max-tokens", type=int,
                    help="cap each model reply; stops a model that rambles past the reply format "
                         "from spending thousands of tokens per turn")
    ap.add_argument("--jev-model", default=triage.MODEL)
    ap.add_argument("--out", default=TRACES_PATH, help="JSONL file each trial is appended to")
    ap.add_argument("--resume", action="store_true",
                    help="skip trials already finished in --out (a model or network error is retried)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompts, the Jev payload and the plan; call nothing")
    ap.add_argument("--check", action="store_true",
                    help="make one small call to each model and to Jev, then stop")
    ap.add_argument("--report", metavar="JSONL", help="score saved traces instead of running")
    ap.add_argument("--html", metavar="PATH",
                    help="also write the leaderboard as an HTML page (with --report or after a run)")
    ap.add_argument("--note", help="a notice shown at the top of the HTML page")
    args = ap.parse_args(argv)
    global SCENARIO_DIR
    if args.scenario_dir:
        if not os.path.isdir(args.scenario_dir):
            ap.error(f"--scenario-dir {args.scenario_dir!r} is not a directory")
        SCENARIO_DIR = os.path.abspath(args.scenario_dir)

    if args.compare:
        import rca_jev_agent
        traces = [t for path in args.compare for t in read_traces(path)]
        rca_jev_agent.print_compare(traces, rca_jev_agent.load_pricing(args.pricing))
        write_html(traces, args.html, args.note)
        return 0

    if args.report:
        traces = read_traces(args.report)
        report(traces)
        write_html(traces, args.html, args.note)
        return 0

    available = scenario_names()
    names = [n.strip() for n in args.scenarios.split(",") if n.strip()] if args.scenarios else available
    unknown = sorted(set(names) - set(available))
    if unknown:
        ap.error(f"unknown scenario(s) {', '.join(unknown)}; available: {', '.join(available)}")
    scenarios = {n: load_scenario(n) for n in names}
    if args.forced and [n for n, sc in scenarios.items() if not sc.get("version_check")]:
        ap.error("--forced needs a designated key check, and these scenarios have none: "
                 + ", ".join(n for n, sc in scenarios.items() if not sc.get("version_check")))
    try:
        models = [parse_model_spec(s) for s in args.models.split(",") if s.strip()]
    except ValueError as e:
        ap.error(str(e))
    setups = list(SETUPS) if args.setup == "all" else [s.strip() for s in args.setup.split(",") if s.strip()]
    if not setups or set(setups) - set(ALL_SETUPS):
        ap.error(f"--setup takes all, or a comma-separated list of: {', '.join(ALL_SETUPS)}")
    agent = AGENT_SETUP in setups
    llm_setups = [s for s in setups if s != AGENT_SETUP]
    uses_jev = bool(set(setups) & {*JEV_SETUPS, AGENT_SETUP})
    explain_spec = None
    if args.explain_model:
        try:
            explain_spec = parse_model_spec(args.explain_model)
        except ValueError as e:
            ap.error(str(e))
    # Jev investigates on its own: one trial per scenario and trial number, credited to
    # Jev (model "jev:<jev model>"), never to an LLM.
    plan = []
    for i in range(args.trials):
        for n in names:
            plan += [(i, n, p, m, s) for p, m in models for s in llm_setups]
            if agent:
                plan.append((i, n, "jev", args.jev_model, AGENT_SETUP))

    if args.dry_run:
        print("=== system prompt (every scenario and setup) ===\n" + SYSTEM_PROMPT)
        for n, scenario in scenarios.items():
            print(f"\n=== {n}: first user message (jev adds Jev's starting scores; jev-contra adds nothing) ===\n"
                  + build_initial_prompt(scenario, args.max_checks))
            if set(setups) & set(JEV_SETUPS):
                print(f"\n=== {n}: Jev payload after the version check ===")
                print(json.dumps(build_jev_payload(scenario, [scenario["version_check"]] if scenario.get("version_check") else [],
                                                   args.jev_model), indent=2))
            if agent:
                import rca_jev_agent
                print(f"\n=== {n}: jev-agent round request after the version check ===")
                print(json.dumps(rca_jev_agent.round_payload(scenario, [scenario["version_check"]] if scenario.get("version_check") else [],
                                                             args.jev_model, args.agent_policy), indent=2))
        print(f"\n=== plan: {len(plan)} trials, {'forced' if args.forced else 'free'} mode ===")
        for i, n, p, m, s in plan:
            print(f"  trial {i + 1}  {n}  {p}:{m}  {s}")
        return 0

    llm_models = models if llm_setups else []
    if explain_spec and explain_spec not in llm_models:
        llm_models = [*llm_models, explain_spec]
    if args.check:
        return 0 if check_access(llm_models, uses_jev, args.jev_model) else 1

    try:
        clients = {(p, m): model_client(p, m, max_tokens=args.max_tokens) for p, m in llm_models}
        jev = (jev_scorer(os.environ.get("TYPESAFE_API_KEY"), args.jev_model)
               if set(llm_setups) & set(JEV_SETUPS) else None)
        if agent:
            import rca_jev_agent
            ask_jev = rca_jev_agent.jev_asker(os.environ.get("TYPESAFE_API_KEY"))
    except ModelError as e:
        sys.exit(f"error: {e}")

    run_id = uuid.uuid4().hex[:8]
    done, taken = set(), {}
    if os.path.exists(args.out):
        existing = read_traces(args.out)
        if args.resume:
            done = {trial_key(t) for t in existing if t.get("status") not in INFRA_STATUSES}
        else:
            # A new run into the same file adds trials after the ones already there,
            # instead of reusing their numbers and replacing them.
            for t in existing:
                taken[cell_key(t)] = max(taken.get(cell_key(t), 0), t.get("trial") or 0)
    digests = {n: scenario_digest(s) for n, s in scenarios.items()}

    def number(i, n, p, m, s):
        return taken.get((n, digests[n], f"{p}:{m}", s, args.forced, HARNESS_VERSION), 0) + i + 1

    todo = [(number(i, n, p, m, s), n, p, m, s) for i, n, p, m, s in plan
            if (n, digests[n], f"{p}:{m}", s, args.forced, HARNESS_VERSION, i + 1) not in done]
    print(f"run {run_id}: {len(todo)} of {len(plan)} trials to run, "
          f"{'forced' if args.forced else 'free'} mode, appending to {args.out}")
    traces = []
    for k, (i, name, provider, model, setup) in enumerate(todo, 1):
        if setup == AGENT_SETUP:
            trace = rca_jev_agent.run_agent_trial(
                scenarios[name], ask_jev, args.forced, args.max_checks, args.max_rounds, args.jev_model,
                clients[explain_spec] if explain_spec else None, args.agent_policy)
            trace["explain_model"] = f"{explain_spec[0]}:{explain_spec[1]}" if explain_spec else None
        else:
            trace = run_trial(scenarios[name], setup, clients[(provider, model)],
                              jev if setup in JEV_SETUPS else None, args.forced, args.max_checks)
        trace.update({"run_id": run_id, "trial": i, "scenario": name,
                      "scenario_dir": (os.path.relpath(SCENARIO_DIR, BASE)
                                       if SCENARIO_DIR != os.path.join(BASE, "rca_scenarios") else None),
                      "scenario_digest": digests[name],
                      "model": f"{provider}:{model}",
                      "jev_model": args.jev_model if setup in (*JEV_SETUPS, AGENT_SETUP) else None,
                      "harness_version": HARNESS_VERSION})
        with open(args.out, "a", encoding="utf-8") as f:
            f.write(json.dumps(trace) + "\n")
        traces.append(trace)
        s = score_trial(trace, load_ground_truth(name))
        answer = (trace["final"]["hypothesis"] if trace["final"] else
                  "abstain" if trace.get("abstained") else trace["status"])
        print(f"[{k}/{len(todo)}] {name} {provider}:{model} {setup:<5} checks={s['checks_used']} "
              f"changed_direction={s['changed_direction']} answer={answer} ({trace['seconds']}s)")
    if args.resume:
        traces = read_traces(args.out)  # the whole run so far, not just this sitting
    report(traces)
    write_html(traces, args.html, args.note)
    return 0


if __name__ == "__main__":
    # Run the importable module's main, not this __main__ copy: rca_jev_agent and
    # rca_report import rca_experiment by name, and --scenario-dir must change the
    # SCENARIO_DIR they read, not a second copy of it.
    import rca_experiment
    sys.exit(rca_experiment.main())
