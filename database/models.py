"""
Datenbank-Modelle (SQLModel).

Alle Tabellen von AICampus. Jede Tabelle hat:
- Ein Table-Model (für die DB)
- Ein CreateSchema (für POST/PUT)
- Ein ReadSchema (für Responses)

Das hält die API sauber und typisiert.
"""

from datetime import datetime
from typing import Optional, List
from enum import Enum
from sqlalchemy import Column, Integer, JSON, UniqueConstraint
from sqlmodel import SQLModel, Field, Relationship


# ═══════════════════════════════════════════════════════════════════
# ENUMS
# ═══════════════════════════════════════════════════════════════════

class GlobalUserRole(str, Enum):
    ADMIN = "ADMIN"
    USER = "USER"


# ─── Import-Stage-Status (Kurs-Material-Import) ──────────────────
# pending      noch nicht gestartet
# running      Job läuft
# paused       vom User pausiert
# done         erfolgreich abgeschlossen
# skipped      bewusst übersprungen (User)
# error        Fehler (Details pro Einheit in chapter_plan/file_map/report)
# cancelled    vom User abgebrochen
# interrupted  App-Neustart während des Jobs (Runner startet stateless neu)
IMPORT_STAGE_STATUSES = (
    "pending", "running", "paused", "done", "skipped", "error", "cancelled", "interrupted",
)
IMPORT_STAGE_ACTIVE = ("running", "paused")
IMPORT_STAGE_ACTIVE_OR_INTERRUPTED = ("running", "paused", "interrupted")


class CourseRole(str, Enum):
    PROF = "PROF"
    TUTOR = "TUTOR"
    STUDENT = "STUDENT"


class TaskType(str, Enum):
    TEXT = "text"
    WORKSPACE = "workspace"
    MC = "mc"  # Multiple Choice (mehrere Fragen, sofortiges Feedback)
    UPLOAD = "upload"  # Datei-/Zip-Einreichung (Template + Musterlösungs-Datei)
    EXTERN = "extern"  # Externe Bewertung (keine Abgabe, kein LLM-Grading)



class SubmissionStatus(str, Enum):
    PENDING = "pending"
    GRADED = "graded"
    OVERRIDDEN = "overridden"


class FeedbackSource(str, Enum):
    LLM = "llm"
    HUMAN = "human"


# ═══════════════════════════════════════════════════════════════════
# USER
# ═══════════════════════════════════════════════════════════════════

class UserBase(SQLModel):
    username: str = Field(unique=True, index=True, max_length=100)
    email: Optional[str] = Field(default=None, max_length=200)  # NULL bei anonymisierten (soft-gelöschten) Accounts
    name: str = Field(max_length=200)
    role: GlobalUserRole = GlobalUserRole.USER    # Global: ADMIN oder USER
    password_hash: Optional[str] = Field(default=None)   # NULL bei LDAP-Users
    ldap_dn: Optional[str] = Field(default=None)         # Distinguished Name
    avatar: Optional[str] = Field(default=None, max_length=255)  # relativer Pfad: avatars/<uuid>.<ext>
    profile: Optional[str] = Field(default=None)  # Steckbrief (Markdown), öffentlich unter /u/<username>


class User(UserBase, table=True):
    __tablename__ = "users"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    deleted_at: Optional[datetime] = Field(default=None)  # Soft Delete (null = aktiv)
    
    # Relationships (mit back_populates, um SQLAlchemy-Warnings zu vermeiden)
    user_courses: List["UserCourse"] = Relationship(back_populates="user")
    created_tasks: List["Task"] = Relationship(back_populates="creator")
    submissions: List["Submission"] = Relationship(back_populates="student")
    feedback_given: List["Feedback"] = Relationship(back_populates="giver")


class UserCreate(UserBase):
    plain_password: str = Field(min_length=6, max_length=128)


class UserRead(UserBase):
    id: int
    username: str


class UserInDB(UserBase):
    id: int
    password_hash: Optional[str]


# ═══════════════════════════════════════════════════════════════════
# COURSE
# ═══════════════════════════════════════════════════════════════════

class CourseBase(SQLModel):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    semester: str = Field(max_length=50)  # z.B. "WS 2025/26"
    toc_visible: bool = Field(default=True)  # Inhaltsverzeichnis für Studenten sichtbar


class Course(CourseBase, table=True):
    __tablename__ = "courses"
    
    # AUTOINCREMENT: verhindert die Wiederverwendung gelöschter Kurs-IDs.
    # Sonst erbt ein neuer Kurs mit wiederverwendeter ID alle verwaisten
    # Restdaten des gelöschten Kurses (s. _migrate_courses_autoincrement).
    id: Optional[int] = Field(
        default=None,
        sa_column=Column(Integer, primary_key=True, autoincrement=True),
    )
    created_by: int = Field(foreign_key="users.id")
    created_at: datetime = Field(default_factory=datetime.now)
    deleted_at: Optional[datetime] = Field(default=None)  # Soft Delete (null = aktiv)
    
    # Relationships
    course_members: List["UserCourse"] = Relationship(back_populates="course")
    tasks: List["Task"] = Relationship(back_populates="course")
    materials: List["CourseMaterial"] = Relationship(back_populates="course")
    script_sections: List["ScriptSection"] = Relationship(back_populates="course")
    media: List["CourseMedia"] = Relationship(back_populates="course")
    references: List["CourseReference"] = Relationship(back_populates="course")
    settings: Optional["CourseSettings"] = Relationship(back_populates="course")
    channels: List["ForumChannel"] = Relationship(back_populates="course")


class CourseCreate(CourseBase):
    pass


class CourseRead(CourseBase):
    id: int
    created_by: int
    created_at: datetime


# ═══════════════════════════════════════════════════════════════════
# USER-COURSE (M:N mit Rolle im Kurs)
# ═══════════════════════════════════════════════════════════════════

class UserCourseBase(SQLModel):
    user_id: int = Field(foreign_key="users.id")
    course_id: int = Field(foreign_key="courses.id")
    role_in_course: CourseRole


class UserCourse(UserCourseBase, table=True):
    __tablename__ = "user_courses"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    
    # Freie Anmerkung der PROF/Tutors je Mitglied (z.B. "Übungsgruppe 2").
    # In der Mitgliederübersicht editierbar, in der Punkteübersicht als
    # Annotations-Filter nutzbar.
    annotation: Optional[str] = Field(default=None)

    user: User = Relationship(back_populates="user_courses")
    course: Course = Relationship(back_populates="course_members")


class UserCourseCreate(SQLModel):
    user_ids: List[int] = Field(default=[])   # IDs der User hinzuzufügen
    role_in_course: CourseRole


class UserCourseRead(SQLModel):
    id: int
    user_id: int
    course_id: int
    role_in_course: CourseRole
    annotation: Optional[str] = None
    user: Optional[UserRead] = None


# ═══════════════════════════════════════════════════════════════════
# TASK (Aufgabe)
# ═══════════════════════════════════════════════════════════════════

class TaskBase(SQLModel):
    course_id: int = Field(foreign_key="courses.id")
    title: str = Field(max_length=300)     # z.B. "Blatt3-01: Rekursion"
    task_type: TaskType
    description: str                        # Aufgabenstellung
    model_solution: Optional[str] = Field(default=None)  # Musterlösung (optional, versteckt für Studenten)
    max_points: int = Field(ge=0)
    max_attempts: Optional[int] = Field(default=None)  # NULL = unlimitiert
    deadline: Optional[str] = Field(default=None)      # ISO-Format: "2025-02-15T23:59"
    text_template: Optional[str] = Field(default=None) # Für Text-Aufgaben: Antwort-Gerüst (Lückentext, Tabelle, …) als Markdown
    is_visible: bool = Field(default=False)            # Für Studenten sichtbar (Default: versteckt)
    display_order: int = Field(default=0)              # Anzeigereihenfolge im Kurs
    hints_enabled: bool = Field(default=True)          # Socratic-Hints fuer Studenten
    auto_grading_enabled: bool = Field(default=True)   # Automatische LLM-Korrektur bei Einreichung (Default: AN; MC wird immer deterministisch bewertet)
    # Workspace-Aufgaben (task_type=workspace):
    # Skript-basiertes Modell: Run/Tests/Init laufen über Task-Dateien
    # (run.sh, test.sh, .test_private.sh, .init.sh, .init_hidden.sh, .test_solution.sh);
    # die Umgebung wird hier per einfachen Feldern konfiguriert.
    workspace_timeout: int = Field(default=900)                 # Run-Timeout (s, 1–7200)
    workspace_cpu: float = Field(default=2.0)                   # CPU-Limit des Student-Containers
    workspace_memory: str = Field(default="4g", max_length=10)  # Memory-Limit („512m“, „4g“, …)
    workspace_disk_quota: float = Field(default=1.0)            # Disk-Quota des Student-Volumes in GB (0 = ohne Limit)
    workspace_internet: bool = Field(default=False)             # Internet für Studenten-Läufe (Build hat immer)
    workspace_main_file: Optional[str] = Field(default=None, max_length=200)  # Editor-Hauptdatei (relativ)
    workspace_assets_status: Optional[str] = Field(default=None)  # JSON je Agent: assets + task_image-Status
    # Compute-Engines + Image-Specs:
    workspace_engines: Optional[str] = Field(default=None)      # JSON-Liste Engine-Namen (geordnet) — Pool für Routing/Prebuild
    workspace_image: Optional[str] = Field(default=None, max_length=50)  # Image-Spec-Name (Kurs-Scope > global)
    workspace_preview_root_ports: Optional[str] = Field(default=None)  # VERWORFEN (Subdomain-Preview) — Spalte bleibt, ungenutzt
    # Multiple-Choice-Aufgaben (task_type=mc):
    # JSON: {"penalty_enabled": bool, "questions": [{"question": str, "points": int,
    #   "multi_select": bool, "options": [{"text": str, "correct": bool, "feedback": str}]}]}
    # max_points = Summe der Fragen-Punkte (serverseitig synchron gehalten).
    mc_data: Optional[str] = Field(default=None)
    # Upload-Aufgaben (task_type=upload): Original-Dateinamen; Disk-Pfad ist
    # deterministisch data/upload_tasks/{task_id}/<name> (Datei wie hochgeladen).
    upload_template: Optional[str] = Field(default=None, max_length=500)      # Template-Datei (Download für Studenten)
    upload_solution_file: Optional[str] = Field(default=None, max_length=500)  # Musterlösungs-Datei (versteckt, LLM-Grading)


