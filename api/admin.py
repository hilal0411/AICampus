"""
Admin-Endpoints: Kurs-Management + User-Verwaltung + Systemeinstellungen.

Rollen: Administrator (global)
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from typing import Optional
from pydantic import BaseModel
from sqlmodel import Session, select

from config import COMPUTE_AGENT_KEY, COMPUTE_AGENT_URL, COMPUTE_AGENT_URL_EXPLICIT
from database.base import get_session
from database.soft_delete import SOFT_DELETE_BYPASS
from database.models import (
    Course,
    CourseCreate,
    CourseRole,
    CourseSettings,
    CourseSettingsUpdate,
    ForumChannel,
    GlobalSettings,
    GlobalSettingsUpdate,
    GlobalSettingsRead,
    GlobalUserRole,
    User,
    UserCourse,
)
from services import course_service, user_service
from services.timeutil import utc_iso
from services.auth_service import hash_password, require_global_admin
from services.llm_service import get_llm_debug_entry, get_llm_debug_log
from services.llm_service import record_llm_debug_entry
from services.settings_resolver import get_effective_llm_config

router = APIRouter(prefix="/api/admin", tags=["Admin"])
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# KURSE
# ═══════════════════════════════════════════════════════════════════

@router.get("/courses")
async def list_courses(
    session: Session = Depends(get_session),
    user: User = Depends(require_global_admin()),
):
    """Alle Kurse auflisten."""
    courses = session.exec(select(Course)).all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "semester": c.semester,
            "created_at": utc_iso(c.created_at),
            "member_count": len(c.course_members),
        }
        for c in courses
    ]


@router.post("/courses")
async def create_course(
    data: CourseCreate,
    session: Session = Depends(get_session),
    user: User = Depends(require_global_admin()),
):
    """Neuen Kurs erstellen."""
    course = Course(
        name=data.name,
        description=data.description,
        semester=data.semester,
        created_by=user.id,  # type: ignore[arg-type]
    )
    session.add(course)
    session.commit()
    session.refresh(course)
    
    # Admin automatisch als Kurs-Member mit PROF-Rolle hinzufügen
    membership = UserCourse(
        user_id=user.id,  # type: ignore[arg-type]
        course_id=course.id,  # type: ignore[arg-type]
        role_in_course=CourseRole.PROF,
    )
    session.add(membership)
    session.commit()

    # Default-Forum-Kanal anlegen
    session.add(
        ForumChannel(
            course_id=course.id,  # type: ignore[arg-type]
            name="Allgemein",
            created_by=user.id,  # type: ignore[arg-type]
        )
    )
    session.commit()

    return {
        "message": f"Kurs '{course.name}' erstellt.",
        "course": {
            "id": course.id,
            "name": course.name,
            "semester": course.semester,
        },
    }


class CourseDuplicateRequest(BaseModel):
    name: str
    description: str
    semester: str


@router.post("/courses/{course_id}/duplicate")
async def duplicate_course(
    course_id: int,
    data: CourseDuplicateRequest,
    session: Session = Depends(get_session),
    user: User = Depends(require_global_admin()),
):
    """Kurs duplizieren (Inhalt + Medien, ohne Mitglieder/Studierende-Daten)."""
    source_course = session.get(Course, course_id)
    if not source_course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    result = course_service.duplicate_course_content(
        session, user, source_course, data.name, data.description, data.semester
    )
    new_course = result["course"]
    return {
        "message": f"Kurs '{data.name}' aus '{source_course.name}' dupliziert.",
        "course": {
            "id": new_course.id,
            "name": new_course.name,
            "semester": new_course.semester,
            "task_count": result["task_count"],
            "section_count": result["section_count"],
            "material_count": result["material_count"],
        },
    }


@router.delete("/courses/{course_id}")
async def delete_course(
    course_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_global_admin()),
):
    """Kurs in den Papierkorb legen (reversibel, ohne Kaskade).

    Wiederherstellung: Admin-Konsole (♻️) oder per `POST .../restore`.
    Endgültige Löschung nur via CLI-Purge (scripts/purge_trash.py).
    """
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    course_name = course_service.soft_delete_course(session, course)
    return {"message": f"Kurs '{course_name}' in den Papierkorb gelegt."}


@router.post("/courses/{course_id}/restore")
async def restore_course(
    course_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_global_admin()),
):
    """Soft gelöschten Kurs wiederherstellen (Inhalt blieb erhalten)."""
    course = session.get(
        Course, course_id, execution_options=SOFT_DELETE_BYPASS
    )
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")
    if course.deleted_at is None:
        return {"message": f"Kurs '{course.name}' ist nicht gelöscht."}
    course_service.restore_course(session, course)
    return {"message": f"Kurs '{course.name}' wiederhergestellt."}


# ═══════════════════════════════════════════════════════════════════
# USER-MANAGEMENT
# ═══════════════════════════════════════════════════════════════════

@router.get("/users")
async def list_users(
    session: Session = Depends(get_session),
    user: User = Depends(require_global_admin()),
):
    """Alle User auflisten."""
    users = session.exec(select(User)).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "name": u.name,
            "email": u.email,
            "role": u.role.value,
        }
        for u in users
    ]


@router.post("/users")
async def create_user(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(require_global_admin()),
):
    """Neuen User erstellen (Admin-only). Always GlobalUserRole.USER."""
    body = await request.json()
    
    username = body.get("username", "").strip()
    email = body.get("email", "").strip()
    name = body.get("name", "").strip()
    password = body.get("password", "")
    
    if not all([username, email, name]):
        raise HTTPException(400, "Username, Email und Name erforderlich.")
    
    # Exists-Check
    existing = session.exec(
        select(User).where(User.username == username)
    ).first()
    if existing:
        raise HTTPException(400, f"Username '{username}' existiert bereits.")
    
    pw_hash = hash_password(password) if password else None
    
    new_user = User(
        username=username,
        email=email,
        name=name,
        role=GlobalUserRole.USER,
        password_hash=pw_hash,
    )
    
    session.add(new_user)
    session.commit()
    session.refresh(new_user)
    
    return {
        "message": f"User '{username}' erstellt.",
        "user": {
            "id": new_user.id,
            "username": new_user.username,
            "name": new_user.name,
            "role": new_user.role.value,
        },
    }


@router.put("/users/{user_id}")
async def update_user(
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
    current_user: User = Depends(require_global_admin()),
):
    """User-Daten aktualisieren (Name, Email, Password, globale Rolle)."""
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(404, "User nicht gefunden.")
    
    body = await request.json()
    
    if "name" in body:
        user.name = body["name"]
    if "email" in body:
        user.email = body["email"]
    if "role" in body:
        # Erlaube Änderung der globalen Rolle (z.B. USER -> ADMIN oder umgekehrt)
        user.role = GlobalUserRole(body["role"].upper())
    if "password" in body and body["password"]:
        user.password_hash = hash_password(body["password"])
    
    session.add(user)
    session.commit()
    session.refresh(user)
    
    return {
        "message": f"User '{user.username}' aktualisiert.",
        "user": {
            "id": user.id,
            "username": user.username,
            "name": user.name,
            "email": user.email,
            "role": user.role.value,
        },
    }


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(require_global_admin()),
):
    """User in den Papierkorb legen: anonymisiert + persönliche Daten hart
    gelöscht (Art. 17). Eigene Kurse und erstellter Kurs-Inhalt bleiben dem
    Institut erhalten. Wiederherstellung: Admin-Konsole (♻️) — Username/Name/
    Email müssen dann über den Edit-Dialog wieder eingetragen werden.
    """
    if user_id == current_user.id:
        raise HTTPException(400, "Du kannst dich selbst nicht löschen.")

    user = session.get(User, user_id)
    if not user:
        raise HTTPException(404, "User nicht gefunden.")

    username = user_service.soft_delete_user(session, user)
    return {"message": f"User '{username}' gelöscht (anonymisiert, persönliche Daten entfernt)."}


@router.post("/users/{user_id}/restore")
async def restore_user(
    user_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(require_global_admin()),
):
    """Soft gelöschten User wiederherstellen (Anker-Zeile bleibt anonymisiert;
    Username/Name/Email per Edit-Dialog neu eintragen)."""
    user = session.get(User, user_id, execution_options=SOFT_DELETE_BYPASS)
    if not user:
        raise HTTPException(404, "User nicht gefunden.")
    if user.deleted_at is None:
        return {"message": f"User '{user.username}' ist nicht gelöscht."}
    user_service.restore_user(session, user)
    return {"message": f"User (ID {user.id}) wiederhergestellt — bitte Username/Name/Email im Edit-Dialog eintragen."}


# ═══════════════════════════════════════════════════════════════════
# KURS-MITGLIEDER (Admin)
# ═══════════════════════════════════════════════════════════════════

@router.post("/courses/{course_id}/members")
async def add_members_to_course(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    current_user: User = Depends(require_global_admin()),
):
    """
    User zu einem Kurs hinzufügen (Admin-only).
    
    Request:
        {
            "user_ids": [1, 2, 3],
            "role_in_course": "prof" | "tutor" | "student"
        }
    """
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")
    
    body = await request.json()
    user_ids = body.get("user_ids", [])
    role_in_course = CourseRole(body.get("role_in_course", "STUDENT").upper())
    
    added = []
    for uid in user_ids:
        user = session.get(User, uid)
        if not user:
            continue
        
        # Prüfen, ob schon Mitglied
        existing = session.exec(
            select(UserCourse)
            .where(UserCourse.user_id == uid)
            .where(UserCourse.course_id == course_id)
        ).first()
        
        if existing:
            existing.role_in_course = role_in_course
            session.add(existing)
        else:
            membership = UserCourse(
                user_id=uid,
                course_id=course_id,
                role_in_course=role_in_course,
            )
            session.add(membership)
        
        added.append({"user_id": uid, "username": user.username})
    
    session.commit()
    
    return {
        "message": f"{len(added)} Mitglieder hinzugefügt.",
        "added": added,
    }


@router.get("/courses/{course_id}/members")
async def list_course_members(
    course_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(require_global_admin()),
):
    """Alle Mitglieder eines Kurses auflisten."""
    members = session.exec(
        select(UserCourse).where(UserCourse.course_id == course_id)
    ).all()
    
    return [
        {
            "id": m.id,
            "user_id": m.user_id,
            "username": m.user.username if m.user else "unknown",
            "name": m.user.name if m.user else "unknown",
            "role_in_course": m.role_in_course.value,
        }
        for m in members
    ]


@router.delete("/courses/{course_id}/members/{user_id}")
async def remove_member_from_course(
    course_id: int,
    user_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(require_global_admin()),
):
    """Mitglied aus Kurs entfernen."""
    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user_id)
        .where(UserCourse.course_id == course_id)
    ).first()
    
    if not membership:
        raise HTTPException(404, "Mitgliedschaft nicht gefunden.")
    
    session.delete(membership)
    session.commit()
    return {"message": "Mitglied entfernt."}


# ═══════════════════════════════════════════════════════════════════
# SYSTEMEINSTELLUNGEN (global — Admin-Dashboard)
# ═══════════════════════════════════════════════════════════════════

def _ensure_global_settings(session: Session) -> GlobalSettings:
    """Stellt sicher, dass genau eine GlobalSettings-Zeile existiert."""
    gs = session.exec(select(GlobalSettings)).first()
    if not gs:
        gs = GlobalSettings(id=1)
        session.add(gs)
        session.commit()
        session.refresh(gs)
    return gs


@router.get("/settings")
async def get_global_settings(
    session: Session = Depends(get_session),
    current_user: User = Depends(require_global_admin()),
):
    """Globale Systemeinstellungen laden (LLM-Config, LDAP, Prompts)."""
    gs = _ensure_global_settings(session)
    return {
        "id": gs.id,
        "llm_api_url": gs.llm_api_url,
        "llm_model": gs.llm_model,
        "grading_prompt": gs.grading_prompt,
        # Public Endpoint (nicht-sensitive Aufgaben); API-Key wird nicht ausgegeben
        "llm_api_url_public": gs.llm_api_url_public,
        "llm_model_public": gs.llm_model_public,
        "use_ldap": gs.use_ldap,
        "ldap_server": gs.ldap_server,
        "ldap_base_dn": gs.ldap_base_dn,
        "ldap_bind_dn": gs.ldap_bind_dn,
        "ldap_user_search": gs.ldap_user_search,
        # Compute/Workspace
        "compute_enabled": bool(gs.compute_enabled),
        "compute_agents": gs.compute_agents or "",
        "workspace_gpu_enabled": bool(gs.workspace_gpu_enabled),
        # .env/Compose deklariert einen lokalen Agent → Admin-UI zeigt ihn
        # als "local"-Zeile an (relevant, wenn die gespeicherte Liste leer ist)
        "compute_env_default": (
            {"name": "local", "url": COMPUTE_AGENT_URL, "key": COMPUTE_AGENT_KEY}
            if COMPUTE_AGENT_URL_EXPLICIT else None
        ),
    }


@router.put("/settings")
async def update_global_settings(
    data: GlobalSettingsUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(require_global_admin()),
):
    """Globale Systemeinstellungen aktualisieren."""
    gs = _ensure_global_settings(session)

    # Nur nicht-None und nicht-leere Werte updaten
    update_data = data.model_dump(exclude_none=True)
    # Empty string fuer ldap_bindpw ausschliessen (bestehendes PW beibehalten)
    update_data = {k: v for k, v in update_data.items() if not (k == "ldap_bind_pw" and v == "")}
    for key, value in update_data.items():
        setattr(gs, key, value)

    session.add(gs)
    session.commit()
    session.refresh(gs)

    return {
        "message": "Globale Einstellungen aktualisiert.",
        "settings": {
            "id": gs.id,
            "use_ldap": gs.use_ldap,
            "llm_model": gs.llm_model,
            "llm_api_url_public": gs.llm_api_url_public,
            "llm_model_public": gs.llm_model_public,
            "ldap_server": gs.ldap_server,
            "ldap_base_dn": gs.ldap_base_dn,
            "ldap_bind_dn": gs.ldap_bind_dn,
        },
    }


# ═══════════════════════════════════════════════════════════════════
# COMPUTE (Workspace-Aufgaben: Feature-Flag, Agent-Registry, Status)
# ═══════════════════════════════════════════════════════════════════

class ComputeTestRequest(BaseModel):
    url: str
    key: Optional[str] = ""


@router.get("/compute/status")
async def compute_status(
    session: Session = Depends(get_session),
    current_user: User = Depends(require_global_admin()),
):
    """Gesamter Compute-Status: Feature-Flag, GPU, Agenten + Health.

    Health-Cache (30 s) wird bewusst genutzt — für den expliziten
    "Verbindung testen"-Button gibt es POST /compute/test.
    """
    from services.workspace_service import workspace_service
    return workspace_service.status(session)


@router.get("/compute/sessions")
async def compute_sessions(
    session: Session = Depends(get_session),
    current_user: User = Depends(require_global_admin()),
):
    """Laufende Workspace-Sessions je Engine (Kurs/Aufgabe/Student/Image)."""
    from services.workspace_service import workspace_service
    return {"agents": workspace_service.engine_sessions(session)}


@router.post("/compute/test")
async def compute_test(
    data: ComputeTestRequest,
    current_user: User = Depends(require_global_admin()),
):
    """Einzelnen Agent-Endpoint testen (ohne Cache, frische Health-Abfrage).

    Dient der Admin-Konsole: sowohl für die eingegebenen (noch nicht
    gespeicherten) Registry-Werte als auch für gespeicherte Einträge.
    """
    from services.compute_client import ComputeAgentError, ComputeClient

    url = (data.url or "").strip().rstrip("/")
    if not url.startswith("http://") and not url.startswith("https://"):
        return {"ok": False, "error": "URL muss mit http:// beginnen."}
    client = ComputeClient(url=url, key=data.key or None)
    try:
        info = client.health(timeout=8.0)
        return {
            "ok": bool(info.get("docker")),
            "docker": bool(info.get("docker")),
            "gpu": bool(info.get("gpu")),
            "gpus": info.get("gpus") or [],
            "gpu_info": info.get("gpu_info") or "",
            "workspaces": info.get("workspaces"),
            "error": None if info.get("docker") else "Docker-Daemon nicht erreichbar",
        }
    except ComputeAgentError as e:
        return {"ok": False, "docker": False, "gpu": False,
                "workspaces": None, "error": e.message}


# ═══════════════════════════════════════════════════════════════════
# KURSEINSTELLUNGEN (LLM, LDAP, Prompts)
# ═══════════════════════════════════════════════════════════════════

@router.get("/courses/{course_id}/settings")
async def get_course_settings(
    course_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(require_global_admin()),
):
    """Kurs-Einstellungen laden mit effektiven Werten (global + Override)."""
    from services.settings_resolver import get_effective_llm_config, get_effective_ldap_config

    # Lese nur den kurs-spezifischen Override (fuer Editierbarkeit)
    cs = session.exec(
        select(CourseSettings).where(CourseSettings.course_id == course_id)
    ).first()

    # Berechne effektive Werte (mit global + env Fallback)
    llm_cfg = get_effective_llm_config(session, course_id)
    ldap_cfg = get_effective_ldap_config(session, course_id)

    return {
        # Kurs-Override (lokal gespeicherte Werte)
        "course_settings": {
            "id": cs.id if cs else None,
            "course_id": cs.course_id if cs else course_id,
            "llm_api_url": cs.llm_api_url if cs else None,
            "llm_model": cs.llm_model if cs else None,
            "grading_prompt": cs.grading_prompt if cs else None,
            "use_ldap": cs.use_ldap if cs else False,
            "ldap_server": cs.ldap_server if cs else None,
            "ldap_base_dn": cs.ldap_base_dn if cs else None,
            "ldap_bind_dn": cs.ldap_bind_dn if cs else None,
            "ldap_user_search": cs.ldap_user_search if cs else None,
        },
        # Effektive Werte (was wirklich benutzt wird)
        "effective": {
            "llm_api_url": llm_cfg["api_url"],
            "llm_model": llm_cfg["model"],
            "grading_prompt": llm_cfg["grading_prompt"],
            "use_ldap": ldap_cfg["use_ldap"],
            "ldap_server": ldap_cfg["ldap_server"],
            "ldap_base_dn": ldap_cfg["ldap_base_dn"],
            "ldap_bind_dn": ldap_cfg["ldap_bind_dn"],
            "ldap_user_search": ldap_cfg["ldap_user_search"],
            "llm_source": llm_cfg["source"],
        },
    }


@router.put("/courses/{course_id}/settings")
async def update_course_settings(
    course_id: int,
    data: CourseSettingsUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(require_global_admin()),
):
    """Kurs-Einstellungen aktualisieren."""
    settings = session.exec(
        select(CourseSettings).where(CourseSettings.course_id == course_id)
    ).first()
    
    if not settings:
        settings = CourseSettings(course_id=course_id)
    
    # Nur nicht-None und nicht-leere Werte updaten
    update_data = data.model_dump(exclude_none=True)
    # Empty string für ldap_bind_pw ausschließen (bestehendes PW beibehalten)
    update_data = {k: v for k, v in update_data.items() if not (k == "ldap_bind_pw" and v == "")}
    for key, value in update_data.items():
        setattr(settings, key, value)
    
    session.add(settings)
    session.commit()
    session.refresh(settings)
    
    return {
        "message": "Einstellungen aktualisiert.", "settings": {
            "id": settings.id,
            "use_ldap": settings.use_ldap,
            "llm_model": settings.llm_model,
            "ldap_server": settings.ldap_server,
            "ldap_base_dn": settings.ldap_base_dn,
            "ldap_bind_dn": settings.ldap_bind_dn,
        }
    }


class LlmTestRequest(BaseModel):
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None


@router.post("/llm/test")
async def test_llm(
    data: LlmTestRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(require_global_admin()),
):
    """
    Testet die LLM-Verbindung.
    Nutzt die eingegebene URL/Modell oder falls leer die globale Konfiguration.
    Sendet eine einfache Testanfrage und gibt Latenz zurueck.
    """
    from time import monotonic
    from openai import AsyncOpenAI, APIConnectionError, APITimeoutError, AuthenticationError, BadRequestError

    # Resolve: request body > global_settings > config.py default
    cfg = get_effective_llm_config(session)
    api_url = data.api_url or cfg["api_url"]
    api_key = data.api_key or cfg["api_key"]
    model = data.model or cfg["model"]

    if not api_url or not model:
        return {"success": False, "error": "API-URL und Modell sind erforderlich."}

    client = AsyncOpenAI(
        base_url=api_url,
        api_key=api_key,
        timeout=60,
    )

    test_prompt = "Beantworte nur mit: OK"
    test_error = None
    start = monotonic()
    try:
        # Thinking-Modelle (z. B. Qwen3) verbrauchen erst viele Tokens fuer
        # internes Denken — 10 reichten nicht und lieferten content=None.
        # Fuer den reinen Verbindungstest denken wir gar nicht erst mit:
        # chat_template_kwargs=enable_thinking:false -> Antwort in <1s statt ~2s.
        test_messages = [{"role": "user", "content": test_prompt}]
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=test_messages,
                max_tokens=2048,
                temperature=0,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
        except BadRequestError:
            # Provider ohne vLLM-Unterstützung lehnt chat_template_kwargs ab
            # (z. B. OpenAI) — ohne Thinking-Optimierung erneut versuchen.
            response = await client.chat.completions.create(
                model=model,
                messages=test_messages,
                max_tokens=2048,
                temperature=0,
            )
        latency_ms = int((monotonic() - start) * 1000)

        msg = response.choices[0].message if response.choices else None
        content = (msg.content or "(keine Antwort)").strip() if msg else "(keine Antwort)"
        record_llm_debug_entry(
            label="ADMIN_LLM_TEST", model=model, url=api_url, is_public=False,
            system_prompt="", prompt=test_prompt,
            response=content, success=True, latency_ms=latency_ms, attempts=1,
        )
        return {
            "success": True,
            "latency_ms": latency_ms,
            "model": model,
            "response": content,
            "source": "global" if not data.api_url else "input",
        }
    except APIConnectionError as e:
        test_error = f"Keine Verbindung: {e}"
    except APITimeoutError:
        test_error = "Zeitueberschreitung (>60s)."
    except AuthenticationError as e:
        test_error = f"Authentifizierung fehlgeschlagen: {e}"
    except Exception as e:
        test_error = f"Fehler: {e}"
    finally:
        await client.close()

    if test_error is not None:
        record_llm_debug_entry(
            label="ADMIN_LLM_TEST", model=model, url=api_url, is_public=False,
            system_prompt="", prompt=test_prompt,
            response="", success=False, error=test_error, attempts=1,
        )
        return {"success": False, "error": test_error}


@router.get("/llm-debug-log")
async def llm_debug_log(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    user_id: Optional[int] = Query(None),
    current_user: User = Depends(require_global_admin()),
):
    """LLM-Debug-Log (paginiert, neueste zuerst) — Listenansicht NUR Metadaten.

    Volltexte (System-Prompt/Prompt/Antwort/Thinking) liefert erst
    GET /llm-debug-log/{entry_id} (Lazy-Loading in der UI).
    Retention: letzte 7 Tage, harte Limits auf Einträge und Größe.
    """
    return get_llm_debug_log(offset=offset, limit=limit, user_id=user_id)


@router.get("/llm-debug-log/{entry_id}")
async def llm_debug_entry_detail(
    entry_id: int,
    current_user: User = Depends(require_global_admin()),
):
    """Kompletter LLM-Debug-Log-Eintrag (mit Volltexten) — Lazy-Loading beim Aufklappen."""
    entry = get_llm_debug_entry(entry_id)
    if entry is None:
        raise HTTPException(404, "Eintrag nicht gefunden (ggf. älter als 7 Tage oder purged).")
    return entry


class LdapTestRequest(BaseModel):
    ldap_server: Optional[str] = None
    ldap_base_dn: Optional[str] = None
    ldap_bind_dn: Optional[str] = None
    ldap_bind_pw: Optional[str] = None


@router.post("/ldap/test")
async def test_ldap(
    data: LdapTestRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(require_global_admin()),
):
    """
    Testet die LDAP-Verbindung mit den eingegebenen Einstellungen.
    Nutzt global_settings als Fallback, falls Felder nicht eingegeben wurden.
    Bindet den Service-Account und fuehrt eine Test-Suche durch.
    """
    from ldap3 import Server, Connection, SUBTREE, ALL
    from services.settings_resolver import get_effective_ldap_config

    # Resolve: request body > global_settings > config.py default
    ldap_cfg = get_effective_ldap_config(session)
    server = data.ldap_server or ldap_cfg["ldap_server"] or ""
    base_dn = data.ldap_base_dn or ldap_cfg["ldap_base_dn"] or ""
    bind_dn = data.ldap_bind_dn or ldap_cfg["ldap_bind_dn"] or ""
    bind_pw = data.ldap_bind_pw or ldap_cfg["ldap_bind_pw"] or ""

    if not server or not base_dn:
        return {"success": False, "error": "Server und Base DN sind erforderlich."}

    try:
        ldap_server_obj = Server(server, get_info=ALL)
        if bind_dn and bind_pw:
            conn = Connection(ldap_server_obj, user=bind_dn, password=bind_pw, auto_bind=True)
        else:
            conn = Connection(ldap_server_obj, auto_bind=True)
    except Exception as e:
        return {"success": False, "error": f"Bind fehlgeschlagen: {e}"}

    try:
        # Test-Suche: Nur die erste Entry finden
        conn.search(base_dn, "(objectClass=*)", search_scope=SUBTREE, size_limit=1)
        entries = conn.entries
        count = len(entries)
        detail = f"Bind erfolgreich. {count} Eintrag(e) gefunden in {base_dn}."
        if entries and count > 0:
            detail += f" Erstes: {entries[0].entry_dn}"
    except Exception as e:
        detail = f"Suche fehlgeschlagen: {e}"
    finally:
        conn.unbind()

    return {"success": True, "detail": detail}