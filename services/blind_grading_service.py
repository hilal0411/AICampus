"""
Blind-Grading-Studie: Logik ohne HTTP (Import, Reihenfolge, Export).

Siehe docs/blind-grading.md. Studien-Daten liegen in eigenen Tabellen
(StudyItem / StudyRating / StudyModelRating) und berühren weder echte
Abgaben noch Noten.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlmodel import Session, select

from database.base import engine
from database.models import StudyItem, StudyModelEndpoint, StudyModelRating, StudyRating, User

# Spalten der Import-Datei (CSV oder JSON-Liste mit denselben Keys)
REQUIRED_FIELDS = ("task_key", "task_title", "task_description", "max_points", "answer")
OPTIONAL_FIELDS = ("model_solution", "intended_points", "error_type", "source")


class ImportError_(ValueError):
    """Fehler in der Import-Datei (mit Zeilenangabe)."""


def _to_float(value: Any, field: str, row_no: int, required: bool) -> Optional[float]:
    if value is None or str(value).strip() == "":
        if required:
            raise ImportError_(f"Zeile {row_no}: '{field}' fehlt.")
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        raise ImportError_(f"Zeile {row_no}: '{field}' ist keine Zahl ({value!r}).")


def parse_rows(raw: bytes, filename: str = "") -> list[dict]:
    """CSV (Komma oder Semikolon) oder JSON-Liste → Liste von Dicts."""
    text = raw.decode("utf-8-sig")
    if filename.lower().endswith(".json") or text.lstrip().startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise ImportError_("JSON muss eine Liste von Objekten sein.")
        return data
    sample = text[:4096]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    return list(csv.DictReader(io.StringIO(text), delimiter=delimiter))


def import_items(session: Session, course_id: int, rows: Iterable[dict]) -> int:
    """Validiert alle Zeilen und legt StudyItems an (alles oder nichts)."""
    items: list[StudyItem] = []
    for i, row in enumerate(rows, start=2):  # Zeile 1 = Header
        row = {k.strip(): v for k, v in row.items() if k}
        for field in REQUIRED_FIELDS:
            if field != "max_points" and not str(row.get(field) or "").strip():
                raise ImportError_(f"Zeile {i}: '{field}' fehlt.")
        max_points = _to_float(row.get("max_points"), "max_points", i, True)
        intended = _to_float(row.get("intended_points"), "intended_points", i, False)
        if intended is not None and not (0 <= intended <= max_points):
            raise ImportError_(f"Zeile {i}: intended_points muss zwischen 0 und max_points liegen.")
        items.append(StudyItem(
            course_id=course_id,
            task_key=str(row["task_key"]).strip(),
            task_title=str(row["task_title"]).strip(),
            task_description=str(row["task_description"]),
            model_solution=str(row.get("model_solution") or ""),
            max_points=max_points,
            answer=str(row["answer"]),
            intended_points=intended,
            error_type=(str(row.get("error_type")).strip() or None) if row.get("error_type") else None,
            source=(str(row.get("source")).strip() or None) if row.get("source") else None,
        ))
    if not items:
        raise ImportError_("Die Datei enthält keine Zeilen.")
    session.add_all(items)
    session.commit()
    return len(items)


def rater_order(session: Session, course_id: int, rater_id: int) -> list[int]:
    """Item-IDs in einer pro Tutor:in festen, zufälligen Reihenfolge.

    Jede:r bekommt eine andere Reihenfolge (gegen Reihenfolge-Effekte),
    die aber stabil bleibt (Reload zeigt dieselbe nächste Antwort).
    """
    ids = sorted(session.exec(select(StudyItem.id).where(StudyItem.course_id == course_id)).all())
    random.Random(f"aicampus-blind-{course_id}-{rater_id}").shuffle(ids)
    return ids


def rated_item_ids(session: Session, course_id: int, rater_id: int) -> set[int]:
    return set(session.exec(
        select(StudyRating.item_id)
        .join(StudyItem, StudyItem.id == StudyRating.item_id)
        .where(StudyItem.course_id == course_id)
        .where(StudyRating.rater_id == rater_id)
    ).all())


def item_for_rater(item: StudyItem) -> dict:
    """Nur was Tutor:innen sehen dürfen — KEINE intended_points, error_type,
    source, Modell- oder fremden Bewertungen."""
    return {
        "id": item.id,
        "task_title": item.task_title,
        "task_description": item.task_description,
        "model_solution": item.model_solution,
        "max_points": item.max_points,
        "answer": item.answer,
    }


def export_csv(session: Session, course_id: int) -> str:
    """Long-Format: eine Zeile pro Bewertung (Mensch oder Modell-Lauf)."""
    items = {i.id: i for i in session.exec(select(StudyItem).where(StudyItem.course_id == course_id)).all()}
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["item_id", "task_key", "max_points", "intended_points", "error_type", "source",
                "rater_type", "rater", "run", "prompt_version", "points", "seconds_spent", "comment"])
    if not items:
        return out.getvalue()
    # Tutor:innen pseudonymisiert (T1, T2, …) — Klarnamen gehören nicht in die Auswertung
    ratings = session.exec(select(StudyRating).where(StudyRating.item_id.in_(items.keys()))).all()  # type: ignore[attr-defined]
    pseudo: dict[int, str] = {}
    for r in sorted(ratings, key=lambda r: (r.rater_id, r.item_id)):
        pseudo.setdefault(r.rater_id, f"T{len(pseudo) + 1}")
    for r in ratings:
        it = items[r.item_id]
        w.writerow([it.id, it.task_key, it.max_points, it.intended_points, it.error_type, it.source,
                    "human", pseudo[r.rater_id], 1, "", r.points, r.seconds_spent, r.comment])
    for m in session.exec(select(StudyModelRating).where(StudyModelRating.item_id.in_(items.keys()))).all():  # type: ignore[attr-defined]
        it = items[m.item_id]
        w.writerow([it.id, it.task_key, it.max_points, it.intended_points, it.error_type, it.source,
                    "model", m.model, m.run, m.prompt_version, m.points, "", m.comment])
    return out.getvalue()


def summary(session: Session, course_id: int) -> dict:
    items = session.exec(select(StudyItem).where(StudyItem.course_id == course_id)).all()
    ids = [i.id for i in items]
    ratings = session.exec(select(StudyRating).where(StudyRating.item_id.in_(ids))).all() if ids else []  # type: ignore[attr-defined]
    model_ratings = session.exec(select(StudyModelRating).where(StudyModelRating.item_id.in_(ids))).all() if ids else []  # type: ignore[attr-defined]
    per_rater: dict[int, int] = {}
    for r in ratings:
        per_rater[r.rater_id] = per_rater.get(r.rater_id, 0) + 1
    names = {u.id: u.name for u in session.exec(select(User).where(User.id.in_(per_rater.keys()))).all()} if per_rater else {}  # type: ignore[attr-defined]
    return {
        "items": len(items),
        "tasks": len({i.task_key for i in items}),
        "human_ratings": len(ratings),
        "model_ratings": len(model_ratings),
        "models": sorted({m.model for m in model_ratings}),
        "raters": [{"name": names.get(rid, f"#{rid}"), "rated": n} for rid, n in sorted(per_rater.items())],
    }


# ═══════════════════════════════════════════════════════════════════
# LLM-Läufe (gemeinsam für Oberfläche und scripts/study_run_models.py)
# ═══════════════════════════════════════════════════════════════════


NO_SOLUTION = "(Keine Musterloesung hinterlegt — bitte eigenstaendig bewerten)"


def mask_key(key: str) -> str:
    if not key:
        return ""
    return "•" * 6 + key[-4:] if len(key) > 8 else "•" * 6


def endpoint_public(ep: StudyModelEndpoint) -> dict:
    """Endpoint für die Oberfläche — API-Key nur maskiert."""
    return {"id": ep.id, "name": ep.name, "api_url": ep.api_url, "model": ep.model,
            "enabled": ep.enabled, "api_key_masked": mask_key(ep.api_key)}


def endpoint_config(ep: StudyModelEndpoint, timeout: int = 300) -> dict:
    return {"api_url": ep.api_url, "api_key": ep.api_key or "none", "model": ep.model, "timeout": timeout}


async def grade_item(llm, item: StudyItem, cfg: dict, prompt: Optional[str] = None) -> dict:
    """Eine Antwort von einem Modell bewerten lassen (unabhängig, ohne Vor-Feedback)."""
    t0 = time.monotonic()
    try:
        res = await llm.grade_text_task(
            task_description=item.task_description,
            model_solution=item.model_solution or NO_SOLUTION,
            student_solution=item.answer,
            max_points=item.max_points,
            custom_prompt=prompt,
            previous_feedback="",
            config=cfg,
        )
    except Exception as e:  # Netzwerk/Endpoint-Fehler nicht durchreichen
        res = {"success": False, "error": str(e)}
    ms = int((time.monotonic() - t0) * 1000)
    points, comment, error = None, "", None
    if res.get("success"):
        data = res.get("data") or {}
        try:
            points = max(0.0, min(float(data.get("points")), item.max_points))
        except (TypeError, ValueError):
            error = f"Keine gültige Punktzahl: {data.get('points')!r}"
        comment = str(data.get("feedback") or "")
    else:
        error = str(res.get("error") or "unbekannter Fehler")
    return {"points": points, "comment": comment, "error": error, "latency_ms": ms}


def _already_done(session: Session, item_id: int, model: str, run: int, prompt_version: str) -> bool:
    return session.exec(
        select(StudyModelRating)
        .where(StudyModelRating.item_id == item_id)
        .where(StudyModelRating.model == model)
        .where(StudyModelRating.run == run)
        .where(StudyModelRating.prompt_version == prompt_version)
        .where(StudyModelRating.points != None)  # noqa: E711 — fehlgeschlagene Läufe neu versuchen
    ).first() is not None


@dataclass
class RunJob:
    course_id: int
    total: int = 0
    done: int = 0
    skipped: int = 0
    errors: int = 0
    current: str = ""
    status: str = "running"          # running | finished | cancelled | failed
    message: str = ""
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    cancel: bool = False

    def public(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "cancel"}


async def run_models(course_id: int, endpoints: list[tuple[str, dict]], runs: int,
                     prompt_version: str = "default", prompt: Optional[str] = None,
                     job: Optional[RunJob] = None, log=print) -> RunJob:
    """Alle Antworten eines Kurses von allen Endpoints `runs`-mal bewerten lassen.
    Bereits erfolgreiche Läufe werden übersprungen (fortsetzbar)."""
    from services.llm_service import LLMService
    llm = LLMService()
    job = job or RunJob(course_id=course_id)
    with Session(engine) as session:
        items = session.exec(select(StudyItem).where(StudyItem.course_id == course_id)).all()
        job.total = len(items) * len(endpoints) * runs
        for name, cfg in endpoints:
            for item in items:
                for run_no in range(1, runs + 1):
                    if job.cancel:
                        job.status, job.message = "cancelled", "Abgebrochen."
                        return job
                    job.current = f"{name} · Antwort {item.id} · Lauf {run_no}"
                    if _already_done(session, item.id, name, run_no, prompt_version):  # type: ignore[arg-type]
                        job.done += 1
                        job.skipped += 1
                        continue
                    r = await grade_item(llm, item, cfg, prompt)
                    session.add(StudyModelRating(item_id=item.id, model=name, run=run_no,  # type: ignore[arg-type]
                                                 prompt_version=prompt_version, **r))
                    session.commit()
                    job.done += 1
                    if r["points"] is None:
                        job.errors += 1
                    status = (f"{r['points']:g}/{item.max_points:g}" if r["points"] is not None
                              else f"FEHLER: {r['error']}")
                    log(f"[{job.done}/{job.total}] {job.current}: {status} ({r['latency_ms']} ms)")
    job.status = "finished"
    job.current = ""
    job.message = f"Fertig: {job.done - job.skipped} neue Bewertungen, {job.skipped} übersprungen, {job.errors} Fehler."
    return job


# Laufende Hintergrund-Jobs pro Kurs (In-Memory; reicht für den lokalen Studienbetrieb).
_JOBS: dict[int, RunJob] = {}


def get_job(course_id: int) -> Optional[RunJob]:
    return _JOBS.get(course_id)


def start_job(course_id: int, runs: int, endpoint_ids: Optional[list[int]] = None) -> RunJob:
    current = _JOBS.get(course_id)
    if current and current.status == "running":
        raise RuntimeError("Für diesen Kurs läuft bereits eine Bewertung.")
    with Session(engine) as session:
        q = select(StudyModelEndpoint).where(StudyModelEndpoint.course_id == course_id)
        eps = [e for e in session.exec(q).all()
               if (e.id in endpoint_ids if endpoint_ids else e.enabled)]
        endpoints = [(e.name, endpoint_config(e)) for e in eps]
        n_items = len(session.exec(select(StudyItem.id).where(StudyItem.course_id == course_id)).all())
    if not endpoints:
        raise RuntimeError("Keine aktiven Modelle ausgewählt.")
    if not n_items:
        raise RuntimeError("Noch keine Antworten importiert.")
    job = RunJob(course_id=course_id, total=n_items * len(endpoints) * runs)
    _JOBS[course_id] = job

    async def _runner():
        try:
            await run_models(course_id, endpoints, runs, job=job, log=lambda *_: None)
        except Exception as e:  # pragma: no cover
            job.status, job.message = "failed", f"Fehler: {e}"

    asyncio.get_running_loop().create_task(_runner())
    return job
