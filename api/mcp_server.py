"""
AICampus MCP-Server (Streamable HTTP, stateless) — Read- + Write-Tools.

Wird unter /mcp gemountet (main.py). Auth: Bearer-Token pro Request —
entsweder PAT (`aic_…`, s. services/mcp_token_service.py) oder OAuth
(Authorization-Code-Flow, s. api/mcp_oauth.py); bei jedem Tool-Call
werden Token, Kurs-Allowlist, Kurs-Mitgliedschaft und Rolle live geprüft.

Design: flach, ohne „entered course"-State — alle Kurs-Tools nehmen
`course_id` als Parameter (s. docs/LLM-plans/mcp-integration.md).

Datenschutz (bewusste Tool-Whitelist, keine generischen Read-Tools):
- Rolle STUDENT: nur `is_visible=True`-Content; Aufgaben ohne
  Musterlösung und ohne MC-Richtigkeitsflags (mc_service.student_view)
- Write-Tools (create_chapter / create_slide_deck / create_task /
  edit_content / edit_applet / create_applet / edit_task / Versions-Tools):
  nur Tutor/Prof +
  Write-Schalter des Tokens (PAT: allow_write, OAuth: Consent-Entscheidung
  bzw. Grant-Update in den Settings — gilt auch für Admins). Versionierung:
  neues Element → Initial-Version „Erstellt“; jedes MCP-Edit → Version
  „MCP Edit“ (neuer Zustand) — der Zustand vorher bleibt in der Version
  davor erhalten (Rückgängig: list_versions + restore_version)
- Nie erreichbar: Einreichungen, Feedback, Punkte, Mitglieder, Forum,
  Hint-Dialoge, interne LLM-Summaries, Settings
"""

import base64
import contextvars
import functools
import io
import ipaddress
import json
import logging
import re
import socket
import zipfile
from datetime import datetime, timezone
from typing import Optional, Union
from urllib.parse import unquote

import httpx
from fastapi import HTTPException
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.routes import create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import TextContent
from sqlmodel import Session, func, select

from api.mcp_oauth import AICampusAuthProvider
from config import PUBLIC_DOMAIN, UPLOAD_MAX_FILE_BYTES
from database.base import engine
from database.models import (
    ContentVersion,
    Course,
    CourseMaterial,
    CourseMedia,
    CourseReference,
    CourseRole,
    GlobalUserRole,
    MaterialType,
    McpClientContext,
    McpToken,
    McpTokenCourse,
    ScriptSection,
    Task,
    TaskType,
    User,
    UserCourse,
)
from prompts.markdown_manual import (
    SCRIPT_CONTENT_EDITS_SPEC,
    SCRIPT_MARKDOWN_MANUAL,
    SLIDES_CONTENT_EDITS_SPEC,
    SLIDES_MARKDOWN_MANUAL,
)
from services import analytics
from services import bibtex as bib
from services import mcp_oauth_service
from services import mcp_token_service
from services import notifications as notify
from services import media_service, upload_service, version_service
from services.auth_service import set_current_user_id
from services.content_edits import ContentEditError, _resolve_span, apply_content_edits
from services.mc_service import McValidationError, mc_total_points, parse_mc_data, student_view, tutor_view
from services.media_service import sync_media_usages
from services.slides_service import SlideError, apply_slide_edits, parse_slides
from services.timeutil import deadline_from_local, deadline_iso

logger = logging.getLogger(__name__)

_MCP_NOOP_VERSION_NOTE = "Keine neue Version angelegt: Inhalt blieb unverändert."


# ─── Versionierung ───────────────────────────────────────────────
def _mcp_version_note(
    session: Session, user: User, entity_type: str, entity_id: int, label: Optional[str] = None
) -> str:
    """Version aus dem aktuellen Zustand nach einem MCP-Edit anlegen —
    Name: optionales version_label, sonst „MCP Edit“. Aber nur, wenn sich
    wirklich etwas geändert hat (sonst: Hinweis ohne Version)."""
    name = (label or "").strip()[:200] or "MCP Edit"
    v = version_service.snapshot_current(
        session, user, entity_type, entity_id, name=name, skip_if_same_as_latest=True)
    if not v:
        return _MCP_NOOP_VERSION_NOTE
    return (
        f"Automatisch angelegt: Version „{name}“ (neuer Zustand). Zustand vorher = "
        "die Version davor (Rückgängig: list_versions + restore_version)."
    )


ROLE_LABELS = {
    CourseRole.PROF: "Professor",
    CourseRole.TUTOR: "Tutor",
    CourseRole.STUDENT: "Student",
}


class McpToolError(ToolError):
    """Tool-Fehler mit Meldung für den LLM-Client (statt generischem 500).

    Subclass von ToolError, damit die Meldung ans Modell durchgeht
    (andere Exceptions erreichen den Client nur als "Error executing tool …").
    """


# ─── Auth / Hilfsfunktionen ───────────────────────────────────────

def _bearer_token(ctx: Context) -> Optional[str]:
    """Bearer-Header → rohes Token (PAT `aic_…` ODER OAuth `aio_…`)."""
    headers = ctx.headers or {}
    value = headers.get("authorization") or headers.get("Authorization")
    if not value:
        return None
    parts = value.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _auth(
    ctx: Context,
    session: Session,
    course_id: Optional[int],
    write: bool = False,
    tool_name: Optional[str] = None,
):
    """Token + (falls course_id) Allowlist + Mitgliedschaft + Rolle prüfen.

    Liefert (user, token, role). Bei course_id=None (list_courses) ist die
    Rolle egal (STUDENT) und wird nicht geprüft. Global-Admin zählt wie
    PROF (analog require_course_access) — die Token-Allowlist gilt trotzdem.

    Token-Typen: PAT (`aic_…`) und OAuth (Consent-Flow). Die
    Kurs-Allowlist kommt jeweils aus der eigenen Tabelle (McpTokenCourse
    bzw. McpOAuthGrantCourse); beide Token-Typen haben einen Write-Schalter
    (PAT: allow_write in den User-Settings, OAuth: Consent-Entscheidung).
    """
    raw = _bearer_token(ctx)
    if not raw:
        raise McpToolError(
            "Nicht authentifiziert. Erwartet wird ein Header "
            "'Authorization: Bearer aic_…' (Token unter Einstellungen → MCP anlegen)."
        )
    if raw.startswith("aic_"):
        token = mcp_token_service.verify_token(session, raw)
        if not token:
            raise McpToolError("Token ist ungültig, unbekannt oder abgelaufen.")
        allowed = {tc.course_id for tc in token.courses}
        allow_write = token.allow_write is not False  # PAT: Write-Schalter (Einstellungen → MCP)
    else:
        token = mcp_oauth_service.lookup_access(session, raw)
        if not token:
            raise McpToolError("Token ist ungültig, unbekannt oder abgelaufen.")
        allowed = set(mcp_oauth_service.grant_course_ids(session, token.user_id, token.client_id))
        allow_write = token.allow_write
    user = session.get(User, token.user_id)
    if not user:
        raise McpToolError("Der Token-Besitzer existiert nicht mehr.")
    set_current_user_id(user.id)
    logger.info("[MCP] tool=%s user=%s course=%s write=%s",
                tool_name or "?", user.username, course_id, write)
    _MCP_CALL.set({
        "user_id": user.id,
        "course_id": course_id,
        "tool_name": tool_name or "unknown",
        "is_write": write,
        "client": (
            token.name
            if isinstance(token, McpToken)
            else mcp_oauth_service.client_name(session, token.client_id)
        ),
    })

    if course_id is None:
        return user, token, CourseRole.STUDENT

    if session.get(Course, course_id) is None:
        raise McpToolError(
            f"Kurs {course_id} existiert nicht (mehr). Nutze list_courses, um "
            "deine verfügbaren Kurse zu sehen."
        )

    if course_id not in allowed:
        raise McpToolError(
            f"Kurs {course_id} ist für dieses Token nicht freigegeben. "
            "Prüfe list_courses für deine Kurse und gib den Kurs ggf. unter "
            "Einstellungen → MCP frei."
        )

    if write and not allow_write:
        raise McpToolError(
            "Dieses Tool erfordert Schreibzugriff — der wurde für diesen "
            "Client beim MCP-Zugriff nicht erteilt (Einstellungen → MCP)."
        )

    if user.role == GlobalUserRole.ADMIN:
        return user, token, CourseRole.PROF

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user.id)
        .where(UserCourse.course_id == course_id)
    ).first()
    if not membership:
        raise McpToolError("Du bist nicht mehr Mitglied dieses Kurses.")
    role = membership.role_in_course
    if write and role not in (CourseRole.PROF, CourseRole.TUTOR):
        raise McpToolError(
            "Dieses Tool erfordert die Rolle Tutor oder Professor "
            "(Studenten haben nur Lesezugriff)."
        )
    return user, token, role


def _is_tutor(role: CourseRole) -> bool:
    return role in (CourseRole.PROF, CourseRole.TUTOR)


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


def _snippet(text: str, query: str, width: int = 200) -> str:
    """~400 Zeichen um die erste Trefferstelle (für Search-Results)."""
    text = text or ""
    idx = text.lower().find(query.lower())
    if idx < 0:
        return text[: width * 2]
    start = max(0, idx - width)
    end = min(len(text), idx + len(query) + width)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


def _resolve(session: Session, model, course_id: int, ref: Union[int, str], label: str):
    """ref = ID (int) oder exakter Titel (case-insensitive)."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        obj = session.get(model, int(ref))
        if not obj or obj.course_id != course_id:
            raise McpToolError(f"{label} mit ID {ref} nicht in diesem Kurs gefunden.")
        return obj
    rows = session.exec(
        select(model)
        .where(model.course_id == course_id)
        .where(func.lower(model.title) == ref.strip().lower())
    ).all()
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise McpToolError(
            f"{label} '{ref}' nicht gefunden. Nutze course_overview für die ID-Liste."
        )
    names = "; ".join(f"{r.title} (ID {r.id})" for r in rows)
    raise McpToolError(f"Titel '{ref}' ist mehrdeutig: {names}. Bitte per ID aufrufen.")


def _ref_match(text: str, query: str) -> bool:
    return query.lower() in (text or "").lower()


# ─── Server + Tools ───────────────────────────────────────────────

# Basis-URL für Deep-Links in den Tool-Antworten (s. instructions unten)
_MCP_BASE = f"https://{PUBLIC_DOMAIN}" if PUBLIC_DOMAIN else "https://aicampus.example.com"

# Server-Instructions: wandern per initialize an Clients, die das anzeigen
# (Claude Desktop & Co.); für Clients ohne Instructions-Support (ChatGPT)
# liefert das aicampus_mcp_instructions-Tool denselben Text.
_MCP_INSTRUCTIONS = (
        "AICampus-Kursinhalte: Vorlesungsskripte, Folien (Slide-Decks), "
        "Übungsaufgaben, Quellen-Bibliothek, Bilder und interaktive Applets. Alle "
        "Kurs-Tools brauchen ein course_id: rufe zuerst list_courses() auf, "
        "um deine Kurse und deine Rolle zu ermitteln. Orientiere dich dann "
        "mit course_overview(course_id) oder search_course(course_id, query) "
        "und lies Details mit read_script_section / read_material / read_task "
        "/ read_applet / list_references / search_references / list_media "
        "(Bilder + Applets mit"
        " fertiger Markdown-Referenz zum Einbinden in Skript/Folien; "
        "Bild-Inhalt ansehen: read_image).\n"
        "Rufe nach jeder User-Eingabe / Prompt ZUERST get_user_focus() auf um den aktuellen"
        "Fokus (offene Seite, aktuelles Kapitel/Folie, Textmarkierung) des Nutzers zu erfassen. "
        "Das ist insbesondere wichtig, wenn sich der User auf etwas bezieht, das er gerade sieht („das“, „diesen "
        "Absatz“, „diese Folie“, „kürze das“), oder du dir unsicher bist, "
        "worauf sich seine Anweisung bezieh — nicht raten oder suchen.\n"
        "Das visible-Flag in course_overview betrifft NUR die Sichtbarkeit "
        "für Studierende: visible=false bedeutet 'noch nicht für Studierende "
        "freigeschaltet'. Als Tutor/Prof sind dir ALLE Inhalte sichtbar und "
        "editierbar, unabhängig vom Flag.\n"
        "Als Tutor/Prof mit Write-Schalter kannst du Inhalte anlegen und "
        "bearbeiten: create_chapter, create_slide_deck (neue Inhalte sind per"
        " Default NICHT für Studenten sichtbar — is_visible=True zum "
        "Freischalten), edit_content (stellenweise Edits ODER kompletter "
        "Content-Ersatz, plus Titel/Kurzfassung), edit_applet (inkl. "
        "Formatwechsel html↔svg, Titel/Beschreibung), create_applet "
        "(HTML-/SVG-Applets) und create_image "
        "(Bilder und Videos (mp4/WebM) per Data-URL oder öffentlicher URL; "
        "SVG-Dateien werden nicht rasterisiert, sondern als SVG-Applet "
        "gespeichert, Video-Dateien als Video-Medium). "
        "Übungsaufgaben: "
        "create_task (type 'text', 'mc', 'upload' oder 'extern' — Workspace-Aufgaben nur "
        "in der Kurs-Web-UI) und edit_task (bestehende Aufgaben, inkl. "
        "MC-Fragen-Ersatz); Upload-Aufgaben: Template-/Musterlösungs-Datei "
        "setzen mit set_upload_task_file (Text, data:-URL oder URL) und lesen "
        "mit read_upload_task_file (Tutor/Prof). "
        "Quellen-Bibliothek: create_reference / edit_reference (einzelne) und "
        "import_bibtex (mehrere auf einmal aus BibTeX) — zitierbar mit "
        "@cite:shannon1948 (key der Quelle, OHNE Klammern; Liste: list_references). "
        "Alle Inhalte (inkl. Aufgabenstellung/Template/Musterlösung) in "
        "AICampus-Markdown (markdown_manual); Musterlösungen IMMER mit "
        "Bewertungskriterien (max. max_points Punkte, nur aus der "
        "Aufgabenstellung ersichtliche). Querverweise einbauen, wo sinnvoll: "
        "@sec:/@eq:/@fig:/@code:/@box:/@tab:/@cite: als normaler Fließtext "
        "(NUR tatsächlich vorhandene Labels — Inhalte vorher lesen, NIEMALS "
        "in Backticks) sowie Aufgaben aus Skript/Folien per @task:243 "
        "(Aufgaben-ID OHNE Klammern; IDs: course_overview). "
        "Vor JEDEM Edit wird "
        "automatisch eine Version des alten Zustands angelegt — Historie: "
        "list_versions, Rückgängig: restore_version. Bevor du Inhalte anlegst"
        " oder änderst, rufe markdown_manual(kind) auf, um die "
        "AICampus-Markdown-Syntax zu lernen (KaTeX, Abbildungen/Labels, "
        "Querverweise, Folien-Direktiven, Applets).\n"
        f"Links: Wenn der User nach einem Link zu einem Kurs-Element fragt, "
        f"konstruiere die URL direkt aus den von den Tools zurückgegebenen "
        f"IDs (Basis-URL {_MCP_BASE}): "
        f"Kurs /courses/{{course_id}}; Übungsaufgabe /courses/{{course_id}}/tasks/{{task_id}}; "
        f"Slide-Deck (Ansicht) /courses/{{course_id}}/slides/{{deck_id}}, "
        f"(Präsentation) /courses/{{course_id}}/slides/{{deck_id}}/present "
        f"(PDF-Variante: ?print-pdf anhängen; einzelne Folie: /present#/H/V anhängen, "
        f"H = 0-basierte Foliennummer, V = Fragment-Nummer im Slide (0, wenn keine)); "
        f"Skript-Kapitel /courses/{{course_id}}/script/{{section_id}}. "
        f"Zu einzelnen Elementen im Skript: auf die Basis-URL "
        f"/courses/{{course_id}}/script einen Fragment-Link anhängen — "
        f"#chapter-{{section_id}} (Kapitel), #eq:{{label}} (Formel), #fig:{{label}} "
        f"(Abbildung), #code:{{label}}, #box:{{label}}, #tab:{{label}}, #sec:{{label}} "
        f"(Abschnitts-Heading); das Kapitel klappt dabei automatisch auf. Die Labels "
        f"siehst du als Marker wie {{#eq:label}} in den von read_script_section() "
        f"gelieferten Inhalten — nur tatsächlich vorhandene Labels verwenden, "
        f"keine erfinden. Die Links funktionieren "
        f"nur für eingeloggte AICampus-User mit Kurs-Mitgliedschaft — course_id "
        f"immer aus list_courses()/course_overview() nehmen, nicht raten."
)

server = MCPServer("AICampus", instructions=_MCP_INSTRUCTIONS)


# ─── Nutzungs-Tracking (Analytics-Dashboard) ───────────────────────
# _MCP_CALL wird von _auth gesetzt; der mcp_tool-Wrapper (ersetzt
# @server.tool()) loggt das Ergebnis jedes Calls in mcp_events
# (s. services/analytics.py: record_mcp_event / mcp_usage / mcp_recent).

_MCP_CALL: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "mcp_call", default=None
)


def _record_call(ok: bool) -> None:
    info = _MCP_CALL.get()
    if not info:
        return  # u. a. Auth-Fehler ohne identifizierbaren User
    try:
        analytics.record_mcp_event(
            user_id=info["user_id"],
            course_id=info["course_id"],
            tool_name=info["tool_name"],
            client=info["client"],
            is_write=info["is_write"],
            ok=ok,
        )
    except Exception:
        logger.exception("[MCP] Nutzungs-Tracking fehlgeschlagen")


def mcp_tool(fn):
    """@server.tool() + Nutzungs-Logging (s. _record_call)."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            result = await fn(*args, **kwargs)
        except Exception:
            _record_call(False)
            raise
        _record_call(True)
        return result

    return server.tool()(wrapper)


