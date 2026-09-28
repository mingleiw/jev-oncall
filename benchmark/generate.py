#!/usr/bin/env python3
"""Generate a labeled benchmark dataset via fault injection.

    python3 generate.py --count 100 --seed 42 --out dataset.jsonl
    python3 generate.py --count 20 --seed 7 --noise 0.0 --out clean.jsonl

Each line of the output is one Incident as JSON (see schema.py). The dataset
is fully determined by --seed: same seed, same incidents, byte for byte.

--noise 0.0 disables the noise layer (clean captures); 1.0 is the default
production-like mess of duplicates, flapping, and unrelated warnings.
"""
from __future__ import annotations

import argparse
import itertools
import random
import sys
from datetime import datetime, timedelta, timezone

from schema import incident_to_json, validate_alert, validate_ground_truth
from faults import FAULTS, FAULT_WEIGHTS
from noise import apply_noise

T0 = datetime(2026, 9, 27, 14, 0, 0, tzinfo=timezone.utc)


def generate(count: int, seed: int, noise: float = 1.0) -> list[str]:
    # Two independent streams: the fault sequence must not change when the
    # noise level changes, so clean and noisy datasets share incidents.
    # Alert ids are per-incident namespaces, so a clean alert keeps its id
    # under any noise level.
    fault_rng = random.Random(seed)
    noise_rng = random.Random(f"{seed}:noise")
    names = list(FAULTS)
    lines = []
    for i in range(count):
        fault_name = fault_rng.choices(names, weights=FAULT_WEIGHTS, k=1)[0]
        incident_id = f"inc-{i + 1:04d}"
        t0 = T0 + timedelta(hours=i * 3)  # incidents don't overlap in time
        incident = FAULTS[fault_name](fault_rng, incident_id,
                                      itertools.count(1), t0)
        incident = apply_noise(incident, noise_rng, itertools.count(1),
                               level=noise)

        # Invariants: schema-valid, and every duplicate_of resolves.
        alert_ids = {a.id for a in incident.alerts}
        errs = [e for a in incident.alerts for e in validate_alert(a)]
        errs += [e for gid, g in incident.ground_truth.items()
                 for e in validate_ground_truth(gid, g, alert_ids)]
        if len(incident.ground_truth) != len(alert_ids):
            errs.append(f"{incident_id}: ground truth covers "
                        f"{len(incident.ground_truth)} of {len(alert_ids)} alerts")
        if errs:
            raise RuntimeError("generated invalid incident:\n" + "\n".join(errs))
        lines.append(incident_to_json(incident))
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--noise", type=float, default=1.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    lines = generate(args.count, args.seed, args.noise)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    import json as _json
    n_alerts = sum(len(_json.loads(l)["alerts"]) for l in lines)
    print(f"wrote {len(lines)} incidents ({n_alerts} alerts) to {args.out} "
          f"(seed={args.seed}, noise={args.noise})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
