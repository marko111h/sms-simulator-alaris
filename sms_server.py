import asyncio
import httpx
import logging
import uuid
import random
import traceback
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
app = FastAPI()

VALID_USERNAME = "testuser"
VALID_PASSWORD = "testpass"
VALID_ACCOUNT = "111"

message_status_db = {}

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

    username = params.get("username")
    password = params.get("password")
    ani = params.get("ani")
    dnis = params.get("dnis")
    message = params.get("message")
    command = params.get("command")

    logging.info(f"Received command value: {repr(command)}")

    if username != VALID_USERNAME or password != VALID_PASSWORD:
        return JSONResponse({"status": "ERROR", "message": "Invalid credentials"}, status_code=401)

    if not command or command.strip().lower() not in ("submit", "s"):
        return JSONResponse({"status": "ERROR", "message": "Invalid command"}, status_code=400)

    message_id = str(uuid.uuid4())
    message_status_db[message_id] = "SENT"
    asyncio.create_task(simulate_delivery_status(message_id))

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
        return JSONResponse({"status": "ERROR", "message": "Invalid credentials"}, status_code=401)

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

    
    callback_url = "https://sms.getverified.alarislabs.com:8004/api/"

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
    logging.info(f"📦 Payload: {payload}")\
    
    try:
            async with httpx.AsyncClient() as client:
                response = await client.post(callback_url, json=payload, timeout=10)
                if response.status_code == 200:
                    logging.info(f"✅ Callback SUCCESS for {message_id}: {response.status_code}")
                else:
                    logging.warning(f"❌ Callback FAILED for {message_id}: {response.status_code}")
    except Exception as e:
            logging.error(f"💥 Callback ERROR for {message_id}: {type(e).__name__} - {e}")