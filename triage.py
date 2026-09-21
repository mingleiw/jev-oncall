#!/usr/bin/env python3
"""Jev incident triage, v2: Jev judges, plain code decides.

One Jev call per production alert asks four typed questions:

    actionable    Noul    does a human need to intervene?
    severity      Score   SEV4 < SEV3 < SEV2 < SEV1; routing uses the distribution
    team          Choice  database / compute / network / deploy
    duplicate_of  Choice  which candidate alert directly causes this one, or "none"

The policy is plain code on those probabilities:

  * Rules first: non-production alerts never page and never cost a model call.
  * Page on P(SEV1) + P(SEV2), not on the top label. The uncertain middle goes
    to REVIEW: low urgency, escalating to a page if nobody acks it in time.
  * DROP is the only silent outcome, so it needs the most certainty, and the
    actionable answer can drop an alert only when severity agrees it's noise.
  * Dedup is a graph over duplicate_of answers. Cycles are broken, each
    cluster's root carries the cluster's most urgent action, and a member
    owned by a different team gets a REVIEW instead of silence.
  * Fail-open: a Jev error, timeout, or malformed answer routes the alert by
    its configured severity, exactly as it would be routed without Jev.

Usage:
    export TYPESAFE_API_KEY=<key from console.typesafe.ai/keys>
    python3 triage.py                        # alerts.json -> results.json
    python3 triage.py --alerts history.jsonl --out replay.json \\
        --timeout 10 --retries 3 --max-wait 30
    python3 evaluate.py replay.json --sweep  # offline metrics and threshold sweep

Standard library only.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
API_URL = "https://api.typesafe.ai/v1/systemone"

# Pinned rather than "jev-latest": an alias moves when TypeSafe ships a
# release, which can shift probabilities under the thresholds in Policy.
# Re-run `evaluate.py --sweep` on replayed history before moving the pin.
MODEL = "jev-1.13.0"
USD_PER_M_INPUT_TOKENS = 0.042  # output tokens are free

SEV_LEVELS = ["SEV4", "SEV3", "SEV2", "SEV1"]  # Score level i is SEV_LEVELS[i]
SEV_CRITERIA = [
    "SEV4: no customer impact, noise or todo",
    "SEV3: minor impact, fix in business hours",
    "SEV2: degraded service or major feature broken, urgent but not a full outage",
    "SEV1: customer-facing outage or data-loss risk",
]
TEAM_CRITERIA = {
    "database": "databases, caches, queues, storage",
    "compute": "servers, containers, batch jobs, stream processing",
    "network": "CDN, edge, DNS, TLS, connectivity",
    "deploy": "releases, deploys, canary analysis, CI/CD",
}

# The instructions state the policy in general terms. v1 illustrated "not
# actionable" with examples that matched the test alerts one-for-one.
ACTIONABLE_Q = (
    "Does this alert need a human to intervene? Yes if something is broken, "
    "degrading, or at concrete risk, and a person's action would prevent or "
    "reduce the impact. No if the alert is informational, already resolved, "
    "or no human action would change the outcome."
)
SEVERITY_Q = "Rate the incident severity of this alert by its customer impact."
TEAM_Q = "Which team owns the fix for this alert?"
DUPLICATE_Q = (
    "Is this alert a downstream symptom of one of the listed alerts? Pick the "
    "alert that directly causes it, or 'none'. Similar symptoms or a shared "
    "time window are not enough on their own."
)
NONE = "none"

# Ascending urgency. REVIEW is a low-urgency notification that escalates to a
# page unless someone acks it within Policy.review_ack_min.
ACTIONS = ["DROP", "LOG", "TICKET", "REVIEW", "PAGE", "PAGE_NOW"]
RANK = {a: i for i, a in enumerate(ACTIONS)}
PAGING = ("PAGE", "PAGE_NOW")
STATIC_ROUTES = {"critical": "PAGE", "warning": "TICKET", "info": "LOG"}
FALLBACK_TEAM = "triage-rotation"
PROD_ENVS = ("prod", "production")


@dataclass
class Policy:
    """Routing thresholds. These are starting points, not tuned values:
    choose them with `evaluate.py --sweep` on replayed, labeled history."""
    page_bar: float = 0.80      # P(page) at or above: page
    no_page_bar: float = 0.20   # P(page) at or below: don't page. In between: REVIEW
    drop_bar: float = 0.05      # P(actionable) at or below, when not paging: DROP
    dedup_bar: float = 0.70     # P(duplicate_of = X) at or above: link to X
    team_bar: float = 0.60      # owner below this: a page or review also notifies the runner-up
    window_min: float = 30.0    # a cause starts at most this long before its symptom...
    skew_min: float = 2.0       # ...or at most this long after (alert delivery jitter)
    max_candidates: int = 50    # Choice allows 255 options; fewer keeps the question short
    review_ack_min: int = 15    # a REVIEW nobody acks within this becomes a page


class JevError(Exception):
    """Any reason Jev's answer for an alert can't be used. Always falls back."""


@dataclass
class Judgment:
    """Jev's answers for one alert, as normalized probability distributions."""
    model: str
    p_actionable: float
    severity: dict                    # SEV4..SEV1 -> probability
    team: dict                        # team -> probability
    duplicate_of: dict | None = None  # candidate id or "none" -> probability

    @property
    def p_page(self) -> float:
        return self.severity["SEV1"] + self.severity["SEV2"]


