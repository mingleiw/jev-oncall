# Benchmark: fault-injected evaluation for incident-triage models

## The problem this solves

`guide/evaluation.md` is honest about the limit of hand-written synthetic
alerts: the 14 alerts in `alerts.json` are "a smoke test, not an evaluation"
because their labels are the author's own guesses. A model that agrees with
those labels may just be agreeing with the author's biases -- the test is
circular. Replay of real historical alerts would fix it, but nobody publishes
a labeled corpus of production incidents.

This benchmark takes a third path: **ground truth from fault injection**.
Instead of writing alerts and then guessing their labels, we script real
faults and record what they produce. The injection log says "at t+20s we
exhausted the orders-db connection pool"; every alert downstream of that is
a symptom of that fault *by construction*. Labels describe what was done,
not what someone inferred afterwards.

## Methodology

1. **Fault catalog** (`benchmark/faults.py`). Eight fault types, each a
   generator: `db_pool_exhaustion`, `bad_deploy`, `disk_full`, `cert_expiry`,
   `latency_spike`, `memory_leak`, `staging_noise`, `slow_batch`. Each takes
   a seeded RNG and produces one `Incident` with alerts plus ground truth
   derived from the fault definition:
   - root alert: `actionable=True`, the fault's SEV, the owning team
   - symptom alert: `actionable=True`, severity one notch less urgent, the
     *root's* team, `duplicate_of` pointing at the root (the correct behavior
     is linking it to the root, not paging a second team)
   - noise alert: `actionable=False`, SEV4 (nothing should reach a human)
2. **Noise replay** (`benchmark/noise.py`). Production doesn't arrive clean,
   so neither does the dataset: duplicate refires, flapping
   (fire → resolve → refire), and unrelated warnings interleaved in time
   order. Noise only *adds* alerts; it never changes what the right answer
   is. Two independent RNG streams keep the fault sequence identical across
   noise levels, so `--noise 0.0` and `--noise 1.0` share incidents.
3. **Eval harness** (`benchmark/harness.py`). Any model implementing the
   `TriageModel` interface (alert dicts in, judgments out -- see
   `benchmark/models.py`) is scored on:
   - `severity_acc`: exact SEV match on actionable alerts
   - `owner_acc`: team match on incident roots
   - `link_acc`: symptom/duplicate alerts linked to the right root --
     the metric naive routers score zero on
   - on-call outcomes: silent misses, missed pages, delayed pages, false
     pages, duplicate pages, misrouted pages
   - calibration: Brier score and ECE for confidence as P(needs a page)

The whole pipeline is deterministic (`--seed`) and offline: no API calls,
no Docker, `python3 test_benchmark.py` covers it.

## Usage

```bash
cd benchmark
python3 generate.py --count 100 --seed 42 --out dataset.jsonl
python3 harness.py --dataset dataset.jsonl --model severity-baseline
python3 harness.py --dataset dataset.jsonl --model random --seed 7
```

`severity-baseline` is the incumbent: route by configured severity only,
the way most teams' Alertmanager routing works today. It scores
`owner_acc = 1.0` (team follows the service map) but `link_acc = 0.0` and
falls for every `critical`-configured noise trap -- that gap is what a
judgment model has to close.

## What the numbers mean (and don't)

This measures *triage judgment on simulated incidents*, not production
readiness. The faults are realistic shapes, not real outages; a high score
means the model reasons well about the alert stream, not that it's safe to
page from. Like `evaluation.md`'s shadow mode: run it against your own
historical alerts before trusting any threshold.

## Adding a fault type

1. Write a generator in `faults.py`: `(rng, incident_id, ids, t0) -> Incident`,
   using the `Builder` helpers (`root` / `symptom` / `noise`). Ground truth
   must follow from the fault definition, never from reading the alert text.
2. Register it in `FAULTS` and add a weight in `FAULT_WEIGHTS` (rough
   real-world mix: noise and minor faults outweigh SEV1s).
3. Add its alert names to `rules_fragment()` so it can later drive the real
   Compose stack.
4. Run `python3 test_benchmark.py` -- the invariant tests (label coverage,
   duplicate chains resolve, symptom team == root team) apply to every fault.

## Roadmap

- **Jev adapter**: map `triage.py` Decisions to `ModelOutput` (confidence =
  P(page) from the Jev distribution). Needs a TypeSafe API key, so it stays
  out of the offline harness. Sketch is in `models.py::JevModel`.
- **Compose driver**: `rules_fragment()` renders time-keyed Prometheus rules
  per fault; the driver writes them into `demo/rules.yml`, brings up the
  stack, and captures the real Alertmanager webhook stream for comparison
  against the simulated one. Untested -- no Docker on the build machine.
- **Public release**: freeze a versioned dataset (seed + count + noise level),
  publish to HuggingFace Datasets, add a leaderboard page. The dataset is the
  moat; version it like a release, not a script output.
