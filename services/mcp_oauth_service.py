"""
MCP-OAuth: Codes, Token-Paare und Grant-Kurse (Authorization-Code + PKCE).

TTLs: Access-Token 1 h, Refresh-Token 90 Tage, Authorization-Code 10 Min.
Wie die PAT-Tokens werden auch hier nur SHA-256-Hashes gespeichert —
Klartext wandert einmalig in die Token-Response bzw. den Redirect.
"""

import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from sqlmodel import Session, select

from database.models import (
    Course,
    McpOAuthClient,
    McpOAuthCode,
    McpOAuthGrantCourse,
    McpOAuthToken,
)
from services.timeutil import utc_iso

ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=90)
CODE_TTL = timedelta(minutes=10)
# Kurzes Fenster, in dem der vor der Rotation verbrauchte Refresh-Hash noch
# akzeptiert wird (parallele/retierende Client-Requests, s. lookup_refresh).
REFRESH_ROTATION_GRACE = timedelta(seconds=30)
# ChatGPT doppelt den Code-Exchange kurz nach dem Erfolg (duplicate
# Callback) — ohne Toleranzfenster bricht das 400 den ganzen Connector.
# Innerhalb des Fensters ist der Replay idempotent (s. Provider-Calls).
CODE_REPLAY_GRACE = timedelta(minutes=5)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_access_token() -> Tuple[str, str]:
    """Erzeugt (klartext_token, sha256_hex)."""
    raw = "aio_" + secrets.token_hex(32)
    return raw, _hash(raw)


def generate_refresh_token() -> Tuple[str, str]:
    raw = "aioR_" + secrets.token_hex(32)
    return raw, _hash(raw)


def generate_code() -> Tuple[str, str]:
    raw = "aioC_" + secrets.token_hex(32)
    return raw, _hash(raw)


# ─── Clients (DCR) ────────────────────────────────────────────────

def store_client(
    session: Session,
    client_id: str,
    client_name: Optional[str],
    redirect_uris: List[str],
    auth_method: str,
    grant_types: List[str],
    client_secret: Optional[str],
) -> McpOAuthClient:
    row = McpOAuthClient(
        client_id=client_id,
        client_name=client_name,
        redirect_uris=json.dumps(redirect_uris),
        token_endpoint_auth_method=auth_method or "none",
        grant_types=json.dumps(grant_types),
        client_secret=client_secret,
    )
    session.add(row)
    session.commit()
    return row


def client_name(session: Session, client_id: str) -> str:
    """Anzeigename eines registrierten Clients (Fallback: client_id)."""
    row = get_client_row(session, client_id)
    return (row.client_name or client_id) if row else client_id


def get_client_row(session: Session, client_id: str) -> Optional[McpOAuthClient]:
    return session.exec(
        select(McpOAuthClient).where(McpOAuthClient.client_id == client_id)
    ).first()


def touch_client(session: Session, client_id: str):
    """last_used aktualisieren (max. 1×/Min, KISS-Guard)."""
    row = get_client_row(session, client_id)
    if not row:
        return
    if row.last_used_at is None or (datetime.now() - row.last_used_at).total_seconds() > 60:
        row.last_used_at = datetime.now()
        session.add(row)
        session.commit()


# ─── Authorization-Codes ──────────────────────────────────────────

def create_code(
    session: Session,
    client_id: str,
    code_challenge: Optional[str],
    redirect_uri: str,
    redirect_uri_explicit: bool,
    state: Optional[str],
) -> Tuple[str, str]:
    """Legt einen (noch nicht consenteten) Code an → (raw_code, hash)."""
    raw, h = generate_code()
    row = McpOAuthCode(
        code_hash=h,
        client_id=client_id,
        code_challenge=code_challenge,
        redirect_uri=redirect_uri,
        redirect_uri_explicit=redirect_uri_explicit,
        state=state,
        expires_at=datetime.now() + CODE_TTL,
    )
    session.add(row)
    session.commit()
    return raw, h


def get_code(session: Session, code_hash: str) -> Optional[McpOAuthCode]:
    """Code-Zeile per Hash; abgelaufene Codes werden lazy gelöscht."""
    row = session.exec(
        select(McpOAuthCode).where(McpOAuthCode.code_hash == code_hash)
    ).first()
    if row and row.expires_at < datetime.now():
        session.delete(row)
        session.commit()
        return None
    return row


