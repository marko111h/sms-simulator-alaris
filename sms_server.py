import asyncio
import httpx
import logging
import uuid
import random
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

VALID_USERNAME = "testuser"
VALID_PASSWORD = "testpass"
VALID_ACCOUNT = "111"

CALLBACK_URL = "http://62.67.222.164:8003/api"

QUOTAGUARD_URL = os.getenv("QUOTAGUARDSTATIC_URL", "").strip()

# Koliko poruka se obradjuje ISTOVREMENO.
NUM_WORKERS = int(os.getenv("NUM_WORKERS", "50"))

# Koliko dugo cuvamo status poruke u memoriji pre brisanja (sekunde).
STATUS_TTL_SECONDS = 300

# Retry logika za callback.
MAX_RETRIES = 3
RETRY_DELAY = 10  # sekundi izmedju pokusaja

# message_status_db: {message_id: (status, timestamp)}
# Status ostaje "SENT" dok isporuka nije POTVRDJENA (200 od downstream-a).
message_status_db: dict[str, tuple[str, float]] = {}

# Red poruka koje cekaju obradu (zivi u RAM-u ovog procesa).
delivery_queue: asyncio.Queue = asyncio.Queue()

http_client: httpx.AsyncClient | None = None
_background_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client

    limits = httpx.Limits(
        max_connections=NUM_WORKERS,
        max_keepalive_connections=NUM_WORKERS,
    )
    client_kwargs = {"timeout": 20, "limits": limits}
    if QUOTAGUARD_URL:
        client_kwargs["proxy"] = QUOTAGUARD_URL
        logging.info(f"Using QuotaGuard static proxy, {NUM_WORKERS} max connections")
    else:
        logging.warning(f"QUOTAGUARDSTATIC_URL is not set, using direct outbound, {NUM_WORKERS} workers")

    http_client = httpx.AsyncClient(**client_kwargs)

    for i in range(NUM_WORKERS):
        _background_tasks.append(asyncio.create_task(delivery_worker(i)))
    _background_tasks.append(asyncio.create_task(cleanup_old_messages()))
    _background_tasks.append(asyncio.create_task(hourly_summary()))

    yield

    for task in _background_tasks:
        task.cancel()
    await asyncio.gather(*_background_tasks, return_exceptions=True)
    await http_client.aclose()


app = FastAPI(lifespan=lifespan)


@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    return {"status": "ok"}


@app.get("/stats")
async def stats():
    """Brzi uvid u stanje sistema - koliko poruka ceka, koliko je u bazi."""
    breakdown: dict[str, int] = {}
    for status, _ in message_status_db.values():
        breakdown[status] = breakdown.get(status, 0) + 1

    return {
        "queue_size": delivery_queue.qsize(),
        "num_workers": NUM_WORKERS,
        "tracked_messages": len(message_status_db),
        "status_breakdown": breakdown,
    }


@app.middleware("http")
async def log_requests(request: Request, call_next):
    logging.info(f"Received request: {request.method} {request.url}")
    response = await call_next(request)
    return response


@app.get("/api")
async def submit_sms(request: Request):
    params = dict(request.query_params)
    logging.info(f"API /api params: {params}")

    username = params.get("username", "").strip()
    password = params.get("password", "").strip()
    ani = params.get("ani", "").strip()
    dnis = params.get("dnis", "").strip()
    command = params.get("command", "").strip()

    logging.info(f"Received command value: {repr(command)}")

    if username != VALID_USERNAME or password != VALID_PASSWORD:
        return JSONResponse(
            {"status": "ERROR", "message": "Invalid credentials"},
            status_code=401,
        )

    if not command or command.lower() not in ("submit", "s"):
        return JSONResponse(
            {"status": "ERROR", "message": "Invalid command"},
            status_code=400,
        )

    message_id = str(uuid.uuid4())
    message_status_db[message_id] = ("SENT", time.time())

    # Jitter: 5-8 sekundi, da se burst ne probudi sav u istoj sekundi.
    delay = 5 + random.uniform(0, 3)

    await delivery_queue.put((message_id, ani, dnis, delay, 1))

    return JSONResponse({
        "status": "submitted",
        "messageId": message_id,
    })


@app.post("/sms/v2/pull-report")
async def pull_report(request: Request):
    params = dict(request.query_params)
    logging.info(f"API /sms/v2/pull-report params: {params}")

    account = params.get("account")
    transaction_id = params.get("transactionId")
    password = params.get("password")
    count = params.get("count")

    if account != VALID_ACCOUNT or password != VALID_PASSWORD:
        return JSONResponse(
            {"status": "ERROR", "message": "Invalid credentials"},
            status_code=401,
        )

    entry = message_status_db.get(transaction_id)
    message_status = entry[0] if entry else "UNKNOWN"

    return JSONResponse({
        "transactionId": transaction_id,
        "status": message_status,
        "count": count,
    })


