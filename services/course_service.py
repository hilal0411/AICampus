"""
Kurs-Löschung in zwei Stufen:

- `soft_delete_course`: setzt nur `deleted_at` (reversibel; Dateien bleiben).
  Genutzt von der Admin-API. Restore per `restore_course`.
- `hard_delete_course`: vollständige Kaskade aller abhängigen Daten inkl.
  Dateien. Wird NICHT aus dem UI aufgerufen — nur vom CLI-Purge
  (scripts/purge_trash.py).
"""

import logging
import shutil
from datetime import datetime

from sqlmodel import Session, select

from config import MEDIA_DIR, SUBMISSION_DIR, VERSION_SNAPSHOT_DIR
from database.models import (
    ContentVersion,
    Course,
    CourseInvite,
    CourseMaterial,
    CourseMedia,
    CourseReference,
    CourseRole,
    CourseSettings,
    CourseSlidesTheme,
    Feedback,
    ForumChannel,
    ForumChannelReadState,
    ForumMessage,
    HintExchange,
    ImageSpec,
    MaterialType,
    MediaUsage,
    McpEvent,
    McpOAuthGrantCourse,
    McpTokenCourse,
    Notification,
    PageView,
    ScriptQuestion,
    ScriptQuestionResponse,
    ScriptSection,
    Submission,
    Task,
    TaskWorkspaceFile,
    User,
    UserCourse,
)
from services import import_service
from services import media_service
from services import upload_service
from services import version_service
from services.workspace_service import workspace_service

logger = logging.getLogger(__name__)


def _delete_content_versions(
    session: Session,
    entity_type: str,
    entity_ids: list[int],
    snapshot_dirs: list,
) -> None:
    """Content-Versionen (DB) eines Typs löschen; Task-Dateisnapshot-Verzeichnisse
    für die Disc-Bereinigung nach dem Commit merken."""
    if not entity_ids:
        return
    for version in session.exec(
        select(ContentVersion).where(
            ContentVersion.entity_type == entity_type,
            ContentVersion.entity_id.in_(entity_ids),
        )
    ).all():
        if version.file_snapshot:
            snapshot_dirs.append(VERSION_SNAPSHOT_DIR / str(version.id))
        session.delete(version)


def soft_delete_course(session: Session, course: Course) -> str:
    """Kurs soft löschen (nur `deleted_at` setzen) — reversibel, keine
    Kaskade, keine Dateien. Der Kurs wird dadurch über den globalen
    Soft-Delete-Read-Filter unsichtbar; Restore = `restore_course`."""
    name = course.name
    course.deleted_at = datetime.now()
    session.add(course)
    session.commit()
    return name


def restore_course(session: Session, course: Course) -> None:
    """Soft gelöschten Kurs wiederherstellen (Kinder blieben erhalten)."""
    course.deleted_at = None
    session.add(course)
    session.commit()


