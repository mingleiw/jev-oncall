#!/usr/bin/env python3
"""Offline evaluation of triage.py results. Makes no API calls.

    python3 evaluate.py [results.json] [--alerts FILE] [--sweep]

For labeled alerts it prints:
  outcomes     the errors that matter on-call, next to the baseline: your
               current routing, by configured severity only (critical
               pages, warning tickets, info logs, in every environment).
               Shadow mode compares against the same baseline.
  agreement    per-question agreement with Wilson 95% intervals
  calibration  Brier score, ECE and a reliability table for P(page) and
               P(actionable); the thresholds are only as good as these
  --sweep      re-routes the stored Jev answers under other thresholds

The 14 synthetic alerts are a smoke test. To pick thresholds you can trust,
replay a few hundred historical alerts labeled with their post-incident SEV.

Labels live under "expected" in each alert:
    {"actionable": bool, "severity": "SEV1".."SEV4", "team": str,
     "duplicate_of": alert id or null}
"""
from __future__ import annotations

import argparse
import dataclasses
import itertools
import math
import os
import sys

import triage

ORDER = ["none", "ticket", "review", "page"]  # how urgently a human was reached
REACH = {"DROP": "none", "LOG": "none", "TICKET": "ticket", "REVIEW": "review",
         "PAGE": "page", "PAGE_NOW": "page"}
OUTCOMES = [
    ("silent_miss", "silent misses: needed a human, reached none"),
    ("missed_page", "missed pages: ticketed instead"),
    ("delayed_page", "delayed pages: REVIEW (ack window)"),
    ("false_page", "false pages"),
    ("duplicate_page", "duplicate pages"),
    ("misrouted", "page or review to the wrong team"),
    ("wrong_link", "wrong incident links"),
    ("extra_review", "reviews that weren't needed"),
    ("extra_ticket", "tickets for noise"),
]
NOT_APPLICABLE_TO_BASELINE = ("misrouted", "wrong_link")
MIN_CALIBRATION_N = 100


