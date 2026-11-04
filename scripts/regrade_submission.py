"""Eine Einreichung neu vom LLM korrigieren (nach einem Grading-Fehler).

Löscht NUR die bestehenden LLM-Feedbacks der Einreichung (manuelle
Korrekturen bleiben erhalten!), führt dann das normale LLM-Grading erneut
aus und informiert den Student per Glocke.

Start: python scripts/regrade_submission.py <submission_id>
"""
import asyncio
import sys

from sqlmodel import Session

from database.base import engine
from database.models import Task, Submission, FeedbackSource
from services import notifications
from services.grading_service import GradingError, GradingService


async def run(submission_id: int) -> int:
    grading = GradingService()
    with Session(engine) as session:
        sub = session.get(Submission, submission_id)
        if not sub:
            print(f"Einreichung {submission_id} nicht gefunden.")
            return 1
        task = session.get(Task, sub.task_id)
        if not task:
            print("Aufgabe nicht gefunden.")
            return 1

        # Nur LLM-Feedbacks entfernen (manuelle Korrekturen bleiben!)
        for fb in [f for f in sub.feedback_list if f.source == FeedbackSource.LLM]:
            session.delete(fb)
        session.commit()

        try:
            result = await grading.grade_submission(task, sub, session)
        except GradingError as e:
            print(f"Grading fehlgeschlagen: {e}")
            return 1
        points = float(result.get("points", 0) or 0)
        max_points = float(result.get("max_points", 0) or 0)
        print(f"Neu bewertet: {points:g} / {max_points:g} Punkte")
        print(f"Feedback: {str(result.get('comment', ''))[:500]}")

        notifications.notify_llm_feedback(
            session=session,
            course_id=task.course_id,
            student_id=sub.student_id,
            task_id=task.id,
            task_title=task.title,
            points=points,
            max_points=max_points,
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(int(sys.argv[1]))))
