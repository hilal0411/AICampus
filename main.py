"""
AICampus: FastAPI Application Entry Point

Start mit:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000

oder:
    python -m uvicorn main:app --reload
"""

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional, Sequence
import logging
import os
import re
import threading
from pathlib import Path
from urllib.parse import quote

# Logging konfigurieren
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

import hashlib
import json

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import TemplateNotFound
from markupsafe import Markup
from sqlmodel import Session, func, select

from config import BASE_DIR, DEBUG, LLM_TIMEOUT, PREVIEW_BASE_DOMAIN, PUBLIC_DOMAIN, VAPID_PUBLIC_KEY, push_enabled
from database.base import create_db_and_tables, engine, get_session, migrate_schema
from database.soft_delete import SOFT_DELETE_BYPASS
from database.models import (
    Course,
    CourseInvite,
    CourseMaterial,
    CourseMedia,
    CourseRole,
    CourseSlidesTheme,
    FeedbackSource,
    ForumChannel,
    ForumMessage,
    GlobalUserRole,
    HintExchange,
    MaterialType,
    Notification,
    ScriptSection,
    Task,
    TaskDraft,
    TaskType,
    User,
    UserCourse,
    Submission,
)
from services.auth_service import get_current_user, hash_password, require_course_access
from services import analytics, media_service, import_service, preview_proxy, timeutil, notifications
from services.media_service import sync_media_usages
from services.mc_service import (
    McValidationError,
    parse_mc_data,
    student_view as _mc_student_view,
    tutor_view as _mc_tutor_view,
)
from services import image_spec_service
from services import upload_service
from services import trash_service
from services.workspace_service import workspace_service, prune_backup_loop
from services.slides_service import (
    ASPECT_LABELS,
    ASPECT_RATIOS,
    THEME_TEMPLATES,
    resolve_theme,
    slide_count,
)

from api import admin, agent_api, analytics as analytics_api, auth, forum, importer, image_specs, media as media_api, materials as materials_api, mcp_context, mcp_oauth, mcp_server as mcp_server_api, mcp_tokens, notifications as notifications_api, push as push_api, references, review_workspace, script as script_api, script_questions, slides as slides_api, student, tutor, user_settings, course_members, versions as versions_api


def _calculate_percentile(my_score: float, other_scores: list[float]) -> int:
    if not other_scores:
        return 100
    below = sum(1 for s in other_scores if s < my_score)
    equal = sum(1 for s in other_scores if s == my_score)
    total = len(other_scores)
    return round((below + 0.5 * equal) / total * 100)


def _get_best_score(subs: Sequence[Submission]) -> float:
    if not subs:
        return 0.0
    latest_sub = subs[0]
    human_points = 0.0
    llm_points = 0.0
    override_exists = False
    for fb in latest_sub.feedback_list:
        if fb.source == FeedbackSource.HUMAN:
            human_points = max(human_points, fb.points_earned)
            override_exists = True
        else:
            llm_points = max(llm_points, fb.points_earned)
    return human_points if override_exists else llm_points


# ═══════════════════════════════════════════════════════════════════
# LIFESPAN: DB-Setup + Admin
# ═══════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """App-Start: DB-Tabellen erstellen + Schema-Migration + Admin-User bei leerer DB."""
    # MCP-Subapp: Starlette-Mount führt den Lifespan des Sub-Apps (Session-Manager-
    # Task-Group) nicht selbst aus — hier manuell starten/stoppen. mcp_app_raw:
    # mcp_app ist der CORS-Wrapper (s. api/mcp_server.py), das nackte SDK-App
    # muss hier laufen.
    async with mcp_server_api.mcp_app_raw.router.lifespan_context(
        mcp_server_api.mcp_app_raw
    ):
        create_db_and_tables()
        migrate_schema()
        migrate_script_sections()
        migrate_forum_channels()
        import_service.mark_interrupted_on_startup()  # aktive Import-Jobs → 'interrupted'
        analytics.prune_old()  # Page-View-Bereinigung (No-Op, solange RETENTION_DAYS=None)
        # Sleep-Backup-Retention (30 Tage nach letzter Aktivität)
        threading.Thread(target=prune_backup_loop, daemon=True).start()

        # Admin-User anlegen, wenn DB leer
        with Session(engine) as session:
            admin_exists = session.exec(
                select(User).where(User.username == "admin")
            ).first()

            if not admin_exists:
                _create_admin(session)

            # Kuratierte globale Image-Specs (idempotent)
            image_spec_service.seed_global_specs(session)

        yield


def _create_admin(session: Session):
    """Erstellt einen Admin-User (admin / admin) für frische Installationen."""
    admin = User(
        username="admin",
        email="admin@tutor-system.local",
        name="Administrator",
        role=GlobalUserRole.ADMIN,
        password_hash=hash_password("admin"),
    )
    session.add(admin)
    session.commit()
    logger.info("Admin-User 'admin' erstellt (Passwort: admin). Bitte aendern!")


def migrate_script_sections():
    """Migration: altes einzelnes Skript-Material → Skript-Kapitel.

    Das Skript besteht seitdem aus mehreren Markdown-Kapiteln (ScriptSection).
    Idempotent: ohne verbleibende script-Materialien ein no-op.
    """
    with Session(engine) as session:
        script_materials = session.exec(
            select(CourseMaterial).where(CourseMaterial.material_type == MaterialType.SCRIPT)
        ).all()
        for mat in script_materials:
            existing = session.exec(
                select(ScriptSection).where(ScriptSection.course_id == mat.course_id)
            ).first()
            if existing is None:
                session.add(
                    ScriptSection(
                        course_id=mat.course_id,
                        title=mat.title,
                        content=mat.content or "",
                        is_visible=mat.is_visible,
                        display_order=0,
                        created_by=mat.created_by,
                    )
                )
                session.commit()
            session.delete(mat)
            session.commit()
            media_service.sync_media_usages(session, mat.course_id)
            logger.info(f"Skript-Material '{mat.title}' (Kurs {mat.course_id}) in Kapitel umgewandelt.")


def migrate_forum_channels():
    """Migration: Forum-Kanäle für Daten aus der Zeit vor der Kanal-Einführung.

    Noch kanallose Nachrichten werden einem Default-Kanal 'Allgemein' zugeordnet
    (der bei Bedarf angelegt wird). Idempotent: ohne kanallose Nachrichten no-op;
    bewusst gelöschte Kanäle werden NICHT neu angelegt.
    """
    with Session(engine) as session:
        orphans_by_course: dict[int, list[ForumMessage]] = {}
        for m in session.exec(
            select(ForumMessage).where(ForumMessage.channel_id == None)
        ).all():
            orphans_by_course.setdefault(m.course_id, []).append(m)

        for course_id, orphans in orphans_by_course.items():
            course = session.get(Course, course_id)
            if course is None:
                continue
            ch = session.exec(
                select(ForumChannel).where(ForumChannel.course_id == course_id)
            ).first()
            if ch is None:
                ch = ForumChannel(
                    course_id=course_id,  # type: ignore[arg-type]
                    name="Allgemein",
                    created_by=course.created_by,
                )
                session.add(ch)
                session.commit()
                session.refresh(ch)
            for m in orphans:
                m.channel_id = ch.id
            session.commit()
            logger.info(
                f"{len(orphans)} Forum-Nachrichten (Kurs {course_id}) dem Default-Kanal 'Allgemein' zugeordnet."
            )


# ═══════════════════════════════════════════════════════════════════
# APP


app = FastAPI(
    title="AICampus",
    description="AI-gestütztes Tutoring-System für Übungsaufgaben an Universitäten",
    version="0.1.0",
    lifespan=lifespan,
)

# Templates + Static
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _css_string(value: object) -> Markup:
    """String als CSS-String für `content:` (z. B. @page-Margin-Boxen).
    Escape nur, was CSS verlangt — Non-ASCII bleibt raw UTF-8 (JSONs
    \\uXXXX, wie von tojson, ist KEIN gültiges CSS-Escape; 6-stellige
    Hex-Escapes verschlingen bei Chromium versehentlich nachfolgende
    Leerzeichen)."""
    s = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return Markup(f'"{s}"')


templates.env.filters["css_string"] = _css_string

# ── Static asset caching (Tornado-style) ─────────────────────
# At startup, hash every file in static/ and build a lookup map.
# `asset("css/main.css")` → "/static/css/main.css?v=a1b2c3d4"
# Changing the file content changes the hash → browser fetches fresh.
# Dev-Modus (AICAMPUS_DEV=1, Live-Code via compose.dev.yml): ?v= kommt
# aus der Datei-Mtime statt aus der Startup-Map → jeder Save erzeugt
# eine neue URL, ohne dass die App neu starten muss.

_static_hashes: dict[str, str] = {}
_dev_mode = os.environ.get("AICAMPUS_DEV") == "1"

if (BASE_DIR / "static").exists():
    for root, _dirs, files in os.walk(BASE_DIR / "static"):
        for fname in files:
            fpath = Path(root) / fname
            rel = fpath.relative_to(BASE_DIR / "static")
            h = hashlib.sha256(fpath.read_bytes()).hexdigest()[:8]
            _static_hashes[str(rel).replace("\\", "/")] = h

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _asset(path: str) -> str:
    """Tornado-style static_url: append content hash as ?v= query param."""
    key = path.lstrip("/")
    if _dev_mode:
        fpath = BASE_DIR / "static" / key
        if fpath.is_file():
            return f"/static/{key}?v={fpath.stat().st_mtime_ns}"
        return f"/static/{key}"
    v = _static_hashes.get(key)
    if v:
        return f"/static/{key}?v={v}"
    return f"/static/{key}"


templates.env.globals["asset"] = _asset

# Zeit-Helper für Templates: [data-ts] wird client-seitig in die lokale
# Zeit umgerechnet (s. base.html); Deadlines werden normalisiert (…Z).
templates.env.globals["utc_iso"] = timeutil.utc_iso
templates.env.globals["deadline_iso"] = timeutil.deadline_iso
templates.env.globals["deadline_expired"] = timeutil.deadline_expired

# Web Push (M3): Status + öffentlicher VAPID-Key für das Frontend
# („Push aktivieren“-Button im Glocken-Menü + SW-Registration).
# Evaluierung beim Import ist okay: uvicorn startet neu bei jedem Reload.
templates.env.globals["push_config"] = {
    "enabled": push_enabled(),
    "public_key": VAPID_PUBLIC_KEY if push_enabled() else "",
}


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Service Worker (M3: Web Push). Muss auf / liegen: Nur dann
    deckt das SW-Scope die ganze App ab und Push/Click greifen überall."""
    return FileResponse(BASE_DIR / "static/sw.js", media_type="application/javascript")


def _public_base_url(request: Request) -> str:
    """Öffentliche Basis-URL (canonical, og:url, Sitemap-Verweis).

    Hinter nginx kommt der Request mit X-Forwarded-Proto — das Scheme
    daraus nehmen (request.url.scheme würde hier 'http' melden).
    """
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    scheme = proto if proto in ("http", "https") else request.url.scheme
    return f"{scheme}://{request.url.netloc}/"


templates.env.globals["public_base_url"] = _public_base_url


@app.middleware("http")
async def _no_store_for_html(request: Request, call_next):
    """HTML-Seiten nie cachen (auch nicht heuristisch).

    Die Seiten tragen ihr JS inline — eine vom Browser gecachte alte
    Version waere dauerhaft defekt, bis manuell hart neu geladen wird
    (Cache-Control: no-store macht das obsolet).
    """
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if "text/html" in ctype and not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def _noindex_api_docs(request: Request, call_next):
    """Swagger/ReDoc/OpenAPI vor dem Index schützen.

    Die API-Doku ist öffentlich erreichbar; X-Robots-Tag ergänzt die
    Disallow-Regeln in robots.txt (beides zusammen ist robuster)."""
    response = await call_next(request)
    if request.url.path in ("/docs", "/redoc", "/openapi.json"):
        response.headers["X-Robots-Tag"] = "noindex"
    return response


@app.middleware("http")
async def _cors_for_static_fonts(request: Request, call_next):
    """CORS für statische Fonts (woff2/woff/ttf/otf).

    Sandboxed Applets (opaque Origin „null“) dürfen @font-face-Fonts nur mit
    Access-Control-Allow-Origin laden — der einzige CORS-geschützte Ressourcen-Typ,
    den Applets brauchen (Scripts/CSS sind nicht betroffen). Deshalb nur auf
    Font-Pfade begrenzt; authentifizierte Antworten (z. B. /media/) bleiben
    unberührt.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/") and re.search(
        r"\.(?:woff2?|ttf|otf)$", request.url.path, re.IGNORECASE
    ):
        response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.middleware("http")
