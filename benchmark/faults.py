#!/usr/bin/env python3
"""Fault catalog for the reliability benchmark.

Each fault type is a generator: given a seeded RNG it produces one Incident
whose ground truth is DERIVED FROM THE INJECTED FAULT, not hand-labeled.

That is the whole methodology. The injection log says "at t+20s we exhausted
the orders-db connection pool"; every alert downstream of that is a symptom of
that fault by construction. No human looked at an alert and guessed its
severity afterwards, so there is no circularity for a model to exploit.

Conventions per fault:
  root alert    actionable=True,  severity=the fault's SEV, team=owning team
  symptom alert actionable=True,  severity=one notch less urgent, team=root's
                team, duplicate_of=root alert id (the correct behavior is to
                link it to the root, not page a second team)
  noise alert   actionable=False, severity=SEV4 (nothing should reach a human)

Each fault also carries `rules_fragment()`: a demo-style Prometheus rule group
(time-keyed, like demo/rules.yml) so the same catalog can later drive the real
Compose stack. That path is untested here -- the generator below simulates the
alert stream directly, which is deterministic and needs no Docker.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

from schema import Alert, GroundTruth, Incident

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

REGIONS = ("us-west-2", "us-east-1", "eu-west-1")

# SEV1 -> symptom SEV2, SEV2 -> SEV2, SEV3 -> SEV3: symptoms never outrank root.
SYMPTOM_SEV = {"SEV1": "SEV2", "SEV2": "SEV2", "SEV3": "SEV3", "SEV4": "SEV4"}


class Builder:
    """Accumulates alerts + ground truth for one incident."""

    def __init__(self, incident_id: str, fault: str, fault_params: dict,
                 t0: datetime, ids: itertools.count):
        self.incident = Incident(incident_id=incident_id, fault=fault,
                                 fault_params=fault_params)
        self.t0 = t0
        self.ids = ids
        self.root_id: str | None = None

    def _alert(self, title, description, service, env, offset_s,
               configured_severity) -> str:
        aid = f"{self.incident.incident_id}-a{next(self.ids):02d}"
        self.incident.alerts.append(Alert(
            id=aid, title=title, description=description, service=service,
            env=env,
            started_at=(self.t0 + timedelta(seconds=offset_s)).isoformat(),
            configured_severity=configured_severity))
        return aid

    def root(self, title, description, service, offset_s,
             configured_severity, severity) -> str:
        aid = self._alert(title, description, service, "prod", offset_s,
                          configured_severity)
        self.incident.ground_truth[aid] = GroundTruth(
            actionable=True, severity=severity,
            team=SERVICE_TEAM[service], duplicate_of=None)
        self.root_id = aid
        return aid

    def symptom(self, title, description, service, offset_s,
                configured_severity, root_severity) -> str:
        aid = self._alert(title, description, service, "prod", offset_s,
                          configured_severity)
        self.incident.ground_truth[aid] = GroundTruth(
            actionable=True, severity=SYMPTOM_SEV[root_severity],
            team=self.incident.ground_truth[self.root_id].team,
            duplicate_of=self.root_id)
        return aid

    def noise(self, title, description, service, offset_s,
              configured_severity, env="prod") -> str:
        aid = self._alert(title, description, service, env, offset_s,
                          configured_severity)
        self.incident.ground_truth[aid] = GroundTruth(
            actionable=False, severity="SEV4",
            team=SERVICE_TEAM[service], duplicate_of=None)
        return aid


# --------------------------------------------------------------------------
# Fault types. Each: (rng, incident_id, ids, t0) -> Incident.
# --------------------------------------------------------------------------

def db_pool_exhaustion(rng, incident_id, ids, t0) -> Incident:
    """orders-db pool saturates; checkout-api and payment-service fail downstream."""
    region = rng.choice(REGIONS)
    pool = rng.choice([500, 1000, 2000])
    b = Builder(incident_id, "db_pool_exhaustion",
                {"region": region, "pool_size": pool}, t0, ids)
    b.root(
        f"Firing: connection pool exhausted on orders-db ({region})",
        f"All {pool} connections in the orders-db pool checked out since "
        f"{t0:%H:%M} UTC. New queries queueing past the 5s timeout after a "
        f"migration added an unindexed lookup.",
        "orders-db", 10, "critical", "SEV1")
    b.symptom(
        "Firing: checkout-api 5xx rate 31% for 5m",
        "checkout-api 5xx climbed from 0.1% to 31%. Most errors are timeouts "
        "acquiring an orders-db connection.",
        "checkout-api", 25, "critical", "SEV1")
    b.symptom(
        "Firing: payment authorization failures 18%",
        "Authorization calls failing with upstream 503 from checkout-api.",
        "payment-service", 40, "critical", "SEV1")
    if rng.random() < 0.5:
        b.noise(f"Firing: TLS cert expires in {rng.randint(20, 30)} days",
                "Public certificate for api.example.com expires soon. "
                "Auto-renewal has failed twice.",
                "edge", rng.randint(30, 60), "warning")
    return b.incident


def bad_deploy(rng, incident_id, ids, t0) -> Incident:
    """A deploy rolls out and the service's error rate spikes minutes later."""
    service = rng.choice(["checkout-api", "inventory-api", "auth-service"])
    region = rng.choice(REGIONS)
    ver = f"v2.{rng.randint(10, 99)}.{rng.randint(0, 9)}"
    pct = rng.randint(40, 98)
    b = Builder(incident_id, "bad_deploy",
                {"service": service, "region": region, "version": ver}, t0, ids)
    b.root(
        f"Firing: {service} error rate {pct}% for 6m ({region})",
        f"5xx rate on {service} climbed from 0.1% to {pct}% starting "
        f"{t0:%H:%M} UTC, {rng.randint(2, 9)} minutes after deploy {ver} "
        f"rolled to {region}.",
        service, 15, "critical", "SEV1")
    if rng.random() < 0.6:
        down = {"checkout-api": "payment-service"}.get(service)
        if down:
            b.symptom(
                f"Firing: {down} 5xx rate {pct // 2}% ({region})",
                f"{down} returning 503s since {t0:%H:%M} UTC. Upstream "
                f"{service} in full outage since deploy {ver}.",
                down, 35, "critical", "SEV1")
    return b.incident