class Task(TaskBase, table=True):
    __tablename__ = "tasks"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    created_by: int = Field(foreign_key="users.id")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    deleted_at: Optional[datetime] = Field(default=None)  # Soft Delete (null = aktiv)
    
    # Relationships
    course: Course = Relationship(back_populates="tasks")
    creator: User = Relationship(back_populates="created_tasks")
    submissions: List["Submission"] = Relationship(back_populates="task")
    hint_exchanges: List["HintExchange"] = Relationship(back_populates="task")
    workspace_files: List["TaskWorkspaceFile"] = Relationship(back_populates="task")


class TaskDraft(SQLModel, table=True):
    """Draft (Autosave) einer Text-Aufgabe pro Student: ungespeicherter
    Zwischenstand im Editor, unabhängig von Submissions (kein Versuch —
    zählt nicht in max_attempts, taucht nirgends in History/Tutor-View
    auf). Analog zum Workspace-Autosave (dort: per-Student-Volume auf
    der Compute-Engine, hier: eine Row pro (task, user)).
    Neue Tabelle wird beim App-Start per create_all angelegt."""
    __tablename__ = "task_drafts"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="tasks.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    solution: str = Field(default="")
    updated_at: datetime = Field(default_factory=datetime.now)

    __table_args__ = (UniqueConstraint("task_id", "user_id", name="uq_draft_task_user"),)


class TaskCreate(SQLModel):
    course_id: int
    title: str
    task_type: TaskType
    description: str
    model_solution: Optional[str] = Field(default=None)
    max_points: int = Field(ge=0)
    max_attempts: Optional[int] = Field(default=None)
    deadline: Optional[str] = Field(default=None)
    text_template: Optional[str] = Field(default=None)
    is_visible: bool = Field(default=False)
    display_order: int = Field(default=0)
    hints_enabled: bool = Field(default=True)
    auto_grading_enabled: bool = Field(default=True)
    # Workspace-Aufgaben (s. TaskBase)
    workspace_timeout: int = Field(default=900)
    workspace_cpu: float = Field(default=2.0)
    workspace_memory: str = Field(default="4g", max_length=10)
    workspace_disk_quota: float = Field(default=1.0)
    workspace_internet: bool = Field(default=False)
    workspace_main_file: Optional[str] = Field(default=None, max_length=200)
    workspace_assets_status: Optional[str] = Field(default=None)
    workspace_engines: Optional[str] = Field(default=None)
    workspace_image: Optional[str] = Field(default=None, max_length=50)
    mc_data: Optional[str] = Field(default=None)
    # Upload-Aufgaben (s. TaskBase)
    upload_template: Optional[str] = Field(default=None, max_length=500)
    upload_solution_file: Optional[str] = Field(default=None, max_length=500)


class TaskRead(TaskBase):
    id: int
    created_by: int
    created_at: datetime
    updated_at: datetime


class TaskUpdate(SQLModel):
    """Partial update — alle Felder optional"""
    title: Optional[str] = None
    task_type: Optional[TaskType] = None
    description: Optional[str] = None
    model_solution: Optional[str] = None
    max_points: Optional[int] = None
    max_attempts: Optional[int] = None
    deadline: Optional[str] = None
    text_template: Optional[str] = None
    is_visible: Optional[bool] = None
    display_order: Optional[int] = None
    hints_enabled: Optional[bool] = None
    # Workspace-Aufgaben (s. TaskBase)
    workspace_timeout: Optional[int] = None
    workspace_cpu: Optional[float] = None
    workspace_memory: Optional[str] = None
    workspace_disk_quota: Optional[float] = None
    workspace_internet: Optional[bool] = None
    workspace_main_file: Optional[str] = None
    # Upload-Aufgaben (s. TaskBase)
    upload_template: Optional[str] = None
    upload_solution_file: Optional[str] = None
    workspace_assets_status: Optional[str] = None
    workspace_engines: Optional[str] = None
    workspace_image: Optional[str] = None
    mc_data: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════
# IMAGE SPEC ("Rezept" für Workspace-Images)
# ═══════════════════════════════════════════════════════════════════

class ImageSpec(SQLModel, table=True):
    """Image-Spec: reines Dockerfile (Name als eigene Spalte) zum Bauen
    eines Workspace-Images auf den Compute-Engines. (scope, name) ist
    eindeutig; dockerfile ist die Single Source of Truth.
    GPU-Regeln gehören zur Compute-Engine (compute_agents-JSON, Key gpus)."""
    __tablename__ = "image_specs"

    id: Optional[int] = Field(default=None, primary_key=True)
    scope: str = Field(max_length=50, index=True)     # "global" | str(course_id)
    name: str = Field(max_length=50, index=True)      # slug (z.B. "python-ml")
    dockerfile: str = Field(default="")
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    __table_args__ = (UniqueConstraint("scope", "name", name="uq_image_spec_scope_name"),)


class ImageSpecRead(SQLModel):
    id: int
    scope: str
    name: str
    dockerfile: str
    created_at: datetime
    updated_at: datetime


# ═══════════════════════════════════════════════════════════════════
# TASK WORKSPACE FILE (Dateien einer Workspace-Aufgabe)
# ═══════════════════════════════════════════════════════════════════

class TaskWorkspaceFile(SQLModel, table=True):
    """Metadaten einer Workspace-Datei. Der Inhalt liegt auf Disk:
    data/workspaces/{task_id}/<path>

    # Zugriffsklassen (s. docs/user-guide.md §3.3):
    `access` ist die EXPLIZITE Klasse der Datei (NULL = erben von den
    Ordner-Vorfahren). Effektive Klasse = restriktivste von (eigene,
    alle Ordner-Vorfahren): hidden > readonly > edit.
    """
    __tablename__ = "task_workspace_files"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="tasks.id", index=True)
    path: str = Field(max_length=500)        # relativer Pfad (z.B. main.py, data/mnist.csv)
    size: int = Field(default=0)
    checksum: Optional[str] = Field(default=None, max_length=64)  # SHA256 des Inhalts
    access: Optional[str] = Field(default=None, max_length=16)    # NULL=erben; "readonly"|"hidden"
    is_binary: bool = Field(default=False)   # Dataset/Binary vs. Textdatei
    sort_order: Optional[int] = Field(default=None)  # manuelle Reihenfolge je Ordner
    updated_at: datetime = Field(default_factory=datetime.now)

    task: Optional[Task] = Relationship(back_populates="workspace_files")


class TaskWorkspaceFolder(SQLModel, table=True):
    """Expliziter Workspace-Ordner (Zugriffsklasse wirkt per Erbung auf
    alle Dateien/Unterordner darunter).

    Eine Zeile = der Ordner ist explizit (persistiert, auch wenn er leer
    bleibt). ``access=None`` (NULL) = explizit „edit“; "readonly"/
    "hidden" setzen eine restriktivere Klasse. Ohne Zeile erbt der
    Ordner nur von seinen Vorfahren.
    """
    __tablename__ = "task_workspace_folders"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="tasks.id", index=True)
    path: str = Field(max_length=500)                 # relativer Ordnerpfad (ohne Slash am Ende)
    access: Optional[str] = Field(default=None, max_length=16)  # NULL=edit | "readonly" | "hidden"
    updated_at: datetime = Field(default_factory=datetime.now)

    __table_args__ = (UniqueConstraint("task_id", "path", name="uq_ws_folder_task_path"),)


