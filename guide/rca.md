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
`jev-contra`, `jev-agent`; `all` means the three LLM setups, see
[Jev as the investigator](#jev-as-the-investigator-jev-agent)), `--max-checks` (default 6),
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

## Jev as the investigator (`jev-agent`)

In the `jev` and `jev-contra` setups, an LLM still picks every check and writes the
diagnosis; Jev only scores. The `jev-agent` setup (`rca_jev_agent.py`) removes the
LLM from the loop. Jev answers bounded questions about the evidence, and plain code
turns the answers into one action per round, validates it, runs the check, and
repeats. The hypotheses and the check menu are the benchmark's own, so nothing is
generated. It asks three questions, and doesn't assume the answer to any of them:

- Can Jev find the cause and collect evidence that supports it?
- Does replacing LLM calls cut latency and cost?
- Does it skip verification or stop too early?

**What Jev sees.** Exactly what the LLM setups see: the incident, the initial
context, the guidance about failed queries, the hypotheses, the check descriptions,
and the results of the checks already run. The observation list is identical to the
`jev` setup's payload. The hypotheses are part of the shared state, not only of some
questions' options. Jev judges each question on its own against the state, so
without this `enough_evidence`, `next_check` and the verification questions couldn't
see what the candidate causes are. Jev never sees an unrun check's result, the truth file, the
scoring keywords, or which check is the key check. `test_rca_agent.py` checks all
four on every scenario, including that no truth file is opened during a trial.

**One round.** It asks a single batched request about the current evidence. The
questions in it can't see each other's answers:

| Question | Type | Status |
| --- | --- | --- |
| `best_explanation` | Choice over the hypotheses | unchanged from the `jev` setup |
| `most_contradicted` | Choice over the hypotheses and none | unchanged |
| `supported` | Choice over the hypotheses and "none adequately supported" | new |
| `enough_evidence` | yes/no: would an SRE accept a diagnosis now | new |
| `next_check` | Choice over the checks not yet run, and none | new |

`best_explanation` keeps its tested wording and criteria. It has no "none" option;
the new `supported` question carries that instead, so the tested question isn't
changed. A second request follows only when code finds a candidate, because its
questions name the candidate:

- For each usable observed result: does it directly support the candidate? (yes/no)
- For each other hypothesis: has the evidence ruled it out? (yes/no)

These are independent judgments about one fixed proposal, so they are per-item
yes/no questions. Comparing hypotheses stays a Choice. Harness v4 moved
`most_contradicted` to a Choice after per-item questions went wrong there.

**The decision rule.** It lives in code (`candidate_from`, `verdict`), with
thresholds fixed before any live run:

1. With no evidence yet, it never concludes.
2. A candidate needs all of these:
   - `best_explanation` and `supported` pick the same hypothesis;
   - `supported` gives it at least 0.5;
   - `enough_evidence` is at least 0.7;
   - the candidate isn't also `most_contradicted`.

   If the answers disagree, the conflict is recorded and the investigation goes
   on. A high ranking alone never ends it.
3. To conclude, at least one usable observed result must directly support the
   candidate (P ≥ 0.5), and every alternative that is still plausible
   (`best_explanation` ≥ 0.1) must be ruled out (P ≥ 0.5). The test doesn't loosen
   when checks run out. A candidate that fails it at the budget ends in an
   abstention (`check_budget_exhausted_unverified`) that names the candidate as
   `tentative_hypothesis`. It is never scored as a diagnosis; whether the tentative
   pick was right is reported on its own row.
4. Otherwise it runs `next_check`'s top pick. If that pick is none, it abstains. It
   also abstains when it runs out of checks (the same budget as the other setups,
   6) or decision rounds (`--max-rounds`, default checks + 1) without a verified
   candidate.

Code recognizes failed and empty queries from the visible result text (`QUERY FAILED`,
`ERROR`, `0 matching log lines`, `No service found`), and never uses them as
evidence. A test checks that this matches every scenario's traps and nothing else.
Code also enforces the rest: a check must be on the menu and not already run, and
timestamps and budgets are kept by code. A failed or malformed Jev reply ends the
trial as `jev_error`, an infrastructure failure, with no LLM fallback.

**The result.** Each trace keeps the shape of the LLM traces, so the shared scoring
applies. It adds `diagnosis`:

- the decision (`diagnosis` or `abstain`) and the hypothesis;
- the supporting evidence IDs, with P for each;
- the evidence excluded as failed or empty;
- the alternatives ruled out, and those left unresolved;
- the stop reason and Jev's probabilities.

It also adds `diagnosis_digest` and `rounds`, a full trace of every call, its
timestamps and tokens, conflicts, verification and action.

**Optional explanation.** `--explain-model provider:model` has an LLM write the
component and mechanism, but only after the diagnosis is frozen. The writer gets a
copy, and the diagnosis digest is checked afterwards. If the writer names another
cause, that is recorded as a disagreement and nothing is changed. Its latency,
tokens and cost are reported on their own.

**Fair comparison.** Run `jev-agent` on the same scenarios, with the same digests,
the same mode and the same check budget as the LLM runs it is compared with.
Traces are credited to `jev:<jev model>`, never to the LLM of another run.
`--compare` reads several trace files and puts the three setups side by side (LLM
alone, LLM + Jev, Jev investigates). Each column is one system under one set of run
conditions: mode, check budget, round budget, harness and agent version. Forced and
free trials, or different budgets, are never merged. It warns when columns differ in
scenario digests, mode, check budget or harness version. Every trace now records its
check budget. Older LLM traces didn't, so the current default of 6 is assumed for
them and marked `*`. The trace can't confirm that assumption. The 2026-09-28 DeepSeek
and GLM hard runs never exceed 6 checks, which fits it. An older pilot in
`2026-09-27-free-models` has a 7-check trial, so don't compare with that one. It
reports:

- correct cause, incorrect conclusions, abstentions (and whether a tentative pick
  at the budget was right) and unfinished trials;
- infrastructure failures, counted apart and excluded from every rate;
- **supported diagnosis**, a rubric on check IDs that applies to every setup alike:
  the right cause, every cited check actually run, no failed or empty query cited,
  and at least one cited check that shows the mechanism;
- failed and empty queries run and cited;
- checks, LLM calls and Jev calls;
- end-to-end seconds, and input/output tokens by model;
- dollar cost from `rca_pricing.json`.

Two metrics need care:

- **Resolved** and the text mechanism score read generated text, and Jev writes
  none. So for `jev-agent` those metrics show "–". They are never earned by pasting
  a hypothesis description or evidence. The explanation's mechanism, if one was
  written, gets its own row.
- **LLM calls** for traces written before this change are estimated from the number
  of states, a lower bound, and are marked "≥".

`rca_pricing.json` prices Jev at the rate in `triage.py`: $0.042 per million input
tokens, with output free. LLM prices are null until someone fills them in with a
source, and a missing price prints as "unpriced", never as zero. The 2026-09-28
DeepSeek and GLM runs used OpenRouter's free tier.

A one-trial-per-scenario pilot on the five hard scenarios costs about 5 to 8 Jev
calls of 1-2k input tokens per trial, well under a cent in total:

```
S=edge_rule_regex,feature_file_flap,gateway_holiday_surge,host_patch_routes,registry_contention
export TYPESAFE_API_KEY=...
python3 rca_experiment.py --dry-run --setup jev-agent --scenarios $S
python3 rca_experiment.py --check   --setup jev-agent
python3 rca_experiment.py --setup jev-agent --scenarios $S --trials 1 \
    --out rca_results/<date>-jev-agent/pilot.jsonl --html rca_results/<date>-jev-agent/pilot.html
```

For the full free-mode comparison, run 5 trials per scenario, the same 25 as the
DeepSeek and GLM runs. Add `--explain-model` to also measure the explanation step,
for example `--explain-model openai:z-ai/glm-5.3` with `OPENAI_BASE_URL` set.

```
python3 rca_experiment.py --setup jev-agent --scenarios $S --trials 5 \
    --out rca_results/<date>-jev-agent/free.jsonl
python3 rca_experiment.py --compare rca_results/2026-09-28-v4-deepseek-hard/free.jsonl \
    rca_results/2026-09-28-v4-glm-hard/free.jsonl rca_results/<date>-jev-agent/free.jsonl \
    --html rca_results/<date>-jev-agent/compare.html
```

### Policy v2: answer the way the LLMs must

The first full run used policy v1 on the five hard scenarios (2026-09-28, 25
trials). It was never wrong, but it committed in only 7 of 24 trials and abstained
in 17. In 15 of those 17, Jev's top pick was the true cause at 0.98 or more, and
`supported` agreed. The `enough_evidence` answer peaked at 0.45–0.69, under v1's
0.7 bar, so verification never ran. v1 also isn't comparable with the LLM setups,
which can't abstain: an LLM out of checks is told to finish, and it always names a
cause.

v2 (`--agent-policy v2`; v1 stays available) changes only the decision
rule. The questions, thresholds for evidence and ruling out, budgets and what Jev
sees are unchanged:

1. **Stop early** only on a verified candidate, as in v1. A candidate now needs
   `best_explanation` and `supported` to agree with both at 0.9 or more, instead of
   `enough_evidence` ≥ 0.7. `enough_evidence` is still asked and recorded, but it no
   longer gates anything.
2. **Otherwise answer anyway**, like an LLM that runs out of checks. This happens
   when checks run out, when Jev judges no remaining check useful, or when rounds
   run out. The answer is `best_explanation`'s top pick. It is verified (one more
   Jev call if it wasn't verified that round) and labelled `verified: true/false`.
   Its supporting evidence is whatever verification accepted, which may be none.
3. **A `choice` field that disagrees with its probabilities** is logged in `warnings`,
   and code acts on the probabilities. In v1 that ended one trial as `jev_error`.

This makes the comparison with the LLMs like for like:

- **Correct cause:** every setup gives one answer per trial.
- **Supported diagnosis:** the ID rubric works the same way for every setup. An
  unverified answer with no accepted evidence fails it.
- **Verified (Jev only):** how often Jev had checked its answer before giving it.
  The LLMs have no equivalent.
- **Effort:** checks, calls, seconds, tokens and cost.

**Honest caveat.** v2 was designed after reading the v1 results on those five
scenarios, and the 0.9 bar sits below the values Jev reached there. They aren't a
clean test of v2. Run it on all eight scenarios and report the three hand-written
ones (`pool_etl_cron`, `pool_lock_batch`, `product_cache_partial`) separately, as the
held-out check. v1 and v2 traces never share a column in `--compare`.

```
A=edge_rule_regex,feature_file_flap,gateway_holiday_surge,host_patch_routes,registry_contention
H=pool_etl_cron,pool_lock_batch,product_cache_partial
R=rca_results/<date>-jev-agent-v2 && mkdir -p $R
python3 rca_experiment.py --setup jev-agent --scenarios $A --trials 5 --out $R/hard.jsonl --html $R/hard.html
python3 rca_experiment.py --setup jev-agent --scenarios $H --trials 5 --out $R/heldout.jsonl --html $R/heldout.html
python3 rca_experiment.py --compare rca_results/2026-09-28-v4-deepseek-hard/free.jsonl \
    rca_results/2026-09-28-v4-glm-hard/free.jsonl $R/hard.jsonl --html $R/compare.html
```

### Policy v3: ruled out means ruled out

The v2 run (2026-09-28, `rca_results/2026-09-28-jev-agent-v2/`):

- **Hard set:** 23/25 correct and 23/25 on supported diagnosis, in about 1.3 s and
  $0.0005 per trial. The LLM runs got 24–25/25 in 84–137 s.
- **Held-out set:** 15/15 correct.

But in every one of the 33 answers v2 called verified, some alternative had not been
ruled out. In 10 held-out trials that alternative was the initial suspect. The cause
was in v1 and v2's verification rule. It only required ruling out alternatives
whose *current* `best_explanation` was at least 0.1. Once Jev put about 1.0 on its
top pick, every alternative fell under that floor and was exempt, so being
outranked counted as being ruled out. pool_etl_cron was "verified" after a single
check.

v3 (`--agent-policy v3`, the default; v1 and v2 stay available) changes only that
rule. An alternative must be ruled out (P ≥ 0.5) if it was plausible at *any* point
in the investigation: its highest `best_explanation` so far, starting before the
first check, is 0.1 or more. The suspect everyone starts with must therefore be
ruled out by a specific result. An alternative that never looked plausible still
needs nothing. Everything else is v2: it always answers, and labels the answer
`verified: true/false`.

Expect more checks, more unverified answers, or both. Correct cause and supported
diagnosis should stay comparable with v2 if Jev's accuracy doesn't depend on
stopping early. Run it exactly like v2: both scenario sets, reported apart.

```
R=rca_results/<date>-jev-agent-v3 && mkdir -p $R
python3 rca_experiment.py --setup jev-agent --agent-policy v3 --scenarios $A --trials 5 --out $R/hard.jsonl --html $R/hard.html
python3 rca_experiment.py --setup jev-agent --agent-policy v3 --scenarios $H --trials 5 --out $R/heldout.jsonl --html $R/heldout.html
python3 rca_experiment.py --compare rca_results/2026-09-28-v4-deepseek-hard/free.jsonl \
    rca_results/2026-09-28-v4-glm-hard/free.jsonl rca_results/2026-09-28-jev-agent-v2/hard.jsonl \
    $R/hard.jsonl --html $R/compare.html
```

Limits specific to this mode:

- The thresholds are judgment calls, not fitted values. v2's early-stop bar was
  set after seeing v1's results; see the caveat above.
- A frozen menu favours a picker. Jev can't write a query the menu lacks, and
  neither can the LLMs here.
- The `next_check` answer is judged from check descriptions, and the check names
  can be suggestive for any investigator.

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
