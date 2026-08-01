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

logging.basicConfig(level=logging.INFO)

VALID_USERNAME = "testuser"
VALID_PASSWORD = "testpass"
VALID_ACCOUNT = "111"

QUOTAGUARD_URL = os.getenv("QUOTAGUARDSTATIC_URL", "").strip()

# Koliko poruka se obradjuje ISTOVREMENO. Na Free planu (0.15 CPU / 512MB)
# drzi ovo malo (5-10). Ako podignes na Starter/Standard plan, mozes probati
# vece vrednosti (15-30) i pratiti Metrics tab da vidis gde puca.
NUM_WORKERS = int(os.getenv("NUM_WORKERS", "10"))

# Koliko dugo cuvamo status poruke u memoriji pre nego sto ga obrisemo (u sekundama).
STATUS_TTL_SECONDS = 300

# message_status_db: {message_id: (status, timestamp)}
message_status_db: dict[str, tuple[str, float]] = {}

# Red poruka koje cekaju da se obrade (asyncio.Queue = zivi u RAM-u ovog procesa).
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
        logging.warning("QUOTAGUARDSTATIC_URL is not set, using direct outbound")

    http_client = httpx.AsyncClient(**client_kwargs)

    # Pokreni worker-e i cleanup task u pozadini.
    for i in range(NUM_WORKERS):
        _background_tasks.append(asyncio.create_task(delivery_worker(i)))
    _background_tasks.append(asyncio.create_task(cleanup_old_messages()))

    yield

    for task in _background_tasks:
        task.cancel()
    await asyncio.gather(*_background_tasks, return_exceptions=True)
    await http_client.aclose()


app = FastAPI(lifespan=lifespan)


@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    return {"status": "ok"}


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

    # Jitter: 5-8 sekundi umesto fiksnih 5, da se burst od 500 poruka ne
    # probudi svih istovremeno u istoj sekundi.
    delay = 5 + random.uniform(0, 3)

    # Samo stavi u red - ne pravi novu konekciju/task odmah.
    await delivery_queue.put((message_id, ani, dnis, delay))

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
    """Jedan od NUM_WORKERS radnika. Vadi poruke iz reda i obradjuje ih
    JEDNU PO JEDNU - to drzi memoriju stabilnom bez obzira koliko poruka
    stigne odjednom na /api."""
    while True:
        message_id, ani, dnis, delay = await delivery_queue.get()
        try:
            await asyncio.sleep(delay)

            status = "DELIVRD" if random.random() < 0.9 else "UNDELIVRD"
            message_status_db[message_id] = (status, time.time())

            callback_url = "http://62.67.222.164:8003/api"
            payload = {
                "command": "deliver",
                "dlvrMsgId": message_id,
                "dlvrMsgStat": status,
                "username": VALID_USERNAME,
                "password": VALID_PASSWORD,
                "ani": ani,
                "dnis": dnis,
            }

            logging.info(f"[worker {worker_id}] Generating '{status}' for {message_id}")
            logging.info(f"[worker {worker_id}] Sending callback to: {callback_url}")

            response = await http_client.get(callback_url, params=payload)

            logging.info(f"[worker {worker_id}] Callback response code: {response.status_code}")
            logging.info(f"[worker {worker_id}] Callback response text: {response.text}")

        except Exception as e:
            logging.error(f"[worker {worker_id}] Callback ERROR for {message_id}: {type(e).__name__} - {e}")
        finally:
            delivery_queue.task_done()


async def cleanup_old_messages():
    """Sprecava da message_status_db raste beskonacno tokom velikih testova.
    NAPOMENA: 'SENT' zapisi se NIKAD ne brisu po vremenu - samo kad postanu
    DELIVRD/UNDELIVRD i onda prodje TTL. Ovo sprecava brisanje poruka koje
    jos cekaju u redu."""
    while True:
        await asyncio.sleep(60)
        cutoff = time.time() - STATUS_TTL_SECONDS
        expired = [
            mid for mid, (status, ts) in message_status_db.items()
            if status != "SENT" and ts < cutoff
        ]
        for mid in expired:
            del message_status_db[mid]
        if expired:
            logging.info(f"Cleanup: removed {len(expired)} expired message records")