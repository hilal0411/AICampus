"""
Blind-Grading-Studie (REST).

Tutor:innen bewerten (synthetische) Antworten, ohne LLM-Bewertungen oder
Bewertungen anderer Tutor:innen zu sehen. PROF/Admin importieren die
Antworten und exportieren alle Bewertungen als CSV. Siehe docs/blind-grading.md.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlmodel import Session, select

from database.base import get_session
from database.models import (
    Course, CourseRole, GlobalUserRole, StudyItem, StudyRating, User, UserCourse,
)
from services import blind_grading_service as bgs
from services.auth_service import get_current_user

router = APIRouter(prefix="/api/blind", tags=["Blind-Grading-Studie"])


def _role(user: User, course_id: int, session: Session) -> str:
    """'admin' | 'PROF' | 'TUTOR'; sonst 403."""
    course = session.get(Course, course_id)
    if not course or course.deleted_at is not None:
        raise HTTPException(404, "Kurs nicht gefunden.")
    if user.role == GlobalUserRole.ADMIN:
        return "admin"
    m = session.exec(
        select(UserCourse).where(UserCourse.user_id == user.id).where(UserCourse.course_id == course_id)
    ).first()
    if not m or m.role_in_course not in (CourseRole.PROF, CourseRole.TUTOR):
        raise HTTPException(403, "Nur PROF/Tutor:innen dürfen an der Blind-Bewertung teilnehmen.")
    return m.role_in_course.value


def _require_prof(user: User, course_id: int, session: Session) -> None:
    if _role(user, course_id, session) not in ("admin", "PROF"):
        raise HTTPException(403, "Nur PROF/Admin.")


# ─── Tutor:innen ────────────────────────────────────────────────

@router.get("/courses/{course_id}/next")
async def next_item(
    course_id: int,
    item_id: Optional[int] = None,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Nächste noch nicht bewertete Antwort (oder ein bestimmtes, eigenes Item
    zum Nachbearbeiten). Enthält nie LLM- oder fremde Bewertungen."""
    _role(user, course_id, session)
    order = bgs.rater_order(session, course_id, user.id)  # type: ignore[arg-type]
    done = bgs.rated_item_ids(session, course_id, user.id)  # type: ignore[arg-type]

    if item_id is not None:
        if item_id not in order:
            raise HTTPException(404, "Antwort nicht gefunden.")
        target = item_id
    else:
        target = next((i for i in order if i not in done), None)

    progress = {"done": len(done), "total": len(order)}
    if target is None:
        return {"item": None, "progress": progress, "finished": len(order) > 0}

    item = session.get(StudyItem, target)
    own = session.exec(
        select(StudyRating).where(StudyRating.item_id == target).where(StudyRating.rater_id == user.id)
    ).first()
    return {
        "item": bgs.item_for_rater(item),  # type: ignore[arg-type]
        "position": order.index(target) + 1,
        "progress": progress,
        "own_rating": {"points": own.points, "comment": own.comment} if own else None,
        "finished": False,
    }


@router.get("/courses/{course_id}/my-ratings")
async def my_ratings(
    course_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Eigene Bewertungen (zum Wiederfinden/Korrigieren). Nur eigene Punkte."""
    _role(user, course_id, session)
    order = bgs.rater_order(session, course_id, user.id)  # type: ignore[arg-type]
    pos = {iid: n + 1 for n, iid in enumerate(order)}
    rows = session.exec(
        select(StudyRating, StudyItem)
        .join(StudyItem, StudyItem.id == StudyRating.item_id)
        .where(StudyItem.course_id == course_id)
        .where(StudyRating.rater_id == user.id)
    ).all()
    return sorted(
        [{"item_id": it.id, "position": pos.get(it.id), "task_title": it.task_title,
          "points": r.points, "max_points": it.max_points} for r, it in rows],
        key=lambda x: x["position"] or 0,
    )


@router.post("/courses/{course_id}/items/{item_id}/rating")
async def save_rating(
    course_id: int,
    item_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Eigene Bewertung speichern/aktualisieren (eine pro Tutor:in und Antwort).
    Andere Tutor:innen und echte Noten bleiben unberührt."""
    _role(user, course_id, session)
    item = session.get(StudyItem, item_id)
    if not item or item.course_id != course_id:
        raise HTTPException(404, "Antwort nicht gefunden.")

    body = await request.json()
    try:
        points = float(body.get("points"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Bitte eine Punktzahl angeben.")
    if not (0 <= points <= item.max_points):
        raise HTTPException(400, f"Punkte müssen zwischen 0 und {item.max_points:g} liegen.")
    comment = str(body.get("comment") or "").strip()
    seconds = body.get("seconds_spent")
    seconds = float(seconds) if isinstance(seconds, (int, float)) and seconds >= 0 else None

    rating = session.exec(
        select(StudyRating).where(StudyRating.item_id == item_id).where(StudyRating.rater_id == user.id)
    ).first()
    if rating:
        rating.points, rating.comment, rating.updated_at = points, comment, datetime.now()
        if seconds is not None:
            rating.seconds_spent = (rating.seconds_spent or 0) + seconds
    else:
        rating = StudyRating(item_id=item_id, rater_id=user.id, points=points,  # type: ignore[arg-type]
                             comment=comment, seconds_spent=seconds)
    session.add(rating)
    session.commit()
    return {"ok": True}


# ─── PROF / Admin ───────────────────────────────────────────────

@router.post("/courses/{course_id}/import")
async def import_items(
    course_id: int,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """CSV/JSON mit Antworten importieren (Spalten s. docs/blind-grading.md)."""
    _require_prof(user, course_id, session)
    raw = await file.read()
    try:
        n = bgs.import_items(session, course_id, bgs.parse_rows(raw, file.filename or ""))
    except bgs.ImportError_ as e:
        raise HTTPException(400, str(e))
    except (ValueError, UnicodeDecodeError) as e:
        raise HTTPException(400, f"Datei konnte nicht gelesen werden: {e}")
    return {"imported": n}


@router.get("/courses/{course_id}/summary")
async def summary(
    course_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    _require_prof(user, course_id, session)
    return bgs.summary(session, course_id)


@router.get("/courses/{course_id}/export.csv")
async def export_csv(
    course_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Alle Bewertungen (Tutor:innen pseudonymisiert + Modell-Läufe) als CSV."""
    _require_prof(user, course_id, session)
    return Response(
        content=bgs.export_csv(session, course_id),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="blind-grading-kurs{course_id}.csv"'},
    )
