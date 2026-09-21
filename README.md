# Jev Incident Triage — POC

One Jev call per alert, 4 typed questions, zero prose to parse:

| Question     | Type   | What it decides |
|--------------|--------|-----------------|
| `actionable` | Noul   | does a human need to do something? |
| `severity`   | Choice | SEV1 / SEV2 / SEV3 / SEV4 |
| `team`       | Choice | database / compute / network / deploy |
| `duplicate`  | Noul   | downstream symptom of another firing alert? |

Routing policy is plain code (`triage.py`): not actionable → DROP,
duplicate → DEDUP, SEV1 → PAGE NOW, SEV2 → PAGE, SEV3 → TICKET,
SEV4 → LOG. Severity confidence < 0.75 → HUMAN REVIEW.

## Run results (2026-09-21, model jev-1.13.0)

- 14 synthetic alerts, 9.2s total, ~658ms per alert end-to-end
  (includes subprocess overhead; model latency typically 70–500ms)
- 12,625 input tokens → **$0.00053 total** at $0.042/MTok input, output free
  (~$0.00004/alert, ~26,000 alerts per dollar)
- Agreement vs author's labels: actionable **13/14**, severity **10/14**,
  team **12/14**, duplicate **13/14**

Notable: every miss carried low confidence and was routed to human review —
e.g. a TLS-cert-expiry warning labeled SEV1 at 0.18 confidence went to
HUMAN REVIEW instead of paging. The confidence gate is the escalation policy.

## Files

- `alerts.json` — 14 synthetic alerts with author's expected labels
- `triage.py` — the triage loop; prints routing table, writes `results.json`
- `results.json` — full per-alert answers, timings, token usage