@dataclass
class Decision:
    id: str
    action: str       # an ACTIONS entry, or "DEDUP" (handled by the cluster root)
    standalone: str   # what this alert alone would get, before dedup
    team: str
    source: str       # "jev" | "rule" | "fallback"
    linked_to: str | None = None                  # incident root, if clustered
    notify: list = field(default_factory=list)    # extra teams, low urgency
    reasons: list = field(default_factory=list)


def top(dist: dict) -> str:
    """Most likely option; ties go to the alphabetically first, deterministically."""
    return max(sorted(dist), key=dist.get)


# --------------------------------------------------------------------------
# Loading

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_alerts(path):
    """A JSON list of alerts, or JSONL with one alert per line."""
    with open(path, encoding="utf-8") as f:
        if path.endswith(".jsonl"):
            alerts = [json.loads(line) for line in f if line.strip()]
        else:
            alerts = json.load(f)
    ids = [a.get("id") for a in alerts]
    if not all(ids) or len(set(ids)) != len(ids):
        sys.exit(f"{path}: every alert needs a unique, non-empty id")
    if NONE in ids:
        sys.exit(f"{path}: {NONE!r} is reserved and can't be an alert id")
    return alerts


def parse_time(value):
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def is_prod(alert) -> bool:
    return str(alert.get("env", "prod")).lower() in PROD_ENVS


# --------------------------------------------------------------------------
# Asking Jev

def upstream(service, topology):
    """The service plus everything it depends on, transitively."""
    seen, stack = {service}, list(topology.get(service, []))
    while stack:
        s = stack.pop()
        if s not in seen:
            seen.add(s)
            stack.extend(topology.get(s, []))
    return seen


def candidate_causes(alert, alerts, topology, policy):
    """Alerts that could plausibly cause this one: production, started inside
    the window, and on this service or upstream of it when the topology knows
    the service. Deterministic filtering in code; Jev only picks among these."""
    t = parse_time(alert.get("started_at"))
    if not is_prod(alert) or t is None:
        return []
    service = alert.get("service")
    allowed = upstream(service, topology) if service in topology else None
    scored = []
    for other in alerts:
        if other["id"] == alert["id"] or not is_prod(other):
            continue
        if allowed is not None and other.get("service") not in allowed:
            continue
        t_other = parse_time(other.get("started_at"))
        if t_other is None:
            continue
        lead_min = (t - t_other).total_seconds() / 60  # > 0: the other started first
        if -policy.skew_min <= lead_min <= policy.window_min:
            scored.append((abs(lead_min), other["id"], other))
    scored.sort(key=lambda s: (s[0], s[1]))
    return [other for _, _, other in scored[: policy.max_candidates]]


def describe(alert):
    t = parse_time(alert.get("started_at"))
    when = t.astimezone(timezone.utc).strftime("%H:%M UTC") if t else "unknown time"
    return f"{alert['title']} (service {alert.get('service', 'unknown')}, started {when})"


