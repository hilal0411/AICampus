"""
Interne AICampus-Endpoints für den Compute-Agenten (Agent → Server).

Auth: derselbe HMAC-Op-Token-Schema wie Server → Agent, nur umgekehrte
Richtung — der Agent signiert mit seinem AGENT_KEY, wir verifizieren gegen
ALLE registrierten Engine-Keys (global + Kurs-Overrides; es gibt nur
wenige Engines, die HMAC-Prüfung ist billig). Kein Session-Zugriff.
"""

import json
import logging
import os
import re
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlmodel import Session, select

from compute_agent.auth import verify_token_with_key
from config import COMPUTE_AGENT_KEY, WORKSPACE_BACKUP_DIR
from database.base import get_session
from database.models import CourseSettings, GlobalSettings, Task
from services import settings_resolver

logger = logging.getLogger("agent_api")

router = APIRouter(prefix="/api/agent", tags=["agent"])

_KEY_RE = re.compile(r"^ws-(\d+)-(\d+)-(\d+)$")
# Harte Obergrenze (der Agent cappt ohnehin auf Task-Quota bzw. 1 GB).
_MAX_SIZE = 8 * 1024 * 1024 * 1024  # 8 GB


def _all_engine_keys(session: Session) -> list[tuple[str, str]]:
    """(Name, Key)-Paare aller registrierten Engines (global + Kurs-Overrides)."""
    out: list[tuple[str, str]] = []
    gs = session.exec(select(GlobalSettings)).first()
    global_agents = (
        settings_resolver._parse_agent_list(getattr(gs, "compute_agents", None))
        if gs else [])
    for a in global_agents:
        if a.get("key"):
            out.append((str(a.get("name") or a.get("url")), str(a["key"])))
    if not global_agents and COMPUTE_AGENT_KEY:
        out.append(("local", COMPUTE_AGENT_KEY))
    for cs in session.exec(select(CourseSettings)).all():
        for a in settings_resolver._parse_agent_list(cs.compute_agents):
            if a.get("key"):
                out.append((str(a.get("name") or a.get("url")), str(a["key"])))
    return out


@router.post("/workspace-backup")
async def workspace_backup(
    request: Request,
    session: Session = Depends(get_session),
    token: str | None = Header(default=None, alias="X-Agent-Token"),
):
    """Sleep-Backup annehmen (tar.gz eines Workspace-Volumes).

    Akzeptiert nur mit gültigem Token einer registrierten Engine und
    nur wenn die referenzierte Aufgabe existiert (keine Waisen-Backups).
    Überschreibt ein vorhandenes Backup desselben Keys (ein Stand je
    Session; Meta `at` = Zeitstempel der letzten Aktivität).
    """
    payload, engine = None, None
    for name, key in _all_engine_keys(session):
        payload = verify_token_with_key(token, key)
        if payload:
            engine = name
            break
    if payload is None:
        raise HTTPException(status_code=401, detail="Token ungültig")
    op = str(payload.get("op") or "")
    if not op.startswith("ws-backup:"):
        raise HTTPException(status_code=403, detail="Token-Scope passt nicht")
    ws_key = op[len("ws-backup:"):]
    m = _KEY_RE.match(ws_key)
    if not m:
        raise HTTPException(status_code=400, detail="Ungültiger Workspace-Key")
    course_id, task_id, student_id = int(m.group(1)), int(m.group(2)), int(m.group(3))
    task = session.exec(select(Task).where(
        Task.id == task_id, Task.course_id == course_id)).first()
    if task is None:
        raise HTTPException(status_code=404, detail="Aufgabe für Backup unbekannt")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Body fehlt")
    if len(data) > _MAX_SIZE:
        raise HTTPException(status_code=413, detail="Backup zu groß")
    tar_path = WORKSPACE_BACKUP_DIR / f"{ws_key}.tar.gz"
    tmp = tar_path.with_name(tar_path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, tar_path)
    meta_path = tar_path.with_name(tar_path.name.replace(".tar.gz", ".json"))
    meta_path.write_text(json.dumps({
        "key": ws_key, "course_id": course_id, "task_id": task_id,
        "student_id": student_id, "size": len(data),
        "engine": engine, "at": time.time(),
    }, sort_keys=True), encoding="utf-8")
    logger.info("sleep-backup gespeichert: %s (%d bytes, %s)",
                ws_key, len(data), engine)
    return {"ok": True, "size": len(data)}
