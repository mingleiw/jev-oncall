# jev-oncall

Incident triage with [Jev](https://typesafe.ai) (TypeSafe's System One decision
model): every alert gets **4 typed questions**, the answers come back as
probabilities, and plain code turns them into routing decisions. No LLM prose,
nothing to parse.

## How it works

```
alert ──► Jev (1 API call, 4 questions in parallel) ──► routing policy (code)
                                                                     │
                              ┌──────────┬──────────┬────────┬─────────┴────────┐
                              ▼          ▼          ▼        ▼                  ▼
                           PAGE NOW    PAGE     TICKET     DROP              HUMAN
                          (SEV1)     (SEV2)    (SEV3)    (noise)            REVIEW
```

**1. Ask.** Each alert is sent as `state` (title + description, plus the other
firing alerts for context) with four typed questions:

| Question     | Type   | Decides |
|--------------|--------|---------|
| `actionable` | Noul   | Does a human need to do something? (test canaries, dev-box metrics, and healthy-but-slow batch jobs are *not* actionable) |
| `severity`   | Choice | SEV1 (page now) / SEV2 (page) / SEV3 (ticket) / SEV4 (log), each with a one-line criterion |
| `team`       | Choice | database / compute / network / deploy |
| `duplicate`  | Noul   | Is this a downstream symptom of another firing alert? (bar is high: p ≥ 0.70, since dedup is destructive) |

Jev returns a probability per question — e.g. `severity: SEV1 @ 0.99`,
`duplicate: 0.31` — in a single parallel pass. All four questions are answered
in one call; extra questions add almost no latency.

**2. Route (plain code).** `triage.py` maps answers to actions: not actionable →
DROP, duplicate → DEDUP, SEV1 → PAGE NOW, SEV2 → PAGE, SEV3 → TICKET,
SEV4 → LOG. Jev never pages anyone itself — it only judges.

**3. Gate on confidence.** Every Choice answer carries a confidence number.
Severity confidence below 0.75 routes to **human review** instead of
auto-paging. This is the whole trick: the cheap model triages everything, and
humans only touch what it flags as unsure.

## Results (14 synthetic alerts, model `jev-1.13.0`)

- **9.2s total, ~658ms per alert** end-to-end (model latency is typically 70–500ms)
- **12,625 input tokens → $0.00053** at $0.042/MTok input; output tokens are free
  (~$0.00004/alert — about 26,000 alerts per dollar)
- Agreement vs author's labels: actionable **13/14**, severity **10/14**,
  team **12/14**, duplicate **13/14**

The misses are the feature: every disagreement came back low-confidence and
was routed to human review. Example: a TLS-cert-expiry warning got labeled
SEV1 at **0.18 confidence** — wrong label, but the system knew it didn't know,
so it escalated instead of paging.

## Run it yourself

```bash
export TYPESAFE_API_KEY=<your key from console.typesafe.ai/keys>
python3 triage.py
```

`triage.py` prints the routing table and writes `results.json` with every
answer, confidence, timing, and token count. One file, no dependencies beyond
the standard library.

## Files

- `triage.py` — the triage loop (Jev call → routing policy → confidence gate)
- `alerts.json` — 14 synthetic alerts with the author's expected labels
- `results.json` — full per-alert answers from the reference run
