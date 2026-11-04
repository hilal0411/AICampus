"""
Reaper: Container nach Idle-Timeout entfernen (Volume bleibt erhalten).

Registry (in-memory): key → {spec, last_activity}. Bei Agent-Neustart wird
die Registry aus den Docker-Labels rekonstruiert (Spec kommt per
idempotentem POST /workspaces nachgereicht). Workspaces mit aktivem
Run-Job sind immun (kein Kill mitten im Training).
"""

import threading
import time

from . import config, docker_ops, runs


class WorkspaceRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}

    def register(self, key: str, spec: dict) -> None:
        # Merge statt Replace: POST /workspaces kommt bei jedem Status-Poll,
        # und der Zustand (disk/mem/hold) darf dabei NIE wegfallen.
        with self._lock:
            item = self._items.get(key) or {}
            item["spec"] = spec
            item["last_activity"] = time.time()
            self._items[key] = item

    def touch(self, key: str) -> None:
        with self._lock:
            if key in self._items:
                self._items[key]["last_activity"] = time.time()

    def get(self, key: str) -> dict | None:
        with self._lock:
            item = self._items.get(key)
            return dict(item) if item else None

    def remove(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    def all_items(self) -> list[tuple[str, dict]]:
        """Alle Einträge als (key, info)-Paare (Kopien — Mutation geht
        nur über die Registry-Methoden)."""
        with self._lock:
            return [(k, dict(v)) for k, v in self._items.items()]

    def set_disk(self, key: str, disk: dict | None) -> None:
        """Disk-Quota-Zustand je Workspace (Cache für UI + Watchdog).

        `all_items()`/`get()` liefern Kopien — Mutationen gehen nur so.
        """
        with self._lock:
            if key in self._items:
                self._items[key]["disk"] = disk

    def set_hold(self, key: str, hold: dict) -> None:
        """Auto-Start-Hold: {reason: "user"|"quota"}.

        "user"  = manuelles Stoppen (endet erst mit explizitem Start),
        "quota" = Sperre nach Quota-Kill + Auto-Aufräumen (endet, wenn
                  der Student per ▶ Starten neu startet).
        """
        with self._lock:
            if key in self._items:
                self._items[key]["hold"] = hold

    def clear_hold(self, key: str) -> None:
        with self._lock:
            if key in self._items:
                self._items[key].pop("hold", None)

    def set_mem(self, key: str, mem: dict | None) -> None:
        """RAM-Usage je Workspace (UI-Cache, Reaper-Zyklus)."""
        with self._lock:
            if key in self._items:
                self._items[key]["mem"] = mem

    def rebuild_from_docker(self) -> int:
        """Nach Neustart: bekannte Container mit Grace-Periode auffüllen."""
        n = 0
        try:
            for ws in docker_ops.list_workspaces():
                with self._lock:
                    if ws["key"] not in self._items:
                        self._items[ws["key"]] = {
                            "spec": None,  # kommt via POST /workspaces
                            "last_activity": time.time(),  # volle Idle-Periode
                        }
                        n += 1
        except Exception:
            pass
        return n


REGISTRY = WorkspaceRegistry()


def _sweep() -> None:
    """Ein Reaper-Durchlauf: Disk-Quota, RAM-Cache, idle Container.

    RAM kommt aus EINEM docker-stats-Call für alle Container (ein Call
    je Container kostet 1–3 s — für den 10-s-Zyklus zu teuer).
    """
    now = time.time()
    active = runs.active_keys()
    mems = docker_ops.all_workspace_mem_usage()
    for key, info in REGISTRY.all_items():
        _check_disk_quota(key, info)
        mem = mems.get(key)
        if mem is not None:
            REGISTRY.set_mem(key, {"usage": mem, "at": now})
        if key in active:
            continue
        if now - info["last_activity"] <= config.IDLE_TIMEOUT:
            continue
        try:
            docker_ops.stop_container_only(key)
            REGISTRY.touch(key)  # Container weg → kein erneuter Kill nötig
        except Exception:
            pass


def _check_disk_quota(key: str, info: dict) -> None:
    """Disk-Quota-Watchdog: Warnung bei 100%, Stopp + Aufräumen bei 150%.

    Die Messung läuft hostseitig (Volume-Mountpoint des Docker-Daemons,
    s. docker_ops.workspace_disk_usage) — vom Container aus nicht
    killbar und auch bei gestopptem Container möglich. Bei >150% wird
    der Container SOFT-gestoppt (Volume bleibt) und automatisch
    aufgeräumt: die neuesten Dateien (typ. Logs/Checkpoints, s.
    purge_newest_files) werden gelöscht, bis wieder unter 100% liegt —
    die (älteren) Eigen-Dateien des Students bleiben erhalten. Danach
    bleibt der Auto-Start gesperrt (hold "quota"); der Student startet
    die Umgebung in Ruhe selbst per ▶ Starten (hebt die Sperre auf).
    Fehlende Quota (None/0) = kein Limit. Fehler bleiben stumm (best
    effort).
    """
    try:
        spec = info.get("spec") or {}
        quota_mb = spec.get("disk_quota_mb")
        # ALLE Workspaces messen (nicht nur Quota-Task): die Engine-UI
        # (Admin/Kurs-Settings) zeigt die Disk-Nutzung je Session.
        usage = docker_ops.workspace_disk_usage(
            key, spec.get("readonly_paths") or [])
        disk = dict(info.get("disk") or {})
        disk.update({"usage": usage, "quota_mb": quota_mb,
                     "at": time.time()})
        if not quota_mb:
            # Kein Limit → kein over/hard-State (UI zeigt nur die Nutzung).
            disk.update({"over": False, "hard": False, "killed_at": None})
            REGISTRY.set_disk(key, disk)
            return
        over = usage > quota_mb * 1024 * 1024
        hard = usage > int(quota_mb * config.QUOTA_KILL_FACTOR) * 1024 * 1024
        disk.update({"over": over, "hard": hard})
        if hard:
            if docker_ops.container_state(key) == "running":
                _note_runs_for_quota_kill(key, usage, quota_mb)
                # SOFT-Stopp (kein rm!): exited-Container + Volume bleiben.
                docker_ops.stop_container_soft(key)
                disk["killed_at"] = time.time()
            # Automatisches Aufräumen (hostseitig, geht auch bei
            # gestopptem Container): neueste Dateien weg, bis wieder
            # unter 100% — danach kann der Student in Ruhe ▶ Starten.
            n_purged, freed = docker_ops.purge_newest_files(
                key, quota_mb * 1024 * 1024,
                spec.get("readonly_paths") or [])
            if n_purged:
                disk["purged"] = {"count": n_purged, "bytes": freed,
                                  "at": time.time()}
                usage -= freed
                disk.update({"usage": usage,
                             "over": usage > quota_mb * 1024 * 1024,
                             "hard": usage > int(quota_mb *
                                                 config.QUOTA_KILL_FACTOR)
                                                 * 1024 * 1024})
            # Auto-Start bleibt gesperrt, bis der Student per ▶ Starten
            # neu startet (kein Auto-Start, kein Kill-Loop). Ein
            # manuelles Stoppen (hold "user") hat Vorrang und wird nicht
            # überschrieben.
            if (info.get("hold") or {}).get("reason") != "user":
                REGISTRY.set_hold(key, {"reason": "quota"})
        else:
            disk["killed_at"] = None
        REGISTRY.set_disk(key, disk)
    except Exception:
        pass


def _note_runs_for_quota_kill(key: str, usage: int, quota_mb: int) -> None:
    """Vor dem Quota-Kill: aktive Runs informieren + stoppen.

    Der Container-Kill beendet die Prozesse ohnehin; die Notiz macht den
    Grund in der Konsolen-Ausgabe verständlich.
    """
    usage_mb = usage // (1024 * 1024)
    for rid in runs.active_run_ids(key):
        job = runs.get_run(key, rid)
        if job is not None:
            with job._lock:
                if job.status in ("queued", "running"):
                    job.stderr.append(
                        f"[agent] Gestoppt: Speicherlimit um 50%+ "
                        f"überschritten ({usage_mb} MB von {quota_mb} MB) — "
                        "Container wurde beendet. Die neuesten Dateien "
                        "(z. B. Checkpoints/Logs) wurden automatisch "
                        "gelöscht, bis das Limit wieder eingehalten ist; "
                        "ältere Dateien bleiben erhalten.")
        runs.stop_run(key, rid)


def reaper_loop(stop_event: threading.Event) -> None:
    """Background-Loop (alle 30 s), läuft in einem eigenen Thread."""
    last_prune = 0.0
    while not stop_event.is_set():
        try:
            _sweep()
            runs.maybe_garbage_collect()
            docker_ops.cull_deadline_images()  # Task-Images nach Deadline
            now = time.time()
            if now - last_prune >= 3600:  # stündlich, moderat (s. Helper)
                last_prune = now
                docker_ops.prune_build_artifacts()  # Build-Cache + Dangling
        except Exception:
            pass
        stop_event.wait(config.REAPER_INTERVAL)