async def _page_view_tracking(request: Request, call_next):
    """Nutzungs-Tracking (in-house): wer hat wann welche Seite geöffnet.

    Füllt die page_views-Tabelle für HTML-Seiten eingeloggter User
    (Filter/Klassifikation: services/analytics.py). MUSS vor dem
    PreviewSubdomainMiddleware registriert werden, damit das Preview-Proxy
    äußerste Schicht bleibt (s. services/preview_proxy.py).
    """
    response = await call_next(request)
    try:
        analytics.maybe_track_page_view(request, response)
    except Exception:
        logger.exception("Analytics-Tracking fehlgeschlagen")
    return response


# Preview-Subdomains (rohes ASGI-Middleware für HTTP + WebSocket) —
# MUSS nach den @app.middleware("http")-Aufrufen hinzukommen, damit es
# äußerste (zuerst laufende) Schicht ist (s. services/preview_proxy.py).
app.add_middleware(preview_proxy.PreviewSubdomainMiddleware)


# API-Routes
app.include_router(auth.router)
app.include_router(agent_api.router)  # /api/agent/… (Agent-Callbacks, HMAC)
app.include_router(user_settings.router)
app.include_router(admin.router)
app.include_router(analytics_api.router)
app.include_router(image_specs.router)
app.include_router(tutor.router)
app.include_router(course_members.router)
app.include_router(student.router)
app.include_router(review_workspace.router)  # /api/review/… (Review-Workspace)
app.include_router(media_api.router)
app.include_router(materials_api.router)
app.include_router(references.router)
app.include_router(slides_api.router)
app.include_router(script_api.router)
app.include_router(forum.router)
app.include_router(script_questions.router)
app.include_router(notifications_api.router)  # /api/notifications/… (in-App-Glocke)
app.include_router(push_api.router)  # /api/push/… (M3: Browser-Push-Subscriptions)
app.include_router(versions_api.router)  # /api/versions/… (Versions-History + Autosave)
app.include_router(importer.router)
app.include_router(mcp_tokens.router)  # /api/mcp-tokens/… (MCP-Token verwalten)
app.include_router(mcp_oauth.router)  # /api/mcp-oauth/… (MCP-OAuth-Grants verwalten)
app.include_router(mcp_context.router)  # /api/mcp/client-context (UI-Zustand für get_user_focus)
app.include_router(preview_proxy.router)  # /preview/{task}/{port}/… (same-origin)

# MCP-Consent-Seite: Route muss VOR dem Mount gematcht werden
# (Starlette-Routenreihenfolge — sonst frisst der Mount /mcp/oauth/authorize).
app.include_router(mcp_oauth.consent_router)

# MCP-Server (streamable HTTP) — Bearer-Token-Auth (PAT + OAuth), s. api/mcp_server.py
# Claude (claude.ai) probiert POST /mcp (ohne Trailing-Slash) — der Mount würde
# 307 auf /mcp/ redirecten, was Claude nicht zuverlässig folgt. Die Route muss
# VOR dem Mount stehen (Starlette: First Match). Class-Instanz statt Funktion:
# Starlette behandelt nur Funktionen/Methoden als request-response-Endpoint,
# alles andere als ASGI-App (Doppel-Wrapping → TypeError).
from starlette.routing import Route
from pydantic import AnyHttpUrl
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions

# OAuth-Endpunkte (RFC 8414/6749) einmal anlegen: am Domain-Root spiegeln
# (Claude streift den /mcp-Pfad von der Issuer-URL und probiert root-level
# URLs; 404 auf .../register war der Grund fuer das Scheitern von
# "Automatisch registrieren") und als Aliase UNTER dem /mcp-Prefix: ChatGPT
# probiert <mcp_url>/.well-known/openid-configuration - die Routen muessten
# VOR dem Mount stehen, sonst frisst die Sub-App den Pfad (404).
# Gleicher Provider -> gleiche Clients.
_mcp_auth_routes = create_auth_routes(
    mcp_server_api.provider,
    AnyHttpUrl(mcp_server_api.MCP_ISSUER),
    client_registration_options=ClientRegistrationOptions(enabled=True),
    revocation_options=RevocationOptions(enabled=True),
)
_mcp_metadata = next(
    _r for _r in _mcp_auth_routes if _r.path == "/.well-known/oauth-authorization-server"
)

class _MCPAppNoSlash:
    async def __call__(self, scope, receive, send):
        scope = {**scope, "path": "/", "raw_path": b"/",
                 "root_path": scope.get("root_path", "") + "/mcp"}
        await mcp_server_api.mcp_app(scope, receive, send)

app.router.routes.append(Route("/mcp", _MCPAppNoSlash()))
# Metadata unter der MCP-URL: OIDC-Name (ChatGPT) + RFC-8414-Name.
app.router.routes.append(Route("/mcp/.well-known/openid-configuration",
                               _mcp_metadata.endpoint, methods=_mcp_metadata.methods))
app.router.routes.append(Route("/mcp/.well-known/oauth-authorization-server",
                               _mcp_metadata.endpoint, methods=_mcp_metadata.methods))
app.mount("/mcp", mcp_server_api.mcp_app)

# RFC 9728 Protected-Resource-Metadata am Domain-Root: der WWW-Authenticate-Header
# verweist dorthin (die Sub-App kopiert die Metadata nur unter dem /mcp-Prefix).
# Claude probiert auch die Varianten ohne Trailing-Slash und die Root-Variante.
for _r in mcp_server_api.protected_resource_routes:
    app.add_route(_r.path, _r.endpoint, methods=_r.methods)
    app.add_route(_r.path.rstrip("/"), _r.endpoint, methods=_r.methods)
    app.add_route("/.well-known/oauth-protected-resource", _r.endpoint, methods=_r.methods)

# OAuth-Endpunkte am Domain-Root (s. oben) + AS-Metadata in der Path-Suffix-
# Form (RFC 8414 Section 3.2.3) und unter dem OIDC-Namen (ChatGPT-Variante).
for _r in _mcp_auth_routes:
    app.add_route(_r.path, _r.endpoint, methods=_r.methods)
    if _r.path == "/.well-known/oauth-authorization-server":
        app.add_route(_r.path + "/mcp", _r.endpoint, methods=_r.methods)
        app.add_route("/.well-known/openid-configuration", _r.endpoint, methods=_r.methods)
        app.add_route("/.well-known/openid-configuration/mcp", _r.endpoint, methods=_r.methods)


# ═══════════════════════════════════════════════════════════════════
# GLOBAL EXCEPTION HANDLERS
# ═══════════════════════════════════════════════════════════════════

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """401 → Redirect zur Login-Seite statt JSON-Fehler."""
    if exc.status_code == 401:
        # Machine-to-Machine (Agent-Callback mit X-Agent-Token): JSON-Fehler,
        # kein Login-Redirect (der Agent hat kein Browser-Konto).
        if "X-Agent-Token" in request.headers:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=exc.status_code,
                                content={"detail": exc.detail})
        # Ziel-URL merken, damit es nach dem Login zurückgeht
        # (Login/Signup selbst und API-Calls ausgenommen).
        next_url = ""
        if request.url.path not in ("/login", "/signup") and not request.url.path.startswith("/api/"):
            next_url = request.url.path
            if request.url.query:
                next_url += f"?{request.url.query}"
        login_url = f"/login?next={quote(next_url, safe='')}" if next_url else "/login"
        # HTMX-Requests: HX-Redirect Header (HTMX folgt automatisch)
        if "HX-Request" in request.headers:
            from fastapi.responses import Response
            return Response(status_code=401, headers={"HX-Redirect": login_url})
        return RedirectResponse(url=login_url, status_code=302)
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ═══════════════════════════════════════════════════════════════════
# WEB-PAGES (HTMX-gerendert)
# ═══════════════════════════════════════════════════════════════════

def _get_user_courses(user: User, session: Session) -> list[dict[str, Any]]:
    """Lädt alle Kurse eines Users mit Rolle."""
    memberships = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .order_by(UserCourse.id.desc())
    ).all()

    courses = []
    for m in memberships:
        course = session.get(Course, m.course_id)
        if course:
            courses.append({
                "id": course.id,
                "name": course.name,
                "semester": course.semester,
                "role_in_course": m.role_in_course.value,
            })
    return courses


def _user_ctx(user: User, role: str) -> dict[str, Any]:
    """Template-Kontext für current_user (Navbar + Seiten-Kopf).

    Enthält das Profilbild (avatar), falls gesetzt — zentrales Format für
    alle Seiten, damit Templates nicht doppelt gebaut werden müssen.
    """
    return {
        "id": user.id,
        "username": user.username,
        "name": user.name,
        "role": role,
        "avatar": f"/avatars/{user.avatar.rsplit('/', 1)[-1]}" if user.avatar else None,
    }


def _course_tab_context(
    session: Session,
    user: User,
    request: Request,
    course_id: int,
    active_tab: str,
    page_title: str | None = None,
) -> tuple[UserCourse | None, dict[str, Any]]:
    """Gemeinsamer Template-Kontext für alle Kurs-Tab-Seiten.

    Lädt Kurs + Membership, bestimmt die Rollen und baut die Tab-Leiste
    (Rollen-Sichtbarkeit zentral geregelt — später um Skript/Slides/Medien
    erweiterbar). Die Zugriffskontrolle (wer darf welche Seite öffnen)
    bleibt Aufgabe der Route: `membership` kann None sein (z. B. Admin
    ohne Membership auf der Mitglieder-Seite).
    """
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()

    is_admin = user.role == GlobalUserRole.ADMIN
    role = membership.role_in_course if membership else None
    is_tutor = role in (CourseRole.PROF, CourseRole.TUTOR)
    is_prof = role == CourseRole.PROF

    # Slide-Decks: für Studenten nur, wenn mindestens eines sichtbar;
    # für Tutor/PROF/Admin immer (ggf. mit Empty-State + Anlegen-CTA)
    slides_decks = session.exec(
        select(CourseMaterial)
        .where(CourseMaterial.course_id == course_id)
        .where(CourseMaterial.material_type == MaterialType.SLIDES)
    ).all()

    # Skript-Kapitel: für Studenten erst, wenn mindestens eines freigeschaltet ist
    visible_sections = session.exec(
        select(ScriptSection)
        .where(ScriptSection.course_id == course_id)
        .where(ScriptSection.is_visible == True)  # noqa: E712
    ).all()

    # Aufgaben: für Studenten erst, wenn mindestens eine sichtbar ist
    visible_tasks = session.exec(
        select(Task)
        .where(Task.course_id == course_id)
        .where(Task.is_visible == True)  # noqa: E712
    ).all()

    tabs: list[dict[str, Any]] = []
    if is_tutor or is_admin or visible_sections:
        tabs.append(
            {
                "key": "script",
                "icon": "📖",
                "label": "Skript",
                "url": f"/courses/{course_id}/script",
                "active": active_tab == "script",
            }
        )
    if is_tutor or is_admin or any(d.is_visible for d in slides_decks):
        tabs.append(
            {
                "key": "slides",
                "icon": "📽️",
                "label": "Folien",
                "url": f"/courses/{course_id}/slides",
                "active": active_tab == "slides",
            }
        )

    if is_tutor or is_admin or visible_tasks:
        tabs.append(
            {
                "key": "tasks",
                "icon": "📝",
                "label": "Aufgaben",
                "url": f"/courses/{course_id}/tasks",
                "active": active_tab == "tasks",
            }
        )
    if is_tutor:
        tabs.append(
            {
                "key": "overview",
                "icon": "📊",
                "label": "Übersicht",
                "url": f"/courses/{course_id}/overview",
                "active": active_tab == "overview",
            }
        )
    # Forum: für alle Kurs-Mitglieder (Student/Tutor/PROF) + Admins
    if membership is not None or is_admin:
        # Ungelesen-Zähler (Summe über alle NICHT gemuteten Kanäle) als Badge am Forum-Tab
        unread = forum.load_unread_counts(session, course_id, user.id)  # type: ignore[arg-type]
        muted = forum.muted_channel_ids(session, user.id)  # type: ignore[arg-type]
        forum_badge = sum(cnt for ch_id, cnt in unread.items() if ch_id not in muted)
        tabs.append(
            {
                "key": "forum",
                "icon": "💬",
                "label": "Forum",
                "url": f"/courses/{course_id}/forum",
                "active": active_tab == "forum",
                "badge": forum_badge,
            }
        )
    if is_prof or is_admin:
        tabs.append(
            {
                "key": "members",
                "icon": "👥",
                "label": "Mitglieder",
                "url": f"/courses/{course_id}/members",
                "active": active_tab == "members",
            }
        )
    if is_prof or is_admin:
        tabs.append(
            {
                "key": "media",
                "icon": "🖼️",
                "label": "Medien",
                "url": f"/courses/{course_id}/media",
                "active": active_tab == "media",
            }
        )
    if is_tutor or is_admin:
        tabs.append(
            {
                "key": "references",
                "icon": "📚",
                "label": "Quellen",
                "url": f"/courses/{course_id}/references",
                "active": active_tab == "references",
            }
        )
    if is_prof or is_admin:
        tabs.append(
            {
                "key": "settings",
                "icon": "⚙️",
                "label": "Einstellungen",
                "url": f"/courses/{course_id}/settings",
                "active": active_tab == "settings",
            }
        )

    ctx = {
        "request": request,
        "page_title": page_title or course.name,
        "current_user": _user_ctx(user, role.value if role else "ADMIN"),
        "courses": _get_user_courses(user, session),
        "selected_course_id": course_id,
        "is_admin": is_admin,
        "course": {
            "id": course.id,
            "name": course.name,
            "description": course.description,
            "semester": course.semester,
        },
        "is_tutor": is_tutor,
        "is_prof": is_prof,
        "tabs": tabs,
        "active_tab": active_tab,
        "has_visible_sections": bool(visible_sections),
        "has_visible_tasks": bool(visible_tasks),
    }
    return membership, ctx


