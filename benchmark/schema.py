#!/usr/bin/env python3
"""Task schema for the jev-oncall reliability benchmark.

One fixed contract every model is judged against. Alerts go in; a routing
judgment comes out. Ground truth comes from the fault injector (see
faults.py), never from a human labeler guessing after the fact.

Alert (input)
    id, title, description, service, env, started_at (ISO-8601 UTC),
    configured_severity ("critical" | "warning" | "info")

GroundTruth (labels, from the injection log)
    actionable   bool -- does any human need to be reached?
    severity     "SEV1".."SEV4" -- SEV1/2 want a page, SEV3 a ticket, SEV4 nothing
    team         owning team: "database" | "compute" | "network" | "deploy"
    duplicate_of alert id of the incident root, or None for independent alerts

ModelOutput (what the model under test must produce per alert)
    severity     "SEV1".."SEV4"
    team         owning team
    action       "page" | "ticket" | "review" | "drop"
    confidence   0.0..1.0 -- P(this alert needs a human paged). Used for
                 calibration scoring, not for the action itself.
    linked_to    alert id this is a symptom/duplicate of, or None

Incident (one labeled scenario in the dataset)
    incident_id, fault (fault type + parameters), alerts, ground_truth
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

TEAMS = ("database", "compute", "network", "deploy")
SEVERITIES = ("SEV1", "SEV2", "SEV3", "SEV4")
ACTIONS = ("page", "ticket", "review", "drop")
CONFIGURED = ("critical", "warning", "info")


@dataclass
class Alert:
    id: str
    title: str
    description: str
    service: str
    env: str
    started_at: str  # ISO-8601 UTC
    configured_severity: str


@dataclass
class GroundTruth:
    actionable: bool
    severity: str  # SEV1..SEV4
    team: str
    duplicate_of: str | None = None


@dataclass
class Incident:
    incident_id: str
    fault: str  # fault type name, e.g. "db_pool_exhaustion"
    fault_params: dict
    alerts: list = field(default_factory=list)  # list[Alert]
    ground_truth: dict = field(default_factory=dict)  # alert id -> GroundTruth


@dataclass
class ModelOutput:
    alert_id: str
    severity: str
    team: str
    action: str
    confidence: float
    linked_to: str | None = None


def validate_alert(a: Alert) -> list[str]:
    errs = []
    if a.configured_severity not in CONFIGURED:
        errs.append(f"{a.id}: bad configured_severity {a.configured_severity!r}")
    return errs


def validate_ground_truth(gid: str, g: GroundTruth, alert_ids: set[str]) -> list[str]:
    errs = []
    if g.severity not in SEVERITIES:
        errs.append(f"{gid}: bad severity {g.severity!r}")
    if g.team not in TEAMS:
        errs.append(f"{gid}: bad team {g.team!r}")
    if g.duplicate_of is not None and g.duplicate_of not in alert_ids:
        errs.append(f"{gid}: duplicate_of points at unknown alert {g.duplicate_of!r}")
    if g.duplicate_of == gid:
        errs.append(f"{gid}: duplicate_of points at itself")
    if not g.actionable and g.severity in ("SEV1", "SEV2"):
        errs.append(f"{gid}: non-actionable but severity {g.severity}")
    return errs


def validate_output(o: ModelOutput) -> list[str]:
    errs = []
    if o.severity not in SEVERITIES:
        errs.append(f"{o.alert_id}: bad severity {o.severity!r}")
    if o.team not in TEAMS:
        errs.append(f"{o.alert_id}: bad team {o.team!r}")
    if o.action not in ACTIONS:
        errs.append(f"{o.alert_id}: bad action {o.action!r}")
    if not 0.0 <= o.confidence <= 1.0:
        errs.append(f"{o.alert_id}: confidence {o.confidence} out of range")
    return errs


def incident_to_json(incident: Incident) -> str:
    return json.dumps({
        "incident_id": incident.incident_id,
        "fault": incident.fault,
        "fault_params": incident.fault_params,
        "alerts": [asdict(a) for a in incident.alerts],
        "ground_truth": {k: asdict(v) for k, v in incident.ground_truth.items()},
    })


def incident_from_json(s: str) -> Incident:
    d = json.loads(s)
    return Incident(
        incident_id=d["incident_id"],
        fault=d["fault"],
        fault_params=d["fault_params"],
        alerts=[Alert(**a) for a in d["alerts"]],
        ground_truth={k: GroundTruth(**v) for k, v in d["ground_truth"].items()},
    )
