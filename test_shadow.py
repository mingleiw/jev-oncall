#!/usr/bin/env python3
"""Shadow mode tests: no API key, no network.

    python3 -m unittest test_shadow -v
"""
import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.client import HTTPConnection
from unittest import mock

import evaluate
import server
import shadow
import test_triage
import triage

PAGE_NOW = {"SEV1": 0.95, "SEV2": 0.05}
UNSURE = {"SEV2": 0.5, "SEV3": 0.5}
QUIET = {"SEV3": 0.2, "SEV4": 0.8}


def alert(aid, sev="critical", **kw):
    return {**test_triage.alert(aid, sev=sev), **kw}


class Compare(unittest.TestCase):
    def test_each_kind_of_difference(self):
        cases = {
            ("PAGE", "PAGE"): "agree",
            ("PAGE_NOW", "PAGE"): "agree",
            ("TICKET", "LOG"): "agree",
            ("PAGE", "TICKET"): "page_added",
            ("REVIEW", "TICKET"): "review_added",
            ("DROP", "LOG"): "dropped",
            ("REVIEW", "PAGE"): "page_to_review",
            ("DEDUP", "PAGE"): "page_deduped",
            ("TICKET", "PAGE"): "page_held_back",
            ("DROP", "PAGE"): "page_held_back",
        }
        for (action, baseline), want in cases.items():
            self.assertEqual(shadow.compare(action, baseline), want, (action, baseline))


class Labels(unittest.TestCase):
    TEAMS = {"database": "dbs", "compute": "servers"}

    def test_a_good_label(self):
        label = shadow.validate_label({"severity": "SEV2", "actionable": True, "team": "database"},
                                      self.TEAMS)
        self.assertEqual(label, {"severity": "SEV2", "actionable": True, "team": "database",
                                 "duplicate_of": None})

    def test_bad_labels_say_what_is_wrong(self):
        bad = [
            ([], "JSON object"),
            ({"severity": "SEV9", "actionable": True}, "severity"),
            ({"severity": "SEV2", "actionable": "yes"}, "actionable"),
            ({"severity": "SEV2", "actionable": True, "team": "payments"}, "team"),
            ({"severity": "SEV2", "actionable": True, "duplicate_of": 7}, "duplicate_of"),
            ({"severity": "SEV2", "actionable": True, "sev": 1}, "unknown key"),
        ]
        for body, fragment in bad:
            with self.assertRaises(ValueError) as cm:
                shadow.validate_label(body, self.TEAMS)
            self.assertIn(fragment, str(cm.exception))


