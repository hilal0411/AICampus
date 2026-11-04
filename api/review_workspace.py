"""
Review-Workspace: eingereichte Lösung LIVE in einem separaten Container
ausführen (Tutor-Korrektur / Klausurvorbereitung).

Zugriff: der Student (eigene Einreichung) oder PROF/Tutor des Kurses /
Global-Admin. Edits sind WEGWERFBAR — das Einreichungs-Snapshot bleibt
unverändert. „Zurücksetzen“ = Workspace löschen (nächster Status-Call
baut ihn aus dem Snapshot neu).

Seitlicher Workspace pro Einreichung: Key mit reserviertem Student-Bereich
(REVIEW_KEY_OFFSET + Submission-Id) → kollidiert mit keinen Live-Workspaces.
"""

import asyncio
import json
import logging
import tarfile
import zlib
from urllib.parse import quote

import websockets
from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, Request,
    Response, UploadFile, WebSocket,
)
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from config import BASE_DIR, PREVIEW_BASE_DOMAIN
from sqlmodel import Session, select
from starlette.websockets import WebSocketDisconnect

from compute_agent.auth import make_token
from database.base import engine, get_session
from database.models import (
    CourseRole, GlobalUserRole, Submission, Task, User, UserCourse,
)
from services.auth_service import decode_access_token, get_current_user
from services.compute_client import ComputeAgentError, ComputeClient
from services.workspace_service import (
    MAX_FILE_BYTES,
    WorkspaceService,
    effective_file_access,
    effective_folder_access,
    file_disk_path,
    snapshot_abs_path,
    snapshot_list_files,
    snapshot_read_file,
    workspace_service,
)

router = APIRouter(prefix="/api/review", tags=["Review-Workspace"])
logger = logging.getLogger(__name__)


# ── Zugriff ────────────────────────────────────────────────────────

def _load_review_context(task_id: int, submission_id: int,
                         session: Session, user: User
                         ) -> tuple[Submission, Task, bool]:
    """Einreichung + Task laden, Zugriff prüfen.

    Liefert (submission, task, privileged) — privileged = PROF/Tutor/
    Global-Admin (darf 👤-Dateien sehen). Student: nur eigene
    Einreichungen."""
    submission = session.get(Submission, submission_id)
    if not submission or submission.task_id != task_id:
        raise HTTPException(404, "Einreichung nicht gefunden.")
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Aufgabe nicht gefunden.")
    if task.task_type.value != "workspace":
        raise HTTPException(400, "Keine Workspace-Aufgabe.")
    if not submission.workspace_snapshot:
        raise HTTPException(400, "Kein Workspace-Snapshot vorhanden.")

    privileged = user.role == GlobalUserRole.ADMIN
    if submission.student_id != user.id and not privileged:
        membership = session.exec(
            select(UserCourse)
            .where(UserCourse.user_id == user.id)
            .where(UserCourse.course_id == task.course_id)
        ).first()
        privileged = bool(
            membership
            and membership.role_in_course in (CourseRole.PROF, CourseRole.TUTOR)
        )
    if submission.student_id != user.id and not privileged:
        raise HTTPException(403, "Nur Zugriff auf eigene Einreichungen.")
    return submission, task, privileged


def _client_or_error(session: Session, task: Task) -> ComputeClient:
    """Gesunden Agent wählen oder 503 (degradierter Modus)."""
    if not workspace_service.is_enabled(session, task.course_id):
        raise HTTPException(503, "Workspace-Aufgaben sind derzeit deaktiviert.")
    not_ready = workspace_service.validate_task_ready(session, task)
    if not_ready:
        raise HTTPException(503, not_ready)
    agent = workspace_service.pick_task_agent(session, task)
    if agent is None:
        raise HTTPException(
            503, workspace_service.pick_agent_error(
                session, task.course_id, workspace_service.task_engine_names(task)))
    return workspace_service.client_for(agent)


def _key(task: Task, submission: Submission) -> str:
    return workspace_service.review_key(task, submission.id)


def _agent_http(e: ComputeAgentError) -> HTTPException:
    """Agent-Fehler → HTTP-Fehler (Agent-5xx → 502)."""
    status = e.status if e.status < 500 else 502
    return HTTPException(status, e.message)


def _safe_ws_path(path: str) -> str:
    """Pfad validieren (relativ, kein ..)."""
    p = str(path).replace("\\", "/").lstrip("/")
    if not p or any(x == ".." for x in p.split("/")):
        raise HTTPException(400, "Ungültiger Dateipfad")
    return p


