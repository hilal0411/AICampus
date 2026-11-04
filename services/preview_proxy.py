"""
Backend-Preview-Proxy (ASGI-Routes auf dem Haupt-Port).

Browser (iframe/WS) ⇄ hier (ASGI, Haupt-Port) ⇄ Agent-Preview-Server
(TCP :8701, HMAC-Op-Token) ⇄ docker-exec-Relay ⇄ Ziel-Server im
Student-Container.

Warum ASGI-Routes auf dem Haupt-Port statt raw-TCP-Server auf einem
zweiten Port (früher :8100): Hinter einem HTTPS-Reverse-Proxy ist ein
zweiter published Port nur als plain HTTP erreichbar → Mixed Content →
Browser blockiert iframe/WS. Same-Origin (Haupt-Port) funktioniert
immer und spart einen zu publizierenden Port.

Browser-Seite:
- HTTP-Requests (Seiten, Assets, SSE, Uploads): Streaming-Route.
- WebSocket: Uvicorn erledigt das Browser-Handshake + Frame-Decoding
  selbst (ASGI-WebSocket-Scope); wir brücken zur Agent-Leiste, die
  nach dem 101 rohe, UNMASKED Frames liefert (Ziel → Agent → uns).
  RFC-6455-Frame-Coding daher nur für die Agent-Leiste:
  inbound dekodieren (unmasked), outbound encodieren (masked —
  hier sind wir der Client).

Auth: access_token-Cookie (JWT, stateless) + Task/Course-Check wie
Student-API. Key = ws-{course}-{task}-{user} aus der SESSION (User-ID
kommt aus dem Token, nicht aus dem Pfad → kein Cross-Student-Zugriff).

Antwort-Sanitizing: X-Frame-Options + Content-Security-Policy werden
entfernt (sonst blockieren Jupyter & Co. das iframe; lokale
Single-User-Installation).

Bekannte Einschränkung: Absolute Asset-Pfade in der previewten App
(z. B. `/static/…`) lösen sich gegen den AICampus-Origin auf → nur
relative Pfade laden zuverlässig (wie beim früheren Zweit-Port-Design).
"""

import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import struct
import time
from datetime import timedelta
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from fastapi import APIRouter, Query, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from sqlmodel import select
from starlette.responses import Response
from starlette.websockets import WebSocket as StarletteWebSocket

from compute_agent.auth import make_token
from compute_agent.preview_pipe import (HeadError, build_request_head,
                                        parse_response_head, read_head)
from config import PREVIEW_AGENT_PORT, PREVIEW_BASE_DOMAIN, SECRET_KEY

logger = logging.getLogger("aicampus.preview")

_HEAD_TIMEOUT = 90.0   # Agent-Head (Relay-Spawn + Ziel-Connect inkl.)
_BODY_IDLE_TIMEOUT = 120.0  # Agent liefert Body nicht weiter → abbrechen
_BODY_CAP = 100 * 1024 * 1024   # Request-Body-Cap (Uploads)
_WS_MSG_CAP = 32 * 1024 * 1024  # WS-Message-Cap pro Richtung

router = APIRouter()


def _json(status: int, detail: str) -> JSONResponse:
    """JSON-Fehlerantwort (Status + Detail; JSONResponse-Args in richtiger Reihenfolge)."""
    return JSONResponse({"detail": detail}, status_code=status)


