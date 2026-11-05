"""
Grading-Service: Koordination zwischen LLM und Feedback-Speicherung.

Orchestriert den kompletten Korrektur-Workflow:
1. Workspace-Aufgaben -> frischer Einweg-Container (Tests)
2. LLM korrigiert (Text: direkt, Workspace: mit Test-Output)
3. Feedback + Punkte in DB speichern
4. Ergebnis an Frontend zurueckgeben
"""

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Optional
from sqlmodel import Session, select

from services.settings_resolver import get_effective_llm_config
from database.models import (
    Task, Submission, Feedback, FeedbackSource,
    SubmissionStatus, WorkspaceRun, WorkspaceRunStatus,
)
from services.llm_service import LLMService


class GradingError(RuntimeError):
    """LLM-Grading fehlgeschlagen (z. B. Timeout). Der Caller
    (z. B. _run_grading_background) fängt das ab und wirft die
    Einreichung weg, damit sie nicht als Versuch zählt."""


class GradingService:
    """
    Koordiniert den kompletten Korrektur-Workflow.

    Usage:
        grading = GradingService()
        result = await grading.grade_submission(task, submission, session)
    """

    def __init__(self):
        self.llm = LLMService()

    @staticmethod
    def _require_success(llm_result: dict) -> dict:
        """Gibt das geparste LLM-JSON zurück. Liefert der LLM-Aufruf
        success=False (Timeout/Fehler), wird GradingError geworfen —
        sonst würde ein Timeout stumm als 0-Punkte-Bewertung mit
        „Kein Feedback generiert.“ gespeichert und als Versuch zählen."""
        if not llm_result.get("success"):
            raise GradingError(
                f"LLM-Grading fehlgeschlagen: "
                f"{llm_result.get('error') or 'unbekannter Fehler'}")
        return llm_result.get("data") or {}

    @staticmethod
    def _previous_feedback_text(task: Task, submission: Submission, session: Session) -> str:
        """LLM-Feedback der früheren Versuche dieses Studenten (Kontext für
        konsistentes Grading). Leerstring, wenn noch keines existiert."""
        prev = session.exec(
            select(Submission)
            .where(Submission.task_id == task.id)
            .where(Submission.student_id == submission.student_id)
            .where(Submission.id != submission.id)
            .where(Submission.attempt_number < submission.attempt_number)
            .order_by(Submission.attempt_number.asc())  # type: ignore[attr-defined]
        ).all()
        lines = []
        for sub in prev:
            for fb in sub.feedback_list:
                if fb.source == FeedbackSource.LLM and fb.comment.strip():
                    lines.append(f"Versuch #{sub.attempt_number}: {fb.comment}")
        if not lines:
            return ""
        return "DAS FEEDBACK AUS DEN LETZTEN SESSIONS WAR:\n" + "\n\n".join(lines)

    def get_llm_config(self, task: Task, session: Session) -> dict:
        """Liest LLM-Config via Resolver (course > global > .env > default)."""
        cfg = get_effective_llm_config(session, task.course_id)
        return {
            "api_url": cfg["api_url"],
            "api_key": cfg["api_key"],
            "model": cfg["model"],
            "grading_prompt": cfg["grading_prompt"],
        }

    async def grade_submission(
        self,
        task: Task,
        submission: Submission,
        session: Session,
        custom_prompt: Optional[str] = None,
    ) -> dict:
        """
        Korrigiert eine Einreichung.

        Returns:
            {
                "points": float,
                "max_points": int,
                "feedback": Feedback,   # Feedback-Objekt (DB)
                "comment": str,         # Der Feedback-Text als String (fuer Frontend)
            }
        """
        if task.task_type.value == "workspace":
            result = await self._grade_workspace(task, submission, session, custom_prompt)
        elif task.task_type.value == "upload":
            result = await self._grade_upload(task, submission, session, custom_prompt)
        else:
            result = await self._grade_text(task, submission, session, custom_prompt)

        # Status updaten
        submission.status = SubmissionStatus.GRADED
        session.add(submission)
        session.commit()
        session.refresh(submission)

        return result

    async def _grade_text(
        self, task: Task, submission: Submission, session: Session,
        custom_prompt: Optional[str] = None,
    ) -> dict:
        """LLM korrigiert Textaufgabe."""
        llm_cfg = self.get_llm_config(task, session)
        data = self._require_success(await self.llm.grade_text_task(
            task_description=task.description,
            model_solution=task.model_solution or "(Keine Musterloesung hinterlegt — bitte eigenstaendig bewerten)",
            student_solution=submission.solution,
            max_points=task.max_points,
            text_template=task.text_template,
            custom_prompt=custom_prompt,
            previous_feedback=self._previous_feedback_text(task, submission, session),
            config=llm_cfg,
        ))
        points = float(data.get("points", 0))
        comment = data.get("feedback", "Kein Feedback generiert.")

        feedback = Feedback(
            submission_id=submission.id,
            source=FeedbackSource.LLM,
            points_earned=points,
            comment=comment,
        )
        session.add(feedback)
        session.commit()
        session.refresh(feedback)

        return {
            "points": points,
            "max_points": task.max_points,
            "feedback": feedback,
            "comment": comment,
        }

    # ──────────────────────────────────────────────────────────────
    # WORKSPACE-AUFGABEN (frischer Einweg-Container + LLM)
    # ──────────────────────────────────────────────────────────────

    async def _grade_workspace(
        self, task: Task, submission: Submission, session: Session,
        custom_prompt: Optional[str] = None,
    ) -> dict:
        """
        Workspace-Aufgabe: 1) Einweg-Container mit Student-Snapshot +
        hidden-Dateien aufsetzen und die Test-Skripte ausfuehren
        (oeffentliche test.sh + hiddenes Judge .test_private.sh,
        je falls hinterlegt),
        2) LLM korrigiert (Dateien + Test-Output).
        Ohne Test-Skripte: Grading OHNE Test-Ausgaben.
        """
        from services.workspace_service import (
            workspace_service, TEST_SCRIPT, ensure_fresh_workspace)
        from services.compute_client import ComputeAgentError, ComputeAgentUnavailable

        judge = workspace_service.judge_script_path(session, task)
        # Testlauf-Schritte: öffentliche test.sh (Konvention, falls
        # hinterlegt) + privater Judge — beide Outputs gehen ans LLM.
        task_file_paths = {
            f.path for f in workspace_service.task_files(session, task)}
        steps: list[tuple[str, str]] = []
        if TEST_SCRIPT in task_file_paths:
            steps.append((TEST_SCRIPT, f"bash {TEST_SCRIPT}"))
        if judge:
            steps.append((judge, f"bash {judge}"))
        default_timeout = int(task.workspace_timeout or 900)

        agent = workspace_service.pick_task_agent(session, task)
        if agent is None:
            raise ComputeAgentUnavailable(
                "Kein Compute-Agent für das Grading verfügbar")
        client = workspace_service.client_for(agent)
        key = workspace_service.grading_key(task, submission.id)

        # Lauf-Historie: WorkspaceRun-Row (Tutor sieht den Grading-Lauf in der Review)
        from services.workspace_service import MAX_LOG_CHARS
        run = WorkspaceRun(
            run_id=f"grade-{submission.id}-{int(time.time() * 1000)}",
            task_id=task.id,
            student_id=submission.student_id,
            submission_id=submission.id,
            command="; ".join(cmd for _, cmd in steps),
            started_at=datetime.now(),
            status=WorkspaceRunStatus.RUNNING,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        run_finalized = False

        def _finalize_run(status: WorkspaceRunStatus, exit_code: int | None = None,
                          stdout: str = "", stderr: str = "") -> None:
            nonlocal run_finalized
            if run_finalized:
                return
            run.status = status
            run.exit_code = exit_code
            if stdout:
                run.stdout = stdout[-MAX_LOG_CHARS:]
            if stderr:
                run.stderr = stderr[-MAX_LOG_CHARS:]
            run.finished_at = datetime.now()
            session.add(run)
            session.commit()
            run_finalized = True

        test_output = ""
        try:
            # Einweg-Workspace: Student-Snapshot + hidden-Dateien (frisch
            # von der Disk), garantiert frisches Volume (Alt-Überreste
            # würden den Snapshot stumm überschreiben lassen).
            starter = workspace_service.grading_starter_files(
                session, task, submission)
            folders = workspace_service.materialize_folders(
                session, task, include_hidden=True)
            spec_for_agent = workspace_service.workspace_spec_for_agent(
                session, task, agent)
            await asyncio.to_thread(
                ensure_fresh_workspace, client, key, spec_for_agent,
                starter, folders)

            sections = []
            raw_out: list[str] = []
            raw_err: list[str] = []
            exit_code: int | None = None
            timed_out = False
            for label, cmd in steps:
                result = await asyncio.to_thread(
                    client.exec_sync, key, cmd, default_timeout)
                sections.append(self._format_workspace_run(result, label))
                raw_out.append(f"── {label} ──\n{result.get('stdout') or ''}")
                err = result.get("stderr") or ""
                if err:
                    raw_err.append(f"── {label} (stderr) ──\n{err}")
                exit_code = result.get("exit_code")
                timed_out = timed_out or bool(result.get("timed_out"))
            if steps:
                test_output = "\n\n".join(sections)
                _finalize_run(
                    WorkspaceRunStatus.TIMEOUT if timed_out
                    else WorkspaceRunStatus.DONE,
                    exit_code,
                    "\n\n".join(raw_out),
                    "\n\n".join(raw_err),
                )
            else:
                test_output = ("(Keine Test-Skripte hinterlegt — Grading "
                               "ohne Test-Ausgaben)")
                _finalize_run(WorkspaceRunStatus.DONE, 0)
        except Exception as e:  # noqa: BLE001 — Run-Row finalisieren, Fehler weiterwerfen
            if not run_finalized:
                try:
                    _finalize_run(
                        WorkspaceRunStatus.KILLED,
                        stderr=f"[grading] Fehler beim Verify-Lauf: {e}",
                    )
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            try:
                await asyncio.to_thread(client.delete_workspace, key)
            except ComputeAgentError:
                pass

        ctx = workspace_service.grade_context(session, task, submission)

        llm_cfg = self.get_llm_config(task, session)
        data = self._require_success(await self.llm.grade_workspace_task(
            task_description=task.description,
            private_files=self._format_files(ctx.get("private_files") or {}) or
                               "(Keine privaten Dateien hinterlegt — bitte eigenstaendig bewerten)",
            student_files=self._format_files(ctx.get("student_files") or {}),
            test_results=test_output,
            max_points=task.max_points,
            custom_prompt=custom_prompt,
            previous_feedback=self._previous_feedback_text(task, submission, session),
            config=llm_cfg,
        ))
        points = float(data.get("points", 0))
        comment = data.get("feedback", "Kein Feedback generiert.")

        feedback = Feedback(
            submission_id=submission.id,
            source=FeedbackSource.LLM,
            points_earned=points,
            comment=comment,
        )
        session.add(feedback)
        session.commit()
        session.refresh(feedback)

        return {
            "points": points,
            "max_points": task.max_points,
            "feedback": feedback,
            "comment": comment,
        }

    async def _grade_upload(
        self, task: Task, submission: Submission, session: Session,
        custom_prompt: Optional[str] = None,
    ) -> dict:
        """Upload-Aufgabe: LLM korrigiert Musterlösung (+ Dateien) +
        hochgeladene Studenten-Dateien (Binarys/Übergroßes aussortiert,
        Jupyter-Outputs gestrippt — s. upload_service.collect_upload_texts)."""
        from services import upload_service

        student_path = upload_service.submission_upload_file(
            submission.id, submission.upload_file)
        student_files = (
            upload_service.collect_upload_texts(student_path)
            if student_path else {})
        model_path = upload_service.task_upload_file(
            task.id, task.upload_solution_file)
        model_files = (
            upload_service.collect_upload_texts(model_path) if model_path else {})

        llm_cfg = self.get_llm_config(task, session)
        data = self._require_success(await self.llm.grade_upload_task(
            task_description=task.description,
            model_solution=task.model_solution or "(Keine Musterloesung hinterlegt — bitte eigenstaendig bewerten)",
            model_solution_files=self._format_files(model_files) or
                                  "(Keine Musterloesungs-Dateien hinterlegt)",
            student_files=self._format_files(student_files) or
                           "(Keine lesbaren Dateien eingereicht)",
            max_points=task.max_points,
            custom_prompt=custom_prompt,
            previous_feedback=self._previous_feedback_text(task, submission, session),
            config=llm_cfg,
        ))
        points = float(data.get("points", 0))
        comment = data.get("feedback", "Kein Feedback generiert.")

        feedback = Feedback(
            submission_id=submission.id,
            source=FeedbackSource.LLM,
            points_earned=points,
            comment=comment,
        )
        session.add(feedback)
        session.commit()
        session.refresh(feedback)

        return {
            "points": points,
            "max_points": task.max_points,
            "feedback": feedback,
            "comment": comment,
        }

    @staticmethod
    def _format_files(files: dict[str, str]) -> str:
        """Datei-Verzeichnis (Pfad → Text) für den LLM-Prompt."""
        if not files:
            return "(Keine Dateien hintergelegt)"
        parts = []
        for name in sorted(files):
            parts.append(f"=== {name} ===\n{files[name]}")
        return "\n\n".join(parts)

    @staticmethod
    def _format_workspace_run(result: dict, label: str = "") -> str:
        """Testlauf (Agent-Exec) als lesbaren Text für den LLM."""
        lines = []
        if label:
            lines.append(f"── {label} ──")
        if result.get("timed_out"):
            lines.append("⏰ TIMEOUT: Zeitlimit des Testlaufs ueberschritten")
        else:
            lines.append(f"Exit-Code: {result.get('exit_code')}")
        stdout = str(result.get("stdout") or "").strip()
        stderr = str(result.get("stderr") or "").strip()
        if stdout:
            lines.append(f"--- stdout ---\n{stdout[-20000:]}")
        if stderr:
            lines.append(f"--- stderr ---\n{stderr[-20000:]}")
        return "\n".join(lines) if lines else "(Kein Output)"


        return "\n".join(lines)