class TaskWorkspaceOrder(SQLModel, table=True):
    """Anzeige-Reihenfolge je Ordner für Ordner und [init]-Artefakte.

    Dateien behalten ``task_workspace_files.sort_order``; Ordner (egal ob
    mit Zugriffs-Klasse) und [init]-Artefakte (virtuell, keine Datei-Zeile)
    liegen hier. Rein kosmetisch — wirkt weder auf Disk/Sync noch auf das
    Grading.
    """
    __tablename__ = "task_workspace_orders"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="tasks.id", index=True)
    path: str = Field(max_length=500)        # relativer Pfad (Ordner oder [init]-Datei)
    sort_order: int = Field(default=0)

    __table_args__ = (UniqueConstraint("task_id", "path", name="uq_ws_order_task_path"),)


class TaskWorkspaceFileRead(SQLModel):
    id: int
    task_id: int
    path: str
    size: int
    checksum: Optional[str] = None
    access: Optional[str] = None
    is_binary: bool
    updated_at: datetime


# ═══════════════════════════════════════════════════════════════════
# COURSE MATERIAL (Vorlesungsskript & Slides)
# ═══════════════════════════════════════════════════════════════════

class MaterialType(str, Enum):
    SCRIPT = "script"
    SLIDES = "slides"


class CourseMaterialBase(SQLModel):
    course_id: int = Field(foreign_key="courses.id", index=True)
    title: str = Field(max_length=300)
    material_type: MaterialType
    content: str = Field(default="")              # Markdown (Slides: Folien mit `---` getrennt)
    is_visible: bool = Field(default=True)        # Für Studenten sichtbar
    display_order: int = Field(default=0)         # Reihenfolge (wichtig für mehrere Slide-Decks)
    summary: str = Field(default="")              # Interne LLM-Zusammenfassung (NICHT für Studenten; Konsistenz zwischen Decks)


class CourseMaterial(CourseMaterialBase, table=True):
    __tablename__ = "course_materials"

    id: Optional[int] = Field(default=None, primary_key=True)
    created_by: int = Field(foreign_key="users.id")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    deleted_at: Optional[datetime] = Field(default=None)  # Soft Delete (null = aktiv)

    # Relationships
    course: Course = Relationship(back_populates="materials")


class CourseMaterialCreate(SQLModel):
    title: str
    material_type: MaterialType
    content: str = ""
    is_visible: bool = True


class CourseMaterialRead(CourseMaterialBase):
    id: int
    created_by: int
    created_at: datetime
    updated_at: datetime


class CourseSlidesTheme(SQLModel, table=True):
    """Design (Theme) für die Slide-Decks eines Kurses (eine Zeile pro Kurs)."""
    __tablename__ = "course_slides_theme"

    course_id: int = Field(foreign_key="courses.id", primary_key=True)
    theme: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    updated_at: datetime = Field(default_factory=datetime.now)


# ═══════════════════════════════════════════════════════════════════
# SCRIPT SECTIONS (Vorlesungsskript als mehrere Markdown-Kapitel)
# ═══════════════════════════════════════════════════════════════════

class ScriptSectionBase(SQLModel):
    course_id: int = Field(foreign_key="courses.id", index=True)
    title: str = Field(max_length=300)         # z.B. "Rekursion" (Nummer wird automatisch angezeigt)
    content: str = Field(default="")           # Markdown (mit LaTeX/Mermaid); Kapitel-Label = {#sec:label} als eigene Zeile am Anfang
    is_visible: bool = Field(default=False)    # für Studenten freigeschaltet
    display_order: int = Field(default=0)      # Reihenfolge im Skript
    summary: str = Field(default="")           # Interne LLM-Zusammenfassung (NICHT für Studenten; Konsistenz zwischen Kapiteln)


class ScriptSection(ScriptSectionBase, table=True):
    __tablename__ = "course_script_sections"

    id: Optional[int] = Field(default=None, primary_key=True)
    created_by: int = Field(foreign_key="users.id")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    deleted_at: Optional[datetime] = Field(default=None)  # Soft Delete (null = aktiv)

    # Relationships
    course: Course = Relationship(back_populates="script_sections")


# ═══════════════════════════════════════════════════════════════════
# CONTENT VERSIONS (Versions-History für bearbeitbare Elemente)
# ═══════════════════════════════════════════════════════════════════
# Generisch über (entity_type, entity_id), damit dieselbe History-Logik für
# Skript-Kapitel, Slide-Decks und (später) Applets/Aufgaben wiederverwendet
# werden kann. `snapshot` ist ein JSON-Dict mit den bearbeitbaren Feldern
# (aktuell: {title, content, summary}); je Entity-Typ entscheidet der
# Adapter (services/version_service.py), welche Felder auf das Element
# angewendet werden.
class ContentVersion(SQLModel, table=True):
    __tablename__ = "content_versions"

    id: Optional[int] = Field(default=None, primary_key=True)
    entity_type: str = Field(max_length=50, index=True)   # z.B. "script_section", "slide_deck"
    entity_id: int = Field(index=True)
    # Versionen-Name; leer = Default (Zeitraffer letzter Bearbeitung, updated_at)
    name: str = Field(default="", max_length=200)
    # Snapshot der bearbeitbaren Felder im Zeitpunkt des letzten Autosave
    snapshot: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    # Optionaler Snapshot des Workspace-Dateibaus (nur Task-Versionen mit
    # Aufgabentyp Workspace): relativer Pfad
    # version_snapshots/{version_id}/workspace.tar.gz (+ meta.json daneben)
    file_snapshot: Optional[str] = Field(default=None, max_length=300)
    created_by: int = Field(foreign_key="users.id")
    # Anlegt = Start der Bearbeitungs-Session; updated_at = letzter Autosave
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


# ═══════════════════════════════════════════════════════════════════
# COURSE MEDIA (Bilder/Applets je Kurs)
# ═══════════════════════════════════════════════════════════════════

class CourseMediaBase(SQLModel):
    course_id: int = Field(foreign_key="courses.id", index=True)
    title: str = Field(max_length=300)
    file_path: str = Field(max_length=500, unique=True)  # relativ zu MEDIA_DIR: course_1/<uuid>.png
    media_type: str = Field(default="image", max_length=50)  # image (später: applet, figure)
    mime_type: str = Field(default="image/png", max_length=100)
    file_size: int = Field(default=0)
    content_hash: Optional[str] = Field(default=None, max_length=64, index=True)  # SHA256 der Quelldatei (Medien-Import-Dedup)
    llm_description: Optional[str] = Field(default=None, max_length=2000)  # Was zeigt das Medium? (für LLM-Pipeline)


class CourseMedia(CourseMediaBase, table=True):
    __tablename__ = "course_media"

    id: Optional[int] = Field(default=None, primary_key=True)
    created_by: int = Field(foreign_key="users.id")
    created_at: datetime = Field(default_factory=datetime.now)
    deleted_at: Optional[datetime] = Field(default=None)  # Soft Delete (null = aktiv)

    # Relationships
    course: Course = Relationship(back_populates="media")
    usages: List["MediaUsage"] = Relationship(back_populates="media")


class CourseMediaRead(CourseMediaBase):
    id: int
    created_by: int
    created_at: datetime


# ═══════════════════════════════════════════════════════════════════
# COURSE REFERENCE (Quellen-Bibliothek, BibTeX-artig, PROF/TUTOR/Admin)
# ═══════════════════════════════════════════════════════════════════

class CourseReferenceBase(SQLModel):
    course_id: int = Field(foreign_key="courses.id", index=True)
    key: str = Field(max_length=100)            # BibTeX-Schlüssel (einzig pro Kurs; @cite:{key})
    entry_type: str = Field(default="misc", max_length=50)   # article/book/inproceedings/...
    authors: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    title: str = Field(default="", max_length=500)
    year: str = Field(default="", max_length=10)
    venue: str = Field(default="", max_length=300)   # Zeitschrift / Konferenz / Verlag
    detail: str = Field(default="", max_length=200)   # Band(Heft), Seiten, Edition ...
    address: str = Field(default="", max_length=200)
    doi: str = Field(default="", max_length=200)
    url: str = Field(default="", max_length=500)      # Link zur Quelle (Nachschlagen)
    note: str = Field(default="", max_length=500)
    description: str = Field(default="")   # Inhalt/Kernpunkte — wann soll die Quelle zitiert werden?
    display_order: int = Field(default=0)  # Reihenfolge = globale Zitations-Nummer (stabil pro Kurs)


class CourseReference(CourseReferenceBase, table=True):
    __tablename__ = "course_references"

    id: Optional[int] = Field(default=None, primary_key=True)
    created_by: int = Field(foreign_key="users.id")
    created_at: datetime = Field(default_factory=datetime.now)

    # Relationships
    course: Course = Relationship(back_populates="references")


