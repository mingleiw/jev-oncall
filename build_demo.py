#!/usr/bin/env python3
"""Build the interactive dashboard demo that GitHub Pages serves.

    python3 build_demo.py                      # writes docs/demo/index.html

It runs the real server pipeline (candidate causes, routing, the dedup
graph, the review queue, shadow mode) on the Docker demo's staged incident.
Only Jev's answers are scripted, chosen so every behavior shows up once, and
the page says so. In the browser, Ack and labels work without a server.

Standard library only.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from unittest import mock

import generate_dashboard
import server
import shadow
import triage

BASE = os.path.dirname(os.path.abspath(__file__))
START = "2026-09-24T14:03"  # the incident's first minute; alerts fire over it

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
    ("gc-pause", 45, "GcPauseSpike: GC pause p99 120ms on batch-worker",
     "One spike during a scheduled compaction; back to 15ms within a minute.", "batch-worker", "prod", "info"),
]

# Scripted answers: (P(actionable), severity, team, duplicate_of).
ANSWERS = {
    "orders-db": (0.97, {"SEV1": 0.70, "SEV2": 0.25, "SEV3": 0.05}, {"database": 0.92, "compute": 0.08}, None),
    "search-mem": (0.60, {"SEV2": 0.35, "SEV3": 0.50, "SEV4": 0.15}, {"compute": 0.85, "database": 0.15}, None),
    "checkout": (0.95, {"SEV1": 0.50, "SEV2": 0.40, "SEV3": 0.10}, {"database": 0.75, "compute": 0.25},
                 {"orders-db": 0.91}),
    "payment": (0.93, {"SEV1": 0.45, "SEV2": 0.45, "SEV3": 0.10}, {"compute": 0.80, "database": 0.20},
                {"checkout": 0.88}),
    "tls-cert": (0.90, {"SEV3": 0.70, "SEV4": 0.30}, {"network": 0.95, "deploy": 0.05}, None),
    "report-job": (0.30, {"SEV3": 0.20, "SEV4": 0.80}, {"compute": 0.90, "deploy": 0.10}, None),
    "homepage": (0.90, {"SEV1": 0.10, "SEV2": 0.75, "SEV3": 0.15}, {"compute": 0.55, "network": 0.45}, None),
    "gc-pause": (0.02, {"SEV3": 0.03, "SEV4": 0.97}, {"compute": 1.0}, None),
}

# Labels someone added after the incident review, so the page shows scoring.
LABELS = {
    "orders-db": {"severity": "SEV1", "actionable": True, "team": "database", "duplicate_of": None},
    "report-job": {"severity": "SEV4", "actionable": False, "team": "compute", "duplicate_of": None},
}

MODEL = "scripted-demo"


def alerts():
    out = []
    for aid, secs, title, desc, service, env, sev in ALERTS:
        out.append({"id": aid, "title": title, "description": desc, "service": service, "env": env,
                    "started_at": f"{START}:{secs:02d}Z", "configured_severity": sev})
    return out


def scripted_judge(batch, candidates, *args, **kwargs):
    """Stands in for triage.judge_all: the scripted answers, as Judgments."""
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
    return judgments, {}, {}


def build():
    config = triage.load_config(os.path.join(BASE, "demo", "jev-oncall.toml"))
    with tempfile.TemporaryDirectory() as tmp:
        config.shadow_log = os.path.join(tmp, "shadow.jsonl")
        runner = server.TriageRunner(topology=config.topology, api_key="scripted", config=config)
        with mock.patch.object(triage, "judge_all", side_effect=scripted_judge):
            runner.triage(alerts())
        for aid, label in LABELS.items():
            runner.shadow.label(aid, label, "incident review")
        results, page_alerts = runner.results()
        live = runner.live_view()
    results["meta"]["note"] = ""  # the demo banner says it, first
    return generate_dashboard.render(
        results, page_alerts, "build_demo.py", label="Demo", live=live, demo=True,
        footer="Built by build_demo.py from sample data. Run the real thing with the Docker demo: "
               "cd demo && docker compose up.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the dashboard demo for GitHub Pages.")
    ap.add_argument("--out", default=os.path.join(BASE, "docs", "demo", "index.html"))
    args = ap.parse_args(argv)
    page = build()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
