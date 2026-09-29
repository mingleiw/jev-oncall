#!/usr/bin/env python3
"""Offline tests for rca_recorded.py: building scenarios from recorded incidents, keeping
the label out of them, the computed statistics, and running the harness on them.
Synthetic case directories only: no dataset download, no network.

    python3 -m unittest test_rca_recorded -v
"""
import io
import json
import math
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import rca_experiment as rca
import rca_jev_agent as agent
import rca_recorded as rec
import triage

T0 = 1_700_000_000


def make_case(root_dir, name, spike=("b", "cpu"), inject_offset=400, length=800,
              services=("a", "b", "frontend")):
    """A synthetic RCAEval-style case: services a, b, frontend with steady metrics, and a
    sustained spike in one metric from the injection time on."""
    case = os.path.join(root_dir, name)
    os.makedirs(case)
    metrics = {}
    for service in services:
        for suffix, base in (("cpu", 2.0), ("mem", 50e6), ("latency-90", 0.1), ("error", 0.01)):
            pts = []
            for i in range(length):
                v = base * (1 + 0.01 * ((i * 7) % 5 - 2))  # small steady wobble
                if (service, suffix.split("-")[0]) == spike and i >= inject_offset:
                    v *= 20
                pts.append([T0 + i, v])
            metrics[f"{service}_{suffix}"] = pts
    with open(os.path.join(case, "metrics.json"), "w") as f:
        json.dump(metrics, f)
    with open(os.path.join(case, "inject_time.txt"), "w") as f:
        f.write(str(T0 + inject_offset))
    return case


class Labels(unittest.TestCase):
    def test_label_parsing(self):
        self.assertEqual(rec.label_of("re2ob_cartservice_cpu_1"), ("re2ob", "cartservice", "cpu", "1"))
        self.assertEqual(rec.label_of("re2tt_ts-auth-service_delay_3"), ("re2tt", "ts-auth-service", "delay", "3"))
        with self.assertRaises(ValueError):
            rec.label_of("cartservice_cpu")

    def test_metric_names(self):
        self.assertEqual(rec.split_metric("cartservice_cpu"), ("cartservice", "cpu"))
        self.assertEqual(rec.split_metric("ts-auth-service_latency-90"), ("ts-auth-service", "latency"))
        self.assertEqual(rec.split_metric("front-end_lat_90"), ("front-end", "latency"))
        self.assertIsNone(rec.split_metric("time"))
        self.assertIsNone(rec.split_metric("cartservice_weird"))
        chosen = rec.pick_metrics({"x_latency-50": [(0, 1)], "x_latency-90": [(0, 1)]})
        self.assertEqual(chosen[("x", "latency")], "x_latency-90")  # the higher percentile


class Statistics(unittest.TestCase):
    def series(self, jump_at=None, factor=10.0, blip=False):
        pts = [(float(i), 1.0 + 0.001 * (i % 3)) for i in range(600)]
        if jump_at is not None:
            pts = [(t, v * factor if t >= jump_at else v) for t, v in pts]
        if blip:  # one noisy sample only
            pts[350] = (350.0, 50.0)
        return pts

    def test_a_sustained_change_has_an_onset(self):
        st = rec.stats(self.series(jump_at=320), alert_t=300, width=300)
        self.assertAlmostEqual(st["ratio"], (280 * 10 + 20) / 300 / 1.001, places=1)
        self.assertEqual(st["onset_s"], 20)

    def test_a_single_blip_is_not_a_change(self):
        st = rec.stats(self.series(blip=True), alert_t=300, width=300)
        self.assertIsNone(st["onset_s"])

    def test_a_tiny_move_on_a_flat_series_is_not_a_change(self):
        pts = [(float(i), 5.0 if i < 300 else 5.2) for i in range(600)]  # +4% on a flat line
        self.assertIsNone(rec.stats(pts, 300, 300)["onset_s"])

    def test_too_few_points(self):
        self.assertIsNone(rec.stats([(0.0, 1.0), (1.0, 1.0)], 1, 300))


