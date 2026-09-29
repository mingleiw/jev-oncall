# Run 2 (preregistered): RE2-SS + RE2-TT instances 2-3 — jev-agent v5 vs v6

Scenario dir: `rca_recorded/re2-test2` (frozen, commit 51fc971 on rca-results/recorded-v2).
Free mode, `--max-checks 8`, 1 trial each. 120 scenarios (60 Sock Shop + 60 Train Ticket).
Suspect recall: 118/120 (two TT cases, rec_tt_37367fdc13 and rec_tt_a601c8faf7,
both ts-auth-service network faults, lost the cause in the 12-suspect cap).
No infrastructure failures in either run.

| | v5 all | v5 SS | v5 TT | v6 all | v6 SS | v6 TT |
|---|---|---|---|---|---|---|
| right service | 110/120 | 56/60 | 54/60 | 108/120 | 55/60 | 53/60 |
| supported diagnosis (primary) | 68/120 | 36/60 | 32/60 | 90/120 | 45/60 | 45/60 |
| verified | 10/120 | 5/60 | 5/60 | 11/120 | 8/60 | 3/60 |
| verified-but-wrong | 0 | 0 | 0 | 0 | 0 | 0 |
| mean checks | 7.67 | 7.50 | 7.85 | 7.90 | 7.83 | 7.97 |
| mean seconds/trial | 5.0 | 5.1 | 4.9 | 3.5 | 3.1 | 3.9 |
| mean $/trial | 0.00421 | — | — | 0.00407 | — | — |

Totals: v5 ~$0.51, v6 ~$0.49 for the 120 trials.

v6 trades 2 right-service picks for +22 supported diagnoses: the what-failed
phase (budget-2 for where, up to 2 checks for what failed inside the answer)
finds the mechanism evidence v5 never ran.