async def delivery_worker(worker_id: int):
    """Jedan od NUM_WORKERS radnika.

    KLJUCNO: finalni status (DELIVRD/UNDELIVRD) se upisuje u message_status_db
    TEK kada downstream vrati HTTP 200. Dok se to ne desi, poruka ostaje "SENT",
    a cleanup NE BRISE "SENT" zapise - tako se nijedna poruka ne izgubi iz
    evidencije pre nego sto je isporuka stvarno potvrdjena.
    """
    while True:
        item = await delivery_queue.get()
        message_id, ani, dnis, delay = item[0], item[1], item[2], item[3]
        attempt = item[4] if len(item) > 4 else 1

        try:
            await asyncio.sleep(delay)

            status = "DELIVRD" if random.random() < 0.9 else "UNDELIVRD"
            payload = {
                "command": "deliver",
                "dlvrMsgId": message_id,
                "dlvrMsgStat": status,
                "username": VALID_USERNAME,
                "password": VALID_PASSWORD,
                "ani": ani,
                "dnis": dnis,
            }

            logging.info(f"[worker {worker_id}] Generating '{status}' for {message_id} (attempt {attempt}/{MAX_RETRIES})")
            logging.info(f"[worker {worker_id}] Sending callback to: {CALLBACK_URL}")

            response = await http_client.get(CALLBACK_URL, params=payload)

            if response.status_code == 200:
                # Isporuka POTVRDJENA - tek sada upisujemo finalni status.
                message_status_db[message_id] = (status, time.time())
                logging.info(f"[worker {worker_id}] Callback response code: {response.status_code}")
                logging.info(f"[worker {worker_id}] Callback response text: {response.text}")
            else:
                raise Exception(f"Non-200 response: {response.status_code}")

        except Exception as e:
            logging.error(
                f"[worker {worker_id}] Callback FAILED for {message_id} "
                f"(attempt {attempt}/{MAX_RETRIES}): {type(e).__name__} - {e}"
            )

            if attempt < MAX_RETRIES:
                # Vrati u red za jos jedan pokusaj.
                # Status ostaje "SENT" -> cleanup ga NECE obrisati.
                logging.info(
                    f"[worker {worker_id}] Requeue {message_id} for retry "
                    f"{attempt + 1}/{MAX_RETRIES} in {RETRY_DELAY}s"
                )
                await delivery_queue.put((message_id, ani, dnis, RETRY_DELAY, attempt + 1))
            else:
                # Svi pokusaji iscrpljeni - oznaci FAILED da ne ostane zauvek u memoriji.
                message_status_db[message_id] = ("FAILED", time.time())
                logging.error(
                    f"[worker {worker_id}] GIVING UP on {message_id} after {MAX_RETRIES} attempts"
                )

        finally:
            delivery_queue.task_done()


async def cleanup_old_messages():
    """Sprecava da message_status_db raste beskonacno.

    NAPOMENA: "SENT" zapisi se NIKAD ne brisu po vremenu - to su poruke koje
    jos cekaju u redu ili cekaju retry. Brisu se samo zapisi koji su dobili
    finalni status (DELIVRD / UNDELIVRD / FAILED) i kojima je proteklo TTL.
    """
    while True:
        await asyncio.sleep(60)
        cutoff = time.time() - STATUS_TTL_SECONDS

        expired_with_status = [
            (mid, status) for mid, (status, ts) in message_status_db.items()
            if status != "SENT" and ts < cutoff
        ]
        expired = [mid for mid, _ in expired_with_status]

        for mid in expired:
            del message_status_db[mid]

        if expired:
            details = ", ".join(f"{mid[:8]}={status}" for mid, status in expired_with_status)
            logging.info(f"Cleanup: removed {len(expired)} expired records: {details}")

async def hourly_summary():
    """Ispisuje sazet pregled jednom na sat - koliko poruka isporuceno/palo."""
    while True:
        await asyncio.sleep(3600)
        delivered = sum(1 for s, _ in message_status_db.values() if s == "DELIVRD")
        undelivered = sum(1 for s, _ in message_status_db.values() if s == "UNDELIVRD")
        failed = sum(1 for s, _ in message_status_db.values() if s == "FAILED")
        pending = sum(1 for s, _ in message_status_db.values() if s == "SENT")
        logging.info(
            f"HOURLY SUMMARY: {delivered} delivered, {undelivered} undelivered, "
            f"{failed} failed, {pending} pending, queue_size={delivery_queue.qsize()}"
        )