class CourseReferenceRead(CourseReferenceBase):
    id: int
    created_by: int
    created_at: datetime


# ═══════════════════════════════════════════════════════════════════
# MEDIA USAGE (wo ein Medium eingebunden ist — abgeleitet aus Markdown)
# ═══════════════════════════════════════════════════════════════════

class MediaUsage(SQLModel, table=True):
    """Ableitung aus dem Markdown-Inhalt (Single Source of Truth = Task/Material).

    Wird von services.media_service.sync_media_usages() bei jeder
    Änderung von Aufgaben/Materialien/Medien neu aufgebaut.
    """
    __tablename__ = "media_usages"

    id: Optional[int] = Field(default=None, primary_key=True)
    media_id: int = Field(foreign_key="course_media.id", index=True)
    task_id: Optional[int] = Field(default=None, foreign_key="tasks.id", index=True)
    material_id: Optional[int] = Field(default=None, foreign_key="course_materials.id", index=True)
    location: str = Field(default="", max_length=500)  # z.B. "Aufgabe: Blatt3-01" / "Skript: Kapitel 2"

    # Relationships
    media: CourseMedia = Relationship(back_populates="usages")


# ═══════════════════════════════════════════════════════════════════
# HINT EXCHANGE (Socratic Hint Dialog)
# ═══════════════════════════════════════════════════════════════════

class HintExchangeBase(SQLModel):
    task_id: int = Field(foreign_key="tasks.id")
    student_id: int = Field(foreign_key="users.id")
    question: str
    llm_response: str = Field(default="")
    current_solution: str = Field(default="")      # Current content in the editor at time of request


class HintExchange(HintExchangeBase, table=True):
    __tablename__ = "hint_exchanges"

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    response_at: Optional[datetime] = None          # Timestamp when LLM responded

    # Relationships
    task: Task = Relationship(back_populates="hint_exchanges")


class HintExchangeCreate(SQLModel):
    task_id: int
    question: str
    current_solution: str = Field(default="")


class HintExchangeRead(SQLModel):
    id: int
    task_id: int
    student_id: int
    question: str
    llm_response: str
    current_solution: str
    created_at: datetime
    response_at: Optional[datetime] = None


# ═══════════════════════════════════════════════════════════════════
# SUBMISSION (Einreichung)
# ═══════════════════════════════════════════════════════════════════

class SubmissionBase(SQLModel):
    task_id: int = Field(foreign_key="tasks.id")
    student_id: int = Field(foreign_key="users.id")
    solution: str = Field(default="")          # Für Text-Aufgaben
    mc_answers: str = Field(default="")        # MC-Antworten (mc:v1:<json>-Payload)
    attempt_number: int = Field(default=1)
    solve_time_seconds: float = Field(default=0.0)  # Zeit in Sekunden bis zum Einreichen
    workspace_snapshot: Optional[str] = Field(default=None, max_length=500)  # Pfad zu workspace.tar.gz (Workspace-Aufgaben)
    mc_result: Optional[str] = Field(default=None)  # MC: persistiertes Grading-Ergebnis (JSON)
    upload_file: Optional[str] = Field(default=None, max_length=500)  # Upload-Aufgaben: Original-Dateiname (data/submissions/{id}/<name>)


class Submission(SubmissionBase, table=True):
    __tablename__ = "submissions"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    submitted_at: datetime = Field(default_factory=datetime.now)
    status: SubmissionStatus = SubmissionStatus.PENDING
    
    # Relationships
    task: Task = Relationship(back_populates="submissions")
    student: User = Relationship(back_populates="submissions")
    feedback_list: List["Feedback"] = Relationship(back_populates="submission")


class SubmissionCreate(SQLModel):
    task_id: int
    solution: str = Field(default="")
    mc_answers: str = Field(default="")
    solve_time_seconds: float = Field(default=0.0)
    workspace_snapshot: Optional[str] = Field(default=None, max_length=500)
    upload_file: Optional[str] = Field(default=None, max_length=500)


class SubmissionRead(SubmissionBase):
    id: int
    submitted_at: datetime
    status: SubmissionStatus
    solve_time_seconds: float = Field(default=0.0)
    feedback_list: List["FeedbackRead"] = []


# ═══════════════════════════════════════════════════════════════════
# WORKSPACE RUN (Lauf-Historie von Workspace-Ausführungen)
# ═══════════════════════════════════════════════════════════════════

class WorkspaceRunStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    TIMEOUT = "timeout"
    KILLED = "killed"


class WorkspaceRun(SQLModel, table=True):
    """Historie der Run-Jobs einer Workspace-Aufgabe (nützlich für Tutor-Debugging).
    stdout/stderr werden beim Schreiben auf ~1 MB getruncatet (Log-Tail).
    submission_id: gesetzt bei Grading-/Tutor-Rerun (Link zu einer Einreichung);
    Student-Läufe im eigenen Workspace haben submission_id = None.
    """
    __tablename__ = "workspace_runs"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: str = Field(max_length=64, index=True)  # run_id des Compute-Agenten
    task_id: int = Field(foreign_key="tasks.id", index=True)
    student_id: int = Field(foreign_key="users.id", index=True)
    submission_id: Optional[int] = Field(default=None, foreign_key="submissions.id", index=True)
    command: str = Field(default="")
    started_at: datetime
    finished_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    status: WorkspaceRunStatus = WorkspaceRunStatus.RUNNING
    stdout: str = Field(default="")
    stderr: str = Field(default="")


class WorkspaceRunRead(SQLModel):
    id: int
    run_id: str
    task_id: int
    student_id: int
    command: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    status: WorkspaceRunStatus
    stdout: str = ""
    stderr: str = ""


# ═══════════════════════════════════════════════════════════════════
# FEEDBACK (LLM + manuell)
# ═══════════════════════════════════════════════════════════════════

class FeedbackBase(SQLModel):
    submission_id: int = Field(foreign_key="submissions.id")
    source: FeedbackSource
    points_earned: float = Field(ge=0)
    comment: str


class Feedback(FeedbackBase, table=True):
    __tablename__ = "feedback"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    giver_id: Optional[int] = Field(default=None, foreign_key="users.id")  # NULL bei LLM
    created_at: datetime = Field(default_factory=datetime.now)
    
    # Relationships
    submission: Submission = Relationship(back_populates="feedback_list")
    giver: Optional[User] = Relationship(back_populates="feedback_given")


class FeedbackCreate(SQLModel):
    source: FeedbackSource
    points_earned: float = Field(ge=0)
    comment: str
    giver_id: Optional[int] = None


class FeedbackRead(FeedbackBase):
    id: int
    giver_id: Optional[int]
    created_at: datetime
    giver: Optional[UserRead] = None


# ================================================================
# GLOBAL SETTINGS (LLM-Config, LDAP, Prompts — instanzweit)
# ================================================================
#
# Diese Tabelle hat genau eine Zeile und bildet die globale Basis-
# Konfiguration.  course_settings kann einzelne Felder pro Kurs
# überschreiben.
#
# Priorität (höchste → tiefste):
#   1. course_settings  (Kurs-Override)
#   2. global_settings   (Admin-Dashboard)
#   3. .env              (Umgebungsvariablen)
#   4. config.py Default (hardcoded Fallback)
#

class GlobalSettings(SQLModel, table=True):
    __tablename__ = "global_settings"

    id: int = Field(default=1, primary_key=True)   # Always id=1, exactly one row
    llm_api_url: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_model: Optional[str] = None
    grading_prompt: Optional[str] = None
    # Public Endpoint für nicht-sensitive Aufgaben (z. B. Task-/Musterlösung-Generierung).
    # Leer = Private Endpoint (llm_api_url) wird verwendet.
    llm_api_url_public: Optional[str] = None
    llm_api_key_public: Optional[str] = None
    llm_model_public: Optional[str] = None
    use_ldap: bool = Field(default=False)
    ldap_server: Optional[str] = None
    ldap_base_dn: Optional[str] = None
    ldap_bind_dn: Optional[str] = None
    ldap_bind_pw: Optional[str] = None
    ldap_user_search: Optional[str] = None
    # Compute/Workspaces: Anbindung an Compute-Agents (per-Student-Docker-Container)
    compute_enabled: bool = Field(default=False)
    # JSON-Liste: [{"name": ..., "url": ..., "key": ..., "gpu": true/false}]
    compute_agents: Optional[str] = None
    compute_idle_timeout: int = Field(default=1200)   # Sekunden bis Container-Abort bei Inaktivität
    workspace_gpu_enabled: bool = Field(default=False)
    compute_gpu_max_jobs: int = Field(default=8)       # parallele GPU-Jobs pro Agent (GPU nicht voll auslasten)


