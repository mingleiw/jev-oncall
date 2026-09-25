# Evaluating jev-oncall

How to measure whether jev-oncall routes your alerts well, and how fast it is.

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
  load. These appear side by side with **your current routing**: every alert routed by
  its configured severity alone (critical pages, warning tickets, info logs, in every
  environment). Shadow mode compares against the same baseline, so the page counts
  agree. Outcome rows count labeled alerts; pages and reviews sent count every alert.
- **Per-question agreement** with Wilson 95% intervals.
- **Calibration:** Brier score, ECE, and a reliability table for P(page) and
  P(actionable).
- **`--sweep`:** re-routes the stored answers under other thresholds, so you can
  choose them from data.

## Benchmarking latency

Latency is the one thing measurable without labeled history. `generate_alerts.py`
writes synthetic alerts — incident clusters with a root cause and downstream
alerts, plus noise — carrying no `expected` labels, because they measure how fast
triage runs, not how well it judges.

```
python3 generate_alerts.py --count 300 --out bench_alerts.json
python3 triage.py --alerts bench_alerts.json --out bench_results.json
python3 generate_dashboard.py bench_results.json --alerts bench_alerts.json \
  --out bench_dashboard.html --label "Synthetic benchmark"
```

The run prints per-call p50/p95/p99 and total cost, and `bench_results.json`
stores them under `summary`. Latency depends on your network path to the API, so
the number is yours, not a published claim.

### One measured run

300 synthetic alerts, 16 workers, default 2-second timeout. 233 reached Jev; the
other 67 were non-prod and never called the model.

| | |
| --- | --- |
| Per-call latency | min 204ms, p50 418ms, p90 674ms, p95 1477ms, p99 1748ms, max 1849ms |
| Wall clock | 7,642ms for all 300 |
| Cost | $0.0128 total, about $0.04 per 1,000 alerts |
| Payload | 304,175 input tokens, ~1,305 per call |
| Fallbacks | 0 |

Two things in that run matter more than the median.

**The tail nearly reaches the timeout.** The slowest call took 1,849ms against a
2,000ms limit — 151ms of margin. Nothing fell back this time, but a slower
network path would push the top of the distribution over the line, and those
alerts would route on configured severity instead. Latency between p90 and p95
jumps 2.2x, so the tail is where the risk lives, not the middle. Raise
`--timeout` or accept that the fail-open path is load-bearing.

**37% of judged alerts landed in REVIEW.** 86 of 233 fell between the 0.20 and
0.80 bars. That is the band working as designed, but it is also a lot of human
attention, and it says the default bars are wide for this alert mix. Tuning them
is what `evaluate.py --sweep` is for.

Both numbers come from one machine on one network path against synthetic alerts.
They say nothing about whether the routing was correct.

Accuracy and calibration are not measurable this way. Synthetic alerts carry the
author's guesses as labels, which is the same circularity the smoke test has. Use
replayed history for those.

Alert format (JSON list or JSONL):

```json
{"id": "a01", "title": "...", "description": "...", "service": "checkout-api",
 "env": "prod", "started_at": "2026-09-18T14:03:00Z", "configured_severity": "critical",
 "expected": {"actionable": true, "severity": "SEV1", "team": "deploy", "duplicate_of": null}}
```

`expected` is optional and only used for evaluation.