class _AuthError(Exception):
    """4xx-Antwort (Status + Nachricht) — kein Tunnel-Setup nötig."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _WsEof(Exception):
    """EOF auf der Agent-Leiste (während Frame-Lesens)."""


# ── Header-Hilfen ─────────────────────────────────────────────────

def _sanitize_cookie(raw: str) -> str | None:
    """Cookie-Header ohne das AICampus-access_token weitergeben.

    Cookies sind pro HOST (nicht pro Port) → die Ziel-App (Jupyter & Co.)
    setzt auf demselben Origin eigene Cookies, die der Browser in
    derselben Cookie-Header wie access_token schickt. Ohne Weiterreichen
    verliert die Ziel-App ihre Sessions (Login-Loop); das access_token
    selbst darf den Container nicht erreichen.
    """
    parts = [p.strip() for p in raw.split(";") if p.strip()]
    keep = [p for p in parts if p.split("=", 1)[0].strip() != "access_token"]
    return "; ".join(keep) if keep else None


def _cookie_value(raw: str, name: str) -> str:
    for part in raw.split(";"):
        kv = part.strip()
        if kv.startswith(name + "="):
            return kv.split("=", 1)[1]
    return ""


def _up_headers(headers, *, upgrade: bool) -> list[tuple[str, str]]:
    """Browser-Header für die Agent-Leiste (Namen klein).

    Weg: host/cookie (→ _sanitize_cookie, separat wieder an),
    Connection/Keep-Alive/TE (von build_request_head neu gesetzt),
    Proxy-Header, sec-websocket-extensions (s. u.). Bei `upgrade=True`:
    sec-websocket-* mitnehmen + `Connection: Upgrade`/`Upgrade: websocket`
    setzen (build_request_head erkennt das Upgrade am Connection-Header).
    """
    out: list[tuple[str, str]] = []
    ws: dict[str, str] = {}
    for name, value in headers.items():
        n = name.lower()
        if n in ("host", "cookie", "connection", "keep-alive",
                 "transfer-encoding", "upgrade", "proxy-authorization",
                 "proxy-authenticate", "te", "trailer"):
            continue
        if n in ("sec-websocket-key", "sec-websocket-version",
                 "sec-websocket-protocol"):
            ws[n] = value
            continue
        if n == "sec-websocket-extensions":
            # NIE an den Ziel-Server weitergeben: permessage-deflate wird
            # hier nicht durchproxiert (Browser-Seite intern bei Uvicorn,
            # Agent-Leiste hier roh). Würde ein deflate-fähiges Ziel (z. B.
            # marimo/websockets) es verhandeln, komprimierte es seine
            # Frames — die gingen hier als Plaintext durch → binärer Müll
            # im Browser (JSON.parse-Fehler). Ziele ohne Deflate
            # (Jupyter/Tornado) ignorieren es, deshalb trat es nur bei
            # Marimo auf.
            continue
        out.append((n, value))
    if upgrade:
        out.append(("upgrade", "websocket"))
        out.append(("connection", "upgrade"))
        if ws.get("sec-websocket-key"):
            out.append(("sec-websocket-key", ws["sec-websocket-key"]))
        out.append(("sec-websocket-version", ws.get("sec-websocket-version", "13")))
        if ws.get("sec-websocket-protocol"):
            out.append(("sec-websocket-protocol", ws["sec-websocket-protocol"]))
    return out


def _build_up_head(method: str, target: str,
                   headers: list[tuple[str, str]], token: str,
                   port: int, host: str | None = None) -> bytes:
    """Head für die Agent-Leiste.

    `host`: Original-Host (z. B. die Preview-Subdomain) — MUSS
    weitergereicht werden: Ziel-Apps (v. a. JupyterServer) vergleichen
    Origin/Referer mit dem Host-Header (XSRF/XSSI-Schutz); würde
    127.0.0.1:<port> ankommen, blöckt das alle Browser-Requests mit
    Origin/Referer (Datei-Öffnen, Datei-Anlegen, WebSockets).
    Fallback nur, wenn der Request ohne Host ankommt.
    """
    hdrs = [(n.encode("latin-1"), v.encode("latin-1")) for n, v in headers]
    host_hdr = host or f"127.0.0.1:{port}"
    return build_request_head(
        method, target, hdrs,
        extra=[(b"x-agent-token", token.encode("latin-1")),
               (b"host", host_hdr.encode("latin-1"))])


def _parse_preview_path(rest: str, query: str) -> tuple[int | None, str]:
    """rest = '<port>' oder '<port>/<subpath…>' → (port, Upstream-Pfad).

    Der Upstream-Pfad wird neu percent-encoded (ASGI liefert dekodiert);
    die Query bleibt raw. Ungültiger Port → (None, "").
    """
    port_str, _, sub = rest.partition("/")
    if not port_str.isdigit() or not (0 < int(port_str) <= 65535):
        return None, ""
    up = "/" + quote(sub, safe="/") if sub else ""
    if query:
        up += "?" + query
    return int(port_str), up


# ── Subdomain-Preview ─────────────────────────────────────────────
# Jede Workspace-Web-App bekommt eine eigene Subdomain:
#   https://<task>-<port>-<user>-<h6>.<PREVIEW_BASE_DOMAIN>/<app-path>
# Die Apps laufen auf / (kein Base-Pfad). Die Subdomain ist nur
# Routungs-Hinweis — Auth bleibt der access_token-Cookie (User muss
# zum Label-User passen) + der übliche Task/Course-Check. Der Cookie
# fehlt auf der fremden Domain initially → Handoff-Ticket (One-Shot,
# s. preview_handoff) setzt ihn beim ersten Besuch.

# Review-Labels: …-r<submission>-<h6> — das Review-Ziel ist Teil der
# Subdomain selbst, damit ALLE Requests darauf (auch App-interne
# XHR/WebSocket ohne Query) auf den Review-Workspace routen.
_LABEL_RE = re.compile(
    r"^(\d{1,9})-(\d{1,5})-(\d{1,9})(?:-r(\d{1,9}))?-([0-9a-f]{6})$")
_TICKET_TTL = 60  # Sekunden (Handoff-Ticket)
_used_tickets: dict[str, float] = {}  # jti → Ablauf (One-Shot)


def _label_h6(task_id: int, port: int, user_id: int,
              review: int | None = None) -> str:
    """24-Bit-Tag aus dem SECRET (Label praktisch unerraten; Auth
    bleibt der Cookie — das Tag verhindert nur, dass beliebige
    Subdomains auf Workspaces routen)."""
    rv = f":r{review}" if review is not None else ""
    return hashlib.sha256(
        f"{SECRET_KEY}|preview-subdomain|{task_id}:{port}:{user_id}{rv}"
        .encode()).hexdigest()[:6]


def preview_label(task_id: int, port: int, user_id: int,
                  review: int | None = None) -> str:
    """Stabiles Subdomain-Label für (Task, Port, User[, Review])."""
    return (f"{task_id}-{port}-{user_id}"
            + (f"-r{review}" if review is not None else "")
            + f"-{_label_h6(task_id, port, user_id, review)}")


def _parse_label_host(host: str):
    """Host (ohne Port) → (task, port, user, h6, review) bei
    Preview-Subdomain, sonst None (review=None = Live-Workspace)."""
    if not PREVIEW_BASE_DOMAIN:
        return None
    suffix = "." + PREVIEW_BASE_DOMAIN
    if not host.endswith(suffix):
        return None
    m = _LABEL_RE.match(host[: -len(suffix)])
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(5),
            int(m.group(4)) if m.group(4) else None)


def _query_param(query_b: bytes, name: str) -> str:
    for k, v in parse_qsl(query_b.decode("latin-1"), keep_blank_values=True):
        if k == name:
            return v
    return ""


def _ticket_create(task_id: int, port: int, user_id: int, next_path: str) -> str:
    """One-Shot-Handoff-Ticket (JWT, 60 s): erlaubt das Setzen des
    access_token-Cookies auf der Subdomain für genau diesen User/
    Task/Port."""
    from services import auth_service
    return auth_service.create_access_token(
        {"sub": user_id, "pv": 1, "task": task_id, "port": port,
         "next": next_path, "jti": secrets.token_hex(8)},
        expires_delta=timedelta(seconds=_TICKET_TTL))


def _ticket_check(ticket: str, task_id: int, port: int, user_id: int):
    """Ticket validieren (Signatur, Expiry, Bindung, One-Shot).
    Liefert das gebundene Ziel-Pfad oder None."""
    from services import auth_service
    now = time.time()
    if len(_used_tickets) > 4096:
        for jti in [j for j, exp in _used_tickets.items() if exp < now]:
            _used_tickets.pop(jti, None)
    payload = auth_service.decode_access_token(ticket) if ticket else None
    if not payload or payload.get("pv") != 1:
        return None
    if payload.get("sub") != user_id:
        return None
    if payload.get("task") != task_id or payload.get("port") != port:
        return None
    jti = payload.get("jti")
    if not isinstance(jti, str) or jti in _used_tickets:
        return None
    _used_tickets[jti] = now + _TICKET_TTL
    nxt = payload.get("next") or "/"
    if (not isinstance(nxt, str) or not nxt.startswith("/")
            or nxt.startswith("//") or "\\" in nxt):
        nxt = "/"
    return nxt


def _error_page(status: int, title: str, hint: str = "") -> Response:
    hint_html = f"<p class='hint'>{hint}</p>" if hint else ""
    html = (
        "<!doctype html><html lang='de'><head><meta charset='utf-8'>"
        "<title>AICampus-Preview</title></head>"
        "<body style='font-family:system-ui,sans-serif;max-width:42em;"
        "margin:5em auto;color:#374151;padding:0 1em'>"
        f"<h2 style='font-size:1.25rem'>{title}</h2>{hint_html}"
        "</body></html>")
    return Response(html, status_code=status, media_type="text/html",
                    headers={"Cache-Control": "no-store"})


class PreviewSubdomainMiddleware:
    """Routet Preview-Subdomains direkt auf den Preview-Proxy.

    Nur aktiv, wenn PREVIEW_BASE_DOMAIN gesetzt ist und der Host ein
    Label '<task>-<port>-<user>-<h6>' der Basis-Domain ist; alle
    anderen Requests gehen unverändert weiter (Inner-App).

    Bewusst rohes ASGI (nicht BaseHTTPMiddleware): WebSocket-Scopes
    müssen ebenfalls behandelt werden. HTTP+WS delegieren auf die
    gewöhnlichen Routen-Funktionen (preview_http/preview_ws) — dieselbe
    Auth/Task-Logik wie im Pfad-Modus.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = ""
        for n, v in scope.get("headers") or ():
            if n == b"host":
                host = v.decode("latin-1").lower()
                if host.rsplit(":", 1)[-1].isdigit() and ":" in host:
                    host = host.rsplit(":", 1)[0]
                break
        parsed = _parse_label_host(host)
        if parsed is None:
            await self.app(scope, receive, send)
            return
        task_id, port, user_id, h6, review_id = parsed
        if not hmac.compare_digest(
                _label_h6(task_id, port, user_id, review_id), h6):
            # Unbekannte Subdomain: keine Details leaksen
            if scope["type"] == "http":
                await _error_page(404, "Unbekannte Preview.")(scope, receive, send)
            else:
                await StarletteWebSocket(scope, receive, send).close(code=4404)
            return

        path = scope.get("path") or "/"
        ticket = _query_param(scope.get("query_string") or b"", "ticket")
        if path == "/__preview_auth":
            await self._auth(scope, receive, send, task_id, port, user_id,
                             ticket)
            return

        cookie = ""
        for n, v in scope.get("headers") or ():
            if n == b"cookie":
                cookie = v.decode("latin-1")
                break
        from services import auth_service
        payload = auth_service.decode_access_token(
            _cookie_value(cookie, "access_token"))
        unauth = not payload or payload.get("sub") != user_id
        if unauth:
            # Browser (v. a. Chrome) laden das PWA-Manifest OHNE
            # Cookies (Manifest-Fetch mit credentials: omit, s.
            # w3c/manifest#776 / Chromium 40734972). Ohne Ausnahme
            # spült jede App mit Manifest (z. B. Marimo) die Konsole
            # mit 403s. Das Manifest ist öffentliche App-Metadaten
            # (Name/Icons, keine Workspace-Daten) → der Label-User
            # (HMAC-verifiziert) dient als Identität; die
            # Task/Course-Checks in _resolve bleiben erhalten.
            manifest_fetch = (
                scope["type"] == "http" and path == "/manifest.json"
                and scope.get("method") in ("GET", "HEAD"))
            if not manifest_fetch:
                if scope["type"] == "http":
                    await _error_page(
                        403, "Zugriff verweigert.",
                        "Öffne die Aufgabe in AICampus — der Link ist nur "
                        "für deine eigene Sitzung gültig.")(scope, receive, send)
                else:
                    await StarletteWebSocket(scope, receive,
                                             send).close(code=1008)
                return

        rest = f"{port}/{path.lstrip('/')}"
        if review_id is not None:
            # Review-Label → review-Param in die Query injizieren; die
            # Routen (_split_review) picken ihn auf und filtern ihn vor
            # dem Weiterleiten heraus. Das Label ist Authorität
            # (HMAC-verifiziert) — bereits vorhandene review-Params
            # werden ersetzt.
            qs = parse_qsl(scope.get("query_string") or b"",
                           keep_blank_values=True)
            qs = [(k, v) for k, v in qs if k != "review"]
            qs.append(("review", str(review_id)))
            scope["query_string"] = urlencode(qs).encode("latin-1")
        if scope["type"] == "http":
            response = await preview_http(
                task_id, rest, Request(scope, receive),
                trusted_user_id=user_id if unauth else None)
            await response(scope, receive, send)
        else:
            await preview_ws(task_id, rest,
                             StarletteWebSocket(scope, receive, send))

    @staticmethod
    async def _auth(scope, receive, send, task_id: int, port: int,
                    user_id: int, ticket: str) -> None:
        """Handoff: Ticket → access_token-Cookie auf der Subdomain → 302."""
        if scope["type"] != "http":
            await StarletteWebSocket(scope, receive, send).close(code=1008)
            return
        nxt = _ticket_check(ticket, task_id, port, user_id)
        if nxt is None:
            resp = _error_page(
                403, "Handoff abgelaufen.",
                "Bitte die Vorschau über AICampus neu öffnen.")
            await resp(scope, receive, send)
            return
        from services import auth_service
        resp = RedirectResponse(nxt, status_code=302)
        resp.set_cookie(
            "access_token",
            auth_service.create_access_token({"sub": user_id}),
            httponly=True, secure=True, samesite="lax",
            max_age=8 * 3600, path="/")
        await resp(scope, receive, send)