@app.get("/")
async def index(
    request: Request,
    session: Session = Depends(get_session),
):
    """Home-Seite: About (Gast), Login (abgelaufene Session) oder Dashboard."""
    # Prüfen, ob User eingeloggt
    token = request.cookies.get("access_token")
    if not token:
        # Gast (noch nie angemeldet) → About-Seite als Landing
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/about", status_code=302)

    try:
        user = await get_current_user(request, session)
    except HTTPException:
        # Token ungültig → Login
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/login")

    courses = _get_user_courses(user, session)

    # Quick Stats fuer Studenten (nur wenn User mindestens eine STUDENT-Rolle hat)
    total_completed = 0
    total_points_earned = 0.0
    total_points_possible = 0
    stats_by_course = {}
    student_courses = [
        c for c in courses
        if c["role_in_course"] == CourseRole.STUDENT.value
    ]
    if student_courses:
        for c in student_courses:
            course_id = c["id"]
            tasks = session.exec(
                select(Task)
                .where(Task.course_id == course_id)
                .where(Task.is_visible == True)  # noqa: E712
            ).all()
            # Ohne Aufgaben mit Punkten: kein Fortschritt/keine Punkte auf dem Kurs-Tile
            point_tasks = [t for t in tasks if t.max_points > 0]
            if not point_tasks:
                continue
            course_earned = 0.0
            course_possible = 0
            completed_count = 0
            medals = {"bronze": 0, "silver": 0, "gold": 0, "platinum": 0}
            for task in point_tasks:
                course_possible += task.max_points
                subs = session.exec(
                    select(Submission)
                    .where(Submission.task_id == task.id)
                    .where(Submission.student_id == user.id)
                    .order_by(Submission.submitted_at.desc())  # type: ignore[attr-defined]
                ).all()

                latest = 0.0
                if subs:
                    human_points = 0.0
                    llm_points = 0.0
                    override_exists = False
                    for fb in subs[0].feedback_list:
                        if fb.source == FeedbackSource.HUMAN:
                            human_points = max(human_points, fb.points_earned)
                            override_exists = True
                        else:
                            llm_points = max(llm_points, fb.points_earned)
                    latest = human_points if override_exists else llm_points

                if subs:
                    completed_count += 1
                course_earned += latest

                # Medal count per task
                if task.max_points > 0:
                    pct = latest / task.max_points
                    if pct >= 1.0: medals["platinum"] += 1
                    elif pct >= 0.9: medals["gold"] += 1
                    elif pct >= 0.8: medals["silver"] += 1
                    elif pct >= 0.7: medals["bronze"] += 1

            total_completed += completed_count
            total_points_earned += course_earned
            total_points_possible += course_possible
            stats_by_course[course_id] = {
                "completed": completed_count,
                "total_tasks": len(point_tasks),
                "earned": course_earned,
                "possible": course_possible,
                "medals": medals,
            }

    pct = 0.0
    if total_points_possible > 0:
        pct = round(total_points_earned / total_points_possible * 100, 1)

    # Rolle anzeigen: Admin bleibt "Admin", User zeigt die Rolle im aktuellen Kurs
    # Auf dem Dashboard (kein Kurs) zeigen wir die globale Rolle
    display_role = "Admin" if user.role == GlobalUserRole.ADMIN else "User"

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "page_title": "Dashboard",
            "current_user": _user_ctx(user, display_role),
            "courses": courses,
            "selected_course_id": None,
            "is_admin": user.role == GlobalUserRole.ADMIN,
            "total_completed": total_completed,
            "total_points_earned": total_points_earned,
            "total_points_possible": total_points_possible,
            "total_percentage": pct,
            "stats_by_course": stats_by_course,
        },
    )


@app.get("/login")
async def login_page(request: Request):
    """Login-Seite anzeigen."""
    next_url = request.query_params.get("next", "")
    return templates.TemplateResponse("login.html", {"request": request, "next_url": next_url})


@app.get("/about")
async def about_page(request: Request, session: Session = Depends(get_session)):
    """Öffentliche About-Seite — Pitch für Dozierende, Verwaltung & Studierende.

    Landing-Page für Gäste: wer nicht eingeloggt ist, landet über / hier.
    Eingeloggte Nutzer bekommen den normalen Nav-Context.
    """
    ctx: dict[str, Any] = {
        "request": request,
        # og:title der Landing-Page (der <title>-Tag kommt aus dem
        # title-Block in about.html — die beiden dürfen abweichen).
        "page_title": "AICampus – KI-Plattform für MINT-Lehre mit lokalen LLMs",
        "page_description": (
            "AICampus erzeugt KI-gestützt Lehrmaterialien und Übungsaufgaben, "
            "korrigiert Abgaben automatisch und gibt Studierenden sofortiges "
            "Feedback — betrieben mit lokalen LLMs auf eigenen Servern, ohne Cloud."
        ),
    }
    ctx["today_de"] = datetime.now().strftime("%d.%m.%Y")
    try:
        user = await get_current_user(request, session)
    except HTTPException:
        user = None
    if user:
        display_role = "Admin" if user.role == GlobalUserRole.ADMIN else "User"
        ctx.update({
            "current_user": _user_ctx(user, display_role),
            "courses": _get_user_courses(user, session),
            "selected_course_id": None,
            "is_admin": user.role == GlobalUserRole.ADMIN,
        })
    return templates.TemplateResponse("about.html", ctx)


@app.get("/datenschutz")
async def datenschutz_page(request: Request, session: Session = Depends(get_session)):
    """Öffentliche Datenschutzerklärung."""
    ctx: dict[str, Any] = {
        "request": request,
        "page_title": "Datenschutzerklärung – AICampus",
        "page_description": (
            "Welche Daten AICampus warum verarbeitet, wie die Nutzung protokolliert wird "
            "und welche Datenschutz-Rechte Ihnen zustehen."
        ),
        "stand_datum": datetime.now().strftime("%d.%m.%Y"),
    }
    try:
        user = await get_current_user(request, session)
    except HTTPException:
        user = None
    if user:
        display_role = "Admin" if user.role == GlobalUserRole.ADMIN else "User"
        ctx.update({
            "current_user": _user_ctx(user, display_role),
            "courses": _get_user_courses(user, session),
            "selected_course_id": None,
            "is_admin": user.role == GlobalUserRole.ADMIN,
        })
    return templates.TemplateResponse("datenschutz.html", ctx)


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt(request: Request):
    """Crawler-Steuerung: nur die öffentliche Landing-Seite indexieren.

    App-Bereiche sind hinter Login (Crawler bekommen 302→/login); die
    Disallow-Regeln hier vermeiden, dass Crawler sie trotzdem anfragen.
    """
    return PlainTextResponse(
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        "# Login/Registrierung + Kurs-Invite (kein Indexierungswert, noindex-Meta in den Templates)\n"
        "Disallow: /login\n"
        "Disallow: /signup\n"
        "Disallow: /join\n"
        "# App-Interna: API, Swagger-Doku, authentifizierte Dateien, Workspace-Previews\n"
        "Disallow: /api/\n"
        "Disallow: /docs\n"
        "Disallow: /redoc\n"
        "Disallow: /openapi.json\n"
        "Disallow: /media/\n"
        "Disallow: /avatars/\n"
        "Disallow: /preview/\n"
        "\n"
        f"Sitemap: {_public_base_url(request)}sitemap.xml\n"
    )


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml(request: Request):
    """Sitemap der indexierbaren (öffentlichen) Seiten.

    Bei neuen indexierbaren Seiten (z. B. publizierte Doku) hier
    weitere <url>-Einträge ergänzen.
    """
    base = _public_base_url(request)
    lastmod = datetime.now().strftime("%Y-%m-%d")
    return Response(
        content=(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n"
            "  <url>\n"
            f"    <loc>{base}about</loc>\n"
            f"    <lastmod>{lastmod}</lastmod>\n"
            "    <changefreq>monthly</changefreq>\n"
            "    <priority>1.0</priority>\n"
            "  </url>\n"
            "</urlset>\n"
        ),
        media_type="application/xml; charset=utf-8",
    )


async def _do_login(
    username: str,
    password: str,
    session: Session,
    request: Request,
):
    """
    Gemeinsame Login-Logik (wird von POST /login und POST /api/auth/login genutzt).
    """
    username = username.strip()

    if not username or not password:
        return None, "Username und Password erforderlich.", 400

    from services.settings_resolver import get_effective_ldap_config
    from services.auth_service import authenticate_user, create_access_token

    ldap_cfg = get_effective_ldap_config(session)

    user = await authenticate_user(
        username, password, session,
        use_ldap=ldap_cfg["use_ldap"],
        ldap_server=ldap_cfg["ldap_server"],
        ldap_base_dn=ldap_cfg["ldap_base_dn"],
        ldap_bind_dn=ldap_cfg["ldap_bind_dn"],
        ldap_bind_pw=ldap_cfg["ldap_bind_pw"],
        ldap_user_search=ldap_cfg["ldap_user_search"],
    )

    if not user:
        return None, "Ungültiger Username oder Password.", 401

    token_data = {"sub": user.id, "username": user.username}
    token = create_access_token(token_data)

    return user, token, 200


@app.post("/login")
async def login_submit(
    request: Request,
    session: Session = Depends(get_session),
):
    """
    Login per Formular-POST (direkt an /login).

    Akzeptiert beide Content-Types:
    - application/x-www-form-urlencoded (normales HTML-Formular)
    - application/json (HTMX, Fetch-API, etc.)
    """
    content_type = request.headers.get("content-type", "")
    next_url = ""

    if "application/json" in content_type:
        # JSON-Body (HTMX / Fetch)
        body: dict = await request.json()
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
        next_url = str(body.get("next_url", ""))
    else:
        # Form-encoded (normales HTML-Formular)
        form = await request.form()
        username_val = form.get("username", "")
        password_val = form.get("password", "")
        if hasattr(username_val, "read"):
            username = ""
        else:
            username = str(username_val)
        if hasattr(password_val, "read"):
            password = ""
        else:
            password = str(password_val)
        next_url = str(form.get("next_url", ""))

    user, token, status = await _do_login(username, password, session, request)

    if not user:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": token,  # Fehlermeldung
            "next_url": next_url,
        }, status_code=status)

    # Redirect zu next_url oder Default
    redirect_to = next_url if next_url else "/"
    # Sicherheitscheck: nur relative URLs erlauben (keine externen Redirects)
    if not redirect_to.startswith("/"):
        redirect_to = "/"

    response = RedirectResponse(url=redirect_to, status_code=303)
    response.set_cookie(
        key="access_token", value=token,
        httponly=not DEBUG, secure=not DEBUG, samesite="lax",
        max_age=8 * 3600, path="/",
    )
    return response


@app.get("/signup")
async def signup_page(request: Request, session: Session = Depends(get_session)):
    """Signup-Seite anzeigen. Bereits angemeldete User direkt zum Dashboard."""
    try:
        user = await get_current_user(request, session)
    except HTTPException:
        user = None
    if user:
        return RedirectResponse(url="/", status_code=302)

    next_url = request.query_params.get("next", "")
    return templates.TemplateResponse("signup.html", {"request": request, "next_url": next_url})