def build_payload(alert, candidates, model=MODEL):
    questions = {
        "actionable": {"type": "noul", "instructions": ACTIONABLE_Q},
        "severity": {"type": "score", "instructions": SEVERITY_Q, "criteria": SEV_CRITERIA},
        "team": {"type": "choice", "instructions": TEAM_Q, "criteria": TEAM_CRITERIA},
    }
    if candidates:
        options = {c["id"]: describe(c) for c in candidates}
        options[NONE] = "None of these alerts causes this one"
        questions["duplicate_of"] = {
            "type": "choice", "instructions": DUPLICATE_Q, "criteria": options}
    fields = ("title", "description", "service", "started_at")
    return {
        "model": model,
        "state": {"alert": {k: alert[k] for k in fields if alert.get(k)}},
        "questions": questions,
    }


def _retry_after(err):
    try:
        return float(err.headers.get("Retry-After"))
    except (TypeError, ValueError, AttributeError):
        return None


def call_jev(payload, api_key, timeout_s=2.0, retries=1, max_wait_s=1.0):
    """POST one request. Retries 429s, 5xx and network errors, but raises
    JevError rather than wait longer than max_wait_s between attempts: on a
    paging path, falling back beats waiting."""
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    last = "no attempt made"
    for attempt in range(retries + 1):
        wait = 0.2 * 2 ** attempt
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data, (time.monotonic() - t0) * 1000
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code != 429 and e.code < 500:
                break  # other 4xx errors won't fix themselves
            wait = _retry_after(e) or wait
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < retries:
            if wait > max_wait_s:
                last += f" (retry would wait {wait:g}s)"
                break
            time.sleep(wait)
    raise JevError(last)


def _distribution(raw, options, what):
    if not isinstance(raw, dict):
        raise JevError(f"{what}: no probabilities")
    dist = dict.fromkeys(options, 0.0)
    for key, value in raw.items():
        if key not in dist:
            raise JevError(f"{what}: unexpected option {key!r}")
        p = float(value)
        if not 0.0 <= p <= 1.0:
            raise JevError(f"{what}: probability {p} out of range")
        dist[key] = p
    total = sum(dist.values())
    if abs(total - 1.0) > 0.05:
        raise JevError(f"{what}: probabilities sum to {total:.3f}")
    return {k: v / total for k, v in dist.items()}


