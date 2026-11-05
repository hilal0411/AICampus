r"""
Serverseitiges Erkennen nummerierter Figuren und ihrer {#fig:…}-Labels.

Single Source of Truth für api/script.py (Refmap, Abbildungsverzeichnis,
Previews), api/slides.py (S-Nummern der Folien) und services/content_edits.py
(LLM-Edit-Label-Warnungen): Die Erkennung muss exakt die Figuren treffen, die
auch der Client-Renderer (static/js/markdown-renderer.js, Schritte 1e/1e0)
nummeriert — sonst driftet die Refmap davon ab und der Client fällt auf die
pro-Rendern-S-Zählung zurück (jede Folie zeigt dann „Abb. S1“).

Regeln (Spiegelbild des Clients):
- Alt-Text: Beliebig, ABER genau EINE Ebene verschachtelter Klammern ist
  erlaubt ([x] im Alt geht, [[x]] nicht) — Parität zu marked/CommonMark
  und zu IMG_REF im Client.
- Einfache Abbildung: ![Alt](src) + Token-Tail. Tail = aufeinanderfolgende
  {…}-Blöcke, durch Leerraum oder genau einen Zeilenumbruch (keine Leerzeile)
  getrennt. Token-Typen (je max. einmal, in BELIEBIGER Reihenfolge):
  {#fig:label}, {#fragment}/{#fragment:id} (auch {#Fragment}), {#aaid:label},
  {.?height=X} (Suffix ∅ oder "px"), {.?zoom=X} (Zoom nur bei einfachen
  Abbildungen/Applets, NICHT bei Subfigure-Komplexen) und
  {.?autoplay=…}/{.?controls=…}/{.?loop=…}/{.?muted=…} (true/false, führender
  Punkt optional — nur wenn die Src ein internes mp4/webm-Video ist).
  Unbekanntes Token oder Duplikat → die Abbildung bleibt literal (wird nicht
  nummeriert, kein Label).
- Subfigure-Komplex: ![Gesamt]( inner inner ) + gleicher Tail (ohne zoom,
  ohne Video-Attribute).
  Je Inner erlaubt der Client nur {height=X} und/oder {#fig:label} und — bei
  Video-Innern — die Video-Playback-Attribute (je max. einmal, beliebige
  Reihenfolge). Gültig = Innere UND Tail valide; nur dann
  zählt der Komplex als EINE Abbildung (nummeriert bei äußerm {#fig:label}
  ODER mindestens einem gelabelten Inner), und die inneren ![…](…) zählen
  nicht als eigene Figuren. Ungültiger Komplex → bleibt literal: die inneren
  Abbildungen werden dann wie normale Bilder geparst (und zählen ggf. als
  solche).
- Code-Blöcke (fenced + inline) werden vorher entfernt — Labels darin zählen
  nicht. [\w-] ≈ JS [\p{L}0-9_-] (Unicode-Buchstaben, Ziffern, Unterstrich,
  Bindestrich).
"""

import re
from typing import Optional

_CODE_FENCED_RE = re.compile(r"```[\s\S]*?```")
_CODE_INLINE_RE = re.compile(r"`[^`]+`")

# Leerraum zwischen Referenz und Token: Leerzeichen/Tab oder genau EIN
# Zeilenumbruch (keine Leerzeile) — identisch zum JS-ATTR_BLOCK.
_GAP = r"[ \t]*(?:\r?\n[ \t]*)?"
_ATTR_BLOCK_RE = re.compile(_GAP + r"\{([^{}]*)\}")

# Token-Typen einer Abbildung (JS 1e), je max. einmal, beliebige Reihenfolge.
_TOK_FIG_RE = re.compile(r"#fig:([\w-]+)")
_TOK_FRAG_RE = re.compile(r"#([Ff])ragment(?::[\w-]+)?")
_TOK_AAID_RE = re.compile(r"#aaid:[\w-]+")
_TOK_HEIGHT_RE = re.compile(r"\.?height=([\d.]+)([a-z]*)")
_TOK_ZOOM_RE = re.compile(r"\.?zoom=([\d.]+)")
_TOK_VIDEO_RE = re.compile(r"\.?(autoplay|controls|loop|muted)=(true|false)", re.IGNORECASE)


