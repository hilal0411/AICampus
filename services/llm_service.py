"""
LLM-Service: Kommunikation mit OpenAI-kompatiblen Endpoints.

Unterstützt:
- Qwen3, Llama, Mistral, etc. (jedes OpenAI-kompatible Modell)
- Grading (Korrektur) mit JSON-Response
- Task-Generierung (Aufgaben neu erstellen/ändern, Musterlösung, Code-Templates)
- Config pro Kurs (URL, Modell, Prompt)
"""

import asyncio
import inspect
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Any

from jinja2 import Template
from openai import AsyncOpenAI, BadRequestError
from sqlmodel import Session, select

from config import LLM_API_URL, LLM_API_KEY, LLM_MODEL, LLM_TEMPERATURE, LLM_TIMEOUT
from database.base import engine
from database.models import LLMDebugEntry, User
from services.auth_service import get_current_user_id
from services.timeutil import utc_iso
from services.workspace_presets import IMAGE_SELECTION_FIELDS
from prompts.grading_prompt import (
    GRADING_TEXT_PROMPT_TEMPLATE,
    GRADING_WORKSPACE_PROMPT_TEMPLATE,
    GRADING_UPLOAD_PROMPT_TEMPLATE,
)
from prompts.creation_prompt import UNIFIED_TASK_PROMPT_TEMPLATE
from prompts.workspace_task_prompt import (
    WORKSPACE_TASK_PROMPT_TEMPLATE,
    DATASET_CATALOG,
)
from prompts.script_prompt import SCRIPT_SECTION_PROMPT_TEMPLATE
from prompts.slides_prompt import SLIDES_PROMPT_TEMPLATE
from prompts.markdown_manual import (
    SCRIPT_MARKDOWN_MANUAL,
    SLIDES_MARKDOWN_MANUAL,
    SCRIPT_CONTENT_EDITS_SPEC,
    SLIDES_CONTENT_EDITS_SPEC,
)
from prompts.mc_task_prompt import MC_TASK_PROMPT_TEMPLATE
from prompts.hint_prompt import SOCRATIC_HINT_PROMPT_TEMPLATE
from prompts.script_question_prompt import SCRIPT_FOLLOW_UP_PROMPT_TEMPLATE, SCRIPT_QUESTION_PROMPT_TEMPLATE
from prompts.report_prompt import COURSE_REPORT_PROMPT_TEMPLATE, STUDENT_REPORT_PROMPT_TEMPLATE
from prompts.applet_prompt import APPLET_PROMPT_TEMPLATE
from prompts.svg_prompt import SVG_PROMPT_TEMPLATE
from prompts.import_prompt import (
    FILE_SUMMARY_PROMPT_TEMPLATE,
    SCRIPT_PLANNER_PROMPT_TEMPLATE,
    SLIDES_PLANNER_PROMPT_TEMPLATE,
    TASKS_PLANNER_PROMPT_TEMPLATE,
    TASKS_GENERATE_PROMPT_TEMPLATE,
    CONVERT_CHAPTER_PROMPT_TEMPLATE,
    CHAPTER_SUMMARY_PROMPT_TEMPLATE,
    SLIDE_DECK_PROMPT_TEMPLATE,
    GATHER_PROMPT_TEMPLATE,
    REF_EXTRACT_PROMPT_TEMPLATE,
    PAGE_TRANSCRIBE_PROMPT_TEMPLATE,
    IMAGE_TRIAGE_PROMPT_TEMPLATE,
    DECK_SUMMARY_PROMPT_TEMPLATE,
    SCRIPT_FORMAT_SPEC,
    SLIDE_FORMAT_SPEC,
    LABEL_RULES_SCRIPT,
    LABEL_RULES_SLIDES,
)

logger = logging.getLogger(__name__)

# Report-Timeout ist höher, da der Prompt sehr groß sein kann
REPORT_TIMEOUT = int(os.getenv("REPORT_TIMEOUT", "180"))
# Applet-Generierung: komplettes HTML-Dokument → längeres Budget + mehr Tokens
APPLET_TIMEOUT = int(os.getenv("APPLET_TIMEOUT", "240"))
APPLET_MAX_TOKENS = int(os.getenv("APPLET_MAX_TOKENS", "16384"))
# Slide-Deck-Generierung: Decks können lang sein (viele Folien + Sprechernotizen)
SLIDES_MAX_TOKENS = int(os.getenv("SLIDES_MAX_TOKENS", "16384"))
# Fortsetzen abgeschnittener Antworten (finish_reason=length): max. Folge-Queries
LLM_MAX_CONTINUATIONS = int(os.getenv("LLM_MAX_CONTINUATIONS", "4"))


# ── LLM Debug Log (persistent, letzte 7 Tage) ────────────────────
# Protokolliert jeden LLM-Call für die Admin-Konsole (Tab "LLM-Debug-Log").
# Gespeichert in der Datenbank (Tabelle llm_debug_entries, überlebt
# Server-Restarts); alte Einträge werden automatisch purgt
# (TTL + Größen-/Eintragslimits).
_LLM_DEBUG_TTL = timedelta(days=7)
_LLM_DEBUG_MAX_FIELD = 200_000        # max. Zeichen je Textfeld
_LLM_DEBUG_MAX_TOTAL_CHARS = 5_000_000  # max. Gesamtgröße aller Einträge
_LLM_DEBUG_MAX_DB_ENTRIES = 500       # max. Einträge, die in der DB gespeichert werden
_LLM_DEBUG_PURGE_INTERVAL = 3600      # Purge-Intervall in Sekunden

_llm_debug_lock = threading.Lock()
_llm_debug_last_purge = 0.0


def _debug_truncate(text: Optional[str]) -> str:
    """Begrenzt ein Log-Feld auf _LLM_DEBUG_MAX_FIELD Zeichen (Speicher-Schutz)."""
    if not text:
        return ""
    if len(text) <= _LLM_DEBUG_MAX_FIELD:
        return text
    return text[:_LLM_DEBUG_MAX_FIELD] + f"\n… [abgeschnitten: {len(text) - _LLM_DEBUG_MAX_FIELD} weitere Zeichen]"


def _debug_caller_label() -> str:
    """Bestimmt das Label aus dem Namen der aufrufenden Service-Methode.

    Geht die Call-Stack um die internen Helper (_call_plain/_call_with_json)
    herum, z. B. grade_text_task → GRADE_TEXT_TASK, import_convert_chapter →
    IMPORT_CONVERT_CHAPTER. Damit alle Aufrufer automatisch geloggt werden.
    """
    internal = {"_debug_caller_label", "_call_plain", "_call_with_json"}
    try:
        frame = inspect.currentframe()
        frame = frame.f_back if frame else None
        while frame is not None:
            name = frame.f_code.co_name
            if name not in internal and not name.startswith("<"):
                return name.upper()
            frame = frame.f_back
    except Exception:
        pass
    return "UNKNOWN"


def _entry_chars(entry: LLMDebugEntry) -> int:
    """Gesamtgröße der Textfelder eines Log-Eintrags."""
    return sum(
        len(getattr(entry, key) or "")
        for key in ("system_prompt", "prompt", "response", "thinking", "error")
    )


def _purge_llm_debug_entries(session: Session) -> None:
    """Löscht abgelaufene Einträge und begrenzt Größe/Eintragszahl (älteste zuerst).

    Geht von den neuesten Einträgen zurück und behält so viele bei, wie die
    Limits zulassen; alles Ältere (inkl. älter als TTL) wird gelöscht.
    """
    rows = session.exec(select(LLMDebugEntry).order_by(LLMDebugEntry.ts.asc())).all()
    if not rows:
        return
    cutoff = datetime.now() - _LLM_DEBUG_TTL
    keep: list[LLMDebugEntry] = []
    total = 0
    for row in reversed(rows):  # neueste zuerst
        if row.ts < cutoff:
            break
        chars = _entry_chars(row)
        if total + chars > _LLM_DEBUG_MAX_TOTAL_CHARS:
            break
        if len(keep) >= _LLM_DEBUG_MAX_DB_ENTRIES:
            break
        total += chars
        keep.append(row)
    keep_ids = {row.id for row in keep}
    for row in rows:
        if row.id not in keep_ids:
            session.delete(row)
    session.commit()


def _maybe_purge_llm_debug() -> None:
    """Führt höchstens alle _LLM_DEBUG_PURGE_INTERVAL Sekunden einen Purge aus."""
    global _llm_debug_last_purge
    if time.monotonic() - _llm_debug_last_purge < _LLM_DEBUG_PURGE_INTERVAL:
        return
    _llm_debug_last_purge = time.monotonic()
    try:
        with Session(engine) as session:
            _purge_llm_debug_entries(session)
    except Exception as e:
        logger.warning(f"LLM-Debug-Log: Purge fehlgeschlagen: {e}")


def record_llm_debug_entry(
    label: str,
    model: str,
    url: str,
    is_public: bool,
    system_prompt: str,
    prompt: str,
    response: str,
    thinking: str = "",
    success: bool = True,
    error: str = "",
    latency_ms: int = 0,
    attempts: int = 1,
) -> None:
    """Hängt einen LLM-Call an das persistente Debug-Log an (lässt Aufrufer nie scheitern).

    Der auslösende User wird aus dem Request-Context (ContextVar, gesetzt von
    den Auth-Dependencies) übernommen — auch in Hintergrund-Tasks, die vom
    auslösenden Request geerbt haben.
    """
    try:
        with _llm_debug_lock:
            _maybe_purge_llm_debug()
            with Session(engine) as session:
                session.add(LLMDebugEntry(
                    ts=datetime.now(),
                    label=label,
                    model=model or "",
                    url=url or "",
                    is_public=bool(is_public),
                    user_id=get_current_user_id(),
                    system_prompt=_debug_truncate(system_prompt),
                    prompt=_debug_truncate(prompt),
                    response=_debug_truncate(response),
                    thinking=_debug_truncate(thinking),
                    success=bool(success),
                    error=_debug_truncate(error),
                    latency_ms=int(latency_ms or 0),
                    attempts=int(attempts or 1),
                ))
                session.commit()
    except Exception as e:
        logger.warning(f"LLM-Debug-Log: Eintrag konnte nicht gespeichert werden: {e}")


