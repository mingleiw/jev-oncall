# RCA on recorded incidents

The main benchmark ([rca.md](rca.md)) uses incidents I wrote by hand: each comes with a
list of suspects, a menu of checks, and check results written as prose. It showed
that Jev with plain code can run the loop and change direction. It didn't show
whether that holds when:

- nobody hands the investigator its options;
- the evidence is recorded telemetry, not text written by someone who knew the answer;
- the incidents weren't used to tune the rules. All eight hand-written scenarios have
  now informed some revision.

This experiment tests all three.

## Data

[RCAEval](https://github.com/phamquiluan/RCAEval) (MIT) records faults injected into
three microservice demo systems. The **RE2** suite has 270 cases, each with metrics,
logs, traces and an injection time. The systems are:

- **Online Boutique (OB):** about 12 services.
- **Sock Shop (SS).**
- **Train Ticket (TT):** about 60 services.

In each system, 5 services each get 6 fault types (cpu, mem, disk, delay, loss,
socket), 3 times over. Each case's directory is named
`{benchmark}_{service}_{fault}_{instance}`, so the label is in the name.

## How a case becomes a scenario

`rca_recorded.py` converts each case into the same scenario and truth files the
benchmark already uses. Every setup (an LLM alone, an LLM + Jev, Jev investigating)
then runs on them unchanged through `rca_experiment.py --scenario-dir`. The suspects,
checks and budget are identical for each.

| Part | Generated from |
|---|---|
| Incident | The user-facing service's latency before and after the alert time |
| Suspects | Every service in the metric names. For large systems, the 12 whose metrics moved most, plus the user-facing one. Ranked from telemetry alone. |
| Checks | For each kind of metric (latency, errors, CPU, memory, disk, sockets, traffic): one overview across all services, and one detail check per suspect |
| Check results | Computed by code: mean and max before and after the alert time, the ratio, and when the series first left its normal range |
| Truth | From the directory name only: the root-cause service, the fault, and the checks that show it |

**What counts as a change.** A series has *left its normal range* when it moves more
than 3 standard deviations, and more than 10% of its normal level, for 10 consecutive
points. One noisy sample, or a tiny move on a flat series, doesn't count.

**Two things are deliberately not given:**
- **No key check or planted decoy.** Real fault propagation provides its own
  distractions: downstream services move too. The truth file records the loudest
  wrong service as the decoy, for the belief metrics only.
- **No label in the scenario.** Scenarios get opaque IDs (`rec_ob_<hash>`). Tests check
  that neither the directory name nor the fault reaches a scenario. The alert time is
  the injection time, as in RCAEval's own baselines. That reveals *when*, not *where*.

**Only metrics so far.** RE2's metrics already include per-service latency and error
rates. Logs and traces come next.

## Preregistration

These are fixed before any case is looked at. Changing one after seeing results means
a new version, reported as such.

1. **Dev split: RE2-OB, instance 1** (30 cases: 5 services × 6 faults). Used only to
   debug the adapter: check wording, windows, the change rule and the suspect cap.
   Nothing learned here may change Jev's policy.
2. **Test split: RE2-SS and RE2-TT, instance 1** (60 cases). The adapter is frozen
   and the generated scenario files are committed *before* the first test trial, so
   their digests lock them. Run once.
3. **Investigators.**
   - Jev: `jev-agent`, policy **v5**, frozen.
   - LLMs, each alone: DeepSeek V4.1 Flash and GLM-5.3, the same models as the main
     benchmark.
   - The LLM + Jev setup is optional.
4. **Conditions:** free mode, at most **8** checks (more suspects than the
   hand-written scenarios), 1 trial per case.
5. **Metrics**, reported per system and for each investigator:
   - **Right service:** correct cause.
   - **Suspect recall:** the share of cases where the cause survived the suspect cap.
     This is the adapter's ceiling on accuracy, reported separately.
   - **Supported diagnosis:** the right service, citing a check that shows the fault
     kind, run by the investigator, with no failed query cited.
   - **Verified, and verified but wrong** (Jev only).
   - **Checks, calls, seconds, tokens and dollars.**
   - **Infrastructure failures**, counted separately from diagnostic results.
6. **Reference points.** RCAEval's paper reports accuracy for dedicated RCA methods
   such as BARO. Those methods see every metric, so they're context, not a
   like-for-like comparison.

## Commands

The runner needs `pip install huggingface_hub pandas pyarrow`, a few GB of disk, and
access to huggingface.co. This sandbox doesn't have the last.

```
# Phase 0: fetch RE2 (Parquet, one suite at a time) and inspect one case
python3 -c "from huggingface_hub import snapshot_download as d; d(repo_id='phamquiluan/RCAEval', repo_type='dataset', allow_patterns='re2*', local_dir='data')"

# Dev: build and pilot
python3 rca_recorded.py build --data data --out rca_recorded/re2-dev --cases 're2ob_*_1'
python3 rca_experiment.py --scenario-dir rca_recorded/re2-dev --setup jev-agent --max-checks 8 \
    --out rca_results/<date>-recorded-dev/jev.jsonl

# Test: build, COMMIT the scenario files, then run once
python3 rca_recorded.py build --data data --out rca_recorded/re2-test --cases 're2ss_*_1'
python3 rca_recorded.py build --data data --out rca_recorded/re2-test --cases 're2tt_*_1'
git add rca_recorded/re2-test && git commit -m "Freeze the recorded test split"
python3 rca_experiment.py --scenario-dir rca_recorded/re2-test --setup jev-agent --max-checks 8 \
    --out rca_results/<date>-recorded-test/jev.jsonl
python3 rca_experiment.py --scenario-dir rca_recorded/re2-test --models openai:<model> --setup alone \
    --max-checks 8 --out rca_results/<date>-recorded-test/<model>.jsonl
python3 rca_experiment.py --scenario-dir rca_recorded/re2-test --compare rca_results/<date>-recorded-test/*.jsonl
```

`build` prints how many cases keep the cause among the suspects. Report it next to the
accuracy.

## Results: test split, run 1

The traces and comparison are in `rca_results/2026-09-29-recorded-test/`.

- **Setup held:** 60 scenarios, and the cause was among the suspects in 60/60. No
  converter fixes were needed.
- **Right service:**
  - Jev v5: 57/60 (29/30 Sock Shop, 28/30 Train Ticket);
  - DeepSeek V4.1 Flash alone: 53/58;
  - GLM-5.3 alone: 52/54.
- **Fair comparison:** on the 52 cases all three completed, Jev got 51, DeepSeek 48 and
  GLM 50.
- **Supported diagnosis:** Jev 35/60, DeepSeek 39/58, GLM 41/54. In 22 of Jev's right
  answers, the cited checks were not ones that show the injected fault type.
- **Verification:** Jev verified 9, and none of them was wrong. All three of its misses
  were unverified:
  - rabbitmq instead of orders;
  - ts-auth-mongo instead of ts-auth-service;
  - ts-basic-service instead of ts-auth-service.
- **Speed and cost:** Jev 5.3 s per case, about $0.004; DeepSeek 32 s; GLM 119 s.
- **Infrastructure failures, excluded from every rate:** DeepSeek 2 (timeouts); GLM 6
  (1 timeout, and 5 OpenRouter credit-cap errors that never reached the model).
- **Deviation:** the 5-case dev sanity check finished after the test trials began.

## Limits

- **Staged faults.** These are injected faults in demo systems, not production
  incidents: recorded, but staged. Each case has one cause.
- **No changes to suspect.** There are no deploy or configuration-change events, so
  "a recent change" can't be a suspect here.
- **Suspects are services.** Naming the fault type is scored through the cited checks
  (supported diagnosis), not a separate answer.
- **Metrics only**, for now.
- **Alert time is the injection time.** A real alert fires later than the fault.
