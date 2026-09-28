#!/usr/bin/env python3
"""Model interface for the benchmark.

A model is anything that turns alerts into routing judgments:

    class MyModel(TriageModel):
        name = "my-model"
        def judge(self, alerts: list[dict]) -> list[dict]:
            ...

Input:  list of alert dicts (see schema.Alert fields).
Output: list of dicts with keys: alert_id, severity (SEV1..SEV4),
        team (database|compute|network|deploy), action
        (page|ticket|review|drop), confidence (0..1 = P(needs a human paged)),
        linked_to (alert id or None).

Included models:
  SeverityBaseline  today's default: route by configured severity only
                    (critical -> page/SEV2, warning -> ticket/SEV3,
                    info -> drop/SEV4), team from service map. This is the
                    baseline every model must beat -- evaluate.py uses the
                    same one.
  RandomModel       seeded random judgments. Exists to exercise the harness,
                    not to be good.

Jev integration (not yet implemented): jev-oncall's triage.py already
produces Decisions with action/team/linked_to from Jev probabilities. The
adapter is a thin mapping: Decision -> ModelOutput, with confidence =
P(page) from the Jev distribution. Left for the next step so this package
stays dependency-free and offline-testable.
"""
from __future__ import annotations

import random
from abc import ABC, abstractmethod

from schema import ACTIONS, SEVERITIES, TEAMS, ModelOutput, validate_output

SERVICE_TEAM = {
    "orders-db": "database", "session-store": "database", "metrics-store": "database",
    "checkout-api": "deploy", "payment-service": "deploy", "inventory-api": "deploy",
    "auth-service": "deploy", "ci-runner": "deploy",
    "search-api": "compute", "billing-events": "compute",
    "log-aggregator": "compute", "notification-svc": "compute",
    "report-batch": "compute",
    "edge": "network", "homepage": "network", "api-edge": "network",
    "cdn-origin": "network",
}


class TriageModel(ABC):
    name = "base"

    @abstractmethod
    def judge(self, alerts: list[dict]) -> list[dict]:
        """Return one output dict per alert, in any order."""


class SeverityBaseline(TriageModel):
    """Route by configured severity only. The incumbent every model must beat."""
    name = "severity-baseline"

    _MAP = {"critical": ("page", "SEV2", 0.9),
            "warning": ("ticket", "SEV3", 0.5),
            "info": ("drop", "SEV4", 0.1)}

    def judge(self, alerts):
        out = []
        for a in alerts:
            action, sev, conf = self._MAP.get(a["configured_severity"],
                                              ("drop", "SEV4", 0.1))
            out.append({"alert_id": a["id"], "severity": sev,
                        "team": SERVICE_TEAM.get(a["service"], "compute"),
                        "action": action, "confidence": conf,
                        "linked_to": None})
        return out


class RandomModel(TriageModel):
    """Seeded random judgments. For harness self-testing only."""
    name = "random"

    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)

    def judge(self, alerts):
        out = []
        for a in alerts:
            out.append({"alert_id": a["id"],
                        "severity": self.rng.choice(SEVERITIES),
                        "team": self.rng.choice(TEAMS),
                        "action": self.rng.choice(ACTIONS),
                        "confidence": round(self.rng.random(), 3),
                        "linked_to": None})
        return out


class JevModel(TriageModel):
    """Adapter for jev-oncall's triage.py + TypeSafe Jev. Not implemented yet.

    The mapping, when built: run triage.route(alerts) to get Decisions, then
      severity    SEV1/2 if action in (PAGE, PAGE_NOW), SEV3 if TICKET/REVIEW,
                  SEV4 if DROP/LOG
      team        Decision.team
      action      page|ticket|review|drop from Decision.action
      confidence  P(page) from the stored Jev answer distribution
      linked_to   Decision.linked_to
    Needs a TypeSafe API key, so it lives outside the offline harness.
    """
    name = "jev"

    def judge(self, alerts):
        raise NotImplementedError(
            "JevModel needs a TypeSafe API key; see the docstring for the mapping.")


def to_model_outputs(raw: list[dict]) -> list[ModelOutput]:
    outs = [ModelOutput(
        alert_id=r["alert_id"], severity=r["severity"], team=r["team"],
        action=r["action"], confidence=float(r["confidence"]),
        linked_to=r.get("linked_to")) for r in raw]
    errs = [e for o in outs for e in validate_output(o)]
    if errs:
        raise ValueError("model produced invalid outputs:\n" + "\n".join(errs))
    return outs
