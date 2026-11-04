"""Read-only-Inspektion: letzte Einreichung eines Students zu einer Aufgabe.

Start: python scripts/inspect_submission.py <task_id> <student_id>
"""
import sys

from sqlmodel import Session, select

from database.base import engine
from database.models import Task, Submission


def main():
    task_id, student_id = int(sys.argv[1]), int(sys.argv[2])
    with Session(engine) as session:
        task = session.get(Task, task_id)
        subs = session.exec(
            select(Submission)
            .where(Submission.task_id == task_id)
            .where(Submission.student_id == student_id)
            .order_by(Submission.submitted_at.desc())
        ).all()
        print(f"Task {task_id}: {task.title} (typ={task.task_type.value})")
        print(f"Submissions von Student {student_id}: {len(subs)}")
        if not subs:
            return
        latest = subs[0]
        print(
            f"Latest: id={latest.id} attempt={latest.attempt_number} "
            f"status={latest.status.value} submitted_at={latest.submitted_at} "
            f"snapshot={bool(latest.workspace_snapshot)} upload={latest.upload_file}"
        )
        print("--- Feedbacks der letzten Einreichung ---")
        for fb in latest.feedback_list:
            print(
                f"  [{fb.id}] source={fb.source.value} points={fb.points_earned} "
                f"created={fb.created_at}\n  comment: {fb.comment[:300]}"
            )


if __name__ == "__main__":
    main()
