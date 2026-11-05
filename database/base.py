"""
Database Engine + Session Management.

Provides SQLModel-compatible engine and session factory for dependency injection.
Supports both SQLite (dev) and PostgreSQL (production).
"""

import re

from sqlalchemy import event
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel, create_engine, Session
from config import DATABASE_URL

# ─── Connection Pool Settings ────────────────────────────────────
if "sqlite" in DATABASE_URL:
    # NullPool: jede Session öffnet eine eigene Connection und schließt sie
    # nach Gebrauch wieder. Ein begrenzter Pool (Default 5+10) lief bei
    # langen Requests (LLM-Generierung, Import-Jobs) komplett leer → alle
    # übrigen Requests warteten bis zu 30 s auf eine Connection
    # (Server-Ausfall 2026-10-06, siehe docs/LLM-plans/db-incident-2026-10-06.md).
    engine = create_engine(
        DATABASE_URL,
        echo=False,              # Set True for SQL-debug output
        poolclass=NullPool,
        connect_args={"check_same_thread": False},  # Required for SQLite + threads
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, connection_record):
        """Pragmas je Connection (bei connect: journal_mode greift nur
        außerhalb eines Transaktionskontexts).

        - WAL: Reader und Writer blockieren sich nicht mehr gegenseitig.
          Ohne WAL (journal_mode=delete) hat eine einzige gehaltene
          Read-Connection (z. B. eine Session während eines mehrminütigen
          LLM-Calls) alle Writes mit „database is locked“ blockiert.
        - busy_timeout: bis zu 10 s auf eine Sperre warten (z. B. vom
          Compute-Agenten), statt 5 s wie pysqlite-Default.
        - foreign_keys: FK-Constraints erzwingen. Ohne sie können Zeilen
          auf gelöschte Eltern schreiben (z. B. ein Kapitel in einen
          bereits gelöschten Kurs) und bei ID-Wiederverwendung still in
          einem neuen Kurs auftauchen.
        """
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
else:
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        pool_size=10,
        max_overflow=20,
        pool_recycle=3600,
    )


def create_db_and_tables():
    """Create all tables. Call once at app startup."""
    SQLModel.metadata.create_all(engine)


