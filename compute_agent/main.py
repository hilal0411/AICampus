"""
Compute-Agent — FastAPI-App (eigener Service auf dem Compute-Server).

Spricht die docker-CLI, verwaltet die Workspace-Container (pro
Aufgabe × Student) und ist die Vertrauensgrenze: Workspace-Specs werden
hier ERNEUT validiert. Erreichbar nur über 127.0.0.1 (SSH-Tunnel) mit
HMAC-Op-Tokens.

Start:  python -m compute_agent
"""

import asyncio
import base64
import functools
import json
import logging
import re
import threading
import time

logger = logging.getLogger("compute_agent")

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import Response
from starlette.websockets import WebSocketDisconnect

from . import auth, config, docker_ops, image_spec, preview, runs, spec, terminal, version
from .reaper import REGISTRY, reaper_loop

app = FastAPI(title="AICampus Compute-Agent", version="1.0")
_stop_event = threading.Event()


@app.on_event("startup")
async def _startup() -> None:
    rebuilt = REGISTRY.rebuild_from_docker()
    logger.info("Agent start (Port %s), Registry: %d Workspace(s) aus Docker",
                config.PORT, rebuilt)
    threading.Thread(target=reaper_loop, args=(_stop_event,), daemon=True).start()
    await preview.server.start()  # Preview-Tunnel (zweiter Port, s. preview.py)


@app.on_event("shutdown")
async def _shutdown() -> None:
    _stop_event.set()
    await preview.server.stop()


def _op(payload: dict, op: str) -> None:
    auth.require_op(payload, op)


# ── Fehler-Übersetzung ────────────────────────────────────────────

def _translate(fn):
    """DockerError/SpecError → HTTPException (einmalig, alle Endpoints).

    functools.wraps ist zwingend: ohne __wrapped__ würde FastAPI die
    Wrapper-Signatur (*args, **kwargs) inspizieren und die Dependencies
    der echten Funktion nicht erkennen. async-Functions brauchen einen
    eigenen awaitenden Wrapper (sync-Wrapper würde die Coroutine nie ausführen).
    """
    if asyncio.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except docker_ops.DockerError as e:
                raise HTTPException(status_code=e.status, detail=str(e))
            except spec.SpecError as e:
                raise HTTPException(status_code=422, detail=str(e))
            except image_spec.ImageSpecError as e:
                raise HTTPException(status_code=422, detail=str(e))
        return awrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except docker_ops.DockerError as e:
            raise HTTPException(status_code=e.status, detail=str(e))
        except spec.SpecError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except image_spec.ImageSpecError as e:
            raise HTTPException(status_code=422, detail=str(e))
    return wrapper


# ── Health & Übersicht ────────────────────────────────────────────

@app.get("/health")
@_translate
def health(payload: dict = Depends(auth.verify_token)) -> dict:
    _op(payload, "health")
    docker_ok = docker_ops.docker_available()
    gpu = docker_ops.gpu_available() if docker_ok else False
    return {
        "ok": docker_ok,
        "docker": docker_ok,
        "gpu": gpu,
        "gpu_info": docker_ops.gpu_info() if gpu else "",
        "gpus": docker_ops.gpu_list() if docker_ok else [],
        "gpu_max_jobs": config.GPU_MAX_JOBS,
        "idle_timeout": config.IDLE_TIMEOUT,
        "workspaces": len(REGISTRY.all_items()),
        "version": version.agent_version(),
        "time": time.time(),
    }


@app.get("/workspaces")
@_translate
def workspaces_list(payload: dict = Depends(auth.verify_token)) -> dict:
    _op(payload, "list")
    states = {w["key"]: w["state"] for w in docker_ops.list_workspaces()}
    out = []
    for key, info in REGISTRY.all_items():
        mem = info.get("mem")
        disk = info.get("disk")
        out.append({
            "key": key,
            "state": states.get(key, "absent"),
            # Image aus dem gemeldeten Spec (Registry-Rebuild: erst per
            # POST /workspaces nachgereicht → vorab "").
            "image": (info.get("spec") or {}).get("image") or "",
            "last_activity": info["last_activity"],
            # Reaper-Zyklus (10 s) je Workspace: RAM nur bei laufenden
            # (docker stats), Disk immer (hostseitig am Volume, geht
            # auch bei gestopptem Container). None = (noch) ungemessen.
            "mem": mem.get("usage") if isinstance(mem, dict) else None,
            "disk": disk.get("usage") if isinstance(disk, dict) else None,
            "disk_quota_mb": disk.get("quota_mb") if isinstance(disk, dict) else None,
            # Sleep-Backup: {at, size} = Volume liegt als tar.gz auf dem
            # AICampus-Server (Volume auf dieser Engine ist gelöscht).
            "slept": info.get("sleep"),
        })
    return {"workspaces": out}


