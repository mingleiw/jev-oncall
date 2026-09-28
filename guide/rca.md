# The RCA experiment

When a deployment looks guilty, the agent starts investigating it, and the next
query contradicts that theory: does the agent change direction? And does it help
to have Jev re-score every hypothesis after each new piece of evidence?

Jev isn't the investigator here. A reasoning model picks the checks and writes the
answer; Jev only judges, after every result, which hypothesis best explains all the
evidence and whether the evidence contradicts each one. So the comparison is each
model **alone** vs the same model **with Jev**, not Jev against the models.

## The scenarios

`rca_scenarios/` holds eight frozen incidents: three written by hand, below, and five
adapted from public postmortems (next section). In each hand-written one, before any
check, the agent is told about a deploy rolled to part of the fleet minutes earlier,
whose change summary sounds relevant. It looks guilty. The first two were written
independently by different authors, so that neither author's sense of the answer is
the only one tested:

| | `pool_etl_cron` | `pool_lock_batch` | `product_cache_partial` |
| --- | --- | --- | --- |
| Incident | Checkout down: database connection pool exhausted | Checkout down: database connection pool exhausted | Product pages slow and failing |
| True cause | An analytics cron job moved from 03:00 to peak hours; its long queries hold 73 of the pool's 200 connections | An archive batch job, triggered by hand at 14:01, holds row locks on `orders`, so checkout queries wait and hold their connections about 50 times longer | A cache node replaced during maintenance came back empty; lookups fall through to the database, which saturates |
| Version check | Clears the deploy: both versions fail alike | Clears the deploy: both versions fail alike | Only weakens it: the new version is worse (11.4% vs 7.1% errors), but the old one fails too |
| Hypotheses / checks | 6 / 15 | 5 / 15 | 6 / 14 |

`product_cache_partial` is the hard one, added after the first pilot showed a strong
model solving the other two alone every time, leaving no room for Jev to help. Its
deploy really is involved: a shorter cache TTL makes the new version recover more
slowly. But it amplifies the outage rather than causing it, and an agent that stops at
"the new version is worse" blames the wrong thing.

Each scenario has:
- telemetry frozen at one moment: metrics, logs, config, job schedules;
- **a version check** showing the old version failing too, from the same moment, which
  contradicts the deploy as the cause (fully in the first two, partly in the third);
- a check that rules out every wrong hypothesis;
- **2 traps**, a query that times out and an empty result from a wrong service name,
  neither of which is evidence;
- **3 noise checks**: real but unrelated signals.

Each `<name>.json` is what the agent and Jev see. The answer, the notes on what each
check shows, and the lists of traps and noise are in `<name>.truth.json`, used only
for scoring. Tests prove no prompt, observation, or Jev payload contains any of it,
and that a trial never opens a truth file. Report results per scenario: a result that
holds on one and not the other is itself a finding.

### Scenarios adapted from public postmortems

Hand-written scenarios share two weaknesses: the deploy is always innocent, so an agent
that just distrusts deploys would score well, and each author's clues fit their own
answer too neatly. Five more scenarios adapt real incidents from public postmortems.
Service names, times and numbers are changed so a model can't answer from memory of
the write-up; each truth file's `_source` names the original.

| Scenario | Adapted from | Looks guilty (the decoy) | True cause |
| --- | --- | --- | --- |
| `edge_rule_regex` | Cloudflare, 2 July 2019 | An HTTP flood attack | **The deploy**: a firewall rule pushed globally has a backtracking regex that burns all CPU |
| `feature_file_flap` | Cloudflare, 18 November 2025 | An attack (the third-party status page also went down) | A database grant change doubles a generated config file past a hard limit; errors flap as it is regenerated every 5 minutes |
| `registry_contention` | Roblox, October 2021 | A registry node with an open hardware ticket | A newly enabled streaming mode makes every write wait on one channel at peak load |
| `host_patch_routes` | Datadog, 8 March 2023 | A cloud provider outage (no deploys: change freeze) | An automatic systemd security update restarts networking and deletes pod network routes on every running node |
| `gateway_holiday_surge` | Slack, 4 January 2021 | A web deploy about first-connect payloads | The first day back after the holidays overloads a managed transit gateway, which drops packets |

