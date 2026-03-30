import asyncio
import httpx
import logging
import uuid
import random
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
app = FastAPI()

VALID_USERNAME = "testuser"
VALID_PASSWORD = "testpass"
VALID_ACCOUNT = "111"

message_status_db = {}

QUOTAGUARD_URL = os.getenv("QUOTAGUARDSTATIC_URL", "").strip()


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
    message = params.get("message", "").strip()
    command = params.get("command", "").strip()

    logging.info(f"Received command value: {repr(command)}")

    if username != VALID_USERNAME or password != VALID_PASSWORD:
        return JSONResponse(
            {"status": "ERROR", "message": "Invalid credentials"},
            status_code=401
        )

    if not command or command.lower() not in ("submit", "s"):
        return JSONResponse(
            {"status": "ERROR", "message": "Invalid command"},
            status_code=400
        )

    message_id = str(uuid.uuid4())
    message_status_db[message_id] = "SENT"

    asyncio.create_task(simulate_delivery_status(message_id, ani, dnis, message))

    return JSONResponse({
        "status": "submitted",
        "messageId": message_id
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
            status_code=401
        )

    message_status = message_status_db.get(transaction_id, "UNKNOWN")

    return JSONResponse({
        "transactionId": transaction_id,
        "status": message_status,
        "count": count
    })


async def simulate_delivery_status(message_id, ani="", dnis="", message=""):
    await asyncio.sleep(5)

    status = "DELIVRD" if random.random() < 0.9 else "UNDELIVRD"
    message_status_db[message_id] = status

    callback_url = "http://62.67.222.164:8003/api"

    payload = {
        "command": "deliver",
        "dlvrMsgId": message_id,
        "dlvrMsgStat": status,
        "username": VALID_USERNAME,
        "password": VALID_PASSWORD,
        "ani": ani,
        "dnis": dnis
    }

    logging.info(f"🔄 Generating delivery status '{status}' for message {message_id}")
    logging.info(f"📤 Sending callback to: {callback_url}")
    logging.info(f"📦 Payload: {payload}")

    try:
        if QUOTAGUARD_URL:
            from urllib.parse import urlparse
            parsed = urlparse(QUOTAGUARD_URL)

            logging.info(f"🔍 QUOTAGUARD URL RAW: {repr(QUOTAGUARD_URL)}")
            logging.info(f"🔍 Using proxy: {bool(QUOTAGUARD_URL)}")
            logging.info(f"🔍 QUOTAGUARD scheme: {parsed.scheme}")
            logging.info(f"🔍 QUOTAGUARD hostname: {parsed.hostname}")
            logging.info(f"🔍 QUOTAGUARD port: {parsed.port}")
            logging.info("🔁 Using QuotaGuard static proxy")

            async with httpx.AsyncClient(proxy=QUOTAGUARD_URL, timeout=20) as client:
                ip_response = await client.get("https://httpbin.org/ip")
                logging.info(f"🚀 OUTBOUND IP VIA PROXY: {ip_response.text}")

                response = await client.get(callback_url, params=payload)
        else:
            logging.warning("⚠️ QUOTAGUARDSTATIC_URL is not set, using direct outbound")
            async with httpx.AsyncClient(timeout=20) as client:
                ip_response = await client.get("https://httpbin.org/ip")
                logging.info(f"🚀 OUTBOUND IP DIRECT: {ip_response.text}")

                response = await client.get(callback_url, params=payload)

        logging.info(f"📨 Callback response code: {response.status_code}")
        logging.info(f"📨 Callback response text: {response.text}")

    except Exception as e:
        logging.error(f"💥 Callback ERROR for {message_id}: {type(e).__name__} - {e}")