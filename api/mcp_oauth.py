"""
MCP-OAuth: Authorization-Code-Flow mit PKCE für MCP-Clients (ChatGPT & Co.).

Drei Bausteine:
1. AICampusAuthProvider — mcp-SDK-OAuth-Provider (Clients via DCR, Codes,
   Token-Paare); PAT-Tokens (`aic_…`) bleiben parallel über den
   Bearer-Header nutzbar (Dispatch in load_access_token/_auth).
2. consent_router — Consent-Seite im Browser (GET/POST /mcp/oauth/authorize,
   POST /mcp/oauth/deny). Muss in main.py VOR app.mount("/mcp") eingebunden
   werden (Starlette-Routenreihenfolge).
3. router — /api/mcp-oauth/… (User-Settings: Grants anzeigen/editieren/trennen)
"""

import json
import logging
from datetime import datetime
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from config import BASE_DIR
from database.base import engine, get_session
from database.models import Course, McpOAuthClient, McpOAuthToken, User, UserCourse
from services import mcp_oauth_service
from services import mcp_token_service
from services.auth_service import decode_access_token, get_current_user

logger = logging.getLogger(__name__)

# ─── Templates (eigene Instanz: main.py importiert diese Datei — kein Zirkel) ─

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _asset(path: str) -> str:
    return f"/static/{path.lstrip('/')}"


def _base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    scheme = proto if proto in ("http", "https") else request.url.scheme
    return f"{scheme}://{request.url.netloc}/"


templates.env.globals["asset"] = _asset
templates.env.globals["public_base_url"] = _base_url
templates.env.globals["push_config"] = {"enabled": False, "public_key": ""}


# ─── SDK-Provider ─────────────────────────────────────────────────

class AICampusAuthCode(AuthorizationCode):
    """SDK-AuthorizationCode + die Consent-Entscheidung (Kurs-Scope).

    Das SDK übergibt das Objekt von load_authorization_code unverändert an
    exchange_authorization_code — die Extra-Fields kommen so an (vom SDK
    explizit erlaubt).
    """

    course_ids: list[int] = []
    allow_write: bool = False


def _client_from_row(row: McpOAuthClient) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=row.client_id,
        client_name=row.client_name,
        redirect_uris=json.loads(row.redirect_uris) or None,
        token_endpoint_auth_method=row.token_endpoint_auth_method,
        grant_types=json.loads(row.grant_types),
        client_secret=row.client_secret,
    )


