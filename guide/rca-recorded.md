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

## Run 2 (preregistered): v6, showing what failed

**Why.** Run 1 left one clear gap. In 22 of Jev's right answers, the cited evidence
didn't include a check for the injected fault type, so Jev said *where* but not
*what*. In 21 of those 22, it never ran such a check:

- in 17, it spent the whole budget finding the service;
- in 5, it stopped early.

**v6** (`--agent-policy v6`) keeps v5 for finding the service, but holds back 2 of the
8 checks for the next step:

1. **What-failed phase.** After the where phase answers, Jev is asked which unrun check
   would best show what failed inside that service: which resource, connection or
   process. Up to 2 such checks are run.
2. **Final re-rank and re-verify** on everything observed. Verification gets one more
   question per check: does it show *what* failed inside the service, not only that
   the service is affected? Checks judged to show it come first in the evidence.
3. **Evidence report.** Code builds a plain-text summary from the frozen diagnosis and
   the measured results:
   - the cause, and whether it's verified;
   - what failed, with the numbers;
   - where it showed;
   - what was ruled out, and what wasn't;
   - which checks returned nothing.

   No model writes it.

v6 became the default after run 2 (below).

**Preregistration.** Run 1's test cases are spent: v6 was designed from their traces.
Run 2 uses cases no version has seen:

1. **Test split:** RE2-SS and RE2-TT, **instances 2 and 3** (120 cases). Build them
   into `rca_recorded/re2-test2` and commit them before any trial.
2. **Investigators:** `jev-agent` **v5 and v6**, both frozen, at most 8 checks, 1 trial
   per case. LLM baselines are optional; the question is v6 against v5.
3. **Primary metric: supported diagnosis.** That means the right service, citing a
   check that shows the injected fault type.
4. **Also reported:** right service (v6's where phase has 2 fewer checks, so watch for
   a drop), verified and verified-but-wrong, checks, seconds and cost.
5. **Adapter tuning:** only on the dev split (RE2-OB instance 1), as before.

```
python3 rca_recorded.py build --data data --out rca_recorded/re2-test2 --cases 're2ss_*_[23]'
python3 rca_recorded.py build --data data --out rca_recorded/re2-test2 --cases 're2tt_*_[23]'
git add rca_recorded/re2-test2 && git commit -m "Freeze the run-2 test split"
for v in v5 v6; do
  python3 rca_experiment.py --scenario-dir rca_recorded/re2-test2 --setup jev-agent --agent-policy $v \
      --max-checks 8 --out rca_results/<date>-recorded-test2/jev-$v.jsonl
done
python3 rca_experiment.py --scenario-dir rca_recorded/re2-test2 --compare rca_results/<date>-recorded-test2/jev-v5.jsonl rca_results/<date>-recorded-test2/jev-v6.jsonl
```

Each v6 trace carries its `evidence_report`. Include a few in the write-up, the
wrong ones among them.

## Results: run 2, v5 against v6

The traces, comparison and sample evidence reports are in
`rca_results/2026-09-29-recorded-test2/`.

- **Setup held:** 120 scenarios, frozen before any trial (commit 51fc971). The cause
  was among the suspects in 118/120. The two lost were ts-auth-service network faults
  cut by the 12-suspect cap. There were no infrastructure failures.
- **Supported diagnosis, the primary metric:** v5 68/120, **v6 90/120.** v6 gained 25
  cases and lost 3 (exact McNemar test, p < 0.001). It ran a check for the injected
  fault type in 101 cases, against 79.
- **Right service:** v5 110/120, v6 108/120. v6 lost 5 and gained 3, a difference
  within noise, despite 2 fewer checks for finding the service.
- **Verification:** v5 verified 10, v6 11, and none of either was wrong.
- **Speed and cost:** v6 3.5 s per case, v5 5.0 s; both about $0.004.

| | v5 Sock Shop | v5 Train Ticket | v6 Sock Shop | v6 Train Ticket |
|---|---|---|---|---|
| Right service | 56/60 | 54/60 | 55/60 | 53/60 |
| Supported diagnosis | 36/60 | 32/60 | 45/60 | 45/60 |
| Verified | 5/60 | 5/60 | 8/60 | 3/60 |

**Where v6 still falls short:**
- **Wrong service, 12 cases.** 10 are network faults (delay or packet loss), where Jev
  blamed a neighbour: a service's database instead of the service, or a caller
  instead of the callee. 2 are the cases whose cause was not among the suspects.
- **Right service, no supporting evidence, 18 cases.** 15 are disk faults. Jev rarely
  picks a service's disk check.
- **Rarely verified.** 112 of 120 investigations used the whole budget, mostly with 1
  or 2 alternatives not ruled out.
- **"What failed" is often empty.** Jev usually runs and cites the fault check, but
  files it as evidence of *where*. Only 30 of the 120 evidence reports have a "What
  failed" section.

Run 2's test cases are now spent too: v7 will be designed from them. The next
preregistered test needs fresh cases, such as RE2-OB instances 2 and 3.

## Limits

- **Staged faults.** These are injected faults in demo systems, not production
  incidents: recorded, but staged. Each case has one cause.
- **No changes to suspect.** There are no deploy or configuration-change events, so
  "a recent change" can't be a suspect here.
- **Suspects are services.** Naming the fault type is scored through the cited checks
  (supported diagnosis), not a separate answer.
- **Metrics only**, for now.
- **Alert time is the injection time.** A real alert fires later than the fault.
