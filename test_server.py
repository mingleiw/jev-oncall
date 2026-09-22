#!/usr/bin/env python3
"""Tests for the webhook adapter (server.py).

    python3 -m unittest test_server -v
"""
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


class HTTPTests(unittest.TestCase):
    """Spin up the server on a random port and test the HTTP layer."""

    @classmethod
    def setUpClass(cls):
        cls.runner = server.TriageRunner(topology={}, api_key=None)
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


if __name__ == "__main__":
    unittest.main()