class AICampusAuthProvider(OAuthAuthorizationServerProvider):
    """Mappt die SDK-OAuth-Calls auf die McpOAuth*-Tabellen.

    Zugriffsgrenzen (Kurse/Rolle/Write) steuern Consent-Entscheidung und
    live-Kursmitgliedschaft — die Tokens selbst sind reine Credentials.
    """

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        with Session(engine) as session:
            row = mcp_oauth_service.get_client_row(session, client_id)
            return _client_from_row(row) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """DCR (RFC 7591): Client registriert sich selbst beim ersten Versuch."""
        with Session(engine) as session:
            mcp_oauth_service.store_client(
                session,
                client_id=client_info.client_id,
                client_name=client_info.client_name,
                redirect_uris=[str(u) for u in (client_info.redirect_uris or [])],
                auth_method=client_info.token_endpoint_auth_method or "none",
                grant_types=list(client_info.grant_types or []),
                client_secret=client_info.client_secret,
            )
            mcp_oauth_service.touch_client(session, client_info.client_id)
        logger.info("[MCP-OAuth] Client '%s' registriert (%s).",
                    client_info.client_name or "?", client_info.client_id)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Legt den (noch nicht consenteten) Code an und schickt den User
        auf die Consent-Seite (relativer Pfad — Browser löst gegen Origin auf)."""
        with Session(engine) as session:
            code_raw, _ = mcp_oauth_service.create_code(
                session,
                client_id=client.client_id,
                code_challenge=params.code_challenge,
                redirect_uri=str(params.redirect_uri),
                redirect_uri_explicit=params.redirect_uri_provided_explicitly,
                state=params.state,
            )
        return f"/mcp/oauth/authorize?session={code_raw}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, code_str: str):
        with Session(engine) as session:
            row = mcp_oauth_service.get_code_by_raw(session, code_str)
            replay = bool(
                row and row.used_at
                and (datetime.now() - row.used_at) <= mcp_oauth_service.CODE_REPLAY_GRACE
            )
            if (
                not row or row.user_id is None or row.expires_at < datetime.now()
                or row.client_id != client.client_id
                or (row.used_at and not replay)
            ):
                # Diagnose (ChatGPT-400s am Token-Endpoint): zu alter Replay,
                # fremder Client oder unbekannter/abgelaufener Code?
                logger.info(
                    "[MCP-OAuth] Code-Exchange abgelehnt: client=%s, vorhanden=%s, "
                    "client_match=%s, verbraucht=%s, replay_fenster=%s, consentet=%s, abgelaufen=%s",
                    client.client_id, bool(row),
                    bool(row and row.client_id == client.client_id),
                    bool(row and row.used_at), replay,
                    bool(row and row.user_id is not None),
                    bool(row and row.expires_at < datetime.now()),
                )
                return None  # abgelaufen / verbraucht / nicht consentet = existiert nicht
            if replay:
                # ChatGPT doppelt den Callback — Replay im Toleranzfenster ist
                # idempotent (sonst bricht das 400 den Connector).
                logger.info("[MCP-OAuth] Code-Replay im Toleranzfenster akzeptiert (client=%s).",
                            client.client_id)
            return AICampusAuthCode(
                code=code_str,
                scopes=[],
                expires_at=row.expires_at.timestamp(),
                client_id=row.client_id,
                code_challenge=row.code_challenge,
                redirect_uri=row.redirect_uri,
                redirect_uri_provided_explicitly=row.redirect_uri_explicit,
                resource=None,
                subject=str(row.user_id),
                course_ids=json.loads(row.course_ids),
                allow_write=row.allow_write,
            )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, code: AICampusAuthCode) -> OAuthToken:
        with Session(engine) as session:
            row = mcp_oauth_service.get_code_by_raw(session, code.code)
            replay = bool(
                row and row.used_at
                and (datetime.now() - row.used_at) <= mcp_oauth_service.CODE_REPLAY_GRACE
            )
            if (
                not row or row.user_id is None or row.expires_at < datetime.now()
                or row.client_id != client.client_id
                or (row.used_at and not replay)
            ):
                raise TokenError(
                    error="invalid_grant",
                    error_description="Der Autorisierungs-Code ist ungültig oder abgelaufen.",
                )
            user_id = row.user_id
            # Replay: altes Paar lebt weiter (delete_existing=False), damit es
            # egal ist, welchen der beiden Responses der Client speichert.
            _, access_raw, refresh_raw = mcp_oauth_service.issue_token_pair(
                session, row.client_id, user_id, row.allow_write,
                delete_existing=not replay,
            )
            mcp_oauth_service.set_grant_courses(session, user_id, row.client_id, code.course_ids)
            mcp_oauth_service.consume_code(session, row)
        logger.info("[MCP-OAuth] Tokens für Client %s / User %s erteilt.",
                    client.client_id, user_id)
        return OAuthToken(
            access_token=access_raw,
            refresh_token=refresh_raw,
            expires_in=int(mcp_oauth_service.ACCESS_TTL.total_seconds()),
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> Optional[RefreshToken]:
        with Session(engine) as session:
            row = mcp_oauth_service.lookup_refresh(session, refresh_token)
            if not row or row.client_id != client.client_id:
                logger.info(
                    "[MCP-OAuth] Refresh abgelehnt: client=%s, gefunden=%s, client_match=%s",
                    client.client_id, bool(row),
                    bool(row and row.client_id == client.client_id),
                )
                return None
            return RefreshToken(
                token=refresh_token,
                client_id=row.client_id,
                scopes=[],
                expires_at=int(row.refresh_expires_at.timestamp()),
                resource=None,
                subject=str(row.user_id),
            )

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, rt: RefreshToken, scopes: list) -> OAuthToken:
        with Session(engine) as session:
            row = mcp_oauth_service.lookup_refresh(session, rt.token)
            if not row or row.client_id != client.client_id:
                raise TokenError(
                    error="invalid_grant",
                    error_description="Der Refresh-Token ist ungültig oder abgelaufen.",
                )
            refresh_raw, _ = mcp_oauth_service.rotate_refresh(session, row)
            access_raw = mcp_oauth_service.rotate_access(session, row)
        return OAuthToken(
            access_token=access_raw,
            refresh_token=refresh_raw,
            expires_in=int(mcp_oauth_service.ACCESS_TTL.total_seconds()),
        )

    async def load_access_token(self, token_str: str) -> Optional[AccessToken]:
        """Bearer-Middleware-Verifikation: `aic_…` = PAT, sonst OAuth."""
        with Session(engine) as session:
            if token_str.startswith("aic_"):
                t = mcp_token_service.verify_token(session, token_str)
                if not t:
                    return None
                return AccessToken(
                    token=token_str,
                    client_id="pat",
                    scopes=[],
                    expires_at=int(t.expires_at.timestamp()) if t.expires_at else None,
                    resource=None,
                    subject=str(t.user_id),
                )
            row = mcp_oauth_service.lookup_access(session, token_str)
            if not row:
                return None
            return AccessToken(
                token=token_str,
                client_id=row.client_id,
                scopes=[],
                expires_at=int(row.access_expires_at.timestamp()),
                resource=None,
                subject=str(row.user_id),
            )

    async def revoke_token(self, token) -> None:
        """Zugriff des Clients komplett trennen (Access- oder Refresh-Token)."""
        with Session(engine) as session:
            row = mcp_oauth_service.lookup_any(session, token.token)
            if row:
                mcp_oauth_service.revoke_grant(session, row.user_id, row.client_id)
                logger.info("[MCP-OAuth] Token von Client %s / User %s widerrufen.",
                            row.client_id, row.user_id)


# ─── Consent-Routen (Browser) ─────────────────────────────────────

consent_router = APIRouter()


def _user_from_cookie(request: Request, session: Session) -> Optional[User]:
    payload = decode_access_token(request.cookies.get("access_token") or "")
    if not payload or not isinstance(payload.get("sub"), int):
        return None
    return session.get(User, payload["sub"])


def _login_redirect(code_raw: str) -> RedirectResponse:
    return RedirectResponse(
        f"/login?next={quote('/mcp/oauth/authorize?session=' + code_raw, safe='')}",
        status_code=302,
    )


def _consent_context(
    request: Request,
    user: Optional[User],
    session: Session,
    client_name: Optional[str],
    code_raw: str,
    can_write: bool,
    error: Optional[str],
) -> dict:
    """Template-Kontext (Format wie die Seiten in main.py)."""
    is_admin = user is not None and user.role.value == "ADMIN"
    courses = []
    if user is not None:
        for m in session.exec(
            select(UserCourse)
            .where(UserCourse.user_id == user.id)  # type: ignore[arg-type]
            .order_by(UserCourse.course_id)  # type: ignore[arg-type]
        ).all():
            course = session.get(Course, m.course_id)
            if not course:
                continue
            role = "PROF" if is_admin else m.role_in_course.value
            courses.append({
                "id": course.id,
                "name": course.name,
                "semester": course.semester,
                "role_in_course": role,
            })
    return {
        "request": request,
        "page_title": "MCP-Zugriff bestätigen",
        "current_user": (
            {
                "id": user.id,
                "username": user.username,
                "name": user.name,
                "role": "Admin" if is_admin else "User",
                "avatar": None,
            }
            if user
            else None
        ),
        "courses": courses,
        "selected_course_id": None,
        "is_admin": is_admin,
        "client_name": client_name,
        "code": code_raw,
        "can_write": can_write,
        "error": error,
    }


@consent_router.get("/mcp/oauth/authorize")
async def consent_page(request: Request, session: str = ""):
    """Consent-Seite: welcher Client, welche Kurse, Write ja/nein."""
    with Session(engine) as db:
        row = mcp_oauth_service.get_code_by_raw(db, session)
        if not row:
            return templates.TemplateResponse(
                "mcp_oauth_consent.html",
                _consent_context(request, None, db, None, session, False,
                                 "Der Link ist ungültig oder abgelaufen. Bitte verbinde dich mit dem Tool erneut."),
            )
        user = _user_from_cookie(request, db)
        if not user:
            return _login_redirect(session)
        client_row = mcp_oauth_service.get_client_row(db, row.client_id)
        client_name = client_row.client_name if client_row else None
        if row.user_id is not None and row.user_id != user.id:
            return templates.TemplateResponse(
                "mcp_oauth_consent.html",
                _consent_context(request, user, db, client_name, session, False,
                                 "Dieser Link wurde von einem anderen Nutzer bestätigt."),
            )
        can_write = user.role.value == "ADMIN" or any(
            m.role_in_course.value in ("PROF", "TUTOR")
            for m in db.exec(
                select(UserCourse).where(UserCourse.user_id == user.id)
            ).all()
        )
        return templates.TemplateResponse(
            "mcp_oauth_consent.html",
            _consent_context(request, user, db, client_name, session, can_write, None),
        )


@consent_router.post("/mcp/oauth/authorize")
async def consent_submit(
    request: Request,
    session: str = Form(...),
    course: List[int] = Form(default=[]),
    allow_write: Optional[str] = Form(default=None),
):
    """Consent bestätigen → Code zurück an den Client (302 mit code+state)."""
    with Session(engine) as db:
        row = mcp_oauth_service.get_code_by_raw(db, session)
        user = _user_from_cookie(request, db)
        if not row or not user or (row.user_id is not None and row.user_id != user.id):
            return _login_redirect(session) if not user else _consent_error(request, session, "Ungültiger oder abgelaufener Link.")

        mine = {
            m.course_id
            for m in db.exec(select(UserCourse).where(UserCourse.user_id == user.id)).all()
        }
        valid = [cid for cid in sorted(set(course)) if cid in mine]
        if not valid:
            return _consent_error(request, session, "Bitte wähle mindestens einen deiner Kurse aus.")

        mcp_oauth_service.confirm_code(db, row, user.id, valid, allow_write == "1")  # type: ignore[arg-type]
        logger.info("[MCP-OAuth] User '%s' hat %s für Kurse %s zugelassen (Write: %s).",
                    user.username, row.client_id, valid, allow_write == "1")
        # URL vor dem Session-Ende bauen (ORM-Objekt danach detached).
        redirect_url = construct_redirect_uri(row.redirect_uri, code=session, state=row.state)
    return RedirectResponse(redirect_url, status_code=302)


@consent_router.post("/mcp/oauth/deny")
async def consent_deny(request: Request, session: str = Form(...)):
    """Consent ablehnen → 302 mit error=access_denied (state wird mitgegeben)."""
    with Session(engine) as db:
        row = mcp_oauth_service.get_code_by_raw(db, session)
        if not row:
            return _consent_error(request, session, "Ungültiger oder abgelaufener Link.")
        user = _user_from_cookie(request, db)
        if user and row.user_id is None:
            mcp_oauth_service.consume_code(db, row)  # Code unbrauchbar machen
        redirect_url = construct_redirect_uri(row.redirect_uri, error="access_denied", state=row.state)
    return RedirectResponse(redirect_url, status_code=302)


def _consent_error(request: Request, code_raw: str, message: str):
    """Fehlerseite (z. B. für Bestätigungs-POSTs ohne gültigen Code)."""
    with Session(engine) as db:
        user = _user_from_cookie(request, db)
        if not user:
            return _login_redirect(code_raw)
        row = mcp_oauth_service.get_code_by_raw(db, code_raw)
        client_row = mcp_oauth_service.get_client_row(db, row.client_id) if row else None
        ctx = _consent_context(
            request, user, db,
            client_row.client_name if client_row else None,
            code_raw, False, message,
        )
    return templates.TemplateResponse("mcp_oauth_consent.html", ctx)


# ─── Settings-API (Grants verwalten) ──────────────────────────────

router = APIRouter(prefix="/api/mcp-oauth", tags=["MCP"])


class GrantUpdate(BaseModel):
    course_ids: list[int] = Field(min_length=1)
    allow_write: bool


def _my_course_ids(session: Session, user: User) -> set[int]:
    return {
        m.course_id
        for m in session.exec(select(UserCourse).where(UserCourse.user_id == user.id)).all()
    }


@router.get("/")
async def list_grants(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Alle MCP-OAuth-Clients des Users (pro Client: Kurse + Write)."""
    return {"grants": mcp_oauth_service.grants_for_user(session, user.id)}  # type: ignore[arg-type]


