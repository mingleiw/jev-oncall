#!/usr/bin/env python3
"""The review clock end to end: acks, cancellations, escalation, duplicate
deliveries, restarts, labels, the two baselines, and the browser demo's
engine (which is this same code on a demo clock). No API key, no network.

    python3 -m unittest test_reviews -v
"""
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import build_demo
import evaluate
import generate_dashboard
import server
import shadow
import test_live
import test_triage
import triage

T0 = 1_790_000_000.0  # an arbitrary wall-clock second
WINDOW = 15 * 60
UNSURE = {"SEV2": 0.5, "SEV3": 0.5}


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def entry(aid, started="2026-09-24T14:00:00Z"):
    return {"id": aid, "title": f"{aid} title", "team": "compute", "action": "REVIEW",
            "reasons": ["unsure"], "started_at": started}


def bundled_run():
    """alerts.json through the batch path with test_triage's fake Jev."""
    path = os.path.join(triage.BASE, "alerts.json")
    alerts = triage.load_alerts(path)
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test"}), \
            mock.patch.object(triage, "call_jev", side_effect=test_triage.label_oracle(alerts)), \
            redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        out = os.path.join(tmp, "results.json")
        triage.main(["--alerts", path, "--out", out])
        return triage.load_json(out), alerts


class Stored(unittest.TestCase):
    def store(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return os.path.join(d.name, "reviews.jsonl")

    def events(self, path):
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]


class ReviewLifecycle(Stored):
    def test_ack_closes_the_review_records_who_and_it_never_escalates(self):
        clock = Clock()
        q = server.ReviewQueue(15, clock=clock)
        q.add(entry("r1"))
        record = q.ack("r1", "alice")
        self.assertEqual((record["state"], record["acked_by"]), ("acked", "alice"))
        self.assertEqual(q.pending(), [])
        clock.t += WINDOW + 1
        self.assertEqual(q.sweep(), [])
        self.assertEqual(q.states()["r1"]["state"], "acked")
        self.assertEqual(q.states()["r1"]["by"], "alice")
        self.assertIsNone(q.ack("r1", "bob"))  # closed: a second ack changes nothing

    def test_a_resolve_cancels_before_the_deadline_and_is_not_an_ack(self):
        clock = Clock()
        q = server.ReviewQueue(15, clock=clock)
        q.add(entry("r1"))
        record = q.cancel("r1")
        self.assertEqual((record["state"], record["cancelled_by"]), ("cancelled", "resolved upstream"))
        self.assertNotIn("acked_by", record)
        clock.t += WINDOW + 1
        self.assertEqual(q.sweep(), [])
        self.assertEqual(q.states()["r1"]["state"], "cancelled")

    def test_simulated_expiry_escalates_exactly_once(self):
        clock = Clock()
        q = server.ReviewQueue(15, clock=clock)
        q.add(entry("r1"))
        clock.t += WINDOW - 1
        self.assertEqual(q.sweep(), [])
        clock.t += 1
        (paged,) = q.sweep()
        self.assertEqual((paged["id"], paged["action"], paged["escalated_from"]), ("r1", "PAGE", "REVIEW"))
        clock.t += WINDOW
        self.assertEqual(q.sweep(), [])
        self.assertIsNone(q.ack("r1", "late"))  # too late: it already paged
        self.assertEqual(q.states()["r1"]["state"], "escalated")

    def test_duplicate_deliveries_keep_the_first_deadline_and_never_reopen(self):
        clock = Clock()
        q = server.ReviewQueue(15, clock=clock)
        self.assertEqual(q.add(entry("r1")), "opened")
        clock.t += 600
        self.assertEqual(q.add(entry("r1")), "kept")
        self.assertEqual(q.pending()[0]["seconds_left"], WINDOW - 600)
        q.ack("r1", "alice")
        self.assertEqual(q.add(entry("r1")), "kept")  # the same firing, delivered again
        self.assertEqual(q.pending(), [])
        # Even stamped with a new start time, as some providers do per delivery.
        self.assertEqual(q.add(entry("r1", started="2026-09-24T14:09:00Z")), "kept")
        # Once the alert resolves, its next firing is a new review.
        q.cleared("r1")
        self.assertEqual(q.add(entry("r1")), "kept")  # a late retry of the first delivery
        self.assertEqual(q.add(entry("r1", started="2026-09-24T18:00:00Z")), "opened")
        self.assertEqual(q.pending()[0]["seconds_left"], WINDOW)
        q.ack("r1", "alice")
        # And an hour after a review closed, a still-firing alert counts as new.
        clock.t += server.ALERT_TTL_S
        self.assertEqual(q.add(entry("r1")), "opened")


