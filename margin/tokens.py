"""Signed, tamper-proof tokens for email links (confirm, manage, unsubscribe, feedback).

No accounts or passwords: possession of a valid link is the credential."""
import base64
import hashlib
import hmac
import json
import time


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload: dict, secret: str, ttl: int | None = None) -> str:
    payload = dict(payload)
    if ttl:
        payload["e"] = int(time.time()) + ttl
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify(token: str, secret: str, kind: str) -> dict | None:
    try:
        body, sig = token.split(".", 1)
        good = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        payload = json.loads(_unb64(body))
    except Exception:
        return None
    if payload.get("t") != kind:
        return None
    if "e" in payload and payload["e"] < time.time():
        return None
    return payload
