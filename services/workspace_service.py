"""
Workspace-Service: Orchestrierung zwischen AICampus (DB, Disk) und den
Compute-Agenten.

Verantwortlichkeiten:
- Agent-Auswahl (effektive Compute-Config: Kurs > Global > .env, GPU-Routing, Health-Cache)
- Zugriffsklassen (explizit pro Datei/Ordner, Erbung = restriktivste):
    ✏️ edit      → public, editierbar (im Student-Volume /workspace)
    🔒 readonly  → public, read-only (shared auf dem Agenten, ro-Mount)
    👤 hidden    → privat (nur bei Grading injiziert, Student sieht nie)
- ensure_workspace (Client + Starter-Dateien von der Disk)
- Asset-Sync & Task-Image-Build via .init.sh/.init_hidden.sh (Task-Save-Seiteneffekte)
- Snapshots (Abgabe) + Lauf-Historie (WorkspaceRun)
"""

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
import zlib
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from compute_agent.version import agent_version
from config import (
    SUBMISSION_DIR,
    VERSION_SNAPSHOT_DIR,
    WORKSPACE_BACKUP_DIR,
    WORKSPACE_BACKUP_RETENTION_DAYS,
    WORKSPACE_DIR,
)
from database.base import engine
from database.models import (
    ContentVersion,
    Course,
    Task,
    TaskWorkspaceFile,
    TaskWorkspaceFolder,
    TaskWorkspaceOrder,
    User,
    WorkspaceRun,
    WorkspaceRunStatus,
)
from services.compute_client import (
    ComputeAgentError,
    ComputeAgentUnavailable,
    ComputeClient,
)

MAX_LOG_CHARS = 1_000_000       # stdout/stderr-Truncation (WorkspaceRun)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Sleep-Backups (Workspace-Volumes, die beim Idle-Kill auf den Server
# gewandert sind; Upload: api/agent_api.workspace_backup,
# Restore: ensure_workspace → client.restore_volume)
# ═══════════════════════════════════════════════════════════════════

def workspace_backup_tar(key: str) -> Path:
    return WORKSPACE_BACKUP_DIR / f"{key}.tar.gz"


def delete_workspace_backup(key: str) -> None:
    """Sleep-Backup (+Meta) eines Keys entfernen (Workspace-Reset,
    Task-Delete). Fehlende Dateien sind ok."""
    tar = workspace_backup_tar(key)
    try:
        tar.unlink(missing_ok=True)
        tar.with_name(tar.name.replace(".tar.gz", ".json")).unlink(missing_ok=True)
    except OSError:
        pass


def delete_task_backups(course_id: int, task_id: int) -> None:
    """Alle Sleep-Backups einer Aufgabe (alle Students)."""
    pattern = f"ws-{course_id}-{task_id}-"
    for tar in WORKSPACE_BACKUP_DIR.glob(f"{pattern}*.tar.gz"):
        delete_workspace_backup(tar.name[:-len(".tar.gz")])


def prune_workspace_backups(retention_days: Optional[int] = None) -> int:
    """Backups löschen, die älter als Retention sind (Default 30 Tage
    nach letzter Aktivität; Meta-`at` bzw. Datei-MTime als Stempel)."""
    if retention_days is None:
        retention_days = WORKSPACE_BACKUP_RETENTION_DAYS
    cutoff = time.time() - retention_days * 86400
    n = 0
    for tar in WORKSPACE_BACKUP_DIR.glob("ws-*.tar.gz"):
        at: Optional[float] = None
        meta = tar.with_name(tar.name.replace(".tar.gz", ".json"))
        if meta.exists():
            try:
                at = float(json.loads(meta.read_text()).get("at") or 0) or None
            except (OSError, ValueError):
                at = None
        if at is None:
            try:
                at = tar.stat().st_mtime
            except OSError:
                continue
        if at < cutoff:
            delete_workspace_backup(tar.name[:-len(".tar.gz")])
            n += 1
    return n


def prune_backup_loop(interval: float = 6 * 3600) -> None:
    """Retention-Thread (Start in main.lifespan): alle 6 h einmal prüfen."""
    while True:
        try:
            n = prune_workspace_backups()
            if n:
                logger.info("sleep-backup Retention: %d Backup(s) gelöscht", n)
        except Exception as e:  # noqa: BLE001
            logger.warning("sleep-backup Retention fehlgeschlagen: %s", e)
        time.sleep(interval)
MAX_LLM_FILE_CHARS = 100_000    # je Datei im LLM-Grading-Kontext
MAX_LLM_TOTAL_CHARS = 400_000   # gesamt im LLM-Grading-Kontext
HEALTH_TTL = 30                 # Health-Cache (Sekunden)

# Dateilimits
MAX_FILE_BYTES = 50 * 1024 * 1024            # 50 MB pro Datei
MAX_WORKSPACE_TOTAL_BYTES = 512 * 1024 * 1024  # 512 MB je Aufgaben-Workspace


# ═══════════════════════════════════════════════════════════════════
# Task-Validierung & Sync-Scheduling
# ═══════════════════════════════════════════════════════════════════
# Diese Helfer stehen hier (statt in api/tutor.py), damit auch
# version_service.py (Versions-Wiederherstellung) sie ohne API-Import
# wiederverwenden kann. api/tutor.py alias-Importiert sie.

_WS_MEM_RE = re.compile(r"^\d+(\.\d+)?[bkmg]$", re.IGNORECASE)


def apply_workspace_env_fields(task: Task, body: dict) -> None:
    """Workspace-Umgebungsfelder validieren & anwenden (nur explizit
    übergebene Keys). HTTPException 400 bei ungültigen Werten."""
    if "workspace_timeout" in body and body["workspace_timeout"] is not None:
        try:
            t = int(body["workspace_timeout"])
        except (TypeError, ValueError):
            raise HTTPException(400, "workspace_timeout muss eine Zahl sein.")
        if not (1 <= t <= 7200):
            raise HTTPException(400, "workspace_timeout muss zwischen 1 und 7200 liegen.")
        task.workspace_timeout = t
    if "workspace_cpu" in body and body["workspace_cpu"] is not None:
        try:
            c = float(body["workspace_cpu"])
        except (TypeError, ValueError):
            raise HTTPException(400, "workspace_cpu muss eine Zahl sein.")
        if not (0 < c <= 32):
            raise HTTPException(400, "workspace_cpu muss größer 0 (max. 32) sein.")
        task.workspace_cpu = c
    if "workspace_memory" in body:
        m = str(body["workspace_memory"] or "4g").strip().lower().replace(" ", "")
        if re.match(r"^\d+(\.\d+)?$", m):
            m += "g"  # UI/LLM senden GB-Zahl (4 → "4g")
        if not _WS_MEM_RE.match(m):
            raise HTTPException(400, "workspace_memory: Zahl in GB (z. B. 4) oder String mit Einheit (z. B. '512m').")
        task.workspace_memory = m
    if "workspace_disk_quota" in body and body["workspace_disk_quota"] is not None:
        try:
            d = float(body["workspace_disk_quota"])
        except (TypeError, ValueError):
            raise HTTPException(400, "workspace_disk_quota muss eine Zahl sein.")
        if not (0 <= d <= 100):
            raise HTTPException(400, "workspace_disk_quota muss zwischen 0 und 100 GB liegen (0 = ohne Limit).")
        task.workspace_disk_quota = d
    if "workspace_internet" in body:
        task.workspace_internet = bool(body["workspace_internet"])
    if "workspace_main_file" in body:
        mf = body["workspace_main_file"]
        if mf is not None:
            mf = str(mf).strip()
            if mf and (mf.startswith("/") or ".." in mf.split("/")):
                raise HTTPException(400, "workspace_main_file muss ein relativer Pfad sein (kein ..).")
            task.workspace_main_file = mf or None
        else:
            task.workspace_main_file = None


def parse_engine_list(val) -> str | None:
    """Engine-Liste (UI) → JSON-String (leere Liste → None)."""
    if val in (None, "", []):
        return None
    if not isinstance(val, list):
        raise HTTPException(400, "workspace_engines muss eine Liste sein")
    names = [str(n).strip() for n in val if str(n).strip()]
    return json.dumps(names) if names else None


def check_image_spec(session: Session, course_id: int, name: str | None) -> str | None:
    """workspace_image validieren (Spec muss global oder Kurs-Scope existieren)."""
    name = str(name or "").strip()
    if name:
        from services import image_spec_service
        if image_spec_service.resolve_spec(session, course_id, name) is None:
            raise HTTPException(
                400, f"Image-Spec {name!r} existiert nicht (global oder Kurs)")
    return name or None


def validate_workspace_provided(session: Session, task: Task) -> None:
    """Validiert NUR die gesetzten Workspace-Werte: Image-Spec muss
    auflösbar sein, aufgeführte Engines müssen registriert sein
    (HTTPException 400 bei Tippfehlern).

    Fehlendes Image/Engine ist erlaubt: mit Auto-Save wird die Aufgabe
    direkt gespeichert, bevor der Tutor Arbeitsumgebung gewählt hat.
    Der Agent-Sync startet automatisch mit dem ersten lauffähigen Save
    (Readiness-Gate in schedule_task_sync); bis dahin zeigen UI und
    Studenten einen Graceful-Hinweis (validate_task_ready).
    """
    if task.task_type.value != "workspace":
        return
    if task.workspace_image:
        from services import image_spec_service
        if image_spec_service.resolve_spec(
                session, task.course_id, task.workspace_image) is None:
            raise HTTPException(
                400, f"Workspace-Aufgabe: Image-Spec {task.workspace_image!r} "
                     "existiert nicht (global oder Kurs).")
    names = workspace_service.task_engine_names(task)
    if names:
        known = {a["name"] for a in
                 workspace_service.get_agents(session, task.course_id)}
        missing = [n for n in names if n not in known]
        if missing:
            raise HTTPException(
                400, f"Workspace-Aufgabe: Compute-Engine(s) nicht registriert: "
                     f"{', '.join(missing)}.")


# Starke Referenzen auf laufende Workspace-Sync-Threads (GC-Schutz)
_task_sync_threads: list[threading.Thread] = []
# Coalescing: bei mehreren File-Änderungen in kurzer Zeit (z. B. Ordner-
# Umbenennen) läuft nur ein Sync; nach Abschluss wird ggf. erneut angestoßen.
_task_sync_inflight: set[int] = set()
_task_sync_retry: set[int] = set()


def schedule_task_sync(task_id: int, force: bool = False) -> None:
    """Task-Save-Seiteneffekte (Asset-Sync + Init-Build) im Hintergrund —
    eigener Session-Thread, damit die Antwort nicht auf den Agent wartet.
    Starke Thread-Referenzen, damit nichts weg-GC'd wird.

    Readiness-Gate: Ist die Aufgabe noch nicht lauffähig (Image-Spec /
    Engine-Pool fehlt — möglich, weil Auto-Save vor der Wahl der
    Arbeitsumgebung speichert), wird der Sync übersprungen. Der erste
    lauffähige Save (Tutor wählt Image + Engine → Auto-Save) startet ihn
    dann automatisch.

    force=True (manuelles „Sync & Init“): Init-Build wird erzwungen —
    .init.sh läuft komplett neu, die alten Init-Artefakte werden zuerst
    gelöscht und dann neu erzeugt (Manifest = exakt dieses Ergebnis)."""

    def _work() -> None:
        try:
            with Session(engine) as bg_session:
                task = bg_session.get(Task, task_id)
                if task and task.task_type.value == "workspace":
                    not_ready = workspace_service.validate_task_ready(bg_session, task)
                    if not_ready:
                        logger.info(
                            "Workspace-Sync (task %s) übersprungen (noch nicht "
                            "lauffähig): %s", task_id, not_ready)
                        return
                    # „pending"-Marker (Objekt-Format wie on_task_saved):
                    # Die UI kann „lädt …“ pollen, bis der Sync fertig ist.
                    if workspace_service.is_enabled(bg_session, task.course_id):
                        try:
                            # Derselbe Agent-Satz wie on_task_saved (Engine-Pool
                            # der Aufgabe, nicht ALLE Kurs-Agents) — sonst zeigt
                            # die UI beim Pending-Poll Engines an, auf denen
                            # gar nichts gebaut wird.
                            task.workspace_assets_status = json.dumps({
                                a["url"]: {"assets": "pending",
                                           "task_image": "pending",
                                           "image": "pending"}
                                for a in workspace_service.target_agents(bg_session, task)
                            })
                            bg_session.add(task)
                            bg_session.commit()
                        except Exception:  # noqa: BLE001
                            pass
                    try:
                        workspace_service.on_task_saved(
                            bg_session, task, force=force)
                    except Exception as e:  # noqa: BLE001 — Status liegt in workspace_assets_status
                        logger.warning("Workspace-Sync (task %s) fehlgeschlagen: %s", task_id, e)
        finally:
            _task_sync_inflight.discard(task_id)
            if _task_sync_retry.discard(task_id):
                schedule_task_sync(task_id)

    if task_id in _task_sync_inflight:
        _task_sync_retry.add(task_id)
        return
    _task_sync_inflight.add(task_id)
    t = threading.Thread(target=_work, daemon=True)
    _task_sync_threads.append(t)
    t.start()


