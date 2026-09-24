#!/usr/bin/env python3
"""Jev incident triage, v2: Jev judges, plain code decides.

One Jev call per production alert asks four typed questions:

    actionable    Noul    does a human need to intervene?
    severity      Score   SEV4 < SEV3 < SEV2 < SEV1; routing uses the distribution
    team          Choice  your teams from the config (default: database /
                          compute / network / deploy)
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
    python3 triage.py --config jev-oncall.toml   # your teams, thresholds, topology

Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import http.client
import json
import math
import os
import ssl
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
API_URL = "https://api.typesafe.ai/v1/systemone"
_API_PARSED = urlparse(API_URL)
_API_HOST = _API_PARSED.hostname
_API_PATH = _API_PARSED.path

_pool_lock = threading.Lock()
_connection_pool: list[http.client.HTTPSConnection] = []
_POOL_MAX = 16


def _proxy_for_host(host):
    """Return the proxy URL for host, or None for a direct connection.

    Honors https_proxy/HTTPS_PROXY (then all_proxy/ALL_PROXY) and no_proxy/
    NO_PROXY, so the client works behind a corporate egress proxy. Without
    this, http.client connects directly and TLS fails against the proxy.
    """
    proxy = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
             or os.environ.get("all_proxy") or os.environ.get("ALL_PROXY"))
    if not proxy:
        return None
    no_proxy = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    host = host.lower()
    for pat in (p.strip().lower() for p in no_proxy.split(",")):
        if not pat:
            continue
        if pat == "*" or host == pat or (pat.startswith(".") and host.endswith(pat)):
            return None
    return proxy


def _get_conn(timeout_s):
    with _pool_lock:
        if _connection_pool:
            conn = _connection_pool.pop()
            conn.timeout = timeout_s
            return conn
    ctx = ssl.create_default_context()
    proxy = _proxy_for_host(_API_HOST)
    if proxy:
        px = urlparse(proxy)
        conn = http.client.HTTPSConnection(
            px.hostname, px.port or 8080, timeout=timeout_s, context=ctx)
        headers = {}
        if px.username:
            auth = base64.b64encode(
                f"{px.username}:{px.password or ''}".encode()).decode()
            headers["Proxy-Authorization"] = f"Basic {auth}"
        conn.set_tunnel(_API_HOST, 443, headers)
        return conn
    return http.client.HTTPSConnection(_API_HOST, timeout=timeout_s, context=ctx)


def _put_conn(conn):
    with _pool_lock:
        if len(_connection_pool) < _POOL_MAX:
            _connection_pool.append(conn)
            return
    try:
        conn.close()
    except Exception:
        pass

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
    fallback_team: str = FALLBACK_TEAM  # owner when Jev didn't name one and the alert has no owner


@dataclass
class Config:
    """Everything a deployment tunes, loaded from a TOML file by load_config().
    Secrets stay in environment variables, never here."""
    model: str = MODEL
    timeout: float = 2.0        # seconds per attempt; keep it short on a paging path
    retries: int = 1
    max_wait: float = 1.0       # longest backoff before giving up and falling back
    teams: dict = field(default_factory=lambda: dict(TEAM_CRITERIA))  # name -> what it owns
    topology: dict | None = None  # service -> upstream services; None: not configured
    policy: Policy = field(default_factory=Policy)
    shadow_log: str | None = None  # JSONL path; set, it turns on shadow mode (shadow.py)
    require_token: bool = False   # /ack and /label need Authorization: Bearer <JEV_WEBHOOK_SECRET>


class ConfigError(Exception):
    """A config file that would route alerts differently than its author meant."""


_JEV_KEYS = ("model", "timeout", "retries", "max_wait")
_POLICY_TYPES = {name: f.type for name, f in Policy.__dataclass_fields__.items()}


def _check_keys(table, allowed, where):
    unknown = sorted(set(table) - set(allowed))
    if unknown:
        # A typo'd threshold would silently keep its default on a paging path.
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}; "
                          f"expected one of {', '.join(allowed)}")


def load_config(path=None):
    """Parse a TOML config into a Config. No path means built-in defaults.
    Unknown keys and out-of-range values are errors, not warnings."""
    config = Config()
    if not path:
        return config
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e
    _check_keys(raw, ["jev", "policy", "teams", "topology", "shadow", "server"], path)

    jev = raw.get("jev", {})
    _check_keys(jev, _JEV_KEYS, f"{path} [jev]")
    for key in _JEV_KEYS:
        if key in jev:
            setattr(config, key, jev[key])
    if not isinstance(config.model, str) or not config.model:
        raise ConfigError(f"{path} [jev]: model must be a non-empty string")
    for key in ("timeout", "retries", "max_wait"):
        value = getattr(config, key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"{path} [jev]: {key} must be a non-negative number")

    policy = raw.get("policy", {})
    _check_keys(policy, list(_POLICY_TYPES), f"{path} [policy]")
    for key, value in policy.items():
        want = _POLICY_TYPES[key]
        types = {"str": str, "int": int, "float": (int, float)}[want]
        ok = isinstance(value, types) and not isinstance(value, bool) and value != ""
        if not ok:
            raise ConfigError(f"{path} [policy]: {key} must be a {want}, got {value!r}")
    config.policy = p = Policy(**policy)
    for key in ("page_bar", "no_page_bar", "drop_bar", "dedup_bar", "team_bar"):
        if not 0.0 <= getattr(p, key) <= 1.0:
            raise ConfigError(f"{path} [policy]: {key} must be between 0 and 1")
    if p.no_page_bar >= p.page_bar:
        raise ConfigError(f"{path} [policy]: no_page_bar ({p.no_page_bar}) must be below "
                          f"page_bar ({p.page_bar}), or nothing lands in REVIEW")
    if not 1 <= p.max_candidates <= 254:
        raise ConfigError(f"{path} [policy]: max_candidates must be 1-254 "
                          "(Choice allows 255 options, one is 'none')")

    if "teams" in raw:
        teams = raw["teams"]
        # The runner-up rule needs a second team; Choice allows at most 255.
        if not 2 <= len(teams) <= 255:
            raise ConfigError(f"{path} [teams]: need 2 to 255 teams, got {len(teams)}")
        for name, desc in teams.items():
            if not isinstance(desc, str) or not desc.strip():
                raise ConfigError(f"{path} [teams]: {name} needs a description of what it "
                                  "owns; Jev routes on it")
        config.teams = dict(teams)

    if "topology" in raw:
        topology = raw["topology"]
        for service, deps in topology.items():
            if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
                raise ConfigError(f"{path} [topology]: {service} must list upstream "
                                  "services as strings")
        config.topology = dict(topology)

    shadow_cfg = raw.get("shadow", {})
    _check_keys(shadow_cfg, ["log"], f"{path} [shadow]")
    if "log" in shadow_cfg:
        if not isinstance(shadow_cfg["log"], str) or not shadow_cfg["log"].strip():
            raise ConfigError(f"{path} [shadow]: log must be a file path")
        config.shadow_log = shadow_cfg["log"]

    server_cfg = raw.get("server", {})
    _check_keys(server_cfg, ["require_token"], f"{path} [server]")
    if "require_token" in server_cfg:
        if not isinstance(server_cfg["require_token"], bool):
            raise ConfigError(f"{path} [server]: require_token must be true or false")
        config.require_token = server_cfg["require_token"]
    return config


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


def build_payload(alert, candidates, model=MODEL, teams=None):
    questions = {
        "actionable": {"type": "noul", "instructions": ACTIONABLE_Q},
        "severity": {"type": "score", "instructions": SEVERITY_Q, "criteria": SEV_CRITERIA},
        "team": {"type": "choice", "instructions": TEAM_Q,
                 "criteria": teams or TEAM_CRITERIA},
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


def _retry_after(headers):
    try:
        return float(headers.get("Retry-After"))
    except (TypeError, ValueError, AttributeError):
        return None


def call_jev(payload, api_key, timeout_s=2.0, retries=1, max_wait_s=1.0):
    """POST one request using a pooled HTTPS connection. Retries 429s, 5xx
    and network errors with escalating backoff (rate-limit 429s use
    Retry-After when available). Raises JevError rather than wait longer
    than max_wait_s between attempts: on a paging path, falling back beats
    waiting."""
    body = json.dumps(payload).encode("utf-8")
    # One key for the whole call, reused by every retry. A timeout does not
    # mean the server didn't answer: without this, retrying a request that
    # actually succeeded judges the alert twice and is billed twice.
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Connection": "keep-alive",
        "Idempotency-Key": uuid.uuid4().hex,
    }
    last = "no attempt made"
    for attempt in range(retries + 1):
        base_wait = 0.5 * 2 ** attempt
        t0 = time.monotonic()
        conn = _get_conn(timeout_s)
        try:
            conn.request("POST", _API_PATH, body=body, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            if 200 <= resp.status < 300:
                data = json.loads(resp_body.decode("utf-8"))
                _put_conn(conn)
                return data, (time.monotonic() - t0) * 1000
            last = f"HTTP {resp.status}"
            if resp.status == 429:
                wait = _retry_after(resp) or base_wait
            elif resp.status >= 500:
                wait = base_wait
            else:
                try:
                    conn.close()
                except Exception:
                    pass
                break
            _put_conn(conn)
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            wait = base_wait
            try:
                conn.close()
            except Exception:
                pass
        if attempt < retries:
            if wait > max_wait_s:
                last += f" (retry would wait {wait:g}s)"
                break
            time.sleep(wait)
    raise JevError(last)


def _validate_finite(value, what):
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise JevError(f"{what}: non-finite value {value!r}")
    return float(value)


def _distribution(raw, options, what):
    if not isinstance(raw, dict):
        raise JevError(f"{what}: no probabilities")
    dist = dict.fromkeys(options, 0.0)
    for key, value in raw.items():
        if key not in dist:
            raise JevError(f"{what}: unexpected option {key!r}")
        p = _validate_finite(value, what)
        if not 0.0 <= p <= 1.0:
            raise JevError(f"{what}: probability {p} out of range")
        dist[key] = p
    total = sum(dist.values())
    if abs(total - 1.0) > 0.05:
        raise JevError(f"{what}: probabilities sum to {total:.3f}")
    return {k: v / total for k, v in dist.items()}


def _validate_choice_max(answer, dist, what):
    """If the response includes a 'choice' field, verify it matches the max probability."""
    choice = answer.get("choice")
    if choice is not None and choice in dist:
        max_p = max(dist.values())
        if dist[choice] < max_p - 1e-6:
            raise JevError(f"{what}: choice {choice!r} is not the most probable option")


def parse_answers(resp, candidate_ids, teams=None):
    """Validate a response and normalize it into a Judgment, or raise JevError.
    Jev guarantees the schema; checking anyway is cheap on a paging path."""
    try:
        answers = resp["answers"]
        p_act = _validate_finite(answers["actionable"]["noul"], "actionable")
        if not 0.0 <= p_act <= 1.0:
            raise JevError(f"actionable: probability {p_act} out of range")
        severity = {}
        for key, value in answers["severity"]["probabilities"].items():
            level = int(key)
            if not 0 <= level < len(SEV_LEVELS):
                raise JevError(f"severity: unexpected level {key!r}")
            severity[SEV_LEVELS[level]] = _validate_finite(value, "severity")
        duplicate_of = None
        if candidate_ids:
            dup_raw = answers["duplicate_of"]["probabilities"]
            duplicate_of = _distribution(dup_raw, [*candidate_ids, NONE], "duplicate_of")
            _validate_choice_max(answers["duplicate_of"], duplicate_of, "duplicate_of")
        sev_dist = _distribution(severity, SEV_LEVELS, "severity")
        team_raw = answers["team"]["probabilities"]
        team_dist = _distribution(team_raw, list(teams or TEAM_CRITERIA), "team")
        _validate_choice_max(answers["team"], team_dist, "team")
        return Judgment(
            model=str(resp.get("model", "unknown")),
            p_actionable=p_act,
            severity=sev_dist,
            team=team_dist,
            duplicate_of=duplicate_of,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise JevError(f"malformed response ({type(e).__name__}: {e})") from e


def judge_all(alerts, candidates, api_key, model=MODEL, timeout_s=2.0, retries=1,
              max_wait_s=1.0, workers=8, teams=None):
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
            payload = build_payload(alert, cands, model, teams)
            resp, ms = call_jev(payload, api_key, timeout_s, retries, max_wait_s)
            judgment = parse_answers(resp, [c["id"] for c in cands], teams)
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

def static_route(alert, error, fallback_team=FALLBACK_TEAM):
    """Fail-open: route exactly as the alert would be routed without Jev."""
    sev = str(alert.get("configured_severity", "")).lower()
    action = STATIC_ROUTES.get(sev, "PAGE")
    basis = f"configured severity {sev!r}" if sev in STATIC_ROUTES else "no configured severity, so page"
    return Decision(alert["id"], action, action, alert.get("owner") or fallback_team,
                    "fallback", reasons=[f"Jev unavailable ({error}); routed by {basis}"])


def route_standalone(alert, judgment, policy, error=None):
    """What this alert would get on its own, before dedup."""
    aid = alert["id"]
    if not is_prod(alert):
        return Decision(aid, "LOG", "LOG", alert.get("owner") or policy.fallback_team, "rule",
                        reasons=[f"env={alert.get('env')}: non-production never pages "
                                 "(rule, no model call)"])
    if judgment is None:
        return static_route(alert, error or "no judgment", policy.fallback_team)

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


def resolve_clusters(alerts, decisions, judgments, policy, prior=None):
    """Turn duplicate_of answers into incident clusters, in place.

    prior maps alert ids decided in earlier batches (the streaming server
    triages one webhook delivery at a time) to {"standalone", "team",
    "action"}. The batch CLI path passes nothing.
    """
    prior = prior or {}
    started = {a["id"]: parse_time(a.get("started_at")) for a in alerts}

    def cause_standalone(cid):
        if cid in decisions:
            return decisions[cid].standalone
        p = prior.get(cid)
        return p.get("standalone") if p else None

    # 1. At most one edge per alert: its most likely cause, if likely enough.
    parent, confidence = {}, {}
    for aid in sorted(judgments):
        causes = {k: v for k, v in (judgments[aid].duplicate_of or {}).items() if k != NONE}
        if not causes:
            continue
        cause = top(causes)
        if causes[cause] < policy.dedup_bar:
            continue
        standalone = cause_standalone(cause)
        if standalone is None:
            decisions[aid].reasons.append(
                f"duplicate_of {cause} ({causes[cause]:.2f}) ignored: "
                f"cause has no recorded decision")
            continue
        if standalone in ("DROP", "LOG"):
            decisions[aid].reasons.append(
                f"duplicate_of {cause} ({causes[cause]:.2f}) ignored: "
                f"{cause} is {standalone}")
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

    def cluster_map():
        clusters = {}
        for aid in sorted(decisions):
            clusters.setdefault(root_of(aid), []).append(aid)
        return clusters

    # A root decided in an earlier batch can't be escalated now: that decision
    # already went out. If it got less than its new members need, or was itself
    # deduped, it can't carry them, so cut their links to it and let them form
    # their own cluster here. Otherwise a page could DEDUP onto a TICKET.
    clusters = cluster_map()
    for root, members in clusters.items():
        if root in decisions:
            continue
        got = prior[root]["action"]
        need = max(RANK[decisions[m].standalone] for m in members)
        if got in RANK and RANK[got] >= need:
            continue
        for m in members:
            if parent.get(m) == root:
                del parent[m]
                decisions[m].reasons.append(
                    f"duplicate_of {root} ({confidence[m]:.2f}) not applied: {root} was "
                    f"decided earlier as {got}, less than this incident needs")
    clusters = cluster_map()

    # 3. The root carries the cluster's most urgent action. Members owned by
    # the root's team are deduped; other teams get a REVIEW, never silence.
    # A root decided in an earlier batch keeps the action it already got; the
    # cut above guarantees that action already covers every member left.
    for root, members in clusters.items():
        if len(members) == 1 and root in decisions:
            continue  # true singleton; a prior-batch root still claims its members
        rd = decisions.get(root)
        rd_team = rd.team if rd is not None else prior[root]["team"]
        rd_action = rd.action if rd is not None else prior[root]["action"]
        most_urgent = max(members, key=lambda m: RANK[decisions[m].standalone])
        if rd is not None and RANK[decisions[most_urgent].standalone] > RANK[rd_action]:
            rd.action = decisions[most_urgent].standalone
            rd.reasons.append(f"escalated to {rd.action}: cluster member {most_urgent} needs it")
            rd_action = rd.action
        for m in members:
            if m == root:
                continue
            md = decisions[m]
            md.linked_to = root
            md.reasons.append(f"duplicate_of {parent[m]} ({confidence[m]:.2f}); incident root {root}")
            if md.team != rd_team and RANK[md.standalone] >= RANK["REVIEW"]:
                md.action = "REVIEW"
                md.reasons.append(f"root is owned by {rd_team}, so {md.team} gets a REVIEW, "
                                  "not silence")
            else:
                md.action = "DEDUP"
    return decisions


def route_all(alerts, judgments, errors, policy, prior=None):
    """The whole policy. Pure: same inputs, same decisions. evaluate.py
    re-runs it over stored answers to sweep thresholds offline. prior carries
    earlier batches' decisions for the streaming server; the batch path omits it."""
    decisions = {a["id"]: route_standalone(a, judgments.get(a["id"]), policy, errors.get(a["id"]))
                 for a in alerts}
    return resolve_clusters(alerts, decisions, judgments, policy, prior)