def get_code_by_raw(session: Session, raw_code: str) -> Optional[McpOAuthCode]:
    return get_code(session, _hash(raw_code))


def confirm_code(
    session: Session,
    row: McpOAuthCode,
    user_id: int,
    course_ids: List[int],
    allow_write: bool,
):
    """Consent-Entscheidung auf den Code schreiben (erst jetzt einlösbar)."""
    row.user_id = user_id
    row.course_ids = json.dumps(sorted(set(course_ids)))
    row.allow_write = allow_write
    session.add(row)
    session.commit()


def consume_code(session: Session, row: McpOAuthCode):
    """Code als verbraucht markieren; bei Replay zieht das Toleranzfenster
    (CODE_REPLAY_GRACE) weiter."""
    row.used_at = datetime.now()
    session.add(row)
    session.commit()


# ─── Token-Paare ──────────────────────────────────────────────────

def issue_token_pair(
    session: Session, client_id: str, user_id: int, allow_write: bool,
    delete_existing: bool = True,
) -> Tuple[McpOAuthToken, str, str]:
    """Neues Access+Refresh-Paar → (row, access_raw, refresh_raw).

    Standard: es existiert genau EIN gültiges Paar pro User+Client — ältere
    Paare werden gelöscht (sonst könnten veraltete allow_write-Snapshots
    neben dem aktuellen weiterleben). Bei Code-Replay (delete_existing=False)
    überlebt das alte Paar bis zum natürlichen Ablauf — egal welches der
    beiden Token-Responses der Client behält, eines bleibt gültig."""
    if delete_existing:
        for row in session.exec(
            select(McpOAuthToken)
            .where(McpOAuthToken.user_id == user_id)
            .where(McpOAuthToken.client_id == client_id)
        ).all():
            session.delete(row)
        session.flush()
    a_raw, a_hash = generate_access_token()
    r_raw, r_hash = generate_refresh_token()
    row = McpOAuthToken(
        access_hash=a_hash,
        refresh_hash=r_hash,
        client_id=client_id,
        user_id=user_id,
        allow_write=allow_write,
        access_expires_at=datetime.now() + ACCESS_TTL,
        refresh_expires_at=datetime.now() + REFRESH_TTL,
    )
    session.add(row)
    session.commit()
    return row, a_raw, r_raw


def rotate_refresh(session: Session, row: McpOAuthToken) -> Tuple[str, str]:
    """Refresh-Token rotieren (neuer Hash auf derselben Zeile). Der alte Hash
    bleibt im kurzen Toleranzfenster gültig (s. lookup_refresh): Client-Requests
    können doppelt ankommen (parallel/Retry) — ohne Fenster würde der zweite
    Request mit 400 den ganzen Grant brechen. → (refresh_raw, refresh_hash)."""
    raw, h = generate_refresh_token()
    row.prev_refresh_hash = row.refresh_hash
    row.prev_refresh_valid_until = datetime.now() + REFRESH_ROTATION_GRACE
    row.refresh_hash = h
    row.refresh_expires_at = datetime.now() + REFRESH_TTL
    session.add(row)
    session.commit()
    return raw, h


def rotate_access(session: Session, row: McpOAuthToken) -> str:
    """Frisches Access-Token auf dem bestehenden Paar → access_raw."""
    raw, h = generate_access_token()
    row.access_hash = h
    row.access_expires_at = datetime.now() + ACCESS_TTL
    session.add(row)
    session.commit()
    return raw


def lookup_access(session: Session, raw: str) -> Optional[McpOAuthToken]:
    """Access-Token-Hash → gültiges Paar (Nebeneffekt: last_used)."""
    row = session.exec(
        select(McpOAuthToken).where(McpOAuthToken.access_hash == _hash(raw))
    ).first()
    if not row or row.access_expires_at < datetime.now():
        return None
    if row.last_used_at is None or (datetime.now() - row.last_used_at).total_seconds() > 60:
        row.last_used_at = datetime.now()
        session.add(row)
        session.commit()
    return row