def parse_answers(resp, candidate_ids):
    """Validate a response and normalize it into a Judgment, or raise JevError.
    Jev guarantees the schema; checking anyway is cheap on a paging path."""
    try:
        answers = resp["answers"]
        p_act = float(answers["actionable"]["noul"])
        if not 0.0 <= p_act <= 1.0:
            raise JevError(f"actionable: probability {p_act} out of range")
        severity = {}
        for key, value in answers["severity"]["probabilities"].items():
            level = int(key)
            if not 0 <= level < len(SEV_LEVELS):
                raise JevError(f"severity: unexpected level {key!r}")
            severity[SEV_LEVELS[level]] = value
        duplicate_of = None
        if candidate_ids:
            duplicate_of = _distribution(answers["duplicate_of"]["probabilities"],
                                         [*candidate_ids, NONE], "duplicate_of")
        return Judgment(
            model=str(resp.get("model", "unknown")),
            p_actionable=p_act,
            severity=_distribution(severity, SEV_LEVELS, "severity"),
            team=_distribution(answers["team"]["probabilities"], list(TEAM_CRITERIA), "team"),
            duplicate_of=duplicate_of,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise JevError(f"malformed response ({type(e).__name__}: {e})") from e


def judge_all(alerts, candidates, api_key, model=MODEL, timeout_s=2.0, retries=1,
              max_wait_s=1.0, workers=8):
    """One Jev call per production alert, in parallel. Returns (judgments,
    errors, calls). An alert Jev couldn't judge has an error and no judgment."""
    todo = [a for a in alerts if is_prod(a)]
    judgments, errors, calls = {}, {}, {}
    if not api_key:
        errors.update({a["id"]: "TYPESAFE_API_KEY not set" for a in todo})
        return judgments, errors, calls

    def one(alert):
        aid, cands = alert["id"], candidates[alert["id"]]
        try:
            payload = build_payload(alert, cands, model)
            resp, ms = call_jev(payload, api_key, timeout_s, retries, max_wait_s)
            judgment = parse_answers(resp, [c["id"] for c in cands])
            return aid, judgment, None, {"ms": round(ms), "usage": resp.get("usage", {})}
        except Exception as e:  # fail-open: whatever broke, the alert still gets routed
            error = str(e) if isinstance(e, JevError) else f"{type(e).__name__}: {e}"
            return aid, None, error, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for aid, judgment, error, call in pool.map(one, todo):
            if judgment is not None:
                judgments[aid] = judgment
            if error:
                errors[aid] = error
            if call:
                calls[aid] = call
    return judgments, errors, calls


# --------------------------------------------------------------------------
# Deciding: plain code, no I/O

def static_route(alert, error):
    """Fail-open: route exactly as the alert would be routed without Jev."""
    sev = str(alert.get("configured_severity", "")).lower()
    action = STATIC_ROUTES.get(sev, "PAGE")
    basis = f"configured severity {sev!r}" if sev in STATIC_ROUTES else "no configured severity, so page"
    return Decision(alert["id"], action, action, alert.get("owner") or FALLBACK_TEAM,
                    "fallback", reasons=[f"Jev unavailable ({error}); routed by {basis}"])


def route_standalone(alert, judgment, policy, error=None):
    """What this alert would get on its own, before dedup."""
    aid = alert["id"]
    if not is_prod(alert):
        return Decision(aid, "LOG", "LOG", alert.get("owner") or FALLBACK_TEAM, "rule",
                        reasons=[f"env={alert.get('env')}: non-production never pages "
                                 "(rule, no model call)"])
    if judgment is None:
        return static_route(alert, error or "no judgment")

    j = judgment
    p_page, p_act, team = j.p_page, j.p_actionable, top(j.team)
    reasons = [f"P(page)={p_page:.2f} P(actionable)={p_act:.2f} team={team}@{j.team[team]:.2f}"]
    if p_act < 0.5 <= p_page:
        reasons.append("answers disagree (pageable severity, not actionable): "
                       "the more urgent answer wins")
    if p_page >= policy.page_bar:
        action = "PAGE_NOW" if j.severity["SEV1"] >= j.severity["SEV2"] else "PAGE"
    elif p_page > policy.no_page_bar:
        action = "REVIEW"
        reasons.append(f"unsure whether to page ({policy.no_page_bar} < P(page) < "
                       f"{policy.page_bar}): a human decides within "
                       f"{policy.review_ack_min}m or it pages")
    elif p_act <= policy.drop_bar:
        action = "DROP"
    else:
        action = "TICKET"
        if p_act < 0.5:
            reasons.append(f"probably noise, but P(actionable) > {policy.drop_bar}: "
                           "ticket, not drop")

    decision = Decision(aid, action, action, team, "jev", reasons=reasons)
    if RANK[action] >= RANK["REVIEW"] and j.team[team] < policy.team_bar:
        runner_up = sorted(j.team, key=lambda t: (-j.team[t], t))[1]
        decision.notify.append(runner_up)
        reasons.append(f"owner uncertain: also notifying {runner_up} "
                       f"({j.team[runner_up]:.2f})")
    return decision


def resolve_clusters(alerts, decisions, judgments, policy):
    """Turn duplicate_of answers into incident clusters, in place."""
    started = {a["id"]: parse_time(a.get("started_at")) for a in alerts}

    # 1. At most one edge per alert: its most likely cause, if likely enough.
    parent, confidence = {}, {}
    for aid in sorted(judgments):
        causes = {k: v for k, v in (judgments[aid].duplicate_of or {}).items() if k != NONE}
        if not causes:
            continue
        cause = top(causes)
        if causes[cause] < policy.dedup_bar:
            continue
        if decisions[cause].standalone in ("DROP", "LOG"):
            decisions[aid].reasons.append(
                f"duplicate_of {cause} ({causes[cause]:.2f}) ignored: "
                f"{cause} is {decisions[cause].standalone}")
            continue
        parent[aid], confidence[aid] = cause, causes[cause]

    # 2. With one parent per alert, any cycle is a simple loop. Two alerts
    # naming each other must not page nobody: break the loop at whichever
    # started first, since a cause can't start after its symptom.
    def started_first(ids):
        floor = datetime.min.replace(tzinfo=timezone.utc)
        return min(ids, key=lambda i: (started[i] is None, started[i] or floor, i))

    for start in sorted(parent):
        path, node = [], start
        while node in parent and node not in path:
            path.append(node)
            node = parent[node]
        if node in path:
            loop = path[path.index(node):]
            root = started_first(loop)
            del parent[root]
            decisions[root].reasons.append(
                f"dedup cycle {' -> '.join(loop + [loop[0]])} broken at {root} (started first)")

    def root_of(aid):
        while aid in parent:
            aid = parent[aid]
        return aid

    clusters = {}
    for aid in sorted(decisions):
        clusters.setdefault(root_of(aid), []).append(aid)

    # 3. The root carries the cluster's most urgent action. Members owned by
    # the root's team are deduped; other teams get a REVIEW, never silence.
    for root, members in clusters.items():
        if len(members) == 1:
            continue
        rd = decisions[root]
        most_urgent = max(members, key=lambda m: RANK[decisions[m].standalone])
        if RANK[decisions[most_urgent].standalone] > RANK[rd.action]:
            rd.action = decisions[most_urgent].standalone
            rd.reasons.append(f"escalated to {rd.action}: cluster member {most_urgent} needs it")
        for m in members:
            if m == root:
                continue
            md = decisions[m]
            md.linked_to = root
            md.reasons.append(f"duplicate_of {parent[m]} ({confidence[m]:.2f}); incident root {root}")
            if md.team != rd.team and RANK[md.standalone] >= RANK["REVIEW"]:
                md.action = "REVIEW"
                md.reasons.append(f"root is owned by {rd.team}, so {md.team} gets a REVIEW, "
                                  "not silence")
            else:
                md.action = "DEDUP"
    return decisions


def route_all(alerts, judgments, errors, policy):
    """The whole policy. Pure: same inputs, same decisions. evaluate.py
    re-runs it over stored answers to sweep thresholds offline."""
    decisions = {a["id"]: route_standalone(a, judgments.get(a["id"]), policy, errors.get(a["id"]))
                 for a in alerts}
    return resolve_clusters(alerts, decisions, judgments, policy)


def check_invariants(decisions):
    """Safety properties that must hold whatever Jev says."""
    problems = []
    for d in decisions.values():
        if d.action == "DEDUP" and not d.linked_to:
            problems.append(f"{d.id}: DEDUP without an incident root")
        if d.linked_to:
            root = decisions[d.linked_to]
            if root.linked_to:
                problems.append(f"{d.id}: linked to {root.id}, which is not a root")
            if RANK[root.action] < RANK[d.standalone]:
                problems.append(f"{d.id}: needs {d.standalone} but its root {root.id} "
                                f"only gets {root.action}")
        if d.action == "DROP" and d.source != "jev":
            problems.append(f"{d.id}: dropped without a model judgment")
    return problems


# --------------------------------------------------------------------------
# CLI

def _percentile(sorted_values, q):
    if not sorted_values:
        return None
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]


