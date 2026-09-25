# The Docker demo, in detail

More on the [five-minute demo](../README.md#try-it-in-five-minutes): what to look for, where to look, and what to do when it doesn't start.

## Timing

The first run builds the jev-oncall image and pulls Prometheus and Alertmanager, which
takes a minute or two. Exporting the key in your shell (`export TYPESAFE_API_KEY=...`)
works as well as `demo/.env`.

Each alert fires at a fixed time after Prometheus starts. Alertmanager groups alerts by
service and waits 5 seconds before sending a group, so each one reaches jev-oncall a
few seconds after it fires. The dashboard refreshes itself every 15 seconds.

| Fires at | Alert |
| --- | --- |
| +10s | `OrdersDbPoolExhausted` |
| +15s | `StagingDiskFull`, `SearchApiHighMemory` (clears at +75s) |
| +20s | `CheckoutApiErrorRate` |
| +25s | `PaymentServiceErrors` |
| +30s | `TlsCertExpiringSoon` |
| +35s | `ReportJobSlow` |
| +40s | `HomepageLatencyHigh` |

The alert text is in [demo/rules.yml](../demo/rules.yml), and
[demo/jev-oncall.toml](../demo/jev-oncall.toml) sets the teams and the service topology
(`payment-service` → `checkout-api` → `orders-db`) that dedup uses.

Pending reviews are kept in a review store on a Docker volume, so a restart picks them
up with their original deadlines; `docker compose down -v` clears them.

## With and without a key

**Without a key**, every alert takes the fail-open path and is routed by its
configured severity, the way it would be routed with no jev-oncall at all. The
dashboard shows 4 pages, 3 tickets, and 1 log. The three alerts from the database
incident page separately, the slow report job pages someone, and the homepage slowdown
only gets a ticket.

**With a key**, Jev judges each production alert, so compare against that baseline:

- Are `CheckoutApiErrorRate` and `PaymentServiceErrors` linked under
  `OrdersDbPoolExhausted`, so the incident pages once?
- Is `ReportJobSlow` held back from paging, and `HomepageLatencyHigh` raised?
- Which alerts land in REVIEW? Select any alert to see its probabilities and the
  reasons behind the decision in the inspector.

To replay the incident, for example after adding a key, restart the stack:

```
docker compose down && docker compose up
```

## Look around

| Where | What you see |
| --- | --- |
| <http://localhost:8090/dashboard> | Every decision, grouped by outcome, with the reasons behind it |
| <http://localhost:8090/recent> | The same decisions as JSON, including reviews that escalated to a page |
| <http://localhost:8090/pending> | REVIEWs waiting on an ack, with seconds left |
| <http://localhost:9090/alerts> | Prometheus: which demo alerts are firing |
| <http://localhost:9093> | Alertmanager: groups, and what it has sent |

A REVIEW pages if nobody acks it within 15 minutes. To ack one, copy its id from
`/pending` and run:

```
curl -X POST localhost:8090/ack/<id> -H 'X-Acked-By: you'
```

`docker compose logs -f jev-oncall` shows each delivery as it arrives.

## If something goes wrong

| Symptom | Fix |
| --- | --- |
| `port is already allocated` | Something else uses 8090, 9090, or 9093. Change the left side of `ports:` in `demo/docker-compose.yml`, for example `"18090:8090"`, and open that port instead |
| `429 Too Many Requests` while pulling images | Docker Hub limits anonymous pulls. Run `docker login`, or wait and try again |
| The dashboard says 0 alerts | Wait 30 seconds. If it stays empty, `docker compose logs alertmanager` shows whether deliveries are failing |
| jev-oncall logs `POST /ingest/alertmanager ... 401` | The token in `demo/alertmanager.yml` must match `JEV_WEBHOOK_SECRET` in `demo/docker-compose.yml`. Both are `demo-secret` |
| Every alert says "Fallback: Jev unavailable" even with a key | Confirm the container received the key with `docker compose exec jev-oncall printenv TYPESAFE_API_KEY`, then check `docker compose logs jev-oncall` for the error |

## Without Docker

You need Python 3.11 or newer and nothing else. Start the server with the demo config,
send it the 14 bundled alerts, and open the same dashboard:

```
python3 server.py --config demo/jev-oncall.toml
curl -X POST localhost:8090/ingest -H 'Content-Type: application/json' -d @alerts.json
```

Then open <http://localhost:8090/dashboard>. This skips Prometheus and Alertmanager,
so every alert arrives at once instead of in sequence.
