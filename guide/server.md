# Running the server

Configuration, the webhook server, the review clock, shadow mode, the dashboard, and each alert provider.

## Configure it

Your teams, thresholds, and service topology live in one TOML file, so you can
adopt jev-oncall without editing its code. Start from the example, which lists every
key with its default:

```
cp jev-oncall.example.toml jev-oncall.toml
python3 triage.py --config jev-oncall.toml
python3 server.py --config jev-oncall.toml     # or: export JEV_ONCALL_CONFIG=jev-oncall.toml
```

| Section | What it sets |
| --- | --- |
| `[jev]` | Pinned model, per-call timeout, retries, longest backoff |
| `[policy]` | Every routing threshold, the review ack window, and the fallback owner |
| `[teams]` | `name = "what it owns"`, 2 to 255 teams. Jev picks the owner from these descriptions, so write them the way you'd brief a new on-call engineer |
| `[topology]` | `service = ["upstream", ...]`, used to narrow dedup candidates |
| `[shadow]` | `log = "shadow.jsonl"` turns on [shadow mode](#shadow-mode) |
| `[server]` | `require_token = true` makes `/ack` and `/label` require the webhook secret as a bearer token ([details](#acting-from-the-dashboard)) |
| `[reviews]` | `store = "reviews.jsonl"` keeps reviews on disk so they survive a restart ([the review clock](#the-review-clock)) |

Every section is optional, and anything left out keeps its default. The file is
checked strictly: an unknown key, a threshold outside 0 to 1, or a `no_page_bar`
at or above `page_bar` stops the program before it triages anything, because a typo
that silently kept a default would change who gets paged. Command-line flags
(`--model`, `--timeout`, `--retries`, `--max-wait`, `--topology`) override the file.
Secrets stay in environment variables and never go in the file.

The config's teams and thresholds are written into `results.json`, so `evaluate.py`
and the dashboard judge a run by the policy it actually ran with.

## Run a batch from the command line

Python 3.11 or newer, standard library only.

```
export TYPESAFE_API_KEY=<your key from console.typesafe.ai/keys>
python3 triage.py          # routing table + results.json, plus the evaluation if alerts are labeled
python3 triage.py --config jev-oncall.toml   # with your teams and thresholds
python3 triage.py -v       # also prints the reasons behind every decision
python3 generate_dashboard.py   # dashboard.html from results.json
python3 -m unittest -v     # offline tests with a fake Jev: no key, no network
```

## Webhook server

`server.py` is an HTTP adapter that receives alerts from monitoring systems,
normalizes them into the `alerts.json` schema, triages with Jev, and returns
decisions as JSON. Standard library only.

```
export TYPESAFE_API_KEY=<your key from console.typesafe.ai/keys>
python3 server.py                           # localhost:8090
python3 server.py --host 0.0.0.0 --port 9000
```

## Docker

```
docker build -t jev-oncall .
docker run -p 8090:8090 -e TYPESAFE_API_KEY -e JEV_WEBHOOK_SECRET \
  -v $PWD/jev-oncall.toml:/config/jev-oncall.toml -e JEV_ONCALL_CONFIG=/config/jev-oncall.toml \
  jev-oncall
```

The image is `python:3.12-slim` plus the scripts: no dependencies to install. It
runs as a non-root user and has a health check on `/health`.

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| POST | `/ingest/<provider>` | Receive a webhook. Providers: `alertmanager`, `datadog`, `pagerduty`, `grafana`, `generic` |
| POST | `/ingest` | Same as `/ingest/generic` |
| POST | `/ack/<alert-id>` | Ack a REVIEW so it does not page. `X-Acked-By` names who took it |
| GET | `/health` | `{"ok": true}` |
| GET | `/recent` | Last 200 triage decisions (in-memory ring buffer) |
| GET | `/pending` | REVIEWs still waiting on an ack, with seconds left and deadline, plus recently closed ones and how they closed |
| GET | `/dashboard` | The last 500 alerts as the HTML dashboard, refreshing every 15 seconds. Each shows the decision it got on arrival and, for a review, where it stands now |
| POST | `/label/<alert-id>` | [Shadow mode](#shadow-mode): record what an alert really was. `X-Labeled-By` names who labeled it |
| GET | `/shadow` | [Shadow mode](#shadow-mode): how decisions compare with routing by configured severity |

## The review clock

![A REVIEW ends in one of three ways: an ack means someone is on it, so it closes without a page; a resolved notification cancels it; and no ack within 15 minutes escalates it to a PAGE.](../docs/review-clock.png)

A REVIEW is only meaningful if something escalates it. The server holds every
REVIEW for `Policy.review_ack_min` (15 minutes), and it ends one of three ways:

- **Acked** (`POST /ack/<id>`): someone is on it. The review closes and won't
  escalate. An ack doesn't resolve anything: the alert stays open until your
  monitoring clears it.
- **Cancelled**: the provider says the alert resolved before anyone acked, so no
  page is needed.
- **Escalated**: nobody acked in time. A sweeper turns it into a PAGE, records it in
  `/recent` with the reason, and counts it under `escalated_reviews`.

```
curl -X POST http://localhost:8090/ack/a07 -H 'X-Acked-By: alice'
curl http://localhost:8090/pending
```

Acking a review that already closed returns 409 with how it closed. The same alert
sent again keeps its first deadline, so resends can't push the page back, and a
closed review stays closed: until the alert resolves, or for an hour, another REVIEW
for it is a duplicate.

"Pages" here are decisions jev-oncall records. Delivering them to PagerDuty, Slack
or Jira isn't built yet; read them from `/recent`, `/pending` and the dashboard.

**Surviving restarts.** Set a review store:

```toml
[reviews]
store = "reviews.jsonl"      # or: python3 server.py --review-store reviews.jsonl
```

Every open, ack, cancellation and escalation is appended to that file and flushed to
disk before it counts. On startup the server replays it: pending reviews come back
with their original deadlines, any whose deadline passed while it was down page
immediately, acked and cancelled reviews stay closed, and an escalation already
recorded is never repeated. The file is compacted on startup to pending reviews plus
the last week of closed ones. Without a store, reviews live in memory, a restart
drops them, and the server warns at startup.

Supported: one server process per store file, on local disk (in Docker, a volume).
Not supported: several servers sharing a store, or a network filesystem. Decisions
and labels in the dashboard's last 500 alerts are in memory too; the shadow log
keeps them across restarts. `--sweep-interval` controls how often deadlines are
checked (default 10 seconds).

## Shadow mode

Shadow mode is how a team tries jev-oncall before trusting it. Run it next to
your existing paging, point your alerts at both, and it records what it would
have done. It never sends anything onward. Once outbound paging exists, shadow
mode will keep it switched off.

```
python3 server.py --config jev-oncall.toml --shadow-log shadow.jsonl
```

or set `[shadow] log = "shadow.jsonl"` in the config. Every decision goes to that
append-only JSONL file next to a **baseline**: the action routing by configured
severity takes, which is what most paging setups do today and exactly what
jev-oncall's fail-open path computes. Review outcomes (acked, escalated, cancelled
by a resolve) are logged too. The file survives restarts; in Docker, put it on a
volume.

`GET /shadow` summarizes the log: how many alerts matched your current routing,
and each kind of difference, with the latest differences and their reasons.

| Difference | What it means |
| --- | --- |
| Would page, your routing didn't | jev-oncall pages an alert configured as a warning or info |
| Would ask for a review, your routing didn't page | An unsure alert gets a person's attention instead of a ticket |
| Your routing paged, would ask for a review instead | Unsure: a person decides within 15 minutes, or it pages |
| Your routing paged, would fold into an incident that already pages | A duplicate page saved |
| Your routing paged, would not page | Held back, for example a non-production alert or one Jev judged minor |
| Would drop, nobody sees it | The only silent outcome. Check these first |

Differences only say where jev-oncall and your routing disagree, not which one was
right. For that, label alerts after the fact, for example in an incident review:

```
curl -X POST localhost:8090/label/<alert-id> -H 'X-Labeled-By: alice' \
  -d '{"severity": "SEV2", "actionable": true, "team": "database", "duplicate_of": null}'
```

`severity` and `actionable` are required. `team` must be one of your teams, and
`duplicate_of` is the alert that caused this one, if any. Then score the log the same
way as a replay, side by side with the baseline, including the threshold sweep:

```
python3 evaluate.py --shadow shadow.jsonl --sweep
```

Only labels posted to `/label` count; an `expected` block inside a webhook payload
is ignored. Like `/ack`, `/label` is open by default; see
[Acting from the dashboard](#acting-from-the-dashboard) to require a token.

## Acting from the dashboard

The live dashboard (`/dashboard`) is where people act, so nobody needs `curl`:

- **Reviews waiting for an ack** lists every open REVIEW with its clock counting
  down and an **Ack** button, then the reviews that closed and how: acked by whom,
  cleared, or paged. Each alert keeps the decision it got on arrival (**Sent to
  review**) and shows where its review stands now beneath it.
- **Compared with your current routing** (shadow mode) shows the counts above, the
  pages each side would have sent, and the latest differences, drops first.
- **What was this really?** (shadow mode) sits in each alert's **Why** panel: pick a
  severity, whether it needed a human, the owner, and the alert that caused it, if
  any, from the alerts received. Saving again replaces the label. Saved labels show
  on the alert and the page re-scores **Against the labels** right away.

After an Ack or a label, and every 15 seconds, the page re-renders from the server in
place, keeping your scroll position, open **Why** panels and focus. It skips a refresh
while you're typing or have a label half filled in.

To see it without running anything, open the
[interactive demo](https://mingleiw.github.io/jev-oncall/demo/): the Docker demo's
incident, with the judgments replayed from a real jev-1.13.0 run instead of calling Jev. The first time you act, the page
loads this repository's Python in your browser with [Pyodide](https://pyodide.org)
and runs the real review queue, routing and evaluation on a demo clock. Besides Ack
and labels, it can advance the clock 15 minutes (unacked reviews page), simulate a
Jev timeout (a new alert falls back to its configured severity) and clear an alert
before its deadline (its review is cancelled). Nothing leaves your browser; what you
did is kept there until **Start over**. `python3 build_demo.py` rebuilds it into
`docs/demo/`, and a test fails if the published copy is stale.

Type your name once at the top; it's remembered in your browser and recorded on
acks and labels.

`/ack` and `/label` are open by default. To require a token, set

```toml
[server]
require_token = true
```

with `JEV_WEBHOOK_SECRET` set; the server refuses to start without it. Either way,
requests a browser makes from another site are refused, so a page elsewhere can't
ack a review behind your back. Both endpoints
then need `Authorization: Bearer <JEV_WEBHOOK_SECRET>`, and the dashboard shows a
**Token** field. Reading `/dashboard`, `/recent` and `/shadow` stays open, so keep the
server on a network you trust either way. Config itself is never editable from the
page: it stays a file you review like code.

## Signing webhooks

Set `JEV_WEBHOOK_SECRET` and every ingest must carry an HMAC-SHA256 of the raw
body. Without it the server starts, warns, and accepts anything: unsigned ingest
lets anyone who can reach the port inject a page, or inject an alert crafted to
be chosen as a real incident's `duplicate_of` root and dedup that page into
silence.

| Provider | Header | Format |
| --- | --- | --- |
| `pagerduty` | `X-PagerDuty-Signature` | `v1=<hex>`, comma-separated during key rotation. Non-`v1` elements are ignored, never trusted |
| `grafana` | `X-Grafana-Alerting-Signature` | bare hex. Grafana's optional timestamped variant is not supported |
| `datadog`, `generic` | `X-Jev-Signature` | `sha256=<hex>` or bare hex, set as a custom header on the outgoing webhook |
| `alertmanager` | `Authorization` | `Bearer <secret>`. Alertmanager can't compute an HMAC, so the secret itself is the credential: it authenticates the sender, not the body, so serve it over TLS |

## Providers

**Alertmanager** (Prometheus) takes the standard v4 webhook. Point a receiver at
the server:

```yaml
receivers:
  - name: jev-oncall
    webhook_configs:
      - url: https://jev-oncall.internal:8090/ingest/alertmanager
        send_resolved: true
        http_config:
          authorization:
            credentials: <same value as JEV_WEBHOOK_SECRET>
```

The title is `alertname: summary`. The `description` annotation, the remaining
labels, and the `generatorURL` go into the description, so Jev sees the instance
and job. Service comes from the `service`, `job`, or `namespace` label, env from `env`
or `environment` (default `prod`), and severity from the `severity` label:
`critical`/`page`/`error` → critical, `warning` → warning, `info`/`none` → info, and
anything else pages.

Two Alertmanager behaviors need handling, and both are covered:

- **Group resends.** Alertmanager re-sends every alert in a group whenever the group
  changes. An alert already judged comes back under `repeats` and isn't judged or
  routed again. Each firing gets one id, from its fingerprint and `startsAt`, so
  a later re-fire of the same labels is judged fresh. A REVIEW also keeps its first
  deadline if the same alert is reviewed again, so no provider's resends can push a
  page back indefinitely.
- **Resolved notifications.** With `send_resolved: true`, a resolved alert is
  never triaged. If its REVIEW is still waiting on an ack, the review is cancelled
  and reported under `reviews_cancelled`, with `cancelled_by: resolved upstream` (a
  cancellation, not an ack): an alert that
  cleared on its own shouldn't page anyone. It stays a dedup candidate until it ages
  out, because things it caused can still arrive. Grafana resolved notifications get
  the same treatment.

**Generic** accepts the jev-oncall alert schema directly (`{id, title, description,
service, env, started_at, configured_severity}`), so any system can integrate by
posting normalized JSON.

**Datadog** maps monitor webhooks. Service and env are extracted from tags
(`service:X`, `env:Y`). Priority P1-P2 → critical, P3 → warning, P4-P5 → info.

**PagerDuty** handles v2 and v3 webhook subscriptions. Urgency high → critical,
low → warning. The `severity` field, if present, takes precedence.

**Grafana** handles both legacy and Unified Alerting webhooks. Severity is read
from the `severity` or `priority` label. Service is read from `service`, `job`,
or `namespace` labels.

Each provider generates a stable deterministic alert ID from `sha256(provider:raw_id)`,
so the same upstream alert always maps to the same triage ID.

## Example: a Datadog alert

```bash
curl -X POST http://localhost:8090/ingest/datadog \
  -H 'Content-Type: application/json' \
  -d '{"id": 12345, "title": "CPU > 90%", "tags": "service:api,env:prod", "priority": "P1"}'
```

## What the dashboard shows

The dashboard is one self-contained HTML file. It opens with a sentence saying what
paged someone and what is waiting for a human. Below that, every judged alert sits as
a dot on a P(page) scale, drawn against the policy's 0.20 and 0.80 bars. Alerts are
then grouped by outcome, with linked alerts nested under the incident they joined and
a "Why" panel holding each decision's reasons and raw probabilities. It ends with run
facts and, when alerts are labeled, the same outcomes, agreement, and calibration
numbers `evaluate.py` prints. It follows the system's light or dark setting.

Without a key, every alert takes the fail-open path, which shows the static baseline.
Standard library only.
