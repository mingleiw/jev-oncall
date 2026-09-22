#!/usr/bin/env python3
"""Offline tests for generate_dashboard.py, with the same fake Jev as test_triage.

    python3 -m unittest -v
"""
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import generate_dashboard as gd
import test_triage
import triage


def repo_alerts():
    return triage.load_alerts(os.path.join(triage.BASE, "alerts.json"))


def run_triage(alerts, env):
    """Run triage.py over these alerts with the label oracle; return results."""
    environ = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
    environ.update(env)
    with tempfile.TemporaryDirectory() as tmp:
        alerts_path, out = os.path.join(tmp, "alerts.json"), os.path.join(tmp, "results.json")
        with open(alerts_path, "w", encoding="utf-8") as f:
            json.dump(alerts, f)
        with mock.patch.dict(os.environ, environ, clear=True), redirect_stdout(io.StringIO()), \
                redirect_stderr(io.StringIO()), \
                mock.patch.object(triage, "call_jev", side_effect=test_triage.label_oracle(alerts)):
            triage.main(["--alerts", alerts_path, "--out", out])
        return triage.load_json(out)


class Dashboard(unittest.TestCase):
    def test_every_alert_renders_and_each_judged_alert_gets_one_dot(self):
        alerts = repo_alerts()
        results = run_triage(alerts, {"TYPESAFE_API_KEY": "test"})
        page = gd.render(results, alerts)
        judged = sum(1 for r in results["alerts"] if r["judgment"])
        self.assertEqual(page.count('class="pin '), judged)
        for a in alerts:
            self.assertIn(f'id="alert-{a["id"]}"', page)
        self.assertIn("flagged for paging", page)
        self.assertIn("Against the labels", page)

    def test_no_em_or_en_dashes_anywhere(self):
        alerts = repo_alerts()
        page = gd.render(run_triage(alerts, {"TYPESAFE_API_KEY": "test"}), alerts)
        self.assertNotIn("\u2014", page)
        self.assertNotIn("\u2013", page)

    def test_alert_text_is_escaped(self):
        alerts = repo_alerts()
        alerts[0] = dict(alerts[0], title="Firing: <script>alert(1)</script>")
        page = gd.render(run_triage(alerts, {"TYPESAFE_API_KEY": "test"}), alerts)
        self.assertNotIn("<script>alert(1)", page)
        self.assertIn("&lt;script&gt;alert(1)", page)

    def test_a_dot_never_crosses_a_policy_bar(self):
        alerts = [test_triage.alert("x1"), test_triage.alert("x2")]
        judgments = {"x1": test_triage.judgment(sev={"SEV2": 0.79, "SEV3": 0.21}),
                     "x2": test_triage.judgment(sev={"SEV2": 0.80, "SEV3": 0.20})}
        decisions = triage.route_all(alerts, judgments, {}, triage.Policy())
        html = gd.rail(alerts, decisions, judgments, {}, triage.Policy())
        # Columns are binned, so a dot sits at its bin's centre rather than its
        # exact probability. 0.79 must still render left of the 0.80 bar.
        for mode in ("d", "m"):
            below = re.search(rf'alert-x1"[^>]*--x{mode}:([\d.]+)%', html)
            at = re.search(rf'alert-x2"[^>]*--x{mode}:([\d.]+)%', html)
            self.assertLess(float(below.group(1)), 80.0)
            self.assertGreaterEqual(float(at.group(1)), 80.0)

    def test_crowded_columns_get_one_overflow_label_each(self):
        alerts = [test_triage.alert(f"x{i:03d}") for i in range(60)]
        judgments = {a["id"]: test_triage.judgment(sev={"SEV1": 0.99, "SEV3": 0.01})
                     for a in alerts}
        decisions = triage.route_all(alerts, judgments, {}, triage.Policy())
        html = gd.rail(alerts, decisions, judgments, {}, triage.Policy())
        # All 60 share a probability, so they land in one column per mode and
        # must produce exactly one "+n" each instead of a pile of labels.
        for mode in ("d", "m"):
            self.assertEqual(html.count(f'class="more only-{mode}"'), 1)
        self.assertIn(f"+{60 - gd.MAX_STACK}<", html)

    def test_fail_open_run_explains_the_empty_scale(self):
        alerts = repo_alerts()
        page = gd.render(run_triage(alerts, {}), alerts)
        self.assertIn("nothing to place on the scale", page)
        self.assertIn("fell back to configured severity", page)
        self.assertNotIn('class="pin ', page)

    def test_safety_violations_come_before_everything_else(self):
        alerts = repo_alerts()
        results = run_triage(alerts, {"TYPESAFE_API_KEY": "test"})
        results["summary"]["invariant_violations"] = ["a02: needs PAGE_NOW but its root a01 only gets TICKET"]
        page = gd.render(results, alerts)
        self.assertLess(page.index("Safety check failed"), page.index("Alerts by outcome"))

    def test_cli_refuses_v1_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "results.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"alerts": 14, "results": []}, f)
            with self.assertRaises(SystemExit):
                gd.main([path, "--out", os.path.join(tmp, "dashboard.html")])


if __name__ == "__main__":
    unittest.main()
