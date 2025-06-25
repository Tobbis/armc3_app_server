import base64
import io
import asyncio
import aiofiles
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, WebSocket, WebSocketDisconnect, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Optionally import Pillow if you wish to inspect or validate the image:
# from PIL import Image

app = FastAPI()

# toggle to store images locally
STORE_IMAGES = False
RECEIVED_IMAGES_DIR = Path(__file__).parent / "received_images"
if STORE_IMAGES:
    RECEIVED_IMAGES_DIR.mkdir(exist_ok=True)

# -------------- Models ----------------
class Base64Upload(BaseModel):
    label: Optional[str] = None
    image: str  # expect 'data:image/jpeg;base64,...' or plain base64

# -------------- Helper ----------------
def current_iso_timestamp() -> str:
    """Return current UTC time as ISO8601 string with timezone Z."""
    return datetime.now(timezone.utc).isoformat()

async def process_image_bytes(image_bytes: bytes) -> None:
    """
    Placeholder for any image processing. Keep minimal for low latency.
    If not needed, skip decoding to PIL.
    Example: to verify it's a valid JPEG, you could do:
      img = Image.open(io.BytesIO(image_bytes))
      img.verify()
    But this adds overhead. For pure echo server, skip this.
    """
    # For low-latency, do nothing or minimal checks.
    # If desired:
    # try:
    #     img = Image.open(io.BytesIO(image_bytes))
    #     img.verify()
    # except Exception as e:
    #     # handle invalid image
    #     pass
    return

def make_control_command(label: Optional[str] = None) -> dict:
    """
    Generate control command. Customize as needed: could depend on 'label' or other logic.
    Example returns a simple command with timestamp.
    """
    # Example logic: echo back label or send a fixed command
    cmd = {"action": "ACK"}  # simple acknowledgment
    if label:
        cmd["labelReceived"] = label
    # You can add other fields, version, flags, etc.
    return cmd

async def save_image(image_bytes: bytes, label: Optional[str] = None) -> None:
    if not STORE_IMAGES:
        return
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"_{label}" if label else ""
    filename = f"{timestamp}{suffix}.jpg"
    path = RECEIVED_IMAGES_DIR / filename
    async with aiofiles.open(path, "wb") as f:
        await f.write(image_bytes)

# -------------- HTTP endpoints ----------------

@app.post("/uploadBase64")
async def upload_base64(
    payload: Base64Upload,
    background_tasks: BackgroundTasks,   # add this
):
    """
    Accept JSON with base64-encoded image.
    Expects payload.image as 'data:image/jpeg;base64,...' or plain base64 string.
    """
    img_str = payload.image
    # Strip data URI prefix if present
    if img_str.startswith("data:"):
        # Find comma
        comma = img_str.find(",")
        if comma != -1:
            img_str = img_str[comma+1:]
    try:
        image_bytes = base64.b64decode(img_str)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid base64 image"})
    # Optionally process image bytes
    background_tasks.add_task(save_image, image_bytes, payload.label)
    await process_image_bytes(image_bytes)
    # Prepare response
    cmd = make_control_command(payload.label)
    response = {
        "command": cmd,
        "commandSentAt": current_iso_timestamp(),
    }
    return JSONResponse(content=response)

@app.post("/upload")
async def upload_form(
    background_tasks: BackgroundTasks,
    label: Optional[str] = Form(None),
    image: UploadFile = File(...),
):
    """
    Accept multipart/form-data with fields:
      - label: optional text
      - image: uploaded file
    """
    # Read file bytes asynchronously
    try:
        contents = await image.read()  # type: bytes
    except Exception:
        return JSONResponse(status_code=500, content={"error": "Failed to read uploaded file"})
    # Optionally process image bytes
    background_tasks.add_task(save_image, contents, label)
    await process_image_bytes(contents)
    # Prepare response
    cmd = make_control_command(label)
    response = {
        "command": cmd,
        "commandSentAt": current_iso_timestamp(),
    }
    return JSONResponse(content=response)

@app.get("/")
async def root():
    return {"message": "Image upload server is running"}

# -------------- WebSocket endpoint ----------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    Accepts JSON text messages: {"label": "...", "image": "data:image/jpeg;base64,..." }
    Responds with JSON: {"command": {...}, "commandSentAt": "..."}
    """
    await ws.accept()
    try:
        while True:
            # Receive text or binary
            data = await ws.receive()
            # data can be dict with keys: type, text, bytes, etc.
            if data["type"] == "websocket.receive":
                # If text message
                if "text" in data and data["text"] is not None:
                    try:
                        import json
                        msg = json.loads(data["text"])
                        label = msg.get("label")
                        img_str = msg.get("image")
                        if isinstance(img_str, str):
                            # Strip data URI prefix
                            if img_str.startswith("data:"):
                                comma = img_str.find(",")
                                if comma != -1:
                                    img_str = img_str[comma+1:]
                            image_bytes = base64.b64decode(img_str)
                        else:
                            # Invalid format
                            await ws.send_json({
                                "error": "Invalid message format: 'image' must be base64 string"
                            })
                            continue
                    except Exception:
                        await ws.send_json({"error": "Malformed JSON or base64 decode error"})
                        continue
                    # Optionally process image_bytes
                    asyncio.create_task(save_image(image_bytes, label))
                    await process_image_bytes(image_bytes)
                    # Prepare and send response
                    cmd = make_control_command(label)
                    await ws.send_json({
                        "command": cmd,
                        "commandSentAt": current_iso_timestamp(),
                    })
                # If binary message: interpret as raw image bytes
                elif "bytes" in data and data["bytes"] is not None:
                    image_bytes = data["bytes"]
                    asyncio.create_task(save_image(image_bytes))
                    # Optionally process image_bytes
                    await process_image_bytes(image_bytes)
                    # Prepare and send response
                    cmd = make_control_command()
                    await ws.send_json({
                        "command": cmd,
                        "commandSentAt": current_iso_timestamp(),
                    })
                else:
                    # Other message types (e.g., ping/pong) can be ignored or handled
                    pass
            elif data["type"] == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        # Client disconnected
        pass

# -------------- Run instructions ----------------
# To run: uvicorn fast_server:app --host 0.0.0.0 --port 5000 --workers 1
# For production, consider adding --loop uvloop (default) and tuning workers.