class GlobalSettingsUpdate(SQLModel):
    llm_api_url: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_model: Optional[str] = None
    grading_prompt: Optional[str] = None
    llm_api_url_public: Optional[str] = None
    llm_api_key_public: Optional[str] = None
    llm_model_public: Optional[str] = None
    use_ldap: Optional[bool] = None
    ldap_server: Optional[str] = None
    ldap_base_dn: Optional[str] = None
    ldap_bind_dn: Optional[str] = None
    ldap_bind_pw: Optional[str] = None
    ldap_user_search: Optional[str] = None
    compute_enabled: Optional[bool] = None
    compute_agents: Optional[str] = None
    compute_idle_timeout: Optional[int] = None
    workspace_gpu_enabled: Optional[bool] = None
    compute_gpu_max_jobs: Optional[int] = None


class GlobalSettingsRead(SQLModel, from_attributes=True):
    id: int
    llm_api_url: Optional[str]
    llm_api_key: Optional[str]
    llm_model: Optional[str]
    grading_prompt: Optional[str]
    llm_api_url_public: Optional[str]
    llm_api_key_public: Optional[str]
    llm_model_public: Optional[str]
    use_ldap: bool
    ldap_server: Optional[str]
    ldap_base_dn: Optional[str]
    ldap_bind_dn: Optional[str]
    ldap_user_search: Optional[str]
    compute_enabled: bool
    compute_agents: Optional[str]
    compute_idle_timeout: int
    workspace_gpu_enabled: bool
    compute_gpu_max_jobs: int


# ═══════════════════════════════════════════════════════════════════
# COURSE SETTINGS (pro Kurs: LLM-Config, LDAP, Prompts)
# ═══════════════════════════════════════════════════════════════════

class CourseSettingsBase(SQLModel):
    course_id: int = Field(foreign_key="courses.id", unique=True)
    llm_api_url: Optional[str] = None          # Override global LLM-URL
    llm_model: Optional[str] = None            # Override globales LLM-Modell
    grading_prompt: Optional[str] = None       # Custom grading prompt
    use_ldap: bool = Field(default=False)
    ldap_server: Optional[str] = None
    ldap_base_dn: Optional[str] = None
    ldap_bind_dn: Optional[str] = None
    ldap_bind_pw: Optional[str] = None
    ldap_user_search: Optional[str] = None  # e.g. (uid={username}) or (sAMAccountName={username})
    # Compute/Workspace-Overrides (None = globale Settings folgen, s. settings_resolver)
    compute_enabled: Optional[bool] = None         # Workspace-Aufgaben im Kurs (Override)
    compute_gpu_enabled: Optional[bool] = None     # GPU-Passthrough im Kurs (Override)
    compute_agents: Optional[str] = None           # JSON-Liste [{name,url,key,gpu}] (Override)


class CourseSettings(CourseSettingsBase, table=True):
    __tablename__ = "course_settings"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    
    course: Course = Relationship(back_populates="settings")


class CourseSettingsCreate(SQLModel):
    llm_api_url: Optional[str] = None
    llm_model: Optional[str] = None
    grading_prompt: Optional[str] = None
    use_ldap: bool = False
    ldap_server: Optional[str] = None
    ldap_base_dn: Optional[str] = None
    ldap_bind_dn: Optional[str] = None
    ldap_bind_pw: Optional[str] = None
    ldap_user_search: Optional[str] = None


class CourseSettingsRead(CourseSettingsBase):
    id: int


class CourseSettingsUpdate(SQLModel):
    llm_api_url: Optional[str] = None
    llm_model: Optional[str] = None
    grading_prompt: Optional[str] = None
    use_ldap: Optional[bool] = None
    ldap_server: Optional[str] = None
    ldap_base_dn: Optional[str] = None
    ldap_bind_dn: Optional[str] = None
    ldap_bind_pw: Optional[str] = None
    ldap_user_search: Optional[str] = None
    # Compute-Overrides: null = zurück auf global (wird im Endpoint per Raw-Body gehandhabt)
    compute_enabled: Optional[bool] = None
    compute_gpu_enabled: Optional[bool] = None
    compute_agents: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════
# COURSE INVITE (Einladungslink)
# ═══════════════════════════════════════════════════════════════════

class CourseInviteBase(SQLModel):
    course_id: int = Field(foreign_key="courses.id")
    token: str = Field(unique=True, index=True, max_length=100)
    expires_at: Optional[datetime] = None  # NULL = kein Ablauf
    max_uses: Optional[int] = None  # NULL = unbegrenzt
    used_count: int = Field(default=0)


class CourseInvite(CourseInviteBase, table=True):
    __tablename__ = "course_invites"

    id: Optional[int] = Field(default=None, primary_key=True)
    created_by: int = Field(foreign_key="users.id")
    created_at: datetime = Field(default_factory=datetime.now)

    course: "Course" = Relationship()


class CourseInviteCreate(SQLModel):
    expires_days: int = 7
    max_uses: Optional[int] = None


class CourseInviteRead(SQLModel):
    id: int
    course_id: int
    token: str
    expires_at: Optional[datetime]
    max_uses: Optional[int]
    used_count: int
    created_by: int
    created_at: datetime

    class Config:
        from_attributes = True


# ═══════════════════════════════════════════════════════════════════
# FORUM (Kurs-Forum: Chat in Kanälen, offen für alle Kurs-Rollen)
# ═══════════════════════════════════════════════════════════════════

class ForumChannelBase(SQLModel):
    course_id: int = Field(foreign_key="courses.id", index=True)
    name: str = Field(max_length=100)
    description: str = Field(default="", max_length=300)
    created_by: int = Field(foreign_key="users.id")
    created_at: datetime = Field(default_factory=datetime.now)


class ForumChannel(ForumChannelBase, table=True):
    __tablename__ = "forum_channels"

    id: Optional[int] = Field(default=None, primary_key=True)

    course: Optional["Course"] = Relationship(back_populates="channels")
    messages: List["ForumMessage"] = Relationship(back_populates="channel")


class ForumChannelCreate(SQLModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=300)


class ForumChannelUpdate(SQLModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=300)


