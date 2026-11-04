"""
Docker-Operationen des Compute-Agenten (via docker-CLI, subprocess).

Alle Funktionen sind synchron; FastAPI führt sie im Threadpool aus
(sync-def-Endpoints). Container-Modell:
- Container `aicampus-{key}`, key = ws-{course}-{task}-{student}
- Volume `aicampus-{key}` → /workspace (einziger schreibbarer Ort)
- Read-only Root-FS, /tmp als tmpfs; je 🔒-Pfad (spec.readonly_paths)
  ein ro-Bind-Mount aus dem geteilten Asset-Verzeichnis nach /workspace
- Container ist ephemeral (Reaper), der State lebt im Volume
"""

import base64
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import config

_KEY_RE = re.compile(r"^ws-(\d+)-(\d+)-(\d+)$")  # ws-{course}-{task}-{student}


class DockerError(Exception):
    """Fehler mit HTTP-Status (409 = Zustand, 500 = Daemon-Fehler, …)."""

    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.status = status


def key_parts(key: str) -> tuple[int, int, int]:
    """key → (course_id, task_id, student_id)."""
    m = _KEY_RE.match(key)
    if not m:
        raise DockerError(f"Ungültiger Workspace-Key: {key!r}", 400)
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def container_name(key: str) -> str:
    return f"aicampus-{key}"


def volume_name(key: str) -> str:
    return f"aicampus-{key}"


def ws_network_name(key: str) -> str:
    return f"aicampus-net-{key}"


def ensure_ws_network(key: str) -> None:
    """Isoliertes Bridge-Netzwerk für den Workspace-Container (idempotent).

    Nur für internet=true-Workspaces: Outbound ins Internet funktioniert
    weiter (NAT), aber der Container ist für andere Container unsichtbar
    — und umgekehrt (kein gegenseitiger Port-Zugriff zwischen Studenten).
    """
    proc = _docker("network", "create", ws_network_name(key),
                   check=False, timeout=30)
    if proc.returncode != 0 and "already exists" not in proc.stderr.decode():
        raise DockerError("Workspace-Netzwerk konnte nicht angelegt werden", 500)


def remove_ws_network(key: str) -> None:
    """Best effort: klappt nur, wenn kein Container mehr daran hängt."""
    _docker("network", "rm", ws_network_name(key), check=False, timeout=30)


def asset_dir(course: int, task: int) -> Path:
    return Path(config.ASSET_ROOT) / str(course) / str(task)


def _daemon_path(p: str | Path) -> str:
    """Bind-Mount-Quelle für den Docker-Daemon (läuft auf dem HOST).

    Läuft der Agent selbst im Container, ist ASSET_ROOT ein Container-
    Pfad, den der Daemon nicht sieht (er würde ein leeres Verzeichnis
    anlegen → leere Mounts). ASSET_ROOT_HOST übersetzt auf den Host-Pfad.
    """
    s = str(p)
    if config.ASSET_ROOT_HOST:
        root = config.ASSET_ROOT
        if s == root or s.startswith(root + os.sep):
            return config.ASSET_ROOT_HOST.rstrip(os.sep) + s[len(root):]
    return s


# ── Basis-CLI ─────────────────────────────────────────────────────

