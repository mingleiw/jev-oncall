#!/usr/bin/env python3
"""HTTP server that receives alerts from monitoring systems, normalizes them,
triages with Jev, and returns decisions as JSON.

    export TYPESAFE_API_KEY=<key>
    python3 server.py                        # localhost:8090
    python3 server.py --port 9000 --host 0.0.0.0

Endpoints:

    POST /ingest/<provider>    Accept a webhook from a monitoring provider.
                               Providers: datadog, pagerduty, grafana, generic.
                               Returns the triage decision for each alert.

    POST /ingest               Same as /ingest/generic.

    GET  /health               Returns {"ok": true}.

    GET  /recent               Last N triage decisions (in-memory ring buffer).

The generic provider accepts the jev-oncall alert schema directly, so any
system can integrate by posting normalized JSON.

Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import triage

PROVIDERS = {}


def provider(name):
    def decorator(fn):
        PROVIDERS[name] = fn
        return fn
    return decorator


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stable_id(provider_name, raw_id):
    """Deterministic alert id from provider + their id."""
    return hashlib.sha256(f"{provider_name}:{raw_id}".encode()).hexdigest()[:12]


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
        alerts.append({
            "id": alert_id,
            "title": title,
            "description": "\n".join(desc_parts),
            "service": service,
            "env": env,
            "started_at": started,
            "configured_severity": configured_severity,
        })
    return alerts


# --------------------------------------------------------------------------
# Triage runner

class TriageRunner:
    def __init__(self, topology=None, api_key=None):
        self.topology = topology or {}
        self.api_key = api_key
        self.policy = triage.Policy()
        self.lock = threading.Lock()
        self.recent = deque(maxlen=200)
        self._active_alerts = []

    def triage(self, alerts):
        with self.lock:
            self._active_alerts = list(alerts)
            candidates = {a["id"]: triage.candidate_causes(a, self._active_alerts, self.topology, self.policy)
                          for a in self._active_alerts}
        t0 = time.monotonic()
        judgments, errors, calls = triage.judge_all(
            alerts, candidates, self.api_key, timeout_s=2.0, retries=1, max_wait_s=1.0)
        wall_ms = (time.monotonic() - t0) * 1000
        decisions = triage.route_all(alerts, judgments, errors, self.policy)
        problems = triage.check_invariants(decisions)
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
                "triaged_at": _now_iso(),
            }
            results.append(entry)
            with self.lock:
                self.recent.append(entry)
        return {
            "decisions": results,
            "wall_ms": round(wall_ms),
            "invariant_violations": problems,
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
            with self.runner.lock:
                items = list(self.runner.recent)
            self._send_json(200, {"decisions": items, "count": len(items)})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.rstrip("/")
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
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            self._send_json(400, {"error": f"invalid JSON: {e}"})
            return

        try:
            alerts = PROVIDERS[provider_name](body)
        except (KeyError, ValueError, TypeError) as e:
            self._send_json(422, {"error": f"normalization failed: {e}"})
            return

        if not alerts:
            self._send_json(200, {"decisions": [], "wall_ms": 0, "invariant_violations": []})
            return

        result = self.runner.triage(alerts)
        self._send_json(200, result)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{_now_iso()}] {fmt % args}\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Webhook server for jev-oncall.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--topology", default=os.path.join(triage.BASE, "topology.json"))
    args = ap.parse_args(argv)

    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print("WARNING: TYPESAFE_API_KEY not set. All alerts will take the fail-open path.",
              file=sys.stderr)

    topology = triage.load_json(args.topology) if os.path.exists(args.topology) else {}
    runner = TriageRunner(topology, api_key)
    Handler.runner = runner

    httpd = HTTPServer((args.host, args.port), Handler)
    print(f"jev-oncall server listening on {args.host}:{args.port}", file=sys.stderr)
    print(f"  POST /ingest/<provider>   providers: {', '.join(sorted(PROVIDERS))}", file=sys.stderr)
    print(f"  GET  /health", file=sys.stderr)
    print(f"  GET  /recent", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()


if __name__ == "__main__":
    main()
