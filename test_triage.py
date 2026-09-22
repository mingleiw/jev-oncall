#!/usr/bin/env python3
"""Offline tests with a fake Jev: no API key, no network.

    python3 -m unittest -v
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import evaluate
import triage

P = triage.Policy()


def alert(aid, started="2026-09-18T14:00:00Z", service=None, env="prod", sev="critical"):
    return {"id": aid, "title": f"alert {aid}", "description": "", "service": service or f"svc-{aid}",
            "env": env, "started_at": started, "configured_severity": sev}


def judgment(p_act=0.95, sev=None, team=None, dup=None):
    sev = sev or {"SEV1": 0.9, "SEV2": 0.1}
    team = team or {"compute": 1.0}
    return triage.Judgment(
        model=triage.MODEL, p_actionable=p_act,
        severity={level: sev.get(level, 0.0) for level in triage.SEV_LEVELS},
        team={t: team.get(t, 0.0) for t in triage.TEAM_CRITERIA},
        duplicate_of=dup)


def response(p_act=0.95, sev=None, team=None, dup=None):
    """A Jev-shaped response body."""
    j = judgment(p_act, sev, team)
    answers = {
        "actionable": {"type": "noul", "noul": j.p_actionable},
        "severity": {"type": "score", "probabilities":
                     {str(i): j.severity[level] for i, level in enumerate(triage.SEV_LEVELS)}},
        "team": {"type": "choice", "choice": triage.top(j.team), "probabilities": j.team},
    }
    if dup is not None:
        answers["duplicate_of"] = {"type": "choice", "choice": triage.top(dup), "probabilities": dup}
    return {"model": triage.MODEL, "answers": answers,
            "usage": {"input_tokens": 900, "output_tokens": 120}}


class ParseAnswers(unittest.TestCase):
    def test_score_levels_run_low_to_high(self):
        j = triage.parse_answers(response(sev={"SEV1": 0.6, "SEV2": 0.3, "SEV3": 0.1}), [])
        self.assertAlmostEqual(j.p_page, 0.9)
        self.assertEqual(triage.top(j.severity), "SEV1")

    def test_duplicate_distribution_covers_every_candidate(self):
        j = triage.parse_answers(response(dup={"a01": 0.8, "none": 0.2}), ["a01", "a09"])
        self.assertEqual(j.duplicate_of, {"a01": 0.8, "a09": 0.0, "none": 0.2})

    def test_malformed_responses_raise(self):
        def broken(mutate):
            r = response()
            mutate(r["answers"])
            return r
        cases = [
            lambda a: a.pop("severity"),
            lambda a: a["team"]["probabilities"].update(marketing=0.1),
            lambda a: a["actionable"].update(noul=1.7),
            lambda a: a["severity"]["probabilities"].update({"7": 0.0}),
            lambda a: a["team"]["probabilities"].update(compute=0.2),  # sums to 0.2
        ]
        for mutate in cases:
            with self.assertRaises(triage.JevError):
                triage.parse_answers(broken(mutate), [])
        with self.assertRaises(triage.JevError):  # asked duplicate_of, got no answer
            triage.parse_answers(response(), ["a01"])

    def test_nan_probability_rejected(self):
        r = response()
        r["answers"]["actionable"]["noul"] = float("nan")
        with self.assertRaises(triage.JevError):
            triage.parse_answers(r, [])

    def test_inf_probability_rejected(self):
        r = response()
        r["answers"]["severity"]["probabilities"]["0"] = float("inf")
        with self.assertRaises(triage.JevError):
            triage.parse_answers(r, [])

    def test_choice_not_matching_max_rejected(self):
        r = response(dup={"a01": 0.8, "none": 0.2})
        r["answers"]["duplicate_of"]["choice"] = "none"
        with self.assertRaises(triage.JevError):
            triage.parse_answers(r, ["a01"])


class RouteStandalone(unittest.TestCase):
    def route(self, **kw):
        return triage.route_standalone(alert("a1"), judgment(**kw), P)

    def test_flat_severity_goes_to_review_not_page_now(self):  # v1 a06
        d = self.route(sev={"SEV1": 0.3, "SEV2": 0.2, "SEV3": 0.3, "SEV4": 0.2})
        self.assertEqual(d.action, "REVIEW")

    def test_distribution_not_top_label_decides(self):
        # The top label is SEV3, which argmax routing would ticket; P(page) is 0.6.
        self.assertEqual(self.route(sev={"SEV1": 0.3, "SEV2": 0.3, "SEV3": 0.4}).action, "REVIEW")

    def test_page_now_vs_page(self):
        self.assertEqual(self.route(sev={"SEV1": 0.5, "SEV2": 0.4, "SEV3": 0.1}).action, "PAGE_NOW")
        self.assertEqual(self.route(sev={"SEV1": 0.3, "SEV2": 0.6, "SEV3": 0.1}).action, "PAGE")

    def test_drop_needs_near_certainty(self):  # v1 a12
        self.assertEqual(self.route(p_act=0.30, sev={"SEV3": 0.9, "SEV2": 0.1}).action, "TICKET")
        self.assertEqual(self.route(p_act=0.02, sev={"SEV4": 0.9, "SEV3": 0.1}).action, "DROP")

    def test_actionable_cannot_drop_a_pageable_alert(self):
        d = self.route(p_act=0.02, sev={"SEV1": 0.9, "SEV2": 0.05, "SEV4": 0.05})
        self.assertEqual(d.action, "PAGE_NOW")
        self.assertTrue(any("disagree" in r for r in d.reasons))
        self.assertEqual(self.route(p_act=0.02, sev={"SEV2": 0.5, "SEV4": 0.5}).action, "REVIEW")

    def test_uncertain_owner_notifies_runner_up(self):
        d = self.route(team={"compute": 0.5, "database": 0.4, "network": 0.1})
        self.assertEqual((d.team, d.notify), ("compute", ["database"]))

    def test_non_prod_never_pages_and_needs_no_model(self):
        d = triage.route_standalone(alert("a1", env="staging"), None, P)
        self.assertEqual((d.action, d.source), ("LOG", "rule"))

    def test_fallback_uses_configured_severity_and_never_drops(self):
        for sev, action in (("critical", "PAGE"), ("warning", "TICKET"), ("info", "LOG"), ("", "PAGE")):
            d = triage.route_standalone(alert("a1", sev=sev), None, P, "HTTP 503")
            self.assertEqual((d.action, d.source), (action, "fallback"))


class Dedup(unittest.TestCase):
    def route_all(self, alerts, judgments):
        decisions = triage.route_all(alerts, judgments, {}, P)
        self.assertEqual(triage.check_invariants(decisions), [])
        return decisions

    def two(self):
        return [alert("a1", "2026-09-18T14:02:00Z"), alert("a2", "2026-09-18T14:03:00Z")]

    def test_links_only_above_the_bar(self):  # v1 a10: 0.65 meant a second page
        for p, action in ((0.65, "PAGE_NOW"), (0.75, "DEDUP")):
            j = {"a1": judgment(), "a2": judgment(dup={"a1": p, "none": 1 - p})}
            self.assertEqual(self.route_all(self.two(), j)["a2"].action, action)

    def test_mutual_dedup_still_pages_the_root(self):
        j = {"a1": judgment(dup={"a2": 0.9, "none": 0.1}), "a2": judgment(dup={"a1": 0.9, "none": 0.1})}
        d = self.route_all(self.two(), j)
        self.assertEqual((d["a1"].action, d["a2"].action, d["a2"].linked_to), ("PAGE_NOW", "DEDUP", "a1"))

    def test_three_way_cycle_breaks_at_the_earliest(self):
        alerts = [alert("a1", "2026-09-18T14:01:00Z"), alert("a2", "2026-09-18T14:02:00Z"),
                  alert("a3", "2026-09-18T14:03:00Z")]
        j = {"a1": judgment(dup={"a2": 0.9, "none": 0.1}), "a2": judgment(dup={"a3": 0.9, "none": 0.1}),
             "a3": judgment(dup={"a1": 0.9, "none": 0.1})}
        d = self.route_all(alerts, j)
        self.assertEqual([d[i].linked_to for i in ("a1", "a2", "a3")], [None, "a1", "a1"])

    def test_root_escalates_to_the_most_urgent_member(self):
        j = {"a1": judgment(sev={"SEV3": 1.0}), "a2": judgment(dup={"a1": 0.9, "none": 0.1})}
        d = self.route_all(self.two(), j)
        self.assertEqual((d["a1"].standalone, d["a1"].action, d["a2"].action),
                         ("TICKET", "PAGE_NOW", "DEDUP"))

    def test_other_team_gets_review_not_silence(self):
        j = {"a1": judgment(team={"deploy": 1.0}),
             "a2": judgment(team={"compute": 1.0}, dup={"a1": 0.9, "none": 0.1})}
        d = self.route_all(self.two(), j)
        self.assertEqual((d["a2"].action, d["a2"].linked_to), ("REVIEW", "a1"))

    def test_never_dedups_into_a_dropped_alert(self):
        j = {"a1": judgment(p_act=0.01, sev={"SEV4": 1.0}), "a2": judgment(dup={"a1": 0.9, "none": 0.1})}
        d = self.route_all(self.two(), j)
        self.assertEqual((d["a1"].action, d["a2"].action, d["a2"].linked_to), ("DROP", "PAGE_NOW", None))

    def test_invariants_catch_a_silenced_page(self):
        decisions = {
            "a1": triage.Decision("a1", "TICKET", "TICKET", "x", "jev"),
            "a2": triage.Decision("a2", "DEDUP", "PAGE_NOW", "x", "jev", linked_to="a1"),
            "a3": triage.Decision("a3", "DROP", "DROP", "x", "fallback"),
        }
        self.assertEqual(len(triage.check_invariants(decisions)), 2)


class Candidates(unittest.TestCase):
    def test_window_skew_env_and_cap(self):
        child = alert("c", "2026-09-18T14:00:00Z")
        others = [alert("early", "2026-09-18T13:31:00Z"),     # 29m before: in
                  alert("too_early", "2026-09-18T13:29:00Z"),  # 31m before: out
                  alert("skew", "2026-09-18T14:02:00Z"),      # 2m after: in
                  alert("late", "2026-09-18T14:03:00Z"),      # 3m after: out
                  alert("staging", "2026-09-18T13:59:00Z", env="staging")]
        ids = [c["id"] for c in triage.candidate_causes(child, [child, *others], {}, P)]
        self.assertEqual(ids, ["skew", "early"])  # nearest first
        capped = triage.candidate_causes(child, [child, *others], {}, triage.Policy(max_candidates=1))
        self.assertEqual([c["id"] for c in capped], ["skew"])

    def test_topology_limits_candidates_to_upstream(self):
        topology = {"payment-service": ["checkout-api"], "checkout-api": ["orders-db"]}
        pay, up, upup, other = (alert("pay", service="payment-service"), alert("up", service="checkout-api"),
                                alert("upup", service="orders-db"), alert("other", service="search-api"))
        everything = [pay, up, upup, other]
        self.assertEqual(sorted(c["id"] for c in triage.candidate_causes(pay, everything, topology, P)),
                         ["up", "upup"])
        # A service the topology doesn't list falls back to the time window alone.
        self.assertEqual(len(triage.candidate_causes(other, everything, topology, P)), 3)


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status = status
        self._body = json.dumps(body).encode() if body is not None else b""
        self._headers = headers or {}

    def read(self):
        return self._body

    def get(self, key, default=None):
        return self._headers.get(key, default)


class FakeConn:
    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0

    def request(self, method, path, body=None, headers=None):
        self.call_count += 1

    def getresponse(self):
        return self._responses.pop(0)

    def close(self):
        pass


class ProxySupport(unittest.TestCase):
    def _env(self, **kw):
        env = {k: v for k, v in os.environ.items()
               if "proxy" not in k.lower()}
        env.update(kw)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_direct_when_no_proxy_set(self):
        with self._env():
            conn = triage._get_conn(2.0)
        try:
            self.assertIsNone(conn._tunnel_host)
            self.assertEqual(conn.host, triage._API_HOST)
        finally:
            conn.close()

    def test_tunnels_through_https_proxy(self):
        with self._env(https_proxy="http://proxy.internal:3128"):
            conn = triage._get_conn(2.0)
        try:
            self.assertEqual(conn.host, "proxy.internal")
            self.assertEqual(conn.port, 3128)
            self.assertEqual(conn._tunnel_host, triage._API_HOST)
            self.assertEqual(conn._tunnel_port, 443)
        finally:
            conn.close()

    def test_proxy_auth_header_sent_on_tunnel(self):
        with self._env(https_proxy="http://user:pw@proxy.internal:3128"):
            conn = triage._get_conn(2.0)
        try:
            import base64 as _b64
            want = "Basic " + _b64.b64encode(b"user:pw").decode()
            self.assertEqual(conn._tunnel_headers.get("Proxy-Authorization"), want)
        finally:
            conn.close()

    def test_no_proxy_bypasses_proxy(self):
        with self._env(https_proxy="http://proxy.internal:3128",
                       no_proxy="api.typesafe.ai"):
            conn = triage._get_conn(2.0)
        try:
            self.assertIsNone(conn._tunnel_host)
            self.assertEqual(conn.host, triage._API_HOST)
        finally:
            conn.close()


@mock.patch("time.sleep")
class CallJev(unittest.TestCase):
    def _patch_pool(self, conn):
        return mock.patch.object(triage, "_get_conn", return_value=conn), \
               mock.patch.object(triage, "_put_conn")

    def test_retries_a_429_then_succeeds(self, sleep):
        conn = FakeConn([FakeResponse(429, headers={"Retry-After": "0.1"}),
                         FakeResponse(200, {"ok": 1})])
        get_patch, put_patch = self._patch_pool(conn)
        with get_patch, put_patch:
            data, _ = triage.call_jev({}, "key", retries=1)
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(conn.call_count, 2)
        sleep.assert_called_once_with(0.1)

    def test_client_errors_are_not_retried(self, sleep):
        conn = FakeConn([FakeResponse(400)])
        get_patch, put_patch = self._patch_pool(conn)
        with get_patch, put_patch:
            with self.assertRaises(triage.JevError):
                triage.call_jev({}, "key", retries=3)
        self.assertEqual(conn.call_count, 1)

    def test_falls_back_rather_than_wait_out_a_long_retry_after(self, sleep):
        conn = FakeConn([FakeResponse(429, headers={"Retry-After": "30"})])
        get_patch, put_patch = self._patch_pool(conn)
        with get_patch, put_patch:
            with self.assertRaises(triage.JevError):
                triage.call_jev({}, "key", retries=3, max_wait_s=1.0)
        sleep.assert_not_called()

    def test_network_errors_exhaust_retries(self, sleep):
        def fail_conn(timeout):
            c = mock.Mock()
            c.request.side_effect = OSError("down")
            return c
        with mock.patch.object(triage, "_get_conn", side_effect=fail_conn), \
             mock.patch.object(triage, "_put_conn"):
            with self.assertRaises(triage.JevError):
                triage.call_jev({}, "key", retries=2)

    def test_connection_reused_on_success(self, sleep):
        conn = FakeConn([FakeResponse(200, {"ok": 1})])
        put_calls = []
        with mock.patch.object(triage, "_get_conn", return_value=conn), \
             mock.patch.object(triage, "_put_conn", side_effect=lambda c: put_calls.append(c)):
            triage.call_jev({}, "key")
        self.assertEqual(len(put_calls), 1)
        self.assertIs(put_calls[0], conn)


def label_oracle(alerts):
    """A fake call_jev that answers from each alert's labels, with some spread."""
    by_title = {a["title"]: a for a in alerts}

    def call(payload, *args, **kwargs):
        e = by_title[payload["state"]["alert"]["title"]]["expected"]
        i = triage.SEV_LEVELS.index(e["severity"])
        neighbors = [triage.SEV_LEVELS[n] for n in (i - 1, i + 1) if 0 <= n < len(triage.SEV_LEVELS)]
        sev = {e["severity"]: 0.85, **{n: 0.15 / len(neighbors) for n in neighbors}}
        other_team = next(t for t in triage.TEAM_CRITERIA if t != e["team"])
        options = payload["questions"].get("duplicate_of", {}).get("criteria")
        dup = None
        if options:
            cause = e["duplicate_of"] if e["duplicate_of"] in options else triage.NONE
            dup = {k: 0.9 if k == cause else 0.1 / (len(options) - 1) for k in options}
        body = response(0.97 if e["actionable"] else 0.03, sev, {e["team"]: 0.9, other_team: 0.1}, dup)
        return body, 400.0
    return call


