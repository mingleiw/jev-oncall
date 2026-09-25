#!/usr/bin/env python3
"""Build the interactive dashboard demo that GitHub Pages serves.

    python3 build_demo.py        # writes docs/demo/index.html and docs/demo/engine/

It runs the real server pipeline (candidate causes, routing, the dedup
graph, the review queue, shadow mode) on the Docker demo's staged incident.
Jev's answers are replayed from the real jev-1.13.0 run on 2026-09-23 (no
model is called when the page loads), and the page says so.

The page is the starting state, prerendered. When a visitor acts (Ack, a
label, advancing the clock, a simulated Jev timeout, an alert clearing), the
page loads this same Python in the browser with Pyodide, replays what the
visitor did on a DemoSession, and re-renders. So the demo runs the production
review queue, routing and evaluation code, on a demo clock that only moves
when asked. Nothing is sent anywhere and no model is called.

Standard library only.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from unittest import mock

import generate_dashboard
import server
import shadow
import triage

BASE = os.path.dirname(os.path.abspath(__file__))
START = "2026-09-24T14:03"  # the incident's first minute; alerts fire over it
# The demo clock when the page opens: just after the last alert arrived.
OPENED = datetime(2026, 9, 24, 14, 4, 0, tzinfo=timezone.utc).timestamp()
PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/"
# What the browser loads to run the demo: the production modules, unchanged.
ENGINE_FILES = ["triage.py", "server.py", "shadow.py", "evaluate.py", "generate_dashboard.py",
                "build_demo.py", "demo/jev-oncall.toml"]
NAV = [("Home", "../"), ("Architecture", "../architecture.html"),
       ("GitHub", "https://github.com/mingleiw/jev-oncall")]
ADVANCE_MIN = 15

# "Simulate a Jev timeout" sends this alert while every Jev call times out.
TIMEOUT_ALERT = ("queue-lag", "OrdersQueueLag: orders-events consumer lag 48k messages",
                 "The order-events consumer group stopped committing offsets 4 minutes ago.",
                 "orders-queue", "prod", "critical")
TIMEOUT_ERROR = "TimeoutError: Jev didn't answer within 2s (simulated), and the retry would wait too long"

# The Docker demo's incident (demo/rules.yml), in the generic webhook schema,
# plus one noisy alert so the demo shows a DROP. Fires at START + seconds.
ALERTS = [
    ("orders-db", 10, "OrdersDbPoolExhausted: connection pool 100% used (500/500) on the orders-db primary",
     "Active connections hit the pool limit and queries are queueing; p99 query wait is 8.2s. "
     "Started 2 minutes after a migration added an unindexed lookup on orders.customer_id.",
     "orders-db", "prod", "critical"),
    ("staging-disk", 15, "StagingDiskFull: disk 97% full on staging CI runner", "",
     "ci-runner", "staging", "critical"),
    ("search-mem", 15, "SearchApiHighMemory: memory 88% of limit on search-api",
     "Heap usage rising after a cache warm-up; GC is keeping up.", "search-api", "prod", "warning"),
    ("checkout", 20, "CheckoutApiErrorRate: 5xx rate 31% for 5m",
     "checkout-api 5xx climbed from 0.1% to 31%. Most errors are timeouts acquiring an orders-db connection.",
     "checkout-api", "prod", "critical"),
    ("payment", 25, "PaymentServiceErrors: payment authorization failures 18%",
     "Authorization calls failing with upstream 503 from checkout-api.", "payment-service", "prod", "critical"),
    ("tls-cert", 30, "TlsCertExpiringSoon: TLS certificate for api.example.com expires in 25 days",
     "Auto-renewal has failed twice; the ACME challenge returns 404.", "edge", "prod", "warning"),
    ("report-job", 35, "ReportJobSlow: nightly revenue report finished 12 minutes later than usual",
     "The job completed successfully and the report was delivered. Runtime 41m against a 29m median. "
     "No customer-facing impact.", "report-batch", "prod", "critical"),
    ("homepage", 40, "HomepageLatencyHigh: p99 latency 2.4s against a 400ms SLO",
     "30% of homepage requests are over the SLO for the last 10 minutes and the checkout conversion "
     "rate dropped 22%.", "homepage", "prod", "warning"),
]

# Jev's answers, replayed from the real jev-1.13.0 run on 2026-09-23 (8 alerts,
# replayed as separate webhook deliveries; decisions captured in the session
# notes as demo-decisions.json). Format: (P(actionable), severity, team,
# duplicate_of). The run recorded top answers rather than full distributions,
# so severity/team carry the model's top label at its recorded confidence;
# severity's SEV1/SEV2 remainder sits on SEV2, which reproduces the recorded
# P(page) (= SEV1 + SEV2) and the top label exactly.
ANSWERS = {
    "orders-db": (0.96, {"SEV2": 1.0}, {"database": 1.00}, None),
    "search-mem": (0.38, {"SEV3": 0.93, "SEV2": 0.07}, {"compute": 1.00}, None),
    "checkout": (0.97, {"SEV1": 1.0}, {"database": 0.99}, None),
    "payment": (0.94, {"SEV2": 1.0}, {"compute": 0.99}, {"checkout": 0.87}),
    "tls-cert": (0.96, {"SEV3": 0.53, "SEV2": 0.47}, {"network": 1.00}, None),
    "report-job": (0.22, {"SEV4": 1.0}, {"compute": 1.00}, None),
    "homepage": (0.96, {"SEV2": 1.0}, {"compute": 0.97}, None),
}

# Labels someone added after the incident review, so the page shows scoring.
LABELS = {
    "orders-db": {"severity": "SEV1", "actionable": True, "team": "database", "duplicate_of": None},
    "report-job": {"severity": "SEV4", "actionable": False, "team": "compute", "duplicate_of": None},
}

MODEL = "jev-1.13.0"


def alerts():
    out = []
    for aid, secs, title, desc, service, env, sev in ALERTS:
        out.append({"id": aid, "title": title, "description": desc, "service": service, "env": env,
                    "started_at": f"{START}:{secs:02d}Z", "configured_severity": sev})
    return out


def replay_judge(batch, candidates, *args, **kwargs):
    """Stands in for triage.judge_all: the real run's judgments, replayed."""
    judgments = {}
    for a in batch:
        if not triage.is_prod(a):
            continue  # like the real judge_all: non-production never reaches Jev
        p_act, sev, team, dup = ANSWERS[a["id"]]
        teams = kwargs.get("teams") or triage.TEAM_CRITERIA
        offered = [c["id"] for c in candidates[a["id"]]]
        duplicate_of = None
        if offered:
            links = {k: v for k, v in (dup or {}).items() if k in offered}
            duplicate_of = {c: links.get(c, 0.0) for c in offered}
            duplicate_of[triage.NONE] = round(1.0 - sum(links.values()), 6)
        judgments[a["id"]] = triage.Judgment(
            model=MODEL, p_actionable=p_act,
            severity={lvl: sev.get(lvl, 0.0) for lvl in triage.SEV_LEVELS},
            team={t: team.get(t, 0.0) for t in teams},
            duplicate_of=duplicate_of)
    # Per-call latency, replayed from the real run.
    real_ms = {"orders-db": 753, "search-mem": 234, "checkout": 200,
               "payment": 221, "tls-cert": 257, "report-job": 228,
               "homepage": 178}
    calls = {aid: {"ms": real_ms[aid], "usage": {}} for aid in judgments}
    return judgments, {}, calls


