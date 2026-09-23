#!/usr/bin/env python3
"""Tests for the webhook adapter (server.py).

    python3 -m unittest test_server -v
"""
import hashlib
import hmac
import io
import json
import os
import threading
import time
import unittest
from http.client import HTTPConnection
from unittest import mock

import server
import test_triage
import triage


def post(conn, path, body):
    raw = json.dumps(body).encode()
    conn.request("POST", path, body=raw, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read())


class NormalizerTests(unittest.TestCase):

    # -- generic --

    def test_generic_single(self):
        alerts = server.normalize_generic({"id": "a1", "title": "down"})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["id"], "a1")
        self.assertEqual(alerts[0]["env"], "prod")

    def test_generic_list(self):
        alerts = server.normalize_generic([
            {"id": "a1", "title": "x"},
            {"id": "a2", "title": "y"},
        ])
        self.assertEqual(len(alerts), 2)

    def test_generic_missing_id(self):
        with self.assertRaises(ValueError):
            server.normalize_generic({"title": "no id"})

    def test_generic_missing_title(self):
        with self.assertRaises(ValueError):
            server.normalize_generic({"id": "a1"})

    # -- datadog --

    def test_datadog_basic(self):
        body = {
            "id": 12345,
            "title": "CPU > 90%",
            "body": "Host xyz is hot",
            "tags": "service:payment-api,env:staging",
            "priority": "P2",
            "date": "2024-01-15T10:30:00Z",
        }
        alerts = server.normalize_datadog(body)
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertEqual(a["title"], "CPU > 90%")
        self.assertEqual(a["service"], "payment-api")
        self.assertEqual(a["env"], "staging")
        self.assertEqual(a["configured_severity"], "critical")
        self.assertIn("Host xyz", a["description"])

    def test_datadog_tags_as_list(self):
        body = {"id": 1, "title": "x", "tags": ["service:web", "env:prod"]}
        a = server.normalize_datadog(body)[0]
        self.assertEqual(a["service"], "web")
        self.assertEqual(a["env"], "prod")

    def test_datadog_low_priority(self):
        body = {"id": 1, "title": "x", "priority": "P4"}
        a = server.normalize_datadog(body)[0]
        self.assertEqual(a["configured_severity"], "info")

    def test_datadog_alert_type_override(self):
        body = {"id": 1, "title": "x", "alert_type": "warning"}
        a = server.normalize_datadog(body)[0]
        self.assertEqual(a["configured_severity"], "warning")

    # -- pagerduty --

    def test_pagerduty_v2_webhook(self):
        body = {
            "messages": [{
                "event": {
                    "data": {
                        "id": "P123ABC",
                        "title": "Disk full on db-primary",
                        "description": "Usage at 98%",
                        "urgency": "high",
                        "service": {"name": "database"},
                        "created_at": "2024-01-15T10:30:00Z",
                        "html_url": "https://example.pagerduty.com/incidents/P123ABC",
                    }
                }
            }]
        }
        alerts = server.normalize_pagerduty(body)
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertEqual(a["title"], "Disk full on db-primary")
        self.assertEqual(a["service"], "database")
        self.assertEqual(a["configured_severity"], "critical")
        self.assertIn("98%", a["description"])

    def test_pagerduty_v3_webhook(self):
        body = {
            "event": {
                "data": {
                    "id": "P456",
                    "title": "Memory leak",
                    "urgency": "low",
                    "service": {"summary": "api-server"},
                }
            }
        }
        alerts = server.normalize_pagerduty(body)
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertEqual(a["configured_severity"], "warning")
        self.assertEqual(a["service"], "api-server")

    def test_pagerduty_severity_field(self):
        body = {
            "messages": [{
                "event": {
                    "data": {
                        "id": "P789",
                        "title": "Info alert",
                        "severity": "info",
                        "service": {},
                    }
                }
            }]
        }
        a = server.normalize_pagerduty(body)[0]
        self.assertEqual(a["configured_severity"], "info")

    # -- grafana --

    def test_grafana_unified_alerting(self):
        body = {
            "alerts": [{
                "fingerprint": "abc123",
                "labels": {
                    "alertname": "HighErrorRate",
                    "service": "checkout",
                    "severity": "critical",
                    "env": "production",
                },
                "annotations": {
                    "summary": "Error rate above 5%",
                    "description": "Checkout error rate is 8.2%",
                },
                "startsAt": "2024-01-15T10:30:00Z",
                "valueString": "[ var='B' labels={} value=8.2 ]",
            }]
        }
        alerts = server.normalize_grafana(body)
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertEqual(a["title"], "HighErrorRate")
        self.assertEqual(a["service"], "checkout")
        self.assertEqual(a["env"], "production")
        self.assertEqual(a["configured_severity"], "critical")
        self.assertIn("8.2%", a["description"])
        self.assertIn("Value:", a["description"])

    def test_grafana_legacy_single(self):
        body = {
            "title": "Panel alert",
            "message": "Something is wrong",
            "panelId": 42,
        }
        alerts = server.normalize_grafana(body)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["title"], "Panel alert")

    def test_grafana_warning_severity(self):
        body = {
            "alerts": [{
                "labels": {"alertname": "SlowQuery", "severity": "warning"},
                "startsAt": "2024-01-15T10:30:00Z",
            }]
        }
        a = server.normalize_grafana(body)[0]
        self.assertEqual(a["configured_severity"], "warning")

    # -- stable IDs --

    def test_stable_ids_are_deterministic(self):
        a = server._stable_id("datadog", "12345")
        b = server._stable_id("datadog", "12345")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 12)

    def test_stable_ids_differ_across_providers(self):
        a = server._stable_id("datadog", "123")
        b = server._stable_id("pagerduty", "123")
        self.assertNotEqual(a, b)


