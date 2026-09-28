#!/usr/bin/env python3
"""Eval harness: score a triage model against a generated dataset.

    python3 harness.py --dataset dataset.jsonl --model severity-baseline
    python3 harness.py --dataset dataset.jsonl --model random --seed 7 --out results.json

Metrics (aggregated over all alerts in all incidents):
  severity_acc   exact SEV match on actionable alerts
  owner_acc      team match on incident roots (duplicate_of is None)
  link_acc       symptom/duplicate alerts: linked_to == duplicate_of
  action outcomes, in on-call terms:
    silent_miss    needed a human, reached none
    missed_page    wanted a page, got a ticket
    delayed_page   wanted a page, got a review (ack window)
    false_page     paged something that wanted nothing
    duplicate_page paged an alert that should have linked to its root
    misrouted      page/review reached the wrong team
  calibration    Brier score and ECE for confidence as P(needs a page)

Deterministic and offline: the only nondeterminism is the model's own.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict

from schema import Incident, incident_from_json
from models import RandomModel, SeverityBaseline, to_model_outputs

WANTS_PAGE_SEV = ("SEV1", "SEV2")


def wants_page(gt) -> bool:
    return gt.actionable and gt.severity in WANTS_PAGE_SEV


def brier_ece(pairs: list[tuple[float, int]], bins: int = 10):
    """pairs: (confidence, 1 if wanted a page else 0)."""
    if not pairs:
        return 0.0, 0.0
    brier = sum((p - y) ** 2 for p, y in pairs) / len(pairs)
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for p, y in pairs:
        buckets[min(int(p * bins), bins - 1)].append((p, y))
    ece = 0.0
    for bkt in buckets:
        if not bkt:
            continue
        mean_p = sum(p for p, _ in bkt) / len(bkt)
        mean_y = sum(y for _, y in bkt) / len(bkt)
        ece += abs(mean_p - mean_y) * len(bkt) / len(pairs)
    return brier, ece


def score_incident(incident: Incident, outputs: list) -> dict:
    by_id = {o.alert_id: o for o in outputs}
    m = {"n": 0, "severity_ok": 0, "owner_ok": 0, "owner_n": 0,
         "link_ok": 0, "link_n": 0,
         "silent_miss": 0, "missed_page": 0, "delayed_page": 0,
         "false_page": 0, "duplicate_page": 0, "misrouted": 0,
         "cal": []}
    for alert in incident.alerts:
        o = by_id.get(alert.id)
        if o is None:
            continue
        gt = incident.ground_truth[alert.id]
        m["n"] += 1
        if gt.actionable and o.severity == gt.severity:
            m["severity_ok"] += 1
        if gt.duplicate_of is None:
            m["owner_n"] += 1
            if o.team == gt.team:
                m["owner_ok"] += 1
        else:
            m["link_n"] += 1
            if o.linked_to == gt.duplicate_of:
                m["link_ok"] += 1
        wp = wants_page(gt)
        m["cal"].append((o.confidence, 1 if wp else 0))
        pages = o.action == "page"
        if wp and o.action == "drop":
            m["silent_miss"] += 1
        elif wp and o.action == "ticket":
            m["missed_page"] += 1
        elif wp and o.action == "review":
            m["delayed_page"] += 1
        if not wp and pages:
            m["false_page"] += 1
        if gt.duplicate_of is not None and pages:
            m["duplicate_page"] += 1
        if o.action in ("page", "review") and o.team != gt.team:
            m["misrouted"] += 1
    return m


def aggregate(per_incident: list[dict]) -> dict:
    tot = {"n": 0, "severity_ok": 0, "owner_ok": 0, "owner_n": 0,
           "link_ok": 0, "link_n": 0, "silent_miss": 0, "missed_page": 0,
           "delayed_page": 0, "false_page": 0, "duplicate_page": 0,
           "misrouted": 0}
    for m in per_incident:
        for k in tot:
            tot[k] += m[k]
    out = dict(tot)
    # calibration pairs are handled by the caller (brier_ece)
    out["severity_acc"] = tot["severity_ok"] / max(tot["n"], 1)
    out["owner_acc"] = tot["owner_ok"] / max(tot["owner_n"], 1)
    out["link_acc"] = tot["link_ok"] / max(tot["link_n"], 1)
    out["incidents"] = len(per_incident)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--model", choices=["severity-baseline", "random"],
                    default="severity-baseline")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    model = SeverityBaseline() if args.model == "severity-baseline" \
        else RandomModel(args.seed)

    per_incident = []
    cal_all: list[tuple[float, int]] = []
    with open(args.dataset, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            incident = incident_from_json(line)
            alerts = [asdict(a) for a in incident.alerts]
            outputs = to_model_outputs(model.judge(alerts))
            m = score_incident(incident, outputs)
            cal_all.extend(m.pop("cal"))
            per_incident.append({"incident_id": incident.incident_id,
                                 "fault": incident.fault, **m})

    summary = aggregate(per_incident)
    summary["brier"], summary["ece"] = (round(v, 4) for v in
                                        brier_ece(cal_all))
    summary["model"] = model.name
    # aggregate() already computed accuracies from per-incident counts.

    print(f"model      {summary['model']}")
    print(f"incidents  {summary['incidents']}  alerts {summary['n']}")
    print(f"severity_acc {summary['severity_acc']:.3f}  "
          f"owner_acc {summary['owner_acc']:.3f}  link_acc {summary['link_acc']:.3f}")
    print(f"silent_miss {summary['silent_miss']}  missed_page {summary['missed_page']}  "
          f"delayed_page {summary['delayed_page']}  false_page {summary['false_page']}  "
          f"duplicate_page {summary['duplicate_page']}  misrouted {summary['misrouted']}")
    print(f"brier {summary['brier']:.4f}  ece {summary['ece']:.4f}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "per_incident": per_incident},
                      f, indent=1)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
