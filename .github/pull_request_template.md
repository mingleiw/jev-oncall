## What this changes

<!-- One or two sentences. Link the issue it closes, e.g. "Closes #12". -->

## How I tested it

<!-- Tests added or updated, and anything you ran by hand (the Docker demo, a curl command). -->

## Checklist

- [ ] `python3 -m unittest test_triage test_server test_dashboard test_config test_shadow test_live` passes
- [ ] No new path can drop, dedup or hide an alert that would have paged
- [ ] README updated if behavior or configuration changed
- [ ] Commits are signed off (`git commit -s`)