def wilson(k, n, z=1.96):
    """Wilson score interval for k successes out of n."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def labels(alert):
    e = alert.get("expected")
    if not e:
        return None
    cause = e.get("duplicate_of")
    if cause is None and e.get("duplicate"):  # v1 labels: a bool without the parent
        cause = "?"
    return {"actionable": bool(e.get("actionable", True)), "severity": e.get("severity"),
            "team": e.get("team"), "duplicate_of": cause}


def expected_reach(lab):
    if not lab["actionable"]:
        return "none"
    return "page" if lab["severity"] in ("SEV1", "SEV2") else "ticket"


def wants_page(lab):
    return lab["actionable"] and lab["severity"] in ("SEV1", "SEV2")


def _root(aid, decisions):
    return decisions[aid].linked_to or aid


def same_incident(aid, lab, decisions):
    """Is this linked alert in the same cluster as its labeled cause?"""
    cause = lab["duplicate_of"]
    if cause is None:
        return False  # labeled independent, yet linked
    if cause == "?":
        return True
    return cause in decisions and _root(cause, decisions) == _root(aid, decisions)


def classify(aid, lab, decisions):
    """Tag one alert's outcome. Under-reach is judged on what reached the
    labeled owner (directly or through the incident root); over-reach only on
    what this alert itself sent."""
    d = decisions[aid]
    expected = expected_reach(lab)
    own = REACH.get(d.action)  # None for DEDUP: the alert sent nothing itself
    got = own or "none"
    if d.linked_to:
        root = decisions[d.linked_to]
        if lab["team"] in (None, root.team):
            got = max(got, REACH[root.action], key=ORDER.index)
    tags = []
    if expected != "none" and got == "none":
        tags.append("silent_miss")
    elif expected == "page" and got == "ticket":
        tags.append("missed_page")
    elif expected == "page" and got == "review":
        tags.append("delayed_page")
    if own == "page" and expected != "page":
        tags.append("false_page")
    if own == "review" and expected != "page":
        tags.append("extra_review")
    if own == "ticket" and expected == "none":
        tags.append("extra_ticket")
    if lab["duplicate_of"] and own == "page" and not d.linked_to:
        tags.append("duplicate_page")
    if (own in ("page", "review") and d.source == "jev" and lab["team"]
            and lab["team"] != d.team and lab["team"] not in d.notify):
        tags.append("misrouted")
    if d.linked_to and not same_incident(aid, lab, decisions):
        tags.append("wrong_link")
    return tags


def count_outcomes(labeled, decisions):
    counts = dict.fromkeys((key for key, _ in OUTCOMES), 0)
    for a in labeled:
        for tag in classify(a["id"], labels(a), decisions):
            counts[tag] += 1
    counts["pages"] = sum(d.action in triage.PAGING for d in decisions.values())
    counts["reviews"] = sum(d.action == "REVIEW" for d in decisions.values())
    return counts


def agreement(labeled, records):
    tallies = {q: [0, 0] for q in ("actionable", "page", "severity", "team", "duplicate_of")}
    never_offered = 0
    for a in labeled:
        lab, rec = labels(a), records.get(a["id"])
        if not rec or not rec.get("judgment"):
            continue
        j = triage.Judgment(**rec["judgment"])
        checks = {
            "actionable": (j.p_actionable >= 0.5) == lab["actionable"],
            "page": (j.p_page >= 0.5) == wants_page(lab),
            "severity": triage.top(j.severity) == lab["severity"],
            "team": triage.top(j.team) == lab["team"],
        }
        cause = lab["duplicate_of"]
        if j.duplicate_of:
            if cause not in (None, "?") and cause not in j.duplicate_of:
                never_offered += 1
            pick = triage.top(j.duplicate_of)
            checks["duplicate_of"] = (pick != triage.NONE if cause == "?"
                                      else pick == (cause or triage.NONE))
        elif cause:
            never_offered += 1
        for q, ok in checks.items():
            tallies[q][0] += int(ok)
            tallies[q][1] += 1
    return tallies, never_offered


def calibration(pairs, bins):
    """Brier score, expected calibration error, and a reliability table."""
    n = len(pairs)
    brier = sum((p - y) ** 2 for p, y in pairs) / n
    ece, table = 0.0, []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        in_bin = [(p, y) for p, y in pairs if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if not in_bin:
            continue
        mean_p = sum(p for p, _ in in_bin) / len(in_bin)
        rate = sum(y for _, y in in_bin) / len(in_bin)
        ece += len(in_bin) / n * abs(mean_p - rate)
        table.append((lo, hi, len(in_bin), mean_p, rate))
    return brier, ece, table


def stored_decisions(results):
    names = [f.name for f in dataclasses.fields(triage.Decision)]
    return {r["id"]: triage.Decision(**{n: r[n] for n in names}) for r in results["alerts"]}


def reroute(results, alerts, policy):
    """Re-apply the routing policy to the stored Jev answers. No API calls."""
    judgments = {r["id"]: triage.Judgment(**r["judgment"])
                 for r in results["alerts"] if r.get("judgment")}
    errors = {r["id"]: r["error"] for r in results["alerts"] if r.get("error")}
    return triage.route_all(alerts, judgments, errors, policy)


def baseline_decisions(alerts):
    """Your current routing: each alert by its configured severity alone, the
    same baseline shadow mode logs. jev-oncall's own rules (non-production
    never pages) are not applied: they are part of what's being compared."""
    return {a["id"]: triage.static_route(a, "baseline") for a in alerts}