# ── Workspace-Lifecycle ───────────────────────────────────────────

@app.post("/workspaces")
@_translate
def workspace_create(body: dict,
                     payload: dict = Depends(auth.verify_token)) -> dict:
    key = str(body.get("key", ""))
    if not docker_ops._KEY_RE.match(key):
        raise HTTPException(status_code=400, detail="Ungültiger Workspace-Key")
    _op(payload, f"ws:{key}")
    spec_dict = spec.parse_spec(body.get("spec") or {})
    if not spec_dict.get("image"):
        raise spec.SpecError(
            "image fehlt (wird normalerweise von AICampus aus der "
            "Image-Spec der Aufgabe injiziert)")
    docker_ops.key_parts(key)  # Key-Format validiert
    REGISTRY.register(key, spec_dict)
    # Auto-Start-Hold: "user" = manuell gestoppt (endet mit ▶ Starten),
    # "quota" = Sperre nach Quota-Kill + Auto-Aufräumen (endet, wenn der
    # Student per ▶ Starten neu startet — s. reaper).
    info = REGISTRY.get(key) or {}
    hold = info.get("hold")
    if hold and docker_ops.container_state(key) != "running":
        return {"key": key, "state": "stopped", "fresh": False,
                "held": hold.get("reason")}
    result = docker_ops.ensure_container(key, spec_dict)
    if result.get("fresh"):
        # Frisches Volume: erst die ✏️-Init-Artefakt-Seeds aus dem
        # Seed-Store (.seeds/), dann die Starter-Dateien (gewinnen bei
        # Kollision).
        course, task, _student = docker_ops.key_parts(key)
        try:
            n_seeds = docker_ops.seed_volume(key, course, task)
            if n_seeds:
                result["seeds_written"] = n_seeds
        except docker_ops.DockerError:
            pass  # Seeds sind optional (keine/fehlgeschlagene Init-Artefakte)
        if body.get("starter_files") or body.get("folders"):
            n = docker_ops.write_starter_files(
                key, body.get("starter_files") or [],
                folders=[str(d) for d in (body.get("folders") or [])
                         if str(d).strip()] or None)
            result["starter_files_written"] = n
    result["key"] = key
    result["gpu"] = spec_dict["gpus"] != "none"
    return result


@app.delete("/workspaces/{key}")
@_translate
def workspace_delete(key: str, payload: dict = Depends(auth.verify_token)) -> dict:
    _op(payload, f"ws:{key}")
    docker_ops.remove_workspace(key)
    REGISTRY.remove(key)
    return {"ok": True}


@app.post("/workspaces/{key}/stop")
@_translate
def workspace_stop(key: str, payload: dict = Depends(auth.verify_token)) -> dict:
    """Manueller Container-Stopp (Volume + Dateien bleiben erhalten).

    Setzt einen Hold, damit der Status-Poll (POST /workspaces) den
    Container nicht sofort wieder auto-startet."""
    _op(payload, f"ws:{key}")
    if not REGISTRY.get(key):
        raise HTTPException(status_code=404, detail="Workspace unbekannt")
    REGISTRY.set_hold(key, {"reason": "user", "until": None})
    docker_ops.stop_container_soft(key)
    _touch(key)
    return {"ok": True, "state": "stopped"}


@app.post("/workspaces/{key}/restore")
@_translate
async def workspace_restore(key: str, request: Request,
                            payload: dict = Depends(auth.verify_token)) -> dict:
    """Sleep-Backup (tar.gz) in das frische Volume entpacken (hostseitig).

    Wird von AICampus direkt nach POST /workspaces mit fresh=true gerufen,
    wenn auf dem Server ein Backup für diesen Key liegt. Bestehende
    (Starter-)Dateien werden vom Backup überschrieben."""
    _op(payload, f"ws:{key}")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Body (tar.gz) fehlt")
    _touch(key)
    n = docker_ops.restore_volume_tar(key, data)
    return {"ok": True, "files": n}


@app.post("/workspaces/{key}/start")
@_translate
def workspace_start(key: str, payload: dict = Depends(auth.verify_token)) -> dict:
    """Expliziter Container-Start (hebt manuellen/Quota-Hold)."""
    _op(payload, f"ws:{key}")
    info = REGISTRY.get(key)
    if not info or not info.get("spec"):
        raise HTTPException(
            status_code=409,
            detail="Workspace unbekannt — erst POST /workspaces (mit Spec)")
    REGISTRY.clear_hold(key)
    result = docker_ops.ensure_container(key, info["spec"])
    _touch(key)
    return result


