"""
HTTP-Client zum Compute-Agenten (httpx).

Jede Request trägt ein HMAC-Op-Token (60 s, Scope je Operation).
Der Client kennt die Agent-Endpoints 1:1 und übersetzt Agent-Fehler
in ComputeAgentError (mit HTTP-Status). Kurze Downtimes (Tunnel-
Reconnect) werden per Single-Retry mit Backoff absorbiert.
"""

import base64
import time

import httpx

from compute_agent.auth import make_token
from config import COMPUTE_AGENT_URL, COMPUTE_AGENT_KEY


class ComputeAgentError(Exception):
    """Agent-Fehler (status: 4xx/5xx; message ist UI-tauglich)."""

    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.status = status
        self.message = message


class ComputeAgentUnavailable(ComputeAgentError):
    """Agent nicht erreichbar (Down/Timeout) — degradierte Modus."""

    def __init__(self, message: str = "Compute-Agent nicht erreichbar"):
        super().__init__(message, status=503)


class ComputeClient:
    def __init__(self, url: str | None = None, key: str | None = None,
                 timeout: float = 30.0):
        self.url = (url or COMPUTE_AGENT_URL).rstrip("/")
        self.key = key if key is not None else COMPUTE_AGENT_KEY
        self.timeout = timeout

    # ── Basis ─────────────────────────────────────────────────────

    def _headers(self, op: str, task_id: int | None = None,
                 student_id: int | None = None) -> dict:
        return {"X-Agent-Token":
                make_token(self.key, op, task_id=task_id, student_id=student_id)}

    def _request(self, method: str, path: str, op: str,
                 task_id: int | None = None, student_id: int | None = None,
                 json_body: dict | None = None, bytes_body: bytes | None = None,
                 timeout: float | None = None) -> httpx.Response:
        headers = self._headers(op, task_id, student_id)
        if bytes_body is not None:
            headers["Content-Type"] = "application/octet-stream"
        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                resp = httpx.request(
                    method, f"{self.url}{path}", headers=headers,
                    json=json_body, content=bytes_body,
                    timeout=timeout or self.timeout,
                )
                break
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                    httpx.RemoteProtocolError) as e:
                last_exc = e
                if attempt == 1:
                    time.sleep(0.5)
                    continue
                raise ComputeAgentUnavailable(str(e)) from e
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text[:300])
            except Exception:
                detail = resp.text[:300]
            raise ComputeAgentError(str(detail), status=resp.status_code)
        return resp

    # ── Health & Übersicht ────────────────────────────────────────

    def health(self, timeout: float = 5.0) -> dict:
        resp = self._request("GET", "/health", op="health", timeout=timeout)
        return resp.json()

    def list_workspaces(self) -> list[dict]:
        resp = self._request("GET", "/workspaces", op="list")
        return resp.json()["workspaces"]

    # ── Workspace-Lifecycle ───────────────────────────────────────

    def create_workspace(self, key: str, spec,
                         starter_files: list | None = None,
                         folders: list | None = None) -> dict:
        course, task, student = _key_parts(key)
        body = {"key": key, "spec": spec}
        if starter_files:
            body["starter_files"] = starter_files
        if folders:
            body["folders"] = folders
        resp = self._request("POST", "/workspaces", op=f"ws:{key}",
                             task_id=task, student_id=student, json_body=body,
                             timeout=120)
        return resp.json()

    def delete_workspace(self, key: str) -> None:
        course, task, student = _key_parts(key)
        self._request("DELETE", f"/workspaces/{key}", op=f"ws:{key}",
                      task_id=task, student_id=student)

    # ── Dateien ───────────────────────────────────────────────────

    def list_files(self, key: str) -> list[dict]:
        course, task, student = _key_parts(key)
        resp = self._request("GET", f"/workspaces/{key}/files", op=f"ws:{key}",
                             task_id=task, student_id=student)
        return resp.json()["files"]

    def list_dirs(self, key: str) -> list[str]:
        course, task, student = _key_parts(key)
        resp = self._request("GET", f"/workspaces/{key}/dirs", op=f"ws:{key}",
                             task_id=task, student_id=student)
        return resp.json().get("dirs") or []

    def create_dir(self, key: str, path: str) -> None:
        course, task, student = _key_parts(key)
        self._request("POST", f"/workspaces/{key}/dirs", op=f"ws:{key}",
                      task_id=task, student_id=student,
                      json_body={"path": path})

    def read_file(self, key: str, path: str) -> bytes:
        course, task, student = _key_parts(key)
        resp = self._request("GET", f"/workspaces/{key}/files/{path}", op=f"ws:{key}",
                             task_id=task, student_id=student, timeout=120)
        return resp.content

    def write_file(self, key: str, path: str, data: bytes) -> dict:
        course, task, student = _key_parts(key)
        resp = self._request("PUT", f"/workspaces/{key}/files/{path}", op=f"ws:{key}",
                             task_id=task, student_id=student,
                             bytes_body=data, timeout=120)
        return resp.json()

    def delete_file(self, key: str, path: str) -> None:
        course, task, student = _key_parts(key)
        self._request("DELETE", f"/workspaces/{key}/files/{path}", op=f"ws:{key}",
                      task_id=task, student_id=student)

    def move_file(self, key: str, src: str, dst: str) -> None:
        course, task, student = _key_parts(key)
        self._request("POST", f"/workspaces/{key}/files/move", op=f"ws:{key}",
                      task_id=task, student_id=student,
                      json_body={"src": src, "dst": dst})

    # ── Ausführung ────────────────────────────────────────────────

    def exec_sync(self, key: str, command: str, timeout: int) -> dict:
        course, task, student = _key_parts(key)
        resp = self._request("POST", f"/workspaces/{key}/exec", op=f"ws:{key}",
                             task_id=task, student_id=student,
                             json_body={"command": command, "async": False,
                                        "timeout": timeout},
                             timeout=timeout + 15)
        return resp.json()

    def start_run(self, key: str, command: str, timeout: int | None = None) -> dict:
        course, task, student = _key_parts(key)
        body = {"command": command, "async": True}
        if timeout:
            body["timeout"] = timeout
        resp = self._request("POST", f"/workspaces/{key}/exec", op=f"ws:{key}",
                             task_id=task, student_id=student, json_body=body)
        return resp.json()

    def run_status(self, key: str, run_id: str) -> dict:
        course, task, student = _key_parts(key)
        resp = self._request("GET", f"/workspaces/{key}/runs/{run_id}", op=f"ws:{key}",
                             task_id=task, student_id=student, timeout=60)
        return resp.json()

    def list_runs(self, key: str) -> list[dict]:
        course, task, student = _key_parts(key)
        resp = self._request("GET", f"/workspaces/{key}/runs", op=f"ws:{key}",
                             task_id=task, student_id=student)
        return resp.json()["runs"]

    def stop_run(self, key: str, run_id: str) -> None:
        course, task, student = _key_parts(key)
        self._request("POST", f"/workspaces/{key}/runs/{run_id}/stop", op=f"ws:{key}",
                      task_id=task, student_id=student)

    def disk(self, key: str) -> dict:
        """Disk-Quota-Status des Workspaces: {usage (Bytes), quota_mb,
        over, hard, mem_usage (Bytes)}."""
        course, task, student = _key_parts(key)
        resp = self._request("GET", f"/workspaces/{key}/disk", op=f"ws:{key}",
                             task_id=task, student_id=student, timeout=60)
        return resp.json()

    def stop_workspace(self, key: str) -> dict:
        """Manueller Container-Stopp (Volume/Dateien bleiben erhalten)."""
        course, task, student = _key_parts(key)
        resp = self._request("POST", f"/workspaces/{key}/stop", op=f"ws:{key}",
                             task_id=task, student_id=student, timeout=120)
        return resp.json()

    def restore_volume(self, key: str, data: bytes) -> dict:
        """Sleep-Backup (tar.gz) in das (frische) Volume entpacken."""
        course, task, student = _key_parts(key)
        resp = self._request("POST", f"/workspaces/{key}/restore", op=f"ws:{key}",
                             task_id=task, student_id=student,
                             bytes_body=data, timeout=600)
        return resp.json()

    def start_workspace(self, key: str) -> dict:
        """Expliziter Container-Start (hebt Stop- Sperre)."""
        course, task, student = _key_parts(key)
        resp = self._request("POST", f"/workspaces/{key}/start", op=f"ws:{key}",
                             task_id=task, student_id=student, timeout=120)
        return resp.json()

    def ports(self, key: str) -> list[dict]:
        """Im Container lauschende Ports: [{port, pid}] (Preview-UI)."""
        course, task, student = _key_parts(key)
        resp = self._request("GET", f"/workspaces/{key}/ports", op=f"ws:{key}",
                             task_id=task, student_id=student, timeout=60)
        return resp.json().get("ports") or []

    def kill_port(self, key: str, port: int, force: bool = False) -> None:
        """Prozessbaum eines lauschenden Ports beenden.

        ohne force: SIGTERM (+5 s Wartezeit) · force: SIGKILL.
        Port frei → 404 · danach noch belegt → 409.
        """
        course, task, student = _key_parts(key)
        self._request("POST",
                      f"/workspaces/{key}/ports/{port}/kill"
                      + ("?force=true" if force else ""),
                      op=f"ws:{key}", task_id=task, student_id=student)

    def snapshot(self, key: str) -> bytes:
        course, task, student = _key_parts(key)
        resp = self._request("GET", f"/workspaces/{key}/snapshot", op=f"ws:{key}",
                             task_id=task, student_id=student, timeout=600)
        return resp.content

    # ── Assets & Task-Images (.init.sh-Build) ────────────────────

    def sync_assets(self, course: int, task: int, files: list[dict],
                    delete_missing: bool = False,
                    folders: list[str] | None = None) -> dict:
        body: dict = {"files": files, "delete_missing": delete_missing}
        if folders:
            body["folders"] = folders
        # Timeout > INIT_TIMEOUT: der Agent hält während eines laufenden
        # Init-Builds die Task-Lock und der Sync wartet darauf, bevor er
        # schreibt (Manifest-Race-Schutz, s. docker_ops.asset_sync_lock).
        resp = self._request("POST", f"/assets/{course}/{task}",
                             op=f"task:{course}:{task}", task_id=task,
                             json_body=body, timeout=3600)
        return resp.json()

    def list_assets(self, course: int, task: int) -> list[dict]:
        resp = self._request("GET", f"/assets/{course}/{task}",
                             op=f"task:{course}:{task}", task_id=task)
        return resp.json()["files"]

    def remove_init_artifact(self, course: int, task: int, path: str) -> dict:
        """Getarnten Manifest-Eintrag (Task-Datei fälschlich als
        Init-Artefakt gelistet) + Datei auf dem Agenten entfernen."""
        resp = self._request("DELETE", f"/tasks/{course}/{task}/init-artifact",
                             op=f"task:{course}:{task}", task_id=task,
                             json_body={"path": path}, timeout=60)
        return resp.json()

    def init_build(self, course: int, task: int, image: str,
                   init_hash: str, deadline: str | None = None,
                   readonly_paths: list[str] | None = None,
                   hidden_paths: list[str] | None = None,
                   init_b64: str | None = None,
                   init_private_b64: str | None = None,
                   folders: list[str] | None = None,
                   force: bool = False) -> dict:
        """Task-Image-Build (2-Phasen: .init.sh + .init_hidden.sh) starten;
        startet nur den Background-Build → kurzer Timeout.

        readonly_paths/hidden_paths: Top-Level-🔒/👤-Pfade (rw-Mounts im
        Build, aus der privaten Agenten-Region bzw. dem Asset-Dir);
        init_b64/init_private_b64: 👤-Skripte (b64, persistiert in
        .private/); folders: Ordner-Pfade der Aufgabe (explizite +
        implizite) — der Agent legt sie in den Build-Quellen an, damit
        .init.sh auch in bestehende Ordner schreiben kann (Build-Umgebung
        ist sonst leer).
        """
        body: dict = {"image": image, "init_hash": init_hash}
        if deadline:
            body["deadline"] = deadline
        if readonly_paths:
            body["readonly_paths"] = readonly_paths
        if hidden_paths:
            body["hidden_paths"] = hidden_paths
        if init_b64 is not None:
            body["init_b64"] = init_b64
        if init_private_b64 is not None:
            body["init_private_b64"] = init_private_b64
        if folders:
            body["folders"] = folders
        if force:
            body["force"] = True
        resp = self._request("POST", f"/tasks/{course}/{task}/init-build",
                             op=f"task:{course}:{task}", task_id=task,
                             json_body=body, timeout=60)
        return resp.json()

    def init_status(self, course: int, task: int, init_hash: str) -> dict:
        resp = self._request(
            "GET", f"/tasks/{course}/{task}/init-status?init_hash={init_hash}",
            op=f"task:{course}:{task}", task_id=task, timeout=60)
        return resp.json()

    def init_artifacts(self, course: int, task: int,
                       scope: str = "all") -> dict:
        """Init-Artefakte (Manifest) je Scope: shared|seed|private|all.

        Liefert {files: [{path, size, is_binary, scope}]} — Pfade sind
        Workspace-Relative (Ziel-Pfad im Baum)."""
        resp = self._request(
            "GET", f"/tasks/{course}/{task}/init-artifacts?scope={scope}",
            op=f"task:{course}:{task}", task_id=task, timeout=60)
        return resp.json()

    def init_artifact_file(self, course: int, task: int, scope: str,
                           path: str) -> tuple[int, bytes]:
        """Inhalt eines Init-Artefakts (raw, binary-safe)."""
        import urllib.parse
        resp = self._request(
            "GET", f"/tasks/{course}/{task}/init-artifact-file?scope={scope}"
            f"&path={urllib.parse.quote(path, safe='/')}",
            op=f"task:{course}:{task}", task_id=task, timeout=120)
        size = int(resp.headers.get("Content-Length") or 0)
        return size, resp.content

    def init_stop(self, course: int, task: int, init_hash: str) -> dict:
        resp = self._request("POST", f"/tasks/{course}/{task}/init-build/stop",
                             op=f"task:{course}:{task}", task_id=task,
                             json_body={"init_hash": init_hash}, timeout=60)
        return resp.json()

    def delete_task_images(self, course: int, task: int) -> dict:
        resp = self._request("DELETE", f"/task-images/{course}/{task}",
                             op=f"task:{course}:{task}", task_id=task, timeout=60)
        return resp.json()

    def delete_assets(self, course: int, task: int) -> None:
        self._request("DELETE", f"/assets/{course}/{task}",
                      op=f"task:{course}:{task}", task_id=task)

    # ── Image-Specs (installieren, listen, entfernen) ──────────────

    def images_list(self) -> list[dict]:
        resp = self._request("GET", "/images", op="images")
        return resp.json()["images"]

    def images_install(self, spec: dict) -> dict:
        """Idempotent: vorhanden → ready, sonst (Background-)Build starten.

        spec: {name, dockerfile}
        """
        resp = self._request("POST", "/images", op="images",
                             json_body=spec, timeout=60)
        return resp.json()

    def images_spec_status(self, specs: list[dict]) -> list[dict]:
        """Installationsstatus der Specs auf der Engine (UI-Filter).

        specs: [{name, dockerfile}, …] → je Spec {refs, installed, building}
        in gleicher Reihenfolge.
        """
        resp = self._request("POST", "/images/spec-status", op="images",
                             json_body={"specs": specs}, timeout=60)
        return resp.json().get("status") or []

    def images_remove(self, ref: str, force: bool = False) -> dict:
        """Image entfernen; force beendet/entfernt die es nutzenden Container.

        Liefert {ok, removed_containers} des Agents.
        """
        resp = self._request("DELETE", f"/images/{ref}", op="images",
                             json_body={"force": force})
        return resp.json()


def _key_parts(key: str) -> tuple[int, int, int]:
    parts = key.split("-")  # ws-{course}-{task}-{student}
    if len(parts) != 4 or parts[0] != "ws":
        raise ComputeAgentError(f"Ungültiger Workspace-Key: {key!r}", 400)
    return int(parts[1]), int(parts[2]), int(parts[3])