def compute_report(results, alerts):
    """Everything the evaluation shows, as data. print_report prints it and
    generate_dashboard.py renders it, so both always agree."""
    records = {r["id"]: r for r in results["alerts"]}
    policy = triage.Policy(**results["meta"]["policy"])
    decisions = stored_decisions(results)
    labeled = [a for a in alerts if labels(a) and a["id"] in decisions]
    report = {"n": len(labeled), "decisions": decisions, "tags": {}}
    if not labeled:
        return report

    rerouted = reroute(results, alerts, policy)
    report["drift"] = sorted(i for i in decisions if i in rerouted and (
        rerouted[i].action, rerouted[i].linked_to) != (decisions[i].action, decisions[i].linked_to))
    baseline = baseline_decisions(alerts)
    report["ours"] = count_outcomes(labeled, decisions)
    report["theirs"] = count_outcomes(labeled, baseline)
    report["tags"] = {a["id"]: classify(a["id"], labels(a), decisions) for a in labeled}
    report["tallies"], report["never_offered"] = agreement(labeled, records)

    pairs = {"P(page)": [], "P(actionable)": []}
    for a in labeled:
        rec = records.get(a["id"])
        if rec and rec.get("judgment"):
            j, lab = triage.Judgment(**rec["judgment"]), labels(a)
            pairs["P(page)"].append((j.p_page, int(wants_page(lab))))
            pairs["P(actionable)"].append((j.p_actionable, int(lab["actionable"])))
    report["calibration_n"] = len(pairs["P(page)"])
    report["calibration"] = {
        name: calibration(p, 5 if len(p) < 200 else 10) for name, p in pairs.items() if p}
    return report


def print_report(results, alerts):
    report = compute_report(results, alerts)
    n = report["n"]
    if not n:
        print("No labeled alerts: nothing to evaluate.")
        return
    if report["drift"]:
        print(f"WARNING: re-routing the stored answers changes {', '.join(report['drift'])}; "
              "the code or policy changed since this run.\n")

    ours, theirs = report["ours"], report["theirs"]
    note = " (a smoke test at this size)" if n < MIN_CALIBRATION_N else ""
    print(f"Outcomes on {n} labeled alerts{note}")
    print(f"  {'':<44}{'jev':>6}{'yours':>8}   (yours: routing by configured severity only)")
    for key, desc in OUTCOMES:
        other = "-" if key in NOT_APPLICABLE_TO_BASELINE else theirs[key]
        print(f"  {desc:<44}{ours[key]:>6}{other:>8}")
    total = f"all {len(report['decisions'])} alerts"
    print(f"  {'pages sent, ' + total:<44}{ours['pages']:>6}{theirs['pages']:>8}")
    print(f"  {'reviews sent, ' + total:<44}{ours['reviews']:>6}{theirs['reviews']:>8}")

    tallies = report["tallies"]
    if not any(m for _, m in tallies.values()):
        print("\nNo Jev answers to score (every alert took the rule or fallback path).")
        return
    print("\nAgreement with labels, Jev-judged alerts only (Wilson 95% CI)")
    for q, (k, m) in tallies.items():
        if m:
            lo, hi = wilson(k, m)
            label = "page (P>=0.5)" if q == "page" else q
            print(f"  {label:<14}{k:>4}/{m:<4}{k / m:>5.0%}   CI {lo:.0%}-{hi:.0%}")
    if report["never_offered"]:
        print(f"  {report['never_offered']} labeled cause(s) were never offered as candidates: "
              "check the window and topology")

    print("\nCalibration")
    for name, (brier, ece, table) in report["calibration"].items():
        k_total = sum(row[2] for row in table)
        print(f"  {name}: n={k_total}  Brier {brier:.3f}  ECE {ece:.3f}")
        for lo, hi, k, mean_p, rate in table:
            print(f"    [{lo:.1f}, {hi:.1f})  n={k:<4} predicted {mean_p:.2f}  observed {rate:.2f}")
    if report["calibration_n"] < MIN_CALIBRATION_N:
        print(f"  n < {MIN_CALIBRATION_N}: treat these as noise. Replay labeled history "
              "before trusting any threshold.")