def duplicate_course_content(
    session: Session,
    user: User,
    source_course: Course,
    name: str,
    description: str,
    semester: str,
) -> dict:
    """Kurs-Kopie anlegen (z. B. für das nächste Semester).

    Kopiert: Tasks (ohne Einreichungen/Feedback), Kurs-Settings,
    Skript-Kapitel, Slide-Decks, Quellen, Slides-Theme und Medien-Dateien.
    Kopiert NICHT: Mitglieder, Einreichungen, Forum. Medien-Referenzen im
    Markdown werden auf die neue Kurs-ID umgeschrieben.
    """
    # 1. Neuer Kurs + Aufrufer als PROF + Default-Forum-Kanal
    new_course = Course(
        name=name,
        description=description,
        semester=semester,
        created_by=user.id,  # type: ignore[arg-type]
    )
    session.add(new_course)
    session.commit()
    session.refresh(new_course)

    session.add(
        UserCourse(
            user_id=user.id,  # type: ignore[arg-type]
            course_id=new_course.id,  # type: ignore[arg-type]
            role_in_course=CourseRole.PROF,
        )
    )
    session.add(
        ForumChannel(
            course_id=new_course.id,  # type: ignore[arg-type]
            name="Allgemein",
            created_by=user.id,  # type: ignore[arg-type]
        )
    )
    session.commit()

    src_id = source_course.id
    dst_id: int = new_course.id  # type: ignore[assignment]
    old_prefix = f"/media/{src_id}/"
    new_prefix = f"/media/{dst_id}/"

    # 2. Tasks kopieren (ohne Submissions/Feedback)
    source_tasks = session.exec(select(Task).where(Task.course_id == src_id)).all()
    new_tasks: list[Task] = []
    for src_task in source_tasks:
        new_task = Task(
            course_id=dst_id,  # type: ignore[arg-type]
            title=src_task.title,
            task_type=src_task.task_type,
            description=src_task.description.replace(old_prefix, new_prefix),
            model_solution=src_task.model_solution.replace(old_prefix, new_prefix)
            if src_task.model_solution
            else None,
            max_points=src_task.max_points,
            max_attempts=src_task.max_attempts,
            deadline=src_task.deadline,
            text_template=src_task.text_template,
            is_visible=src_task.is_visible,
            hints_enabled=src_task.hints_enabled,
            auto_grading_enabled=src_task.auto_grading_enabled,
            display_order=src_task.display_order,
            created_by=user.id,  # type: ignore[arg-type]
        )
        session.add(new_task)
        new_tasks.append(new_task)
    session.commit()
    for t in new_tasks:
        version_service.mark_created(session, user, "task", t.id)  # type: ignore[arg-type]

    # 3. CourseSettings kopieren
    source_settings = session.exec(
        select(CourseSettings).where(CourseSettings.course_id == src_id)
    ).first()
    if source_settings:
        session.add(
            CourseSettings(
                course_id=dst_id,  # type: ignore[arg-type]
                llm_api_url=source_settings.llm_api_url,
                llm_model=source_settings.llm_model,
                grading_prompt=source_settings.grading_prompt,
                use_ldap=source_settings.use_ldap,
                ldap_server=source_settings.ldap_server,
                ldap_base_dn=source_settings.ldap_base_dn,
                ldap_bind_dn=source_settings.ldap_bind_dn,
                ldap_bind_pw=source_settings.ldap_bind_pw,
                ldap_user_search=source_settings.ldap_user_search,
                compute_enabled=source_settings.compute_enabled,
                compute_gpu_enabled=source_settings.compute_gpu_enabled,
                compute_agents=source_settings.compute_agents,
            )
        )
        session.commit()

    # 4. Skript-Kapitel kopieren
    source_sections = session.exec(
        select(ScriptSection).where(ScriptSection.course_id == src_id)
    ).all()
    new_sections: list[ScriptSection] = []
    for src_section in source_sections:
        s = ScriptSection(
            course_id=dst_id,  # type: ignore[arg-type]
            title=src_section.title,
            content=src_section.content.replace(old_prefix, new_prefix),
            is_visible=src_section.is_visible,
            display_order=src_section.display_order,
            summary=src_section.summary,
            created_by=user.id,  # type: ignore[arg-type]
        )
        session.add(s)
        new_sections.append(s)
    session.commit()
    for s in new_sections:
        version_service.mark_created(session, user, "script_section", s.id)  # type: ignore[arg-type]

    # 5. Materialien (Slide-Decks) kopieren
    source_materials = session.exec(
        select(CourseMaterial).where(CourseMaterial.course_id == src_id)
    ).all()
    new_materials: list[CourseMaterial] = []
    for src_material in source_materials:
        m = CourseMaterial(
            course_id=dst_id,  # type: ignore[arg-type]
            title=src_material.title,
            material_type=src_material.material_type,
            content=src_material.content.replace(old_prefix, new_prefix),
            is_visible=src_material.is_visible,
            display_order=src_material.display_order,
            summary=src_material.summary,
            created_by=user.id,  # type: ignore[arg-type]
        )
        session.add(m)
        new_materials.append(m)
    session.commit()
    for m in new_materials:
        if m.material_type == MaterialType.SLIDES:
            version_service.mark_created(session, user, "slide_deck", m.id)  # type: ignore[arg-type]

    # 6. Quellen-Referenzen + Slides-Theme kopieren
    source_references = session.exec(
        select(CourseReference).where(CourseReference.course_id == src_id)
    ).all()
    new_refs: list[CourseReference] = []
    for src_ref in source_references:
        r = CourseReference(
            course_id=dst_id,  # type: ignore[arg-type]
            key=src_ref.key,
            entry_type=src_ref.entry_type,
            authors=src_ref.authors,
            title=src_ref.title,
            year=src_ref.year,
            venue=src_ref.venue,
            detail=src_ref.detail,
            address=src_ref.address,
            doi=src_ref.doi,
            created_by=user.id,  # type: ignore[arg-type]
        )
        session.add(r)
        new_refs.append(r)
    source_theme = session.exec(
        select(CourseSlidesTheme).where(CourseSlidesTheme.course_id == src_id)
    ).first()
    if source_theme:
        session.add(
            CourseSlidesTheme(
                course_id=dst_id,  # type: ignore[arg-type]
                theme=source_theme.theme,
            )
        )
    session.commit()
    for r in new_refs:
        version_service.mark_created(session, user, "reference", r.id)  # type: ignore[arg-type]

    # 7. Medien kopieren (Dateien + DB-Einträge)
    source_media = session.exec(
        select(CourseMedia).where(CourseMedia.course_id == src_id)
    ).all()
    new_media: list[CourseMedia] = []
    for src_m in source_media:
        fname = src_m.file_path.rsplit("/", 1)[-1]
        src_path = MEDIA_DIR / src_m.file_path
        if src_path.is_file():
            dst_dir = MEDIA_DIR / f"course_{dst_id}"
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_dir / fname)
        new_m = CourseMedia(
            course_id=dst_id,  # type: ignore[arg-type]
            title=src_m.title,
            file_path=f"course_{dst_id}/{fname}",
            media_type=src_m.media_type,
            mime_type=src_m.mime_type,
            file_size=src_m.file_size,
            content_hash=src_m.content_hash,
            llm_description=src_m.llm_description,
            created_by=user.id,  # type: ignore[arg-type]
        )
        session.add(new_m)
        new_media.append(new_m)
    session.commit()

    # 8. Medien-Nutzung aus dem kopierten Markdown neu ableiten
    if new_media:
        media_service.sync_media_usages(session, dst_id)

    return {
        "course": new_course,
        "task_count": len(source_tasks),
        "section_count": len(source_sections),
        "material_count": len(source_materials),
        "media_count": len(new_media),
    }


