#!/usr/bin/env python3
"""Generate synthetic alerts for latency benchmarking.

These alerts carry no `expected` labels: they measure how fast triage runs,
not how well it judges. Accuracy and calibration need real labeled history.

    python3 generate_alerts.py --count 300 --out bench_alerts.json
    python3 triage.py --alerts bench_alerts.json --out bench_results.json
"""
import argparse
import json
import random
from datetime import datetime, timedelta, timezone

SERVICES = [
    ("checkout-api", "deploy"), ("payment-service", "deploy"),
    ("search-api", "compute"), ("orders-db", "database"),
    ("session-store", "database"), ("billing-events", "compute"),
    ("edge", "network"), ("homepage", "network"), ("api-edge", "network"),
    ("log-aggregator", "compute"), ("notification-svc", "compute"),
    ("inventory-api", "deploy"), ("auth-service", "deploy"),
    ("metrics-store", "database"), ("cdn-origin", "network"),
]

ROOT_TEMPLATES = [
    ("error rate {pct}% for {mins}m ({region})",
     "5xx rate on {service} climbed from 0.1% to {pct}% starting {hhmm} UTC, "
     "{mins} minutes after deploy v{ver} rolled to {region}.", "critical"),
    ("all {region} instances returning 503",
     "Full outage in {region} since {hhmm} UTC, correlating with the config "
     "push at {hhmm} UTC. Upstream healthy.", "critical"),
    ("p99 latency above {secs}s for {mins}m",
     "p99 latency on {service} has exceeded the 800ms SLO for {mins} minutes, "
     "currently {secs}s. Error rate normal, traffic normal.", "critical"),
    ("replica lag {secs}s and growing after failover",
     "Automatic failover completed at {hhmm} UTC. Replica lag is {secs}s and "
     "growing. Read-your-write complaints starting in the support queue.", "critical"),
    ("connection pool exhausted ({region})",
     "All {pct} connections in the {service} pool are checked out since "
     "{hhmm} UTC. New requests queueing past the 5s timeout.", "critical"),
]

DOWNSTREAM_TEMPLATES = [
    ("5xx rate {pct}% ({region})",
     "{service} returning 503s at {pct}% since {hhmm} UTC. Upstream dependency "
     "{upstream} is in full outage since {hhmm_up} UTC.", "critical"),
    ("health check failing from {n} regions",
     "Synthetic checks for {service} failing from {n} regions since {hhmm} UTC. "
     "Upstream {upstream} degraded since {hhmm_up} UTC.", "critical"),
    ("queue depth above {pct}k",
     "Queue depth on {service} exceeded {pct}k and is growing. Downstream of "
     "{upstream}, which has been degraded since {hhmm_up} UTC.", "critical"),
]

NOISE_TEMPLATES = [
    ("disk usage {pct}% on {service}-0{n}",
     "Disk usage at {pct}%, growing ~2%/day for the past week. At current rate "
     "fills in ~{mins} days.", "warning"),
    ("TLS cert expires in {n} days",
     "Public certificate for {service} expires in {n} days. Auto-renewal job "
     "last succeeded 60 days ago.", "warning"),
    ("nightly batch took {mins}m (usual {secs}m)",
     "The {service} batch job completed successfully in {mins} minutes vs a "
     "{secs}-minute trailing average. No errors, output validated.", "info"),
    ("canary analysis paused in {region}",
     "Automated canary analysis for v{ver} paused: insufficient traffic in "
     "{region} to reach significance. No errors detected.", "warning"),
    ("CPU above 90% for {mins}m",
     "CPU on {service} above 90% for {mins} minutes. Load test left running "
     "by its owner.", "critical"),
    ("memory usage {pct}% on {service}",
     "Resident memory at {pct}% of limit, flat for the past 6 hours. No OOM "
     "kills recorded.", "warning"),
]

REGIONS = ["us-west-2", "us-east-1", "eu-west-1", "ap-southeast-2"]
ENVS = ["prod"] * 8 + ["staging", "dev"]


def _fill(template, rng, service, base, upstream=None, up_time=None):
    title_t, desc_t, sev = template
    fields = {
        "service": service,
        "pct": rng.choice([61, 72, 82, 91, 94, 98, 100, 200, 500]),
        "mins": rng.choice([2, 5, 6, 9, 12, 15, 25, 42]),
        "secs": rng.choice([2, 3, 8, 35, 45, 90]),
        "n": rng.choice([3, 5, 7, 13, 30]),
        "region": rng.choice(REGIONS),
        "ver": f"2.{rng.randint(10, 20)}.{rng.randint(0, 9)}",
        "hhmm": base.strftime("%H:%M"),
        "upstream": upstream or "",
        "hhmm_up": up_time.strftime("%H:%M") if up_time else "",
    }
    return title_t.format(**fields), desc_t.format(**fields), sev


def generate(count, seed):
    rng = random.Random(seed)
    start = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)
    alerts = []
    n = 0

    while len(alerts) < count:
        n += 1
        service, _ = rng.choice(SERVICES)
        at = start + timedelta(minutes=rng.randint(0, 480))
        env = rng.choice(ENVS)

        # Roughly a third of alerts arrive as an incident cluster: one root
        # cause plus downstream alerts a few minutes later.
        if rng.random() < 0.33 and len(alerts) + 3 <= count:
            title, desc, sev = _fill(rng.choice(ROOT_TEMPLATES), rng, service, at)
            root_id = f"b{n:04d}"
            alerts.append({
                "id": root_id,
                "title": f"Firing: {service} {title}",
                "description": desc,
                "service": service, "env": env,
                "started_at": at.isoformat().replace("+00:00", "Z"),
                "configured_severity": sev,
            })
            for _ in range(rng.randint(1, 3)):
                if len(alerts) >= count:
                    break
                n += 1
                down_svc, _ = rng.choice(SERVICES)
                down_at = at + timedelta(minutes=rng.randint(1, 8))
                title, desc, sev = _fill(rng.choice(DOWNSTREAM_TEMPLATES), rng,
                                         down_svc, down_at, service, at)
                alerts.append({
                    "id": f"b{n:04d}",
                    "title": f"Firing: {down_svc} {title}",
                    "description": desc,
                    "service": down_svc, "env": env,
                    "started_at": down_at.isoformat().replace("+00:00", "Z"),
                    "configured_severity": sev,
                })
        else:
            title, desc, sev = _fill(rng.choice(NOISE_TEMPLATES), rng, service, at)
            prefix = {"critical": "Firing", "warning": "Warning", "info": "Info"}[sev]
            alerts.append({
                "id": f"b{n:04d}",
                "title": f"{prefix}: {service} {title}",
                "description": desc,
                "service": service, "env": env,
                "started_at": at.isoformat().replace("+00:00", "Z"),
                "configured_severity": sev,
            })

    return alerts[:count]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=300)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="bench_alerts.json")
    args = ap.parse_args(argv)

    alerts = generate(args.count, args.seed)
    with open(args.out, "w") as f:
        json.dump(alerts, f, indent=2)
    clusters = sum(1 for a in alerts if "Upstream dependency" in a["description"]
                   or "Upstream" in a["description"])
    print(f"{len(alerts)} alerts -> {args.out} "
          f"({clusters} downstream of another alert, seed {args.seed})")


if __name__ == "__main__":
    main()