def _is_video_src(src: str) -> bool:
    """Internes mp4/webm-Video (Parität: isVideoSrc in markdown-renderer.js)."""
    return not re.match(r"https?://", src, re.IGNORECASE) and bool(
        re.search(r"\.(mp4|webm)$", re.split(r"[?#]", src)[0], re.IGNORECASE)
    )

# Alt-Text: [^\[\]] oder genau EINE Ebene geklammerte Gruppen — Parität mit
# marked/CommonMark (Link-Text erlaubt eine Ebene verschachtelte Klammern)
# UND mit IMG_REF in markdown-renderer.js. Ohne die Klammern bricht
# ![…$(\phi = [\phi_0, \phi_1])$…] beim ersten ] ab → Figur nicht erkannt
# (unnummeriert, Caption fehlt, {#fig:label} bleibt literal).
_ALT = r"((?:[^\[\]]|\[[^\]]*\])*)"

# Einfache Abbildung (JS IMG_REF): Src = [^)\s]+ (bewusst auch `!` erlaubt —
# bei unmaskierten ungültigen Komplexen matcht damit der äußere Kopf auf das
# erste Inner; der Client hat denselben Fall (src.startsWith('!')-Guard),
# beide Seiten zählen exakt dieselben Labels.
_IMG_RE = re.compile(r"!\[" + _ALT + r"\]\(([^)\s]+)\)")

# Subfigure-Komplex (JS SUBFIG_RE): je Inner ![Alt](src) + bis zu sechs
# beliebige {…}-Blöcke (Src schließt `!` aus, damit der äußere Kopf
# ![Gesamt]( nicht auf das erste Inner matcht). Die Validierung der Innere
# (nur height/fig erlaubt) passiert in _subfig_inners() — wie im Client
# (SUBFIG_RE matcht lose, _parseSubfigInners validiert streng).
# Inner-Alt: eine Ebene Klammern wie _ALT, aber NICHT capturend (die
# Gruppen-Nummern von _SUBFIG_ANY_RE bleiben stabil: 1 = äußere Caption,
# 2 = Inner-Sequenz).
_INNER_SRC = r"!\[(?:[^\[\]]|\[[^\]]*\])*\]\([^)\s!]+\)(?:" + _GAP + r"\{[^{}]*\}){0,6}"
_SUBFIG_ANY_RE = re.compile(
    r"!\[" + _ALT + r"\]\([ \t]*(?:\r?\n[ \t]*)?("
    + _INNER_SRC
    + r"(?:[ \t\r\n]+"
    + _INNER_SRC
    + r")+"
    + r")[ \t]*(?:\r?\n[ \t]*)?\)"
)
_INNER_HEAD_RE = re.compile(r"!\[" + _ALT + r"\]\(([^)\s!]+)\)")
_INNER_WS_RE = re.compile(r"[ \t\r\n]+")


def strip_code(content: str) -> str:
    """Fenced + inline Code-Blöcke entfernen (Labels in Code zählen nicht)."""
    text = _CODE_FENCED_RE.sub("", content or "")
    return _CODE_INLINE_RE.sub("", text)


def _parse_attr_tail(
    text: str, pos: int, allow_zoom: bool, video: bool = False
) -> tuple[bool, Optional[str], int, int]:
    """Verbraucht aufeinanderfolgende {…}-Tokens hinter einer Medien-Referenz
    (pos = Position KURZ nach dem schließenden „)“ der Referenz).

    Liefert (valid, fig-Label oder None, Label-Pos, End-Pos). valid=False bei
    unbekanntem Token oder Duplikat — der Client lässt das Medium dann
    literal stehen (keine Nummer, kein Label).
    """
    label: Optional[str] = None
    label_pos = -1
    seen: set[str] = set()
    end = pos
    while (bm := _ATTR_BLOCK_RE.match(text, end)):
        tok = bm.group(1).strip()
        kind: Optional[str] = None
        if _TOK_FIG_RE.fullmatch(tok):
            kind = "fig"
        elif _TOK_FRAG_RE.fullmatch(tok):
            kind = "frag"
        elif _TOK_AAID_RE.fullmatch(tok):
            kind = "aaid"
        elif (hm := _TOK_HEIGHT_RE.fullmatch(tok)) and hm.group(2) in ("", "px"):
            kind = "height"
        elif allow_zoom and _TOK_ZOOM_RE.fullmatch(tok):
            kind = "zoom"
        elif video and (vm := _TOK_VIDEO_RE.fullmatch(tok)):
            kind = "video:" + vm.group(1).lower()
        if kind is None or kind in seen:
            return False, None, -1, end
        if kind == "fig":
            label = tok[len("#fig:") :]
            label_pos = bm.start(1) + len("#fig:")
        seen.add(kind)
        end = bm.end()
    return True, label, label_pos, end