@mcp_tool
async def aicampus_mcp_instructions(ctx: Context) -> str:
    """AICampus-MCP-Anweisungen — READ FIRST (once per session).

    Please read these instructions first if you don't already have access
    to the AICampus MCP server instructions (some clients, e.g. ChatGPT,
    do not pass them to the model). They contain important usage
    information for all AICampus tools: the expected workflow, how to
    navigate courses, and how to create links/URLs to course elements
    (tasks, slide decks, script chapters). Call it before using the
    other tools."""
    with Session(engine) as session:
        _auth(ctx, session, None, tool_name="aicampus_mcp_instructions")
    return _MCP_INSTRUCTIONS


@mcp_tool
async def list_courses(ctx: Context) -> str:
    """Alle für dieses Token freigegebenen Kurse (ID, Name, Semester, deine Rolle).

    Erst aufrufen, um das course_id für alle anderen Tools zu ermitteln."""
    with Session(engine) as session:
        user, token, _ = _auth(ctx, session, None, tool_name="list_courses")
        if isinstance(token, McpToken):
            allowed_ids = sorted(tc.course_id for tc in token.courses)
        else:
            allowed_ids = mcp_oauth_service.grant_course_ids(session, user.id, token.client_id)
        courses = []
        for cid in allowed_ids:
            course = session.get(Course, cid)
            if not course:
                continue
            if user.role == GlobalUserRole.ADMIN:
                role = CourseRole.PROF
            else:
                m = session.exec(
                    select(UserCourse)
                    .where(UserCourse.user_id == user.id)
                    .where(UserCourse.course_id == course.id)
                ).first()
                if not m:
                    continue
                role = m.role_in_course
            courses.append({
                "course_id": course.id,
                "name": course.name,
                "semester": course.semester,
                "role": ROLE_LABELS[role],
            })
        return _json({
            "courses": courses,
            "hint": "Nutze das course_id aus dieser Liste für alle anderen Tools.",
        })


# ─── User-Fokus (welche Seite/Markierung hat der User gerade offen?) ──

_FOCUS_STALE_SECONDS = 600  # > 10 min → „keine aktuelle Seite“


def _parse_focus_url(url: str):
    """/courses/{id}/… → (course_id, page, entity_type, entity_id) —
    Seite + Entity kommen aus der URL, die das Frontend meldet."""
    m = re.fullmatch(r"/courses/(\d+)(?:/(.*))?", url or "")
    if not m:
        return None
    course_id = int(m.group(1))
    parts = [p for p in (m.group(2) or "").split("/") if p]
    if not parts:
        return course_id, "course", None, None
    if parts[0] == "script":
        return course_id, "script", None, None
    if parts[0] == "slides":
        if len(parts) >= 2 and parts[1].isdigit():
            deck = int(parts[1])
            if len(parts) >= 3 and parts[2] == "present":
                return course_id, "slides_present", "slide_deck", deck
            return course_id, "slides", "slide_deck", deck
        return course_id, "slides", None, None
    if parts[0] == "tasks":
        if len(parts) >= 2 and parts[1].isdigit():
            return course_id, "task", "task", int(parts[1])
        return course_id, "tasks", None, None
    if parts[0] == "applets":
        if len(parts) >= 2 and parts[1].isdigit():
            return course_id, "applet", "applet", int(parts[1])
        return course_id, "applets", None, None
    return course_id, "other", None, None


def _focus_entity_title(session: Session, entity_type: Optional[str], entity_id: Optional[int]) -> Optional[str]:
    if not entity_id:
        return None
    model = {"slide_deck": CourseMaterial, "task": Task, "applet": CourseMedia}.get(entity_type or "")
    if not model:
        return None
    obj = session.get(model, entity_id)
    return getattr(obj, "title", None)


def _focus_hint(page: str, entity_id: Optional[int], section: Optional[dict], can_write: bool) -> str:
    if not can_write:
        return ("Dieses Token hat hier nur Lesezugriff — zum Bearbeiten bitte "
                "den User um einen Client/Token mit Schreibrecht bitten.")
    if page == "script" and section:
        return (f"Aktuelles Kapitel: „{section['title']}“ (ID {section['id']}). "
                f"Bearbeiten: edit_content (type='script_section', ref={section['id']}, "
                "edits=[{\"op\": \"replace_span\", …}] für die markierte Stelle, "
                "content für ein Neuschreiben) — selection/selection_context "
                "identifiziert die exakte Stelle.")
    if page in ("slides", "slides_present") and entity_id:
        return (f"Bearbeiten: edit_content (type='slide_deck', ref={entity_id}, …) — "
                "Folien sind mit '---' getrennt; selection bzw. slide identifizieren "
                "die Stelle.")
    if page == "task" and entity_id:
        return f"Bearbeiten: edit_task (ref={entity_id})."
    if page == "applet" and entity_id:
        return f"Quelle ansehen: read_applet; bearbeiten: edit_applet (ref={entity_id})."
    return "Details: course_overview / read_…-Tools; Bearbeiten: die Write-Tools."


@mcp_tool
async def get_user_focus(ctx: Context) -> str:
    """Was schaut sich der User gerade in AICampus an?

    Wenn du dir nicht sicher bist, worauf sich die Anweisung des Users
    bezieht — besonders wenn er auf das Bezug nimmt, was er gerade
    sieht/öffnet/gerade gemacht hat („das hier“, „diesen Absatz“, „diese Folie“,
    „bitte kürze das“, „bitte formuliere das ein bisschen aus“, „die Stelle mit X“) —
    rufe immer ZUERST dieses Tool auf, statt zu raten oder zu suchen: Es
    liefert die offene Seite (Skript/Slides/Aufgabe/Applet), das aktuelle
    Kapitel (Skript) bzw. die aktuelle Folie (Slides inkl. Präsentationsmodus)
    und — falls der User Text markiert hat — die Markierung inkl.
    Umgebung. Danach bearbeiten: edit_content / edit_applet / edit_task
    (ref kommt aus diesem Rückgabewert)."""
    with Session(engine) as session:
        user, _token, _ = _auth(ctx, session, None, tool_name="get_user_focus")
        row = session.get(McpClientContext, user.id)
        if not row:
            return _json({"focus": None,
                          "note": "Der User hat gerade keine AICampus-Seite geöffnet (oder noch keine gemeldet). "
                                  "Bitte den User bitten, die Seite zu öffnen und ggf. Text zu markieren."})
        age = (datetime.now() - row.updated_at).total_seconds()
        if age > _FOCUS_STALE_SECONDS:
            return _json({"focus": None,
                          "note": f"Kein aktueller Zustand (zuletzt vor {int(age // 60)} min gemeldet — "
                                  "Browser ggf. geschlossen). Bitte den User bitten, die Seite zu öffnen "
                                  "und ggf. Text zu markieren."})
        parsed = _parse_focus_url(row.url)
        if not parsed:
            return _json({"focus": None,
                          "note": f"Der User ist auf einer Seite, aus der sich der Kurs nicht ableiten lässt ({row.url}). "
                                  "Bitte den User fragen, welchen Kurs er meint."})
        course_id, page, entity_type, entity_id = parsed
        course = session.get(Course, course_id)
        course_name = course.name if course else f"Kurs {course_id}"
        # Zugriffs-Check: derselbe Pfad wie alle anderen Kurs-Tools.
        try:
            _u, token, role = _auth(ctx, session, course_id, tool_name="get_user_focus")
        except McpToolError as e:
            reason = str(e)
            note = (f"Der User hat gerade offen: {course_name} – Seite „{page}“ — aber "
                    f"dieses Token hat dort keinen Zugriff. Ursache: {reason}")
            if "nicht freigegeben" in reason:
                note += (" Bitte den User bitten, den Kurs unter Einstellungen → MCP "
                         "für diesen Token/Client freizugeben.")
            return _json({
                "focus": page,
                "course_id": course_id,
                "course": course_name,
                "access": "denied",
                "note": note,
            })
        allow_write = (token.allow_write is not False) if isinstance(token, McpToken) else bool(token.allow_write)
        can_write = bool(allow_write and _is_tutor(role))
        result = {
            "focus": page,
            "course_id": course_id,
            "course": course_name,
            "role": ROLE_LABELS[role],
            "can_write": can_write,
            "updated_seconds_ago": int(age),
        }
        if entity_type:
            result["entity"] = {
                "type": entity_type,
                "id": entity_id,
                "title": _focus_entity_title(session, entity_type, entity_id),
            }
        section = None
        if page == "script" and row.section_id:
            s = session.get(ScriptSection, row.section_id)
            if s and s.course_id == course_id:
                section = {"id": s.id, "title": s.title}
                result["section"] = section
        if page in ("slides", "slides_present") and (row.slide_index is not None or row.slide_text):
            slide: dict = {}
            if row.slide_index is not None:
                slide["index"] = row.slide_index
            if row.slide_count:
                slide["count"] = row.slide_count
            if row.slide_text:
                slide["text"] = row.slide_text
            result["slide"] = slide
        if row.selection:
            result["selection"] = row.selection
            if row.selection_context:
                result["selection_context"] = row.selection_context
                if row.selection_offset is not None:
                    result["selection_offset"] = row.selection_offset
        result["hint"] = _focus_hint(page, entity_id, section, can_write)
        return _json(result)


@mcp_tool
async def course_overview(course_id: int, ctx: Context) -> str:
    """Kursübersicht ohne Inhalt: Skript-Kapitel, Slide-Decks, Aufgaben, Quellen-Anzahl.

    Für Studenten nur freigeschaltete Inhalte; für Tutor/Prof ALLE Einträge
    plus visible-Flag (visible=false = noch nicht für Studenten freigeschaltet
    — für dich als Tutor/Prof trotzdem les- und editierbar)."""
    with Session(engine) as session:
        _user, _token, role = _auth(ctx, session, course_id, tool_name="course_overview")
        course = session.get(Course, course_id)
        if not course:
            raise McpToolError(f"Kurs {course_id} nicht gefunden (siehe list_courses).")
        tutor = _is_tutor(role)

        sections = session.exec(
            select(ScriptSection)
            .where(ScriptSection.course_id == course_id)
            .order_by(ScriptSection.display_order.asc())  # type: ignore[attr-defined]
        ).all()
        section_list = []
        for i, s in enumerate(sections, start=1):
            if not tutor and not s.is_visible:
                continue
            entry = {"id": s.id, "title": s.title}
            if tutor:
                entry["visible"] = s.is_visible
            section_list.append(entry)

        materials = session.exec(
            select(CourseMaterial)
            .where(CourseMaterial.course_id == course_id)
            .order_by(CourseMaterial.display_order.asc())  # type: ignore[attr-defined]
        ).all()
        deck_list = []
        for m in materials:
            if not tutor and not m.is_visible:
                continue
            entry = {
                "id": m.id,
                "title": m.title,
                "type": "slides" if m.material_type == MaterialType.SLIDES else "script (legacy)",
            }
            if tutor:
                entry["visible"] = m.is_visible
            deck_list.append(entry)

        tasks = session.exec(
            select(Task)
            .where(Task.course_id == course_id)
            .order_by(Task.display_order.asc())  # type: ignore[attr-defined]
        ).all()
        task_list = []
        for t in tasks:
            if not tutor and not t.is_visible:
                continue
            entry = {"id": t.id, "title": t.title, "type": t.task_type.value}
            if tutor:
                entry["visible"] = t.is_visible
            task_list.append(entry)

        ref_count = session.exec(
            select(func.count(CourseReference.id)).where(
                CourseReference.course_id == course_id
            )
        ).one()

        out = {
            "course": {"course_id": course.id, "name": course.name, "semester": course.semester},
            "script_sections": section_list,
            "slide_decks_and_scripts": deck_list,
            "tasks": task_list,
            "reference_count": ref_count,
            "hint": (
                "Details: read_script_section / read_material / read_task; "
                "Volltextsuche: search_course; Quellen: list_references / "
                "search_references; Medien (Bilder/Videos/Applets) zur "
                "Einbindung: list_media."
            ),
        }
        if tutor:
            out["hint"] += (
                " visible=false heißt nur: noch nicht für Studenten freigeschaltet;"
                " den Inhalt kannst du normal lesen/editieren."
                " Aufgaben: read_task (inkl. Musterlösung); anlegen/editieren:"
                " create_task (Typ 'text'/'mc') / edit_task."
            )
            out["applets"] = [
                {"id": m.id, "title": m.title}
                for m in session.exec(
                    select(CourseMedia)
                    .where(CourseMedia.course_id == course_id)
                    .where(CourseMedia.media_type == "applet")
                    .order_by(CourseMedia.created_at.asc())  # type: ignore[arg-type]
                ).all()
            ]
        return _json(out)