async def _render_signup_error(
    request: Request,
    error: str,
    next_url: str = "",
    username: str = "",
    name: str = "",
    email: str = "",
):
    """Signup-Seite mit Fehlermeldung neu rendern (eingetragene Werte bleiben erhalten)."""
    return templates.TemplateResponse("signup.html", {
        "request": request,
        "error": error,
        "next_url": next_url,
        "username": username,
        "name": name,
        "email": email,
    }, status_code=400)


@app.post("/signup")
async def signup_submit(
    request: Request,
    session: Session = Depends(get_session),
):
    """
    Self-Service-Registrierung für Studierende (nur Rolle USER, nie Admin).

    Akzeptiert beide Content-Types wie /login:
    - application/x-www-form-urlencoded (normales HTML-Formular)
    - application/json (HTMX, Fetch-API, etc.)

    Bei Erfolg: JWT in Cookie setzen und weiterleiten (Auto-Login).
    """
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body: dict = await request.json()
        username = str(body.get("username", "")).strip()
        name = str(body.get("name", "")).strip()
        email = str(body.get("email", "")).strip()
        password = str(body.get("password", ""))
        next_url = str(body.get("next_url", ""))
    else:
        form = await request.form()

        def _form_str(key: str) -> str:
            val = form.get(key, "")
            return str(val) if not hasattr(val, "read") else ""

        username = _form_str("username").strip()
        name = _form_str("name").strip()
        email = _form_str("email").strip()
        password = _form_str("password")
        next_url = _form_str("next_url")

    # ── Validierung ───────────────────────────────────────────
    if not username or not name or not email or not password:
        return await _render_signup_error(
            request, "Bitte alle Felder ausfüllen.", next_url, username, name, email)

    if len(password) < 6:
        return await _render_signup_error(
            request, "Das Password muss mindestens 6 Zeichen lang sein.",
            next_url, username, name, email)

    if "@" not in email:
        return await _render_signup_error(
            request, "Bitte eine gültige E-Mail-Adresse angeben.",
            next_url, username, name, email)

    # Username-Prüfung (case-insensitive, damit z. B. 'Max' und 'max' nicht kollidieren)
    existing = session.exec(
        select(User).where(func.lower(User.username) == username.lower())
    ).first()
    if existing:
        return await _render_signup_error(
            request, f"Username '{username}' ist bereits vergeben.",
            next_url, username, name, email)

    # ── User anlegen (immer Rolle USER, nie Admin) ───────────
    new_user = User(
        username=username,
        email=email,
        name=name,
        role=GlobalUserRole.USER,
        password_hash=hash_password(password),
    )
    session.add(new_user)
    session.commit()
    session.refresh(new_user)
    logger.info(f"[Signup] Neuer User '{username}' (ID {new_user.id}) registriert sich selbst.")

    # ── Auto-Login: JWT in Cookie setzen und weiterleiten ─────
    from services.auth_service import create_access_token
    token = create_access_token({"sub": new_user.id, "username": new_user.username})

    redirect_to = next_url if next_url else "/"
    # Sicherheitscheck: nur relative URLs erlauben (keine externen Redirects)
    if not redirect_to.startswith("/"):
        redirect_to = "/"

    response = RedirectResponse(url=redirect_to, status_code=303)
    response.set_cookie(
        key="access_token", value=token,
        httponly=not DEBUG, secure=not DEBUG, samesite="lax",
        max_age=8 * 3600, path="/",
    )
    return response


