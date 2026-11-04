"""
Kurs-Mitglieder-Management für PROFs und Admins.

Eine PROF kann Mitglieder ihres Kurses hinzufügen, Rollen ändern
(inkl. PROF-Verleihung) oder entfernen. Ein globaler Administrator
hat die gleichen Rechte.

Diese Endpunkte sind unter /api/courses/{course_id}/members erreichbar.
"""

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from database.base import get_session
from database.models import (
    Course,
    CourseInvite,
    CourseRole,
    GlobalUserRole,
    User,
    UserCourse,
)
from services import course_service
from services.auth_service import get_current_user
from services.timeutil import utc_iso

router = APIRouter(prefix="/api", tags=["Kurs-Mitglieder (PROF/Admin)"])


# ─── Eigene Dependency: PROF oder global Admin ──────────────────
# require_course_access(CourseRole.PROF) würde zwar Admins durchlassen,
# setzt aber voraus, dass der User auch in der UserCourse-Tabelle steht.
# Ein Admin kann theoretisch jeden Kurs verwalten, auch ohne Membership.

def require_prof_or_admin():
    """Prüft: User ist PROF im Kurs ODER global Admin. Gibt (User, course_id, ist_admin) zurück."""
    async def course_check(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        course_id = request.path_params.get("course_id")
        if course_id is None:
            raise HTTPException(
                status_code=400,
                detail="Keine course_id im Pfad gefunden.",
            )
        course_id = int(course_id)

        is_admin = user.role == GlobalUserRole.ADMIN

        # Global-Admin: darf alles
        if is_admin:
            return user, course_id, True

        #ansonsten: muss PROF im Kurs sein
        membership = session.exec(
            select(UserCourse)
            .where(UserCourse.user_id == user.id)
            .where(UserCourse.course_id == course_id)
        ).first()

        if not membership:
            raise HTTPException(
                status_code=403,
                detail="Du bist kein Mitglied dieses Kurses.",
            )
        if membership.role_in_course != CourseRole.PROF:
            raise HTTPException(
                status_code=403,
                detail=f"Zugriff verweigert. Deine Rolle im Kurs: {membership.role_in_course.value}",
            )

        return user, course_id, False

    return course_check


@router.post("/courses/{course_id}/members")
async def add_members_to_course(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    auth_result: tuple = Depends(require_prof_or_admin()),
):
    """
    User zu einem Kurs hinzufügen (PROF oder Admin).

    Request:
        {
            "user_ids": [1, 2, 3],
            "role_in_course": "prof" | "tutor" | "student"
        }

    Jede PROF im Kurs darf alle Rollen vergeben.
    """
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    body = await request.json()
    user_ids = body.get("user_ids", [])
    role_in_course_str = body.get("role_in_course", "STUDENT").upper()
    role_in_course = CourseRole(role_in_course_str)

    added = []
    for uid in user_ids:
        target_user = session.get(User, uid)
        if not target_user:
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

        added.append({"user_id": uid, "username": target_user.username})

    session.commit()

    return {
        "message": f"{len(added)} Mitglieder hinzugefügt.",
        "added": added,
    }


@router.get("/courses/{course_id}/members")
async def list_course_members(
    course_id: int,
    session: Session = Depends(get_session),
    auth_result: tuple = Depends(require_prof_or_admin()),
):
    """Alle Mitglieder eines Kurses auflisten (PROF oder Admin)."""
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
            "annotation": m.annotation,
        }
        for m in members
    ]


@router.delete("/courses/{course_id}/members/{user_id}")
async def remove_member_from_course(
    course_id: int,
    user_id: int,
    session: Session = Depends(get_session),
    auth_result: tuple = Depends(require_prof_or_admin()),
):
    """Mitglied aus Kurs entfernen (PROF oder Admin)."""
    user, _, is_admin = auth_result
    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user_id)
        .where(UserCourse.course_id == course_id)
    ).first()

    if not membership:
        raise HTTPException(404, "Mitgliedschaft nicht gefunden.")

    # Niemand kann sich selbst entfernen
    if user_id == user.id:
        raise HTTPException(400, "Du kannst dich selbst nicht entfernen.")

    session.delete(membership)
    session.commit()
    return {"message": "Mitglied entfernt."}