# ── Pfad-Konvention (einzige Stelle, die sie ableitet) ─────────────

def workspace_key(course_id: int, task_id: int, student_id: int) -> str:
    return f"ws-{course_id}-{task_id}-{student_id}"


def test_run_key(task: Task) -> str:
    """Einweg-Workspace für den Tutor-Testlauf (student_id=0 reserviert,
    kollidiert mit keinen echten User-/Submission-IDs)."""
    return workspace_key(task.course_id, task.id, 0)


def ensure_fresh_workspace(client: ComputeClient, key: str, spec: dict,
                           starter_files: list[dict] | None,
                           folders: list[str] | None = None) -> dict:
    """Einweg-Workspace (Testlauf, Grading, Rerun) mit GARANTIERT frischem
    Volume anlegen.

    ``remove_workspace`` auf dem Agenten schluckt Docker-Fehler
    (check=False): ein überlebendes Volume/Container lässt
    ``workspace_create`` die Starter-Dateien stillschweigend überspringen
    (fresh=False) — statt dem aktuellen Task-Stand würde der Zustand des
    vorherigen Laufs ausgeführt (z. B. gelöschte 👤/✏️-Dateien fehlen
    dann). In dem Fall einmal löschen + neu anlegen; immer noch nicht
    frisch → klares Fehlerbild statt stummer Alt-Daten.
    """
    try:
        client.delete_workspace(key)
    except ComputeAgentError:
        pass  # war schon weg
    resp = client.create_workspace(key, spec,
                                   starter_files=starter_files or None,
                                   folders=folders or None)
    if (starter_files or folders) and not resp.get("fresh"):
        client.delete_workspace(key)
        resp = client.create_workspace(key, spec,
                                       starter_files=starter_files or None,
                                       folders=folders or None)
        if not resp.get("fresh"):
            raise ComputeAgentError(
                "Workspace konnte nicht frisch angelegt werden (altes "
                "Volume blockiert noch den Container) — bitte erneut "
                "versuchen.", 502)
    return resp


# ── Zugriffsklassen (explizit pro Datei/Ordner) ──────────────────
# Effektive Klasse = restriktivste von (eigene explizite, alle Ordner-
# Vorfahren). Restriktivität: hidden > readonly > edit.
ACCESS_VALUES = ("edit", "readonly", "hidden")
ACCESS_RANK = {"edit": 0, "readonly": 1, "hidden": 2}
_ACCESS_BY_RANK = ("edit", "readonly", "hidden")

# Skript-Konventionen (feste Wurzelpfade, weiche Punkt-Konvention:
# 👤-Skripte beginnen mit "."):
RUN_SCRIPT = "run.sh"
INIT_SCRIPT = ".init.sh"                    # 👤 (versteckt); Source: .private/, per Init-Request
INIT_PRIVATE_SCRIPT = ".init_hidden.sh"
TEST_SCRIPT = "test.sh"
JUDGE_SCRIPT = ".test_private.sh"           # 👤-Datei; Judge des Grading-Laufs
TEST_SOLUTION_SCRIPT = ".test_solution.sh"   # 👤-Datei; Tutor-Testlauf


def task_workspace_dir(task_id: int) -> Path:
    return WORKSPACE_DIR / str(task_id)


def file_disk_path(task_id: int, path: str) -> Path:
    """Disk-Pfad einer TaskWorkspaceFile (alle Klassen unter dem Task-Dir;
    die Zugriffs-Klasse lebt in der DB, nicht im Pfad)."""
    base = task_workspace_dir(task_id)
    p = str(path).replace("\\", "/").lstrip("/")
    return base / p


def _ancestor_rank(path: str, folder_map: dict[str, str]) -> int:
    """Max-Rank über alle Ordner-Vorfahren von ``path``."""
    rank = 0
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    while parent:
        rank = max(rank, ACCESS_RANK.get(folder_map.get(parent, "edit"), 0))
        parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
    return rank


def effective_file_access(path: str, file_access: Optional[str],
                          folder_map: dict[str, str]) -> str:
    """Effektive Zugriffs-Klasse einer Datei (eigene + Ordner-Vorfahren)."""
    own = ACCESS_RANK.get(file_access or "edit", 0)
    return _ACCESS_BY_RANK[max(own, _ancestor_rank(path, folder_map))]


def effective_folder_access(path: str, folder_map: dict[str, str]) -> str:
    """Effektive Zugriffs-Klasse eines Ordners (eigene Zeile + Vorfahren)."""
    own = ACCESS_RANK.get(folder_map.get(path, "edit"), 0)
    return _ACCESS_BY_RANK[max(own, _ancestor_rank(path, folder_map))]


def _top_level(paths: set[str]) -> list[str]:
    """Pfade ohne echten Präfix innerhalb der Menge (Top-Level für Mounts)."""
    return sorted(
        p for p in paths
        if not any(p.startswith(q + "/") for q in paths if q != p)
    )


def readonly_mount_paths(files: list[TaskWorkspaceFile],
                         folder_map: dict[str, str]) -> list[str]:
    """Top-Level-🔒-Pfade (Dateien + Ordner) für die ro-Mounts des
    Student-Containers (geteilt auf dem Agenten)."""
    ro_files = {
        f.path for f in files
        if effective_file_access(f.path, f.access, folder_map) == "readonly"
    }
    ro_folders = {
        p for p in folder_map
        if effective_folder_access(p, folder_map) == "readonly"
    }
    return _top_level(ro_files | ro_folders)


def hidden_mount_paths(files: list[TaskWorkspaceFile],
                       folder_map: dict[str, str]) -> list[str]:
    """Top-Level-👤-Pfade (Dateien + Ordner) für die rw-Mounts der
    Init-Phase 2 (private Agenten-Region) & Grading-Injection."""
    hid_files = {
        f.path for f in files
        if effective_file_access(f.path, f.access, folder_map) == "hidden"
    }
    hid_folders = {
        p for p in folder_map
        if effective_folder_access(p, folder_map) == "hidden"
    }
    return _top_level(hid_files | hid_folders)


# ── System-Skripte mit fester Zugriffs-Klasse ────────────────────
# Diese Dateien regeln die Systematik der Aufgabe (Run-/Test-/Init-
# Skripte). Ihre Klasse ist FEST (nicht pro Datei änderbar):
#   🔒 read-only — Studenten dürfen sie nicht überschreiben;
#   👤 versteckt — Studenten sehen sie nie (private Init-, Judge- und
#     Musterlösung-Testlauf-Skripte).
# Alle sechs stehen IMMER in der Wurzel (weiche Punkt-Konvention: 👤-Skripte
# beginnen mit ".") — private Skripte dürfen nie studentensichtbar werden.
SYSTEM_FILE_ACCESS = {
    "run.sh": "readonly",
    ".init.sh": "hidden",
    "test.sh": "readonly",
    ".init_hidden.sh": "hidden",
    ".test_private.sh": "hidden",
    ".test_solution.sh": "hidden",
}

# Stubs: werden per ensure_system_stubs angelegt, wenn der Datei-Baum
# beim Task-Save noch leer ist (Selbstheilung) — damit der Tutor die
# Skript-Namen nicht auswendig kennen muss.
SYSTEM_STUBS: dict[str, str] = {
    "run.sh": """#!/bin/bash
# Startbefehl der Aufgabe (Button „▶ Ausführen“ im Student-Container).
# Read-only für Studenten (🔒) — passe ihn hier an.
echo "Noch nicht konfiguriert: rufe hier den Startbefehl der Aufgabe auf (z. B. python main.py)."
""",
    ".init.sh": """#!/bin/bash
# Initialisierung (Phase 1): läuft bei jedem Task-Image-Build und lädt/
# erzeugt die editierbaren (✏️) und read-only (🔒) Dateien der Studenten.
# Für Studenten versteckt (👤) — sie sehen nur das Ergebnis.
# Darf NICHT in versteckte (👤) Pfade schreiben — dafür .init_hidden.sh.
exit 0
""",
    ".init_hidden.sh": """#!/bin/bash
# Private Initialisierung (Phase 2, nach .init.sh): darf in versteckte (👤)
# Pfade schreiben (z. B. .solution/, private Testdaten).
# Für Studenten nie sichtbar.
exit 0
""",
    "test.sh": """#!/bin/bash
# Öffentliche Tests: laufen im Student-Container gegen die
# Studenten-Antwort (Button „🧪 Test“). Read-only für Studenten (🔒).
echo "Noch keine öffentlichen Tests."
exit 0
""",
    ".test_private.sh": """#!/bin/bash
# Private Tests (Judge): laufen bei der Korrektur gegen die
# Studenten-Antwort. Für Studenten nie sichtbar (👤).
echo "Noch keine privaten Tests."
exit 0
""",
    ".test_solution.sh": """#!/bin/bash
# Tutor-Testlauf (Button „🧪 Musterlösung testen“): legt die Musterlösung
# aus .solution/ über die editierbaren Dateien und prüft sie gegen die
# öffentlichen + privaten Tests. Fehlende Skripte werden übersprungen;
# der Exit-Code des ersten Fehlers zählt. Reihenfolge/Selektion der
# Skripte ist frei anpassbar (z. B. mehrere Run-Skripte).
cd /workspace
rc=0

if [ -d .solution ]; then
  echo "── Musterlösung einbetten (.solution/) ──"
  cp -rf .solution/. ./
else
  echo "Keine Lösung spezifiziert"
  exit 0
fi

if [ -f run.sh ]; then
  echo "── run.sh ──"
  bash run.sh || rc=$?
fi
if [ -f test.sh ] && [ "$rc" -eq 0 ]; then
  echo "── test.sh ──"
  bash test.sh || rc=$?
fi
if [ -f .test_private.sh ] && [ "$rc" -eq 0 ]; then
  echo "── .test_private.sh ──"
  bash .test_private.sh || rc=$?
fi
exit $rc
""",
}


def system_file_access(path: str) -> Optional[str]:
    """Feste Zugriffs-Klasse einer System-Datei (None = keine)."""
    p = str(path).replace("\\", "/").lstrip("/")
    return SYSTEM_FILE_ACCESS.get(p)


def snapshot_abs_path(snapshot_rel: str) -> Path:
    """Relativer Pfad (submissions/{id}/workspace.tar.gz) → absolut."""
    return WORKSPACE_DIR.parent / snapshot_rel