# ── Dateien ─────────────────────────────────────────────────────

def _touch(key: str) -> None:
    REGISTRY.touch(key)


def _reconcile(key: str) -> None:
    """Datei-Listings zeigen das AKTUELLE Mount-Layout: wenn das Registry
    eine Spec hält (aus dem letzten Status-Poll), wird der Container
    dagegen abgeglichen — sonst könnte ein reiner List-Refresh (ohne
    Status) nach einer Tutor-Änderung der Zugriffs-Klassen noch das alte
    ro-Mount-Layout anzeigen.

    NUR bei bereits laufendem Container: ensure_container würde einen
    gestoppten (oder fehlenden) Container sonst on-demand (re-)starten
    und den manuellen Stopp aufheben."""
    info = REGISTRY.get(key)
    sp = info.get("spec") if info else None
    if sp and docker_ops.container_state(key) == "running":
        docker_ops.ensure_container(key, sp)


@app.get("/workspaces/{key}/files")
@_translate
def files_list(key: str, payload: dict = Depends(auth.verify_token)) -> dict:
    _op(payload, f"ws:{key}")
    _touch(key)
    _reconcile(key)
    files = docker_ops.list_files(key)
    return {"files": files, "total": sum(f["size"] for f in files)}


@app.get("/workspaces/{key}/dirs")
@_translate
def dirs_list(key: str, payload: dict = Depends(auth.verify_token)) -> dict:
    _op(payload, f"ws:{key}")
    _touch(key)
    _reconcile(key)
    return {"dirs": docker_ops.list_dirs(key)}


@app.post("/workspaces/{key}/dirs")
@_translate
async def dirs_create(key: str, request: Request,
                      payload: dict = Depends(auth.verify_token)) -> dict:
    """(Möglicherweise leeren) Ordner anlegen ({path})."""
    _op(payload, f"ws:{key}")
    _touch(key)
    body = await request.json()
    path = str(body.get("path") or "")
    if not path:
        raise HTTPException(status_code=422, detail="Feld 'path' fehlt.")
    info = REGISTRY.get(key)
    spec_dict = info.get("spec") if info else None
    docker_ops.create_dir(key, path, spec_dict)
    return {"ok": True, "path": path}


@app.get("/workspaces/{key}/files/{path:path}")
@_translate
def files_read(key: str, path: str,
               payload: dict = Depends(auth.verify_token)) -> Response:
    _op(payload, f"ws:{key}")
    _touch(key)
    data = docker_ops.read_file(key, path)
    return Response(content=data, media_type="application/octet-stream")


@app.put("/workspaces/{key}/files/{path:path}")
@_translate
async def files_write(key: str, path: str, request: Request,
                      payload: dict = Depends(auth.verify_token)) -> dict:
    _op(payload, f"ws:{key}")
    _touch(key)
    data = await request.body()
    if len(data) > config.MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="Datei zu groß (max. 50 MB)")
    info = REGISTRY.get(key)
    spec_dict = info.get("spec") if info else None
    size = docker_ops.write_file(key, path, data, spec_dict)
    return {"ok": True, "size": size}


@app.post("/workspaces/{key}/files/move")
@_translate
async def files_move(key: str, request: Request,
                     payload: dict = Depends(auth.verify_token)) -> dict:
    """Datei verschieben/umbenennen ({src, dst}) — nur innerhalb der
    schreibbaren Workspace-Zone (Read-only-Guards in docker_ops)."""
    _op(payload, f"ws:{key}")
    _touch(key)
    body = await request.json()
    src = str(body.get("src") or "")
    dst = str(body.get("dst") or "")
    if not src or not dst:
        raise HTTPException(status_code=422, detail="Felder 'src' und 'dst' fehlen.")
    info = REGISTRY.get(key)
    spec_dict = info.get("spec") if info else None
    docker_ops.move_file(key, src, dst, spec_dict)
    return {"ok": True, "path": dst}


@app.delete("/workspaces/{key}/files/{path:path}")
@_translate
def files_delete(key: str, path: str,
                 payload: dict = Depends(auth.verify_token)) -> dict:
    _op(payload, f"ws:{key}")
    _touch(key)
    info = REGISTRY.get(key)
    spec_dict = info.get("spec") if info else None
    docker_ops.delete_path(key, path, spec_dict)
    return {"ok": True}


# ── Ausführung ────────────────────────────────────────────────────