class ValidationTests(unittest.TestCase):
    def test_valid_alert_passes(self):
        a = server.validate_alert({"id": "x1", "title": "down", "configured_severity": "critical",
                                   "started_at": "2024-01-15T10:30:00Z"})
        self.assertEqual(a["id"], "x1")
        self.assertEqual(a["configured_severity"], "critical")

    def test_invalid_severity_defaults_to_critical(self):
        a = server.validate_alert({"id": "x1", "title": "down", "configured_severity": "banana"})
        self.assertEqual(a["configured_severity"], "critical")

    def test_invalid_timestamp_replaced(self):
        a = server.validate_alert({"id": "x1", "title": "down", "started_at": "not-a-date"})
        self.assertRegex(a["started_at"], r"^\d{4}-\d{2}-\d{2}T")

    def test_long_fields_clamped(self):
        a = server.validate_alert({"id": "x1", "title": "a" * 5000, "description": "b" * 10000})
        self.assertLessEqual(len(a["title"]), server.MAX_FIELD_LEN)
        self.assertLessEqual(len(a["description"]), 5000)

    def test_empty_id_rejected(self):
        with self.assertRaises(ValueError):
            server.validate_alert({"id": "", "title": "down"})

    def test_empty_title_rejected(self):
        with self.assertRaises(ValueError):
            server.validate_alert({"id": "x1", "title": "  "})


class SignatureTests(unittest.TestCase):
    BODY = b'{"id":"x","title":"y"}'
    SECRET = "topsecret"

    def _hex(self, body=None, secret=None):
        return hmac.new((secret or self.SECRET).encode(),
                        body or self.BODY, hashlib.sha256).hexdigest()

    def test_no_secret_configured_allows_everything(self):
        self.assertTrue(server.verify_signature("generic", self.BODY, {}, None))

    def test_missing_header_is_rejected(self):
        self.assertFalse(server.verify_signature("generic", self.BODY, {}, self.SECRET))

    def test_generic_accepts_sha256_prefix_and_bare_hex(self):
        for sent in (f"sha256={self._hex()}", self._hex()):
            self.assertTrue(server.verify_signature(
                "generic", self.BODY, {"X-Jev-Signature": sent}, self.SECRET))

    def test_wrong_secret_is_rejected(self):
        bad = self._hex(secret="wrong")
        self.assertFalse(server.verify_signature(
            "generic", self.BODY, {"X-Jev-Signature": bad}, self.SECRET))

    def test_tampered_body_is_rejected(self):
        sig = self._hex()
        self.assertFalse(server.verify_signature(
            "generic", b'{"id":"evil"}', {"X-Jev-Signature": sig}, self.SECRET))

    def test_pagerduty_v1_format(self):
        h = {"X-PagerDuty-Signature": f"v1={self._hex()}"}
        self.assertTrue(server.verify_signature("pagerduty", self.BODY, h, self.SECRET))

    def test_pagerduty_accepts_any_v1_during_key_rotation(self):
        h = {"X-PagerDuty-Signature": f"v1=dead{'0' * 60},v1={self._hex()}"}
        self.assertTrue(server.verify_signature("pagerduty", self.BODY, h, self.SECRET))

    def test_pagerduty_ignores_non_v1_elements(self):
        # A caller must not be able to downgrade the scheme by offering one.
        h = {"X-PagerDuty-Signature": f"v0={self._hex()}"}
        self.assertFalse(server.verify_signature("pagerduty", self.BODY, h, self.SECRET))

    def test_grafana_bare_hex_header(self):
        h = {"X-Grafana-Alerting-Signature": self._hex()}
        self.assertTrue(server.verify_signature("grafana", self.BODY, h, self.SECRET))

    def test_a_providers_signature_does_not_work_on_another(self):
        h = {"X-Jev-Signature": self._hex()}
        self.assertFalse(server.verify_signature("pagerduty", self.BODY, h, self.SECRET))


