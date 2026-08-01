# SMS Simulator — Alaris

A lightweight FastAPI service that simulates an SMS gateway (submit + delivery report flow) for testing the GetVerified / Alaris A2P messaging integration. It accepts submit requests the way a real SMSC would, then asynchronously sends a delivery report (`DELIVRD`/`UNDELIVRD`) callback a few seconds later.

Live URL: https://sms-simulator-alaris.onrender.com

## How it works

1. A submit request hits `GET /api`. Credentials and command are validated, a `message_id` is generated, and the message is placed on an internal queue. The endpoint responds immediately with `{"status": "submitted", "messageId": ...}` — it does **not** wait for delivery.
2. A fixed pool of background workers pulls messages off that queue, one at a time per worker, waits a randomized 5–8 second delay (simulating real network delivery time), then sends a delivery-report callback to the configured callback URL through the QuotaGuard static proxy.
3. Delivery status can be polled via `POST /sms/v2/pull-report`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api` | Submit an SMS. Requires `username`, `password`, `ani`, `dnis`, `message`, `command=submit`. |
| `POST` | `/sms/v2/pull-report` | Look up the delivery status of a previously submitted message by `transactionId`. |
| `GET`/`HEAD` | `/health` | Health check used by Render and uptime monitors. |

Test credentials: `testuser` / `testpass`.

## Architecture: queue + worker pool

Earlier versions spawned a new `asyncio.create_task` and a brand-new `httpx.AsyncClient` (new TCP/TLS handshake through the proxy) **per submitted message**. Under burst traffic (300–500+ messages/minute), this caused the Render Free instance to run out of its 512MB memory limit and crash, silently dropping every in-flight message.

The current design instead:
- Uses **one shared `httpx.AsyncClient`** with a bounded connection pool, created once at startup.
- Runs a **fixed number of worker coroutines** (`NUM_WORKERS`) that pull from a single `asyncio.Queue`, processing messages one at a time per worker instead of all at once.
- Adds **random jitter** (5–8s instead of a fixed 5s) to the delivery delay so a burst of submits doesn't wake up hundreds of workers in the same instant.

This keeps memory usage roughly constant regardless of how many messages arrive at once — incoming volume affects how long the queue is, not how much RAM is in use at any given moment.

## Status tracking and cleanup

Message statuses (`SENT` → `DELIVRD`/`UNDELIVRD`) are kept in an in-memory dict (`message_status_db`) so `/sms/v2/pull-report` has something to answer with. A background task clears out entries older than `STATUS_TTL_SECONDS` (default 300s) every 60 seconds — **but only entries that have already reached a final status**. A message still sitting in the queue (status `SENT`) is never deleted by the cleanup task, no matter how long it waits, so a busy queue can't cause a lookup to go missing.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `QUOTAGUARDSTATIC_URL` | — | Proxy URL used for outbound delivery callbacks (static IP for allowlisting). If unset, callbacks go out directly. |
| `NUM_WORKERS` | `10` | Number of concurrent delivery workers. Higher values process the queue faster but open more simultaneous connections — watch memory on constrained instances. |

## Requirements

```
fastapi
uvicorn
httpx
```

(`asyncio` and `uuid` are part of the Python standard library and don't belong in `requirements.txt`.)

## Deployment notes (Render)

This service currently runs on Render's **Free** instance type: 512MB RAM, 0.15 CPU. A few things follow from that:

- The **Metrics** tab only shows Outbound Bandwidth on Free — live Memory/CPU graphs require a paid instance type. The **Events** tab is the only way to see past OOM kills (`Instance failed: Ran out of memory`).
- The free instance spins down after 15 minutes of inactivity, adding 50+ seconds to the first request after idle.
- `NUM_WORKERS` is a tuning knob for this constraint: more workers drain the queue faster but each one may hold an open connection during its callback, so pushing it too high risks OOM again. Increase gradually and watch the Events tab after each change.

## Known limitations / possible next steps

- The queue and status dict are **in-memory only** — a restart or crash loses anything still in flight. Fine for a test simulator; would need Redis or a database for anything that must survive a restart.
- No `/stats` endpoint yet to see queue depth or worker load live — currently the only way to gauge backlog is reading logs or noticing delayed callbacks.
- Failure rate (currently a fixed 10%) and delay range (5–8s) are hardcoded — could be made configurable via environment variables for testing different network conditions.
- No automated tests (pytest) or CI yet to catch syntax/logic errors before deploy.