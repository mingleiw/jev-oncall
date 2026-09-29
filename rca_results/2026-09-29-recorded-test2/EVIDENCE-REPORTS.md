# v6 evidence reports (code-built, plain text)

Three examples: one verified right, one right-but-unverified with what-failed evidence, one wrong.

## rec_ss_1fc7315d30 (right and verified)

```
Root cause: carts (verified)
Where it showed:
  - latency_by_service: carts: 0.0403 -> 0.172 (4.27x, left normal range at +11s)
  - cpu_by_service: carts: 1.65 -> 68.86 (41.70x, left normal range at +4s)
  - carts_sockets: carts open sockets (carts_socket). Before: mean 10.80, max 11.00. After: mean 26.53, max 28.00 (2.46x the before mean). First left its normal range 4s after the alert time.
Ruled out: front-end, payment, rabbitmq-exporter, session-db, shipping, user, user-db
Checks run: 6: latency_by_service, carts-db_cpu, cpu_by_service, queue-master_cpu, carts_sockets, carts_memory
```

## rec_ss_0320c9b146 (right, not verified — what-failed evidence present)

```
Root cause: payment (not verified)
What failed:
  - payment_sockets: payment open sockets (payment_socket). Before: mean 3.28, max 4.00. After: mean 7.75, max 8.00 (2.36x the before mean). First left its normal range 19s after the alert time.
  - payment_memory: payment memory usage (payment_mem). Before: mean 5 MiB, max 5 MiB. After: mean 530 MiB, max 600 MiB (116.79x the before mean). First left its normal range 19s after the alert time.
Where it showed:
  - cpu_by_service: payment: 0.0895 -> 19.66 (219.75x, left normal range at +19s)
Ruled out: front-end, user
Not ruled out: orders
Checks run: 8: latency_by_service, errors_by_service, traffic_by_service, orders_latency, orders_cpu, cpu_by_service, payment_sockets, payment_memory
```

## rec_ss_6ed16da568 (WRONG: picked user-db, truth was user, not verified)

```
Root cause: user-db (not verified)
Where it showed:
  - user-db_sockets: user-db open sockets (user-db_socket). Before: mean 12.00, max 12.00. After: mean 17.78, max 18.00 (1.48x the before mean). First left its normal range 11s after the alert time.
  - user-db_disk: user-db disk I/O (user-db_diskio). Before: mean 1434455, max 1921328. After: mean 2098383, max 2643007 (1.46x the before mean). First left its normal range 43s after the alert time.
Ruled out: carts, catalogue, payment, shipping
Not ruled out: front-end, orders, user
Checks run: 8: latency_by_service, orders_latency, orders_cpu, user_latency, user_cpu, user-db_sockets, user-db_memory, user-db_disk
```