def _llm_debug_user_names(session: Session, user_ids: set[int]) -> dict[int, str]:
    """user_id → Anzeigename (name, Fallback username)."""
    if not user_ids:
        return {}
    return {
        u.id: (u.name or u.username)
        for u in session.exec(select(User).where(User.id.in_(user_ids))).all()
    }


def get_llm_debug_log(offset: int = 0, limit: int = 20, user_id: Optional[int] = None) -> dict:
    """Paginiertes Debug-Log (neueste zuerst) — NUR Metadaten, keine Volltexte.

    Die Volltexte (System-Prompt/Prompt/Antwort/Thinking) werden erst auf
    Anfrage via get_llm_debug_entry() geladen (Lazy-Loading in der UI).
    Returns: entries (Metadaten), total, users (distinct für den Filter), offset, limit.
    """
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 20), 100))
    try:
        cutoff = datetime.now() - _LLM_DEBUG_TTL
        with Session(engine) as session:
            base = select(LLMDebugEntry).where(LLMDebugEntry.ts >= cutoff)
            if user_id is not None:
                base = base.where(LLMDebugEntry.user_id == user_id)
            rows = session.exec(
                base.order_by(LLMDebugEntry.ts.desc(), LLMDebugEntry.id.desc())
                .offset(offset).limit(limit)
            ).all()
            total = len(
                session.exec(
                    select(LLMDebugEntry.id).where(LLMDebugEntry.ts >= cutoff)
                ).all()
            )
            # Alle distinct Users im Retention-Fenster (für den Filter-Dropdown,
            # unabhängig vom aktuell gesetzten User-Filter)
            distinct_user_ids = {
                u for u in session.exec(
                    select(LLMDebugEntry.user_id)
                    .where(LLMDebugEntry.ts >= cutoff)
                    .where(LLMDebugEntry.user_id.isnot(None))
                    .distinct()
                ).all()
                if u is not None
            }
        user_names: dict[int, str] = {}
        if distinct_user_ids:
            with Session(engine) as session:
                user_names = _llm_debug_user_names(session, distinct_user_ids)
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "users": [
                {"id": uid, "name": name}
                for uid, name in sorted(user_names.items(), key=lambda kv: kv[1].lower())
            ],
            "entries": [
                {
                    "id": row.id,
                    "ts": utc_iso(row.ts),
                    "label": row.label,
                    "user_id": row.user_id,
                    "user": user_names.get(row.user_id),
                    "model": row.model,
                    "url": row.url,
                    "is_public": row.is_public,
                    "success": row.success,
                    "error": (row.error or "")[:300],  # Exzerpt (Volltext via Detail-Endpoint)
                    "latency_ms": row.latency_ms,
                    "attempts": row.attempts,
                }
                for row in rows
            ],
        }
    except Exception as e:
        logger.warning(f"LLM-Debug-Log: Log konnte nicht geladen werden: {e}")
        return {"total": 0, "offset": offset, "limit": limit, "users": [], "entries": []}


def get_llm_debug_entry(entry_id: int) -> Optional[dict]:
    """Einzelnes Debug-Log-Eintrag MIT Volltexten — nur auf Anfrage (Lazy-Loading)."""
    try:
        cutoff = datetime.now() - _LLM_DEBUG_TTL
        with Session(engine) as session:
            row = session.get(LLMDebugEntry, entry_id)
            if row is None or row.ts < cutoff:
                return None
            user_name = None
            if row.user_id is not None:
                u = session.get(User, row.user_id)
                user_name = (u.name or u.username) if u else None
            return {
                "id": row.id,
                "ts": utc_iso(row.ts),
                "label": row.label,
                "user_id": row.user_id,
                "user": user_name,
                "model": row.model,
                "url": row.url,
                "is_public": row.is_public,
                "system_prompt": row.system_prompt,
                "prompt": row.prompt,
                "response": row.response,
                "thinking": row.thinking,
                "success": row.success,
                "error": row.error,
                "latency_ms": row.latency_ms,
                "attempts": row.attempts,
            }
    except Exception as e:
        logger.warning(f"LLM-Debug-Log: Eintrag {entry_id} konnte nicht geladen werden: {e}")
        return None


_CONTINUE_INSTRUCTION = (
    "Deine vorherige Antwort wurde wegen des Output-Limits abgeschnitten. Setze sie EXAKT an "
    "der Stelle fort, an der sie abbrach: gib NUR die fehlende Fortsetzung aus und beginne mit "
    "dem nächsten Zeichen — ohne Wiederholung der bereits geschriebenen Zeilen, ohne "
    "Einleitung, ohne Kommentare."
)


def _overlap_len(prev: str, nxt: str, max_check: int = 2000) -> int:
    """Länge des längsten Präfixes von `nxt`, der zugleich Suffix von `prev` ist
    (Overlap bei Antwort-Fortsetzungen, die das Modell manchmal wiederholt)."""
    if not nxt:
        return 0
    for n in range(min(max_check, len(nxt)), 0, -1):
        if prev.endswith(nxt[:n]):
            return n
    return 0


