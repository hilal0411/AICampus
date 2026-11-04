"""
Sleep-Backup: beim Idle-Kill wird das Workspace-Volume tarren, auf den
AICampus-Server hochgeladen und danach gelöscht (Engine-Disk wird frei).

Der Arbeitsstand lebt dann auf dem Server (data/workspace_backups/);
wenn der Student zurückkehrt, spielt AICampus das Backup in das frisch
reanimierte Volume zurück (ensure_workspace → POST /workspaces/{key}/restore).

Inaktiv (altes Verhalten: Volume bleibt auf der Engine), wenn:
- AICAMPUS_URL nicht gesetzt ist,
- das Volume nicht hostseitig erreichbar ist (keine /var/lib/docker-Sicht),
- das Volume über der Cap (Task-Disk-Quota bzw. MAX_WORKSPACE_SIZE) liegt —
  dann wäre es auch der Submit-Snapshot zu groß; der Student muss aufräumen.
"""

import logging
import time
import urllib.error
import urllib.request

from . import auth, config, docker_ops

logger = logging.getLogger("compute_agent")

_RETRY_AFTER: dict[str, float] = {}  # key → Zeitstempel letzten Fehlschlags
_RETRY_WAIT = 300.0  # 5 min Backoff, wenn der Server nicht antritt


def maybe_sleep_backup(key: str) -> bool:
    """Sleep-Backup nach dem Idle-Kill (Volume liegt noch).

    Liefert True = erledigt (Registry touchen, kein Retry mehr),
    False = Retry im nächsten Reaper-Sweep (Upload-Fehler, mit Backoff).

    Läuft inline im Sweep: das Tar ist durch die Disk-Cap begrenzt
    (Quota bzw. 1 GB) und passiert nur im Schlaf — der 10-s-Zyklus
    blockiert nur im (seltenen) 1-GB-Fall nennenswert.
    """
    from .reaper import REGISTRY  # lazy: reaper importiert dieses Modul
    if not config.AICAMPUS_URL:
        return True  # Feature aus — Volume bleibt (altes Verhalten)
    last = _RETRY_AFTER.get(key)
    if last and time.time() - last < _RETRY_WAIT:
        return False
    try:
        spec = (REGISTRY.get(key) or {}).get("spec") or {}
        quota_mb = spec.get("disk_quota_mb")
        cap = quota_mb * 1024 * 1024 if quota_mb else config.MAX_WORKSPACE_SIZE
        usage = docker_ops.workspace_disk_usage(
            key, spec.get("readonly_paths") or [])
        if usage > cap:
            logger.info(
                "sleep-backup %s: Volume über Cap (%d MB) — Volume bleibt "
                "(Student muss aufräumen, wie beim Submit)",
                key, cap // (1024 * 1024))
            return True
        data = docker_ops.volume_backup_tar(key, cap)
        if data is None:
            logger.info(
                "sleep-backup %s: Volume nicht hostseitig erreichbar — "
                "Volume bleibt", key)
            return True  # in dieser Deployment nicht machbar — kein Retry
        if _upload(key, data):
            docker_ops.remove_volume_only(key)
            _RETRY_AFTER.pop(key, None)
            REGISTRY.set_sleep(key, {"at": time.time(), "size": len(data)})
            logger.info(
                "sleep-backup %s: %d bytes auf dem Server, Volume gelöscht",
                key, len(data))
            return True
        _RETRY_AFTER[key] = time.time()
        return False
    except Exception as e:
        logger.warning("sleep-backup %s fehlgeschlagen: %s", key, e)
        _RETRY_AFTER[key] = time.time()
        return False


def _upload(key: str, data: bytes) -> bool:
    """tar.gz an AICampus senden (2 Versuche).

    urllib (Stdlib) statt httpx — der Agent hält die Dependencies
    bewusst klein (s. requirements.txt)."""
    _course, task, student = docker_ops.key_parts(key)
    url = config.AICAMPUS_URL.rstrip("/") + "/api/agent/workspace-backup"
    token = auth.make_token(config.AGENT_KEY, f"ws-backup:{key}",
                             task_id=task, student_id=student)
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"X-Agent-Token": token,
                 "Content-Type": "application/gzip"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                if 200 <= resp.status < 300:
                    return True
                body = resp.read(200).decode("utf-8", "replace")
                logger.warning("sleep-backup %s: HTTP %s: %s",
                               key, resp.status, body)
        except urllib.error.HTTPError as e:
            body = e.read(200).decode("utf-8", "replace")
            logger.warning("sleep-backup %s: HTTP %s: %s",
                           key, e.code, body)
        except Exception as e:
            logger.warning("sleep-backup %s: %s", key, e)
        time.sleep(5 * attempt)
    return False
