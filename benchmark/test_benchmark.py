#!/usr/bin/env python3
"""Tests for the benchmark package. Offline, deterministic, no API calls.

Run from this directory:  python3 test_benchmark.py
"""
from __future__ import annotations

import sys

from schema import incident_from_json
from generate import generate
from faults import FAULTS, rules_fragment
from harness import aggregate, brier_ece, score_incident
from models import RandomModel, SeverityBaseline, to_model_outputs
from dataclasses import asdict


def _incidents(count=30, seed=42, noise=1.0):
    return [incident_from_json(l) for l in generate(count, seed, noise)]


def test_determinism():
    a = generate(50, seed=123, noise=1.0)
    b = generate(50, seed=123, noise=1.0)
    assert a == b, "same seed must produce byte-identical datasets"
    c = generate(50, seed=124, noise=1.0)
    assert a != c, "different seeds must differ"


def test_ground_truth_invariants():
    for inc in _incidents(60, seed=7):
        ids = {a.id for a in inc.alerts}
        assert set(inc.ground_truth) == ids, f"{inc.incident_id}: label coverage"
        roots = [aid for aid, g in inc.ground_truth.items()
                 if g.duplicate_of is None]
        assert roots, f"{inc.incident_id}: no root alert"
        root_team = inc.ground_truth[roots[0]].team
        for aid, g in inc.ground_truth.items():
            assert g.duplicate_of in ids or g.duplicate_of is None
            assert g.duplicate_of != aid
            if g.duplicate_of is not None:
                # symptoms/duplicates belong to the root's team: the correct
                # behavior is linking, not paging a second team
                rg = inc.ground_truth[g.duplicate_of]
                assert g.team == rg.team, f"{aid}: symptom team != root team"
            if not g.actionable:
                assert g.severity == "SEV4", f"{aid}: noise must be SEV4"


def test_noise_adds_but_never_relabels():
    clean = _incidents(20, seed=11, noise=0.0)
    noisy = _incidents(20, seed=11, noise=1.0)
    for c, n in zip(clean, noisy):
        assert c.fault == n.fault
        assert len(n.alerts) >= len(c.alerts), "noise only adds alerts"
        # every clean alert keeps its id and ground truth
        ngt = n.ground_truth
        for a in c.alerts:
            assert a.id in ngt, f"clean alert {a.id} lost under noise"
            cg = c.ground_truth[a.id]
            assert ngt[a.id].severity == cg.severity
            assert ngt[a.id].actionable == cg.actionable


def test_baseline_owner_perfect_link_zero():
    incs = _incidents(20, seed=5)
    model = SeverityBaseline()
    per, cal = [], []
    for inc in incs:
        outs = to_model_outputs(model.judge([asdict(a) for a in inc.alerts]))
        m = score_incident(inc, outs)
        cal.extend(m.pop("cal"))
        per.append(m)
    agg = aggregate(per)
    # baseline routes by service->team, which is exactly how roots are labeled
    assert agg["owner_acc"] == 1.0, agg
    # ...but it never links symptoms to their root
    assert agg["link_acc"] == 0.0, agg
    # and it pages staging-noise / slow_batch traps (configured critical)
    assert agg["false_page"] > 0, "baseline should fall for critical noise"


def test_random_model_runs():
    incs = _incidents(5, seed=9)
    for inc in incs:
        outs = to_model_outputs(RandomModel(seed=1).judge(
            [asdict(a) for a in inc.alerts]))
        assert len(outs) == len(inc.alerts)
    # seeded random is deterministic
    r1 = RandomModel(seed=1).judge([asdict(a) for a in incs[0].alerts])
    r2 = RandomModel(seed=1).judge([asdict(a) for a in incs[0].alerts])
    assert r1 == r2


def test_calibration_math():
    brier, ece = brier_ece([(1.0, 1), (0.0, 0)])
    assert brier == 0.0 and ece == 0.0
    brier, ece = brier_ece([(1.0, 0), (0.0, 1)])
    assert brier == 1.0 and ece == 1.0


def test_rules_fragments_render():
    for name in FAULTS:
        frag = rules_fragment(name)
        assert "groups:" in frag and "alert:" in frag, name


def main() -> int:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