def _effective_access(session: Session, task: Task, path: str) -> str:
    """Effektive Zugriffs-Klasse eines Pfads (eigene + Ordner-Vorfahren)."""
    fm = workspace_service.folder_map(session, task)
    file_acc = {f.path: f.access
                for f in workspace_service.task_files(session, task)}
    return effective_file_access(path, file_acc.get(path), fm)


def _require_writable(session: Session, task: Task, path: str,
                      privileged: bool) -> None:
    """Schreib-Guard: nur effektive Klasse ✏️ edit.

    👤: für Studenten unsichtbar (404), für Privilegierte read-only
    (403 — private Tests dürfen nicht manipuliert werden). 🔒: 403."""
    p = str(path).replace("\\", "/").lstrip("/")
    acc = _effective_access(session, task, p)
    if acc == "hidden":
        if privileged:
            raise HTTPException(403, "Private Test-Datei ist read-only.")
        raise HTTPException(404, "Datei nicht gefunden.")
    if acc == "readonly":
        raise HTTPException(403, "Diese Datei ist read-only (vom Tutor verwaltet).")


def _ensure(session: Session, task: Task, submission: Submission,
            privileged: bool) -> dict:
    """Review-Container idempotent sicherstellen.

    Starter-Dateien: ✏️ (+ 👤 für Privilegierte); der Lösungszustand
    kommt per First-Restore aus dem Submission-Snapshot (nur beim
    allerersten frischen Volume — danach Sleep-Backup-Flow)."""
    reviewer_id = (WorkspaceService.REVIEW_KEY_OFFSET
                   + submission.id)
    return workspace_service.ensure_workspace(
        session, task, reviewer_id,
        starter_files=workspace_service.review_starter_files(
            session, task, include_hidden=privileged),
        first_restore_tar=snapshot_abs_path(submission.workspace_snapshot),
        hidden_folders=privileged,
    )


# ── Cold-State: Snapshot statt Volume ─────────────────────────────
# Solange der Review-Container (noch) nicht existiert, liefert der
# Dateibaum die Snapshot-Dateien der Einreichung (read-only) — so zeigt
# die eingebettete Abgabe-Ansicht die Lösung, ohne dass ein Container
# läuft. Beim ▶ Starten entsteht der Container aus dem Snapshot.

async def _review_state(client: ComputeClient, key: str) -> str:
    """Echten Zustand des Review-Containers: running / stopped / absent.

    Docker-Status wird normalisiert (exited/created/paused → stopped).
    Agent-Problem → "running" (kein falscher Cold-Fallback — der
    eigentliche Request meldet dann sauber degradiert)."""
    try:
        states = {w.get("key"): w.get("state")
                  for w in await asyncio.to_thread(client.list_workspaces)}
    except ComputeAgentError:
        return "running"
    state = states.get(key) or "absent"
    if state in ("exited", "created", "paused"):
        state = "stopped"
    return state


async def _review_state_absent(client: ComputeClient, key: str) -> bool:
    """True, wenn der Workspace-Container auf der Engine nicht existiert."""
    return await _review_state(client, key) == "absent"


