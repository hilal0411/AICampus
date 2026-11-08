"""
Blind-Grading-Studie: Logik ohne HTTP (Import, Reihenfolge, Export).

Siehe docs/blind-grading.md. Studien-Daten liegen in eigenen Tabellen
(StudyItem / StudyRating / StudyModelRating) und berühren weder echte
Abgaben noch Noten.
"""

from __future__ import annotations

import csv
import io
import json
import random
from typing import Any, Iterable, Optional

from sqlmodel import Session, select

from database.models import StudyItem, StudyModelRating, StudyRating, User

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