class LLMService:
    """
    Client für OpenAI-kompatible LLM-APIs.

    Usage:
        llm = LLMService()
        result = await llm.grade(task, student_solution)
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.api_url = api_url or LLM_API_URL
        self.api_key = api_key or LLM_API_KEY
        self.model = model or LLM_MODEL
        self.temperature = LLM_TEMPERATURE
        self.timeout = LLM_TIMEOUT

        self.client = AsyncOpenAI(
            base_url=self.api_url,
            api_key=self.api_key,
            timeout=self.timeout,
        )

    @staticmethod
    def _render_prompt(template: str, **kwargs: Any) -> str:
        """Substituiert __PLACEHOLDER__-Muster in einem Prompt-Template.

        Verwendet .replace() statt str.format() oder Jinja2, damit geschweifte
        Klammern im Prompt-Text (z. B. JSON-Beispiele) keine Probleme bereiten.
        """
        result = template
        for key, value in kwargs.items():
            placeholder = f"__{key.upper()}__"
            result = result.replace(placeholder, str(value))
        return result

    # ── Public methods ───────────────────────────────────────────

    async def grade_text_task(
        self,
        task_description: str,
        model_solution: str,
        student_solution: str,
        max_points: int,
        text_template: Optional[str] = None,
        custom_prompt: Optional[str] = None,
        config: Optional[dict] = None,
    ):
        """Korrigiert eine Textaufgabe via LLM."""

        prompt = self._render_prompt(
            custom_prompt or GRADING_TEXT_PROMPT_TEMPLATE,
            task_description=task_description,
            model_solution=model_solution,
            student_solution=student_solution,
            max_points=max_points,
            text_template=text_template or "(Keine Vorlage hintergelegt)",
        )

        return await self._call_with_json(prompt, response_format={"type": "json_object"}, config=config)

    async def grade_workspace_task(
        self,
        task_description: str,
        private_files: str,
        student_files: str,
        test_results: str,
        max_points: int,
        custom_prompt: Optional[str] = None,
        config: Optional[dict] = None,
    ):
        """Korrigiert eine Workspace-Aufgabe via LLM
        (Dateien + Test-Ausführung inkl. Test-Output-Metriken)."""

        prompt = self._render_prompt(
            custom_prompt or GRADING_WORKSPACE_PROMPT_TEMPLATE,
            task_description=task_description,
            private_files=private_files,
            student_solution=student_files,
            test_results=test_results,
            max_points=max_points,
        )

        return await self._call_with_json(prompt, response_format={"type": "json_object"}, config=config)

    async def grade_upload_task(
        self,
        task_description: str,
        model_solution: str,
        model_solution_files: str,
        student_files: str,
        max_points: int,
        custom_prompt: Optional[str] = None,
        config: Optional[dict] = None,
    ):
        """Korrigiert eine Upload-Aufgabe via LLM
        (Musterlösung/-Dateien + hochgeladene Studenten-Dateien)."""

        prompt = self._render_prompt(
            custom_prompt or GRADING_UPLOAD_PROMPT_TEMPLATE,
            task_description=task_description,
            model_solution=model_solution,
            model_solution_files=model_solution_files,
            student_solution=student_files,
            max_points=max_points,
        )

        return await self._call_with_json(prompt, response_format={"type": "json_object"}, config=config)

    async def generate_task_fields(
        self,
        topic: str,
        difficulty: str,
        task_type: str,
        max_points: int,
        generate_fields: list[str],
        current_title: str = "",
        current_description: str = "",
        current_model_solution: str = "",
        current_text_template: str = "",
        script_chapters: Optional[list[dict]] = None,
        course_media: Optional[list[dict]] = None,
        references: str = "",
        config: Optional[dict] = None,
    ):
        """Generiert/ändert die angeforderten Felder einer Text-Aufgabe via LLM.

        generate_fields: Untermenge von ["title", "description", "model_solution",
        "text_template"].
        Das LLM liefert JSON mit EXAKT diesen Schlüsseln — nicht angeforderte
        Felder werden nicht zurückgegeben.
        references: Kurs-Quellenverzeichnis als Text (Zitations-Keys) — leer,
        wenn der Kurs kein Quellenverzeichnis hat.

        Workspace-Aufgaben laufen über einen eigenen Single-Prompt:
        generate_workspace_task_fields.

        Enthält keine sensitive Studentendaten — nutzt daher den Public
        Endpoint, falls konfiguriert.
        """
        task_type_description = {
            "text": "Textaufgabe",
            "upload": ("Upload-Aufgabe (Studierende reichen ihre Lösung als Datei/Zip "
                       "ein; der Musterlösungstext dient hier als Bewertungskriterien)"),
            "extern": ("Externe Aufgabe (wird außerhalb des Systems bewertet, z. B. "
                       "Vortrag oder Notenübertragung aus anderen Systemen; "
                       "Studierende geben nichts ab; der Musterlösungstext = "
                       "Bewertungskriterien)"),
        }.get(task_type, task_type)
        generate_list = ", ".join(f'"{f}"' for f in generate_fields)

        prompt = Template(UNIFIED_TASK_PROMPT_TEMPLATE).render(
            task_type_description=task_type_description,
            topic=topic,
            difficulty=difficulty,
            max_points=max_points,
            generate_list=generate_list,
            current_title=current_title,
            current_description=current_description,
            current_model_solution=current_model_solution,
            current_text_template=current_text_template,
            script_chapters=script_chapters or [],
            course_media=course_media or [],
            references=references,
        )

        return await self._call_with_json(
            prompt, response_format={"type": "json_object"}, config=self._public_config(config)
        )

    async def generate_mc_task_fields(
        self,
        topic: str,
        difficulty: str,
        max_points: int,
        generate_fields: list[str],
        current_title: str = "",
        current_description: str = "",
        current_model_solution: str = "",
        current_questions: str = "",
        script_chapters: Optional[list[dict]] = None,
        course_media: Optional[list[dict]] = None,
        references: str = "",
        config: Optional[dict] = None,
    ):
        """Generiert/ändert die angeforderten Felder einer MC-AUFGABE in
        EINEM LLM-Call (Single-Prompt): title, description, model_solution,
        mc_questions (Untermenge).

        mc_questions: [{question, points, multi_select,
        options: [{text, correct, feedback}]}] — das LLM legt auch das
        Feedback je Option an (wird Studenten nach dem Einreichen gezeigt).

        current_questions: bestehende Fragen als JSON-Text (Kontext für
        Änderungs-Wünsche) — leer bei Neu-Generierung.

        Enthält keine sensitive Studentendaten — nutzt daher den Public
        Endpoint, falls konfiguriert.
        """
        # Fragen-Anzahl passend zum Punktebudget andeuten
        if max_points <= 3:
            question_count_hint = "2"
        elif max_points <= 6:
            question_count_hint = "2–3"
        else:
            question_count_hint = "3–4"

        generate_list = ", ".join(f'"{f}"' for f in generate_fields)

        prompt = Template(MC_TASK_PROMPT_TEMPLATE).render(
            topic=topic or "(Kein Thema angegeben — überarbeite die Aufgabe sinnvoll.)",
            difficulty=difficulty,
            max_points=max_points,
            question_count_hint=question_count_hint,
            generate_list=generate_list,
            current_title=current_title,
            current_description=current_description,
            current_model_solution=current_model_solution,
            current_questions=current_questions,
            script_chapters=script_chapters or [],
            course_media=course_media or [],
            references=references,
        )

        return await self._call_with_json(
            prompt, response_format={"type": "json_object"}, config=self._public_config(config)
        )

    async def generate_workspace_task_fields(
        self,
        topic: str,
        difficulty: str,
        max_points: int,
        generate_fields: list[str],
        current_title: str = "",
        current_description: str = "",
        current_model_solution: str = "",
        current_env: Optional[dict] = None,
        current_files: Optional[list[dict]] = None,
        engines: Optional[list[dict]] = None,
        image_specs: Optional[list[dict]] = None,
        script_chapters: Optional[list[dict]] = None,
        course_media: Optional[list[dict]] = None,
        references: str = "",
        require_image_selection: bool = True,
        config: Optional[dict] = None,
    ):
        """Generiert/ändert die angeforderten Felder einer Workspace-Aufgabe
        in EINEM LLM-Call (Single-Prompt): title, description,
        model_solution (Skizze + Bewertungskriterien), env, files
        (Volltext ODER stellenweise Datei-„edits“), folders,
        delete_files (NUR bei bestehenden Dateien) — plus
        workspace_image/workspace_engines/proposed_image_spec, wenn
        require_image_selection (das LLM schlägt vor, die
        Infrastruktur befehligt nie; Validierung serverseitig).

        generate_fields ist in der Abarbeitungs-REIHENFOLGE übergeben
        (Implementierung zuerst, dann Lösungsskizze/Kriterien, dann
        Beschreibung/Titel) — das Template weist das LLM darauf hin, genau
        dieser Reihenfolge zu folgen.
        current_env: bestehende Umgebungsfelder
        {workspace_timeout, workspace_cpu, workspace_memory, workspace_internet,
        workspace_main_file} als Prompt-Kontext (JSON).
        current_files: bestehende Task-Dateien [{path, content}] als Kontext.
        engines: Compute-Engines des Kurses [{name, healthy, gpu_info,
        installed_specs: [{name, dockerfile}]}] (Prompt-Kontext für
        workspace_engines + das passende Image+Engine-Paar).
        image_specs: Image-Specs [{name, scope, base, dockerfile}]
        (Prompt-Kontext für workspace_image/proposed_image_spec).

        Enthält keine Studentendaten — nutzt den Public Endpoint, falls konfiguriert.
        """
        import json as _json
        mandatory = list(IMAGE_SELECTION_FIELDS) if require_image_selection else []
        generate_list = ", ".join(
            f'"{f}"' for f in generate_fields + mandatory
        )

        prompt = Template(WORKSPACE_TASK_PROMPT_TEMPLATE).render(
            topic=topic or "(Kein Thema angegeben — überarbeite die Aufgabe sinnvoll.)",
            difficulty=difficulty,
            max_points=max_points,
            generate_list=generate_list,
            dataset_catalog=DATASET_CATALOG,
            engines=engines or [],
            image_specs=image_specs or [],
            current_title=current_title,
            current_description=current_description,
            current_model_solution=current_model_solution,
            current_env=_json.dumps(current_env or {}, ensure_ascii=False, indent=2),
            current_files=current_files or [],
            script_chapters=script_chapters or [],
            course_media=course_media or [],
            references=references,
            require_image_selection=require_image_selection,
        )

        return await self._call_with_json(
            prompt, response_format={"type": "json_object"}, config=self._public_config(config)
        )

    async def generate_image_spec(
        self,
        description: str,
        name: Optional[str] = None,
        current_dockerfile: Optional[str] = None,
        config: Optional[dict] = None,
    ):
        """LLM generiert ein komplettes Dockerfile (Image-Spec) aus einer
        Beschreibung. Das Ergebnis ist
        ein ENTWURF: validiert hier (Dockerfile-Regeln), bestätigt vom
        Tutor/Prof in der UI, erst dann gespeichert/installiert.

        Mit current_dockerfile: LLM passt das bestehende Dockerfile an
        (description = Änderungswunsch) statt eines neuen zu erzeugen.

        Der Name kommt aus dem vorgegebenen name-Param (das Dockerfile
        selbst enthält keinen Namen).

        Liefert {success, dockerfile?, name?, error?}.
        """
        from prompts.image_spec_prompt import IMAGE_SPEC_PROMPT_TEMPLATE
        from compute_agent.image_spec import ImageSpecError, validate

        prompt = Template(IMAGE_SPEC_PROMPT_TEMPLATE).render(
            description=description or "(Keine Beschreibung angegeben)",
            name=name or "",
            current_dockerfile=current_dockerfile or "",
        )
        result = await self._call_plain(prompt, config=self._public_config(config),
                                        max_tokens=4096)
        if not result.get("success"):
            return {"success": False,
                    "error": result.get("error", "LLM-Fehler")}
        dockerfile = self._extract_code_block(result.get("raw_response") or "")
        if not dockerfile or dockerfile == "(keine Antwort)":
            return {"success": False, "error": "LLM lieferte kein Dockerfile."}
        try:
            validate(dockerfile)
        except ImageSpecError as e:
            return {"success": False, "error": f"LLM-Dockerfile ungültig: {e}"}
        return {"success": True, "dockerfile": dockerfile,
                "name": (name or "").strip()}

    @staticmethod
    def _extract_code_block(text: str) -> str:
        """Codeblock aus der LLM-Antwort extrahieren (Code-Zaun optional,
        beliebiges Sprach-Tag, z. B. yaml/dockerfile)."""
        import re as _re
        m = _re.search(r"```(?:[a-zA-Z0-9_-]+)?\s*\n(.*?)```", text, _re.DOTALL)
        if m:
            return m.group(1).strip()
        return text.strip()

    async def generate_script_section(
        self,
        course_name: str,
        topic: str,
        generate_fields: list[str],
        current_title: str = "",
        current_content: str = "",
        other_chapters: Optional[list[dict]] = None,
        unused_media: Optional[list[dict]] = None,
        course_tasks: Optional[list[dict]] = None,
        references: str = "",
        config: Optional[dict] = None,
    ):
        """Generiert/ändert die angeforderten Felder eines Skript-Kapitels via LLM.

        topic: Vereinheitlichtes Feld aus der UI (Thema des Kapitels und/oder
        konkrete Änderungswünsche).
        generate_fields: Untermenge von ["title", "content", "summary"].
        other_chapters: [{title, summary}] der anderen Kapitel (Konsistenz).
        unused_media: [{title, description, url}] — noch nicht im Skript
        verwendete Medien (ggf. einbindbar).
        course_tasks: [{id, title}] — Übungsaufgaben des Kurses (ggf. per
        @task:{id} im Kapitel einbindbar → Aufgaben-Box für Studenten).
        references: Kurs-Quellenverzeichnis als Text (Zitations-Keys) — leer,
        wenn der Kurs kein Quellenverzeichnis hat.
        Das LLM liefert JSON mit einer Untermenge der angeforderten Schlüssel —
        weggelassene Schlüssel = das Feld bleibt unverändert (leeres Feld im
        Response, Frontend behält den vorhandenen Wert). Für lokale Änderungen
        an vorhandenem Inhalt kann es „content_edits“ (Liste von Edit-Objekten)
        statt „content“ liefern — die Anwendung auf den bestehenden Inhalt
        erfolgt serverseitig (api.script._apply_content_edits).

        Enthält keine sensitive Studentendaten — nutzt daher den Public
        Endpoint, falls konfiguriert.
        """
        generate_list = ", ".join(f'"{f}"' for f in generate_fields)

        prompt = Template(SCRIPT_SECTION_PROMPT_TEMPLATE).render(
            course_name=course_name,
            topic=topic or "(Kein Thema angegeben — überarbeite das Kapitel sinnvoll.)",
            generate_list=generate_list,
            other_chapters=other_chapters or [],
            unused_media=unused_media or [],
            course_tasks=course_tasks or [],
            references=references,
            current_title=current_title,
            current_content=current_content,
            markdown_manual=SCRIPT_MARKDOWN_MANUAL,
            edits_spec=SCRIPT_CONTENT_EDITS_SPEC,
        )

        return await self._call_with_json(
            prompt, response_format={"type": "json_object"}, config=self._public_config(config)
        )

    async def generate_slide_deck(
        self,
        course_name: str,
        topic: str,
        minutes: int,
        generate_fields: list[str],
        current_title: str = "",
        current_content: str = "",
        chapters: Optional[list[dict]] = None,
        course_media: Optional[list[dict]] = None,
        course_tasks: Optional[list[dict]] = None,
        references: str = "",
        config: Optional[dict] = None,
    ):
        """Generiert/ändert ein Slide-Deck (Vorlesungsfolien) via LLM.

        topic: Vereinheitlichtes Feld aus der UI (Thema des Decks und/oder
        konkrete Änderungswünsche, z.B. „Folien zu Kapitel 3 erstellen“).
        minutes: Dauer der Präsentation in Minuten → das LLM plant die
        Folienanzahl entsprechend (Richtwert 1,5–2 min/Folie).
        generate_fields: Untermenge von ["title", "content", "summary"].
        chapters: [{title, labels, content}] der Skript-Kapitel (Inhalt als
        Folien-Basis; labels für Label-Konsistenz — gleiche Objekte dürfen
        dieselben Labels verwenden → gleiche Nummer + Link zum Skript).
        course_media: [{title, description, url}] — alle Medien des Kurses
        (ggf. einbinden; auch solche, die bereits im Skript vorkommen —
        Medien dürfen in Skript UND Slides verwendet werden).
        course_tasks: [{id, title}] — Übungsaufgaben des Kurses (ggf. per
        @task:{id} im Deck einbindbar → Aufgaben-Box).
        references: Kurs-Quellenverzeichnis als Text (Zitations-Keys) — leer,
        wenn der Kurs kein Quellenverzeichnis hat.
        current_content: bestehendes Deck — für die Folien-Referenz in
        „content_edits“ mit expliziten Nummern („%% Folie N %%“) vorliegen
        (s. slides_service.numbered_slide_content); die Marker sind KEIN
        Deck-Inhalt und werden vom LLM nicht zurückgeliefert.
        Das LLM liefert JSON mit einer Untermenge der angeforderten Schlüssel —
        weggelassene Schlüssel = das Feld bleibt unverändert (leeres Feld im
        Response, Frontend behält den vorhandenen Wert). Für lokale Änderungen
        an vorhandenem Inhalt kann es „content_edits“ (Liste von Edit-Objekten)
        statt „content“ liefern — die Anwendung auf den bestehenden Inhalt
        erfolgt serverseitig (slides_service.apply_slide_edits).

        Enthält keine sensitive Studentendaten — nutzt daher den Public
        Endpoint, falls konfiguriert.
        """
        generate_list = ", ".join(f'"{f}"' for f in generate_fields)

        prompt = Template(SLIDES_PROMPT_TEMPLATE).render(
            course_name=course_name,
            topic=topic or "(Kein Thema angegeben — erstelle ein sinnvolles Deck bzw. überarbeite das bestehende Deck.)",
            minutes=minutes,
            generate_list=generate_list,
            chapters=chapters or [],
            course_media=course_media or [],
            course_tasks=course_tasks or [],
            references=references,
            current_title=current_title,
            current_content=current_content,
            markdown_manual=SLIDES_MARKDOWN_MANUAL,
            edits_spec=SLIDES_CONTENT_EDITS_SPEC,
        )

        return await self._call_with_json(
            prompt,
            response_format={"type": "json_object"},
            config=self._public_config(config),
            max_tokens=SLIDES_MAX_TOKENS,
        )

    async def generate_applet(
        self,
        prompt: str,
        generate_fields: Optional[list[str]] = None,
        existing_media: str = "",
        current_title: str = "",
        current_description: str = "",
        image_data_url: Optional[str] = None,
        console_errors: Optional[list[str]] = None,
        config: Optional[dict] = None,
        format: str = "html",
    ):
        """Generiert/überarbeitet ein Applet (HTML oder SVG) via LLM.

        prompt: Was das Applet zeigen/ermöglichen soll (bzw. Änderungswunsch
        bei existing_media).
        generate_fields: Untermenge von ["title", "description", "media"],
        die das LLM in seiner JSON-Antwort liefern darf (Default: alle) —
        nicht angeforderte Felder bleiben unverändert. Das LLM MUSS sie aber
        nicht alle ändern, wenn die Anweisung sie nicht betrifft (Schlüssel
        weglassen = unverändert).
        existing_media: aktueller Applet-Code, HTML ODER SVG (Refinement).
        current_title/current_description: vorhandene Metadaten (Kontext für
        das LLM, z. B. für „nur Titel ändern“).
        image_data_url: optionales Referenzbild (Data-URL, z. B. Medium aus
        der Bibliothek oder hochgeladen/gezeichnet) — wird multimodal
        angehängt; das LLM nutzt es als Basis (Umwandlung/Skizze).
        console_errors: optionale Web-Konsole-Fehler aus der Applet-Preview
        (Fehlermeldung + Stack) — dienen dem LLM als Diagnose-Hinweis
        (nur HTML-Applets, die in der Preview laufen).
        format: "html" (Default) ODER "svg" — wählt das Prompt-Template.
        Das LLM liefert JSON mit einer Untermenge der angeforderten Schlüssel
        ({"media" ODER "media_edits", "title", "description"}) — wird hier
        NICHT gespeichert (Preview-first im Applet-Studio).

        Applets enthalten keine Studentendaten — nutzt den Public Endpoint,
        falls konfiguriert.
        """
        generate_list = ", ".join(
            f'"{f}"' for f in (generate_fields or ["title", "description", "media"])
        )
        template = SVG_PROMPT_TEMPLATE if format == "svg" else APPLET_PROMPT_TEMPLATE
        tpl = Template(template).render(
            prompt=prompt,
            generate_list=generate_list,
            existing_media=existing_media,
            current_title=current_title,
            current_description=current_description,
            has_image=bool(image_data_url),
            console_errors=console_errors or [],
        )

        # Komplettes HTML-Dokument → großzügiges Timeout + Token-Budget.
        cfg = dict(config or {})
        cfg["timeout"] = max(cfg.get("timeout", self.timeout), APPLET_TIMEOUT)

        return await self._call_with_json(
            tpl,
            response_format={"type": "json_object"},
            config=self._public_config(cfg),
            max_tokens=APPLET_MAX_TOKENS,
            images=[image_data_url] if image_data_url else None,
        )

    async def generate_socratic_hint(
        self,
        task_description: str,
        model_solution: str,
        text_template: str,
        current_solution: str,
        previous_submissions: str,
        hint_history: str,
        student_question: str,
        script_context: str = "",
        media_context: str = "",
        mc_context: str = "",
        custom_prompt: Optional[str] = None,
        config: Optional[dict] = None,
    ):
        """Generiert einen sokratischen Hinweis fuer einen Studenten.

        Verwendet die Sokratische Methode: Stellt Fragen, gibt gezielte Hinweise,
        aber verratet niemals die direkte Loesung.

        Returns JSON mit:
            hint: Markdown-formatierter Text
            suggestion_type: 'question' | 'hint' | 'encouragement' | 'correction'
        """
        prompt = self._render_prompt(
            custom_prompt or SOCRATIC_HINT_PROMPT_TEMPLATE,
            task_description=task_description,
            model_solution=model_solution,
            text_template=text_template or "(Keine Vorlage hintergelegt)",
            current_solution=current_solution or "(Noch keine Loesung)",
            previous_submissions=previous_submissions or "(Keine vorherigen Abgaben)",
            hint_history=hint_history or "(Dies ist die erste Frage)",
            student_question=student_question,
            script_context=script_context,
            media_context=media_context,
            mc_context=mc_context,
        )

        return await self._call_with_json(prompt, response_format={"type": "json_object"}, config=config)

    async def convert_image_to_latex(
        self,
        image_base64: str,
        mime_type: str = "image/png",
        config: Optional[dict] = None,
    ):
        """Konvertiert ein Foto einer handgeschriebenen Notiz mit Formeln in Markdown mit LaTeX.

        Nutzt die multimodalen Faehigkeiten des LLM, um das Bild zu analysieren
        und den enthaltenen Text, Graphen und die enthaltene(n) Formel(n) als Markdown mit LaTeX-Code zurueckzugeben.

        Laueft gestreamt, damit bei Timeout die bereits eingetroffene (partialle)
        Ausgabe erhalten bleibt und im Debug-Log landend — man sieht so, womit
        sich das Modell aufgehaelt hat. Das Reasoning ist deaktiviert
        (vLLM chat_template_kwargs): Transkription braucht kein Denken, und
        gerade das langsame Thinking ueberschritt bisher die Timeout-Schranke.
        """
        client = self._get_client(config)
        model = config.get("model", self.model) if config else self.model
        timeout = config.get("timeout", self.timeout) if config else self.timeout
        is_temp = config is not None
        url, model, is_public = self._effective_endpoint(config)

        max_retries = 2
        last_error = None
        total_ms = 0
        last_content = ""
        last_thinking = ""

        image_prompt_text = (
            "Konvertiere die Formel(n) in diesem Foto in Markdown, Mermaid und LaTeX-Code. "
            "Gib NUR den Markdown, Mermaid bzw LaTeX-Code zurueck. "
            "Enthält das Bild keinen Text oder Formeln, gib einen leeren String zurück."
        )
        # Bild-Base64 wird NICHT geloggt (Speicher + Datenschutz)
        log_prompt = f"[BILD: {mime_type}, {len(image_base64)} Zeichen Base64] {image_prompt_text}"

        system_prompt = (
            "Du bist ein Experte fuer das Erkennen von Text, Graphen und mathematischen Formeln in Bildern. "
            "Analysiere das Foto und konvertiere den Text und alle sichtbaren Graphen und Formeln in gueltigen Markdown mit Mermaid oder LaTeX-Code. "
            "Antworte NUR mit Markdown, Mermaid und LaTeX-Code, keine Erklaerungen. "
            "Nutze ```mermaid ... ``` für Mermaid und $ ... $ fuer inline Math und $$ ... $$ fuer display Math. "
        )

        messages = [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{image_base64}"
                        },
                    },
                    {
                        "type": "text",
                        "text": image_prompt_text,
                    },
                ],
            },
        ]

        # Transkription braucht kein Reasoning: Thinking aus (vLLM).
        no_thinking_body = {"chat_template_kwargs": {"enable_thinking": False}}

        deadline = time.monotonic() + timeout

        for attempt in range(max_retries):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_error = f"LLM-Timeout: Antwort nicht innerhalb von {timeout}s erhalten"
                logger.warning(f"convert_image_to_latex total timeout")
                break

            content_parts: list = []
            thinking_parts: list = []

            try:
                start = time.time()

                try:
                    stream = await client.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=0.0,
                        stream=True,
                        extra_body=no_thinking_body,
                    )
                except BadRequestError:
                    # Provider ohne vLLM-Unterstützung lehnt chat_template_kwargs ab
                    # (z. B. OpenAI) — ohne Thinking-Optimierung erneut versuchen.
                    stream = await client.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=0.0,
                        stream=True,
                    )

                stream_task = asyncio.ensure_future(
                    self._consume_stream(stream, content_parts, thinking_parts)
                )
                timer_task = asyncio.ensure_future(asyncio.sleep(remaining))
                try:
                    done, _ = await asyncio.wait(
                        {stream_task, timer_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if timer_task in done:
                        # Timeout: Stream abbrechen, partialle Ausgabe behalten
                        stream_task.cancel()
                        try:
                            await stream_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        partial_content = "".join(content_parts)
                        partial_thinking = "".join(thinking_parts)
                        last_content = partial_content or "(keine Antwort — durch Timeout abgebrochen)"
                        last_thinking = partial_thinking
                        last_error = (
                            f"LLM-Timeout: Antwort nicht innerhalb von {timeout}s erhalten "
                            f"(partiell geloggt: {len(partial_content)} Zeichen Antwort, "
                            f"{len(partial_thinking)} Zeichen Thinking)"
                        )
                        logger.warning(
                            f"convert_image_to_latex timeout (attempt {attempt+1}/{max_retries}), "
                            f"partiell: {len(partial_content)} Zeichen Antwort, "
                            f"{len(partial_thinking)} Zeichen Thinking"
                        )
                    else:
                        stream_task.result()  # wirft Stream-Fehler weiter
                        elapsed = time.time() - start
                        total_ms += round(elapsed * 1000)
                        content = "".join(content_parts).strip()
                        thinking = "".join(thinking_parts)
                        last_content = content or "(keine Antwort)"
                        last_thinking = thinking

                        if is_temp:
                            await client.close()

                        record_llm_debug_entry(
                            label="CONVERT_IMAGE_TO_LATEX", model=model, url=url, is_public=is_public,
                            system_prompt=system_prompt, prompt=log_prompt,
                            response=last_content, thinking=last_thinking,
                            success=True, latency_ms=total_ms, attempts=attempt + 1,
                        )
                        return {
                            "success": True,
                            "data": {"latex": content},
                            "latency_ms": round(elapsed * 1000),
                            "raw_response": content,
                        }
                finally:
                    if not timer_task.done():
                        timer_task.cancel()
                    if not stream_task.done():
                        stream_task.cancel()
                        try:
                            await stream_task
                        except (asyncio.CancelledError, Exception):
                            pass

            except asyncio.TimeoutError:
                last_error = f"LLM-Timeout: Antwort nicht innerhalb von {timeout}s erhalten"
                logger.warning(f"convert_image_to_latex timeout (attempt {attempt+1}/{max_retries})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))

            except Exception as e:
                last_error = str(e)
                logger.error(f"convert_image_to_latex error: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))

        if is_temp:
            await client.close()

        record_llm_debug_entry(
            label="CONVERT_IMAGE_TO_LATEX", model=model, url=url, is_public=is_public,
            system_prompt=system_prompt, prompt=log_prompt,
            response=last_content, thinking=last_thinking,
            success=False, error=last_error, latency_ms=total_ms, attempts=max_retries,
        )
        return {
            "success": False,
            "error": last_error,
            "data": {"latex": ""},
            "latency_ms": 0,
            "raw_response": last_content,
        }

    @staticmethod
    async def _consume_stream(stream, content_parts: list, thinking_parts: list) -> None:
        """Laeuft einen Chat-Completion-Stream ab und sammelt Content/Thinking.

        Die Teil-Listen werden von auessen uebergeben: Wird der Stream bei
        Timeout gecancelt, bleiben die bereits empfangenen Teile erhalten.
        """
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if delta.content:
                content_parts.append(delta.content)
            reasoning = LLMService._extract_thinking_delta(delta)
            if reasoning:
                thinking_parts.append(reasoning)

    @staticmethod
    def _extract_thinking_delta(delta) -> str:
        """Extrahiert optionales Reasoning/Thinking aus einem Streaming-Delta.

        Je nach Provider (z. B. Qwen3 via vLLM/Ollama) liegt das Reasoning in
        reasoning_content/reasoning — teils nur in model_extra.
        """
        for attr in ("reasoning_content", "reasoning"):
            value = getattr(delta, attr, None)
            if isinstance(value, str) and value:
                return value
        extra = getattr(delta, "model_extra", None)
        if isinstance(extra, dict):
            for key in ("reasoning_content", "reasoning"):
                value = extra.get(key)
                if isinstance(value, str) and value:
                    return value
        return ""

    async def _describe_media(
        self,
        media_part: dict,
        log_hint: str,
        prompt_text: str,
        system_prompt: str,
        label: str,
        config: Optional[dict] = None,
    ):
        """Gemeinsamer LLM-Call für die Beschreibung von Kurs-Medien (Bild/Video).

        media_part: fertiger OpenAI-Content-Part (image_url bzw. video_url) mit
        data:...-URL — das Base64 wird NICHT geloggt (Speicher + Datenschutz).
        """
        client = self._get_client(config)
        model = config.get("model", self.model) if config else self.model
        timeout = config.get("timeout", self.timeout) if config else self.timeout
        is_temp = config is not None
        url, model, is_public = self._effective_endpoint(config)

        max_retries = 2
        last_error = None
        total_ms = 0
        last_content = ""
        last_thinking = ""

        log_prompt = f"{log_hint} {prompt_text}"

        deadline = time.monotonic() + timeout

        for attempt in range(max_retries):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_error = f"LLM-Timeout: Antwort nicht innerhalb von {timeout}s erhalten"
                logger.warning(f"{label} total timeout")
                break

            try:
                start = time.time()

                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=model,
                        messages=[
                            {
                                "role": "system",
                                "content": system_prompt,
                            },
                            {
                                "role": "user",
                                "content": [media_part, {"type": "text", "text": prompt_text}],
                            },
                        ],
                        temperature=0.0,
                    ),
                    timeout=remaining,
                )

                elapsed = time.time() - start
                total_ms += round(elapsed * 1000)
                content = response.choices[0].message.content or ""
                last_content = content
                last_thinking = self._extract_thinking(response)

                result = None
                if content:
                    try:
                        result = json.loads(content)
                    except json.JSONDecodeError:
                        result = self._extract_json(content)
                if result is None or not isinstance(result.get("description"), str):
                    last_error = "LLM hat kein gultiges JSON geliefert"
                    logger.warning(f"{label} JSON extraction failed (attempt {attempt+1}/{max_retries})")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1 * (attempt + 1))
                    continue

                if is_temp:
                    await client.close()

                record_llm_debug_entry(
                    label=label, model=model, url=url, is_public=is_public,
                    system_prompt=system_prompt, prompt=log_prompt,
                    response=content, thinking=last_thinking,
                    success=True, latency_ms=total_ms, attempts=attempt + 1,
                )
                return {
                    "success": True,
                    "data": {
                        "title": (result.get("title") or "").strip(),
                        "description": (result.get("description") or "").strip(),
                    },
                    "latency_ms": round(elapsed * 1000),
                    "raw_response": content,
                }

            except asyncio.TimeoutError:
                last_error = f"LLM-Timeout: Antwort nicht innerhalb von {timeout}s erhalten"
                logger.warning(f"{label} timeout (attempt {attempt+1}/{max_retries})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))

            except Exception as e:
                last_error = str(e)
                logger.error(f"{label} error: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))

        if is_temp:
            await client.close()

        record_llm_debug_entry(
            label=label, model=model, url=url, is_public=is_public,
            system_prompt=system_prompt, prompt=log_prompt,
            response=last_content, thinking=last_thinking,
            success=False, error=last_error, latency_ms=total_ms, attempts=max_retries,
        )
        return {
            "success": False,
            "error": last_error,
            "data": {"title": "", "description": ""},
            "latency_ms": 0,
            "raw_response": "",
        }

    _MEDIA_DESCRIBE_PROMPT = (
        "Beschreibe dieses Medium fuer eine Kurs-Medienbibliothek. "
        "Der Titel wird in Markdown-Referenzen verwendet, die "
        "Beschreibung hilft, das Medium korrekt in Skript, "
        "Slides oder Aufgaben einzubinden."
    )
    _MEDIA_DESCRIBE_JSON_RULE = (
        'Antworte NUR mit einem JSON-Objekt der Form {"title": "...", "description": "..."} '
        "ohne Code-Bloecke oder Erklaerungen."
    )

    async def describe_media_image(
        self,
        image_base64: str,
        mime_type: str = "image/png",
        config: Optional[dict] = None,
    ):
        """Erstellt per LLM einen Titel und eine kurze Beschreibung für ein Kurs-Medium.

        Nutzt die multimodalen Faehigkeiten des LLM, um das Bild zu analysieren.

        Returns JSON mit:
            title, description
        """
        system_prompt = (
            "Du bist ein Experte fuer die Beschreibung von Kurs-Medien (Abbildungen, Diagramme, "
            "Plots, Fotos) in Lehrmaterialien. Analysiere das Bild und erstelle "
            "(1) einen kurzen, aussagekraeftigen Titel (max. 8 Woerter, kein voelliger Satz) und "
            "(2) eine praezise Beschreibung (2-4 Saetze), die erklaert, was das Medium zeigt und "
            f"welchen Lehrinhalt es illustriert. {self._MEDIA_DESCRIBE_JSON_RULE}"
        )
        return await self._describe_media(
            media_part={"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_base64}"}},
            log_hint=f"[BILD: {mime_type}, {len(image_base64)} Zeichen Base64]",
            prompt_text=self._MEDIA_DESCRIBE_PROMPT,
            system_prompt=system_prompt,
            label="DESCRIBE_MEDIA_IMAGE",
            config=config,
        )

    async def describe_media_video(
        self,
        video_base64: str,
        config: Optional[dict] = None,
    ):
        """Erstellt per LLM einen Titel und eine kurze Beschreibung für ein Kurs-Video (mp4).

        Das LLM analysiert das Video (visuelle Abfolge + Tonspur, falls vorhanden).

        Returns JSON mit:
            title, description
        """
        system_prompt = (
            "Du bist ein Experte fuer die Beschreibung von Kurs-Medien (Videos) in Lehrmaterialien. "
            "Analysiere das VIDEO (visuelle Abfolge und Tonspur, falls vorhanden) und erstelle "
            "(1) einen kurzen, aussagekraeftigen Titel (max. 8 Woerter, kein voelliger Satz) und "
            "(2) eine praezise Beschreibung (2-4 Saetze), die erklaert, was das Video zeigt und "
            f"welchen Lehrinhalt es illustriert. {self._MEDIA_DESCRIBE_JSON_RULE}"
        )
        return await self._describe_media(
            media_part={"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{video_base64}"}},
            log_hint=f"[VIDEO: {len(video_base64)} Zeichen Base64]",
            prompt_text=self._MEDIA_DESCRIBE_PROMPT,
            system_prompt=system_prompt,
            label="DESCRIBE_MEDIA_VIDEO",
            config=config,
        )

    async def generate_course_report(
        self,
        course_name: str,
        tasks_data: str,
        students_data: str,
        config: Optional[dict] = None,
    ):
        """Generiert einen detaillierten Kurs-Performance-Report als Markdown.

        Analysiert die Daten aller gefilterten Aufgaben und Studierenden und erstellt
        einen strukturierten Report fuer Tutoren.

        Args:
            course_name: Name des Kurses
            tasks_data: Formatierter Text mit Aufgaben-Informationen (Titel, Typ, Punkte, etc.)
            students_data: Formatierter Text mit Studentendaten (Einreichungen, Feedback, Hinweise)
            config: Optionale LLM-Konfiguration pro Kurs

        Returns:
            Dict mit "success", "data"{"report": markdown_text}, "latency_ms", "raw_response"
        """
        prompt = self._render_prompt(
            COURSE_REPORT_PROMPT_TEMPLATE,
            course_name=course_name,
            tasks_data=tasks_data,
            students_data=students_data,
        )

        result = await self._call_plain(prompt, config=config)

        # Passe das Return-Format an, um "report" statt "model_solution" zu verwenden
        if result["success"]:
            result["data"] = {"report": result["data"]["model_solution"]}

        return result

    async def generate_student_report(
        self,
        course_name: str,
        tasks_data: str,
        student_data: str,
        config: Optional[dict] = None,
    ):
        """Generiert einen persönlichen Performance-Report für einen Studenten als Markdown.

        Analysiert die Daten der gefilterten Aufgaben des Studenten und erstellt
        einen strukturierten Report mit Tipps und Empfehlungen zur Verbesserung.

        Args:
            course_name: Name des Kurses
            tasks_data: Formatierter Text mit Aufgaben-Informationen (Titel, Typ, Punkte, etc.)
            student_data: Formatierter Text mit den persönlichen Daten des Studenten
                (Einreichungen, Feedback, Hinweise, Bearbeitungsdauer)
            config: Optionale LLM-Konfiguration pro Kurs

        Returns:
            Dict mit "success", "data"{"report": markdown_text}, "latency_ms", "raw_response"
        """
        prompt = self._render_prompt(
            STUDENT_REPORT_PROMPT_TEMPLATE,
            course_name=course_name,
            tasks_data=tasks_data,
            student_data=student_data,
        )

        result = await self._call_plain(prompt, config=config)

        # Passe das Return-Format an, um "report" statt "model_solution" zu verwenden
        if result["success"]:
            result["data"] = {"report": result["data"]["model_solution"]}

        return result

    async def answer_script_question(
        self,
        course_name: str,
        section_context: str,
        quote_context: str,
        chapter_index: str,
        question_history: str,
        student_question: str,
        config: Optional[dict] = None,
    ):
        """Beantwortet eine Studenten-Frage zum Skript (Freitext, kein JSON).

        section_context: Markdown-Inhalt des zitierten Kapitels (ggf. gekürzt)
            oder Hinweis, dass die Frage allgemein ist.
        quote_context: Textauswahl des Studenten oder Hinweis, dass keine da ist.
        chapter_index: Titel (+ Zusammenfassung) der sichtbaren Kapitel —
            für konsistente Querverweise (@fig:/@eq:).
        question_history: Die letzten Fragen des Studenten (Kontext).

        Returns:
            Dict mit "success", "data"{"text": markdown}, "latency_ms", "raw_response"
        """
        prompt = self._render_prompt(
            SCRIPT_QUESTION_PROMPT_TEMPLATE,
            course_name=course_name,
            section_context=section_context,
            quote_context=quote_context,
            chapter_index=chapter_index,
            question_history=question_history,
            student_question=student_question,
        )

        result = await self._call_plain(prompt, config=config)

        # Passe das Return-Format an: "text" statt "model_solution"
        if result["success"]:
            result["data"] = {"text": result["data"]["model_solution"]}

        return result

    async def maybe_answer_script_follow_up(
        self,
        course_name: str,
        section_context: str,
        quote_context: str,
        chapter_index: str,
        thread_text: str,
        author_name: str,
        author_role: str,
        new_message: str,
        config: Optional[dict] = None,
    ):
        """Prüft, ob eine neue menschliche Nachricht in einem Skript-Fragen-Thread
        die KI anspricht (Folgefrage/Hinterfragung), und beantwortet sie ggf.

        thread_text: chronologischer Verlauf (Frage + vorherige Antworten inkl.
            Namen/Rollen; die KI spricht dort als „AICampus (KI)“).
        author_name/author_role: Absender:in der neuen Nachricht.

        Returns:
            Dict mit "success", "data"{"respond": bool, "answer": markdown},
            "latency_ms", "raw_response" — "answer" ist leer, wenn respond=False.
        """
        prompt = self._render_prompt(
            SCRIPT_FOLLOW_UP_PROMPT_TEMPLATE,
            course_name=course_name,
            section_context=section_context,
            quote_context=quote_context,
            chapter_index=chapter_index,
            thread_text=thread_text,
            author_name=author_name,
            author_role=author_role,
            new_message=new_message,
        )

        result = await self._call_with_json(
            prompt, response_format={"type": "json_object"}, config=config
        )

        # Daten normalisieren (respond=bool, answer=str, ggf. leer)
        if result["success"] and isinstance(result.get("data"), dict):
            data = result["data"]
            respond_raw = data.get("respond", False)
            if isinstance(respond_raw, str):
                # Modell liefert teils "true"/"false" als String
                respond = respond_raw.strip().lower() in ("true", "1", "ja", "yes")
            else:
                respond = bool(respond_raw)
            answer = str(data.get("answer") or "").strip()
            result["data"] = {"respond": respond, "answer": answer if respond else ""}

        return result

    # ── Import (Kurs-Materialien aus Zip) ─────────────────────

    async def import_analyze_file(
        self,
        filename: str,
        line_range: str,
        chunk_text: str,
        config: Optional[dict] = None,
    ):
        """Struktur-Digest (JSON) für einen Text-Chunk: summary + headings."""
        prompt = self._render_prompt(
            FILE_SUMMARY_PROMPT_TEMPLATE,
            filename=filename,
            line_range=line_range,
            chunk_text=chunk_text,
        )
        return await self._call_with_json(
            prompt, response_format={"type": "json_object"}, config=config
        )

    async def import_script_planner_step(
        self,
        file_tree: str,
        digests: str,
        main_tex: str,
        previous_plan: str,
        steps: str,
        config: Optional[dict] = None,
    ):
        """Ein Schritt des agentic Skript-Kapitel-Planners (JSON-Action).

        previous_plan: frühere Vorschläge (Titel + Beschreibung) oder "(keine)";
        der neue Plan ERSETZT die alte Liste.
        """
        prompt = self._render_prompt(
            SCRIPT_PLANNER_PROMPT_TEMPLATE,
            file_tree=file_tree,
            digests=digests,
            main_tex=main_tex,
            previous_plan=previous_plan,
            steps=steps,
        )
        return await self._call_with_json(
            prompt, response_format={"type": "json_object"}, config=config,
            max_tokens=32768,
        )

    async def import_slides_planner_step(
        self,
        file_tree: str,
        digests: str,
        zip_decks: str,
        previous_plan: str,
        steps: str,
        config: Optional[dict] = None,
    ):
        """Ein Schritt des agentic Folien-Planners (JSON-Action).

        previous_plan: frühere Vorschläge (Titel + Beschreibung) oder "(keine)";
        der neue Plan ERSETZT die alte Liste.
        """
        prompt = self._render_prompt(
            SLIDES_PLANNER_PROMPT_TEMPLATE,
            file_tree=file_tree,
            digests=digests,
            zip_decks=zip_decks,
            previous_plan=previous_plan,
            steps=steps,
        )
        return await self._call_with_json(
            prompt, response_format={"type": "json_object"}, config=config,
            max_tokens=32768,
        )

    async def import_tasks_planner_step(
        self,
        file_tree: str,
        digests: str,
        previous_plan: str,
        steps: str,
        config: Optional[dict] = None,
    ):
        """Ein Schritt des agentic Aufgaben-Planners (JSON-Action).

        previous_plan: frühere Vorschläge (Titel + Beschreibung) oder "(keine)";
        der neue Plan ERSETZT die alte Liste.
        """
        prompt = self._render_prompt(
            TASKS_PLANNER_PROMPT_TEMPLATE,
            file_tree=file_tree,
            digests=digests,
            previous_plan=previous_plan,
            steps=steps,
        )
        return await self._call_with_json(
            prompt, response_format={"type": "json_object"}, config=config,
            max_tokens=32768,
        )

    async def import_generate_task(
        self,
        title: str,
        task_type: str,
        points: int,
        script_context: str,
        media: str,
        references: str,
        source_text: str,
        config: Optional[dict] = None,
        max_tokens: Optional[int] = None,
    ):
        """Wortgetreue Konvertierung EINER Übungsaufgabe (Text/MC) — 1 LLM-Call (JSON).

        script_context: Skript-Kapitel (Titel + Summary, inkl. Labels) für
        @fig:/@eq:-Querverweise; media/references: Kurs-Kontext für Einbindungen.
        """
        prompt = self._render_prompt(
            TASKS_GENERATE_PROMPT_TEMPLATE,
            title=title,
            task_type=task_type,
            points=points,
            script_context=script_context,
            media=media,
            references=references,
            source_text=source_text,
        )
        return await self._call_with_json(
            prompt, response_format={"type": "json_object"}, config=config,
            max_tokens=max_tokens,
        )

    async def import_gather_step(
        self,
        kind: str,
        kind_rules: str,
        title: str,
        description: str,
        file_tree: str,
        digests: str,
        media: str,
        references: str,
        steps: str,
        config: Optional[dict] = None,
    ):
        """Ein Schritt der agentic Quellen-Sammlung (JSON-Action)."""
        prompt = self._render_prompt(
            GATHER_PROMPT_TEMPLATE,
            kind=kind,
            kind_rules=kind_rules,
            title=title,
            description=description,
            file_tree=file_tree,
            digests=digests,
            media=media,
            references=references,
            steps=steps,
        )
        return await self._call_with_json(
            prompt, response_format={"type": "json_object"}, config=config
        )

    async def import_ref_extract_step(
        self,
        bib_text: str,
        known_refs: str,
        digests: str,
        steps: str,
        config: Optional[dict] = None,
    ):
        """Ein Schritt der agentic Quellen-Extraktion (JSON-Action)."""
        prompt = self._render_prompt(
            REF_EXTRACT_PROMPT_TEMPLATE,
            bib_text=bib_text,
            known_refs=known_refs,
            digests=digests,
            steps=steps,
        )
        return await self._call_with_json(
            prompt, response_format={"type": "json_object"}, config=config
        )

    async def import_convert_chapter(
        self,
        chapter_title: str,
        other_chapters: str,
        image_map: str,
        source_text: str,
        references: str = "",
        config: Optional[dict] = None,
        max_tokens: Optional[int] = None,
    ):
        """Wortgetreue Konvertierung eines Kapitels in Skript-Markdown (ein Call).

        references: Kurs-Quellenverzeichnis (Zitations-Keys) als Kontext.
        """
        prompt = self._render_prompt(
            CONVERT_CHAPTER_PROMPT_TEMPLATE,
            chapter_title=chapter_title,
            other_chapters=other_chapters,
            label_rules=LABEL_RULES_SCRIPT,
            image_map=image_map,
            references=references,
            script_format=SCRIPT_FORMAT_SPEC,
            source_text=source_text,
        )
        return await self._call_plain(prompt, config=config, max_tokens=max_tokens)

    async def import_chapter_summary(
        self,
        chapter_title: str,
        chapter_content: str,
        config: Optional[dict] = None,
    ):
        """Kurze interne Zusammenfassung eines generierten Skript-Kapitels."""
        prompt = self._render_prompt(
            CHAPTER_SUMMARY_PROMPT_TEMPLATE,
            chapter_title=chapter_title,
            chapter_content=chapter_content,
        )
        return await self._call_plain(prompt, config=config)

    async def import_describe_reference(
        self,
        entry_text: str,
        config: Optional[dict] = None,
    ):
        """Inhalts-/Kernpunkt-Beschreibung einer importierten Bibliographie-Quelle.

        Ziel: Das LLM im Kurs soll anhand der Beschreibung entscheiden können,
        WANN diese Quelle zitiert werden sollte.
        """
        prompt = (
            "Du pflegst die wissenschaftliche Quellenbibliothek eines Kurses. "
            "Gegeben ist eine Bibliographie-Quelle:\n\n"
            f"{entry_text}\n\n"
            "Erstelle eine kurze Beschreibung (1-3 Saetze) des Inhalts bzw. der Kernpunkte "
            "dieser Quelle, sodass ein LLM anhand der Beschreibung entscheiden kann, "
            "WANN diese Quelle zitiert werden sollte. Nutze dein Wissen ueber bekannte "
            "Werke; ist dir das Werk unbekannt, leite die Beschreibung konservativ aus "
            "Titel und Kontextfeldern ab und merke das in einem kurzen Zusatz an. "
            "Antworte NUR mit dem Beschreibungstext, ohne Anfuehrungszeichen oder Einleitung."
        )
        return await self._call_plain(prompt, config=config)

    async def import_generate_slide_deck(
        self,
        chapter_title: str,
        other_context: str,
        script_labels: str,
        image_map: str,
        references: str,
        source_slides: str,
        source: str,
        config: Optional[dict] = None,
        max_tokens: Optional[int] = None,
    ):
        """Slide-Deck für ein Kapitel (plain Text im Slide-Format).

        other_context: Summaries der andern Kapitel/Folien-Decks (Konsistenz/Querverweise).
        script_labels: Labels der Skript-Quell-Kapitel (gemeinsame Objekte wiederverwenden).
        image_map: importierte Medien (Original-Pfad → URL, mit Kurzbeschreibung).
        references: Kurs-Quellenverzeichnis (Zitations-Keys für @cite: etc.).
        max_tokens: optional höheres Output-Budget (z. B. 1:1-Decks mit vielen Folien).
        """
        prompt = self._render_prompt(
            SLIDE_DECK_PROMPT_TEMPLATE,
            chapter_title=chapter_title,
            other_context=other_context,
            label_rules=LABEL_RULES_SLIDES,
            script_labels=script_labels,
            image_map=image_map,
            references=references,
            source_slides=source_slides,
            slide_format=SLIDE_FORMAT_SPEC,
            source=source,
        )
        return await self._call_plain(prompt, config=config, max_tokens=max_tokens or SLIDES_MAX_TOKENS)

    async def import_deck_summary(
        self,
        deck_title: str,
        deck_content: str,
        config: Optional[dict] = None,
    ):
        """Kurze interne Zusammenfassung eines generierten Slide-Decks."""
        prompt = self._render_prompt(
            DECK_SUMMARY_PROMPT_TEMPLATE,
            deck_title=deck_title,
            deck_content=deck_content,
        )
        return await self._call_plain(prompt, config=config)

    async def import_transcribe_page_image(
        self,
        image_base64: str,
        mime_type: str,
        text_layer: str,
        page_no: int,
        page_count: int,
        pdf_images: list[dict],
        config: Optional[dict] = None,
    ):
        """M3: Vision-Transkription einer PDF-Seite (Markdown + Bild-Triage).

        pdf_images: M2-Liste [{idx, pfad, position, width_px, height_px}] —
        das LLM triagt jedes Bild (import/applet/ignore, M3b-Schema) im
        selben Call.
        Base64 wird nie geloggt (_call_with_json loggt nur den Prompt).
        """
        image_list = "\n".join(
            f"{i}. idx={im.get('idx')} · Lage: {im.get('position') or 'unbekannt'} · "
            f"{im.get('width_px') or '?'}×{im.get('height_px') or '?'} px"
            for i, im in enumerate(pdf_images or [], 1)
        ) or "(keine Bilder auf dieser Seite)"
        prompt = self._render_prompt(
            PAGE_TRANSCRIBE_PROMPT_TEMPLATE,
            page_no=page_no,
            page_count=page_count,
            text_layer=(text_layer or "(Seite hat keine Textschicht — vom Bild transkribieren)").strip(),
            image_list=image_list,
        )
        return await self._call_with_json(
            prompt,
            response_format={"type": "json_object"},
            config=config,
            max_tokens=8000,
            images=[f"data:{mime_type};base64,{image_base64}"],
        )

    async def import_triage_image(
        self,
        image_base64: str,
        mime_type: str,
        file_name: str,
        context: str,
        config: Optional[dict] = None,
    ):
        """M3b: Vision-Triage EINER eingebetteten Grafik (1 Call pro unikatem
        Bild). Liefert {"recommendation": import|applet|ignore, "title",
        "description", "reason"}.
        Bei "ignore" mit übernehmbarem Inhalt steckt der komplette Bildinhalt
        (Markdown/LaTeX/Mermaid) direkt in der "description".

        Base64 wird nie geloggt (_call_with_json loggt nur den Prompt).
        """
        prompt = self._render_prompt(
            IMAGE_TRIAGE_PROMPT_TEMPLATE,
            file_name=file_name or "Vorlesungsdatei",
            context=(context or "(kein Kontext verfügbar)").strip(),
        )
        return await self._call_with_json(
            prompt,
            response_format={"type": "json_object"},
            config=config,
            max_tokens=2000,
            images=[f"data:{mime_type};base64,{image_base64}"],
        )

    # ── Private call methods ───────────────────────────────────

    async def _call_plain(self, prompt: str, config: Optional[dict] = None, max_tokens: Optional[int] = None):
        """Einfacher LLM-Aufruf ohne JSON-Parser — gibt rohen Text zurück.

        Falls config uebergeben wird, wird ein temporares Client mit dieser Config
        verwendet (unterstuetzt global_settings / course_settings Resolver).
        max_tokens: optionale Obergrenze fuer die Antwortlaenge (sonst Modell-Default).
        """
        max_retries = 2
        last_error = None
        client = self._get_client(config)
        model = config.get("model", self.model) if config else self.model
        timeout = config.get("timeout", self.timeout) if config else self.timeout
        url, model, is_public = self._effective_endpoint(config)
        label = _debug_caller_label()
        system_prompt = "Du bist ein hilfsbereiter Tutor."
        total_ms = 0
        last_content = ""
        last_thinking = ""

        deadline = time.monotonic() + timeout

        for attempt in range(max_retries):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_error = f"LLM-Timeout: Antwort nicht innerhalb von {timeout}s erhalten"
                logger.warning(f"_call_plain total timeout")
                break

            try:
                start = time.time()

                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ]
                create_kwargs = {
                    "model": model,
                    "messages": messages,
                    "temperature": self.temperature,
                }
                if max_tokens is not None:
                    create_kwargs["max_tokens"] = max_tokens

                response = await asyncio.wait_for(
                    client.chat.completions.create(**create_kwargs),
                    timeout=remaining,
                )
                content = response.choices[0].message.content or ""
                last_thinking = self._extract_thinking(response)
                finish = response.choices[0].finish_reason

                # Output-Limit erreicht → an der Bruchstelle fortsetzen (max.
                # LLM_MAX_CONTINUATIONS Mal): ein Prompt, Output in Teilstücken.
                n_cont = 0
                while finish == "length" and n_cont < LLM_MAX_CONTINUATIONS:
                    n_cont += 1
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    cont_kwargs = {
                        "model": model,
                        "messages": messages + [
                            {"role": "assistant", "content": content or " "},
                            {"role": "user", "content": _CONTINUE_INSTRUCTION},
                        ],
                        "temperature": self.temperature,
                    }
                    if max_tokens is not None:
                        cont_kwargs["max_tokens"] = max_tokens
                    c_resp = await asyncio.wait_for(
                        client.chat.completions.create(**cont_kwargs), timeout=remaining
                    )
                    chunk = c_resp.choices[0].message.content or ""
                    # Overlap an der Naht: das Modell wiederholt teils den
                    # vorherigen Endabschnitt — Trimmen.
                    content += chunk[_overlap_len(content, chunk):]
                    finish = c_resp.choices[0].finish_reason
                    logger.info(f"_call_plain: Fortsetzung {n_cont} (+{len(chunk)} Zeichen)")

                total_ms += round((time.time() - start) * 1000)
                content = content or "(keine Antwort)"
                last_content = content

                if finish == "length":
                    last_error = (
                        f"LLM-Antwort nach {n_cont} Fortsetzungen noch abgeschnitten "
                        f"(Output-Limit des Modells erreicht)."
                    )
                    logger.warning("_call_plain: Antwort auch nach Fortsetzungen abgeschnitten")
                    break

                if config:
                    await client.close()

                record_llm_debug_entry(
                    label=label, model=model, url=url, is_public=is_public,
                    system_prompt=system_prompt, prompt=prompt,
                    response=content, thinking=last_thinking,
                    success=True, latency_ms=total_ms, attempts=attempt + 1,
                )
                return {
                    "success": True,
                    "data": {"model_solution": content.strip()},
                    "latency_ms": total_ms,
                    "raw_response": content,
                }

            except asyncio.TimeoutError:
                last_error = f"LLM-Timeout: Antwort nicht innerhalb von {timeout}s erhalten"
                logger.warning(f"_call_plain timeout (attempt {attempt+1}/{max_retries})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))

            except Exception as e:
                last_error = str(e)
                logger.error(f"_call_plain error: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))

        if config:
            await client.close()

        record_llm_debug_entry(
            label=label, model=model, url=url, is_public=is_public,
            system_prompt=system_prompt, prompt=prompt,
            response=last_content, thinking=last_thinking,
            success=False, error=last_error, latency_ms=total_ms, attempts=max_retries,
        )
        return {
            "success": False,
            "error": last_error,
            "data": {"model_solution": ""},
            "latency_ms": 0,
            "raw_response": "",
        }

    async def _call_with_json(self, prompt: str, response_format: Optional[dict] = None, config: Optional[dict] = None, max_tokens: Optional[int] = None, images: Optional[list[str]] = None):
        """
        Generischer LLM-Aufruf mit JSON-Response-Format.

        Falls config uebergeben wird, wird ein temporares Client mit dieser Config
        verwendet (unterstuetzt global_settings / course_settings Resolver).
        max_tokens: optionale Obergrenze für die Antwortlänge (z. B. für große
        HTML-Generierungen), sonst Modell-Default.
        images: optionale Data-URLs (z. B. Referenzbilder) — werden als
        image_url-Content-Parts vor dem Prompt-Text übergeben (multimodal).

        retry=2 bei Fehlern (Rate Limits, Timeouts).
        """
        max_retries = 2
        last_error = None
        client = self._get_client(config)
        model = config.get("model", self.model) if config else self.model
        timeout = config.get("timeout", self.timeout) if config else self.timeout
        url, model, is_public = self._effective_endpoint(config)
        label = _debug_caller_label()
        system_prompt = "Du bist ein hilfsbereiter Tutor. Antworte NUR mit gueltigem JSON."
        total_ms = 0
        last_content = ""
        last_thinking = ""

        if images:
            user_content: object = [
                {"type": "image_url", "image_url": {"url": url}} for url in images
            ] + [{"type": "text", "text": prompt}]
        else:
            user_content = prompt

        create_kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": self.temperature,
        }
        if response_format is not None:
            create_kwargs["response_format"] = response_format
        if max_tokens is not None:
            create_kwargs["max_tokens"] = max_tokens

        deadline = time.monotonic() + timeout

        for attempt in range(max_retries):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_error = f"LLM-Timeout: Antwort nicht innerhalb von {timeout}s erhalten"
                logger.warning(f"_call_with_json total timeout")
                break

            try:
                start = time.time()

                response = await asyncio.wait_for(
                    client.chat.completions.create(**create_kwargs),
                    timeout=remaining,
                )

                elapsed = time.time() - start
                total_ms += round(elapsed * 1000)

                content = response.choices[0].message.content
                last_content = content or ""
                last_thinking = self._extract_thinking(response)
                if content is None:
                    # Antwort wurde abgeschnitten (max_tokens erreicht)
                    last_error = "LLM-Antwort wurde abgeschnitten (max_tokens)."
                    continue

                try:
                    result = json.loads(content)#.replace('\\','\\\\'))
                except json.JSONDecodeError:
                    # Fallback: JSON aus freiem Text extrahieren
                    result = self._extract_json(content)
                    if result is None:
                        last_error = "LLM hat kein gueltiges JSON geliefert"
                        logger.warning(f"_call_with_json JSON extraction failed (attempt {attempt+1}/{max_retries})")
                        continue

                if config:
                    await client.close()

                record_llm_debug_entry(
                    label=label, model=model, url=url, is_public=is_public,
                    system_prompt=system_prompt, prompt=prompt,
                    response=content, thinking=last_thinking,
                    success=True, latency_ms=total_ms, attempts=attempt + 1,
                )
                return {
                    "success": True,
                    "data": result,
                    "latency_ms": round(elapsed * 1000),
                    "raw_response": content,
                }

            except asyncio.TimeoutError:
                last_error = f"LLM-Timeout: Antwort nicht innerhalb von {timeout}s erhalten"
                logger.warning(f"_call_with_json timeout (attempt {attempt+1}/{max_retries})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))

            except Exception as e:
                last_error = str(e)
                logger.error(f"_call_with_json error: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))

        if config:
            await client.close()

        record_llm_debug_entry(
            label=label, model=model, url=url, is_public=is_public,
            system_prompt=system_prompt, prompt=prompt,
            response=last_content, thinking=last_thinking,
            success=False, error=last_error, latency_ms=total_ms, attempts=max_retries,
        )
        return {
            "success": False,
            "error": last_error,
            "data": {},
            "latency_ms": 0,
            "raw_response": "",
        }

    def _public_config(self, config: Optional[dict]) -> Optional[dict]:
        """Config für nicht-sensitive Aufgaben (z. B. Task-/Musterlösung-Generierung).

        Nutzt den Public Endpoint (api_url_public), falls konfiguriert.
        API-Key und Modell fallen pro Feld auf die Private-Endpoint-Config
        zurück. Ohne api_url_public wird die Config unverändert zurückgegeben
        (gleicher Endpoint wie für sensitive Daten).
        """
        if not config:
            return config
        if not config.get("api_url_public"):
            return config
        public = dict(config)
        public["api_url"] = config["api_url_public"]
        public["api_key"] = config.get("api_key_public") or config.get("api_key") or self.api_key
        public["model"] = config.get("model_public") or config.get("model") or self.model
        return public

    def _get_client(self, config: Optional[dict] = None) -> AsyncOpenAI:
        """Gibt einen OpenAI-Client zurueck.

        Falls config uebergeben wird, wird ein temporares Client mit dieser Config
        erstellt. Sonst wird der persistente self.client verwendet.
        """
        if config:
            return AsyncOpenAI(
                base_url=config.get("api_url", self.api_url),
                api_key=config.get("api_key", self.api_key),
                timeout=config.get("timeout", self.timeout),
            )
        return self.client

    def _effective_endpoint(self, config: Optional[dict]) -> tuple[str, str, bool]:
        """Liefert (url, model, is_public) des effektiven Endpoints für einen Call.

        is_public ist True, wenn die effektive URL der konfigurierten
        api_url_public entspricht (z. B. nach _public_config()). Bei identischen
        URLs bleibt es privat (unklar).
        """
        url = (config.get("api_url") if config else None) or self.api_url
        model = (config.get("model") if config else None) or self.model
        is_public = False
        if config:
            public_url = config.get("api_url_public")
            if public_url:
                is_public = public_url.rstrip("/") == url.rstrip("/")
        return url, model, is_public

    @staticmethod
    def _extract_thinking(response) -> str:
        """Extrahiert optionales Reasoning/Thinking aus einer Chat-Completion-Antwort.

        Je nach Provider (z. B. Qwen3 via vLLM/Ollama) liegt das Reasoning in
        reasoning_content/reasoning — teils nur in model_extra.
        """
        try:
            message = response.choices[0].message
            for attr in ("reasoning_content", "reasoning"):
                value = getattr(message, attr, None)
                if isinstance(value, str) and value:
                    return value
            extra = getattr(message, "model_extra", None)
            if isinstance(extra, dict):
                for key in ("reasoning_content", "reasoning"):
                    value = extra.get(key)
                    if isinstance(value, str) and value:
                        return value
        except Exception:
            pass
        return ""

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        """Versucht, JSON aus freiem Text zu extrahieren.

        Prueft zuerst auf ```json-Codeblocks, dann auf { }-Muster.
        Gibt None zurueck, wenn kein gultiges JSON gefunden wurde.
        """
        # First try to extract from ```json ... ``` code blocks
        json_block = None
        cb_start = text.find("```json")
        if cb_start >= 0:
            cb_start = text.find("{", cb_start)
            cb_end = text.rfind("}")
            if cb_start >= 0 and cb_end > cb_start:
                json_block = text[cb_start:cb_end + 1]

        # Also try raw { } extraction
        raw_start = text.find("{")
        raw_end = text.rfind("}") + 1
        raw_block = None
        if raw_start >= 0 and raw_end > raw_start:
            raw_block = text[raw_start:raw_end]

        # Try code block first, then raw
        for block in (json_block, raw_block):
            if block:
                try:
                    return json.loads(block)
                except json.JSONDecodeError:
                    continue
        return None