@router.put("/{client_id}")
async def update_grant(
    client_id: str,
    data: GrantUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kurse/Write eines Clients ändern (ersetzt die Allowlist)."""
    if not session.exec(
        select(McpOAuthToken)
        .where(McpOAuthToken.user_id == user.id)
        .where(McpOAuthToken.client_id == client_id)
    ).first():
        raise HTTPException(404, "Keine Verbindung zu diesem Client vorhanden.")
    mine = _my_course_ids(session, user)
    if not set(data.course_ids) <= mine:
        raise HTTPException(400, "Alle ausgewählten Kurse müssen deine eigenen Kurse sein.")
    mcp_oauth_service.set_grant_courses(session, user.id, client_id, data.course_ids)  # type: ignore[arg-type]
    mcp_oauth_service.set_allow_write(session, user.id, client_id, data.allow_write)  # type: ignore[arg-type]
    logger.info("[MCP-OAuth] User '%s' hat Grant %s aktualisiert (Kurse: %s, Write: %s).",
                user.username, client_id, data.course_ids, data.allow_write)
    return {"message": "Zugriff wurde aktualisiert."}


@router.delete("/{client_id}")
async def revoke_grant(
    client_id: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Client vollständig trennen (Tokens + Allowlist)."""
    token = session.exec(
        select(McpOAuthToken)
        .where(McpOAuthToken.user_id == user.id)
        .where(McpOAuthToken.client_id == client_id)
    ).first()
    if not token:
        raise HTTPException(404, "Keine Verbindung zu diesem Client vorhanden.")
    mcp_oauth_service.revoke_grant(session, user.id, client_id)  # type: ignore[arg-type]
    logger.info("[MCP-OAuth] User '%s' hat Client %s getrennt.", user.username, client_id)
    return {"message": "Der Client wurde getrennt."}