class LogAndReport(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.addCleanup(os.unlink, self.path)
        self.log = shadow.ShadowLog(self.path)

    def record(self, aid, action, standalone=None):
        return {"id": aid, "action": action, "standalone": standalone or action,
                "team": "compute", "source": "jev", "linked_to": None, "notify": [],
                "reasons": [f"why {aid}"], "candidates": [], "error": None, "call": None,
                "judgment": {"model": triage.MODEL, "p_actionable": 0.9,
                             "severity": {"SEV4": 0.0, "SEV3": 0.1, "SEV2": 0.3, "SEV1": 0.6},
                             "team": {t: (1.0 if t == "compute" else 0.0) for t in triage.TEAM_CRITERIA},
                             "duplicate_of": None}}

    def test_summary_counts_the_latest_decision_per_alert(self):
        self.log.start(triage.Config())
        self.log.decision(alert("a1", "critical"), self.record("a1", "REVIEW"))
        self.log.decision(alert("a2", "warning"), self.record("a2", "PAGE_NOW"))
        self.log.decision(alert("a3", "critical"), self.record("a3", "PAGE"))
        self.log.decision(alert("a3", "critical"), self.record("a3", "PAGE_NOW"))  # judged again
        self.log.review("a1", "acked", "alice")
        self.log.label("a2", {"severity": "SEV1", "actionable": True, "team": None,
                              "duplicate_of": None})
        with open(self.path, "a") as f:
            f.write('{"type": "decision", "id": "half-writ')  # a crash mid-line

        events, bad = shadow.load(self.path)
        s = shadow.summarize(events, bad)
        self.assertEqual(s["alerts"], 3)
        counts = {c["key"]: c["count"] for c in s["comparisons"]}
        self.assertEqual((counts["agree"], counts["page_to_review"], counts["page_added"]),
                         (1, 1, 1))
        self.assertEqual(s["pages"], {"your_routing": 2, "jev_oncall": 2})
        self.assertEqual(s["reviews"], {"acked": 1})
        self.assertEqual(s["labeled"], 1)
        self.assertEqual(s["unreadable_lines"], 1)
        self.assertEqual({d["id"] for d in s["recent_differences"]}, {"a1", "a2"})
        self.assertEqual(s["model"], triage.MODEL)

    def test_a_missing_log_is_empty(self):
        self.assertEqual(shadow.load(self.path + ".nope"), ([], 0))

    def test_labels_become_evaluation_input(self):
        self.log.start(triage.Config())
        self.log.decision(alert("a1"), self.record("a1", "PAGE_NOW"))
        # A payload's own "expected" block is not a label: only /label counts.
        self.log.decision(alert("a2", expected={"severity": "SEV4", "actionable": False}),
                          self.record("a2", "PAGE"))
        self.log.label("a1", {"severity": "SEV1", "actionable": True, "team": "compute",
                              "duplicate_of": None})
        results, alerts = shadow.to_results(shadow.load(self.path)[0])
        self.assertEqual(results["meta"]["version"], 2)
        self.assertEqual([a.get("expected", {}).get("severity") for a in alerts], ["SEV1", None])
        report = evaluate.compute_report(results, alerts)
        self.assertEqual(report["n"], 1)
        self.assertEqual(list(report["tallies"]["severity"]), [1, 1])

    def test_evaluate_reads_a_shadow_log(self):
        self.log.start(triage.Config())
        self.log.decision(alert("a1"), self.record("a1", "REVIEW"))
        self.log.label("a1", {"severity": "SEV2", "actionable": True, "team": None,
                              "duplicate_of": None})
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(evaluate.main(["--shadow", self.path]), 0)
        text = out.getvalue()
        self.assertIn("1 alerts", text)
        self.assertIn("1 labeled", text)
        self.assertIn("would ask for a review instead", text)


class RunnerInShadowMode(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        os.unlink(self.path)  # the runner creates it
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.runner = server.TriageRunner(api_key="k", rate_limit=1000,
                                          config=triage.Config(shadow_log=self.path))
        self.answers = {}
        judge = lambda alerts, *a, **k: ({x["id"]: self.answers[x["id"]] for x in alerts}, {}, {})
        patcher = mock.patch.object(triage, "judge_all", side_effect=judge)
        patcher.start()
        self.addCleanup(patcher.stop)

    def events(self, kind):
        return [e for e in shadow.load(self.path)[0] if e["type"] == kind]

    def test_decisions_and_review_outcomes_are_logged(self):
        self.answers = {"r1": test_triage.judgment(sev=UNSURE),
                        "r2": test_triage.judgment(sev=UNSURE),
                        "r3": test_triage.judgment(sev=UNSURE),
                        "q1": test_triage.judgment(sev=QUIET)}
        self.runner.triage([alert("r1"), alert("r2"), alert("r3"), alert("q1", "warning")])
        self.assertEqual(len(self.events("start")), 1)
        by_id = {e["id"]: e for e in self.events("decision")}
        self.assertEqual(by_id["r1"]["baseline"], "PAGE")
        self.assertEqual(by_id["r1"]["comparison"], "page_to_review")
        self.assertEqual(by_id["q1"]["comparison"], "agree")

        self.runner.ack("r1", "alice")
        self.runner.resolve(["r2"])
        self.runner.reviews._pending["r3"]["deadline"] = time.monotonic() - 1
        self.runner.sweep_reviews()
        outcomes = {e["id"]: (e["outcome"], e["by"]) for e in self.events("review")}
        self.assertEqual(outcomes, {"r1": ("acked", "alice"),
                                    "r2": ("cancelled", "resolved upstream"),
                                    "r3": ("escalated", None)})

    def test_shadow_mode_off_logs_nothing(self):
        runner = server.TriageRunner(api_key="k")
        self.answers = {"a1": test_triage.judgment(sev=PAGE_NOW)}
        runner.triage([alert("a1")])
        self.assertIsNone(runner.shadow)
        self.assertIsNone(runner.label("a1", {"severity": "SEV1", "actionable": True}))


class ShadowHTTP(unittest.TestCase):
    def serve(self, runner):
        from http.server import HTTPServer
        old = getattr(server.Handler, "runner", None)
        server.Handler.runner = runner
        httpd = HTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close(),
                                 setattr(server.Handler, "runner", old)))
        return httpd.server_address[1]

    def request(self, port, method, path, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        with mock.patch("sys.stderr", io.StringIO()):
            conn.request(method, path, body=json.dumps(body) if body is not None else None,
                         headers=headers or {})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())

    def test_endpoints_say_when_shadow_mode_is_off(self):
        port = self.serve(server.TriageRunner())
        self.assertEqual(self.request(port, "GET", "/shadow")[0], 404)
        status, body = self.request(port, "POST", "/label/a1",
                                    {"severity": "SEV1", "actionable": True})
        self.assertEqual(status, 404)
        self.assertIn("shadow mode is off", body["error"])

    def test_label_and_summary(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        runner = server.TriageRunner(config=triage.Config(shadow_log=path))
        port = self.serve(runner)
        self.request(port, "POST", "/ingest", {"id": "g1", "title": "db down",
                                               "configured_severity": "critical"})
        status, body = self.request(port, "POST", "/label/g1",
                                    {"severity": "SEV1", "actionable": True},
                                    {"X-Labeled-By": "alice"})
        self.assertEqual((status, body["labeled_by"]), (201, "alice"))
        status, body = self.request(port, "POST", "/label/g1", {"severity": "SEV7"})
        self.assertEqual(status, 400)
        status, summary = self.request(port, "GET", "/shadow")
        self.assertEqual(status, 200)
        # No key: the decision falls back to configured severity, same as the baseline.
        self.assertEqual((summary["alerts"], summary["labeled"]), (1, 1))
        self.assertEqual(summary["comparisons"][0], {"key": "agree", "count": 1,
                                                     "label": "Same as your current routing"})


class ShadowConfig(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        return triage.load_config(f.name)

    def test_log_path(self):
        self.assertEqual(self.load('[shadow]\nlog = "/var/lib/jev/shadow.jsonl"\n').shadow_log,
                         "/var/lib/jev/shadow.jsonl")
        self.assertIsNone(triage.load_config(None).shadow_log)

    def test_bad_shadow_config(self):
        for text, fragment in (('[shadow]\npath = "x"\n', "unknown key"),
                               ('[shadow]\nlog = ""\n', "file path")):
            with self.assertRaises(triage.ConfigError) as cm:
                self.load(text)
            self.assertIn(fragment, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