The decoy is whatever looks guilty at the start; the truth file names it, and the
metrics follow it (so "P(decoy)" is the probability given to the attack in
`edge_rule_regex`, and to the deploy in `pool_etl_cron`). The
scenario's `version_check` field names its **key check**, the one that contradicts
the decoy; it is a version split only where the decoy is a deploy. Adaptations
simplify: each scenario has one cause, so contributing factors in the original (the
storage bug at Roblox, the provisioning limits at Slack) are either ruled out or shown
as later effects.

The first version of these five gave the answer away. In DeepSeek's run
(`rca_results/2026-09-28-v4-deepseek/`) the model favoured the true cause before any
check in three of them, because hypotheses were worded as the mechanism ("the release
pushed at 13:42 makes request processing too expensive") and the initial context
singled out the real change. The current version words hypotheses neutrally, lists the
real change as one routine change among several, adds a distractor change with its own
hypothesis in two scenarios, and gives the decoy fresh evidence (flood alerts, a disk
alert minutes earlier, the provider's own outage notice). Traces record the scenario
digest: don't pool runs from before and after this change.

To add a scenario, drop a `<name>.json` and `<name>.truth.json` pair in the folder,
following the existing ones; the scenario tests check its shape and run the leak test
on it automatically. Put the source of an adapted incident in the truth file, never in
the scenario file.

## The three setups

Same model, scenario, check menu, and prompt wording in all three.

- **alone**: after every observation the model reports its beliefs over all
  hypotheses as JSON, then picks a check or gives a final answer (hypothesis,
  component, mechanism, cited evidence).
- **jev**: the same, but before the first check and after every check Jev re-scores
  every hypothesis on all the evidence so far, and the model sees those scores: Jev's
  ranking (which hypothesis best explains the evidence) and a contradiction score for
  each.
- **jev-contra**: Jev scores the same way, but the model sees only the one hypothesis
  Jev judges the evidence most contradicts, or that none is, and nothing before the
  first check, when there is no evidence to contradict. It never sees Jev's ranking.

Why a third setup: in the first pilot the model sometimes adopted Jev's ranking
wholesale, including Jev's overconfident start (P(deploy) of 0.88 to 1.0 before any
evidence). After the version check, Jev's contradiction score for the deploy was
right (0.75) while its ranking still put the deploy first, and the model followed the
ranking. jev-contra tests whether Jev helps when it only says what the evidence rules
out.

The belief metrics measure the **model's own stated belief in every setup**, so the
alone and + Jev columns compare the same thing. Jev's scores are reported separately,
as their own lines ("Jev: changed direction", and a Jev row in the page's belief
table). Mixing them would compare two different believers: in the first pilot, Jev
started at P(deploy) = 1.0 and still ranked the deploy first after the version check,
while the model alone moved off it.

Jev gets one call per state with two Choice questions: which hypothesis best explains
all the evidence, and which one the evidence most contradicts (or none). It goes
through `triage.call_jev`, the same client triage uses.

Harness version 4 changed the second question. Version 3 asked a yes/no question per
hypothesis ("is this one contradicted?"); in the pilot Jev answered yes for most of
them at once, the true cause included, while still ranking the deploy highest. A
single choice has to compare them. Version 3 traces still re-score with `--report`,
but don't pool them with version 4.

Fairness: what Jev is told about failed and empty queries is also in the model's
system prompt, and Jev sees the same initial context the model does. Trials rotate
scenarios, models and setups (trial 1 runs every combination once, alone then with
Jev, before trial 2 starts), so API drift over a run hits every cell alike.

## The metrics

Reported as counts ("3/5"), per scenario, model and mode, with a column per setup.

| Metric | Meaning |
| --- | --- |
| finished the investigation | Gave a parseable final answer within the check limit |
| decoy on top at the start | The decoy led the measured belief before any check |
| ran the version check | It chose (or, in forced mode, was given) the scenario's key check |
| changed direction after it | The decoy led right before the key check and not right after. Trials where it didn't lead before are left out |
| mean drop in P(decoy) | Measured P(decoy) before the key check minus after |
| checks the decoy still led | Checks, from the key check on, for which the decoy was still the top hypothesis (ties count). How long the decoy survived the contradiction |
| checks until the cause led | Checks until the true cause was the top hypothesis on its own. Separates agents that all reach the right answer by how fast they get there |
| right hypothesis | The final hypothesis is the true one |
| found the mechanism | Ran a check that shows it, and the answer says what changed and how it exhausted the pool. Scored apart from the hypothesis |
| named the component | Names the job at fault, not just "the database" |
| blamed the decoy | Final hypothesis is the decoy |
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
   tokens and time per trial, and every trial's checks and how P(decoy) moved. It
   works after a run or with `--report`; `--note` adds a notice at the top, for
   example to mark test data.

Every run covers all scenarios unless `--scenarios pool_etl_cron` (a comma-separated
list) narrows it. If a run stops (a rate limit, a network error), rerun the same command with
`--resume`: it skips finished trials and retries the ones that errored. The same
command without `--resume` adds a new set of trials after the ones already in the file. Options: `--setup` (`all`, the default, or a comma-separated list of `alone`, `jev`,
`jev-contra`), `--max-checks` (default 6),
`--max-tokens` (caps each model reply; use it for models that ramble), `--jev-model`
(default the pinned triage model), `--out`. A model is `provider:model`; a bare
provider uses its default (`anthropic` → `claude-opus-5`, `openai` → `gpt-5`).

## Run it without paid keys

Any OpenAI-compatible server works: set `OPENAI_BASE_URL` and use `openai:<model id>`.

| Where | Key | `OPENAI_BASE_URL` |
| --- | --- | --- |
| Your own machine, with [Ollama](https://ollama.com) | none | `http://localhost:11434/v1` |
| Groq free tier | free, from console.groq.com | `https://api.groq.com/openai/v1` |
| OpenRouter free models (ids end in `:free`) | free, from openrouter.ai | `https://openrouter.ai/api/v1` |
| Google Gemini free tier | free, from aistudio.google.com | `https://generativelanguage.googleapis.com/v1beta/openai` |

```
export OPENAI_BASE_URL=http://localhost:11434/v1
python3 rca_experiment.py --check --models openai:qwen3 --setup alone
python3 rca_experiment.py --models openai:qwen3 --setup alone --trials 1 --forced --html rca_report.html
```

- Take the model id from the service's model list; names change often.
- Free tiers are rate-limited. A run that stops on `HTTP 429` can simply be rerun:
  every finished trial is already saved.
- `--setup alone` needs no Jev key. Both Jev setups need `TYPESAFE_API_KEY` wherever
  the model runs.
- Small models often break the reply format; those trials count as invalid replies.
  Use these runs to test the pipeline, not as headline results.

## What would count as a result

Jev earns its place if, for most models, the jev column changes direction more
often, drops P(decoy) further, and blames the decoy less, without costing right
answers or mechanisms. It doesn't if models alone already change direction
reliably, or if Jev's scores move but the final answers don't improve.

## Known limits

- **Eight scenarios, all simplified.** Three are written by hand, and each author knew
  the answer while writing it. Five adapt public postmortems, but the telemetry is
  still written, not recorded, and each has a single cause. Models may have read the
  original postmortems; renamed services and changed numbers reduce that, and a
  held-out set of unpublished scenarios would remove it.
- **Few trials.** Five trials per cell show direction, not significance.
- **Stated beliefs.** The belief metrics use what the model says its probabilities
  are, which may not be calibrated. The final-answer metrics don't depend on them.
- **Keyword scoring.** Mechanism and component checks match words in the answer.
  Read the traces of any surprising trial.
- **A frozen menu.** The agent picks from fixed checks rather than writing
  queries, so it can't search the wrong interval or invent a service name itself.