@app.get("/courses/{course_id}")
async def course_page(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kurs-Start: leitet zum Skript weiter (falls sichtbar), sonst zu den Aufgaben, sonst zum Forum."""
    membership, ctx = _course_tab_context(
        session, user, request, course_id, active_tab="tasks"
    )

    if not membership:
        raise HTTPException(403, "Du bist kein Mitglied dieses Kurses.")

    # Standard-Landing: Skript (wenn der Nutzer es sieht), sonst Aufgaben,
    # sonst Forum (verbleibender Tab, wenn auch keine sichtbaren Aufgaben da sind).
    if ctx["is_tutor"] or ctx["is_admin"] or ctx["has_visible_sections"]:
        return RedirectResponse(url=f"/courses/{course_id}/script", status_code=302)
    if ctx["has_visible_tasks"]:
        return RedirectResponse(url=f"/courses/{course_id}/tasks", status_code=302)
    return RedirectResponse(url=f"/courses/{course_id}/forum", status_code=302)


@app.get("/courses/{course_id}/tasks")
async def tasks_page(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kurs-Tab 'Aufgaben': Aufgaben-Liste (Student- bzw. Tutor-Ansicht)."""
    membership, ctx = _course_tab_context(
        session, user, request, course_id, active_tab="tasks"
    )

    if not membership:
        raise HTTPException(403, "Du bist kein Mitglied dieses Kurses.")

    # Tasks laden
    tasks = session.exec(
        select(Task)
        .where(Task.course_id == course_id)
        .order_by(Task.display_order.asc())  # type: ignore[attr-defined]
    ).all()

    # Template je nach Rolle
    is_tutor = membership.role_in_course in (CourseRole.PROF, CourseRole.TUTOR)
    template = "course/tasks_tutor.html" if is_tutor else "course/tasks_student.html"

    # Für Studenten: nur sichtbare Aufgaben, my_points und has_feedback pro Aufgabe berechnen
    if not is_tutor:
        visible_tasks = [t for t in tasks if t.is_visible]
        if not visible_tasks:
            raise HTTPException(404, "Keine Aufgaben für diesen Kurs vorhanden.")
        task_list = []
        for t in visible_tasks:
            subs = session.exec(
                select(Submission)
                .where(Submission.task_id == t.id)
                .where(Submission.student_id == user.id)
                .order_by(Submission.submitted_at.desc())  # type: ignore[attr-defined]
            ).all()

            human_points = 0.0
            llm_points = 0.0
            override_exists = False
            has_feedback = False
            if subs:
                latest_sub = subs[0]  # newest
                for fb in latest_sub.feedback_list:
                    has_feedback = True
                    if fb.source == FeedbackSource.HUMAN:
                        human_points = max(human_points, fb.points_earned)
                        override_exists = True
                    else:
                        llm_points = max(llm_points, fb.points_earned)
            my_points = human_points if override_exists else llm_points

            task_list.append({
                "id": t.id,
                "title": t.title,
                "task_type": t.task_type.value,
                "max_points": t.max_points,
                "max_attempts": t.max_attempts,
                "attempts_used": len(subs),
                "deadline": t.deadline,
                "my_points": my_points,
                "has_feedback": has_feedback,
                "display_order": t.display_order,
            })
    else:
        task_list = [
            {
                "id": t.id,
                "title": t.title,
                "task_type": t.task_type.value,
                "max_points": t.max_points,
                "max_attempts": t.max_attempts,
                "deadline": t.deadline,
                "my_points": 0,
                "has_feedback": False,
                "is_visible": t.is_visible,
                "display_order": t.display_order,
            }
            for t in tasks
        ]

    ctx["tasks"] = task_list
    return templates.TemplateResponse(template, ctx)


def _render_script_page(session, user, request, course_id, open_section_id=None):
    """Geteiltes Rendering des Skript-Tabs (auch für Deep-Links zu einzelnen Kapiteln).

    open_section_id: Deep-Link /script/{section_id} → Template klafft das
    Kapitel client-seitig auf (SQ_OPEN_SECTION in script.html).
    """
    membership, ctx = _course_tab_context(session, user, request, course_id, active_tab="script")
    if not membership and user.role != GlobalUserRole.ADMIN:
        raise HTTPException(403, "Du bist kein Mitglied dieses Kurses.")

    is_tutor = ctx["is_tutor"] or ctx["is_admin"]
    q = select(ScriptSection).where(ScriptSection.course_id == course_id)
    if not is_tutor:
        q = q.where(ScriptSection.is_visible == True)  # noqa: E712
    sections = session.exec(q.order_by(ScriptSection.display_order.asc())).all()  # type: ignore[attr-defined]
    if not is_tutor and not sections:
        raise HTTPException(404, "Kein Skript für diesen Kurs vorhanden.")

    ctx["sections"] = [
        {
            "id": s.id,
            "title": s.title,
            "is_visible": s.is_visible,
            "display_order": s.display_order,
            "updated_at": timeutil.utc_iso(s.updated_at),
        }
        for s in sections
    ]
    ctx["page_title"] = f"Skript — {ctx['course']['name']}"
    ctx["code_editor"] = True  # CodeMirror (Markdown-Mode) für die Kapitel-Editoren
    ctx["open_section_id"] = open_section_id
    return templates.TemplateResponse("course/script.html", ctx)


@app.get("/courses/{course_id}/script")
async def script_page(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kurs-Tab 'Skript': mehrere Markdown-Kapitel (gemeinsame Ansicht; Tutor/PROF mit Bearbeitung)."""
    return _render_script_page(session, user, request, course_id)


@app.get("/courses/{course_id}/script/{section_id}")
async def script_section_page(
    course_id: int,
    section_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Deep-Link zu einem Skript-Kapitel: rendert den Skript-Tab und klafft das
    Kapitel per #chapter-Hash auf (Tracking: page_key 'script_section')."""
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")
    section = session.get(ScriptSection, section_id)
    if not section or section.course_id != course_id:
        raise HTTPException(404, "Kapitel nicht gefunden.")
    return _render_script_page(session, user, request, course_id, open_section_id=section_id)


@app.get("/courses/{course_id}/script/{section_id}/print")
async def script_section_print_page(
    course_id: int,
    section_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """PDF-Export eines Skript-Kapitels: eigenständige Print-Seite (1:1
    Rendering wie im Skript, Applets vor dem Export interaktiv einstellbar)."""
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")
    section = session.get(ScriptSection, section_id)
    if not section or section.course_id != course_id:
        raise HTTPException(404, "Kapitel nicht gefunden.")

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()
    is_admin = user.role == GlobalUserRole.ADMIN
    if not membership and not is_admin:
        raise HTTPException(403, "Du bist kein Mitglied dieses Kurses.")
    role = membership.role_in_course if membership else None
    is_tutor = is_admin or role in (CourseRole.PROF, CourseRole.TUTOR)
    if not section.is_visible and not is_tutor:
        raise HTTPException(404, "Kapitel nicht gefunden.")

    # Kapitelnummer (wie script-refmap: rollenabhängige Sichtbarkeit +
    # display_order) — für den Laufkopf im Druck (Margin-Box, serverseitig)
    q = select(ScriptSection).where(ScriptSection.course_id == course_id)
    if not is_tutor:
        q = q.where(ScriptSection.is_visible == True)
    sections = session.exec(q.order_by(ScriptSection.display_order.asc())).all()  # type: ignore[attr-defined]
    chapter_num = next((i for i, s in enumerate(sections, 1) if s.id == section_id), None)

    # Dateiname für den PDF-Speicherdialog (Browser = document.title,
    # wird im Template gesetzt): „Kursname - KapitelNr - Kapiteltitel"
    pdf_name_parts = [course.name]
    if chapter_num:
        pdf_name_parts.append(str(chapter_num))
    pdf_name_parts.append(section.title)

    # Volleigenständige Seite (ohne Kurs-Tab-Leiste): Kontext manuell bauen
    ctx = {
        "request": request,
        "page_title": " - ".join(pdf_name_parts),
        "current_user": _user_ctx(user, role.value if role else "ADMIN"),
        "courses": _get_user_courses(user, session),
        "selected_course_id": course_id,
        "is_admin": is_admin,
        "course": {
            "id": course.id,
            "name": course.name,
            "description": course.description,
            "semester": course.semester,
        },
        "section": {
            "id": section.id,
            "title": section.title,
            # Klartext für den Laufkopf (Margin-Box rendert keinen HTML-Math)
            "header": f"Kap. {chapter_num} · {section.title}" if chapter_num else section.title,
        },
        "chapter_num": chapter_num,
        "is_tutor": is_tutor,
    }
    return templates.TemplateResponse("course/script_print.html", ctx)


@app.post("/api/analytics/script-section")
async def track_script_section_view(
    course_id: int,
    section_id: int,
    user: User = Depends(get_current_user),
):
    """Beacon (sendBeacon): Skript-Kapitel wurde geöffnet — der Inhalt wird
    clientseitig lazy geladen, ohne diesen Call wäre das Tracking blind.

    Zählt als Page-View 'script_section' mit dem Deep-Link-Pfad; serverseitiges
    Dedupe (30 s, gleicher Pfad) verhindert Doppelzählung bei Deep-Link-Laden.
    """
    analytics.record_page_view(user.id, f"/courses/{course_id}/script/{section_id}")
    return {"ok": True}


def _slides_theme_ctx(session: Session, course_id: int) -> dict:
    """Aufgelöstes Folien-Theme für Template-Kontexte (Folien-Seiten).

    Liefert:
      slides_theme        – aufgelöstes Theme-JSON (für JS/Modals)
      slides_theme_css    – CSS-Variablen als Deklarationen (für <style>:root{…})
      slides_theme_templates – Template-Defaults (für Design-Modal)
      slides_aspect_ratios  – Seitenverhältnis-Map Key→Wert (für Design-Modal/JS)
      slides_aspect_labels  – Seitenverhältnis-Labels (für Design-Modal)
      slides_footer_text  – Fußzeilen-Text („Kurs — Semester")
    """
    row = session.get(CourseSlidesTheme, course_id)
    theme = resolve_theme(row.theme if row else None)
    course = session.get(Course, course_id)

    logo_url = None
    if theme["logo_media_id"]:
        media = session.get(CourseMedia, theme["logo_media_id"])
        if media and media.course_id == course_id and media.media_type == "image":
            logo_url = media_service.media_url(media)

    colors = theme["colors"]
    css = (
        f"--slides-primary: {colors['primary']}; "
        f"--slides-accent: {colors['accent']}; "
        f"--slides-bg: {colors['background']}; "
        f"--slides-text: {colors['text']}; "
        f"--slides-font-scale: {theme['font_scale']}; "
        f"--slides-padding-scale: {theme['padding_scale']}; "
        f"--slides-aspect: {theme['aspect_value']:.4f}; "
        f"--slides-logo-scale: {theme['logo_scale']}"
    )
    if logo_url:
        css += f"; --slides-logo: url(\"{logo_url}\")"

    return {
        "slides_theme": theme,
        "slides_theme_css": css,
        "slides_theme_templates": THEME_TEMPLATES,
        "slides_aspect_ratios": ASPECT_RATIOS,
        "slides_aspect_labels": ASPECT_LABELS,
        "slides_footer_text": f"{course.name} — {course.semester}" if course else "",
        "slides_has_logo": logo_url is not None,
    }


@app.get("/courses/{course_id}/slides")
async def slides_page(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kurs-Tab 'Folien': Slide-Decks als Kacheln (Anlegen, Präsentieren, PDF, Design)."""
    membership, ctx = _course_tab_context(session, user, request, course_id, active_tab="slides")
    if not membership and user.role != GlobalUserRole.ADMIN:
        raise HTTPException(403, "Du bist kein Mitglied dieses Kurses.")

    is_tutor = ctx["is_tutor"] or ctx["is_admin"]
    decks = session.exec(
        select(CourseMaterial)
        .where(CourseMaterial.course_id == course_id)
        .where(CourseMaterial.material_type == MaterialType.SLIDES)
        .order_by(CourseMaterial.display_order.asc(), CourseMaterial.id.asc())  # type: ignore[attr-defined]
    ).all()
    if not is_tutor:
        decks = [d for d in decks if d.is_visible]

    ctx["decks"] = [
        {
            "id": d.id,
            "title": d.title,
            "is_visible": d.is_visible,
            "slide_count": slide_count(d.content),
            # ISO-UTC (…Z): Template gibt es per [data-ts] aus, JS rechnet
            # client-seitig in lokale Zeit um. (Datetime hier wäre für das
            # `decks | tojson` im extra_Scripts-Block nicht serialisierbar.)
            "updated_at": timeutil.utc_iso(d.updated_at),
        }
        for d in decks
    ]
    ctx.update(_slides_theme_ctx(session, course_id))
    ctx["page_title"] = f"Folien — {ctx['course']['name']}"
    return templates.TemplateResponse("course/slides.html", ctx)


@app.get("/courses/{course_id}/slides/{deck_id}")
async def slides_deck_page(
    course_id: int,
    deck_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Slide-Deck-Ansicht (Vorschau aller Folien + Markdown-Editor)."""
    membership, ctx = _course_tab_context(session, user, request, course_id, active_tab="slides")
    if not membership and user.role != GlobalUserRole.ADMIN:
        raise HTTPException(403, "Du bist kein Mitglied dieses Kurses.")

    deck = session.get(CourseMaterial, deck_id)
    if not deck or deck.course_id != course_id or deck.material_type != MaterialType.SLIDES:
        raise HTTPException(404, "Slide-Deck nicht gefunden.")
    is_tutor = ctx["is_tutor"] or ctx["is_admin"]
    if not deck.is_visible and not is_tutor:
        raise HTTPException(404, "Slide-Deck nicht gefunden.")
    # Studenten brauchen den Editor nicht → direkt in die Präsentation.
    if not is_tutor:
        return RedirectResponse(f"/courses/{course_id}/slides/{deck_id}/present")

    ctx["material"] = {
        "id": deck.id,
        "title": deck.title,
        "material_type": deck.material_type.value,
        "is_visible": deck.is_visible,
    }
    ctx["material_kind"] = MaterialType.SLIDES.value
    ctx["material_label"] = "Folien"
    ctx.update(_slides_theme_ctx(session, course_id))
    ctx["page_title"] = f"{deck.title} — {ctx['course']['name']}"
    ctx["code_editor"] = True  # CodeMirror (Markdown-Mode) für den Folien-Editor
    return templates.TemplateResponse("course/slides_edit.html", ctx)


@app.get("/courses/{course_id}/slides/{deck_id}/present")
async def slides_present_page(
    course_id: int,
    deck_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Präsentation (Reveal.js) / PDF-Export (mit ?print-pdf)."""
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")
    deck = session.get(CourseMaterial, deck_id)
    if not deck or deck.course_id != course_id or deck.material_type != MaterialType.SLIDES:
        raise HTTPException(404, "Slide-Deck nicht gefunden.")

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()
    is_admin = user.role == GlobalUserRole.ADMIN
    if not membership and not is_admin:
        raise HTTPException(403, "Du bist kein Mitglied dieses Kurses.")
    role = membership.role_in_course if membership else None
    is_tutor = is_admin or role in (CourseRole.PROF, CourseRole.TUTOR)
    if not deck.is_visible and not is_tutor:
        raise HTTPException(404, "Slide-Deck nicht gefunden.")

    # Volleigenständige Seite (ohne Kurs-Tab-Leiste): Kontext manuell bauen
    ctx = {
        "request": request,
        "page_title": f"{deck.title} — Präsentation",
        "current_user": _user_ctx(user, role.value if role else "ADMIN"),
        "courses": _get_user_courses(user, session),
        "selected_course_id": course_id,
        "is_admin": is_admin,
        "course": {
            "id": course.id,
            "name": course.name,
            "description": course.description,
            "semester": course.semester,
        },
        "deck": {
            "id": deck.id,
            "title": deck.title,
            "is_visible": deck.is_visible,
        },
        "is_tutor": is_tutor,
    }
    ctx.update(_slides_theme_ctx(session, course_id))
    return templates.TemplateResponse("course/slides_present.html", ctx)


@app.get("/courses/{course_id}/media")
async def media_page(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kurs-Tab 'Medien': Medienbibliothek (nur PROF/Admin)."""
    membership, ctx = _course_tab_context(session, user, request, course_id, active_tab="media")
    if not (ctx["is_prof"] or ctx["is_admin"]):
        raise HTTPException(403, "Nur PROFs und Administratoren dürfen die Medien verwalten.")
    ctx["page_title"] = f"Medien — {ctx['course']['name']}"
    return templates.TemplateResponse("course/media.html", ctx)


@app.get("/courses/{course_id}/references")
async def references_page(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kurs-Tab 'Quellen': Quellenbibliothek (PROF/TUTOR/Admin)."""
    membership, ctx = _course_tab_context(session, user, request, course_id, active_tab="references")
    if not (ctx["is_tutor"] or ctx["is_admin"]):
        raise HTTPException(403, "Nur Tutoren, PROFs und Administratoren dürfen die Quellen verwalten.")
    ctx["page_title"] = f"Quellen — {ctx['course']['name']}"
    return templates.TemplateResponse("course/references.html", ctx)


@app.get("/courses/{course_id}/applets/new")
async def applet_new_page(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Applet-Studio: neues Applet generieren (nur PROF/Admin)."""
    membership, ctx = _course_tab_context(session, user, request, course_id, active_tab="media")
    if not (ctx["is_prof"] or ctx["is_admin"]):
        raise HTTPException(403, "Nur PROFs und Administratoren dürfen Medien verwalten.")
    ctx["page_title"] = f"Applet erstellen — {ctx['course']['name']}"
    ctx["media"] = None
    ctx["code_editor"] = True
    return templates.TemplateResponse("course/applet_studio.html", ctx)


@app.get("/courses/{course_id}/applets/{media_id}")
async def applet_edit_page(
    course_id: int,
    media_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Applet-Studio: Applet bearbeiten (nur PROF/Admin)."""
    membership, ctx = _course_tab_context(session, user, request, course_id, active_tab="media")
    if not (ctx["is_prof"] or ctx["is_admin"]):
        raise HTTPException(403, "Nur PROFs und Administratoren dürfen Medien verwalten.")
    media = session.get(CourseMedia, media_id)
    if not media or media.course_id != course_id or media.media_type != "applet":
        raise HTTPException(404, "Applet nicht gefunden.")
    ctx["page_title"] = f"Applet bearbeiten — {ctx['course']['name']}"
    ctx["media"] = media
    ctx["code_editor"] = True
    return templates.TemplateResponse("course/applet_studio.html", ctx)


_APPLET_HEIGHT_SCRIPT = (
    "\n<script>\n"
    "(function () {\n"
    "  function report() {\n"
    '    try { parent.postMessage({ source: "aicampus-applet", height: document.body.scrollHeight }, "*"); } catch (e) {}\n'
    "  }\n"
    '  window.addEventListener("load", report);\n'
    '  window.addEventListener("resize", report);\n'
    "  if (window.ResizeObserver) { new ResizeObserver(report).observe(document.body); }\n"
    "  report();\n"
    "  // PDF-Capture (Kapitel-PDF-Export, script_print.html): Das Parent bittet\n"
    '  // per postMessage { source: "aicampus-capture-request", id } um einen\n'
    "  // Screenshot des AKTUELLEN Applet-Zustands (Canvas-Applets: toDataURL,\n"
    "  // DOM-Applets: html2canvas) und erhält die Antwort\n"
    '  // { source: "aicampus-capture-result", id, dataUrl } (null = Fehler).\n'
    '  window.addEventListener("message", function (e) {\n'
    "    var d = e.data;\n"
    '    if (!d || d.source !== "aicampus-capture-request" || !d.id) return;\n'
    '    // resize: statische WebGL-Applets rendern (falls überhaupt) meist nur neu auf resize\n'
    '    try { window.dispatchEvent(new Event("resize")); } catch (x) {}\n'
    "    requestAnimationFrame(function () {\n"
    "      var dataUrl = null;\n"
    "      try {\n"
    '        var cs = document.querySelectorAll("canvas");\n'
    "        if (cs.length) {\n"
    "          var best = null, area = 0;\n"
    "          for (var i = 0; i < cs.length; i++) {\n"
    "            var a = cs[i].width * cs[i].height;\n"
    "            if (a > area) { area = a; best = cs[i]; }\n"
    "          }\n"
    '          if (best) dataUrl = best.toDataURL("image/png");\n'
    "        }\n"
    "      } catch (err) { dataUrl = null; }\n"
    "      function done() {\n"
    '        try { parent.postMessage({ source: "aicampus-capture-result", id: d.id, dataUrl: dataUrl || null }, "*"); } catch (x) {}\n'
    "      }\n"
    "      if (dataUrl) { done(); return; }\n"
    '      var s = document.createElement("script");\n'
    '      s.src = "/static/vendor/html2canvas.min.js";\n'
    "      s.onload = function () {\n"
    "        try {\n"
    '          window.html2canvas(document.body, { useCORS: true, backgroundColor: "#ffffff" }).then(function (c) {\n'
    '            try { dataUrl = c.toDataURL("image/png"); } catch (t) { dataUrl = null; }\n'
    "            done();\n"
    "          }).catch(done);\n"
    "        } catch (x) { done(); }\n"
    "      };\n"
    "      s.onerror = done;\n"
    "      document.body.appendChild(s);\n"
    "    });\n"
    "  });\n"
    "})();\n"
    "</script>\n"
)


def _with_applet_height_script(html: str) -> str:
    """Applet-HTML um ein Auto-Size-Boilerplate erweitern (idempotent)."""
    if "aicampus-applet" in html:
        return html  # Boilerplate bereits vorhanden
    m = re.search(r"</body\s*>", html, re.IGNORECASE)
    if m:
        return html[: m.start()] + _APPLET_HEIGHT_SCRIPT + html[m.start():]
    return html + _APPLET_HEIGHT_SCRIPT


@app.get("/media/{course_id}/{filename}")
async def serve_media(
    course_id: int,
    filename: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Authentifizierter Medien-Versand (kein Static-Mount!).

    Zugriff für alle Kurs-Mitglieder. Wer ein Medium sehen darf, steuert
    die Sichtbarkeit des einbindenden Inhalts (Aufgabe/Skript/Slides).
    """
    if not re.fullmatch(r"[A-Za-z0-9._\-]+", filename):
        raise HTTPException(404, "Medium nicht gefunden.")

    media = session.exec(
        select(CourseMedia).where(CourseMedia.file_path == f"course_{course_id}/{filename}")
    ).first()
    if not media:
        raise HTTPException(404, "Medium nicht gefunden.")

    if user.role != GlobalUserRole.ADMIN:
        membership = session.exec(
            select(UserCourse)
            .where(UserCourse.user_id == user.id)
            .where(UserCourse.course_id == course_id)
        ).first()
        if not membership:
            raise HTTPException(403, "Du bist kein Mitglied dieses Kurses.")

    path = media_service.resolve_media_path(course_id, filename)
    if not path:
        raise HTTPException(404, "Datei nicht gefunden.")
    # Applets werden unter derselben URL in-place ersetzt (Studio/MCP/Versions-
    # Restore) → no-store: Der Browser-Memory-Cache würde bei Live-Re-Render
    # stummen Stale-Content liefern (Chromium revalidiert no-cache-Einträge
    # für Bilder im Memory-Cache nicht). Bilder/Video: Datei pro Pfad bleibt
    # unverändert (Re-Upload = neue UUID) → no-cache reicht.
    headers = {
        "Cache-Control": "no-store" if media.media_type == "applet" else "no-cache"
    }
    if media.media_type == "applet" and not filename.lower().endswith(".svg"):
        # HTML-Applets explizit als HTML markieren — kein MIME-Guessing.
        # Auto-Size-Boilerplate wird serverseitig injiziert (wirkt auch für manuelle Edits).
        headers["X-Content-Type-Options"] = "nosniff"
        raw = path.read_text(encoding="utf-8")
        return HTMLResponse(content=_with_applet_height_script(raw), headers=headers)
    return FileResponse(path, media_type=media.mime_type, headers=headers)


@app.get("/avatars/{filename}")
async def serve_avatar(
    filename: str,
    user: User = Depends(get_current_user),
):
    """Profilbild-Versand: für alle eingeloggten User (nicht kurs-spezifisch)."""
    if not re.fullmatch(r"[A-Za-z0-9._\-]+", filename):
        raise HTTPException(404, "Avatar nicht gefunden.")

    path = media_service.resolve_avatar_path(filename)
    if not path:
        raise HTTPException(404, "Avatar nicht gefunden.")

    mime = media_service.ALLOWED_MEDIA.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "no-cache"})


@app.get("/courses/{course_id}/forum")
async def forum_page(
    course_id: int,
    request: Request,
    channel: Optional[int] = Query(None, description="Ausgewählter Forum-Kanal"),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kurs-Tab 'Forum': Chat in Kanälen für alle Kurs-Mitglieder (Student/Tutor/PROF)."""
    membership, ctx = _course_tab_context(
        session, user, request, course_id, active_tab="forum"
    )

    if not membership and user.role != GlobalUserRole.ADMIN:
        raise HTTPException(403, "Du bist kein Mitglied dieses Kurses.")

    channels = forum.load_channels_payload(
        session, course_id, user, ctx["current_user"]["role"]
    )
    active = next((c for c in channels if c["id"] == channel), None) if channel is not None else None
    channel_selected = active is not None
    if active is None:
        # Kein (gültiger) Kanal gewählt: Desktop zeigt den ersten Kanal,
        # die schmale (mobile) Ansicht nur die Kanal-Liste (CSS im Template).
        active = channels[0] if channels else None

    ctx["page_title"] = f"Forum — {ctx['course']['name']}"
    ctx["forum_channels"] = channels
    ctx["active_channel"] = active
    ctx["channel_selected"] = channel_selected
    ctx["forum_messages"] = (
        forum.load_forum_payload(
            session, course_id, active["id"], user, ctx["current_user"]["role"], None
        )
        if active
        else []
    )
    return templates.TemplateResponse("course/forum.html", ctx)


@app.get("/courses/{course_id}/members")
async def members_page(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kurs-Tab 'Mitglieder': Für PROFs und Admins."""
    membership, ctx = _course_tab_context(
        session, user, request, course_id, active_tab="members"
    )
    ctx["page_title"] = f"Mitglieder — {ctx['course']['name']}"

    is_admin = user.role == GlobalUserRole.ADMIN

    # Admin: darf alles — auch ohne Membership
    if not is_admin and (not membership or membership.role_in_course != CourseRole.PROF):
        raise HTTPException(403, "Nur PROFs und Administratoren können die Mitglieder verwalten.")

    # Alle Mitglieder mit User-Info laden
    user_courses = session.exec(
        select(UserCourse).where(UserCourse.course_id == course_id)
    ).all()

    members = [
        {
            "id": uc.id,
            "user_id": uc.user_id,
            "username": uc.user.username if uc.user else "unknown",
            "name": uc.user.name if uc.user else "unknown",
            "avatar": f"/avatars/{uc.user.avatar.rsplit('/', 1)[-1]}" if uc.user and uc.user.avatar else None,
            "role_in_course": uc.role_in_course.value,
            "annotation": uc.annotation,
        }
        for uc in user_courses
    ]

    ctx["members"] = members
    return templates.TemplateResponse("course/members.html", ctx)


@app.get("/courses/{course_id}/settings")
async def course_settings_page(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kurs-Tab 'Einstellungen': Kurs-Grunddaten bearbeiten (nur PROF/Admin)."""
    membership, ctx = _course_tab_context(
        session, user, request, course_id, active_tab="settings"
    )
    if not (ctx["is_prof"] or ctx["is_admin"]):
        raise HTTPException(403, "Nur PROFs und Administratoren dürfen die Einstellungen bearbeiten.")

    # Papierkorb: soft gelöschte Kurs-Inhalte (Bypass, damit die Filter sie sehen)
    trash = []
    trash_queries = [
        ("task", Task, "Aufgabe"),
        ("material", CourseMaterial, "Slides"),
        ("section", ScriptSection, "Kapitel"),
        ("media", CourseMedia, "Medium"),
    ]
    for kind, model, label in trash_queries:
        for row in session.exec(
            select(model)
            .where(model.course_id == course_id)
            .where(model.deleted_at.is_not(None))
            .execution_options(**SOFT_DELETE_BYPASS)
        ).all():
            trash.append(
                {"kind": kind, "id": row.id, "title": row.title, "label": label,
                 "deleted_at": row.deleted_at}
            )
    ctx["trash"] = trash

    ctx["page_title"] = f"Einstellungen — {ctx['course']['name']}"
    return templates.TemplateResponse("course/settings.html", ctx)


@app.post("/api/courses/{course_id}/trash/{kind}/{entity_id}")
async def trash_restore(
    course_id: int,
    kind: str,
    entity_id: int,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF)),
):
    """Soft gelöschtes Kurs-Item aus dem Papierkorb wiederherstellen (PROF/Admin)."""
    kind_model = {
        "task": Task,
        "material": CourseMaterial,
        "section": ScriptSection,
        "media": CourseMedia,
    }
    if kind not in kind_model:
        raise HTTPException(404, "Unbekannter Papierkorb-Typ.")
    model = kind_model[kind]
    entity = session.get(model, entity_id, execution_options=SOFT_DELETE_BYPASS)
    if entity is None or entity.course_id != course_id:
        raise HTTPException(404, "Element nicht gefunden.")
    if entity.deleted_at is None:
        raise HTTPException(409, "Element ist nicht im Papierkorb.")

    trash_service.restore(session, entity)
    # Usages aus dem Markdown-Scan neu aufbauen (gelöschte Usages tauchen wieder auf)
    sync_media_usages(session, course_id)
    if kind == "media":
        # Live-Update: offene Medien-Listen/Studio-Tabs zeigen die Kachel wieder.
        notifications.push_content_updated(
            session, course_id, "applet" if entity.media_type == "applet" else "media", entity.id
        )
    return {"message": f"{kind.title()} wiederhergestellt.", "kind": kind, "id": entity.id}


@app.get("/join/{token}")
async def join_page(
    token: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """
    Einladungsseite — Student tritt dem Kurs bei.

    Bei Klick auf den Einladungslink landet der Student hier.
    - Ist er bereits angemeldet → direkt beitreten.
    - Ist er nicht angemeldet → Redirect zum Login mit Next-URL.
    """
    invite = session.exec(
        select(CourseInvite).where(CourseInvite.token == token)
    ).first()

    if not invite:
        raise HTTPException(404, "Einladungslink ungültig oder nicht mehr vorhanden.")

    # Ablauf prüfen
    if invite.expires_at and invite.expires_at < datetime.now():
        raise HTTPException(410, "Dieser Einladungslink ist abgelaufen.")

    # Max-Nutzungen prüfen
    if invite.max_uses is not None and invite.used_count >= invite.max_uses:
        raise HTTPException(410, "Dieser Einladungslink wurde bereits maximal genutzt.")

    course = session.get(Course, invite.course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    user = None
    try:
        user = await get_current_user(request, session)
    except HTTPException:
        pass

    if not user:
        # Nicht eingeloggt → Redirect zum Login mit Next-URL
        next_url = f"/join/{token}"
        redirect = RedirectResponse(url=f"/login?next={next_url}", status_code=302)
        # Wir speichern eine kleine Message im Session-Flash (hier via Query-Param)
        return redirect

    assert user.id is not None, "User-ID ist None"
    user_id = user.id

    # Bereits im Kurs?
    existing = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user_id)
        .where(UserCourse.course_id == invite.course_id)
    ).first()

    if existing:
        return templates.TemplateResponse(
            "join.html",
            {
                "request": request,
                "page_title": f"Kurs beitreten — {course.name}",
                "current_user": _user_ctx(user, existing.role_in_course.value),
                "courses": _get_user_courses(user, session),
                "selected_course_id": invite.course_id,
                "course": {
                    "id": course.id,
                    "name": course.name,
                    "description": course.description,
                    "semester": course.semester,
                },
                "already_member": True,
            },
        )

    # Student dem Kurs hinzufügen
    membership = UserCourse(
        user_id=user_id,
        course_id=invite.course_id,
        role_in_course=CourseRole.STUDENT,
    )
    session.add(membership)

    invite.used_count += 1
    session.add(invite)
    session.commit()

    return templates.TemplateResponse(
        "join.html",
        {
            "request": request,
            "page_title": f"Kurs beitreten — {course.name}",
            "current_user": _user_ctx(user, "STUDENT"),
            "courses": _get_user_courses(user, session),
            "selected_course_id": invite.course_id,
            "course": {
                "id": course.id,
                "name": course.name,
                "description": course.description,
                "semester": course.semester,
            },
            "already_member": False,
        },
    )


def _mc_context(task, is_tutor: bool, user_id: Optional[int] = None):
    """mc_data für den Template-Kontext (None bei Nicht-MC-Aufgaben).

    Studenten sehen NUR die Options-Texte (ggf. geshuffelt, Seed aus
    User-ID) — Correct-Flags und Feedback dürfen nicht im DevTools
    erkennbar sein.
    """
    if task.task_type.value != "mc" or not task.mc_data:
        return None
    try:
        data = parse_mc_data(task.mc_data)
    except McValidationError as e:
        logger.warning("mc_data von Task %s ist ungültig: %s", task.id, e)
        return None
    if is_tutor:
        return _mc_tutor_view(data)
    return _mc_student_view(data, task_id=task.id, user_id=user_id)


def _pick_task_template(candidates: list[str]) -> str:
    """Wählt das erste vorhandene Template aus candidates (Fallback: letzter Eintrag).

    Ermöglicht typspezifische Templates (z. B. task_detail_code.html,
    task_solve_workspace.html) mit Fallback auf die Base-Vorlage,
    ohne dass fehlende Dateien einen 500-Fehler verursachen.
    """
    for name in candidates:
        try:
            templates.env.get_template(name)
            return name
        except TemplateNotFound:
            continue
    return candidates[-1]


@app.get("/courses/{course_id}/tasks/new")
async def new_task_page(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user_and_course: tuple[User, int] = Depends(require_course_access(CourseRole.PROF, CourseRole.TUTOR)),
):
    """Neue Aufgabe erstellen."""
    user, _ = user_and_course
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    courses = _get_user_courses(user, session)

    # Rolle im Kurs ermitteln
    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()
    course_role = membership.role_in_course.value if membership else "TUTOR"

    # Global-Admin zeigt als PROF im Kurs-Kontext
    if user.role == GlobalUserRole.ADMIN:
        course_role = "PROF"

    # Typ des Templates: Default „text“, per Query-Param umschaltbar (z. B. ?task_type=mc).
    # Workspace ist immer wählbar — ohne erreichbare Engine degradiert die View sauber.
    allowed_types = ("text", "workspace", "mc", "upload", "extern")
    tpl_type = request.query_params.get("task_type", "text")
    if tpl_type not in allowed_types:
        tpl_type = "text"
    template = _pick_task_template([f"tutor/task_detail_{tpl_type}.html", "tutor/task_detail_base.html"])

    # Prev-Link schon auf der New-Seite zeigen: Die neue Aufgabe landet
    # am Ende der Liste (display_order = max + 1, s. create_task) →
    # „Vorherige“ ist die aktuell letzte Aufgabe (TUT/PROF sieht auch
    # versteckte, wie in task_page). Ohne den Link springt das Layout
    # erst nach Anlage + Reload.
    prev_task = session.exec(
        select(Task)
        .where(Task.course_id == course_id)
        .order_by(Task.display_order.desc())  # type: ignore[attr-defined]
    ).first()

    # Kurs-Tab-Leiste: „Aufgaben" ist aktiv
    _tab_membership, tab_ctx = _course_tab_context(
        session, user, request, course_id, active_tab="tasks"
    )

    return templates.TemplateResponse(
        template,
        {
            "request": request,
            "page_title": "Neue Aufgabe",
            "tabs": tab_ctx["tabs"],
            "current_user": _user_ctx(user, course_role),
            "courses": courses,
            "selected_course_id": course_id,
            "is_admin": user.role == GlobalUserRole.ADMIN,
            "course": {
                "id": course.id,
                "name": course.name,
                "semester": course.semester,
                "description": course.description,
            },
            "task": None,
            "prev_task": {"id": prev_task.id, "title": prev_task.title} if prev_task else None,
            "next_task": None,
            "tpl_type": tpl_type,
            "is_tutor": True,
            "code_editor": True,  # CodeMirror (Markdown-Mode) für Aufgabenstellung/Musterlösung
            "LLM_TIMEOUT": LLM_TIMEOUT,
        },
    )


@app.get("/courses/{course_id}/tasks/{task_id}")
async def task_page(
    course_id: int,
    task_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Einzelne Aufgabe: Bearbeiten (Student) oder erstellen (Tutor)."""
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Aufgabe nicht gefunden.")

    if task.course_id != course_id:
        raise HTTPException(404, "Aufgabe nicht gefunden.")

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()

    if not membership:
        raise HTTPException(403, "Kein Zugriff.")

    is_tutor = membership.role_in_course in (CourseRole.PROF, CourseRole.TUTOR)

    # Studenten duerfen versteckte Aufgaben nicht sehen
    if not is_tutor and not task.is_visible:
        raise HTTPException(404, "Aufgabe nicht gefunden oder noch nicht freigeschaltet.")

    # Tutoren koennen mit ?as_student=1 die Aufgabe aus Studentensicht sehen
    is_student_view = is_tutor and request.query_params.get("as_student") in ("1", "true")

    # Kurs-Tab-Leiste: „Aufgaben" ist aktiv. Die Tabs zeigen immer die
    # eigentliche Rolle des Nutzers — der as_student-Preview betrifft nur
    # den Inhalt der Aufgaben-Seite, nicht die Navigation.
    _tab_membership, tab_ctx = _course_tab_context(
        session, user, request, course_id, active_tab="tasks",
    )

    is_workspace = task.task_type.value == "workspace"

    # Tutoren in Student-View: Zeige alle Aufgaben (auch versteckte) bei Prev/Next
    show_hidden = is_tutor  # egal ob as_student oder nicht

    # Typspezifisches Template (task_detail_{type}.html / task_solve_{type}.html)
    # mit Fallback auf die Base-Vorlage. Tutoren können per ?task_type=
    # umschalten (z. B. Text-Aufgabe im MC-Template öffnen, um den Typ zu ändern).
    if is_tutor and not is_student_view:
        tpl_type = task.task_type.value
        override = request.query_params.get("task_type")
        if override in ("text", "workspace", "mc", "upload", "extern"):
            tpl_type = override
        template = _pick_task_template([f"tutor/task_detail_{tpl_type}.html", "tutor/task_detail_base.html"])
    else:
        tpl_type = task.task_type.value
        template = _pick_task_template([f"student/task_solve_{tpl_type}.html", "student/task_solve_base.html"])

    courses = _get_user_courses(user, session)

    # Find previous and next task in the same course
    # Studenten: nur sichtbar e Aufgaben, TUTs/PROFs: alle Aufgaben
    prev_query = (
        select(Task)
        .where(Task.course_id == course_id)
        .where(Task.display_order < task.display_order)  # type: ignore[operator]
        .order_by(Task.display_order.desc())  # type: ignore[attr-defined]
    )
    if not show_hidden:
        prev_query = prev_query.where(Task.is_visible == True)  # type: ignore[operator]
    prev_task = session.exec(prev_query).first()

    # Studenten: nur sichtbar e Aufgaben, TUTs/PROFs: alle Aufgaben
    next_query = (
        select(Task)
        .where(Task.course_id == course_id)
        .where(Task.display_order > task.display_order)  # type: ignore[operator]
        .order_by(Task.display_order.asc())  # type: ignore[attr-defined]
    )
    if not show_hidden:
        next_query = next_query.where(Task.is_visible == True)  # type: ignore[operator]
    next_task = session.exec(next_query).first()

    my_submissions = []
    latest_points = 0
    # Text-Aufgabe: Draft (Autosave) für den Editor-Startzustand —
    # ohne Draft fällt das Template auf text_template zurück.
    draft_solution: Optional[str] = None
    draft_updated_at = None
    if not is_tutor or is_student_view:
        if task.task_type.value == "text":
            draft = session.exec(
                select(TaskDraft)
                .where(TaskDraft.task_id == task.id)
                .where(TaskDraft.user_id == user.id)
            ).first()
            if draft:
                draft_solution = draft.solution
                draft_updated_at = draft.updated_at
        subs = session.exec(
            select(Submission)
            .where(Submission.task_id == task.id)
            .where(Submission.student_id == user.id)
            .order_by(Submission.submitted_at.desc())  # type: ignore[attr-defined]
        ).all()
        # Serialize zu sichere Dicts — keine rohen ORM-Objekte ans Template!
        my_submissions = []
        for sub in subs:
            feedbacks = []
            for fb in sub.feedback_list:
                feedbacks.append({
                    "id": fb.id,
                    "source": fb.source.value,
                    "points_earned": fb.points_earned,
                    "comment": fb.comment,
                    "giver_name": fb.giver.name if fb.giver else "LLM",
                    "created_at": fb.created_at,
                })
            my_submissions.append({
                "id": sub.id,
                "solution": sub.solution,
                "mc_answers": sub.mc_answers,
                "workspace_snapshot": sub.workspace_snapshot is not None,
                "upload_file": sub.upload_file,
                "mc_result": sub.mc_result,
                "attempt_number": sub.attempt_number,
                "submitted_at": sub.submitted_at,
                "status": sub.status.value,
                "solve_time_seconds": sub.solve_time_seconds,
                "feedback_list": feedbacks,
            })

        # MC: Historie-Ergebniskarten — das bei der Einreichung persistierte,
        # self-contained Ergebnis (Options-Texte, Status, Feedback, Punkte
        # in der Originalreihenfolge der Aufgabe). Es wird nie neu
        # berechnet: altes Feedback bleibt unverändert, auch wenn die
        # Aufgabe später bearbeitet oder neu gemischt wird.
        if task.task_type.value == "mc":
            for sub_dict in my_submissions:
                res = None
                raw = sub_dict.get("mc_result") or None
                if raw:
                    try:
                        cand = json.loads(raw)
                        if isinstance(cand, dict) and "questions" in cand:
                            res = cand
                    except ValueError:
                        res = None
                sub_dict["mc_result"] = res

        if my_submissions:
            latest_sub = my_submissions[0]  # newest (ordered desc)
            human_points = 0.0
            llm_points = 0.0
            override_exists = False
            for fb in latest_sub["feedback_list"]:
                if fb["source"] == "human":
                    human_points = max(human_points, fb["points_earned"])
                    override_exists = True
                else:
                    llm_points = max(llm_points, fb["points_earned"])
            latest_points = human_points if override_exists else llm_points

        # Perzentil fuer aktuelle Aufgabe berechnen
        student_members = session.exec(
            select(UserCourse)
            .where(UserCourse.course_id == course_id)
            .where(UserCourse.role_in_course == CourseRole.STUDENT)
        ).all()
        other_scores = []
        for member in student_members:
            if member.user_id == user.id:
                continue
            other_subs = session.exec(
                select(Submission)
                .where(Submission.task_id == task.id)
                .where(Submission.student_id == member.user_id)
                .order_by(Submission.submitted_at.desc())  # type: ignore[attrdefined]
            ).all()
            other_scores.append(_get_best_score(other_subs))
        task_percentile = _calculate_percentile(latest_points, other_scores)

        # Gruppen-Durchschnitt: Alle Kursstudenten zählen (ohne Abgabe = 0 Punkte)
        all_scores = other_scores + [latest_points]
        task_group_avg = round(sum(all_scores) / len(all_scores), 1)
    else:
        task_percentile = None
        task_group_avg = None

    return templates.TemplateResponse(
        template,
        {
            "request": request,
            "page_title": task.title,
            "current_user": _user_ctx(user, membership.role_in_course.value),
            "courses": courses,
            "selected_course_id": course_id,
            "is_admin": user.role == GlobalUserRole.ADMIN,
            "course": {
                "id": course.id,
                "name": course.name,
                "semester": course.semester,
                "description": course.description,
            },
            "task": {
                "id": task.id,
                "title": task.title,
                "task_type": task.task_type.value,
                "description": task.description,
                "max_points": task.max_points,
                "max_attempts": task.max_attempts,
                "deadline": task.deadline,
                "text_template": task.text_template,
                "model_solution": task.model_solution if is_tutor else None,
                "is_visible": task.is_visible if is_tutor else None,
                "hints_enabled": task.hints_enabled,
                "auto_grading_enabled": task.auto_grading_enabled,
                # Workspace: Main-Datei für alle (Editor-Modus),
                # Spec/Dataset/Status nur für Tutoren
                "workspace_main_file": task.workspace_main_file,
                # Workspace-Umgebung (nur Tutor): Simple-Fields im
                # task_detail_workspace.html
                "workspace_timeout": task.workspace_timeout if is_tutor else None,
                "workspace_cpu": task.workspace_cpu if is_tutor else None,
                "workspace_memory": task.workspace_memory if is_tutor else None,
                "workspace_disk_quota": task.workspace_disk_quota if is_tutor else None,
                "workspace_internet": task.workspace_internet if is_tutor else None,
                "workspace_assets_status": task.workspace_assets_status if is_tutor else None,
                # Compute-Engines/Image-Spec (nur Tutor)
                "workspace_engines": (
                    json.loads(task.workspace_engines)
                    if (task.workspace_engines and is_tutor) else None),
                "workspace_image": task.workspace_image if is_tutor else None,
                # Multiple-Choice (Student: nur Options-Texte, Tutor: komplett).
                # ?as_student=1 → Student-Ansicht (Options-Reihenfolge wird
                # bei jedem Seitenaufruf neu gemischt).
                "mc_data": _mc_context(
                    task, is_tutor and not is_student_view, user.id),
                # Upload: Template-Name für alle (Download-Button),
                # Musterlösungs-Datei nur für Tutoren
                "upload_template": task.upload_template,
                "has_upload_template": (
                    upload_service.task_upload_file(task.id, task.upload_template) is not None),
                "upload_solution_file": task.upload_solution_file if is_tutor else None,
                "has_upload_solution_file": (
                    upload_service.task_upload_file(task.id, task.upload_solution_file) is not None
                    if is_tutor else False),
                # Anzahl der Studierenden mit Abgabe (nur Tutor) —
                # „Alle als Zip“-Button (passt zur Ordner-Anzahl im Zip)
                "upload_submitter_count": (
                    len({s.student_id for s in task.submissions}) if is_tutor else 0),
            },
            "is_tutor": is_tutor,
            "is_student_view": is_student_view,
            "draft_solution": draft_solution,
            "draft_updated_at": draft_updated_at,
            "tabs": tab_ctx["tabs"],
            "tpl_type": tpl_type,
            "code_editor": is_tutor or is_workspace,  # Tutoren + Workspace-IDE: CodeMirror
            "my_submissions": my_submissions,
            "latest_points": latest_points,
            "total_attempts": len(my_submissions),
            "task_percentile": task_percentile,
            "task_group_avg": task_group_avg,
            # Preview-Subdomain-Basis (leer/null = aus, s. config)
            "preview_base_domain": PREVIEW_BASE_DOMAIN or None,

            "LLM_TIMEOUT": LLM_TIMEOUT,
            "prev_task": {"id": prev_task.id, "title": prev_task.title} if prev_task else None,
            "next_task": {"id": next_task.id, "title": next_task.title} if next_task else None,
        },
    )


@app.get("/courses/{course_id}/overview")
async def overview_page(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Kurs-Tab 'Übersicht': Punkte-Tabelle mit Excel-Export (PROF/Tutor)."""
    membership, ctx = _course_tab_context(
        session, user, request, course_id, active_tab="overview"
    )
    if not membership or membership.role_in_course not in (CourseRole.PROF, CourseRole.TUTOR):
        raise HTTPException(403, "Nur für PROF/Tutor.")

    ctx["page_title"] = f"Übersicht — {ctx['course']['name']}"
    return templates.TemplateResponse("course/overview.html", ctx)


@app.get("/courses/{course_id}/live", response_class=HTMLResponse)
async def live_dashboard_page(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Anonymisiertes Live-Dashboard (PROF/Tutor/Admin).

    Für alle anderen (z.B. Studenten, die den Link aus den Slides
    folgen) wird eine freundliche Zugriffs-Sperre gerendert statt 403.
    """
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()

    is_admin = user.role == GlobalUserRole.ADMIN
    can_view = is_admin or (
        membership is not None
        and membership.role_in_course in (CourseRole.PROF, CourseRole.TUTOR)
    )

    ctx = {
        "request": request,
        "page_title": f"Live-Dashboard — {course.name}",
        "page_description": f"Anonymisiertes Live-Dashboard für den Kurs {course.name}",
        "current_user": _user_ctx(
            user,
            membership.role_in_course.value if membership else ("ADMIN" if is_admin else "USER"),
        ),
        "courses": _get_user_courses(user, session),
        "selected_course_id": course_id,
        "is_admin": is_admin,
        "course": {
            "id": course.id,
            "name": course.name,
            "description": course.description,
            "semester": course.semester,
        },
        "can_view": can_view,
    }
    return templates.TemplateResponse("course/live_dashboard.html", ctx)


@app.get("/courses/{course_id}/live/mc", response_class=HTMLResponse)
async def live_mc_dashboard_page(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Anonymisiertes MC-Details-Dashboard (PROF/Tutor/Admin).

    Antwortverteilung der MC-Aufgaben (wie oft welche Option gewählt
    wurde), ohne jede studentenidentifizierenden Daten. Für alle
    anderen (z. B. Studenten) wird eine freundliche Zugriffs-Sperre
    gerendert statt 403.
    """
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()

    is_admin = user.role == GlobalUserRole.ADMIN
    can_view = is_admin or (
        membership is not None
        and membership.role_in_course in (CourseRole.PROF, CourseRole.TUTOR)
    )

    mc_tasks = session.exec(
        select(Task)
        .where(Task.course_id == course_id)
        .where(Task.is_visible == True)  # type: ignore[attr-defined]
        .where(Task.task_type == TaskType.MC)
        .order_by(Task.display_order.asc())  # type: ignore[attr-defined]
    ).all()

    ctx = {
        "request": request,
        "page_title": f"MC-Details — {course.name}",
        "page_description": f"Anonymisiertes MC-Details-Dashboard für den Kurs {course.name}",
        "current_user": _user_ctx(
            user,
            membership.role_in_course.value if membership else ("ADMIN" if is_admin else "USER"),
        ),
        "courses": _get_user_courses(user, session),
        "selected_course_id": course_id,
        "is_admin": is_admin,
        "course": {
            "id": course.id,
            "name": course.name,
            "description": course.description,
            "semester": course.semester,
        },
        "mc_tasks": [
            {"id": t.id, "title": t.title, "max_points": t.max_points}
            for t in mc_tasks
        ],
        "can_view": can_view,
    }
    return templates.TemplateResponse("course/live_mc_dashboard.html", ctx)


@app.get("/courses/{course_id}/tasks/{task_id}/students/{student_id}/review")
async def submission_review_page(
    course_id: int,
    task_id: int,
    student_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Tutor: Bewertungsseite für eine Studenteneinreichung."""
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()

    if not membership or membership.role_in_course not in (CourseRole.PROF, CourseRole.TUTOR):
        raise HTTPException(403, "Nur für PROF/Tutor.")

    task = session.get(Task, task_id)
    if not task or task.course_id != course_id:
        raise HTTPException(404, "Aufgabe nicht gefunden.")

    student = session.get(User, student_id)
    if not student:
        raise HTTPException(404, "Student nicht gefunden.")

    courses = _get_user_courses(user, session)

    # Kurs-Tab-Leiste: „Übersicht" ist aktiv (Bewertung wird dort verlinkt)
    _tab_membership, tab_ctx = _course_tab_context(
        session, user, request, course_id, active_tab="overview"
    )

    # Previous and next visible tasks (like student view)
    prev_task_obj = session.exec(
        select(Task)
        .where(Task.course_id == course_id)
        .where(Task.display_order < task.display_order)  # type: ignore[operator]
        .where(Task.is_visible == True)
        .order_by(Task.display_order.desc())  # type: ignore[attr-defined]
    ).first()

    next_task_obj = session.exec(
        select(Task)
        .where(Task.course_id == course_id)
        .where(Task.display_order > task.display_order)  # type: ignore[operator]
        .where(Task.is_visible == True)
        .order_by(Task.display_order.asc())  # type: ignore[attr-defined]
    ).first()

    # Calculate student's latest points for this task (like student view)
    student_subs = session.exec(
        select(Submission)
        .where(Submission.task_id == task_id)
        .where(Submission.student_id == student_id)
        .order_by(Submission.submitted_at.desc())  # type: ignore[attr-defined]
    ).all()
    latest_points = 0.0
    if student_subs:
        latest_sub = student_subs[0]  # newest
        human_points = 0.0
        llm_points = 0.0
        override_exists = False
        for fb in latest_sub.feedback_list:
            if fb.source == FeedbackSource.HUMAN:
                human_points = max(human_points, fb.points_earned)
                override_exists = True
            elif fb.source == FeedbackSource.LLM:
                llm_points = max(llm_points, fb.points_earned)
        latest_points = human_points if override_exists else llm_points

    task_type_display = {"text": "Textaufgabe",
                         "workspace": "Workspace-Aufgabe",
                         "mc": "Multiple-Choice-Aufgabe",
                         "upload": "Upload-Aufgabe",
                         "extern": "Externe Aufgabe"}.get(
        task.task_type.value, task.task_type.value
    )

    # Load hints for this student + task
    hints = session.exec(
        select(HintExchange)
        .where(HintExchange.task_id == task_id)
        .where(HintExchange.student_id == student_id)
        .order_by(HintExchange.created_at.asc())
    ).all()
    hints_data = [
        {
            "question": h.question,
            "llm_response": h.llm_response,
            "created_at": timeutil.utc_iso(h.created_at),
        }
        for h in hints
    ]

    return templates.TemplateResponse(
        "tutor/submission_review.html",
        {
            "request": request,
            "page_title": f"Bewertung — {task.title}",
            "current_user": _user_ctx(user, membership.role_in_course.value),
            "is_admin": user.role == GlobalUserRole.ADMIN,
            "courses": courses,
            "selected_course_id": course_id,
            "course_id": course_id,
            "task_id": task_id,
            "student_id": student_id,
            "task_title": task.title,
            "task_type_display": task_type_display,
            "task_type": task.task_type.value,
            # Code-Mirror-Shim für Workspace-Abgaben (Read-only-Editor
            # in der Lösungsdarstellung, s. base.html {% if code_editor %})
            "code_editor": task.task_type.value in ("workspace", "upload"),
            "max_points": task.max_points,
            "student_name": student.name,
            "student_username": student.username,
            "course": {
                "id": course.id,
                "name": course.name,
                "semester": course.semester,
                "description": course.description,
            },
            "tabs": tab_ctx["tabs"],
            "latest_points": latest_points,
            "total_attempts": len(student_subs),
            "hints": hints_data,
            "hints_enabled": task.hints_enabled,
            "prev_task": {"id": prev_task_obj.id, "title": prev_task_obj.title} if prev_task_obj else None,
            "next_task": {"id": next_task_obj.id, "title": next_task_obj.title} if next_task_obj else None,
        },
    )


@app.get("/settings")
async def settings_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """User-Einstellungen: Name / Passwort ändern."""
    courses = _get_user_courses(user, session)
    use_ldap = not user.password_hash  # LDAP-User haben keinen DB-Password-Hash

    # Rolle anzeigen: Admin bleibt "Admin", User zeigt generisch "User"
    display_role = "Admin" if user.role == GlobalUserRole.ADMIN else "User"

    return templates.TemplateResponse(
        "user_settings.html",
        {
            "request": request,
            "page_title": "Einstellungen",
            "current_user": _user_ctx(user, display_role),
            "courses": courses,
            "selected_course_id": None,
            "is_admin": user.role == GlobalUserRole.ADMIN,
            "use_ldap": use_ldap,
            "mcp_url": (
                f"https://{PUBLIC_DOMAIN}/mcp/"
                if PUBLIC_DOMAIN
                else _public_base_url(request) + "mcp/"
            ),
        },
    )


@app.get("/notifications/archive")
async def notifications_archive_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Nachrichten-Archiv: gelesene Benachrichtigungen (read_at gesetzt)."""
    items = session.exec(
        select(Notification)
        .where(Notification.user_id == user.id)
        .where(Notification.read_at.is_not(None))
        .order_by(Notification.id.desc())
        .limit(100)
    ).all()

    return templates.TemplateResponse(
        "notifications_archive.html",
        {
            "request": request,
            "page_title": "Nachrichten-Archiv",
            "current_user": _user_ctx(
                user, "Admin" if user.role == GlobalUserRole.ADMIN else "User"
            ),
            "courses": _get_user_courses(user, session),
            "selected_course_id": None,
            "is_admin": user.role == GlobalUserRole.ADMIN,
            "archive_items": items,
        },
    )


@app.get("/admin")
async def admin_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Admin: Kurse verwalten, User erstellen, Settings."""
    # Admin-Check (manuell, da wir in Page-Route sind)
    if user.role != GlobalUserRole.ADMIN:
        raise HTTPException(403, "Nur für Administratoren.")

    # Alle Kurse für den Überblick (inkl. Papierkorb, für Restore)
    all_courses = session.exec(
        select(Course).execution_options(**SOFT_DELETE_BYPASS)
    ).all()

    # Memberships des Users sammeln (für Link-Logik)
    user_memberships = session.exec(
        select(UserCourse).where(UserCourse.user_id == user.id)
    ).all()
    member_course_ids = {m.course_id for m in user_memberships}
    membership_roles = {m.course_id: m.role_in_course.value for m in user_memberships}

    courses = [
        {
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "semester": c.semester,
            "role_in_course": membership_roles.get(c.id, "NONE"),
            "is_member": c.id in member_course_ids,
            "deleted": c.deleted_at is not None,
        }
        for c in all_courses
    ]

    all_users = session.exec(
        select(User).execution_options(**SOFT_DELETE_BYPASS)
    ).all()

    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "page_title": "Admin",
            "current_user": _user_ctx(user, user.role.value),
            "courses": courses,
            "selected_course_id": None,
            "is_admin": True,
            "all_users": [
                {
                    "id": u.id,
                    "username": u.username,
                    "name": u.name,
                    "email": u.email,
                    "role": u.role.value,
                    "deleted": u.deleted_at is not None,
                }
                for u in all_users
            ],
        },
    )