def disk_full(rng, incident_id, ids, t0) -> Incident:
    """Log aggregator disk fills; ingestion degrades."""
    pct = rng.randint(94, 99)
    b = Builder(incident_id, "disk_full", {"disk_pct": pct}, t0, ids)
    b.root(
        f"Firing: disk usage {pct}% on log-aggregator-01",
        f"Disk at {pct}% on log-aggregator-01, growing ~2%/day. Log ingestion "
        f"dropping writes; retention jobs failing.",
        "log-aggregator", 20, "critical", "SEV2")
    if rng.random() < 0.5:
        b.symptom(
            "Firing: billing-events write errors 12%",
            "billing-events failing to ship audit logs; upstream "
            "log-aggregator rejecting writes (disk full).",
            "billing-events", 45, "warning", "SEV2")
    return b.incident


def cert_expiry(rng, incident_id, ids, t0) -> Incident:
    """TLS cert expiring soon. Real, but not urgent: SEV3, no cascade."""
    days = rng.randint(18, 29)
    b = Builder(incident_id, "cert_expiry", {"days_left": days}, t0, ids)
    b.root(
        f"Firing: TLS certificate for api.example.com expires in {days} days",
        f"Auto-renewal has failed twice; the ACME challenge returns 404. "
        f"No customer impact yet.",
        "edge", 30, "warning", "SEV3")
    return b.incident


def latency_spike(rng, incident_id, ids, t0) -> Incident:
    """Homepage p99 blows past SLO; conversion drops. Configured as warning,
    but customers are feeling it -- the trap is under-reacting."""
    p99 = round(rng.uniform(1.8, 3.2), 1)
    conv = rng.randint(15, 30)
    b = Builder(incident_id, "latency_spike",
                {"p99_s": p99, "conv_drop_pct": conv}, t0, ids)
    b.root(
        "Firing: homepage p99 latency above SLO",
        f"p99 latency {p99}s against a 400ms SLO for 10 minutes; checkout "
        f"conversion down {conv}%. Error rate normal.",
        "homepage", 40, "warning", "SEV2")
    return b.incident