@mcp_tool
async def search_course(ctx: Context, course_id: int, query: str, limit: int = 10) -> str:
    """Volltextsuche (Keyword) über Skript-Kapitel, Folien/Skripte, Aufgabenstellungen
    und Quellen. Liefert je Treffer Ort + Snippet um die Trefferstelle.

    Für Studenten nur freigeschaltete Inhalte; Musterlösungen werden NICHT durchsucht."""
    with Session(engine) as session:
        _user, _token, role = _auth(ctx, session, course_id, tool_name="search_course")
        if not (query or "").strip():
            raise McpToolError("Leere Suchanfrage.")
        course = session.get(Course, course_id)
        if not course:
            raise McpToolError(f"Kurs {course_id} nicht gefunden (siehe list_courses).")
        tutor = _is_tutor(role)
        lim = max(1, min(int(limit), 25))

        hits = []

        sections = session.exec(
            select(ScriptSection).where(ScriptSection.course_id == course_id)
        ).all()
        for s in sorted(sections, key=lambda x: x.display_order):
            if not tutor and not s.is_visible:
                continue
            text = f"{s.title}\n{s.content}"
            if _ref_match(text, query):
                hits.append({
                    "location": "script_section",
                    "id": s.id,
                    "title": s.title,
                    "snippet": _snippet(text, query),
                    "read_with": "read_script_section",
                })

        materials = session.exec(
            select(CourseMaterial).where(CourseMaterial.course_id == course_id)
        ).all()
        for m in sorted(materials, key=lambda x: x.display_order):
            if not tutor and not m.is_visible:
                continue
            text = f"{m.title}\n{m.content}"
            if _ref_match(text, query):
                hits.append({
                    "location": "material",
                    "id": m.id,
                    "title": m.title,
                    "type": "slides" if m.material_type == MaterialType.SLIDES else "script (legacy)",
                    "snippet": _snippet(text, query),
                    "read_with": "read_material",
                })

        tasks = session.exec(select(Task).where(Task.course_id == course_id)).all()
        for t in sorted(tasks, key=lambda x: x.display_order):
            if not tutor and not t.is_visible:
                continue
            text = f"{t.title}\n{t.description}"
            if _ref_match(text, query):
                hits.append({
                    "location": "task",
                    "id": t.id,
                    "title": t.title,
                    "type": t.task_type.value,
                    "snippet": _snippet(text, query),
                    "read_with": "read_task",
                })

        refs = session.exec(
            select(CourseReference).where(CourseReference.course_id == course_id)
        ).all()
        for r in sorted(refs, key=lambda x: x.display_order):
            authors = ", ".join(r.authors or [])
            text = f"{r.key} {authors} {r.title} {r.venue} {r.description}"
            if _ref_match(text, query):
                hits.append({
                    "location": "reference",
                    "key": r.key,
                    "title": r.title,
                    "snippet": _snippet(r.description or r.title, query),
                    "read_with": "search_references",
                })

        total = len(hits)
        return _json({
            "course": course.name,
            "query": query,
            "total_hits": total,
            "hits": hits[:lim],
            "hint": "Ggf. limit erhöhen oder Suchbegriff verfeinern." if total > lim else None,
        })


@mcp_tool
async def read_script_section(course_id: int, ref: Union[int, str], ctx: Context) -> str:
    """Komplettes Skript-Kapitel (Markdown). ref = Kapitel-ID oder exakter Titel."""
    with Session(engine) as session:
        _user, _token, role = _auth(ctx, session, course_id, tool_name="read_script_section")
        section = _resolve(session, ScriptSection, course_id, ref, "Skript-Kapitel")
        if not _is_tutor(role) and not section.is_visible:
            raise McpToolError("Kapitel nicht gefunden oder noch nicht freigeschaltet.")
        return _json({
            "id": section.id,
            "course_id": course_id,
            "title": section.title,
            "content": section.content,
        })


@mcp_tool
async def read_material(course_id: int, ref: Union[int, str], ctx: Context) -> str:
    """Slide-Deck oder (Legacy-)Skript als Markdown. ref = ID oder exakter Titel.

    Slides-Folien sind mit `---` getrennt."""
    with Session(engine) as session:
        _user, _token, role = _auth(ctx, session, course_id, tool_name="read_material")
        material = _resolve(session, CourseMaterial, course_id, ref, "Material")
        if not _is_tutor(role) and not material.is_visible:
            raise McpToolError("Material nicht gefunden oder noch nicht freigeschaltet.")
        return _json({
            "id": material.id,
            "course_id": course_id,
            "title": material.title,
            "type": "slides" if material.material_type == MaterialType.SLIDES else "script (legacy)",
            "content": material.content,
        })


@mcp_tool
async def read_task(course_id: int, ref: Union[int, str], ctx: Context) -> str:
    """Übungsaufgabe inkl. Aufgabenstellung. ref = Aufgaben-ID oder exakter Titel.

    Für Studenten nur freigeschaltete Aufgaben OHNE Musterlösung und ohne
    MC-Richtigkeitszeichen; für Tutor/Prof vollständig."""
    with Session(engine) as session:
        user, _token, role = _auth(ctx, session, course_id, tool_name="read_task")
        task = _resolve(session, Task, course_id, ref, "Aufgabe")
        tutor = _is_tutor(role)
        if not tutor and not task.is_visible:
            raise McpToolError("Aufgabe nicht gefunden oder noch nicht freigeschaltet.")
        out = {
            "id": task.id,
            "course_id": course_id,
            "title": task.title,
            "type": task.task_type.value,
            "max_points": task.max_points,
            "deadline": deadline_iso(task.deadline),
            "description": task.description,
        }
        if task.text_template:
            out["text_template"] = task.text_template
        if task.task_type.value == "upload":
            # Template: alle sehen den Dateinamen (Studierende können die
            # Datei in der Web-UI herunterladen); Musterlösung nur Tutor.
            if task.upload_template:
                out["upload_template"] = task.upload_template
            if tutor and task.upload_solution_file:
                out["upload_solution_file"] = task.upload_solution_file
            if tutor:
                out["hint"] = (
                    "Datei-Inhalt lesen: read_upload_task_file(course_id, task_id, kind)"
                )
        if tutor and task.model_solution:
            out["model_solution"] = task.model_solution
        if task.task_type.value == "mc" and task.mc_data:
            try:
                data = parse_mc_data(task.mc_data)
                out["mc_questions"] = (
                    tutor_view(data)
                    if tutor
                    else student_view(data, task_id=task.id, user_id=user.id)
                )
            except McValidationError:
                pass
        return _json(out)


@mcp_tool
async def search_references(course_id: int, query: str, ctx: Context) -> str:
    """Quellen-Bibliothek durchsuchen (Key, Titel, Autoren, Venue, Beschreibung).

    Liefert Zitat + Kurzbeschreibung je Treffer. Vollständige Liste:
    list_references."""
    with Session(engine) as session:
        _user, _token, _role = _auth(ctx, session, course_id, tool_name="search_references")
        if not (query or "").strip():
            raise McpToolError("Leere Suchanfrage.")
        course = session.get(Course, course_id)
        if not course:
            raise McpToolError(f"Kurs {course_id} nicht gefunden (siehe list_courses).")
        refs = session.exec(
            select(CourseReference).where(CourseReference.course_id == course_id)
        ).all()
        hits = []
        for r in sorted(refs, key=lambda x: x.display_order):
            authors = ", ".join(r.authors or [])
            haystack = f"{r.key} {authors} {r.title} {r.venue} {r.detail} {r.description}"
            if not _ref_match(haystack, query):
                continue
            citation = f"{authors} ({r.year}): {r.title}."
            if r.venue:
                citation += f" {r.venue}."
            hits.append({
                "key": r.key,
                "citation": citation,
                "description": r.description,
                "url": r.url or None,
                "doi": r.doi or None,
            })
        return _json({
            "course": course.name,
            "query": query,
            "hits": hits,
            "hint": "Zitieren im Kurs-Markdown mit @cite:shannon1948 (key ohne Klammern)." if hits else None,
        })


# ─── Quellen-Tools (Quellen-Bibliothek, Zitation per @cite:{key}) ──

_REF_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")  # = api/references.py


def _ref_dict(r: CourseReference) -> dict:
    return {
        "id": r.id,
        "key": r.key,
        "entry_type": r.entry_type,
        "authors": r.authors or [],
        "title": r.title or "",
        "year": r.year or "",
        "venue": r.venue or "",
        "detail": r.detail or "",
        "address": r.address or "",
        "doi": r.doi or "",
        "url": r.url or "",
        "note": r.note or "",
        "description": r.description or "",
        "citation": bib.format_entry(
            r.authors or [], r.title or "", r.year or "",
            r.venue or "", r.detail or "", r.doi or "", r.url or "",
        ),
        "cite_as": f"@cite:{r.key}",
    }


def _next_ref_order(session: Session, course_id: int) -> int:
    m = session.exec(
        select(func.max(CourseReference.display_order))
        .where(CourseReference.course_id == course_id)
    ).one()
    return (m or 0) + 1


