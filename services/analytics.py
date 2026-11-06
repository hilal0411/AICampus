"""
In-house Nutzungs-Analytics: Page-View-Tracking + Aggregationen für das
Analytics-Dashboard in der Admin-Konsole.

Keine externen Dienste: Die Einträge landen in der lokalen Datenbank
(Tabelle page_views), alle Aggregationen laufen per SQL auf dem selben
Server. Die Daten verlassen die Plattform nicht.

Privacy: Es werden nur User-ID + Seite (path) + Seitenart (page_key) +
Kurs-ID + Zeitstempel gespeichert — bewusst keine IP-Adressen, User-Agents
oder Referrer.

Tracking-Pipeline:
1. main.py: HTTP-Middleware `_page_view_tracking` → maybe_track_page_view()
2. maybe_track_page_view(): Filter (GET/200/HTML/logged-in/kein HTMX)
   + User-ID aus dem JWT (kein DB-Lauf für den Filter)
3. record_page_view(): Seiten-Klassifikation + Kurzzeit-Dedupe + DB-Insert

Nur bekannte HTML-Seiten (Allowlist in _PAGE_RULES) werden gezählt —
API-Endpunkte, Dateien, HTMX-Partials, Redirects und Gäste-Zugriffe bleiben
außen vor. Neue Seiten müssen in _PAGE_RULES ergänzt werden, um gezählt
zu werden.

MCP-Tracking (Tabelle mcp_events): api/mcp_server.py ruft bei jedem
Tool-Call record_mcp_event() auf (User, Client, Kurs, Tool, Read/Write,
Ergebnis) — ebenfalls nur für das Admin-Dashboard lesbar.
"""

import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlmodel import Session, text

from database.base import engine
from database.models import McpEvent, PageView
from services.auth_service import decode_access_token
from services.timeutil import utc_iso


def _ts_iso(ts: Any) -> str:
    """Roher SQL-Zeitstempel → ISO-UTC (Z).

    Je nach DB kommt created_at als datetime ODER String zurück;
    ohne Zonen-Marker = UTC (unser DB-Format).
    """
    if hasattr(ts, "isoformat"):
        return utc_iso(ts)
    return utc_iso(datetime.fromisoformat(str(ts).replace("Z", "+00:00")))

logger = logging.getLogger(__name__)

# Einträge älter als das werden beim App-Start gelöscht (RETENTION).
# None = unbegrenzte Speicherung (Data-Basis für Learning-Analytics-
# Publikation). Auf eine Zahl setzen, um die automatische Löschung zu
# reaktivieren.
RETENTION_DAYS: Optional[int] = None

# Dieselbe Seite desselben Users innerhalb des Zeitfensters zählt nur
# einmal (schützt gegen F5-Flut).
DEDUP_SECONDS = 30

