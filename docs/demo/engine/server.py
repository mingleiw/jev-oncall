#!/usr/bin/env python3
"""HTTP server that receives alerts from monitoring systems, normalizes them,
triages with Jev, and returns decisions as JSON.

    export TYPESAFE_API_KEY=<key>
    python3 server.py                        # localhost:8090
    python3 server.py --port 9000 --host 0.0.0.0
    python3 server.py --config jev-oncall.toml   # your teams, thresholds, topology

Endpoints:

    POST /ingest/<provider>    Accept a webhook from a monitoring provider.
                               Providers: alertmanager, datadog, pagerduty,
                               grafana, generic.
                               Returns the triage decision for each alert.

    POST /ingest               Same as /ingest/generic.

    POST /ack/<alert-id>       Ack a REVIEW: someone is on it, so it closes
                               and won't escalate to a page. It doesn't
                               resolve the alert. Optional X-Acked-By header
                               names who acked.

    GET  /health               Returns {"ok": true}.

    GET  /recent               Last N triage decisions (in-memory ring buffer).

    GET  /pending              REVIEWs still waiting on an ack, and how
                               recent ones ended (acked, cancelled, escalated).

    GET  /dashboard            The last alerts received, as the HTML
                               dashboard generate_dashboard.py renders.

    POST /label/<alert-id>     Shadow mode: record what an alert really was.

    GET  /shadow               Shadow mode: how decisions compare with routing
                               by configured severity.

The generic provider accepts the jev-oncall alert schema directly, so any
system can integrate by posting normalized JSON.

Set JEV_WEBHOOK_SECRET to require a signature on every ingest. PagerDuty and
Grafana sign with their own headers; datadog and generic use X-Jev-Signature,
set as a custom header on the outgoing webhook. Alertmanager can't compute an
HMAC, so it sends the secret as a bearer token. Without the variable, ingest
is unauthenticated.

Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import shadow
import triage

PROVIDERS = {}

# Each provider signs the raw body with a shared secret. PagerDuty and Grafana
# have their own header and encoding; datadog and generic use ours, set as a
# custom header on the outgoing webhook.
SIGNATURE_HEADERS = {
    "pagerduty": "X-PagerDuty-Signature",
    "grafana": "X-Grafana-Alerting-Signature",
    "datadog": "X-Jev-Signature",
    "generic": "X-Jev-Signature",
    "alertmanager": "Authorization",
}

# Providers that re-send every alert in a group whenever the group changes.
# A re-sent alert was already judged: judging it again costs a call and, worse,
# re-adding a REVIEW would otherwise restart its ack clock on every resend.
RESENDS_GROUPS = {"alertmanager"}

VALID_SEVERITIES = {"critical", "warning", "info"}
MAX_FIELD_LEN = 1000
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

DEFAULT_RATE_LIMIT = 120
DASHBOARD_ALERTS = 500
SHADOW_OFF = "shadow mode is off: set [shadow] log in the config, or pass --shadow-log"
ALERT_TTL_S = 3600


def provider(name):
    def decorator(fn):
        PROVIDERS[name] = fn
        return fn
    return decorator


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(epoch_s):
    return datetime.fromtimestamp(epoch_s, timezone.utc).isoformat(timespec="seconds")


def _stable_id(provider_name, raw_id):
    """Deterministic alert id from provider + their id."""
    return hashlib.sha256(f"{provider_name}:{raw_id}".encode()).hexdigest()[:12]


def _clamp(value, max_len=MAX_FIELD_LEN):
    if not isinstance(value, str):
        return value
    return value[:max_len]


def verify_signature(provider, raw, headers, secret):
    """Is this body signed with the shared secret?

    Unsigned ingest is not only a way to inject a fake page: an alert crafted
    to be chosen as another alert's `duplicate_of` root can DEDUP a real page
    into silence. Fails closed on anything malformed.
    """
    if not secret:
        return True
    name = SIGNATURE_HEADERS.get(provider, "X-Jev-Signature")
    sent = headers.get(name)
    if not sent:
        return False
    want = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()

    if provider == "alertmanager":
        # Alertmanager has no HMAC signing; its http_config sends a static
        # credential. Over TLS that authenticates the sender, not the body.
        offered = [sent.strip()[7:]] if sent.strip().startswith("Bearer ") else []
        return any(hmac.compare_digest(secret.encode(), o.encode()) for o in offered if o)
    if provider == "pagerduty":
        # "v1=<hex>[,v1=<hex>]" during key rotation. Anything that is not a v1
        # element is dropped rather than trusted, so a caller cannot downgrade
        # the scheme by inventing a weaker one.
        offered = [p.strip()[3:] for p in sent.split(",") if p.strip().startswith("v1=")]
    elif provider == "grafana":
        offered = [sent.strip()]
    else:
        offered = [sent.strip()[7:] if sent.strip().startswith("sha256=") else sent.strip()]

    return any(hmac.compare_digest(want, o) for o in offered if o)


def validate_alert(alert):
    """Enforce schema constraints on a normalized alert before triage."""
    if not isinstance(alert.get("id"), str) or not alert["id"].strip():
        raise ValueError("alert 'id' must be a non-empty string")
    if not isinstance(alert.get("title"), str) or not alert["title"].strip():
        raise ValueError("alert 'title' must be a non-empty string")
    alert["id"] = _clamp(alert["id"], 64)
    alert["title"] = _clamp(alert["title"])
    alert["description"] = _clamp(alert.get("description", ""), 5000)
    if alert.get("service"):
        alert["service"] = _clamp(alert["service"], 200)
    sev = alert.get("configured_severity", "critical")
    if sev not in VALID_SEVERITIES:
        alert["configured_severity"] = "critical"
    ts = alert.get("started_at", "")
    if ts and not _ISO_RE.match(str(ts)):
        alert["started_at"] = _now_iso()
    return alert


# --------------------------------------------------------------------------
# Provider normalizers
#
# Each takes the parsed JSON body and returns a list of alerts in the
# jev-oncall schema: {id, title, description, service, env, started_at,
# configured_severity}.

@provider("generic")
def normalize_generic(body):
    """The jev-oncall schema itself. Accepts a single alert or a list."""
    alerts = body if isinstance(body, list) else [body]
    for a in alerts:
        if "id" not in a or "title" not in a:
            raise ValueError("generic alerts need at least 'id' and 'title'")
        a.setdefault("description", "")
        a.setdefault("env", "prod")
        a.setdefault("started_at", _now_iso())
    return alerts


@provider("datadog")
def normalize_datadog(body):
    """Datadog webhook payload. Datadog sends one alert per webhook."""
    alert_id = _stable_id("datadog", str(body.get("id", body.get("alert_id", ""))))
    title = body.get("title") or body.get("event_title") or "Datadog alert"
    desc_parts = []
    if body.get("body"):
        desc_parts.append(body["body"])
    if body.get("event_msg"):
        desc_parts.append(body["event_msg"])
    tags = body.get("tags", "")
    if isinstance(tags, list):
        tags = ",".join(tags)
    service = None
    env = "prod"
    for tag in tags.split(","):
        tag = tag.strip()
        if tag.startswith("service:"):
            service = tag.split(":", 1)[1]
        elif tag.startswith("env:"):
            env = tag.split(":", 1)[1]
    priority = body.get("priority", "").lower()
    sev_map = {"p1": "critical", "p2": "critical", "p3": "warning",
               "p4": "info", "p5": "info"}
    configured_severity = sev_map.get(priority, "critical")
    alert_type = body.get("alert_type", "").lower()
    if alert_type in ("info", "recommendation"):
        configured_severity = "info"
    elif alert_type == "warning":
        configured_severity = "warning"
    return [{
        "id": alert_id,
        "title": title,
        "description": "\n".join(desc_parts),
        "service": service,
        "env": env,
        "started_at": body.get("date") or _now_iso(),
        "configured_severity": configured_severity,
    }]


@provider("pagerduty")
def normalize_pagerduty(body):
    """PagerDuty v2 webhook (Events API v2 or webhook subscription)."""
    messages = body.get("messages") or body.get("events") or []
    if not messages and body.get("event"):
        messages = [body]
    alerts = []
    for msg in messages:
        event = msg.get("event", msg)
        data = event.get("data", event.get("incident", event))
        alert_id = _stable_id("pagerduty", str(
            data.get("id") or data.get("incident_number") or data.get("dedup_key", "")))
        title = data.get("title") or data.get("summary") or data.get("description") or "PagerDuty alert"
        desc = data.get("description") or data.get("summary") or ""
        if data.get("html_url"):
            desc += f"\n{data['html_url']}"
        service_obj = data.get("service", {})
        service = service_obj.get("name") or service_obj.get("summary") if isinstance(service_obj, dict) else None
        urgency = str(data.get("urgency", "")).lower()
        sev_map = {"high": "critical", "low": "warning"}
        severity_val = str(data.get("severity", "")).lower()
        sev_severity_map = {"critical": "critical", "error": "critical",
                            "warning": "warning", "info": "info"}
        configured_severity = sev_severity_map.get(severity_val, sev_map.get(urgency, "critical"))
        created = data.get("created_at") or data.get("created_on") or _now_iso()
        alerts.append({
            "id": alert_id,
            "title": title,
            "description": desc.strip(),
            "service": service,
            "env": "prod",
            "started_at": created,
            "configured_severity": configured_severity,
        })
    return alerts


@provider("grafana")
def normalize_grafana(body):
    """Grafana Alerting webhook (both legacy and Unified Alerting)."""
    raw_alerts = body.get("alerts", [body] if "title" in body else [])
    alerts = []
    for raw in raw_alerts:
        labels = raw.get("labels", {})
        annotations = raw.get("annotations", {})
        raw_id = (raw.get("fingerprint")
                  or raw.get("panelId")
                  or labels.get("alertname", "")
                  or raw.get("title", ""))
        alert_id = _stable_id("grafana", str(raw_id))
        title = (raw.get("title")
                 or labels.get("alertname")
                 or annotations.get("summary")
                 or body.get("title")
                 or "Grafana alert")
        desc_parts = []
        if annotations.get("description"):
            desc_parts.append(annotations["description"])
        if annotations.get("summary") and annotations["summary"] != title:
            desc_parts.append(annotations["summary"])
        if raw.get("message"):
            desc_parts.append(raw["message"])
        if body.get("message") and body["message"] not in desc_parts:
            desc_parts.append(body["message"])
        if raw.get("valueString"):
            desc_parts.append(f"Value: {raw['valueString']}")
        service = labels.get("service") or labels.get("job") or labels.get("namespace")
        env = labels.get("env") or labels.get("environment", "prod")
        raw_sev = (labels.get("severity") or labels.get("priority") or "").lower()
        sev_map = {"critical": "critical", "high": "critical", "warning": "warning",
                   "info": "info", "low": "info"}
        configured_severity = sev_map.get(raw_sev, "critical")
        started = raw.get("startsAt") or raw.get("starts_at") or _now_iso()
        alert = {
            "id": alert_id,
            "title": title,
            "description": "\n".join(desc_parts),
            "service": service,
            "env": env,
            "started_at": started,
            "configured_severity": configured_severity,
        }
        # Unified alerting marks each alert; legacy marks the whole body "ok".
        if raw.get("status") == "resolved" or body.get("state") == "ok":
            alert["resolved"] = True
        alerts.append(alert)
    return alerts


# Alertmanager has no standard severity; these are the common `severity` label values.
_AM_SEVERITIES = {"critical": "critical", "page": "critical", "error": "critical",
                  "high": "critical", "warning": "warning", "warn": "warning",
                  "info": "info", "low": "info", "none": "info"}


@provider("alertmanager")
def normalize_alertmanager(body):
    """Prometheus Alertmanager webhook (payload version 4).

    The id is the alert's fingerprint plus its startsAt, so every resend of
    one firing maps to one id, the matching "resolved" notification carries
    the same id, and a later re-fire of the same labels is a new alert.
    """
    raw_alerts = body.get("alerts")
    if not isinstance(raw_alerts, list):
        raise ValueError("alertmanager payload needs an 'alerts' list")
    alerts = []
    for raw in raw_alerts:
        labels = raw.get("labels") or {}
        annotations = raw.get("annotations") or {}
        started = raw.get("startsAt") or _now_iso()
        fingerprint = raw.get("fingerprint") or json.dumps(labels, sort_keys=True)
        alertname = labels.get("alertname") or "Alertmanager alert"
        summary = annotations.get("summary")
        title = f"{alertname}: {summary}" if summary else alertname
        desc_parts = []
        if annotations.get("description"):
            desc_parts.append(annotations["description"])
        extra = {k: v for k, v in sorted(labels.items())
                 if k not in ("alertname", "severity")}
        if extra:
            desc_parts.append("Labels: " + ", ".join(f"{k}={v}" for k, v in extra.items()))
        if raw.get("generatorURL"):
            desc_parts.append(raw["generatorURL"])
        raw_sev = str(labels.get("severity") or labels.get("priority") or "").lower()
        alert = {
            "id": _stable_id("alertmanager", f"{fingerprint}:{started}"),
            "title": title,
            "description": "\n".join(desc_parts),
            "service": (labels.get("service") or labels.get("job")
                        or labels.get("namespace")),
            "env": labels.get("env") or labels.get("environment") or "prod",
            "started_at": started,
            # An unknown severity pages: the fail-safe direction.
            "configured_severity": _AM_SEVERITIES.get(raw_sev, "critical"),
        }
        if raw.get("status") == "resolved":
            alert["resolved"] = True
        alerts.append(alert)
    return alerts


# --------------------------------------------------------------------------
# Triage runner

class RateLimiter:
    def __init__(self, max_per_minute=DEFAULT_RATE_LIMIT):
        self.max_per_minute = max_per_minute
        self._timestamps: deque = deque()
        self._lock = threading.Lock()

    def allow(self):
        now = time.monotonic()
        cutoff = now - 60
        with self._lock:
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.max_per_minute:
                return False
            self._timestamps.append(now)
            return True


def _percentile(values, q):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


class ReviewQueue:
    """REVIEW decisions that become pages if nobody acks them in time.

    The middle band only means anything if something escalates: an uncertain
    alert is supposed to cost a human's attention, never silence. Without this
    a REVIEW is just a TICKET with a different label.

    A review ends exactly one way: acked (someone is on it, so it won't page;
    the incident isn't resolved), cancelled (the alert cleared before anyone
    acked) or escalated (nobody acked in time, so it pages). Closed reviews
    are kept, so the same firing delivered again can't reopen one: until the
    alert resolves, or for an hour, a new REVIEW for it is a duplicate.

    With `store`, every open, ack, cancel and escalation is appended to a
    JSONL file and flushed to disk before it counts. A restarted server
    replays the file: pending reviews keep their original deadlines, closed
    ones stay closed, and an escalation already recorded is never repeated.
    One server process per store file, on local disk.

    Deadlines are wall-clock seconds (`clock`, default time.time) so they
    mean the same thing after a restart.
    """

    KEEP_CLOSED_S = 7 * 24 * 3600  # closed reviews kept across restarts
    STATES = ("pending", "acked", "cancelled", "escalated")

    def __init__(self, ack_minutes, store=None, clock=time.time):
        self.ack_seconds = ack_minutes * 60
        self.clock = clock
        self.store = store
        self._lock = threading.Lock()
        self._pending = {}  # id -> {"deadline", "opened", "entry"}
        self._closed = {}   # id -> {"state", "by", "at", "opened", "deadline", "entry"}
        self.recovered = {"pending": 0, "closed": 0, "unreadable": 0}
        if store:
            self._replay()

    def _window(self):
        if self.ack_seconds >= 60:
            return f"{self.ack_seconds / 60:g}m"
        return f"{self.ack_seconds:g}s"

    # -- the durable log ---------------------------------------------------

    def _write(self, events, mode="a", path=None):
        if not self.store:
            return
        with open(path or self.store, mode, encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _replay(self):
        if os.path.exists(self.store):
            with open(self.store, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        e = json.loads(line)
                        self._apply(e)
                    except (ValueError, KeyError, TypeError):
                        self.recovered["unreadable"] += 1  # a torn last line after a crash
        # Compact: pending reviews plus the last week of closed ones. Written
        # beside the store and renamed over it, so a crash leaves one or the other.
        cutoff = self.clock() - self.KEEP_CLOSED_S
        self._closed = {i: c for i, c in self._closed.items() if c["at"] >= cutoff}
        events = [{"event": "open", "id": i, "at": p["opened"], "deadline": p["deadline"],
                   "entry": p["entry"]} for i, p in self._pending.items()]
        for i, c in self._closed.items():
            events.append({"event": "open", "id": i, "at": c["opened"], "deadline": c["deadline"],
                           "entry": c["entry"]})
            events.append({"event": c["state"], "id": i, "at": c["at"], "by": c["by"]})
            if c["cleared"] and c["state"] != "cancelled":
                events.append({"event": "cleared", "id": i, "at": c["at"]})
        tmp = f"{self.store}.tmp"
        self._write(events, "w", tmp)
        os.replace(tmp, self.store)
        self.recovered.update(pending=len(self._pending), closed=len(self._closed))

    def _apply(self, e):
        aid, kind = e["id"], e["event"]
        if kind == "open":
            self._closed.pop(aid, None)  # a new firing of an alert whose review closed
            self._pending[aid] = {"deadline": e["deadline"], "opened": e["at"], "entry": e["entry"]}
        elif kind in ("acked", "cancelled", "escalated") and aid in self._pending:
            p = self._pending.pop(aid)
            self._closed[aid] = {"state": kind, "by": e.get("by"), "at": e["at"],
                                 "opened": p["opened"], "deadline": p["deadline"], "entry": p["entry"],
                                 "cleared": kind == "cancelled"}
        elif kind == "cleared" and aid in self._closed:
            self._closed[aid]["cleared"] = True

    # -- the review clock --------------------------------------------------

    @staticmethod
    def _same_firing(closed, entry, now):
        """Is a new REVIEW for an alert whose review closed the same firing,
        delivered again? Before the provider says the alert resolved: yes, for
        an hour after the review closed, like an active alert (ALERT_TTL_S),
        whatever its start time (some providers stamp each delivery anew).
        After it resolved: only a late retry with the same start time is."""
        if closed["cleared"]:
            return entry.get("started_at") == closed["entry"].get("started_at")
        return now - closed["at"] < ALERT_TTL_S

    def add(self, entry, now=None):
        """Open a review. Returns "opened", or "kept" when this firing already
        has one: a pending review keeps its first deadline, so a provider
        re-sending an alert can't keep pushing its page back, and a closed one
        stays closed, so a re-sent alert can't page twice or undo an ack."""
        now = self.clock() if now is None else now
        with self._lock:
            if entry["id"] in self._pending:
                return "kept"  # pending: whatever the provider says, the first deadline stands
            closed = self._closed.get(entry["id"])
            if closed and self._same_firing(closed, entry, now):
                return "kept"
            event = {"event": "open", "id": entry["id"], "at": now,
                     "deadline": now + self.ack_seconds, "entry": entry}
            self._write([event])
            self._apply(event)
            return "opened"

    def _close(self, alert_id, state, who, now=None):
        now = self.clock() if now is None else now
        with self._lock:
            if alert_id not in self._pending:
                return None
            event = {"event": state, "id": alert_id, "at": now, "by": who}
            self._write([event])  # on disk before it counts
            self._apply(event)
            return dict(self._closed[alert_id])

    def ack(self, alert_id, who=None, now=None):
        """Someone is on it: the review closes and won't escalate. The alert
        itself isn't resolved by this."""
        c = self._close(alert_id, "acked", who or "unknown", now)
        if c is None:
            return None
        return {"id": alert_id, "state": "acked", "acked_by": c["by"], "acked_at": _iso(c["at"])}

    def cancel(self, alert_id, why="resolved upstream", now=None):
        """The alert cleared before anyone acked: no page is needed."""
        c = self._close(alert_id, "cancelled", why, now)
        if c is None:
            return None
        return {"id": alert_id, "state": "cancelled", "cancelled_by": c["by"],
                "cancelled_at": _iso(c["at"])}

    def cleared(self, alert_id, now=None):
        """The provider says an alert resolved after its review closed (acked
        or escalated). Its next firing then gets a new review."""
        now = self.clock() if now is None else now
        with self._lock:
            closed = self._closed.get(alert_id)
            if closed and not closed["cleared"]:
                event = {"event": "cleared", "id": alert_id, "at": now}
                self._write([event])
                self._apply(event)

    def sweep(self, now=None):
        """Page everything past its deadline. Returns the escalated entries.
        Each escalation is on disk before it's returned, so a restart can
        never escalate the same review twice."""
        now = self.clock() if now is None else now
        with self._lock:
            due = sorted((it["deadline"], aid) for aid, it in self._pending.items()
                         if it["deadline"] <= now)
            events = [{"event": "escalated", "id": aid, "at": now, "by": None} for _, aid in due]
            self._write(events)
            for event in events:
                self._apply(event)
            items = [self._closed[aid] for _, aid in due]
        escalated = []
        for it in items:
            entry = dict(it["entry"])
            entry["action"] = "PAGE"
            entry["escalated_from"] = "REVIEW"
            entry["reasons"] = list(entry.get("reasons") or []) + [
                f"nobody acked within {self._window()}, so it pages"]
            entry["triaged_at"] = _iso(now)
            escalated.append(entry)
        return escalated

    def pending(self, now=None):
        now = self.clock() if now is None else now
        with self._lock:
            items = list(self._pending.values())
        return [{"id": it["entry"]["id"],
                 "title": it["entry"].get("title", ""),
                 "team": it["entry"].get("team"),
                 "opened_at": _iso(it["opened"]),
                 "deadline_at": _iso(it["deadline"]),
                 "seconds_left": round(it["deadline"] - now, 1)}
                for it in sorted(items, key=lambda i: i["deadline"])]

    def closed(self):
        """Reviews that ended, newest first: how, by whom, and when."""
        with self._lock:
            items = list(self._closed.items())
        return [{"id": aid, "title": c["entry"].get("title", ""), "team": c["entry"].get("team"),
                 "state": c["state"], "by": c["by"], "at": _iso(c["at"]),
                 "opened_at": _iso(c["opened"]), "deadline_at": _iso(c["deadline"])}
                for aid, c in sorted(items, key=lambda kv: -kv[1]["at"])]

    def states(self, now=None):
        """Every review this queue knows, by alert id: its current state."""
        out = {c["id"]: c for c in self.closed()}
        out.update({p["id"]: {**p, "state": "pending"} for p in self.pending(now)})
        return out