class ForumMessageBase(SQLModel):
    course_id: int = Field(foreign_key="courses.id", index=True)
    channel_id: Optional[int] = Field(default=None, foreign_key="forum_channels.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    content: str = Field(max_length=5000)
    created_at: datetime = Field(default_factory=datetime.now)


class ForumMessage(ForumMessageBase, table=True):
    __tablename__ = "forum_messages"

    id: Optional[int] = Field(default=None, primary_key=True)

    user: Optional["User"] = Relationship()
    course: Optional["Course"] = Relationship()
    channel: Optional["ForumChannel"] = Relationship(back_populates="messages")


class ForumMessageCreate(SQLModel):
    content: str = Field(min_length=1, max_length=5000)


class ForumMessageRead(SQLModel):
    id: int
    course_id: int
    channel_id: Optional[int] = None
    user_id: int
    content: str
    created_at: datetime
    username: str
    name: str
    role: str
    avatar: Optional[str] = None
    can_delete: bool = False


class ForumChannelReadState(SQLModel, table=True):
    """Letzter gelesener Nachrichten-Stand je User und Forum-Kanal.

    Basis für die Ungelesen-Zähler: Alle Nachrichten mit
    id > last_read_message_id sind ungelesen (0 = Kanal noch nie gelesen).
    Neue Tabelle wird beim App-Start per create_all angelegt.
    """

    __tablename__ = "forum_channel_read_state"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    channel_id: int = Field(foreign_key="forum_channels.id", index=True)
    last_read_message_id: int = Field(default=0)

    __table_args__ = (UniqueConstraint("user_id", "channel_id"),)


class ForumChannelMute(SQLModel, table=True):
    """Forum-Kanäle, die ein User gemutet hat (keine Glocke-Benachrichtigungen).

    Das Muten ist rein persönlich: Der User kann den Kanal weiterhin lesen
    (der Ungelesen-Zähler bleibt in der Sidebar sichtbar, grau statt rot),
    bekommt aber keine forum_message-Benachrichtigungen mehr und der Kanal
    zählt nicht auf das Forum-Tab-Badge. Neue Tabelle wird beim App-Start
    per create_all angelegt.
    """

    __tablename__ = "forum_channel_mutes"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    channel_id: int = Field(foreign_key="forum_channels.id", index=True)
    created_at: datetime = Field(default_factory=datetime.now)

    __table_args__ = (UniqueConstraint("user_id", "channel_id"),)


# ═══════════════════════════════════════════════════════════════════
# MESSAGING (Direktnachrichten + Gruppenchats, plattform-weit;
# Architektur-Basis: Kurs-Forum, s. docs/LLM-plans/messaging.md)
# ═══════════════════════════════════════════════════════════════════

class DmChatBase(SQLModel):
    # NULL = 1:1-Chat (Titel wird aus dem Gegenüber abgeleitet)
    name: Optional[str] = Field(default=None, max_length=100)
    created_by: int = Field(foreign_key="users.id")
    created_at: datetime = Field(default_factory=datetime.now)


class DmChat(DmChatBase, table=True):
    __tablename__ = "dm_chats"

    id: Optional[int] = Field(default=None, primary_key=True)

    members: List["DmChatMember"] = Relationship()
    messages: List["DmMessage"] = Relationship()


class DmChatMember(SQLModel, table=True):
    """Mitgliedschaft in einem Chat (N:M Chat↔User).

    Neue Tabelle wird beim App-Start per create_all angelegt.
    """

    __tablename__ = "dm_chat_members"

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: int = Field(foreign_key="dm_chats.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    joined_at: datetime = Field(default_factory=datetime.now)

    __table_args__ = (UniqueConstraint("chat_id", "user_id"),)


class DmMessageBase(SQLModel):
    chat_id: int = Field(foreign_key="dm_chats.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    content: str = Field(max_length=5000)
    created_at: datetime = Field(default_factory=datetime.now)


class DmMessage(DmMessageBase, table=True):
    __tablename__ = "dm_messages"

    id: Optional[int] = Field(default=None, primary_key=True)

    user: Optional["User"] = Relationship()


class DmChatReadState(SQLModel, table=True):
    """Letzter gelesener Nachrichten-Stand je User und Chat.

    Basis für die Ungelesen-Zähler: Alle Nachrichten mit
    id > last_read_message_id sind ungelesen (0 = Chat noch nie gelesen).
    Neue Tabelle wird beim App-Start per create_all angelegt.
    """

    __tablename__ = "dm_chat_read_state"

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: int = Field(foreign_key="dm_chats.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    last_read_message_id: int = Field(default=0)

    __table_args__ = (UniqueConstraint("chat_id", "user_id"),)


class DmChatCreate(SQLModel):
    member_id: int  # 1:1-Chat mit diesem User (bestehender wird wiederverwendet)


class DmGroupChatCreate(SQLModel):
    name: str = Field(min_length=1, max_length=100)
    member_ids: List[int] = Field(min_length=1, max_length=100)


class DmChatUpdate(SQLModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)


class DmMessageCreate(SQLModel):
    content: str = Field(min_length=1, max_length=5000)


class DmMessageRead(SQLModel):
    id: int
    chat_id: int
    user_id: int
    content: str
    created_at: datetime
    username: str
    name: str
    avatar: Optional[str] = None
    is_self: bool = False


# ═══════════════════════════════════════════════════════════════════
# SKRIPT-FRAGEN (Studenten-Fragen zum Skript, inkl. LLM- +
# Human-Antworten; für Tutoren/Profs zur Skript-Verbesserung)
# ═══════════════════════════════════════════════════════════════════

class ScriptQuestionBase(SQLModel):
    course_id: int = Field(foreign_key="courses.id", index=True)
    # None = Allgemeine Frage (kein konkretes Kapitel / Kapitel gelöscht)
    section_id: Optional[int] = Field(default=None, foreign_key="course_script_sections.id", index=True)
    student_id: int = Field(foreign_key="users.id", index=True)
    question: str = Field(max_length=2000)
    # Optionaler Text-Exkurs aus dem Skript, auf den sich die Frage bezieht
    quote: str = Field(default="", max_length=1000)
    # Kontext um die Quote (Quote + ~120 Zeichen vor/nachher, normalisiert),
    # damit die Stelle im Skript eindeutig zu finden ist; None bei älteren Fragen
    quote_ctx: Optional[str] = Field(default=None, max_length=2000)
    # Startoffset der Quote innerhalb von quote_ctx (normalisierter String)
    quote_off: int = Field(default=0)
    # "open" | "addressed" (von Tutoren/Profs umschaltbar)
    status: str = Field(default="open", max_length=20)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class ScriptQuestion(ScriptQuestionBase, table=True):
    __tablename__ = "script_questions"

    id: Optional[int] = Field(default=None, primary_key=True)

    responses: List["ScriptQuestionResponse"] = Relationship(back_populates="question")


class ScriptQuestionResponse(SQLModel, table=True):
    __tablename__ = "script_question_responses"

    id: Optional[int] = Field(default=None, primary_key=True)
    question_id: int = Field(foreign_key="script_questions.id", index=True)
    # None = LLM-Antwort (source == "llm")
    user_id: Optional[int] = Field(default=None, foreign_key="users.id")
    # "llm" | "human"
    source: str = Field(default="human", max_length=10)
    content: str = Field(default="", max_length=8000)
    created_at: datetime = Field(default_factory=datetime.now)

    question: Optional["ScriptQuestion"] = Relationship(back_populates="responses")


# ═══════════════════════════════════════════════════════════════════
# COURSE IMPORT (Kurs-Materialien aus einem Zip importieren, PROF/Admin)
# ═══════════════════════════════════════════════════════════════════

class CourseImport(SQLModel, table=True):
    """Eine Zeile pro Zip-Import. Max. ein Import pro Kurs (gelöscht via API).

    Der Status pro Stufe IST das Steuerungs-Flag (kein separates paused-Feld):
    pending → running → done / error, dazwischen pausierbar (paused),
    abbrechbar (cancelled), bei App-Neustart interrupted. Der Runner liest
    den Status stateless aus der DB (frische Session pro Gate/Unit) —
    Pause/Resume/Cancel/Neustart laufen über denselben Code-Pfad.

    JSON-Spalten:
    - manifest:     [{path, type, size, read_path?, pages?, line_count?, main_tex?}]
                    (klassifizierte Zip-Einträge; read_path = Datei, die gelesen
                    wird = Sidecar (.md) für docx/pptx/pdf, sonst die Originale)
    - file_map:     {path: {status, chunks: [{start_line, end_line, summary,
                    headings: [{text, line, level}], error?}], error?}}
                    (LLM-Struktur-Digests der Text-Dateien; Zeilen 1-basiert, inclusive)
    - media_map:    {path: {url, urls?, media_id, media_ids?}} (importierte Medien)
    - reference_map: {key: {entry_type, authors, title, year, venue, detail, address,
                    doi, url, note, stub?, sources: [Datei], imported_ref_id?}}
                    (detected Quellen aus .bib-Files + \\cite-Keys; stub = Key ohne Bib-Daten)
    - chapter_plan: [{title, enabled, description?, section_id?,
                    script_status: pending|done|error, script_error?}]
                    (Skript-Plan; description nennt die Text-Quelldateien mit
                    Pfad + Zeilenbereich. sources/sections (Gather-Ergebnis) und
                    section_id (generiertes Kapitel, Edit-Modus/Reihenfolge) werden
                    zur Laufzeit ergänzt; ältere Pläne können zusätzlich „sources“/
                    „slides“ enthalten und werden toleriert, aber ignoriert)
    - slides_plan:  [{title, enabled, description?, material_id?,
                    slides_status: pending|done|error, slides_error?}]
                    (Folien-Plan; description nennt Quelldateien inkl. pptx-Pfad
                    + Folienbereich. sources/sections (Gather-Ergebnis) und
                    material_id (generiertes Deck) werden zur Laufzeit ergänzt)
    - tasks_plan:   [{title, enabled, points, task_type: text|mc|workspace,
                    description?, task_id?, tasks_status: pending|done|error,
                    tasks_error?}]
                    (Aufgaben-Plan, 1 Eintrag = 1 Übungsaufgabe; description nennt
                    Quelldatei + Zeilenbereich + Aufgaben-Kennung. task_id (generierter
                    Task) wird zur Laufzeit ergänzt)
    - progress:     {current: str, stages: {stage: {total, done, failed}},
                    units: {unit_key: {done, total}}}
    - report:       {warnings: [], errors: [], summary?, slides_options?: {...}}
    """
    __tablename__ = "course_imports"

    id: Optional[int] = Field(default=None, primary_key=True)
    course_id: int = Field(foreign_key="courses.id", index=True)
    # Staging-Verzeichnisname: data/imports/course_{id}/{job_id}/
    job_id: str = Field(max_length=32, unique=True)
    zip_name: str = Field(max_length=300)
    created_by: int = Field(foreign_key="users.id")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    filemap_status: str = Field(default="pending", max_length=20)
    media_status: str = Field(default="pending", max_length=20)
    references_status: str = Field(default="pending", max_length=20)
    ref_extract_status: str = Field(default="pending", max_length=20)  # LLM-Quellen-Extraktion
    plan_status: str = Field(default="pending", max_length=20)  # Skript-Kapitel-Planner
    slides_plan_status: str = Field(default="pending", max_length=20)  # Folien-Planner
    script_status: str = Field(default="pending", max_length=20)
    slides_status: str = Field(default="pending", max_length=20)
    tasks_plan_status: str = Field(default="pending", max_length=20)  # Aufgaben-Planner
    tasks_status: str = Field(default="pending", max_length=20)  # Aufgaben-Generierung

    manifest: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    file_map: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    media_map: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    reference_map: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    chapter_plan: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    slides_plan: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    tasks_plan: list = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    progress: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    report: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


class LLMDebugEntry(SQLModel, table=True):
    """Persistentes LLM-Debug-Log für die Admin-Konsole (Tab "LLM-Debug-Log").

    Jeder LLM-Call wird mit Prompt-Typ, auslösendem Nutzer, Modell/URL,
    public/private, System-Prompt, Prompt, Antwort, Thinking, Latenz und
    Status protokolliert.
    Retention: 7 Tage — alte Einträge werden beim Purgen gelöscht
    (services/llm_service.py). Neue Tabelle wird beim App-Start per create_all angelegt.
    """
    __tablename__ = "llm_debug_entries"

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=datetime.now, index=True)
    label: str = Field(default="", max_length=100)
    model: str = Field(default="", max_length=200)
    url: str = Field(default="", max_length=500)
    is_public: bool = False
    user_id: Optional[int] = Field(default=None, index=True)  # auslösender User (NULL = kein User-Kontext)
    system_prompt: str = ""
    prompt: str = ""
    response: str = ""
    thinking: str = ""
    success: bool = True
    error: str = ""
    latency_ms: int = 0
    attempts: int = 1


# ═══════════════════════════════════════════════════════════════
# NOTIFICATIONS (in-App-Benachrichtigungen, Glocke in der Top-Bar)
# ═══════════════════════════════════════════════════════════════

class Notification(SQLModel, table=True):
    """Benachrichtigung für einen Plattform-User (Glocke in der Top-Bar).

    Typen (type):
    - "forum_message": neue Forum-Nachricht (alle Kurs-Mitglieder außer Absender)
    - "script_question_answer": neue Antwort auf eine Skript-Frage (Fragesteller)
    - "llm_feedback": LLM-Feedback nach Abgabe (Student; Erfolg oder Fehler)
    - "dm_message": neue Direktnachricht (alle Chat-Mitglieder außer Absender)

    title/body sind beim Erstellen vor-berechnete Snapshots (kein Join nötig
    für die Listen-Anzeige). Neue Tabelle wird beim App-Start per create_all
    angelegt.
    """

    __tablename__ = "notifications"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)  # Empfänger
    course_id: Optional[int] = Field(default=None, foreign_key="courses.id", index=True)
    channel_id: Optional[int] = Field(default=None, index=True)  # Forum-Kanal (nur forum_message)
    chat_id: Optional[int] = Field(default=None, index=True)  # DM-Chat (nur dm_message; Auto-Read beim Chat-Öffnen)
    actor_id: Optional[int] = Field(default=None, foreign_key="users.id")  # None = System (LLM)
    # "forum_message" | "script_question_answer" | "llm_feedback" | "dm_message"
    type: str = Field(max_length=30)
    title: str = Field(max_length=200)
    body: str = Field(default="", max_length=500)
    link: str = Field(default="", max_length=300)
    read_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.now, index=True)


class PushSubscription(SQLModel, table=True):
    """Browser-Web-Push-Subscription (M3): eine Zeile pro Browser/Gerät.

    Der Service Worker (static/sw.js) meldet die PushManager-Subscription
    per POST /api/push/subscribe; das Endpoint-URL (FCM) ist pro Gerät
    eindeutig und dient als Dedupe-Key. Der Server braucht nur endpoint
    + p256dh/auth, um per pywebpush (VAPID-JWT) eine Push-Nachricht zu
    verschicken. Neue Tabelle wird beim App-Start per create_all angelegt.
    """

    __tablename__ = "push_subscriptions"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    endpoint: str = Field(unique=True, max_length=500)
    p256dh: str = Field(max_length=255)  # E2E-Verschlüsselung (AES128GCM)
    auth: str = Field(max_length=255)
    user_agent: str = Field(default="", max_length=300)
    created_at: datetime = Field(default_factory=datetime.now)


# ═══════════════════════════════════════════════════════════════════
# ANALYTICS: PAGE-VIEW-TRACKING (in-house, s. services/analytics.py)
# ═══════════════════════════════════════════════════════════════════

class PageView(SQLModel, table=True):
    """Nutzungs-Tracking (in-house Analytics): wer hat wann welche Seite geöffnet.

    Wird vom Tracking-Middleware (main.py) bei jedem Aufruf einer HTML-Seite
    eines eingeloggten Users gefüllt und ausschließlich vom Analytics-
    Dashboard in der Admin-Konsole (api/analytics.py) gelesen. Bewusst ohne
    IP / User-Agent / Referrer: nur User-ID, Seite, Kurs und Zeitstempel.
    Alte Einträge werden beim App-Start bereinigt (RETENTION_DAYS).
    """

    __tablename__ = "page_views"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    path: str = Field(max_length=500)              # maskierter Pfad (z. B. /join/<token>)
    page_key: str = Field(max_length=50, index=True)  # normalisierte Seitenart (s. analytics.classify_path)
    course_id: Optional[int] = Field(default=None, foreign_key="courses.id", index=True)
    created_at: datetime = Field(default_factory=datetime.now, index=True)


# ═══════════════════════════════════════════════════════════════════
# MCP TOKENS (MCP-Anbindung: ein Token pro User + Kurs-Allowlist)
# ═══════════════════════════════════════════════════════════════════

class McpToken(SQLModel, table=True):
    """MCP-Zugangstoken (analog GitHub-PATs).

    Der Token selbst (Format `aic_<64 hex>`) wird NIE gespeichert — nur
    sein SHA-256-Hash (Lookup-Index). Ein Token pro User mit einer
    Kurs-Allowlist (McpTokenCourse); die Rolle wird pro Request live aus
    UserCourse abgeleitet (s. services/mcp_token_service.py).

    allow_write = Write-Schalter (Einstellungen → MCP): ohne ihn sind die
    MCP-Write-Tools gesperrt; die Kurs-Rolle (Tutor/Prof) begrenzt zusätzlich.
    """

    __tablename__ = "mcp_tokens"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    name: str = Field(max_length=100)
    token_hash: str = Field(max_length=64, unique=True, index=True)
    allow_write: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.now)
    last_used_at: Optional[datetime] = Field(default=None)
    expires_at: Optional[datetime] = Field(default=None)  # NULL = kein Ablauf

    courses: List["McpTokenCourse"] = Relationship(back_populates="token")


