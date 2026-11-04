"""
HMAC-Op-Tokens: jede Anfrage aus AICampus trägt ein kurzlebiges Token
(Payload {task_id, student_id, op, exp=60s} + HMAC-SHA256-Signatur).

Format: <base64url(payload)>.<base64url(signature)>
Der Agent verifiziert Signatur + Expiry und prüft je Endpoint den
erwarteten `op` (Scope), z.B. "ws:ws-1-2-3" oder "task:1:7".

Zusätzlich: Der Agent bindet nur auf 127.0.0.1 — ohne gültiges Token
reicht der Netzwerkpfad nicht aus, ohne Tunnel erreicht niemand den Port.
"""

import base64
import hashlib
import hmac
import json
import logging
import time

from fastapi import Header, HTTPException, Request

from . import config

logger = logging.getLogger("compute_agent")
_WARNED_NO_KEY = False


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(key: str, op: str, task_id: int | None = None,
               student_id: int | None = None, ttl: int = 60) -> str:
    """Token signieren (wird auf der AICampus-Seite in compute_client genutzt;
    hier definiert, damit beide Seiten die exakt selbe Logik haben)."""
    payload = {"op": op, "task_id": task_id, "student_id": student_id,
               "exp": int(time.time()) + ttl}
    raw = _b64e(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    sig = hmac.new(key.encode(), raw.encode(), hashlib.sha256).digest()
    return f"{raw}.{_b64e(sig)}"


def verify_token_raw(token: str | None) -> dict:
    """Token verifizieren (Signatur + Expiry) OHNE Request-Objekt.

    Wird von der FastAPI-Dependency und vom WS-/Preview-Code geteilt
    (die haben keinen Header-Mechanismus). Wirft ValueError bei
    fehlendem/defektem/ungültigem/abgelaufenem Token.
    """
    global _WARNED_NO_KEY
    if not config.AGENT_KEY:
        if not _WARNED_NO_KEY:
            logger.warning(
                "AGENT_KEY leer — Auth deaktiviert (nur für Entwicklung)!")
            _WARNED_NO_KEY = True
        return {"op": "*", "task_id": None, "student_id": None}
    if not token or token.count(".") != 1:
        raise ValueError("Token fehlt oder defekt")
    raw, sig = token.split(".")
    try:
        expected = hmac.new(config.AGENT_KEY.encode(), raw.encode(),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(_b64d(sig), expected):
            raise ValueError
        payload = json.loads(_b64d(raw))
    except Exception:
        raise ValueError("Token-Signatur ungültig") from None
    if time.time() > float(payload.get("exp", 0)):
        raise ValueError("Token abgelaufen")
    return payload


def verify_token_with_key(token: str | None, key: str) -> dict | None:
    """Token gegen ein EXPLIZITES Key verifizieren (Signatur + Expiry).

    Für die AICampus-Seite: Dort kommt der Agent-Callback mit dem Key der
    jeweiligen Engine — da die Engine nicht bekannt ist, wird jeder
    registrierte Engine-Key einmal versucht. None = ungültig/abgelaufen.
    """
    if not token or not key or token.count(".") != 1:
        return None
    raw, sig = token.split(".")
    try:
        expected = hmac.new(key.encode(), raw.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64d(sig), expected):
            return None
        payload = json.loads(_b64d(raw))
    except Exception:
        return None
    if time.time() > float(payload.get("exp", 0)):
        return None
    return payload


def verify_token(request: Request,
                 token: str | None = Header(default=None, alias="X-Agent-Token")):
    """FastAPI-Dependency: liefert das verifizierte Payload-Dict."""
    try:
        return verify_token_raw(token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Token fehlt oder ungültig")


def require_op(payload: dict, op: str) -> None:
    """Scope-Prüfung: das Token darf nur für genau diese Operation gelten."""
    if payload.get("op") not in ("*", op):
        raise HTTPException(status_code=403, detail="Token-Scope passt nicht")