# ─── Seiten-Klassifikation (Allowlist) ──────────────────────────
# (Regex, page_key, Gruppe der Kurs-ID oder None)
_PAGE_RULES: list[tuple[re.Pattern, str, Optional[int]]] = [
    (re.compile(r"^/$"), "home", None),
    (re.compile(r"^/courses/(\d+)$"), "course", 1),
    (re.compile(r"^/courses/(\d+)/tasks$"), "tasks", 1),
    (re.compile(r"^/courses/(\d+)/tasks/new$"), "task_new", 1),
    (re.compile(r"^/courses/(\d+)/tasks/(\d+)$"), "task", 1),
    (re.compile(r"^/courses/(\d+)/tasks/(\d+)/students/(\d+)/review$"), "review", 1),
    (re.compile(r"^/courses/(\d+)/script$"), "script", 1),
    (re.compile(r"^/courses/(\d+)/script/(\d+)$"), "script_section", 1),
    (re.compile(r"^/courses/(\d+)/script/(\d+)/print$"), "script_pdf", 1),
    (re.compile(r"^/courses/(\d+)/slides$"), "slides", 1),
    (re.compile(r"^/courses/(\d+)/slides/(\d+)$"), "slides", 1),
    (re.compile(r"^/courses/(\d+)/slides/(\d+)/present$"), "slides", 1),
    (re.compile(r"^/courses/(\d+)/media$"), "media", 1),
    (re.compile(r"^/courses/(\d+)/references$"), "references", 1),
    (re.compile(r"^/courses/(\d+)/applets/new$"), "applet", 1),
    (re.compile(r"^/courses/(\d+)/applets/(\d+)$"), "applet", 1),
    (re.compile(r"^/courses/(\d+)/forum$"), "forum", 1),
    (re.compile(r"^/courses/(\d+)/members$"), "members", 1),
    (re.compile(r"^/courses/(\d+)/settings$"), "course_settings", 1),
    (re.compile(r"^/courses/(\d+)/overview$"), "overview", 1),
    (re.compile(r"^/courses/(\d+)/live$"), "live", 1),
    (re.compile(r"^/courses/(\d+)/live/mc$"), "live", 1),
    (re.compile(r"^/settings$"), "user_settings", None),
    (re.compile(r"^/notifications/archive$"), "notifications", None),
    (re.compile(r"^/u/[^/]+$"), "profile", None),
    (re.compile(r"^/admin$"), "admin", None),
]
_JOIN_RE = re.compile(r"^/join/[A-Za-z0-9_\-]+$")

# Pfade, die nie getrackt werden (Dateien, API, Docs, …).
_EXCLUDED_PREFIXES = (
    "/static/", "/api/", "/media/", "/avatars/", "/preview/",
    "/docs", "/redoc", "/openapi.json", "/sw.js",
    "/robots.txt", "/sitemap.xml",
)


def classify_path(path: str) -> Optional[tuple[str, Optional[int], str]]:
    """Liefert (page_key, course_id, stored_path) für eine trackbare Seite.

    None, wenn der Pfad nicht auf der Allowlist steht. Der Einladungs-Token
    in /join/… wird maskiert, damit er nicht in der DB landet.
    """
    for rx, page_key, group in _PAGE_RULES:
        m = rx.match(path)
        if m:
            course_id = int(m.group(group)) if group else None
            return page_key, course_id, path
    if _JOIN_RE.match(path):
        return "join", None, "/join/<token>"
    return None


# ─── Middleware-Hook ─────────────────────────────────────────────

def maybe_track_page_view(request, response) -> None:
    """Ruft das Tracking an (wird vom Middleware in main.py aufgerufen).

    Geht nie in die Knie: Fehler hier dürfen den Request nicht stören.
    """
    if request.method != "GET" or response.status_code != 200:
        return
    path = request.url.path
    if any(path == p or path.startswith(p) for p in _EXCLUDED_PREFIXES):
        return
    if "text/html" not in response.headers.get("content-type", ""):
        return
    if "HX-Request" in request.headers:  # HTMX-Partials zählen nicht als Seitenaufruf
        return
    token = request.cookies.get("access_token")
    if not token:
        return  # Gäste (Login/About) werden nicht getrackt
    payload = decode_access_token(token)
    if not payload:
        return
    user_id = payload.get("sub")
    if not isinstance(user_id, int):
        return
    record_page_view(user_id, path)


# ─── Aufzeichnung (mit Kurzzeit-Dedupe) ─────────────────────────
_last_view: dict[int, tuple[str, float]] = {}


def record_page_view(user_id: int, path: str) -> None:
    classified = classify_path(path)
    if not classified:
        return
    page_key, course_id, stored_path = classified

    now = time.monotonic()
    last = _last_view.get(user_id)
    if last and last[0] == stored_path and now - last[1] < DEDUP_SECONDS:
        return
    _last_view[user_id] = (stored_path, now)

    try:
        with Session(engine) as session:
            session.add(PageView(
                user_id=user_id,
                path=stored_path,
                page_key=page_key,
                course_id=course_id,
                created_at=datetime.now(),
            ))
            session.commit()
    except Exception:
        logger.exception("Analytics: Page-View konnte nicht gespeichert werden")