# ── Agent-Leiste: TCP + WS-Frames + Chunked ──────────────────────

async def _connect_agent(agent_url: str):
    split = urlsplit(agent_url)
    host = split.hostname or "127.0.0.1"
    return await asyncio.wait_for(
        asyncio.open_connection(host, PREVIEW_AGENT_PORT), timeout=5)


async def _aclose(writer) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


def _encode_frame(opcode: int, payload: bytes) -> bytes:
    """WS-Frame für die Agent-Leiste (wir sind Client → masked)."""
    head = bytearray([0x80 | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        head.append(0x80 | n)
    elif n < 1 << 16:
        head.append(0x80 | 126)
        head += n.to_bytes(2, "big")
    else:
        head.append(0x80 | 127)
        head += n.to_bytes(8, "big")
    mask = os.urandom(4)
    head += mask
    head += bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return bytes(head)


class _FrameReader:
    """Liest UNMASKED WS-Frames von der Agent-Leiste (der Ziel-Server
    ist auf deren Verbindung der Server). Optionaler Leftover-Puffer
    für Frame-Bytes aus dem 101-Head-Read."""

    def __init__(self, reader: asyncio.StreamReader,
                 pending: bytes = b"") -> None:
        self._r = reader
        self._buf = pending

    async def _take(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = await self._r.read(65536)
            if not chunk:
                raise _WsEof()
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    async def next(self) -> tuple[bool, int, bytes] | None:
        """(fin, opcode, payload); None = EOF."""
        try:
            b0, b1 = await self._take(2)
        except _WsEof:
            return None
        fin = bool(b0 & 0x80)
        op = b0 & 0x0F
        if b0 & 0x70:
            raise ValueError("RSV-Bits gesetzt (WS-Extension nicht unterstützt)")
        if b1 & 0x80:
            raise ValueError("Masked Frame von Server erwartet")
        n = b1 & 0x7F
        if n == 126:
            n = int.from_bytes(await self._take(2), "big")
        elif n == 127:
            n = int.from_bytes(await self._take(8), "big")
        if n > _WS_MSG_CAP:
            raise ValueError("WS-Frame zu groß")
        payload = await self._take(n) if n else b""
        return fin, op, payload


class _ChunkedDecoder:
    """Dekodiert das chunked Framing der Agent-Leiste (der Agent leitet
    die Chunked-Encoding des Ziels als rohe Bytes weiter; ASGI/Starlette
    chunkt den Body selbst neu)."""

    def __init__(self) -> None:
        self._buf = b""
        self._chunk = -1  # -1 = Size-Zeile ausstehend
        self._done = False

    def feed(self, data: bytes) -> bytes:
        if self._done:
            return b""
        self._buf += data
        out = bytearray()
        while True:
            if self._chunk < 0:
                idx = self._buf.find(b"\r\n")
                if idx < 0:
                    if len(self._buf) > 32:
                        raise ValueError("Chunk-Size defekt")
                    break
                line = self._buf[:idx].decode("latin-1")
                try:
                    self._chunk = int(line.split(";", 1)[0].strip() or "0", 16)
                except ValueError:
                    raise ValueError("Chunk-Size defekt") from None
                self._buf = self._buf[idx + 2:]
                if self._chunk == 0:
                    self._done = True  # Trailer ignorieren
                    break
            n = min(self._chunk, len(self._buf))
            out += self._buf[:n]
            self._buf = self._buf[n:]
            self._chunk -= n
            if self._chunk == 0:
                if len(self._buf) < 2:
                    break
                self._buf = self._buf[2:]  # Chunk-CRLF
        return bytes(out)


# ── Auth + Routing (sync, läuft im Executor) ──────────────────────

def _split_review(query: str) -> tuple[int | None, str]:
    """Preview-Query splitten in (review_submission_id, Forwarded-Query).

    review=<id>: Vorschau aus dem REVIEW-Workspace dieser Einreichung
    (Path-Modus: vom Browser; Subdomain-Modus: vom Middleware aus dem
    Review-Label injiziert). Der Parameter wird vor dem Weiterleiten
    aus dem Query herausgefiltert — die gepreviewte App soll ihn nicht
    sehen.
    """
    review = None
    kept = []
    for k, v in parse_qsl(query or "", keep_blank_values=True):
        if k == "review" and review is None:
            review = v
        else:
            kept.append((k, v))
    if review is None:
        return None, urlencode(kept)
    try:
        review_id = int(review)
    except ValueError:
        raise _AuthError(400, "Ungültiger review-Parameter.")
    if review_id <= 0:
        raise _AuthError(400, "Ungültiger review-Parameter.")
    return review_id, urlencode(kept)


def _resolve_sync(task_id: int, token: str,
                  trusted_user_id: int | None = None,
                  review_submission_id: int | None = None):
    """Cookie-Auth + Task/Course-Check + Agent-Pick.

    trusted_user_id ersetzt die Cookie-Auth durch den Label-User
    (HMAC-verifizierte Subdomain) — nur für Cookie-freie Browser-
    Fetches ohne Datencharakter (PWA-Manifest, s. Middleware). Die
    Task/Course-Mitgliedschaft wird auch dann geprüft.

    review_submission_id: Preview aus dem Review-Workspace (separater
    Workspace je Einreichung). Zugriff wie im Review-Workspace: Student
    nur eigene Einreichung, sonst PROF/Tutor/Global-Admin.

    Liefert (agent_url, agent_key, workspace_key, user_id);
    None = degraded (503); wirft _AuthError bei 4xx.
    """
    from database.base import Session, engine
    from database.models import (GlobalUserRole, Submission, Task, User,
                                 UserCourse, CourseRole)
    from services import auth_service
    from services.workspace_service import workspace_key, workspace_service

    if trusted_user_id is not None:
        user_id = trusted_user_id
    else:
        payload = auth_service.decode_access_token(token) if token else None
        if not payload:
            raise _AuthError(401, "Nicht authentifiziert.")
        user_id = payload.get("sub")
    if not user_id:
        raise _AuthError(401, "Ungültiges Token.")
    with Session(engine) as session:
        user = session.get(User, user_id)
        if user is None:
            raise _AuthError(401, "Nicht authentifiziert.")
        task = session.get(Task, task_id)
        if task is None:
            raise _AuthError(404, "Aufgabe nicht gefunden.")
        membership = session.exec(
            select(UserCourse)
            .where(UserCourse.user_id == user.id)
            .where(UserCourse.course_id == task.course_id)
        ).first()
        if not membership or membership.role_in_course not in (
                CourseRole.STUDENT, CourseRole.TUTOR, CourseRole.PROF):
            raise _AuthError(403, "Kein Zugriff auf diese Aufgabe.")
        if task.task_type.value != "workspace":
            raise _AuthError(400, "Keine Workspace-Aufgabe.")
        if not workspace_service.is_enabled(session, task.course_id):
            return None
        if workspace_service.validate_task_ready(session, task):
            return None
        agent = workspace_service.pick_task_agent(session, task)
        if agent is None:
            return None
        if review_submission_id is not None:
            submission = session.get(Submission, review_submission_id)
            if submission is None or submission.task_id != task.id:
                raise _AuthError(404, "Einreichung nicht gefunden.")
            if submission.student_id != user.id:
                if not (user.role == GlobalUserRole.ADMIN
                        or membership.role_in_course in (
                            CourseRole.PROF, CourseRole.TUTOR)):
                    raise _AuthError(
                        403, "Nur Zugriff auf eigene Einreichungen.")
            key = workspace_service.review_key(task, review_submission_id)
        else:
            key = workspace_key(task.course_id, task.id, user.id)
        return (agent["url"], agent.get("key") or "", key, user.id)


async def _resolve(task_id: int, token: str,
                   trusted_user_id: int | None = None,
                   review_submission_id: int | None = None):
    """_resolve_sync im Executor (DB) mit Timeout."""
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            None, _resolve_sync, task_id, token, trusted_user_id,
            review_submission_id),
        timeout=20)


# ── Routes ────────────────────────────────────────────────────────

@router.api_route(
    "/preview/{task_id}/{rest:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def preview_http(task_id: int, rest: str, request: Request,
                       trusted_user_id: int | None = None):
    """HTTP-Preview: streamt Request/Response zum Agent-Preview-Server.

    trusted_user_id: nur vom Subdomain-Middleware gesetzt (Cookie-freie
    PWA-Manifest-Fetches, s. dort).
    """
    try:
        review_id, fwd_query = _split_review(request.url.query)
    except _AuthError as e:
        return _json(e.status, e.message)
    port, up_path = _parse_preview_path(rest, fwd_query)
    if port is None:
        return _json(400, "Ungültiger Port.")

    try:
        ctx = await _resolve(task_id, _cookie_value(
            request.headers.get("cookie", ""), "access_token"),
            trusted_user_id=trusted_user_id,
            review_submission_id=review_id)
    except _AuthError as e:
        return _json(e.status, e.message)
    except Exception:
        logger.warning("Preview-Proxy: Auth/Task-Check fehlgeschlagen",
                       exc_info=True)
        return _json(500, "Interner Fehler.")
    if ctx is None:
        return _json(503, "Workspace derzeit nicht verfügbar.")
    agent_url, agent_key, key, user_id = ctx

    method = request.method
    has_body = method in ("POST", "PUT", "PATCH", "DELETE")
    cl = request.headers.get("content-length")
    if has_body and cl is None:
        return _json(411, "Content-Length erforderlich.")
    if has_body and cl is not None:
        try:
            if int(cl) > _BODY_CAP:
                return _json(413, "Datei zu groß.")
        except ValueError:
            return _json(400, "Ungültiges Content-Length.")

    cookie = _sanitize_cookie(request.headers.get("cookie", ""))
    headers = _up_headers(request.headers, upgrade=False)
    if cookie:
        headers.append(("cookie", cookie))
    token = make_token(agent_key, f"ws:{key}",
                       task_id=task_id, student_id=user_id)

    try:
        a_reader, a_writer = await _connect_agent(agent_url)
    except (OSError, asyncio.TimeoutError):
        return _json(503, "Preview-Server der Compute-Engine nicht erreichbar.")

    try:
        a_writer.write(_build_up_head(
            method, f"/ws-preview/{key}/preview/{port}{up_path}",
            headers, token, port, host=request.headers.get("host")))
        await a_writer.drain()
        try:
            async for chunk in request.stream():
                a_writer.write(chunk)
                await a_writer.drain()
        except (OSError, ConnectionResetError, BrokenPipeError):
            await _aclose(a_writer)
            return _json(502, "Request-Body konnte nicht weitergeleitet werden.")

        # Antwort-Head vom Agenten
        try:
            resp_head_b, resp_leftover = await asyncio.wait_for(
                read_head(a_reader), timeout=_HEAD_TIMEOUT)
        except (HeadError, asyncio.TimeoutError, OSError):
            await _aclose(a_writer)
            return _json(502, "Ziel-Server im Workspace nicht erreichbar.")
        try:
            status, resp_headers = parse_response_head(resp_head_b)
        except HeadError:
            await _aclose(a_writer)
            return _json(502, "Ungültige Ziel-Antwort.")
        if status == 101:
            # WS-Upgrade kommt nie hier an (Uvicorn → websocket-Scope)
            await _aclose(a_writer)
            return _json(502, "Unerwartetes Upgrade — WebSocket nutzen.")
    except Exception:
        await _aclose(a_writer)
        logger.warning("Preview-Proxy (HTTP): Tunnel-Setup fehlgeschlagen",
                       exc_info=True)
        return _json(502, "Ziel-Server im Workspace nicht erreichbar.")

    is_chunked = any(n == b"transfer-encoding" and b"chunked" in v
                     for n, v in resp_headers)
    hdrs: dict[str, str] = {}
    for n, v in resp_headers:
        if n in (b"connection", b"keep-alive", b"transfer-encoding",
                 b"x-frame-options", b"content-security-policy",
                 b"content-security-policy-report-only"):
            continue
        hdrs[n.decode("latin-1")] = v.decode("latin-1")
    # Preview-Subdomains zeigen Workspace-Apps (User-Content) → nie indexieren
    hdrs["x-robots-tag"] = "noindex, nofollow"
    # Pfad-Modus: relative Location-Header des Workspace-Servers (z.B.
    # Jupyter: 302 → /tree) würden ohne Präfix gegen die AICampus-App
    # selbst laufen (404). Subdomain-Modus: App läuft auf / → Location
    # bleibt. (Absolute/foreign Locations bleiben ebenfalls unverändert.)
    if status in (301, 302, 303, 307, 308) and "location" in hdrs:
        loc = hdrs["location"]
        host = request.headers.get("host", "").lower().rsplit(":", 1)[0]
        if loc.startswith("/") and not loc.startswith("//") \
                and _parse_label_host(host) is None:
            new = f"/preview/{task_id}/{port}{loc}"
            if review_id is not None:
                new += ("&" if "?" in new else "?") + f"review={review_id}"
            hdrs["location"] = new
    decoder = _ChunkedDecoder() if is_chunked else None

    async def _body():
        # a_writer gehört ab jetzt hier (Schließung am Stream-Ende —
        # der Agent schließt nach Body-EOD zuverlässig).
        try:
            if resp_leftover:
                out = decoder.feed(resp_leftover) if decoder else resp_leftover
                if out:
                    yield out
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        a_reader.read(65536), timeout=_BODY_IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    break  # Agent totstill → Stream beenden (Leak-Schutz)
                if not chunk:
                    break
                if decoder:
                    out = decoder.feed(chunk)
                    if out:
                        yield out
                else:
                    yield chunk
        except ValueError:
            logger.warning("Preview-Proxy (HTTP): Chunked-Body defekt",
                           exc_info=True)
        finally:
            await _aclose(a_writer)

    return StreamingResponse(_body(), status_code=status, headers=hdrs)


@router.websocket("/preview/{task_id}/{rest:path}")
async def preview_ws(task_id: int, rest: str, websocket: WebSocket):
    """WS-Preview: brückt Browser-Frames (uvicorn-dekodiert) auf die
    Agent-Leiste (rohe Frames nach dem 101)."""
    try:
        review_id, fwd_query = _split_review(websocket.url.query)
    except _AuthError:
        await websocket.close(code=1008)
        return
    port, up_path = _parse_preview_path(rest, fwd_query)
    if port is None:
        await websocket.close(code=1008)
        return

    try:
        ctx = await _resolve(task_id, _cookie_value(
            websocket.headers.get("cookie", ""), "access_token"),
            review_submission_id=review_id)
    except _AuthError:
        await websocket.close(code=1008)
        return
    except Exception:
        logger.warning("Preview-Proxy (WS): Auth/Task-Check fehlgeschlagen",
                       exc_info=True)
        await websocket.close(code=1011)
        return
    if ctx is None:
        await websocket.close(code=1013)
        return
    agent_url, agent_key, key, user_id = ctx
    token = make_token(agent_key, f"ws:{key}",
                       task_id=task_id, student_id=user_id)

    try:
        a_reader, a_writer = await _connect_agent(agent_url)
    except (OSError, asyncio.TimeoutError):
        await websocket.close(code=1013)
        return

    cookie = _sanitize_cookie(websocket.headers.get("cookie", ""))
    headers = _up_headers(websocket.headers, upgrade=True)
    if cookie:
        headers.append(("cookie", cookie))

    try:
        a_writer.write(_build_up_head(
            "GET", f"/ws-preview/{key}/preview/{port}{up_path}",
            headers, token, port, host=websocket.headers.get("host")))
        await a_writer.drain()
        resp_head_b, resp_leftover = await asyncio.wait_for(
            read_head(a_reader), timeout=_HEAD_TIMEOUT)
        status, resp_headers = parse_response_head(resp_head_b)
    except (OSError, HeadError, asyncio.TimeoutError, ValueError):
        await _aclose(a_writer)
        await websocket.close(code=1011)
        return
    if status != 101:
        await _aclose(a_writer)
        await websocket.close(code=1011)
        return

    # Vom Ziel verhandelte Subprotocol (falls vorhanden) an das
    # Browser-Handshake weitergeben.
    sub = next((v for n, v in resp_headers if n == b"sec-websocket-protocol"),
               None)
    try:
        await websocket.accept(
            subprotocol=sub.decode("latin-1") if sub else None)
    except Exception:
        await _aclose(a_writer)
        return

    frames = _FrameReader(a_reader, resp_leftover)

    async def _browser_to_agent() -> None:
        try:
            while True:
                msg = await websocket.receive()
                t = msg["type"]
                if t == "websocket.receive":
                    if "text" in msg:
                        opcode, payload = 0x1, msg["text"].encode("utf-8")
                    else:
                        opcode, payload = 0x2, msg.get("bytes") or b""
                    if len(payload) > _WS_MSG_CAP:
                        a_writer.write(_encode_frame(0x8, struct.pack(">H", 1009)))
                        await a_writer.drain()
                        return
                    try:
                        a_writer.write(_encode_frame(opcode, payload))
                        await a_writer.drain()
                    except Exception:
                        return
                elif t == "websocket.disconnect":
                    code = msg.get("code") or 1000
                    if not (1000 <= code <= 4999):
                        code = 1000
                    try:
                        a_writer.write(_encode_frame(0x8, struct.pack(">H", code)))
                        await a_writer.drain()
                    except Exception:
                        pass
                    return
        except Exception:
            pass

    async def _agent_to_browser() -> None:
        buf = bytearray()
        cur: int | None = None
        try:
            while True:
                try:
                    fr = await frames.next()
                except Exception:
                    raise
                if fr is None:
                    break
                fin, op, payload = fr
                if op == 0x8:  # close: echo + Browser-Seite schließen
                    code = int.from_bytes(payload[:2], "big") if len(payload) >= 2 else 1000
                    try:
                        a_writer.write(_encode_frame(0x8, payload[:2] or b""))
                        await a_writer.drain()
                    except Exception:
                        pass
                    if not (1000 <= code <= 4999):
                        code = 1000
                    await websocket.close(code=code)
                    return
                if op == 0x9:  # ping → pong (Browser-Seite macht uvicorn)
                    a_writer.write(_encode_frame(0xA, payload))
                    await a_writer.drain()
                    continue
                if op == 0xA:  # pong: ignorieren
                    continue
                if op != 0x0:  # Daten-Frame (Continuation = 0)
                    cur = op
                buf.extend(payload)
                if len(buf) > _WS_MSG_CAP:
                    try:
                        a_writer.write(_encode_frame(0x8, struct.pack(">H", 1009)))
                        await a_writer.drain()
                    except Exception:
                        pass
                    await websocket.close(code=1009)
                    return
                if fin:  # (umgesetzte) Message vollständig
                    if cur == 0x1:
                        await websocket.send({"type": "websocket.send",
                                              "text": buf.decode("utf-8", "replace")})
                    else:
                        await websocket.send({"type": "websocket.send",
                                              "bytes": bytes(buf)})
                    buf = bytearray()
                    cur = None
        except Exception:
            logger.warning("Preview-WS: Agent-Pump beendet (Fehler)",
                           exc_info=True)
        # EOF ohne Close-Frame
        try:
            await websocket.close(code=1001)
        except Exception:
            pass

    t1 = asyncio.create_task(_browser_to_agent())
    t2 = asyncio.create_task(_agent_to_browser())
    try:
        done, _pending = await asyncio.wait({t1, t2},
                                            return_when=asyncio.FIRST_COMPLETED)
        for t in (t1, t2):
            t.cancel()
        for t in (t1, t2):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        # Auch bei Cancellation des Handlers (Client-RST, Shutdown) den
        # Agent-Writer schließen — sonst wartet der Agent-Pump ewig auf
        # EOF und das Relay frisst das pids-Limit des Containers.
        await _aclose(a_writer)
    # Browser-Seite ggf. noch offen (Agent-EOF ohne Close)
    try:
        await websocket.close(code=1001)
    except Exception:
        pass


@router.get("/preview-handoff/{task_id}")
async def preview_handoff(request: Request,
                          task_id: int,
                          port: int = Query(0),
                          path: str = Query("/"),
                          review: int | None = Query(None)):
    """302 zur Preview-Subdomain (nur mit PREVIEW_BASE_DOMAIN).

    Erstbesuch: legt über ein One-Shot-Handoff-Ticket den access_token-
    Cookie auf der Subdomain an (/__preview_auth) und zeigt dann die
    App unter `path`. Browser folgt der Kette in iframe ODER neuem Tab
    — die UI nutzt nur diese URL, das Label kennt sie nicht.

    review=<submission_id>: Review-Workspace — die Subdomain selbst
    trägt das Review-Ziel (Label …-r<id>-…), alle Requests darauf
    routen auf die Einreichung, Auth-Check wie im Review-Workspace.
    """
    if not PREVIEW_BASE_DOMAIN:
        return _json(400, "Preview-Subdomains sind nicht konfiguriert.")
    if not (1 <= port <= 65535):
        return _json(400, "Ungültiger Port.")
    nxt = (path or "/").strip()
    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
        return _json(400, "Ungültiger Pfad.")
    if review is not None and review <= 0:
        return _json(400, "Ungültige Einreichung.")
    try:
        ctx = await _resolve(task_id, _cookie_value(
            request.headers.get("cookie", ""), "access_token"),
            review_submission_id=review)
    except _AuthError as e:
        return _json(e.status, e.message)
    except Exception:
        logger.warning("Preview-Handoff: Auth/Task-Check fehlgeschlagen",
                       exc_info=True)
        return _json(500, "Interner Fehler.")
    if ctx is None:
        return _json(503, "Workspace derzeit nicht verfügbar.")
    _agent_url, _agent_key, _key, user_id = ctx
    if not user_id:
        return _json(401, "Nicht authentifiziert.")
    ticket = _ticket_create(task_id, port, user_id, nxt)
    label = preview_label(task_id, port, user_id, review)
    return RedirectResponse(
        url=(f"https://{label}.{PREVIEW_BASE_DOMAIN}"
             f"/__preview_auth?ticket={ticket}"),
        status_code=302)