def _resolve_reference(session: Session, course_id: int, ref: Union[int, str]) -> CourseReference:
    """ref = ID (int) oder exakter Quellen-Key (case-insensitive)."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        r = session.get(CourseReference, int(ref))
        if not r or r.course_id != course_id:
            raise McpToolError(
                f"Quelle mit ID {ref} nicht in diesem Kurs gefunden "
                "(Liste: list_references)."
            )
        return r
    rows = session.exec(
        select(CourseReference)
        .where(CourseReference.course_id == course_id)
        .where(func.lower(CourseReference.key) == (ref or "").strip().lower())
    ).all()
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise McpToolError(
            f"Quelle mit Key '{ref}' nicht gefunden (Liste: list_references)."
        )
    names = "; ".join(f"{r.key} (ID {r.id})" for r in rows)
    raise McpToolError(f"Key '{ref}' ist mehrdeutig: {names}. Bitte per ID aufrufen.")


def _set_ref_fields(
    ref: CourseReference,
    *,
    key: Optional[str] = None,
    title: Optional[str] = None,
    authors: Optional[Union[list, str]] = None,
    entry_type: Optional[str] = None,
    year: Optional[str] = None,
    venue: Optional[str] = None,
    detail: Optional[str] = None,
    address: Optional[str] = None,
    doi: Optional[str] = None,
    url: Optional[str] = None,
    note: Optional[str] = None,
    description: Optional[str] = None,
) -> None:
    """Setzt die angegebenen Quellen-Felder (None = unberührt, '' = leeren);
    Trimming/Längen analog api/references.py."""
    if key is not None:
        ref.key = key.strip()
    if entry_type is not None:
        ref.entry_type = (entry_type or "misc").strip()[:50] or "misc"
    if authors is not None:
        if isinstance(authors, str):
            ref.authors = bib.split_authors(authors)
        else:
            ref.authors = [str(a).strip() for a in authors if str(a).strip()]
    if title is not None:
        ref.title = title.strip()[:500]
    if year is not None:
        ref.year = year.strip()[:10]
    if venue is not None:
        ref.venue = venue.strip()[:300]
    if detail is not None:
        ref.detail = detail.strip()[:200]
    if address is not None:
        ref.address = address.strip()[:200]
    if doi is not None:
        ref.doi = doi.strip()[:200]
    if url is not None:
        ref.url = url.strip()[:500]
    if note is not None:
        ref.note = note.strip()[:500]
    if description is not None:
        ref.description = description.strip()


@mcp_tool
async def list_references(course_id: int, ctx: Context) -> str:
    """Alle Quellen (Quellen-Bibliothek) des Kurses in Zitations-Reihenfolge.

    key = BibTeX-Schlüssel — Zitieren im Kurs-Markdown mit @cite:shannon1948
    (key ohne Klammern; Varianten @citet:/@citep: — Details: markdown_manual). description =
    Inhalt/Kernpunkte der Quelle (wichtig für korrekte Zitation). Anlegen:
    create_reference / import_bibtex; durchsuchen: search_references."""
    with Session(engine) as session:
        _user, _token, role = _auth(ctx, session, course_id, tool_name="list_references")
        course = session.get(Course, course_id)
        if not course:
            raise McpToolError(f"Kurs {course_id} nicht gefunden (siehe list_courses).")
        refs = session.exec(
            select(CourseReference).where(CourseReference.course_id == course_id)
        ).all()
        refs.sort(key=lambda r: (r.display_order, r.id))
        hint = None
        if refs:
            hint = "Zitieren im Kurs-Markdown mit @cite:shannon1948 (key ohne Klammern)."
            if _is_tutor(role):
                hint += " Anlegen: create_reference / import_bibtex; ändern: edit_reference."
        return _json({
            "course": course.name,
            "references": [_ref_dict(r) for r in refs],
            "hint": hint,
        })


@mcp_tool
async def create_reference(
    course_id: int,
    key: str,
    title: str,
    ctx: Context,
    authors: Optional[Union[list, str]] = None,
    entry_type: str = "misc",
    year: Optional[str] = None,
    venue: Optional[str] = None,
    detail: Optional[str] = None,
    address: Optional[str] = None,
    doi: Optional[str] = None,
    url: Optional[str] = None,
    note: Optional[str] = None,
    description: Optional[str] = None,
    version_label: Optional[str] = None,
) -> str:
    """Legt eine neue Quelle in der Kurs-Quellen-Bibliothek an (Write).

    key = BibTeX-Schlüssel (Pflicht; nur Buchstaben, Ziffern, '_' und '-',
    z. B. 'smith2021deep'), einzig pro Kurs — danach mit @cite:smith2021deep
    (key ohne Klammern) in Skript/Folien/Aufgaben zitierbar. Mehrere Quellen auf einmal:
    import_bibtex (rohes BibTeX). authors: Liste von Namen ODER String mit
    ' and ' als Trenner (BibTeX-Konvention, z. B. 'Smith, J. and Doe, A.').
    entry_type: article/book/inproceedings/… (Default 'misc').
    venue: Zeitschrift / Konferenz / Verlag. detail: Band(Heft), Seiten,
    Edition. description (empfohlen): 1–2 Sätze zum Inhalt der Quelle —
    Skript/Aufgaben nutzen sie, um die Quelle korrekt zu zitieren.
    Die neue Quelle erhält die nächste Zitations-Nummer. version_label:
    optionales kurzes Label für die Initial-Version (Default „Erstellt")."""
    with Session(engine) as session:
        user, _token, _role = _auth(ctx, session, course_id, write=True, tool_name="create_reference")
        k = (key or "").strip()
        if not _REF_KEY_RE.fullmatch(k):
            raise McpToolError(
                "Ungültiger Quellen-Key (erlaubt: Buchstaben, Ziffern, '_' und '-')."
            )
        if not (title or "").strip():
            raise McpToolError("Der Titel darf nicht leer sein.")
        clash = session.exec(
            select(CourseReference)
            .where(CourseReference.course_id == course_id)
            .where(func.lower(CourseReference.key) == k.lower())
        ).first()
        if clash:
            raise McpToolError(
                f"Der Key '{k}' existiert bereits in diesem Kurs "
                "(Liste: list_references — ändern: edit_reference)."
            )
        ref = CourseReference(
            course_id=course_id,
            created_by=user.id,  # type: ignore[arg-type]
            display_order=_next_ref_order(session, course_id),
        )
        _set_ref_fields(ref, key=k, title=title, authors=authors, entry_type=entry_type,
                        year=year, venue=venue, detail=detail, address=address,
                        doi=doi, url=url, note=note, description=description)
        session.add(ref)
        session.commit()
        session.refresh(ref)
        version_service.mark_created(session, user, "reference", int(ref.id), name=version_label)
        return _json(_ref_dict(ref) | {
            "note": (
                f"Quelle angelegt (Zitations-Nummer {ref.display_order + 1}). "
                f"Zitieren mit @cite:{ref.key}."
            ),
        })


@mcp_tool
async def edit_reference(
    course_id: int,
    ref: Union[int, str],
    ctx: Context,
    key: Optional[str] = None,
    title: Optional[str] = None,
    authors: Optional[Union[list, str]] = None,
    entry_type: Optional[str] = None,
    year: Optional[str] = None,
    venue: Optional[str] = None,
    detail: Optional[str] = None,
    address: Optional[str] = None,
    doi: Optional[str] = None,
    url: Optional[str] = None,
    note: Optional[str] = None,
    description: Optional[str] = None,
    version_label: Optional[str] = None,
) -> str:
    """Bearbeitet eine bestehende Quelle (Write). ref = Quellen-Key
    (z. B. 'smith2021deep') oder ID (Liste: list_references).

    Alle Parameter optional — nur angegebene Felder werden geändert
    ('' = Feld leeren). ACHTUNG: key ist der Zitier-Schlüssel — ein
    Schlüssel-Wechsel bricht @cite:-Referenzen in Skript/Folien,
    bis der Inhalt angepasst ist. description = Inhalt/Kernpunkte der
    Quelle (wichtig, dass Skript/Aufgaben sie korrekt zitieren).

    Nach dem Edit wird automatisch die Version „MCP Edit“ (neuer Zustand)
    angelegt — der Zustand vorher ist die Version davor
    (Rückgängig: list_versions + restore_version). version_label: optionales
    kurzes Label für die angelegte Version (z. B. "DOI korrigiert");
    Default „MCP Edit“."""
    with Session(engine) as session:
        user, _token, _role = _auth(ctx, session, course_id, write=True, tool_name="edit_reference")
        existing = _resolve_reference(session, course_id, ref)
        params = dict(key=key, title=title, authors=authors, entry_type=entry_type,
                      year=year, venue=venue, detail=detail, address=address,
                      doi=doi, url=url, note=note, description=description)
        if all(v is None for v in params.values()):
            raise McpToolError(
                "Nichts zu ändern: mindestens ein Feld angeben (key, title, authors, "
                "entry_type, year, venue, detail, address, doi, url, note, description)."
            )
        if key is not None:
            k = key.strip()
            if not _REF_KEY_RE.fullmatch(k):
                raise McpToolError(
                    "Ungültiger Quellen-Key (erlaubt: Buchstaben, Ziffern, '_' und '-')."
                )
            if k.lower() != existing.key.lower():
                clash = session.exec(
                    select(CourseReference)
                    .where(CourseReference.course_id == course_id)
                    .where(func.lower(CourseReference.key) == k.lower())
                ).first()
                if clash and clash.id != existing.id:
                    raise McpToolError(f"Der Key '{k}' existiert bereits in diesem Kurs.")
        if title is not None and not title.strip():
            raise McpToolError("Der Titel darf nicht leer sein.")
        changed = [f for f, v in params.items() if v is not None]
        _set_ref_fields(existing, **params)
        session.add(existing)
        session.commit()
        session.refresh(existing)
        version_note = _mcp_version_note(session, user, "reference", int(existing.id), version_label)
        return _json(_ref_dict(existing) | {
            "changed": changed,
            "note": version_note,
        })


@mcp_tool
async def import_bibtex(course_id: int, bibtex: str, ctx: Context,
                        version_label: Optional[str] = None) -> str:
    """Importiert mehrere Quellen auf einmal aus rohem BibTeX (Write).

    bibtex = BibTeX-Text mit einem oder mehreren @-Entries (z. B.
    '@article{smith2021deep, author = {Smith, J.}, title = {…}, …}').
    Vorhandene Keys werden übersprungen (NICHT überschrieben). description
    (Inhalt/Kernpunkte) bleibt leer — bei Bedarf danach per edit_reference
    ergänzen. Einzelne Quelle: create_reference. version_label: optionales
    kurzes Label für die Initial-Version aller neu angelegten Quellen
    (Default „Erstellt")."""
    with Session(engine) as session:
        user, _token, _role = _auth(ctx, session, course_id, write=True, tool_name="import_bibtex")
        entries = bib.parse_bibtex(bibtex or "")
        if not entries:
            raise McpToolError(
                "Keine BibTeX-Entries erkannt (erwartet: @article{key, …} o. ä.)."
            )
        existing_keys = {
            (r.key or "").lower()
            for r in session.exec(
                select(CourseReference).where(CourseReference.course_id == course_id)
            ).all()
        }
        order = _next_ref_order(session, course_id)
        imported, skipped, errors, created_refs = [], [], [], []
        for e in entries:
            k = e["key"]
            if not _REF_KEY_RE.fullmatch(k):
                errors.append(
                    f"'{k}': ungültiger Key (erlaubt: Buchstaben, Ziffern, '_' und '-')"
                )
                continue
            if k.lower() in existing_keys:
                skipped.append(k)
                continue
            ref = CourseReference(
                course_id=course_id,
                created_by=user.id,  # type: ignore[arg-type]
                key=k,
                display_order=order,
                **bib.entry_to_reference(e),
            )
            session.add(ref)
            existing_keys.add(k.lower())
            imported.append(k)
            created_refs.append(ref)
            order += 1
        session.commit()
        for r in created_refs:
            version_service.mark_created(session, user, "reference", int(r.id), name=version_label)
        return _json({
            "imported": imported,
            "skipped": skipped,
            "errors": errors,
            "note": (
                "Neue Quellen sind per @cite: zitierbar (key ohne Klammern, z. B. "
                "@cite:smith2021deep); description (Inhalt) fehlt — bei Bedarf per "
                "edit_reference ergänzen."
            ),
        })


@mcp_tool
async def list_media(course_id: int, ctx: Context, kind: Optional[str] = None, limit: int = 50) -> str:
    """Alle Medien (Bilder + Videos + Applets) des Kurses — mit FERTIGER
    Markdown-Referenz zum Einbinden in Skript/Folien:

        ![<Titel>](<url>)   (Applets zusätzlich: {height=X} / {zoom=X})

    kind (optional): "image" (nur Bilder) | "applet" (nur Applets);
    weggelassen = alles. description = Kurzbeschreibung, was das Medium
    zeigt. Bild-Inhalt ansehen: read_image. Neues Bild: create_image.
    Syntax/Einbettungsregeln: markdown_manual("script"|"slides")."""
    with Session(engine) as session:
        _user, _token, _role = _auth(ctx, session, course_id, tool_name="list_media")
        course = session.get(Course, course_id)
        if not course:
            raise McpToolError(f"Kurs {course_id} nicht gefunden (siehe list_courses).")
        if kind not in (None, "image", "applet"):
            raise McpToolError("kind muss 'image', 'applet' oder weggelassen sein.")
        rows = session.exec(
            select(CourseMedia).where(CourseMedia.course_id == course_id)
        ).all()
        rows = [m for m in rows if kind is None or m.media_type == kind]
        # Bilder zuerst, dann Applets, jeweils in Anlegereihenfolge.
        rows.sort(key=lambda m: (m.media_type != "image", m.created_at))
        lim = max(1, min(int(limit), 200))
        entries = []
        for m in rows[:lim]:
            url = media_service.media_url(m)
            entries.append({
                "id": m.id,
                "title": m.title,
                "type": m.media_type,
                "reference": f"![{m.title}]({url})",
                "description": m.llm_description or "",
                "size_kb": round(m.file_size / 1024, 1),
            })
        return _json({
            "course": course.name,
            "media": entries,
            "total": len(rows),
            "hint": (
                "Zum Einbinden den 'reference'-Wert in den Inhalt kopieren "
                "(Größe/Zoom: {height=X} / {zoom=X} danach). Bild ansehen: "
                "read_image. Neue Applets: create_applet, neue "
                "Bilder/Videos: create_image. Einbettungsregeln: markdown_manual."
            ) if entries else None,
        })


# ─── Bild-Tools ─────────────────────────────────────────────

MAX_IMAGE_READ_BYTES = 2 * 1024 * 1024  # Cap für read_image (Base64-Block)

_DATA_URL_RE = re.compile(r"^data:[\w/+.-]+;base64,([A-Za-z0-9+/=]+)\Z")
_IMAGE_MAGIC: list[tuple[bytes, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", ".png", "png"),
    (b"\xff\xd8\xff", ".jpg", "jpeg"),
    (b"GIF87a", ".gif", "gif"),
    (b"GIF89a", ".gif", "gif"),
]


def _detect_image(data: bytes) -> tuple[str, str]:
    """Erkennt das Bildformat per Magic-Bytes → (extension, format).

    Die Extension allein wird NICHT vertraut (Upload/URL dürfen die
    wahre Datei-Typus tarnen)."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "webp"
    for magic, ext, fmt in _IMAGE_MAGIC:
        if data.startswith(magic):
            return ext, fmt
    raise McpToolError("Ungültiges Bildformat. Erlaubt: PNG, JPEG, WebP, GIF.")


def _decode_data_url(source: str) -> bytes:
    """'data:<mime>;base64, …' → Bytes."""
    m = _DATA_URL_RE.match(source.strip())
    if not m:
        raise McpToolError("source muss eine Data-URL (data:image/png;base64,…) sein.")
    try:
        return base64.b64decode(m.group(1))
    except ValueError:
        raise McpToolError("Ungültiges Base64 in der Data-URL.")


# Wikimedia-Datei-URLs (Pfad /wikipedia/<lang>/… oder /wikimedia/<projekt>/…):
# LLMs raten die Hash-Pfade und Thumbnail-Breiten häufig falsch (→ 404/400),
# daher wird die Datei NACH NAME über die MediaWiki-API aufgelöst (kanonische
# URL; große Bilder kommen als Thumbnail mit erlaubter Breite zurück, SVGs
# als ORIGINALE Vektor-Quelle — die thumburl wäre eine rasterisierte PNG).
_WIKIMEDIA_PROJECTS = {
    "commons", "meta", "species", "wikibooks", "wikinews",
    "wikiquote", "wikisource", "wikiversity", "wikidata",
}
_WM_DOWNLOAD_HOSTS = ("upload.wikimedia.org", "thumb.wikimedia.org")

# User-Agent für Bild-Downloads: viele Hosts (z. B. Wikimedia) blockieren
# Standard-Client-User-Agents (python-httpx/…) mit 403.
_UA_IMAGE_DOWNLOAD = (
    f"AICampus/1.0 (+https://{PUBLIC_DOMAIN})" if PUBLIC_DOMAIN else "AICampus/1.0"
)


def _wikimedia_file_ref(url: httpx.URL) -> Optional[tuple[str, str]]:
    """(api_host, dateiname) für Wikimedia-Datei-URLs, sonst None.

    Pfade: /wikipedia/<lang>/[thumb/]<x>/<y>/<name>[/Wpx-…] oder
           /wikimedia/<projekt>/… (commons, meta, …)."""
    if url.host not in _WM_DOWNLOAD_HOSTS:
        return None
    parts = [p for p in url.path.split("/") if p]
    if len(parts) < 5 or parts[0] not in ("wikipedia", "wikimedia"):
        return None
    wiki = parts[1].lower()
    if wiki in _WIKIMEDIA_PROJECTS:
        api_host = f"{wiki}.wikimedia.org"
    elif parts[0] == "wikipedia":
        api_host = f"{wiki}.wikipedia.org"
    else:
        return None
    # Original-Dateiname: bei Thumbnails das Segment VOR „<Wpx-…“,
    # sonst das letzte Pfad-Segment.
    if parts[2] == "thumb" and len(parts) >= 7:
        name = parts[5]
    else:
        name = parts[-1]
    return api_host, unquote(name)


async def _resolve_wikimedia_url(client: httpx.AsyncClient, api_host: str, filename: str) -> httpx.URL:
    """Kanonische (Thumbnail-)URL einer Datei per MediaWiki-API auflösen."""
    resp = await client.get(
        f"https://{api_host}/w/api.php",
        params={
            "action": "query",
            "titles": f"File:{filename}",
            "prop": "imageinfo",
            "iiprop": "url|mime",
            "iiurlwidth": "1600",
            "format": "json",
        },
    )
    if resp.status_code != 200:
        raise McpToolError(f"MediaWiki-API ({api_host}) Fehler (HTTP {resp.status_code}).")
    try:
        data = resp.json()
    except ValueError:
        raise McpToolError(f"MediaWiki-API ({api_host}): ungültige Antwort.")
    pages = data.get("query", {}).get("pages") or {}
    page = next(iter(pages.values()), {})
    info = (page.get("imageinfo") or [{}])[0]
    # SVG: Original-Vektor-Quelle (die thumburl wäre eine rasterisierte PNG);
    # Videos: Original-Datei (die thumburl wäre nur ein JPEG-Frame).
    wm_mime = info.get("mime") or ""
    if wm_mime == "image/svg+xml" or wm_mime.startswith("video/"):
        target = info.get("url")
    else:
        target = info.get("thumburl") or info.get("url")
    if not target:
        raise McpToolError(
            f"Datei 'File:{filename}' existiert nicht auf {api_host} — Dateiname prüfen."
        )
    parsed = httpx.URL(target)
    if parsed.host not in _WM_DOWNLOAD_HOSTS:
        raise McpToolError("Unerwartete URL von der MediaWiki-API (abgelehnt).")
    return parsed


async def _download_image_bytes(client: httpx.AsyncClient, url: httpx.URL, max_bytes: int) -> bytes:
    """Stream-Download mit Größen-Limit (keine Redirects)."""
    chunks: list[bytes] = []
    total = 0
    async with client.stream("GET", url) as resp:
        if resp.status_code >= 300:
            extra = " (Redirects werden nicht gefolgt)" if resp.status_code < 400 else ""
            raise McpToolError(f"Download fehlgeschlagen (HTTP {resp.status_code}){extra}.")
        async for chunk in resp.aiter_bytes(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise McpToolError(f"Datei zu groß (max. {max_bytes // (1024 * 1024)} MB).")
            chunks.append(chunk)
    return b"".join(chunks)


async def _fetch_file_bytes(url_str: str, max_bytes: int, timeout: float = 20.0) -> bytes:
    """Lädt eine Datei von einer öffentlichen http(s)-URL.

    Sicherheit: keine Redirects, keine lokalen/internen Ziel-Adressen
    (SSRF-Schutz via DNS-Auflösung vor dem Download)."""
    url = httpx.URL(url_str.strip())
    if url.scheme not in ("http", "https") or not url.host:
        raise McpToolError("Die URL muss http(s) sein.")
    try:
        infos = socket.getaddrinfo(url.host, None)
    except (socket.gaierror, OSError):
        raise McpToolError(f"Hostname '{url.host}' konnte nicht aufgelöst werden.")
    for _fam, _ty, _pr, _cn, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise McpToolError("Der Download von lokalen/internen Adressen ist nicht erlaubt.")
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            headers={"User-Agent": _UA_IMAGE_DOWNLOAD},
        ) as client:
            return await _download_image_bytes(client, url, max_bytes)
    except McpToolError:
        raise
    except (httpx.HTTPError, OSError):
        raise McpToolError("Download fehlgeschlagen (Netzwerk-Fehler).")


async def _fetch_image_bytes(url_str: str, max_bytes: int = media_service.MAX_UPLOAD_BYTES,
                             timeout: float = 20.0) -> bytes:
    """Lädt eine Datei (Bild/Video) von einer öffentlichen http(s)-URL.

    Sicherheit: keine Redirects, keine lokalen/internen Ziel-Adressen
    (SSRF-Schutz via DNS-Auflösung vor dem Download).
    User-Agent: viele Hosts (z. B. Wikimedia) blockieren Standard-Client-User-Agents
    (python-httpx/…) mit 403 — daher eigener, beschreibender UA.
    Wikimedia-URLs werden per MediaWiki-API nach Dateiname aufgelöst
    (s. _wikimedia_file_ref)."""
    url = httpx.URL(url_str.strip())
    if url.scheme not in ("http", "https") or not url.host:
        raise McpToolError("source muss eine http(s)-URL sein.")
    try:
        infos = socket.getaddrinfo(url.host, None)
    except (socket.gaierror, OSError):
        raise McpToolError(f"Hostname '{url.host}' konnte nicht aufgelöst werden.")
    for _fam, _ty, _pr, _cn, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise McpToolError("Der Download von lokalen/internen Adressen ist nicht erlaubt.")
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            headers={"User-Agent": _UA_IMAGE_DOWNLOAD},
        ) as client:
            wm = _wikimedia_file_ref(url)
            if wm:
                url = await _resolve_wikimedia_url(client, *wm)
            return await _download_image_bytes(client, url, max_bytes)
    except McpToolError:
        raise
    except (httpx.HTTPError, OSError):
        raise McpToolError("Download fehlgeschlagen (Netzwerk-Fehler).")


@mcp_tool
async def read_image(course_id: int, ref: Union[int, str], ctx: Context) -> list[TextContent | Image]:
    """Zeigt ein Bild-Medium des Kurses (das BILD selbst + Metadaten).

    ref = Medium-ID (aus list_media) oder exakter Titel. Liefert das Bild
    zum Betrachten/Verstehen plus Metadaten (Titel, Beschreibung und die
    fertige Markdown-Referenz zum Einbinden in Skript/Folien)."""
    with Session(engine) as session:
        _user, _token, _role = _auth(ctx, session, course_id, tool_name="read_image")
        media = _resolve(session, CourseMedia, course_id, ref, "Bild")
        if media.media_type != "image":
            raise McpToolError(f"Medium '{media.title}' ist kein Bild (für Applets: read_applet).")
        path = media_service.resolve_media_path(course_id, media.file_path.rsplit("/", 1)[-1])
        if not path:
            raise McpToolError("Bild-Datei fehlt (Element defekt).")
        size = path.stat().st_size
        if size > MAX_IMAGE_READ_BYTES:
            raise McpToolError(
                f"Bild zu groß zum Anzeigen ({size // 1024} KB > {MAX_IMAGE_READ_BYTES // 1024} KB) — "
                "Metadaten liefert list_media."
            )
        meta = (
            f"Medium {media.id}: {media.title}\n"
            f"Typ: {media.mime_type} ({size // 1024} KB)"
            + (f"\nBeschreibung: {media.llm_description}" if media.llm_description else "")
            + f"\nMarkdown-Referenz: ![{media.title}]({media_service.media_url(media)})"
        )
        return [
            TextContent(type="text", text=meta),
            Image(data=path.read_bytes(), format=media.mime_type.split("/", 1)[1]),
        ]


# ─── Markdown-Manual + Write-Tools (Tutor/Prof) ────────────

APPLET_MANUAL = """\
Applets = SELF-CONTAINED-HTML- oder SVG-Dokumente, die im Kurs-Markdown
wie Bilder eingebunden werden:

    ![Titel](/media/<course_id>/<datei>)   (z. B. ![](…/course_3/ab12.html)
                                           oder ![](…/course_3/ab12.svg))

Größe steuern direkt nach dem Snippet: {height=X} = Max-Höhe in Pixeln
(HTML und SVG), {zoom=X} = Zoom-Faktor des Applet-Inhalts (nur HTML).

Format "html" — interaktives Applet, läuft in einem sandboxed Iframe
(nur allow-scripts):
- KOMPLETTES HTML-DOKUMENT: <!DOCTYPE html> … </html>, ALLES CSS in einem
  <style>-Block im <head>, ALLES JS in <script>-Blöcken im <body>.
  Keine ES-Module (kein type="module"), keine Import-Maps.
- NUR Vanilla JS, Canvas, SVG, CSS plus die LOKAL gespiegelten Libs (KEIN CDN):
  - Chart.js:  <script src="/static/vendor/chart.umd.js"></script>
  - Plotly:    <script src="/static/vendor/plotly.min.js"></script>
  - Three.js:  <script src="/static/vendor/three.min.js"></script> und
               <script src="/static/vendor/three.OrbitControls.js"></script>
  - KaTeX:     <link rel="stylesheet" href="/static/vendor/katex/katex.min.css"></link> und
               <script src="/static/vendor/katex/katex.min.js"></script>
- Keine anderen externen Ressourcen (keine weiteren CDN-Links, kein
  fetch/XHR, keine Bilder von URLs). Bei Plotly keine mapbox/map-Traces.
- KEIN localStorage/sessionStorage/Cookies — Zustand nur in JS-Variablen.
- Kein Webcam-/Mikrofon-Zugriff; bei Device-Input stattdessen
  <input type="file"> + URL.createObjectURL().
- Mathematische Formeln: KaTeX (katex.renderToString("…", {displayMode: true})),
  niemals rohen LaTeX-Quelltext anzeigen.

Design (HTML-Applets werden rahmenlos mit transparentem Hintergrund eingebettet):
- PLATZSPAREND: keine Karten/großes Padding am Root (≤ 8 px), das Haupt-
  Visualisierungsfenster reicht bis an die Ränder; Steuerungen als kompaktes
  Overlay oder schmale Leiste (1–2 Zeilen), keine großen Bedienbereiche.
- Kompakte Typografie (12–14 px); responsiv 100 % Breite, muss auch in
  ~300x150 px gut aussehen (ResizeObserver bzw. resize-Listener, KEINE festen
  Pixelmaße für das Root-Layout).
- Höhe ist inhaltsgetrieben (KEIN 100vh/vh oder min-height am Root); Canvas-/3D-/
  Chart-Bereiche bekommen eine feste Pixelhöhe (300–450 px), statische SVGs
  ihre Höhe über das Seitenverhältnis (viewBox).
- Lehrbuch-Qualität: präzise Beschriftungen, Legenden, sinnvolle Defaults,
  flüssige Interaktion (Slider, Buttons, Hover-Tooltips; Three.js: OrbitControls).
- Wichtige Elemente bekommen einen eigenen Hintergrund, damit der Skript-/Folien-
  Hintergrund nicht störend durchscheint.
- requestAnimationFrame für Animationen (keine Busy-Loops); Chart.js:
  maintainAspectRatio: false, Plotly: {responsive: true}.

Format "svg" — statische ODER animierte Vektorgrafik, rendert als <img>
(KEIN JavaScript, keine Interaktion — nur SMIL/CSS-Animation):
- ROTES <svg>-Element als Wurzel (KEIN HTML-Dokument), mit
  xmlns="http://www.w3.org/2000/svg" und viewBox (z. B. viewBox="0 0 800 600");
  width/height weglassen (Größe über Seitenverhältnis, responsiv).
- KEIN <script>, KEINE Event-Handler (onclick="…"), KEIN <foreignObject>,
  KEIN iframe/object/embed, KEINE externen Referenzen (keine http(s)-URLs,
  keine data:text/… in href/src).
- Animation: SMIL (<animate>, <animateTransform>, <set>) oder CSS
  @keyframes in einem <style>-Block IM SVG.
- Formeln als <text>/<tspan> (kein LaTeX-Rendering — vereinfacht als Text mit
  Unicode-Symbolen, z. B. H(X) = −Σ p(x)·log₂ p(x)).
- Braucht die Figur Interaktion (Slider, Buttons, …)? Dann Format "html"
  statt "svg" wählen.

Stellenweise Änderungen an BESTEHENDEN Applets: „media_edits“ — Liste von
Edit-Objekten {"op": "replace_span", "old": "…", "new": "…"}, wobei "old" exakt
einmal im bestehenden Code vorkommen muss (s. Tool edit_applet).

Formatwechsel (html↔svg): edit_applet mit "format" — die Datei wird
umbenannt und alle Markdown-Referenzen repariert. Der Code muss im selben
Aufruf konvertiert werden: "media" mit dem umgewandelten Code im NEUEN
Format anliefern (z. B. möglichst getreue Umwandlung HTML → SVG).
"""


def _apply_html_edits(html: str, edits: object) -> str:
    """Stellenweise Edits am Applet-Code (HTML/SVG) — nur replace_span mit
eindeutig vorkommendem Anker (Analogie: services/content_edits.py)."""
    if not isinstance(edits, list) or not edits:
        raise McpToolError("'media_edits' muss eine (nicht-leere) Liste von Edit-Objekten sein.")
    spans: list[tuple[int, int, str]] = []
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict) or str(edit.get("op") or "").strip() != "replace_span":
            raise McpToolError(f"Edit {i + 1} ungültig: es wird nur \"op\": \"replace_span\" unterstützt.")
        try:
            s, e = _resolve_span(html, str(edit.get("old") or ""))
        except ContentEditError as err:
            raise McpToolError(str(err))
        spans.append((s, e, str(edit.get("new") or "")))
    for a in range(len(spans)):
        for b in range(a + 1, len(spans)):
            a1, a2, _ = spans[a]
            b1, b2, _ = spans[b]
            if a1 == a2 or b1 == b2:
                p = a1 if a1 == a2 else b1
                lo, hi = (b1, b2) if a1 == a2 else (a1, a2)
                if lo < p < hi:
                    raise McpToolError(f"Edit {a + 1} überschneidet sich mit Edit {b + 1}.")
            elif a1 < b2 and b1 < a2:
                raise McpToolError(f"Edit {a + 1} überschneidet sich mit Edit {b + 1}.")
    spans.sort(key=lambda sp: (sp[0], sp[1]))
    parts: list[str] = []
    pos = 0
    for s, e, repl in spans:
        parts.append(html[pos:s])
        parts.append(repl)
        pos = e
    parts.append(html[pos:])
    return "".join(parts)


def _resolve_editable(session: Session, course_id: int, ref: Union[int, str], type: str):
    """type + ref auflösen → Element (inkl. Typ-Check)."""
    if type == "script_section":
        return _resolve(session, ScriptSection, course_id, ref, "Skript-Kapitel")
    if type == "slide_deck":
        m = _resolve(session, CourseMaterial, course_id, ref, "Slide-Deck")
        if m.material_type != MaterialType.SLIDES:
            raise McpToolError("Dieses Material ist kein Slide-Deck (MCP-Edits: nur Slide-Decks).")
        return m
    if type == "applet":
        m = _resolve(session, CourseMedia, course_id, ref, "Applet")
        if m.media_type != "applet":
            raise McpToolError("Dieses Medium ist kein Applet.")
        return m
    if type == "task":
        return _resolve(session, Task, course_id, ref, "Aufgabe")
    if type == "reference":
        return _resolve_reference(session, course_id, ref)
    raise McpToolError(
        "type muss 'script_section', 'slide_deck', 'applet', 'task' oder "
        "'reference' sein."
    )


@mcp_tool
async def markdown_manual(ctx: Context, kind: str = "script") -> str:
    """AICampus-Formatierung für Kursinhalte — VOR dem Anlegen/Bearbeiten
    von Skript-, Folien-, Applet- oder Aufgaben-Inhalten aufrufen.

    kind: "script" (Skript-Kapitel; GILT AUCH FÜR AUFGABENTEXT: Aufgabe/
    Template/Musterlösung) | "slides" (Slide-Decks) | "applet" (Applets:
    HTML/SVG)."""
    with Session(engine) as session:
        _auth(ctx, session, None, tool_name="markdown_manual")
        if kind == "slides":
            return SLIDES_MARKDOWN_MANUAL + "\n\nStellenweise Edits (\u201econtent_edits\u201c):\n" + SLIDES_CONTENT_EDITS_SPEC
        if kind == "applet":
            return APPLET_MANUAL
        if kind != "script":
            raise McpToolError("kind muss 'script', 'slides' oder 'applet' sein.")
        return SCRIPT_MARKDOWN_MANUAL + "\n\nStellenweise Edits (\u201econtent_edits\u201c):\n" + SCRIPT_CONTENT_EDITS_SPEC


@mcp_tool
async def read_applet(course_id: int, ref: Union[int, str], ctx: Context) -> str:
    """Applet-Quelle (HTML oder SVG; "format" sagt welches). ref = Applet-ID
    oder exakter Titel (Liste: course_overview → applets).

    Nur für Tutor/Prof: Applet-Quellen sind kursleiterorientiert.
    Einbindung im Kurs-Markdown s. markdown_manual("applet")."""
    with Session(engine) as session:
        _user, _token, role = _auth(ctx, session, course_id, tool_name="read_applet")
        if not _is_tutor(role):
            raise McpToolError("Applet-Quellen sind nur mit der Rolle Tutor oder Professor lesbar.")
        media = _resolve(session, CourseMedia, course_id, ref, "Applet")
        if media.media_type != "applet":
            raise McpToolError(f"Medium '{media.title}' ist kein Applet.")
        path = media_service.MEDIA_DIR / media.file_path
        if not path.is_file():
            raise McpToolError("Applet-Datei fehlt (Element defekt).")
        return _json({
            "id": media.id,
            "course_id": course_id,
            "title": media.title,
            "llm_description": media.llm_description or "",
            "url": media_service.media_url(media),
            "format": media_service.applet_format_of(media.file_path),
            "media": path.read_text(encoding="utf-8"),
        })


@mcp_tool
async def edit_content(
    course_id: int,
    type: str,
    ref: Union[int, str],
    ctx: Context,
    edits: Optional[list[dict]] = None,
    content: Optional[str] = None,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    version_label: Optional[str] = None,
) -> str:
    """Bearbeitet ein Skript-Kapitel (type="script_section") oder Slide-Deck
    (type="slide_deck"). ref = ID oder exakter Titel (Liste: course_overview).
    Inhalte zuerst mit read_script_section / read_material lesen; Syntax des
    neuen Contents: markdown_manual("script"|"slides"). Neue/änderte
    Querverweise: NUR Labels/IDs, die tatsächlich existieren (@fig:/@eq:/
    @sec:/…, Aufgaben: @task:243 = ID ohne Klammern) — aus dem gelesenen
    Inhalt bzw. course_overview.

    Genau EINE Content-Änderung angeben:
    - edits: stellenweise Edits (unabhängig angewendet; fehlgeschlagene
      Edits werden als Warnung gemeldet)
    - content: KOMPLETTER neuer Inhalt (Ersatz; große Überarbeitungen)
    … oder NUR title/summary, wenn nur Metadaten geändert werden sollen.

    title: neuer Titel (optional). summary: neue Kurzfassung (optional;
    "" leeren String = zurücksetzen). Diese interne LLM-Zusammenfassung ist
    NICHT für Studenten sichtbar (Dienlich: Konsistenz zwischen Kapiteln).

    Nach dem Edit wird automatisch die Version „MCP Edit“ (neuer Zustand)
    angelegt — der Zustand vorher ist die Version davor
    (Rückgängig: list_versions + restore_version). version_label: optionales
    kurzes Label für die angelegte Version (z. B. "Gleichung 3 korrigiert");
    Default „MCP Edit“.

    Edits (script_section) — "heading" = EXISTIERENDE Heading, wortgleich
    (Überschriften tragen im Quelltext KEINE Nummern — die werden beim
    Rendern automatisch vorangestellt):
    - {"op": "replace_section", "heading": "### Beispiel", "content": "…"}
    - {"op": "insert_after", "heading": "## Grundlagen", "content": "…"}
    - {"op": "delete_section", "heading": "### Altes Beispiel"}
    - {"op": "replace_span", "old": "…", "new": "…"}

    Edits (slide_deck) — "slide" = Foliennummer (1. Folie = 1):
    - {"op": "replace_slide", "slide": 3, "content": "…"}
    - {"op": "insert_slide_after", "slide": 3, "content": "…"}   (0 = als erste Folie)
    - {"op": "delete_slide", "slide": 4}
    - {"op": "replace_span", "old": "…", "new": "…"}   (innerhalb EINER Folie,
      "old" darf keinen Trenner "---"/"--" enthalten)
    """
    with Session(engine) as session:
        user, _token, _role = _auth(ctx, session, course_id, write=True, tool_name="edit_content")
        if type not in ("script_section", "slide_deck"):
            raise McpToolError("type muss 'script_section' oder 'slide_deck' sein.")
        full_rewrite = bool(content)  # ("" = nicht angegeben, s. Docstring)
        partial = isinstance(edits, list) and bool(edits)
        if full_rewrite and partial:
            raise McpToolError(
                "Genau EINE Content-Änderung angeben: 'edits' (stellenweise) "
                "ODER 'content' (kompletter Ersatz) — oder NUR title/summary."
            )
        if title is None and summary is None and not (full_rewrite or partial):
            raise McpToolError("Nichts zu ändern: 'edits', 'content', 'title' oder 'summary' angeben.")
        if title is not None and not title.strip():
            raise McpToolError("Der Titel darf nicht leer sein.")
        if type == "script_section":
            section = _resolve(session, ScriptSection, course_id, ref, "Skript-Kapitel")
            warnings = None
            if full_rewrite:
                new_content, applied = content, None
            elif partial:
                try:
                    new_content, applied, warnings = apply_content_edits(section.content or "", edits)
                except ContentEditError as e:
                    raise McpToolError(str(e))
            else:  # nur Metadaten
                new_content, applied = section.content or "", 0
            if full_rewrite or partial:
                section.content = new_content
            if title is not None:
                section.title = title.strip()
            if summary is not None:
                section.summary = summary
            section.updated_at = datetime.now(timezone.utc)
            session.add(section)
            session.commit()
            sync_media_usages(session, course_id)
            version_note = _mcp_version_note(session, user, "script_section", int(section.id), version_label)
            return _json({
                "id": section.id,
                "title": section.title,
                "changed": (
                    "kompletter Content-Ersatz" if full_rewrite
                    else f"{applied} Edit(s) angewendet" if partial
                    else "nur Metadaten ge\u00e4ndert"
                ),
                "content": section.content,
                "warnings": warnings,
                "note": version_note,
            })
        material = _resolve(session, CourseMaterial, course_id, ref, "Slide-Deck")
        if material.material_type != MaterialType.SLIDES:
            raise McpToolError("Dieses Material ist kein Slide-Deck (MCP-Edits: nur Slide-Decks).")
        try:
            if full_rewrite:
                new_content = content
                parse_slides(new_content)
            elif partial:
                new_content = apply_slide_edits(material.content or "", edits)
                parse_slides(new_content)
            else:  # nur Metadaten
                new_content = None
        except SlideError as e:
            raise McpToolError(f"Kein g\u00fcltiges Slide-Deck: {e}")
        if new_content is not None:
            material.content = new_content
        if title is not None:
            material.title = title.strip()
        if summary is not None:
            material.summary = summary
        material.updated_at = datetime.now(timezone.utc)
        session.add(material)
        session.commit()
        sync_media_usages(session, course_id)
        version_note = _mcp_version_note(session, user, "slide_deck", int(material.id), version_label)
        return _json({
            "id": material.id,
            "title": material.title,
            "changed": (
                "kompletter Content-Ersatz" if full_rewrite
                else f"{len(edits)} Edit(s) angewendet" if partial
                else "nur Metadaten ge\u00e4ndert"
            ),
            "content": material.content,
            "note": version_note,
        })


@mcp_tool
async def edit_applet(
    course_id: int,
    ref: Union[int, str],
    ctx: Context,
    media: Optional[str] = None,
    media_edits: Optional[list[dict]] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    format: Optional[str] = None,
    version_label: Optional[str] = None,
) -> str:
    """Ändert Code, Format, Titel oder Beschreibung eines bestehenden Applets.
    ref = Applet-ID oder exakter Titel (Quelle zuerst mit read_applet lesen).

    Code: genau EINES angeben (oder weglassen, wenn nur Format/Metadaten):
    - media: kompletter neuer Code (größere Überarbeitungen)
    - media_edits: stellenweise Edits, nur {"op": "replace_span", "old": "…",
      "new": "…"} — "old" muss EXAKT EINMAL im bestehenden Code vorkommen
      (kurze, eindeutige Snippets)

    format: "html" ODER "svg" — Formatwechsel (Datei-Umbenennung mit gleicher
    UUID + Reparatur aller Markdown-Referenzen). Der Code bleibt dabei
    UNGEKONVERTIERT, außer im selben Aufruf konvertiert: "media" mit dem
    Code im NEUEN Format anliefern (empfohlen, z. B. möglichst getreue
    Umwandlung). Ohne konvertierten Code ist das Applet vorübergehend
    ungültig — der nächste gültige Code-Ersatz im neuen Format repariert es.

    title: neuer Applet-Titel (optional). description: neue Kurzbeschreibung,
    was das Applet zeigt (optional; "" = zurücksetzen).

    Nach dem Edit wird automatisch die Version „MCP Edit“ (neuer Zustand)
    angelegt — der Zustand vorher ist die Version davor
    (Rückgängig: list_versions + restore_version). version_label: optionales
    kurzes Label für die angelegte Version (z. B. "Slider für Lernrate");
    Default „MCP Edit“. Syntax-Regeln: markdown_manual("applet")."""
    with Session(engine) as session:
        user, _token, _role = _auth(ctx, session, course_id, write=True, tool_name="edit_applet")
        full_media = bool(media)
        media_partial = isinstance(media_edits, list) and bool(media_edits)
        if full_media and media_partial:
            raise McpToolError("Genau eines angeben: 'media' (kompletter Ersatz) ODER 'media_edits' (stellenweise Edits).")
        if format is not None and format not in ("html", "svg"):
            raise McpToolError("'format' muss 'html' oder 'svg' sein.")
        if title is None and description is None and format is None and not (full_media or media_partial):
            raise McpToolError("Nichts zu ändern: 'media', 'media_edits', 'title', 'description' oder 'format' angeben.")
        if title is not None and not title.strip():
            raise McpToolError("Der Titel darf nicht leer sein.")
        applet = _resolve(session, CourseMedia, course_id, ref, "Applet")
        if applet.media_type != "applet":
            raise McpToolError(f"Medium '{applet.title}' ist kein Applet.")
        parts = []
        if format is not None and format != media_service.applet_format_of(applet.file_path):
            # Formatwechsel ERST (Datei wird umbenannt), dann evtl. Content.
            # Vorher Sicherheits-Snapshot (Rollback über Versions-History) —
            # analog Studio-Endpoint; ohne Skip, da ein reiner Wechsel
            # ansonsten spurlos bleibt (Snapshot wäre identisch).
            version_service.snapshot_current(
                session, user, "applet", int(applet.id), name=f"Formatwechsel → {format}")
            media_service.switch_applet_format(session, course_id, applet, format)
            parts.append(f"Format auf '{format}' gewechselt (Datei umbenannt, Referenzen repariert)")
        path = media_service.MEDIA_DIR / applet.file_path
        if not path.is_file():
            raise McpToolError("Applet-Datei fehlt (Element defekt).")
        old_content = path.read_text(encoding="utf-8")
        if full_media:
            new_content = media
        elif media_partial:
            new_content = _apply_html_edits(old_content, media_edits)
        else:
            new_content = None
        if new_content is not None:
            try:
                rel_path, mime, size = media_service.replace_applet(applet.file_path, new_content)
            except HTTPException as e:
                raise McpToolError(str(e.detail))
            applet.file_path = rel_path
            applet.mime_type = mime
            applet.file_size = size
            parts.append(
                f"{len(media_edits)} Edit(s) angewendet" if media_partial
                else "Code vollständig ersetzt"
            )
        if title is not None:
            applet.title = title.strip()
        if description is not None:
            applet.llm_description = description.strip() or None
        if title is not None or description is not None:
            parts.append("Metadaten: " + "/".join(
                n for n, v in (("Titel", title), ("Beschreibung", description)) if v is not None))
        if not parts:
            parts.append("nichts geändert")
        session.add(applet)
        session.commit()
        sync_media_usages(session, course_id)
        version_note = _mcp_version_note(session, user, "applet", int(applet.id), version_label)
        return _json({
            "id": applet.id,
            "title": applet.title,
            "url": media_service.media_url(applet),
            "format": media_service.applet_format_of(applet.file_path),
            "changed": "; ".join(parts),
            **({"hint": "Die Markdown-Referenz im Kursinhalt zeigt weiter auf das Applet (Pfad unverändert) — bei Titelwechsel den Alt-Text der Referenz ggf. per edit_content anpassen."} if title is not None else {}),
            "note": version_note,
        })


@mcp_tool
async def create_applet(
    course_id: int,
    title: str,
    media: str,
    ctx: Context,
    description: Optional[str] = None,
    format: Optional[str] = "html",
    version_label: Optional[str] = None,
) -> str:
    """Legt ein neues Applet (HTML oder SVG) im Kurs an.

    media = kompletter self-contained Code (Regeln: markdown_manual("applet")).
    format = "html" (Default) ODER "svg". description = 2–4 Sätze, was das
    Applet zeigt (optional). Der Rückgabewert enthält die fertige
    Markdown-Referenz zur Einbindung in Skript/Folien:
    ![<Titel>](/media/<course_id>/<datei>). version_label: optionales kurzes
    Label für die Initial-Version (Default „Erstellt")."""
    with Session(engine) as session:
        user, _token, _role = _auth(ctx, session, course_id, write=True, tool_name="create_applet")
        if not (title or "").strip():
            raise McpToolError("Der Titel darf nicht leer sein.")
        fmt = (format or "html").strip().lower()
        if fmt not in ("html", "svg"):
            raise McpToolError("'format' muss 'html' oder 'svg' sein.")
        try:
            rel_path, mime, size = media_service.save_applet(course_id, media or "", fmt)
        except HTTPException as e:
            raise McpToolError(str(e.detail))
        applet = CourseMedia(
            course_id=course_id,
            title=title.strip(),
            file_path=rel_path,
            media_type="applet",
            mime_type=mime,
            file_size=size,
            llm_description=(description or "").strip() or None,
            created_by=user.id,  # type: ignore[arg-type]
        )
        session.add(applet)
        session.commit()
        session.refresh(applet)
        sync_media_usages(session, course_id)
        version_service.mark_created(session, user, "applet", int(applet.id), name=version_label)
        return _json({
            "id": applet.id,
            "title": applet.title,
            "url": media_service.media_url(applet),
            "format": fmt,
            "markdown_reference": f"![{applet.title}]({media_service.media_url(applet)})",
            "note": "Applet anlegen UND im Kurs einbinden: die Markdown-Referenz per edit_content in ein Skript-Kapitel oder Slide-Deck einfügen (Syntax: markdown_manual).",
        })


def _create_svg_applet(
    session: Session, user, course_id: int, title: str, data: bytes,
    description: Optional[str], label: Optional[str] = None,
) -> str:
    """SVG-Eingang in create_image → SVG-Applet (Vektor, nicht rasterisiert)."""
    try:
        rel_path, mime, size = media_service.save_svg_upload(course_id, data)
    except HTTPException as e:
        raise McpToolError(str(e.detail))
    applet = CourseMedia(
        course_id=course_id,
        title=title.strip(),
        file_path=rel_path,
        media_type="applet",
        mime_type=mime,
        file_size=size,
        llm_description=(description or "").strip() or None,
        created_by=user.id,  # type: ignore[arg-type]
    )
    session.add(applet)
    session.commit()
    session.refresh(applet)
    sync_media_usages(session, course_id)
    version_service.mark_created(session, user, "applet", int(applet.id), name=label)
    return _json({
        "id": applet.id,
        "title": applet.title,
        "url": media_service.media_url(applet),
        "format": "svg",
        "markdown_reference": f"![{applet.title}]({media_service.media_url(applet)})",
        "note": "SVG-Datei als SVG-Applet gespeichert (Vektor, nicht rasterisiert). "
                "Einbinden: Markdown-Referenz per edit_content in Skript/Folien "
                "einfügen (Syntax: markdown_manual). Konvertierung zu "
                "interaktivem HTML: edit_applet (format='html').",
    })


def _video_ext(data: bytes) -> Optional[str]:
    """Video-Format per Magic-Bytes erkennen → Extension, sonst None.

    mp4: Bytes 4–8 = b"ftyp" (nach 4-Byte-Size-Header);
    webm/Matroska: EBML-Header (die ersten 4 Bytes)."""
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return ".mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
    return None


def _create_video(
    session: Session, user, course_id: int, title: str, data: bytes,
    description: Optional[str], ext: str,
) -> str:
    """Video-Eingang in create_image → Video-Medium (media_type='video')."""
    try:
        rel_path, mime, size = media_service.save_video_upload(
            course_id, f"video{ext}", io.BytesIO(data), len(data)
        )
    except HTTPException as e:
        raise McpToolError(str(e.detail))
    media = CourseMedia(
        course_id=course_id,
        title=title.strip(),
        file_path=rel_path,
        media_type="video",
        mime_type=mime,
        file_size=size,
        llm_description=(description or "").strip() or None,
        created_by=user.id,  # type: ignore[arg-type]
    )
    session.add(media)
    session.commit()
    session.refresh(media)
    sync_media_usages(session, course_id)
    notify.push_content_updated(session, course_id, "media", int(media.id))
    return _json({
        "id": media.id,
        "title": media.title,
        "url": media_service.media_url(media),
        "format": ext.lstrip("."),
        "markdown_reference": f"![{media.title}]({media_service.media_url(media)})",
        "note": "Video anlegen UND im Kurs einbinden: die Markdown-Referenz per "
                "edit_content in ein Skript-Kapitel oder Slide-Deck einfügen "
                "(Syntax: markdown_manual). Höhe steuern: {.height=<px>}.",
    })


@mcp_tool
async def create_image(
    course_id: int,
    title: str,
    source: str,
    ctx: Context,
    description: Optional[str] = None,
    version_label: Optional[str] = None,
) -> str:
    """Legt ein neues Bild- ODER Video-Medium im Kurs an.

    Bilder: PNG/JPEG/WebP/GIF, max. 5 MB. Videos: mp4/WebM, max. 500 MB —
    werden erkannt und als Video-Medium gespeichert (kein Bild; Einbindung
    wie bei Bildern, Höhe via {.height=<px>}).

    source = Data-URL (data:image/png;base64,…) für kleine Dateien ODER eine
    öffentliche http(s)-URL, die der Server lädt (keine Redirects, keine
    lokalen/internen Adressen). description = 2–4 Sätze, was das Medium zeigt
    (optional). Der Rückgabewert enthält die fertige Markdown-Referenz zum
    Einbinden in Skript/Folien: ![<Titel>](/media/<course_id>/<datei>).

    SVG (Data-URL oder URL): wird NICHT rasterisiert, sondern als editierbares
    SVG-Applet gespeichert (Media-Format „svg“). Konvertierung zu interaktivem
    HTML: edit_applet (format='html').

    Wikimedia-URLs (upload.wikimedia.org / thumb.wikimedia.org): die Datei
    wird AUTOMATISCH über die MediaWiki-API nach Dateiname aufgelöst — der
    Dateiname in der URL muss stimmen, Hash-Pfad und Thumbnail-Breite dürfen
    ungenau/ungültig sein (der Server holt die korrekte URL).
    Im Chat selbst generierte Bilder (z. B. DALL·E) sind meist zu groß für
    die Data-URL-Variante — dann nur die URL-Variante nutzen, wenn eine
    öffentliche Download-URL vorliegt; andernfalls das Bild in der
    Kurs-Web-UI hochladen. version_label: optionales kurzes Label für die
    Initial-Version — greift NUR bei SVG-Eingang (SVG-Applet); Raster-Bilder
    und Videos haben keine Versions-History (Default „Erstellt")."""
    with Session(engine) as session:
        user, _token, _role = _auth(ctx, session, course_id, write=True, tool_name="create_image")
        if not (title or "").strip():
            raise McpToolError("Der Titel darf nicht leer sein.")
        src = (source or "").strip()
        if not src:
            raise McpToolError("source fehlt (Data-URL oder http(s)-URL).")
        if src.lower().startswith("data:"):
            data = _decode_data_url(src)
        else:
            data = await _fetch_image_bytes(
                src, max_bytes=media_service.MAX_VIDEO_BYTES, timeout=120.0
            )
        if not data:
            raise McpToolError("Leere Datei.")
        vext = _video_ext(data)
        if vext:
            return _create_video(session, user, course_id, title, data, description, vext)
        if len(data) > media_service.MAX_UPLOAD_BYTES:
            raise McpToolError(f"Bild zu groß (max. {media_service.MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
        try:
            ext, _fmt = _detect_image(data)
        except McpToolError:
            # Kein Raster-Format → SVG-Versuch (sanitize_svg validiert hart
            # und lehnt alles Nicht-SVG mit klarer Fehlermeldung ab).
            return _create_svg_applet(session, user, course_id, title, data, description, version_label)
        try:
            rel_path, mime, size = media_service.save_upload(course_id, f"img{ext}", data)
        except HTTPException as e:
            raise McpToolError(str(e.detail))
        media = CourseMedia(
            course_id=course_id,
            title=title.strip(),
            file_path=rel_path,
            media_type="image",
            mime_type=mime,
            file_size=size,
            llm_description=(description or "").strip() or None,
            created_by=user.id,  # type: ignore[arg-type]
        )
        session.add(media)
        session.commit()
        session.refresh(media)
        sync_media_usages(session, course_id)
        notify.push_content_updated(session, course_id, "media", int(media.id))
        return _json({
            "id": media.id,
            "title": media.title,
            "url": media_service.media_url(media),
            "markdown_reference": f"![{media.title}]({media_service.media_url(media)})",
            "note": "Bild anlegen UND im Kurs einbinden: die Markdown-Referenz per edit_content in ein Skript-Kapitel oder Slide-Deck einfügen (Syntax: markdown_manual).",
        })


@mcp_tool
async def create_chapter(
    course_id: int,
    title: str,
    ctx: Context,
    content: str = "",
    summary: Optional[str] = None,
    is_visible: bool = False,
    version_label: Optional[str] = None,
) -> str:
    """Legt ein neues Skript-Kapitel am ENDE des Kurs-Skripts an.

    content = Kapitel-Inhalt in der AICampus-Markdown-Syntax
    (markdown_manual("script")). Querverweise einbauen, wo sinnvoll:
    @sec:/@eq:/@fig:/… auf bestehende Labels (vorher z. B. per
    read_script_section lesen) und passende Übungsaufgaben per @task:243
    (Aufgaben-ID ohne Klammern; IDs: course_overview). summary = Kurzfassung
    (optional, intern —
    NICHT für Studenten sichtbar). is_visible = für Studenten sichtbar
    (Default: False = Entwurf; erst bei fertigen Inhalt auf True setzen).
    version_label: optionales kurzes Label für die Initial-Version
    (Default „Erstellt")."""
    with Session(engine) as session:
        user, _token, _role = _auth(ctx, session, course_id, write=True, tool_name="create_chapter")
        if not (title or "").strip():
            raise McpToolError("Der Titel darf nicht leer sein.")
        max_order = session.exec(
            select(func.max(ScriptSection.display_order))
            .where(ScriptSection.course_id == course_id)
        ).one()
        section = ScriptSection(
            course_id=course_id,
            title=title.strip(),
            content=content or "",
            is_visible=bool(is_visible),
            display_order=(max_order or 0) + 1,
            summary=(summary or "").strip(),
            created_by=user.id,  # type: ignore[arg-type]
        )
        session.add(section)
        session.commit()
        session.refresh(section)
        sync_media_usages(session, course_id)
        version_service.mark_created(session, user, "script_section", int(section.id), name=version_label)
        return _json({
            "id": section.id,
            "title": section.title,
            "display_order": section.display_order,
            "visible": section.is_visible,
            "note": (
                "Kapitel am Ende des Skripts angelegt. "
                + ("Für Studenten sichtbar." if section.is_visible
                   else "Noch NICHT für Studenten sichtbar (Entwurf) — nach Fertigstellung in der Kurs-Web-UI freischalten.")
            ),
        })


@mcp_tool
async def create_slide_deck(
    course_id: int,
    title: str,
    ctx: Context,
    content: str = "",
    summary: Optional[str] = None,
    is_visible: bool = False,
    version_label: Optional[str] = None,
) -> str:
    """Legt ein neues Slide-Deck am ENDE der Deck-Liste des Kurses an.

    content = Folien als Markdown, je Folie getrennt durch eine eigene Zeile
    "---" (Syntax: markdown_manual("slides")); der Inhalt wird strikt
    validiert. Querverweise einbauen, wo sinnvoll: @sec:/@eq:/@fig:/… auf
    das Skript (bestehende Labels) und passende Übungsaufgaben per
    @task:243 (Aufgaben-ID ohne Klammern; IDs: course_overview). summary /
    is_visible wie bei
    create_chapter. version_label: optionales kurzes Label für die
    Initial-Version (Default „Erstellt")."""
    with Session(engine) as session:
        user, _token, _role = _auth(ctx, session, course_id, write=True, tool_name="create_slide_deck")
        if not (title or "").strip():
            raise McpToolError("Der Titel darf nicht leer sein.")
        try:
            parse_slides(content or "")
        except SlideError as e:
            raise McpToolError(f"Kein gültiges Slide-Deck: {e}")
        max_order = session.exec(
            select(func.max(CourseMaterial.display_order))
            .where(CourseMaterial.course_id == course_id)
        ).one()
        material = CourseMaterial(
            course_id=course_id,
            title=title.strip(),
            material_type=MaterialType.SLIDES,
            content=content or "",
            is_visible=bool(is_visible),
            display_order=(max_order or 0) + 1,
            summary=(summary or "").strip(),
            created_by=user.id,  # type: ignore[arg-type]
        )
        session.add(material)
        session.commit()
        session.refresh(material)
        sync_media_usages(session, course_id)
        version_service.mark_created(session, user, "slide_deck", int(material.id), name=version_label)
        return _json({
            "id": material.id,
            "title": material.title,
            "display_order": material.display_order,
            "visible": material.is_visible,
            "note": (
                "Slide-Deck angelegt. "
                + ("Für Studenten sichtbar." if material.is_visible
                   else "Noch NICHT für Studenten sichtbar (Entwurf) — nach Fertigstellung in der Kurs-Web-UI freischalten.")
            ),
        })


def _validate_deadline(deadline: Optional[str]) -> None:
    if not deadline:
        return
    try:
        datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    except ValueError:
        raise McpToolError(
            f"deadline ist kein gültiges ISO-Datum ('2025-02-15T23:59'): {deadline!r}"
        )


def _normalize_deadline_mcp(deadline: Optional[str]) -> Optional[str]:
    """Deadline → ISO-UTC (Z). Ohne Zonen-Marker gilt die Campus-Zone
    (Europe/Berlin) — der Agent liefert lokale Wall-Clock-Zeiten."""
    if not deadline:
        return None
    try:
        return deadline_from_local(deadline)
    except (ValueError, KeyError):
        raise McpToolError(f"deadline ist nicht interpretierbar: {deadline!r}")


def _mc_data_from_questions(existing: Optional[str], questions: list[dict]) -> dict:
    """Fragen-Liste → validiertes mc_data-Dict. penalty_enabled wird aus dem
    bestehenden mc_data übernommen (neue Aufgabe → False)."""
    penalty = False
    if existing:
        try:
            penalty = bool(json.loads(existing).get("penalty_enabled", False))
        except (json.JSONDecodeError, TypeError):
            pass
    try:
        return parse_mc_data(json.dumps(
            {"penalty_enabled": penalty, "questions": questions}, ensure_ascii=False
        ))
    except McValidationError as e:
        raise McpToolError(f"Ungültige Multiple-Choice-Fragen: {e}")


@mcp_tool
async def create_task(
    course_id: int,
    type: str,
    title: str,
    description: str,
    ctx: Context,
    model_solution: Optional[str] = None,
    max_points: Optional[int] = None,
    max_attempts: Optional[int] = None,
    deadline: Optional[str] = None,
    text_template: Optional[str] = None,
    mc_questions: Optional[list[dict]] = None,
    is_visible: bool = False,
    version_label: Optional[str] = None,
) -> str:
    """Legt eine neue Übungsaufgabe am ENDE der Aufgaben-Liste des Kurses an.

    type = "text" (Klassische Aufgabe, Abgabe als Text), "mc" (Multiple
    Choice mit sofortigem Feedback), "upload" (Abgabe als Datei/Zip —
    Template- und Musterlösungs-Datei danach mit set_upload_task_file
    setzen) oder "extern" (außerhalb des Systems bewertet, z. B. Vortrag
    oder Notenübertragung — Studierende geben NICHTS ab; die Bewertung
    erfolgt vom Tutor/Prof in der Kurs-Web-UI). Workspace-Aufgaben
    (Code-Runs) sind via MCP NICHT unterstützt — bitte Kurs-Web-UI.

    description / text_template / model_solution: AICampus-Markdown wie
    im Skript (Details: markdown_manual("script")) — **fett**, Listen,
    $math$, $$formel$$, Tabellen, ```mermaid```-Graphen, Medien aus
    list_media. Querverweise auf Skript/Quellen als NORMALER FLOSSTEXT
    (nie in Backticks): @fig:/@eq:/@code:/@box:/@tab:/@sec:/@cite: — NUR
    Labels/Keys, die im Kurs tatsächlich existieren (vorher z. B. per
    read_script_section lesen); in der Aufgabe selbst KEINE neuen
    fig/eq/code/box/tab-Labels anlegen.
    model_solution: IMMER inkl. Bewertungskriterien (max. max_points
    Punkte; nur Punkte, die aus der Aufgabenstellung ersichtlich sind);
    für Studenten nur nach Abgabe sichtbar.
    text_template (nur type="text"): Antwort-Gerüst für die Studenten
    (Lückentext, Tabelle, …) — nur wenn ein Gerüst sinnvoll ist; die
    Musterlösung zeigt die vollständig ausgefüllte Vorlage. Bei
    type="upload" dient model_solution als Bewertungskriterien-Text;
    die eigentliche Musterlösung ist eine DATEI (set_upload_task_file,
    kind="solution").
    max_points: Pflicht bei type="text"/"upload"/"extern"; bei type="mc"
    wird sie aus der Punkte-Summe der Fragen abgeleitet (Angabe wird
    ignoriert).
    mc_questions (nur type="mc", Pflicht): Liste von Fragen im Format
    {"question": str, "points": int ≥ 0, "multi_select": bool (Default false),
     "options": [{"text": str, "correct": bool, "feedback": str (optional),
                  "feedback_wrong": str (optional, bei multi_select)}]}
    Regeln: mind. 2 Optionen je Frage; multi_select=false → genau EINE
    richtige Option; multi_select=true → mind. 1 richtig UND mind. 1
    falsch; insgesamt mind. 1 Punkt.
    max_attempts: max. Versuchszahl (optional, weggelassen =
    unlimitiert). deadline: "2025-02-15T23:59" (optional; ohne Zeitzone
    = Campus-Zone Europe/Berlin, wird intern in UTC gespeichert).
    is_visible: für Studenten sichtbar (Default: False = Entwurf — erst bei
    fertiger Aufgabe auf True setzen). version_label: optionales kurzes
    Label für die Initial-Version (Default „Erstellt")."""
    with Session(engine) as session:
        user, _token, _role = _auth(ctx, session, course_id, write=True, tool_name="create_task")
        if type not in ("text", "mc", "upload", "extern"):
            raise McpToolError(
                "type muss 'text', 'mc', 'upload' oder 'extern' sein "
                "(Workspace-Aufgaben werden via MCP nicht unterstützt — "
                "bitte in der Kurs-Web-UI anlegen)."
            )
        if not (title or "").strip():
            raise McpToolError("Der Titel darf nicht leer sein.")
        if not (description or "").strip():
            raise McpToolError("Die Aufgabenstellung (description) darf nicht leer sein.")
        _validate_deadline(deadline)
        if max_attempts is not None and max_attempts < 1:
            raise McpToolError("max_attempts muss ≥ 1 sein (weggelassen = unlimitiert).")
        if max_points is not None and max_points < 0:
            raise McpToolError("max_points muss ≥ 0 sein.")
        mc_data = None
        if type == "mc":
            if text_template is not None:
                raise McpToolError("text_template ist nur für type='text' erlaubt.")
            if not (isinstance(mc_questions, list) and mc_questions):
                raise McpToolError("type='mc' erfordert mc_questions (Liste von mind. einer Frage).")
            mc_data = _mc_data_from_questions(None, mc_questions)
        else:
            if mc_questions is not None:
                raise McpToolError("mc_questions ist nur für type='mc' erlaubt.")
            if type in ("upload", "extern") and text_template is not None:
                raise McpToolError("text_template ist nur für type='text' erlaubt.")
            if max_points is None:
                raise McpToolError(f"type='{type}' erfordert max_points.")
        max_order = session.exec(
            select(func.max(Task.display_order)).where(Task.course_id == course_id)
        ).one()
        task = Task(
            course_id=course_id,
            title=title.strip(),
            task_type=TaskType(type),
            description=description.strip(),
            model_solution=(model_solution or "").strip() or None,
            max_points=mc_total_points(mc_data) if mc_data else max_points,
            max_attempts=max_attempts,
            deadline=_normalize_deadline_mcp(deadline),
            text_template=((text_template or "").strip() or None) if type == "text" else None,
            is_visible=bool(is_visible),
            display_order=(max_order or 0) + 1,
            created_by=user.id,  # type: ignore[arg-type]
        )
        if mc_data:
            task.mc_data = json.dumps(mc_data, ensure_ascii=False)
        session.add(task)
        session.commit()
        session.refresh(task)
        sync_media_usages(session, course_id)
        version_service.mark_created(session, user, "task", int(task.id), name=version_label)
        return _json({
            "id": task.id,
            "title": task.title,
            "type": task.task_type.value,
            "max_points": task.max_points,
            "display_order": task.display_order,
            "visible": task.is_visible,
            "note": (
                "Aufgabe am Ende der Aufgaben-Liste angelegt. "
                + ("Für Studenten sichtbar." if task.is_visible
                   else "Noch NICHT für Studenten sichtbar (Entwurf) — nach Fertigstellung in der Kurs-Web-UI freischalten.")
                + (" Template- und Musterlösungs-Datei mit set_upload_task_file setzen."
                   if task.task_type.value == "upload" else "")
                + (" Extern bewertet: Tutor/Prof vergibt in der Kurs-Web-UI Punkte + Feedback (keine Abgabe, kein LLM-Grading)."
                   if task.task_type.value == "extern" else "")
            ),
        })


@mcp_tool
async def edit_task(
    course_id: int,
    ref: Union[int, str],
    ctx: Context,
    title: Optional[str] = None,
    description: Optional[str] = None,
    model_solution: Optional[str] = None,
    max_points: Optional[int] = None,
    max_attempts: Optional[int] = None,
    deadline: Optional[str] = None,
    hints_enabled: Optional[bool] = None,
    is_visible: Optional[bool] = None,
    text_template: Optional[str] = None,
    mc_questions: Optional[list[dict]] = None,
    version_label: Optional[str] = None,
) -> str:
    """Bearbeitet eine bestehende Übungsaufgabe. ref = Aufgabe-ID oder
    exakter Titel (Liste: course_overview); Aufgabe zuerst mit read_task
    lesen. Der Aufgabentyp kann NICHT gewechselt werden. Gleiche
    Inhalts-Regeln wie create_task (AICampus-Markdown, Musterlösung mit
    Bewertungskriterien, Querverweise nur mit tatsächlich vorhandenen
    Labels).

    Alle Parameter optional — nur angegebene Felder werden geändert:
    - title / description: neuer Wert
    - model_solution: neue Musterlösung ("" = entfernen)
    - max_points: nur für Text-Aufgaben; bei MC-Aufgaben wird sie aus der
      Fragen-Punkte-Summe abgeleitet (→ mc_questions angeben)
    - max_attempts / hints_enabled / is_visible: neuer Wert
    - deadline: neues ISO-Datum ("" = entfernen; ohne Zeitzone = Campus-Zone Europe/Berlin)
    - text_template: nur für Text-Aufgaben ("" = entfernen)
    - mc_questions: nur für MC-Aufgaben — KOMPLETTER Ersatz der Fragen
      (Format wie bei create_task)

    Nach dem Edit wird automatisch die Version „MCP Edit“ (neuer Zustand)
    angelegt — der Zustand vorher ist die Version davor
    (Rückgängig: list_versions + restore_version). version_label: optionales
    kurzes Label für die angelegte Version (z. B. "Frage 2 hinzugefügt");
    Default „MCP Edit“."""
    with Session(engine) as session:
        user, _token, _role = _auth(ctx, session, course_id, write=True, tool_name="edit_task")
        task = _resolve(session, Task, course_id, ref, "Aufgabe")
        params = (
            title, description, model_solution, max_points, max_attempts,
            deadline, hints_enabled, is_visible, text_template, mc_questions,
        )
        if all(p is None for p in params):
            raise McpToolError(
                "Nichts zu ändern: mindestens ein Feld angeben (title, description, "
                "model_solution, max_points, max_attempts, deadline, hints_enabled, "
                "is_visible, text_template, mc_questions)."
            )
        _validate_deadline(deadline)
        if max_attempts is not None and max_attempts < 1:
            raise McpToolError("max_attempts muss ≥ 1 sein (weggelassen = unlimitiert).")
        if max_points is not None and max_points < 0:
            raise McpToolError("max_points muss ≥ 0 sein.")
        if title is not None and not title.strip():
            raise McpToolError("Der Titel darf nicht leer sein.")
        if description is not None and not description.strip():
            raise McpToolError("Die Aufgabenstellung darf nicht leer sein.")
        ttype = task.task_type
        if text_template is not None and ttype != TaskType.TEXT:
            raise McpToolError("text_template ist nur für Text-Aufgaben erlaubt.")
        if mc_questions is not None and ttype != TaskType.MC:
            raise McpToolError("mc_questions ist nur für MC-Aufgaben erlaubt.")
        if max_points is not None and ttype == TaskType.MC:
            raise McpToolError(
                "Bei MC-Aufgaben wird max_points aus der Punkte-Summe der Fragen "
                "abgeleitet — mc_questions statt max_points angeben."
            )
        changed: list[str] = []
        if title is not None:
            task.title = title.strip()
            changed.append("title")
        if description is not None:
            task.description = description.strip()
            changed.append("description")
        if model_solution is not None:
            task.model_solution = model_solution.strip() or None
            changed.append("model_solution")
        if max_points is not None:
            task.max_points = max_points
            changed.append("max_points")
        if max_attempts is not None:
            task.max_attempts = max_attempts
            changed.append("max_attempts")
        if deadline is not None:
            task.deadline = _normalize_deadline_mcp(deadline)
            changed.append("deadline")
        if hints_enabled is not None:
            task.hints_enabled = bool(hints_enabled)
            changed.append("hints_enabled")
        if is_visible is not None:
            task.is_visible = bool(is_visible)
            changed.append("is_visible")
        if text_template is not None:
            task.text_template = text_template.strip() or None
            changed.append("text_template")
        if mc_questions is not None:
            if not (isinstance(mc_questions, list) and mc_questions):
                raise McpToolError("mc_questions muss eine Liste von mind. einer Frage sein.")
            mc_data = _mc_data_from_questions(task.mc_data, mc_questions)
            task.mc_data = json.dumps(mc_data, ensure_ascii=False)
            task.max_points = mc_total_points(mc_data)
            changed.append("mc_questions")
        task.updated_at = datetime.now(timezone.utc)
        session.add(task)
        session.commit()
        session.refresh(task)
        sync_media_usages(session, course_id)
        version_note = _mcp_version_note(session, user, "task", int(task.id), version_label)
        return _json({
            "id": task.id,
            "title": task.title,
            "type": task.task_type.value,
            "max_points": task.max_points,
            "changed": changed,
            "note": version_note,
        })


@mcp_tool
async def set_upload_task_file(
    course_id: int,
    task_id: int,
    kind: str,
    filename: str,
    source: str,
    ctx: Context,
) -> str:
    """Setzt die Datei einer Upload-Aufgabe: Template (kind="template",
    von Studierenden zum Bearbeiten herunterladbar) oder Musterlösung
    (kind="solution", nur für die LLM-Bewertung relevant). ERSETZT eine
    bestehende Datei — ein Löschen gibt es nicht (neues Setzen =
    Überschreiben).

    source: die Datei als TEXT (Code/Text/Markdown — einfach der Inhalt)
    ODER eine data:-URL (base64, für Binärdateien und Zips) ODER eine
    öffentliche http(s)-URL. Max. 200 MB.

    task_id: die Upload-Aufgabe (read_task zeigt bei type="upload" die
    vorhandenen Dateinamen; Liste: course_overview)."""
    with Session(engine) as session:
        user, _token, _role = _auth(ctx, session, course_id, write=True, tool_name="set_upload_task_file")
        task = _resolve(session, Task, course_id, task_id, "Aufgabe")
        if task.task_type.value != "upload":
            raise McpToolError("Das ist keine Upload-Aufgabe (type muss 'upload' sein).")
        if kind not in ("template", "solution"):
            raise McpToolError('kind muss "template" oder "solution" sein.')
        try:
            safe_name = upload_service.safe_filename(filename)
        except ValueError:
            raise McpToolError("filename: ungültiger Dateiname (nur Basisname ohne Pfade).")
        src = (source or "").strip()
        if not src:
            raise McpToolError("source darf nicht leer sein.")
        if src.startswith("data:"):
            data = _decode_data_url(src)
        elif src.startswith(("http://", "https://")):
            data = await _fetch_file_bytes(src, UPLOAD_MAX_FILE_BYTES)
        else:
            data = src.encode("utf-8")
        if len(data) > UPLOAD_MAX_FILE_BYTES:
            raise McpToolError(f"Datei zu groß (max. {UPLOAD_MAX_FILE_BYTES // (1024 * 1024)} MB).")
        field = "upload_template" if kind == "template" else "upload_solution_file"
        old = getattr(task, field)
        upload_service.save_task_file(int(task.id), safe_name, data)
        if old and old != safe_name:
            upload_service.remove_task_file(int(task.id), old)
        setattr(task, field, safe_name)
        task.updated_at = datetime.now(timezone.utc)
        session.add(task)
        session.commit()
        session.refresh(task)
        # Datei kann sich bei gleichem Dateinamen geändert haben →
        # bewusst OHNE skip_if_same_as_latest (Feld-Snapshot allein
        # würde den no-op melden).
        version_service.snapshot_current(
            session, user, "task", int(task.id), name="MCP Edit")
        return _json({
            "id": task.id,
            "kind": kind,
            "filename": safe_name,
            "size": len(data),
            "note": (
                'Automatisch angelegt: Version „MCP Edit“ (neuer Zustand). Zustand '
                "vorher = die Version davor (Rückgängig: list_versions + restore_version)."
            ),
        })


@mcp_tool
async def read_upload_task_file(
    course_id: int,
    task_id: int,
    kind: str,
    ctx: Context,
    path: Optional[str] = None,
) -> str:
    """Liest die Template- (kind="template") oder Musterlösungs-Datei
    (kind="solution") einer Upload-Aufgabe (Inhalt als Text, gekürzt).

    Bei ZIP-Dateien: ohne path = Dateiliste im Archiv; mit path = Inhalt
    einer Datei aus dem Archiv (Pfade aus der Liste). Nur für Tutor/Prof."""
    with Session(engine) as session:
        user, _token, role = _auth(ctx, session, course_id, tool_name="read_upload_task_file")
        if not _is_tutor(role):
            raise McpToolError(
                "Nur für Tutor/Prof (Studenten sehen die Template-Datei in der Web-UI).")
        task = _resolve(session, Task, course_id, task_id, "Aufgabe")
        if task.task_type.value != "upload":
            raise McpToolError("Das ist keine Upload-Aufgabe (type muss 'upload' sein).")
        if kind not in ("template", "solution"):
            raise McpToolError('kind muss "template" oder "solution" sein.')
        name = task.upload_template if kind == "template" else task.upload_solution_file
        p = upload_service.task_upload_file(int(task.id), name)
        if not p:
            raise McpToolError(
                f"Keine {kind}-Datei für diese Aufgabe (setzen: set_upload_task_file).")
        truncated = False
        if upload_service.is_zip_file(p):
            if not path:
                files = upload_service.list_upload_files(p)
                return _json({
                    "file": name,
                    "type": "zip",
                    "files": [{"path": f["path"], "size": f["size"]} for f in files],
                    "hint": "Inhalt lesen: path = Pfad aus der Liste.",
                })
            try:
                data = upload_service.read_upload_file(p, path)
            except (ValueError, OSError, zipfile.BadZipFile):
                raise McpToolError(
                    f"Datei '{path}' nicht im Archiv (Liste: ohne path aufrufen).")
        else:
            if path:
                raise McpToolError("path ist nur für ZIP-Dateien relevant.")
            data = p.read_bytes()
            if len(data) > 2_000_000:
                data = data[:2_000_000]
                truncated = True
        if b"\x00" in data[:1024]:
            return _json({
                "file": name,
                "path": path,
                "binary": True,
                "note": f"Binärdatei ({len(data)} Bytes) — Inhalt nicht als Text lesbar.",
            })
        text = data.decode("utf-8", errors="replace")
        if len(text) > 100_000:
            text = text[:100_000] + "\n… (gekürzt) …"
            truncated = True
        return _json({"file": name, "path": path, "truncated": truncated, "content": text})


@mcp_tool
async def list_versions(
    course_id: int,
    type: str,
    ref: Union[int, str],
    ctx: Context,
) -> str:
    """Version-Historie eines Skript-Kapitels, Slide-Decks, Applets, einer
    Aufgabe oder Quelle.

    type = "script_section" | "slide_deck" | "applet" | "task" | "reference"
    (bei reference: ref = Quellen-Key oder ID; sonst: ID oder exakter Titel).
    Neues Element → Version „Erstellt“; jedes MCP-Edit → Version „MCP Edit“
    (neuer Zustand). Zum Zurücksetzen: die Version VOR dem Edit per
    restore_version wiederherstellen."""
    with Session(engine) as session:
        user, _token, _role = _auth(ctx, session, course_id, write=True, tool_name="list_versions")
        entity = _resolve_editable(session, course_id, ref, type)
        versions = version_service.list_versions(session, user, type, int(entity.id))
        return _json({
            "id": entity.id,
            "title": entity.title,
            "versions": versions,
            "hint": "Rückgängig: restore_version(course_id, type, ref, version_id).",
        })


@mcp_tool
async def restore_version(
    course_id: int,
    type: str,
    ref: Union[int, str],
    version_id: int,
    ctx: Context,
) -> str:
    """Setzt ein Skript-Kapitel, Slide-Deck, Applet, eine Aufgabe oder Quelle
    auf eine frühere Version zurück (version_id aus list_versions; ref/Type
    wie bei list_versions). Der alte Zustand wird angewendet und als NEUE
    Version gespeichert — nichts geht verloren, Rückrollen ist jederzeit
    wieder möglich."""
    with Session(engine) as session:
        user, _token, _role = _auth(ctx, session, course_id, write=True, tool_name="restore_version")
        entity = _resolve_editable(session, course_id, ref, type)
        v = session.get(ContentVersion, version_id)
        if not v or v.entity_type != type or v.entity_id != entity.id:
            raise McpToolError(f"Version {version_id} gehört nicht zu diesem Element (siehe list_versions).")
        result = version_service.restore_version(session, user, version_id)
        return _json({
            "message": f"Element '{entity.title}' wurde auf Version {version_id} zurückgesetzt.",
            "new_version_id": result["id"],
            "note": "Der wiederhergestellte Zustand ist jetzt die aktuelle (neueste) Version.",
        })


# ─── Mount-App (stateless Streamable HTTP + OAuth) ─────────────
#
# stateless_http: neue Session pro Request, kein serverseitiger State —
# passt zum flachen Design (Auth kommt ohnehin pro Request per Header).
# DNS-Rebinding-Schutz wird deaktiviert: die eigentliche Sicherheitsebene
# ist TLS (nginx) + hochentropiges Bearer-Token; Browser-Clients sind
# nicht Zielgruppe dieses Endpunkts.
#
# Auth: OAuth-2.0 Authorization-Code + PKCE (s. api/mcp_oauth.py), PAT-Tokens
# bleiben parallel über den Bearer-Header nutzbar.
# Lowlevel-Server statt MCPServer, weil die Highlevel-Klasse
# auth_server_provider und token_verifier nicht zusammen akzeptiert
# (ValueError) — hier ist beides nötig, weil Authorization- und
# Resource-Server in derselben App laufen.
MCP_ISSUER = (
    f"https://{PUBLIC_DOMAIN}/mcp"
    if PUBLIC_DOMAIN
    else "http://127.0.0.1:8000/mcp"
)

provider = AICampusAuthProvider()
# RFC 9728: Protected-Resource-Metadata gehört unter die Domain-Root
# (dorthin verweist der WWW-Authenticate-Header). Die Sub-App kopiert sie
# zusätzlich unter dem /mcp-Prefix (falsche URL, harmlos) — main.py hängt
# diese Routen an der Root an.
protected_resource_routes = create_protected_resource_routes(
    resource_url=f"{MCP_ISSUER}/",
    authorization_servers=[MCP_ISSUER],
)

# RFC 8414 Authorization-Server-Metadata: main.py spiegelt sie über
# create_auth_routes(…) am Domain-Root (Claude-Discovery) — die Optionen
# dort müssen mit den AuthSettings unten synchron bleiben!

mcp_app_raw = server._lowlevel_server.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    auth=AuthSettings(
        issuer_url=MCP_ISSUER,
        resource_server_url=f"{MCP_ISSUER}/",
        # Unsere Tokens tragen keinen resource-Claim; die Verifikation läuft
        # selbst über die DB-Lookups (ProviderTokenVerifier) → kein Check.
        validate_token_resource=False,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    ),
    token_verifier=ProviderTokenVerifier(provider),
    auth_server_provider=provider,
)


# ─── CORS für Browser-basierte MCP-Clients (z. B. lokale LLM-WebUIs) ───
#
# Das SDK hängt CORS-Header nur an die OAuth-Metadata-/Token-Endpunkte, nicht
# an den Streamable-HTTP-Endpunkt selbst — Browser-Clients (cross-site Origin
# + Authorization-Header) scheitern am fehlenden Preflight + ACAO-Header.
# Reines ASGI (kein BaseHTTPMiddleware): streamt SSE ohne Pufferung.
# ACAO „*“ ist sicher: ohne gültiges Bearer-Token gibt es 401, Cookies
# spielt der Endpunkt keine Rolle.
_CORS_ALLOW_HEADERS = (
    "authorization, content-type, accept, mcp-session-id, last-event-id, "
    "mcp-protocol-version"
)


def _with_cors(app):
    async def wrapped(scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "OPTIONS":
            async def send_with_cors(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers") or [])
                    headers.append((b"access-control-allow-origin", b"*"))
                    message = {**message, "headers": headers}
                await send(message)
            await app(scope, receive, send_with_cors)
            return
        # Angefragte Header 1:1 erlauben (Echo) — so funktionieren auch
        # MCP-Header, die wir hier nicht einzeln kennen müssen.
        requested = next(
            (v.decode() for k, v in scope.get("headers", [])
             if k == b"access-control-request-headers"),
            _CORS_ALLOW_HEADERS,
        )
        await send({
            "type": "http.response.start",
            "status": 204,
            "headers": [
                (b"access-control-allow-origin", b"*"),
                (b"access-control-allow-methods", b"GET, POST, DELETE, OPTIONS"),
                (b"access-control-allow-headers", requested.encode()),
                (b"access-control-max-age", b"86400"),
            ],
        })
        await send({"type": "http.response.body", "body": b""})

    return wrapped


# mcp_app_raw bleibt für main.py referenziert: der Sub-App-Lifespan
# (Session-Manager-Task-Group) muss über das nackte SDK-App laufen.
mcp_app = _with_cors(mcp_app_raw)