class Build(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, "data")
        self.out = os.path.join(self.tmp.name, "out")
        os.makedirs(self.data)
        make_case(self.data, "re2xx_b_cpu_1")

    def tearDown(self):
        self.tmp.cleanup()

    def built(self):
        built, skipped = rec.build(self.data, self.out)
        self.assertEqual(skipped, [])
        (sid,) = built
        with open(os.path.join(self.out, f"{sid}.json")) as f:
            scenario = json.load(f)
        with open(os.path.join(self.out, f"{sid}.truth.json")) as f:
            truth = json.load(f)
        return sid, scenario, truth

    def test_suspects_checks_and_truth(self):
        sid, scenario, truth = self.built()
        self.assertEqual(set(scenario["hypotheses"]), {"a", "b", "frontend"})
        self.assertIn("cpu_by_service", scenario["checks"])
        self.assertIn("b_cpu", scenario["checks"])
        self.assertRegex(scenario["checks"]["b_cpu"]["result"], r"\((19|20)\.\d\dx the before mean\)")
        self.assertIn("left its normal range 0s after", scenario["checks"]["b_cpu"]["result"])
        self.assertIn("Stayed within its normal range", scenario["checks"]["a_cpu"]["result"])
        self.assertEqual(truth["hypothesis"], "b")
        self.assertEqual(truth["fault"], "cpu")
        self.assertEqual(set(truth["mechanism_checks"]), {"b_cpu", "cpu_by_service"})
        self.assertNotEqual(truth["decoy"], "b")
        self.assertIsNone(scenario["version_check"])
        self.assertIn("frontend", scenario["incident"]["description"])

    def test_the_label_never_reaches_the_scenario(self):
        sid, scenario, truth = self.built()
        blob = json.dumps(scenario)
        self.assertNotIn("re2xx_b_cpu_1", blob)
        self.assertNotIn("re2xx", sid)
        self.assertNotIn("_b_", sid)
        self.assertNotIn("cpu", sid)
        for secret in (truth["mechanism"], truth["_source"], "fault injected", '"fault"', "decoy",
                       "mechanism_checks"):
            self.assertNotIn(secret, blob)
        # The scenario would be the same whichever service was at fault, apart from the
        # measured numbers: the suspects are simply every service.
        self.assertEqual(sorted(scenario["hypotheses"]), sorted({"a", "b", "frontend"}))

    def test_ids_are_stable_and_distinct(self):
        make_case(self.data, "re2xx_a_mem_1", spike=("a", "mem"))
        built, _ = rec.build(self.data, self.out)
        self.assertEqual(len(set(built)), 2)
        again, _ = rec.build(self.data, os.path.join(self.tmp.name, "out2"))
        self.assertEqual(sorted(built), sorted(again))

    def test_pattern_selects_cases_and_bad_cases_are_skipped(self):
        make_case(self.data, "re2xx_a_mem_2", spike=("a", "mem"))
        bad = os.path.join(self.data, "re2xx_ghost_cpu_1")  # root cause not among the services
        make_case(self.data, "tmp_case_x_1")
        os.rename(os.path.join(self.data, "tmp_case_x_1"), bad)
        built, skipped = rec.build(self.data, self.out, pattern="re2xx_*_1")
        self.assertEqual(len(built), 1)
        self.assertEqual([n for n, _ in skipped], ["re2xx_ghost_cpu_1"])

    def test_a_large_system_is_narrowed_without_the_label(self):
        many = tuple(f"svc{i:02d}" for i in range(20)) + ("frontend",)
        make_case(self.data, "re2yy_svc07_cpu_1", spike=("svc07", "cpu"), services=many)
        built, _ = rec.build(self.data, self.out, pattern="re2yy_*", max_suspects=5)
        with open(os.path.join(self.out, f"{built[0]}.json")) as f:
            scenario = json.load(f)
        with open(os.path.join(self.out, f"{built[0]}.truth.json")) as f:
            truth = json.load(f)
        # The 5 that moved most (ties broken by name), plus the front end if not among them.
        self.assertLessEqual(len(scenario["hypotheses"]), 6)
        self.assertIn("svc07", scenario["hypotheses"])  # the one that moved
        self.assertIn("frontend", scenario["hypotheses"])
        self.assertTrue(truth["root_in_suspects"])
        self.assertEqual(truth["n_services"], 21)
        self.assertEqual(truth["n_suspects"], len(scenario["hypotheses"]))
        self.assertNotIn("svc15_cpu", scenario["checks"])  # detail checks for suspects only
        self.assertIn("svc15", scenario["checks"]["cpu_by_service"]["result"])  # overviews cover all
        # If the cause shows nothing, it can fall out of the suspects: recorded, not hidden.
        make_case(self.data, "re2zz_svc03_disk_1", spike=("svc11", "mem"), services=many)
        built, _ = rec.build(self.data, self.out, pattern="re2zz_*", max_suspects=1)
        with open(os.path.join(self.out, f"{built[0]}.truth.json")) as f:
            self.assertFalse(json.load(f)["root_in_suspects"])

    def test_cli(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = rec.main(["build", "--data", self.data, "--out", self.out])
        self.assertEqual(code, 0)
        self.assertIn("built 1 scenarios", buf.getvalue())


def generic_jev(payload):
    """A fake Jev for any scenario: first option leads, yes/no questions at 0.5."""
    answers = {}
    for key, q in payload["questions"].items():
        if q["type"] == "noul":
            answers[key] = {"type": "noul", "noul": 0.5}
        else:
            opts = list(q["criteria"])
            probs = {o: (0.6 if i == 0 else 0.4 / (len(opts) - 1)) for i, o in enumerate(opts)}
            answers[key] = {"type": "choice", "choice": opts[0], "probabilities": probs}
    return {"model": triage.MODEL, "answers": answers, "usage": {"input_tokens": 100}}, 5.0


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = os.path.join(self.tmp.name, "data")
        self.out = os.path.join(self.tmp.name, "scen")
        os.makedirs(data)
        make_case(data, "re2xx_b_cpu_1")
        (self.sid,), _ = rec.build(data, self.out)

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *argv, env=None):
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.dict(os.environ, env or {}, clear=True):
            code = rca.main(list(argv))
        return code, buf.getvalue()

    def test_dry_run_on_a_scenario_dir_and_the_default_is_restored(self):
        before = rca.SCENARIO_DIR
        code, out = self.run_main("--scenario-dir", self.out, "--dry-run", "--setup", "alone,jev-agent",
                                  "--models", "openai:m")
        self.assertEqual(code, 0)
        self.assertIn(self.sid, out)
        self.assertIn("cpu_by_service", out)
        self.assertEqual(rca.SCENARIO_DIR, before)

    def test_forced_mode_needs_a_key_check(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            self.run_main("--scenario-dir", self.out, "--dry-run", "--forced")

    def test_jev_agent_runs_and_is_scored_against_the_recorded_truth(self):
        with tempfile.TemporaryDirectory() as d:
            traces = os.path.join(d, "t.jsonl")
            with mock.patch.object(agent, "jev_asker", return_value=generic_jev):
                code, text = self.run_main("--scenario-dir", self.out, "--setup", "jev-agent",
                                           "--max-checks", "8", "--out", traces,
                                           env={"TYPESAFE_API_KEY": "t"})
            self.assertEqual(code, 0)
            (t,) = rca.read_traces(traces)
            self.assertEqual(t["status"], "ok")
            self.assertEqual(t["scenario_dir"], os.path.relpath(self.out, rca.BASE))
            self.assertLessEqual(len(t["observed"]), 8)
            code, text = self.run_main("--scenario-dir", self.out, "--compare", traces)
            self.assertIn("Investigator comparison", text)
        old = rca.SCENARIO_DIR
        try:
            rca.SCENARIO_DIR = self.out
            s = rca.score_trial(t, rca.load_ground_truth(self.sid))
        finally:
            rca.SCENARIO_DIR = old
        self.assertIn(s["correct_hypothesis"], (True, False))
        self.assertIsNone(s["ran_version_check"] or None)  # no key check in recorded scenarios


if __name__ == "__main__":
    unittest.main()
