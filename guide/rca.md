# The RCA experiment

When a deployment looks guilty, the agent starts investigating it, and the next
query contradicts that theory: does the agent change direction? And does it help
to have Jev re-score every hypothesis after each new piece of evidence?

Jev isn't the investigator here. A reasoning model picks the checks and writes the
answer; Jev only judges, after every result, which hypothesis best explains all the
evidence and whether the evidence contradicts each one. So the comparison is each
model **alone** vs the same model **with Jev**, not Jev against the models.

## The scenarios

`rca_scenarios/` holds two frozen incidents, written independently by different
authors so that neither author's sense of the answer is the only one tested. Both
start the same way: database connection pool exhaustion takes down checkout, and
before any check the agent is told about a `checkout-api` deploy, rolled to part of
the fleet minutes earlier, whose change summary sounds relevant. It looks guilty. The
causes differ:

| | `pool_etl_cron` | `pool_lock_batch` |
| --- | --- | --- |
| True cause | An analytics cron job moved from 03:00 to peak hours; its long queries hold 73 of the pool's 200 connections | An archive batch job, triggered by hand at 14:01, holds row locks on `orders`, so checkout queries wait and hold their connections about 50 times longer |
| Kind of failure | Direct resource use | Lock contention, seen first as slow queries |
| Hypotheses / checks | 6 / 15 | 5 / 15 |

Each scenario has:
- telemetry frozen at one moment: metrics, logs, config, job schedules;
- **a version check** showing the old and new versions fail at the same rate, from the
  same moment, which contradicts the deploy;
- a check that rules out every wrong hypothesis;
- **2 traps**, a query that times out and an empty result from a wrong service name,
  neither of which is evidence;
- **3 noise checks**: real but unrelated signals.

Each `<name>.json` is what the agent and Jev see. The answer, the notes on what each
check shows, and the lists of traps and noise are in `<name>.truth.json`, used only
for scoring. Tests prove no prompt, observation, or Jev payload contains any of it,
and that a trial never opens a truth file. Report results per scenario: a result that
holds on one and not the other is itself a finding.

To add a scenario, drop a `<name>.json` and `<name>.truth.json` pair in the folder,
following the existing two; the scenario tests check its shape and run the leak test
on it automatically.

## The two setups

Same model, scenario, check menu, and prompt wording in both.

- **alone**: after every observation the model reports its beliefs over all
  hypotheses as JSON, then picks a check or gives a final answer (hypothesis,
  component, mechanism, cited evidence). Its own beliefs are measured.
- **jev**: the same, but before the first check and after every check Jev re-scores
  every hypothesis on all the evidence so far, and the model sees those scores. Jev's
  distribution is the belief measured.

Jev gets one call per state: a Choice question over the hypotheses ("which best
explains all the evidence") and a Noul question per hypothesis ("does any observation
contradict it"). It goes through `triage.call_jev`, the same client triage uses.

Fairness: what Jev is told about failed and empty queries is also in the model's
system prompt, and Jev sees the same initial context the model does. Trials rotate
scenarios, models and setups (trial 1 runs every combination once, alone then with
Jev, before trial 2 starts), so API drift over a run hits every cell alike.

## The metrics

Reported as counts ("3/5"), per scenario, model and mode, with a column per setup.

| Metric | Meaning |
| --- | --- |
| finished the investigation | Gave a parseable final answer within the check limit |
| decoy on top at the start | The deploy led the measured belief before any check |
| ran the version check | It chose (or, in forced mode, was given) `error-by-version` |
| changed direction after it | The deploy led right before the version check and not right after. Trials where it didn't lead before are left out |
| mean drop in P(deploy) | Measured P(deploy) before the version check minus after |
| right hypothesis | The final hypothesis is the true one |
| found the mechanism | Ran a check that shows it, and the answer says what changed and how it exhausted the pool. Scored apart from the hypothesis |
| named the component | Names the job at fault, not just "the database" |
| blamed the decoy | Final hypothesis is the deploy |
| cited only checks it ran | Every cited check was observed |
| cited a failed/empty query | Cited a trap as evidence |
| mean checks used, mean noise checks | Effort, and effort spent on noise |
| Jev call errors | Jev calls that failed; the trial carries on without scores |

## Run it

Keys go in environment variables, never in files or chat: `TYPESAFE_API_KEY` for
Jev, `ANTHROPIC_API_KEY` for Claude models, and `OPENAI_API_KEY` (plus
`OPENAI_BASE_URL` for any OpenAI-compatible server) for the rest.

```
python3 rca_experiment.py --dry-run --models anthropic:claude-opus-5,openai:gpt-5 --trials 5 --forced
python3 rca_experiment.py --check   --models anthropic:claude-opus-5,openai:gpt-5
python3 rca_experiment.py           --models anthropic:claude-opus-5,openai:gpt-5 --trials 5 --forced
python3 rca_experiment.py           --models anthropic:claude-opus-5,openai:gpt-5 --trials 5
python3 rca_experiment.py --report rca_traces.jsonl --html rca_report.html
```

1. `--dry-run` prints the system prompt, the first message, the Jev payload after
   the version check, and the trial plan. It calls nothing and needs no keys.
2. `--check` makes one tiny call to each model and one to Jev, so a bad key or a
   blocked host fails in seconds, not twenty trials in.
3. `--forced` runs the version check first in every trial, so every trial meets
   the contradiction. The model still states its beliefs before seeing it. Run
   this first; free mode then shows whether agents find the check on their own.
4. Each trial is appended to `rca_traces.jsonl` as soon as it ends: every
   state's beliefs and Jev scores, the checks, the final answer, token usage, and
   the scenario name and digest. `--report` re-scores any trace file, so scoring changes
   don't need new runs.
5. `--html rca_report.html` also writes the results as a leaderboard page, laid out
   like an open model benchmark: each system (a model alone, or the model + Jev)
   ranked by % Resolved (right cause and its mechanism), with the raw count and a 95%
   confidence interval, a tab per scenario, a chart of each model alone vs + Jev,
   tokens and time per trial, and every trial's checks and how P(deploy) moved. It
   works after a run or with `--report`; `--note` adds a notice at the top, for
   example to mark test data.

Every run covers all scenarios unless `--scenarios pool_etl_cron` (a comma-separated
list) narrows it. Options: `--setup alone|jev|both`, `--max-checks` (default 10), `--jev-model`
(default the pinned triage model), `--out`. A model is `provider:model`; a bare
provider uses its default (`anthropic` → `claude-opus-5`, `openai` → `gpt-5`).

## What would count as a result

Jev earns its place if, for most models, the jev column changes direction more
often, drops P(deploy) further, and blames the decoy less, without costing right
answers or mechanisms. It doesn't if models alone already change direction
reliably, or if Jev's scores move but the final answers don't improve.

## Known limits

- **Two scenarios, written by hand.** Each author knew their answer while writing it.
  Two independent authors is better than one, but both scenarios share a shape
  (pool exhaustion, a deploy decoy). Recorded incidents would be better still.
- **Few trials.** Five trials per cell show direction, not significance.
- **Measured belief differs by setup.** Alone measures the model's stated beliefs,
  which may not be calibrated; jev measures Jev's. The final-answer metrics are
  comparable across setups either way.
- **Keyword scoring.** Mechanism and component checks match words in the answer.
  Read the traces of any surprising trial.
- **A frozen menu.** The agent picks from fixed checks rather than writing
  queries, so it can't search the wrong interval or invent a service name itself.