def lookup_refresh(session: Session, raw: str) -> Optional[McpOAuthToken]:
    row = session.exec(
        select(McpOAuthToken).where(McpOAuthToken.refresh_hash == _hash(raw))
    ).first()
    if not row:
        # Replay eines bereits rotierten Refresh-Token im Toleranzfenster →
        # idempotent auf dasselbe Paar (sonst 400 → Grant kaputt).
        row = session.exec(
            select(McpOAuthToken).where(
                McpOAuthToken.prev_refresh_hash == _hash(raw),
                McpOAuthToken.prev_refresh_valid_until > datetime.now(),
            )
        ).first()
    if not row or row.refresh_expires_at < datetime.now():
        return None
    return row


def lookup_any(session: Session, raw: str) -> Optional[McpOAuthToken]:
    """Paar per Access- ODER Refresh-Hash (für die Revocation)."""
    h = _hash(raw)
    row = session.exec(select(McpOAuthToken).where(McpOAuthToken.access_hash == h)).first()
    if row:
        return row
    return session.exec(select(McpOAuthToken).where(McpOAuthToken.refresh_hash == h)).first()


# ─── Grants (pro-Client Kurs-Allowlist + Write-Schalter) ──────────

def set_grant_courses(session: Session, user_id: int, client_id: str, course_ids: List[int]):
    """Allowlist ersetzen (replace-all, analog McpTokenCourse-Update)."""
    for row in session.exec(
        select(McpOAuthGrantCourse)
        .where(McpOAuthGrantCourse.user_id == user_id)
        .where(McpOAuthGrantCourse.client_id == client_id)
    ).all():
        session.delete(row)
    session.flush()
    for cid in sorted(set(course_ids)):
        session.add(McpOAuthGrantCourse(user_id=user_id, client_id=client_id, course_id=cid))
    session.commit()


def set_allow_write(session: Session, user_id: int, client_id: str, allow_write: bool):
    """Write-Schalter auf alle Token-Paare des User+Client übertragen."""
    for row in session.exec(
        select(McpOAuthToken)
        .where(McpOAuthToken.user_id == user_id)
        .where(McpOAuthToken.client_id == client_id)
    ).all():
        row.allow_write = allow_write
        session.add(row)
    session.commit()


def grant_course_ids(session: Session, user_id: int, client_id: str) -> List[int]:
    rows = session.exec(
        select(McpOAuthGrantCourse.course_id)
        .where(McpOAuthGrantCourse.user_id == user_id)
        .where(McpOAuthGrantCourse.client_id == client_id)
    ).all()
    return sorted(rows)


def revoke_grant(session: Session, user_id: int, client_id: str):
    """Zugriff des Clients komplett trennen: Token-Paare + Allowlist."""
    for row in session.exec(
        select(McpOAuthToken)
        .where(McpOAuthToken.user_id == user_id)
        .where(McpOAuthToken.client_id == client_id)
    ).all():
        session.delete(row)
    for row in session.exec(
        select(McpOAuthGrantCourse)
        .where(McpOAuthGrantCourse.user_id == user_id)
        .where(McpOAuthGrantCourse.client_id == client_id)
    ).all():
        session.delete(row)
    session.commit()


def grants_for_user(session: Session, user_id: int) -> List[dict]:
    """Alle Grants des Users, aggregiert pro Client (für die Settings-UI)."""
    clients: dict[str, dict] = {}

    for g in session.exec(
        select(McpOAuthGrantCourse).where(McpOAuthGrantCourse.user_id == user_id)
    ).all():
        entry = clients.setdefault(g.client_id, {"course_ids": set(), "last_used": None, "allow_write": False})
        entry["course_ids"].add(g.course_id)

    for t in session.exec(
        select(McpOAuthToken).where(McpOAuthToken.user_id == user_id)
    ).all():
        entry = clients.setdefault(t.client_id, {"course_ids": set(), "last_used": None, "allow_write": False})
        entry["allow_write"] = entry["allow_write"] or t.allow_write
        if t.last_used_at and (entry["last_used"] is None or t.last_used_at > entry["last_used"]):
            entry["last_used"] = t.last_used_at

    out = []
    for client_id, e in sorted(clients.items()):
        crow = get_client_row(session, client_id)
        courses = []
        for cid in sorted(e["course_ids"]):
            course = session.get(Course, cid)
            if course:
                courses.append({"id": course.id, "name": course.name, "semester": course.semester})
        out.append({
            "client_id": client_id,
            "name": (crow.client_name if crow and crow.client_name else client_id),
            "courses": courses,
            "allow_write": e["allow_write"],
            "last_used_at": utc_iso(e["last_used"]),
        })
    return out
