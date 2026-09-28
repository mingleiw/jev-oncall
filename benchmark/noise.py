#!/usr/bin/env python3
"""Noise replay: layer production-like mess over clean fault captures.

Three noise types, each preserving ground truth:

duplicates   an alert refires (new id, later timestamp); the copy is labeled
             duplicate_of the original, and if the original was a symptom the
             copy inherits its duplicate_of chain root.
flapping     a firing alert resolves and fires again (memory_leak marks its
             alert via fault_params["flapping_alert"]). The refire is a
             duplicate_of the original firing: paging twice is the error.
unrelated    interleaved warnings from other services (cert, disk) with their
             own independent ground truth: actionable=False, SEV4.

Noise never changes what the right answer is; it only makes the stream look
like 3am on a bad week.
"""
from __future__ import annotations

import copy
import itertools
import random
from datetime import datetime, timedelta

from schema import Alert, GroundTruth, Incident

UNRELATED = [
    ("Firing: TLS cert expires in {n} days",
     "Public certificate for api.example.com expires in {n} days. "
     "Auto-renewal job failing.", "edge", "warning"),
    ("Firing: disk usage {pct}% on metrics-store-0",
     "Disk at {pct}%, growing ~2%/day. Fills in ~{d} days at current rate.",
     "metrics-store", "warning"),
    ("Firing: notification-svc queue depth above {k}k",
     "Queue depth elevated but draining; no customer impact observed.",
     "notification-svc", "warning"),
]


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def apply_noise(incident: Incident, rng: random.Random, ids: itertools.count,
                level: float = 1.0) -> Incident:
    """Return a new Incident with noise layered in. level 0.0 = clean."""
    inc = copy.deepcopy(incident)
    if level <= 0.0:
        return inc

    def new_id() -> str:
        return f"{inc.incident_id}-n{next(ids):02d}"

    # -- duplicates: pick a random non-noise alert and refire it -------------
    actionable = [a for a in inc.alerts
                  if inc.ground_truth[a.id].actionable]
    if actionable and rng.random() < 0.6 * level:
        orig = rng.choice(actionable)
        g = inc.ground_truth[orig.id]
        dup = copy.deepcopy(orig)
        dup.id = new_id()
        dup.title = "Firing (repeat): " + orig.title.replace("Firing: ", "")
        dup.started_at = (_parse(orig.started_at)
                          + timedelta(seconds=rng.randint(120, 900))).isoformat()
        inc.alerts.append(dup)
        root = g.duplicate_of or orig.id
        inc.ground_truth[dup.id] = GroundTruth(
            actionable=True, severity=g.severity, team=g.team,
            duplicate_of=root)

    # -- flapping: resolve + refire ------------------------------------------
    flap_id = inc.fault_params.get("flapping_alert")
    if flap_id and rng.random() < 0.8 * level:
        orig = next(a for a in inc.alerts if a.id == flap_id)
        g = inc.ground_truth[flap_id]
        refire = copy.deepcopy(orig)
        refire.id = new_id()
        refire.title = "Firing (again): " + orig.title.replace("Firing: ", "")
        refire.started_at = (_parse(orig.started_at)
                             + timedelta(seconds=rng.randint(600, 1800))).isoformat()
        inc.alerts.append(refire)
        inc.ground_truth[refire.id] = GroundTruth(
            actionable=True, severity=g.severity, team=g.team,
            duplicate_of=flap_id)
        inc.fault_params.setdefault("resolves", []).append({
            "alert_id": flap_id,
            "resolved_at": (_parse(orig.started_at)
                            + timedelta(seconds=rng.randint(120, 500))).isoformat(),
        })

    # -- unrelated warnings ---------------------------------------------------
    for _ in range(rng.randint(0, int(2 * level))):
        title_t, desc_t, service, sev = rng.choice(UNRELATED)
        kw = {"n": rng.randint(20, 30), "pct": rng.randint(80, 90),
              "d": rng.randint(3, 9), "k": rng.randint(5, 40)}
        aid = new_id()
        base = _parse(inc.alerts[0].started_at)
        inc.alerts.append(Alert(
            id=aid, title=title_t.format(**kw), description=desc_t.format(**kw),
            service=service, env="prod",
            started_at=(base + timedelta(
                seconds=rng.randint(-300, 1500))).isoformat(),
            configured_severity=sev))
        from faults import SERVICE_TEAM
        inc.ground_truth[aid] = GroundTruth(
            actionable=False, severity="SEV4",
            team=SERVICE_TEAM[service], duplicate_of=None)

    # Keep the stream in time order, like Alertmanager would deliver it.
    inc.alerts.sort(key=lambda a: a.started_at)
    return inc