@app.post("/workspaces/{key}/exec")
@_translate
def exec_cmd(key: str, body: dict,
             payload: dict = Depends(auth.verify_token)) -> dict:
    _op(payload, f"ws:{key}")
    _touch(key)
    info = REGISTRY.get(key)
    if not info or not info.get("spec"):
        raise HTTPException(
            status_code=409,
            detail="Workspace unbekannt — erst POST /workspaces (mit Spec)")
    sp = info["spec"]
    # (Kein Quota-Block hier mehr: der Watchdog warnt bei 100% und
    # stoppt den Container hart bei 150% — s. reaper._check_disk_quota.)
    # Freie Commands kommen NUR von AICampus (Backend baut sie aus den
    # Task-Skripten); es gibt kein Default mehr aus der Spec.
    command = str(body.get("command") or "")
    if not command.strip():
        raise HTTPException(status_code=422, detail="Kein Befehl angegeben")
    timeout = min(int(body.get("timeout") or sp["timeout"]), config.MAX_TIMEOUT)
    gpu = sp["gpus"] != "none"

    if body.get("async"):
        job = runs.start_run(key, command, working_dir=sp["working_dir"],
                             timeout=timeout, gpu=gpu)
        return {"run_id": job.run_id, "status": job.status, "gpu": gpu}
    result = docker_ops.exec_sync(key, command, working_dir=sp["working_dir"],
                                  timeout=timeout)
    result.update({"gpu": gpu})
    return result


@app.get("/workspaces/{key}/runs")
@_translate
def runs_list(key: str, payload: dict = Depends(auth.verify_token)) -> dict:
    _op(payload, f"ws:{key}")
    return {"runs": runs.list_runs(key)}


