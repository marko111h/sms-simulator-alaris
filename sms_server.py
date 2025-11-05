from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import asyncio
import logging
import uuid
import httpx

app = FastAPI()

VALID_USERNAME = "testuser"
VALID_PASSWORD = "testpass"
VALID_ACCOUNT = "111"

message_status_db = {}

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

    # Posle 5 sekundi, update status na DELIVRD i triggeruj callback
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

async def simulate_delivery_status(message_id):
    await asyncio.sleep(5)
    message_status_db[message_id] = "DELIVRD"

    # Slanje delivery reporta na eksterni callback URL
    callback_url = "https://api.getverified.alarislabs.com/api/"
    payload = {
        "messageId": message_id,
        "status": "DELIVRD",
    }
    logging.info(f"Callback payload sent: {payload}")
    logging.info(f"Callback URL: {callback_url}")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(callback_url, json=payload, timeout=10)
            logging.info(f"Callback sent for {message_id}: {response.status_code}")
    except Exception as e:
        logging.error(f"Failed to send callback for {message_id}: {e}")