def _subfig_inners(inner_text: str) -> Optional[list[tuple[int, Optional[str], str]]]:
    """Innere eines Subfigure-Komplexes auflösen (Parität: JS _parseSubfigInners).

    Liefert [(Label-Pos, fig-Label oder None, Caption)] relativ zu inner_text,
    oder None wenn der Komplex ungültig ist: Inner mit anderem Token als
    height/fig, Duplikat, Lücke mit Nicht-Leerraum oder <2 Innere. Der Client
    lässt ihn dann literal stehen (die Innere werden als normale Bilder geparst).
    """
    inners: list[tuple[int, Optional[str], str]] = []
    rest = inner_text
    off = 0  # bereits verbrauchtes Präfix (für absolute Label-Positionen)
    while True:
        hm = _INNER_HEAD_RE.match(rest)
        if not hm:
            return None
        caption = hm.group(1)
        rest = rest[hm.end() :]
        off += hm.end()
        label: Optional[str] = None
        label_pos = -1
        height_seen = False
        vid_seen: set[str] = set()
        video = _is_video_src(hm.group(2))
        for _ in range(6):
            am = _ATTR_BLOCK_RE.match(rest)
            if not am:
                break
            tok = am.group(1).strip()
            fm = _TOK_FIG_RE.fullmatch(tok)
            hm2 = _TOK_HEIGHT_RE.fullmatch(tok)
            if fm:
                if label is not None:
                    return None  # Duplikat-Label
                label = fm.group(1)
                label_pos = off + am.start(1) + len("#fig:")
            elif hm2:
                if hm2.group(2) not in ("", "px") or height_seen:
                    return None  # falscher Suffix bzw. Duplikat-Height
                height_seen = True
            elif video and (vm := _TOK_VIDEO_RE.fullmatch(tok)):
                name = vm.group(1).lower()
                if name in vid_seen:
                    return None  # Duplikat
                vid_seen.add(name)
            else:
                return None  # unbekanntes Token (z. B. {#fragment}, {zoom=…})
            rest = rest[am.end() :]
            off += am.end()
        inners.append((label_pos, label, caption))
        if not rest:
            break
        gm = _INNER_WS_RE.match(rest)
        if not gm:
            return None  # Lücke ist nicht reiner Leerraum
        rest = rest[gm.end() :]
        off += gm.end()
    return inners if len(inners) >= 2 else None


def _complex_info(
    text: str, m: "re.Match[str]"
) -> Optional[tuple[Optional[str], int, list[tuple[int, Optional[str], str]], int]]:
    """Gültiger Subfigure-Komplex (Parität: JS 1e0) →
    (äußeres Label oder None, Label-Pos,
    [(Inner-Label-Pos, Inner-Label, Inner-Caption)], Ende des gültigen
    äußeren Token-Tails) — alles absolute Positionen in text.

    None wenn Innere oder Tail ungültig sind — der Client lässt den Komplex
    dann literal stehen (die Innere werden als normale Bilder geparst).
    """
    inners = _subfig_inners(m.group(2))
    if inners is None:
        return None
    valid, label, label_pos, tail_end = _parse_attr_tail(text, m.end(), allow_zoom=False)
    if not valid:
        return None
    base = m.start(2)
    return (label, label_pos, [(base + p, l, c) for p, l, c in inners], tail_end)


