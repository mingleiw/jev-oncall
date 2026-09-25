#!/usr/bin/env python3
"""Live dashboard tests: the review queue, shadow comparison, label forms,
and the optional token on /ack and /label. No API key, no network.

    python3 -m unittest test_live -v
"""
import io
import json
import os
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from unittest import mock

import server
import shadow
import test_triage
import triage

UNSURE = {"SEV2": 0.5, "SEV3": 0.5}
QUIET = {"SEV3": 0.1, "SEV4": 0.9}


def alert(aid, sev="critical"):
    return test_triage.alert(aid, sev=sev)


class Serving(unittest.TestCase):
    """Runs a real server on a free port around one runner."""

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
            raw = resp.read()
        ctype = resp.getheader("Content-Type", "")
        return resp.status, (json.loads(raw) if "json" in ctype else raw.decode())

    def shadow_log(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        return path

    def judged(self, runner, answers):
        judge = lambda alerts, *a, **k: ({x["id"]: answers[x["id"]] for x in alerts}, {}, {})
        with mock.patch.object(triage, "judge_all", side_effect=judge):
            runner.triage([alert(aid) for aid in answers])


class TokenOption(Serving):
    def test_ack_and_label_are_open_by_default(self):
        runner = server.TriageRunner(api_key="k", secret="s3",
                                     config=triage.Config(shadow_log=self.shadow_log()))
        self.judged(runner, {"x": test_triage.judgment(sev=QUIET)})
        port = self.serve(runner)
        self.assertEqual(self.request(port, "POST", "/ack/nope")[0], 404)
        self.assertEqual(self.request(port, "POST", "/label/x", {"severity": "SEV1",
                                                                  "actionable": True})[0], 201)

    def test_require_token_guards_ack_and_label(self):
        config = triage.Config(shadow_log=self.shadow_log(), require_token=True)
        runner = server.TriageRunner(api_key="k", secret="s3", config=config)
        self.judged(runner, {"r1": test_triage.judgment(sev=UNSURE)})
        port = self.serve(runner)
        label = {"severity": "SEV2", "actionable": True}
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "s3"}):
            self.assertEqual(self.request(port, "POST", "/ack/r1", {}, headers)[0], 401, headers)
            status, body = self.request(port, "POST", "/label/r1", label, headers)
            self.assertEqual(status, 401)
            self.assertIn("Bearer", body["error"])
        good = {"Authorization": "Bearer s3", "X-Acked-By": "alice"}
        status, body = self.request(port, "POST", "/ack/r1", {}, good)
        self.assertEqual((status, body["acked_by"]), (200, "alice"))
        self.assertEqual(self.request(port, "POST", "/label/r1", label, good)[0], 201)
        # Reading stays open: the token only guards actions.
        self.assertEqual(self.request(port, "GET", "/shadow")[0], 200)

    def test_other_sites_cannot_ack_or_label(self):
        runner = server.TriageRunner(api_key="k", config=triage.Config(shadow_log=self.shadow_log()))
        self.judged(runner, {"r1": test_triage.judgment(sev=UNSURE)})
        port = self.serve(runner)
        for site in ("cross-site", "same-site"):
            for path, body in (("/ack/r1", {}), ("/label/r1", {"severity": "SEV2", "actionable": True})):
                status, _ = self.request(port, "POST", path, body, {"Sec-Fetch-Site": site})
                self.assertEqual(status, 403, (site, path))
        self.assertEqual(len(runner.reviews.pending()), 1)  # still pending, still pages
        self.assertEqual(self.request(port, "POST", "/ack/r1", {},
                                      {"Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_config_and_startup_checks(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write("[server]\nrequire_token = true\n")
        self.addCleanup(os.unlink, f.name)
        self.assertTrue(triage.load_config(f.name).require_token)
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as cm:
            server.main(["--config", f.name, "--port", "0"])
        self.assertIn("JEV_WEBHOOK_SECRET", str(cm.exception.code))

        with open(f.name, "w") as g:
            g.write('[server]\nrequire_token = "yes"\n')
        with self.assertRaises(triage.ConfigError):
            triage.load_config(f.name)


class Demo(unittest.TestCase):
    def test_demo_page_shows_every_behavior_and_says_where_answers_come_from(self):
        import build_demo
        html = build_demo.build()
        flat = " ".join(html.split())
        self.assertIn("Interactive demo", flat)
        self.assertIn(f"replayed from the real {build_demo.MODEL} run", flat)
        self.assertIn("no model is called in your browser", flat)
        self.assertIn("window.JEV_DEMO = {engine:", html)
        for control in ('id="demo-reset"', 'id="demo-advance"', 'id="demo-timeout"', 'class="live-btn ghost resolve"'):
            self.assertIn(control, html)
        import html as htmllib
        # The real run has no dedup and no drop, so those two kinds don't occur.
        for key in ("agree", "page_added", "review_added", "page_to_review", "page_held_back"):
            self.assertIn(htmllib.escape(shadow.COMPARISON_LABELS[key]), html)
        self.assertEqual(html.count('class="live-btn ack"'), 2)  # an unsure and a cross-team review
        self.assertIn("root is owned by database, so compute gets a REVIEW", html)
        self.assertIn("isn't built yet", html)  # decisions are recorded, not delivered
        self.assertNotIn("No alert in this run has an expected label", html)


class LiveApp(Serving):
    def test_dashboard_uses_the_demo_layout_without_the_demo_parts(self):
        port = self.serve(server.TriageRunner(api_key="k"))
        _, html = self.request(port, "GET", "/dashboard")
        self.assertIn('<body class="app"', html)
        self.assertIn("No alerts yet.", html)

        runner = server.TriageRunner(api_key="k")
        self.judged(runner, {"r1": test_triage.judgment(sev=UNSURE)})
        _, html = self.request(self.serve(runner), "GET", "/dashboard")
        left = html.split('<div id="left-content">', 1)[1].split('<div class="col-center">', 1)[0]
        self.assertIn('id="you-name"', left)  # next to the Ack buttons, not in a hidden tab
        self.assertIn('class="live-btn ack" id="ack-r1" data-id="r1"', left)
        self.assertIn('class="ev-card" data-id="r1"', html)
        self.assertIn("Shadow mode is off", html)
        for demo_only in ("window.JEV_DEMO = {engine:", "Interactive demo", 'id="demo-advance"', "Demo clock"):
            self.assertNotIn(demo_only, html)


class LivePage(Serving):
    def test_review_queue_shadow_comparison_and_labels(self):
        path = self.shadow_log()
        config = triage.Config(shadow_log=path)
        runner = server.TriageRunner(api_key="k", config=config)
        self.judged(runner, {"r1": test_triage.judgment(sev=UNSURE),
                             "q1": test_triage.judgment(p_act=0.01, sev=QUIET)})
        shadow.ShadowLog(path).label("q1", {"severity": "SEV4", "actionable": False,
                                            "team": None, "duplicate_of": None})
        port = self.serve(runner)
        status, html = self.request(port, "GET", "/dashboard")
        self.assertEqual(status, 200)

        # The review queue: r1 is unsure, so it waits with an Ack button and a clock.
        self.assertIn("Reviews waiting for an ack", html)
        self.assertRegex(html, r'class="live-btn ack" id="ack-r1" data-id="r1"')
        self.assertRegex(html, r'data-left="(8\d\d|900)"')
        # The shadow comparison: q1 is dropped where your routing paged, r1 goes to review.
        self.assertIn("Compared with your current routing", html)
        self.assertIn(shadow.COMPARISON_LABELS["page_held_back"], html)
        self.assertIn(shadow.COMPARISON_LABELS["page_to_review"], html)
        # A label form per alert, offering your teams.
        self.assertEqual(html.count('<form class="label-form"'), 2)
        for team in triage.TEAM_CRITERIA:
            self.assertIn(f'<option value="{team}">', html)
        # The label posted to /label shows up, and the evaluation scores it.
        self.assertIn("<dt>Labeled</dt><dd>not actionable, SEV4", html)
        self.assertNotIn("No alert in this run has an expected label", html)

    def test_without_shadow_mode_there_is_no_comparison_or_label_form(self):
        runner = server.TriageRunner(api_key="k")
        self.judged(runner, {"r1": test_triage.judgment(sev=UNSURE)})
        port = self.serve(runner)
        _, html = self.request(port, "GET", "/dashboard")
        self.assertIn("Reviews waiting for an ack", html)
        self.assertNotIn("Compared with your current routing", html)
        self.assertNotIn('<form class="label-form"', html)
        self.assertNotIn('id="you-token"', html)

    def test_token_field_only_when_required(self):
        config = triage.Config(require_token=True)
        port = self.serve(server.TriageRunner(secret="tok-9f3b1c7e", config=config))
        _, html = self.request(port, "GET", "/dashboard")
        self.assertIn('id="you-token"', html)
        self.assertNotIn("tok-9f3b1c7e", html)  # never the secret itself

    def test_payload_labels_are_ignored_in_shadow_mode(self):
        path = self.shadow_log()
        runner = server.TriageRunner(config=triage.Config(shadow_log=path))
        runner.triage([{**alert("p1"), "expected": {"severity": "SEV1", "actionable": True}}])
        results, alerts = runner.results()
        self.assertNotIn("expected", alerts[0])


if __name__ == "__main__":
    unittest.main()
