"""
AICampus: Zentrale Konfiguration / Settings

Alle Konfigurationswerte werden von Umgebungsvariablen oder .env-Datei gelesen.
Ermöglicht flexibles Deployen ohne Code-Änderungen.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# .env-Datei laden (wenn vorhanden)
load_dotenv()

# ─── Projekt-Root ───────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()
DB_FILE = BASE_DIR / "data" / "aicampus.db"
DB_FILE.parent.mkdir(parents=True, exist_ok=True)

# Kurs-Medien (Bilder/Applets): data/media/course_{id}/<uuid>.<ext>
# Versand über authentifizierte Route GET /media/{course_id}/{filename} (kein Static-Mount!)
MEDIA_DIR = BASE_DIR / "data" / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# Profilbilder: data/avatars/<uuid>.<ext>
# Versand über authentifizierte Route GET /avatars/{filename} (kein Static-Mount!)
AVATAR_DIR = BASE_DIR / "data" / "avatars"
AVATAR_DIR.mkdir(parents=True, exist_ok=True)

# Workspace-Aufgaben: Dateibäume der Aufgaben (public + privat in .private/)
# data/workspaces/{task_id}/<pfad> bzw. data/workspaces/{task_id}/.private/<pfad>
WORKSPACE_DIR = BASE_DIR / "data" / "workspaces"
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

# Einreichungs-Snapshots: data/submissions/{submission_id}/workspace.tar.gz
# Upload-Aufgaben: eingereichte Datei (1 Datei) unter data/submissions/{id}/<name>
SUBMISSION_DIR = BASE_DIR / "data" / "submissions"
SUBMISSION_DIR.mkdir(parents=True, exist_ok=True)

# Sleep-Backups: Workspace-Volumes, die beim Idle-Kill von der Compute-Engine
# auf den Server gewandert sind (data/workspace_backups/ws-{c}-{t}-{s}.tar.gz
# + .json-Meta). Beim nächsten Workspace-Start werden sie in das frische
# Volume zurückgespielt (s. services/workspace_service.ensure_workspace).
WORKSPACE_BACKUP_DIR = BASE_DIR / "data" / "workspace_backups"
WORKSPACE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
# Retention: Backup wird X Tage nach letzter Aktivität gelöscht.
WORKSPACE_BACKUP_RETENTION_DAYS = int(
    os.getenv("WORKSPACE_BACKUP_RETENTION_DAYS", "30"))

# Upload-Aufgaben: Template- + Musterlösungs-Dateien (Datei wie hochgeladen,
# Name aus Task-Feldern) unter data/upload_tasks/{task_id}/<name>
UPLOAD_TASK_DIR = BASE_DIR / "data" / "upload_tasks"
UPLOAD_TASK_DIR.mkdir(parents=True, exist_ok=True)

# Größenlimit Upload-Aufgaben (je hochgeladener Datei: Template,
# Musterlösung, Student-Einreichung)
UPLOAD_MAX_FILE_BYTES = 200 * 1024 * 1024  # 200 MB

# Versions-Snapshots der Workspace-Dateibäume in Task-Versionen:
# data/version_snapshots/{version_id}/workspace.tar.gz (+ meta.json)
VERSION_SNAPSHOT_DIR = BASE_DIR / "data" / "version_snapshots"
VERSION_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

# ─── Server ──────────────────────────────────────────
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
DEBUG = os.getenv("DEBUG", "true").lower() in ("true", "1", "yes")
# Port des Agent-Preview-Servers (compute_agent, PREVIEW_AGENT_PORT dort;
# nur Agent-Netz, kein Host-Publish) — Ziel des Backend-Preview-Proxy.
PREVIEW_AGENT_PORT = int(os.getenv("PREVIEW_AGENT_PORT", "8701"))
# Basis-Domain der Preview-Subdomains (Workspace-Web-UIs): jede App
# bekommt https://<task>-<port>-<user>-<h6>.<PREVIEW_BASE_DOMAIN>/ —
# die Apps laufen auf / (kein Base-Pfad), Routing/Auth übernimmt die
# Middleware in services/preview_proxy.py. Voraussetzungen: Wildcard-
# DNS + TLS (Wildcard-Zertifikat) + Nginx-Server-Block OHNE
# X-Frame-Options. Leer = aus.
PREVIEW_BASE_DOMAIN = os.getenv("PREVIEW_BASE_DOMAIN", "").strip().lower()
# Öffentliche Basis-Domain der App für generierte URLs (z. B. MCP-URL in
# den User-Settings): Die UI zeigt URLs immer mit dieser Domain, nicht mit
# dem Host des Requests — sonst laufen veraltete Zugangs-Domains (z. B.
# cg2-04.…-bonn.de) in kopierte Configs hinein. Leer = aus Request ableiten (Dev).
PUBLIC_DOMAIN = os.getenv("PUBLIC_DOMAIN", "").strip().lower()

# ─── Datenbank ──────────────────────────────────────────────────
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{DB_FILE}"
)

# ─── Auth / JWT ─────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))  # 8h

# ─── LLM / OpenAI-kompatibler Endpoint ──────────────────────────
LLM_API_URL = os.getenv("LLM_API_URL", "http://localhost:8001/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "sk-default")
LLM_MODEL = os.getenv("LLM_MODEL", "Qwen3-32B")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "120"))

# Public Endpoint für nicht-sensitive Aufgaben
# (z. B. Generierung neuer Aufgaben oder Musterlösungen).
# Leer lassen = Private Endpoint (wie oben) wird verwendet.
LLM_API_URL_PUBLIC = os.getenv("LLM_API_URL_PUBLIC", "")
LLM_API_KEY_PUBLIC = os.getenv("LLM_API_KEY_PUBLIC", "")
LLM_MODEL_PUBLIC = os.getenv("LLM_MODEL_PUBLIC", "")

# ─── LDAP (optional) ───────────────────────────────────────────
LDAP_ENABLED = os.getenv("LDAP_ENABLED", "false").lower() in ("true", "1", "yes")
LDAP_SERVER = os.getenv("LDAP_SERVER", "ldap.uni.de")
LDAP_BASE_DN = os.getenv("LDAP_BASE_DN", "ou=people,dc=uni,dc=de")
LDAP_BIND_DN = os.getenv("LDAP_BIND_DN", "cn=admin,dc=uni,dc=de")
LDAP_BIND_PW = os.getenv("LDAP_BIND_PW", "")
LDAP_USER_SEARCH = os.getenv("LDAP_USER_SEARCH", "(uid={username})")

# ─── Compute / Workspaces ──────────────────────────────────────────
# Anbindung an Compute-Engines für Workspace-Aufgaben (per-Student-Docker-Container
# auf einem separaten oder lokalen Server, s. docs/installation.md).
# Workspace-Aufgaben sind immer verfügbar; ohne erreichbare Engine degradieren
# die Views sauber (kein Feature-Flag mehr).
COMPUTE_AGENT_URL = os.getenv("COMPUTE_AGENT_URL", "http://127.0.0.1:8700")
COMPUTE_AGENT_KEY = os.getenv("COMPUTE_AGENT_KEY", "")
# True nur, wenn ein lokaler Agent EXPLIZIT deklariert ist (Compose `environment`
# oder .env) — dann erscheint er in der Admin-Registry als "local"-Engine.
# Der nackte Hardcoded-Default oben gilt nicht als Deklaration.
COMPUTE_AGENT_URL_EXPLICIT = "COMPUTE_AGENT_URL" in os.environ

# ─── Web Push (M3: Browser-Push via FCM/VAPID) ─────────────────
# Einmalig generiertes ECDSA P-256 Paar (base64url). Der Private Key
# signiert die VAPID-JWT für den FCM-Web-Push-Endpunkt (verlässt den
# Server nie); der Public Key wird an die Browser ausgegeben
# (PushManager.subscribe). PUSH_APP_URL = kanonische Site-URL,
# dient als VAPID-"sub"-Claim. Push ist nur aktiv, wenn alle drei
# Werte gesetzt sind → sonst wird das Feature sauber ausgeblendet.
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
PUSH_APP_URL = os.getenv("PUSH_APP_URL", "").strip().rstrip("/")


def push_enabled() -> bool:
    """Web Push verfügbar? (Keys + Site-URL in .env gesetzt)"""
    return bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY and PUSH_APP_URL)


# ─── Zeit ──────────────────────────────────────────────────────
# Campus-Zone: interpretiert Deadlines ohne Zonen-Marker. Die Web-UI
# liefert die Browser-Zone des Anlegers mit (deadline_tz); der MCP-Pfad
# und alte DB-Einträge liefern naive Wall-Clock-Zeiten → Campus-Zone.
# Alle übrigen Zeitstempel laufen durchgehend in UTC (s. services/timeutil.py).
CAMPUS_TIMEZONE = os.getenv("CAMPUS_TIMEZONE", "Europe/Berlin")

# ─── Frontend ───────────────────────────────────────────────────
CODEMIRROR_THEME = os.getenv("CODEMIRROR_THEME", "dracula")
CODEMIRROR_VERSION = "5.65.16"

# ─── Excel-Export ────────────────────────────────────────────────
EXCEL_SHEET_NAME = os.getenv("EXCEL_SHEET_NAME", "Punktestand")

# ─── Kurs-Material-Import (Zip) ─────────────────────────────────
# Staging-Verzeichnis: data/imports/course_{id}/{job_id}/
# (upload.zip + extracted/ + sidecars/ = .md-Konvertierungen von docx/pptx/pdf)
IMPORT_DIR = BASE_DIR / "data" / "imports"
IMPORT_DIR.mkdir(parents=True, exist_ok=True)

IMPORT_MAX_ZIP_BYTES = int(os.getenv("IMPORT_MAX_ZIP_BYTES", str(500 * 1024 * 1024)))            # 500 MB
IMPORT_MAX_SINGLE_FILE_BYTES = int(os.getenv("IMPORT_MAX_SINGLE_FILE_BYTES", str(200 * 1024 * 1024)))  # 200 MB
IMPORT_MAX_ENTRIES = int(os.getenv("IMPORT_MAX_ENTRIES", "10000"))
IMPORT_MAX_EXTRACTED_BYTES = int(os.getenv("IMPORT_MAX_EXTRACTED_BYTES", str(1024 * 1024 * 1024)))  # 1 GB
IMPORT_MIN_FREE_DISK_FACTOR = float(os.getenv("IMPORT_MIN_FREE_DISK_FACTOR", "3.0"))  # freier Platz ≥ Faktor × Zip-Größe
IMPORT_LLM_CONCURRENCY = int(os.getenv("IMPORT_LLM_CONCURRENCY", "2"))     # parallele LLM-Calls pro Import (GPU-Schutz)
IMPORT_FILE_CONCURRENCY = int(os.getenv("IMPORT_FILE_CONCURRENCY", "4"))   # parallele Dateianalysen
IMPORT_CHUNK_CHARS = int(os.getenv("IMPORT_CHUNK_CHARS", "40000"))         # max. Zeichen pro Text-Chunk (Analyse)
CHAPTER_INPUT_WARN_CHARS = int(os.getenv("CHAPTER_INPUT_WARN_CHARS", "120000"))    # Warnschwelle für sehr große Kapitel-Quelltexte (Skript, kein Cap)
IMPORT_SLIDE_DECK_SOURCE_WARN_CHARS = int(os.getenv("IMPORT_SLIDE_DECK_SOURCE_WARN_CHARS", "120000"))  # Warnschwelle für sehr große Deck-Quelltexte (Slides, kein Cap)
IMPORT_READ_CHARS = int(os.getenv("IMPORT_READ_CHARS", "30000"))           # pro read_file-Call des Planners
IMPORT_READ_LINES = int(os.getenv("IMPORT_READ_LINES", "1500"))
IMPORT_PLAN_MAX_ITERATIONS = int(os.getenv("IMPORT_PLAN_MAX_ITERATIONS", "10"))
IMPORT_PLAN_TOOL_BUDGET = int(os.getenv("IMPORT_PLAN_TOOL_BUDGET", "150000"))  # Summe der Tool-Ergebnisse (Planner)
IMPORT_GATHER_MAX_ITERATIONS = int(os.getenv("IMPORT_GATHER_MAX_ITERATIONS", "8"))  # Agentic Quellen-Sammlung (Skript/Folien)
IMPORT_PNG_DPI = int(os.getenv("IMPORT_PNG_DPI", "300"))                   # PDF-Figur → PNG
IMPORT_PNG_MAX_DIM = int(os.getenv("IMPORT_PNG_MAX_DIM", "4096"))          # Dimensions-Cap (PNG-Dateigröße)
IMPORT_EMBED_MIN_PX = int(os.getenv("IMPORT_EMBED_MIN_PX", "150"))         # PDF-eingebettetes Bild: max. Kante < → Rauschen (M2)
IMPORT_MEDIA_TRIAGE_MAX_IMAGES = int(os.getenv("IMPORT_MEDIA_TRIAGE_MAX_IMAGES", "300"))  # M3b: max. LLM-Triage-Calls pro Import (Rest bleibt „unbekannt“)
IMPORT_SCAN_PNG_DPI = int(os.getenv("IMPORT_SCAN_PNG_DPI", "150"))             # M3: Seiten-PNG-DPI (Vision-Input)
IMPORT_PAGE_CONCURRENCY = int(os.getenv("IMPORT_PAGE_CONCURRENCY", "2"))       # M3: parallele Seiten-Transkriptionen
IMPORT_SLIDE_DECK_MAX_SLIDES_1TO1 = int(os.getenv("IMPORT_SLIDE_DECK_MAX_SLIDES_1TO1", "150"))  # Sicherheits-Cap Quell-Folien (1:1-Modus)
# Feature-Timeouts in Sekunden (lokale Modelle können langsam sein)
IMPORT_TIMEOUT_ANALYSIS = int(os.getenv("IMPORT_TIMEOUT_ANALYSIS", "180"))
IMPORT_TIMEOUT_MEDIA = int(os.getenv("IMPORT_TIMEOUT_MEDIA", "120"))
IMPORT_TIMEOUT_PLAN = int(os.getenv("IMPORT_TIMEOUT_PLAN", "900"))
IMPORT_TIMEOUT_CONVERT = int(os.getenv("IMPORT_TIMEOUT_CONVERT", "600"))