#!/usr/bin/env python3
"""Config loading tests: no API key, no network.

    python3 -m unittest test_config -v
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import server
import triage

BASE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(BASE, "jev-oncall.example.toml")


class LoadConfig(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        return triage.load_config(f.name)

    def assertRejected(self, text, fragment):
        with self.assertRaises(triage.ConfigError) as cm:
            self.load(text)
        self.assertIn(fragment, str(cm.exception))

    def test_no_path_is_the_builtin_defaults(self):
        c = triage.load_config(None)
        self.assertEqual(c.policy, triage.Policy())
        self.assertEqual(c.teams, triage.TEAM_CRITERIA)
        self.assertEqual(c.model, triage.MODEL)
        self.assertIsNone(c.topology)

    def test_example_matches_the_builtin_defaults(self):
        # The example documents the defaults; if they drift, the docs lie.
        c = triage.load_config(EXAMPLE)
        d = triage.Config()
        self.assertEqual((c.model, c.timeout, c.retries, c.max_wait, c.policy, c.teams),
                         (d.model, d.timeout, d.retries, d.max_wait, d.policy, d.teams))
        self.assertEqual(c.topology, triage.load_json(os.path.join(BASE, "topology.json")))

    def test_partial_file_keeps_other_defaults(self):
        c = self.load('[policy]\npage_bar = 0.9\n\n[teams]\npayments = "billing"\n'
                      'identity = "login, SSO"\n')
        self.assertEqual(c.policy.page_bar, 0.9)
        self.assertEqual(c.policy.no_page_bar, triage.Policy().no_page_bar)
        self.assertEqual(list(c.teams), ["payments", "identity"])
        self.assertEqual(c.model, triage.MODEL)

    def test_typos_are_errors(self):
        self.assertRejected("[policy]\npage_barr = 0.9\n", "page_barr")
        self.assertRejected("[jev]\ntimout = 5\n", "timout")
        self.assertRejected("[polcy]\n", "polcy")

    def test_bad_values_are_errors(self):
        self.assertRejected("[policy]\npage_bar = 1.5\n", "between 0 and 1")
        self.assertRejected("[policy]\nno_page_bar = 0.9\n", "below")
        self.assertRejected("[policy]\npage_bar = \"high\"\n", "page_bar must be a float")
        self.assertRejected("[policy]\nmax_candidates = 10.5\n", "max_candidates must be a int")
        self.assertRejected("[policy]\nmax_candidates = 255\n", "1-254")
        self.assertRejected("[jev]\ntimeout = -1\n", "non-negative")
        self.assertRejected('[teams]\nsolo = "everything"\n', "2 to 255")
        self.assertRejected('[teams]\na = "x"\nb = ""\n', "description")
        self.assertRejected('[topology]\napi = "db"\n', "upstream")
        self.assertRejected("not toml at all [", "")


class ConfiguredTeams(unittest.TestCase):
    TEAMS = {"payments": "billing and checkout", "identity": "login, SSO"}

    def test_teams_reach_the_payload_and_the_parser(self):
        payload = triage.build_payload({"id": "a", "title": "t"}, [], teams=self.TEAMS)
        self.assertEqual(payload["questions"]["team"]["criteria"], self.TEAMS)
        resp = {"model": triage.MODEL, "answers": {
            "actionable": {"noul": 0.9},
            "severity": {"probabilities": {"0": 0.1, "1": 0.1, "2": 0.2, "3": 0.6}},
            "team": {"choice": "identity", "probabilities": {"payments": 0.3, "identity": 0.7}},
        }}
        j = triage.parse_answers(resp, [], self.TEAMS)
        self.assertEqual(triage.top(j.team), "identity")
        # The built-in teams would reject this answer as naming unknown options.
        with self.assertRaises(triage.JevError):
            triage.parse_answers(resp, [])

    def test_fallback_team_is_configurable(self):
        policy = triage.Policy(fallback_team="sre-oncall")
        a = {"id": "a", "title": "t", "env": "prod", "configured_severity": "critical"}
        self.assertEqual(triage.route_standalone(a, None, policy, "down").team, "sre-oncall")


class MainUsesConfig(unittest.TestCase):
    def test_config_drives_a_run_and_flags_override_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "c.toml")
            with open(cfg, "w") as f:
                f.write('[jev]\nmodel = "jev-9.9.9"\ntimeout = 7.0\n\n'
                        '[policy]\nfallback_team = "sre"\n\n'
                        '[teams]\npayments = "billing"\nidentity = "login"\n\n'
                        '[topology]\napi = ["db"]\n')
            alerts = os.path.join(tmp, "alerts.json")
            with open(alerts, "w") as f:
                json.dump([{"id": "a1", "title": "down", "env": "prod",
                            "configured_severity": "critical"}], f)
            out = os.path.join(tmp, "out.json")
            with mock.patch.dict(os.environ, {"JEV_ONCALL_CONFIG": cfg}, clear=True), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                triage.main(["--alerts", alerts, "--out", out, "--timeout", "3"])
            with open(out) as f:
                meta = json.load(f)["meta"]
            self.assertEqual(meta["model_requested"], "jev-9.9.9")
            self.assertEqual(meta["teams"], {"payments": "billing", "identity": "login"})
            self.assertEqual(meta["topology"], {"api": ["db"]})
            self.assertEqual(meta["policy"]["fallback_team"], "sre")

    def test_bad_config_exits_before_triaging(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write("[policy]\npage_bar = 2\n")
        self.addCleanup(os.unlink, f.name)
        with self.assertRaises(SystemExit) as cm:
            triage.main(["--config", f.name])
        self.assertIn("config error", str(cm.exception.code))

    def test_server_runner_uses_config(self):
        config = triage.Config(teams=dict(ConfiguredTeams.TEAMS), timeout=4.0)
        runner = server.TriageRunner(config=config)
        with mock.patch.object(triage, "judge_all", return_value=({}, {}, {})) as judge:
            runner.triage([{"id": "a1", "title": "t", "env": "prod",
                            "configured_severity": "critical"}])
        args, kwargs = judge.call_args
        self.assertEqual(args[4], 4.0)
        self.assertEqual(kwargs["teams"], ConfiguredTeams.TEAMS)


if __name__ == "__main__":
    unittest.main()