@router.put("/courses/{course_id}/members/{user_id}/role")
async def update_member_role(
    course_id: int,
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
    auth_result: tuple = Depends(require_prof_or_admin()),
):
    """
    Rolle eines Kursmitglieds ändern (PROF oder Admin).

    Request:
        {
            "role_in_course": "prof" | "tutor" | "student"
        }

    Jede PROF im Kurs darf alle Rollen vergeben.
    """
    body = await request.json()
    role_in_course_str = body.get("role_in_course", "").upper()
    role_in_course = CourseRole(role_in_course_str)

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user_id)
        .where(UserCourse.course_id == course_id)
    ).first()

    if not membership:
        raise HTTPException(404, "Mitgliedschaft nicht gefunden.")

    membership.role_in_course = role_in_course
    session.add(membership)
    session.commit()

    return {
        "message": "Rolle geändert.",
        "membership": {
            "user_id": membership.user_id,
            "username": membership.user.username if membership.user else "unknown",
            "role_in_course": membership.role_in_course.value,
        },
    }


@router.put("/courses/{course_id}/members/{user_id}/annotation")
async def update_member_annotation(
    course_id: int,
    user_id: int,
    request: Request,
    session: Session = Depends(get_session),
    auth_result: tuple = Depends(require_prof_or_admin()),
):
    """
    Annotation (freier Text) eines Kursmitglieds setzen (PROF oder Admin).

    Request:
        {
            "annotation": "Übungsgruppe 2"   // leer/None löscht die Annotation
        }
    """
    body = await request.json()
    annotation = body.get("annotation")
    if isinstance(annotation, str):
        annotation = annotation.strip() or None
    else:
        annotation = None

    membership = session.exec(
        select(UserCourse)
        .where(UserCourse.user_id == user_id)
        .where(UserCourse.course_id == course_id)
    ).first()

    if not membership:
        raise HTTPException(404, "Mitgliedschaft nicht gefunden.")

    membership.annotation = annotation
    session.add(membership)
    session.commit()

    return {
        "message": "Gruppe aktualisiert.",
        "membership": {
            "user_id": membership.user_id,
            "username": membership.user.username if membership.user else "unknown",
            "annotation": membership.annotation,
        },
    }


@router.put("/courses/{course_id}/name")
async def update_course_name(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    auth_result: tuple = Depends(require_prof_or_admin()),
):
    """
    Kurstitel, Beschreibung und Semester beschreiben (PROF oder Admin).

    Request:
        {
            "name": "Neuer Kurstitel",
            "description": "Neue Beschreibung",
            "semester": "WS 2025/26",
        }
    """
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    body = await request.json()
    if "name" in body:
        course.name = body["name"]
    if "description" in body:
        course.description = body["description"]
    if "semester" in body:
        course.semester = body["semester"]

    session.add(course)
    session.commit()
    session.refresh(course)

    return {
        "message": f"Kurs '{course.name}' aktualisiert.",
        "course": {
            "id": course.id,
            "name": course.name,
            "description": course.description,
            "semester": course.semester,
        },
    }


# ═══════════════════════════════════════════════════════════════
# KURS DUPLIZIEREN / LÖSCHEN
# ═══════════════════════════════════════════════════════════════

class CourseDuplicateRequest(BaseModel):
    name: str
    description: str
    semester: str


@router.post("/courses/{course_id}/duplicate")
async def duplicate_course(
    course_id: int,
    data: CourseDuplicateRequest,
    session: Session = Depends(get_session),
    auth_result: tuple = Depends(require_prof_or_admin()),
):
    """Kurs duplizieren — z. B. für das nächste Semester (PROF oder Admin).

    Kopiert die Kurs-Materialien (Skript, Slides, Aufgaben, Medien,
    Einstellungen) — aber keine Studierenden-Daten (Mitglieder, Einreichungen).
    """
    user, _, _ = auth_result
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    result = course_service.duplicate_course_content(
        session, user, course, data.name, data.description, data.semester
    )
    new_course = result["course"]
    return {
        "message": f"Kurs '{data.name}' aus '{course.name}' dupliziert.",
        "course": {
            "id": new_course.id,
            "name": new_course.name,
            "semester": new_course.semester,
            "task_count": result["task_count"],
            "section_count": result["section_count"],
            "material_count": result["material_count"],
            "media_count": result["media_count"],
        },
    }


class DeleteCourseRequest(BaseModel):
    confirm: str


@router.delete("/courses/{course_id}")
async def delete_course(
    course_id: int,
    data: DeleteCourseRequest,
    session: Session = Depends(get_session),
    auth_result: tuple = Depends(require_prof_or_admin()),
):
    """Kurs in den Papierkorb legen (PROF oder Admin).

    Soft Delete: alle Inhalte bleiben erhalten und können (von Admins)
    wiederhergestellt werden. Bestätigung: exakt 'DELETE' eingeben.
    """
    if data.confirm.strip() != "DELETE":
        raise HTTPException(
            400, "Bestätigung unvollständig: 'DELETE' eingeben, um zu bestätigen."
        )

    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    name = course_service.soft_delete_course(session, course)
    return {"message": f"Kurs '{name}' in den Papierkorb gelegt."}