def memory_leak(rng, incident_id, ids, t0) -> Incident:
    """search-api heap grows until OOMKill, then recovers. Flaps: fires,
    clears on restart, fires again. The trap is paging twice."""
    b = Builder(incident_id, "memory_leak", {}, t0, ids)
    aid = b.root(
        "Firing: memory 91% of limit on search-api",
        "Heap usage climbing steadily across restarts; GC not keeping up. "
        "Pattern matches a slow leak in the query cache.",
        "search-api", 15, "warning", "SEV3")
    b.incident.fault_params["flapping_alert"] = aid
    return b.incident


def staging_noise(rng, incident_id, ids, t0) -> Incident:
    """CI runner disk full in staging, configured critical. The trap is paging
    on a non-production alert: nothing should reach a human."""
    b = Builder(incident_id, "staging_noise", {}, t0, ids)
    b.noise("Firing: disk 97% full on staging CI runner",
            "Staging CI runner disk nearly full. No production impact.",
            "ci-runner", 15, "critical", env="staging")
    return b.incident


def slow_batch(rng, incident_id, ids, t0) -> Incident:
    """Nightly report ran late but completed. Configured critical; the trap is
    paging on something with zero customer impact."""
    mins = rng.randint(8, 20)
    b = Builder(incident_id, "slow_batch", {"late_mins": mins}, t0, ids)
    b.noise(f"Firing: nightly revenue report finished {mins}m late",
            f"The job completed successfully and the report was delivered. "
            f"Runtime against a 29m median. No customer-facing impact.",
            "report-batch", 35, "critical")
    return b.incident


FAULTS = {
    "db_pool_exhaustion": db_pool_exhaustion,
    "bad_deploy": bad_deploy,
    "disk_full": disk_full,
    "cert_expiry": cert_expiry,
    "latency_spike": latency_spike,
    "memory_leak": memory_leak,
    "staging_noise": staging_noise,
    "slow_batch": slow_batch,
}

# Rough real-world mix: most incidents are noise or minor; SEV1s are rare.
# Weights are over fault names, in the same order as FAULTS.
FAULT_WEIGHTS = [3, 3, 2, 3, 2, 2, 4, 3]


def rules_fragment(fault_name: str, uptime_offset_s: int = 0) -> str:
    """Render a demo-style Prometheus rule group for a fault.

    Time-keyed like demo/rules.yml (fires based on seconds since Prometheus
    started), so the same catalog can drive the real Compose stack later:
    write the fragment into rules.yml, `docker compose up`, capture the
    Alertmanager webhook stream. UNTESTED against live Compose -- the
    generator path below is the tested one.
    """
    names = {
        "db_pool_exhaustion": [("OrdersDbPoolExhausted", 10, "critical"),
                               ("CheckoutApiErrorRate", 25, "critical"),
                               ("PaymentServiceErrors", 40, "critical")],
        "bad_deploy": [("DeployErrorRateSpike", 15, "critical")],
        "disk_full": [("LogAggregatorDiskFull", 20, "critical")],
        "cert_expiry": [("TlsCertExpiringSoon", 30, "warning")],
        "latency_spike": [("HomepageLatencyHigh", 40, "warning")],
        "memory_leak": [("SearchApiHighMemory", 15, "warning")],
        "staging_noise": [("StagingDiskFull", 15, "critical")],
        "slow_batch": [("ReportJobSlow", 35, "critical")],
    }[fault_name]
    lines = ["groups:", f"  - name: benchmark-{fault_name}", "    rules:"]
    for alert, at, sev in names:
        t = at + uptime_offset_s
        lines += [
            f"      - alert: {alert}",
            "        expr: time() - max(process_start_time_seconds{job=\"prometheus\"})"
            f" > {t}",
            f"        labels: {{severity: {sev}}}",
        ]
    return "\n".join(lines) + "\n"