class TriageRunner:
    def __init__(self, topology=None, api_key=None, rate_limit=DEFAULT_RATE_LIMIT,
                 secret=None, config=None, clock=time.time):
        self.config = config or triage.Config()
        self.clock = clock  # wall clock; the browser demo passes its own
        self.topology = topology or {}
        self.api_key = api_key
        self.policy = self.config.policy
        self.lock = threading.Lock()
        self.recent = deque(maxlen=200)
        self._active_alerts = []
        self._alert_times: dict[str, float] = {}
        self.limiter = RateLimiter(rate_limit)
        self.secret = secret
        self.reviews = ReviewQueue(self.policy.review_ack_min, self.config.review_store, clock)
        self.escalated = 0
        # Full records, in triage.py's results.json shape, for /dashboard.
        self.records = deque(maxlen=DASHBOARD_ALERTS)
        self.calls = deque(maxlen=DASHBOARD_ALERTS)
        # Shadow mode: every decision and review outcome goes to an append-only
        # log beside what routing by configured severity would have done.
        self.shadow = (shadow.ShadowLog(self.config.shadow_log, clock)
                       if self.config.shadow_log else None)
        if self.shadow:
            self.shadow.start(self.config)

    def _prune_stale(self):
        cutoff = time.monotonic() - ALERT_TTL_S
        stale = [aid for aid, t in self._alert_times.items() if t < cutoff]
        for aid in stale:
            del self._alert_times[aid]
        self._active_alerts = [a for a in self._active_alerts
                               if a["id"] not in stale]

    def triage(self, alerts, skip_repeats=False):
        now = time.monotonic()
        repeats = []
        with self.lock:
            self._prune_stale()
            existing_ids = {a["id"] for a in self._active_alerts}
            if skip_repeats:
                repeats = [a["id"] for a in alerts if a["id"] in existing_ids]
                for aid in repeats:
                    self._alert_times[aid] = now  # still firing: still a dedup candidate
                alerts = [a for a in alerts if a["id"] not in existing_ids]
                if not alerts:
                    return {"decisions": [], "wall_ms": 0, "invariant_violations": [],
                            "repeats": repeats}
            for a in alerts:
                self._alert_times[a["id"]] = now
                if a["id"] not in existing_ids:
                    self._active_alerts.append(a)
            candidates = {a["id"]: triage.candidate_causes(a, self._active_alerts, self.topology, self.policy)
                          for a in alerts}
        t0 = time.monotonic()
        c = self.config
        judgments, errors, calls = triage.judge_all(
            alerts, candidates, self.api_key, c.model, c.timeout, c.retries, c.max_wait,
            teams=c.teams)
        wall_ms = (time.monotonic() - t0) * 1000
        with self.lock:
            # Dedup candidates span webhook deliveries, but each delivery is
            # routed as its own batch: hand the cluster step the earlier
            # batches' decisions so a duplicate_of pointing at a previous
            # alert resolves instead of KeyErroring.
            prior = {a["id"]: {"standalone": rec["standalone"],
                               "team": rec["team"], "action": rec["action"]}
                     for a, rec in self.records}
        decisions = triage.route_all(alerts, judgments, errors, self.policy, prior=prior)
        problems = triage.check_invariants(decisions)
        with self.lock:
            for a in alerts:
                aid, j = a["id"], judgments.get(a["id"])
                record = {
                    **asdict(decisions[aid]),
                    "candidates": [c["id"] for c in candidates[aid]],
                    "judgment": asdict(j) if j else None,
                    "error": errors.get(aid),
                    "call": calls.get(aid),
                }
                self.records.append((a, record))
                if self.shadow:
                    self.shadow.decision(a, record)
        results = []
        for a in alerts:
            d = decisions[a["id"]]
            j = judgments.get(a["id"])
            entry = {
                "id": a["id"],
                "title": a.get("title", ""),
                "action": d.action,
                "team": d.team,
                "source": d.source,
                "linked_to": d.linked_to,
                "notify": d.notify,
                "reasons": d.reasons,
                "p_page": round(j.p_page, 3) if j else None,
                "p_actionable": round(j.p_actionable, 3) if j else None,
                "severity": triage.top(j.severity) if j else None,
                "error": errors.get(a["id"]),
                "ms": calls[a["id"]]["ms"] if a["id"] in calls else None,
                "started_at": a.get("started_at"),
                "triaged_at": _iso(self.clock()),
            }
            results.append(entry)
            if d.action == "REVIEW":
                self.reviews.add(entry)
            with self.lock:
                self.recent.append(entry)
        return {
            "decisions": results,
            "wall_ms": round(wall_ms),
            "invariant_violations": problems,
            "repeats": repeats,
        }

    def resolve(self, alert_ids):
        """The provider says these alerts cleared. A REVIEW still waiting on
        one is cancelled: paging someone for an alert that already went away
        is the noise this exists to cut. Returns the cancelled reviews.
        The alert stays a dedup candidate until it ages out: something it
        caused can still arrive after it clears."""
        cancelled = [r for r in (self.reviews.cancel(aid, "resolved upstream")
                                 for aid in alert_ids) if r]
        for aid in alert_ids:
            self.reviews.cleared(aid)  # a closed review: the next firing is new
        if self.shadow:
            for r in cancelled:
                self.shadow.review(r["id"], "cancelled", "resolved upstream")
        return cancelled

    def results(self):
        """The last DASHBOARD_ALERTS alerts as a triage.py results dict, so
        the dashboard and evaluate.py read a live server like a batch run.
        Each alert keeps the decision it got on arrival; what happened to a
        REVIEW since (acked, cancelled, escalated) is in live_view()."""
        with self.lock:
            pairs = list(self.records)
        # Only the latest record per id: an alert re-sent later was re-judged.
        latest = {a["id"]: (a, rec) for a, rec in pairs}
        alerts = [a for a, _ in latest.values()]
        if self.shadow:
            # In shadow mode, labels come only from /label, as in shadow.to_results.
            labels = shadow.labels_by_id(shadow.load(self.shadow.path)[0])
            alerts = [{**{k: v for k, v in a.items() if k != "expected"},
                       **({"expected": labels[a["id"]]} if a["id"] in labels else {})}
                      for a in alerts]
        records = [rec for _, rec in latest.values()]
        decisions = {r["id"]: triage.Decision(**{k: r[k] for k in triage.Decision.__dataclass_fields__})
                     for r in records}
        judgments = {r["id"]: triage.Judgment(**r["judgment"]) for r in records if r["judgment"]}
        calls = {r["id"]: r["call"] for r in records if r["call"]}
        c = self.config
        return {
            "meta": {
                "version": 2,
                "model_requested": c.model,
                "models_answered": sorted({j.model for j in judgments.values()}),
                "policy": asdict(self.policy),
                "teams": c.teams,
                "topology": self.topology,
                "generated_at": _iso(self.clock()),
                "note": f"Live from server.py: the last {len(alerts)} alerts received. "
                        "Each shows the decision it got on arrival, and a review its "
                        "current state.",
            },
            "summary": triage.summarize(alerts, decisions, judgments, calls, 0,
                                        triage.check_invariants(decisions)),
            "alerts": records,
        }, alerts

    def sweep_reviews(self):
        """Escalate unacked reviews. Returns what was escalated. Nothing is
        sent anywhere: the escalation is recorded in /recent, /pending's
        history and the shadow log, for whatever reads them."""
        escalated = self.reviews.sweep()
        if escalated:
            with self.lock:
                for entry in escalated:
                    self.recent.append(entry)
                    self.escalated += 1
            if self.shadow:
                for entry in escalated:
                    self.shadow.review(entry["id"], "escalated")
        return escalated

    def ack(self, alert_id, who=None):
        record = self.reviews.ack(alert_id, who)
        if record and self.shadow:
            self.shadow.review(alert_id, "acked", who)
        return record

    def known_alerts(self):
        """Every alert id this server can label: received since it started,
        or logged in shadow mode before a restart."""
        with self.lock:
            ids = {a["id"] for a, _ in self.records}
        if self.shadow:
            ids |= set(shadow._latest(shadow.load(self.shadow.path)[0])[0])
        return ids

    def label(self, alert_id, body, who=None):
        """Shadow mode: record what an alert really was. A newer label for the
        same alert replaces the older one. Raises ValueError on a bad label
        or an unknown alert or cause; returns None when shadow mode is off."""
        if not self.shadow:
            return None
        label = shadow.validate_label(body, self.config.teams, self.known_alerts(), alert_id)
        self.shadow.label(alert_id, label, who)
        return {"id": alert_id, "label": label, "labeled_by": who or "unknown"}

    def live_view(self):
        """What the live dashboard adds on top of the run: the review queue,
        and the shadow comparison and label forms when shadow mode is on."""
        return {
            "now": _iso(self.clock()),
            "ack_min": self.policy.review_ack_min,
            "pending": self.reviews.pending(),
            "closed": self.reviews.closed(),
            "reviews": self.reviews.states(),
            "durable": bool(self.reviews.store),
            "shadow_on": bool(self.shadow),
            "shadow": self.shadow_summary() if self.shadow else None,
            "teams": list(self.config.teams),
            "require_token": self.config.require_token,
        }

    def action_allowed(self, headers):
        """/ack and /label change state. With [server] require_token they need
        the webhook secret as a bearer token; otherwise they're open, as before."""
        if not self.config.require_token:
            return True
        sent = (headers.get("Authorization") or "").strip()
        if not self.secret or not sent.startswith("Bearer "):
            return False
        return hmac.compare_digest(self.secret.encode(), sent[7:].strip().encode())

    def shadow_summary(self):
        events, bad = shadow.load(self.shadow.path)
        return shadow.summarize(events, bad)

    def stats(self):
        with self.lock:
            items = list(self.recent)
            escalated = self.escalated
        latencies = [d["ms"] for d in items if d.get("ms") is not None]
        return {
            "decisions": items,
            "count": len(items),
            "active_alerts": len(self._active_alerts),
            "pending_reviews": len(self.reviews.pending()),
            "escalated_reviews": escalated,
            "latency_ms_p50": _percentile(latencies, 0.50),
            "latency_ms_p95": _percentile(latencies, 0.95),
            "latency_ms_p99": _percentile(latencies, 0.99),
        }