@app.get("/workspaces/{key}/runs/{run_id}")
@_translate
def run_status(key: str, run_id: str,
               payload: dict = Depends(auth.verify_token)) -> dict:
    _op(payload, f"ws:{key}")
    job = runs.get_run(key, run_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Run nicht (mehr) verfügbar")
    return job.to_status()


@app.post("/workspaces/{key}/runs/{run_id}/stop")
@_translate
def run_stop(key: str, run_id: str,
             payload: dict = Depends(auth.verify_token)) -> dict:
    _op(payload, f"ws:{key}")
    ok = runs.stop_run(key, run_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Run nicht (mehr) aktiv")
    return {"ok": True}


# ── Ports & Terminal (Preview) ─────────────────────────────────

@app.get("/workspaces/{key}/ports")
@_translate
def workspace_ports(key: str, payload: dict = Depends(auth.verify_token)) -> dict:
    """Im Container lauschende Ports: {ports: [{port, pid}]} (Preview-UI)."""
    _op(payload, f"ws:{key}")
    _touch(key)
    return {"ports": docker_ops.list_workspace_ports(key)}


@app.post("/workspaces/{key}/ports/{port}/kill")
@_translate
def workspace_port_kill(key: str, port: int, force: bool = False,
                        payload: dict = Depends(auth.verify_token)) -> dict:
    """Prozessbaum des Ports beenden (verifiziert per Re-Scan).

    Port nicht belegt → 404 · danach noch belegt → 409 (UI kann mit
    force=true erneut versuchen: SIGKILL statt SIGTERM).
    """
    _op(payload, f"ws:{key}")
    _touch(key)
    docker_ops.kill_workspace_port(key, port, force=force)
    return {"ok": True}


@app.websocket("/workspaces/{key}/terminal")
async def terminal_ws(ws_conn: WebSocket, key: str) -> None:
    """Terminal (PTY) im Workspace-Container.

    Token per Query-Param (kein Header-Mechanismus bei WS). Optional
    ?cmd=<relativer Pfad>: PTY startet das Skript direkt als
    Hauptprozess (statt interaktiver Shell) — für die Play-Buttons im
    Dateibaum. Protokoll: Binary = rohes Terminal-Input/Output ·
    Text = Kontrolle ({"cols","rows"} init/resize ·
    {"type":"exit","code"} bei Prozess-Ende).
    """
    try:
        docker_ops.key_parts(key)
    except docker_ops.DockerError:
        await ws_conn.close(code=4400)
        return
    try:
        payload = auth.verify_token_raw(ws_conn.query_params.get("token", ""))
    except ValueError:
        await ws_conn.close(code=4401)
        return
    if payload.get("op") not in ("*", f"ws:{key}"):
        await ws_conn.close(code=4403)
        return
    raw_cmd = ws_conn.query_params.get("cmd")
    script_path = None
    if raw_cmd:
        cp = raw_cmd.replace("\\", "/").lstrip("/")
        if not cp or any(seg == ".." for seg in cp.split("/")):
            await ws_conn.close(code=4409)
            return
        script_path = cp
    await ws_conn.accept()
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, docker_ops._ensure_running, key)
    except docker_ops.DockerError:
        await ws_conn.send_text(json.dumps(
            {"type": "error", "message": "Workspace-Container nicht verfügbar"}))
        await ws_conn.close(code=1011)
        return
    REGISTRY.touch(key)  # offenes Terminal schützt vor Idle-Kill

    exit_code: dict = {}
    exit_event = asyncio.Event()

    def _on_output(data: bytes) -> None:
        asyncio.create_task(_safe_send_bytes(ws_conn, data))

    def _on_exit(code) -> None:
        exit_code["code"] = code
        exit_event.set()

    session = terminal.TerminalSession(
        key, loop, _on_output, _on_exit, script_path)

    async def _keepalive() -> None:
        while True:
            await asyncio.sleep(60)
            REGISTRY.touch(key)

    keep_task = asyncio.create_task(_keepalive())

    async def _recv() -> None:
        try:
            while True:
                msg = await ws_conn.receive()
                mtype = msg.get("type")
                if mtype == "websocket.disconnect":
                    return
                if mtype != "websocket.receive":
                    continue
                data = msg.get("bytes")
                if data is not None:
                    await session.write_input(data)
                    continue
                text = msg.get("text")
                if not text:
                    continue
                try:
                    ctrl = json.loads(text)
                except ValueError:
                    # Eingabe per Text-Frame (Defensive: Clients senden
                    # Terminal-Input primär als Binary).
                    await session.write_input(text.encode("utf-8"))
                    continue
                if not isinstance(ctrl, dict):
                    await session.write_input(text.encode("utf-8"))
                    continue
                cols = ctrl.get("cols")
                if cols:
                    await session.resize(cols, ctrl.get("rows", 40))
        except WebSocketDisconnect:
            return
        finally:
            session.close()

    recv_task = asyncio.create_task(_recv())
    try:
        await exit_event.wait()
    finally:
        keep_task.cancel()
        recv_task.cancel()
        session.close()
        try:
            await ws_conn.send_text(json.dumps(
                {"type": "exit", "code": exit_code.get("code")}))
        except Exception:
            pass
        try:
            await ws_conn.close()
        except Exception:
            pass


async def _safe_send_bytes(ws_conn: WebSocket, data: bytes) -> None:
    try:
        await ws_conn.send_bytes(data)
    except Exception:
        pass


@app.get("/workspaces/{key}/snapshot")
@_translate
def snapshot(key: str, payload: dict = Depends(auth.verify_token)) -> Response:
    """tar.gz des /workspace-Volumes (für „Abgeben" + Tutor-Review).

    Enthaelt NUR das schreibbare Named-Volume (Student-Dateien) —
    🔒-ro-Mounts (Assets/Datasets) sind ausgeschlossen. Cap = Task-Disk-
    Quota (Fallback: MAX_WORKSPACE_SIZE) — bei Überschreitung schlägt
    der Snapshot fehl, bis der Student aufräumt.
    """
    _op(payload, f"ws:{key}")
    _touch(key)
    spec = ((REGISTRY.get(key) or {}).get("spec") or {})
    quota_mb = spec.get("disk_quota_mb")
    cap = quota_mb * 1024 * 1024 if quota_mb else config.MAX_WORKSPACE_SIZE
    data = docker_ops.snapshot(
        key, cap=cap, exclude_paths=spec.get("readonly_paths"))
    return Response(content=data, media_type="application/gzip",
                    headers={"Content-Disposition":
                             f'attachment; filename="{key}-workspace.tar.gz"'})


@app.get("/workspaces/{key}/disk")
@_translate
def workspace_disk(key: str, payload: dict = Depends(auth.verify_token)) -> dict:
    """Disk-Quota-Status des Workspaces: {usage, quota_mb, over, mem_usage}.

    Usage kommt aus dem Reaper-Cache (Reaper-Zyklus, default 10 s); bei
    fehlendem oder >90 s altem Cache wird frisch gemessen. Ohne Quota:
    usage=None. mem_usage = aktueller RAM-Verbrauch (Bytes, UI-Info).
    purged = letzter automatischer Aufräum-Schritt (Count/Bytes).
    """
    _op(payload, f"ws:{key}")
    _touch(key)
    out = {"usage": None, "quota_mb": None, "over": False, "hard": False,
           "mem_usage": None}
    info = REGISTRY.get(key)
    if not info:
        return out
    spec = info.get("spec") or {}
    # RAM-Usage (Reaper-Cache, Reaper-Zyklus) — reine UI-Info. Fresh-
    # Fallback, solange der Cache leer ist (direkt nach Agent-/Container-
    # Start): docker stats dauert 1–3 s, akzeptabel im Status-Poll.
    mem = info.get("mem")
    if mem and (time.time() - (mem.get("at") or 0)) < 90:
        out["mem_usage"] = mem.get("usage")
    elif docker_ops.container_state(key) == "running":
        usage = docker_ops.workspace_mem_usage(key)
        if usage is not None:
            REGISTRY.set_mem(key, {"usage": usage, "at": time.time()})
            out["mem_usage"] = usage
    quota_mb = spec.get("disk_quota_mb")
    if not quota_mb:
        return out
    out["quota_mb"] = quota_mb
    disk = info.get("disk")
    if disk and (time.time() - (disk.get("at") or 0)) < 90:
        out["usage"] = disk.get("usage")
        out["over"] = bool(disk.get("over"))
        out["hard"] = bool(disk.get("hard"))
        if disk.get("purged"):
            out["purged"] = disk["purged"]
        return out
    if docker_ops.container_state(key) != "running":
        return out
    usage = docker_ops.workspace_disk_usage(key, spec.get("readonly_paths") or [])
    over = usage > quota_mb * 1024 * 1024
    hard = usage > int(quota_mb * config.QUOTA_KILL_FACTOR) * 1024 * 1024
    new_disk = {"usage": usage, "quota_mb": quota_mb, "over": over,
                "hard": hard, "at": time.time()}
    if disk and disk.get("killed_at"):
        new_disk["killed_at"] = disk["killed_at"]
    REGISTRY.set_disk(key, new_disk)
    out["usage"] = usage
    out["over"] = over
    out["hard"] = hard
    return out


# ── Assets (geteilte public-Dateien/Datasets je Aufgabe) ──────────

@app.post("/assets/{course}/{task}")
@_translate
def assets_sync(course: int, task: int, body: dict,
                payload: dict = Depends(auth.verify_token)) -> dict:
    """Assets bringen: Uploads (content_b64), optional Verwaisten aufräumen.

    files: [{path, content_b64}] — Daten-DOWNLOADS laufen in .init.sh,
    nicht hier. folders: [pfad, …] — explizite Ordner (auch leer) werden
    angelegt und beim Purge behalten. delete_missing=true: Remote-Dateien
    löschen, die lokal nicht mehr existieren (init-Manifest-Dateien und
    explizite Ordner bleiben verschont).
    """
    _op(payload, f"task:{course}:{task}")
    files = body.get("files") or []
    folders = [str(d).strip() for d in (body.get("folders") or []) if str(d).strip()]
    # Task-Lock während der Schreibphase: ohne Sperre könnte ein laufender
    # Init-Build die sync-ten Dateien in sein After-Snapshot einfangen
    # (→ fälschlich als Init-Artefakt im Manifest, s. asset_sync_lock).
    # Der Sync wartet dann auf den Build (Build-Budget ≤ INIT_TIMEOUT).
    with docker_ops.asset_sync_lock(course, task):
        for d in folders:
            try:
                docker_ops.ensure_asset_dir(course, task, d)
            except docker_ops.DockerError as e:
                raise HTTPException(status_code=getattr(e, "status", 422),
                                    detail=f"{d}: {e}") from e
        results = []
        for f in files:
            path = str(f.get("path", ""))
            try:
                data = base64.b64decode(f.get("content_b64") or "")
                size = docker_ops.write_asset_file(course, task, path, data)
                results.append({"path": path, "status": "ready", "size": size})
            except docker_ops.DockerError as e:
                raise HTTPException(status_code=getattr(e, "status", 422),
                                    detail=f"{path}: {e}") from e
        if body.get("delete_missing"):
            # Sync-Sidecar: exakt die aktuell gesyncten Task-Dateien
            # (Vollspiegel) — der Init-Build-Cleansweep nutzt das, um
            # Task-Dateien von Init-Artefakten zu unterscheiden.
            docker_ops.write_task_files(
                docker_ops.asset_dir(course, task),
                [r["path"] for r in results])
        removed = []
        if body.get("delete_missing"):
            if docker_ops.init_build_running(course, task):
                # Purge würde Init-Artefakte im shared Asset-Dir löschen,
                # BEVOR der Build das Manifest (den Purge-Schutz) geschrieben
                # hat — inkl. laufender Downloads (leere Ziel-Ordner werden
                # mit-rasirt → z. B. FileNotFoundError in Download-Skripten).
                # Der nächste Sync nach dem Build räumt dann auf.
                logger.info("Asset-Purge übersprungen (Init-Build läuft): %s/%s",
                            course, task)
            else:
                removed = docker_ops.purge_missing_assets(
                    course, task, [r["path"] for r in results],
                    keep_dirs=folders)
    return {"status": "ready", "files": results, "removed": removed}


@app.get("/assets/{course}/{task}")
@_translate
def assets_list(course: int, task: int,
                payload: dict = Depends(auth.verify_token)) -> dict:
    _op(payload, f"task:{course}:{task}")
    return {"files": docker_ops.list_assets(course, task)}


@app.delete("/tasks/{course}/{task}/init-artifact")
@_translate
def init_artifact_remove(course: int, task: int, body: dict,
                         payload: dict = Depends(auth.verify_token)) -> dict:
    """Manifest-Eintrag (shared/seed/private) + Datei entfernen — für
    Task-Dateien, die der Asset-Sync fälschlich als Init-Artefakt
    registriert hat (Race: Upload während laufendem Build). Der nächste
    Init-Build schreibt das Manifest neu."""
    _op(payload, f"task:{course}:{task}")
    path = str((body or {}).get("path") or "")
    return docker_ops.remove_init_artifact(course, task, path)


@app.delete("/assets/{course}/{task}")
@_translate
def assets_delete(course: int, task: int,
                  payload: dict = Depends(auth.verify_token)) -> dict:
    _op(payload, f"task:{course}:{task}")
    docker_ops.remove_asset_tree(course, task)
    return {"ok": True}


# ── Image-Registry (Image-Specs installieren, s. Plan) ─────────

@app.get("/images")
@_translate
def images_list(payload: dict = Depends(auth.verify_token)) -> dict:
    """Lokale Images + laufende/fehlgeschlagene Builds (Log-Tail)."""
    _op(payload, "images")
    return {"images": docker_ops.list_images()}


@app.post("/images")
@_translate
def images_install(body: dict,
                   payload: dict = Depends(auth.verify_token)) -> dict:
    """Image-Spec installieren (idempotent: vorhanden → ready).

    Body: {name, dockerfile} — No-op-Specs (nur FROM) versuchen,
    das Basis-Image zu pullen, sonst startet der Build im Hintergrund
    (Status via GET /images).
    """
    _op(payload, "images")
    from . import image_spec
    name = str(body.get("name") or "").strip()
    dockerfile = str(body.get("dockerfile") or "").strip()
    if not name or not dockerfile:
        raise docker_ops.DockerError("Body braucht {name, dockerfile}", 400)
    if not image_spec.name_ok(name):
        raise docker_ops.DockerError(
            f"Spec-Name {name!r} ungültig (erlaubt: a-z, 0-9, -, _)", 400)
    image_spec.validate(dockerfile)
    return docker_ops.install_spec_image({"name": name, "dockerfile": dockerfile})


@app.post("/images/spec-status")
@_translate
def images_spec_status(body: dict,
                       payload: dict = Depends(auth.verify_token)) -> dict:
    """Installationsstatus der übergebenen Image-Specs auf diesem Node.

    Body: {specs: [{name, dockerfile}, …]} → {"status": […]}, gleiche
    Reihenfolge. UI-Filter: bereits installierte Specs aus der
    Installier-Liste ausblenden. Ungültige Specs → installed=false
    (kein Fehler, es geht um die Anzeige).
    """
    _op(payload, "images")
    from . import image_spec
    specs = body.get("specs") or []
    if not isinstance(specs, list) or len(specs) > 50:
        raise docker_ops.DockerError("specs: Liste mit max. 50 Einträgen", 400)
    status = []
    for raw in specs:
        try:
            if isinstance(raw, str):  # Legacy: rohes Dockerfile ohne Name
                raw = {"name": "", "dockerfile": raw}
            name = str(raw.get("name") or "").strip()
            dockerfile = str(raw.get("dockerfile") or "").strip()
            if not (name and dockerfile):
                raise image_spec.ImageSpecError("name + dockerfile nötig")
            image_spec.validate(dockerfile)
            status.append(docker_ops.spec_image_status(
                {"name": name, "dockerfile": dockerfile}))
        except image_spec.ImageSpecError:
            status.append({"refs": [], "installed": False, "building": False})
    return {"status": status}


@app.delete("/images/{ref:path}")
@_translate
async def images_remove(ref: str,
                        request: Request,
                        payload: dict = Depends(auth.verify_token)) -> dict:
    _op(payload, "images")
    force = False
    try:
        body = await request.json()
        if isinstance(body, dict):
            force = bool(body.get("force"))
    except Exception:
        pass  # kein Body (ältere Clients) → normales Löschen
    removed = docker_ops.remove_image(ref, force=force)
    return {"ok": True, "removed_containers": removed}


# ── Task-Images (.init.sh-Build, 1× je (Task, init-Hash)) ───────

@app.post("/tasks/{course}/{task}/init-build")
@_translate
def init_build(course: int, task: int, body: dict,
               payload: dict = Depends(auth.verify_token)) -> dict:
    """Task-Image-Build starten (idempotent: vorhanden → ready).

    Body: {image, init_hash, deadline?, readonly_paths?, hidden_paths?,
    init_b64?, init_private_b64?, folders?, force?} — image ist die
    aufgelöste Spec-Image-Referenz, init_hash = Backend-Hash (Format:
    12 Hex-Zeichen), readonly/hidden = Top-Level-🔒/👤-Pfade (rw-Mounts
    im Build), init_b64/init_private_b64 = 👤-Skripte .init.sh/.init_
    hidden.sh (werden in .private/ persistiert), folders = Ordner-Pfade
    der Aufgabe (werden in den Build-Quellen angelegt), force = Build
    erzwingen (überspringt die Idempotenz-Prüfung, Skript läuft komplett
    neu, alte Init-Artefakte werden gelöscht + neu erzeugt).
    """
    _op(payload, f"task:{course}:{task}")
    image = str(body.get("image") or "")
    init_hash = str(body.get("init_hash") or "")
    if not image or not re.fullmatch(r"[a-f0-9]{12}", init_hash):
        raise HTTPException(status_code=400, detail="image + init_hash nötig")
    readonly_paths = []
    for p in body.get("readonly_paths") or []:
        readonly_paths.append(docker_ops.safe_asset_path(str(p)))
    hidden_paths = []
    for p in body.get("hidden_paths") or []:
        hidden_paths.append(docker_ops.safe_asset_path(str(p)))
    folders = []
    for d in body.get("folders") or []:
        folders.append(docker_ops.safe_asset_path(str(d)))
    init_b64 = body.get("init_b64")
    if init_b64 is not None:
        init_b64 = str(init_b64)
    init_private_b64 = body.get("init_private_b64")
    if init_private_b64 is not None:
        init_private_b64 = str(init_private_b64)
    return docker_ops.start_init_build(
        course, task, init_hash, image, body.get("deadline"),
        readonly_paths, hidden_paths, init_b64, init_private_b64, folders,
        force=bool(body.get("force")))


@app.get("/tasks/{course}/{task}/init-status")
@_translate
def init_status(course: int, task: int, init_hash: str = "",
                payload: dict = Depends(auth.verify_token)) -> dict:
    """Init-Status: none | ready | building (Log-Tail) | failed | idle."""
    _op(payload, f"task:{course}:{task}")
    if not re.fullmatch(r"[a-f0-9]{12}", init_hash):
        raise HTTPException(status_code=400, detail="init_hash nötig")
    return docker_ops.init_build_status(course, task, init_hash)


@app.get("/tasks/{course}/{task}/init-artifacts")
@_translate
def init_artifacts(course: int, task: int, scope: str = "all",
                   payload: dict = Depends(auth.verify_token)) -> dict:
    """Init-Artefakte (Manifest) je Scope: shared|seed|private|all."""
    _op(payload, f"task:{course}:{task}")
    return docker_ops.init_artifacts(course, task, scope)


@app.get("/tasks/{course}/{task}/init-artifact-file")
@_translate
def init_artifact_file(course: int, task: int, scope: str = "", path: str = "",
                       payload: dict = Depends(auth.verify_token)) -> Response:
    """Inhalt eines Init-Artefakts (raw, binary-safe; 5-MB-Cap)."""
    _op(payload, f"task:{course}:{task}")
    if not scope or not path:
        raise HTTPException(status_code=400, detail="scope + path nötig")
    size, data = docker_ops.init_artifact_file(course, task, scope, path)
    return Response(
        content=data, media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "Content-Length": str(size)})


@app.post("/tasks/{course}/{task}/init-build/stop")
@_translate
def init_build_stop(course: int, task: int, body: dict,
                    payload: dict = Depends(auth.verify_token)) -> dict:
    """Aktiven Init-Build abbrechen (Build-Container weg, Init-Daten cleanup)."""
    _op(payload, f"task:{course}:{task}")
    init_hash = str(body.get("init_hash") or "")
    if not re.fullmatch(r"[a-f0-9]{12}", init_hash):
        raise HTTPException(status_code=400, detail="init_hash nötig")
    return docker_ops.stop_init_build(course, task, init_hash)


@app.delete("/task-images/{course}/{task}")
@_translate
def task_images_delete(course: int, task: int,
                       payload: dict = Depends(auth.verify_token)) -> dict:
    """Alle Task-Images der Aufgabe löschen (Task-Delete / keine Init-Skripte)."""
    _op(payload, f"task:{course}:{task}")
    removed = docker_ops.remove_task_images(course, task)
    return {"ok": True, "removed": removed}