class RestartRecovery(Stored):
    def test_pending_reviews_keep_their_original_deadline_across_a_restart(self):
        path, clock = self.store(), Clock()
        server.ReviewQueue(15, path, clock).add(entry("r1"))
        clock.t += 300
        q = server.ReviewQueue(15, path, clock)  # restarted
        self.assertEqual(q.recovered["pending"], 1)
        self.assertEqual(q.pending()[0]["seconds_left"], WINDOW - 300)
        clock.t += 60
        self.assertEqual(q.add(entry("r1")), "kept")  # a duplicate after the restart
        self.assertEqual(q.pending()[0]["seconds_left"], WINDOW - 360)

    def test_restart_escalates_overdue_reviews_once_and_keeps_closed_ones_closed(self):
        path, clock = self.store(), Clock()
        q = server.ReviewQueue(15, path, clock)
        for aid in ("acked", "resolved", "overdue"):
            q.add(entry(aid))
        q.ack("acked", "alice")
        q.cancel("resolved")
        # The server is down past the deadline, then comes back.
        clock.t += WINDOW + 120
        q = server.ReviewQueue(15, path, clock)
        paged = q.sweep()
        self.assertEqual([e["id"] for e in paged], ["overdue"])
        self.assertEqual(q.pending(), [])
        q.cleared("acked")  # the acked alert resolved later: recorded, and it survives a restart
        # And again: nothing escalates a second time, nothing reopens.
        for _ in range(2):
            q = server.ReviewQueue(15, path, clock)
            self.assertEqual(q.sweep(), [])
            self.assertEqual(q.pending(), [])
            states = {aid: s["state"] for aid, s in q.states().items()}
            self.assertEqual(states, {"acked": "acked", "resolved": "cancelled", "overdue": "escalated"})
            for aid in ("resolved", "overdue"):
                self.assertEqual(q.add(entry(aid)), "kept")  # re-sent after the restart
            self.assertTrue(q.states()["acked"]["state"] == "acked")
        self.assertEqual(q.states()["acked"]["by"], "alice")
        escalations = [e for e in self.events(path) if e["event"] == "escalated"]
        self.assertEqual([e["id"] for e in escalations], ["overdue"])

    def test_a_torn_last_line_is_skipped(self):
        path, clock = self.store(), Clock()
        server.ReviewQueue(15, path, clock).add(entry("r1"))
        with open(path, "a") as f:
            f.write('{"event": "acked", "id": "r1"')  # the crash hit mid-write
        q = server.ReviewQueue(15, path, clock)
        self.assertEqual(q.recovered["unreadable"], 1)
        self.assertEqual(len(q.pending()), 1)  # an ack that never landed doesn't count

    def test_compaction_keeps_pending_and_a_week_of_closed_reviews(self):
        path, clock = self.store(), Clock()
        q = server.ReviewQueue(15, path, clock)
        q.add(entry("old"))
        q.ack("old", "alice")
        clock.t += 8 * 24 * 3600
        q = server.ReviewQueue(15, path, clock)
        q.add(entry("new"))
        q = server.ReviewQueue(15, path, clock)
        self.assertNotIn("old", q.states())
        self.assertEqual([p["id"] for p in q.pending()], ["new"])
        self.assertEqual({e["id"] for e in self.events(path)}, {"new"})

    def test_runner_recovers_through_the_shadow_log_without_duplicates(self):
        path, clock = self.store(), Clock()
        log = self.store() + ".shadow"
        config = triage.Config(review_store=path, shadow_log=log)
        judge = lambda alerts, *a, **k: ({x["id"]: test_triage.judgment(sev=UNSURE) for x in alerts}, {}, {})
        runner = server.TriageRunner(api_key="k", config=config, clock=clock)
        with mock.patch.object(triage, "judge_all", side_effect=judge):
            runner.triage([test_triage.alert("a1"), test_triage.alert("a2"), test_triage.alert("a3")])
        runner.ack("a1", "alice")
        runner.resolve(["a2"])
        clock.t += WINDOW + 1
        for _ in range(3):  # restart three times; the sweep runs on each start
            runner = server.TriageRunner(api_key="k", config=config, clock=clock)
            runner.sweep_reviews()
        outcomes = [(e["id"], e["outcome"]) for e in shadow.load(log)[0] if e["type"] == "review"]
        self.assertEqual(outcomes, [("a1", "acked"), ("a2", "cancelled"), ("a3", "escalated")])
        view = runner.live_view()
        self.assertEqual(view["pending"], [])
        self.assertTrue(view["durable"])

    def test_config_and_flag(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[reviews]\nstore = "reviews.jsonl"\n')
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(triage.load_config(f.name).review_store, "reviews.jsonl")
        with open(f.name, "w") as g:
            g.write("[reviews]\nstore = 3\n")
        with self.assertRaises(triage.ConfigError):
            triage.load_config(f.name)


class AckOverHTTP(test_live.Serving):
    def test_acking_a_closed_review_says_how_it_closed(self):
        runner = server.TriageRunner(api_key="k")
        self.judged(runner, {"r1": test_triage.judgment(sev=UNSURE)})
        port = self.serve(runner)
        self.assertEqual(self.request(port, "POST", "/ack/r1", {}, {"X-Acked-By": "alice"})[0], 200)
        status, body = self.request(port, "POST", "/ack/r1", {}, {"X-Acked-By": "bob"})
        self.assertEqual((status, body["state"], body["by"]), (409, "acked", "alice"))
        _, html = self.request(port, "GET", "/dashboard")
        self.assertIn("Reviews waiting for an ack <span class=\"count\">0</span>", html)
        self.assertIn("Now: acked by alice, won&#x27;t page", html)
        self.assertIn("Reviews are held in memory on this server", html)  # no store configured
        _, pending = self.request(port, "GET", "/pending")
        self.assertEqual((pending["count"], pending["closed"][0]["state"]), (0, "acked"))


class Labels(test_live.Serving):
    LABEL = {"severity": "SEV2", "actionable": True, "team": None, "duplicate_of": None}

    def setUp(self):
        self.runner = server.TriageRunner(api_key="k", config=triage.Config(shadow_log=self.shadow_log()))
        self.judged(self.runner, {"a1": test_triage.judgment(sev=UNSURE),
                                  "a2": test_triage.judgment(sev=UNSURE)})
        self.port = self.serve(self.runner)

    def post(self, aid, label):
        return self.request(self.port, "POST", f"/label/{aid}", label)

    def test_editing_a_label_replaces_it_and_the_evaluation_uses_the_newest(self):
        self.assertEqual(self.post("a1", self.LABEL)[0], 201)
        self.assertEqual(self.post("a1", {**self.LABEL, "severity": "SEV4", "actionable": False})[0], 201)
        self.assertEqual(self.runner.shadow_summary()["labeled"], 1)
        results, alerts = self.runner.results()
        report = evaluate.compute_report(results, alerts)
        self.assertEqual(report["n"], 1)
        self.assertEqual(evaluate.labels(alerts[0])["severity"], "SEV4")
        _, html = self.request(self.port, "GET", "/dashboard")
        self.assertIn("1 alert labeled so far", html)
        self.assertIn("<dt>Labeled</dt><dd>not actionable, SEV4", html)
        self.assertIn(">Update label<", html)

    def test_required_fields_and_cause_references_are_checked(self):
        cases = [({**self.LABEL, "severity": None}, 400),
                 ({**self.LABEL, "actionable": "yes"}, 400),
                 ({**self.LABEL, "duplicate_of": "never-received"}, 400),
                 ({**self.LABEL, "duplicate_of": "a1"}, 400)]  # its own cause
        for label, want in cases:
            status, body = self.post("a1", label)
            self.assertEqual(status, want, label)
        self.assertEqual(self.post("nope", self.LABEL)[0], 404)
        self.assertEqual(self.post("a1", {**self.LABEL, "duplicate_of": "a2"})[0], 201)
        self.assertEqual(self.runner.shadow_summary()["labeled"], 1)

    def test_the_cause_is_chosen_from_alerts_received(self):
        _, html = self.request(self.port, "GET", "/dashboard")
        form = re.search(r'<form class="label-form" data-id="a1".*?</form>', html, re.S).group(0)
        self.assertIn('<select id="lf-a1-dup" name="duplicate_of">', form)
        self.assertIn('<option value="a2">', form)
        self.assertNotIn('<option value="a1">', form)


class Baselines(unittest.TestCase):
    """Your current routing, counted the same way in shadow mode and the evaluation."""

    def test_the_sample_incident_counts_agree(self):
        session = build_demo.DemoSession()
        summary = session.runner.shadow_summary()
        results, alerts = session.runner.results()
        report = evaluate.compute_report(results, alerts)
        self.assertEqual(summary["pages"]["your_routing"], 5)
        self.assertEqual(report["theirs"]["pages"], 5)  # was 4: the evaluation skipped staging
        self.assertEqual((summary["pages"]["jev_oncall"], report["ours"]["pages"]), (3, 3))
        self.assertEqual(summary["alerts"], len(report["decisions"]))
        # The staging alert: configured critical, so your routing pages it; jev-oncall logs it.
        self.assertEqual(evaluate.baseline_decisions(alerts)["staging-disk"].action, "PAGE")
        html = session.render()
        self.assertIn("Your current routing paged 5;", html)
        self.assertIn('<td>Pages sent, all 8 alerts</td><td class="n">3</td><td class="n">5</td>', html)
        self.assertIn("Your current routing means configured severity only", html)

    def test_the_bundled_run_uses_the_shadow_baseline(self):
        results, alerts = bundled_run()
        report = evaluate.compute_report(results, alerts)
        want = sum(triage.static_route(a, "x").action in triage.PAGING for a in alerts
                   if a["id"] in report["decisions"])
        self.assertEqual(report["theirs"]["pages"], want)


class DemoEngine(unittest.TestCase):
    """The browser demo runs DemoSession: the server's own code on a demo clock."""

    def setUp(self):
        # No action may reach the network: a real Jev call would fail here.
        patcher = mock.patch.object(triage, "_get_conn", side_effect=AssertionError("network"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def text(self, html):
        html = re.sub(r"<(script|style).*?</\1>", "", html, flags=re.S)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).replace("&#x27;", "'")

    def test_ack_updates_the_count_status_and_story_and_it_cannot_escalate(self):
        s = build_demo.DemoSession()
        before = self.text(s.render())
        self.assertIn("Reviews waiting for an ack 2", before)
        self.assertIn("Of 2 sent to review, 2 still waiting.", before)
        self.assertTrue(s.apply({"type": "ack", "id": "tls-cert", "by": "alice"})["ok"])
        self.assertTrue(s.apply({"type": "ack", "id": "payment", "by": "bob"})["ok"])
        after = s.render()
        text = self.text(after)
        self.assertIn("Reviews waiting for an ack 0", text)
        self.assertIn("Of 2 sent to review, 2 acked.", text)
        for who in ("alice", "bob"):
            self.assertIn(f"Acked by {who} at 14:04 UTC: someone is on it, so it won't escalate. "
                          "The alert itself isn't resolved.", text)
        self.assertNotIn("Waiting for an ack since", text)  # the cards moved on too
        self.assertNotIn('class="pend-left"', after)  # no countdown left running
        self.assertIn("Sent to review", text)  # the arrival decision is kept, not rewritten
        s.apply({"type": "advance"})
        self.assertEqual(s.runner.stats()["escalated_reviews"], 0)
        self.assertFalse(s.apply({"type": "ack", "id": "payment"})["ok"])

    def test_advancing_the_clock_escalates_what_nobody_acked(self):
        s = build_demo.DemoSession([{"type": "ack", "id": "tls-cert", "by": "alice"}])
        result = s.apply({"type": "advance"})
        self.assertIn("Nobody acked payment in time", result["message"])
        text = self.text(s.render())
        self.assertIn("Demo clock 14:19 UTC", text)
        self.assertIn("Paged (escalated)", text)
        self.assertIn("Nobody acked within 15 min, so it paged at 14:19 UTC.", text)
        self.assertIn("jev-oncall paged 3 on arrival, plus 1 review nobody acked, which then paged", text)

    def test_an_alert_that_clears_cancels_its_review(self):
        s = build_demo.DemoSession()
        self.assertTrue(s.apply({"type": "resolve", "id": "tls-cert"})["ok"])
        s.apply({"type": "advance"})
        states = {aid: st["state"] for aid, st in s.runner.reviews.states().items()}
        self.assertEqual(states, {"tls-cert": "cancelled", "payment": "escalated"})
        self.assertIn("the alert cleared before anyone acked, so no page.", self.text(s.render()))

    def test_a_jev_timeout_falls_back_to_configured_severity(self):
        s = build_demo.DemoSession()
        with mock.patch.object(triage, "judge_all", wraps=triage.judge_all) as judge:
            result = s.apply({"type": "timeout"})
        judge.assert_called_once()  # the real judge_all, whose call to Jev timed out
        self.assertTrue(result["ok"], result)
        (a, rec) = next((a, r) for a, r in s.runner.records if a["id"] == "queue-lag")
        self.assertEqual((rec["action"], rec["source"], rec["team"]), ("PAGE", "fallback", "triage-rotation"))
        self.assertIn("TimeoutError", rec["error"])
        self.assertIsNone(rec["judgment"])
        text = self.text(s.render())
        self.assertIn("1 alert fell back to configured severity because Jev was unavailable", text)
        self.assertIn("Jev unavailable (TimeoutError", text)  # on the alert's own card
        self.assertIn("1 by fallback", text)
        self.assertFalse(s.apply({"type": "timeout"})["ok"])  # once per session

    def test_labels_recompute_the_evaluation_and_edits_replace(self):
        s = build_demo.DemoSession()
        self.assertIn("2 alerts labeled so far", self.text(s.render()))
        label = {"severity": "SEV2", "actionable": True, "team": "compute", "duplicate_of": None}
        self.assertTrue(s.apply({"type": "label", "id": "homepage", "label": label})["ok"])
        text = self.text(s.render())
        self.assertIn("3 alerts labeled so far", text)
        self.assertIn("3 labeled alerts is a smoke test", text)
        self.assertIn("Owner 3 of 3", text)
        s.apply({"type": "label", "id": "homepage", "label": {**label, "team": "network"}})
        text = self.text(s.render())
        self.assertIn("3 alerts labeled so far", text)
        self.assertIn("Owner 2 of 3", text)
        bad = s.apply({"type": "label", "id": "homepage", "label": {**label, "duplicate_of": "nope"}})
        self.assertFalse(bad["ok"])
        self.assertFalse(s.apply({"type": "label", "id": "homepage", "label": {**label, "severity": ""}})["ok"])
        self.assertEqual(len(s.actions), 2)  # rejected actions aren't replayed

    def test_start_over_and_replay_are_exact(self):
        actions = [{"type": "ack", "id": "tls-cert", "by": "alice"}, {"type": "advance"},
                   {"type": "timeout"},
                   {"type": "label", "id": "homepage", "label": {"severity": "SEV2", "actionable": True,
                                                                  "team": None, "duplicate_of": None}}]
        live = build_demo.DemoSession()
        for a in actions:
            live.apply(a)
        self.assertEqual(build_demo.DemoSession(actions).render(), live.render())  # a reload
        fresh = build_demo.DemoSession().render()  # Start over
        self.assertEqual(fresh, build_demo.build())
        self.assertIn("2 alerts labeled so far", self.text(fresh))
        self.assertIn("Demo clock 14:04 UTC", self.text(fresh))

    def test_the_published_demo_is_current(self):
        docs = os.path.join(triage.BASE, "docs", "demo")
        with open(os.path.join(docs, "index.html"), encoding="utf-8") as f:
            self.assertEqual(f.read(), build_demo.build(), "run python3 build_demo.py")
        for name in build_demo.ENGINE_FILES:
            with open(os.path.join(triage.BASE, name), "rb") as a, open(os.path.join(docs, "engine", name), "rb") as b:
                self.assertEqual(a.read(), b.read(), f"docs/demo/engine/{name} is stale: run python3 build_demo.py")

    def test_replayed_wording_and_navigation(self):
        html = build_demo.build()
        text = self.text(html)
        self.assertIn(f"replayed from the real {build_demo.MODEL} run on 2026-09-23", text)
        self.assertIn("7 judged by Jev, 1 by rule, 0 by fallback", text)
        self.assertNotIn("scripted", text)
        for href in ('href="../"', 'href="../architecture.html"', 'href="https://github.com/mingleiw/jev-oncall"'):
            self.assertIn(href, html)
        # A real run still says Jev, with its cost.
        with mock.patch.object(triage, "_get_conn", side_effect=AssertionError):  # fake Jev only
            results, alerts = bundled_run()
        real = self.text(generate_dashboard.render(results, alerts))
        self.assertIn("answered by Jev", real)
        self.assertNotIn("scripted", real)

    def test_overrides_are_named_where_they_happen(self):
        html = build_demo.build()
        item = re.search(r'<div class="insp-item" data-id="payment".*?(?=<div class="insp-item"|$)',
                         html, re.S).group(0)
        self.assertIn("Policy override", item)
        self.assertIn("On its own, its P(page) says page. It joined incident checkout, and database owns "
                      "checkout, so compute gets a review", self.text(item))
        self.assertIn("root is owned by database, so compute gets a REVIEW", item)

    def test_the_homepage_instrument_matches_the_demo(self):
        """docs/index.html draws the demo's alerts by hand: keep it honest."""
        with open(os.path.join(triage.BASE, "docs", "index.html"), encoding="utf-8") as f:
            page = f.read()
        drawn = {m.group(1): (m.group(2), m.group(3)) for m in re.finditer(
            r'data-id="([^"]+)" data-p="([\d.]+)" data-final="([A-Z_]+)"', page)}
        session = build_demo.DemoSession()
        want = {a["id"]: (f'{triage.Judgment(**r["judgment"]).p_page:.2f}', r["action"])
                for a, r in session.runner.records if r["judgment"]}
        self.assertEqual(drawn, want)

    def test_the_demo_links_alerts_to_their_cards(self):
        html = build_demo.build()
        for a in build_demo.alerts():
            self.assertIn(f'data-id="{a["id"]}"', html)
        self.assertIn("a[href^=\"#alert-\"]", html)  # #alert-<id> links (and the homepage's) open the card
        self.assertIn('location.hash.indexOf("#alert-")', html)

    def test_chart_targets_are_44px_and_never_overlap(self):
        results, alerts = bundled_run()
        html = generate_dashboard.render(results, alerts)
        self.assertIn("--hit: 44px; --step: 44px;", html)
        self.assertIn("width: min(var(--hit), var(--w))", html)
        pins = re.findall(r'<a class="(pin[^"]*)"[^>]*style="([^"]*)"[^>]*aria-label="([^"]+)"', html)
        self.assertEqual(len(pins), sum(1 for r in results["alerts"] if r["judgment"]))
        for mode in "dtm":
            spots = set()
            for cls, style, label in pins:
                if f"hide-{mode}" in cls:
                    continue  # past the stack limit: counted in the "+n" label instead
                v = dict(re.findall(r"--(\w+):([\d.]+)%?", style))
                self.assertGreaterEqual(float(v[f"w{mode}"]) / 100, generate_dashboard.RAIL_GAPS[mode] - 1e-9)
                spot = (v[f"x{mode}"], v[f"k{mode}"])
                self.assertNotIn(spot, spots, (mode, label))  # one target per column and row
                spots.add(spot)
                self.assertTrue(label.endswith("Jump to the alert."))


if __name__ == "__main__":
    unittest.main()