class McpTokenCourse(SQLModel, table=True):
    """Allowlist-Eintrag: welchen Kurs dieses Token adressieren darf.

    Zugriffs-Check = Allowlist-Row ODER Kurs-Mitgliedschaft (beides muss
    gelten) — s. api/mcp_server.py.
    """

    __tablename__ = "mcp_token_courses"

    id: Optional[int] = Field(default=None, primary_key=True)
    token_id: int = Field(foreign_key="mcp_tokens.id", index=True)
    course_id: int = Field(foreign_key="courses.id", index=True)

    token: McpToken = Relationship(back_populates="courses")


# ═══════════════════════════════════════════════════════════════════
# MCP OAUTH (Authorization-Code-Flow + PKCE für ChatGPT & Co.)
# ═══════════════════════════════════════════════════════════════════

class McpOAuthClient(SQLModel, table=True):
    """OAuth-Client, registriert via DCR (RFC 7591) durch den MCP-Client.

    ChatGPT & Co. registrieren sich selbst per POST /mcp/register beim
    ersten Verbindungsversuch. client_secret nur für confiante Clients
    (token_endpoint_auth_method != 'none').
    """

    __tablename__ = "mcp_oauth_clients"

    id: Optional[int] = Field(default=None, primary_key=True)
    client_id: str = Field(max_length=100, unique=True, index=True)
    client_name: Optional[str] = Field(default=None, max_length=200)
    redirect_uris: str  # JSON-Liste, z. B. '["https://chatgpt.com/mcp/oauth/callback"]'
    token_endpoint_auth_method: str = "none"  # 'none' (Public Client) etc.
    grant_types: str  # JSON-Liste, z. B. '["authorization_code", "refresh_token"]'
    client_secret: Optional[str] = Field(default=None, max_length=255)
    created_at: datetime = Field(default_factory=datetime.now)
    last_used_at: Optional[datetime] = Field(default=None)


class McpOAuthCode(SQLModel, table=True):
    """Authorization-Code des OAuth-Flusses (Einmal-Code, 10 Min. gültig).

    Gespeichert wird nur der SHA-256-Hash — der Klartext-Code wandert
    einmalig in den Redirect an den Client. user_id bleibt NULL, bis der
    User die Consent-Seite bestätigt; ein Code ohne Consent ist nicht
    einlösbar. course_ids/allow_write = Consent-Entscheidung (Scope).
    """

    __tablename__ = "mcp_oauth_codes"

    id: Optional[int] = Field(default=None, primary_key=True)
    code_hash: str = Field(max_length=64, unique=True, index=True)
    client_id: str = Field(max_length=100, index=True)
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    code_challenge: Optional[str] = Field(default=None, max_length=128)  # PKCE S256
    redirect_uri: str  # URL des Clients, an die der Code zurückgeht
    redirect_uri_explicit: bool = False  # True → Token-Request muss sie übergeben
    state: Optional[str] = Field(default=None, max_length=512)  # wird unverändert zurückgegeben
    course_ids: str = "[]"  # JSON-Liste: Consent-Entscheidung, welche Kurse
    allow_write: bool = False  # Consent-Entscheidung: Schreibzugriff?
    expires_at: datetime
    used_at: Optional[datetime] = Field(default=None)


