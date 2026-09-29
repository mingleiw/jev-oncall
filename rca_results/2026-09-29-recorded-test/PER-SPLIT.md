# Recorded-incident experiment, test split — per-app metrics (2026-09-29)

Scenario dir: `rca_recorded/re2-test` (frozen, commit aa59acb500a6ea14dfb5bec24e93ddc56ab09c90).
Jev-agent policy v5, `--max-checks 8`, one trial each. LLM investigators `alone`, same budget.
Infrastructure failures are excluded from every rate (counted separately), per the guide.

## Sock Shop (30 scenarios)

| investigator | trials ok | right service | verified (Jev) | supported diagnosis | mean checks | mean s/trial | infra |
|---|---|---|---|---|---|---|---|
| Jev-agent v5 | 30 | 29/30 | 8/30 | 17/30 | 7.23 | 5.2 | 0 |
| DeepSeek V4.1 Flash alone | 28 | 25/28 | - | 19/28 | 6.64 | 27.1 | 2 |
| GLM-5.3 alone | 29 | 28/29 | - | 22/29 | 6.52 | 112.9 | 1 |

## Train Ticket (30 scenarios)

| investigator | trials ok | right service | verified (Jev) | supported diagnosis | mean checks | mean s/trial | infra |
|---|---|---|---|---|---|---|---|
| Jev-agent v5 | 30 | 28/30 | 1/30 | 18/30 | 7.90 | 5.4 | 0 |
| DeepSeek V4.1 Flash alone | 30 | 28/30 | - | 20/30 | 6.30 | 36.6 | 0 |
| GLM-5.3 alone | 25 | 24/25 | - | 19/25 | 6.72 | 125.9 | 5 |

## Infrastructure failures (preserved in the JSONL as `model_error` records)

- DeepSeek: `rec_ss_9ef8613d59`, `rec_ss_eae5bba9c3` — model streaming stalled,
  300 s timeout hit on two attempts each. Recovered trials exist for the other
  three DeepSeek timeouts (`rec_ss_99cc74825f`, `rec_ss_f4e9fe8b8e`, `rec_tt_83e50d0447`).
- GLM-5.3: `rec_tt_5b07c4ccd5` — 300 s timeout on two attempts. Five further GLM
  trials (`rec_ss_ebb72cb3ab`, `rec_tt_7bf780462a`, `rec_tt_ab56c585ef`,
  `rec_tt_b22c4e1832`, `rec_tt_2b828164cf`) failed with HTTP 402: the OpenRouter
  account reached its $10 credit cap mid-run ($9.76 used by end of day).
  7 of 9 GLM timeouts recovered on retry.

## Notes / deviations

- Dev LLM sanity check finished late: 5/5 correct hypothesis (3/5 supported
  diagnosis) on five Online Boutique dev cases, but the last two cases ran
  after test trials had begun (preregistration-order deviation).
- No converter fixes: RE2 layout matched expectations; the dev misses were
  investigator check-selection misses, not parsing/window/wording bugs.
- `rca_experiment.py --compare` ignores `--scenario-dir` when run as
  `__main__` (dual-module `SCENARIO_DIR` global; `rca_jev_agent` imports its own
  copy). Worked around with an in-process driver script; no repo code changed.
- OpenRouter spend for the day: ~$6.24 ($3.52 -> $9.76 of a $10 cap).
  Jev-agent v5 test cost: ~$0.0042/trial, ~$0.25 total.