def _sqlite_drop_column(conn, table: str, column: str) -> None:
    """Entfernt eine Spalte (SQLite).

    SQLite >= 3.35: ALTER TABLE ... DROP COLUMN; ältere Versionen:
    Tabellen-Rebuild (neue Tabelle anlegen, Daten kopieren, alte löschen).
    """
    try:
        conn.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {column}")
        return
    except Exception:
        pass  # SQLite < 3.35 → Rebuild

    cols = [
        row[1]
        for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
        if row[1] != column
    ]
    create_sql = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]
    # Die Spalten-Definition aus dem CREATE-TABLE-DDL entfernen. Zwei Fälle:
    # 1) eigene Zeile (so legt SQLAlchemy die Tabelle an)
    new_create = re.sub(
        rf"^[ \t]*{re.escape(column)}[ \t]+[^\n]*\n",
        "",
        create_sql,
        count=1,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    # 2) an die vorherige Zeile angehängt (so schreibt SQLite das DDL nach
    #    ALTER TABLE ... ADD COLUMN um)
    if re.search(rf"\b{re.escape(column)}\b", new_create, flags=re.IGNORECASE):
        new_create = re.sub(
            rf",\s*{re.escape(column)}[ \t]+[A-Za-z0-9_()+'\" ]*",
            "",
            new_create,
            count=1,
            flags=re.IGNORECASE,
        )
    if re.search(rf"\b{re.escape(column)}\b", new_create, flags=re.IGNORECASE):
        raise RuntimeError(
            f"Migration: Spalte {table}.{column} konnte nicht aus dem CREATE-TABLE-DDL entfernt werden."
        )
    new_create = new_create.replace(",\n)", "\n)")
    new_table = f"{table}_migration_tmp"
    new_create = (
        new_create.replace(f'CREATE TABLE "{table}"', f'CREATE TABLE "{new_table}"', 1)
        .replace(f"CREATE TABLE {table}", f"CREATE TABLE {new_table}", 1)
    )
    quoted = ", ".join(f'"{c}"' for c in cols)
    conn.exec_driver_sql(new_create)
    conn.exec_driver_sql(f"INSERT INTO {new_table} ({quoted}) SELECT {quoted} FROM {table}")
    conn.exec_driver_sql(f"DROP TABLE {table}")
    conn.exec_driver_sql(f"ALTER TABLE {new_table} RENAME TO {table}")


def migrate_schema():
    """Migriert Schema-Änderungen an bestehenden Datenbanken.

    create_all() legt nur fehlende Tabellen an, ergänzt aber keine neuen
    Spalten bzw. entfernt keine entfallene. Hier werden fehlende Spalten
    nachgezogen und obsolete Spalten entfernt (SQLite + PostgreSQL).
    """
    column_migrations = {
        "users": {
            "avatar": "VARCHAR",  # relatives Pfad des Profilbilds (avatars/<uuid>.<ext>)
            "deleted_at": "DATETIME",  # Soft Delete (null = aktiv)
            "profile": "TEXT",  # Steckbrief (Markdown), öffentlich unter /u/<username>
        },
        "global_settings": {
            "llm_api_url_public": "VARCHAR",
            "llm_api_key_public": "VARCHAR",
            "llm_model_public": "VARCHAR",
            # Compute/Workspaces (per-Student-Docker-Container)
            "compute_enabled": "BOOLEAN DEFAULT 0",
            "compute_agents": "VARCHAR",  # JSON-Liste [{name,url,key,gpu}]
            "compute_idle_timeout": "INTEGER DEFAULT 1200",
            "workspace_gpu_enabled": "BOOLEAN DEFAULT 0",
            "compute_gpu_max_jobs": "INTEGER DEFAULT 8",
        },
        "course_script_sections": {
            "summary": "TEXT",  # Interne LLM-Zusammenfassung (nicht für Studenten)
            "deleted_at": "DATETIME",  # Soft Delete (null = aktiv)
        },
        "forum_messages": {
            "channel_id": "INTEGER",  # Forum-Kanal, dem die Nachricht zugeordnet ist
        },
        "script_questions": {
            "quote_ctx": "VARCHAR(2000)",  # Kontext um die Quote (eindeutige Ortung)
            "quote_off": "INTEGER",  # Startoffset der Quote innerhalb von quote_ctx
        },
        "user_courses": {
            "annotation": "TEXT",  # Freie Anmerkung je Mitglied (z.B. Übungsgruppe)
        },
        "courses": {
            "toc_visible": "BOOLEAN DEFAULT 1",  # Inhaltsverzeichnis für Studenten sichtbar
            "deleted_at": "DATETIME",  # Soft Delete (null = aktiv)
        },
        "tasks": {
            # (Legacy-Spalten workspace_spec/workspace_dataset wurden mit dem
            # Skript-Modell ersetzt — siehe column_drops; Migration:
            # scripts/migrate_workspace_scripts.py, einmalig gelaufen.)
            "workspace_main_file": "VARCHAR",  # Editor-Hauptdatei (relativ)
            "workspace_assets_status": "TEXT",  # JSON je Agent: assets + task_image-Status
            "workspace_engines": "VARCHAR",  # JSON-Liste Engine-Namen (geordnet) — Engine-Pool
            "workspace_image": "VARCHAR",  # Image-Spec-Name (Kurs-Scope > global)
            # Skript-basiertes Workspace-Modell (Umgebung je Aufgabe):
            "workspace_timeout": "INTEGER DEFAULT 900",  # Run-Timeout (s, 1–7200)
            "workspace_cpu": "REAL DEFAULT 2",  # CPU-Limit
            "workspace_memory": "VARCHAR DEFAULT '4g'",  # Memory-Limit
            "workspace_disk_quota": "REAL DEFAULT 1.0",  # Disk-Quota des Student-Volumes in GB (0 = ohne Limit)
            "workspace_internet": "BOOLEAN DEFAULT 0",  # Internet für Studenten-Läufe
            "workspace_preview_root_ports": "VARCHAR",  # JSON-Liste Ports ohne Base-Path-Preview (z. B. [8000])
            "mc_data": "TEXT",  # Multiple-Choice: Fragen/Optionen/Feedback als JSON
            "text_template": "TEXT",  # Text-Aufgabe: Antwort-Gerüst (Lückentext/Tabelle) als Markdown
            "auto_grading_enabled": "BOOLEAN DEFAULT 1",  # Automatische LLM-Korrektur (Default: AN)
            # Upload-Aufgaben: Original-Dateinamen (Datei unter data/upload_tasks/{task_id}/)
            "upload_template": "VARCHAR",  # Template-Datei (Download für Studenten)
            "upload_solution_file": "VARCHAR",  # Musterlösungs-Datei (versteckt, LLM-Grading)
            "deleted_at": "DATETIME",  # Soft Delete (null = aktiv)
        },
        "submissions": {
            "workspace_snapshot": "VARCHAR",  # Pfad zu workspace.tar.gz
            "mc_result": "TEXT",  # MC: persistiertes Grading-Ergebnis (JSON)
            "upload_file": "VARCHAR",  # Upload-Aufgabe: Original-Dateiname (submissions/{id}/<name>)
        },
        "task_workspace_files": {
            "sort_order": "INTEGER",  # manuelle Dateireihenfolge je Ordner (NULL = alphabetisch)
            "access": "VARCHAR(16)",  # explizite Zugriffs-Klasse (NULL=erben; readonly/hidden)
        },
        "content_versions": {
            "file_snapshot": "VARCHAR",  # relativer Pfad des Workspace-Dateibau-Snapshots (nur Task-Versionen)
        },
        "workspace_runs": {
            "submission_id": "INTEGER",  # Grading-/Tutor-Rerun: Link zur Einreichung
        },
        "course_settings": {
            "compute_enabled": "BOOLEAN",  # None = globale Settings (Workspace-Aufgaben)
            "compute_gpu_enabled": "BOOLEAN",  # None = global (GPU-Passthrough)
            "compute_agents": "VARCHAR",  # JSON-Liste [{name,url,key,gpu}] (Override)
        },
        "course_materials": {
            "display_order": "INTEGER DEFAULT 0",  # Reihenfolge (mehrere Slide-Decks pro Kurs)
            "summary": "TEXT",  # Interne LLM-Zusammenfassung (nicht für Studenten; Slide-Decks)
            "deleted_at": "DATETIME",  # Soft Delete (null = aktiv)
        },
        "course_imports": {
            "slides_plan": "JSON NOT NULL DEFAULT '[]'",  # Folien-Plan (separat vom Skript-Plan)
            "references_status": "VARCHAR(20) DEFAULT 'pending'",  # Stufe: Quellen-Import
            "reference_map": "JSON NOT NULL DEFAULT '{}'",  # detektierte Quellen (BibTeX-Keys)
            "ref_extract_status": "VARCHAR(20) DEFAULT 'pending'",  # Stufe: LLM-Quellen-Extraktion
            "slides_plan_status": "VARCHAR(20) DEFAULT 'pending'",  # Stufe: Folien-Planner
            "tasks_plan": "JSON NOT NULL DEFAULT '[]'",  # Aufgaben-Plan (1 Eintrag = 1 Übungsaufgabe)
            "tasks_plan_status": "VARCHAR(20) DEFAULT 'pending'",  # Stufe: Aufgaben-Planner
            "tasks_status": "VARCHAR(20) DEFAULT 'pending'",  # Stufe: Aufgaben-Generierung
        },
        "course_media": {
            "content_hash": "VARCHAR(64)",  # SHA256 der Quelldatei (Medien-Import-Dedup)
            "deleted_at": "DATETIME",  # Soft Delete (null = aktiv)
        },
        "llm_debug_entries": {
            "user_id": "INTEGER",  # auslösender User (NULL = kein User-Kontext)
        },
        "notifications": {
            "channel_id": "INTEGER",  # Forum-Kanal (nur forum_message; Auto-Read beim Kanal-Besuch)
        },
        "mcp_tokens": {
            "allow_write": "BOOLEAN DEFAULT 1",  # PAT-Write-Schalter (Einstellungen → MCP)
        },
        "mcp_oauth_tokens": {
            # Replay-Toleranz für Refresh-Rotation (ChatGPT-400s, s. McpOAuthToken)
            "prev_refresh_hash": "VARCHAR(64)",
            "prev_refresh_valid_until": "DATETIME",
        },
    }
    column_drops = {
        "course_media": ["is_visible"],  # Sichtbarkeit steuert der einbindende Inhalt
        "course_imports": ["refine_status"],  # Stufe „Nachbesserung“ entfallen
        # YAML-Spec-Modell abgelöst durch Skripte + Simple-Felder (2026-09);
        # Migration: scripts/migrate_workspace_scripts.py (einmalig, gelaufen).
        # Artefakt-Sammlung entfernt (2026-09): Ergebnisdaten kommen per
        # Test-Output (test.sh/.test_private.sh echoen Metriken knapp).
        # Code-Aufgaben entfernt (2026-10): Vorlage/Tests durch Workspace ersetzt.
        "tasks": ["workspace_spec", "workspace_dataset", "workspace_artifacts",
                  "code_template", "test_code"],
        # is_public (Pfad-Konvention) abgelöst durch explizite access-Klassen;
        # Migration: scripts/migrate_workspace_access.py (einmalig).
        "task_workspace_files": ["is_public"],
    }
    # Spalten-Umbenennungen (idempotent): Tabelle -> {alter Name: neuer Name}
    column_renames = {
        # Code-Aufgaben entfernt (2026-10): Spalte speichert nur noch die
        # MC-Antworten (mc:v1:<json>-Payload).
        "submissions": {"code_solution": "mc_answers"},
    }
    is_sqlite = "sqlite" in DATABASE_URL

    with engine.begin() as conn:
        # Unique-Constraint (max. ein Material pro Kurs & Typ) ist obsolet:
        # Pro Kurs dürfen mehrere Slide-Decks existieren (Skripte sind
        # Kapitel und liegen in course_script_sections).
        if is_sqlite:
            # In SQLite ist das Constraint Teil des CREATE-TABLE-DDLs
            # (UNIQUE-Table-Constraint, realisiert als Auto-Index) und kann
            # nicht per DROP INDEX entfernt werden → Tabelle ohne das
            # Constraint neu aufbauen, Daten übernehmen, alte Tabelle ersetzen.
            row = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='course_materials'"
            ).fetchone()
            if row and re.search(
                r"CONSTRAINT\s+uq_material_course_type\s+UNIQUE", row[0], re.IGNORECASE
            ):
                new_ddl = re.sub(
                    r"^\s*CONSTRAINT\s+uq_material_course_type\s+UNIQUE"
                    r"\s*\(course_id\s*,\s*material_type\)\s*,?\s*$",
                    "",
                    row[0],
                    count=1,
                    flags=re.IGNORECASE | re.MULTILINE,
                )
                tmp = "course_materials_migration_tmp"
                conn.exec_driver_sql(
                    new_ddl.replace("course_materials", tmp, 1)
                )
                conn.exec_driver_sql(
                    f"INSERT INTO {tmp} SELECT * FROM course_materials"
                )
                conn.exec_driver_sql("DROP TABLE course_materials")
                conn.exec_driver_sql(f"ALTER TABLE {tmp} RENAME TO course_materials")
                # Sekundärindex wird mit der alten Tabelle gelöscht → neu anlegen
                conn.exec_driver_sql(
                    "CREATE INDEX ix_course_materials_course_id"
                    " ON course_materials (course_id)"
                )
        else:
            conn.exec_driver_sql(
                "ALTER TABLE course_materials DROP CONSTRAINT IF EXISTS uq_material_course_type"
            )

        for table, new_columns in column_migrations.items():
            if is_sqlite:
                rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
                if not rows:
                    continue
                existing = {row[1] for row in rows}
                for col, col_type in new_columns.items():
                    if col not in existing:
                        conn.exec_driver_sql(
                            f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
                        )
            else:
                for col, col_type in new_columns.items():
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}"
                    )

        for table, dropped in column_drops.items():
            if is_sqlite:
                rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
                if not rows:
                    continue
                existing = {row[1] for row in rows}
                for col in dropped:
                    if col in existing:
                        _sqlite_drop_column(conn, table, col)
            else:
                for col in dropped:
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table} DROP COLUMN IF EXISTS {col}"
                    )

        for table, renames in column_renames.items():
            if is_sqlite:
                # SQLite >= 3.25: ALTER TABLE ... RENAME COLUMN
                rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
                if not rows:
                    continue
                existing = {row[1] for row in rows}
                for old_name, new_name in renames.items():
                    if old_name in existing and new_name not in existing:
                        conn.exec_driver_sql(
                            f"ALTER TABLE {table} RENAME COLUMN {old_name} TO {new_name}"
                        )
            else:
                for old_name, new_name in renames.items():
                    row = conn.exec_driver_sql(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = :t AND column_name = :c",
                        {"t": table, "c": old_name},
                    ).fetchone()
                    if row:
                        conn.exec_driver_sql(
                            f"ALTER TABLE {table} RENAME COLUMN {old_name} TO {new_name}"
                        )

    # Eigene Connection: PRAGMA foreign_keys greift nur außerhalb einer
    # Transaktion (s. Funktion).
    _migrate_courses_autoincrement()