async def _snapshot_tree(session: Session, task: Task,
                         submission: Submission,
                         privileged: bool) -> dict:
    """Dateibaum aus dem Einreichungs-Snapshot (gleiche Struktur wie die
    Live-Liste; Zugriffs-Klassen wie dort, 👤-Filter wie dort)."""
    try:
        raw = await asyncio.to_thread(
            snapshot_list_files, snapshot_abs_path(submission.workspace_snapshot))
    except (OSError, tarfile.TarError, EOFError, zlib.error, ValueError):
        raise HTTPException(422, "Snapshot nicht lesbar (Datei beschädigt?).")
    fm = workspace_service.folder_map(session, task)
    file_rows = {f.path: f for f in workspace_service.task_files(session, task)}
    # Read-only-Mount-Dateien (Skripte, Datasets, 👤-Private-Tests …)
    # liegen NICHT im Snapshot (im Container ist es ein separater
    # ro-Mount) — aus dem Task-Dir ergänzen. 👤 (hidden) nur für
    # Privilegierte (read-only) → Tutor kann private Tests laufen lassen.
    snap_paths = {f["path"] for f in raw}
    for row in file_rows.values():
        if row.path in snap_paths:
            continue
        acc = effective_file_access(row.path, row.access, fm)
        if acc == "hidden" and not privileged:
            continue
        if acc not in ("readonly", "hidden"):
            continue
        dp = file_disk_path(task.id, row.path)
        if not dp.is_file():
            continue
        try:
            with open(dp, "rb") as fh:
                is_binary = b"\x00" in fh.read(1024)
            raw.append({"path": row.path, "size": dp.stat().st_size,
                        "is_binary": is_binary})
        except OSError:
            continue
    out_files = []
    for f in raw:
        p = f["path"]
        row = file_rows.get(p)
        access = effective_file_access(p, row.access if row else None, fm)
        # 👤: für Privilegierte read-only sichtbar, für Studenten unsichtbar.
        if access == "hidden":
            if not privileged:
                continue
            access = "readonly"
        f["access"] = access
        f["file_access"] = row.access if row else None
        f["sort_order"] = row.sort_order if row else None
        out_files.append(f)
    folder_paths = set(fm)
    for f in raw:
        parts = f["path"].split("/")
        for i in range(1, len(parts)):
            folder_paths.add("/".join(parts[:i]))
    out_folders = [{
        "path": p,
        "access": effective_folder_access(p, fm),
        "own": fm.get(p),
    } for p in sorted(folder_paths)
        if effective_folder_access(p, fm) != "hidden"]
    if privileged:
        # 👤-Ordner als read-only anzeigen (wie die Live-Liste).
        hidden_dirs = {p for p, row in fm.items()
                       if effective_folder_access(p, fm) == "hidden"}
        out_paths = {f["path"] for f in out_folders}
        for d in sorted(hidden_dirs - out_paths):
            out_folders.append({"path": d, "access": "readonly",
                                "own": fm.get(d)})
    if privileged:
        # 👤-Ordner als read-only anzeigen (existieren im Snapshot-Tree).
        hidden_dirs = {p for p, row in fm.items()
                       if effective_folder_access(p, fm) == "hidden"}
        shown = {f["path"] for f in out_folders}
        for d in sorted(hidden_dirs - shown):
            out_folders.append({"path": d, "access": "readonly",
                                "own": fm.get(d)})
    return {
        "files": out_files,
        "folders": out_folders,
        "folder_order": workspace_service.order_map(session, task),
        "total": sum(f["size"] for f in out_files),
        "cold": True,
    }


async def _snapshot_read(session: Session, task: Task,
                         submission: Submission,
                         path: str,
                         privileged: bool) -> bytes | None:
    """Datei-Inhalt aus dem Snapshot; Fallback: read-only Task-Datei
    (👤/hidden nur für Privilegierte)."""
    try:
        data = await asyncio.to_thread(
            snapshot_read_file, snapshot_abs_path(submission.workspace_snapshot),
            path)
    except (OSError, tarfile.TarError, EOFError, zlib.error, ValueError):
        raise HTTPException(422, "Snapshot nicht lesbar (Datei beschädigt?).")
    if data is not None:
        return data
    row = workspace_service.get_task_file(session, task, path)
    if not row:
        return None
    fm = workspace_service.folder_map(session, task)
    acc = effective_file_access(path, row.access, fm)
    if acc == "hidden" and not privileged:
        return None
    if acc not in ("readonly", "hidden"):
        return None
    dp = file_disk_path(task.id, path)
    if not dp.is_file():
        return None
    return dp.read_bytes()


# ── Endpoints ──────────────────────────────────────────────────────

