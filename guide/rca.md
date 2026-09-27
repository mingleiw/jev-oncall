# The RCA experiment

Does an AI agent change direction when evidence contradicts its leading theory?

Coding agents have a feedback loop: tests. They write code, run the tests, and
know whether it worked. Incident agents have no equivalent. When a deployment
looks guilty and the agent starts investigating it, and the next query says
"errors are the same on old and new versions," does the agent update? Or does it
keep digging into the deploy?

This experiment measures that.

## The scenario

A frozen incident: the orders-db connection pool is exhausted, checkout is down.

The agent sees a **decoy** first: a recent deploy (v2.47.1) that changed
connection retry and timeout settings. The timing and the change summary are
designed to look guilty.

The **true cause** is that the analytics ETL cron job was rescheduled from
off-peak (03:00 UTC) to peak hours (14:00 UTC). It runs long analytical queries
on the primary database, each holding a connection for minutes. At peak traffic,
its 73 connections plus normal application load exceed the 200-connection limit.

The key evidence is the **version check** (`error-by-version`): error rates are
3.42% on v2.47.0 and 3.38% on v2.47.1, proportional to traffic share. Both
versions started failing at the same moment. This directly contradicts the deploy
hypothesis.

Six hypotheses are available, with 15 diagnostic checks:
- 3 support the true cause (pool-metrics, active-queries, cron-history)
- 5 rule out wrong hypotheses (traffic-stats, db-config, conn-audit, dns-log,
  error-by-version)
- 2 are traps: a query that times out and an empty result from a wrong service
  name. Neither is evidence
- 3 are noise: CDN cache, canary memory, TLS cert expiry

Ground truth is in a separate file (`rca_ground_truth.json`) that the agent never
sees. A test proves no check result contains it.

## The two setups

Both use the same model, scenario, check menu, and prompt wording.

**Baseline.** The model picks checks from the menu, reports its beliefs as a
probability distribution over hypotheses after every observation, and gives a
final answer: hypothesis, component, mechanism, and cited evidence.

**Jev.** Same, but after every observation, Jev re-scores every hypothesis using
the same evidence the model sees. Jev answers two questions:
- Which hypothesis best explains all the evidence so far? (Choice → distribution)
- Which hypothesis is most contradicted? (Choice → distribution)

The model sees Jev's scores alongside its own observations. In this setup, Jev's
distribution is the belief being measured for scoring.

## The metrics

| Metric | What it measures |
| --- | --- |
| Decoy led initially | Did the deploy hypothesis start as the top belief? |
| Ran version check | Did the agent choose to run `error-by-version`? |
| Changed direction | Was deploy the top hypothesis before the version check, and not after? |
| P(deploy) drop | How much P(deploy) fell after the version check |
| Correct hypothesis | Did the final answer name `etl-cron`? |
| Correct mechanism | Did the mechanism mention the cron/schedule change and connection holding? |
| Correct component | Did it name `analytics-etl` (or close)? |
| Blamed decoy | Did the final answer name `deploy`? |
| All evidence observed | Was every cited piece of evidence actually a check the agent ran? |
| Cited a trap | Did the agent cite the timed-out or empty-result check as evidence? |
| Checks used | Which checks, and how many |
| Noise checks | How many of the 3 noise checks were run |

Counts are reported as fractions ("3/5"), not percentages.

## Running it

Python 3.11+, standard library only.

```
export ANTHROPIC_API_KEY=...          # or OPENAI_API_KEY
export TYPESAFE_API_KEY=...           # for the jev setup
python3 rca_experiment.py --trials 5  # 5 baseline + 5 jev, alternating
python3 rca_experiment.py --trials 5 --forced   # version check first in every trial
python3 rca_experiment.py --dry-run              # print prompts, no API calls
python3 rca_experiment.py --report rca_traces.jsonl  # score and report from saved traces
```

Options:
- `--setup baseline|jev|both` — run one or both setups (default: both)
- `--provider anthropic|openai` — which model API to use
- `--model <name>` — model name (default: claude-sonnet-4-20250514 or gpt-4o)
- `--jev-model <name>` — Jev model (default: jev-1.13.0)
- `--forced` — force the version check as the first observation
- `--max-checks 12` — stop after this many checks
- `--out <path>` — JSONL file for traces (default: rca_traces.jsonl)

Each trial's full step-by-step trace is appended to the JSONL file.

## What would count as "Jev helps"

Jev earns its place if:
1. The jev setup changes direction more often than baseline after the version
   check (higher changed-direction rate)
2. P(deploy) drops further in the jev setup (the contradiction is absorbed, not
   ignored)
3. The jev setup reaches the correct hypothesis more often

Jev doesn't earn its place if the baseline model already changes direction
reliably, or if Jev's scores don't improve the final answer rate.

## Known limits

- **One scenario.** The experiment tests direction change on one incident. A real
  evaluation needs multiple scenarios with different decoys and true causes.
- **Frozen evidence.** The checks return canned text, not live systems. A real RCA
  agent would query dashboards and decide what to look at.
- **Whoever writes the scenario knows the answer.** The scenario is designed to
  make the ETL cron job discoverable. An independent second scenario (on the
  `rca-experiment` branch) mitigates this.
- **Self-reported beliefs.** In baseline mode, the model reports its own beliefs;
  it may not be calibrated. In jev mode, Jev's beliefs are the measurement, which
  is more comparable across trials but measures Jev, not the model.
- **Prompt sensitivity.** The model's behavior depends on the prompt wording, the
  hypothesis ordering, and the check menu. Small changes could shift results.
- **No cost control.** Each trial makes many model calls and Jev calls. At scale,
  the experiment needs budgeting.

## Fairness

The guidance about failed queries and empty results ("a timeout is not evidence
that nothing happened") appears in both the model's system prompt and the Jev
payload, so neither setup gets a structural advantage from it.

Trials alternate between setups (baseline, jev, baseline, jev, ...) so API drift
or rate limiting affects both equally.
