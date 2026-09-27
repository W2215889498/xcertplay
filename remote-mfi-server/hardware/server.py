"""Remote MFi authentication server backed by a CH341A + MFi coprocessor.

Implements the protocol of
shared/.../mfi/RemoteMfiAuthenticationClient.kt with type="mfi":

  GET  /mfi/certificate -> {"protocolMajor", "type":"mfi", "certificate", "certificateSha256"}
  POST /mfi/sign        -> {"signature"}       body {"challenge","requestId"}
  POST /mfi/reset       -> {"detail":""}

The CH341A (USB) and the MFi coprocessor (I2C, address 0x11) must be attached to
the machine running this service.
"""

import base64
import hashlib
import os
import threading

from fastapi import FastAPI, Header, HTTPException, Request

from ch341 import Ch341Error, Ch341I2c
from mfi_chip import MfiChip, MfiChipError

VENDOR_ID = int(os.environ.get("CH341_VID", "0x1A86"), 16)
PRODUCT_ID = int(os.environ.get("CH341_PID", "0x5512"), 16)
SPEED = int(os.environ.get("CH341_SPEED", "0x61"), 16)
ADDRESS_7BIT = int(os.environ.get("MFI_I2C_ADDRESS", "0x11"), 16)
TOKEN = os.environ.get("MFI_TOKEN") or None

app = FastAPI()
_lock = threading.Lock()
_chip: MfiChip | None = None


def _open_chip() -> MfiChip:
    global _chip
    if _chip is None:
        try:
            transport = Ch341I2c(vendor_id=VENDOR_ID, product_id=PRODUCT_ID, speed_command=SPEED)
        except Ch341Error as error:
            raise HTTPException(status_code=503, detail=f"CH341 unavailable: {error}")
        _chip = MfiChip(transport, address7=ADDRESS_7BIT)
    return _chip


def _authorize(authorization: str | None) -> None:
    if TOKEN is not None and authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/mfi/certificate")
def certificate(authorization: str | None = Header(default=None)):
    _authorize(authorization)
    with _lock:
        try:
            chip = _open_chip()
            protocol_major = chip.protocol_major()
            certificate_bytes = chip.certificate()
        except (Ch341Error, MfiChipError) as error:
            # Drop the cached handle so a transient USB issue can recover.
            global _chip
            _chip = None
            raise HTTPException(status_code=503, detail=f"MFi chip error: {error}")
    return {
        "protocolMajor": protocol_major,
        "type": "mfi",
        "certificate": base64.b64encode(certificate_bytes).decode(),
        "certificateSha256": hashlib.sha256(certificate_bytes).hexdigest(),
    }


@app.post("/mfi/sign")
async def sign(request: Request, authorization: str | None = Header(default=None)):
    _authorize(authorization)
    try:
        body = await request.json()
        challenge = base64.b64decode(body["challenge"])
    except Exception:
        raise HTTPException(status_code=400, detail="invalid request body")
    if not 1 <= len(challenge) <= 128:
        raise HTTPException(status_code=400, detail="challenge must be 1..128 bytes")
    with _lock:
        try:
            signature = _open_chip().sign_challenge(challenge)
        except (Ch341Error, MfiChipError) as error:
            raise HTTPException(status_code=503, detail=f"MFi signing failed: {error}")
    return {"signature": base64.b64encode(signature).decode()}


@app.post("/mfi/reset")
def reset():
    with _lock:
        if _chip is not None:
            _chip.reset_session()
    return {"detail": ""}


@app.get("/mfi/health")
def health():
    with _lock:
        try:
            chip = _open_chip()
            return {"status": "ok", "protocolMajor": chip.protocol_major()}
        except Exception as error:
            return {"status": "error", "detail": str(error)}