class ReviewQueueTests(unittest.TestCase):
    def _entry(self, alert_id="r1"):
        return {"id": alert_id, "title": "t", "team": "compute",
                "action": "REVIEW", "reasons": ["unsure"]}

    def test_a_review_is_pending_until_acked(self):
        q = server.ReviewQueue(ack_minutes=15)
        q.add(self._entry())
        self.assertEqual(len(q.pending()), 1)
        self.assertIsNotNone(q.ack("r1", "alice"))
        self.assertEqual(len(q.pending()), 0)

    def test_acking_an_unknown_alert_reports_nothing(self):
        q = server.ReviewQueue(ack_minutes=15)
        self.assertIsNone(q.ack("nope"))

    def test_nothing_escalates_before_the_deadline(self):
        q = server.ReviewQueue(ack_minutes=15)
        q.add(self._entry())
        self.assertEqual(q.sweep(), [])
        self.assertEqual(len(q.pending()), 1)

    def test_an_unacked_review_pages(self):
        q = server.ReviewQueue(ack_minutes=15)
        now = time.monotonic()
        q.add(self._entry(), now=now)
        escalated = q.sweep(now=now + 15 * 60 + 1)
        self.assertEqual(len(escalated), 1)
        self.assertEqual(escalated[0]["action"], "PAGE")
        self.assertEqual(escalated[0]["escalated_from"], "REVIEW")
        self.assertIn("nobody acked within 15m", " ".join(escalated[0]["reasons"]))
        self.assertEqual(len(q.pending()), 0)

    def test_an_acked_review_never_pages(self):
        q = server.ReviewQueue(ack_minutes=15)
        now = time.monotonic()
        q.add(self._entry(), now=now)
        q.ack("r1", "alice")
        self.assertEqual(q.sweep(now=now + 15 * 60 + 1), [])

    def test_pending_reports_time_left(self):
        q = server.ReviewQueue(ack_minutes=15)
        now = time.monotonic()
        q.add(self._entry(), now=now)
        left = q.pending(now=now)[0]["seconds_left"]
        self.assertAlmostEqual(left, 900, delta=1)

    def test_runner_escalation_lands_in_recent_and_stats(self):
        runner = server.TriageRunner(topology={}, api_key=None)
        runner.reviews.add(self._entry(), now=time.monotonic() - 10 ** 6)
        escalated = runner.sweep_reviews()
        self.assertEqual(len(escalated), 1)
        stats = runner.stats()
        self.assertEqual(stats["escalated_reviews"], 1)
        self.assertEqual(stats["pending_reviews"], 0)
        self.assertEqual(stats["decisions"][-1]["action"], "PAGE")


class RateLimitTests(unittest.TestCase):
    def test_allows_up_to_limit(self):
        limiter = server.RateLimiter(max_per_minute=3)
        self.assertTrue(limiter.allow())
        self.assertTrue(limiter.allow())
        self.assertTrue(limiter.allow())
        self.assertFalse(limiter.allow())

    def test_window_expires(self):
        limiter = server.RateLimiter(max_per_minute=1)
        limiter.allow()
        limiter._timestamps[0] = time.monotonic() - 61
        self.assertTrue(limiter.allow())


class StalenessTests(unittest.TestCase):
    def test_stale_alerts_pruned(self):
        runner = server.TriageRunner(topology={}, api_key=None)
        runner._active_alerts = [{"id": "old", "title": "old"}]
        runner._alert_times = {"old": time.monotonic() - server.ALERT_TTL_S - 1}
        runner._prune_stale()
        self.assertEqual(len(runner._active_alerts), 0)

    def test_fresh_alerts_kept(self):
        runner = server.TriageRunner(topology={}, api_key=None)
        runner._active_alerts = [{"id": "new", "title": "new"}]
        runner._alert_times = {"new": time.monotonic()}
        runner._prune_stale()
        self.assertEqual(len(runner._active_alerts), 1)