def summarize(alerts, decisions, judgments, calls, wall_ms, problems):
    ms = sorted(c["ms"] for c in calls.values())
    tokens_in = sum(c["usage"].get("input_tokens", 0) for c in calls.values())
    tokens_out = sum(c["usage"].get("output_tokens", 0) for c in calls.values())
    actions = {}
    for d in decisions.values():
        actions[d.action] = actions.get(d.action, 0) + 1
    return {
        "alerts": len(alerts),
        "jev_answered": len(judgments),
        "rule": sum(d.source == "rule" for d in decisions.values()),
        "fallback": sum(d.source == "fallback" for d in decisions.values()),
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cost_usd": round(tokens_in * USD_PER_M_INPUT_TOKENS / 1e6, 6),
        "latency_ms_p50": _percentile(ms, 0.50),
        "latency_ms_p95": _percentile(ms, 0.95),
        "wall_ms": round(wall_ms),
        "actions": dict(sorted(actions.items(), key=lambda kv: -RANK.get(kv[0], -1))),
        "invariant_violations": problems,
    }


def print_table(alerts, decisions, judgments, verbose=False):
    print(f"{'id':<6}{'action':<16}{'team':<20}{'P(page)':>8}{'P(act)':>8}  source")
    for a in alerts:
        d, j = decisions[a["id"]], judgments.get(a["id"])
        action = f"{d.action}->{d.linked_to}" if d.linked_to else d.action
        team = d.team + (f" +{','.join(d.notify)}" if d.notify else "")
        p_page = f"{j.p_page:.2f}" if j else "-"
        p_act = f"{j.p_actionable:.2f}" if j else "-"
        print(f"{d.id:<6}{action:<16}{team:<20}{p_page:>8}{p_act:>8}  {d.source}")
        if verbose:
            for reason in d.reasons:
                print(f"{'':6}- {reason}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Triage alerts with Jev: Jev judges, code decides.")
    ap.add_argument("--alerts", default=os.path.join(BASE, "alerts.json"),
                    help="JSON list or JSONL of alerts (default: alerts.json)")
    ap.add_argument("--topology", default=os.path.join(BASE, "topology.json"),
                    help="optional service -> upstream services map (default: topology.json)")
    ap.add_argument("--out", default=os.path.join(BASE, "results.json"))
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--timeout", type=float, default=2.0,
                    help="seconds per attempt; keep it short on a paging path")
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--max-wait", type=float, default=1.0,
                    help="longest backoff before giving up and falling back")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print the reasons behind every decision")
    args = ap.parse_args(argv)

    alerts = load_alerts(args.alerts)
    topology = load_json(args.topology) if os.path.exists(args.topology) else {}
    policy = Policy()
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print("WARNING: TYPESAFE_API_KEY is not set, so every alert is routed by its "
              "configured severity (the fail-open path).\n", file=sys.stderr)

    candidates = {a["id"]: candidate_causes(a, alerts, topology, policy) for a in alerts}
    t0 = time.monotonic()
    judgments, errors, calls = judge_all(alerts, candidates, api_key, args.model,
                                         args.timeout, args.retries, args.max_wait, args.workers)
    wall_ms = (time.monotonic() - t0) * 1000
    decisions = route_all(alerts, judgments, errors, policy)
    problems = check_invariants(decisions)

    print_table(alerts, decisions, judgments, args.verbose)
    models = sorted({j.model for j in judgments.values()})
    if models and models != [args.model]:
        print(f"\nWARNING: Jev answered as {', '.join(models)}, but the policy thresholds "
              f"were set up for {args.model}.", file=sys.stderr)

    summary = summarize(alerts, decisions, judgments, calls, wall_ms, problems)
    results = {
        "meta": {
            "version": 2,
            "model_requested": args.model,
            "models_answered": models,
            "policy": asdict(policy),
            "alerts_path": os.path.abspath(args.alerts),
            "topology": topology,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "summary": summary,
        "alerts": [
            {
                **asdict(decisions[a["id"]]),
                "candidates": [c["id"] for c in candidates[a["id"]]],
                "judgment": asdict(judgments[a["id"]]) if a["id"] in judgments else None,
                "error": errors.get(a["id"]),
                "call": calls.get(a["id"]),
            }
            for a in alerts
        ],
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    s = summary
    print(f"\n{s['alerts']} alerts: Jev {s['jev_answered']}, rule {s['rule']}, "
          f"fallback {s['fallback']}")
    if calls:
        print(f"{s['input_tokens']} input tokens = ${s['cost_usd']:.5f} | per call "
              f"p50 {s['latency_ms_p50']}ms, p95 {s['latency_ms_p95']}ms | "
              f"wall {s['wall_ms']}ms with {args.workers} workers")
    print("actions: " + ", ".join(f"{k} {v}" for k, v in s["actions"].items()))
    print(f"wrote {args.out}")

    if any(a.get("expected") for a in alerts):
        import evaluate  # local import: evaluate.py imports this module
        print()
        evaluate.print_report(results, alerts)

    if problems:
        print("\nINVARIANT VIOLATIONS:", *problems, sep="\n  ", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
