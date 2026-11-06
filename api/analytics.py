"""
Analytics-Endpoints (Admin): Nutzungs-Daten aus dem in-house
Page-View-Tracking (services/analytics.py, Tabelle page_views).

Rollen: Administrator (global)
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from database.models import CourseRole, User
from services import analytics
from services.auth_service import require_course_access, require_global_admin

router = APIRouter(prefix="/api/admin/analytics", tags=["Analytics"])


@router.get("/overview")
async def overview(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(require_global_admin()),
):
    """DAU-Serie (pro Tag im Fenster) + KPIs für das Dashboard."""
    dau = analytics.daily_active_users(days)
    return {
        "days": days,
        "dau": dau,
        "dau_avg": round(sum(d["count"] for d in dau) / days, 1) if days else 0,
        "dau_today": dau[-1]["count"] if dau else 0,
        "mau_30": analytics.count_active_users(30),
        "active_last7": analytics.count_active_users(7),
        "total_views": analytics.count_views(days),
        "users_by_weekday": analytics.avg_users_by_weekday(days),
        "users_by_hour": analytics.avg_users_by_hour(days),
    }


@router.get("/top-pages")
async def top_pages(
    days: int = Query(30, ge=1, le=365),
    course_id: Optional[int] = Query(None),
    user: User = Depends(require_global_admin()),
):
    """Beliebteste Seitenarten, optional auf einen Kurs begrenzt."""
    return analytics.top_pages(days, course_id=course_id)


@router.get("/top-courses")
async def top_courses(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(require_global_admin()),
):
    """Beliebteste Kurse im Fenster."""
    return analytics.top_courses(days)


@router.get("/top-users")
async def top_users(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(require_global_admin()),
):
    """Aktivste User im Fenster."""
    return analytics.top_users(days)


@router.get("/recent")
async def recent(
    limit: int = Query(100, ge=10, le=500),
    user: User = Depends(require_global_admin()),
):
    """Neueste Page-Views aller User (Wer-Wann-Was)."""
    return analytics.recent_views(limit)


@router.get("/feedback")
async def feedback(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(require_global_admin()),
):
    """Feedback-Performance: Einreichungen, KI-/Menschen-Korrektur,
    Ø-Leistung je Versuch, KI-vs.-Mensch (Bias + Streuung)."""
    return analytics.feedback_performance(days)


@router.get("/mcp")
async def mcp_usage(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(require_global_admin()),
):
    """MCP-Nutzung (ChatGPT & Co.): KPIs, Top-Tools, Top-Clients."""
    return analytics.mcp_usage(days)


@router.get("/dm")
async def dm_usage(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(require_global_admin()),
):
    """DM-Nutzung: KPIs, tägliche Serie, Chat-Größen-Histogramm."""
    return analytics.dm_usage(days)


@router.get("/mcp/recent")
async def mcp_recent(
    limit: int = Query(100, ge=10, le=500),
    user: User = Depends(require_global_admin()),
):
    """Neueste MCP-Tool-Calls (Wer-Wann-Was)."""
    return analytics.mcp_recent(limit)


@router.get("/users/{user_id}/views")
async def user_views(
    user_id: int,
    limit: int = Query(200, ge=10, le=500),
    user: User = Depends(require_global_admin()),
):
    """Page-View-Historie eines einzelnen Users."""
    return analytics.recent_views(limit, user_id=user_id)


# ═══ Kurs-Analytics (PROF/TUTOR/Admin — Kurs-Einstellungen) ══════

course_router = APIRouter(prefix="/api/courses/{course_id}/analytics", tags=["Analytics"])


@course_router.get("")
async def course_overview(
    days: int = Query(30, ge=1, le=365),
    user_and_course: tuple[User, int] = Depends(
        require_course_access(CourseRole.PROF, CourseRole.TUTOR)
    ),
):
    """Abgespecktes Analytics für einen Kurs: DAU-KPIs + Serie + Feedback-Performance."""
    _user, course_id = user_and_course
    dau = analytics.daily_active_users(days, course_id=course_id)
    return {
        "days": days,
        "dau": dau,
        "dau_avg": round(sum(d["count"] for d in dau) / days, 1) if days else 0,
        "dau_today": dau[-1]["count"] if dau else 0,
        "mau_30": analytics.count_active_users(30, course_id=course_id),
        "active_last7": analytics.count_active_users(7, course_id=course_id),
        "total_views": analytics.count_views(days, course_id=course_id),
        "feedback": analytics.feedback_performance(days, course_id=course_id),
    }
