# jev-oncall

Incident triage with [Jev](https://typesafe.ai), TypeSafe's System One decision model.
Each production alert gets one Jev call with four typed questions. Jev returns
probabilities, and plain code turns them into routing decisions. Jev never pages
anyone. It only judges.

## How it works

```
alert ──► rules ──► non-prod: LOG (no model call)
            │
            ▼
          Jev: 1 call, 4 questions ──(error / timeout / malformed)──► configured severity
            │                                                          (fail-open)
            ▼
          policy on probabilities ──► dedup graph ──► PAGE_NOW · PAGE · REVIEW · TICKET · LOG · DROP · DEDUP
```

| Question | Type | Used for |
| --- | --- | --- |
| `actionable` | Noul | Can drop an alert only when severity agrees it's noise |
| `severity` | Score: SEV4 < SEV3 < SEV2 < SEV1 | P(page) = P(SEV1) + P(SEV2) |
| `team` | Choice | Owner. Below 0.60, the runner-up team is notified too |
| `duplicate_of` | Choice over candidate alerts + `none` | Edges of the dedup graph |

### Routing policy

The thresholds live in `Policy` in `triage.py`. They are starting points: tune them
with `evaluate.py --sweep` on replayed history.

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

### Dedup as a graph

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

### Failure handling

- The model is pinned to `jev-1.13.0`, not `jev-latest`. An alias moves when
  TypeSafe ships a release, which can shift probabilities under the thresholds.
  Re-run the sweep before moving the pin. A warning prints if Jev answers as a
  different version.
- Each call gets a 2-second timeout and one retry. A retry that would wait more than
  1 second falls back instead: on a paging path, falling back beats waiting.
- `results.json` keeps every raw probability, so any decision, including every DROP,
  can be audited and re-routed offline.

## Evaluating it

The 14 synthetic alerts in `alerts.json` are a smoke test, not an evaluation. At that
size, the 11/12 severity agreement has a 95% interval of roughly 65 to 99%, and
calibration, which every threshold depends on, can't be measured at all.

The smoke test has a second limit. The alert titles start with Firing, Warning, or
Info, and routing on that configured severity alone already matches every label
except the two duplicates. So this set can only show what Jev adds through dedup.
Real alert streams have noisier configured severities, and measuring that gap is what
a replay is for.

Replay a few hundred historical alerts, labeled with the severity assigned after
each incident:

```
python3 triage.py --alerts history.jsonl --out replay.json --timeout 10 --retries 3 --max-wait 30
python3 evaluate.py replay.json --sweep
```

`evaluate.py` makes no API calls. It reports:

- **Outcomes that matter on-call:** silent misses; missed, delayed, false, and
  duplicate pages; pages to the wrong team; wrong incident links; review and ticket
  load. These appear side by side with the static baseline, meaning routing by
  configured severity without Jev.
- **Per-question agreement** with Wilson 95% intervals.
- **Calibration:** Brier score, ECE, and a reliability table for P(page) and
  P(actionable).
- **`--sweep`:** re-routes the stored answers under other thresholds, so you can
  choose them from data.

Alert format (JSON list or JSONL):

```json
{"id": "a01", "title": "...", "description": "...", "service": "checkout-api",
 "env": "prod", "started_at": "2026-09-18T14:03:00Z", "configured_severity": "critical",
 "expected": {"actionable": true, "severity": "SEV1", "team": "deploy", "duplicate_of": null}}
```

`expected` is optional and only used for evaluation.

## Run it

```
export TYPESAFE_API_KEY=<your key from console.typesafe.ai/keys>
python3 triage.py          # routing table + results.json, plus the evaluation if alerts are labeled
python3 triage.py -v       # also prints the reasons behind every decision
python3 generate_dashboard.py   # dashboard.html from results.json
python3 -m unittest -v     # offline tests with a fake Jev: no key, no network
```

The dashboard is one self-contained HTML file. It opens with a sentence saying what
paged someone and what is waiting for a human. Below that, every judged alert sits as
a dot on a P(page) scale, drawn against the policy's 0.20 and 0.80 bars. Alerts are
then grouped by outcome, with linked alerts nested under the incident they joined and
a "Why" panel holding each decision's reasons and raw probabilities. It ends with run
facts and, when alerts are labeled, the same outcomes, agreement, and calibration
numbers `evaluate.py` prints. It follows the system's light or dark setting.

Without a key, every alert takes the fail-open path, which shows the static baseline.
Standard library only.

## Files

- `triage.py`: Jev calls, routing policy, dedup graph, fallback, invariants
- `evaluate.py`: offline outcomes, agreement, calibration, threshold sweep
- `generate_dashboard.py`: renders `results.json` as `dashboard.html`
- `test_triage.py`, `test_dashboard.py`: offline tests with a fake Jev
- `alerts.json`: 14 synthetic alerts with the author's labels
- `topology.json`: service → upstream dependencies, used to narrow dedup candidates
- `results.json`, `dashboard.html`: written by `triage.py` and `generate_dashboard.py`