def check_invariants(decisions):
    """Safety properties that must hold whatever Jev says."""
    problems = []
    for d in decisions.values():
        if d.action == "DEDUP" and not d.linked_to:
            problems.append(f"{d.id}: DEDUP without an incident root")
        if d.linked_to and d.linked_to in decisions:
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
        "latency_ms_p99": _percentile(ms, 0.99),
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


def resolve_topology(topology_path, config):
    """An explicit --topology file wins, then the config's [topology], then
    topology.json next to this script, then none."""
    if topology_path:
        return load_json(topology_path)
    if config.topology is not None:
        return config.topology
    default = os.path.join(BASE, "topology.json")
    return load_json(default) if os.path.exists(default) else {}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Triage alerts with Jev: Jev judges, code decides.")
    ap.add_argument("--alerts", default=os.path.join(BASE, "alerts.json"),
                    help="JSON list or JSONL of alerts (default: alerts.json)")
    ap.add_argument("--config", default=os.environ.get("JEV_ONCALL_CONFIG"),
                    help="TOML file with teams, thresholds, and topology "
                         "(default: $JEV_ONCALL_CONFIG, else built-in defaults)")
    ap.add_argument("--topology",
                    help="service -> upstream services JSON; overrides the config's "
                         "[topology] (default: topology.json if the config has none)")
    ap.add_argument("--out", default=os.path.join(BASE, "results.json"))
    ap.add_argument("--model", help="overrides [jev] model")
    ap.add_argument("--timeout", type=float,
                    help="seconds per attempt; keep it short on a paging path")
    ap.add_argument("--retries", type=int)
    ap.add_argument("--max-wait", type=float,
                    help="longest backoff before giving up and falling back")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print the reasons behind every decision")
    args = ap.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as e:
        sys.exit(f"config error: {e}")
    for key in _JEV_KEYS:  # flags beat the file
        if getattr(args, key) is not None:
            setattr(config, key, getattr(args, key))
    args.model, args.timeout, args.retries, args.max_wait = (
        config.model, config.timeout, config.retries, config.max_wait)

    alerts = load_alerts(args.alerts)
    topology = resolve_topology(args.topology, config)
    policy = config.policy
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print("WARNING: TYPESAFE_API_KEY is not set, so every alert is routed by its "
              "configured severity (the fail-open path).\n", file=sys.stderr)

    candidates = {a["id"]: candidate_causes(a, alerts, topology, policy) for a in alerts}
    t0 = time.monotonic()
    judgments, errors, calls = judge_all(alerts, candidates, api_key, args.model,
                                         args.timeout, args.retries, args.max_wait, args.workers,
                                         config.teams)
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
            "teams": config.teams,
            "config_path": os.path.abspath(args.config) if args.config else None,
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
              f"p50 {s['latency_ms_p50']}ms, p95 {s['latency_ms_p95']}ms, "
              f"p99 {s['latency_ms_p99']}ms | "
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