@router.get("/tasks/{task_id}/submissions/{submission_id}/workspace/status")
async def review_workspace_status(
    task_id: int,
    submission_id: int,
    ensure: bool = Query(True),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Workspace-Status für die Review-UI (Container, Preset, degraded).

    Nebeneffekt (ensure=true, Default): Container wird idempotent
    gesichert (gestartet/erstellt, beim Erststart aus dem
    Submission-Snapshot befüllt).

    ensure=false (Cold-Instanz, eingebettete Abgabe-Ansicht): kein
    Auto-Start — fehlt der Container, meldet state "absent"."""
    submission, task, privileged = _load_review_context(
        task_id, submission_id, session, user)
    out = {
        "enabled": workspace_service.is_enabled(session, task.course_id),
        "degraded": False,
        "agent": None,
        "container": None,
        "error": None,
        "main_file": task.workspace_main_file,
        "timeout": task.workspace_timeout,
        "memory": task.workspace_memory,
        "assets": [],
        "disk": None,
        "ports": [],
        "review": True,
        "privileged": privileged,
        "submission": {
            "id": submission.id,
            "submitted_at": submission.submitted_at.isoformat()
            if submission.submitted_at else None,
            "student": None,
        },
    }
    if not out["enabled"]:
        out["degraded"] = True
        out["error"] = "Workspace-Aufgaben sind derzeit deaktiviert."
        return out

    not_ready = workspace_service.validate_task_ready(session, task)
    if not_ready:
        out["degraded"] = True
        out["error"] = not_ready
        return out

    student = session.get(User, submission.student_id)
    out["submission"]["student"] = {
        "id": student.id,
        "name": student.name,
        "email": student.email,
    } if student else None

    try:
        client = _client_or_error(session, task)
    except HTTPException as e:
        out["degraded"] = True
        out["error"] = e.detail
        return out

    key = _key(task, submission)
    try:
        if ensure:
            # Idempotent sichern (gestartet/erstellt, Holds bleiben
            # erhalten, s. Agent).
            ws = await asyncio.to_thread(
                _ensure, session, task, submission, privileged)
            out["agent"] = ws.get("agent")
            out["container"] = {"state": ws.get("state"),
                                "fresh": bool(ws.get("fresh")),
                                "held": ws.get("held")}
        else:
            # Cold-Ansicht: NIEMALS (re-)starten — den echten Zustand
            # nur melden (gestoppter Container bleibt gestoppt).
            # (Hier ohne _review_state-Catch: Agent-Down muss als
            # degraded durchkommen.)
            states = {w.get("key"): w.get("state")
                      for w in await asyncio.to_thread(
                          client.list_workspaces)}
            state = states.get(key) or "absent"
            if state in ("exited", "created", "paused"):
                state = "stopped"
            out["container"] = {"state": state}
        assets = await asyncio.to_thread(
            client.list_assets, task.course_id, task.id)
        out["assets"] = [{"path": a.get("path"), "size": a.get("size")}
                         for a in assets]
        try:
            out["disk"] = await asyncio.to_thread(client.disk, key)
        except ComputeAgentError:
            out["disk"] = None
        try:
            out["ports"] = await asyncio.to_thread(client.ports, key)
        except ComputeAgentError:
            out["ports"] = []
    except ComputeAgentError as e:
        out["degraded"] = True
        out["error"] = e.message
    return out


# Eigene Jinja-Instanz (main.templates nicht importierbar → Zirkularimport).
# Die Partial nutzen keine Custom-Filter (asset() lädt die Seite selbst).
_frag_templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@router.get(
    "/tasks/{task_id}/submissions/{submission_id}/workspace/ide",
    include_in_schema=False,
)
async def review_workspace_ide_fragment(
    task_id: int,
    submission_id: int,
    request: Request,
    part: str = Query("markup", pattern="^(markup|script)$"),
    inst: str = Query("frag", pattern="^[A-Za-z0-9_-]{1,32}$"),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Server-rendered IDE-Fragment für JS-generierte Seiten (Tutor-
    Abgabe-Ansicht, dynamische Neue-Abgabe-Karte).

    part=markup → HTML-Partial, part=script → JS-IIFE (die Seite führt
    es per <script>-Element aus; kein CSP). inst = eindeutiger
    Id-Suffix pro Instanz auf derselben Seite."""
    submission, task, _privileged = _load_review_context(
        task_id, submission_id, session, user)
    ctx = {
        "request": request,
        "task": {"id": task.id,
                 "workspace_main_file": task.workspace_main_file,
                 "description": task.description},
        "current_user": {"id": user.id},
        "preview_base_domain": PREVIEW_BASE_DOMAIN or None,
        "ws_inst": inst,
        "ws_api": f"/api/review/tasks/{task.id}/submissions/"
                  f"{submission.id}/workspace",
        "ws_review_id": submission.id,
        "ws_auto_start": False,
        "ws_title": "",  # eingebettet: kein Heading (Kontext oben)
        "ws_show_timer": False,
        "ws_show_grading": False,
        "ws_show_submit": False,
        "ws_show_taskdesc": True,  # wie Live-Workspace (Bar-Klick-Binding)
        "ws_reset_label": "↺ Zur Einreichung zurücksetzen",
        "ws_reset_title": "Auf den eingereichten Stand zurücksetzen "
                          "(Änderungen in der Nachberechnung gehen verloren!)",
    }
    name = ("partials/workspace_ide.html" if part == "markup"
            else "partials/workspace_ide_scripts.html")
    resp = _frag_templates.TemplateResponse(request, name, ctx)
    if part == "script":
        resp.headers["Content-Type"] = "text/javascript; charset=utf-8"
    return resp


@router.post("/tasks/{task_id}/submissions/{submission_id}/workspace/ports/{port}/kill")
async def review_workspace_port_kill(
    task_id: int,
    submission_id: int,
    port: int,
    force: bool = False,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Prozess eines lauschenden Ports beenden (Preview-UI: × beim Port)."""
    submission, task, _privileged = _load_review_context(
        task_id, submission_id, session, user)
    client = _client_or_error(session, task)
    try:
        await asyncio.to_thread(
            client.kill_port, _key(task, submission), port, force)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True}


@router.websocket("/tasks/{task_id}/submissions/{submission_id}/workspace/terminal")
async def review_workspace_terminal(ws: WebSocket,
                                    task_id: int, submission_id: int) -> None:
    """Terminal (PTY) im Review-Workspace.

    1:1-WS-Weiterleitung zum Agenten (Token per Query-Param). Optional
    ?cmd=<relativer Pfad>: PTY startet das Skript direkt (Play-Button
    im Dateibaum). 👤-Dateien dürfen nur Privilegierte ausführen."""
    token = ws.cookies.get("access_token", "")
    payload = decode_access_token(token) if token else None
    user_id = payload.get("sub") if payload else None
    if not user_id:
        await ws.close(code=4401)
        return
    await ws.accept()

    agent_url = agent_key = key = cmd_path = None
    reviewer_id = None
    try:
        with Session(engine) as session:
            user = session.get(User, user_id)
            if user is None:
                await _ws_error_close(ws, "Nicht authentifiziert.")
                return
            submission, task, privileged = _load_review_context(
                task_id, submission_id, session, user)
            try:
                if not workspace_service.is_enabled(session, task.course_id):
                    raise HTTPException(
                        503, "Workspace-Aufgaben sind derzeit deaktiviert.")
                not_ready = workspace_service.validate_task_ready(session, task)
                if not_ready:
                    raise HTTPException(503, not_ready)
                agent = workspace_service.pick_task_agent(session, task)
                if agent is None:
                    raise HTTPException(
                        503, workspace_service.pick_agent_error(
                            session, task.course_id,
                            workspace_service.task_engine_names(task)))
                agent_url = agent["url"]
                agent_key = agent.get("key") or ""
            except HTTPException as e:
                await _ws_error_close(ws, str(e.detail))
                return
            key = _key(task, submission)
            reviewer_id = (WorkspaceService.REVIEW_KEY_OFFSET
                           + submission.id)
            raw_cmd = ws.query_params.get("cmd")
            if raw_cmd:
                cmd_path = _safe_ws_path(raw_cmd)
                if _effective_access(session, task, cmd_path) == "hidden" \
                        and not privileged:
                    raise HTTPException(404, "Datei nicht gefunden.")
    except HTTPException as e:
        await _ws_error_close(ws, str(e.detail))
        return
    except Exception:
        logger.warning("Review-Terminal: Setup fehlgeschlagen", exc_info=True)
        await _ws_error_close(ws, "Terminal derzeit nicht verfügbar.")
        return

    agent_token = make_token(agent_key, f"ws:{key}",
                             task_id=task_id, student_id=reviewer_id)
    uri = (agent_url.replace("https://", "wss://")
                  .replace("http://", "ws://").rstrip("/")
           + f"/workspaces/{key}/terminal"
           + f"?token={quote(agent_token, safe='')}"
           + (f"&cmd={quote(cmd_path, safe='')}" if cmd_path else ""))
    try:
        async with websockets.connect(uri, max_size=None, open_timeout=10) as up:
            async def _browser_to_agent() -> None:
                try:
                    while True:
                        msg = await ws.receive()
                        mtype = msg.get("type")
                        if mtype == "websocket.disconnect":
                            return
                        if mtype != "websocket.receive":
                            continue
                        if msg.get("bytes") is not None:
                            await up.send(msg["bytes"])
                        elif msg.get("text") is not None:
                            await up.send(msg["text"])
                except WebSocketDisconnect:
                    pass
                except Exception:
                    pass

            async def _agent_to_browser() -> None:
                try:
                    async for msg in up:
                        if isinstance(msg, (bytes, bytearray)):
                            await ws.send_bytes(bytes(msg))
                        else:
                            await ws.send_text(msg)
                except Exception:
                    pass

            t1 = asyncio.create_task(_browser_to_agent())
            t2 = asyncio.create_task(_agent_to_browser())
            try:
                await asyncio.wait(
                    {t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in (t1, t2):
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
    except Exception:
        logger.warning("Review-Terminal: Agent-WS fehlgeschlagen",
                       exc_info=True)
        await _ws_error_close(ws, "Terminal-Verbindung fehlgeschlagen.")
        return
    try:
        await ws.close()
    except Exception:
        pass


async def _ws_error_close(ws: WebSocket, message: str) -> None:
    """WS-Fehlermeldung (JSON) + Close — für die Terminal-Route."""
    try:
        await ws.send_text(json.dumps({"type": "error", "message": message}))
    except Exception:
        pass
    try:
        await ws.close(code=1011)
    except Exception:
        pass


@router.get("/tasks/{task_id}/submissions/{submission_id}/workspace/files")
async def review_workspace_list_files(
    task_id: int,
    submission_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Dateiliste des Review-Containers (/workspace).

    Cold-State (Container fehlt ODER ist gestoppt): Snapshot-Dateien
    der Einreichung (read-only) statt Volume. Pre-Check auf "running":
    der Agent würde einen gestoppten Container ohne Hold beim
    File-Listing on-demand (re-)starten."""
    submission, task, privileged = _load_review_context(
        task_id, submission_id, session, user)
    client = _client_or_error(session, task)
    key = _key(task, submission)
    if await _review_state(client, key) != "running":
        return JSONResponse(
            content=await _snapshot_tree(session, task, submission, privileged),
            headers={"Cache-Control": "no-store"})
    try:
        files = await asyncio.to_thread(client.list_files, key)
    except ComputeAgentError as e:
        # Race: Container wurde zwischen Check und Call gestoppt.
        if e.status in (404, 409):
            return JSONResponse(
                content=await _snapshot_tree(session, task, submission, privileged),
                headers={"Cache-Control": "no-store"})
        raise _agent_http(e)
    fm = workspace_service.folder_map(session, task)
    file_rows = {f.path: f
                 for f in workspace_service.task_files(session, task)}
    out_files = []
    for f in files:
        p = str(f.get("path") or "")
        row = file_rows.get(p)
        access = effective_file_access(p, row.access if row else None, fm)
        # 👤: für Privilegierte als read-only sichtbar, für Studenten
        # unsichtbar.
        if access == "hidden":
            if not privileged:
                continue
            access = "readonly"
        f["access"] = access
        f["sort_order"] = row.sort_order if row else None
        out_files.append(f)
    files = out_files
    try:
        vol_dirs = await asyncio.to_thread(client.list_dirs, key)
    except ComputeAgentError:
        vol_dirs = []
    folder_paths = set(fm)
    for d in vol_dirs:
        if d and d not in folder_paths:
            folder_paths.add(d)
    out_folders = [{
        "path": p,
        "access": effective_folder_access(p, fm),
        "own": fm.get(p),
    } for p in sorted(folder_paths)
        if effective_folder_access(p, fm) != "hidden"]
    if privileged:
        # 👤-Ordner als read-only anzeigen (existieren im Volume).
        hidden_dirs = {p for p, row in fm.items()
                       if effective_folder_access(p, fm) == "hidden"}
        out_dirs = {f["path"] for f in out_folders}
        for d in sorted(hidden_dirs - out_dirs):
            out_folders.append({"path": d, "access": "readonly",
                                "own": fm.get(d)})
    return JSONResponse(
        content={
            "files": files,
            "folders": out_folders,
            "folder_order": workspace_service.order_map(session, task),
            "total": sum(f.get("size", 0) for f in files),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/tasks/{task_id}/submissions/{submission_id}/workspace/files/{path:path}")
async def review_workspace_read_file(
    task_id: int,
    submission_id: int,
    path: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Datei-Inhalt laden (für den Editor). Binarys: als octet-stream.

    Cold-State (Container fehlt ODER ist gestoppt): Inhalt aus dem
    Einreichungs-Snapshot (Fallback: read-only Task-Datei). Pre-Check
    auf "running" — s. review_workspace_list_files."""
    submission, task, privileged = _load_review_context(
        task_id, submission_id, session, user)
    client = _client_or_error(session, task)
    key = _key(task, submission)
    p = _safe_ws_path(path)
    if _effective_access(session, task, p) == "hidden" and not privileged:
        raise HTTPException(404, "Datei nicht gefunden.")
    if await _review_state(client, key) != "running":
        data = await _snapshot_read(session, task, submission, p, privileged)
        if data is None:
            raise HTTPException(404, "Datei nicht gefunden.")
    else:
        try:
            data = await asyncio.to_thread(client.read_file, key, p)
        except ComputeAgentError as e:
            # Race: Container wurde zwischen Check und Call gestoppt.
            if e.status in (404, 409):
                data = await _snapshot_read(
                    session, task, submission, p, privileged)
                if data is None:
                    raise HTTPException(404, "Datei nicht gefunden.")
            else:
                raise _agent_http(e)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei zu groß für den Editor (max. 50 MB).")
    is_binary = b"\x00" in data[:1024]
    headers = {"Cache-Control": "no-store"}
    if is_binary:
        headers["Content-Disposition"] = f'attachment; filename="{p.split("/")[-1]}"'
    return Response(
        content=data,
        media_type="application/octet-stream" if is_binary else "text/plain; charset=utf-8",
        headers=headers,
    )


@router.put("/tasks/{task_id}/submissions/{submission_id}/workspace/files/{path:path}")
async def review_workspace_write_file(
    task_id: int,
    submission_id: int,
    path: str,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Textdatei speichern (Editor-Save)."""
    submission, task, privileged = _load_review_context(
        task_id, submission_id, session, user)
    client = _client_or_error(session, task)
    key = _key(task, submission)
    p = _safe_ws_path(path)
    _require_writable(session, task, p, privileged)
    body = await request.json()
    content = body.get("content")
    if not isinstance(content, str):
        raise HTTPException(422, "Feld 'content' (String) fehlt.")
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei zu groß (max. 50 MB).")
    try:
        result = await asyncio.to_thread(client.write_file, key, p, data)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True, "path": p, "size": result.get("size")}


@router.post("/tasks/{task_id}/submissions/{submission_id}/workspace/files")
async def review_workspace_create_file(
    task_id: int,
    submission_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Neue Textdatei anlegen ({path, content})."""
    submission, task, privileged = _load_review_context(
        task_id, submission_id, session, user)
    client = _client_or_error(session, task)
    key = _key(task, submission)
    body = await request.json()
    if not body.get("path"):
        raise HTTPException(422, "Feld 'path' fehlt.")
    p = _safe_ws_path(str(body["path"]))
    _require_writable(session, task, p, privileged)
    content = body.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise HTTPException(422, "Feld 'content' muss ein String sein.")
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei zu groß (max. 50 MB).")
    try:
        result = await asyncio.to_thread(client.write_file, key, p, data)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True, "path": p, "size": result.get("size")}


@router.post("/tasks/{task_id}/submissions/{submission_id}/workspace/upload")
async def review_workspace_upload_file(
    task_id: int,
    submission_id: int,
    file: UploadFile = File(...),
    path: str = Form(...),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Binary-Upload in den Review-Container."""
    submission, task, privileged = _load_review_context(
        task_id, submission_id, session, user)
    client = _client_or_error(session, task)
    key = _key(task, submission)
    p = _safe_ws_path(path)
    _require_writable(session, task, p, privileged)
    data = await file.read()
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei zu groß (max. 50 MB).")
    try:
        result = await asyncio.to_thread(client.write_file, key, p, data)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True, "path": p, "size": result.get("size")}


@router.post("/tasks/{task_id}/submissions/{submission_id}/workspace/files/move")
async def review_workspace_move_file(
    task_id: int,
    submission_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Datei verschieben/umbenennen ({src, dst}) — nur innerhalb der
    editierbaren Bereiche."""
    submission, task, privileged = _load_review_context(
        task_id, submission_id, session, user)
    client = _client_or_error(session, task)
    key = _key(task, submission)
    body = await request.json()
    src = _safe_ws_path(str(body.get("src") or ""))
    dst = _safe_ws_path(str(body.get("dst") or ""))
    src_acc = _effective_access(session, task, src)
    if src_acc == "hidden":
        raise HTTPException(
            404 if not privileged else 403,
            "Datei nicht gefunden." if not privileged
            else "Private Test-Datei ist read-only.")
    if src_acc != "edit" or _effective_access(session, task, dst) != "edit":
        raise HTTPException(
            403, "Verschieben ist nur innerhalb der editierbaren Bereiche möglich.")
    try:
        await asyncio.to_thread(client.move_file, key, src, dst)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True, "path": dst}


@router.delete("/tasks/{task_id}/submissions/{submission_id}/workspace/files/{path:path}")
async def review_workspace_delete_file(
    task_id: int,
    submission_id: int,
    path: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Datei aus dem Review-Container löschen."""
    submission, task, privileged = _load_review_context(
        task_id, submission_id, session, user)
    client = _client_or_error(session, task)
    key = _key(task, submission)
    p = _safe_ws_path(path)
    _require_writable(session, task, p, privileged)
    try:
        await asyncio.to_thread(client.delete_file, key, p)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True}


@router.post("/tasks/{task_id}/submissions/{submission_id}/workspace/folders")
async def review_workspace_create_folder(
    task_id: int,
    submission_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """(Möglicherweise leeren) Verzeichnis anlegen ({path})."""
    submission, task, privileged = _load_review_context(
        task_id, submission_id, session, user)
    client = _client_or_error(session, task)
    key = _key(task, submission)
    body = await request.json()
    p = _safe_ws_path(str(body.get("path") or ""))
    _require_writable(session, task, p, privileged)
    try:
        await asyncio.to_thread(client.create_dir, key, p)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True, "path": p}


@router.post("/tasks/{task_id}/submissions/{submission_id}/workspace/folders/move")
async def review_workspace_move_folder(
    task_id: int,
    submission_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Verzeichnis verschieben/umbenennen ({src, dst}) — nur innerhalb
    der editierbaren Bereiche."""
    submission, task, privileged = _load_review_context(
        task_id, submission_id, session, user)
    client = _client_or_error(session, task)
    key = _key(task, submission)
    body = await request.json()
    src = _safe_ws_path(str(body.get("src") or ""))
    dst = _safe_ws_path(str(body.get("dst") or ""))
    if src == dst:
        raise HTTPException(400, "Quelle und Ziel sind identisch.")
    if dst.startswith(src + "/"):
        raise HTTPException(400, "Ein Ordner kann nicht in sich selbst verschoben werden.")
    fm = workspace_service.folder_map(session, task)
    src_acc = effective_folder_access(src, fm)
    if src_acc == "hidden" and not privileged:
        raise HTTPException(404, "Ordner nicht gefunden.")
    if src_acc != "edit" or effective_folder_access(dst, fm) != "edit":
        raise HTTPException(
            403, "Verschieben ist nur innerhalb der editierbaren Bereiche möglich.")
    try:
        await asyncio.to_thread(client.move_file, key, src, dst)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True, "path": dst}


@router.delete("/tasks/{task_id}/submissions/{submission_id}/workspace/folders/{path:path}")
async def review_workspace_delete_folder(
    task_id: int,
    submission_id: int,
    path: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Verzeichnis inkl. Inhalt aus dem Review-Container löschen."""
    submission, task, privileged = _load_review_context(
        task_id, submission_id, session, user)
    client = _client_or_error(session, task)
    key = _key(task, submission)
    p = _safe_ws_path(path)
    fm = workspace_service.folder_map(session, task)
    acc = effective_folder_access(p, fm)
    if acc == "hidden" and not privileged:
        raise HTTPException(404, "Ordner nicht gefunden.")
    if acc != "edit":
        raise HTTPException(403, "Dieser Ordner ist read-only.")
    try:
        await asyncio.to_thread(client.delete_file, key, p)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True}


@router.post("/tasks/{task_id}/submissions/{submission_id}/workspace/reset")
async def review_workspace_reset(
    task_id: int,
    submission_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Auf die EINGEREICHTE Lösung zurücksetzen.

    Löscht Review-Container + Volume + Sleep-Backup; beim nächsten
    Status-Call wird der Workspace aus dem Submission-Snapshot neu
    gebaut. Die Einreichung selbst bleibt unberührt."""
    submission, task, _privileged = _load_review_context(
        task_id, submission_id, session, user)
    await asyncio.to_thread(
        workspace_service.delete_review_workspace, session, task,
        submission.id)
    return {"ok": True}


@router.post("/tasks/{task_id}/submissions/{submission_id}/workspace/stop")
async def review_workspace_stop(
    task_id: int,
    submission_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Review-Container stoppen — Test-Edits werden VERWORFEN (nächster
    Start = frische Einreichung, wie ↺ Zurücksetzen).

    Bewusst anders als der Live-Workspace (dort bleibt das Volume):
    der Review-Container ist Wegwerf-Umgebung, der Kanon-Zustand ist
    immer die Einreichung."""
    submission, task, _privileged = _load_review_context(
        task_id, submission_id, session, user)
    await asyncio.to_thread(
        workspace_service.delete_review_workspace, session, task,
        submission.id)
    return {"ok": True}


@router.post("/tasks/{task_id}/submissions/{submission_id}/workspace/start")
async def review_workspace_start(
    task_id: int,
    submission_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Review-Container explizit starten (hebt die manuelle Stop-Sperre).

    Review-Semantik: Start = EINGEREICHTE Dateien. Cold-Start
    (Container fehlt): aus dem Einreichungs-Snapshot anlegen;
    Rest-Container (gestoppt, z. B. nach Agent-Neustart): löschen
    und neu bauen (alte Test-Edits nicht wiederbeleben)."""
    submission, task, privileged = _load_review_context(
        task_id, submission_id, session, user)
    client = _client_or_error(session, task)
    key = _key(task, submission)
    try:
        state = await _review_state(client, key)
        if state == "stopped":
            await asyncio.to_thread(
                workspace_service.delete_review_workspace,
                session, task, submission.id)
        if state in ("stopped", "absent"):
            await asyncio.to_thread(
                _ensure, session, task, submission, privileged)
        await asyncio.to_thread(client.start_workspace, key)
    except ComputeAgentError as e:
        raise _agent_http(e)
    return {"ok": True}