class HTTPTests(unittest.TestCase):
    """Spin up the server on a random port and test the HTTP layer."""

    @classmethod
    def setUpClass(cls):
        cls.runner = server.TriageRunner(topology={}, api_key=None, rate_limit=1000)
        server.Handler.runner = cls.runner
        from http.server import HTTPServer
        cls.httpd = HTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _conn(self):
        return HTTPConnection("127.0.0.1", self.port, timeout=5)

    def test_health(self):
        conn = self._conn()
        conn.request("GET", "/health")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.read())
        self.assertTrue(body["ok"])

    def test_recent_returns_valid_response(self):
        conn = self._conn()
        conn.request("GET", "/recent")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.read())
        self.assertIn("decisions", body)
        self.assertIn("count", body)
        self.assertEqual(body["count"], len(body["decisions"]))

    def test_404_on_unknown_path(self):
        conn = self._conn()
        conn.request("GET", "/nope")
        self.assertEqual(conn.getresponse().status, 404)

    def test_unknown_provider(self):
        conn = self._conn()
        status, body = post(conn, "/ingest/opsgenie", {"id": "x"})
        self.assertEqual(status, 400)
        self.assertIn("unknown provider", body["error"])

    def test_invalid_json(self):
        conn = self._conn()
        conn.request("POST", "/ingest/generic", body=b"not json",
                     headers={"Content-Type": "application/json",
                              "Content-Length": "8"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)

    def test_generic_ingest_fail_open(self):
        conn = self._conn()
        alert = {"id": "test-1", "title": "test alert",
                 "service": "api", "configured_severity": "critical"}
        status, body = post(conn, "/ingest/generic", alert)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["decisions"]), 1)
        d = body["decisions"][0]
        self.assertEqual(d["id"], "test-1")
        self.assertIn(d["action"], ("PAGE", "PAGE_NOW", "REVIEW", "TICKET", "DROP"))
        self.assertEqual(d["source"], "fallback")

    def test_ingest_bare_path_defaults_to_generic(self):
        conn = self._conn()
        alert = {"id": "test-bare", "title": "bare path"}
        status, body = post(conn, "/ingest", alert)
        self.assertEqual(status, 200)
        self.assertEqual(body["decisions"][0]["id"], "test-bare")

    def test_datadog_ingest(self):
        conn = self._conn()
        body = {"id": 999, "title": "CPU spike", "tags": "service:web"}
        status, resp = post(conn, "/ingest/datadog", body)
        self.assertEqual(status, 200)
        self.assertEqual(len(resp["decisions"]), 1)

    def test_normalization_error_returns_422(self):
        conn = self._conn()
        status, body = post(conn, "/ingest/generic", {"no_id_or_title": True})
        self.assertEqual(status, 422)
        self.assertIn("normalization failed", body["error"])

    def test_recent_fills_after_ingest(self):
        conn = self._conn()
        alert = {"id": "recent-1", "title": "for recent"}
        post(conn, "/ingest/generic", alert)
        conn2 = self._conn()
        conn2.request("GET", "/recent")
        body = json.loads(conn2.getresponse().read())
        ids = [d["id"] for d in body["decisions"]]
        self.assertIn("recent-1", ids)

    def test_recent_includes_latency_percentiles(self):
        conn = self._conn()
        conn.request("GET", "/recent")
        body = json.loads(conn.getresponse().read())
        for key in ("latency_ms_p50", "latency_ms_p95", "latency_ms_p99", "active_alerts"):
            self.assertIn(key, body)

    def test_validation_rejects_empty_id_over_http(self):
        conn = self._conn()
        status, body = post(conn, "/ingest/generic", {"id": "", "title": "x"})
        self.assertEqual(status, 422)
        self.assertIn("validation failed", body["error"])

    def test_invalid_severity_normalized_over_http(self):
        conn = self._conn()
        alert = {"id": "sev-test", "title": "bad sev", "configured_severity": "banana"}
        status, body = post(conn, "/ingest/generic", alert)
        self.assertEqual(status, 200)

    def test_ack_clears_a_pending_review_over_http(self):
        self.runner.reviews.add({"id": "http-ack", "title": "t", "team": "compute",
                                 "action": "REVIEW", "reasons": []})
        conn = self._conn()
        conn.request("GET", "/pending")
        body = json.loads(conn.getresponse().read())
        self.assertIn("http-ack", [p["id"] for p in body["pending"]])

        conn2 = self._conn()
        conn2.request("POST", "/ack/http-ack", body=b"",
                      headers={"Content-Length": "0", "X-Acked-By": "alice"})
        resp = conn2.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.read())["acked_by"], "alice")

        conn3 = self._conn()
        conn3.request("GET", "/pending")
        body = json.loads(conn3.getresponse().read())
        self.assertNotIn("http-ack", [p["id"] for p in body["pending"]])

    def test_acking_an_unknown_review_is_404(self):
        conn = self._conn()
        conn.request("POST", "/ack/never-seen", body=b"", headers={"Content-Length": "0"})
        self.assertEqual(conn.getresponse().status, 404)

    def test_ingest_without_a_signature_is_401_when_a_secret_is_set(self):
        old = self.runner.secret
        self.runner.secret = "topsecret"
        try:
            conn = self._conn()
            status, body = post(conn, "/ingest/generic", {"id": "s1", "title": "x"})
            self.assertEqual(status, 401)
            self.assertIn("signature", body["error"])
        finally:
            self.runner.secret = old

    def test_a_correctly_signed_ingest_is_accepted(self):
        old = self.runner.secret
        self.runner.secret = "topsecret"
        try:
            raw = json.dumps({"id": "s2", "title": "signed"}).encode()
            sig = hmac.new(b"topsecret", raw, hashlib.sha256).hexdigest()
            conn = self._conn()
            conn.request("POST", "/ingest/generic", body=raw,
                         headers={"Content-Type": "application/json",
                                  "Content-Length": str(len(raw)),
                                  "X-Jev-Signature": f"sha256={sig}"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read())["decisions"][0]["id"], "s2")
        finally:
            self.runner.secret = old

    def test_rate_limit_returns_429(self):
        limited_runner = server.TriageRunner(topology={}, api_key=None, rate_limit=1)
        old_runner = server.Handler.runner
        server.Handler.runner = limited_runner
        try:
            conn = self._conn()
            post(conn, "/ingest/generic", {"id": "rl1", "title": "first"})
            conn2 = self._conn()
            status, body = post(conn2, "/ingest/generic", {"id": "rl2", "title": "second"})
            self.assertEqual(status, 429)
        finally:
            server.Handler.runner = old_runner