class DemoClock:
    """Epoch seconds that move only when the visitor advances them."""

    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def timed_out(*args, **kwargs):
    """Stands in for triage.call_jev during the timeout drill."""
    raise triage.JevError(TIMEOUT_ERROR)


class DemoSession:
    """The demo's state: the staged incident, then whatever the visitor did,
    as a list of actions. The browser keeps that list, replays it here after
    a reload, and renders the page from the result.

    Everything runs through the server's TriageRunner: its review queue on a
    demo clock, routing, the shadow log and labels, and evaluation."""

    def __init__(self, actions=()):
        self.clock = DemoClock(OPENED)
        self._tmp = tempfile.TemporaryDirectory()
        config = triage.load_config(os.path.join(BASE, "demo", "jev-oncall.toml"))
        config.shadow_log = os.path.join(self._tmp.name, "shadow.jsonl")
        config.review_store = os.path.join(self._tmp.name, "reviews.jsonl")
        self.runner = server.TriageRunner(topology=config.topology, api_key="scripted",
                                          config=config, clock=self.clock)
        # The alerts arrived over the minute before the page opens.
        self.clock.t = OPENED - 15
        with mock.patch.object(triage, "judge_all", side_effect=replay_judge):
            self.runner.triage(alerts())
        self.clock.t = OPENED
        for aid, label in LABELS.items():
            self.runner.shadow.label(aid, label, "incident review")
        self.timeout_used = False
        self.actions = []
        for action in actions:
            self.apply(action)

    def apply(self, action):
        """One visitor action. Returns {"ok": bool, "message": str}; only
        actions that succeed are kept for replay."""
        if not isinstance(action, dict):
            return {"ok": False, "message": "not an action"}
        kind, aid = action.get("type"), str(action.get("id") or "")
        who = str(action.get("by") or "").strip()[:40] or "you"
        runner = self.runner
        if kind == "ack":
            record = runner.ack(aid, who)
            if not record:
                return self._not_pending(aid)
            result = {"ok": True, "message": f"Acked by {who}. The review is closed and won't page; "
                                             "the alert isn't resolved."}
        elif kind == "resolve":
            if not runner.resolve([aid]):
                return self._not_pending(aid)
            result = {"ok": True, "message": f"{aid} cleared before anyone acked, so its review "
                                             "was cancelled. No page."}
        elif kind == "advance":
            self.clock.t += ADVANCE_MIN * 60
            paged = runner.sweep_reviews()
            ids = ", ".join(e["id"] for e in paged)
            result = {"ok": True, "message": f"Demo clock is now {_iso(self.clock.t)[11:16]} UTC. " + (
                f"Nobody acked {ids} in time, so {'it pages' if len(paged) == 1 else 'they page'}."
                if paged else "No review reached its deadline.")}
        elif kind == "timeout":
            if self.timeout_used:
                return {"ok": False, "message": "The timeout drill already ran. Start over to run it again."}
            aid, title, desc, service, env, sev = TIMEOUT_ALERT
            alert = {"id": aid, "title": title, "description": desc, "service": service, "env": env,
                     "started_at": _iso(self.clock.t - 30), "configured_severity": sev}
            with mock.patch.object(triage, "call_jev", side_effect=timed_out):
                d = runner.triage([alert])["decisions"][0]
            self.timeout_used = True
            result = {"ok": True, "message": f"Jev timed out on {aid}, so it was routed by its configured "
                                             f"severity ({sev}): {d['action']} to {d['team']}."}
        elif kind == "label":
            try:
                runner.label(aid, action.get("label"), who)
            except ValueError as e:
                return {"ok": False, "message": str(e)}
            result = {"ok": True, "message": "Label saved. The evaluation below now uses it."}
        else:
            return {"ok": False, "message": f"unknown action {kind!r}"}
        self.actions.append(action)
        return result

    def _not_pending(self, aid):
        st = self.runner.reviews.states().get(aid)
        if st:
            return {"ok": False, "message": f"The review for {aid} is already {st['state']}."}
        return {"ok": False, "message": f"No review is waiting for {aid}."}

    def render(self):
        results, page_alerts = self.runner.results()
        results["meta"]["note"] = ""  # the demo banner says it, first
        results["meta"]["answer_source"] = "real"
        live = self.runner.live_view()
        live["demo_clock"] = True
        demo = {"clock": live["now"], "timeout_used": self.timeout_used, "advance_min": ADVANCE_MIN,
                "engine": {"pyodide": PYODIDE, "base": "engine/", "files": ENGINE_FILES}}
        return generate_dashboard.render_app(
            results, page_alerts, "build_demo.py", live=live, demo=demo, nav=NAV,
            footer="Built by build_demo.py from the real jev-1.13.0 run on 2026-09-23. "
                   "Run the real thing with the Docker demo: cd demo && docker compose up.")


def build():
    return DemoSession().render()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the dashboard demo for GitHub Pages.")
    ap.add_argument("--out", default=os.path.join(BASE, "docs", "demo", "index.html"))
    args = ap.parse_args(argv)
    page = build()
    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(page)
    # The engine: the same modules, served beside the page for the browser.
    for name in ENGINE_FILES:
        dest = os.path.join(out_dir, "engine", name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(os.path.join(BASE, name), dest)
    print(f"wrote {args.out} and {len(ENGINE_FILES)} engine files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
