# RCA experiment: does the agent change its mind?

jev-oncall triages alerts. This experiment asks whether the same idea, Jev
judging evidence and plain code deciding, can help with the harder part:
finding the cause. It is an experiment, not a feature. Nothing here touches the
paging path.

## The question

An incident opens with a fresh deploy that looks guilty. The agent investigates.
One check, errors split by application version, shows that old and new
instances fail equally. That result contradicts the deploy theory.

**Does the agent change direction when that happens, and does it end up at the
real cause?**

## The scenario

`rca_scenarios/pool_exhaustion_v1.json` is one frozen incident: checkout errors
from database connection pool exhaustion at 14:07 UTC.

- **The decoy.** checkout-api v2.14.0 went to 6 of 12 instances at 14:02 and
  added a query against orders-db. The agent sees this before it runs any check.
- **The cause.** A batch job on orders-db, manually triggered at 14:01, holds
  row locks on the orders table. Queries wait on the locks, connections are held
  about 50x longer, and the pool fills on every instance.
- **Five hypotheses.** Deploy regression, slow queries, connection leak,
  traffic spike, pool config change. Each wrong one has a check that rules it
  out.
- **Traps.** Logs for `orders-service` return nothing because the service name
  is wrong. The orders-db log search times out. Neither is evidence that
  nothing happened.
- **Noise.** A downstream latency symptom, a cache flush and a certificate
  warning, all real, all unrelated.

The ground truth lives in `pool_exhaustion_v1.truth.json`. It is used for
scoring only. A test checks that no agent input contains it.

Evidence is frozen: every check returns what the telemetry showed at 14:10. The
scenario has 16 checks. The deploy list is shown up front, and the agent picks
up to 8 more from the other 15.

## Two setups

Same model, same scenario, same menu, same prompt wording.

| Setup | Who picks the next check | Whose beliefs we measure |
| --- | --- | --- |
| `baseline` | the reasoning model | the model's own, reported as JSON after every observation |
| `jev` | the reasoning model, which also sees Jev's scores | Jev's: after every observation, one call asks which hypothesis best explains all the evidence (Choice) and whether any observation contradicts each one (a Noul per hypothesis) |

In the `jev` setup the model still writes the final answer. What changes is
that every hypothesis is re-scored after every observation, and the score is a
probability, not a paragraph.

Trials alternate between setups so API drift hits both alike.

## Two modes

- **Free.** The agent chooses every check. Tests whether it looks for the
  evidence that could prove it wrong.
- **Forced** (`--force-version-check`). The agent's first check is replaced
  with the version check. Every trial sees the contradiction, so "does it change
  direction?" is measured on every trial, not only on the ones that happened to
  look.

## Metrics

Per trial, from `score_trial` in `rca.py`:

| Metric | Meaning |
| --- | --- |
| decoy on top at start | the deploy was the leading hypothesis after the first observation |
| ran the version check | the agent chose (or was forced) to split errors by version |
| changed direction | right after the version check, the leading hypothesis is no longer the deploy |
| drop in P(decoy) | how much the deploy's probability fell at the version check |
| right hypothesis | final answer is `slow_queries` |
| found the mechanism | right hypothesis, a lock or batch-job check was run, and the explanation mentions locks, the batch job or the archive |
| named the component | the answer names orders-db |
| blamed the decoy | final answer is `deploy` |
| evidence all observed | every check the answer cites was actually run |
| cited a failed/empty query | the answer cites one of the two traps |
| checks used, noise checks | cost, and how much went to unrelated signals |

"Right hypothesis" and "found the mechanism" are scored separately on purpose.
Naming slow queries is much easier than explaining why they got slow.

## Running it

```
export ANTHROPIC_API_KEY=...     # or OPENAI_API_KEY with --llm openai
export TYPESAFE_API_KEY=...
python3 rca.py run --setup both --trials 5
python3 rca.py run --setup both --trials 5 --force-version-check
python3 rca.py report rca_results.jsonl
```

`python3 rca.py run --dry-run` prints the prompt and the Jev payload without
calling anything. Every trial is appended to the results file with the full
step-by-step trace, so a surprising number can be traced back to the exact
beliefs and reasoning behind it.

Offline tests use a scripted model and a fake Jev:
`python3 -m unittest test_rca -v`.

## What would count as a result

- **Jev helps** if, in forced mode, the `jev` setup changes direction more
  often than `baseline` and finds the mechanism at least as often, at a similar
  number of checks.
- **Jev doesn't earn its place** if `baseline` matches it. One model with the
  same tools is simpler, and simpler wins ties.
- **Calibration** is a separate question. If Jev's scores move the right way
  here, that's one scenario. It says nothing yet about incident evidence in
  general.

## Known limits

- One scenario. The next ones should have a different true cause (a real
  deploy regression, so blaming the deploy is sometimes right), a compound
  failure, and a case where the right answer is "not enough evidence."
- A fixed menu of checks is easier than writing queries. It isolates the
  reasoning from query skill, which is the point for now.
- Five trials per setup is a smoke test, not statistics. Report counts, not
  percentages.
- Whoever writes a scenario knows its answer. The next one should come from
  someone who hasn't seen this one.