# --------------------------------------------------------------------------
# HTTP handler

class Handler(BaseHTTPRequestHandler):
    runner: TriageRunner

    def _send_json(self, status, obj):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 10 * 1024 * 1024:
            self._send_json(413, {"error": "payload too large"})
            return None
        return self.rfile.read(length)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"ok": True})
        elif self.path.startswith("/recent"):
            self._send_json(200, self.runner.stats())
        elif self.path.rstrip("/") == "/pending":
            pending = self.runner.reviews.pending()
            self._send_json(200, {"pending": pending, "count": len(pending),
                                  "closed": self.runner.reviews.closed()[:100]})
        elif self.path.rstrip("/") == "/shadow":
            if not self.runner.shadow:
                self._send_json(404, {"error": SHADOW_OFF})
            else:
                self._send_json(200, self.runner.shadow_summary())
        elif self.path.rstrip("/") == "/dashboard":
            import generate_dashboard  # local import: only this endpoint needs it
            results, alerts = self.runner.results()
            page = generate_dashboard.render(
                results, alerts, "server.py", label="Live", live=self.runner.live_view(),
                footer="Rendered live by server.py from the alerts it has received. It refreshes "
                       "every 15 seconds, except while you're reading a Why panel or labeling.")
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.rstrip("/")
        if (path.startswith("/ack/") or path.startswith("/label/")) \
                and self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            # A browser on another site must not be able to ack a review, which
            # would stop it from paging. curl and the dashboard itself are fine.
            self._send_json(403, {"error": "cross-site requests can't ack or label"})
            return
        if (path.startswith("/ack/") or path.startswith("/label/")) \
                and not self.runner.action_allowed(self.headers):
            self._send_json(401, {"error": "this server requires a token: "
                                           "Authorization: Bearer <JEV_WEBHOOK_SECRET>"})
            return
        if path.startswith("/ack/"):
            alert_id = path.split("/ack/", 1)[1]
            who = self.headers.get("X-Acked-By")
            record = self.runner.ack(alert_id, who)
            closed = self.runner.reviews.states().get(alert_id) if record is None else None
            if closed:
                self._send_json(409, {"error": f"review for {alert_id} already {closed['state']}",
                                      "state": closed["state"], "by": closed["by"],
                                      "at": closed["at"]})
            elif record is None:
                self._send_json(404, {"error": f"no review pending for {alert_id}"})
            else:
                self._send_json(200, record)
            return
        if path.startswith("/label/"):
            alert_id = path.split("/label/", 1)[1]
            raw = self._read_body()
            if raw is None:
                return
            try:
                result = self.runner.label(alert_id, json.loads(raw or b"null"),
                                           self.headers.get("X-Labeled-By"))
            except shadow.UnknownAlert as e:
                self._send_json(404, {"error": str(e)})
                return
            except (json.JSONDecodeError, ValueError) as e:
                self._send_json(400, {"error": f"bad label: {e}"})
                return
            if result is None:
                self._send_json(404, {"error": SHADOW_OFF})
            else:
                self._send_json(201, result)
            return
        if path == "/ingest":
            provider_name = "generic"
        elif path.startswith("/ingest/"):
            provider_name = path.split("/ingest/", 1)[1]
        else:
            self._send_json(404, {"error": "not found"})
            return

        if provider_name not in PROVIDERS:
            self._send_json(400, {"error": f"unknown provider: {provider_name}",
                                  "available": sorted(PROVIDERS)})
            return

        raw = self._read_body()
        if raw is None:
            return
        if not verify_signature(provider_name, raw, self.headers, self.runner.secret):
            self._send_json(401, {"error": "bad or missing signature",
                                  "header": SIGNATURE_HEADERS.get(provider_name)})
            return
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            self._send_json(400, {"error": f"invalid JSON: {e}"})
            return

        if not self.runner.limiter.allow():
            self._send_json(429, {"error": "rate limit exceeded"})
            return

        try:
            alerts = PROVIDERS[provider_name](body)
        except (KeyError, ValueError, TypeError) as e:
            self._send_json(422, {"error": f"normalization failed: {e}"})
            return

        if not alerts:
            self._send_json(200, {"decisions": [], "wall_ms": 0, "invariant_violations": []})
            return

        cleared = [bool(a.pop("resolved", False)) for a in alerts]
        # Clamped like validate_alert clamps a firing id, so the two match.
        resolved = [_clamp(str(a.get("id", "")), 64) for a, c in zip(alerts, cleared) if c]
        firing = [a for a, c in zip(alerts, cleared) if not c]
        try:
            firing = [validate_alert(a) for a in firing]
        except (ValueError, TypeError) as e:
            self._send_json(422, {"error": f"validation failed: {e}"})
            return

        if firing:
            result = self.runner.triage(firing, provider_name in RESENDS_GROUPS)
        else:
            result = {"decisions": [], "wall_ms": 0, "invariant_violations": [],
                      "repeats": []}
        result["resolved"] = resolved
        result["reviews_cancelled"] = self.runner.resolve(resolved)
        self._send_json(200, result)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{_now_iso()}] {fmt % args}\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Webhook server for jev-oncall.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--config", default=os.environ.get("JEV_ONCALL_CONFIG"),
                    help="TOML file with teams, thresholds, and topology "
                         "(default: $JEV_ONCALL_CONFIG, else built-in defaults)")
    ap.add_argument("--topology",
                    help="service -> upstream services JSON; overrides the config's "
                         "[topology] (default: topology.json if the config has none)")
    ap.add_argument("--shadow-log",
                    help="turn on shadow mode, logging to this JSONL file; "
                         "overrides the config's [shadow] log")
    ap.add_argument("--rate-limit", type=int, default=DEFAULT_RATE_LIMIT,
                    help=f"max ingests per minute (default: {DEFAULT_RATE_LIMIT})")
    ap.add_argument("--review-store",
                    help="keep reviews in this JSONL file so they survive a restart; "
                         "overrides the config's [reviews] store")
    ap.add_argument("--sweep-interval", type=float, default=10.0,
                    help="seconds between checks for unacked reviews")
    args = ap.parse_args(argv)

    try:
        config = triage.load_config(args.config)
    except triage.ConfigError as e:
        sys.exit(f"config error: {e}")
    if args.shadow_log:
        config.shadow_log = args.shadow_log
    if args.review_store:
        config.review_store = args.review_store
    if config.require_token and not os.environ.get("JEV_WEBHOOK_SECRET"):
        sys.exit("config error: [server] require_token needs JEV_WEBHOOK_SECRET, "
                 "which is the token /ack and /label will require")

    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print("WARNING: TYPESAFE_API_KEY not set. All alerts will take the fail-open path.",
              file=sys.stderr)

    secret = os.environ.get("JEV_WEBHOOK_SECRET")
    if not secret:
        print("WARNING: JEV_WEBHOOK_SECRET not set. Ingest is unauthenticated: anyone who "
              "can reach this port can inject alerts, including one crafted to dedup a real "
              "page into silence.", file=sys.stderr)

    if not config.review_store:
        print("WARNING: no review store. Pending reviews live in memory: a restart drops "
              "them, and they never page. Set [reviews] store or --review-store.",
              file=sys.stderr)

    topology = triage.resolve_topology(args.topology, config)
    runner = TriageRunner(topology, api_key, args.rate_limit, secret, config)
    Handler.runner = runner
    if config.review_store:
        r = runner.reviews.recovered
        print(f"  reviews: {config.review_store}: {r['pending']} pending, {r['closed']} closed "
              f"restored" + (f", {r['unreadable']} unreadable lines skipped" if r["unreadable"] else ""),
              file=sys.stderr)

    stop = threading.Event()

    def sweeper():
        while not stop.wait(args.sweep_interval):
            for entry in runner.sweep_reviews():
                print(f"ESCALATED {entry['id']}: unacked review now pages {entry['team']}",
                      file=sys.stderr)

    # Reviews whose deadline passed while the server was down page now.
    for entry in runner.sweep_reviews():
        print(f"ESCALATED {entry['id']}: its review deadline passed while the server was down; "
              f"now pages {entry['team']}", file=sys.stderr)
    threading.Thread(target=sweeper, daemon=True).start()

    httpd = HTTPServer((args.host, args.port), Handler)
    print(f"jev-oncall server listening on {args.host}:{args.port}", file=sys.stderr)
    print(f"  config: {args.config or 'built-in defaults'} | model {config.model} | "
          f"teams: {', '.join(config.teams)}", file=sys.stderr)
    print(f"  POST /ingest/<provider>   providers: {', '.join(sorted(PROVIDERS))}", file=sys.stderr)
    print(f"  POST /ack/<alert-id>      ack a review: someone is on it, so it won't page",
          file=sys.stderr)
    print(f"  GET  /health", file=sys.stderr)
    print(f"  GET  /recent", file=sys.stderr)
    print(f"  GET  /pending             reviews waiting on an ack", file=sys.stderr)
    print(f"  GET  /dashboard           live HTML dashboard", file=sys.stderr)
    if runner.shadow:
        print(f"  shadow mode: logging to {runner.shadow.path}", file=sys.stderr)
        print(f"  POST /label/<alert-id>    record what an alert really was", file=sys.stderr)
        print(f"  GET  /shadow              compare with routing by configured severity",
              file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    stop.set()
    httpd.server_close()


if __name__ == "__main__":
    main()
