# SMS Gateway Simulator

A lightweight FastAPI service that simulates a real SMS gateway — accepts submit requests, then asynchronously sends realistic delivery-report callbacks (`DELIVRD`/`UNDELIVRD`). Built for teams testing A2P messaging integrations who don't want to burn real SMS credits on every test run.

If you're integrating with an SMS provider (Twilio-style, SMPP-adjacent HTTP APIs, or a custom aggregator like Alaris/DigiTouch) and need to verify your submit → delivery-report flow, error handling, and retry logic — this simulates the other side so you can test end-to-end without sending a single real text message.

## Why this exists

Testing an SMS integration usually means either:
- Sending real messages and paying for each one, or
- Mocking the HTTP calls in isolation, which never tests the full async delivery-report flow

This simulator behaves like an actual gateway: it accepts a submit, responds immediately, and — after a realistic delay — calls back to your delivery-report endpoint with a status. That means you can test your **whole pipeline**, including timing, retries, and how your system handles delayed or occasionally failed deliveries.

## How it works

1. `GET /api` — submit an SMS. Validates credentials and command, generates a message ID, and queues it. Responds immediately — it does not wait for delivery.
2. A fixed pool of background workers pulls from that queue, waits a randomized delay (simulating real network delivery time), then sends a delivery-report callback to your configured endpoint.
3. `POST /sms/v2/pull-report` — poll delivery status by transaction ID.
4. `GET /stats` — live view of queue depth, worker count, and message status breakdown.

## Architecture: queue + worker pool

Instead of spawning a new connection per message (which falls over under burst traffic), this uses:
- **One shared HTTP client** with a bounded connection pool
- **A fixed number of worker coroutines** pulling from a single queue — memory usage stays roughly constant no matter how many messages arrive at once
- **Randomized delay jitter** so a burst of submits doesn't wake up every worker in the same instant
- **Retry logic with backoff** — if a callback fails, the message is retried rather than silently lost, and its status stays `SENT` (not falsely marked delivered) until a callback actually succeeds

## Quick start

```bash
git clone <this-repo>
cd sms-simulator-alaris
docker compose up -d --build
```

Test it:
```bash
curl "http://localhost:8000/health"

curl "http://localhost:8000/api?username=testuser&password=testpass&ani=TEST&dnis=15550001234&message=Hello&command=submit"

curl "http://localhost:8000/stats"
```

## Configuration

All settings are environment variables (set them in `docker-compose.yml` or your environment):

| Variable | Default | Purpose |
|---|---|---|
| `CALLBACK_URL` | `http://62.67.222.164:8003/api` | Where delivery-report callbacks are sent. **Set this to your own endpoint.** |
| `SIM_USERNAME` / `SIM_PASSWORD` | `testuser` / `testpass` | Credentials the simulator accepts on `/api`. |
| `SIM_ACCOUNT` | `111` | Account value accepted on `/sms/v2/pull-report`. |
| `NUM_WORKERS` | `50` | Concurrent delivery workers. Raise for higher throughput, watch memory on small instances. |
| `DELIVERY_SUCCESS_RATE` | `0.9` | Fraction of messages marked `DELIVRD` vs `UNDELIVRD`. |
| `MIN_DELAY_SECONDS` / `MAX_DELAY_SECONDS` | `5` / `8` | Delay range before a delivery-report callback is sent. |
| `MAX_RETRIES` | `3` | Callback retry attempts before giving up. |
| `RETRY_DELAY_SECONDS` | `10` | Wait between retry attempts. |
| `STATUS_TTL_SECONDS` | `300` | How long a finished message's status is kept before cleanup. |
| `QUOTAGUARDSTATIC_URL` | *(unset)* | Optional HTTP proxy for outbound callbacks (e.g. if you need a static IP for allowlisting and aren't hosting on infrastructure that already provides one). |

## Deployment notes

- Runs comfortably on a small VPS (2 vCPU / 4GB handles 50+ concurrent workers with CPU usage in the low single digits under real traffic).
- If your callback destination requires IP allowlisting, deploy somewhere with a static outbound IP (most VPS providers give you one by default — no proxy needed).
- Docker logging is capped (`max-size`/`max-file` in `docker-compose.yml`) so logs don't grow unbounded under sustained traffic.

## Known limitations

- Queue and status tracking are in-memory — a restart loses anything still in flight. Fine for testing; would need Redis or a database for anything requiring durability across restarts.
- Single-process — for very high sustained throughput across multiple cores, you'd want to run multiple instances behind a load balancer.

## License

MIT — use it, modify it, deploy it for clients. See `LICENSE`.