def am_payload(status="firing", fingerprint="fp1", starts="2026-09-18T14:03:00Z", **labels):
    """An Alertmanager v4 webhook body carrying one alert."""
    labels = {"alertname": "HighErrorRate", "severity": "critical",
              "service": "checkout-api", "instance": "10.0.0.7:9090", **labels}
    return {"version": "4", "status": status, "receiver": "jev-oncall",
            "groupKey": "{}:{alertname=\"HighErrorRate\"}", "truncatedAlerts": 0,
            "groupLabels": {"alertname": "HighErrorRate"}, "commonLabels": labels,
            "commonAnnotations": {}, "externalURL": "http://am:9093",
            "alerts": [{"status": status, "labels": labels,
                        "annotations": {"summary": "5xx above 5%",
                                        "description": "error ratio 0.31 over 5m"},
                        "startsAt": starts, "endsAt": "0001-01-01T00:00:00Z",
                        "generatorURL": "http://prom:9090/graph?g0.expr=x",
                        "fingerprint": fingerprint}]}


class AlertmanagerNormalizerTests(unittest.TestCase):
    def test_maps_labels_and_annotations(self):
        a = server.normalize_alertmanager(am_payload())[0]
        self.assertEqual(a["title"], "HighErrorRate: 5xx above 5%")
        self.assertEqual(a["service"], "checkout-api")
        self.assertEqual(a["env"], "prod")
        self.assertEqual(a["configured_severity"], "critical")
        self.assertEqual(a["started_at"], "2026-09-18T14:03:00Z")
        self.assertIn("error ratio 0.31", a["description"])
        self.assertIn("instance=10.0.0.7:9090", a["description"])
        self.assertIn("http://prom:9090", a["description"])
        self.assertNotIn("resolved", a)
        server.validate_alert(a)

    def test_severity_env_and_service_fallbacks(self):
        a = server.normalize_alertmanager(am_payload(severity="warning", env="staging",
                                                     service=""))[0]
        self.assertEqual((a["configured_severity"], a["env"]), ("warning", "staging"))
        body = am_payload()
        body["alerts"][0]["labels"] = {"alertname": "Down", "job": "node", "severity": "p9"}
        a = server.normalize_alertmanager(body)[0]
        self.assertEqual(a["service"], "node")
        self.assertEqual(a["configured_severity"], "critical")  # unknown pages
        self.assertEqual(a["title"], "Down: 5xx above 5%")

    def test_ids_are_one_per_firing(self):
        firing = server.normalize_alertmanager(am_payload())[0]["id"]
        resolved = server.normalize_alertmanager(am_payload("resolved"))[0]
        self.assertEqual(resolved["id"], firing)
        self.assertTrue(resolved["resolved"])
        refire = server.normalize_alertmanager(am_payload(starts="2026-09-18T16:00:00Z"))
        self.assertNotEqual(refire[0]["id"], firing)
        other = server.normalize_alertmanager(am_payload(fingerprint="fp2"))
        self.assertNotEqual(other[0]["id"], firing)

    def test_rejects_a_body_without_alerts(self):
        with self.assertRaises(ValueError):
            server.normalize_alertmanager({"status": "firing"})

    def test_grafana_resolved_is_marked(self):
        body = {"alerts": [{"status": "resolved", "labels": {"alertname": "x"},
                            "fingerprint": "f"}]}
        self.assertTrue(server.normalize_grafana(body)[0]["resolved"])
        legacy = {"title": "x", "state": "ok"}
        self.assertTrue(server.normalize_grafana(legacy)[0]["resolved"])


