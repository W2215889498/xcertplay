"""Minimal remote MFi/BAA authentication server for xcertplay.

Implements the protocol expected by
shared/src/main/java/com/shilapi/xcertplay/mfi/RemoteMfiAuthenticationClient.kt:

  GET  /mfi/certificate -> {"protocolMajor", "type", "certificate", "certificateSha256"}
  POST /mfi/sign        -> {"signature"}       body {"challenge", "requestId"}
  POST /mfi/reset       -> {"detail":""}

Optional Bearer auth via the MFI_TOKEN env var.
"""

import base64
import hashlib
import os
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding
from fastapi import FastAPI, Header, HTTPException, Request

# --- credentials -----------------------------------------------------------------
# type=baa: Apple-issued BAA/DeviceIdentity leaf + intermediate (DER) and the leaf key.
# type=mfi: MFi coprocessor certificate (DER) and its RSA private key (not exported by
#           most chips; only usable if you already have the key material).
MFI_TYPE = os.environ.get("MFI_TYPE", "baa")
PROTOCOL_MAJOR = int(os.environ.get("MFI_PROTOCOL_MAJOR", "2"))
TOKEN = os.environ.get("MFI_TOKEN") or None


def _read(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


LEAF = _read(os.environ["MFI_LEAF_DER"]) if MFI_TYPE == "baa" else _read(os.environ["MFI_CERT_DER"])
INTERMEDIATE = _read(os.environ["MFI_INTERMEDIATE_DER"]) if MFI_TYPE == "baa" else b""
PRIVATE_KEY = serialization.load_pem_private_key(_read(os.environ["MFI_KEY_PEM"]), password=None)

app = FastAPI()


def _authorize(authorization: str | None) -> None:
    if TOKEN is not None and authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


def _certificate_package() -> bytes:
    """BAA: [u32 BE leaf length][u32 BE intermediate length][leaf][intermediate].

    The SHA-256 in the response is computed over exactly these bytes.
    """
    if MFI_TYPE == "baa":
        return struct.pack(">II", len(LEAF), len(INTERMEDIATE)) + LEAF + INTERMEDIATE
    return LEAF


@app.get("/mfi/certificate")
def certificate(authorization: str | None = Header(default=None)):
    _authorize(authorization)
    package = _certificate_package()
    return {
        "protocolMajor": PROTOCOL_MAJOR,
        "type": MFI_TYPE,
        "certificate": base64.b64encode(package).decode(),
        "certificateSha256": hashlib.sha256(package).hexdigest(),
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

    if MFI_TYPE == "baa":
        # ECDSA P-256 over SHA-256, ASN.1 DER (X9.62) encoded.
        signature = PRIVATE_KEY.sign(challenge, ec.ECDSA(hashes.SHA256()))
    else:
        # RSA PKCS#1 v1.5 over SHA-256.
        signature = PRIVATE_KEY.sign(challenge, padding.PKCS1v15(), hashes.SHA256())

    return {"signature": base64.b64encode(signature).decode()}


@app.post("/mfi/reset")
def reset():
    # The client calls this when a new session starts; clear any per-session state here.
    return {"detail": ""}
