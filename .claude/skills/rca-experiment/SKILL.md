---
name: rca-experiment
description: Run the jev-oncall RCA experiment on real models (each alone and with Jev), and report the results as a leaderboard. Use when asked to run, rerun, extend or report the RCA experiment or benchmark.
---

# Running the RCA experiment

The question: when a deploy looks guilty and a version check clears it, does the
agent change course, and does Jev re-scoring every hypothesis after each check help?
`guide/rca.md` has the design and metrics. This is the operating procedure.

## Rules

- **Keys come from environment variables only.** Never ask for a key in chat, print
  one, or write one to a file. Check presence with
  `python3 -c "import os;print({k: bool(os.environ.get(k)) for k in ('ANTHROPIC_API_KEY','OPENAI_API_KEY','OPENAI_BASE_URL','TYPESAFE_API_KEY')})"`.
  If one is missing, tell the user which variable to add in the environment settings.
- **Don't change the scenarios, prompts or scoring mid-experiment.** If what the model
  or Jev is sent must change, bump `HARNESS_VERSION` in `rca_experiment.py` and rerun
  everything: results from different versions are not comparable. A scoring-only change
  needs no rerun: `--report` re-scores saved traces.
- **Belief metrics are the model's own stated belief in every setup.** Jev's scores are
  reported on separate lines. Never compare the model's belief in one setup with Jev's
  in the other.
- **Report every trial.** Invalid replies, unfinished runs and errors count. Never drop
  or rerun a trial because its result looks wrong; `--resume` only retries trials that
  failed with a model or network error.
- **Never mix test data into results.** Model names starting `test:` are stand-ins.
- Report counts with their intervals ("4/5, CI 38–96"), forced and free mode
  separately, and each scenario as well as the total. Few trials show direction,
  not significance: say so.

## Procedure

1. `git pull origin main`, then run the tests:
   `python3 -m unittest test_triage test_server test_dashboard test_config test_shadow test_live test_reviews test_rca`
2. Pick the models with the user. A model is `provider:model`: `anthropic:<id>`, or
   `openai:<id>` for OpenAI or any OpenAI-compatible server (`OPENAI_BASE_URL`; see
   the guide's "Run it without paid keys" table). Without `TYPESAFE_API_KEY`, add
   `--setup alone` and say the Jev rows are missing. By default every run covers all
   three setups: `alone`, `jev` (the model sees Jev's ranking and contradiction scores)
   and `jev-contra` (the model sees only the hypothesis Jev judges most contradicted).
   Include at least one strong model and one weaker one: Jev can only show value where
   the agent fails alone.
3. Make a results folder: `R=rca_results/$(date +%F)-<short label>` and `mkdir -p $R`.
4. `python3 rca_experiment.py --dry-run --models <models> --trials 5 --forced` and
   check the plan's trial count with the user if it's large.
5. `python3 rca_experiment.py --check --models <models>`. Stop and report any FAIL.
6. Pilot: `python3 rca_experiment.py --models <models> --trials 1 --forced --out $R/pilot.jsonl`.
   Read one trace per model: does it follow the reply format, and do Jev's scores move
   after the version check? Drop a model from the full run if its pilot trials end in
   invalid replies or replies of many thousands of tokens (try `--max-tokens 4000`
   first), and say so. From the token counts, estimate the full run's cost and tell
   the user before continuing.
7. Full run, forced mode first:
   `python3 rca_experiment.py --models <models> --trials 5 --forced --out $R/forced.jsonl --html $R/forced.html`.
   If it stops (a rate limit, a network error), rerun the same command with `--resume`.
8. Then free mode, the same way, with `--out $R/free.jsonl --html $R/free.html`.
9. Commit `$R` on a new branch (`rca-results/<date>-<label>`), open a PR, and report
   to the user: per model, % Resolved for each setup with counts and intervals, changed
   course, blamed deploy, and anything odd you saw in the traces. Offer the HTML page.
