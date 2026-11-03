"""
Zentrale Zeit-Helper: UTC-Serialisierung + Campus-Zone für Deadlines.

Konvention:
- In der DB liegen naive UTC-Zeitstempel (Container läuft mit TZ=UTC).
- Zum Client wird immer ISO-8601 mit Zonen-Marker (…Z) geschickt, damit
  der Browser in die lokale Zeit des Nutzers umrechnet (new Date(iso) in
  den Seiten-Skripten bzw. [data-ts]-Elemente, s. base.html).
- Deadlines sind semantisch anders: Sie werden lokal eingegeben
  (PROF-Browser / MCP-Agent) und liegen oft ohne Zonen-Marker in der DB.
  Naive Deadline-Strings gelten in der Campus-Zone (config.CAMPUS_TIMEZONE);
  beim Speichern werden sie in einen UTC-Moment (…Z) überführt.
"""

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import config


def utc_iso(dt: Optional[datetime]) -> Optional[str]:
    """datetime → ISO-8601 in UTC mit Z.

    naive = UTC (unser DB-Format), aware = wird nach UTC konvertiert.
    None → None.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_deadline(value: Optional[str]) -> Optional[datetime]:
    """Deadline-String → aware UTC-Datetime.

    Ohne Zonen-Marker gilt die Campus-Zone (so wurden Deadlines
    historisch eingegeben: lokale Wall-Clock). Leerer Wert → None.
    Wirft ValueError bei unparsebaren Strings.
    """
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(config.CAMPUS_TIMEZONE))
    return dt.astimezone(timezone.utc)


def deadline_iso(value: Optional[str]) -> Optional[str]:
    """Deadline-String normalisieren (ISO-UTC mit Z) für die Ausgabe.

    Unparsebare/leere Werte → None bzw. unverändert (defensiv).
    """
    if not value:
        return None
    try:
        return utc_iso(parse_deadline(value))
    except (ValueError, KeyError):
        return value


def deadline_expired(value: Optional[str]) -> bool:
    """True, wenn die Deadline existiert und bereits verstrichen ist (UTC).

    Unparsebare Werte → False (defensiv).
    """
    if not value:
        return False
    try:
        dl = parse_deadline(value)
    except (ValueError, KeyError):
        return False
    return datetime.now(timezone.utc) > dl


def deadline_from_local(value: str, tz_name: Optional[str] = None) -> str:
    """Lokaler Wall-Clock-Wert → ISO-UTC mit Z.

    tz_name: IANA-Zone des Anlegers (Web-UI liefert die Browser-Zone);
    fehlt = Campus-Zone (MCP-Pfad). Wirft ValueError (ungültiges Datum)
    bzw. KeyError (unbekannte Zone).
    """
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name or config.CAMPUS_TIMEZONE))
    return utc_iso(dt.astimezone(timezone.utc))


def campus_date(dt: Optional[datetime], fmt: str = "%d.%m.%y") -> str:
    """Datum in der Campus-Zone formatiert (naive = UTC)."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(config.CAMPUS_TIMEZONE)).strftime(fmt)