def hard_delete_course(session: Session, course: Course) -> str:
    """Kurs endgültig löschen, inkl. aller abhängigen Daten (ein Commit, atomar).

    Manuelle Cascading deletes in korrekter Reihenfolge (Kinder zuerst).
    flush() nach jeder Gruppe, damit die Lösch-Reihenfolge garantiert ist.
    Dateien des Kurses (Medien, Submissions-, Versions-Snapshots) werden
    nach dem Commit best-effort entfernt.
    """
    course_id = course.id
    course_name = course.name
    snapshot_dirs: list = []  # VERSION_SNAPSHOT_DIR-Ordner (Task-Versionen)
    submission_dirs: list = []  # SUBMISSION_DIR-Ordner (Workspace-Einreichungen)

    # 1. Skript-Fragen: Antworten → Fragen → Kapitel
    questions = session.exec(
        select(ScriptQuestion).where(ScriptQuestion.course_id == course_id)
    ).all()
    for question in questions:
        responses = session.exec(
            select(ScriptQuestionResponse).where(
                ScriptQuestionResponse.question_id == question.id
            )
        ).all()
        for response in responses:
            session.delete(response)
        session.delete(question)
    session.flush()
    sections = session.exec(
        select(ScriptSection).where(ScriptSection.course_id == course_id)
    ).all()
    for section in sections:
        session.delete(section)
    _delete_content_versions(session, "script_section", [s.id for s in sections], snapshot_dirs)
    session.flush()

    # 2. Forum: Nachrichten → Read-States → Kanäle
    messages = session.exec(
        select(ForumMessage).where(ForumMessage.course_id == course_id)
    ).all()
    for message in messages:
        session.delete(message)
    session.flush()
    channels = session.exec(
        select(ForumChannel).where(ForumChannel.course_id == course_id)
    ).all()
    for channel in channels:
        read_states = session.exec(
            select(ForumChannelReadState).where(
                ForumChannelReadState.channel_id == channel.id
            )
        ).all()
        for read_state in read_states:
            session.delete(read_state)
        session.delete(channel)
    session.flush()

    # 3. Einladungslinks
    invites = session.exec(
        select(CourseInvite).where(CourseInvite.course_id == course_id)
    ).all()
    for invite in invites:
        session.delete(invite)
    session.flush()

    # 4. Medien: Usages → DB-Einträge (Dateien auf Disc nach dem Commit)
    media = session.exec(
        select(CourseMedia).where(CourseMedia.course_id == course_id)
    ).all()
    for medium in media:
        usages = session.exec(
            select(MediaUsage).where(MediaUsage.media_id == medium.id)
        ).all()
        for usage in usages:
            session.delete(usage)
        session.delete(medium)
    _delete_content_versions(session, "applet", [m.id for m in media], snapshot_dirs)
    session.flush()

    # 5. Materialien, Referenzen, Slides-Theme
    materials = session.exec(
        select(CourseMaterial).where(CourseMaterial.course_id == course_id)
    ).all()
    material_ids = [material.id for material in materials]
    for material in materials:
        session.delete(material)
    _delete_content_versions(session, "slide_deck", material_ids, snapshot_dirs)
    references = session.exec(
        select(CourseReference).where(CourseReference.course_id == course_id)
    ).all()
    for reference in references:
        session.delete(reference)
    _delete_content_versions(session, "reference", [r.id for r in references], snapshot_dirs)
    theme = session.exec(
        select(CourseSlidesTheme).where(CourseSlidesTheme.course_id == course_id)
    ).first()
    if theme:
        session.delete(theme)
    session.flush()

    # 6. Tasks: Feedbacks → Submissions → Hint-Dialoge → Tasks
    task_ids: list[int] = []
    tasks = session.exec(select(Task).where(Task.course_id == course_id)).all()
    for task in tasks:
        task_id = task.id
        task_ids.append(task_id)
        # Workspace-Zeilen können auch bei nicht-Workspace-Typen vorhanden
        # sein (z. B. per Typ-Wechsel verwaist) — dann ebenfalls aufräumen.
        task_has_workspace_rows = session.exec(
            select(TaskWorkspaceFile.id).where(TaskWorkspaceFile.task_id == task.id)
        ).first() is not None
        if task.task_type.value == "workspace" or task_has_workspace_rows:
            # Agent-Ressourcen + lokale Dateien + Workspace-DB-Rows
            # (NOT NULL-FKs ohne Relationship-Cascade)
            workspace_service.discard_workspace_content(session, task)
        if task.task_type.value == "upload":
            # Template-/Musterlösungs-Dateien der Aufgabe
            upload_service.cleanup_task(task_id)
        submissions = session.exec(
            select(Submission).where(Submission.task_id == task_id)
        ).all()
        for sub in submissions:
            feedbacks = session.exec(
                select(Feedback).where(Feedback.submission_id == sub.id)
            ).all()
            for fb in feedbacks:
                session.delete(fb)
            if sub.workspace_snapshot or sub.upload_file:
                submission_dirs.append(SUBMISSION_DIR / str(sub.id))
            session.delete(sub)
        hints = session.exec(
            select(HintExchange).where(HintExchange.task_id == task_id)
        ).all()
        for hint in hints:
            session.delete(hint)
        _delete_content_versions(session, "task", [task_id], snapshot_dirs)
        session.delete(task)
    # Media-Usages, die Tasks/Materialien (nicht Medien) referenzieren
    if task_ids:
        for usage in session.exec(
            select(MediaUsage).where(MediaUsage.task_id.in_(task_ids))
        ).all():
            session.delete(usage)
    if material_ids:
        for usage in session.exec(
            select(MediaUsage).where(MediaUsage.material_id.in_(material_ids))
        ).all():
            session.delete(usage)
    session.flush()

    # 7. Kurs-Settings + Mitgliedschaften
    settings = session.exec(
        select(CourseSettings).where(CourseSettings.course_id == course_id)
    ).first()
    if settings:
        session.delete(settings)
    memberships = session.exec(
        select(UserCourse).where(UserCourse.course_id == course_id)
    ).all()
    for mc in memberships:
        session.delete(mc)
    session.flush()

    # 8. Kurs-Import (DB-Zeile + Staging-Dateien, eigenes Session-Handling)
    import_service.delete_import(course_id)

    # 8b. MCP-Allowlisten + Tracking + Benachrichtigungen (Kurs-/Studierenden-Daten)
    for model in (McpEvent, McpTokenCourse, McpOAuthGrantCourse, Notification, PageView):
        for row in session.exec(select(model).where(model.course_id == course_id)).all():
            session.delete(row)
    for spec in session.exec(
        select(ImageSpec).where(ImageSpec.scope == str(course_id))
    ).all():
        session.delete(spec)
    session.flush()

    # 9. Schließlich den Kurs selbst
    session.delete(course)
    session.commit()

    # 10. Dateien des Kurses von Disc entfernen (best effort)
    shutil.rmtree(MEDIA_DIR / f"course_{course_id}", ignore_errors=True)
    for d in snapshot_dirs + submission_dirs:
        shutil.rmtree(d, ignore_errors=True)

    return course_name