class McpOAuthToken(SQLModel, table=True):
    """OAuth-Token-Paar (Access + Refresh) pro User+Client.

    Access (`aio_<64 hex>`) läuft nach 1 h ab, Refresh (`aioR_<64 hex>`) nach
    90 Tagen; der Refresh wird bei jedem Einsatz rotiert (neuer Hash auf
    derselben Zeile, der Alte ist sofort ungültig). Es existiert genau EIN
    gültiges Paar pro User+Client (ältere werden beim nächsten Consent
    gelöscht). allow_write = Schalter des Grants — Änderungen in den
    Einstellungen werden auf die Zeile übertragen (s. Service).
    """

    __tablename__ = "mcp_oauth_tokens"

    id: Optional[int] = Field(default=None, primary_key=True)
    access_hash: str = Field(max_length=64, unique=True, index=True)
    refresh_hash: str = Field(max_length=64, unique=True, index=True)
    client_id: str = Field(max_length=100, index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    allow_write: bool = False
    access_expires_at: datetime
    refresh_expires_at: datetime
    created_at: datetime = Field(default_factory=datetime.now)
    last_used_at: Optional[datetime] = Field(default=None)
    # Replay-Toleranz nach Refresh-Rotation: der alte Hash bleibt kurz gültig,
    # damit parallele/retierende Client-Requests (z. B. ChatGPT) den Grant
    # nicht mit einem 400 brechen (RFC 6749 §10.4).
    prev_refresh_hash: Optional[str] = Field(default=None, max_length=64)
    prev_refresh_valid_until: Optional[datetime] = Field(default=None)


class McpOAuthGrantCourse(SQLModel, table=True):
    """Pro-Client Kurs-Allowlist (OAuth): welche Kurse dieser Client erreicht.

    Setzt sich die Consent-Seite; in den User-Settings nachträglich
    editierbar (replace-all). Analog McpTokenCourse für die PAT-Tokens.
    """

    __tablename__ = "mcp_oauth_grant_courses"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    client_id: str = Field(max_length=100, index=True)
    course_id: int = Field(foreign_key="courses.id", index=True)


class McpEvent(SQLModel, table=True):
    """MCP-Nutzungs-Tracking (Analytics-Dashboard): welcher User hat über
    welchen Client welches Tool in welchem Kurs mit welchem Ergebnis aufgerufen.

    Wird bei jedem Tool-Call von api/mcp_server.py geschrieben und
    ausschließlich von api/analytics.py gelesen. Wie bei PageView Privacy:
    nur User, Client-Name, Kurs, Tool, Read/Write und Ergebnis — kein
    Inhalt, keine IPs.
    """

    __tablename__ = "mcp_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    course_id: Optional[int] = Field(default=None, foreign_key="courses.id", index=True)
    tool_name: str = Field(max_length=100, index=True)
    client: Optional[str] = Field(default=None, max_length=100)  # PAT-Token-Name oder OAuth-Client-Name
    is_write: bool = Field(default=False)
    ok: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.now, index=True)


class McpClientContext(SQLModel, table=True):
    """Aktueller UI-Zustand des Users für das MCP-Tool get_user_focus
    (welche Seite/Kapitel/Folie offen, welcher Text markiert).

    Eine Zeile pro User; der Browser meldet ihn via POST
    /api/mcp/client-context (Session-Auth), last-write-wins — bei mehreren
    Tabs zählt der zuletzt aktive (versteckte Tabs melden nicht).
    Nur nicht-sensible UI-Zustände (keine Studierenden-Daten); kein
    Logging des Inhalts (s. docs/LLM-plans/mcp-client-context.md).
    """

    __tablename__ = "mcp_client_context"

    user_id: int = Field(foreign_key="users.id", primary_key=True)
    url: str = Field(max_length=500)  # pathname der letzten gemeldeten Seite
    selection: Optional[str] = Field(default=None, max_length=1000)
    selection_context: Optional[str] = Field(default=None, max_length=2000)
    selection_offset: Optional[int] = Field(default=None)
    section_id: Optional[int] = Field(default=None)  # Skript: aktuelles Kapitel
    slide_index: Optional[int] = Field(default=None)
    slide_count: Optional[int] = Field(default=None)
    slide_text: Optional[str] = Field(default=None, max_length=2000)
    updated_at: datetime = Field(default_factory=datetime.now, index=True)

# ═══════════════════════════════════════════════════════════════════
# BLIND-GRADING-STUDIE (Projektgruppe: Varianz Tutor:innen vs. LLMs)
# ═══════════════════════════════════════════════════════════════════
#
# Bewusst KOMPLETT getrennt von Task/Submission/Feedback:
# - Studien-Antworten sind (synthetische) Testdaten, keine echten Abgaben
#   → tauchen nicht in Übersicht/Punkten/Statistik des Kurses auf.
# - Tutor-Bewertungen hier ändern keine Noten und lösen keine
#   Benachrichtigungen aus.
# - Pro Antwort beliebig viele Bewertungen (eine je Tutor:in), damit die
#   Streuung zwischen Menschen messbar ist.
# Details: docs/blind-grading.md

class StudyItem(SQLModel, table=True):
    """Eine zu bewertende Antwort (inkl. Aufgabe + Musterlösung)."""
    __tablename__ = "study_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    course_id: int = Field(foreign_key="courses.id", index=True)
    task_key: str = Field(max_length=100, index=True)     # gruppiert Antworten derselben Aufgabe, z. B. "swt-ue01-a1"
    task_title: str = Field(max_length=300)
    task_description: str                                 # Aufgabenstellung (Markdown)
    model_solution: str = Field(default="")               # Musterlösung (Markdown)
    max_points: float = Field(ge=0)
    answer: str                                           # die zu bewertende (synthetische) Antwort
    # Nur für die Auswertung — wird Tutor:innen NIE angezeigt:
    intended_points: Optional[float] = Field(default=None)  # beabsichtigte Punktzahl beim Erstellen
    error_type: Optional[str] = Field(default=None, max_length=100)  # z. B. "korrekt", "fehlkonzept", "off-topic"
    source: Optional[str] = Field(default=None, max_length=100)      # z. B. "handgeschrieben", "llm:qwen3"
    created_at: datetime = Field(default_factory=datetime.now)


class StudyRating(SQLModel, table=True):
    """Blinde Bewertung einer StudyItem-Antwort durch eine:n Tutor:in."""
    __tablename__ = "study_ratings"
    __table_args__ = (UniqueConstraint("item_id", "rater_id", name="uq_study_rating_item_rater"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    item_id: int = Field(foreign_key="study_items.id", index=True)
    rater_id: int = Field(foreign_key="users.id", index=True)
    points: float = Field(ge=0)
    comment: str = Field(default="")
    seconds_spent: Optional[float] = Field(default=None)  # Bearbeitungszeit im Browser
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class StudyModelRating(SQLModel, table=True):
    """Bewertung einer StudyItem-Antwort durch ein LLM (ein Lauf)."""
    __tablename__ = "study_model_ratings"

    id: Optional[int] = Field(default=None, primary_key=True)
    item_id: int = Field(foreign_key="study_items.id", index=True)
    model: str = Field(max_length=200, index=True)        # z. B. "Qwen3-32B"
    run: int = Field(default=1)                           # Wiederholung (Selbst-Konsistenz)
    prompt_version: str = Field(default="default", max_length=100)
    points: Optional[float] = Field(default=None)         # None = Lauf fehlgeschlagen
    comment: str = Field(default="")
    error: Optional[str] = Field(default=None)
    latency_ms: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.now)


class StudyModelEndpoint(SQLModel, table=True):
    """Ein LLM-Endpoint für die Studie (in der Oberfläche hinzufügen/entfernen).

    Beliebige OpenAI-kompatible APIs: lokales Ollama, vLLM auf einem
    GPU-Server, kostenlose Cloud-Anbieter (OpenRouter, Groq, …).
    `name` ist das Label in den Ergebnissen (StudyModelRating.model).
    """
    __tablename__ = "study_model_endpoints"
    __table_args__ = (UniqueConstraint("course_id", "name", name="uq_study_endpoint_course_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    course_id: int = Field(foreign_key="courses.id", index=True)
    name: str = Field(max_length=200)          # Anzeige/Ergebnis-Label, z. B. "qwen3:4b (lokal)"
    api_url: str = Field(max_length=500)        # z. B. http://localhost:11434/v1
    api_key: str = Field(default="", max_length=500)
    model: str = Field(max_length=200)          # Modellname beim Anbieter, z. B. "qwen3:4b"
    enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.now)
