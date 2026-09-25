# How the routing works

The policy, the dedup graph and the failure handling behind each decision. The [README](../README.md#how-it-works) has the overview.

```
alert ──► rules ──► non-prod: LOG (no model call)
            │
            ▼
          Jev: 1 call, 4 questions ──(error / timeout / malformed)──► configured severity
            │                                                          (fail-open)
            ▼
          policy on probabilities ──► dedup graph ──► PAGE_NOW · PAGE · REVIEW · TICKET · LOG · DROP · DEDUP
```

Only the Jev call leaves the server.

## The four questions

| Question | Type | Used for |
| --- | --- | --- |
| `actionable` | Noul | Can drop an alert only when severity agrees it's noise |
| `severity` | Score: SEV4 < SEV3 < SEV2 < SEV1 | P(page) = P(SEV1) + P(SEV2) |
| `team` | Choice over your [teams](server.md#configure-it) | Owner. Below 0.60, the runner-up team is notified too |
| `duplicate_of` | Choice over candidate alerts + `none` | Edges of the dedup graph |

## Routing policy

The thresholds are set in the `[policy]` section of the [config file](server.md#configure-it),
with defaults in `Policy` in `triage.py`. They are starting points: tune them with
`evaluate.py --sweep` on replayed history.

| Condition | Action |
| --- | --- |
| `env` is not prod | LOG. A rule, not a model call |
| Jev error, timeout, or malformed answer | The alert's `configured_severity`: critical → PAGE, warning → TICKET, info → LOG |
| P(page) ≥ 0.80 | PAGE_NOW if P(SEV1) ≥ P(SEV2), otherwise PAGE |
| 0.20 < P(page) < 0.80 | REVIEW: low urgency, and it pages if nobody acks it within 15 minutes |
| P(page) ≤ 0.20 and P(actionable) ≤ 0.05 | DROP |
| Otherwise | TICKET |

The bars are asymmetric on purpose. DROP is the only outcome no human ever sees, so it
needs the most certainty. Being unsure costs a REVIEW, never silence. When `actionable`
and `severity` disagree, the more urgent answer wins and the disagreement is logged.

## Dedup as a graph

1. **Candidates.** `duplicate_of` offers other production alerts that started up to
   30 minutes before this one, or up to 2 minutes after (delivery jitter). If
   `topology.json` lists the alert's service, candidates are limited to that service
   and its upstream dependencies. At most 50 are offered; Choice allows 255.
2. **Edges.** An alert links to its most likely cause when that probability is at
   least 0.70 and the cause isn't itself dropped or logged.
3. **Cycles.** If alerts name each other, the loop is broken at whichever started
   first, so two alerts can never dedup each other into silence.
4. **Clusters.** Each cluster's root gets the most urgent action of any member.
   Members owned by the root's team are DEDUPed. A member owned by another team that
   would have paged gets a REVIEW instead, so a wrong link can delay another team by
   the ack window but never silence it.

`check_invariants()` exits the run with status 1 if a linked alert's root is less
urgent than the alert itself, or if anything was dropped without a model judgment.

## Failure handling

- The model is pinned to `jev-1.13.0`, not `jev-latest`. An alias moves when
  TypeSafe ships a release, which can shift probabilities under the thresholds.
  Re-run the sweep before moving the pin. A warning prints if Jev answers as a
  different version.
- Each call gets a 2-second timeout and one retry. A retry that would wait more than
  1 second falls back instead: on a paging path, falling back beats waiting.
- Every call carries one `Idempotency-Key`, reused by its retry. A client-side
  timeout does not mean Jev failed to answer, so retrying under a fresh key would
  judge the alert twice and be billed twice.
- `results.json` keeps every raw probability, so any decision, including every DROP,
  can be audited and re-routed offline.

## Hardening

- **Connection pooling.** `triage.py` keeps a thread-safe pool of `HTTPSConnection`
  objects (up to 16) with keep-alive, avoiding a TLS handshake on every Jev call.
  Connections are returned to the pool on success and discarded on error.
- **Response validation.** Probabilities are checked for NaN and Inf. Choice answers
  (`team`, `duplicate_of`) are verified to match the max-probability entry in their
  distribution. Malformed answers trigger fail-open.
- **Rate limiting.** The webhook server enforces a sliding-window rate limit (default
  120 requests per minute, configurable with `--rate-limit`). Excess requests get a
  429 response.
- **Input validation.** `validate_alert()` enforces non-empty `id` and `title`,
  clamps field lengths, normalizes unknown severities to `critical`, and replaces
  malformed timestamps with the current time.
- **Alert staleness.** Active alerts older than one hour are pruned from the server's
  in-memory set, preventing unbounded memory growth on long-running instances.
- **Backoff.** Retries use escalating `0.5 * 2^attempt` delays. 429 responses honor
  the `Retry-After` header when present.