def mask_subfigs(text: str) -> str:
    """GÜLTIGE Subfigure-Komplexe (inkl. gültigem äußeren Token-Tail)
    längentreu maskieren — exakt die Komplexe, die der Client (JS 1e0)
    maskiert, damit die inneren ![…](…) und ihre {#fig:}-Labels nicht als
    eigene Figuren aufgesammelt werden. Ungültige Komplexe bleiben stehen:
    ihre Innere werden dann über den einfachen-Bild-Pfad gezählt (Parität).

    text muss code-gereinigt sein (s. strip_code).
    """

    def _sub(m: "re.Match[str]") -> str:
        info = _complex_info(text, m)
        if info is None:
            return m.group(0)
        return " " * (info[3] - m.start())

    return _SUBFIG_ANY_RE.sub(_sub, text)


def _numbered_figure_events(
    text: str,
) -> list[tuple[int, tuple[str, Optional[str], int, list[tuple[int, Optional[str], str]]]]]:
    """Alle Figuren, die der Client (JS 1e/1e0) nummeriert, als
    (Position, (Caption, Label, Label-Pos,
    [(Inner-Label-Pos, Inner-Label, Inner-Caption)])) in Textreihenfolge.
    text muss code-gereinigt sein (s. strip_code).

    Nummeriert = einfache Abbildung mit {#fig:label} im (voll gültigen)
    Token-Tail; Subfigure-Komplex mit äußerm {#fig:label} ODER mindestens
    einem gelabelten Inner (dann darf Label None sein). Inners zählen nicht
    als eigene Figuren (s. mask_subfigs).
    """
    found: list[tuple[int, tuple[str, Optional[str], int, list[tuple[int, Optional[str], str]]]]] = []
    for m in _SUBFIG_ANY_RE.finditer(text):
        info = _complex_info(text, m)
        if info is None:
            continue  # ungültig → literal; die Innere zählen über den Bild-Pfad
        label, label_pos, inners, _tail_end = info
        if label is None and not any(l is not None for _p, l, _c in inners):
            continue  # kein Label irgendwo → keine Nummer
        found.append((m.start(), (m.group(1), label, label_pos, inners)))
    masked = mask_subfigs(text)
    for m in _IMG_RE.finditer(masked):
        valid, label, label_pos, _end = _parse_attr_tail(
            masked, m.end(), allow_zoom=True, video=_is_video_src(m.group(2))
        )
        if valid and label is not None:
            found.append((m.start(), (m.group(1), label, label_pos, [])))
    found.sort(key=lambda e: e[0])
    return found


def scan_figures(
    content: str,
) -> list[tuple[str, Optional[str], Optional[list[tuple[Optional[str], str]]]]]:
    """(Caption, Label, Inners) nummerierter Figuren in Reihenfolge des
    Vorkommens (Code-Blöcke ignoriert).

    Nummeriert = einfache Abbildung mit {#fig:label} im gültigen Token-Tail
    (Tokens in beliebiger Reihenfolge, s. Modul-Docstring) oder gültiger
    Subfigure-Komplex mit äußerm {#fig:label} ODER mindestens einem gelabelten
    Inner (dann darf Label None sein — Parität JS 1e0). Inners eines Komplexes
    zählen nicht als eigene Figuren (s. mask_subfigs); Inners =
    [(fig-Label oder None, Inner-Caption)], sonst None.
    """
    out: list[tuple[str, Optional[str], Optional[list[tuple[Optional[str], str]]]]] = []
    for _pos, (cap, label, _label_pos, inners) in _numbered_figure_events(strip_code(content)):
        out.append((cap, label, [(l, c) for _p, l, c in inners] if inners else None))
    return out


def scan_fig_labels(content: str) -> list[str]:
    """Alle {#fig:…}-Labels nummerierbarer Figuren in Reihenfolge des
    Vorkommens (Code-Blöcke ignoriert): äußere Komplex-Labels, Labels ihrer
    Inners (ebenfalls fig-Labels) und Labels einfacher Abbildungen.
    Ungültige Token-Tails (Duplikat/unbekanntes Token) liefern kein Label —
    der Client zählt sie nicht (Refmap-Parität).
    """
    found: list[tuple[int, str]] = []
    for _pos, (_cap, label, label_pos, inners) in _numbered_figure_events(strip_code(content)):
        for pos, ilabel, _cap in inners:
            if ilabel is not None:
                found.append((pos, ilabel))
        if label is not None:
            found.append((label_pos, label))
    return [label for _pos, label in sorted(found)]