def _docker(*args, input_bytes: bytes | None = None, timeout: int = 60,
            check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            ["docker", *args], input=input_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise DockerError(f"docker {' '.join(args[:2])} timeout", 504)
    if check and proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        raise DockerError(f"docker {args[0]} fehlgeschlagen: {err[:500]}", 500)
    return proc


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        _docker("info", check=False, timeout=10)
        return True
    except Exception:
        return False


_GPU_CACHE: bool | None = None


def gpu_available() -> bool:
    """GPU + nvidia-container-toolkit auf diesem Host? Ergebnis wird gecacht.

    1) Host-nvidia-smi (falls der Agent systemd-nativ läuft),
    2) Fallback: kleiner Test-Container (falls der Agent selbst
       containerisiert läuft und kein /usr/bin/nvidia-smi sieht).
    """
    global _GPU_CACHE
    if config.GPU_ENABLED == "false":
        return False
    if _GPU_CACHE is not None:
        return _GPU_CACHE
    try:
        proc = subprocess.run(["nvidia-smi", "-L"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=15)
        _GPU_CACHE = proc.returncode == 0
        return _GPU_CACHE
    except FileNotFoundError:
        pass
    except Exception:
        _GPU_CACHE = False
        return _GPU_CACHE
    # Kein nvidia-smi (Agent läuft selbst containerisiert) → prüfe die
    # nvidia-Docker-Runtime des Hosts. Bewusst KEIN Test-Container:
    # der würde ein CUDA-Image (mehrere GB) pullen und den Health-Check
    # Minuten lang blockieren.
    try:
        proc = _docker("info", "--format", "{{ json .Runtimes }}",
                       check=False, timeout=15)
        runtimes = json.loads(proc.stdout.decode() or "{}")
        _GPU_CACHE = "nvidia" in runtimes
    except Exception:
        _GPU_CACHE = False
    return _GPU_CACHE


# ── Images ────────────────────────────────────────────────────────

def image_exists(image: str) -> bool:
    proc = _docker("image", "inspect", image, check=False)
    return proc.returncode == 0


def pull_image(image: str) -> None:
    _docker("pull", image, timeout=1800)


def resolve_image(image: str) -> str:
    """Image-Referenz prüfen (und bei Autopull nachziehen, wenn öffentlich).

    AICampus-gelieferte Refs (aicampus/spec/*, kuratierte aicampus/*-Images)
    werden NICHT gepullt — die installiert AICampus per Image-Spec auf der
    Engine. Fehlt ein solches Image → klare 409-Meldung.
    """
    if not image_exists(image) and config.AUTOPULL \
            and not image.startswith("aicampus/"):
        try:
            pull_image(image)
        except DockerError as e:
            raise DockerError(f"Image-Pull fehlgeschlagen: {e}", 409) from e
    if not image_exists(image):
        if image.startswith("aicampus/spec/"):
            raise DockerError(
                f"Image {image} fehlt auf diesem Node — bitte die "
                "Image-Spec in der AICampus-UI auf der Engine installieren", 409)
        if image.startswith("aicampus/task/"):
            raise DockerError(
                f"Task-Image {image} fehlt auf diesem Node — die "
                "Initialisierung (.init.sh-Build) läuft noch oder ist "
                "fehlgeschlagen (Status in der Aufgabe ansehen)", 409)
        raise DockerError(f"Image {image} fehlt auf diesem Node", 409)
    return image


# ── GPU-Details (für Health/Status) ───────────────────────────

_GPU_INFO_CACHE: str | None = None


def gpu_info() -> str:
    """Besteffort-GPU-Beschriftung, z. B. „2× NVIDIA A40" (leer = unbekannt).

    Funktioniert nur, wenn der Agent nvidia-smi direkt sieht (systemd-nativ
    oder mit --gpus all containerisiert); sonst bleibt es leer — der
    Boolean `gpu` im Health bleibt die Vertrauensquelle.
    """
    global _GPU_INFO_CACHE
    if _GPU_INFO_CACHE is not None:
        return _GPU_INFO_CACHE
    try:
        proc = subprocess.run(["nvidia-smi", "-L"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=15)
        if proc.returncode == 0:
            counts: dict[str, int] = {}
            for line in proc.stdout.decode().splitlines():
                line = line.strip()
                if not line:
                    continue
                m = re.match(r"GPU \d+:\s*(.+?)\s*\(UUID:", line)
                name = m.group(1) if m else line
                counts[name] = counts.get(name, 0) + 1
            _GPU_INFO_CACHE = ", ".join(f"{n}× {k}" for k, n in counts.items())
            return _GPU_INFO_CACHE
    except Exception:
        pass
    _GPU_INFO_CACHE = ""
    return ""


_GPU_LIST_CACHE: list[int] | None = None


def gpu_list() -> list[int]:
    """Indizes der verfügbaren GPUs (nvidia-smi -L) → [0, 1, …]; gecacht.

    Containerisierter Agent ohne GPU-Sichtbarkeit (kein nvidia-smi) → [].
    Die Engine-UI zeigt daraus die per-Engine erlaubten GPUs (Checkboxen);
    die Regel gilt für ALLE Container auf der Engine.
    """
    global _GPU_LIST_CACHE
    if config.GPU_ENABLED == "false":
        return []
    if _GPU_LIST_CACHE is not None:
        return _GPU_LIST_CACHE
    try:
        proc = subprocess.run(["nvidia-smi", "-L"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=15)
        if proc.returncode == 0:
            n = sum(1 for l in proc.stdout.decode().splitlines()
                    if l.strip().startswith("GPU"))
            _GPU_LIST_CACHE = list(range(n))
            return _GPU_LIST_CACHE
    except Exception:
        pass
    _GPU_LIST_CACHE = []
    return []


# ── Image-Registry (Image-Specs installieren, s. Plan) ────────────

_BUILD_TIMEOUT = 3600  # 1 h harte Obergrenze je Build (Base-Pull inklusive)
_BUILDS: dict[str, dict] = {}   # ref → {status, start, error, log_tail}
_BUILDS_LOCK = threading.Lock()


def list_images() -> list[dict]:
    """Lokale Images + laufende Builds (Spec-Metadaten aus Labels)."""
    proc = _docker(
        "images", "--format",
        "{{.ID}}\t{{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}",
        check=False, timeout=30)
    out = []
    for line in proc.stdout.decode().splitlines():
        parts = line.split("\t")
        if len(parts) < 4 or not parts[1]:
            continue
        img_id, ref = parts[0], parts[1]
        if ":" in ref:
            repo, _tag = ref.rsplit(":", 1)
        else:
            repo, _tag = ref, ""
        if repo == "<none>" or repo in config.HIDDEN_IMAGE_REPOS:
            continue
        out.append({"id": img_id[:12], "ref": ref, "size": parts[2],
                    "created": parts[3], "spec_name": None, "spec_hash": None})
    # Labels: `docker images` kennt {{.Label}} nicht → inspect in einem Zug
    # (alle Image-IDs als Argumente). Neben den Spec-Labels werden die
    # Task-Image-Labels (aicampus.task / aicampus.task.hash) mit ausgelesen.
    ids_proc = _docker("images", "--format", "{{.ID}}", check=False, timeout=30)
    ids = ids_proc.stdout.decode().split()
    labels: dict[str, tuple[str | None, str | None, str | None, str | None]] = {}
    if ids:
        proc = _docker(
            "image", "inspect", "--format",
            '{{.Id}}\t{{index .Config.Labels "aicampus.spec.name"}}\t'
            '{{index .Config.Labels "aicampus.spec.hash"}}\t'
            '{{index .Config.Labels "aicampus.task"}}\t'
            '{{index .Config.Labels "aicampus.task.hash"}}',
            *ids, check=False, timeout=30)
        for line in proc.stdout.decode().splitlines():
            parts = line.split("\t")
            if not parts or not parts[0]:
                continue
            full_id = parts[0].replace("sha256:", "")[:12]
            # Fehlendes Label gibt das Go-Template als "<no value>" aus
            labels[full_id] = tuple(
                parts[i] if i < len(parts) and parts[i] != "<no value>" else None
                for i in range(1, 5))
    for img in out:
        name, img_hash, task_name, task_hash = \
            labels.get(img["id"], (None, None, None, None))
        img["spec_name"], img["spec_hash"] = name, img_hash
        img["task_name"], img["task_hash"] = task_name, task_hash
    with _BUILDS_LOCK:
        for ref, b in _BUILDS.items():
            if b["status"] == "building":
                out.append({"ref": ref, "size": None, "created": None,
                            "spec_name": None, "spec_hash": None,
                            "building": True,
                            "build_log": b.get("log_tail", "")[-2000:]})
            elif b["status"] == "failed":
                out.append({"ref": ref, "size": None, "created": None,
                            "spec_name": None, "spec_hash": None,
                            "build_failed": True,
                            "build_log": (b.get("error") or "")[-2000:]})
    return out


def _do_spec_build(ref: str, dockerfile: str,
                   spec_name: str = "", spec_hash: str = "") -> None:
    with tempfile.TemporaryDirectory(prefix="aicampus-imgbuild-") as tmp:
        ctx = Path(tmp)
        (ctx / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        args = ["docker", "build", "-t", ref, "-f", str(ctx / "Dockerfile")]
        if spec_name:
            args += ["--label", f"aicampus.spec.name={spec_name}"]
        if spec_hash:
            args += ["--label", f"aicampus.spec.hash={spec_hash}"]
        args.append(str(ctx))
        try:
            proc = subprocess.run(
                args,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=_BUILD_TIMEOUT)
            out = proc.stdout.decode(errors="replace")
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired:
            out, ok = "Build-Timeout (1 h)", False
        except Exception as e:  # noqa: BLE001
            out, ok = str(e), False
    if ok:
        # Build-Reste entfernen (Build-Cache + Legacy-Zwischen-Images),
        # sonst hinterlässt jeder Spec-Build GBs auf der Engine.
        prune_after_spec_build()
    with _BUILDS_LOCK:
        b = _BUILDS.get(ref)
        if b is not None:
            b["status"] = "done" if ok else "failed"
            b["log_tail"] = out[-4000:]
            b["error"] = None if ok else out[-1000:]


def install_spec_image(spec_dict: dict) -> dict:
    """Image-Spec auf diesem Node installieren (idempotent).

    spec_dict: {name, dockerfile}.
    No-op-Spec (nur FROM): Basis-Image muss existieren — bei expliziter
    Admin-Handlung IMMER Pull-Versuch (nicht an AGENT_AUTOPULL gebunden).
    Sonst: EIN Background-Build mit deterministischem Tag.
    """
    from . import image_spec
    name = spec_dict["name"]
    dockerfile = spec_dict["dockerfile"]
    details = []
    if not image_spec.has_build_content(dockerfile):
        ref = image_spec.base_image(dockerfile)
        if not image_exists(ref):
            try:
                pull_image(ref)
            except DockerError as e:
                raise DockerError(
                    f"Basis-Image {ref} fehlt und der Pull ist fehlgeschlagen: "
                    f"{e} (ist es ein öffentliches Image?)", 409) from e
            if not image_exists(ref):
                raise DockerError(
                    f"Basis-Image {ref} fehlt auf diesem Node", 409)
        details.append({"ref": ref, "status": "ready"})
    else:
        ref = image_spec.image_tags(name, dockerfile)[0]
        h = image_spec.content_hash(dockerfile)
        if image_exists(ref):
            details.append({"ref": ref, "status": "ready"})
            return {"status": "ready", "tags": [ref], "details": details}
        with _BUILDS_LOCK:
            b = _BUILDS.get(ref)
            if b and b["status"] == "building":
                details.append({"ref": ref, "status": "building"})
            else:
                _BUILDS[ref] = {"status": "building", "start": time.time(),
                                "error": None, "log_tail": ""}
                threading.Thread(target=_do_spec_build,
                                 args=(ref, dockerfile, name, h),
                                 daemon=True).start()
                details.append({"ref": ref, "status": "building"})
    status = "ready" if all(d["status"] == "ready" for d in details) else "building"
    return {"status": status,
            "tags": [d["ref"] for d in details],
            "details": details}


def spec_image_status(spec_dict: dict) -> dict:
    """Installationsstatus einer Image-Spec auf diesem Node (für UI-Filter).

    Gleiche Ref-Auflösung wie install_spec_image: No-op-Spec → Basis-Image,
    Build-Spec → deterministisches Spec-Tag. installed = Ref vorhanden
    (sonst wäre ein erneutes Installieren nötig — ein erneuter Install-Call
    wäre sonst ein No-op).
    """
    from . import image_spec
    if not image_spec.has_build_content(spec_dict["dockerfile"]):
        refs = [image_spec.base_image(spec_dict["dockerfile"])]
    else:
        refs = image_spec.image_tags(spec_dict["name"], spec_dict["dockerfile"])
    with _BUILDS_LOCK:
        building = any(_BUILDS.get(r, {}).get("status") == "building" for r in refs)
    return {
        "refs": refs,
        "installed": all(image_exists(r) for r in refs),
        "building": building,
    }


def remove_image(ref: str, force: bool = False) -> int:
    """Image entfernen; liefert Anzahl gewaltsam beendeter Container.

    Sperrung: Core-Repos, laufender Build oder Container, die das Image
    nutzen. Mit force: laufende Container werden gestoppt (graziös,
    10 s) und alle abhängigen Container entfernt — Workspace-Dateien
    bleiben in ihrem named Volume erhalten.
    """
    last = ref.rsplit("/", 1)[-1]
    repo = last.rsplit(":", 1)[0] if ":" in last else last
    if repo in config.HIDDEN_IMAGE_REPOS:
        raise DockerError(
            "Core-Image des AICampus-Sets kann nicht entfernt werden", 409)
    with _BUILDS_LOCK:
        b = _BUILDS.get(ref)
        if b and b["status"] == "building":
            raise DockerError("Build läuft noch — bitte später erneut versuchen", 409)
    proc = _docker("ps", "-a", "--filter", f"ancestor={ref}",
                   "--format", "{{.ID}}\t{{.State}}", check=False, timeout=30)
    rows = []
    for line in proc.stdout.decode().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0]:
            rows.append((parts[0], parts[1]))
    removed = 0
    if rows:
        if not force:
            raise DockerError(
                f"Image wird von {len(rows)} Container(s) genutzt — "
                "gewaltsames Löschen beendet sie", 409)
        for cid, state in rows:
            if state in ("running", "paused"):
                _docker("stop", "-t", "10", cid, check=False, timeout=30)
            _docker("rm", "-f", cid, check=False, timeout=30)
            removed += 1
    proc = _docker("rmi", ref, check=False, timeout=60)
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        if "No such image" in err or "not found" in err.lower():
            with _BUILDS_LOCK:
                _BUILDS.pop(ref, None)
            return removed
        raise DockerError(f"Image-Löschen fehlgeschlagen: {err[:300]}", 500)
    with _BUILDS_LOCK:
        _BUILDS.pop(ref, None)
        _BUILDS.pop(ref + "-gpu", None)
    return removed


# ── Container-Lifecycle ───────────────────────────────────────────

def container_state(key: str) -> str | None:
    """running / created / exited / … oder None (kein Container)."""
    proc = _docker("inspect", "-f", "{{.State.Status}}",
                   container_name(key), check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode().strip()


def volume_exists(key: str) -> bool:
    proc = _docker("volume", "inspect", volume_name(key), check=False)
    return proc.returncode == 0


def _spec_gpus(spec: dict) -> str | list[int]:
    """GPU-Modus der Workspace-Spec: "all" | "none" | [int, …].

    Kommt aus der Engine-Regel "gpus" (von AICampus injiziert); ohne
    Injektion: "none" (GPU-Fähigkeit folgt aus der Compute-Engine).
    """
    gpus = spec.get("gpus")
    if gpus is None:
        return "none"
    return gpus


_NVIDIA_RUNTIME: bool | None = None


def nvidia_runtime_available() -> bool:
    """Hat der Daemon ein „nvidia“-Runtime registriert? (gecacht)

    Docker wechselt für --gpus-Requests nicht automatisch ins
    nvidia-runtime (mindestens bis v28): Der Container läuft dann auf
    runc — die /dev/nvidia*-Knoten sind mountet, aber die
    Treiber-Bibliotheken (NVML/CUDA) fehlen und die GPU ist tot. Bei
    registriertem nvidia-runtime wird es daher explizit mit --runtime
    übergeben; ohne Registrierung bleibt das Flag weg (Legacy-Setups
    injizieren über den Default-Runtime).
    """
    global _NVIDIA_RUNTIME
    if _NVIDIA_RUNTIME is None:
        try:
            proc = _docker("info", "--format", "{{json .Runtimes}}",
                           check=False, timeout=30)
            runtimes = json.loads(proc.stdout.decode() or "{}")
            _NVIDIA_RUNTIME = "nvidia" in runtimes
        except Exception:
            _NVIDIA_RUNTIME = False
    return _NVIDIA_RUNTIME


def _gpu_args(spec: dict) -> list[str]:
    """--gpus-Flags aus dem GPU-Modus (s. _spec_gpus).

    "all": GPU-Fähigkeit vorhanden → --gpus all, sonst still CPU.
    [int, …]: explizite Devices — OHNE GPU-Fähigkeit = klarer Fehler
    (Konfig-Fehler der Engine-Regel, kein stiller Fallback).
    "none"/leer: kein Flag.
    Bei registriertem nvidia-runtime zusätzlich --runtime nvidia
    (s. nvidia_runtime_available).
    """
    gpus = _spec_gpus(spec)
    if not gpus or gpus == "none":
        return []
    runtime = ["--runtime", "nvidia"] if nvidia_runtime_available() else []
    if gpus == "all":
        return (runtime + ["--gpus", "all"]) if gpu_available() else []
    if isinstance(gpus, list) and gpus:
        if not gpu_available():
            raise DockerError(
                "GPU-Devices angefordert, aber dieser Node hat keine GPU-"
                f"Fähigkeit (gefordert: {', '.join(map(str, gpus))}) — "
                "Engine-GPU-Konfiguration prüfen", 409)
        return runtime + ["--gpus", f'"device={",".join(str(g) for g in gpus)}"']
    return []


# /tmp tmpfs: der Preview-Relay (/tmp/relay) muss dort ausführbar sein.
# WICHTIG: `exec` explizit setzen — der Docker-Daemon ergänzt bei tmpfs
# standardmäßig `noexec` (und `nodev`), und ohne explizites `exec`
# gewinnt das Daemon-Default. Sicherheitsniveau bleibt gleich: der
# Student hat auf /workspace ohnehin Exec-Rechte.
_TMPFS_SPEC = "/tmp:rw,nosuid,exec,size=256m,mode=1777"


def _build_create_args(key: str, spec: dict, image: str) -> list[str]:
    course, _task, _student = key_parts(key)
    args = [
        "create",
        "--name", container_name(key),
        "--label", f"aicampus.ws={key}",
        "--label", f"aicampus.course={course}",
        "--label", f"aicampus.mounts={_mounts_hash(spec, course, _task)}",
        "--read-only",
        "--tmpfs", _TMPFS_SPEC,
        "-v", f"{volume_name(key)}:/workspace",
        # 1024 (war 256→512): ML-Kernels sind thread-hungrig — ein
        # torch-Training ohne OMP_NUM_THREADS size OpenBLAS/OMP/CPU-Pools
        # auf die Host-Core-Zahl (einziger Kernel: 200–300 Threads), dazu
        # TB (~53) + Jupyter + Preview-Relays. 512 war bei Kernel + 2
        # Preview-Apps zu knapp → EAGAIN („can't start new thread", Relays
        # starben, Dateibaum leerte sich). 1024 gibt Luft; Fork-Bomb-Schutz
        # bleibt: CPU-Limit + Memory-Limit begrenzen den Schaden, die Bomb
        # pinnt den Container auf 1024 (≈1024 threads ≈ CPU-Spin, kein
        # Host-Kern-Druck).
        "--pids-limit", "1024",
        # Isoliertes Netz pro Container (Outbound via NAT, KEINE
        # Container-Sichtbarkeit) — nicht die geteilte Default-Bridge.
        "--network", (ws_network_name(key) if spec["internet"] else "none"),
        "-w", spec["working_dir"],
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "HOME=/tmp",
    ]
    limits = spec.get("limits") or {}
    if limits.get("cpu"):
        args += ["--cpus", str(limits["cpu"])]
    if limits.get("memory"):
        mem = str(limits["memory"])
        args += ["--memory", mem, "--memory-swap", mem]
    args += _gpu_args(spec)
    # Geteilte read-only Bereiche (Zugriffs-Klasse 🔒): je Top-Level-Pfad
    # ein ro-Bind-Mount aus dem Asset-Verzeichnis (Datei ODER Ordner).
    # Bind-Mounts sind live — Tutor-Änderungen sind in laufenden
    # Containern sofort sichtbar. Fehlende Quellen (noch kein Sync)
    # werden übersprungen; der Mount-Hash (Label) zwingt beim Erscheinen
    # die Container neu anzulegen (ensure_container).
    a_dir = asset_dir(course, _task)
    for rp in spec.get("readonly_paths") or []:
        src = a_dir / rp
        if src.exists():
            args += ["-v", f"{_daemon_path(src.resolve())}:/workspace/{rp}:ro"]
    # Deterministischer Entry-Point (Images mit eigenem ENTRYPOINT wären
    # sonst nicht für sleep-infinity tauglich)
    args += ["--entrypoint", "/bin/sh", image, "-c", "sleep infinity"]
    return args


def _mounts_hash(spec: dict, course: int, task: int) -> str:
    """Fingerprint des ro-Mount-Layouts (Pfad + Quellen-Status).

    Ländet als Label `aicampus.mounts`; ändert sich das Layout (neuer 🔒-
    Pfad, Quelle erscheint/verschwindet, 🔒-DATEI wird editiert), wird
    der Container neu angelegt (Volume bleibt). Datei-Quellen brauchen
    Size+Mtime im Hash: ein ro-File-Bind-Mount klebt auf der alten Inode,
    wenn die Quelle ersetzt wird — ohne Fingerprint sähe ein laufender
    Container ewig die alte Datei-Version. Ordner-Quellen sind live
    (Inode stabil), da genügt Vorhandensein."""
    a_dir = asset_dir(course, task)
    # `pvb:4` = PIDs-Limit 512→1024 (s. _build_create_args) — statischer
    # Marker zwingt beim Agent-Update die einmalige Recreate aller
    # bestehenden Container (die das alte Limit tragen). WICHTIG: Marker
    # und Create-Args immer gemeinsam deployen, sonst stimmt der Hash nie
    # und der Container wird bei jedem Access rekriert.
    parts = [f"tmpfs:{_TMPFS_SPEC}", "pvb:4"]  # Spec-Wechsel zwingt einmalige Recreate
    for rp in sorted(spec.get("readonly_paths") or []):
        src = a_dir / rp
        if src.is_dir():
            parts.append(f"{rp}:d")
        elif src.is_file():
            try:
                st = src.stat()
                parts.append(f"{rp}:f:{st.st_size}:{st.st_mtime_ns}")
            except OSError:
                parts.append(f"{rp}:f")
        else:
            parts.append(f"{rp}:n")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _container_label(key: str, label: str) -> str | None:
    proc = _docker("inspect", "-f", "{{index .Config.Labels \"%s\"}}" % label,
                   container_name(key), check=False)
    if proc.returncode != 0:
        return None
    val = proc.stdout.decode().strip()
    return None if val in ("", "<no value>") else val


def _desired_image(key: str, spec: dict) -> str:
    """Lauffähiges Image der Aufgabe: Task-Commit-Image > Basis-Image
    (falls der Init-Build das Image nicht verändert hat — Marker im
    Asset-Dir, ersetzt das frühere Alias-Tag) > Spec-Image.

    Fallback: task_image gesetzt, aber nichts auflösbar UND die Init-
    Skripte sind nicht mehr da → Spec-Image, damit der Container
    trotzdem startet. Bei vorhandenen Init-Skripten bleibt ein fehlendes
    Image ein harter Fehler (409).
    """
    task_image = spec.get("task_image")
    if task_image:
        course, task, _ = key_parts(key)
        if ":" in task_image:
            resolved = resolve_task_image(
                course, task, task_image.rsplit(":", 1)[-1])
            if resolved:
                return resolved
        if task_has_init(course, task):
            return task_image  # Build ausstehend → harter Fehler (409)
    return spec["image"]


def _container_image(key: str) -> str | None:
    """Image-Referenz, mit der der Container angelegt wurde (None = kein Container)."""
    proc = _docker("inspect", "-f", "{{.Config.Image}}",
                   container_name(key), check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode().strip()


def create_container(key: str, spec: dict) -> str:
    """Container erzeugen (und starten). Liefert das aufgelöste Image."""
    _gpu_args(spec)  # list[int] ohne GPU-Fähigkeit → klarer 409
    if spec["internet"]:
        ensure_ws_network(key)
    image = resolve_image(_desired_image(key, spec))
    args = _build_create_args(key, spec, image)
    _docker(*args, timeout=120)
    _docker("start", container_name(key), timeout=60)
    return image


def ensure_container(key: str, spec: dict) -> dict:
    """Container sicherstellen: läuft → ok; gestoppt → start; fehlt → create.

    Idempotent. Starter-Dateien werden NUR geschrieben, wenn das Volume
    noch frisch ist (erster Start des Studenten). Läuft der Container mit
    anderem Image als gewünscht (z. B. Task-Image-Wechsel nach
    .init.sh-Änderung) ODER mit einem anderen ro-Mount-Layout (Label
    aicampus.mounts), wird der Container neu angelegt — das Volume
    (Studenten-Dateien) bleibt erhalten.
    """
    course, task, _ = key_parts(key)
    desired = _desired_image(key, spec)
    state = container_state(key)
    if state is not None and (
            _container_image(key) not in (None, desired)
            or _container_label(key, "aicampus.mounts") != _mounts_hash(spec, course, task)):
        # Image-/Mount-Mismatch → Container weg (Volume bleibt), neu anlegen
        clean_phantom_mounts(key)  # Mount-Point-Reste VOR dem Rm räumen
        _docker("rm", "-f", container_name(key), timeout=60, check=False)
        state = None
    if state == "running":
        return {"state": "running", "fresh": False}
    if state in ("created", "exited", "paused"):
        if state != "created":
            _docker("start", container_name(key), timeout=60)
        return {"state": "running", "fresh": False}
    # Neu anlegen (Volume bleibt erhalten)
    had_volume = volume_exists(key)
    image = create_container(key, spec)
    return {"state": "running", "fresh": not had_volume, "image": image}


def remove_workspace(key: str) -> None:
    """Container entfernen (Volume bleibt!); mit force für laufende Jobs."""
    c = container_name(key)
    if container_state(key) is not None:
        _docker("rm", "-f", c, timeout=60, check=False)
    _docker("volume", "rm", volume_name(key), check=False)
    remove_ws_network(key)
    _PORT_CONFIRMED.pop(key, None)  # frischer Container = frische Bestätigung


def remove_volume_only(key: str) -> None:
    """Nur das /workspace-Volume entfernen (Sleep-Backup liegt auf dem
    Server; Engine-Disk wird frei). Container/Netz sind dann schon weg."""
    _docker("volume", "rm", volume_name(key), check=False)


def stop_container_only(key: str) -> None:
    """Nur Container entfernen (Idle-Kill des Reapers), Volume bleibt."""
    if container_state(key) is not None:
        clean_phantom_mounts(key)  # Mount-Point-Reste VOR dem Rm räumen
        _docker("rm", "-f", container_name(key), timeout=60, check=False)
        remove_ws_network(key)
    _PORT_CONFIRMED.pop(key, None)  # frischer Container = frische Bestätigung


def stop_container_soft(key: str) -> None:
    """Container stoppen OHNE zu entfernen (manueller Stopp + Quota-Kill).

    Der (exited) Container bleibt bestehen, damit Datei-/Port-Operationen
    ihn per docker start wiederbeleben können (s. _ensure_running) —
    Voraussetzung, dass ein Student nach einem Quota-Kill überhaupt noch
    aufräumen kann. Der Idle-Reaper entfernt ihn nach IDLE_TIMEOUT.
    Volume bleibt erhalten (in beiden Fällen)."""
    if container_state(key) in ("running", "created"):
        _docker("stop", container_name(key), timeout=60)
    _PORT_CONFIRMED.pop(key, None)  # Prozesse tot = Bestätigung veraltet


def list_workspaces() -> list[dict]:
    proc = _docker("ps", "-a", "--filter", "label=aicampus.ws",
                   "--format", "{{.Labels}}\t{{.State}}", check=False)
    out = []
    for line in proc.stdout.decode().splitlines():
        if "\t" not in line:
            continue
        labels, state = line.split("\t", 1)
        key = None
        for part in labels.split(","):
            if part.startswith("aicampus.ws="):
                key = part.split("=", 1)[1]
                break
        if key:
            # {{.State}} = sauberer Status (running/exited/created) —
            # {{.Status}} wäre ein Menschentext ("Up 5 min") ohne
            # maschinenlesbares State-Field.
            out.append({"key": key, "state": state.strip() or "?"})
    return out


# ── Datei-Operationen (via docker exec im Container) ──────────────

def safe_workspace_path(path: str) -> str:
    """Pfad sanitieren: relativ, kein .., Ergebnis unter /workspace."""
    p = path.replace("\\", "/").lstrip("/")
    if not p or p.startswith("..") or any(x.startswith("..") for x in p.split("/")):
        raise DockerError("Ungültiger Dateipfad", 400)
    full = os.path.normpath("/workspace/" + p)
    if not full.startswith("/workspace/"):
        raise DockerError("Ungültiger Dateipfad", 400)
    return full


def safe_asset_path(path: str) -> str:
    p = path.replace("\\", "/").lstrip("/")
    if not p or any(x in ("", "..") for x in p.split("/")):
        raise DockerError("Ungültiger Asset-Pfad", 400)
    return os.path.normpath(p)


def _is_readonly_workspace_path(rel: str, spec: dict | None) -> bool:
    """Read-only-Pfade im Workspace (Zugriffs-Klasse 🔒): der Pfad selbst
    oder etwas darunter (ro-Bind-Mounts aus dem Asset-Dir)."""
    for rp in (spec or {}).get("readonly_paths") or []:
        if rel == rp or rel.startswith(rp + "/"):
            return True
    return False


def _ensure_running(key: str) -> None:
    state = container_state(key)
    if state == "running":
        return
    if state is None:
        raise DockerError("Workspace existiert nicht — erst POST /workspaces", 409)
    # Gestoppter Workspace (hold "user" = manuell, "quota" = Watchdog):
    # NICHT on-demand starten — der Stopp bleibt bestehen, Start läuft nur
    # per ▶ Starten. (Quota: erst starten, DANN aufräumen per Dateibaum.)
    from . import reaper  # Lazy: reaper importiert docker_ops (Zyklus)
    hold = (reaper.REGISTRY.get(key) or {}).get("hold") or {}
    if hold.get("reason") == "user":
        raise DockerError("Arbeitsumgebung ist gestoppt — bitte erst starten (▶ Starten).", 409)
    if hold.get("reason") == "quota":
        raise DockerError(
            "Arbeitsumgebung ist gestoppt: Disk-Limit um mehr als 50% "
            "überschritten (neueste Dateien wurden automatisch gelöscht) "
            "— bitte ▶ Starten.", 409)
    _docker("start", container_name(key), timeout=60)


# ── Preview-Relay ───────────────────────────────────────────────

_RELAY_LOCAL_SHA: str | None = None  # SHA des lokalen Binaries (lazy, 1×)


def _relay_local_sha() -> str:
    """SHA-256 des lokalen Relay-Binaries (lazys — 2 MB nur 1× lesen)."""
    global _RELAY_LOCAL_SHA
    if _RELAY_LOCAL_SHA is None:
        rel = Path(config.RELAY_PATH)
        if not rel.exists():
            raise DockerError(
                "Relay-Binary fehlt im Agent — compute-agent neu bauen "
                "bzw. scripts/build_relay.sh ausführen", 500)
        _RELAY_LOCAL_SHA = hashlib.sha256(rel.read_bytes()).hexdigest()
    return _RELAY_LOCAL_SHA


def ensure_relay(key: str) -> None:
    """Relay-Binary im Container sicherstellen (/tmp/relay, on-demand).

    IMMER In-Container-Check (SHA-256-Marker /tmp/.relay_sha), bewusst
    KEIN In-Memory-Cache: /tmp (tmpfs) kann jederzeit leergefegt werden
    (Container-Recreate, manuelles Aufräumen im Terminal) — ein
    veralteter Cache würde dann ALLE Previews mit 502 blockieren,
    bis der Agent neu startet (empirisch beobachtet). Der Check ist
    ein kleiner `docker exec` (test -x + cat) — vernachlässigbar
    gegenüber dem Relay-Spawn selbst.

    KEIN docker cp: der Container hat read-only Rootfs, und der Daemon
    lehnt cp-Ziele außerhalb der Volumes (hier: tmpfs /tmp) ab
    („container rootfs is marked read-only") — das Binary wird
    stattdessen per exec-stdin gestreamt (sh-Redirect schreibt in die
    schreibbare tmpfs).

    Nach einem Agent-Image-Update (geändertes Relay-Binary) weicht der
    SHA ab → das Binary wird in LAUFENDEN Containern automatisch
    ersetzt.
    """
    _ensure_running(key)
    want = _relay_local_sha()
    code, out, _err, _ = _run_capped(
        ["docker", "exec", container_name(key), "sh", "-c",
         "test -x /tmp/relay && cat /tmp/.relay_sha 2>/dev/null"],
        timeout=30, cap=1024)
    have = out.decode("latin-1").strip()
    if code != 0 or have != want:
        # Temp+Rename (atomares mv): /tmp/relay lässt sich NICHT
        # überschreiben, solange ein Relay-Prozess mit dem alten Binary
        # noch läuft (ETXTBSY „Text file busy") — bei aktivem
        # Preview-Traffic wäre das Update sonst ein harter 500. Mit
        # mv hält der alte Prozess sein altes Inode weiter (läuft
        # sauber aus), neue Execs bekommen automatisch das neue Binary.
        rel = Path(config.RELAY_PATH)
        _docker("exec", "-i", container_name(key), "sh", "-c",
                f"cat > /tmp/relay.new && chmod +x /tmp/relay.new "
                f"&& mv -f /tmp/relay.new /tmp/relay "
                f"&& echo {want} > /tmp/.relay_sha",
                input_bytes=rel.read_bytes(),
                timeout=120)


def _run_capped(cmd: list[str], timeout: int, cap: int,
                input_bytes: bytes | None = None,
                on_progress: Callable[[bytes], None] | None = None) -> tuple[int, bytes, bytes, bool]:
    """subprocess mit Timeout + Output-Cap.

    Liefert (exit_code, stdout, stderr, timed_out). Bei Cap-Überschreitung
    wird der Rest verworfen (Pipe weitergelesen, Prozess blockiert nicht).

    on_progress: optionaler Callback pro gelesenen Chunk — bekommt den
    Tail (letzte ~8 KB) der kombinierten stdout+stderr-Ausgabe, für
    Live-Logs (z. B. init-Build). Der Caller darf throttlen.
    """
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL if input_bytes is None else subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if input_bytes is not None:
        try:
            proc.stdin.write(input_bytes)
        except BrokenPipeError:
            pass
        proc.stdin.close()

    out, err = bytearray(), bytearray()
    overflow = {"out": False, "err": False}
    live = bytearray() if on_progress is not None else None

    def drain(pipe, buf: bytearray, flag: str) -> None:
        # os.read (nicht pipe.read!): BufferedReader.read(n) auf einer
        # Pipe blockiert, bis n Bytes o. EOD zusammenkommen — das würde
        # den Live-Log (on_progress) erst beim Prozessende feuern.
        # os.read liefert sofort, sobald Daten da sind.
        try:
            while True:
                chunk = os.read(pipe.fileno(), 65536)
                if not chunk:
                    break
                if len(buf) < cap:
                    buf.extend(chunk[: cap - len(buf)])
                else:
                    overflow[flag] = True
                if live is not None:
                    live.extend(chunk)
                    if len(live) > 16384:
                        del live[: len(live) - 8192]
                    try:
                        on_progress(bytes(live))
                    except Exception:
                        pass
        except Exception:
            pass

    t1 = threading.Thread(target=drain, args=(proc.stdout, out, "out"), daemon=True)
    t2 = threading.Thread(target=drain, args=(proc.stderr, err, "err"), daemon=True)
    t1.start()
    t2.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait(timeout=10)
    t1.join(timeout=5)
    t2.join(timeout=5)

    if overflow["out"]:
        out += b"\n... (weitere Ausgabe nicht aufgezeichnet)\n"
    if overflow["err"]:
        err += b"\n... (weitere Ausgabe nicht aufgezeichnet)\n"
    return proc.returncode, bytes(out), bytes(err), timed_out


def exec_sync(key: str, command: str, working_dir: str = "/workspace",
              timeout: int = 60, cap: int = 2_000_000) -> dict:
    """Synchronen Lauf (kurze Befehle)."""
    _ensure_running(key)
    code, out_b, err_b, timed_out = _run_capped(
        ["docker", "exec", "-i", "-w", working_dir, container_name(key),
         "sh", "-c", command],
        timeout=timeout, cap=cap,
    )
    return {
        "exit_code": None if timed_out else code,
        "stdout": out_b.decode(errors="replace")[-1_000_000:],
        "stderr": err_b.decode(errors="replace")[-1_000_000:],
        "timed_out": timed_out,
    }


# ── Port-Erkennung / -Kill (Preview) ──────────────────────────────

# Einmaliger Scan im Container: erst die LISTEN-Sockets aus
# /proc/net/tcp{,6} (Inode + Port-HEX + Bind-Adresse-HEX), dann der
# fd-Walk (PID → Inode). Kein procps/ss nötig (fehlen in slim-Images);
# mawk hat kein strtonum → Port-HEX kommt nach Python.
#
# Die Bind-Adresse fliegt mit, weil der Preview-Relay IMMER
# 127.0.0.1 dialt (s. relay/relay.go): nur Listener, die von dort aus
# erreichbar sind, sind in der UI sinnvoll — also 0.0.0.0 / 127.0.0.1 /
# :: (Dual-Stack) bzw. v4-mapped ::ffff:-Formen davon. Listener, die
# ausschliesslich auf ::1 oder auf die NIC-IP (eth0) gebunden sind,
# wären in der Liste sichtbar, aber per Preview NICHT antwortbar —
# ebenso der Docker-Built-in-DNS (127.0.0.11, von dem der Daemon bei
# User-Defined-Netzwerken Listener in den Container-Netns injiziert).
# Gefiltert wird in _port_scan per Adress-Whitelist (little-endian-
# HEX, s. _loopback_reachable).
_PORT_SCAN_SCRIPT = (
    'echo "L"; '
    'awk \'FNR>1 && $4=="0A" {split($2,a,":"); '
    'print $10" "a[2]" "a[1]}\' '
    '/proc/net/tcp /proc/net/tcp6 2>/dev/null; '
    'echo "P"; '
    'for f in /proc/[0-9]*/fd/*; do '
    'l=$(readlink "$f" 2>/dev/null) || continue; '
    'case "$l" in socket:*) '
    'ino=${l#socket:[}; ino=${ino%]}; '
    'case "$ino" in *[!0-9]*) continue;; esac; '
    'pid=${f#/proc/}; pid=${pid%%/*}; '
    'echo "$pid $ino";; esac; done'
)


# /proc-Adressen sind little-endian: 127.0.0.1 → „0100007F“,
# 127.0.0.11 (Docker-DNS) → „0B00007F“, 172.18.0.2 → „020012AC“.
_REACHABLE_V4 = ("00000000", "0100007F")  # 0.0.0.0, 127.0.0.1


def _loopback_reachable(addr: str) -> bool:
    """HEX-Bind-Adresse aus /proc/net/tcp{,6} → per 127.0.0.1 erreichbar?

    True nur für die Adressen, an die der Preview-Relay (127.0.0.1) auch
    wirklich connecten kann: v4-Wildcard, 127.0.0.1, v6-Wildcard
    (Dual-Stack) sowie die seltenen expliziten v4-mapped-Binds
    ::ffff:0.0.0.0 / ::ffff:127.0.0.1. ::1, NIC-IPs und 127.0.0.11
    fallen raus → werden in der UI nicht gelistet.
    """
    a = addr.upper()
    if len(a) == 8:
        return a in _REACHABLE_V4
    if len(a) == 32:
        if a == "0" * 32:  # :: (Dual-Stack, v4-mapped-Connects angenommen)
            return True
        if a[16:24] == "FFFF0000":  # ::ffff:a.b.c.d → v4-Teil prüfen
            return a[24:32] in _REACHABLE_V4
    return False


def _port_scan(key: str) -> dict[int, set[str]]:
    """Vom Preview-Relay erreichbare LISTEN-Ports: {port: {pid, …}}.

    Sammelt ALLE PIDs, die den Socket-Inode halten (früher nur der
    erste). Listener auf Bind-Adressen, an die 127.0.0.1 nicht
    connecten kann (::1, NIC-IP, Docker-DNS), bleiben ausgeblendet.
    """
    _ensure_running(key)
    code, out_b, _err, _ = _run_capped(
        ["docker", "exec", container_name(key), "sh", "-c", _PORT_SCAN_SCRIPT],
        timeout=30, cap=1_000_000)
    if code != 0:
        raise DockerError("Port-Scan im Workspace fehlgeschlagen", 500)
    pids_by_ino: dict[str, set[str]] = {}
    listeners: list[tuple[str, int]] = []
    section = None
    for line in out_b.decode(errors="replace").splitlines():
        if line in ("L", "P"):
            section = line
            continue
        parts = line.split()
        if section == "L":
            if len(parts) != 3:
                continue
            ino, port_hex, addr = parts
            try:
                port = int(port_hex, 16)
            except ValueError:
                continue
            if 0 < port <= 65535 and _loopback_reachable(addr):
                listeners.append((ino, port))
        elif section == "P":
            if len(parts) != 2:
                continue
            pid, ino = parts
            pids_by_ino.setdefault(ino, set()).add(pid)
    ports: dict[int, set[str]] = {}
    for ino, port in listeners:
        ports.setdefault(port, set()).update(pids_by_ino.get(ino, ()))
    return ports


# Anti-Flacker-Debounce (s. list_workspace_ports): letzter Scan je Key.
# Wird in remove_workspace/stop_container_only geleert (frischer
# Container = frische Bestätigung).
_PORT_CONFIRMED: dict[str, dict[int, set[str]]] = {}


def list_workspace_ports(key: str) -> list[dict]:
    """Im Container lauschende, BESTÄTIGTE Ports: [{port, pid}] (sortiert).

    Bestätigt = in zwei aufeinanderfolgenden Scans gesehen (UI-Poll ist
    ~8 s): echte Web-Dienste sind über Scans hinweg stabil und erscheinen
    einen Poll später; kurzlebige Helfer-Listener (z. B. ephemale
    localhost-Ports, die ML-Tools um ihren Hauptprozess aufmachen) wären
    sonst als „Ghost-Port" in der UI sichtbar, bis der Prozess stirbt —
    der Preview-Klick darauf endet dann in „connection refused".
    Funktioniert auch bei network=none (Loopback existiert immer).
    """
    ports = _port_scan(key)
    prev = _PORT_CONFIRMED.get(key)
    _PORT_CONFIRMED[key] = ports
    if prev is None:
        return []  # erster Scan: alles erst in Bestätigung
    confirmed = {p: pids for p, pids in ports.items() if p in prev}
    return [{"port": p, "pid": min(pids) if pids else None}
            for p, pids in sorted(confirmed.items())]


# Wie runs._TREE_KILL, aber Signal-Parameter (15/9): rekursiv den
# Prozessbaum (Kinder zuerst, dann Wurzel) mit dem Signal versehen.
_TREE_SIGNAL = (
    'kt(){ r=$1; sig=$2; for d in /proc/[0-9]*; do p=${d#/proc/}; '
    '[ "$p" = "$r" ] && continue; s=$(cat "$d/stat" 2>/dev/null) || continue; '
    's=${s##*) }; set -- $s; [ "$2" = "$r" ] && kt $p $sig; done; '
    'kill -$sig "$r" 2>/dev/null; }'
)


def _port_tree_signal(key: str, pids: set[str], sig: int) -> None:
    """Signal an den Prozessbaum jedes PIDs senden.

    check=True: schlug der docker-exec-Spawn fehl (z. B. PIDs-Limit
    EAGAIN), ist der Kill NICHT erfolgt → Fehler nach oben statt
    stillen 200. Der Exit-Code selbst ist egal (verifiziert wird
    per Re-Scan); deshalb am Ende `true`.
    """
    for p in pids:
        if not p.isdigit():
            raise DockerError(f"Ungültige PID aus Port-Scan: {p!r}", 500)
    cmd = (_TREE_SIGNAL + "; "
           + "".join(f"kt {p} {sig}; " for p in sorted(pids)) + "true")
    _docker("exec", container_name(key), "sh", "-c", cmd, timeout=30)


def kill_workspace_port(key: str, port: int, force: bool = False) -> None:
    """Prozessbaum des Ports beenden und per Re-Scan VERIFIZIEREN.

    - ohne force: SIGTERM, bis zu 5 s warten. Port danach noch belegt
      → 409 (UI kann den Force-Kill als zweiten Schritt anbieten).
    - force: SIGKILL über den ganzen Baum. Danach noch belegt → 409
      (Prozess z. B. in uninterruptible Sleep).
    - Port nicht (mehr) belegt → 404.
    """
    pids = _port_scan(key).get(port) or set()
    if not pids:
        raise DockerError(f"Port {port} wird nicht (mehr) belegt", 404)
    _port_tree_signal(key, pids, 9 if force else 15)
    deadline = time.monotonic() + (3.0 if force else 5.0)
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if not _port_scan(key).get(port):
            return
    if force:
        msg = (f"Port {port} ist auch nach erzwungener Beendigung noch belegt — "
               f"Prozess hängt vermutlich (uninterruptible State). "
               f"Workspace ggf. zurücksetzen.")
    else:
        msg = (f"Port {port} ist nach dem Stop-Versuch noch belegt — "
               f"Prozess reagiert nicht auf SIGTERM.")
    raise DockerError(msg, 409)


def list_files(key: str) -> list[dict]:
    _ensure_running(key)
    code, out_b, _err, _ = _run_capped(
        ["docker", "exec", container_name(key),
         "sh", "-c", "find /workspace -type f -printf '%s %p\\n' 2>/dev/null"],
        timeout=30, cap=10_000_000,
    )
    if code != 0:
        # exec scheitert u. a. am PIDs-Limit des Containers (EAGAIN). Vorher
        # wurde still eine LEERE Liste geliefert → der Dateibaum im UI "leerte"
        # sich ohne jeden Fehler (nur DB-gepflegte Ordner blieben sichtbar).
        raise DockerError(
            "Dateilistung fehlgeschlagen — Container nicht antwortfähig "
            "(PIDs-/Ressourcen-Limit?)", 500)
    files = []
    for line in out_b.decode(errors="replace").splitlines():
        if " " not in line:
            continue
        size, path = line.split(" ", 1)
        rel = path[len("/workspace/"):]
        if rel:
            files.append({"path": rel, "size": int(size) if size.isdigit() else 0})
    return files


def list_dirs(key: str) -> list[str]:
    """Alle Verzeichnisse unter /workspace (relativ, sortiert) — auch leere,
    damit z. B. per Terminal/mkdir angelegte Ordner im Tree sichtbar sind."""
    _ensure_running(key)
    code, out_b, _err, _ = _run_capped(
        ["docker", "exec", container_name(key),
         "sh", "-c", "find /workspace -mindepth 1 -type d -printf '%p\\n' 2>/dev/null"],
        timeout=30, cap=1_000_000,
    )
    if code != 0:
        # s. list_files: keine stille Leere-Liste bei Executor-Fehlern.
        raise DockerError(
            "Verzeichnisliste fehlgeschlagen — Container nicht "
            "antwortfähig (PIDs-/Ressourcen-Limit?)", 500)
    dirs = []
    for line in out_b.decode(errors="replace").splitlines():
        rel = line[len("/workspace/"):]
        if rel:
            dirs.append(rel)
    return sorted(dirs)


def read_file(key: str, path: str) -> bytes:
    full = safe_workspace_path(path)
    _ensure_running(key)
    code, out_b, _err, _ = _run_capped(
        ["docker", "exec", "-i", container_name(key), "cat", full],
        timeout=120, cap=config.MAX_FILE_SIZE + 1,
    )
    if code != 0 or len(out_b) > config.MAX_FILE_SIZE:
        raise DockerError("Datei nicht lesbar oder zu groß", 404)
    return out_b


def write_file(key: str, path: str, data: bytes,
               spec: dict | None = None) -> int:
    full = safe_workspace_path(path)
    if _is_readonly_workspace_path(full[len("/workspace/"):], spec):
        raise DockerError("Datei ist read-only (vom Tutor verwaltet)", 403)
    if len(data) > config.MAX_FILE_SIZE:
        raise DockerError("Datei zu groß (max. 50 MB)", 413)
    _ensure_running(key)
    parent = os.path.dirname(full)
    code, _o, err_b, _ = _run_capped(
        ["docker", "exec", "-i", container_name(key), "sh", "-c",
         f"mkdir -p {shlex.quote(parent)} && cat > {shlex.quote(full)}"],
        timeout=120, cap=64 * 1024, input_bytes=data,
    )
    if code != 0:
        raise DockerError(f"Schreiben fehlgeschlagen: {err_b.decode(errors='replace')[:300]}", 500)
    return len(data)


def move_file(key: str, src: str, dst: str,
              spec: dict | None = None) -> None:
    full_src = safe_workspace_path(src)
    full_dst = safe_workspace_path(dst)
    src_rel = full_src[len("/workspace/"):]
    dst_rel = full_dst[len("/workspace/"):]
    if _is_readonly_workspace_path(src_rel, spec):
        raise DockerError("Datei ist read-only (vom Tutor verwaltet)", 403)
    if _is_readonly_workspace_path(dst_rel, spec):
        raise DockerError("Zielpfad ist read-only (vom Tutor verwaltet)", 403)
    _ensure_running(key)
    parent = os.path.dirname(full_dst)
    code, _o, err_b, _ = _run_capped(
        ["docker", "exec", "-i", container_name(key), "sh", "-c",
         f"test -e {shlex.quote(full_src)} && "
         f"mkdir -p {shlex.quote(parent)} && "
         f"mv {shlex.quote(full_src)} {shlex.quote(full_dst)}"],
        timeout=60, cap=64 * 1024,
    )
    if code != 0:
        msg = err_b.decode(errors="replace")[:300] or "Zielpfad belegt?"
        raise DockerError(f"Verschieben fehlgeschlagen: {msg}", 400)


def create_dir(key: str, path: str, spec: dict | None = None) -> None:
    full = safe_workspace_path(path)
    if _is_readonly_workspace_path(full[len("/workspace/"):], spec):
        raise DockerError("Ordner ist read-only (vom Tutor verwaltet)", 403)
    _ensure_running(key)
    code, _o, err_b, _ = _run_capped(
        ["docker", "exec", container_name(key), "sh", "-c",
         f"mkdir -p {shlex.quote(full)}"],
        timeout=30, cap=64 * 1024,
    )
    if code != 0:
        raise DockerError(
            f"Ordner-Anlage fehlgeschlagen: {err_b.decode(errors='replace')[:300]}", 500)


def delete_path(key: str, path: str, spec: dict | None = None) -> None:
    full = safe_workspace_path(path)
    if _is_readonly_workspace_path(full[len("/workspace/"):], spec):
        raise DockerError("Datei ist read-only (vom Tutor verwaltet)", 403)
    _ensure_running(key)
    code, _o, _e, _ = _run_capped(
        ["docker", "exec", container_name(key), "sh", "-c",
         f"rm -rf {shlex.quote(full)}"],
        timeout=30, cap=64 * 1024,
    )
    if code != 0:
        raise DockerError("Löschen fehlgeschlagen", 500)


def write_starter_files(key: str, files: list[dict],
                        folders: list[str] | None = None) -> int:
    """Starter-Dateien (path + content_b64) + leere Ordner in ein FRISCHES
    Volume schreiben.

    Nutzt docker cp über einen Temp-Ordner (funktioniert auch bei
    gestopptem/neuem Container, da /workspace der Volume-Mount ist).

    ``folders``: explizite Task-Ordner, die auch ohne Dateien existieren
    müssen (z. B. ein leerer .solution/ — würde sonst im Volume fehlen,
    weil Dateien den Ordner sonst erst erzeugen). docker cp ist
    tar-basiert und behält leere Verzeichnisse bei. Gleiche Konvention
    wie assets_sync (Host-Asset-Dir) und init_build (Build-Quellen).
    """
    n = 0
    with tempfile.TemporaryDirectory(prefix="aicampus-starter-") as tmp:
        for d in folders or []:
            rel = safe_workspace_path(str(d))  # wirft bei unsauberem Pfad
            os.makedirs(os.path.join(tmp, os.path.relpath(rel, "/workspace")),
                        exist_ok=True)
        for f in files:
            rel = safe_workspace_path(f["path"])  # werft bei unsauberem Pfad
            target = os.path.join(tmp, os.path.relpath(rel, "/workspace"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(base64.b64decode(f["content_b64"]))
            if os.path.basename(target).endswith(".sh"):
                os.chmod(target, 0o755)  # Skripte ausführbar (Seed-Volumes)
            n += 1
        if n == 0 and not (folders or []):
            return 0
        # Alles in einem Rutsch: docker cp (Quelle-Endung / → Inhalt kopieren)
        _docker("cp", tmp + "/.", f"{container_name(key)}:/workspace", timeout=300)
    return n


def clean_phantom_mounts(key: str) -> None:
    """0-Byte-Platzhalter aus dem /workspace-Volume entfernen, die das
    Docker-Runtime beim Container-Start für jede 🔒-Datei-Mount-Ziel anlegt
    (Mount-Point, falls dort nichts existiert). Solange der ro-Mount
    existiert, verstecken sie sich darunter; verschwindet der Mount
    (Zugriffs-Klasse 🔒→✏️/👤, Datei gelöscht), würden sie als leere
    Schatten-Dateien im Studenten-Volume sichtbar.

    VOR dem Container-Rm aufrufen (liest dessen Mounts + Image). Best-
    effort: ein fehlgeschlagener Lauf ist harmlos (der nächste Rm räumt
    erneut auf)."""
    proc = _docker("inspect", "-f", "{{json .Mounts}}",
                   container_name(key), check=False)
    if proc.returncode != 0:
        return  # kein Container → nichts zu räumen
    image = _container_image(key)
    if not image or not volume_exists(key):
        return
    try:
        mounts = json.loads(proc.stdout.decode() or "[]")
    except ValueError:
        return
    phantoms = sorted(
        m["Destination"][len("/workspace/"):]
        for m in mounts
        if m.get("Type") == "bind"
        and str(m.get("Destination", "")).startswith("/workspace/"))
    if not phantoms:
        return
    # Datei → rm -f; Ordner → rmdir (nur wenn leer — unter einem 🔒-Mount
    # kann der Student nichts hineinlegen, ist also immer sicher).
    script = "; ".join(
        f"if [ -d {q} ]; then rmdir {q} 2>/dev/null; else rm -f {q}; fi"
        for q in (shlex.quote(f"/ws/{p}") for p in phantoms))
    _docker("run", "--rm", "--entrypoint", "/bin/sh",
            "-v", f"{volume_name(key)}:/ws", image, "-c", script,
            check=False, timeout=120)


def snapshot(key: str, cap: int | None = None,
            exclude_paths: list[str] | None = None) -> bytes:
    """tar.gz des /workspace-Volumes (mit Größen-Cap).

    exclude_paths: 🔒-ro-Mounts (spec.readonly_paths). Die liegen NICHT
    im Named-Volume (geteilte Asset-Region, z. B. Datasets aus .init.sh)
    und zählen weder gegen die Disk-Quota noch zur Abgabe — von der
    Container-Sicht aus würde tar sie aber trotzdem miterfassen und das
    Snapshot um mehrere GB aufblähen, daher explizit ausschließen.

    Bei Cap-Erreichung wird der Snapshot abgelehnt, statt still
    abzuschneiden: ein abgeschnittenes tar.gz wäre bei der Korrektur
    unlesbar und die Einreichung wäre unwiederbringlich beschädigt.
    """
    _ensure_running(key)
    cap = cap or config.MAX_WORKSPACE_SIZE
    cmd = ["docker", "exec", container_name(key), "tar", "-czf", "-"]
    # Verankert auf "./<pfad>": pruned exakt den ro-Mount, aber nie
    # Student-Dateien mit gleichem Namen tiefer im Baum (z. B. src/data).
    for rp in exclude_paths or []:
        cmd += ["--exclude", f"./{rp}"]
    cmd += ["-C", "/workspace", "."]
    code, out_b, _err, _ = _run_capped(cmd, timeout=600, cap=cap)
    if code != 0:
        raise DockerError("Snapshot fehlgeschlagen", 500)
    # _run_capped bricht den Stream bei cap ab (Exit-Code bleibt 0, da der
    # Pipe-Drain tar normal abschließen lässt) → Größen-Check als Guard.
    if len(out_b) >= cap:
        raise DockerError(
            f"Workspace-Snapshot überschreitet das Größenlimit "
            f"({cap // (1024 * 1024)} MB) — bitte unnötige Dateien "
            f"(z. B. Datasets, Logs, alte Checkpoints) aus dem "
            f"Workspace löschen und erneut einreichen", 413)
    return out_b


def volume_backup_tar(key: str, cap: int) -> bytes | None:
    """tar.gz des /workspace-Volumes, HOSTSEITIG (Sleep-Backup).

    Funktioniert auch bei gestopptem Container (Volume-Mountpoint).
    Enthält exakt das Named-Volume — 🔒-ro-Mounts sind physikalisch
    nicht darin (keine Doppel-Sicherung der Task-Datasets).

    None = Volume nicht hostseitig erreichbar (Feature in dieser
    Deployment nicht verfügbar) oder nach dem Tarren über `cap`
    (Volume bleibt dann auf der Engine, s. backup.maybe_sleep_backup).
    """
    mp = _volume_mountpoint(key)
    if not mp or not os.path.isdir(mp):
        return None
    buf = io.BytesIO()
    try:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for root, dirs, files in os.walk(mp):
                for name in sorted(files):
                    p = os.path.join(root, name)
                    rel = os.path.relpath(p, mp).replace(os.sep, "/")
                    try:
                        tar.add(p, arcname=rel, recursive=False)
                    except OSError:
                        continue  # Datei im selben Moment weg → überspringen
    except Exception:
        return None
    if len(buf.getvalue()) > cap:
        return None
    return buf.getvalue()


def restore_volume_tar(key: str, data: bytes) -> int:
    """Sleep-Backup (tar.gz) in das /workspace-Volume entpacken (hostseitig).

    Liefert die Anzahl der wiederhergestellten Dateien. Bestehende
    Dateien (Starter-Dateien im frischen Volume) werden vom Backup
    überschrieben — der Student-Stand gewinnt. Pfad-Escape und
    Device-Files werden abgelehnt; Symlinks sind erlaubt (venvs o.ä.).
    """
    mp = _volume_mountpoint(key)
    if not mp or not os.path.isdir(mp):
        raise DockerError(
            "Volume nicht hostseitig erreichbar — Restore nicht möglich", 500)
    n_files = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            name = member.name.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise DockerError(f"Ungültiger Backup-Eintrag: {member.name}", 500)
            if member.isdev():
                raise DockerError(f"Device-Files nicht erlaubt: {member.name}", 500)
            n_files += 1 if member.isfile() else 0
        tar.extractall(mp)
    return n_files


def _volume_mountpoint(key: str) -> str | None:
    """Host-Mountpoint des /workspace-Named-Volumes (für die hostseitige
    Disk-Messung + Aufräumen); None, wenn das Volume fehlt."""
    proc = _docker("volume", "inspect", "-f", "{{.Mountpoint}}",
                   volume_name(key), check=False)
    if proc.returncode != 0:
        return None
    p = proc.stdout.decode(errors="replace").strip()
    return p or None


def workspace_disk_usage(key: str, exclude_paths: list[str] | None = None) -> int:
    """Belegte Bytes im /workspace-Volume (Summe der Datei-Größen).

    Primär HOSTSEITIG (os.walk über den Volume-Mountpoint) — funktioniert
    auch bei gestopptem Container und ist vom Student-Container aus nicht
    beeinflussbar (Quota-Safety-Net, s. plan + reaper._check_disk_quota).
    Fallback: docker exec (nur mit laufendem Container). `exclude_paths`
    (🔒-Top-Level-Mounts) werden immer ausgespart — geteilte Assets
    zählen nicht gegen das Student-Quota. Liefert 0 bei Fehlschlag
    (best effort; die Quota darf den Workspace nicht lahmlegen).
    """
    mp = _volume_mountpoint(key)
    if mp and os.path.isdir(mp):
        try:
            excl = {str(p).strip("/") for p in (exclude_paths or [])
                    if str(p).strip("/")}
            total = 0
            for root, dirs, files in os.walk(mp):
                rel = os.path.relpath(root, mp)
                top = None if rel == "." else rel.split(os.sep)[0]
                if top in excl:
                    dirs[:] = []
                    files = []
                    continue
                for name in files:
                    try:
                        total += os.lstat(os.path.join(root, name)).st_size
                    except OSError:
                        pass
            return total
        except Exception:
            pass  # z. B. kein ro-Mount im Agent (Fremd-Data-Root) → Fallback
    if container_state(key) != "running":
        return 0
    prune = ""
    for p in exclude_paths or []:
        prune += f" -path {shlex.quote('/workspace/' + p)} -prune -o"
    shell = (
        f"find /workspace{prune} -type f -printf '%s\\n' 2>/dev/null | "
        "{ s=0; while read -r n; do s=$((s + n)); done; echo $s; }"
    )
    try:
        code, out_b, _err, _timed = _run_capped(
            ["docker", "exec", container_name(key), "sh", "-c", shell],
            timeout=120, cap=4096,
        )
    except Exception:
        return 0
    if code != 0:
        return 0
    try:
        return int(out_b.decode(errors="replace").strip() or 0)
    except ValueError:
        return 0


def purge_newest_files(key: str, target_bytes: int,
                       exclude_paths: list[str] | None = None
                       ) -> tuple[int, int]:
    """Löscht die NEUESTEN Dateien im /workspace-Volume (hostseitig),
    bis die Summe der restlichen Dateien ≤ target_bytes ist.

    Heuristik: die Eigen-Dateien des Students sind „von Anfang an“ da
    (alte mtime); das Volumen füllt sich typischerweise mit neuen
    Artefakten (Logs, Checkpoints) → die werden zuerst weggenommen.
    `exclude_paths` (🔒-Top-Level-Mounts) werden nicht angefasst. Liefert
    (Anzahl gelöschter Dateien, freigegebene Bytes); (0, 0), wenn nichts
    zu tun ist oder die Messung nicht geht (Fremd-Data-Root ohne Mount).
    """
    mp = _volume_mountpoint(key)
    if not (mp and os.path.isdir(mp)):
        return 0, 0
    excl = {str(p).strip("/") for p in (exclude_paths or [])
            if str(p).strip("/")}
    entries: list[tuple[float, int, str]] = []
    total = 0
    try:
        for root, dirs, files in os.walk(mp):
            rel = os.path.relpath(root, mp)
            top = None if rel == "." else rel.split(os.sep)[0]
            if top in excl:
                dirs[:] = []
                continue
            for name in files:
                p = os.path.join(root, name)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, p))
                total += st.st_size
        if total <= target_bytes:
            return 0, 0
        entries.sort(key=lambda e: e[0], reverse=True)  # neueste zuerst
        freed = 0
        count = 0
        for _mtime, size, p in entries:
            if total - freed <= target_bytes:
                break
            try:
                os.remove(p)
                freed += size
                count += 1
            except OSError:
                continue
        return count, freed
    except Exception:
        return 0, 0


_MEM_UNIT_MULT = {
    "B": 1, "KB": 1024, "KiB": 1024, "MB": 1024**2, "MiB": 1024**2,
    "GB": 1024**3, "GiB": 1024**3, "TB": 1024**4, "TiB": 1024**4,
}


def workspace_mem_usage(key: str) -> int | None:
    """Aktueller RAM-Verbrauch des Containers in Bytes (docker stats).

    Liest die erste Komponente von MemUsage („1.2GiB / 4GiB"); None,
    wenn der Container nicht läuft oder die Messung fehlschlägt
    (best effort — reine UI-Info).
    """
    if container_state(key) != "running":
        return None
    try:
        proc = _docker("stats", "--no-stream",
                       "--format", "{{.MemUsage}}",
                       container_name(key),
                       check=False, timeout=30)
        if proc.returncode != 0:
            return None
        line = proc.stdout.decode(errors="replace").strip()
        m = re.match(r"([\d.]+)\s*([KMGTPE]?i?B)\b", line)
        if not m:
            return None
        return int(float(m.group(1)) * _MEM_UNIT_MULT.get(m.group(2), 1))
    except Exception:
        return None


def all_workspace_mem_usage() -> dict[str, int]:
    """RAM-Verbrauch aller LAUFENDEN Workspaces in EINEM
    docker-stats-Call (statt je ein Call pro Container, der 1–3 s
    kostet). Liefert {key: bytes}; fehlgeschlagene Einträge fehlen
    einfach (best effort — reine UI-Info)."""
    try:
        proc = _docker("stats", "--no-stream",
                       "--format", "{{.Name}} {{.MemUsage}}",
                       check=False, timeout=30)
        if proc.returncode != 0:
            return {}
        out: dict[str, int] = {}
        for line in proc.stdout.decode(errors="replace").splitlines():
            parts = line.split(" ", 1)
            if len(parts) != 2:
                continue
            name, mem = parts
            if not name.startswith("aicampus-ws-"):
                continue
            m = re.match(r"([\d.]+)\s*([KMGTPE]?i?B)\b", mem)
            if m:
                out[name[len("aicampus-"):]] = int(
                    float(m.group(1)) * _MEM_UNIT_MULT.get(m.group(2), 1))
        return out
    except Exception:
        return {}


# ── Assets (geteilte public-Dateien je Aufgabe) ───────────────────

# Reservierte Verzeichnisse im Asset-Dir (werden NIE gelistet, gesynced
# oder gepurgt): .private/ = 👤-Init-Artefakte + .init_hidden.sh (private
# Agenten-Region, nur Grading), .seeds/ = ✏️-Init-Artefakte (frische
# Student-Volumes werden davon gepopuliert), .init_alias/ = Marker-Dateien
# für Init-Builds ohne Image-Änderung (ersetzen die alten Alias-Tags auf
# dem Basis-Image, s. task_image_alias). .init_tmp_* = Build-Temp.
RESERVED_ASSET_DIRS = (".private", ".seeds", ".init_alias", ".init_backup")
INIT_TMP_PREFIX = ".init_tmp_"
INIT_MANIFEST_NAME = ".init_manifest.json"
# Sync-Sidecar: Task-Dateien des letzten (Vollspiegel-)Syncs — der
# Build-Cleansweep nutzt sie, um Task-Dateien von Init-Artefakten zu
# unterscheiden. Dot-File → fällt durch Snapshots/Purge durch.
TASK_FILES_NAME = ".task_files.json"
# Backup der alten Init-Artefakte (vom Cleansweep verlagert; Rollback
# stellt sie her, Erfolg wird verworfen).
INIT_BACKUP_NAME = ".init_backup"


def _is_reserved_asset_part(part: str) -> bool:
    return part in RESERVED_ASSET_DIRS or part.startswith(INIT_TMP_PREFIX)


def _iter_asset_files(base: Path):
    """Dateien des Asset-Dirs (ohne reservierte Verzeichnisse, ohne
    Dot-Files und Build-Teildateien)."""
    if not base.is_dir():
        return
    for root, dirs, files in os.walk(base):
        rel_root = Path(root).relative_to(base)
        if rel_root.parts and _is_reserved_asset_part(rel_root.parts[0]):
            continue
        dirs[:] = [d for d in dirs if not _is_reserved_asset_part(d)]
        for fn in files:
            if fn.startswith(".") or fn.endswith((".part", ".progress")):
                continue
            yield Path(root) / fn


def _check_under(base: Path, target: Path) -> None:
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError:
        raise DockerError("Asset-Pfad entweicht dem Asset-Ordner", 400)


def _file_matches(path: Path, data: bytes) -> bool:
    try:
        with open(path, "rb") as fh:
            off = 0
            while off < len(data):
                if fh.read(1 << 20) != data[off:off + (1 << 20)]:
                    return False
                off += 1 << 20
            return fh.read(1) == b""
    except OSError:
        return False


def write_asset_file(course: int, task: int, relpath: str, data: bytes) -> int:
    if len(data) > config.MAX_ASSET_FILE:
        raise DockerError("Asset zu groß (max. 5 GB)", 413)
    base = asset_dir(course, task)
    target = base / safe_asset_path(relpath)
    _check_under(base, target)
    # Unveränderte Inhalte NICHT neu schreiben: os.replace würde die
    # Inode tauschen — ein 🔒-Datei-Bind-Mount in einem laufenden
    # Container klebt auf der alten Inode (Stale-View), und der
    # Mount-Hash (Datei-Mtime) würde bei JEDM Sync ein Container-
    # Recreate auslösen.
    try:
        st = target.stat()
        if st.st_size == len(data) and _file_matches(target, data):
            if target.name.endswith(".sh") and not st.st_mode & 0o111:
                os.chmod(target, 0o755)
            return len(data)
    except OSError:
        pass
    os.makedirs(target.parent, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(data)
    # Skripte ausführbar — ro-Bind-Mounts zeigen die Bits live, der
    # Student kann ./run.sh & Co. im Terminal direkt starten.
    if target.name.endswith(".sh"):
        os.chmod(tmp, 0o755)
    os.replace(tmp, target)
    return len(data)


def ensure_asset_dir(course: int, task: int, relpath: str) -> None:
    """Expliziten Ordner im Asset-Dir anlegen (persistierte 🔒/👤-Ordner,
    die auch leer bleiben dürfen)."""
    base = asset_dir(course, task)
    target = base / safe_asset_path(relpath)
    _check_under(base, target)
    if target.exists() and not target.is_dir():
        raise DockerError("Asset-Pfad ist eine Datei", 400)
    target.mkdir(parents=True, exist_ok=True)


def list_assets(course: int, task: int) -> list[dict]:
    base = asset_dir(course, task)
    out = []
    for p in sorted(_iter_asset_files(base)):
        out.append({"path": str(p.relative_to(base)), "size": p.stat().st_size})
    return out


def remove_asset_tree(course: int, task: int) -> None:
    base = asset_dir(course, task)
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)


def _live_mount_sources(course: int, task: int) -> set[str]:
    """Host-Quellpfade, die gerade als Bind-Mounts in laufenden
    Workspace-Containern der Aufgabe hängen.

    rmdir/Ersetzung einer solchen Quelle desynct den Mount auf der alten
    Inode (Stale View im Container, ggf. Kernel-Hang) — Purge/Prune
    dürfen aktive Quellen daher nicht räumen."""
    prefix = f"aicampus-ws-{course}-{task}-"
    out: set[str] = set()
    proc = _docker("ps", "--filter", f"label=aicampus.course={course}",
                   "--format", "{{.Names}}", check=False)
    if proc.returncode != 0:
        return out
    for name in proc.stdout.decode().splitlines():
        name = name.strip()
        if not name.startswith(prefix):
            continue
        p2 = _docker("inspect", "-f", "{{range .Mounts}}{{.Source}}\n{{end}}",
                     name, check=False, timeout=30)
        if p2.returncode == 0:
            out.update(l.strip() for l in p2.stdout.decode().splitlines()
                       if l.strip())
    return out


def purge_missing_assets(course: int, task: int, keep_paths: list[str],
                         keep_dirs: list[str] | None = None) -> list[str]:
    """Remote-Dateien löschen, die nicht mehr in keep_paths sind.

    Init-generierte SHARED-Dateien (aus dem Manifest .init_manifest.json)
    werden NIE gelöscht — die gehören zum Init-Build und werden nur per
    Rebuild verwaltet. keep_dirs: explizite Ordner (auch leer) bleiben.
    """
    base = asset_dir(course, task)
    if not base.is_dir():
        return []
    man = _read_init_manifest(base)
    keep = set(keep_paths) | man["shared"]
    keep = {k for k in keep if not k.endswith("/")}
    keep_dirs = {d.strip().strip("/") for d in (keep_dirs or []) if d.strip()}
    # Init-Ordner (Manifest) bleiben auch leer — sie sind nur per
    # neuem Init-Build zu ändern.
    keep_dirs |= man["shared_dirs"]
    removed: list[str] = []
    for p in sorted(_iter_asset_files(base)):
        rel = str(p.relative_to(base))
        if rel not in keep:
            p.unlink(missing_ok=True)
            removed.append(rel)
    # Leergefegte Ordner aufräumen (von unten nach oben) — explizite
    # Ordner (keep_dirs) bleiben, auch wenn sie leer sind.
    dirs = [d for d in base.rglob("*") if d.is_dir()
            and not _is_reserved_asset_part(d.relative_to(base).parts[0])]
    live = _live_mount_sources(course, task)
    for d in sorted(dirs, key=lambda x: len(str(x)), reverse=True):
        if str(d.relative_to(base)) in keep_dirs:
            continue
        if d.resolve() in live:
            continue  # aktiver 🔒-Mount → rmdir würde den Mount stale machen
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    return removed


# ── Task-Images (2-Phasen-Init-Build, 1× je (Task, init-Hash)) ─────
#
# Phase 1 (.init.sh, 👤-Skript, public-Bereiche; Quelle: .private/, kommt
# per Init-Request): Build-Container aus dem Basis-Image mit
# /workspace = Host-Temp-Dir (rw, ✏️-Bereich) + je 🔒-Pfad rw-Bind (shared).
# 👤-Pfade sind NICHT gemountet. Danach Commit → Zwischen-Image (oder
# direkt Final-Image, wenn keine Phase 2).
# Phase 2 (.init_hidden.sh, 👤): Container aus dem Phase-1-Commit + je
# 👤-Pfad rw-Bind (private Region asset_dir/.private/). Commit → Final.
# Das Task-Image ist reine Umgebung (Base + Pakete), enthält keine
# Task-Dateien. Init-Artefakte: 🔒-Schreiber landen im shared Asset-Dir,
# ✏️-Schreiber im Seed-Store (.seeds/), 👤-Schreiber in .private/ —
# trackt .init_manifest.json {shared, seed, private} (Re-Init-Cleanup
# + Purge-Schutz).
#
# Storage-Optimierung: Init-Skripte, die das Image NICHT verändern
# (z. B. nur Datei-Downloads in /workspace), erzeugen KEIN Commit —
# die Ref wird per docker tag als Alias des Basis-Images gesetzt
# (Phasen-1-Image, wenn es ein Commit gibt). docker diff entscheidet
# (Bind-Mount-Writes landen nicht in der Image-Schicht); die Ref
# existiert danach immer → Status-/Workspace-Logik läuft unverändert.

INIT_BUILDS: dict[str, dict] = {}   # "{course}:{task}:{hash}" → Build-Status
INIT_BUILDS_LOCK = threading.Lock()
# Task-Locks: Builds derselben Aufgabe teilen sich den Build-Container-Namen
# (aicampus-init-{c}-{t}) — parallele Builds derselben Aufgabe würden sich
# gegenseitig per rm -f/create bekämpfen (Race bei schnellen Re-Klicks).
INIT_TASK_LOCKS: dict[tuple[int, int], threading.Lock] = {}
INIT_TASK_LOCKS_LOCK = threading.Lock()


def _init_task_lock(course: int, task: int) -> threading.Lock:
    with INIT_TASK_LOCKS_LOCK:
        l = INIT_TASK_LOCKS.get((course, task))
        if l is None:
            l = threading.Lock()
            INIT_TASK_LOCKS[(course, task)] = l
        return l


@contextmanager
def asset_sync_lock(course: int, task: int):
    """Task-Lock für Asset-Sync-Schreibzugriffe (assets_sync-Endpoint).

    Dieselbe Sperre wie der Init-Build (tlock): der Sync wartet, bis ein
    laufender Build das Before-/After-Snapshot-Fenster verlassen hat.
    Ohne Sperre könnte eine sync-te User-Datei zwischen die Snapshots
    landen und fälschlich als Init-Artefakt (shared) ins Manifest
    geraten — oder vom Build-Rollback als „neue Datei" gelöscht werden.
    """
    l = _init_task_lock(course, task)
    l.acquire()
    try:
        yield
    finally:
        l.release()


def task_image_ref(course: int, task: int, init_hash: str) -> str:
    return f"aicampus/task/{course}-{task}:{init_hash}"


def task_image_alias(course: int, task: int, init_hash: str) -> str | None:
    """Basis-Image-Ref, falls der Init-Build KEIN eigenes Image erzeugt
    hat (Marker-Datei im Asset-Dir; ersetzt das frühere Alias-Tag, das
    in docker images als voll große Zeile erschien); sonst None."""
    p = asset_dir(course, task) / ".init_alias" / init_hash
    try:
        if p.is_file():
            ref = p.read_text(encoding="utf-8").strip()
            return ref or None
    except OSError:
        pass
    return None


def resolve_task_image(course: int, task: int, init_hash: str) -> str | None:
    """Lauffähiges Image für (Task, Init-Hash): Commit > Alias-Marker.

    None = weder Commit-Image noch gültiger Marker vorhanden.
    """
    ref = task_image_ref(course, task, init_hash)
    if image_exists(ref):
        return ref
    aliased = task_image_alias(course, task, init_hash)
    if aliased and image_exists(aliased):
        return aliased
    return None


def _init_id(course: int, task: int, init_hash: str) -> str:
    return f"{course}:{task}:{init_hash}"


def init_build_running(course: int, task: int) -> bool:
    """True, wenn für (course, task) gerade ein Init-Build läuft
    (unabhängig vom Hash). Der Sync-Purge MUSS dann pausieren: das
    Manifest (Purge-Schutz für Init-Artefakte) wird erst am ENDE des
    Builds geschrieben — ein Purge dazwischen löscht laufende
    Downloads und fertige Artefakte aus dem shared Asset-Dir."""
    prefix = f"{course}:{task}:"
    with INIT_BUILDS_LOCK:
        return any(k.startswith(prefix) and b.get("status") == "building"
                   for k, b in INIT_BUILDS.items())


def _init_container_name(course: int, task: int) -> str:
    return f"aicampus-init-{course}-{task}"


def _init_tmp_dir(course: int, task: int, init_hash: str) -> Path:
    """Build-Temp-Dir (✏️-Bereich; unter dem Asset-Root, damit der
    Docker-Daemon sie sieht) — wird nach Erfolg/Abbruch entfernt."""
    return asset_dir(course, task) / f"{INIT_TMP_PREFIX}{init_hash}"


def task_has_init(course: int, task: int) -> bool:
    """Task hat Init-Build nötig (👤-Skript-Quellen in .private/)."""
    priv_dir = asset_dir(course, task) / ".private"
    return ((priv_dir / ".init.sh").is_file()
            or (priv_dir / ".init_hidden.sh").is_file())


def _read_init_manifest(base: Path) -> dict:
    """Init-Manifest (Pfad-Sets; leer bei Fehler).

    shared/seed/private = relative DATEI-Pfade je Store (Asset-Dir /
    .seeds/ / .private/); shared_dirs/seed_dirs/private_dirs =
    relative ORDNER-Pfade (Vor-/Nach-Diff des Builds: exakt die
    Ordner, die .init.sh angelegt hat — inkl. leerer). Schützt
    Init-Artefakte vor dem Sync-Purge, read-only im Tutor-Baum."""
    m = base / INIT_MANIFEST_NAME
    try:
        data = json.loads(m.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            out = {}
            for k in ("shared", "seed", "private"):
                out[k] = {str(x) for x in (data.get(k) or [])
                          if isinstance(x, str)}
                out[k + "_dirs"] = {str(x) for x in (data.get(k + "_dirs") or [])
                                    if isinstance(x, str)}
            return out
    except Exception:
        pass
    return {"shared": set(), "seed": set(), "private": set(),
            "shared_dirs": set(), "seed_dirs": set(),
            "private_dirs": set()}


def _asset_file_snapshot(base: Path) -> set[str]:
    """Aktuelle (nicht reservierte) Asset-Dateien (relativ zum Asset-Dir)."""
    return {str(p.relative_to(base)) for p in _iter_asset_files(base)}


def _asset_file_snapshot_meta(base: Path) -> dict[str, tuple[int, int]]:
    """Asset-Dateien → (size, mtime_ns), relativ zum Asset-Dir."""
    out: dict[str, tuple[int, int]] = {}
    for p in _iter_asset_files(base):
        try:
            st = p.stat()
        except OSError:
            continue
        out[str(p.relative_to(base))] = (st.st_size, st.st_mtime_ns)
    return out


def _asset_dir_snapshot(base: Path) -> set[str]:
    """Asset-Ordner (ohne reservierte + Dot-Ordner), relativ zum
    Asset-Dir — spiegelt die Datei-Semantik von _iter_asset_files."""
    out: set[str] = set()
    if not base.is_dir():
        return out
    for root, dirs, _files in os.walk(base):
        rel_root = Path(root).relative_to(base)
        if rel_root.parts and _is_reserved_asset_part(rel_root.parts[0]):
            continue
        dirs[:] = [d for d in dirs
                   if not _is_reserved_asset_part(d)
                   and not d.startswith(".")]
        for d in dirs:
            out.add(str(rel_root / d))
    return out


def _dir_snapshot(d: Path) -> set[str]:
    """Alle Ordner unter d (relativ zu d, inkl. Dot-Ordner); leer,
    wenn d fehlt — für die .seeds/.private-Regionen (spiegelt die
    Datei-Semantik von _dir_file_snapshot)."""
    out: set[str] = set()
    if not d.is_dir():
        return out
    for p in d.rglob("*"):
        if p.is_dir():
            out.add(str(p.relative_to(d)))
    return out


def read_task_files(base: Path) -> set[str]:
    """Task-Dateien des letzten Asset-Syncs (Sidecar .task_files.json).

    Der Sync schickt immer den Vollspiegel (delete_missing=True) → die
    Liste ist exakt die aktuell gesyncten (studentensichtbaren) Dateien.
    """
    m = base / TASK_FILES_NAME
    try:
        data = json.loads(m.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(x) for x in data if isinstance(x, str)}
    except Exception:
        pass
    return set()


def write_task_files(base: Path, paths: list[str]) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / TASK_FILES_NAME).write_text(
        json.dumps(sorted({str(p) for p in paths if str(p).strip()}),
                   indent=1, ensure_ascii=False),
        encoding="utf-8")


def _prepare_init_cleansweep(base: Path) -> tuple[dict, Path | None]:
    """VOR dem Build: alte Init-Artefakte löschen (mit Backup).

    Manifest-Semantik: exakt das Ergebnis DIESES Builds — Artefakte
    früherer Builds, die nicht mehr erzeugt werden, müssen verschwinden.
    Das ist nur bestimmbar, wenn sie vor dem .init.sh-Lauf weg sind
    (sonst sind „nicht mehr erzeugt“ und „deterministisch neu erzeugt“
    am Dateisystem nicht unterscheidbar). Task-Dateien (Sync-Sidecar)
    bleiben unberührt. Läuft unter der Task-Lock (kein anderer Writer).

    Liefert (altes Manifest, Backup-Verzeichnis oder None).
    """
    old = _read_init_manifest(base)
    stores = {"shared": base, "seed": base / ".seeds",
              "private": base / ".private"}
    task_files = read_task_files(base)
    backup = base / INIT_BACKUP_NAME
    moved = 0
    for scope in ("shared", "seed", "private"):
        store = stores[scope]
        for rel in sorted(old.get(scope) or ()):
            target = store / rel
            if scope == "shared" and rel in task_files:
                continue  # seltene Pfad-Kollision: Task-Datei gewinnt
            if not target.is_file():
                continue
            dst = backup / scope / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(dst))
            moved += 1
    # Init-Ordner: leer → weg (der Build erzeugt sie ggf. neu),
    # nicht leer (Task-/Fremd-Dateien überlebt den Datei-Cleansweep)
    # → unangetastet lassen, die Daten dürfen nicht mit-rasirt werden.
    # Kinder zuerst, damit Eltern nach dem Räumen ihrer Kinder leer
    # sind und mit wegfallen.
    for scope in ("shared", "seed", "private"):
        store = stores[scope]
        for rel in sorted(old.get(f"{scope}_dirs") or (),
                          key=len, reverse=True):
            target = store / rel
            if not target.is_dir():
                continue
            try:
                if not any(target.iterdir()):
                    shutil.rmtree(target, ignore_errors=True)
            except OSError:
                pass
    if moved == 0:
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        return old, None
    return old, backup


def _restore_init_backup(base: Path, backup: Path | None,
                         old_manifest: dict | None) -> None:
    """Rollback: alte Artefakte (Backup) + altes Manifest wiederherstellen."""
    if backup and backup.is_dir():
        stores = {"shared": base, "seed": base / ".seeds",
                  "private": base / ".private"}
        for scope, store in stores.items():
            src_root = backup / scope
            if not src_root.is_dir():
                continue
            for p in sorted(src_root.rglob("*")):
                if not p.is_file():
                    continue
                dst = store / p.relative_to(src_root)
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.exists():
                    shutil.move(str(p), str(dst))
        shutil.rmtree(backup, ignore_errors=True)
    if old_manifest is not None:
        # Alte Init-Ordner wieder anlegen (die Datei-Restellung oben
        # bringt nur Dateien zurück) → exakter Vor-Build-Zustand.
        stores = {"shared": base, "seed": base / ".seeds",
                  "private": base / ".private"}
        for scope, store in stores.items():
            for rel in sorted(old_manifest.get(f"{scope}_dirs") or ()):
                try:
                    (store / rel).mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
        (base / INIT_MANIFEST_NAME).write_text(
            json.dumps({k: sorted(v) for k, v in old_manifest.items()},
                       indent=1, ensure_ascii=False),
            encoding="utf-8")


def _dir_file_snapshot(d: Path) -> set[str]:
    """Alle Dateien unter d (relativ zu d); leer, wenn d fehlt."""
    if not d.is_dir():
        return set()
    return {str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()}


def _prune_empty_dirs(base: Path, live: set[str] | None = None) -> None:
    """Leere (nicht reservierte) Ordner von unten nach oben räumen.
    `live`: aktive Bind-Mount-Quellen (s. _live_mount_sources) bleiben.
    """
    dirs = [d for d in base.rglob("*") if d.is_dir()
            and not _is_reserved_asset_part(d.relative_to(base).parts[0])]
    for d in sorted(dirs, key=lambda x: len(str(x)), reverse=True):
        if live is not None and d.resolve() in live:
            continue
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


def _cleanup_init_writes(base: Path, before_shared: set[str],
                         before_private: set[str], tmp: Path,
                         backup: Path | None = None,
                         old_manifest: dict | None = None) -> None:
    """Init-Schreiben eines fehlgeschlagenen/abgebrochenen Builds
    zurückrollen (neu angelegte Dateien in shared + private Region,
    Temp-Dir, geleerte Ordner) + Vor-Build-Zustand wiederherstellen
    (Cleansweep-Backup der alten Artefakte, altes Manifest)."""
    priv_dir = base / ".private"
    for p in sorted(_iter_asset_files(base),
                    key=lambda x: len(str(x)), reverse=True):
        if str(p.relative_to(base)) not in before_shared:
            p.unlink(missing_ok=True)
    if priv_dir.is_dir():
        for p in sorted(priv_dir.rglob("*"), key=lambda x: len(str(x)),
                        reverse=True):
            if p.is_file() and str(p.relative_to(priv_dir)) not in before_private:
                p.unlink(missing_ok=True)
    if tmp.is_dir():
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        live = _live_mount_sources(int(base.parent.name), int(base.name))
    except (ValueError, OSError):
        live = set()
    _prune_empty_dirs(base, live)
    _restore_init_backup(base, backup, old_manifest)


def _set_init_status(id_key: str, status: str,
                     error: str | None = None, log_tail: str | None = None) -> None:
    with INIT_BUILDS_LOCK:
        b = INIT_BUILDS.get(id_key)
        if b is not None:
            b["status"] = status
            if error is not None:
                b["error"] = error
            if log_tail is not None:
                b["log_tail"] = log_tail


# Ephemere Pfade, die init-Skripte typischerweise beschmutzen, die aber
# KEINE echte Image-Änderung sind (Paket-Manager-Caches, Logs, von
# Docker verwaltete Bind-Dateien) — ohne Filter würde z. B. ein reines
# "apt-get update" oder "pip download" einen fast leeren Commit auslösen.
_EPHEMERAL_IMAGE_PATHS = (
    "/root/.cache", "/root/.pip", "/root/.npm",
    "/var/cache", "/var/log", "/var/tmp", "/var/lib/apt",
    "/root/.bash_history",
    "/etc/resolv.conf", "/etc/hostname", "/etc/hosts",
)


def _compact_cr_log(log: str) -> str:
    """tqdm-\r-Progress-Updates komprimieren (nur der Endstand je Zeile).

    Ohne Kompression fressen die \r-Updates die letzten 4 KB des
    Log-Tails und verdecken die eigentliche Skript-Ausgabe."""
    out = []
    for line in log.split("\n"):
        parts = [p for p in line.split("\r") if p]
        out.append(parts[-1] if parts else "")
    return "\n".join(out)


def _is_pycache_path(p: str) -> bool:
    """Python-Import-Artefakt: .pyc-Datei oder __pycache__-Verzeichnis
    (auch neu ANGELEGTE, d. h. ohne Trailing Slash im Pfad)."""
    return p.endswith(".pyc") or "__pycache__" in p.split("/")


def _image_changes(container: str) -> list[str] | None:
    """Echte Image-Änderungen per docker diff (leer = Image unverändert).

    Bind-Mount-Writes (der gesamte /workspace-Bereich) landen NICHT in
    der Image-Schicht — der Filter ist eine Absicherung. Ephemere Pfade
    (Caches, Logs, s. _EPHEMERAL_IMAGE_PATHS) werden ignoriert.
    Python-Import-Artefakte (__pycache__/.pyc) sind KEINE echte Änderung:
    ein reiner Datei-Download per `python -c` würde sonst einen
    mehr-GB-Commit des kompletten Basis-Images auslösen. docker diff
    markiert zusätzlich die komplette Vorfahren-Kette geänderter Pfade
    als „C“ — ein „C“-Pfad, der nur __pycache__-Rauschen enthält,
    ist damit ebenfalls Rauschen.
    None = diff fehlgeschlagen (→ defensiv als "geändert" behandeln,
    d. h. Commit nicht überspringen)."""
    proc = _docker("diff", container, check=False, timeout=60)
    if proc.returncode != 0:
        return None
    # Zeilenformat: "A /pfad" (angelegt), "C /pfad" (geändert),
    # "D /pfad" (gelöscht)
    entries: list[tuple[str, str]] = []
    for line in proc.stdout.decode().splitlines():
        if not line or line[0] not in "ACD":
            continue
        p = line[1:].strip()
        if p:
            entries.append((line[0], p))
    pycache = {p for _, p in entries if _is_pycache_path(p)}
    out: list[str] = []
    for flag, p in entries:
        if not p or p == "/workspace" or p.startswith("/workspace/"):
            continue
        if any(p == e or p.startswith(e + "/") for e in _EPHEMERAL_IMAGE_PATHS):
            continue
        if _is_pycache_path(p):
            continue
        if flag == "C" and any(pc.startswith(p + "/") for pc in pycache):
            continue
        out.append(p)
    return out


def _do_init_build(id_key: str, course: int, task: int, init_hash: str,
                   image: str, deadline: str | None,
                   readonly_paths: list[str], hidden_paths: list[str],
                   folders: list[str]) -> None:
    """2-Phasen-Init-Build + Aufräumen der alten Task-Image-Varianten."""
    _do_init_build_core(id_key, course, task, init_hash, image, deadline,
                        readonly_paths, hidden_paths, folders)
    # Aufräumen NACH dem Build (der Build-Container ist dann in finally
    # entfernt): jeder Hash-Wechsel (Skript-Edit, Access-Layout-Änderung)
    # erzeugt sonst ein neues Task-Image bzw. einen Alias-Marker, der
    # nie wieder referenziert wird (Stapel).
    ref = task_image_ref(course, task, init_hash)
    if image_exists(ref) or task_image_alias(course, task, init_hash):
        try:
            remove_task_images(course, task, keep=ref)
        except Exception:  # noqa: BLE001 — Cleanup darf den Build nicht brechen
            pass


def _do_init_build_core(id_key: str, course: int, task: int, init_hash: str,
                        image: str, deadline: str | None,
                        readonly_paths: list[str], hidden_paths: list[str],
                        folders: list[str]) -> None:
    """2-Phasen-Init-Build (s. Sektions-Header) + Artefakt-Sammlung.

    Gemeinsames Zeitbudget (config.INIT_TIMEOUT) für beide Phasen;
    gemeinsames Live-Log (Trennlinie vor Phase 2). Die 👤-Skript-Quellen
    (.init.sh, .init_hidden.sh) sind in start_init_build VOR dem Start
    in .private/ persistiert (Before-Snapshots enthalten sie → Rollback
    löscht sie nicht).
    """
    ref = task_image_ref(course, task, init_hash)
    p1_ref = ref + "-p1"
    name = _init_container_name(course, task)
    base = asset_dir(course, task)
    priv_dir = base / ".private"
    tmp = _init_tmp_dir(course, task, init_hash)

    # Task-Lock VOR dem Before-Snapshot: der Asset-Sync hält dieselbe
    # Sperre während seiner Schreibphase (asset_sync_lock) — so kann
    # keine sync-te User-Datei zwischen Before- und After-Snapshot
    # landen und fälschlich als Init-Artefakt (shared) ins Manifest
    # geraten (Rollback-Fall: als „neue Datei" gelöscht werden).
    tlock = _init_task_lock(course, task)
    tlock.acquire()
    # Cleansweep: alte Init-Artefakte weg (mit Backup) → Manifest =
    # exakt das Ergebnis DIESES Builds (s. _prepare_init_cleansweep).
    # Läuft unter der Task-Lock → kein Sync-Race beim Verschieben.
    old_manifest, backup = _prepare_init_cleansweep(base)
    before_meta = _asset_file_snapshot_meta(base)
    before_shared = set(before_meta)
    before_private = _dir_file_snapshot(priv_dir)
    with INIT_BUILDS_LOCK:
        b = INIT_BUILDS.get(id_key)
        if b is not None:
            b["_before_shared"] = before_shared
            b["_before_private"] = before_private
            b["_tmp"] = str(tmp)
            b["_p1_created"] = False
            b["_backup"] = str(backup) if backup else None
            b["_old_manifest"] = old_manifest
    _docker("rm", "-f", name, check=False, timeout=60)
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    # Ordner-Struktur der Aufgabe in den Host-Quellen vorlegen: die
    # ✏️-Build-Umgebung ist sonst leer (frische Temp-Dir), und 🔒/👤-
    # Subordner existieren nur, falls ein Sync sie schon geschrieben hat.
    # .init.sh kann so auch in bestehende Ordner schreiben. Routing je
    # Region; Vorfahren eines 👤-Tops bleiben NICHT in tmp (Phase 1 darf
    # 👤-Nachbarschaft nicht sehen — Docker legt den Ordner in Phase 2
    # selbst als Mount-Eltern an).
    for d in sorted(folders):
        if any(d == hp or d.startswith(hp + "/") for hp in hidden_paths):
            target = priv_dir / d
        elif any(hp.startswith(d + "/") for hp in hidden_paths):
            continue
        elif any(d == rp or d.startswith(rp + "/") for rp in readonly_paths):
            target = base / d
        else:
            target = tmp / d
        target.mkdir(parents=True, exist_ok=True)

    # Mount-Quellen für alle 🔒-Top-Level-Pfade sicherstellen: fehlt die
    # Host-Quelle, würde der rw-Mount stillschweigend weggelassen und
    # .init.sh in die Temp-Dir schreiben — die Artefakte gingen dann beim
    # Build-Cleanup verloren (kein Mount, kein Manifest-Eintrag). Eine
    # Datei kann an einer fehlenden Stelle nicht liegen (noch nicht
    # gesynct) → Ordner anlegen ist die einzig sinnvolle Default.
    for rp in readonly_paths:
        src = base / rp
        if not src.exists():
            src.mkdir(parents=True, exist_ok=True)

    # Ordner-Before-Snapshots NACH dem Vorlegen der Ordner-Struktur
    # (Tutor-Layout) + der Mount-Quellen: nur Ordner, die .init.sh
    # SELBST anlegt, landen als [init]-Ordner im Manifest (Diff zum
    # After-Snapshot) — das Tutor-Layout zählt nicht dazu.
    before_dirs = _asset_dir_snapshot(base)
    before_tmp_dirs = _dir_snapshot(tmp)
    before_priv_dirs = _dir_snapshot(priv_dir)

    # Gemeinsames Zeitbudget für beide Phasen
    deadline_ts = time.time() + config.INIT_TIMEOUT
    phase_logs: list[str] = []
    live_tail = {"cur": ""}
    last_live = 0.0

    def _live() -> None:
        # Live-Log für die UI — Throttle: max. 1 Update/s (kombinierter Tail)
        nonlocal last_live
        now = time.time()
        if now - last_live < 1.0:
            return
        last_live = now
        tail = ("\n".join(phase_logs + [live_tail["cur"]]))[-8000:]
        _set_init_status(id_key, "building", log_tail=tail)

    def _on_progress(tail: bytes) -> None:
        live_tail["cur"] = tail.decode(errors="replace")
        _live()

    def _make_container(img: str, private_phase: bool) -> None:
        args = [
            "create",
            "--name", name,
            "--label", "aicampus.init-build=1",
            "--label", f"aicampus.task={course}/{task}",
            "--label", f"aicampus.task.hash={init_hash}",
        ]
        if deadline:
            args += ["--label", f"aicampus.task.deadline={deadline}"]
        args += ["--network", "bridge", "--pids-limit", "1024",
                 "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m,mode=1777"]
        # ✏️-Bereich = Host-Temp-Dir (rw); 🔒-Bereiche = shared Asset-Dir
        # (rw, damit .init.sh schreiben darf). 👤-Pfade NUR in Phase 2
        # (rw aus der privaten Region) — Mount-Enforcement.
        # /tmp als tmpfs (wie Student-Container): .init.sh-Scratch landet
        # nicht im Image-Commit (Diff-Skip-Logik, s. Sektions-Header).
        args += ["-v", f"{_daemon_path(tmp.resolve())}:/workspace:rw"]
        for rp in readonly_paths:
            src = base / rp
            if src.is_file() or src.is_dir():
                args += ["-v", f"{_daemon_path(src.resolve())}:/workspace/{rp}:rw"]
        if private_phase:
            for hp in hidden_paths:
                src = priv_dir / hp
                src.parent.mkdir(parents=True, exist_ok=True)
                args += ["-v", f"{_daemon_path(src.resolve())}:/workspace/{hp}:rw"]
        args += ["-w", "/workspace", "--entrypoint", "/bin/sh",
                 img, "-c", "sleep infinity"]
        _docker(*args, timeout=120)
        _docker("start", name, timeout=60)

    def _commit(target_ref: str) -> None:
        # docker commit unterstützt KEINE Labels-Flags — die Cleanup-Labels
        # (aicampus.task, .hash, .deadline) kommen als Create-Labels des
        # Build-Containers und werden vom Commit vererbt.
        _docker("stop", name, timeout=60)
        _docker("commit", "--change", 'CMD ["sleep", "infinity"]',
                name, target_ref, timeout=600)

    def _run_script(script_name: str) -> str:
        left = int(deadline_ts - time.time())
        if left <= 0:
            raise DockerError(
                f"Init-Timeout ({config.INIT_TIMEOUT} s gesamt) — das "
                "Zeitbudget ist aufgebraucht", 504)
        # stdbuf: bash/echo puffern stdout blockweise auf Pipes (docker-
        # Exec-Socket) — ohne Line-Buffering würde die init-Ausgabe erst
        # beim Exit erscheinen und der Live-Log in der UI leer bleiben.
        _init_cmd = (
            f"if command -v stdbuf >/dev/null 2>&1; then "
            f"exec stdbuf -oL -eL bash /workspace/{script_name}; "
            f"else exec bash /workspace/{script_name}; fi"
        )
        code, out_b, err_b, timed_out = _run_capped(
            ["docker", "exec", "-i", "-w", "/workspace", name, "bash", "-c",
             _init_cmd],
            timeout=left, cap=2_000_000,
            on_progress=_on_progress,
        )
        log = (out_b + b"\n" + err_b).decode(errors="replace")
        if timed_out:
            raise DockerError(
                f"Init-Timeout ({config.INIT_TIMEOUT} s gesamt) — "
                "Init-Skript läuft zu lang, der Build wurde abgebrochen", 504)
        if code != 0:
            raise DockerError(
                f"{script_name} beendet mit Exit-Code {code}: {log[-500:]}", 500)
        return log

    try:
        init_p = priv_dir / ".init.sh"
        priv_p = priv_dir / ".init_hidden.sh"
        has_p1 = init_p.is_file()
        has_p2 = priv_p.is_file()
        if not has_p1 and not has_p2:
            # Skripte wurden während des Builds gelöscht (Sync-Race) →
            # sauber abbrechen; kein "failed", da kein Fehler aufgetreten ist.
            _set_init_status(
                id_key, "none",
                log_tail=".init.sh/.init_hidden.sh nicht mehr vorhanden — "
                         "Build abgebrochen.")
            # Vor-Build-Zustand wiederherstellen (Cleansweep wurde schon
            # durchgeführt) → keine Artefakte verloren.
            _restore_init_backup(base, backup, old_manifest)
            return

        # ── Phase 1: .init.sh (public: darf in ✏️+🔒, NICHT in 👤) ──
        p1_ref_image: str | None = None   # Phase-1-Commit (nur wenn nötig)
        if has_p1:
            _make_container(image, private_phase=False)
            shutil.copy2(init_p, tmp / ".init.sh")
            phase_logs.append(_compact_cr_log(_run_script(".init.sh")))
            # Commit NUR bei echten Image-Änderungen (Pakete installieren
            # etc.) — reine Datei-Downloads in /workspace (Bind-Mounts)
            # lassen das Image unverändert (s. _image_changes).
            changes = _image_changes(name)
            if changes is None or changes:
                # Commit: Phase-2-Zwischen-Image ODER direkt Final-Image
                _commit(p1_ref if has_p2 else ref)
                if has_p2:
                    p1_ref_image = p1_ref
                    with INIT_BUILDS_LOCK:
                        b = INIT_BUILDS.get(id_key)
                        if b is not None:
                            b["_p1_created"] = True
            else:
                phase_logs.append("(Keine Image-Änderungen — Commit übersprungen.)")
            _docker("rm", "-f", name, check=False, timeout=60)

        # ── Phase 2: .init_hidden.sh (👤, aus dem Phase-1-Commit) ──
        if has_p2:
            live_tail["cur"] = "\n── .init_hidden.sh ──\n"
            phase_logs.append("── .init_hidden.sh ──")
            _make_container(p1_ref_image or image, private_phase=True)
            shutil.copy2(priv_p, tmp / ".init_hidden.sh")
            phase_logs.append(_compact_cr_log(_run_script(".init_hidden.sh")))
            changes = _image_changes(name)
            if changes is None or changes:
                _commit(ref)
            elif p1_ref_image:
                # Phase 2 hat das Image nicht verändert → Phasen-1-Image
                # als Final-Image übernehmen (Tag statt neuem Commit).
                _docker("tag", p1_ref, ref, timeout=60)
                phase_logs.append(
                    "(Keine Image-Änderungen — Phasen-1-Image wird verwendet.)")

        # ── Ref sichern: Commit fehlt (Image unverändert) → Alias-Marker ──
        if not image_exists(ref):
            # Keine Phase hat das Image geändert (z. B. nur Datei-Downloads
            # in /workspace) → Task-Image = Basis-Image. Ein Marker im
            # Asset-Dir dokumentiert das (kein Alias-Tag mehr: das würde
            # in docker images als voll große Zeile erscheinen); Container
            # laufen direkt auf dem Basis-Image.
            alias_dir = base / ".init_alias"
            alias_dir.mkdir(parents=True, exist_ok=True)
            (alias_dir / init_hash).write_text(image, encoding="utf-8")
            phase_logs.append(
                "(Keine Image-Änderungen — Task-Image ist das "
                "Basis-Image.)")

        # ── Artefakte sammeln + Manifest schreiben ──
        # Manifest = exakt das Ergebnis DIESES Builds: alte Artefakte
        # wurden vorab gecleanswept (s. _prepare_init_cleansweep) →
        # alles, was jetzt neu/verändert ist, stammt aus diesem Lauf,
        # und was nicht mehr erzeugt wurde, ist weg.
        after_meta = _asset_file_snapshot_meta(base)
        after_private = _dir_file_snapshot(priv_dir)
        after_dirs = _asset_dir_snapshot(base)
        after_priv_dirs = _dir_snapshot(priv_dir)
        mount_paths = list(readonly_paths) + list(hidden_paths)
        # ✏️-Schreiber = Temp-Dir-Inhalt (minus Skripten; Mount-Overlays
        # existieren dort physisch nicht — Filter als Absicherung).
        seed: set[str] = set()
        seed_dirs: set[str] = set()
        for p in tmp.rglob("*"):
            rel = str(p.relative_to(tmp))
            if any(rel == m or rel.startswith(m + "/") for m in mount_paths):
                continue
            if p.is_file():
                if rel in (".init.sh", ".init_hidden.sh"):
                    continue
                seed.add(rel)
            elif p.is_dir():
                seed_dirs.add(rel)
        seeds_dir = base / ".seeds"
        seeds_dir.mkdir(parents=True, exist_ok=True)
        for rel in sorted(seed):
            src = tmp / rel
            dst = seeds_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        # Leere Seed-Ordner ebenfalls anlegen (exist_ok: Ordner mit
        # Dateien sind oben mit-entstanden) → .seeds spiegelt die
        # Init-Struktur exakt, auch für leere Verzeichnisse.
        for rel in sorted(seed_dirs):
            (seeds_dir / rel).mkdir(parents=True, exist_ok=True)
        # .seeds = exakt das Ergebnis dieses Builds: ältere Seeds früherer
        # Builds löschen (sie würden sonst weiterhin in frische Student-
        # Volumes wandern, ohne im Baum sichtbar zu sein).
        for rel in sorted(_dir_file_snapshot(seeds_dir) - seed):
            (seeds_dir / rel).unlink(missing_ok=True)
        # private = Schreib-Diff: alte Artefakte wurden gecleanswept, die
        # Skript-Quellen .init.sh/.init_hidden.sh sind im Before-Snapshot
        # (persistiert VOR dem Build) → Diff = exakt die neuen Artefakte.
        private = sorted(after_private - before_private)
        # shared = neu/verändert seit dem Before-Snapshot (nach dem
        # Cleansweep): unveränderte Task-Dateien des Syncs fallen raus,
        # überschriebene Task-Dateien (Pfad-Kollision) gewinnt init →
        # read-only.
        shared = sorted(p for p, m in after_meta.items()
                        if before_meta.get(p) != m)
        # [init]-Ordner = Vor-/Nach-Diff je Store: exakt die Ordner,
        # die .init.sh angelegt hat (inkl. leerer). Access erbt den
        # Ziel-Ordner (Store): shared→🔒, seed→✏️ (Root), private→👤.
        shared_dirs = sorted(after_dirs - before_dirs)
        seed_dirs = sorted(seed_dirs - before_tmp_dirs)
        private_dirs = sorted(after_priv_dirs - before_priv_dirs)
        manifest = {
            "shared": shared,
            "seed": sorted(seed),
            "private": private,
            "shared_dirs": shared_dirs,
            "seed_dirs": seed_dirs,
            "private_dirs": private_dirs,
        }
        (base / INIT_MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=1, ensure_ascii=False),
            encoding="utf-8")
        # Build erfolgreich → Cleansweep-Backup verwerfen.
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
        _set_init_status(id_key, "ready",
                         log_tail=("\n".join(phase_logs))[-4000:])
    except Exception as e:  # noqa: BLE001 — Build-Thread darf nie sterben
        # Rollback: neue Dateien in shared + private Region, Temp-Dir,
        # Phase-1-Zwischen-Image, Cleansweep-Backup + altes Manifest.
        _cleanup_init_writes(base, before_shared, before_private, tmp,
                             backup=backup, old_manifest=old_manifest)
        with INIT_BUILDS_LOCK:
            b = INIT_BUILDS.get(id_key)
            if b is not None and b.get("_p1_created"):
                _docker("rmi", "-f", p1_ref, check=False, timeout=60)
        _set_init_status(id_key, "failed", error=str(e)[:1000])
    finally:
        _docker("rm", "-f", name, check=False, timeout=60)
        shutil.rmtree(tmp, ignore_errors=True)
        tlock.release()


def _persist_init_scripts(course: int, task: int,
                          init_b64: str | None,
                          init_private_b64: str | None) -> None:
    """👤-Skript-Quellen (.init.sh, .init_hidden.sh) in der privaten Region
    persistieren — sie sind hidden (nicht Teil des Asset-Syncs) und kommen
    NUR per Init-Request vom Backend (b64).

    LÄUFT VOR dem task_has_init-Check in start_init_build (die Quellen
    müssen existieren, bevor der Check sie sieht). None → alte Datei
    entfernen (Task hat das Skript nicht mehr). Legacy-Namen werden
    mitgeräumt, damit alte Orphans task_has_init nicht täuschen.
    """
    base = asset_dir(course, task)
    priv_dir = base / ".private"
    try:
        priv_dir.mkdir(parents=True, exist_ok=True)
        for name, b64 in ((".init.sh", init_b64),
                          (".init_hidden.sh", init_private_b64)):
            if b64 is not None:
                (priv_dir / name).write_bytes(base64.b64decode(str(b64)))
            else:
                (priv_dir / name).unlink(missing_ok=True)
        (base / "init.sh").unlink(missing_ok=True)
        (priv_dir / "init_private.sh").unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        raise DockerError(f"Init-Skripte konnten nicht persistiert werden: {e}")


def start_init_build(course: int, task: int, init_hash: str, image: str,
                     deadline: str | None = None,
                     readonly_paths: list[str] | None = None,
                     hidden_paths: list[str] | None = None,
                     init_b64: str | None = None,
                     init_private_b64: str | None = None,
                     folders: list[str] | None = None,
                     force: bool = False) -> dict:
    """Task-Image-Build starten (idempotent: vorhanden → ready).

    force=True überspringt die Existenzprüfung (manuelles „Sync & Init“):
    das Skript läuft komplett neu und die alten Init-Artefakte werden
    gelöscht + neu erzeugt (Cleansweep im Build-Kern).
    """
    _persist_init_scripts(course, task, init_b64, init_private_b64)
    if not task_has_init(course, task):
        return {"status": "none"}
    ref = task_image_ref(course, task, init_hash)
    if not force and (
            image_exists(ref) or task_image_alias(course, task, init_hash)):
        return {"status": "ready", "ref": ref}
    id_key = _init_id(course, task, init_hash)
    with INIT_BUILDS_LOCK:
        b = INIT_BUILDS.get(id_key)
        if b and b["status"] == "building":
            return {"status": "building", "ref": ref,
                    "log_tail": (b.get("log_tail") or "")[-2000:]}
        INIT_BUILDS[id_key] = {"status": "building", "start": time.time(),
                               "error": None, "log_tail": ""}
    threading.Thread(
        target=_do_init_build,
        args=(id_key, course, task, init_hash, image, deadline,
              list(readonly_paths or []), list(hidden_paths or []),
              list(folders or [])),
        daemon=True).start()
    return {"status": "building", "ref": ref}


def init_build_status(course: int, task: int, init_hash: str) -> dict:
    """Status: none (kein init-Skript) | ready | building | failed | idle."""
    if not task_has_init(course, task):
        return {"status": "none"}
    ref = task_image_ref(course, task, init_hash)
    id_key = _init_id(course, task, init_hash)
    if image_exists(ref) or task_image_alias(course, task, init_hash):
        # Finaler Log-Tail (sofern noch in Memory) — die UI zeigt ihn
        # auch nach dem Build weiter an.
        with INIT_BUILDS_LOCK:
            tail = (INIT_BUILDS.get(id_key) or {}).get("log_tail") or ""
        out = {"status": "ready", "ref": ref}
        if tail:
            out["log_tail"] = tail[-2000:]
        return out
    with INIT_BUILDS_LOCK:
        b = dict(INIT_BUILDS.get(id_key) or {})
    if b.get("status") == "building":
        return {"status": "building", "ref": ref,
                "log_tail": (b.get("log_tail") or "")[-2000:]}
    if b.get("status") == "failed":
        return {"status": "failed", "ref": ref,
                "error": b.get("error") or "Unbekannter Fehler"}
    return {"status": "idle", "ref": ref}


# Max. Dateigröße für den Einlesen aus der Agenten-Region (5 MB, base64-
# codierte Antwort ≈ 6,7 MB) — entspricht der Editor-Begrenzung.
_ARTIFACT_FILE_MAX_BYTES = 5 * 1024 * 1024


def init_artifacts(course: int, task: int, scope: str = "all") -> dict:
    """Init-Artefakte je Scope (shared|seed|private|all) — aus dem
    Manifest, Größen von der Disk.

    Pfade sind Workspace-relativ (der Ziel-Pfad im Tutor-/Grading-Baum):
    shared → asset_dir/<path>, seed → .seeds/<path>, private → .private/<path>.
    Liefert {files: [...], dirs: [...]} — dirs = Ordner, die .init.sh
    im letzten Build angelegt hat (Manifest-Vor-/Nach-Diff, inkl. leerer).
    """
    if scope not in ("shared", "seed", "private", "all"):
        raise DockerError("scope muss shared|seed|private|all sein", 400)
    base = asset_dir(course, task)
    manifest = _read_init_manifest(base)
    scopes = ("shared", "seed", "private") if scope == "all" else (scope,)
    stores = {"shared": base, "seed": base / ".seeds",
              "private": base / ".private"}
    files = []
    dirs = []
    for sc in scopes:
        for rel in sorted(manifest.get(sc, set())):
            if any(x in ("", ".", "..") for x in rel.split("/")):
                continue
            p = stores[sc] / rel
            if not p.is_file():
                continue
            try:
                with open(p, "rb") as fh:
                    head = fh.read(1024)
            except OSError:
                continue
            files.append({
                "path": rel,
                "size": p.stat().st_size,
                "is_binary": b"\x00" in head,
                "scope": sc,
            })
        for rel in sorted(manifest.get(f"{sc}_dirs", set())):
            if any(x in ("", ".", "..") for x in rel.split("/")):
                continue
            p = stores[sc] / rel
            if not p.is_dir():
                continue
            dirs.append({"path": rel, "scope": sc})
    return {"files": files, "dirs": dirs}


def init_artifact_file(course: int, task: int, scope: str,
                       path: str) -> tuple[int, bytes]:
    """Inhalt eines Init-Artefakts (binary-safe, 5-MB-Cap)."""
    if scope not in ("shared", "seed", "private"):
        raise DockerError("scope muss shared|seed|private sein", 400)
    p_rel = str(path).replace("\\", "/").lstrip("/")
    if not p_rel or any(x in ("", "..") for x in p_rel.split("/")):
        raise DockerError("Ungültiger Dateipfad", 400)
    base = asset_dir(course, task)
    if p_rel not in _read_init_manifest(base).get(scope, set()):
        raise DockerError("Datei nicht als Init-Artefakt gelistet", 404)
    target = ({"shared": base, "seed": base / ".seeds",
               "private": base / ".private"}[scope]) / p_rel
    _check_under(base, target)
    if not target.is_file():
        raise DockerError("Init-Artefakt nicht auf Disk gefunden", 404)
    size = target.stat().st_size
    if size > _ARTIFACT_FILE_MAX_BYTES:
        raise DockerError("Datei zu groß (max. 5 MB)", 413)
    return size, target.read_bytes()


def remove_init_artifact(course: int, task: int, relpath: str) -> dict:
    """Manifest-Eintrag (shared/seed/private) + Datei entfernen.

    Heilung für getarnte Task-Dateien: ein Upload, der während eines
    laufenden Init-Builds sync-te wurde, ist fälschlich im Manifest
    gelandet (After-Snapshot hat die sync-te Datei erfasst). Der nächste
    Init-Build schreibt das Manifest neu; Dateien, die .init.sh wirklich
    erzeugt, entstehen beim Rebuild erneut (und werden neu gelistet).
    """
    p_rel = str(relpath or "").replace("\\", "/").lstrip("/")
    if not p_rel or any(x in ("", "..") for x in p_rel.split("/")):
        raise DockerError("Ungültiger Dateipfad", 400)
    base = asset_dir(course, task)
    m = base / INIT_MANIFEST_NAME
    try:
        data = json.loads(m.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    stores = {"shared": base, "seed": base / ".seeds",
              "private": base / ".private"}
    removed = []
    for scope, store in stores.items():
        entries = data.get(scope) or []
        if p_rel in entries:
            data[scope] = [x for x in entries if x != p_rel]
            target = store / p_rel
            _check_under(base, target)
            target.unlink(missing_ok=True)
            removed.append(scope)
    if not removed:
        raise DockerError("Pfad ist nicht als Init-Artefakt gelistet", 404)
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text(json.dumps(data, indent=1, ensure_ascii=False),
                 encoding="utf-8")
    return {"ok": True, "removed": removed}


def seed_volume(key: str, course: int, task: int) -> int:
    """Frisches Student-Volume mit den ✏️-Init-Artefakt-Seeds (.seeds/) befüllen.

    Wird bei workspace_create VOR den Starter-Dateien aufgerufen — bei
    Kollision gewinnen die Starter-Dateien (die nach den Seeds kopiert
    werden). Liefert die Anzahl der kopierten Dateien.
    """
    base = asset_dir(course, task)
    manifest = _read_init_manifest(base)
    seed_paths = manifest.get("seed") or set()
    if not seed_paths:
        return 0
    with tempfile.TemporaryDirectory(prefix="aicampus-seeds-") as tmp:
        n = 0
        for rel in sorted(seed_paths):
            if any(x in ("", "..") for x in rel.split("/")):
                continue
            src = base / ".seeds" / rel
            if not src.is_file():
                continue
            target = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(src, target)
            n += 1
        if n == 0:
            return 0
        # Alles in einem Rutsch: docker cp (Quelle-Endung / → Inhalt kopieren)
        _docker("cp", tmp + "/.", f"{container_name(key)}:/workspace", timeout=300)
    return n


def stop_init_build(course: int, task: int, init_hash: str) -> dict:
    """Aktiven Init-Build abbrechen (docker rm -f) + Init-Schreiben
    zurückrollen (shared/private-Diffs, Temp-Dir, Phase-1-Image)."""
    id_key = _init_id(course, task, init_hash)
    with INIT_BUILDS_LOCK:
        b = INIT_BUILDS.get(id_key)
        building = bool(b and b["status"] == "building")
        before_shared = b.get("_before_shared") if b else None
        before_private = b.get("_before_private") if b else None
        tmp_s = b.get("_tmp") if b else None
        backup_s = b.get("_backup") if b else None
        old_manifest = b.get("_old_manifest") if b else None
        p1_created = bool(b and b.get("_p1_created"))
    if not building:
        return {"ok": False, "message": "Kein Init-Build aktiv"}
    base = asset_dir(course, task)
    _docker("rm", "-f", _init_container_name(course, task), check=False, timeout=60)
    if p1_created:
        _docker("rmi", "-f", task_image_ref(course, task, init_hash) + "-p1",
                check=False, timeout=60)
    if isinstance(before_shared, set) and isinstance(before_private, set):
        tmp = Path(tmp_s) if tmp_s else _init_tmp_dir(course, task, init_hash)
        _cleanup_init_writes(
            base, before_shared, before_private, tmp,
            backup=Path(backup_s) if backup_s else None,
            old_manifest=old_manifest)
    _set_init_status(id_key, "failed", error="Build manuell abgebrochen")
    return {"ok": True}


def remove_task_images(course: int, task: int,
                       keep: str | None = None) -> list[str]:
    """Alle Task-Images der Aufgabe löschen (Label-Filter + Namensraum,
    force) sowie alte Alias-Marker (Image-Commit-Skip-Builds).
    `keep` (aktuelle Task-Image-Ref) bleibt erhalten — ebenso der Marker
    des dazugehörigen Hashes."""
    proc = _docker("images", "--filter", f"label=aicampus.task={course}/{task}",
                   "--format", "{{.Repository}}:{{.Tag}}", check=False, timeout=30)
    refs = [l.strip() for l in proc.stdout.decode().splitlines() if l.strip()]
    # Legacy: Alias-Tags auf dem Basis-Image (vor der Marker-Umstellung)
    # vererben KEINE Task-Labels — zusätzlich per Namensraum fangen (nur
    # der Tag wird entfernt, das Basis-Image bleibt über eigene Tags).
    proc = _docker("images", "--filter",
                   f"reference=aicampus/task/{course}-{task}:*",
                   "--format", "{{.Repository}}:{{.Tag}}", check=False,
                   timeout=30)
    for l in proc.stdout.decode().splitlines():
        l = l.strip()
        if l and l not in refs:
            refs.append(l)
    removed = []
    for ref in refs:
        if keep and ref == keep:
            continue
        p = _docker("rmi", "-f", ref, check=False, timeout=120)
        err = p.stderr.decode(errors="replace")
        if p.returncode == 0 or "No such image" in err:
            removed.append(ref)
    # Alias-Marker (reserviert, daher nicht vom Sync-Purge berührt) mit
    # demselben Lebenszyklus räumen.
    keep_hash = keep.rsplit(":", 1)[-1] if keep else None
    alias_dir = asset_dir(course, task) / ".init_alias"
    try:
        if alias_dir.is_dir():
            for p in alias_dir.iterdir():
                if p.is_file() and p.name != keep_hash:
                    p.unlink(missing_ok=True)
    except OSError:
        pass
    return removed


def cull_deadline_images() -> int:
    """Task-Images mit abgelaufener Deadline räumen (lazy, Reaper-Loop).

    Nur Images ohne (laufende oder gestoppte) nutzung; der Rest wird im
    nächsten Durchlauf erneut geprüft. Fehler werden still geschluckt.
    """
    proc = _docker("images", "--filter", "label=aicampus.task.deadline",
                   "--format", "{{.Repository}}:{{.Tag}}", check=False, timeout=30)
    refs = [l.strip() for l in proc.stdout.decode().splitlines() if l.strip()]
    if not refs:
        return 0
    proc = _docker("image", "inspect",
                   "--format", '{{index .Config.Labels "aicampus.task.deadline"}}',
                   *refs, check=False, timeout=30)
    removed = 0
    now = time.time()
    for ref, line in zip(refs, proc.stdout.decode().splitlines()):
        deadline = line.strip()
        if not deadline or deadline == "<no value>":
            continue
        try:
            dl = datetime.fromisoformat(deadline)
            if dl.tzinfo is None:
                dl = dl.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dl.timestamp() > now:
            continue
        usage = _docker("ps", "-a", "--filter", f"ancestor={ref}",
                        "--format", "{{.ID}}", check=False, timeout=30)
        if usage.stdout.decode().strip():
            continue  # noch in Nutzung → nächster Durchlauf
        if _docker("rmi", ref, check=False, timeout=60).returncode == 0:
            removed += 1
    return removed


def prune_after_spec_build() -> None:
    """Komplettes Aufräumen nach erfolgreichem Spec-Build.

    Beide Build-Pfade hinterlassen Reste: BuildKit → Build-Cache, Legacy
    Builder → ungetaggte Zwischen-Images. Das neue Image ist getaggt, und
    noch genutzte Artefakte (Container, parallele Builds) werden von
    Docker automatisch geschützt.
    """
    try:
        _docker("builder", "prune", "-f", check=False, timeout=300)
        _docker("image", "prune", "-f", check=False, timeout=300)
    except Exception:  # noqa: BLE001
        pass


def prune_build_artifacts() -> None:
    """Periodischer (Reaper-)Cleanup, bewusst moderat.

    - Build-Cache: nur Einträge älter als 24 h (frische Dev-/Compose-
      Rebuilds bleiben schnell).
    - Dangling Images: nur älter als 1 h (frische, kurzzeitig
      ungetaggte Task-Commits bleiben unangetastet).
    """
    try:
        _docker("builder", "prune", "-f", "--filter", "until=24h",
                check=False, timeout=300)
        _docker("image", "prune", "-f", "--filter", "until=1h",
                check=False, timeout=300)
    except Exception:  # noqa: BLE001
        pass
