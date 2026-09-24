# Contributing to jev-oncall

Thanks for looking. jev-oncall sits on a paging path, so the bar for changes is
"never makes it easier to miss a page". Everything else is open to discussion.

## Where to start

- **[Good first issues](https://github.com/mingleiw/jev-oncall/issues?q=is%3Aopen+label%3A%22good+first+issue%22)**
  are small and self-contained, and each one names the function to change and how
  to test it.
- **[Roadmap](https://github.com/mingleiw/jev-oncall/issues?q=is%3Aopen+label%3Aroadmap)**
  issues are bigger. Comment on one before starting, so we can agree on the shape
  first.
- Found a bug or want something else? [Open an issue](https://github.com/mingleiw/jev-oncall/issues/new/choose).

If you pick up an issue, leave a comment saying so, so two people don't do the
same work.

## Set up

You need Python 3.11 or newer and nothing else. jev-oncall uses the standard
library only, and the tests use a fake Jev, so you don't need an API key.

```
git clone https://github.com/mingleiw/jev-oncall
cd jev-oncall
python3 -m unittest test_triage test_server test_dashboard test_config -v
```

To see your change working end to end, run the Docker demo
(`cd demo && docker compose up --build`) and open <http://localhost:8090/dashboard>,
or run `python3 server.py` and post alerts to it with `curl`. The
[README](README.md#try-it-in-five-minutes) has both.

## Where things live

| File | What it does |
| --- | --- |
| `triage.py` | Jev calls, the routing policy, the dedup graph, fail-open, invariants |
| `server.py` | Webhook server: provider normalizers, signatures, the review clock, `/dashboard` |
| `evaluate.py` | Offline scoring and threshold sweeps over stored answers |
| `generate_dashboard.py` | Renders a run as one HTML page |
| `test_*.py` | Offline tests, one file per module |
| `docs/` | The website, served by GitHub Pages |

[docs/architecture.html](https://mingleiw.github.io/jev-oncall/architecture.html)
shows how an alert moves through the server.

## Rules that keep paging safe

Reviewers check every change against these. A change that breaks one needs a
very good reason in the PR description.

1. **Unsure costs a REVIEW, never silence.** No new path may DROP, DEDUP or
   otherwise hide an alert that would have paged, unless something else pages
   for it.
2. **Fail open.** If Jev can't answer, the alert routes by its configured
   severity, exactly as it would without jev-oncall.
3. **Keep `check_invariants()` meaningful.** If you add a new way to link or
   suppress alerts, add the invariant that catches it going wrong.
4. **Standard library only.** A new dependency needs a strong case.
5. **Tests use the fake Jev.** No test may need a key or the network.

## Adding a provider

Most integrations are a new webhook source. Follow the existing ones in
`server.py`:

1. Write `normalize_<name>(body)` with the `@provider("<name>")` decorator. It
   returns a list of alerts in the jev-oncall schema (`id`, `title`,
   `description`, `service`, `env`, `started_at`, `configured_severity`). Give
   each alert a stable id with `_stable_id()`, and set `"resolved": True` on
   resolved notifications.
2. Decide how the provider authenticates, and add it to `SIGNATURE_HEADERS` and
   `verify_signature()`.
3. Add tests in `test_server.py`: a firing payload, a resolved one, and a bad
   signature.
4. Add a row to the README's provider and signing tables.

## Pull requests

- Keep each PR to one change, and say in the description how you tested it.
- Run the full test suite before pushing. CI runs it on Python 3.11 to 3.13 and
  builds the Docker image.
- Update the README when behavior or configuration changes.
- Sign off your commits with `git commit -s`. The DCO check asks for a
  `Signed-off-by:` line, which certifies you wrote the change or have the right
  to submit it under the project's MIT license
  ([Developer Certificate of Origin](https://developercertificate.org/)).

## Reporting a security problem

Don't open a public issue for anything that could let someone inject, silence or
forge a page. Use GitHub's
[private vulnerability reporting](https://github.com/mingleiw/jev-oncall/security/advisories/new)
instead.