def _migrate_courses_autoincrement() -> None:
    """Einmalig: courses-Table ohne ID-Wiederverwendung aufbauen (SQLite).

    Ohne AUTOINCREMENT setzt SQLite die nächste Kurs-ID auf die zuletzt
    gelöschte Rowid — verwaiste Restdaten des gelöschten Kurses würden
    dann stillschweigend zum neuen Kurs „gehören“ (Vorfall 2026-10-20:
    Testkurs 37 gelöscht → sein Kapitel + Studierende-Views landeten im
    neuen Kurs 37 „VRVis Demo“). AUTOINCREMENT macht ID-Wiederverwendung
    unmöglich; die Migration läuft mit FKs OFF und eigener Connection,
    weil PRAGMA foreign_keys innerhalb einer Transaktion wirkungslos ist.
    """
    if "sqlite" not in DATABASE_URL:
        return
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        row = cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='courses'"
        ).fetchone()
        if not row or "AUTOINCREMENT" in (row[0] or "").upper():
            return
        cur.execute("PRAGMA foreign_keys=OFF")
        ddl = row[0]
        ddl = ddl.replace(
            "id INTEGER NOT NULL,",
            "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,",
        )
        ddl = re.sub(r"^[ \t]*PRIMARY KEY \(id\),[ \t]*\n", "", ddl, flags=re.MULTILINE)
        tmp = "courses_autoincrement_tmp"
        ddl = ddl.replace("CREATE TABLE courses", f"CREATE TABLE {tmp}", 1)
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(ddl)
            cur.execute(f"INSERT INTO {tmp} SELECT * FROM courses")
            cur.execute("DROP TABLE courses")
            cur.execute(f"ALTER TABLE {tmp} RENAME TO courses")
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        cur.execute("PRAGMA foreign_keys=ON")
    finally:
        raw.close()


def get_session():
    """Yield a DB session. Use as FastAPI dependency: `Depends(get_session)`"""
    with Session(engine) as session:
        yield session


# Soft-Delete-Read-Filter registrieren (muss vor den ersten Queries laufen)
from database import soft_delete  # noqa: E402,F401