class EndToEnd(unittest.TestCase):
    path = os.path.join(triage.BASE, "alerts.json")

    def run_main(self, env, **patches):
        alerts = triage.load_alerts(self.path)
        environ = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
        environ.update(env)
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, environ, clear=True), \
                redirect_stdout(out), redirect_stderr(err):
            results_path = os.path.join(tmp, "results.json")
            with mock.patch.object(triage, "call_jev", side_effect=label_oracle(alerts)):
                code = triage.main(["--alerts", self.path, "--out", results_path])
            results = triage.load_json(results_path)
            with redirect_stdout(io.StringIO()) as sweep_out:
                evaluate.main([results_path, "--sweep"])
        return code, results, out.getvalue(), sweep_out.getvalue()

    def test_repo_alerts_with_a_label_oracle(self):
        code, results, out, sweep = self.run_main({"TYPESAFE_API_KEY": "test"})
        self.assertEqual(code, 0)
        d = {r["id"]: r for r in results["alerts"]}
        self.assertEqual((d["a02"]["action"], d["a02"]["linked_to"]), ("DEDUP", "a01"))
        self.assertEqual((d["a10"]["action"], d["a10"]["linked_to"]), ("DEDUP", "a09"))
        self.assertEqual({d["a07"]["action"], d["a13"]["action"]}, {"LOG"})
        self.assertEqual(d["a12"]["action"], "TICKET")
        self.assertEqual(d["a08"]["action"], "DROP")
        self.assertEqual(d["a02"]["candidates"], ["a01"])  # topology did the narrowing
        self.assertIsNotNone(d["a01"]["judgment"])
        self.assertIn("silent misses", out)
        self.assertIn("Threshold sweep", sweep)
        self.assertEqual(results["summary"]["invariant_violations"], [])

    def test_no_api_key_takes_the_fail_open_path(self):
        code, results, _, _ = self.run_main({})
        self.assertEqual(code, 0)
        sources = {r["source"] for r in results["alerts"]}
        self.assertEqual(sources, {"fallback", "rule"})
        self.assertNotIn("DROP", {r["action"] for r in results["alerts"]})


class Stats(unittest.TestCase):
    def test_wilson_interval(self):
        lo, hi = evaluate.wilson(10, 14)
        self.assertAlmostEqual(lo, 0.454, places=2)
        self.assertAlmostEqual(hi, 0.883, places=2)

    def test_perfect_calibration_has_zero_ece(self):
        _, ece, _ = evaluate.calibration([(0.0, 0), (1.0, 1)], 5)
        self.assertEqual(ece, 0.0)


if __name__ == "__main__":
    unittest.main()