def prune_old(days: Optional[int] = RETENTION_DAYS) -> None:
    """Beim App-Start: Einträge außerhalb des Retention-Fensters löschen.

    days=None → unbegrenzte Retention, No-Op.
    """
    if days is None:
        return
    cutoff = datetime.now() - timedelta(days=days)
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM page_views WHERE created_at < :cutoff"),
                {"cutoff": cutoff},
            )
            mcp = conn.execute(
                text("DELETE FROM mcp_events WHERE created_at < :cutoff"),
                {"cutoff": cutoff},
            )
            deleted = (result.rowcount or 0) + (mcp.rowcount or 0)
        if deleted:
            logger.info(f"Analytics: {deleted} Tracking-Einträge älter als {days} Tage gelöscht.")
    except Exception:
        logger.exception("Analytics: Bereinigung alter Einträge fehlgeschlagen")


# ─── Aggregationen (Admin-Dashboard) ─────────────────────────────
# Die Queries nutzen nur SQL, das in SQLite UND PostgreSQL läuft
# (date(created_at), COUNT(DISTINCT …), JOINs).

def _since(days: int) -> datetime:
    return datetime.now() - timedelta(days=days)


def daily_active_users(days: int, course_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Aktive User pro Tag im Fenster (Lücken mit 0 aufgefüllt).

    course_id: nur Page-Views dieser Kurse (Kurs-DAU).
    """
    sql = (
        "SELECT date(created_at) AS d, COUNT(DISTINCT user_id) AS n "
        "FROM page_views WHERE created_at >= :since"
    )
    params: dict[str, Any] = {"since": _since(days)}
    if course_id is not None:
        sql += " AND course_id = :course_id"
        params["course_id"] = course_id
    sql += " GROUP BY d"
    with Session(engine) as session:
        rows = session.execute(text(sql), params).all()
    counts = {str(r[0]): r[1] for r in rows}
    out = []
    today = datetime.now().date()
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        out.append({"date": d.isoformat(), "count": counts.get(d.isoformat(), 0)})
    return out


def avg_users_by_weekday(days: int) -> list[dict[str, Any]]:
    """Ø aktive User je Wochentag (Mo..So) im Fenster.

    Durchschnitt der täglichen DAU-Werte über alle Vorkommnisse des
    Wochentags (s. daily_active_users).
    """
    series = daily_active_users(days)
    sums = [0.0] * 7
    counts = [0] * 7
    for entry in series:
        wd = datetime.fromisoformat(entry["date"]).weekday()  # Mo=0
        sums[wd] += entry["count"]
        counts[wd] += 1
    return [
        {
            "weekday": wd,
            "avg": round(sums[wd] / counts[wd], 1) if counts[wd] else None,
            "days": counts[wd],
        }
        for wd in range(7)
    ]


def _hour_of(ts: Any) -> int:
    """Stunde aus rohem SQL-Zeitstempel (datetime ODER String)."""
    if hasattr(ts, "hour"):
        return ts.hour
    return int(str(ts)[11:13])


def avg_users_by_hour(days: int) -> list[dict[str, Any]]:
    """Ø aktive User je Uhrzeit (0-1 … 23-24) im Fenster.

    Für jede Stunde: Ø der täglichen distinct-User-Zahlen über alle
    Tage des Fensters (Tage ohne Aktivität zählen mit 0).
    """
    since = _since(days)
    with Session(engine) as session:
        rows = session.execute(
            text(
                "SELECT date(created_at) AS d, created_at, user_id "
                "FROM page_views WHERE created_at >= :since"
            ),
            {"since": since},
        ).all()
    per_day_hour: dict[tuple[str, int], set[int]] = {}
    for d, ts, uid in rows:
        per_day_hour.setdefault((str(d), _hour_of(ts)), set()).add(uid)
    return [
        {
            "hour": h,
            "avg": round(
                sum(len(s) for (d, hh), s in per_day_hour.items() if hh == h) / days, 1
            ),
        }
        for h in range(24)
    ]


def count_active_users(days: int, course_id: Optional[int] = None) -> int:
    """Distinct Users mit ≥1 Page-View im Fenster (Basis für MAU)."""
    sql = "SELECT COUNT(DISTINCT user_id) FROM page_views WHERE created_at >= :since"
    params: dict[str, Any] = {"since": _since(days)}
    if course_id is not None:
        sql += " AND course_id = :course_id"
        params["course_id"] = course_id
    with Session(engine) as session:
        row = session.execute(text(sql), params).first()
    return row[0] if row else 0


def count_views(days: int, course_id: Optional[int] = None) -> int:
    """Anzahl Page-Views im Fenster."""
    sql = "SELECT COUNT(*) FROM page_views WHERE created_at >= :since"
    params: dict[str, Any] = {"since": _since(days)}
    if course_id is not None:
        sql += " AND course_id = :course_id"
        params["course_id"] = course_id
    with Session(engine) as session:
        row = session.execute(text(sql), params).first()
    return row[0] if row else 0


def top_pages(days: int, course_id: Optional[int] = None, limit: int = 20) -> list[dict[str, Any]]:
    """Beliebteste Seitenarten (page_key), optional auf einen Kurs begrenzt."""
    sql = (
        "SELECT pv.page_key, COUNT(*) AS views, COUNT(DISTINCT pv.user_id) AS users "
        "FROM page_views pv "
        "WHERE pv.created_at >= :since"
    )
    params: dict[str, Any] = {"since": _since(days), "limit": limit}
    if course_id is not None:
        sql += " AND pv.course_id = :course_id"
        params["course_id"] = course_id
    sql += " GROUP BY pv.page_key ORDER BY views DESC LIMIT :limit"
    with Session(engine) as session:
        rows = session.execute(text(sql), params).all()
    return [
        {"page_key": r[0], "views": r[1], "users": r[2]} for r in rows
    ]


def top_courses(days: int, limit: int = 15) -> list[dict[str, Any]]:
    """Beliebteste Kurse (Views + distinct User) im Fenster."""
    sql = (
        "SELECT pv.course_id, c.name, c.semester, COUNT(*) AS views, "
        "COUNT(DISTINCT pv.user_id) AS users "
        "FROM page_views pv "
        "JOIN courses c ON c.id = pv.course_id "
        "WHERE pv.created_at >= :since AND pv.course_id IS NOT NULL "
        "GROUP BY pv.course_id, c.name, c.semester "
        "ORDER BY views DESC LIMIT :limit"
    )
    with Session(engine) as session:
        rows = session.execute(text(sql), {"since": _since(days), "limit": limit}).all()
    return [
        {
            "course_id": r[0],
            "name": r[1],
            "semester": r[2],
            "views": r[3],
            "users": r[4],
        }
        for r in rows
    ]


def top_users(days: int, limit: int = 15) -> list[dict[str, Any]]:
    """Aktivste User im Fenster (Views + Anzahl verschiedener Seiten)."""
    sql = (
        "SELECT pv.user_id, u.username, u.name, COUNT(*) AS views, "
        "COUNT(DISTINCT pv.path) AS pages "
        "FROM page_views pv "
        "JOIN users u ON u.id = pv.user_id "
        "WHERE pv.created_at >= :since "
        "GROUP BY pv.user_id, u.username, u.name "
        "ORDER BY views DESC LIMIT :limit"
    )
    with Session(engine) as session:
        rows = session.execute(text(sql), {"since": _since(days), "limit": limit}).all()
    return [
        {
            "user_id": r[0],
            "username": r[1],
            "name": r[2],
            "views": r[3],
            "pages": r[4],
        }
        for r in rows
    ]


def _std(vals: list[float]) -> float:
    """Stichproben-Standardabweichung (n<2 → 0)."""
    n = len(vals)
    if n < 2:
        return 0.0
    mean = sum(vals) / n
    return (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5


# Versuchs-Ränge ab hier (0 = letzter Versuch, gezählt vom letzten Versuch
# eines Students je Aufgabe) werden zu einem Bucket zusammengefasst.
ATTEMPT_OFFSET_CAP = 10


def feedback_performance(days: int, course_id: Optional[int] = None) -> dict[str, Any]:
    """Feedback-Performance im Fenster: Einreichungen, KI-/Menschen-Korrektur,
    tägliche Serie, Ø-Leistung je Versuch und KI-vs.-Mensch-Vergleich.

    Offizielle Punkte pro Einreichung = Mensch (Override) > LLM,
    wie in main._get_best_score. Alle Prozentwerte beziehen sich auf die
    erreichbaren Gesamtpunkte der Aufgabe (max_points).

    Die Versuchs-Ränge zählen vom LETZTEN Versuch je (Student, Aufgabe)
    rückwärts (0 = letzter, 1 = vorletzter, …), da die meisten Studierenden
    bei 100 % aufhören — der letzte Versuch ist also der "Endzustand".

    course_id: nur Aufgaben dieses Kurses (Kurs-Feedback).
    """
    since = _since(days)
    course_filter = " AND t.course_id = :course_id" if course_id is not None else ""
    params: dict[str, Any] = {"since": since}
    if course_id is not None:
        params["course_id"] = course_id
    with Session(engine) as session:
        daily = session.execute(
            text(
                "SELECT date(s.submitted_at) AS d, COUNT(*) AS n "
                "FROM submissions s JOIN tasks t ON t.id = s.task_id "
                "WHERE s.submitted_at >= :since" + course_filter + " GROUP BY d"
            ),
            params,
        ).all()
        rows = session.execute(
            text(
                "SELECT s.id, s.student_id, s.task_id, s.submitted_at, "
                "t.max_points, f.source, f.points_earned "
                "FROM submissions s "
                "JOIN tasks t ON t.id = s.task_id "
                "LEFT JOIN feedback f ON f.submission_id = s.id "
                "WHERE s.submitted_at >= :since" + course_filter + " "
                "ORDER BY s.id"
            ),
            params,
        ).all()

    # Tägliche Serie mit 0 auffüllen (wie daily_active_users)
    counts = {str(r[0]): r[1] for r in daily}
    out_daily = []
    today = datetime.now().date()
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        out_daily.append({"date": d.isoformat(), "count": counts.get(d.isoformat(), 0)})

    # Pro Einreichung: höchster LLM- + höchster Mensch-Wert
    subs: dict[int, dict[str, Any]] = {}
    pair_subs: dict[tuple[int, int], list[tuple[Any, int]]] = {}
    for sub_id, student_id, task_id, submitted_at, max_points, source, points in rows:
        info = subs.setdefault(sub_id, {
            "max_points": max_points,
            "llm": None,
            "human": None,
        })
        pair_subs.setdefault((student_id, task_id), []).append((submitted_at, sub_id))
        src = (source or "").upper()
        if src == "LLM":
            info["llm"] = max(info["llm"] or 0.0, points)
        elif src == "HUMAN":
            info["human"] = max(info["human"] or 0.0, points)

    # Rang je (Student, Aufgabe): 0 = letzter Versuch, 1 = vorletzter, …
    offset_of: dict[int, int] = {}
    for lst in pair_subs.values():
        lst.sort()
        last = len(lst) - 1
        for i, (_, sub_id) in enumerate(lst):
            offset_of[sub_id] = last - i

    attempts: dict[int, list[float]] = {}
    diffs: list[float] = []
    scatter_counts: dict[tuple[float, float], int] = {}
    for sub_id, s in subs.items():
        if not s["max_points"]:
            continue
        official = s["human"] if s["human"] is not None else s["llm"]
        if official is not None:
            bucket = min(offset_of[sub_id], ATTEMPT_OFFSET_CAP)
            attempts.setdefault(bucket, []).append(
                official / s["max_points"] * 100)
        if s["human"] is not None and s["llm"] is not None:
            h = s["human"] / s["max_points"] * 100
            a = s["llm"] / s["max_points"] * 100
            key = (round(h, 1), round(a, 1))
            scatter_counts[key] = scatter_counts.get(key, 0) + 1
            diffs.append(a - h)

    return {
        "days": days,
        "submissions": len(subs),
        "graded_by_ai": sum(1 for s in subs.values() if s["llm"] is not None),
        "graded_by_human": sum(1 for s in subs.values() if s["human"] is not None),
        "daily": out_daily,
        "attempts": [
            {
                "offset": offset,
                "avg": round(sum(vals) / len(vals), 1),
                "std": round(_std(vals), 1),
                "n": len(vals),
            }
            for offset, vals in sorted(attempts.items())
        ],
        "attempt_cap": ATTEMPT_OFFSET_CAP,
        "scatter": [
            {"x": x, "y": y, "n": n}
            for (x, y), n in sorted(scatter_counts.items())
        ],
        "both": len(diffs),
        "bias": round(sum(diffs) / len(diffs), 1) if diffs else None,
        "std": round(_std(diffs), 1) if diffs else None,
    }


def record_mcp_event(
    user_id: int,
    course_id: Optional[int],
    tool_name: str,
    client: Optional[str],
    is_write: bool,
    ok: bool,
) -> None:
    """MCP-Tool-Call (wird von api/mcp_server.py aufgerufen).

    Geht nie in die Knie: Tracking-Fehler dürfen den Tool-Call nicht stören.
    """
    try:
        with Session(engine) as session:
            session.add(McpEvent(
                user_id=user_id,
                course_id=course_id,
                tool_name=tool_name,
                client=client,
                is_write=is_write,
                ok=ok,
                created_at=datetime.now(),
            ))
            session.commit()
    except Exception:
        logger.exception("Analytics: MCP-Event konnte nicht gespeichert werden")


def mcp_usage(days: int) -> dict[str, Any]:
    """MCP-Nutzung im Fenster: KPIs + Top-Tools + Top-Clients."""
    since = _since(days)
    with Session(engine) as session:
        row = session.execute(
            text(
                "SELECT COUNT(*), COUNT(DISTINCT user_id), "
                "COALESCE(SUM(CASE WHEN is_write THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) "
                "FROM mcp_events WHERE created_at >= :since"
            ),
            {"since": since},
        ).first()
        tools = session.execute(
            text(
                "SELECT tool_name, COUNT(*) AS calls, COUNT(DISTINCT user_id) AS users, "
                "COALESCE(SUM(CASE WHEN is_write THEN 1 ELSE 0 END), 0) AS writes, "
                "COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS errors "
                "FROM mcp_events WHERE created_at >= :since "
                "GROUP BY tool_name ORDER BY calls DESC LIMIT 20"
            ),
            {"since": since},
        ).all()
        clients = session.execute(
            text(
                "SELECT COALESCE(client, '(unbekannt)') AS client, COUNT(*) AS calls, "
                "COUNT(DISTINCT user_id) AS users "
                "FROM mcp_events WHERE created_at >= :since "
                "GROUP BY client ORDER BY calls DESC LIMIT 10"
            ),
            {"since": since},
        ).all()
    return {
        "calls": (row[0] if row else 0) or 0,
        "users": (row[1] if row else 0) or 0,
        "writes": int(row[2] or 0) if row else 0,
        "errors": int(row[3] or 0) if row else 0,
        "tools": [
            {
                "tool_name": r[0], "calls": r[1], "users": r[2],
                "writes": int(r[3] or 0), "errors": int(r[4] or 0),
            }
            for r in tools
        ],
        "clients": [
            {"client": r[0], "calls": r[1], "users": r[2]} for r in clients
        ],
    }


def dm_usage(days: int) -> dict[str, Any]:
    """DM-Nutzung im Fenster: KPIs, tägliche Serie, Chat-Größen-Histogramm.

    Die Größen-Verteilung ist ein aktueller Snapshot aller existierenden
    Chats (strukturell, nicht fenstergebunden) — gelöschte Chats (unter 2
    Mitglieder) fallen von selbst heraus.
    """
    since = _since(days)
    with Session(engine) as session:
        row = session.execute(
            text(
                "SELECT COUNT(*), COUNT(DISTINCT user_id), COUNT(DISTINCT chat_id) "
                "FROM dm_messages WHERE created_at >= :since"
            ),
            {"since": since},
        ).first()
        total_msgs = session.execute(text("SELECT COUNT(*) FROM dm_messages")).first()
        total_chats = session.execute(text("SELECT COUNT(*) FROM dm_chats")).first()
        daily = session.execute(
            text(
                "SELECT date(created_at) AS d, COUNT(*) AS n "
                "FROM dm_messages WHERE created_at >= :since GROUP BY d"
            ),
            {"since": since},
        ).all()
        sizes = session.execute(
            text(
                "SELECT size, COUNT(*) AS chats FROM ("
                "  SELECT chat_id, COUNT(*) AS size FROM dm_chat_members GROUP BY chat_id"
                ") GROUP BY size ORDER BY size"
            )
        ).all()

    # Tägliche Serie mit 0er auffüllen (wie daily_active_users)
    counts = {str(r[0]): r[1] for r in daily}
    out_daily = []
    today = datetime.now().date()
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        out_daily.append({"date": d.isoformat(), "count": counts.get(d.isoformat(), 0)})

    return {
        "days": days,
        "messages": row[0] if row else 0,
        "senders": row[1] if row else 0,
        "active_chats": row[2] if row else 0,
        "total_messages": total_msgs[0] if total_msgs else 0,
        "total_chats": total_chats[0] if total_chats else 0,
        "daily": out_daily,
        "sizes": [{"size": r[0], "chats": r[1]} for r in sizes],
    }


def mcp_recent(limit: int = 100) -> list[dict[str, Any]]:
    """Neueste MCP-Tool-Calls (Wer-Wann-Was), inkl. User-/Kursname."""
    sql = (
        "SELECT me.created_at, u.username, u.name, me.client, me.tool_name, "
        "me.is_write, me.ok, c.name AS course_name "
        "FROM mcp_events me "
        "LEFT JOIN users u ON u.id = me.user_id "
        "LEFT JOIN courses c ON c.id = me.course_id "
        "ORDER BY me.created_at DESC LIMIT :limit"
    )
    with Session(engine) as session:
        rows = session.execute(text(sql), {"limit": limit}).all()
    out = []
    for r in rows:
        out.append({
            "created_at": _ts_iso(r[0]),
            "username": r[1],
            "name": r[2],
            "client": r[3],
            "tool_name": r[4],
            "is_write": bool(r[5]),
            "ok": bool(r[6]),
            "course_name": r[7],
        })
    return out


def recent_views(limit: int = 100, user_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Neueste Page-Views (optional: nur für einen User), inkl. User-/Kursname."""
    sql = (
        "SELECT pv.created_at, pv.user_id, u.username, u.name, pv.page_key, "
        "pv.path, pv.course_id, c.name AS course_name "
        "FROM page_views pv "
        "LEFT JOIN users u ON u.id = pv.user_id "
        "LEFT JOIN courses c ON c.id = pv.course_id"
    )
    params: dict[str, Any] = {"limit": limit}
    if user_id is not None:
        sql += " WHERE pv.user_id = :user_id"
        params["user_id"] = user_id
    sql += " ORDER BY pv.created_at DESC LIMIT :limit"
    with Session(engine) as session:
        rows = session.execute(text(sql), params).all()
    out = []
    for r in rows:
        out.append({
            "created_at": _ts_iso(r[0]),
            "user_id": r[1],
            "username": r[2],
            "name": r[3],
            "page_key": r[4],
            "path": r[5],
            "course_id": r[6],
            "course_name": r[7],
        })
    return out