class AlertmanagerAuthTests(unittest.TestCase):
    def test_bearer_token(self):
        ok = server.verify_signature("alertmanager", b"{}", {"Authorization": "Bearer s3"}, "s3")
        self.assertTrue(ok)
        for sent in ("Bearer wrong", "Basic s3", "s3", ""):
            self.assertFalse(server.verify_signature(
                "alertmanager", b"{}", {"Authorization": sent}, "s3"), sent)


class ReviewDeadlineTests(unittest.TestCase):
    def test_a_repeated_review_keeps_its_first_deadline(self):
        q = server.ReviewQueue(ack_minutes=15)
        now = time.monotonic()
        q.add({"id": "r1", "title": "t"}, now=now)
        q.add({"id": "r1", "title": "t"}, now=now + 10 * 60)
        self.assertEqual(len(q.sweep(now=now + 15 * 60 + 1)), 1)


class AlertmanagerFlowTests(unittest.TestCase):
    """Firing, group resend, and resolve through the real HTTP handler."""

    def setUp(self):
        from http.server import HTTPServer
        self.runner = server.TriageRunner(topology={}, api_key="k", rate_limit=1000)
        self.old_runner = getattr(server.Handler, "runner", None)
        server.Handler.runner = self.runner
        self.httpd = HTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        review = test_triage.judgment(sev={"SEV2": 0.5, "SEV3": 0.5})
        self.judge = mock.patch.object(
            triage, "judge_all",
            side_effect=lambda alerts, *a, **k: ({x["id"]: review for x in alerts}, {}, {}))
        self.judge_mock = self.judge.start()

    def tearDown(self):
        self.judge.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        server.Handler.runner = self.old_runner

    def send(self, body):
        conn = HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        with mock.patch("sys.stderr", io.StringIO()):
            return post(conn, "/ingest/alertmanager", body)

    def test_resend_is_not_rejudged_and_resolve_cancels_the_review(self):
        status, first = self.send(am_payload())
        self.assertEqual(status, 200)
        self.assertEqual(first["decisions"][0]["action"], "REVIEW")
        aid = first["decisions"][0]["id"]
        self.assertEqual(len(self.runner.reviews.pending()), 1)

        _, resend = self.send(am_payload())
        self.assertEqual((resend["decisions"], resend["repeats"]), ([], [aid]))
        self.assertEqual(self.judge_mock.call_count, 1)

        _, cleared = self.send(am_payload("resolved"))
        self.assertEqual(cleared["resolved"], [aid])
        self.assertEqual(cleared["reviews_cancelled"][0]["acked_by"], "resolved upstream")
        self.assertEqual(self.runner.reviews.pending(), [])
        self.assertEqual(self.judge_mock.call_count, 1)

    def test_other_providers_still_rejudge_a_resend(self):
        conn = HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        with mock.patch("sys.stderr", io.StringIO()):
            post(conn, "/ingest/generic", {"id": "g1", "title": "t"})
            post(conn, "/ingest/generic", {"id": "g1", "title": "t"})
        self.assertEqual(self.judge_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