# ═══════════════════════════════════════════════════════════════
# USER-SUCHE (damit PROF/Admin User finden kann, um sie hinzuzufügen)
# ═══════════════════════════════════════════════════════════════════

@router.get("/courses/{course_id}/search-users")
async def search_users_for_course(
    course_id: int,
    q: str = "",
    session: Session = Depends(get_session),
    auth_result: tuple = Depends(require_prof_or_admin()),
):
    """
    User durchsuchen, die noch nicht im Kurs sind.

    Query-Parameter:
        q: Suchbegriff (wird gegen username, name und email gematcht)
    """
    # Aktive Mitglieder des Kurses sammeln
    members = session.exec(
        select(UserCourse).where(UserCourse.course_id == course_id)
    ).all()
    member_ids = {m.user_id for m in members}

    # Alle User durchsuchen
    all_users = session.exec(select(User)).all()

    if q:
        q_lower = q.lower()
        all_users = [
            u for u in all_users
            if q_lower in u.username.lower()
            or q_lower in u.name.lower()
            or q_lower in u.email.lower()
        ]

    # Nur User anzeigen, die noch nicht im Kurs sind
    results = [
        {
            "id": u.id,
            "username": u.username,
            "name": u.name,
            "email": u.email,
        }
        for u in all_users
        if u.id not in member_ids
    ]

    return {"users": results}


# ═══════════════════════════════════════════════════════════════════
# EINLADUNGSLINKS (CourseInvite)
# ═══════════════════════════════════════════════════════════════════

import secrets
from datetime import timedelta


@router.post("/courses/{course_id}/invites")
async def create_invite(
    course_id: int,
    request: Request,
    session: Session = Depends(get_session),
    auth_result: tuple = Depends(require_prof_or_admin()),
):
    """
    Einladungslink für einen Kurs erstellen.

    Request:
        {
            "expires_days": 7,        // Gültigkeitsdauer in Tagen (default: 7)
            "max_uses": 10           // Max. Nutzungen (optional, null = unbegrenzt)
        }
    """
    user, _, _ = auth_result
    course = session.get(Course, course_id)
    if not course:
        raise HTTPException(404, "Kurs nicht gefunden.")

    body = await request.json()
    expires_days = body.get("expires_days", 7)
    max_uses = body.get("max_uses", None)

    token = secrets.token_urlsafe(20)
    expires_at = (
        datetime.now() + timedelta(days=expires_days)
        if expires_days > 0
        else None
    )

    invite = CourseInvite(
        course_id=course_id,
        token=token,
        expires_at=expires_at,
        max_uses=max_uses,
        created_by=user.id,
    )
    session.add(invite)
    session.commit()
    session.refresh(invite)

    return {
        "message": "Einladungslink erstellt.",
        "invite": {
            "id": invite.id,
            "token": invite.token,
            "expires_at": utc_iso(invite.expires_at),
            "max_uses": invite.max_uses,
            "used_count": invite.used_count,
            "created_at": utc_iso(invite.created_at),
        },
    }


@router.get("/courses/{course_id}/invites")
async def list_invites(
    course_id: int,
    session: Session = Depends(get_session),
    auth_result: tuple = Depends(require_prof_or_admin()),
):
    """Alle aktiven Einladungslinks eines Kurses auflisten."""
    invites = session.exec(
        select(CourseInvite)
        .where(CourseInvite.course_id == course_id)
    ).all()
    invites = list(reversed(invites))

    return [
        {
            "id": inv.id,
            "token": inv.token,
            "expires_at": utc_iso(inv.expires_at),
            "max_uses": inv.max_uses,
            "used_count": inv.used_count,
            "created_at": utc_iso(inv.created_at),
            "is_expired": inv.expires_at is not None and inv.expires_at < datetime.now(),
            "is_used_up": inv.max_uses is not None and inv.used_count >= inv.max_uses,
        }
        for inv in invites
    ]


@router.delete("/courses/{course_id}/invites/{invite_id}")
async def delete_invite(
    course_id: int,
    invite_id: int,
    session: Session = Depends(get_session),
    auth_result: tuple = Depends(require_prof_or_admin()),
):
    """Einladungslink löschen."""
    invite = session.get(CourseInvite, invite_id)
    if not invite:
        raise HTTPException(404, "Einladungslink nicht gefunden.")
    if invite.course_id != course_id:
        raise HTTPException(400, "Link gehört nicht zu diesem Kurs.")

    session.delete(invite)
    session.commit()
    return {"message": "Einladungslink gelöscht."}