def print_sweep(results, alerts, show=10):
    base = triage.Policy(**results["meta"]["policy"])
    labeled = [a for a in alerts if labels(a)]
    if not labeled:
        print("No labeled alerts: nothing to sweep.")
        return
    rows = []
    for page_bar, no_page_bar, drop_bar in itertools.product(
            (0.6, 0.7, 0.8, 0.9), (0.1, 0.2, 0.3, 0.4), (0.02, 0.05, 0.1)):
        if no_page_bar >= page_bar:
            continue
        policy = dataclasses.replace(base, page_bar=page_bar, no_page_bar=no_page_bar,
                                     drop_bar=drop_bar)
        c = count_outcomes(labeled, reroute(results, alerts, policy))
        # Worst first: silence, then missed pages, delays, noise, review load.
        cost = (c["silent_miss"], c["missed_page"], c["delayed_page"],
                c["false_page"] + c["duplicate_page"] + c["misrouted"], c["reviews"])
        rows.append((cost, (page_bar, no_page_bar, drop_bar), c))
    rows.sort(key=lambda r: r[0])
    current = (base.page_bar, base.no_page_bar, base.drop_bar)
    shown = rows[:show] + [r for r in rows[show:] if r[1] == current]
    print(f"\nThreshold sweep over stored answers, best first (* = current policy)")
    print("   page  no_page  drop | silent missed delayed false  dup | reviews pages")
    for _, (page_bar, no_page_bar, drop_bar), c in shown:
        mark = "*" if (page_bar, no_page_bar, drop_bar) == current else " "
        print(f"  {mark}{page_bar:<5}{no_page_bar:>8}{drop_bar:>6} |"
              f"{c['silent_miss']:>7}{c['missed_page']:>7}{c['delayed_page']:>8}"
              f"{c['false_page']:>6}{c['duplicate_page']:>5} |{c['reviews']:>8}{c['pages']:>6}")
    if len(labeled) < MIN_CALIBRATION_N:
        print(f"  n={len(labeled)}: most settings tie at this size. The sweep means "
              "something on replayed history, not on the smoke test.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Evaluate triage.py results offline.")
    ap.add_argument("results", nargs="?", default=os.path.join(triage.BASE, "results.json"))
    ap.add_argument("--alerts", help="labeled alerts (default: the file triage.py read)")
    ap.add_argument("--sweep", action="store_true", help="re-route stored answers under other thresholds")
    ap.add_argument("--shadow", metavar="LOG",
                    help="score a shadow-mode log (server.py --shadow-log) against its labels "
                         "instead of a results file")
    args = ap.parse_args(argv)

    if args.shadow:
        import shadow  # local import: only this mode needs it
        if not os.path.exists(args.shadow):
            sys.exit(f"{args.shadow} not found. Run server.py with --shadow-log first.")
        events, bad = shadow.load(args.shadow)
        summary = shadow.summarize(events, bad, recent=0)
        print(f"Shadow log: {summary['alerts']} alerts since {summary['since']}, "
              f"{summary['labeled']} labeled")
        print(f"Pages: your routing {summary['pages']['your_routing']}, "
              f"jev-oncall {summary['pages']['jev_oncall']}")
        for c in summary["comparisons"]:
            if c["count"]:
                print(f"  {c['count']:>5}  {c['label']}")
        if bad:
            print(f"  ({bad} unreadable lines skipped)")
        print()
        results, alerts = shadow.to_results(events)
        print_report(results, alerts)
        if args.sweep:
            print_sweep(results, alerts)
        return 0

    results = triage.load_json(args.results)
    if not isinstance(results, dict) or results.get("meta", {}).get("version") != 2:
        sys.exit(f"{args.results} predates v2 and has no stored answers. Re-run triage.py.")
    path = args.alerts or results["meta"]["alerts_path"]
    if not os.path.exists(path):  # the repo moved since the run
        path = os.path.join(triage.BASE, os.path.basename(path))
    alerts = triage.load_alerts(path)
    print_report(results, alerts)
    if args.sweep:
        print_sweep(results, alerts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
