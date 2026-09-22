#!/usr/bin/env python3
"""Jev incident-triage POC.

One Jev API call per alert asks 4 typed questions (no LLM prose, no parsing):
  actionable  Noul    - does a human need to do something?
  severity    Choice  - SEV1..SEV4
  team        Choice  - database / compute / network / deploy
  duplicate   Noul    - downstream symptom of another firing alert?

Routing policy lives in plain code; Jev only makes the judgments.

Setup:
  export TYPESAFE_API_KEY=<your key from console.typesafe.ai/keys>

Run:
  python3 triage.py          # prints routing table, writes results.json
"""

import json
import os
import sys
import time
import urllib.request

API_URL = "https://api.typesafe.ai/v1/systemone"
BASE = os.path.dirname(os.path.abspath(__file__))

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

DUP_THRESHOLD = 0.70   # dedup is destructive: needs a high bar
REVIEW_CONF = 0.75     # below this severity confidence -> human review


def build_payload(alert, all_alerts):
    return {
        "model": "jev-latest",
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


def call_jev(payload, api_key):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result, (time.time() - t0) * 1000


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
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        sys.exit("Set TYPESAFE_API_KEY first "
                 "(get one at console.typesafe.ai/keys).")
    with open(os.path.join(BASE, "alerts.json")) as f:
        alerts = json.load(f)
    results = []
    agree = {"actionable": 0, "severity": 0, "team": 0, "duplicate": 0}
    tot_in, tot_out, tot_ms = 0, 0, 0.0

    for a in alerts:
        resp, dt_ms = call_jev(build_payload(a, alerts), api_key)
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
        print(f"[{a['id']}] {action:34s} sev={sev}({sev_conf:.2f}) "
              f"team={team} dup_p={ans['duplicate']['noul']:.2f} "
              f"{dt_ms:6.0f}ms")

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
    with open(os.path.join(BASE, "results.json"), "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n{n} alerts | {tot_in} in / {tot_out} out tokens | "
          f"{tot_ms/1000:.1f}s total, {tot_ms/n:.0f}ms avg per alert")
    print("agreement vs author's labels:",
          ", ".join(f"{k} {v}/{n}" for k, v in agree.items()))


if __name__ == "__main__":
    main()