def snapshot_list_files(snapshot_rel: str) -> list[dict]:
    """Dateiliste eines Workspace-Snapshots (read-only, z. B. Abgabe-Ansicht).

    Liefert [{path, size, is_binary}]. Namens-Normalisierung streicht nur
    einen exakten „./“-Prefix — NICHT lstrip("./"), das würde führende
    Dots wegnehmen (.gitignore → gitignore).
    """
    p = snapshot_abs_path(snapshot_rel)
    if not p.is_file():
        raise HTTPException(404, "Snapshot nicht gefunden.")
    files = []
    with tarfile.open(p, "r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            name = m.name.removeprefix("./")
            name = name.replace("\\", "/").lstrip("/")
            if not name or any(x == ".." for x in name.split("/")):
                continue
            is_binary = False
            try:
                f = tar.extractfile(m)
                if f is not None:
                    is_binary = b"\x00" in f.read(1024)
            except (OSError, EOFError):
                pass
            files.append({"path": name, "size": m.size, "is_binary": is_binary})
    return files


def snapshot_read_file(snapshot_rel: str, path: str) -> Optional[bytes]:
    """Inhalt einer Datei aus dem Snapshot (None = nicht enthalten)."""
    p = snapshot_abs_path(snapshot_rel)
    if not p.is_file():
        raise HTTPException(404, "Snapshot nicht gefunden.")
    with tarfile.open(p, "r:gz") as tar:
        member = None
        for candidate in (path, "./" + path):
            try:
                member = tar.getmember(candidate)
                break
            except KeyError:
                continue
        if member is None or not member.isfile():
            return None
        f = tar.extractfile(member)
        if f is None:
            return None
        return f.read()


# ── Task-Image (.init.sh-Build, 1× je (Task, init-Hash)) ─────────

def task_has_init(task: Task) -> bool:
    """Task hat Task-Image-Build nötig (.init.sh und/oder .init_hidden.sh)."""
    return (file_disk_path(task.id, INIT_SCRIPT).is_file()
            or file_disk_path(task.id, INIT_PRIVATE_SCRIPT).is_file())


def task_init_hash(session: Session, task: Task, base_image: str) -> str:
    """Identität des Task-Images:
    sha256(base_image + .init.sh + .init_hidden.sh + sortierte Access-Map)[:12].

    Skript-Änderung ODER Access-Layout-Änderung (Dateien/Ordner-Klassen)
    → neuer Hash → neuer Init-Build. Der Agent validiert nur das Format;
    die Ref-Form ist identisch (docker_ops.task_image_ref).
    """
    init_p = file_disk_path(task.id, INIT_SCRIPT)
    priv_p = file_disk_path(task.id, INIT_PRIVATE_SCRIPT)
    fm = WorkspaceService.folder_map(session, task)
    access_map = {
        f.path: effective_file_access(f.path, f.access, fm)
        for f in WorkspaceService.task_files(session, task)
    }
    for p, a in fm.items():
        access_map[f"folder:{p}"] = a
    h = hashlib.sha256()
    h.update(base_image.encode())
    h.update(b"\n")
    h.update(init_p.read_bytes() if init_p.is_file() else b"")
    h.update(b"\n")
    h.update(priv_p.read_bytes() if priv_p.is_file() else b"")
    h.update(b"\n")
    h.update(json.dumps(access_map, sort_keys=True, ensure_ascii=False).encode())
    return h.hexdigest()[:12]


def task_image_ref(course_id: int, task_id: int, init_hash: str) -> str:
    return f"aicampus/task/{course_id}-{task_id}:{init_hash}"


# ── Health-Cache (module-level, pro Agent-URL) ──────────────────────
# (Zeitstempel, ok, Fehlermeldung, volles Health-Dict)

_HEALTH_CACHE: dict[str, tuple[float, bool, str, dict]] = {}
_HEALTH_LOCK = threading.Lock()


class WorkspaceService:
    # ═══════════════════════════════════════════════════════════
    # Agenten
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def is_enabled(session: Session, course_id: Optional[int] = None) -> bool:
        """Feature-Flag: effektive Compute-Config (Kurs-Override > Global > .env)."""
        from services.settings_resolver import get_effective_compute_config
        return get_effective_compute_config(session, course_id)["enabled"]

    @staticmethod
    def _normalize_agent(a: dict) -> dict:
        return {
            "name": str(a.get("name") or a["url"]),
            "url": str(a["url"]).rstrip("/"),
            "key": str(a.get("key") or ""),
            # GPU-Regel der Engine (JSON): None/"all" = alle, "none", [0, 2]
            "gpus": a.get("gpus"),
            # Herkunftsschicht für die UI (read-only-Anzeige):
            # "global" | "course" | "env"
            "scope": str(a.get("scope") or "global"),
        }

    @staticmethod
    def get_agents(session: Session, course_id: Optional[int] = None) -> list[dict]:
        """Agent-Registry: effektive Compute-Config (Kurs-Override > Global > .env)."""
        from services.settings_resolver import get_effective_compute_config
        return [
            WorkspaceService._normalize_agent(a)
            for a in get_effective_compute_config(session, course_id)["agents"]
        ]

    @staticmethod
    def _agent_healthy(agent: dict) -> tuple[bool, str]:
        url = agent["url"]
        with _HEALTH_LOCK:
            cached = _HEALTH_CACHE.get(url)
        if cached and time.time() - cached[0] < HEALTH_TTL:
            return cached[1], cached[2]
        try:
            client = ComputeClient(url=url, key=agent.get("key") or None)
            info = client.health(timeout=5.0)
            ok = bool(info.get("docker"))
            result = (time.time(), ok, "", info if ok else {})
        except ComputeAgentError as e:
            result = (time.time(), False, e.message[:200], {})
        with _HEALTH_LOCK:
            _HEALTH_CACHE[url] = result
        return result[1], result[2]

    @staticmethod
    def _agent_health(agent: dict) -> Optional[dict]:
        """Volles Health-Dict (gpu, gpu_info, …) aus dem Cache;
        None = Agent nicht gesund/erreichbar."""
        ok, _err = WorkspaceService._agent_healthy(agent)
        if not ok:
            return None
        with _HEALTH_LOCK:
            cached = _HEALTH_CACHE.get(agent["url"])
        return cached[3] if cached else None

    def pick_agent(self, session: Session,
                   course_id: Optional[int] = None,
                   engine_names: Optional[list[str]] = None) -> Optional[dict]:
        """Routing:
        - Engine-Pool: geordnete Liste (Task.workspace_engines) oder alle
          registrierten Engines (lokal bevorzugt, wenn kein Pool)
        - GPU-Regeln entscheidet die Engine (gpus-Setting), nicht das Task
        - Erste gesunde Engine des Pools — kein stilles Fallback.
        """
        agents = self.get_agents(session, course_id)
        if engine_names:
            by_name = {a["name"]: a for a in agents}
            candidates = [by_name[n] for n in engine_names if n in by_name]
        else:
            candidates = sorted(agents, key=lambda a: 0 if a["name"] == "local" else 1)
        for agent in candidates:
            if self._agent_health(agent) is None:
                continue
            return agent
        return None

    def pick_agent_error(self, session: Session,
                         course_id: Optional[int] = None,
                         engine_names: Optional[list[str]] = None) -> str:
        """UI-taugliche Erklärung, warum pick_agent None liefert."""
        pool_txt = ""
        if engine_names:
            pool_txt = f" Die Aufgabe benötigt die Engine(s) “{', '.join(engine_names)}”."
        agents = self.get_agents(session, course_id)
        if engine_names:
            by_name = {a["name"]: a for a in agents}
            candidates = [by_name[n] for n in engine_names if n in by_name]
        else:
            candidates = agents
        if not candidates:
            return "Keine Compute-Engine registriert." + pool_txt
        healthy = [a for a in candidates if self._agent_health(a)]
        if not healthy:
            return ("Compute-Server nicht erreichbar — Workspace derzeit "
                    "nicht verfügbar. Ausführung und Abgabe erst wieder "
                    "möglich, wenn er online ist." + pool_txt)
        return "Compute-Server nicht erreichbar."

    @staticmethod
    def client_for(agent: dict) -> ComputeClient:
        return ComputeClient(url=agent["url"], key=agent.get("key") or None)

    @staticmethod
    def _gpu_enabled(session: Session, course_id: Optional[int] = None) -> bool:
        from services.settings_resolver import get_effective_compute_config
        return get_effective_compute_config(session, course_id)["gpu_enabled"]

    def status(self, session: Session, course_id: Optional[int] = None) -> dict:
        """Für Admin-Konsole + UI-Banner (degradierter Modus).
        gpu/gpu_info kommen aus dem Health (detektiert) — das alte
        Registry-Flag wird überschrieben.

        version/version_current: Content-Hash des Agent-Quellcodes
        (compute_agent/version.py — dieselbe Funktion, die der Agent
        über /health meldet). Fehlt die Angabe (alte Agent-Builds
        ohne Version-Field), bleibt version_current False; die UI
        zeigt dann einfach kein Versions-Badge.
        """
        expected_version = agent_version()
        agents = []
        for a in self.get_agents(session, course_id):
            ok, err = self._agent_healthy(a)
            info = self._agent_health(a) or {}
            v = info.get("version") or ""
            agents.append({**a, "key": None, "healthy": ok, "error": err,
                           "gpu": bool(info.get("gpu")),
                           "gpu_info": info.get("gpu_info", ""),
                           "gpus": info.get("gpus") or [],
                           "version": v,
                           "version_current": bool(v) and v == expected_version})
        return {
            "enabled": self.is_enabled(session, course_id),
            "gpu_enabled": self._gpu_enabled(session, course_id),
            "agents": agents,
        }

    _WS_KEY_RE = re.compile(r"^ws-(\d+)-(\d+)-(\d+)$")
    # States, die NIE laufen (neuer Agent: sauberes {{.State}})
    _WS_STATE_NOT_RUNNING = {"exited", "created", "absent", "dead",
                             "paused", "restarting", "removing"}

    @classmethod
    def _ws_running(cls, state) -> bool:
        """Container läuft? Neuer Agent meldet {{.State}} (running/…);
        ÄLTERE Agent-Builds melden das letzte Wort von {{.Status}}
        („Up 5 minutes“ → „minutes“, „Exited (0) …“ → „ago“) — dort per
        Ausschluss nähern (bis alle Engines das neue Agent-Image haben).
        """
        s = str(state or "").strip().lower()
        if not s:
            return False
        if s in cls._WS_STATE_NOT_RUNNING:
            return False
        return not s.endswith("ago")

    def engine_sessions(self, session: Session,
                        course_id: Optional[int] = None) -> list[dict]:
        """Live-Sessions je Engine (aktiv + inaktiv, inkl. RAM/Disk) —
        für die Engine-Ansicht in Admin-Konsole/Kurs-Settings.

        course_id=None → alle Kurse (Admin); sonst nur Sessions des
        Kurses. Erreichbarkeit pro Engine aus dem Health-Cache
        (30 s); unerreichbare Engines liefern leer statt Fehler.
        """
        agents = self.get_agents(session, course_id)
        raw = []  # (agent_idx, entry, course_id, task_id, student_id)
        for idx, a in enumerate(agents):
            if self._agent_health(a) is None:
                continue
            try:
                entries = self.client_for(a).list_workspaces()
            except ComputeAgentError:
                continue
            for w in entries:
                m = self._WS_KEY_RE.match(str(w.get("key") or ""))
                if not m:
                    continue
                c, t, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if course_id is not None and c != course_id:
                    continue
                raw.append((idx, w, c, t, s))
        # Namen in je EINEM Batch-Query (statt N+1)
        task_ids = {t for _, _, _, t, _ in raw}
        user_ids = {s for _, _, _, _, s in raw}
        course_ids = {c for _, _, c, _, _ in raw}
        tasks = ({t.id: t for t in session.exec(
            select(Task).where(Task.id.in_(list(task_ids)))).all()}
            if task_ids else {})
        users = ({u.id: u for u in session.exec(
            select(User).where(User.id.in_(list(user_ids)))).all()}
            if user_ids else {})
        courses = ({c.id: c for c in session.exec(
            select(Course).where(Course.id.in_(list(course_ids)))).all()}
            if course_ids else {})
        out = []
        for idx, a in enumerate(agents):
            sessions = []
            for aidx, w, c, t, s in raw:
                if aidx != idx:
                    continue
                u = users.get(s)
                sessions.append({
                    "key": w.get("key"),
                    "active": self._ws_running(w.get("state")),
                    "image": w.get("image") or "",
                    "course": courses[c].name if c in courses else "",
                    "task": tasks[t].title if t in tasks else "",
                    # student_id=0 = Tutor-Testlauf (kein User)
                    "user": u.name if u else ("Tutor-Testlauf" if s == 0 else ""),
                    # Bytes (None = Agent-Build meldet es nicht / ungemessen)
                    "mem": w.get("mem"),
                    "disk": w.get("disk"),
                    "disk_quota_mb": w.get("disk_quota_mb"),
                    "last_activity": w.get("last_activity"),
                    # Sleep-Backup auf dem Server (Volume von der Engine weg)
                    "backed_up": bool(w.get("slept")),
                })
            out.append({"name": a["name"], "sessions": sessions})
        return out

    # ═══════════════════════════════════════════════════════════
    # Workspace des Studenten
    # ═══════════════════════════════════════════════════════════

    def task_engine_names(self, task: Task) -> Optional[list[str]]:
        from services import image_spec_service
        return image_spec_service.task_engine_names(task)

    def target_agents(self, session: Session, task: Task) -> list[dict]:
        """Agents, auf denen Task-Sync/Init-Build laufen: der Engine-Pool
        der Aufgabe (task.workspace_engines) — ohne Pool: alle registrierten
        Agents des Kurses. Wird von on_task_saved UND dem Pending-Marker
        (api/tutor.py) genutzt, damit beide denselben Agent-Satz zeigen."""
        agents = self.get_agents(session, task.course_id)
        names = self.task_engine_names(task)
        if names:
            by_name = {a["name"]: a for a in agents}
            agents = [by_name[n] for n in names if n in by_name]
        return agents

    @staticmethod
    def validate_task_ready(session: Session, task: Task) -> Optional[str]:
        """Prüft, ob die Aufgabe startbar ist (Image-Spec + Engine-Pool).

        Liefert None = ok, sonst eine UI-taugliche Fehlermeldung (Student:
        degraded-Banner; Tutor: Hinweis in der Task-Edit). Abstrakt —
        kein Agent-Health-Check (der läuft separat über pick_task_agent).
        """
        from services import image_spec_service
        if not task.workspace_image:
            return ("Für diese Aufgabe ist kein Image (Image-Spec) gesetzt. "
                    "Bitte in der Aufgabenbearbeitung eine Image-Spec wählen.")
        row = image_spec_service.resolve_spec(
            session, task.course_id, task.workspace_image)
        if row is None:
            return (f"Die Image-Spec {task.workspace_image!r} existiert nicht "
                    "mehr. Bitte in der Aufgabenbearbeitung ein anderes Image wählen.")
        names = workspace_service.task_engine_names(task)
        if not names:
            return ("Für diese Aufgabe ist keine Compute-Engine zugewiesen. "
                    "Bitte in der Aufgabenbearbeitung mindestens eine Engine wählen.")
        known = {a["name"] for a in workspace_service.get_agents(session, task.course_id)}
        missing = [n for n in names if n not in known]
        if missing:
            return ("Die zugewiesene Compute-Engine(s) existiert nicht mehr: "
                    f"{', '.join(missing)}. Bitte in der Aufgabenbearbeitung korrigieren.")
        return None

    def pick_task_agent(self, session: Session, task: Task) -> Optional[dict]:
        """Agent für eine konkrete Aufgabe (Engine-Pool; GPU-Regeln liegen
        bei der Engine, nicht beim Task)."""
        return self.pick_agent(session, task.course_id,
                               engine_names=self.task_engine_names(task))

    def ensure_workspace(self, session: Session, task: Task,
                         student_id: int,
                         starter_files: Optional[list[dict]] = None,
                         first_restore_tar: Optional[Path] = None,
                         hidden_folders: bool = False) -> dict:
        """Container sicherstellen (idempotent). Starter-Dateien (alle mit
        effektiver Klasse ✏️ edit) werden nur bei frischem Volume vom
        Agenten geschrieben.

        Review-Workspace: eigene `starter_files` übergeben (z. B. +👤 für
        den Tutor) und `first_restore_tar` (Submission-Snapshot) — der
        wird beim ERSTEN frischen Volume (kein Sleep-Backup vorhanden)
        darüber gespielt, danach gilt der normale Sleep-Backup-Flow.
        `hidden_folders`: 👤-Ordner in das frische Volume anlegen
        (nur mit +👤-Starter-Dateien sinnvoll).
        """
        agent = self.pick_task_agent(session, task)
        if agent is None:
            raise ComputeAgentUnavailable(
                self.pick_agent_error(
                    session, task.course_id, self.task_engine_names(task)))
        client = self.client_for(agent)
        key = workspace_key(task.course_id, task.id, student_id)
        starter = (starter_files if starter_files is not None
                   else self.starter_files(session, task))
        folders = self.materialize_folders(
            session, task, include_hidden=hidden_folders)
        spec = self.workspace_spec_for_agent(session, task, agent)
        result = client.create_workspace(key, spec,
                                          starter_files=starter or None,
                                          folders=folders or None)
        if result.get("fresh"):
            # Sleep-Backup (bestehender Stand) gewinnt; sonst (Erststart)
            # der First-Restore (Review: der Submission-Snapshot).
            result["restored"] = self._restore_backup_if_any(client, key)
            if not result["restored"] and first_restore_tar \
                    and first_restore_tar.is_file():
                self._restore_tar_to_workspace(
                    client, key, first_restore_tar,
                    "die eingereichte Lösung ist unverändert erhalten")
                result["restored"] = True
        result["agent"] = agent["name"]
        return result

    @staticmethod
    def _restore_tar_to_workspace(client: ComputeClient, key: str,
                                  tar: Path, safe_hint: str) -> None:
        """tar.gz in das frische Volume entpacken (darüber liegen nur die
        Starter-Dateien — der Restore gewinnt).

        Bei Fehlschlag wird der Workspace zurückgerollt (Container +
        Volume weg) — der nächste Status-Call startet fresh=true und
        wiederholt den Restore. Die Quelldatei bleibt erhalten."""
        try:
            data = tar.read_bytes()
            client.restore_volume(key, data)
            logger.info("workspace %s: %s wiederhergestellt (%d bytes)",
                        key, tar.name, len(data))
        except ComputeAgentError as e:
            try:
                client.delete_workspace(key)
            except ComputeAgentError:
                pass
            raise ComputeAgentError(
                f"Arbeitsstand konnte nicht wiederhergestellt werden: "
                f"{e.message} — Seite neu laden ({safe_hint}).", 500) from e

    @classmethod
    def _restore_backup_if_any(cls, client: ComputeClient, key: str) -> bool:
        """Sleep-Backup in das frische Volume zurückspielen (Student kommt
        zurück nach einem Idle-Sleep)."""
        if int(key.rsplit("-", 1)[1]) >= cls.REVIEW_KEY_OFFSET:
            # Review: Zustand kommt aus dem Submission-Snapshot — ein
            # (historisches) Backup wäre der alte, beworfene Edit-Stand.
            delete_workspace_backup(key)
            return False
        tar = workspace_backup_tar(key)
        if not tar.is_file():
            return False
        cls._restore_tar_to_workspace(
            client, key, tar, "das Backup ist auf dem Server sicher")
        return True

    def workspace_spec_for_agent(self, session: Session, task: Task,
                                 agent: dict) -> dict:
        """Slim-Spec (dict) für den Agenten: konkrete Image-Referenz aus der
        Image-Spec + Umgebung (Timeout/Limits/Internet/Artefakte) aus den
        Task-Feldern + GPU-Regel der Engine injizieren. Bei .init.sh wird
        zusätzlich die Task-Image-Referenz gesetzt (Agent startet Container
        vom gebauten Task-Image statt vom Basis-Image).

        Wirft ComputeAgentError, wenn das Image nicht auflösbar ist
        (z. B. Spec gelöscht) — der Workspace darf in dem Fall nicht
        starten (Tutor muss ein anderes Image setzen).
        """
        from services import image_spec_service
        try:
            resolved = image_spec_service.resolve_task_image(session, task)
        except image_spec_service.ImageSpecError as e:
            raise ComputeAgentError(
                f"Workspace-Image ungültig: {e} — bitte in der "
                "Aufgabenbearbeitung korrigieren.", 500) from e
        if resolved is None:
            raise ComputeAgentError(
                f"Workspace-Image {task.workspace_image!r} nicht auflösbar — "
                "bitte in der Aufgabenbearbeitung ein anderes Image setzen.", 500)
        spec: dict = {
            "image": resolved["image"],
            "gpus": agent.get("gpus") or "all",
            "timeout": int(task.workspace_timeout or 900),
            "limits": {
                "cpu": float(task.workspace_cpu or 2.0),
                "memory": str(task.workspace_memory or "4g"),
            },
            "internet": bool(task.workspace_internet),
        }
        if task.workspace_disk_quota:
            spec["disk_quota_mb"] = int(round(task.workspace_disk_quota * 1024))
        if task.workspace_main_file:
            spec["main_file"] = task.workspace_main_file
        # 🔒-Pfade (top-level, Dateien + Ordner) → je einer ein ro-Bind-Mount
        # aus dem geteilten Asset-Verzeichnis (eine Kopie für alle Studenten).
        fm = self.folder_map(session, task)
        ro_paths = readonly_mount_paths(self.task_files(session, task), fm)
        if ro_paths:
            spec["readonly_paths"] = ro_paths
        if task_has_init(task):
            spec["task_image"] = task_image_ref(
                task.course_id, task.id,
                task_init_hash(session, task, resolved["image"]))
        return spec

    def starter_files(self, session: Session, task: Task) -> list[dict]:
        """Starter-Dateien für Student-Volumes: alle Dateien mit
        effektiver Klasse ✏️ edit (b64-Liste für den Agenten).

        🔒-Dateien landen per ro-Mount, 👤-Dateien gar nicht im
        Student-Container — beide gehören daher nicht hier rein.
        Nur bei frischem Volume (Reset) geschrieben — danach ist das
        Volume der Stand des Studenten (Tutor-Änderungen an ✏️-Dateien
        wirken erst ab dem nächsten Reset; 🔒 ist per Mount live)."""
        fm = self.folder_map(session, task)
        out = []
        for f in self.task_files(session, task):
            if effective_file_access(f.path, f.access, fm) != "edit":
                continue
            p = file_disk_path(task.id, f.path)
            if not p.is_file():
                continue
            out.append({
                "path": f.path,
                "content_b64": base64.b64encode(p.read_bytes()).decode(),
            })
        return out

    def materialize_folders(self, session: Session, task: Task,
                            include_hidden: bool = False) -> list[str]:
        """Ordner, die in frische Volumes angelegt werden müssen, obwohl
        sie (noch) keine Dateien enthalten: explizite Ordner mit effektiver
        Klasse ✏️ (+ mit ``include_hidden`` auch 👤 — für die Einweg-
        Workspaces Testlauf/Grading/Rerun, die die 👤-Struktur benötigen).

        🔒-Ordner entfallen: sie sind ro-Bind-Mounts, deren Mount-Points
        das Docker-Runtime beim Container-Start anlegt. Ordner mit Dateien
        würden ohnehin beim Schreiben der Dateien entstehen — das Mkdir
        ist idempotent. Gleiche Konvention wie assets_sync/init_build.
        """
        fm = self.folder_map(session, task)
        wanted = {"edit", "hidden"} if include_hidden else {"edit"}
        return [p for p in sorted(fm)
                if effective_folder_access(p, fm) in wanted]

    # ═══════════════════════════════════════════════════════════
    # Task-Dateien (Tutor-Datei-Manager → Disk + DB)
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def save_task_file(session: Session, task: Task, path: str,
                       data: bytes, access: Optional[str] = None) -> TaskWorkspaceFile:
        """Datei auf Disk schreiben + DB-Row upsert.

        ``access``: explizite Klasse (None = erben, "readonly" | "hidden").
        Wird sie nicht übergeben, bleibt der bisherige Wert bestehen
        (Neu-Datei: erben) — zum Zurücksetzen auf „erben“ gibt es
        ``set_file_access``. System-Skripte (``system_file_access``)
        behalten IMMER ihre feste Klasse — übergebene Werte werden
        ignoriert (selbstheilend bei älteren Zeilen).
        """
        if access is not None and access not in ("readonly", "hidden"):
            raise ValueError(f"Ungültige Zugriffs-Klasse: {access!r}")
        fixed = system_file_access(path)
        if fixed is not None:
            access = fixed
            if fixed == "readonly":
                fm = WorkspaceService.folder_map(session, task)
                if effective_file_access(path, "readonly", fm) != "readonly":
                    raise ValueError(
                        f"„{path}“ ist ein System-Skript und muss für "
                        "Studenten lesbar bleiben — der Eltern-Ordner ist "
                        "aber versteckt.")
        p = file_disk_path(task.id, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        sha = hashlib.sha256(data).hexdigest()
        row = session.exec(
            select(TaskWorkspaceFile).where(
                TaskWorkspaceFile.task_id == task.id,
                TaskWorkspaceFile.path == path,
            )
        ).first()
        if row is None:
            row = TaskWorkspaceFile(task_id=task.id, path=path)
        row.size = len(data)
        row.checksum = sha
        if access is not None:
            row.access = access
        row.is_binary = b"\x00" in data[:1024]
        session.add(row)
        session.commit()
        session.refresh(row)
        return row

    @staticmethod
    def delete_task_file(session: Session, task: Task, path: str) -> None:
        p = file_disk_path(task.id, path)
        if p.is_file():
            p.unlink()
        row = session.exec(
            select(TaskWorkspaceFile).where(
                TaskWorkspaceFile.task_id == task.id,
                TaskWorkspaceFile.path == path,
            )
        ).first()
        if row is not None:
            session.delete(row)
            session.commit()

    @staticmethod
    def move_task_file(session: Session, task: Task, src: str, dst: str) -> TaskWorkspaceFile:
        """Datei verschieben/umbenennen.

        Die explizite Zugriffs-Klasse reist mit der Datei (die effektive
        Klasse kann sich durch neue Vorfahren ändern). Die System-Skripte
        bleiben in der Wurzel (Run-/Test-/Init-/Grading-Logik hängt an
        den festen Pfaden). Wirft ValueError bei ungültigem Move.
        """
        if src in SYSTEM_FILE_ACCESS:
            raise ValueError(
                f"„{src}“ ist ein System-Skript und muss in der Wurzel bleiben.")
        src_p = file_disk_path(task.id, src)
        dst_p = file_disk_path(task.id, dst)
        if not src_p.is_file():
            raise ValueError("Quelldatei nicht gefunden.")
        if dst_p.exists():
            raise ValueError("Zielpfad existiert bereits.")
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src_p, dst_p)
        row = session.exec(
            select(TaskWorkspaceFile).where(
                TaskWorkspaceFile.task_id == task.id,
                TaskWorkspaceFile.path == src,
            )
        ).first()
        if row is None:
            # Datei auf Disk, aber ohne DB-Row → nachtragen
            data = dst_p.read_bytes()
            row = TaskWorkspaceFile(task_id=task.id, path=dst)
            row.size = len(data)
            row.checksum = hashlib.sha256(data).hexdigest()
            row.is_binary = b"\x00" in data[:1024]
        else:
            row.path = dst
        session.add(row)
        session.commit()
        session.refresh(row)
        return row

    @staticmethod
    def task_files(session: Session, task: Task) -> list[TaskWorkspaceFile]:
        return session.exec(
            select(TaskWorkspaceFile)
            .where(TaskWorkspaceFile.task_id == task.id)
            .order_by(
                TaskWorkspaceFile.sort_order.asc().nulls_last(),
                TaskWorkspaceFile.path,
            )
        ).all()

    @staticmethod
    def get_task_file(session: Session, task: Task,
                      path: str) -> Optional[TaskWorkspaceFile]:
        return session.exec(
            select(TaskWorkspaceFile).where(
                TaskWorkspaceFile.task_id == task.id,
                TaskWorkspaceFile.path == path,
            )
        ).first()

    @staticmethod
    def order_map(session: Session, task: Task) -> dict[str, int]:
        """Anzeige-Reihenfolge (path → sort_order) für Ordner und
        [init]-Artefakte (task_workspace_orders)."""
        return {
            r.path: r.sort_order
            for r in session.exec(
                select(TaskWorkspaceOrder).where(
                    TaskWorkspaceOrder.task_id == task.id
                )
            ).all()
        }

    @staticmethod
    def reorder_workspace(session: Session, task: Task, order: list[str],
                          virtual_paths: set[str] | None = None) -> None:
        """Anzeige-Reihenfolge einer Gruppe in einem Ordner speichern.

        ``order`` ist die vollständige, neue Reihenfolge einer Gruppe
        (Dateien + Ordner + [init]-Artefakte gemischt — sie werden in der
        View getrennt gerendert, zählen also für sich). Dateien behalten
        ``task_workspace_files.sort_order``; Ordner und virtuelle Pfade
        (``virtual_paths`` = [init]-Artefakte ohne DB-Zeile) landen in
        ``task_workspace_orders``. Rein kosmetisch (Disk/Sync/Grading
        bleiben unangetastet).

        Wirft ValueError bei gemischten Eltern-Ordnern oder unbekanntem
        Pfad.
        """
        virtual = virtual_paths or set()
        if not order or len(order) > 500:
            raise ValueError("Ungültige Reihenfolge (leere bzw. zu lange Liste).")
        parents = {p.rsplit("/", 1)[0] if "/" in p else "" for p in order}
        if len(parents) > 1:
            raise ValueError("Reihenfolge mischt Pfade aus verschiedenen Ordnern.")
        file_rows = {f.path: f for f in WorkspaceService.task_files(session, task)}
        fm = WorkspaceService.folder_map(session, task)
        known = list(file_rows) + list(virtual)
        for i, p in enumerate(order):
            if p in file_rows:
                file_rows[p].sort_order = i
            elif p in virtual or p in fm or any(x.startswith(p + "/") for x in known):
                row = session.exec(
                    select(TaskWorkspaceOrder).where(
                        TaskWorkspaceOrder.task_id == task.id,
                        TaskWorkspaceOrder.path == p,
                    )
                ).first()
                if row is None:
                    row = TaskWorkspaceOrder(task_id=task.id, path=p)
                    session.add(row)
                row.sort_order = i
            else:
                raise ValueError("Unbekannter Pfad in Reihenfolge: " + p)
        session.commit()

    # ═══════════════════════════════════════════════════════════
    # Zugriffsklassen (Dateien + Ordner, DB)
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def task_folders(session: Session, task: Task) -> list[TaskWorkspaceFolder]:
        return session.exec(
            select(TaskWorkspaceFolder)
            .where(TaskWorkspaceFolder.task_id == task.id)
            .order_by(TaskWorkspaceFolder.path)
        ).all()

    @classmethod
    def folder_map(cls, session: Session, task: Task) -> dict[str, str]:
        """Explizite Ordner-Klassen als Map (path → "readonly"/"hidden")."""
        return {f.path: f.access for f in cls.task_folders(session, task)}

    @staticmethod
    def set_file_access(session: Session, task: Task, path: str,
                        access: Optional[str]) -> TaskWorkspaceFile:
        """Explizite Klasse einer Datei (None = erben von den Ordnern).

        System-Skripte (``system_file_access``) haben eine feste Klasse
        und können nicht umgestellt werden.
        """
        if access is not None and access not in ("readonly", "hidden"):
            raise ValueError(f"Ungültige Zugriffs-Klasse: {access!r}")
        if system_file_access(path) is not None:
            raise ValueError(
                f"„{path}“ ist ein System-Skript — seine Zugriffs-Klasse "
                "ist festgelegt und kann nicht geändert werden.")
        row = session.exec(
            select(TaskWorkspaceFile).where(
                TaskWorkspaceFile.task_id == task.id,
                TaskWorkspaceFile.path == path,
            )
        ).first()
        if row is None:
            raise ValueError("Datei nicht gefunden.")
        row.access = access
        row.updated_at = datetime.now()
        session.add(row)
        session.commit()
        session.refresh(row)
        return row

    @staticmethod
    def set_folder_access(session: Session, task: Task, path: str,
                          access: Optional[str]) -> None:
        """Explizite Klasse eines Ordners (None = „edit“).

        Die Zeile bleibt bei None bestehen bzw. wird angelegt — der
        Ordner ist danach explizit (persistiert, auch wenn er leer
        bleibt). Nur ``delete_task_folder`` entfernt die Zeile wieder.
        """
        p = str(path).replace("\\", "/").lstrip("/")
        if not p or p.endswith("/") or any(x == ".." for x in p.split("/")):
            raise ValueError("Ungültiger Ordnerpfad.")
        if access not in (None, "readonly", "hidden"):
            raise ValueError(f"Ungültige Zugriffs-Klasse: {access!r}")
        # System-Skripte (z. B. run.sh, test.sh) müssen IMMER für
        # Studenten lesbar bleiben — ein Ordner, der sie (auch per Erbung)
        # verstecken würde, darf die Setzung nicht bekommen.
        if access is not None:
            fm_new = dict(WorkspaceService.folder_map(session, task))
            fm_new[p] = access
            for f in WorkspaceService.task_files(session, task):
                if system_file_access(f.path) == "readonly" and \
                        effective_file_access(f.path, "readonly", fm_new) != "readonly":
                    raise ValueError(
                        f"„{p}“ enthält das System-Skript {f.path} — der "
                        "Ordner darf es nicht verbergen (es muss für "
                        "Studenten read-only lesbar bleiben).")
        # Self-Healing: doppelte Zeilen desselben Pfads (Race bei
        # parallelen Setzungen) bereinigen, bevor geschrieben wird.
        rows = session.exec(
            select(TaskWorkspaceFolder).where(
                TaskWorkspaceFolder.task_id == task.id,
                TaskWorkspaceFolder.path == p,
            )
        ).all()
        for dup in rows[1:]:
            session.delete(dup)
        row = rows[0] if rows else None
        if row is None:
            session.add(TaskWorkspaceFolder(task_id=task.id, path=p,
                                            access=access))
        else:
            row.access = access
            row.updated_at = datetime.now()
            session.add(row)
        session.commit()

    @staticmethod
    def create_task_folder(session: Session, task: Task, path: str) -> None:
        """(Möglichlicherweise leeren) Ordner anlegen: Disk + explizite
        Zeile (access=NULL=edit) — bleibt also auch leer sichtbar
        (Tree, Student-Ansicht, frische Volumes). Ein bereits impliziter
        Ordner (Dateien darunter, keine Zeile) wird nur explizit.
        """
        p = str(path).replace("\\", "/").lstrip("/")
        if not p or p.endswith("/") or any(x == ".." for x in p.split("/")):
            raise ValueError("Ungültiger Ordnerpfad.")
        d = task_workspace_dir(task.id) / p
        if d.is_file():
            raise ValueError(f"„{p}“ existiert bereits als Datei.")
        row = session.exec(
            select(TaskWorkspaceFolder).where(
                TaskWorkspaceFolder.task_id == task.id,
                TaskWorkspaceFolder.path == p,
            )
        ).first()
        if row is not None:
            raise ValueError("Ordner existiert bereits.")
        d.mkdir(parents=True, exist_ok=True)
        session.add(TaskWorkspaceFolder(task_id=task.id, path=p, access=None))
        session.commit()

    @staticmethod
    def delete_task_folder(session: Session, task: Task, path: str) -> None:
        """Ordner-Subtree aus dem Aufgaben-Workspace entfernen: Disk
        (Verzeichnis inkl. Inhalt) + DB-Rows (Dateien + Ordner-Klassen,
        auch verschachtelt)."""
        p = str(path).replace("\\", "/").lstrip("/")
        if not p or p.endswith("/") or any(x == ".." for x in p.split("/")):
            raise ValueError("Ungültiger Ordnerpfad.")
        d = task_workspace_dir(task.id) / p
        if d.is_file():
            raise ValueError("Pfad ist eine Datei — bitte als Datei löschen.")
        prefix = p + "/"
        files = [f for f in session.exec(select(TaskWorkspaceFile).where(
            TaskWorkspaceFile.task_id == task.id)).all()
            if f.path == p or f.path.startswith(prefix)]
        folders = [r for r in session.exec(select(TaskWorkspaceFolder).where(
            TaskWorkspaceFolder.task_id == task.id)).all()
            if r.path == p or r.path.startswith(prefix)]
        orders = [r for r in session.exec(select(TaskWorkspaceOrder).where(
            TaskWorkspaceOrder.task_id == task.id)).all()
            if r.path == p or r.path.startswith(prefix)]
        if not d.is_dir() and not files and not folders:
            raise ValueError("Ordner nicht gefunden.")
        for f in files:
            session.delete(f)
        for r in folders:
            session.delete(r)
        for r in orders:
            session.delete(r)
        if d.is_dir():
            shutil.rmtree(d)
        session.commit()

    @staticmethod
    def move_folder(session: Session, task: Task, src: str, dst: str) -> None:
        """Ordner verschieben/umbenennen (Disk + DB-Rows für Dateien,
        Ordner-Klassen und Anzeige-Reihenfolge). Wirft ValueError bei
        ungültigem Move."""
        src = str(src).replace("\\", "/").lstrip("/")
        dst = str(dst).replace("\\", "/").lstrip("/")
        if not src or not dst or src == dst or ".." in dst.split("/"):
            raise ValueError("Ungültiger Ordner-Move.")
        base = task_workspace_dir(task.id)
        src_p = base / src
        dst_p = base / dst
        if not src_p.is_dir():
            raise ValueError("Quellordner nicht gefunden.")
        if dst_p.exists():
            raise ValueError("Zielordner existiert bereits.")
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src_p, dst_p)
        psrc, pdst = src + "/", dst + "/"
        for row in session.exec(
            select(TaskWorkspaceFile)
            .where(TaskWorkspaceFile.task_id == task.id)
        ).all():
            if row.path == src:
                continue
            if row.path.startswith(psrc):
                row.path = pdst + row.path[len(psrc):]
                session.add(row)
        for row in session.exec(
            select(TaskWorkspaceFolder)
            .where(TaskWorkspaceFolder.task_id == task.id)
        ).all():
            if row.path == src:
                row.path = dst
            elif row.path.startswith(psrc):
                row.path = pdst + row.path[len(psrc):]
            else:
                continue
            session.add(row)
        for row in session.exec(
            select(TaskWorkspaceOrder)
            .where(TaskWorkspaceOrder.task_id == task.id)
        ).all():
            if row.path == src:
                row.path = dst
            elif row.path.startswith(psrc):
                row.path = pdst + row.path[len(psrc):]
            else:
                continue
            session.add(row)
        session.commit()

    def judge_script_path(self, session: Session, task: Task) -> Optional[str]:
        """Pfad des hidden-Judge-Skripts (fester Wurzelpfad .test_private.sh)
        oder None → Grading ohne private Tests."""
        row = session.exec(
            select(TaskWorkspaceFile).where(
                TaskWorkspaceFile.task_id == task.id,
                TaskWorkspaceFile.path == JUDGE_SCRIPT,
            )
        ).first()
        if row is None or not file_disk_path(task.id, JUDGE_SCRIPT).is_file():
            return None
        fm = self.folder_map(session, task)
        if effective_file_access(JUDGE_SCRIPT, row.access, fm) != "hidden":
            return None
        return JUDGE_SCRIPT

    def ensure_system_stubs(self, session: Session, task: Task) -> list[str]:
        """System-Skript-Stubs anlegen, wenn der Datei-Baum noch leer ist
        (Selbstheilung beim Task-Save) — der Tutor muss die Skript-Namen
        nicht auswendig kennen.

        Liefert die angelegten Pfade (leer, wenn schon Dateien oder
        explizite Ordner existieren). Legt zusätzlich .solution/ (👤) als
        Referenzordner für die Musterlösung an.
        """
        if self.task_files(session, task) or self.task_folders(session, task):
            return []
        created: list[str] = []
        for path, content in SYSTEM_STUBS.items():
            self.save_task_file(session, task, path, content.encode("utf-8"))
            created.append(path)
        self.set_folder_access(session, task, ".solution", "hidden")
        return created

    # ═════════════════════════════════════════════════════════
    # Task-Save: Assets + Task-Image (1× je Task-Änderung, je Agent)
    # ═════════════════════════════════════════════════════════

    def sync_assets(self, session: Session, task: Task,
                    agent: Optional[dict] = None) -> dict:
        """Alle für Studenten sichtbaren Dateien (effektive Klasse ≠ 👤)
        in das geteilte Asset-Verzeichnis auf dem Agenten bringen.

        Explizite Ordner-Pfade (inkl. leerer) werden mitgegeben — der
        Agent legt sie an und behält sie beim Purge bei.
        `delete_missing` ist immer an: Dateien, die der Tutor gelöscht
        hat, verschwinden auch auf dem Agent-Host. Init-generierte
        Dateien sind beim Agenten über das Manifest geschützt.
        """
        agent = agent or self.pick_task_agent(session, task)
        if agent is None:
            raise ComputeAgentUnavailable("Kein Agent für Asset-Sync")
        client = self.client_for(agent)
        fm = self.folder_map(session, task)
        files = []
        for f in self.task_files(session, task):
            if effective_file_access(f.path, f.access, fm) == "hidden":
                continue
            p = file_disk_path(task.id, f.path)
            if not p.is_file():
                continue
            files.append({
                "path": f.path,
                "content_b64": base64.b64encode(p.read_bytes()).decode(),
            })
        result = client.sync_assets(task.course_id, task.id, files,
                                    delete_missing=True,
                                    folders=sorted(fm.keys()))
        result["agent"] = agent["name"]
        return result

    def _resolve_base_image(self, session: Session, task: Task) -> str:
        """Konkrete Image-Referenz der Aufgabe (Image-Spec) oder Fehler."""
        from services import image_spec_service
        try:
            resolved = image_spec_service.resolve_task_image(session, task)
        except image_spec_service.ImageSpecError as e:
            raise ComputeAgentError(
                f"Init-Build fehlgeschlagen: Workspace-Image ungültig: {e}", 500) from e
        if resolved is None:
            raise ComputeAgentError(
                "Init-Build fehlgeschlagen: Workspace-Image nicht auflösbar "
                "(Image-Spec fehlt oder wurde gelöscht).", 500)
        return resolved["image"]

    def init_build(self, session: Session, task: Task,
                   agent: Optional[dict] = None,
                   force: bool = False) -> dict:
        """Task-Image (.init.sh-Build) auf dem Agenten bauen (idempotent
        über den init-Hash: gleicher Hash + vorhandenes Image → sofort
        ready).

        force=True: Idempotenz überspringen — Skript läuft komplett neu,
        alte Init-Artefakte werden gelöscht + neu erzeugt (Cleansweep).

        Ohne .init.sh/.init_hidden.sh: verwaiste Task-Images entfernen
        (kein Task-Image mehr nötig) → Status „none“.
        """
        agent = agent or self.pick_task_agent(session, task)
        if agent is None:
            raise ComputeAgentUnavailable("Kein Agent für Init-Build")
        client = self.client_for(agent)
        if not task_has_init(task):
            try:
                client.delete_task_images(task.course_id, task.id)
            except ComputeAgentError:
                pass
            return {"status": "none", "agent": agent["name"]}
        image = self._resolve_base_image(session, task)
        fm = self.folder_map(session, task)
        files = self.task_files(session, task)
        priv_p = file_disk_path(task.id, INIT_PRIVATE_SCRIPT)
        init_private_b64 = (base64.b64encode(priv_p.read_bytes()).decode()
                            if priv_p.is_file() else None)
        # .init.sh ist 👤 → nicht im Asset-Sync; der Agent holt es per
        # Request-Body und persistiert es in der privaten Region.
        init_p = file_disk_path(task.id, INIT_SCRIPT)
        init_b64 = (base64.b64encode(init_p.read_bytes()).decode()
                    if init_p.is_file() else None)
        # Ordner-Struktur für die Build-Umgebung: explizite Ordner (aus
        # der Access-Map) + implizite Eltern-Ordner aller Dateien — im
        # Init-Build existiert sonst nur, was .init.sh selbst anlegt.
        folder_paths = set(fm)
        for f in files:
            if "/" in f.path:
                folder_paths.add(f.path.rsplit("/", 1)[0])
        result = client.init_build(
            task.course_id, task.id, image,
            task_init_hash(session, task, image),
            deadline=task.deadline,
            readonly_paths=readonly_mount_paths(files, fm),
            hidden_paths=hidden_mount_paths(files, fm),
            init_b64=init_b64,
            init_private_b64=init_private_b64,
            folders=sorted(folder_paths),
            force=force,
        )
        result["agent"] = agent["name"]
        return result

    def init_status(self, session: Session, task: Task,
                   agent: Optional[dict] = None) -> dict:
        """Init-Status auf dem Agenten: none|ready|building|failed|idle."""
        agent = agent or self.pick_task_agent(session, task)
        if agent is None:
            raise ComputeAgentUnavailable("Kein Agent für Init-Status")
        client = self.client_for(agent)
        if not task_has_init(task):
            return {"status": "none", "agent": agent["name"]}
        result = client.init_status(task.course_id, task.id,
                                    task_init_hash(
                                        session, task,
                                        self._resolve_base_image(
                                            session, task)))
        result["agent"] = agent["name"]
        return result

    def stop_init_build(self, session: Session, task: Task,
                        agent: Optional[dict] = None) -> dict:
        """Aktiven Init-Build auf dem Agenten abbrechen."""
        agent = agent or self.pick_task_agent(session, task)
        if agent is None:
            raise ComputeAgentUnavailable("Kein Agent für Init-Abbruch")
        client = self.client_for(agent)
        result = client.init_stop(task.course_id, task.id,
                                  task_init_hash(
                                      session, task,
                                      self._resolve_base_image(
                                          session, task)))
        result["agent"] = agent["name"]
        return result

    def on_task_saved(self, session: Session, task: Task,
                      force: bool = False) -> dict:
        """Task-Save-Seiteneffekte: Image-Spec + Assets + Init-Build auf
        ALLE Engines des Pools (damit der erste Student auf einem fertigen
        Node landet). Fehler werden pro Agent gesammelt, nicht geworfen.

        force=True (manuelles „Sync & Init“): Init-Build wird erzwungen —
        das Skript läuft komplett neu, die alten Init-Artefakte werden
        gelöscht und neu erzeugt.

        Persistiert den Per-Agenten-Status als JSON in
        Task.workspace_assets_status: {agent_url: {assets, task_image,
        image, error?}} mit Status-Strings (UI-Chips pro Engine).
        """
        results = {"assets": [], "task_image": [], "image": []}
        if not self.is_enabled(session, task.course_id) or task.task_type.value != "workspace":
            return results
        # System-Skript-Stubs bei leerem Baum (Selbstheilung) — VOR dem
        # Agent-Loop, damit Asset-Sync + Init-Build die Stubs sehen.
        try:
            self.ensure_system_stubs(session, task)
        except Exception as e:  # noqa: BLE001 — Stubs dürfen den Sync nicht blockieren
            logger.warning("System-Stubs (task %s): %s", task.id, e)
        from services import image_spec_service
        spec_row = None
        if task.workspace_image:
            spec_row = image_spec_service.resolve_spec(
                session, task.course_id, task.workspace_image)
        agents = self.target_agents(session, task)
        status: dict = {}
        for agent in agents:
            a_status: dict = {
                "agent": agent["name"],
                "image": {"status": "skipped"},
            }
            if spec_row is not None:
                try:
                    a_status["image"] = self.client_for(agent).images_install(
                        {"name": spec_row.name, "dockerfile": spec_row.dockerfile})
                except ComputeAgentError as e:
                    a_status["image"] = {"status": "failed", "error": e.message}
            # Assets + Init-Build NACH dem Image: der Init-Build verwendet
            # das Image als Basis (muss auf dem Node vorhanden sein).
            try:
                a_status["assets"] = self.sync_assets(session, task, agent)
            except ComputeAgentError as e:
                a_status["assets"] = {"status": "failed", "error": e.message}
            try:
                a_status["task_image"] = self.init_build(
                    session, task, agent, force=force)
            except ComputeAgentError as e:
                a_status["task_image"] = {"status": "failed", "error": e.message}
            results["assets"].append(dict(a_status))
            results["task_image"].append(dict(a_status))
            results["image"].append(dict(a_status))
            st: dict = {
                "assets": a_status["assets"].get("status", "failed")
                if isinstance(a_status["assets"], dict) else str(a_status["assets"]),
                "task_image": a_status["task_image"].get("status", "failed")
                if isinstance(a_status["task_image"], dict) else str(a_status["task_image"]),
                "image": a_status["image"].get("status", "failed")
                if isinstance(a_status["image"], dict) else str(a_status["image"]),
            }
            errors = [str(sub.get("error")) for sub in a_status.values()
                      if isinstance(sub, dict) and sub.get("error")]
            if errors:
                st["error"] = "; ".join(errors)
            status[agent["url"]] = st
        task.workspace_assets_status = json.dumps(status)
        session.add(task)
        session.commit()
        return results

    def _cleanup_workspace_assets(self, session: Session, task: Task) -> None:
        """Agent-Ressourcen + lokale Workspace-Dateien der Aufgabe entfernen."""
        for agent in self.get_agents(session, task.course_id):
            client = self.client_for(agent)
            try:
                client.delete_assets(task.course_id, task.id)
                client.delete_task_images(task.course_id, task.id)
            except ComputeAgentError:
                pass
        base = task_workspace_dir(task.id)
        if base.exists():
            shutil.rmtree(base, ignore_errors=True)

    def on_task_deleted(self, session: Session, task: Task) -> None:
        """Task-Entfernung: Agent-Ressourcen + lokale Dateien aufräumen."""
        self._cleanup_workspace_assets(session, task)
        delete_task_backups(task.course_id, task.id)
        # Version-Snapshots der Aufgabe entfernen
        for v in session.exec(
            select(ContentVersion)
            .where(ContentVersion.entity_type == "task")
            .where(ContentVersion.entity_id == task.id)
        ).all():
            if v.file_snapshot:
                shutil.rmtree(VERSION_SNAPSHOT_DIR / str(v.id), ignore_errors=True)

    def delete_task_db_rows(self, session: Session, task: Task) -> None:
        """Workspace-DB-Rows der Aufgabe entfernen.

        Die Tabellen haben NOT NULL-FKs auf tasks.id ohne
        Relationship-Cascade — session.delete(task) würde sonst
        task_id=NULL setzen und mit IntegrityError abbrechen (500).
        """
        for f in self.task_files(session, task):
            session.delete(f)
        for fo in self.task_folders(session, task):
            session.delete(fo)
        for row in session.exec(
            select(TaskWorkspaceOrder).where(TaskWorkspaceOrder.task_id == task.id)
        ).all():
            session.delete(row)
        for run in session.exec(
            select(WorkspaceRun).where(WorkspaceRun.task_id == task.id)
        ).all():
            session.delete(run)

    def discard_workspace_content(self, session: Session, task: Task,
                                  keep_version_snapshots: bool = False) -> None:
        """Workspace-Inhalt einer Aufgabe entsorgen (DB-Rows + Dateien).

        Wird aufgerufen, wenn eine Aufgabe aufhört, Workspace zu sein, aber
        noch Workspace-Zeilen trägt (z. B. per Typ-Wechsel verwaist):
        session.delete(task) würde die NOT-NULL-FKs sonst auf NULL setzen
        und mit IntegrityError abbrechen (500).

        keep_version_snapshots: bei einem Typ-Wechsel bleiben die
        Version-Snapshots der alten Workspace-Zustände erhalten (die
        Workspace-Versionen bleiben damit wiederherstellbar); bei einer
        endgültigen Löschung werden sie mit entfernt.
        """
        try:
            if keep_version_snapshots:
                self._cleanup_workspace_assets(session, task)
            else:
                self.on_task_deleted(session, task)
        except Exception as e:  # noqa: BLE001
            logger.warning("Workspace-Aufräumen (task %s) fehlgeschlagen: %s", task.id, e)
        self.delete_task_db_rows(session, task)

    # ═══════════════════════════════════════════════════════════
    # Abgabe (Snapshot) & Lauf-Historie
    # ═══════════════════════════════════════════════════════════

    def save_snapshot(self, session: Session, task: Task, student_id: int,
                      submission_id: int) -> str:
        """Workspace-Snapshot (tar.gz) aus dem Agenten sichern.
        Liefert relativen Pfad (submissions/{id}/workspace.tar.gz)."""
        agent = self.pick_task_agent(session, task)
        if agent is None:
            raise ComputeAgentUnavailable("Kein Agent für Snapshot")
        client = self.client_for(agent)
        key = workspace_key(task.course_id, task.id, student_id)
        data = client.snapshot(key)
        dest = SUBMISSION_DIR / str(submission_id)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "workspace.tar.gz").write_bytes(data)
        return f"submissions/{submission_id}/workspace.tar.gz"

    @staticmethod
    def extract_snapshot(snapshot_rel: str, dest: Path) -> None:
        """Snapshot sicher entpacken (keine Symlinks, kein Entweichen)."""
        src = snapshot_abs_path(snapshot_rel)
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(src, "r:gz") as tar:
            for member in tar.getmembers():
                target = (dest / member.name).resolve()
                if not str(target).startswith(str(dest.resolve()) + os.sep) \
                        and target != dest.resolve():
                    raise ValueError(f"Ungültiger Snapshot-Eintrag: {member.name}")
                if member.issym() or member.islnk():
                    raise ValueError(f"Links nicht erlaubt: {member.name}")
            tar.extractall(dest)

    # ═══════════════════════════════════════════════════════════
    # Version-Snapshots (Workspace-Dateibäume in Task-Versionen)
    # ═══════════════════════════════════════════════════════════

    def capture_version_snapshot(self, session: Session, task: Task,
                                 version_id: int) -> Optional[str]:
        """Workspace-Dateibau der Aufgabe als Version-Snapshot sichern.

        Liefert den relativen Pfad (version_snapshots/{id}/workspace.tar.gz)
        oder None (Aufgabentyp ≠ Workspace). Überschreibt den Snapshot der
        Version — außer nichts hat sich geändert (Digest-Check: Auto-Save
        feuert bei jeder Tipp-Pause, ein Rewrite wäre reine Verschwendung).

        Große Datensätze gehören NICHT hier rein — die werden von
        .init.sh bei der Initialisierung heruntergeladen (Doku).
        """
        if task.task_type.value != "workspace":
            return None
        meta = self._version_meta(session, task)
        digest = self._version_digest(task, meta)
        vdir = VERSION_SNAPSHOT_DIR / str(version_id)
        hash_path = vdir / "manifest.sha256"
        if hash_path.exists() and hash_path.read_text().strip() == digest:
            return f"version_snapshots/{version_id}/workspace.tar.gz"
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / "meta.json").write_text(
            json.dumps(meta, sort_keys=True, indent=2), encoding="utf-8")
        base = task_workspace_dir(task.id)
        tmp = vdir / "workspace.tar.gz.tmp"
        with tarfile.open(tmp, "w:gz") as tar:
            if base.exists():
                for p in sorted(base.rglob("*")):
                    if p.is_file():
                        tar.add(p, arcname=p.relative_to(base).as_posix())
        tmp.replace(vdir / "workspace.tar.gz")
        hash_path.write_text(digest, encoding="utf-8")
        return f"version_snapshots/{version_id}/workspace.tar.gz"

    @staticmethod
    def _version_meta(session: Session, task: Task) -> dict:
        """Metadaten, die in der DB und NICHT im Dateibau selbst leben
        (Zugriffsklassen/Sortierung) — sonst gingen sie beim Restore
        verloren (meta.json neben dem tar)."""
        return {
            "files": {
                f.path: {
                    "access": f.access,
                    "is_binary": f.is_binary,
                    "sort_order": f.sort_order,
                }
                for f in WorkspaceService.task_files(session, task)
            },
            "folders": {
                fo.path: {"access": fo.access}
                for fo in WorkspaceService.task_folders(session, task)
            },
            "orders": WorkspaceService.order_map(session, task),
        }

    @staticmethod
    def _version_digest(task: Task, meta: dict) -> str:
        """Fingerprint des Dateibaus (Pfad+Größe+mtime je Datei + JSON-Meta).
        Dient als Skip-Kriterium für capture_version_snapshot."""
        base = task_workspace_dir(task.id)
        entries = []
        if base.exists():
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    st = p.stat()
                    entries.append([
                        p.relative_to(base).as_posix(),
                        int(st.st_size),
                        int(st.st_mtime_ns),
                    ])
        blob = json.dumps({"files": entries, "meta": meta}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def restore_version_snapshot(self, session: Session, task: Task,
                                 snapshot_rel: str) -> Optional[Path]:
        """Version-Snapshot auf den Task-Workspace anwenden: DB-Zeilen +
        Disk-Dateien durch den Snapshot ersetzen.

        Commitet NICHT — der Caller (Versions-Restore) commitet zusammen
        mit den Feld-Änderungen (adapter.apply). Liefert den Backup-Pfad
        des alten Dateibaus (None = war leer); der Caller ist
        verantwortlich, ihn zu rollbacken (Fehler) oder zu verwerfen
        (Erfolg). Bei Entpack-Fehler wird der alte Stand automatisch
        wiederhergestellt.
        """
        base = task_workspace_dir(task.id)
        backup: Optional[Path] = None
        if base.exists():
            backup = base.with_name(base.name + ".restore-bak")
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            shutil.move(str(base), str(backup))
        for f in self.task_files(session, task):
            session.delete(f)
        for fo in self.task_folders(session, task):
            session.delete(fo)
        for row in session.exec(
            select(TaskWorkspaceOrder).where(TaskWorkspaceOrder.task_id == task.id)
        ).all():
            session.delete(row)
        meta_path = snapshot_abs_path(snapshot_rel).parent / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        try:
            self.extract_snapshot(snapshot_rel, base)
        except Exception:
            self.rollback_version_snapshot_backup(task.id, backup)
            raise
        now = datetime.now()
        for path, fm in (meta.get("files") or {}).items():
            p = file_disk_path(task.id, path)
            session.add(TaskWorkspaceFile(
                task_id=task.id, path=path,
                size=int(p.stat().st_size) if p.is_file() else 0,
                access=fm.get("access"),
                is_binary=bool(fm.get("is_binary", False)),
                sort_order=fm.get("sort_order"),
                updated_at=now,
            ))
        for path, fm in (meta.get("folders") or {}).items():
            session.add(TaskWorkspaceFolder(
                task_id=task.id, path=path,
                access=fm.get("access"), updated_at=now))
        for path, so in (meta.get("orders") or {}).items():
            session.add(TaskWorkspaceOrder(
                task_id=task.id, path=path, sort_order=int(so)))
        return backup

    @staticmethod
    def rollback_version_snapshot_backup(task_id: int,
                                         backup: Optional[Path]) -> None:
        """Backup des alten Dateibaus nach FEHLGESCHLAGENEM Restore
        zurückführen (aktueller, teils entpackter Zustand wird ersetzt)."""
        if not backup:
            return
        base = task_workspace_dir(task_id)
        if base.exists():
            shutil.rmtree(base, ignore_errors=True)
        try:
            shutil.move(str(backup), str(base))
        except OSError:
            shutil.rmtree(backup, ignore_errors=True)

    @staticmethod
    def discard_version_snapshot_backup(backup: Optional[Path]) -> None:
        """Backup nach ERFOLGREICHEM Restore entfernen."""
        if backup and Path(backup).exists():
            shutil.rmtree(backup, ignore_errors=True)

    def copy_version_snapshot(self, src_version_id: int,
                              dst_version_id: int) -> str:
        """Version-Snapshot kopieren (Restore erzeugt eine neue Version,
        die denselben Datei-Stand trägt — eigene Kopie = einfache
        Delete-Semantik)."""
        src = VERSION_SNAPSHOT_DIR / str(src_version_id)
        dst = VERSION_SNAPSHOT_DIR / str(dst_version_id)
        dst.mkdir(parents=True, exist_ok=True)
        for name in ("workspace.tar.gz", "meta.json", "manifest.sha256"):
            p = src / name
            if p.exists():
                shutil.copy2(p, dst / name)
        return f"version_snapshots/{dst_version_id}/workspace.tar.gz"

    @staticmethod
    def record_run(session: Session, task: Task, student_id: int, command: str,
                   result: dict) -> Optional[WorkspaceRun]:
        """WorkspaceRun-Row anlegen (Job-Start)."""
        run = WorkspaceRun(
            run_id=str(result.get("run_id") or ""),
            task_id=task.id,
            student_id=student_id,
            command=command,
            started_at=datetime.now(),
            status=WorkspaceRunStatus.RUNNING,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        return run

    @staticmethod
    def update_run_from_status(session: Session, task: Task,
                               status: dict) -> Optional[WorkspaceRun]:
        """WorkspaceRun aus Agent-Status aktualisieren (final oder nicht)."""
        run = session.exec(
            select(WorkspaceRun).where(WorkspaceRun.run_id == status.get("run_id"))
        ).first()
        if run is None:
            return None
        agent_status = status.get("status")
        if agent_status in ("running", "queued"):
            return run
        mapping = {
            "done": WorkspaceRunStatus.DONE,
            "timeout": WorkspaceRunStatus.TIMEOUT,
            "killed": WorkspaceRunStatus.KILLED,
        }
        run.status = mapping.get(agent_status, WorkspaceRunStatus.DONE)
        run.exit_code = status.get("exit_code")
        run.finished_at = datetime.now()
        # stdout/stderr: Agent liefert Zeilen-Listen, das Backend normalisiert
        # an manchen Stellen bereits auf Strings → beides tolerant behandeln
        out = status.get("stdout")
        if isinstance(out, list):
            out = "\n".join(out)
        err = status.get("stderr")
        if isinstance(err, list):
            err = "\n".join(err)
        run.stdout = (out or "")[-MAX_LOG_CHARS:]
        run.stderr = (err or "")[-MAX_LOG_CHARS:]
        session.add(run)
        session.commit()
        return run

    # ═══════════════════════════════════════════════════════════
    # Grading-Kontext (frischer Container, private Dateien, LLM)
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def grading_key(task: Task, submission_id: int) -> str:
        """Einweg-Workspace je Einreichung (student_id=Submission-Id → kollisionsfrei)."""
        return workspace_key(task.course_id, task.id, submission_id)

    # Reservierter Student-Bereich für Review-Workspaces (1M + Submission-Id):
    # kollidiert mit keinen echten User-IDs (0 = Tutor-Testlauf).
    REVIEW_KEY_OFFSET = 1_000_000

    @classmethod
    def review_key(cls, task: Task, submission_id: int) -> str:
        """Review-Workspace je Einreichung: die eingereichte Lösung wird
        hier live ausgeführt (Tutor-Review / Klausurvorbereitung).
        Separater Key → die Live-Workspaces und die Snapshots bleiben
        unangetastet."""
        return workspace_key(task.course_id, task.id,
                             cls.REVIEW_KEY_OFFSET + submission_id)

    def review_starter_files(self, session: Session, task: Task,
                             include_hidden: bool = False) -> list[dict]:
        """Starter-Dateien des Review-Containers: ✏️-Dateien der Aufgabe
        (+ 👤-Dateien, nur für Tutor/Prof — private Tests lassen sich so
        interaktiv ausführen). Der eigentliche Lösungszustand kommt
        danach per tar-Restore des Submission-Snapshots (ensure_workspace
        mit first_restore_tar)."""
        files = self.starter_files(session, task)
        if include_hidden:
            seen = {f["path"] for f in files}
            for e in self._hidden_injection_files(session, task):
                if e["path"] not in seen:
                    files.append(e)
                    seen.add(e["path"])
        return files

    @staticmethod
    def delete_review_workspace(session: Session, task: Task,
                                submission_id: int) -> None:
        """Review-Workspace räumen: Volume/Container auf der Engine +
        Sleep-Backup auf dem Server (beide sind wegwerfbar — der
        Zustand kommt aus dem Submission-Snapshot)."""
        key = WorkspaceService.review_key(task, submission_id)
        delete_workspace_backup(key)
        agent = workspace_service.pick_task_agent(session, task)
        if agent is None:
            return
        try:
            workspace_service.client_for(agent).delete_workspace(key)
        except ComputeAgentError:
            pass  # war schon weg

    def _private_init_artifact_entries(self, session: Session,
                                       task: Task) -> list[dict]:
        """Private (👤) Init-Artefakte vom Agenten ({path, size, is_binary}).

        Kommen aus dem Init-Build (.init_hidden.sh) und liegen in der
        privaten Agenten-Region — für Studenten nie sichtbar, bei der
        Korrektur injiziert. Agent nicht erreichbar → [] (Grading läuft
        ohne die Init-Artefakte weiter, wie ohne Agent überhaupt)."""
        from services.compute_client import ComputeAgentError
        agent = self.pick_task_agent(session, task)
        if agent is None:
            return []
        try:
            data = self.client_for(agent).init_artifacts(
                task.course_id, task.id, "private")
        except ComputeAgentError:
            return []
        return [e for e in data.get("files", []) if e.get("path")]

    def _private_init_artifact_bytes(self, session: Session, task: Task,
                                     path: str) -> Optional[bytes]:
        """Inhalt eines privaten Init-Artefakts (None bei Fehler)."""
        from services.compute_client import ComputeAgentError
        agent = self.pick_task_agent(session, task)
        if agent is None:
            return None
        try:
            _size, data = self.client_for(agent).init_artifact_file(
                task.course_id, task.id, "private", path)
            return data
        except ComputeAgentError:
            return None

    def _hidden_injection_files(self, session: Session, task: Task) -> list[dict]:
        """Alle versteckten (👤) Dateien als Starter-Einträge (b64): DB-
        Dateien frisch von der Disk + private Init-Artefakte von der
        Agenten-Region (Ergebnisse von .init_hidden.sh)."""
        fm = self.folder_map(session, task)
        out: list[dict] = []
        seen: set[str] = set()
        for f in self.task_files(session, task):
            if effective_file_access(f.path, f.access, fm) != "hidden":
                continue
            if f.path in seen:
                continue
            disk = file_disk_path(task.id, f.path)
            if not disk.is_file():
                continue
            out.append({
                "path": f.path,
                "content_b64": base64.b64encode(disk.read_bytes()).decode(),
            })
            seen.add(f.path)
        # Private Init-Artefakte (.init_hidden.sh-Ergebnisse, Agenten-Region)
        for e in self._private_init_artifact_entries(session, task):
            if e["path"] in seen:
                continue
            data = self._private_init_artifact_bytes(session, task, e["path"])
            if data is None or len(data) > MAX_WORKSPACE_TOTAL_BYTES:
                continue
            out.append({
                "path": e["path"],
                "content_b64": base64.b64encode(data).decode(),
            })
            seen.add(e["path"])
        return out

    def test_run_starter_files(self, session: Session, task: Task) -> list[dict]:
        """Starter-Dateien für den Tutor-Testlauf: alle editierbaren (✏️)
        Dateien + alle versteckten (👤) Dateien (Musterlösung, private
        Tests) — der Lauf prüft die Musterlösung gegen beide Tests."""
        return (self.starter_files(session, task)
                + self._hidden_injection_files(session, task))

    def grading_starter_files(self, session: Session, task: Task,
                              submission) -> list[dict]:
        """Starter-Dateien des Grading-Containers:
        Student-Snapshot (ohne 👤-Pfade) + alle 👤-Dateien frisch von der
        Disk + private Init-Artefakte (Agenten-Region)."""
        fm = self.folder_map(session, task)
        file_acc = {f.path: f.access for f in self.task_files(session, task)}

        files: list[dict] = []
        seen: set[str] = set()
        if submission.workspace_snapshot:
            with tempfile.TemporaryDirectory(prefix="aicampus-grade-") as tmp:
                try:
                    self.extract_snapshot(
                        submission.workspace_snapshot, Path(tmp))
                except (tarfile.TarError, OSError, EOFError, zlib.error):
                    # Beschädigtes/unvollständiges tar.gz (z. B. durch
                    # ältere, still abgeschnittene Snapshots) — statt
                    # kryptischem zlib-Fehler klaren Fehler an die Einreichung.
                    raise ValueError(
                        "Student-Snapshot ist beschädigt oder unvollständig — "
                        "bitte die Lösung erneut einreichen")
                for p in sorted(Path(tmp).rglob("*")):
                    if not p.is_file():
                        continue
                    rel = p.relative_to(Path(tmp)).as_posix()
                    # Aus dem Snapshot zählen nur ✏️-Dateien:
                    # 👤-Pfade kommen frisch von der Disk (Studenten dürfen
                    # sie nicht besitzen), 🔒-Pfade sind per ro-Bind-Mount
                    # im Grading-Container — docker cp dorthin scheitert am
                    # Lchown auf dem read-only Mount.
                    if effective_file_access(rel, file_acc.get(rel), fm) != "edit":
                        continue
                    files.append({
                        "path": rel,
                        "content_b64": base64.b64encode(p.read_bytes()).decode(),
                    })
                    seen.add(rel)
        # 👤-Dateien (Disk + private Init-Artefakte)
        for e in self._hidden_injection_files(session, task):
            if e["path"] in seen:
                continue
            files.append(e)
            seen.add(e["path"])
        return files

    @staticmethod
    def _strip_notebook_outputs(text: str) -> str:
        """Jupyter-Notebook (v4): outputs + execution_count der Code-Cells
        entfernen, damit gespeicherte Run-Outputs (z. B. base64-Matplotlib-
        Bilder) den LLM-Grading-Kontext nicht sprengen.
        Ungueltiges JSON: unveraendert zurueckgeben."""
        try:
            nb = json.loads(text)
        except ValueError:
            return text
        if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
            return text
        for cell in nb["cells"]:
            if isinstance(cell, dict) and cell.get("cell_type") == "code":
                cell.pop("outputs", None)
                cell.pop("execution_count", None)
        return json.dumps(nb, ensure_ascii=False, indent=1)

    @staticmethod
    def _collect_texts(root: Path, max_file: int = MAX_LLM_FILE_CHARS,
                       max_total: int = MAX_LLM_TOTAL_CHARS) -> dict[str, str]:
        """Textdateien aus dem übergebenen Verzeichnis für den LLM-Prompt
        sammeln (Binarys/Übergroßes wird übersprungen)."""
        out: dict[str, str] = {}
        total = 0
        if not root.is_dir():
            return out
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if total >= max_total:
                break
            try:
                data = p.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:1024]:
                continue  # Binary — nicht in den LLM
            text = data.decode(errors="replace")
            if p.suffix == ".ipynb":
                text = WorkspaceService._strip_notebook_outputs(text)
            if len(text) > max_file:
                text = text[:max_file] + "\n… (gekürzt)"
            out[rel] = text
            total += len(text)
        return out

    def grade_context(self, session: Session, task: Task, submission) -> dict:
        """Material für den LLM-Grading-Prompt (nur relevante Teile!):
        student_files (aus Snapshot, ohne 👤-Pfade) + private_files
        (alle 👤-Dateien von der Disk + private Init-Artefakte vom Agenten).
        Kommt NIE zum LLM: alles Übrige (Mounts, Volume-Verwaltung …)."""
        fm = self.folder_map(session, task)
        file_acc = {f.path: f.access for f in self.task_files(session, task)}
        ctx: dict = {"student_files": {}, "private_files": {}}
        if submission.workspace_snapshot:
            with tempfile.TemporaryDirectory(prefix="aicampus-grade-") as tmp:
                try:
                    self.extract_snapshot(submission.workspace_snapshot, Path(tmp))
                except (ValueError, tarfile.TarError, OSError,
                        EOFError, zlib.error):
                    return ctx
                texts = self._collect_texts(Path(tmp))
                # NUR ✏️-Dateien des Students ans LLM — 🔒 (Task-Vorlage,
                # z. B. ro-Mount-Inhalte in älteren Snapshots) und 👤
                # (privat, kommt ohnehin nie ins Volume) bleiben draußen.
                for rel in [r for r in texts
                            if effective_file_access(
                                r, file_acc.get(r), fm) != "edit"]:
                    del texts[rel]
                ctx["student_files"] = texts
        total = 0
        for f in self.task_files(session, task):
            if effective_file_access(f.path, f.access, fm) != "hidden":
                continue
            if total >= MAX_LLM_TOTAL_CHARS:
                break
            p = file_disk_path(task.id, f.path)
            if not p.is_file():
                continue
            try:
                data = p.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:1024]:
                continue  # Binary — nicht in den LLM
            text = data.decode(errors="replace")
            if len(text) > MAX_LLM_FILE_CHARS:
                text = text[:MAX_LLM_FILE_CHARS] + "\n… (gekürzt)"
            ctx["private_files"][f.path] = text
            total += len(text)
        # Private Init-Artefakte (Textdateien) ergänzen
        for e in self._private_init_artifact_entries(session, task):
            if total >= MAX_LLM_TOTAL_CHARS or e["path"] in ctx["private_files"]:
                break
            if e.get("is_binary"):
                continue
            data = self._private_init_artifact_bytes(session, task, e["path"])
            if data is None or b"\x00" in data[:1024]:
                continue
            text = data.decode(errors="replace")
            if len(text) > MAX_LLM_FILE_CHARS:
                text = text[:MAX_LLM_FILE_CHARS] + "\n… (gekürzt)"
            ctx["private_files"][e["path"]] = text
            total += len(text)
        return ctx


# Singleton (stateless — Session kommt immer mit)
workspace_service = WorkspaceService()
