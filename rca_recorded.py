#!/usr/bin/env python3
"""Turn recorded incidents into RCA benchmark scenarios, with generated suspects and checks.

The frozen scenarios in rca_scenarios/ were written by hand: their suspects, check menu
and check results are prose. This adapter builds the same scenario and truth files from
recorded telemetry instead, so every setup (an LLM alone, an LLM + Jev, Jev
investigating) runs on them unchanged through rca_experiment.py --scenario-dir.

Source: RCAEval (https://github.com/phamquiluan/RCAEval, MIT), whose cases are faults
injected into microservice demo systems with metrics recorded around them. Each case
directory holds metrics.json (or metrics.parquet) and inject_time.txt, and is named
{benchmark}_{service}_{fault}_{instance}.

What is generated, and from what:

  incident      the alert: the front-end service's latency before and after the alert time
  suspects      one hypothesis per service found in the metric names
  checks        per kind of metric (cpu, memory, disk, sockets, traffic, latency, errors):
                one overview across all services, and one detail check per service
  results       computed by code from the recorded series: mean and max before and
                after the alert time, the ratio, and when the series first left its
                normal range. Nothing is written by hand.

The label (the root-cause service and the fault) is read from the case directory name
and goes only into the truth file. The scenario gets an opaque id; a test checks that
neither the directory name nor the fault reaches it. The alert time is the injection
time, as in RCAEval's own baselines: that reveals when, not where.

Usage:
    python3 rca_recorded.py build --data data/ --out rca_recorded/re2-ob --cases 're2ob_*_1'
    python3 rca_experiment.py --scenario-dir rca_recorded/re2-ob --setup jev-agent --max-checks 8

guide/rca-recorded.md has the design, the pre-registered split and the commands.
Standard library only, except that reading a metrics.parquet case needs pandas.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import math
import os
import statistics
import sys
from datetime import datetime, timezone

WINDOW_S = 300  # seconds of "before" and of "after" around the alert time
SUSTAIN = 10    # consecutive points outside the normal range before a change counts
REL_MIN = 0.10  # a change must also be at least 10% of the normal level
MAX_SUSPECTS = 12  # suspects kept per case: the services whose metrics moved most, plus the front end
KIND_ORDER = ("latency", "errors", "cpu", "memory", "disk", "sockets", "traffic")
KIND_TEXT = {"latency": "request latency", "errors": "error rate", "cpu": "CPU usage",
             "memory": "memory usage", "disk": "disk I/O", "sockets": "open sockets",
             "traffic": "request rate"}
# Which kinds of check show each injected fault. Used for the truth file only.
FAULT_KINDS = {"cpu": ["cpu"], "mem": ["memory"], "disk": ["disk"], "delay": ["latency"],
               "loss": ["latency", "errors"], "socket": ["sockets"]}
FAULT_WORDS = {"cpu": [["cpu", "processor", "compute"]],
               "mem": [["memory", "mem", "oom", "heap"]],
               "disk": [["disk", "i/o", "io "]],
               "delay": [["delay", "latency", "slow"]],
               "loss": [["loss", "packet", "drop"]],
               "socket": [["socket", "connection"]]}
FRONT_NAMES = ("frontend", "front-end", "ui-dashboard", "gateway")
GUIDANCE = ("Check results are computed from recorded metrics. 'Before' is the window just before "
            "the alert time and 'after' the window just after it. A metric can move because its "
            "service is at fault or because a service it depends on is. A check that returns no "
            "series means the metric was not recorded, not that nothing happened.")


def kind_of(suffix):
    s = suffix.lower()
    if s.startswith("lat"):
        return "latency"
    if s.startswith("err"):
        return "errors"
    if s.startswith("cpu"):
        return "cpu"
    if s.startswith("mem"):
        return "memory"
    if s.startswith("disk"):
        return "disk"
    if s.startswith("socket"):
        return "sockets"
    if s.startswith("workload") or s.startswith("traffic") or s.startswith("qps"):
        return "traffic"
    return None


def split_metric(name, services=None):
    """'cartservice_cpu' -> ('cartservice', 'cpu'); None for 'time' or unknown kinds."""
    if name == "time" or "_" not in name:
        return None
    service, _, suffix = name.partition("_")
    kind = kind_of(suffix)
    return (service, kind) if kind else None


def read_series(case_dir):
    """{metric name: [(t, value), ...]} sorted by time, from metrics.json or .parquet."""
    path = os.path.join(case_dir, "metrics.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        out = {}
        for name, points in raw.items():
            pts = sorted((float(t), float(v)) for t, v in points or [] if v is not None)
            if pts:
                out[name] = pts
        return out
    path = os.path.join(case_dir, "metrics.parquet")
    if os.path.exists(path):
        import pandas as pd  # only needed for the Hugging Face copy
        df = pd.read_parquet(path)
        out = {}
        for col in df.columns:
            if col == "time":
                continue
            s = df[["time", col]].dropna()
            if len(s):
                out[col] = [(float(t), float(v)) for t, v in s.itertuples(index=False)]
        return out
    raise FileNotFoundError(f"no metrics.json or metrics.parquet in {case_dir}")


def read_inject_time(case_dir):
    with open(os.path.join(case_dir, "inject_time.txt"), encoding="utf-8") as f:
        return float(f.read().strip().split()[0])


def label_of(case_name):
    """{benchmark}_{service}_{fault}_{instance} -> (benchmark, service, fault, instance).
    Services may contain underscores in principle, so the service is everything between
    the first and the last two fields."""
    parts = case_name.split("_")
    if len(parts) < 4:
        raise ValueError(f"case name {case_name!r} is not benchmark_service_fault_instance")
    return parts[0], "_".join(parts[1:-2]), parts[-2], parts[-1]


# --------------------------------------------------------------------------
# Statistics, all in code

def window(points, t0, t1):
    return [v for t, v in points if t0 <= t < t1]


def stats(points, alert_t, width=WINDOW_S):
    before, after = window(points, alert_t - width, alert_t), window(points, alert_t, alert_t + width)
    if len(before) < 3 or len(after) < 3:
        return None
    bm, am = statistics.fmean(before), statistics.fmean(after)
    sd = statistics.pstdev(before)
    # Outside the normal range: beyond 3 standard deviations AND at least REL_MIN of the
    # normal level, so a nearly flat series does not flag a tiny move.
    band = max(3 * sd, REL_MIN * abs(bm))
    hi, lo = bm + band, bm - band
    # Onset: the first point of a run of SUSTAIN consecutive points outside the normal
    # range, so one noisy sample does not count as a change.
    after_pts = [(t, v) for t, v in points if alert_t <= t < alert_t + width]
    outside = [(v > hi or v < lo) and abs(v - bm) > 1e-9 for _, v in after_pts]
    onset = next((after_pts[i][0] - alert_t for i in range(len(after_pts) - SUSTAIN + 1)
                  if all(outside[i:i + SUSTAIN])), None)
    return {"before_mean": bm, "before_max": max(before), "after_mean": am, "after_max": max(after),
            "ratio": (am / bm) if abs(bm) > 1e-9 else (math.inf if abs(am) > 1e-9 else 1.0),
            "onset_s": onset}


def cap(text):
    return text[0].upper() + text[1:]


def fmt(v, kind):
    if kind == "memory" and abs(v) >= 1e5:
        return f"{v / 2**20:.0f} MiB"
    if abs(v) >= 100:
        return f"{v:.0f}"
    if abs(v) >= 1:
        return f"{v:.2f}"
    return f"{v:.3g}"


def ratio_text(r):
    if math.isinf(r):
        return "from zero"
    return f"{r:.2f}x"


def detail_text(service, kind, metric, st):
    if st is None:
        return f"0 matching series: {metric} has too few points around the alert time."
    moved = (f"First left its normal range {st['onset_s']:.0f}s after the alert time."
             if st["onset_s"] is not None else "Stayed within its normal range.")
    return (f"{service} {KIND_TEXT[kind]} ({metric}). Before: mean {fmt(st['before_mean'], kind)}, "
            f"max {fmt(st['before_max'], kind)}. After: mean {fmt(st['after_mean'], kind)}, "
            f"max {fmt(st['after_max'], kind)} ({ratio_text(st['ratio'])} the before mean). {moved}")


def overview_text(kind, rows):
    lines = [f"{cap(KIND_TEXT[kind])} by service, before -> after the alert time (mean):"]
    for service, st in rows:
        if st is None:
            lines.append(f"- {service}: too few points")
        else:
            onset = f", left normal range at +{st['onset_s']:.0f}s" if st["onset_s"] is not None else ""
            lines.append(f"- {service}: {fmt(st['before_mean'], kind)} -> {fmt(st['after_mean'], kind)} "
                         f"({ratio_text(st['ratio'])}{onset})")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# One case

def pick_metrics(series):
    """{(service, kind): metric name}, choosing the highest percentile for latency."""
    chosen = {}
    for name in sorted(series):
        sk = split_metric(name)
        if not sk:
            continue
        prev = chosen.get(sk)
        if prev is None or ("90" in name and "90" not in prev) or ("99" in name and "99" not in prev):
            chosen[sk] = name
    return chosen


def movement(stats_by_kind):
    """How much a service's metrics moved at the alert: the largest |log ratio|, with a
    sustained change (an onset) counting at least as a doubling. No label involved."""
    best = 0.0
    for st in stats_by_kind:
        if not st:
            continue
        r = st["ratio"]
        score = 10.0 if math.isinf(r) else abs(math.log(max(r, 1e-9)))
        if st["onset_s"] is not None:
            score = max(score, math.log(2))
        best = max(best, score)
    return best


def build_case(case_dir, id_prefix="rec", width=WINDOW_S, max_suspects=MAX_SUSPECTS):
    """(id, scenario, truth) for one recorded case."""
    case = os.path.basename(os.path.normpath(case_dir))
    benchmark, root, fault, instance = label_of(case)
    series = read_series(case_dir)
    alert_t = read_inject_time(case_dir)
    chosen = pick_metrics(series)
    all_services = sorted({s for s, _ in chosen})
    if root not in all_services:
        raise ValueError(f"{case}: root-cause service {root!r} is not among the metric services")
    st = {sk: stats(series[m], alert_t, width) for sk, m in chosen.items()}
    kinds = [k for k in KIND_ORDER if any(kk == k for _, kk in chosen)]
    # Suspects: like a real tool, narrow a large system to the services that moved most.
    # This uses only the telemetry, never the label; whether the cause survives is
    # recorded in the truth file as suspect recall.
    front = next((s for s in all_services for n in FRONT_NAMES if n in s and (s, "latency") in chosen), None)
    ranked = sorted(all_services, key=lambda s: (-movement([st.get((s, k)) for k in kinds]), s))
    keep = set(ranked[:max_suspects]) | ({front} if front else set())
    services = sorted(keep)

    checks = {}
    for kind in kinds:
        rows = [(s, st.get((s, kind))) for s in all_services if (s, kind) in chosen]
        checks[f"{kind}_by_service"] = {
            "description": f"{cap(KIND_TEXT[kind])} of every service, before and after the alert time.",
            "result": overview_text(kind, rows)}
    for s in services:
        for kind in kinds:
            if (s, kind) in chosen:
                checks[f"{s}_{kind}"] = {
                    "description": f"{cap(KIND_TEXT[kind])} of {s} in detail, before and after the alert time.",
                    "result": detail_text(s, kind, chosen[(s, kind)], st[(s, kind)])}

    fst = st.get((front, "latency")) if front else None
    when = datetime.fromtimestamp(alert_t, timezone.utc)
    if fst:
        alert = (f"Alert at {when:%H:%M:%S} UTC on the user-facing service {front}. Its request latency: "
                 f"mean {fmt(fst['before_mean'], 'latency')} before, {fmt(fst['after_mean'], 'latency')} "
                 f"after ({ratio_text(fst['ratio'])}).")
    else:
        alert = f"User-facing latency and errors alerted at {when:%H:%M:%S} UTC."
    digest = hashlib.sha256(case.encode()).hexdigest()[:10]
    system = benchmark[-2:] if len(benchmark) >= 2 else benchmark
    scenario = {
        "_origin": "Generated by rca_recorded.py from recorded telemetry (RCAEval, MIT).",
        "incident": {"title": "UserFacingLatency", "description": alert,
                     "started_at": when.isoformat(timespec="seconds").replace("+00:00", "Z")},
        "initial_context": (f"Recorded telemetry from a microservice system with {len(all_services)} services"
                            + (f"; the {len(services)} suspects are the services whose metrics moved most at "
                               "the alert, plus the user-facing one" if len(services) < len(all_services) else "")
                            + ". No deploys or configuration changes are recorded for this window. Checks "
                            f"compare the {width // 60} minutes before the alert time with the "
                            f"{width // 60} minutes after it. Overview checks cover every service."),
        "hypotheses": {s: f"The fault is in {s}: a resource, network or process problem in {s} itself."
                       for s in services},
        "checks": checks,
        "version_check": None,
        "guidance": GUIDANCE,
    }
    mech = [c for k in FAULT_KINDS.get(fault, []) for c in (f"{root}_{k}", f"{k}_by_service") if c in checks]
    # The loudest wrong suspect: the other service whose latency (or else any metric) moved most.
    def loudness(s):
        vals = [st.get((s, k)) for k in ("latency", "errors", "cpu")]
        return max((abs(math.log(max(v["ratio"], 1e-9))) if v and not math.isinf(v["ratio"]) else 0.0)
                   for v in vals)
    others = [s for s in services if s != root] or [s for s in all_services if s != root]
    truth = {
        "_source": f"RCAEval case {case}",
        "case": case, "benchmark": benchmark, "instance": instance, "fault": fault,
        "hypothesis": root, "component": root, "component_aliases": [root],
        "root_in_suspects": root in services, "n_services": len(all_services), "n_suspects": len(services),
        "mechanism": f"{fault} fault injected into {root}",
        "mechanism_checks": mech,
        "mechanism_keywords": FAULT_WORDS.get(fault, [[fault]]),
        "decoy": max(others, key=loudness) if others else root,
        "version_check": None, "traps": [c for c, v in checks.items() if v["result"].startswith("0 matching")],
        "noise": [], "check_notes": {},
    }
    return f"{id_prefix}_{system}_{digest}", scenario, truth


def build(data_dir, out_dir, pattern="*", id_prefix="rec", width=WINDOW_S, max_suspects=MAX_SUSPECTS):
    """Build every case under data_dir whose directory name matches pattern."""
    os.makedirs(out_dir, exist_ok=True)
    built, skipped = [], []
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        if not os.path.isdir(path) or not fnmatch.fnmatch(name, pattern):
            continue
        try:
            sid, scenario, truth = build_case(path, id_prefix, width, max_suspects)
        except (ValueError, FileNotFoundError, OSError) as e:
            skipped.append((name, str(e)))
            continue
        with open(os.path.join(out_dir, f"{sid}.json"), "w", encoding="utf-8") as f:
            json.dump(scenario, f, indent=2)
        with open(os.path.join(out_dir, f"{sid}.truth.json"), "w", encoding="utf-8") as f:
            json.dump(truth, f, indent=2)
        built.append(sid)
    return built, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build RCA scenarios from recorded incidents (RCAEval).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="convert case directories into scenario and truth files")
    b.add_argument("--data", required=True, help="directory holding RCAEval case directories")
    b.add_argument("--out", required=True, help="output scenario directory")
    b.add_argument("--cases", default="*", help="glob on case directory names, e.g. 're2ob_*_1'")
    b.add_argument("--window", type=int, default=WINDOW_S, help="seconds before and after the alert time")
    b.add_argument("--max-suspects", type=int, default=MAX_SUSPECTS,
                   help="suspects kept per case (the services that moved most, plus the front end)")
    args = ap.parse_args(argv)
    built, skipped = build(args.data, args.out, args.cases, width=args.window, max_suspects=args.max_suspects)
    recall = []
    for sid in built:
        with open(os.path.join(args.out, f"{sid}.truth.json"), encoding="utf-8") as f:
            recall.append(json.load(f)["root_in_suspects"])
    print(f"built {len(built)} scenarios in {args.out}; the cause is among the suspects in "
          f"{sum(recall)}/{len(recall)}")
    for name, why in skipped:
        print(f"skipped {name}: {why}", file=sys.stderr)
    return 0 if built else 1


if __name__ == "__main__":
    sys.exit(main())
