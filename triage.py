#!/usr/bin/env python3
"""Jev incident-triage POC.

Every alert gets 4 typed Jev questions (no LLM prose, no parsing):
  actionable  Noul    - does a human need to do something?
  severity    Choice  - SEV1..SEV4
  team        Choice  - database / compute / network / deploy
  duplicate   Noul    - downstream symptom of another firing alert?

Routing policy lives in plain code; Jev only makes the judgments.
Usage: python3 triage.py  (prints table, writes results.json)
"""

import json
import subprocess
import sys
import time

BASE = "/home/hatch/workspace/jev-incident-triage"
BIN = "/home/hatch/workspace/skills/typesafe/bin/system_one.py"

SEV_CRITERIA = {
    "SEV1": "customer-facing outage or data-loss risk, page immediately",
    "SEV2": "degraded service or major feature broken, urgent but not full outage",
    "SEV3": "minor impact, fix in business hours",
    "SEV4": "no customer impact, noise or todo",
}
TEAM_CRITERIA = {
    "database": "databases, caches, queues, storage",
    "compute": "servers, containers, batch jobs, stream processing",
    "network": "CDN, edge, DNS, TLS, connectivity",
    "deploy": "releases, deploys, canary analysis, CI/CD",
}

DUP_THRESHOLD = 0.70   # dedup is destructive: needs high bar
REVIEW_CONF = 0.75     # below this severity confidence -> human review


def build_payload(alert, all_alerts):
    return {
        "state": {
            "alert": {"title": alert["title"], "description": alert["description"]},
            "other_firing_alerts": [
                x["title"] for x in all_alerts if x["id"] != alert["id"]
            ],
        },
        "questions": {
            "actionable": {
                "type": "noul",
                "instructions": (
                    "Is this alert actionable - does it require a human to do "
                    "something right now? A firing test canary, a dev-box metric, "
                    "or a healthy-but-slow batch job is NOT actionable."
                ),
            },
            "severity": {
                "type": "choice",
                "instructions": "Rate incident severity for this alert.",
                "criteria": SEV_CRITERIA,
            },
            "team": {
                "type": "choice",
                "instructions": "Which team owns this alert?",
                "criteria": TEAM_CRITERIA,
            },
            "duplicate": {
                "type": "noul",
                "instructions": (
                    "Is this alert a duplicate or downstream symptom of one of "
                    "the other firing alerts listed? Only say yes if it is "
                    "clearly caused by another alert, not merely similar."
                ),
            },
        },
    }


def call_jev(payload):
    with open(BASE + "/_payload.json", "w") as f:
        json.dump(payload, f)
    t0 = time.time()
    out = subprocess.run(
        [sys.executable, BIN, "--payload-file", BASE + "/_payload.json"],
        capture_output=True, text=True, timeout=120,
    )
    dt_ms = (time.time() - t0) * 1000
    if out.returncode != 0:
        raise RuntimeError(out.stderr[-500:])
    resp = json.loads(out.stdout)
    return resp, dt_ms


def route(actionable, dup, sev, sev_conf):
    needs_review = sev_conf < REVIEW_CONF
    if not actionable:
        action = "DROP (noise)"
    elif dup:
        action = "DEDUP (link to primary)"
    elif sev == "SEV1":
        action = "PAGE NOW"
    elif sev == "SEV2":
        action = "PAGE"
    elif sev == "SEV3":
        action = "TICKET"
    else:
        action = "LOG"
    if needs_review and actionable:
        action += " + HUMAN REVIEW (low conf)"
    return action


def main():
    alerts = json.load(open(BASE + "/alerts.json"))
    results = []
    agree = {"actionable": 0, "severity": 0, "team": 0, "duplicate": 0}
    tot_in, tot_out, tot_ms = 0, 0, 0.0

    for a in alerts:
        resp, dt_ms = call_jev(build_payload(a, alerts))
        ans = resp["answers"]
        actionable = ans["actionable"]["noul"] >= 0.5
        dup = ans["duplicate"]["noul"] >= DUP_THRESHOLD
        sev = ans["severity"]["choice"]
        sev_conf = ans["severity"].get("confidence", 0.0)
        team = ans["team"]["choice"]
        team_conf = ans["team"].get("confidence", 0.0)
        action = route(actionable, dup, sev, sev_conf)

        exp = a["expected"]
        agree["actionable"] += actionable == exp["actionable"]
        agree["severity"] += sev == exp["severity"]
        agree["team"] += team == exp["team"]
        agree["duplicate"] += dup == exp["duplicate"]

        tot_in += resp["usage"]["input_tokens"]
        tot_out += resp["usage"]["output_tokens"]
        tot_ms += dt_ms
        results.append({
            "id": a["id"], "actionable": actionable, "severity": sev,
            "sev_conf": round(sev_conf, 2), "team": team,
            "team_conf": round(team_conf, 2),
            "duplicate": dup,
            "dup_p": round(ans["duplicate"]["noul"], 2),
            "action": action, "ms": round(dt_ms),
        })
        flag = "" if action.split(" ")[0] in ("PAGE", "DROP", "DEDUP", "TICKET", "LOG") else ""
        print(f"[{a['id']}] {action:34s} sev={sev}({sev_conf:.2f}) team={team} dup_p={ans['duplicate']['noul']:.2f} {dt_ms:6.0f}ms")

    n = len(alerts)
    summary = {
        "alerts": n,
        "total_input_tokens": tot_in,
        "total_output_tokens": tot_out,
        "total_ms": round(tot_ms),
        "avg_ms_per_alert": round(tot_ms / n),
        "agreement_vs_author_labels": {k: f"{v}/{n}" for k, v in agree.items()},
        "results": results,
    }
    json.dump(summary, open(BASE + "/results.json", "w"), indent=2)
    print(f"\n{n} alerts | {tot_in} in / {tot_out} out tokens | "
          f"{tot_ms/1000:.1f}s total, {tot_ms/n:.0f}ms avg per alert")
    print("agreement vs author's labels:",
          ", ".join(f"{k} {v}/{n}" for k, v in agree.items()))


if __name__ == "__main__":
    main()
