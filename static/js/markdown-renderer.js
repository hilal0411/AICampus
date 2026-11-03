/**
 * Markdown + LaTeX + Mermaid Renderer für AICampus.
 *
 * Verwendet:
 * - marked.js (MIT) für Markdown-Parser
 * - KaTeX (MIT) für LaTeX-Rendering
 * - Mermaid.js (MIT) für Diagramme (flowchart, sequence, class, state, etc.)
 * - highlight.js (BSD-3) für Python-Syntax-Highlighting
 *
 * Inline-Latex:  $...$                            → Inline
 * Display-Latex: $$...$$                          → Block
 * Mermaid:       ```mermaid                       → SVG-Diagramm;
 *                                                            öffnende Zeile wie bei Code-Blöcken:
 *                                                            {#code:label}[Caption] (in beliebiger
 *                                                            Reihenfolge) → nummerierte Figur
 *                                                            ("Code N: Caption", Anker code:label);
 *                                                            $$…$$ in Knoten-/Kanten-Texten → KaTeX
 *                                                            (nativ von Mermaid gerendert als MathML,
 *                                                            Display-Mode; Text darin via \text{…};
 *                                                            max. 1 Block pro Text)
 * Escaped dollar: \$                              → literal $ (no LaTeX)
 * Nummerierte Figur:  ![caption](src){#fig:label} → "Abb. N: caption" (Anker fig:label)
 * Nummerierte Formel: $$...$$ {#eq:label}         → "(N)" neben der Formel (Anker eq:label)
 * Nummerierte Tabelle: Pipe-Tabelle + Zeile {#tab:label}[Caption]{zoom=X} darunter
 *                                                            → "Tab. N: Caption" unter der Tabelle
 *                                                              (Anker tab:label); {zoom=X} (führender Punkt
 *                                                            optional) = Schriftgröße ×X (Slides UND Skript)
 * Nummerierte Section: ## Titel {#sec:label}      → "K.N[.M]" vor der Überschrift (h2–h6,
 *                                                            kapitellokal; Kapitelnummer aus dem refmap);
 *                                                            Anker sec:label bzw. sec:{sectionId}-{num}
 * Querverweise:       @fig:label / @eq:label / @tab:label / @sec:label
 *                                                            → klickbares "Abb. N" / "Abb. N a)" (Subfigure-Inner) /
 *                                                            "Gl. N" / "Tab. N" / "Abs. N.M"
 *                                                            (In-Page- oder Kapitel-übergreifender Link, ❓ wenn unbekannt)
 *                    @kap:label = Legacy-Alias für @sec:label. Kapitel-Label = {#sec:label}
 *                    als EIGENE ZEILE (erste nicht-leere Zeile) am Kapitelanfang → "Kap. N"
 *                    (verlinkt auf #chapter-{id}); die Label-Zeile wird selbst nicht gerendert
 * Zitationen:        @cite:{key} / @citet:{key} / @citep:{key}
 *                                                            → "[N]" (Superscript) / "Autor (Jahr)" / "(Autor, Jahr)"
 *                                                            N = kursweite stabile Nummer (script-refmap.references);
 *                                                            bei options.bibliography: „Quellen“-Liste (nur zitierte
 *                                                            Einträge) ans Dokument-Ende + In-Page-Anker auf den
 *                                                            Eintrag; Slide-Mode: Link auf die auto-Quellen-Folie des
 *                                                            Decks (parseSlides hängt sie an, Navigation/Highlight per
 *                                                            slides.js-Click-Handler); sonst: Link auf den Quellen-Tab;
 *                                                            ❓ wenn der Key unbekannt ist
 *                                                            Auch in Figuren-Captions (Alt-Text) erlaubt — wird dort
 *                                                            ebenso aufgelöst (renderCaptionRef) und im TOC gerendert
 * Quellen-Eintrag:    @bibentry:{key}             → komplette Quellenangabe (Nummer [N] OHNE
 *                                                            Link, Titel kursiv, DOI/Link) im Design des Skript-
 *                                                            Quellenverzeichnisses — Einträge der auto-Quellen-Folie
 *                                                            der Slide-Decks (trägt id + data-refkey als
 *                                                            Zitations-Ziel); ❓ wenn der Key unbekannt ist
 * Aufgaben-Box:       @task:{id}                  → Aufgaben-Box (Student: Punkte/Medaille analog
 *                                                            Aufgabenübersicht, PROF/TUTOR: kompakt; ❓ wenn unbekannt)
 * Hinweis-Boxen:      @startbox:{typ}[Caption] {#box:label} … @endbox
 *                                                            → farbig markierte Box (merksatz/hinweis/bemerkung/
 *                                                            warnung/beispiel; unbekannte Typen = neutrale Box);
 *                                                            [Caption] = Box-Überschrift („Definition N: Caption“,
 *                                                            $…$-Math erlaubt); Caption/Label in beliebiger
 *                                                            Reihenfolge (je max. einmal)
 *                                                            NESTBAR (beliebige Tiefe): @startbox innerhalb einer Box,
 *                                                            jeweils mit eigenem @endbox (s. Schritt 1d)
 *                                                            highlight: Box OHNE Kopf (transparent + Blur,
 *                                                            Primärfarbe), z.B. für Titel auf Deckslides;
 *                                                            @boxcolor:<farbe> als ERSTE Zeile übersteuert
 *                                                            die Boxfarbe (#hex/rgb()/CSS-Farbname)
 *                                                            code: dunkle Code-Box mit 💻-Kopf
 *                                                            (Inhalt = fenced Code-Block)
 * Spalten:             @startcolumn[:gewicht] … @nextcolumn[:gewicht] … @endcolumn
 *                                                            → Spaltenzeile (CSS Grid; Gewicht = positive Zahl,
 *                                                            Default 1 → fr-Breitenanteil; Marker je eigene Zeile
 *                                                            am Zeilenanfang; Inhalt VOR @startcolumn (z. B.
 *                                                            Überschrift) bleibt vollbreit über der Zeile);
 *                                                            NESTBAR (beliebige Tiefe) — Slides UND Skript
 * Code-Blöcke:        ```<sprache> … ```           → Syntax-Highlighting (hljs, alle Sprachen;
 *                                                            ohne Sprache = Auto-Detection)
 *                                                            Öffnende Zeile: Sprache + optionale Tokens in
 *                                                            BELIEBIGER Reihenfolge (parseFenceHead):
 *                                                            {#lines:1,3-5} → (Slides) Zeilennummern +
 *                                                            Zeilen-Highlight, "|" = weiterer Schritt
 *                                                            (Per-Line-Reveal, Reveal-Highlight-Plugin)
 *                                                            {#aaid:label} → (Slides) Auto-Animate-ID auf dem
 *                                                            <pre> (data-id): derselbe Label auf der nächsten
 *                                                            Folie → native Zeilen-Animation
 *                                                            {.zoom=X} → Schriftgröße ×X, {.height=Y} →
 *                                                            Max-Höhe Y px + internes Scrollen (beides:
 *                                                            Slides UND Skript; führender Punkt optional,
 *                                                            wie bei Applets; Inline-Styles am <pre>)
 *                                                            (unbekanntes Token → 1. Zeile bleibt Code-Text)
 *                                                            $…$ im Code (z. B. Pseudo-Code) → Inline-Formel
 *                                                            (nur Paare, die nach Math aussehen; s. applyCodeMath)
 * AutoAnimate-IDs:    {#aaid:label}                → (nur Slides) explizites Auto-Animate-Element-
 *                                                            Matching: Elemente mit demselben Label auf
 *                                                            zwei Folien animieren per ID ineinander
 *                                                            (Reveal 4.6: data-id; in Reveal 5 heißt es
 *                                                            data-auto-animate-id). Am Zeilenende (nach
 *                                                            Bullet/Absatz/Formel/Figur) oder als Token
 *                                                            nach $$…$$ {#eq:…} bzw. Bild-Snippets.
 * LaTeX-Fragmente:    \fragment{…}                 → (Slides) der eingewickelte Teil der Formel
 *                                                            (Kurzform von \htmlClass{fragment}{…})
 *                                                            erscheint mit einem extra Klick;
 *                                                            \fragment{id}{…} = ID-Gruppe
 *                                                            (gleichzeitig, = \htmlClass{fragment:id}{…}).
 *                                                            Andere Trust-Kommandos
 *                                                            (\href, \includegraphics, …) bleiben
 *                                                            abgelehnt (Security).
 * Applet-Abbildung:   ![caption](src.html)         → interaktives (sandboxed) Iframe;
 *                    ![caption](https://…)          → dito für externe Websites
 *                                                            (http(s)-URL ohne Bild-Endung);
 *                                                            mit {#fig:label} nummeriert wie Bilder
 * Subfiguren:         ![Gesamt](![…](u1){height=…}{#fig:a} ![…](u2){#fig:b}){#fig:label}
 *                                                            → EINE Abbildung mit mehreren Medien in einer
 *                                                            Flex-Zeile (jedes mit eigener Caption; innere
 *                                                            {height=X} und optionales {#fig:label}, je max. einmal,
 *                                                            s. 1e0/6a4)
 *                                                            Sobald mindestens ein Inner ein {#fig:label} trägt,
 *                                                            wird der KOMPLEXX als "Abb. N" nummeriert (auch
 *                                                            OHNE eigenes Komplex-{#fig:label}!) und ALLE Inners
 *                                                            automatisch mit a), b), c), … versehen (Letter =
 *                                                            Position im Komplex; Prefix in der Caption). Gelabelte
 *                                                            Inners sind per @fig:label referenzierbar → "Abb. N a)".
 *                                                            Ohne jegliches Label bleibt die Zeile unnummeriert.
 * Labels dürfen (Unicode-)Buchstaben enthalten, z.B. Umlaute: {#fig:verteilung_überblick}
 *
 * Code blocks (```...``` and `...`) are protected from LaTeX extraction.
 *
 * usage:
 *   await renderMarkdown(text, element)
 *   await renderMarkdown(text, element, { preview: true })  // Editor-Preview
 */

// Initialize Mermaid on first load
if (typeof mermaid !== 'undefined') {
  mermaid.initialize({
    startOnLoad: false,
    theme: 'default',
    securityLevel: 'loose',
  });
}

// ─── Kapitel-übergreifende Referenz-Map ─────────────────────────────────────
// GET /api/courses/{courseId}/script-refmap: live-berechnete globale
// Nummerierung aller fig:/eq:-Labels des Skripts (ohne DB). Wird einmal
// pro Seite geholt und gecacht. Das globale `courseId` wird auf Kurs-Seiten
// definiert (course/base.html bzw. direkt in task_detail/task_solve);
// fehlt es → null.
let _refMapPromise = null;
function _getCourseId() {
  try {
    const cid = (typeof courseId !== 'undefined') ? courseId : null;
    return (cid === null || cid === undefined) ? null : cid;
  } catch (e) {
    return null; // TDZ: courseId wird auf der Seite deklariert, aber noch nicht initialisiert
  }
}

function getCourseRefMap() {
  const cid = _getCourseId();
  if (cid === null) {
    return Promise.resolve(null); // bewusst ohne Caching → nächster Render versucht es erneut
  }
  if (!_refMapPromise) {
    _refMapPromise = fetch(`/api/courses/${cid}/script-refmap`, {
      credentials: 'same-origin',
      cache: 'no-store',
    })
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null)
      .then((data) => {
        // Fehler (5xx, Timeout, Server-Neustart, …): das Fehlschlag-Ergebnis
        // NICHT dauerhaft cachen — sonst rendern alle folgenden Renders der
        // Seite mit Fallback-Nummern (Zähler „springt auf 1“), bis zu einem
        // Reload. Nächstes getCourseRefMap() versucht es erneut.
        if (data === null) _refMapPromise = null;
        return data;
      });
  }
  return _refMapPromise;
}

// GeCachten Refmap verwerfen (nach Kapitel-Änderungen: speichern/löschen/
// verschieben) und neu laden — Nummern/TOC ändern sich sonst erst beim Reload.
function refreshCourseRefMap() {
  _refMapPromise = null;
  return getCourseRefMap();
}

// ─── Live-Refmap beim Bearbeiten (ungespeicherter Inhalt) ──────────────────
// Die gecachte Refmap spiegelt den DB-Stand; neue/geänderte Labels des
// gerade bearbeiteten Kapitels fehlen darin noch → Querverweise rendern ❓
// bis Speichern + Neuöffen (besonders sichtbar in der internen
// Zusammenfassung, die selbst keine Labels enthält). syncRefMapChapter()
// lässt den Server die Nummerierung für den Draft-Inhalt berechnen
// (POST script-refmap-chapter, Dry-Run) und merge das Ergebnis IN die
// gecachte Refmap: die Kapitel-Entries werden ersetzt, die Nummern der
// folgenden Kapitel um den Zähler-Delta verschoben. Nach dem Speichern
// ersetzt refreshCourseRefMap() den ganzen Stand durch den Server-Stand.
// Aufrufe laufen serialized (Promise-Kette) — bei schnellem Tippen gewinnt
// immer der jüngste Inhalt; ein Refresh dazwischen ist unkritisch (der
// Merge trifft dann nur noch das verwaiste Objekt).
let _refMapSyncChain = Promise.resolve();
function syncRefMapChapter(sectionId, content, title) {
  const cid = _getCourseId();
  if (cid === null) return Promise.resolve(null);
  const run = _refMapSyncChain.then(() => _syncRefMapChapterImpl(cid, sectionId, content, title));
  _refMapSyncChain = run.then(() => {}, () => {});
  return run;
}

async function _syncRefMapChapterImpl(cid, sectionId, content, title) {
  const refMap = await getCourseRefMap();
  if (!refMap || !refMap.labels) return null;
  let data;
  try {
    const r = await fetch(`/api/courses/${cid}/script-refmap-chapter`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ section_id: sectionId, content, title }),
    });
    if (!r.ok) return null;
    data = await r.json();
  } catch (e) {
    return null;
  }
  _mergeRefMapChapter(refMap, sectionId, data);
  return data;
}

function _mergeRefMapChapter(refMap, sectionId, d) {
  const key = String(sectionId);
  // 1) Label-Entries dieses Kapitels ersetzen
  for (const k of Object.keys(refMap.labels)) {
    const e = refMap.labels[k];
    if (e && String(e.sectionId) === key) delete refMap.labels[k];
  }
  for (const [k, v] of Object.entries(d.labels || {})) refMap.labels[k] = v;
  // 2) Folgende Kapitel: Nummern je Kind um den Zähler-Delta verschieben
  const order = Array.isArray(refMap.chapterOrder) ? refMap.chapterOrder : [];
  const idx = order.indexOf(key);
  const later = new Set(idx >= 0 ? order.slice(idx + 1) : []);
  const old = (refMap.chapters && refMap.chapters[key]) || {};
  const maxKeys = { fig: 'maxFig', eq: 'maxEq', code: 'maxCode', box: 'maxBox', tab: 'maxTab' };
  const tocLists = { fig: 'figures', code: 'codes', tab: 'tables' };
  for (const kind of Object.keys(maxKeys)) {
    const delta = (d[maxKeys[kind]] || 0) - (old[maxKeys[kind]] || 0);
    if (!delta) continue;
    for (const k of Object.keys(refMap.labels)) {
      const e = refMap.labels[k];
      if (k.startsWith(kind + ':') && e && later.has(String(e.sectionId))) e.num += delta;
    }
    const listName = tocLists[kind];
    if (listName && Array.isArray(refMap[listName])) {
      for (const f of refMap[listName]) {
        if (later.has(String(f.sectionId))) f.num += delta;
      }
    }
  }
  // 3) Kapitel-Entry: Max-Werte, Kapitel-Label, Sections (TOC)
  // (Nur gespeicherte Kapitel — ein neues Kapitel hat noch keine ID und
  // keinen Refmap-Entry; die Content-Preview braucht ihn nicht.)
  if (sectionId !== null && sectionId !== undefined && refMap.chapters) {
    const ch = refMap.chapters[key] || (refMap.chapters[key] = {});
    ch.maxFig = d.maxFig || 0;
    ch.maxEq = d.maxEq || 0;
    ch.maxCode = d.maxCode || 0;
    ch.maxBox = d.maxBox || 0;
    ch.maxTab = d.maxTab || 0;
    if (d.label !== undefined) ch.label = d.label;
    if (d.sections) ch.sections = d.sections;
  }
  // 4) TOC-Listen (figures/codes/tables): Einträge dieses Kapitels ersetzen
  for (const listName of Object.values(tocLists)) {
    const fresh = d[listName];
    if (!Array.isArray(fresh) || !Array.isArray(refMap[listName])) continue;
    const list = refMap[listName];
    for (let i = list.length - 1; i >= 0; i--) {
      if (String(list[i].sectionId) === key) list.splice(i, 1);
    }
    let pos = list.length;
    if (idx >= 0) {
      pos = list.findIndex((f) => later.has(String(f.sectionId)));
      if (pos < 0) pos = list.length;
    }
    list.splice(pos, 0, ...fresh);
  }
}

// ─── Slide-Ref-Map (S-Nummern) ─────────────────────────────────────────────
// GET /api/courses/{courseId}/slides-refmap: slide-eigene Labels (die im
// Skript NICHT vorkommen) mit fortlaufenden S-Nummern über ALLE Slide-Decks
// (je Typ fig/eq/code eigener Zähler, kanonische Deck-Reihenfolge aus der DB)
// + Reveal-Koordinaten (deckId, h, v) fürs Verlinken. Eigenes gecachtes
// Promise, unabhängig von der Skript-Ref-Map.
let _slidesRefMapPromise = null;
function getCourseSlidesRefMap() {
  const cid = _getCourseId();
  if (cid === null) {
    return Promise.resolve(null); // bewusst ohne Caching → nächster Render versucht es erneut
  }
  if (!_slidesRefMapPromise) {
    _slidesRefMapPromise = fetch(`/api/courses/${cid}/slides-refmap`, {
      credentials: 'same-origin',
      cache: 'no-store',
    })
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null)
      .then((data) => {
        // Wie script-refmap: Fehlschläge nicht dauerhaft cachen (s. dort).
        if (data === null) _slidesRefMapPromise = null;
        return data;
      });
  }
  return _slidesRefMapPromise;
}

function refreshCourseSlidesRefMap() {
  _slidesRefMapPromise = null;
  return getCourseSlidesRefMap();
}

function _chapterRef(refMap, sectionId) {
  if (!refMap || !refMap.chapters || sectionId === null || sectionId === undefined) return null;
  return refMap.chapters[String(sectionId)] || null;
}

// ─── Quellzeilen-Tracking (Preview→Editor-Scroll-Sync) ────────────────
// renderMarkdown(sourceLines) pflegt während der Extraktionsstufen
// lines[i] = Originalzeile (0-basiert) der i-ten Pipeline-Zeile und
// markiert die Top-Level-Blocks der Preview mit data-src-line (s.
// _insertSrcLineMarkers/_applySrcLineMarkers am Ende). Verwendung:
// Skript-Edit-Modus — Preview-Scroll scrollt den Editor auf die
// zugehörige Quellzeile.
function _nlPrefix(text) {
  // prefix[i] = Anzahl \n in text[0..i) → Zeilenindex für jeden Offset
  const p = new Array(text.length + 1);
  p[0] = 0;
  for (let i = 0; i < text.length; i++) p[i + 1] = p[i] + (text[i] === '\n' ? 1 : 0);
  return p;
}

// [start, end) → repl im Zeilen-Tracking: alle Zeilen von repl (und der
// sie tragenden Zeile) bekommen die Zeilennummer der Regions-Startzeile.
function _replLines(lines, prefix, start, end, repl) {
  const sl = prefix[start];
  const el = prefix[end - 1];
  const line0 = lines[sl];
  lines.splice(sl, el - sl + 1, ...repl.split('\n').map(() => line0));
}

// lines-Abschnitt für den Text-Slice [a, b) (b = Ende wenn weggelassen);
// a darf mid-line sein (die Partial-Zeile übernimmt die Nummer ihrer Zeile).
function _sliceLines(text, lines, a, b) {
  const end = b === undefined ? text.length : b;
  if (end <= a) return [];
  const p = _nlPrefix(text);
  return lines.slice(p[a], p[end - 1] + 1);
}

// Anzahl Zeilen, die ein leading trim() entfernt (reine Whitespace-Zeilen).
function _leadTrimLines(s) {
  let m = 0;
  while (m < s.length && /\s/.test(s[m])) m++;
  return (s.slice(0, m).match(/\n/g) || []).length;
}

// Zeilen-Zuweisungen für aus Segmente-Texten zusammengesetzten
// Wrapper-Text (Boxen/Spalten): eine Zeile bekommt die Zuweisung des
// Segments, das sie zuerst mit (Nicht-Whitespace-)Inhalt füllt;
// leere Zeilen → line0.
function _segLineAssign(line0, segs) {
  const out = [];
  let cur = null;
  for (const seg of segs) {
    let k = 0;
    for (let i = 0; i < seg.t.length; i++) {
      const c = seg.t[i];
      if (c === '\n') {
        out.push(cur === null ? line0 : cur);
        cur = null;
        k += 1;
      } else if (cur === null && c !== ' ' && c !== '\t' && c !== '\r') {
        cur = seg.f(k);
      }
    }
  }
  if (cur !== null) out.push(cur);
  return out;
}

// .replace + Zeilen-Tracking in einem Rutsch: pro Match wird der
// Ersatz (Offsets im Vorersatz-Text) gesammelt und nach dem Replace
// rückwärts angewendet. fn-Signatur wie gewohnt (match, groups, …).
function _trackedReplace(text, lines, re, fn) {
  const prefix = _nlPrefix(text);
  const pend = [];
  const out = text.replace(re, (...args) => {
    const m = args[0];
    const off = args[args.length - 2];
    const repl = fn(...args);
    if (typeof repl === 'string' && repl !== m) pend.push([off, off + m.length, repl]);
    return repl;
  });
  for (let i = pend.length - 1; i >= 0; i--) _replLines(lines, prefix, pend[i][0], pend[i][1], pend[i][2]);
  return out;
}

// ─── Hinweis-/Merksatz-/Mathe-Boxen: @startbox:{typ} … @endbox ─────────
// Bekannte Typen mit Icon & Überschrift. Unbekannte Typen werden als neutrale
// Box mit dem rohen Typen als Titel gerendert (Inhalt geht nicht verloren).
// [Caption] auf der @startbox:-Zeile (beliebige Reihenfolge mit {#box:label})
// ergänzt den Kopf: „Definition N: Caption“ (Math: s. renderCaptionMath).
// Mathe-Typen (definition, satz, …): Referenzen auf beschriftete Boxen
// ({#box:label}) zeigen den Typ-Titel („Satz N“), s. Xref-Auflösung unten.
// NESTBAR (beliebige Tiefe, s. Schritt 1d / _convertBoxBlocks): ein gültiges
// @startbox:{typ} öffnet, ein @endbox am Zeilenanfang schließt — die
// zugehörigen Marker paart die Tiefenzählung (BOX_EVENT_RE).
const CALLOUT_TYPES = {
  merksatz: { icon: '📌', title: 'Merksatz' },
  hinweis: { icon: '💡', title: 'Hinweis' },
  bemerkung: { icon: 'ℹ️', title: 'Bemerkung' },
  warnung: { icon: '⚠️', title: 'Warnung' },
  beispiel: { icon: '📎', title: 'Beispiel' },
  code: { icon: '💻', title: 'Code' },
  definition: { icon: '📖', title: 'Definition' },
  satz: { icon: '📜', title: 'Satz' },
  theorem: { icon: '⭐', title: 'Theorem' },
  lemma: { icon: '🧩', title: 'Lemma' },
  proposition: { icon: '📃', title: 'Proposition' },
  korollar: { icon: '🌟', title: 'Korollar' },
  beweis: { icon: '🧮', title: 'Beweis' },
  frage: { icon: '❔', title: 'Frage' },
};

// Box-Events für das Paaren/Nesten (Schritt 1d): gültiges @startbox:{typ}
// (Typ direkt nach dem Doppelpunkt) ÖFFNET, @endbox SCHLIESST (nur am
// Zeilenanfang wirksam — den Check macht _findBoxClose; Parität zum alten
// 1d-Regex \r?\n@endbox).
const BOX_EVENT_RE = /@startbox:([\p{L}0-9_-]+)|@endbox/gu;

// ─── Spalten: @startcolumn … @nextcolumn … @endcolumn ──────────────────
// Spaltenzeile als CSS Grid (Styling: main.css .aicampus-cols/.aicampus-col).
// @startcolumn öffnet die Zeile (1. Spalte), jedes @nextcolumn startet die
// nächste Spalte, @endcolumn (nur am Zeilenanfang) schließt die Zeile.
// Gewicht = positive Zahl nach ":" (Default 1) → fr-Breitenanteil (z. B.
// @startcolumn:2 … @nextcolumn:1 = 2:1). NESTBAR (beliebige Tiefe).
// Die Marker wirken NUR am Zeilenanfang; hinter dem Marker ist nur
// optionales :gewicht + Leerraum erlaubt (sonst bleibt die Zeile literal).
// Code-Blöcke sind zu diesem Zeitpunkt bereits extrahiert (1a–1c) →
// in Code bleiben die Marker literal. Ungepaarte/ungeschlossene Marker
// bleiben sichtbar (Tippfehler fallen auf).
const COLUMN_EVENT_RE = /@startcolumn(?::([0-9]+(?:\.[0-9]+)?))?|@nextcolumn(?::([0-9]+(?:\.[0-9]+)?))?|@endcolumn/gu;

// Head nach einem Column-Marker (Index direkt nach dem gematchten Marker +
// optionalem :gewicht): nur Leerraum erlaubt, Gewicht (falls vorhanden)
// muss > 0 sein → Gewicht oder null (Marker bleibt literal).
function _columnHeadWeight(text, markerEnd, weightRaw) {
  const nl = text.indexOf('\n', markerEnd);
  const head = nl === -1 ? '' : text.slice(markerEnd, nl);
  if (!/^[ \t]*\r?$/.test(head)) return null;
  const weight = weightRaw === undefined ? 1 : parseFloat(weightRaw);
  return Number.isFinite(weight) && weight > 0 ? weight : null;
}

// Nächste gültige @startcolumn (Zeilenanfang, gültige Head-Zeile) ab pos
// → { start, end (Index nach der Head-Zeile), weight } oder null.
function _nextColumnOpen(text, pos) {
  const re = /@startcolumn(?::([0-9]+(?:\.[0-9]+)?))?/gu;
  re.lastIndex = pos;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > 0 && text[m.index - 1] !== '\n') continue; // nur Zeilenanfang
    const weight = _columnHeadWeight(text, m.index + m[0].length, m[1]);
    if (weight === null) continue;
    const nl = text.indexOf('\n', m.index + m[0].length);
    return { start: m.index, end: nl === -1 ? text.length : nl + 1, weight };
  }
  return null;
}

// Zeile ab from (Index NACH der @startcolumn-Head-Zeile) mit Depth-Zählung
// scannen (1 = äußere Zeile offen): gültiges @startcolumn (Zeilenanfang)
// öffnet eine verschachtelte Zeile (Depth+1), @endcolumn (Zeilenanfang)
// schließt (Depth−1; bei 0 = Ende der äußeren Zeile). @nextcolumn zählt
// NUR auf Depth 1 (Split der äußeren Zeile) — tiefere @nextcolumn gehören
// zu verschachtelten Zeilen. → { close, closeEnd, splits } oder null
// (ungeschlossen).
function _scanColumnBlock(text, from) {
  COLUMN_EVENT_RE.lastIndex = from;
  let depth = 1;
  const splits = []; // @nextcolumn auf Depth 1: { start, weight, headEnd }
  let m;
  while ((m = COLUMN_EVENT_RE.exec(text)) !== null) {
    if (m.index > 0 && text[m.index - 1] !== '\n') continue; // nur Zeilenanfang
    const isNext = m[0].startsWith('@nextcolumn');
    const weight = _columnHeadWeight(text, m.index + m[0].length, isNext ? m[2] : m[1]);
    if (isNext) {
      if (weight !== null && depth === 1) {
        const nl = text.indexOf('\n', m.index + m[0].length);
        splits.push({ start: m.index, weight, headEnd: nl === -1 ? text.length : nl + 1 });
      }
      continue;
    }
    if (m[0].startsWith('@startcolumn')) {
      if (weight !== null) depth += 1;
      continue;
    }
    // @endcolumn — wie @endbox: ohne Head-Check (Parität); ein
    // FALSCH geschriebener Marker matcht den Regex nicht und hält die
    // Zeile geöffnet → bleibt literal sichtbar (Tippfehler fällt auf).
    depth -= 1;
    if (depth === 0) return { close: m.index, closeEnd: m.index + '@endcolumn'.length, splits };
  }
  return null;
}

function _convertColumnBlocks(text, lines) {
  const open = _nextColumnOpen(text, 0);
  if (open === null) return [text, lines];
  const scan = _scanColumnBlock(text, open.end);
  if (scan === null) {
    // ungeschlossene Zeile → bleibt literal, ab nach der Head-Zeile weiter
    const p = _nlPrefix(text);
    const res = _convertColumnBlocks(text.slice(open.end), _sliceLines(text, lines, open.end));
    return [text.slice(0, open.end) + res[0], lines.slice(0, p[open.end]).concat(res[1])];
  }
  const weights = [open.weight];
  // Zellen-Grenzen paarweise: [offen, @nextcolumn-Start), [@nextcolumn-Ende,
  // @nextcolumn-Start), … — die @nextcolumn-Zeilen gehören zu KEINER Zelle
  // (Marker-Text wird verworfen, nur das Gewicht zählt).
  const bounds = [open.end];
  for (const s of scan.splits) { bounds.push(s.start, s.headEnd); weights.push(s.weight); }
  bounds.push(scan.close);
  const p = _nlPrefix(text);
  const line0 = lines[p[open.start]];
  const gridOpen = '\n\n<div class="aicampus-cols" style="grid-template-columns:' +
    weights.map((w) => w + 'fr').join(' ') + '">';
  const cellOpen = '\n<div class="aicampus-col">\n\n';
  const cellClose = '\n\n</div>';
  const gridClose = '\n</div>\n\n';
  let html = gridOpen;
  const segs = [{ t: gridOpen, f: () => line0 }];
  for (let i = 0; i < weights.length; i++) {
    const cell = _convertColumnBlocks(
      text.slice(bounds[i * 2], bounds[i * 2 + 1]),
      _sliceLines(text, lines, bounds[i * 2], bounds[i * 2 + 1])
    );
    const cellTrimmed = cell[0].trim();
    const lead = _leadTrimLines(cell[0]);
    html += cellOpen + cellTrimmed + cellClose;
    segs.push(
      { t: cellOpen, f: () => line0 },
      { t: cellTrimmed, f: (k) => cell[1][k + lead] },
      { t: cellClose, f: () => line0 },
    );
  }
  html += gridClose;
  segs.push({ t: gridClose, f: () => line0 });
  const tailRes = _convertColumnBlocks(text.slice(scan.closeEnd), _sliceLines(text, lines, scan.closeEnd));
  segs.push({ t: tailRes[0], f: (k) => tailRes[1][k] });
  return [
    text.slice(0, open.start) + html + tailRes[0],
    lines.slice(0, p[open.start]).concat(_segLineAssign(line0, segs)),
  ];
}

// ─── Highlight-Box: @boxcolor:<farbe> ───────────────────────────────────
// @startbox:highlight ist die headless Variante der Hinweis-Boxen (kein
// Icon/Überschrift-Kopf, Styling in slides.css). Als ERSTE Zeile des
// Boxinhalts darf @boxcolor:<farbe> die Boxfarbe übersteuern
// (#rgb/#rrggbb, rgb()/rgba(), klassische CSS-Farbnamen). Der Renderer
// emittiert eine normalisierte rgba()-Zeile als Inline-Style (überlebt
// DOMPurify, s. Applet-Styling); ungültige Werte → CSS-Default
// (Primärfarbe mit HIGHLIGHT_BOX_ALPHA).
const HIGHLIGHT_BOX_ALPHA = 0.3;
const BOX_COLOR_NAMES = {
  white: [255, 255, 255], black: [0, 0, 0], red: [220, 38, 38], green: [22, 163, 74],
  blue: [37, 99, 235], yellow: [234, 179, 8], orange: [249, 115, 22], purple: [147, 51, 234],
  pink: [236, 72, 153], brown: [146, 64, 14], gray: [107, 114, 128], grey: [107, 114, 128],
  cyan: [6, 182, 212], magenta: [233, 30, 99], lime: [132, 204, 22], teal: [13, 148, 136],
  navy: [30, 58, 138], maroon: [127, 29, 29], olive: [113, 99, 41],
};

function _boxColorToRgba(value) {
  // → normalisierte "rgba(r, g, b, a)"-Zeile oder null (→ CSS-Default).
  // Nur regulargültige Werte werden akzeptiert → kein Weg, beliebigen
  // CSS-Text in den Inline-Style zu schmuggeln.
  value = String(value).trim().replace(/^["']+|["']+$/g, '');
  if (!value) return null;

  let m = /^#([\da-f]{3}|[\da-f]{6})$/i.exec(value);
  if (m) {
    let h = value.slice(1);
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    return 'rgba(' + parseInt(h.slice(0, 2), 16) + ', ' + parseInt(h.slice(2, 4), 16) +
      ', ' + parseInt(h.slice(4, 6), 16) + ', ' + HIGHLIGHT_BOX_ALPHA + ')';
  }

  m = /^rgba?\(\s*(0|[1-9]\d{0,2})\s*,\s*(0|[1-9]\d{0,2})\s*,\s*(0|[1-9]\d{0,2})\s*(?:,\s*([0-9]*\.?[0-9]+))?\s*\)$/i.exec(value);
  if (m) {
    let alpha = HIGHLIGHT_BOX_ALPHA;
    if (m[4] !== undefined) {
      alpha = parseFloat(m[4]);
      if (alpha < 0 || alpha > 1) return null;
    }
    return 'rgba(' + m[1] + ', ' + m[2] + ', ' + m[3] + ', ' + alpha + ')';
  }

  const rgb = BOX_COLOR_NAMES[value.toLowerCase()];
  if (rgb) return 'rgba(' + rgb[0] + ', ' + rgb[1] + ', ' + rgb[2] + ', ' + HIGHLIGHT_BOX_ALPHA + ')';
  return null;
}

// ─── Applet-/Website-Erkennung ──────────────────────────────────────────
// Ein „Applet“ ist ein .html-Medium (eigene /media-Applets) ODER eine
// externe Website (http(s)-URL ohne Bild-Endung) — beide werden als
// Iframe gerendert. Die Sandbox unterscheidet sich:
// - eigene Applets: allow-scripts OHNE allow-same-origin — sie laufen
//   unter eigener (App-)Origin, mit same-origin könnten kompromittierte
//   Applets via window.parent auf das Parent-DOM zugreifen;
// - externe Websites: + allow-same-origin — ohne Storage/Subresource-
//   Zugriffe läuft kaum eine reale Site; sie haben eine fremde Origin
//   und erreichen das Parent-DOM daher nicht.
const APPLET_IMAGE_EXT = /\.(?:png|jpe?g|gif|webp|svg|bmp|avif|ico)(?:[?#].*)?$/i;
function isAppletSrc(src) {
  // ?v=-Suffix (Live-Update-Cache-Buster, s. _mediaBustPass) durchlässig
  const path = src.split(/[?#]/)[0];
  return /\.html?$/i.test(path) ||
    (/^https?:\/\//i.test(src) && !APPLET_IMAGE_EXT.test(path));
}
// Internes mp4-Video (eigene /media-URL) → natives <video>-Element mit
// Controls (Play/Seek). Externe http(s)-Videos laufen über die Applet-Logik
// (Iframe, s. isAppletSrc — mp4 ist keine Bild-Endung).
function isVideoSrc(src) {
  const path = src.split(/[?#]/)[0];
  return !/^https?:\/\//i.test(src) && /\.(mp4|webm)$/i.test(path);
}
// <video>-Playback-Attribute aus den Attribut-Tokens (s. 1e). Defaults:
// controls=true, autoplay/loop/muted=false; explizites {controls=false}
// ohne explizites {autoplay=…} ⇒ autoplay=true (sonst lässt sich das Video
// nicht starten).
function videoAttrs(f) {
  let s = f.controls !== false ? ' controls' : '';
  if (f.autoplay !== null ? f.autoplay : f.controls === false) s += ' autoplay';
  if (f.loop === true) s += ' loop';
  if (f.muted === true) s += ' muted';
  return s;
}
// Live-Update-Cache-Bust: Applet-Medien, die in dieser Seite per Live-Event
// (origin_media) geändert wurden, haben in window.aicampusMediaBusts einen
// Zeitstempel. Beim Render wird der Dateiname durch <name>?v=<ts> ersetzt.
// Notwendig, weil der Browser-Memory-Cache für Bilder pro URL keyt und die
// bereits geladene Datei bei in-place Re-Render ohne Request wiederverwendet
// (auch no-cache/no-store-Header ändern das nicht) → stummer Stale-Content.
// Die rohe Markdown-Quelle bleibt unberührt (nur das gerenderte HTML wird
// gebusted) → keine ?v=-Lecks in gespeicherten Inhalten.
function _mediaBustPass(html) {
  const busts = window.aicampusMediaBusts || {};
  for (const [fname, ts] of Object.entries(busts)) {
    if (!html.includes(fname)) continue;
    html = html.split(fname + "?v=").join(fname); // Alt-Bust → idempotent
    html = html.split(fname).join(fname + "?v=" + ts);
  }
  return html;
}
function appletSandboxAttr(src) {
  if (!/^https?:\/\//i.test(src)) return "allow-scripts";
  // YouTube-Player: der „Zu YouTube“-Link braucht allow-popups. Vollbild ist
  // per Embed-Param fs=0 deaktiviert (Button versteckt), weil fullscreen aus
  // dem cross-origin-Frame trotz Permissions-Policy-Delegation in Chrome
  // blockiert wird → kein allow-fullscreen mehr nötig.
  if (/youtube(?:-nocookie)?\.com/i.test(src)) {
    return "allow-scripts allow-same-origin allow-popups";
  }
  return "allow-scripts allow-same-origin";
}

// YouTube-URL (watch?v=…, youtu.be/…, shorts/…, embed/…; youtube.com und
// youtube-nocookie.com) → Embed-URL des Players. Damit werden YouTube-
// Links wie jedes andere Medium eingebunden (Iframe-Player statt der
// ganzen Watch-Seite). Nicht-YouTube-URLs → null.
function youtubeEmbedSrc(src) {
  let m;
  if ((m = /^https?:\/\/(?:www\.|m\.)?youtube(?:-nocookie)?\.com\/(?:watch\?(?:[^#]*&)?v=|embed\/|shorts\/)([A-Za-z0-9_-]+)(?:[?&#]|$)/i.exec(src)) ||
      (m = /^https?:\/\/(?:www\.)?youtu\.be\/([A-Za-z0-9_-]+)(?:[?&#]|$)/i.exec(src))) {
    // fs=0: Vollbild-Button im Player verstecken — fullscreen aus dem
    // cross-origin-Frame ist ohnehin nicht nutzbar (s. appletSandboxAttr).
    return `https://www.youtube.com/embed/${m[1]}?fs=0`;
  }
  return null;
}

// ─── Applet-Auto-Sizing ───────────────────────────────────────────────
// Eigene Applets (.html-Medien) melden ihre Inhaltshöhe per postMessage — das
// Boilerplate wird serverseitig in jeden Applet-Versand injiziert (siehe
// serve_media in main.py). Das Iframe passt sich an (150–600 px);
// darüber hinaus scrollt das Applet im Iframe intern.
// Externe Websites senden keine solche Nachricht → Default-Höhe bleibt.
const APPLET_MIN_H = 150;
const APPLET_MAX_H = 600;
window.addEventListener('message', (event) => {
  const d = event.data;
  if (!d || d.source !== 'aicampus-applet' || typeof d.height !== 'number' || !isFinite(d.height)) return;
  document.querySelectorAll('iframe.aicampus-applet').forEach((f) => {
    if (f.contentWindow === event.source) {
      // Gezoomte Applets ({.zoom=X}): Clamp zoom-korrigiert, damit die
      // VISIBLE Höhe (gemeldete Höhe × zoom) im selben Bereich bleibt.
      const zoom = parseFloat(f.getAttribute('data-zoom')) || 1;
      let h = Math.round(
        Math.min(Math.max(d.height + 8, APPLET_MIN_H / zoom), APPLET_MAX_H / zoom)
      );
      // Explizite Max-Höhe ({.height=X} → data-max-h, sichtbare px):
      // darüber hinaus scrollt das Applet im Iframe intern.
      const maxH = parseFloat(f.getAttribute('data-max-h'));
      if (maxH > 0) {
        const v = Math.min(h * zoom, maxH);
        h = Math.max(1, Math.round(v / zoom));
      }
      f.style.height = h + 'px';
      // Gezoomte Applets stehen in einem .aicampus-applet-zoom-Wrapper, dessen
      // Höhe der sichtbaren (geskalten) Höhe folgen muss (transform ändert
      // nicht die Iframe-Layout-Box). offsetHeight statt h: die Iframe-Box
      // kann per CSS max-height kleiner sein (Default-Höhe ohne explizites
      // {.height} / {.height}=X), der Wrapper muss dem sichtbaren Ergebnis
      // folgen, nicht der unbeachteten Zielhöhe.
      const wrap = f.parentElement && f.parentElement.classList.contains('aicampus-applet-zoom')
        ? f.parentElement
        : null;
      if (wrap) wrap.style.height = Math.round((f.offsetHeight || h) * zoom) + 'px';
    }
  });
});

// ─── Fenced-Code-Block-Head ──────────────────────────────────────────
// Öffnende Zeile eines ```-Blocks: optionale Sprache (bares Wort an
// erster Position) + optionale Tokens in beliebiger Reihenfolge —
// durch Leerraum getrennt ODER nahtlos direkt aneinander:
//   {#lines:1,3-5}   Zeilennummern + Highlight-Schritte (Slides)
//   {#aaid:label}    Auto-Animate-ID (Slides)
//   {#code:label}    Code-Label (nummeriert "Code N", Codes-Tabelle im
//                    Inhaltsverzeichnis, Verweis ins Skript)
//   [caption]        Code-Caption (NUR in Kombination mit {#code:label};
//                    ohne Label bleibt die Zeile Code-Text, damit z. B.
//                    ```python [1,2,3] weiter ein Code-Array ist)
//   {.zoom=X}        Schriftgröße ×X
//   {.height=Y}      Max-Höhe Y px (Suffix ∅ oder "px", wie Applets)
// (Zoom/Height: führender Punkt optional, wie bei Applets; wirken in
// Slides UND Skript — Inline-Styles am <pre>, kein Kontext-CSS nötig.)
// Unbekanntes Token oder Duplikat → null (Fallback: 1. Zeile = Code-Text,
// Tippfehler fallen sichtbar auf — wie beim Applet-Attribut-Parser).
function parseFenceHead(head) {
  const out = { language: '', lines: null, aaid: null, zoom: null, height: null, codeLabel: null, caption: null };
  let matchedAny = false;
  let i = 0;
  const n = head.length;
  const isWS = (ch) => ch === ' ' || ch === '\t';
  while (i < n) {
    if (isWS(head[i])) { i += 1; continue; }
    // Token = {…} (bis zum nächsten }), […] (bis zum nächsten ]) oder
    // bares Wort (bis zum nächsten Leerraum/{/[) → nahtlose Tokens sind
    // damit automatisch möglich.
    let tok, isBare = false;
    if (head[i] === '{') {
      const end = head.indexOf('}', i);
      if (end === -1) return null;
      tok = head.slice(i, end + 1);
      i = end + 1;
    } else if (head[i] === '[') {
      const end = head.indexOf(']', i);
      if (end === -1) return null;
      tok = head.slice(i, end + 1);
      i = end + 1;
    } else {
      let j = i;
      while (j < n && !isWS(head[j]) && head[j] !== '{' && head[j] !== '[') j += 1;
      tok = head.slice(i, j);
      i = j;
      isBare = true;
    }
    let m;
    if (isBare) {
      // Sprache: bares Wort nur an erster Position (vor jedem Token)
      if (matchedAny) return null;
      if (!/^[a-zA-Z][a-zA-Z0-9+-]*$/.test(tok)) return null;
      out.language = tok;
      matchedAny = true;
      continue;
    }
    if ((m = tok.match(/^\{#lines:([\d,| -]+)\}$/u))) {
      if (out.lines !== null) return null; // Duplikat
      out.lines = m[1];
      matchedAny = true;
    } else if ((m = tok.match(/^\{#aaid:([\p{L}0-9_-]+)\}$/u))) {
      if (out.aaid !== null) return null; // Duplikat
      out.aaid = m[1];
      matchedAny = true;
    } else if ((m = tok.match(/^\{#code:([\p{L}0-9_-]+)\}$/u))) {
      if (out.codeLabel !== null) return null; // Duplikat
      out.codeLabel = m[1];
      matchedAny = true;
    } else if (tok[0] === '[') {
      // Caption nur nach {#code:label} (und nur einmal); leer → ungültig.
      if (out.codeLabel === null) return null;
      if (out.caption !== null) return null; // Duplikat
      const cap = tok.slice(1, -1).trim();
      if (!cap) return null;
      out.caption = cap;
      matchedAny = true;
    } else if ((m = tok.match(/^\{\.?zoom=([\d.]+)\}$/))) {
      if (out.zoom !== null) return null; // Duplikat
      out.zoom = parseFloat(m[1]);
      matchedAny = true;
    } else if ((m = tok.match(/^\{\.?height=([\d.]+)([a-z]*)\}$/))) {
      if (m[2] !== '' && m[2] !== 'px') return null; // Suffix: ∅ oder "px"
      if (out.height !== null) return null; // Duplikat
      out.height = parseFloat(m[1]); // px
      matchedAny = true;
    } else {
      return null; // unbekanntes Token → Fallback
    }
  }
  return matchedAny ? out : null;
}

// Bibliographie-Eintrag: "[N] Autoren (Jahr). <em>Titel</em>. Venue, Detail. DOI Link" —
// gemeinsame Basis für das Quellenverzeichnis am Dokument-Ende (6b) und die
// @bibentry:-Einträge der auto-Quellen-Folie (Slide-Decks). Die Nummer [N] ist
// bewusst KEIN Link: der Quellen-Tab in der Kurs-Navigation ist für Studenten
// nicht erreichbar; DOI/URL bleiben klickbar.
function _bibEntryHtml(r) {
  let entry = '';
  const authors = (r.authors || []).join(' & ');
  if (authors) entry += escapeHtml(authors);
  if (r.year) entry += ' (' + escapeHtml(r.year) + ')';
  if (entry) entry += '.';
  if (r.title) entry += ' <em>' + escapeHtml(r.title) + '</em>.';
  const tail = [r.venue, r.detail].filter(Boolean).join(', ');
  if (tail) entry += ' ' + escapeHtml(tail) + '.';
  // DOI und/oder Link immer klickbar anzeigen, wenn vorhanden
  const links = [];
  if (r.doi) links.push(
    `<a href="https://doi.org/${escapeHtml(r.doi)}" target="_blank" rel="noopener" class="text-blue-600 hover:underline">DOI</a>`);
  if (r.url) links.push(
    `<a href="${escapeHtml(r.url)}" target="_blank" rel="noopener" class="text-blue-600 hover:underline">Link</a>`);
  if (links.length) entry += ' ' + links.join(' ');
  return `<span class="aicampus-bibnum text-gray-400 font-mono">[${r.num}]</span> ` + entry;
}

// Tabellen: inhaltslose Kopfzeile (alle <th> leer) → <thead> entfernen.
// GFM-Tabellen haben IMMER eine erste Zeile als <thead> (grau
// gehighlightet); ist sie ohne Inhalt, wäre das ein leerer grauer Balken
// über der Tabelle → wird dann nicht gerendert.
function _stripEmptyTableHead(html) {
  return html.replace(/<table[^>]*>([\s\S]*?)<\/table>/g, (m, inner) => {
    const tm = inner.match(/<thead>\s*<tr[^>]*>([\s\S]*?)<\/tr>\s*<\/thead>/);
    if (!tm) return m;
    const cells = tm[1].match(/<th[^>]*>[\s\S]*?<\/th>/g) || [];
    const allEmpty = cells.every((c) =>
      c.replace(/<[^>]+>/g, '').replace(/&nbsp;/gi, '').trim() === '');
    return allEmpty ? m.replace(tm[0], '') : m;
  });
}

async function renderMarkdown(text, targetElement, options = {}) {
  if (!text || typeof text !== 'string') {
    targetElement.innerHTML = '';
    return;
  }

  const { preview = false, sectionId = null, slideMode = false, slidePos = null, sourceLines = false, breaks = false } = options;

  // Globale Label-Map für Querverweise (gecacht; null auf Nicht-Kurs-Seiten).
  // Keys sind kind-prefixed ("eq:test" ≠ "box:test"), s. script-refmap.
  const refMap = await getCourseRefMap();
  const globalLabels = (refMap && refMap.labels) || {};
  const chapterRef = _chapterRef(refMap, sectionId);
  // Slide-Ref-Map: slide-eigene Labels → S-Nummern (fortlaufend über alle
  // Decks) + Link-Ziele (deckId/h/v). Immer holen (gecacht) — auch
  // Skript-/Aufgaben-Seiten dürfen per @fig:/@eq:/@code: auf Slide-Objekte
  // referenzieren.
  const slidesRefMap = await getCourseSlidesRefMap();
  const slidesLabels = (slidesRefMap && slidesRefMap.labels) || {};
  const slidesMaxSFig = (slidesRefMap && slidesRefMap.maxSFig) || 0;
  const slidesMaxSEq = (slidesRefMap && slidesRefMap.maxSEq) || 0;
  const slidesMaxSCode = (slidesRefMap && slidesRefMap.maxSCode) || 0;
  const slidesMaxSBox = (slidesRefMap && slidesRefMap.maxSBox) || 0;
  const slidesMaxSTab = (slidesRefMap && slidesRefMap.maxSTab) || 0;

  // Für das Quellenverzeichnis (Nur zitierte Einträge) — auch Caption-
  // Zitationen (renderCaptionRef) füllen sie, damit Zitate in Figuren-
  // Captions im Quellenverzeichnis erscheinen.
  const citedRefs = [];
  // In-Deck-Anker auf die auto-Quellen-Folie (parseSlides hängt sie an): deck-
  // scoped (ref-{deckId}-…), damit mehrere Decks auf einer Seite (Folien-
  // Übersicht mit Kacheln) keine doppelten IDs bekommen; Skript: ref-{key}
  // (Kapitel-Seite, dort eindeutig).
  const refAnchorId = (key) =>
    slideMode && slidePos && slidePos.deckId != null ? `ref-${slidePos.deckId}-${key}` : `ref-${key}`;
  // Caption-Kontext für renderCaptionRef (Figuren-Captions + TOC): dieselbe
  // Link-Ziel-Regel wie die Fließtext-Zitationen (Schritt 6b).
  const captionCtx = {
    refMap,
    citedRefs,
    hrefFor: (label) =>
      options.bibliography || slideMode
        ? `#${refAnchorId(label)}`
        : `/courses/${(refMap && refMap.courseId) || ''}/references#ref-${label}`,
  };

  // 0. Kapitel-Label ({#sec:label} als erste nicht-leere Zeile) → kein Content, entfernen
  let lineOffset = 0; // (sourceLines) Zeilen, die Schritt 0 entfernt
  {
    const m0 = /^(?:[ \t]*\n)*[ \t]*\{#sec:[\p{L}0-9_-]+\}[ \t]*(?:\r?\n|$)/u.exec(text);
    if (m0) {
      lineOffset = (m0[0].match(/\n/g) || []).length + (m0[0].endsWith('\n') ? 0 : 1);
      text = text.slice(m0[0].length);
    }
  }
  // (sourceLines) lines[i] = Originalzeile (0-basiert) der i-ten
  // Pipeline-Zeile; jede Extraktionsstufe unten pflegt sie.
  let lines = text.split('\n').map((_, i) => i);

  // ── Pre-extraction phase ──────────────────────────────────────────
  // Order matters: extract code blocks FIRST so LaTeX extraction never
  // sees $ signs inside them.

  // 1a. Extract ```mermaid ... ``` blocks.
  //     Öffnende Fence-Zeile: optionales {#code:label} und/oder [Caption]
  //     (beliebige Reihenfolge, je max. einmal) → das Diagramm bekommt wie
  //     Code-Blöcke eine nummerierte Caption ("Code N: Caption", s. Schritt 11).
  //     Jedes andere Token → Block fällt durch zum normalen Code (1b).
  const mermaidBlocks = [];
  let processed = _trackedReplace(text, lines, /```mermaid([^\n]*)\n([\s\S]*?)```/g, (match, head, diagram) => {
    head = head.trim();
    let mLabel = null;
    let mCaption = null;
    if (head) {
      let valid = true;
      let last = 0;
      const tokRe = /\{#code:([\p{L}0-9_-]+)\}|\[([^\]]*)\]/gu;
      let tm;
      while ((tm = tokRe.exec(head)) !== null) {
        if (head.slice(last, tm.index).trim()) { valid = false; break; }
        if (tm[1] !== undefined) {
          if (mLabel !== null) { valid = false; break; }
          mLabel = tm[1];
        } else {
          if (mCaption !== null) { valid = false; break; }
          const cap = tm[2].trim();
          if (!cap) { valid = false; break; }
          mCaption = cap;
        }
        last = tm.index + tm[0].length;
      }
      if (valid && head.slice(last).trim()) valid = false;
      if (!valid) return match; // unbekanntes Token → 1b rendert als Code-Block
    }
    mermaidBlocks.push({ diagram: diagram.trim(), label: mLabel, caption: mCaption });
    return `%%MERmaid_BLOCK_${mermaidBlocks.length - 1}%%`;
  });

  // 1b. Extract remaining fenced code blocks (``` ... ```)
  const fencedCodeBlocks = [];
  processed = _trackedReplace(processed, lines, /```([\s\S]*?)```/g, (match, content) => {
      fencedCodeBlocks.push(content);
    return `%%FC${fencedCodeBlocks.length - 1}%%`;
  });

  // 1c. Extract inline code spans (`...`)
  const inlineCodeSpans = [];
  processed = _trackedReplace(processed, lines, /`([^`]+?)`/g, (match, content) => {
    inlineCodeSpans.push(content);
    return `%%IC${inlineCodeSpans.length - 1}%%`;
  });

  // 1d. Convert callout boxes: @startbox:{typ} … @endbox — NESTBAR
  //     (beliebige Tiefe, z. B. Beweis-Box in Satz-Box; s. _convertBoxBlocks).
  //     → statischer HTML-Wrapper (marked lässt HTML-Blöcke unverändert
  //     durch, DOMPurify behält die divs). Der INHALT bleibt im Fließtext →
  //     $...$/{#fig:…}/@fig:/@box:/@task:… darin werden wie gewohnt extrahiert.
  //     Code-Blöcke sind zu diesem Zeitpunkt bereits extrahiert →
  //     in Code bleibt @startbox:… literal.
  //     Paaren (Nesten): gültiges @startbox:{typ} (Typ direkt nach dem
  //     Doppelpunkt) ÖFFNET, @endbox am Zeilenanfang SCHLIESST — die
  //     Tiefenzählung (BOX_EVENT_RE / _findBoxClose) findet den zugehörigen
  //     Schließmarker. Ein Startbox ohne passendes @endbox (oder mit
  //     ungültiger Head-Zeile) bleibt literal, die darin enthaltenen Boxen
  //     werden trotzdem konvertiert (Tippfehler fallen so auf).
  //     Tokens auf der @startbox:-Zeile (beliebige Reihenfolge, je max. einmal,
  //     nur Leerraum dazwischen, sonst bleibt die Box literal — Tippfehler
  //     fallen auf):
  //     {#box:label}: nummerierte Box ("Satz N", "Definition N", …) mit Anker
  //     — Nummer wie bei Abbildungen/Gleichungen/Code (im Skript gespeichertes
  //     Label → Skript-Nummer, in Slides: eigene Labels → S1, S2, …). In
  //     Slides ist die Nummer eines Skript-Labels klickbar (→ Skript).
  //     [Caption]: Box-Überschrift → Kopf "Typ N: Caption" ($…$ wird
  //     gerendert, s. renderCaptionMath).
  const boxLabelNumbers = {};
  const boxLabelTypes = {};
  const boxFallbackBase = chapterRef ? (chapterRef.maxBox || 0) : 0;
  let boxFallbackCount = 0;
  // Slide-Decks: slide-eigene Box-Labels → S1, S2, … (wie eq/fig/code).
  let slideBoxCount = 0;
  const boxRes = _convertBoxBlocks(processed, lines);
  processed = boxRes[0];
  lines = boxRes[1];
  // 1d2. Convert columns: @startcolumn … @nextcolumn … @endcolumn →
  //      Spalten-Grid (s. _convertColumnBlocks). Läuft NACH den Boxen:
  //      Boxen in Spalten sind dann bereits konvertiert, Spalten in Boxen
  //      (die Marker überleben die Box-Konversion als literal) werden
  //      hier im Box-Body umgewandelt.
  const colRes = _convertColumnBlocks(processed, lines);
  processed = colRes[0];
  lines = colRes[1];

  // Erste Box-Öffnung ab pos: gültiges @startbox:{typ} (Typ direkt nach dem
  // Doppelpunkt, s. BOX_EVENT_RE) → { start, type, typeEnd } oder null.
  function _nextBoxOpen(text, pos) {
    const re = /@startbox:([\p{L}0-9_-]+)/gu;
    re.lastIndex = pos;
    const m = re.exec(text);
    if (m === null) return null;
    return { start: m.index, type: m[1], typeEnd: m.index + m[0].length };
  }

  // Zur bereits geöffneten Box (Depth 1; from = Index direkt nach deren
  // Head-Zeile) passende @endbox → { start, end } oder null (ungeschlossen).
  // @endbox zählt nur am Zeilenanfang (Semantik des alten 1d-Regex \r?\n@endbox).
  function _findBoxClose(text, from) {
    BOX_EVENT_RE.lastIndex = from;
    let depth = 1;
    let m;
    while ((m = BOX_EVENT_RE.exec(text)) !== null) {
      if (m[1] !== undefined) {
        depth += 1; // verschachtelte Öffnung
        continue;
      }
      if (m.index > 0 && text[m.index - 1] !== '\n') continue; // nicht Zeilenanfang → literal
      depth -= 1;
      if (depth === 0) return { start: m.index, end: m.index + '@endbox'.length };
    }
    return null;
  }

  // Head-Zeile tokenisieren: [Caption] und {#box:label}, je max. einmal, nur
  // Leerraum zwischen/nach den Tokens (CRLF: trailing \r vorher entfernen) →
  // { valid, caption, label } (null = nicht vorhanden). Abweichung (z. B.
  // weiterer Text) → valid = false (Box bleibt literal).
  function _parseBoxHead(headLine) {
    let caption = null;
    let label = null;
    let valid = true;
    let tokEnd = 0;
    const line = headLine.replace(/\r$/, '');
    const tokRe = /\[([^\]]*)\]|\{#box:([\p{L}0-9_-]+)\}/gu;
    let bt;
    while ((bt = tokRe.exec(line)) !== null) {
      if (!/^[ \t]*$/.test(line.slice(tokEnd, bt.index))) { valid = false; break; }
      if (bt[1] !== undefined) {
        if (caption !== null) { valid = false; break; }
        caption = bt[1];
      } else {
        if (label !== null) { valid = false; break; }
        label = bt[2];
      }
      tokEnd = bt.index + bt[0].length;
    }
    if (valid && !/^[ \t]*$/.test(line.slice(tokEnd))) valid = false;
    return { valid, caption, label };
  }

  function _convertBoxBlocks(text, lines) {
    const open = _nextBoxOpen(text, 0);
    if (open === null) return [text, lines];
    const nl = text.indexOf('\n', open.typeEnd);
    if (nl === -1) return [text, lines]; // kein Ende der Head-Zeile → bleibt literal
    const close = _findBoxClose(text, nl + 1);
    const head = _parseBoxHead(text.slice(open.typeEnd, nl));
    const p = _nlPrefix(text);
    if (close === null || !head.valid) {
      // Öffnung bleibt literal (ungeschlossen/ungültige Head-Zeile); ab nach
      // der Head-Zeile weiter verarbeiten, damit verschachtelte Boxen
      // weiterhin konvertiert werden.
      const res = _convertBoxBlocks(text.slice(nl + 1), _sliceLines(text, lines, nl + 1));
      return [text.slice(0, nl + 1) + res[0], lines.slice(0, p[nl + 1]).concat(res[1])];
    }
    const type = open.type;
    const label = head.label;
    const caption = head.caption;
    let boxNum = null;
    if (label) {
      if (label in boxLabelNumbers) {
        boxNum = boxLabelNumbers[label]; // Duplikat → erstes Vorkommen gewinnt
      } else {
        const g = globalLabels['box:' + label];
        if (g && g.kind === 'box') {
          boxNum = g.num; // gespeichertes Label → exakte globale Nummer
        } else if (slideMode) {
          const sl = slidesLabels['box:' + label]; // typ-qualifiziert (s. slides-refmap)
          if (sl && sl.kind === 'box') {
            boxNum = 'S' + sl.num; // slide-eigenes Label → S-Nummer
          } else {
            slideBoxCount += 1;
            boxNum = 'S' + (slidesMaxSBox + slideBoxCount); // ungespeichert
          }
        } else {
          boxFallbackCount += 1;
          boxNum = boxFallbackBase + boxFallbackCount; // neues (ungespeichertes) Label
        }
        boxLabelNumbers[label] = boxNum;
        boxLabelTypes[label] = type;
      }
    }
    const rawContent = text.slice(nl + 1, close.start);
    const line0 = lines[p[open.start]];
    const tailRes = _convertBoxBlocks(text.slice(close.end), _sliceLines(text, lines, close.end));
    // (sourceLines) Partial-Zeile vor @startbox (wenn mid-line)
    const headPartial = (() => {
      let lo = 0;
      for (let L = 0; L < p[open.start]; L++) lo = text.indexOf('\n', lo) + 1;
      return text.slice(lo, open.start);
    })();
    let bodyStart = nl + 1;
    let wo, wc;
    if (type === 'highlight') {
      // Headless-Box (kein Kopf). Optional: ERSTE Zeile @boxcolor:<farbe>
      // → normalisierter Inline-Style (ungültig → CSS-Default, Primärfarbe);
      // die @boxcolor-Zeile wird im Match-Fall immer entfernt. Ein Label
      // ist erlaubt (Anker + Nummerierung, aber ohne sichtbaren Kopf).
      const idAttr = label ? ` id="box:${label}"` : '';
      const m = /^@boxcolor:\s*(.+?)\s*\r?\n([\s\S]*)$/u.exec(rawContent);
      let rgba = null;
      if (m) {
        rgba = _boxColorToRgba(m[1]);
        bodyStart += m[0].length - m[2].length;
      }
      const styleAttr = rgba ? ' style="background-color:' + rgba + '"' : '';
      wo = '\n\n<div class="aicampus-callbox aicampus-callbox-highlight"' + idAttr + styleAttr + '>\n\n';
      wc = '\n\n</div>\n\n';
    } else {
      const info = CALLOUT_TYPES[type] || { icon: '📄', title: type.charAt(0).toUpperCase() + type.slice(1) };
      const idAttr = label ? ` id="box:${label}"` : '';
      // Nummer im Kopf: in Slides trägt ein im Skript vorhandenes Label die
      // Skript-Nummer als Link zur Box im Skript (wie bei eq/fig/code).
      let headTitle;
      if (boxNum !== null) {
        const g = globalLabels['box:' + label];
        if (slideMode && g && g.kind === 'box') {
          const cid = (refMap && refMap.courseId) || _getCourseId() || '';
          headTitle =
            escapeHtml(info.title) +
            ` <a class="aicampus-callbox-num-link" href="/courses/${cid}/script#box:${label}"` +
            ` title="Zur Box im Skript">${boxNum}</a>`;
        } else {
          headTitle = escapeHtml(info.title) + ' ' + boxNum;
        }
      } else {
        headTitle = escapeHtml(info.title);
      }
      if (caption !== null && caption.trim() !== '') {
        headTitle += ': ' + renderCaptionMath(caption.trim());
      }
      wo = '\n\n<div class="aicampus-callbox aicampus-callbox-' + type + '"' + idAttr + '>\n' +
        '<div class="aicampus-callbox-head">' +
        '<span class="aicampus-callbox-icon" aria-hidden="true">' + info.icon + '</span> ' +
        headTitle + '</div>\n' +
        '<div class="aicampus-callbox-body">\n\n';
      wc = '\n\n</div>\n</div>\n\n';
    }
    const bodyRes = _convertBoxBlocks(
      text.slice(bodyStart, close.start),
      _sliceLines(text, lines, bodyStart, close.start)
    );
    const bodyTrimmed = bodyRes[0].trim();
    const lead = _leadTrimLines(bodyRes[0]);
    const segs = [
      { t: headPartial, f: () => line0 },
      { t: wo, f: () => line0 },
      { t: bodyTrimmed, f: (k) => bodyRes[1][k + lead] },
      { t: wc, f: () => line0 },
      { t: tailRes[0], f: (k) => tailRes[1][k] },
    ];
    return [
      text.slice(0, open.start) + wo + bodyTrimmed + wc + tailRes[0],
      lines.slice(0, p[open.start]).concat(_segLineAssign(line0, segs)),
    ];
  }

  // 1e. Extract figures (labeled + unlabeled) mit Attribut-Tokens.
  //     → labeliert {#fig:label} = nummerierte Abbildung ("Abb. N") mit Anker,
  //       latex-artig verlinkbar; unlabelt .html = Applet (Iframe); unlabeltes
  //       Bild + {.height=X} = Bild mit Max-Höhe.
  //     Nummerierung: global (kursweit) via refmap; neue (noch ungespeicherte)
  //     Labels bekommen in Slides S-Nummern (S1, S2, …) bzw. im Skript
  //     Fallback-Nummern nach der letzten bekannten des Kapitels.
  //     Attribut-Tokens nach dem Bild (in BELIEBIGER Reihenfolge, gleiche Zeile
  //     oder Zeilenumbbruch ohne Leerzeile):
  //       {#fig:label}, {#fragment}, {#fragment:id}, {#Fragment},
  //       {.height=X} (Max-Höhe in px, optionaler Suffix "px"),
  //       {.zoom=X} (Iframes: Zoom-Faktor des Applet-Inhalts),
  //       {autoplay=…}/{controls=…}/{loop=…}/{muted=…} (true/false, führender
  //       Punkt optional — NUR bei internen Videos, s. isVideoSrc).
  //     height/zoom wirken in Slides UND Skript. Unlabelte Medien nehmen
  //     optional ihre Attribute an (Applet: {.zoom=X} und/oder {.height=X},
  //     Bild: nur {.height=X}, Video: {.height=X} + Playback-Attribute);
  //     ohne Attribut bleibt ein Applet ein normales Iframe. Medien mit
  //     Alt-Text bekommen diesen als Caption unter sich angezeigt
  //     (unlabelt: ohne Nummer; labelt: „Abb. N: Caption").
  //     Alles andere (auch unbekannte Tokens) → Match wird abgebrochen, der
  //     Text bleibt literal (Tippfehler fallen so auf).
  const figures = [];
  const appletFigures = [];
  const plainFigures = [];
  const subfigures = [];
  const figLabelNumbers = {};
  // Gelabelte Subfigure-Inners des DOKUMENTS: Label → { num, letter }
  // (num = Nummer des zugehörigen Komplexes, letter = Position des Inners im
  // Komplex; "erstes Vorkommen gewinnt").
  const figInnerLocal = {};
  const figFallbackBase = chapterRef ? (chapterRef.maxFig || 0) : 0;
  let figFallbackCount = 0;
  // Unlabeled-but-numbered Subfigure-Komplexe (gelabelte Inners, kein
  // äußeres Label): Nummer NICHT aus maxFig+Count (das wäre die Kapitel-Max
  // statt der Dokument-Position) → aus der Server-Liste per Positions-Zip in
  // Vorkommensreihenfolge. Skript: refMap.figures, gefiltert auf dieses
  // Kapitel + label==null. Slides: slidesRefMap.figures (nur unlabeled
  // Komplexe), gefiltert auf die exakte Folien-Position (deckId/h/v/p —
  // p = Teil-Index, s. renderSlideInto; der Renderer kennt nur EINEN Teil).
  // Fallback (maxFig+Count bzw. maxSFig+Count) nur für neue, noch
  // ungespeicherte Komplexe.
  const figUnlabeledNums = [];
  if (!slideMode && refMap && Array.isArray(refMap.figures) && sectionId != null) {
    for (const f of refMap.figures) {
      if (String(f.sectionId) === String(sectionId) && f.label == null) {
        figUnlabeledNums.push(f.num);
      }
    }
  } else if (slideMode && slidePos && slidesRefMap && Array.isArray(slidesRefMap.figures)) {
    for (const f of slidesRefMap.figures) {
      if (
        String(f.deckId) === String(slidePos.deckId) &&
        f.h === slidePos.h && f.v === slidePos.v && f.p === slidePos.p
      ) {
        figUnlabeledNums.push(f.num);
      }
    }
  }
  let figUnlabeledIdx = 0;
  // Slide-Decks: eigene Labels (nicht im Skript enthalten) bekommen eigene
  // Nummerierung (S1), (S2), … — abgesetzt von der Skript-Nummerierung
  // (wie bei den Formeln, s. 1g).
  let slideFigCount = 0;
  // Ein Attribut-Block: {…} direkt hinter der Referenz, nur Leerraum oder
  // ein Zeilenumbruch (ohne Leerzeile) dazwischen.
  const ATTR_BLOCK = /^[ \t]*(?:\r?\n[ \t]*)?\{([^{}]*)\}/;
  // Figuren-Nummer eines Labels (einfache Figuren s. 1e UND Subfigure-
  // Komplexe s. 1e0): gespeichertes Label → exakte globale Nummer;
  // slide-eigenes Label → S-Nummer; ungespeichert → Fallback nach der
  // letzten bekannten des Kapitels. Duplikat → erstes Vorkommen gewinnt.
  const figNumberForLabel = (label) => {
    if (label in figLabelNumbers) return figLabelNumbers[label];
    const g = globalLabels['fig:' + label];
    let num;
    if (g && g.kind === 'fig') {
      num = g.num; // gespeichertes Label → exakte globale Nummer
    } else if (slideMode) {
      const sl = slidesLabels['fig:' + label]; // typ-qualifiziert (s. slides-refmap)
      if (sl && sl.kind === 'fig') {
        num = 'S' + sl.num; // slide-eigenes Label → S-Nummer aus der Slide-Ref-Map
      } else {
        slideFigCount += 1;
        num = 'S' + (slidesMaxSFig + slideFigCount); // ungespeichert → weiterzählen
      }
    } else {
      figFallbackCount += 1;
      num = figFallbackBase + figFallbackCount; // neues (ungespeichertes) Label
    }
    figLabelNumbers[label] = num;
    return num;
  };
  // Zwei einfache Regexes statt einem verschachtelten Monster:
  const IMG_REF = /!\[([^\]]*)\]\(([^)\s]+)\)/g;
  // Literale Tails (unbekannte/ungültige Tokens, schlichte <img>): werden
  // vor den Folgeschritten (1f–1k) aus dem Text genommen, damit z. B.
  // {#fragment} im Tail nicht als echtes Fragment-Sentinel interpretiert
  // wird, und direkt vor marked.parse (Step 2) wiederhergestellt.
  const imgLiteralTails = [];
  // 1e0. Subfigure-Komplexe:
  //       ![Gesamt](![Caption 1](u1){height=300}{#fig:teil1} ![Caption 2](u2){#fig:teil2}){#fig:label}
  //      → EINE (gelabelt =) nummerierte Abbildung ("Abb. N: Gesamt") mit
  //      mehreren Medien in einer Flex-Zeile (jedes mit eigener Caption),
  //      s. Restore-Step 6a4. Sobald mindestens ein Inner ein {#fig:label}
  //      trägt, wird der KOMPLEXX nummeriert — auch ohne eigenes
  //      {#fig:label} — und ALLE Inners bekommen a), b), c), … (s. 6a4);
  //      ohne jegliches Label bleibt er unnummeriert.
  //      VOR dem IMG_REF-Loop maskieren (Platzhalter), damit die inneren
  //      ![…](…) nicht als Einzelabbildungen gezählt/gerendert werden.
  //      Inneren sind NUR {height=X} und/oder ein {#fig:label} erlaubt
  //      (je max. einmal, beliebige Reihenfolge; keine Fragments). Die
  //      Server-Regex (api/script.py) ist exakt so streng, damit beide
  //      Seiten dieselben Komplexe erkennen (Ref-Map ↔ Nummerierung).
  //      Ungültig (andere Tokens, <2 Innere, …) → bleibt literal
  //      (Tippfehler fallen auf); die Innere werden dann ggf. als normale
  //      Bilder geparst (s. Guard im IMG_REF-Loop).
  {
    // ACHTUNG: `!` in src ausschließen ([^)\s!]+) — sonst matcht der äußere
    // Komplex-Kopf ![Gesamt]( auf das erste INNER als src (s. _SF_INNER).
    const SUBFIG_INNER_SRC =
      /!\[[^\]]*\]\([^)\s!]+\)(?:[ \t]*(?:\r?\n[ \t]*)?\{[^{}]*\}){0,6}/.source;
    const SUBFIG_RE = new RegExp(
      String.raw`!\[([^\]]*)\]\([ \t]*(?:\r?\n[ \t]*)?` +
        String.raw`(` + SUBFIG_INNER_SRC + String.raw`(?:\s+` + SUBFIG_INNER_SRC + String.raw`)+)` +
        String.raw`[ \t]*(?:\r?\n[ \t]*)?\)`,
      'g'
    );
    let out = '';
    let last = 0;
    let m;
    const pend = [];
    while ((m = SUBFIG_RE.exec(processed)) !== null) {
      const inners = _parseSubfigInners(m[2]);
      if (inners === null) continue; // ungültig → literal (Regex rückt selbst weiter)
      // Attribut-Tokens des Komplexes hinter dem schließenden ) — wie bei
      // Einzelabbildungen (s. 1e): {#fig:label}, {#fragment…}, {#aaid:…},
      // {height=X}; je Token max. einmal, sonst bleibt der Komplex literal.
      const tokens = [];
      let pos = m.index + m[0].length;
      for (;;) {
        const bm = ATTR_BLOCK.exec(processed.slice(pos));
        if (!bm) break;
        tokens.push(bm[1].trim());
        pos += bm[0].length;
      }
      const a = { label: null, frag: null, height: null, aaid: null };
      let valid = true;
      for (const inner of tokens) {
        let t;
        if ((t = inner.match(/^#fig:([\p{L}0-9_-]+)$/u))) {
          if (a.label !== null) { valid = false; break; }
          a.label = t[1];
        } else if ((t = inner.match(/^#([Ff])ragment(?::([\p{L}0-9_-]+))?$/u))) {
          if (a.frag) { valid = false; break; }
          a.frag = { type: _fragType(t[1], t[2]), id: t[2] || null };
        } else if ((t = inner.match(/^#aaid:([\p{L}0-9_-]+)$/u))) {
          if (a.aaid !== null) { valid = false; break; }
          a.aaid = t[1];
        } else if ((t = inner.match(/^\.?height=([\d.]+)([a-z]*)$/))) {
          if (t[2] !== '' && t[2] !== 'px') { valid = false; break; }
          if (a.height !== null) { valid = false; break; }
          a.height = parseFloat(t[1]); // px
        } else {
          valid = false; break;
        }
      }
      if (!valid) continue;
      // Nummer: eigenes {#fig:label} → wie andere Figuren (s. figNumberForLabel);
      // sonst: gelabelte Inners → Komplex-Nummer (Fallback-/S-Zählung),
      // sonst gar keine Nummer.
      const hasInnerLabel = inners.some((x) => x.label !== null);
      const num = a.label !== null
        ? figNumberForLabel(a.label)
        : hasInnerLabel
          ? // Gespeicherte unlabeled Komplexe: exakte Server-Nummer per
            // Positions-Zip (s. figUnlabeledNums); ungespeichert: Fallback
            // (Skript: nach Kapitel-Max, Slides: nach maxSFig weiterzählen).
            (slideMode
              ? 'S' + (figUnlabeledNums[figUnlabeledIdx++] ?? (slidesMaxSFig + ++slideFigCount))
              : figUnlabeledNums[figUnlabeledIdx++] ?? (figFallbackBase + ++figFallbackCount))
          : null;
      subfigures.push({
        alt: m[1],
        inners,
        label: a.label,
        num,
        frag: a.frag,
        height: a.height,
        aaid: a.aaid,
      });
      inners.forEach((inner, i) => {
        if (inner.label && !(inner.label in figInnerLocal)) {
          figInnerLocal[inner.label] = { num, letter: String.fromCharCode(97 + i) };
        }
      });
      out += processed.slice(last, m.index) + `%%SUBFIG_${subfigures.length - 1}%%`;
      pend.push([m.index, pos, `%%SUBFIG_${subfigures.length - 1}%%`]);
      last = pos;
      SUBFIG_RE.lastIndex = pos;
    }
    if (subfigures.length) {
      const prefix = _nlPrefix(processed);
      for (let i = pend.length - 1; i >= 0; i--) _replLines(lines, prefix, pend[i][0], pend[i][1], pend[i][2]);
      processed = out + processed.slice(last);
    }
  }
  {
    let out = '';
    let last = 0;
    let m;
    const prefix1 = _nlPrefix(processed);
    const pend = [];
    while ((m = IMG_REF.exec(processed)) !== null) {
      const alt = m[1];
      const src = m[2];
      // Subfigure-Komplex, der in 1e0 NICHT maskiert wurde (ungültig →
      // literal): als "Quelle" wäre nur der Fragment "![a" gültig →
      // abfangen: der äußere Kopf "![Alt](" bleibt literal, ab hier (dem
      // inneren "![…](…)"-Snippet) wird normal weitergeparst (die Innere
      // werden dann ggf. normale Bilder — der Komplex degradiert sichtbar).
      if (src.startsWith('!')) {
        const innerStart = m.index + alt.length + 4; // "![<alt>(" = 2 + Alt + 2
        out += processed.slice(last, innerStart);
        last = innerStart;
        IMG_REF.lastIndex = innerStart;
        continue;
      }
      // Aufeinanderfolgende Attribut-Blöcke hinter der Referenz konsumieren.
      const tokens = [];
      let pos = m.index + m[0].length;
      for (;;) {
        const bm = ATTR_BLOCK.exec(processed.slice(pos));
        if (!bm) break;
        tokens.push(bm[1].trim());
        pos += bm[0].length;
      }
      // Tokens validieren: nur bekannte Typen, je Attribut höchstens einmal.
      const a = { label: null, frag: null, height: null, zoom: null, aaid: null,
                  autoplay: null, controls: null, loop: null, muted: null };
      let valid = true;
      for (const inner of tokens) {
        let t;
        if ((t = inner.match(/^#fig:([\p{L}0-9_-]+)$/u))) {
          if (a.label !== null) { valid = false; break; } // Duplikat-Label
          a.label = t[1];
        } else if ((t = inner.match(/^#([Ff])ragment(?::([\p{L}0-9_-]+))?$/u))) {
          if (a.frag) { valid = false; break; } // Duplikat-Fragment
          a.frag = { type: _fragType(t[1], t[2]), id: t[2] || null };
        } else if ((t = inner.match(/^#aaid:([\p{L}0-9_-]+)$/u))) {
          // Auto-Animate-Element-ID (nur Slides; Reveal 4.6: data-id)
          if (a.aaid !== null) { valid = false; break; } // Duplikat
          a.aaid = t[1];
        } else if ((t = inner.match(/^\.?height=([\d.]+)([a-z]*)$/))) {
          // Führender Punkt optional (beide Formen valid): {height=X} /
          // {.height=X}. Suffix-Check separat (einfacher als `px?` im Regex
          // selbst — robust gegenüber V8/Chrome-Regel-3-Veränderungen):
          // erlaubt: kein Suffix oder genau "px".
          if (t[2] !== '' && t[2] !== 'px') { valid = false; break; }
          if (a.height !== null) { valid = false; break; } // Duplikat
          a.height = parseFloat(t[1]); // px
        } else if ((t = inner.match(/^\.?zoom=([\d.]+)$/))) {
          if (a.zoom !== null) { valid = false; break; } // Duplikat
          a.zoom = parseFloat(t[1]);
        } else if (isVideoSrc(src) &&
                   (t = inner.match(/^\.?(autoplay|controls|loop|muted)=(true|false)$/i))) {
          // Playback-Attribute nur für interne Videos (sonst = unbekanntes
          // Token → literal, Tippfehler fallen auf).
          if (a[t[1]] !== null) { valid = false; break; } // Duplikat
          a[t[1]] = t[2].toLowerCase() === 'true';
        } else {
          valid = false; break; // unbekanntes Attribut → literal
        }
      }
      // Unlabelt: Applet nimmt (k)ein {.zoom=X} und/oder (k)ein {.height=X},
      // Bild (k)ein {.height=X} (Zoom nur für Applets). Medien mit Alt-Text
      // bekommen ihre Caption unter sich (unlabelt ohne Nummer).
      // Ungültige Kombinationen bleiben literal (Tippfehler fallen auf).
      let repl = null;
      if (valid) {
        if (a.label !== null) {
          const num = figNumberForLabel(a.label);
          figures.push({ alt, src, label: a.label, num, frag: a.frag, height: a.height, zoom: a.zoom, aaid: a.aaid,
                         autoplay: a.autoplay, controls: a.controls, loop: a.loop, muted: a.muted });
          repl = `%%FIG_${figures.length - 1}%%`;
        } else if (isAppletSrc(src) && !a.frag) {
          appletFigures.push({ alt, src, zoom: a.zoom, height: a.height });
          repl = `%%APPLETFIG_${appletFigures.length - 1}%%`;
        } else if (!isAppletSrc(src) && !a.frag && a.zoom === null &&
                   (a.height !== null || alt.trim() !== '' || isVideoSrc(src))) {
          // Bild mit Max-Höhe und/oder Caption (= Alt-Text, ohne Nummer).
          // Ohne Attribut UND ohne Alt-Text bleibt das Bild literal
          // (normales marked-<img>), wie bisher — Videos brauchen immer
          // einen Render-Pfad (<video>, kein <img>).
          plainFigures.push({ alt, src, height: a.height,
                              autoplay: a.autoplay, controls: a.controls, loop: a.loop, muted: a.muted });
          repl = `%%PLAINFIG_${plainFigures.length - 1}%%`;
        }
      }
      // repl = null (inkl. Bild ohne Attribute) → Referenz + Tail bleiben
      // literal (per Platzhalter, s. imgLiteralTails); assign = Zeilen der
      // Tail-Zeilen (für das 1l-Restore).
      const litIdx = repl === null
        ? imgLiteralTails.push({
            text: processed.slice(m.index, pos),
            assign: lines.slice(prefix1[m.index], prefix1[pos - 1] + 1),
          }) - 1
        : null;
      const litRepl = repl !== null ? repl : `%%IMGLIT_${litIdx}%%`;
      out += processed.slice(last, m.index) + litRepl;
      pend.push([m.index, pos, litRepl]);
      last = pos;
      IMG_REF.lastIndex = pos;
    }
    {
      const prefix = _nlPrefix(processed);
      for (let i = pend.length - 1; i >= 0; i--) _replLines(lines, prefix, pend[i][0], pend[i][1], pend[i][2]);
    }
    processed = out + processed.slice(last);
  }

  // 1f. Handle escaped dollar signs: \$ → placeholder
  const escapedDollar = '%%ED%%';
  processed = _trackedReplace(processed, lines, /\\\$/g, () => escapedDollar);

  // 1g. Extract $$...$$ display blocks (optional trailing {#eq:label}
  //     → nummerierte Gleichung "(N)" mit Anker, latex-artig verlinkbar)
  const latexBlocks = [];
  const latexLabels = [];
  const latexAaids = [];
  const eqLabelNumbers = {};
  const eqFallbackBase = chapterRef ? (chapterRef.maxEq || 0) : 0;
  let eqFallbackCount = 0;
  // Slide-Decks: eigene Labels (nicht im Skript enthalten) bekommen eigene
  // Nummerierung (S1), (S2), … — abgesetzt von der Skript-Nummerierung.
  let slideEqCount = 0;
  const latexFragments = [];
  processed = _trackedReplace(processed, lines,
    /\$\$([\s\S]*?)\$\$(?:\s*\{#eq:([\p{L}0-9_-]+)\})?(?:\s*\{#([Ff])ragment(?::([\p{L}0-9_-]+))?\})?(?:\s*\{#aaid:([\p{L}0-9_-]+)\})?/gu,
    (match, latex, label, fchar, id, aaid) => {
      latexBlocks.push(latex.trim());
      if (label && !(label in eqLabelNumbers)) {
        const g = globalLabels['eq:' + label];
        if (g && g.kind === 'eq') {
          eqLabelNumbers[label] = g.num; // gespeichertes Label → exakte globale Nummer
        } else if (slideMode) {
          const sl = slidesLabels['eq:' + label]; // typ-qualifiziert (s. slides-refmap)
          if (sl && sl.kind === 'eq') {
            eqLabelNumbers[label] = 'S' + sl.num; // slide-eigenes Label → S-Nummer
          } else {
            slideEqCount += 1;
            eqLabelNumbers[label] = 'S' + (slidesMaxSEq + slideEqCount); // ungespeichert
          }
        } else {
          eqFallbackCount += 1;
          eqLabelNumbers[label] = eqFallbackBase + eqFallbackCount; // neues (ungespeichertes) Label
        }
      }
      latexLabels.push(label || null);
      latexFragments.push(fchar !== undefined ? { type: _fragType(fchar, id), id: id || null } : null);
      latexAaids.push(aaid || null);
      return `%%LATEX_BLOCK_${latexBlocks.length - 1}%%`;
    }
  );

  // 1h. Extract $...$ inline math (no newlines allowed)
  const latexInlines = [];
  processed = _trackedReplace(processed, lines, /\$([^$\n]+?)\$/g, (match, latex) => {
    latexInlines.push(latex.trim());
    return `%%LATEX_INLINE_${latexInlines.length - 1}%%`;
  });

  // 1i. Extract cross-references: @fig:label / @eq:label / @code:label / @box:label / @tab:label / @sec:label / @kap:label
  //     + Zitationen @cite:{key} / @citet:{key} / @citep:{key} (BibTeX-Keys; längere
  //     Alternativen zuerst, damit @citet:/@citep: nicht zu @cite: verkürzt werden)
  //     + @bibentry:{key} = komplette Quellenangabe (auto-Quellen-Folie der Slide-Decks)
  // Altes Quellen-Folie-Format aus bereits generierten Decks "[@cite:key] Autoren …":
  // der manuell ausformulierte Rest der Zeile ist redundant (die Angaben rendert
  // jetzt der Renderer im Design des Skript-Quellenverzeichnisses) → in
  // @bibentry:{key} umschreiben; damit verschwinden auch die Links auf den für
  // Studenten nicht erreichbaren Quellen-Tab.
  processed = _trackedReplace(processed, lines, /\[@cite:([\p{L}0-9_-]+)\][^\n]*/gu, (m, key) => '@bibentry:' + key);
  const xrefs = [];
  processed = _trackedReplace(processed, lines, /@(fig|eq|sec|kap|code|box|tab|bibentry|citep|citet|cite):([\p{L}0-9_-]+)/gu, (match, kind, label) => {
    xrefs.push({ kind, label });
    return `%%XREF_${xrefs.length - 1}%%`;
  });

  // 1j. Extract task references: @task:{id}
  //     → Aufgaben-Box (Daten via refmap.tasks; Code-Blöcke sind zu
  //     diesem Zeitpunkt bereits extrahiert → in Code bleibt es literal).
  const taskRefs = [];
  processed = _trackedReplace(processed, lines, /@task:(\d+)/g, (match, id) => {
    taskRefs.push(id);
    return `%%TASKREF_${taskRefs.length - 1}%%`;
  });

  // 1j2. Extract labeled tables: Pipe-Tabellen-Block + Label-Zeile
  //      {#tab:label} (optional mit [caption], optional mit {zoom=X})
  //      direkt darunter → nummerierte Tabelle ("Tab. N: caption") mit
  //      Anker — Nummer wie bei Abbildungen/Gleichungen/Code/Boxen (im
  //      Skript gespeichertes Label → Skript-Nummer, in Slides: eigene
  //      Labels → S1, S2, …). {zoom=X} (führender Punkt optional) skaliert
  //      die Tabellenschrift (×X, Slides UND Skript) per --tab-zoom an der
  //      Figure (s. 4b + CSS: main.css/slides.css).
  //      Der Tabellen-Markdown-Text wird beim Restore per marked.parse
  //      gerendert; darin enthaltene Platzhalter (%%IC%%/%%LATEX_*%%/
  //      %%XREF%%/%%TASKREF%% — Extraktion erfolgte vor diesem Schritt)
  //      füllen die nachfolgenden Restore-Schritte in der Tabelle.
  //      Unlabelte Tabellen bleiben im Fließtext (natives GFM-Table).
  //      Ungültige Schreibweise (Text nach der Caption, Label ohne
  //      Tabelle) → bleibt literal (Tippfehler fallen so auf).
  const tables = [];
  const tabLabelNumbers = {};
  const tabFallbackBase = chapterRef ? (chapterRef.maxTab || 0) : 0;
  let tabFallbackCount = 0;
  // Slide-Decks: slide-eigene Tabellen-Labels → S1, S2, … (wie eq/fig/code).
  let slideTabCount = 0;
  // Regex-Ende als Lookahead (kein \n-Konsum!): würde der Match das \n der
  // Label-Zeile fressen, merged die nächste Zeile mit der Placeholder-Zeile
  // und lines[] behält eine tote Entry → alle folgenden Zeilen um 1 falsch.
  processed = _trackedReplace(processed, lines,
    /((?:^[ \t]*\|[^\n]*\r?\n)+)[ \t]*(?:\r?\n[ \t]*)*\{#tab:([\p{L}0-9_-]+)\}(?:[ \t]*\[([^\]]*)\])?(?:[ \t]*\{\.?zoom=([\d.]+)\})?[ \t]*(?=\r?\n|$)/gmu,
    (match, tableMd, label, caption, zoom, off, src) => {
      let num;
      if (label in tabLabelNumbers) {
        num = tabLabelNumbers[label]; // Duplikat → erstes Vorkommen gewinnt
      } else {
        const g = globalLabels['tab:' + label];
        if (g && g.kind === 'tab') {
          num = g.num; // gespeichertes Label → exakte globale Nummer
        } else if (slideMode) {
          const sl = slidesLabels['tab:' + label]; // typ-qualifiziert (s. slides-refmap)
          if (sl && sl.kind === 'tab') {
            num = 'S' + sl.num; // slide-eigenes Label → S-Nummer
          } else {
            slideTabCount += 1;
            num = 'S' + (slidesMaxSTab + slideTabCount); // ungespeichert
          }
        } else {
          tabFallbackCount += 1;
          num = tabFallbackBase + tabFallbackCount; // neues (ungespeichertes) Label
        }
        tabLabelNumbers[label] = num;
      }
      // (sourceLines) Original-Zeilen der Tabellen-Zeilen JETZT erfassen:
      // der _trackedReplace-Splice ersetzt die ganze Match-Region durch
      // EINE Pipeline-Zeile — das per-Zeile-Mapping wäre danach weg.
      // l0 = Pipeline-Zeile der Kopfzeile (Match startet an Zeilenanfang).
      const l0 = src.slice(0, off).split('\n').length - 1;
      const nRows = tableMd.split('\n').length - 1; // md endet mit \n
      tables.push({
        md: tableMd,
        label,
        caption: caption || '',
        num,
        zoom: zoom !== undefined ? parseFloat(zoom) : null, // Schrift ×X (s. 4b)
        rows: lines.slice(l0, l0 + nRows),
      });
      return `%%TAB_${tables.length - 1}%%`;
    }
  );

  // 1k. Slide-Fragments → unsichtbarer Sentinel (Typ/ID als data-Attribute,
  //     überleben die DOMPurify-Sanitize). Formen (Details:
  //     _applyFragmentMarkers am Ende von renderMarkdown):
  //       {#fragment}      → normales Fragment (eigener Einblend-Schritt)
  //       {#fragment:id}   → ID-Gruppe (gleiche ID → gleicher Schritt)
  //       {#Fragment}      → Gate (Groß, ohne ID): Element + alle FOLGENDEN
  //                           Inhalte der Folie erscheinen in einem Schritt
  //     eq/fig haben ihren Marker oben bereits selbst extrahiert.
  if (slideMode) {
    processed = _trackedReplace(processed, lines,
      /\{#([Ff])ragment(?::([\p{L}0-9_-]+))?\}/gu,
      (m, fchar, id) => {
        const idAttr = id ? ` data-frag-id="${id}"` : '';
        return `<span class="aicampus-frag-marker" data-frag="${_fragType(fchar, id)}"${idAttr}></span>`;
      }
    );
    // {#aaid:label} → Sentinel: _applyFragmentMarkers setzt data-id auf das
    // umgebende Block-Element (Reveal 4.6: explizites Auto-Animate-Element-
    // Matching per data-id; in Reveal 5 heißt das Attribut
    // data-auto-animate-id). eq/fig nehmen {#aaid:…} oben bereits direkt.
    processed = _trackedReplace(processed, lines,
      /\{#aaid:([\p{L}0-9_-]+)\}/gu,
      (m, label) => `<span class="aicampus-aaid-marker" data-aaid="${label}"></span>`
    );
  }

  // 1l. Literal-Image-Tails wiederherstellen (nach 1k, damit enthaltene
  //     {#fragment}-Token als sichtbarer Text bleiben und KEIN Sentinel werden)
  imgLiteralTails.forEach((item, idx) => {
    const ph = `%%IMGLIT_${idx}%%`;
    const off = processed.indexOf(ph);
    if (off === -1) return;
    const prefix = _nlPrefix(processed);
    lines.splice(prefix[off], 1, ...item.assign);
    processed = processed.slice(0, off) + item.text + processed.slice(off + ph.length);
  });

  // 2. Render Markdown (marked) — bei sourceLines erst Zeilen-Marker
  //    einfügen (1m), damit am Ende data-src-line gesetzt werden kann.
  let html = marked.parse(sourceLines ? _insertSrcLineMarkers(processed, lines, lineOffset, tables) : processed, {
    breaks,
    gfm: true,
    headerIds: false,
    mangle: false,
  });

  // 3. Restore fenced code blocks
  //    Öffnende Zeile (parseFenceHead): optionale Sprache + optionale
  //    Tokens in beliebiger Reihenfolge, nahtlos oder mit Leerzeichen:
  //    {#lines:…} (Slides: Zeilennummern + Per-Line-Reveal, Reveal-
  //    Highlight-Plugin; "|" = weiterer Schritt),
  //    {#aaid:label} (Slides: Auto-Animate-ID → <pre data-id=…>; Blöcke
  //    mit demselben Label auf aufeinanderfolgenden Folien animiert Reveal
  //    nativ per Zeile ineinander, s. Reveal-Code-Beispiel),
  //    {#code:label} (+ optionales [caption] direkt danach, ohne
  //    Leerzeichen): nummerierter Code-Block ("Code N") mit Anker —
  //    Nummer wie bei Abbildungen/Formeln (im Skript gespeichertes Label →
  //    Skript-Nummer, in Slides: eigene Labels → S1, S2, …); in Slides ist
  //    die Nummer klickbar (→ Skript). Beschriftete Blöcke stehen in einer
  //    „Codes-Tabelle“ im Inhaltsverzeichnis (via refmap).
  //    {.zoom=X} / {.height=Y} (Schriftgröße / Max-Höhe + internes
  //    Scrollen; Slides UND Skript — Inline-Styles am <pre>, die auch von
  //    den hljs-Clones geerbt werden).
  //    Unbekanntes Token/Duplikat → Fallback: die erste Zeile ist Sprache
  //    NUR wenn rein (sonst Code-Teil).
  const codeLabelNumbers = {};
  const codeFallbackBase = chapterRef ? (chapterRef.maxCode || 0) : 0;
  let codeFallbackCount = 0;
  // Slide-Decks: slide-eigene Code-Labels → S1, S2, … (wie eq/fig).
  let slideCodeCount = 0;
  fencedCodeBlocks.forEach((content, idx) => {
    const safe = content.replace(escapedDollar, '$');
    const lines = safe.split('\n');
    let language = '';
    let lineNumbers = null;
    let blockAaid = null;
    let codeZoom = null;
    let codeHeight = null;
    let codeLabel = null;
    let codeCaption = null;
    let codeNum = null;
    let codeBody;
    if (lines.length > 1) {
      const parsed = parseFenceHead(lines[0].trim());
      if (parsed) {
        language = parsed.language;
        lineNumbers = parsed.lines;
        blockAaid = parsed.aaid;
        codeZoom = parsed.zoom;
        codeHeight = parsed.height;
        codeLabel = parsed.codeLabel;
        codeCaption = parsed.caption;
        codeBody = lines.slice(1).join('\n');
      }
    }
    if (codeBody === undefined) codeBody = safe;
    const escaped = codeBody.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\$\$/g, '$$$$$$$$');
    const langAttr = language ? ` class="language-${language}"` : '';
    // {#aaid} ohne {#lines}: bares data-line-numbers → das Reveal-Highlight-
    // Plugin baut die hljs-ln-Tabelle OHNE Highlight-Schritte — die
    // .hljs-ln-code-Zellen braucht das native AutoAnimate zum Zeilen-Matching.
    const linesAttr = lineNumbers
      ? ` data-line-numbers="${escapeHtml(lineNumbers.trim())}"`
      : (blockAaid ? ' data-line-numbers' : '');
    const aaidAttr = blockAaid ? ` data-id="${blockAaid}"` : '';
    // {.zoom=X} → font-size in em (Text bleibt scharf; das Theme-Padding
    // 1em am code skaliert proportional mit), {.height=Y} → max-height +
    // internes Scrollen (die hljs-Clones sind absolute Kinder → sie
    // scrollen MIT dem Container, das Zeilen-Alignement bleibt erhalten).
    // !important: muss auch die !important-Regeln von main.css (Skript-
    // Ansicht/Thumbnails: pre font-size 0.825rem) schlagen — Inline-
    // !important gewinnt gegen Stylesheet-!important.
    let preStyle = '';
    if (codeZoom !== null) {
      preStyle += `font-size:${codeZoom}em !important;`;
      // --tz: Default-Max-Höhe (9em, slides.css) wird durch den Zoom-Faktor
      // geteilt → sichtbare Default-Höhe identisch zum zoomlosen Default
      // (exakt wie bei gezoomten Applets, s. dort).
      preStyle += `--tz:${codeZoom};`;
    }
    if (codeHeight !== null) preStyle += `max-height:${codeHeight}px !important;overflow:auto !important;`;
    const preStyleAttr = preStyle ? ` style="${preStyle}"` : '';
    let blockHtml = `<pre${aaidAttr}${preStyleAttr}><code${langAttr}${linesAttr}>${escaped}</code></pre>`;
    // {#code:label} und/oder [caption] → Code-Figur mit Caption
    // ("Code N: caption"), wie bei Abbildungen.
    if (codeLabel !== null || codeCaption !== null) {
      if (codeLabel !== null) {
        if (codeLabel in codeLabelNumbers) {
          codeNum = codeLabelNumbers[codeLabel]; // Duplikat → erstes Vorkommen
        } else {
          const g = globalLabels['code:' + codeLabel];
          if (g && g.kind === 'code') {
            codeNum = g.num; // gespeichertes Label → exakte globale Nummer
          } else if (slideMode) {
            const sl = slidesLabels['code:' + codeLabel]; // typ-qualifiziert (s. slides-refmap)
            if (sl && sl.kind === 'code') {
              codeNum = 'S' + sl.num; // slide-eigenes Label → S-Nummer
            } else {
              slideCodeCount += 1;
              codeNum = 'S' + (slidesMaxSCode + slideCodeCount); // ungespeichert
            }
          } else {
            codeFallbackCount += 1;
            codeNum = codeFallbackBase + codeFallbackCount;
          }
          codeLabelNumbers[codeLabel] = codeNum;
        }
      }
      let numHtml = '';
      if (codeNum !== null) {
        const g = globalLabels['code:' + codeLabel];
        if (slideMode && g && g.kind === 'code') {
          const cid = (refMap && refMap.courseId) || _getCourseId() || '';
          numHtml =
            `<a class="aicampus-code-num-link" href="/courses/${cid}/script#code:${codeLabel}"` +
            ` title="Zum Code im Skript">Code ${codeNum}</a>`;
        } else {
          numHtml = `Code ${codeNum}`;
        }
      }
      const capParts = [];
      if (numHtml) capParts.push(numHtml);
      if (codeCaption) capParts.push(renderCaptionMath(codeCaption));
      const idAttr = codeLabel !== null ? ` id="code:${codeLabel}"` : '';
      blockHtml = `<figure class="aicampus-code-figure"${idAttr}>${blockHtml}` +
        `<figcaption>${capParts.join(': ')}</figcaption></figure>`;
    }
    html = html.replace(`%%FC${idx}%%`, blockHtml.replace(/\$/g, '$$$$'));
  });

  // 4. Restore inline code spans
  inlineCodeSpans.forEach((content, idx) => {
    const safe = content.replace(escapedDollar, '$');
    const escaped = safe.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\$\$/g, '$$$$$$$$');
    html = html.replace(`%%IC${idx}%%`, `<code>${escaped}</code>`);
  });

  // 4b. Restore labeled tables: Tabellen-Markdown per marked.parse → <table>,
  //     in <figure class="aicampus-table-figure"> mit Caption "Tab. N: caption"
  //     + Anker tab:label (wie Abbildungen/Code). MUST vor Schritt 6
  //     (Inline-Math) stehen: die Zell-Platzhalter (%%LATEX_INLINE%%/
  //     %%XREF%%/%%TASKREF%%) füllen die nachfolgenden html.replace direkt
  //     in der Tabelle. Nummer: im Skript gespeichertes Label → in Slides
  //     klickbar (→ Skript), wie bei eq/fig/code.
  tables.forEach((t, idx) => {
    // Bild-Literal-Tails, die IN der Tabelle liegen, hier mit-restaurieren
    // (1l hat nur den Haupttext behandelt — die Tabelle ist zu dem Zeitpunkt
    // bereits extrahiert).
    let tableMd = t.md;
    imgLiteralTails.forEach((item, i) => {
      tableMd = tableMd.split(`%%IMGLIT_${i}%%`).join(item.text);
    });
    const tableHtml = marked.parse(tableMd, { breaks: false, gfm: true, headerIds: false, mangle: false });
    const g = globalLabels['tab:' + t.label];
    const numHtml = (slideMode && g && g.kind === 'tab')
      ? (() => {
        const cid = (refMap && refMap.courseId) || _getCourseId() || '';
        return `<a class="aicampus-tab-num-link" href="/courses/${cid}/script#tab:${t.label}" title="Zur Tabelle im Skript">Tab. ${t.num}</a>`;
      })()
      : `Tab. ${t.num}`;
    // {zoom=X}: --tab-zoom an der Figure (vererbt die Tabelle) → Schrift ×X
    // (CSS: calc(… * var(--tab-zoom, 1)) in main.css/slides.css); die
    // Caption (figcaption) bleibt in Normalgröße, wie beim Code-Zoom.
    const zoomAttr = t.zoom != null ? ` style="--tab-zoom: ${t.zoom}"` : '';
    const figHtml =
      `<figure id="tab:${t.label}" class="aicampus-table-figure"${zoomAttr}>${tableHtml}` +
      `<figcaption>${numHtml}${t.caption ? `: ${renderCaptionMath(t.caption)}` : ''}</figcaption></figure>`;
    html = html.replace(`%%TAB_${idx}%%`, figHtml.replace(/\$/g, '$$$$'));
  });

  // Xref-/Zitations-Tokens IN Formeln (@eq:/@cite:/@citet:/@citep:) →
  // KaTeX-\href-Links (aufrecht, klickbar) — erst hier, im Restore, denn
  // eqLabelNumbers/Ref-Maps sind zu Extraktionszeit (1g/1h) noch unvollständig
  // (Vorwärtsreferenzen!). Auflösung spiegelt den Fließtext (6b): eq lokal
  // (nur Skript) → Skript-Ref-Map → Slide-Ref-Map; Zitationen = kursweite
  // Nummer + dieselbe href-Regel wie 6b. Zitierte Einträge landen zusätzlich
  // in citedRefs (→ Quellenliste / auto-Quellen-Folie). IM MATH-MODUS wird
  // das Link in \text{…} verpackt (gültiges aufrechtes Atom, auch in
  // aligned/\frac); in KaTeX-Textmodi (\text/\mbox/\textrm/…) erbt das nackte
  // \href die umgebende Schrift. Unbekannte Labels → Literal
  // "? kind:label" (kein KaTeX-Error). refs = [{url, key?}] für
  // _injectMathRefKeys (key = Zitations-Schlüssel → data-refkey).
  const _mathXrefsToLatex = (latex) => {
    const refs = [];
    const out = latex.replace(
      /@(eq|citep|citet|cite):([\p{L}0-9_-]+)/gu,
      (m, kind, label, offset) => {
        const cid = (refMap && refMap.courseId) || '';
        const wrap = (s) => (_mathXrefInText(latex, offset) ? s : `\\text{${s}}`);
        if (kind === 'eq') {
          let url = null;
          let text = null;
          if (!slideMode && eqLabelNumbers[label]) {
            url = '#eq:' + label;
            text = 'Gl. ' + eqLabelNumbers[label];
          } else {
            const g = globalLabels['eq:' + label];
            const sl = slidesLabels['eq:' + label];
            if (g && g.kind === 'eq') {
              url = '/courses/' + cid + '/script#eq:' + label;
              text = 'Gl. ' + g.num;
            } else if (sl && sl.kind === 'eq') {
              const scid = (slidesRefMap && slidesRefMap.courseId) || cid;
              url = `/courses/${scid}/slides/${sl.deckId}/present#/${sl.h}/${sl.v}`;
              text = 'Gl. S' + sl.num;
            }
          }
          if (url) {
            refs.push({ url });
            return wrap(`\\href{${url}}{${_texSafeText(text)}}`);
          }
        } else {
          const r = ((refMap && refMap.references) || {})[label];
          if (r) {
            citedRefs.push(r);
            const a0 = (r.authors && r.authors.length)
              ? r.authors[0] + (r.authors.length > 3 ? ' et al.' : '')
              : '';
            const y = r.year || '';
            const text = kind === 'cite'
              ? '[' + r.num + ']'
              : kind === 'citet'
                ? a0 + (y ? ' (' + y + ')' : '')
                : '(' + a0 + (y ? ', ' + y : '') + ')';
            const url = options.bibliography || slideMode
              ? '#' + refAnchorId(label)
              : `/courses/${cid}/references#ref-${label}`;
            refs.push({ url, key: label });
            return wrap(`\\href{${url}}{${_texSafeText(text)}}`);
          }
        }
        return wrap('? ' + _texSafeText(kind + ':' + label));
      },
    );
    return { latex: out, refs };
  };

  // 5. Restore LaTeX blocks (labeled ones as numbered equation "(N)")
  //    Slide-Mode: Nummer eines im Skript vorhandenen Labels ist klickbar und
  //    verlinkt zur Gleichung im Skript; slide-eigene Labels zeigen (S1), …
  latexBlocks.forEach((latex, idx) => {
    const mx = _mathXrefsToLatex(latex);
    const rendered = _injectMathRefKeys(renderLatexBlock(mx.latex), mx.refs);
    const label = latexLabels[idx];
    const frag = latexFragments[idx];
    const aaid = latexAaids[idx];
    const fragAttrs = frag
      ? ` data-frag="${frag.type}"${frag.id ? ` data-frag-id="${frag.id}"` : ''}`
      : '';
    // Auto-Animate-Element-ID (Reveal 4.6: data-id, s. {#aaid:…})
    const aaidAttr = aaid ? ` data-id="${aaid}"` : '';
    if (label) {
      const num = eqLabelNumbers[label];
      const g = globalLabels['eq:' + label];
      let numHtml;
      if (slideMode && g && g.kind === 'eq') {
        const cid = (refMap && refMap.courseId) || _getCourseId() || '';
        numHtml =
          `<a class="aicampus-eq-num aicampus-eq-num-link" href="/courses/${cid}/script#eq:${label}"` +
          ` title="Zur Gleichung im Skript">(${num})</a>`;
      } else {
        numHtml = `<span class="aicampus-eq-num">(${num})</span>`;
      }
      const wrapped =
        `<div id="eq:${label}" class="aicampus-equation"${fragAttrs}${aaidAttr}>${rendered}${numHtml}</div>`;
      html = html.replace(`%%LATEX_BLOCK_${idx}%%`, wrapped.replace(/\$/g, '$$$$'));
    } else if (frag || aaid) {
      // Unlabeled + Fragment: Katex' <span class="katex-display"> in ein
      // fragmentierbares Block-Element verpacken (wird beim innerHTML-Parse
      // aus dem umgebenden <p> gehoistet, wie beim labeled Fall). Die
      // Reveal-Klasse + Index vergibt _applyFragmentMarkers (slideMode).
      html = html.replace(`%%LATEX_BLOCK_${idx}%%`, `<div${fragAttrs}${aaidAttr}>${rendered}</div>`.replace(/\$/g, '$$$$'));
    } else {
      html = html.replace(`%%LATEX_BLOCK_${idx}%%`, rendered.replace(/\$/g, '$$$$'));
    }
  });

  // 6. Restore inline LaTeX (inkl. in-Formel-Xrefs, s. _mathXrefsToLatex)
  latexInlines.forEach((latex, idx) => {
    const mx = _mathXrefsToLatex(latex);
    html = html.replace(`%%LATEX_INLINE_${idx}%%`, _injectMathRefKeys(renderLatexInline(mx.latex), mx.refs));
  });

  // 6a. Restore numbered figures (.html-Medien als interaktives Iframe)
  //     {.height=X} (px, Max-Höhe) / {.zoom=X} wirken in Slides UND Skript.
  //     !important: schlägt die generischen img-/applet-Regeln aus
  //     slides.css/main.css (height:auto, max-height, width:100% — alle mit
  //     !important; Inline-!important gewinnt).
  //     Höhe = max-height (nicht exakt): Medien nutzen die verfügbare Breite
  //     ratio-erhaltend aus, bis die Max-Höhe erreicht ist ("contain"-Effekt
  //     über die Basis-CSS max-width:100% + height:auto). Applets: volle
  //     Breite, interne Scrollbar, wenn die Max-Höhe überschritten wird.
  figures.forEach((f, idx) => {
    const safeAlt = escapeHtml(f.alt);
    const isApplet = isAppletSrc(f.src);
    // Externe Website/YouTube (http(s)): keine Auto-Sizing-Nachrichten →
    // {.height=Y} setzt die ECHTE Höhe (height, nicht max-height) —
    // sonst bliebe das Iframe bei der Browser-Default-Höhe (150 px) und
    // die Angabe wirkte nicht. YouTube-Watch-Links → Embed-Player.
    const isExternalApplet = isApplet && /^https?:\/\//i.test(f.src);
    const isVideo = youtubeEmbedSrc(f.src) !== null;
    const effSrc = youtubeEmbedSrc(f.src) || f.src;
    const parts = [];
    let dataZoom = '';
    if (isVideo && f.height != null) {
      // YouTube: fixe Höhe + proportionale Breite (16:9), zentriert in
      // der Figure (text-align:center). Inline-!important schlägt die
      // width:100%-Regel (slides.css) und die 9em-Default-Max-Höhe.
      const hExpr = f.zoom != null ? `calc(${f.height}px / ${f.zoom})` : `${f.height}px`;
      const wExpr = f.zoom != null
        ? `calc(${f.height}px * 16 / 9 / ${f.zoom})`
        : `${Math.round((f.height * 16) / 9)}px`;
      parts.push(`height: ${hExpr} !important`);
      parts.push(`max-height: ${hExpr} !important`);
      parts.push(`width: ${wExpr} !important`);
    } else if (f.height != null) {
      // Höhe in sichtbaren px. Gezoomtes Applet: geteilt durch den
      // Zoom-Faktor, da die transform die Iframe-Layout-Box skaliert.
      parts.push(f.zoom != null
        ? `${isExternalApplet ? 'height' : 'max-height'}: calc(${f.height}px / ${f.zoom}) !important`
        : `${isExternalApplet ? 'height' : 'max-height'}: ${f.height}px !important`);
      // Externe Website: die 9em-Default-Max-Höhe (slides.css, Stylesheet-
      // !important) dürfte die explizite height nicht deckeln → gleiche
      // Max-Höhe inline (Inline-!important gewinnt).
      if (isExternalApplet) {
        parts.push(f.zoom != null
          ? `max-height: calc(${f.height}px / ${f.zoom}) !important`
          : `max-height: ${f.height}px !important`);
      }
    }
    if (f.zoom != null) {
      // Zoom via transform (universell unterstützt, im Gegensatz zur
      // zoom-Property): Width-Kompensation + Skalierung füllen exakt die
      // Spaltenbreite; der Wrapper übernimmt die sichtbare (geskalte) Höhe.
      // (Video mit expliziter {.height} trägt oben bereits die 16:9-Breite.)
      if (!(isVideo && f.height != null)) parts.push(`width: calc(100% / ${f.zoom}) !important`);
      parts.push(`transform: scale(${f.zoom})`);
      parts.push('transform-origin: 0 0');
      // Ohne explizite {.height} gilt die Default-Max-Höhe (9em in Slides)
      // auch für gezoomte Applets — die slides.css-Regel teilt sie durch
      // --tz (transform vergrößert die Layout-Box), damit die sichtbare
      // Höhe der zoomlosen Default-Höhe entspricht. --tz setzen, damit sie
      // ohne data-max-h greift. (Bilder mit Zoom behalten none: Zoom ist
      // Applet-Semantik, die 7em-Bild-Cap soll dort nicht zuschlagen.)
      if (isApplet) parts.push(`--tz: ${f.zoom}`);
      else if (f.height == null) parts.push('max-height: none !important');
      dataZoom = ` data-zoom="${f.zoom}"`; // Auto-Sizing: Clamp zoom-korrigiert
    }
    const mediaStyle = parts.length ? ` style="${parts.join('; ')}"` : '';
    const dataMaxH = isApplet && !isExternalApplet && f.height != null ? ` data-max-h="${f.height}"` : '';
    const mediaTag = isApplet
      ? `<iframe src="${escapeHtml(effSrc)}" class="aicampus-applet${isVideo ? ' aicampus-video' : ''}" sandbox="${appletSandboxAttr(effSrc)}" loading="lazy" title="${safeAlt}"${dataZoom}${dataMaxH}${mediaStyle}></iframe>`
      : isVideoSrc(f.src)
      ? `<video src="${escapeHtml(f.src)}" preload="metadata"${videoAttrs(f)}${mediaStyle}></video>`
      : `<img src="${escapeHtml(f.src)}" alt="${safeAlt}"${mediaStyle}>`;
    // Gezoomte Applets in einen overflow:hidden-Wrapper (sichtbare =
    // geskalte Höhe; transform ändert die Iframe-Layout-Box nicht). Initiale
    // Höhe: Applets = Auto-Sizing-Default (150 px × zoom), ggf. auf die
    // Max-Höhe begrenzt (der Auto-Sizing-Listener clamped danach ebenfalls);
    // externe Websites = exakt die vorgegebene Höhe (Iframe-Höhe ist fix);
    // Videos ohne {.height} = Spaltenbreite × 9/16 (16:9, CSS aspect-ratio).
    // WICHTIG: <span>, kein <div> — ein div würde das umgebende <p>
    // (HTML-Parsing, unabhängig vom CSS display) schließen und das Iframe
    // in einen anderen font-size-Kontext rücken (p = 0.95em); die
    // em-basierte Default-Max-Höhe müsste dann nicht mehr der zoomlosen
    // Default-Höhe am selben Ort entsprechen. Ein span mit
    // display:inline-block + width:100% bleibt im <p> (s. CSS).
    const zoomInitH = f.height != null
      ? (isExternalApplet
        ? `${f.height}px`
        : `min(calc(150px * ${f.zoom}), ${f.height}px)`)
      : (isVideo
        ? 'calc(100% * 0.5625)'
        : `calc(150px * ${f.zoom})`);
    const innerMedia = (isApplet && f.zoom != null)
      ? `<span class="aicampus-applet-zoom" style="height: ${zoomInitH}">${mediaTag}</span>`
      : mediaTag;
    const figFragAttrs = f.frag
      ? ` data-frag="${f.frag.type}"${f.frag.id ? ` data-frag-id="${f.frag.id}"` : ''}`
      : '';
    const figAaidAttr = f.aaid ? ` data-id="${f.aaid}"` : '';
    // Nummer: im Skript gespeichertes Label → in Slides klickbar (→ Skript),
    // wie bei den Formeln; sonst (auch slide-eigene S-Nummern) plain.
    const g = globalLabels['fig:' + f.label];
    const numHtml = (slideMode && g && g.kind === 'fig')
      ? (() => {
        const cid = (refMap && refMap.courseId) || _getCourseId() || '';
        return `<a class="aicampus-fig-num-link" href="/courses/${cid}/script#fig:${f.label}" title="Zur Abbildung im Skript">Abb. ${f.num}</a>`;
      })()
      : `Abb. ${f.num}`;
    const figHtml =
      `<figure id="fig:${f.label}" class="aicampus-figure"${figFragAttrs}${figAaidAttr}>` +
      innerMedia +
      `<figcaption>${numHtml}${f.alt ? `: ${renderCaptionRef(f.alt, captionCtx)}` : ''}</figcaption></figure>`;
    html = html.replace(`%%FIG_${idx}%%`, figHtml.replace(/\$/g, '$$$$'));
  });

  // 6a2. Restore unlabeled HTML figures (interaktives Applet/YouTube, ohne
  //      Nummer) — Medien mit Alt-Text bekommen die Caption unter sich.
  //      {.height=X} / {.zoom=X}: Semantik wie bei labeled Figures (s. 6a).
  appletFigures.forEach((f, idx) => {
    const isExternal = /^https?:\/\//i.test(f.src);
    const isVideo = youtubeEmbedSrc(f.src) !== null;
    const effSrc = youtubeEmbedSrc(f.src) || f.src;
    const parts = [];
    let dataZoom = '';
    if (isVideo && f.height != null) {
      // YouTube: fixe Höhe + proportionale Breite (16:9) — s. labeled-
      // Figures oben.
      const hExpr = f.zoom != null ? `calc(${f.height}px / ${f.zoom})` : `${f.height}px`;
      const wExpr = f.zoom != null
        ? `calc(${f.height}px * 16 / 9 / ${f.zoom})`
        : `${Math.round((f.height * 16) / 9)}px`;
      parts.push(`height: ${hExpr} !important`);
      parts.push(`max-height: ${hExpr} !important`);
      parts.push(`width: ${wExpr} !important`);
    } else if (f.height != null) {
      parts.push(f.zoom != null
        ? `${isExternal ? 'height' : 'max-height'}: calc(${f.height}px / ${f.zoom}) !important`
        : `${isExternal ? 'height' : 'max-height'}: ${f.height}px !important`);
      // Externe Website: 9em-Default-Max-Höhe nicht zulassen (s. 6a).
      if (isExternal) {
        parts.push(f.zoom != null
          ? `max-height: calc(${f.height}px / ${f.zoom}) !important`
          : `max-height: ${f.height}px !important`);
      }
    }
    if (f.zoom != null) {
      // (Video mit expliziter {.height} trägt bereits die 16:9-Breite.)
      if (!(isVideo && f.height != null)) parts.push(`width: calc(100% / ${f.zoom}) !important`);
      parts.push(`transform: scale(${f.zoom})`);
      parts.push('transform-origin: 0 0');
      // Ohne {.height} gilt die Default-Max-Höhe (9em in Slides) — teilt die
      // slides.css-Regel durch --tz (s. labeled-Figures oben).
      parts.push(`--tz: ${f.zoom}`);
      dataZoom = ` data-zoom="${f.zoom}"`;
    }
    const mediaStyle = parts.length ? ` style="${parts.join('; ')}"` : '';
    const dataMaxH = !isExternal && f.height != null ? ` data-max-h="${f.height}"` : '';
    const iframeTag =
      `<iframe src="${escapeHtml(effSrc)}" class="aicampus-applet${isVideo ? ' aicampus-video' : ''}" sandbox="${appletSandboxAttr(effSrc)}" loading="lazy" title="${escapeHtml(f.alt)}"${dataZoom}${dataMaxH}${mediaStyle}></iframe>`;
    const zoomInitH = f.height != null
      ? (isExternal ? `${f.height}px` : `min(calc(150px * ${f.zoom}), ${f.height}px)`)
      : (isVideo ? 'calc(100% * 0.5625)' : `calc(150px * ${f.zoom})`);
    // <span>-Wrapper (kein <div>): muss im umgebenden <p> bleiben, damit
    // das Iframe dieselbe font-size erbt wie im zoomlosen Fall (s. 6a).
    const iframeHtml = (f.zoom != null)
      ? `<span class="aicampus-applet-zoom" style="height: ${zoomInitH}">${iframeTag}</span>`
      : iframeTag;
    // Caption unter dem Medium: Alt-Text (unlabelt → ohne Nummer).
    const figHtml = f.alt.trim()
      ? `<figure class="aicampus-figure">${iframeHtml}<figcaption>${renderCaptionRef(f.alt.trim(), captionCtx)}</figcaption></figure>`
      : iframeHtml;
    html = html.replace(`%%APPLETFIG_${idx}%%`, figHtml.replace(/\$/g, '$$$$'));
  });

  // 6a3. Restore unlabeled plain images — mit Max-Höhe ({.height=X}, s. 6a)
  //      und/oder Caption (= Alt-Text, ohne Nummer) → <figure>;
  //      sonst simples <img> (wie ein normales marked-Bild).
  plainFigures.forEach((f, idx) => {
    const isVid = isVideoSrc(f.src);
    const imgStyle = f.height != null ? ` style="max-height: ${f.height}px !important"` : '';
    const imgTag = isVid
      ? `<video src="${escapeHtml(f.src)}" preload="metadata"${videoAttrs(f)}${imgStyle}></video>`
      : `<img src="${escapeHtml(f.src)}" alt="${escapeHtml(f.alt)}"${imgStyle}>`;
    // Videos immer im <figure>-Wrapper (zentrierung/Margen), auch ohne
    // Caption/Height.
    const imgHtml = (isVid || f.height != null || f.alt.trim())
      ? `<figure class="aicampus-figure">${imgTag}` +
        (f.alt.trim() ? `<figcaption>${renderCaptionRef(f.alt.trim(), captionCtx)}</figcaption>` : '') +
        `</figure>`
      : imgTag;
    html = html.replace(`%%PLAINFIG_${idx}%%`, imgHtml.replace(/\$/g, '$$$$'));
  });

  // 6a4. Restore Subfigure-Komplexe (s. 1e0): EINE (gelabelt =) nummerierte
  //      Figure mit Flex-Zeile (wrap, space-between) an Medien — jedes mit
  //      eigener Caption (Math ok), darunter die Gesamt-Caption. Nummer/Link
  //      wie bei einfachen Figuren (s. 6a); {height=X} = Max-Höhe der Zeile,
  //      innere {height=X} = Max-Höhe des einzelnen Mediums. Sobald
  //      mindestens ein Inner ein {#fig:label} trägt, bekommen ALLE Inners
  //      den Letter-Prefix („a) “, „b) “, … nach Position im Komplex) in der
  //      Caption; gelabelte Inners zusätzlich den Anker id="fig:label"
  //      (Ziel von @fig:-Referenzen → „Abb. N a)").
  subfigures.forEach((f, idx) => {
    const hasInnerLabel = f.inners.some((x) => x.label !== null);
    const items = f.inners.map((inner, i) => {
      const safeAlt = escapeHtml(inner.alt);
      const style = inner.height != null ? ` style="max-height: ${inner.height}px !important"` : '';
      const mediaTag = isAppletSrc(inner.src)
        ? `<iframe src="${escapeHtml(inner.src)}" class="aicampus-applet" sandbox="${appletSandboxAttr(inner.src)}" loading="lazy" title="${safeAlt}"${style}></iframe>`
        : isVideoSrc(inner.src)
        ? `<video src="${escapeHtml(inner.src)}" preload="metadata"${videoAttrs(inner)}${style}></video>`
        : `<img src="${escapeHtml(inner.src)}" alt="${safeAlt}"${style}>`;
      const innerId = inner.label ? ` id="fig:${inner.label}"` : '';
      const letterPrefix = hasInnerLabel ? `${String.fromCharCode(97 + i)}) ` : '';
      const capText = (letterPrefix + inner.alt.trim()).trim();
      return `<figure class="aicampus-subfig-item"${innerId}>${mediaTag}` +
        (capText ? `<figcaption>${renderCaptionRef(capText, captionCtx)}</figcaption>` : '') +
        `</figure>`;
    }).join('');
    const rowStyle = f.height != null ? ` style="max-height: ${f.height}px"` : '';
    const fragAttrs = f.frag
      ? ` data-frag="${f.frag.type}"${f.frag.id ? ` data-frag-id="${f.frag.id}"` : ''}`
      : '';
    const aaidAttr = f.aaid ? ` data-id="${f.aaid}"` : '';
    const idAttr = f.label !== null ? ` id="fig:${f.label}"` : '';
    let numHtml = '';
    if (f.num !== null) {
      const g = globalLabels['fig:' + f.label];
      numHtml = (slideMode && g && g.kind === 'fig')
        ? (() => {
          const cid = (refMap && refMap.courseId) || _getCourseId() || '';
          return `<a class="aicampus-fig-num-link" href="/courses/${cid}/script#fig:${f.label}" title="Zur Abbildung im Skript">Abb. ${f.num}</a>`;
        })()
        : `Abb. ${f.num}`;
    }
    const capParts = [];
    if (numHtml) capParts.push(numHtml);
    if (f.alt.trim()) capParts.push(renderCaptionRef(f.alt.trim(), captionCtx));
    const figHtml =
      `<figure${idAttr} class="aicampus-figure aicampus-subfig"${fragAttrs}${aaidAttr}>` +
      `<div class="aicampus-subfig-row"${rowStyle}>${items}</div>` +
      (capParts.length ? `<figcaption>${capParts.join(': ')}</figcaption>` : '') +
      `</figure>`;
    html = html.replace(`%%SUBFIG_${idx}%%`, figHtml.replace(/\$/g, '$$$$'));
  });

  // 6a3. Heading-Nummerierung (h2–h6) + {#sec:label}-Anker
  //      Kapitelnummer aus dem refmap (rollenabhängig: Student = veröffentlichte
  //      Nummerierung). Ohne Kapitel-Kontext (Aufgaben-Seiten, Antwort-Previews)
  //      → keine Nummer; labelte Sections bekommen trotzdem ihren Anker.
  const chapterNum = chapterRef && chapterRef.num != null ? chapterRef.num : null;
  const secLabelNumbers = {};
  {
    const frag = document.createElement('div');
    frag.innerHTML = html;
    let n2 = 0;
    let n3 = 0;
    let n4 = 0;
    let n5 = 0;
    let n6 = 0;
    frag.querySelectorAll('h2, h3, h4, h5, h6').forEach((h) => {
      let num = null;
      const lvl = Number(h.tagName.charAt(1));
      if (lvl === 2) {
        n2 += 1; n3 = n4 = n5 = n6 = 0;
      } else if (lvl === 3) {
        n3 += 1; n4 = n5 = n6 = 0;
      } else if (lvl === 4) {
        n4 += 1; n5 = n6 = 0;
      } else if (lvl === 5) {
        n5 += 1; n6 = 0;
      } else {
        n6 += 1;
      }
      if (chapterNum != null) {
        num = [chapterNum, n2, n3, n4, n5, n6].slice(0, lvl).join('.');
      }
      let label = null;
      const lm = h.innerHTML.match(/\s*\{#sec:([\p{L}0-9_-]+)\}\s*$/u);
      if (lm) {
        label = lm[1];
        h.innerHTML = h.innerHTML.slice(0, lm.index).replace(/\s+$/, '');
        if (num != null) secLabelNumbers[label] = num;
      }
      if (label) {
        h.id = `sec:${label}`;
      } else if (num != null && sectionId != null) {
        h.id = `sec:${sectionId}-${num}`;
      }
      if (num != null) {
        const span = document.createElement('span');
        span.className = 'aicampus-sec-num';
        span.textContent = num;
        const space = document.createTextNode('\u00a0');
        h.insertBefore(span, h.firstChild);
        h.insertBefore(space, span.nextSibling);
      }
    });
    html = frag.innerHTML;
  }

  // 6b. Restore cross-references (@fig:label / @eq:label / @code:label /
  //     @box:label / @tab:label / @sec:label; @kap:label = Legacy-Alias für @sec:label)
  //     Auflösung: 1) in diesem Dokument (nur außerhalb der Slides: dort
  //                  würde Reveal den In-Page-Hash als Folien-Übergang lesen)
  //                  → In-Page-Anker,
  //               2) Skript-Ref-Map → direkter Link zum Objekt
  //                  (#fig:label / #eq:label / #code:label / #box:label / #sec:label) auf
  //                  der Skript-Seite (alle Rollen; das Kapitel wird dort
  //                  aufgeklappt); Kapitel-Labels (chapter: true) verlinken auf
  //                  #chapter-{id} und werden als „Kap. N“ angezeigt,
  //               3) Slide-Ref-Map → Link auf die Folie im Slide-Deck
  //                  (/slides/{deckId}/present#/{h}/{v}, Anzeige „… S{n}“),
  //               4) aktuelle Folie (nur Slides): im Slide definiert, aber noch
  //                  NICHT in der Refmap (z. B. ungespeichert im Editor) →
  //                  lokale S-Fallback-Nummer, Link auf die aktuelle Folie
  //                  (In-Page-Anker ginge nicht: Reveal liest sie als
  //                  Folien-Übergang — daher auch 1. deaktiviert),
  //               5) unbekannt → ❓
  //     @box:-Referenzen zeigen den Box-Typ-Titel an (z. B. „Satz N“ —
  //     CALLOUT_TYPES; Typ-Quelle: Skript-Ref-Map → Slide-Ref-Map → lokal).
  xrefs.forEach((x, idx) => {
    const kind = x.kind === 'kap' ? 'sec' : x.kind;
    if (kind === 'bibentry') {
      // Komplette Quellenangabe (auto-Quellen-Folie der Slide-Decks): Design wie das
      // Quellenverzeichnis am Kapitelende im Skript — Nummer [N] OHNE Link (der
      // Quellen-Tab ist für Studenten nicht erreichbar), DOI/Link bleiben klickbar.
      // id + data-refkey = Ziel der Zitations-Links (slides.js navigiert + flashen).
      const r = ((refMap && refMap.references) || {})[x.label];
      const refHtml = r
        ? `<span id="${refAnchorId(x.label)}" data-refkey="${x.label}" class="aicampus-bibentry">${_bibEntryHtml(r)}</span>`
        : `<span class="aicampus-xref-broken" title="Quellen-Schlüssel unbekannt — es existiert keine solche Quelle im Kurs">❓ bibentry:${x.label}</span>`;
      html = html.replace(`%%XREF_${idx}%%`, refHtml);
      return;
    }
    if (kind === 'cite' || kind === 'citet' || kind === 'citep') {
      // Zitationen: @cite → "[N]", @citet → "Autor (Jahr)", @citep → "(Autor, Jahr)".
      // N = kursweite stabile Nummer (script-refmap.references, display_order) —
      // dieselbe Quelle trägt über das gesamte Kurs (Skript/Aufgaben/Slides) dieselbe Nummer.
      const r = ((refMap && refMap.references) || {})[x.label];
      if (r) citedRefs.push(r);
      // Vorschau: komplette Bibliographie-Zeile (Autoren, Jahr, Titel, …)
      const tipPreview = r ? (r.entry || r.title) : null;
      const tipAttr = tipPreview
        ? ` data-xref-tip="${JSON.stringify({ k: kind, p: tipPreview })
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/\$/g, '$$$$')}"`
        : '';
      // Mit bibliography (Skript): In-Page-Anker auf den Listeneintrag;
      // Slide-Mode: Anker auf den Eintrag der auto-Quellen-Folie im Deck
      // (Navigation/Highlight übernimmt der slides.js-Click-Handler, data-refkey);
      // sonst: Link auf den Quellen-Tab.
      const cid = (refMap && refMap.courseId) || '';
      const href = options.bibliography || slideMode ? `#${refAnchorId(x.label)}` : `/courses/${cid}/references#ref-${x.label}`;
      const refHtml = _citeHtml(kind, x.label, r, href, tipAttr);
      html = html.replace(`%%XREF_${idx}%%`, refHtml);
      return;
    }
    const local =
      kind === 'fig' ? figLabelNumbers[x.label]
      : kind === 'eq' ? eqLabelNumbers[x.label]
      : kind === 'code' ? codeLabelNumbers[x.label]
      : kind === 'box' ? boxLabelNumbers[x.label]
      : kind === 'tab' ? tabLabelNumbers[x.label]
      : secLabelNumbers[x.label];
    const g = globalLabels[kind + ':' + x.label]; // kind-prefixed (s. script-refmap)
    const sl = slidesLabels[kind + ':' + x.label]; // typ-qualifiziert (s. slides-refmap)
    // Hover-Vorschau (refmap-"preview"): zeigt das Zielobjekt beim Hovern an.
    // JSON im data-Attribut: &/-"-Escape für HTML (beim getAttribute wird das
    // automatisch zurück-dekodiert), $$-Escape, damit der
    // html.replace(`%%XREF_${idx}%%`, …) unten keine $-Backrefs einliest.
    const tipPreview =
      (g && g.kind === kind && g.preview) ||
      (sl && sl.kind === kind && sl.preview) ||
      null;
    const tipAttr = tipPreview
      ? ` data-xref-tip="${JSON.stringify({ k: kind, p: tipPreview })
          .replace(/&/g, '&amp;')
          .replace(/"/g, '&quot;')
          .replace(/\$/g, '$$$$')}"`
      : '';
    let kindText;
    if (kind === 'box') {
      const boxType =
        (g && g.kind === 'box' && g.type) ||
        (sl && sl.kind === 'box' && sl.type) ||
        boxLabelTypes[x.label] || null;
      kindText = boxType && CALLOUT_TYPES[boxType] ? CALLOUT_TYPES[boxType].title : 'Box';
    } else {
      kindText = kind === 'fig' ? 'Abb.' : kind === 'eq' ? 'Gl.' : kind === 'code' ? 'Code' : kind === 'tab' ? 'Tab.' : 'Abs.';
    }
    let refHtml;
    const useLocalAnchor = kind === 'sec' || !slideMode;
    const figInner = kind === 'fig' && useLocalAnchor ? figInnerLocal[x.label] : null;
    if (useLocalAnchor && figInner && figInner.num != null) {
      // Subfigure-Inner: „Abb. N a)“ = Komplex-Nummer + Letter, In-Page-Anker.
      refHtml = `<a href="#fig:${x.label}" class="aicampus-xref"${tipAttr}>Abb. ${figInner.num} ${figInner.letter})</a>`;
    } else if (useLocalAnchor && local) {
      refHtml = `<a href="#${kind}:${x.label}" class="aicampus-xref"${tipAttr}>${kindText} ${local}</a>`;
    } else if (g && g.kind === kind) {
      const cid = (refMap && refMap.courseId) || '';
      const text = g.chapter ? 'Kap.' : kindText;
      const anchor = g.chapter ? `chapter-${g.sectionId}` : `${kind}:${x.label}`;
      const subSuffix = kind === 'fig' && g.sub ? ` ${g.sub})` : '';
      refHtml = `<a href="/courses/${cid}/script#${anchor}" class="aicampus-xref"${tipAttr}>${text} ${g.num}${subSuffix}</a>`;
    } else if (sl && sl.kind === kind) {
      // Slide-eigenes Objekt (kein Skript-Label): Link auf die Folie.
      const cid = (slidesRefMap && slidesRefMap.courseId) || (refMap && refMap.courseId) || '';
      const subSuffix = kind === 'fig' && sl.sub ? ` ${sl.sub})` : '';
      refHtml = `<a href="/courses/${cid}/slides/${sl.deckId}/present#/${sl.h}/${sl.v}" class="aicampus-xref"${tipAttr}>${kindText} S${sl.num}${subSuffix}</a>`;
    } else if (slideMode && (local || (kind === 'fig' && figInnerLocal[x.label]))) {
      // Im aktuellen Slide definiert, aber noch nicht in der Refmap
      // (z. B. im Editor gerade erst angelegt und ungespeichert): Nummer
      // aus der lokalen Extraktion (S-Fallback, dieselbe Zählung wie
      // Refmap), Link auf die aktuelle Folie — In-Page-Anker sind in
      // Slides nicht nutzbar (Reveal liest sie als Folien-Übergang).
      const inner = kind === 'fig' ? figInnerLocal[x.label] : null;
      const num = inner && inner.num != null ? inner.num : local;
      const subSuffix = inner && inner.letter ? ` ${inner.letter})` : '';
      const href = slidePos ? `#/${slidePos.h}/${slidePos.v}` : null;
      refHtml = href
        ? `<a href="${href}" class="aicampus-xref"${tipAttr}>${kindText} ${num}${subSuffix}</a>`
        : `<span class="aicampus-xref"${tipAttr}>${kindText} ${num}${subSuffix}</span>`;
    } else {
      refHtml = `<span class="aicampus-xref-broken" title="Label unbekannt — zugehöriges Objekt fehlt">❓ ${x.kind}:${x.label}</span>`;
    }
    html = html.replace(`%%XREF_${idx}%%`, refHtml);
  });

  // 6b. Quellenverzeichnis: „Quellen“-Liste ans Dokument-Ende (nur bei options.bibliography
  //     und ohne slideMode — das pro-Folie-Rendern würde Nummerierung/Liste zersplittern;
  //     in Slides übernimmt die auto-Quellen-Folie dieselbe Rolle: parseSlides hängt
  //     sie ans Deck-Ende, die Einträge rendert @bibentry:). Gelistet werden nur die
  //     tatsächlich zitierten Einträge, mit ihrer kursweiten stabilen Nummer.
  if (options.bibliography && !slideMode && citedRefs.length) {
    const seen = new Map();
    citedRefs.forEach((r) => { if (!seen.has(r.key)) seen.set(r.key, r); });
    const items = Array.from(seen.values())
      .sort((a, b) => (a.num || 0) - (b.num || 0))  // nach Quellen-Nummer, nicht Textvorkommnis
      .map((r) =>
        `<li id="ref-${escapeHtml(r.key)}" class="text-sm text-gray-700 leading-relaxed">${_bibEntryHtml(r)}</li>`
      ).join('');
    html += `<div class="aicampus-bibliography mt-6">`
      + `<h3 class="text-base font-semibold text-gray-800 mb-2">Quellen</h3>`
      + `<ul class="aicampus-bibliography list-none space-y-1.5">${items}</ul></div>`;
  }

  // 6c. Restore task references (@task:{id})
  //     Daten via refmap.tasks: Student (mode "reading") → nur freigeschaltete
  //     Aufgaben inkl. eigener Punkte/Medaille (analog Aufgabenübersicht);
  //     PROF/TUTOR/Admin (mode "edit") → alle Aufgaben, kompakte Box mit Link.
  const refTasks = (refMap && refMap.tasks) || {};
  const taskCid = (refMap && refMap.courseId) || '';
  taskRefs.forEach((id, idx) => {
    const t = refTasks[id];
    let boxHtml;
    if (!t) {
      boxHtml = `<span class="aicampus-xref-broken" title="Aufgabe unbekannt — existiert nicht (mehr) oder ist nicht freigeschaltet">❓ Aufgabe ${escapeHtml(id)}</span>`;
    } else if (refMap && refMap.mode === 'edit') {
      // PROF/TUTOR/Admin: kompakte Box mit Link zur Aufgabenseite
      boxHtml =
        `<div class="aicampus-taskbox">` +
        `<span class="aicampus-taskbox-icon" aria-hidden="true">📝</span>` +
        `<div class="flex-1 min-w-0">` +
        `<a href="/courses/${taskCid}/tasks/${t.id}" class="aicampus-xref font-semibold">${escapeHtml(t.title)}</a>` +
        `<div class="text-sm text-gray-500 mt-0.5">${t.maxPoints} Punkte · ${( { text: '📄 Text-Aufgabe', workspace: '💻 Workspace-Aufgabe', mc: '🎯 Multiple-Choice' } ) [t.taskType] || '📄 Text-Aufgabe'}</div>` +
        `</div></div>`;
    } else {
      // Student: Karte analog zur Aufgabenübersicht (Punkte, Medaille, Versuche, Deadline)
      const pct = t.maxPoints > 0 ? t.myPoints / t.maxPoints : 0;
      let pointColor = 'text-gray-400';
      if (pct >= 0.8) pointColor = 'text-green-600';
      else if (pct >= 0.5) pointColor = 'text-yellow-600';
      else if (t.myPoints > 0) pointColor = 'text-orange-600';
      let medalBadge = '';
      if (pct >= 1.0) medalBadge = '<span class="badge-tier badge-platinum">💎</span>';
      else if (pct >= 0.9) medalBadge = '<span class="badge-tier badge-gold">🥇</span>';
      else if (pct >= 0.8) medalBadge = '<span class="badge-tier badge-silver">🥈</span>';
      else if (pct >= 0.7) medalBadge = '<span class="badge-tier badge-bronze">🥉</span>';
      let deadlineHtml = '';
      if (t.deadline) {
        const dl = new Date(t.deadline).toLocaleDateString('de-DE');
        deadlineHtml = ` · ⏰ Deadline ${dl}`;
      }
      const typeBadge = { text: '📄 Text', workspace: '💻 Workspace', mc: '🎯 Multiple Choice' }[t.taskType] || '📄 Text';
      const typeBadgeClass = { text: 'bg-gray-100 text-gray-600', workspace: 'bg-indigo-100 text-indigo-700', mc: 'bg-emerald-100 text-emerald-700' }[t.taskType] || 'bg-gray-100 text-gray-600';
      boxHtml =
        `<div class="aicampus-taskbox">` +
        `<span class="aicampus-taskbox-icon" aria-hidden="true">📝</span>` +
        `<div class="flex-1 min-w-0">` +
        `<a href="/courses/${taskCid}/tasks/${t.id}" class="aicampus-xref text-base font-semibold">${escapeHtml(t.title)}</a>` +
        `<div class="text-sm text-gray-500 mt-1">` +
        `${t.attemptsUsed}${t.maxAttempts != null ? '/' + t.maxAttempts : ''} Versuche${deadlineHtml}&nbsp;<span class="px-2 py-0.5 rounded text-xs font-medium ${typeBadgeClass}">${typeBadge}</span>${medalBadge ? '&nbsp;&nbsp;&nbsp;&nbsp;' + medalBadge : ''}` +
        `</div></div>` +
        `<div class="text-right flex-shrink-0">` +
        `<div class="text-lg font-bold ${pointColor}">${t.myPoints}/${t.maxPoints}</div>` +
        `<div class="text-xs text-gray-400">Punkte</div>` +
        `</div></div>`;
    }
    // $ in der Ersatz-String escapen (String.replace-Backrefs), wie bei Figuren/Latex
    html = html.replace(`%%TASKREF_${idx}%%`, boxHtml.replace(/\$/g, '$$$$$$$$'));
  });

  // 7. Decode escaped dollar signs back to literal $
  html = html.replace(new RegExp(escapedDollar, 'g'), '$');

  // 8. Apply syntax highlighting
  html = highlightCodeBlocks(html);

  // 9a. Tabellen: leere (grau gehighlightete) Kopfzeilen nicht rendern
  html = _stripEmptyTableHead(html);

  // 9. Decode HTML entities in non-code text
  html = decodeTextEntities(html);

  // 10. Sanitize with DOMPurify
  //     (ADD_TAGS/ADD_ATTR: Applet-Iframes werden sonst komplett entfernt —
  //     iframe steht nicht in der Default-Allow-List von DOMPurify)
  if (typeof DOMPurify !== 'undefined') {
    html = DOMPurify.sanitize(html, { ADD_TAGS: ['iframe'], ADD_ATTR: ['sandbox'] });
    // Sicherheitsnetz: Applet-Iframes dürfen IMMER nur sandboxed laufen
    // (allow-scripts, ohne allow-same-origin). Falls DOMPurify das sandbox-
    // Attribut entfernt, wird es neu eingefügt.
    html = html.replace(/<iframe(?![^>]*\bsandbox=)([^>]*)>/g, (_m, attrs) => {
      return '<iframe sandbox="allow-scripts"' + attrs.replace(/\/\s*$/, '') + '>';
    });
  }

  // 11. Render Mermaid diagrams.
  //     Gelabelte/beschriftete Blöcke (```mermaid {#code:label}[Caption], s. 1a):
  //     wie Code-Blöcke nummeriert ("Code N: Caption") — die Code-Label-
  //     Nummerierung (globalLabels/fallback-Zähler aus Schritt 3) wird weiterverwendet —
  //     in einer Figure mit Anker code:label.
  if (mermaidBlocks.length > 0) {
    const renderedDiagrams = await Promise.all(mermaidBlocks.map((b) => {
      return renderMermaid(b.diagram);
    }));
    renderedDiagrams.forEach((svg, idx) => {
      const b = mermaidBlocks[idx];
      let blockHtml = svg;
      if (b.label !== null || b.caption !== null) {
        let merNum = null;
        if (b.label !== null) {
          if (b.label in codeLabelNumbers) {
            merNum = codeLabelNumbers[b.label]; // Duplikat → erstes Vorkommen
          } else {
            const g = globalLabels['code:' + b.label];
            if (g && g.kind === 'code') {
              merNum = g.num; // gespeichertes Label → exakte globale Nummer
            } else if (slideMode) {
              const sl = slidesLabels['code:' + b.label]; // typ-qualifiziert (s. slides-refmap)
              if (sl && sl.kind === 'code') {
                merNum = 'S' + sl.num; // slide-eigenes Label → S-Nummer
              } else {
                slideCodeCount += 1;
                merNum = 'S' + (slidesMaxSCode + slideCodeCount); // ungespeichert
              }
            } else {
              codeFallbackCount += 1;
              merNum = codeFallbackBase + codeFallbackCount;
            }
            codeLabelNumbers[b.label] = merNum;
          }
        }
        let numHtml = '';
        if (merNum !== null) {
          const g = globalLabels['code:' + b.label];
          if (slideMode && g && g.kind === 'code') {
            const cid = (refMap && refMap.courseId) || _getCourseId() || '';
            numHtml =
              `<a class="aicampus-code-num-link" href="/courses/${cid}/script#code:${b.label}"` +
              ` title="Zum Code im Skript">Code ${merNum}</a>`;
          } else {
            numHtml = `Code ${merNum}`;
          }
        }
        const capParts = [];
        if (numHtml) capParts.push(numHtml);
        if (b.caption) capParts.push(renderCaptionMath(b.caption));
        const idAttr = b.label !== null ? ` id="code:${b.label}"` : '';
        blockHtml = `<figure class="aicampus-code-figure"${idAttr}>${blockHtml}` +
          `<figcaption>${capParts.join(': ')}</figcaption></figure>`;
      }
      html = html.replace(`%%MERmaid_BLOCK_${idx}%%`, blockHtml.replace(/\$/g, '$$$$'));
    });
  }

  // Live-Update-Cache-Bust (s. _mediaBustPass): geändertes Applet → ?v=<ts>
  html = _mediaBustPass(html);
  targetElement.innerHTML = `<div class="markdown-preview">${html}</div>`;
  // Überbreite Tabellen (viele Spalten): Horizontal-Scrollfläche mit
  // dezentem, schmalen Scrollbar (CSS .aicampus-table-scroll, main.css) —
  // die Tabelle scrollt in x (Trackpad/Mausrad/Touch), statt über den
  // Container/über den Slide-Rand zu ragen. hljs-ln (Code-Zeilennummern-)
  // Tabelle ausgenommen: sie gehört zum <pre>-Layout.
  targetElement.querySelectorAll('.markdown-preview table:not(.hljs-ln)').forEach((tbl) => {
    const wrap = document.createElement('div');
    wrap.className = 'aicampus-table-scroll';
    tbl.parentNode.insertBefore(wrap, tbl);
    wrap.appendChild(tbl);
  });
  _cleanupBlockArtifacts(targetElement);
  if (slideMode) _applyFragmentMarkers(targetElement);
  // Code-LaTeX (z. B. Pseudo-Code): im Skript direkt auf der finalen DOM
  // anwenden; in Slides erst NACH Reveal's Highlight-Pass (s. aicampusWireCodeMath).
  if (!slideMode) applyCodeMath(targetElement);
  if (sourceLines) _applySrcLineMarkers(targetElement);
}

// (sourceLines) Zeilen-Marker vor jedes Top-Level-Block einfügen:
// marked-Tokens haben keine Zeilen-Info (kein token.map in v14), sind aber
// lückenlose Source-Substrings (token.raw; Lexer: src = src.substring(raw.length))
// → Positions-Walk über den Token-Stream. norm = text mit derselben
// Normalisierung wie marked (_Lexer.lex: \r\n|\r→\n; blockTokens: Leading-Tabs
// → 4 Spaces); Norm-Zeile k = Originalzeile k (die Normalisierung ändert
// keine Zeilenzahl). Marker = <div> auf eigener Zeile (1. Zeile endet mit
// "\u003e" + Zeilenumbruch → marked-HTML-Block), vom Folgeblock durch Leerzeile
// getrennt. Fortsetzungszeilen mehrzeiliger Absätze (Listen-Items,
// normale Absätze) erhalten zusätzlich ein leeres Inline-Span (sq-subline):
// der Block-Marker deckt nur die erste Blockzeile ab. →
// _applySrcLineMarkers am Ende.
function _insertSrcLineMarkers(text, lines, lineOffset, tables) {
  const tokens = marked.lexer(text, { breaks: false, gfm: true });
  const norm = text.replace(/\r\n|\r/g, '\n')
                   .replace(/^( *)(\t+)/gm, (m, l, t) => l + '    '.repeat(t.length));
  const lineOff = [0];
  for (let i = 0; i < norm.length; i++) if (norm[i] === '\n') lineOff.push(i + 1);
  const subLine = (l) => (lines[l] != null ? lines[l] : 0) + lineOffset;
  const lineAt = (pos) => {
    let l = 0;
    while (lineOff[l + 1] !== undefined && lineOff[l + 1] <= pos) l++;
    return l;
  };
  // Pass 1: Marker-Positionen sammeln — Block-Marker (erste Zeile je
  // Block) + Zeilen-Marker (Fortsetzungszeilen mehrzeiliger Absätze,
  // z. B. Listen-Item über zwei Source-Zeilen): ohne sie erreichte
  // Doppelklick/Scroll-Sync nur die erste Zeile des Blocks.
  const marks = []; // { pos, html }
  const addLineMarks = (start, end, firstLine) => {
    for (let j = firstLine + 1; j <= lineAt(end - 1); j++) {
      let p = lineOff[j];
      const e2 = lineOff[j + 1] !== undefined && lineOff[j + 1] <= end
        ? lineOff[j + 1] - 1 : end;
      while (p < e2 && norm[p] === ' ') p++; // Einzug bleibt vor dem Marker
      if (p < e2) marks.push({
        pos: p,
        // Leeres, zero-width Span (Content NICHT einpacken: marked parst
        // Markdown nicht innerhalb von Inline-HTML → **fett** bräche
        // kaputt). Top = Position der Zeile; für die Scroll-Sync reicht
        // das. _applySrcLineMarkers setzt data-src-line auf der DOM.
        html: `\u003cspan class="sq-subline" data-line="${subLine(j)}"\u003e\u003c/span\u003e`,
      });
    }
  };
  let cursor = 0;
  let line = 0;
  let lastPos = 0;
  for (const tok of tokens) {
    const raw = tok.raw;
    // Contiguity-Check: fällt er durch, weicht unsere Norm von der
    // marked-internen ab → sicherheitshalber ganz ohne Marker.
    if (typeof raw !== 'string' || !norm.startsWith(raw, cursor)) return text;
    if (tok.type !== 'space') {
      while (lineOff[line + 1] !== undefined && lineOff[line + 1] <= cursor) line++;
      const off = lineOff[line];
      if (off > lastPos) {
        const n = subLine(line);
        // Feinere Zeilen innerhalb eines Blocks (data-sub): marked lext
        // eine ganze Liste/Tabelle als EINEN Token — ohne Sub-Zeilen
        // träfen Doppelklick/Scroll-Sync immer nur die Block-Anfangs-
        // zeile. Format "l1,l2,..." (gleiche Nummerierung wie data-line);
        // _applySrcLineMarkers verteilt sie auf \u003cli\u003e bzw. \u003ctr\u003e.
        let sub = '';
        if (tok.type === 'list' && tok.items && tok.items.length) {
          // Item-Raws kacheln list.raw lückenlos (nur das letzte Item
          // ist trimEnd'ed — seine Startposition bleibt exakt) →
          // Präfixsumme über die Item-Raws = Startzeile je Item.
          const parts = [];
          let o = cursor;
          for (const it of tok.items) {
            const l = lineAt(o);
            parts.push(subLine(l));
            // Mehrzeiliges Item (reiner Ein-Absatz) → Fortsetzungszeilen
            // adressierbar. Items mit eingebetteten Blöcken (verschachtelte
            // Listen, Code …) bleiben unmarkiert — erste Zeile gilt.
            // (marked: Item-Content ist Token-Typ "text", kein "paragraph".)
            if (it.tokens && it.tokens.length === 1 &&
                (it.tokens[0].type === 'text' || it.tokens[0].type === 'paragraph')) {
              addLineMarks(o, o + it.raw.length, l);
            }
            o += it.raw.length;
          }
          sub = parts.join(',');
        } else if (tok.type === 'table') {
          // GFM: Kopfzeile, Trennzeile, dann je Zeile genau eine
          // Source-Zeile → Kopf = line, Zeile i = line+2+i.
          const parts = [subLine(line)];
          for (let i = 0; i < tok.rows.length; i++) parts.push(subLine(line + 2 + i));
          sub = parts.join(',');
        } else if (tok.type === 'paragraph' && tables && tables.length) {
          // Beschriftete Tabelle: die Pipeline hat „Tabellen-Blöcke +
          // Label-Zeile“ bereits zu %%TAB_N%% extrahiert (eine Zeile)
          // → marked sieht KEIN table-Token. Per-Zeile-Mapping wurde in 1j2
          // als tbl.rows erfasst (Original-Zeilen: Kopf, Trennzeile, Daten).
          const t = /^%%TAB_(\d+)%%$/.exec(tok.text.trim());
          const tbl = t && tables[t[1]];
          if (tbl && tbl.rows && tbl.rows.length) {
            // sub-Format wie beim nativen table-Token: [Kopf, Daten…] —
            // die Trennzeile (Index 1) hat kein \u003ctr\u003e → weglassen.
            const parts = [tbl.rows[0]];
            for (let i = 2; i < tbl.rows.length; i++) parts.push(tbl.rows[i]);
            sub = parts.map((r) => r + lineOffset).join(',');
          }
        } else if (tok.type === 'paragraph') {
          // Mehrzeiliger Absatz → Fortsetzungszeilen adressierbar
          // (wie bei Listen-Items).
          addLineMarks(cursor, cursor + raw.length, line);
        }
        marks.push({
          pos: off,
          html: `\u003cdiv class="sq-slm" data-line="${n}"${sub ? ` data-sub="${sub}"` : ''}\u003e\n\u003c/div\u003e\n\n`,
        });
        lastPos = off;
      }
    }
    cursor += raw.length;
  }
  marks.sort((a, b) => a.pos - b.pos);
  // Pass 2: norm kopieren und Marker an den Positionen einfügen.
  let out = '';
  let i = 0;
  for (const mk of marks) {
    if (mk.pos < i) return text; // defensiv: Reihenfolge kaputt → ohne Marker
    out += norm.slice(i, mk.pos) + mk.html;
    i = mk.pos;
  }
  return out + norm.slice(i);
}

// (sourceLines) Marker → data-src-line: jeder Marker liegt direkt vor dem
// Block, dessen Anfangszeile er trägt — auch verschachtelt (z. B. in
// Box-Bodies; der dazwischenliegende Block wird vom HTML-Parser als
// Geschwister des Markers gelegt). Läuft am SEHR Ende, nach
// _cleanupBlockArtifacts (leere <p>-Bruchstücke sind dann weg).
function _applySrcLineMarkers(root) {
  for (const m of root.querySelectorAll('.sq-slm')) {
    const el = m.nextElementSibling;
    if (el) {
      el.dataset.srcLine = m.dataset.line;
      // Sub-Zeilen (Listen-Items / Tabellen-Zeilen) auf die Kinder
      // verteilen, damit Doppelklick/Scroll-Sync blockintern fein
      // auflösen können.
      const sub = m.dataset.sub;
      if (sub) {
        const subLines = sub.split(',').map(Number);
        if (el.tagName === 'UL' || el.tagName === 'OL') {
          const lis = el.children; // nur <li>
          for (let i = 0; i < subLines.length; i++) {
            if (!lis[i]) break;
            lis[i].dataset.srcLine = subLines[i];
          }
        } else {
          // Tabelle (ggf. in .aicampus-table-scroll verpackt).
          // subLines[0] = Kopfzeile, subLines[1+i] = Datenzeile i; leere
          // theads sind entfernt (_stripEmptyTableHead) — die tbody-
          // Indexierung (i+1) ist in beiden Fällen korrekt.
          const tbl = el.tagName === 'TABLE' ? el : el.querySelector('table');
          if (tbl) {
            const theadTr = tbl.querySelector('thead > tr');
            if (theadTr) theadTr.dataset.srcLine = subLines[0];
            const bodyTrs = tbl.querySelectorAll('tbody > tr');
            for (let i = 0; i + 1 < subLines.length; i++) {
              if (!bodyTrs[i]) break;
              bodyTrs[i].dataset.srcLine = subLines[i + 1];
            }
          }
        }
      }
    }
    m.remove();
  }
  // Zeilen-Marker (Fortsetzungszeilen mehrzeiliger Absätze/Listen-Items):
  // leeres Span bleibt als Inline-Ziel für die Scroll-Sync (Top =
  // Position der Zeile).
  for (const s of root.querySelectorAll('.sq-subline')) {
    s.dataset.srcLine = s.dataset.line;
    s.removeAttribute('data-line');
    s.classList.remove('sq-subline');
  }
}

// Byproducts aufräumen: Figure-/Applet-/Code-/Taskbox-/Tabellen-Placeholders
// sind Inline-Tokens. marked rendert mit Standards-Markdown (breaks:false):
// ein Zeilenumbruch im Quelltext ohne Leerzeile wird nur zu Leerraum, d. h.
// ein Token mitten zwischen Textzeilen liegt INLINE im selben <p> wie der
// umgebende Text. Beim Restore ersetzt der Block-HTML des Tokens das Token
// im <p>; beim innerHTML-Parsen schließen <figure>/<pre>/<div> das umgebende
// <p> IMPLIZIT (HTML-Parser-Regel), sodass (a) ein <p>-Rest mit Text vor dem
// Block bleibt und (b) der Folgetext (inkl. Inline-Elemente wie
// <code>/Katex-Spans) als Direktkinder der .markdown-preview landet.
// Aufräumen:
//   (1) Top-Level-<br> entfernen,
//   (2) laufende Inline-Läufe (Text + inline gerenderte Elemente) in
//       EINEM <p> hüllen — würde jeder Textknoten sein eigenes <p>
//       bekommen, bricht der Satz an den Inline-Elementen um (falsche
//       Zeilenumbrüche um Formeln/`<code>`),
//   (3) leere Top-Level-<p> entfernen,
//   (4) trailing <br> am Ende eines <p> entfernen, wenn danach (über
//       Whitespace hinweg) ein Block-Element folgt (z. B. manueller
//       Zeilenumbruch via 2 Leerzeichen direkt vor der Token-Zeile).
// Legitime <br> (z. B. in Listen oder zwischen zwei Zeilen desselben
// Absatzes) liegen nie als Direktkind der Preview und bleiben erhalten.
function _cleanupBlockArtifacts(root) {
  const clean = (mp) => {
    // (1) Top-Level-<br> entfernen
    for (const br of Array.from(mp.querySelectorAll(':scope > br'))) br.remove();

    // (2) Top-Level-Inline-Läufe in <p> gruppieren.
    // Tag-basiert statt getComputedStyle: beim Render sind die
    // Reveal-Sections noch detached (bzw. display:none) — für nicht
    // eingebundene Elemente liefert getComputedStyle keine aufgelösten
    // Werte (display="" → kein "inline") → Inline-Elemente blieben
    // ungruppiert und Absätze zerfielen (nur im Skript, wo der Container
    // im DOM hängt, griff die computed-style-Variante).
    const isBlockTag = /^(?:P|DIV|PRE|FIGURE|TABLE|UL|OL|BLOCKQUOTE|H[1-6]|HR|SECTION|ARTICLE|ASIDE|DETAILS|FORM|FIELDSET|BR|VIDEO|AUDIO|IFRAME)$/;
    const isInlineEl = (node) =>
      node.nodeType === Node.ELEMENT_NODE && !isBlockTag.test(node.tagName);
    let run = [];
    const flush = () => {
      if (!run.length) return;
      const p = document.createElement('p');
      mp.insertBefore(p, run[0]);
      for (const n of run) p.appendChild(n);
      run = [];
    };
    for (const node of Array.from(mp.childNodes)) {
      if (node.nodeType === Node.TEXT_NODE) {
        // Whitespace-Text ohne aktiven Lauf: unsichtbar → stehen lassen.
        if (node.textContent.trim() || run.length) run.push(node);
      } else if (node.nodeType === Node.ELEMENT_NODE) {
        if (isInlineEl(node)) run.push(node);
        else flush();
      }
    }
    flush();

    // (3) Leere Top-Level-<p> entfernen
    for (const p of Array.from(mp.querySelectorAll(':scope > p'))) {
      if (!p.childElementCount && !p.textContent.trim()) p.remove();
    }

    // (4) Trailing <br> am <p>-Ende entfernen, wenn danach ein Block-Element
    // folgt (z. B. manueller Zeilenumbruch via 2 Leerzeichen direkt vor
    // der Token-Zeile — kein absichtlicher Abstand).
    const isBlockEl = (el) => !!(el && isBlockTag.test(el.tagName));
    for (const p of Array.from(mp.querySelectorAll(':scope > p'))) {
      let sib = p.nextSibling;
      while (sib && sib.nodeType === Node.TEXT_NODE && !sib.textContent.trim()) {
        sib = sib.nextSibling;
      }
      if (!sib || sib.nodeType !== Node.ELEMENT_NODE || !isBlockEl(sib)) continue;
      while (p.lastChild) {
        const n = p.lastChild;
        if (n.nodeType === Node.ELEMENT_NODE && n.tagName === 'BR') {
          n.remove();
        } else if (n.nodeType === Node.TEXT_NODE && !n.textContent.trim()) {
          n.remove();
        } else {
          break;
        }
      }
    }
  };
  root.querySelectorAll('.markdown-preview').forEach(clean);
}

// Fragment-Marker (Reveal) auflösen: {#fragment}, {#fragment:id} (ID-Gruppe),
// {#Fragment} (Gate), {#aaid:label} (Auto-Animate-Element-ID → data-id) und
// \htmlClass{fragment…} in Formeln (s. _katexFragRewrite). Der Sentinel ist
// ein Inline-<span>, das marked in das nächste Block-Element einbettet;
// eq/fig tragen data-frag/data-id bereits aus der Restore-Phase. Phasen:
//   1: Sentinel → umgebendes Block-Element (p/li/h*/pre/…) als Host
//      (data-frag/data-frag-id bzw. data-id vom Span auf den Host kopiert;
//      Sonderfälle: leeres <p> nach Codeblock, Marker direkt im Wrapper).
//   1b: \htmlClass{fragment…}-Spans in gerenderten Formeln als Fragment-
//      Hosts markieren (group/normal aus der ID-Klasse) — die inerten
//      aicampus-katex-frag*-Klassen werden entfernt, „enclosing“ bleibt.
//   2: Alle [data-frag]-Elemente + Top-Level-Elemente der Folie (direkte
//      Kinder der .markdown-preview-Wrapper, bei Boxen/Spaltenzeilen der
//      Wrapper-<div>) in Dokumentreihenfolge sammeln.
//   3: Reveal data-fragment-index vergeben (gleicher Index = gleichzeitig
//      eingeblendet): ID-Gruppen teilen sich den Schritt des ersten
//      Vorkommens; ein Gate ("reveal") bekommt einen neuen Schritt, und
//      ALLE nachfolgenden Inhalte der Folie (Dokumentreihenfolge) ohne
//      eigenes Fragment davor bekommen denselben Schritt — explizite
//      Fragments nach dem Gate werden also vom Gate "verschlungen"
//      (ein späteres Gate übertrumpft ein früheres).
//   4: data-frag*-Attribute entfernen ("fragment"-Klasse + Index bleiben;
//      data-id bleibt — es ist Reveal's Auto-Animate-Matching-Key).
function _applyFragmentMarkers(container) {
  const blockRe = /^(P|LI|H[1-6]|PRE|BLOCKQUOTE|FIGURE|TABLE|DIV)$/;
  const isPreviewWrap = (el) => el && el.classList && el.classList.contains('markdown-preview');

  // Phase 1: Sentinel auflösen (Fragmente + Auto-Animate-IDs)
  container.querySelectorAll('span.aicampus-frag-marker, span.aicampus-aaid-marker').forEach((span) => {
    const isAaid = span.classList.contains('aicampus-aaid-marker');
    const fragType = span.getAttribute('data-frag') || 'normal';
    const fragId = span.getAttribute('data-frag-id');
    const aaid = span.getAttribute('data-aaid');
    let el = span.parentElement;
    while (
      el &&
      el !== container &&
      !isPreviewWrap(el) &&
      !blockRe.test(el.tagName) &&
      !el.classList.contains('aicampus-equation')
    ) {
      el = el.parentElement;
    }
    let host = el && el !== container && !isPreviewWrap(el) ? el : null;
    // Leeres <p> direkt nach einem Block (z. B. Codeblock): Marker → Block davor
    if (host && host.tagName === 'P' && !host.textContent.trim() && host.previousElementSibling) {
      host = host.previousElementSibling;
    }
    if (!host) {
      // Marker steht direkt im Wrapper (Codeblock ohne Leerzeile): vorheriges
      // Block-Element nehmen, evtl. dazwischenliegendes <br> überspringen.
      let sib = span.previousElementSibling;
      while (sib && sib.tagName === 'BR') sib = sib.previousElementSibling;
      host = sib && blockRe.test(sib.tagName) ? sib : null;
    }
    if (host) {
      if (isAaid) {
        // Auto-Animate-Element-ID (bleibt dauerhaft, s. Phase-4-Kommentar)
        host.setAttribute('data-id', aaid);
      } else {
        host.setAttribute('data-frag', fragType);
        if (fragId) host.setAttribute('data-frag-id', fragId);
      }
    }
    // Vom Sentinel getrenntes <br> (manueller Zeilenumbruch) wieder entfernen
    if (span.previousElementSibling && span.previousElementSibling.tagName === 'BR') {
      span.previousElementSibling.remove();
    }
    span.remove();
  });

  // Phase 1b: \htmlClass{fragment…} in Formeln (via _katexFragRewrite nach
  // aicampus-katex-frag(-id-<label>) umgeschrieben, s. renderLatex*): die
  // Spans als Fragment-Hosts markieren (ID → Gruppe), inerte Klassen
  // entfernen — ab Phase 2 laufen sie wie jedes [data-frag]-Element.
  container.querySelectorAll('span.aicampus-katex-frag').forEach((span) => {
    let fragId = null;
    for (const c of span.classList) {
      if (c.startsWith('aicampus-katex-fragid-')) fragId = c.slice('aicampus-katex-fragid-'.length);
    }
    span.classList.remove('aicampus-katex-frag');
    if (fragId) span.classList.remove('aicampus-katex-fragid-' + fragId);
    span.setAttribute('data-frag', fragId ? 'group' : 'normal');
    if (fragId) span.setAttribute('data-frag-id', fragId);
    // Marker für Phase 2b (visuelle Lesereihenfolge innerhalb der
    // Gleichung), in Phase 4 wieder entfernt.
    span.setAttribute('data-aicampus-katexfrag', '');
  });

  // Phase 2: Fragment-Elemente + Top-Level-Elemente, Dokumentreihenfolge
  const fragEls = Array.from(container.querySelectorAll('[data-frag]'));
  if (fragEls.length === 0) return;
  const allEls = _collectFragmentEls(container, fragEls);

  // Phase 2b: Katex-Fragmente in visueller Lesereihenfolge (s. Hilf).
  // Beim ersten Render sind die Reveal-Sections noch display:none (Rects=0)
  // → no-op; aicampusResortFragments() macht es später noch einmal.
  _visualKatexRunSort(allEls);

  // Phase 3: Schritt-Indizes
  _assignFragmentSteps(allEls);

  // Phase 4: Die Marker data-frag/data-frag-id/data-aicampus-katexfrag
  // bleiben bewusst stehen: aicampusResortFragments() (slides.js ruft sie
  // auf ready/slidechanged bzw. bei ?print-pdf auf) läuft dieselbe
  // Ableitung erneut, sobald die Folie gelayoutet ist, und braucht sie.
}

// Fragment-Elemente + Top-Level-Elemente der .markdown-preview-Blöcke in
// Dokumentreihenfolge sammeln (die Gate-Logik in _assignFragmentSteps
// braucht die Top-Level-Elemente).
function _collectFragmentEls(container, fragEls) {
  const topLevel = [];
  container.querySelectorAll('.markdown-preview').forEach((mp) => {
    Array.from(mp.children).forEach((c) => topLevel.push(c));
  });
  const allEls = [...new Set([...fragEls, ...topLevel])];
  allEls.sort((a, b) => {
    const pos = a.compareDocumentPosition(b);
    if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
    if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
    return 0;
  });
  return allEls;
}

// Katex-Fragment-Elemente in visueller Lesereihenfolge sortieren.
// KaTeX rendert z. B. \underbrace{a+b}_{c} als munder-Vlist, in dem die
// Subscript-Zelle DOM-mäßig VOR der Haupt-Zelle steht — die Schrittnummern
// würden sonst c→a→b statt a→b→c vergeben. Deshalb NUR aufeinanderfolgende
// katex-Frag-Elemente derselben Gleichung (gleiche .katex-Root) per
// Bounding-Box (top, dann left; Toleranz 2px) neu sortieren. Stabiler Sort
// hält bei Ties die DOM-Reihenfolge. Nicht global visuell: würde z. B.
// die Spaltenreihenfolge einer Spaltenzeile brechen. Ungelayoutete Slides
// (Rects=0) → no-op.
function _visualKatexRunSort(allEls) {
  for (let i = 0; i < allEls.length; ) {
    const first = allEls[i];
    if (!first.hasAttribute('data-aicampus-katexfrag')) { i++; continue; }
    const root = first.closest ? first.closest('.katex') : null;
    let j = i + 1;
    while (
      j < allEls.length &&
      allEls[j].hasAttribute('data-aicampus-katexfrag') &&
      root && allEls[j].closest && allEls[j].closest('.katex') === root
    ) j++;
    if (j - i > 1) {
      const run = allEls.slice(i, j).map((el) => ({ el, r: el.getBoundingClientRect() }));
      run.sort((x, y) => {
        const dTop = x.r.top - y.r.top;
        if (Math.abs(dTop) > 2) return dTop; // erst Zeile
        const dLeft = x.r.left - y.r.left;
        if (Math.abs(dLeft) > 2) return dLeft; // dann Spalte
        return 0; // Tie → DOM-Order (stabil)
      });
      run.forEach((item, k) => { allEls[i + k] = item.el; });
    }
    i = j;
  }
  return allEls;
}

// Fragment-Schritt-Indizes vergeben (Gate-Logik + ID-Gruppen).
function _assignFragmentSteps(allEls) {
  let nextStep = 0;
  let activeGateStep = null;
  const idToStep = {};
  for (const el of allEls) {
    const fragType = el.getAttribute('data-frag');
    const fragId = el.getAttribute('data-frag-id');
    // Ein Top-Level-Element OHNE Marker wird nur fragmentiert, wenn das
    // Gate es verschlingt und KEIN Fragment-Element darin einen eigenen
    // (früheren) Schritt bestimmt.
    const becomesFrag = fragType !== null ||
      (activeGateStep !== null && !el.querySelector('[data-frag]'));
    if (!becomesFrag) continue;

    let step;
    if (fragType === 'reveal') {
      step = nextStep++; // Gate: eigener neuer Schritt
      activeGateStep = step;
    } else if (activeGateStep !== null) {
      step = activeGateStep; // vom Gate verschlungen (auch mit eigenem Marker)
    } else if (fragId && fragId in idToStep) {
      step = idToStep[fragId]; // ID-Gruppe: Schritt des ersten Vorkommens
    } else {
      step = nextStep++;
    }
    if (fragId && !(fragId in idToStep)) idToStep[fragId] = step;
    el.setAttribute('data-fragment-index', String(step));
    el.classList.add('fragment');
  }
}

// Fragment-Schritte neu ableiten, sobald der Container (Folie) gelayoutet
// ist. Beim ersten Render sind die Reveal-Sections noch display:none
// (Reveal-CSS), daher kann die visuelle Run-Sortierung (KaTeX-Underbraces)
// dort nur ein no-op sein. slides.js (wireKatexFragmentResort) ruft das auf
// Reveal's ready/slidechanged (Präsentation/Vorschau) und — im ?print-pdf-
// Modus — synchron auf, sobald html.print-pdf gesetzt ist (noch vor
// Reveal's setupPDF-Fragment-Paginierung). Setzt dieselben Attribute/Klassen
// wie der erste Durchlauf (idempotent).
function aicampusResortFragments(container) {
  if (!container || !container.querySelectorAll) return;
  const fragEls = Array.from(container.querySelectorAll('[data-frag]'));
  if (fragEls.length === 0) return;
  _assignFragmentSteps(_visualKatexRunSort(_collectFragmentEls(container, fragEls)));
}

// Fragment-Typ aus der Markersyntax: {#Fragment} (Groß, ohne ID) = Gate,
// {#fragment:id}/{#Fragment:id} = ID-Gruppe, {#fragment} = normal.
function _fragType(fchar, id) {
  if (fchar === 'F' && !id) return 'reveal';
  return id ? 'group' : 'normal';
}

// Escape HTML special characters to prevent XSS in error messages
function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ─── Xref/Zitationen IN Formeln (Restore-Schritte 5/6 von renderMarkdown) ──
// KaTeX-Textmodus-Sonderzeichen → Escape-Kommandos (einmalige Pass; Quellen
// sind nur Plain-Link-Texte, keine Doppele-Scape). Nur nötig für den
// Link-TEXT — die URL-Gruppe von \href wird von KaTeX roh geparst.
const _TEX_TEXT_ESCAPES = {
  '\\': '\\textbackslash', '&': '\\&', '%': '\\%', '$': '\\$', '#': '\\#',
  '_': '\\_', '{': '\\{', '}': '\\}', '~': '\\textasciitilde',
  '^': '\\textasciicircum', "'": '\\textquotesingle', '"': '\\textquotedbl',
};
const _texSafeText = (s) =>
  String(s).replace(/[\\&%$#_{}~^"']/g, (c) => _TEX_TEXT_ESCAPES[c]);

// KaTeX-Kommandos, deren {}-Argument im TEXT-Modus geparst wird: darin
// wird das \href-Link nackt emittiert (allowedInText) und erbt damit die
// umgebende Schrift (fette Caption bleibt fett).
const _MATH_TEXT_CMDS = new Set([
  'text', 'mbox', 'textrm', 'textit', 'textbf', 'textsc', 'texttt',
  'xleftarrow', 'xrightarrow',
]);

// Liegt Position pos in der LaTeX-Zeichenfolge im TEXT-Modus? Rückwärts-
// Scan: innere offene Gruppe = ERSTES nicht-escapedes { bei Klammer-Tiefe
// 0 (vorher geschlossene Gruppen heben sich via depth auf), der vor dem {
// stehende Kommandoname entscheidet den Modus (_MATH_TEXT_CMDS); alles
// andere (keine Gruppe, mathematische Gruppen wie x_{…}) = Math-Modus.
function _mathXrefInText(latex, pos) {
  let depth = 0;
  for (let i = pos - 1; i >= 0; i -= 1) {
    const c = latex[i];
    const escaped = i > 0 && latex[i - 1] === '\\';
    if (c === '}' && !escaped) {
      depth += 1;
    } else if (c === '{' && !escaped) {
      if (depth > 0) { depth -= 1; continue; }
      let j = i - 1;
      while (j >= 0 && /[ \t]/.test(latex[j])) j -= 1;
      if (j >= 0 && /[a-zA-Z]/.test(latex[j])) {
        let k = j;
        while (k >= 0 && /[a-zA-Z]/.test(latex[k])) k -= 1;
        return _MATH_TEXT_CMDS.has(latex.slice(k + 1, j + 1));
      }
      return false;
    }
  }
  return false;
}

// data-refkey in ALLE \href-Anker des KaTeX-HTMLs injizieren (derselbe
// URL kann zweimal in einer Formel vorkommen → alle Vorkommen). Nötig nur
// bei Zitationen (slides.js: Klick → auto-Quellen-Folie + flash); eq-Links
// navigieren über den plain href.
function _injectMathRefKeys(texHtml, refs) {
  let out = texHtml;
  for (const r of refs) {
    if (!r.key) continue;
    out = out.split(`href="${r.url}"`).join(`href="${r.url}" data-refkey="${r.key}"`);
  }
  return out;
}

// Sanitize LaTeX input to prevent HTML/JS injection.
function sanitizeLatex(latex) {
  let prev = '';
  while (prev !== latex) {
    prev = latex;
    latex = latex.replace(/\[HTML\][\s\S]*?\[TeX\]/gi, '');
  }
  latex = latex.replace(/\[HTML\]/gi, '').replace(/\[TeX\]/gi, '');
  latex = latex.replace(/<\/?script[\s>][^>]*>/gi, '');
  latex = latex.replace(/<\s*[a-zA-Z][^>=]*=[^>]*>/gi, '');
  // @-Xref-/Zitations-Tokens in Formeln auf Render-Pfaden OHNE Ref-Map
  // (Hover-Tooltips, Code-Formeln): → Literal "? kind:label" als aufrechte
  // Text statt KaTeX-Error (@ ist undefiniert). Der Hauptpfad (Schritte 5/6)
  // wandelt die Tokens vorher in \href-Links um → dort kein Effekt.
  latex = latex.replace(
    /@(eq|fig|code|box|tab|sec|kap|bibentry|citep|citet|cite|task):([\p{L}0-9_-]+)/gu,
    (m, kind, label) => `\\text{? ${_texSafeText(kind + ':' + label)}}`,
  );
  return latex;
}

function renderLatexBlock(latex) {
  try {
    return katex.renderToString(_katexFragRewrite(sanitizeLatex(latex)), {
      displayMode: true,
      throwOnError: false,
      trust: _katexTrust,
    });
  } catch (e) {
    return `<pre class="text-red-500 bg-red-50 p-2 rounded">KaTeX Error: ${escapeHtml(e.message)}</pre>`;
  }
}

function renderLatexInline(latex) {
  try {
    return katex.renderToString(_katexFragRewrite(sanitizeLatex(latex)), {
      displayMode: false,
      throwOnError: false,
      trust: _katexTrust,
    });
  } catch (e) {
    return `<span class="text-red-500">\(${escapeHtml(latex)}\)</span>`;
  }
}

// ─── Caption-/Code-Math & Subfigure-Helfer ──────────────────────────────
// $…$-Paare in Captions (Figure/Code/Tabelle/Subfigure/Box) rendern.
// Captions sind Kurztexte → hier (im Gegensatz zum Fließtext) werden ALLE
// $-Paare als Formel gerendert; der Rest wird HTML-escaped.
// Zitations-Link-HTML (Fließtext UND Figuren-Captions, s. renderCaptionRef):
// @cite → Superscript "[N]", @citet → "Autor (Jahr)", @citep → "(Autor, Jahr)";
// unbekannter Key → ❓-Marker (wie im Fließtext). tipAttr = optionales
// data-xref-tip-Attribut (nur Fließtext-Pfad; Caption-HTML läuft durch
// html.replace, wo das dortige $$-Escaping es verballern würde).
function _citeHtml(kind, label, r, href, tipAttr) {
  if (!r) {
    return `<span class="aicampus-xref-broken" title="Quellen-Schlüssel unbekannt — es existiert keine solche Quelle im Kurs">❓ ${kind}:${label}</span>`;
  }
  const a0 = (r.authors && r.authors.length)
    ? r.authors[0] + (r.authors.length > 3 ? ' et al.' : '')
    : '';
  const y = r.year || '';
  if (kind === 'cite') {
    return `<sup><a href="${href}" data-refkey="${label}" class="aicampus-xref aicampus-cite"${tipAttr}>[${r.num}]</a></sup>`;
  }
  if (kind === 'citet') {
    return `<a href="${href}" data-refkey="${label}" class="aicampus-xref"${tipAttr}>${escapeHtml(a0)}${y ? ' (' + escapeHtml(y) + ')' : ''}</a>`;
  }
  return `<a href="${href}" data-refkey="${label}" class="aicampus-xref"${tipAttr}>(${escapeHtml(a0)}${y ? ', ' + escapeHtml(y) : ''})</a>`;
}

function renderCaptionMath(text) {
  let out = '';
  let last = 0;
  const re = /\$([^$\n]+?)\$/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    out += escapeHtml(text.slice(last, m.index)) + renderLatexInline(m[1].trim());
    last = m.index + m[0].length;
  }
  return out + escapeHtml(text.slice(last));
}

// Caption-Text rendern: inline-Math ($...$) + Zitationen (@cite / @citet /
// @citep — für Figuren-Captions; Box-/Code-/Tabellen-Captions bleiben
// Math-only). ctx = captionCtx aus renderMarkdown: { refMap, citedRefs,
// hrefFor }; null/undefined → Math-only wie renderCaptionMath. Zitierte
// Quellen landen in ctx.citedRefs → Quellenverzeichnis (bzw. auto-Quellen-
// Folie in Slides, die den rohen Markdown-Alt-Text selbst scannt).
function renderCaptionRef(text, ctx) {
  if (text == null) return '';
  text = String(text);
  if (!ctx) return renderCaptionMath(text);
  const refs = (ctx.refMap && ctx.refMap.references) || {};
  const re = /@(citep|citet|cite):([\p{L}0-9_-]+)/gu;
  let out = '';
  let last = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    out += renderCaptionMath(text.slice(last, m.index));
    const kind = m[1];
    const label = m[2];
    const r = refs[label];
    if (r && ctx.citedRefs) ctx.citedRefs.push(r);
    out += _citeHtml(kind, label, r, ctx.hrefFor ? ctx.hrefFor(label) : '#', '');
    last = m.index + m[0].length;
  }
  return out + renderCaptionMath(text.slice(last));
}

// Code-LaTeX-Heuristik: sieht ein $…$-Paar im Code nach Math aus?
// (Backslash oder Math-Symbol) — sonst bleiben Shell-Variablen wie
// $HOME $USER literal (einzelne Buchstaben wie $i$ sind mehrdeutig →
// werden NICHT gerendert).
function _codeMathLooksLikeMath(latex) {
  return /\\|[{}^_()=+\-*\/<>≤≥≠∞∑∫√]/.test(latex);
}

// Subfigure-Inner-Text (s. 1e0) in [ {alt, src, height, label, …} ]
// auflösen: pro Medium ![Caption](src) + optional BIS ZU SECHS
// Attribut-Blöcke, die NUR {height=X} und/oder {#fig:label} und — bei
// Videos — die Playback-Attribute {autoplay=…}/{controls=…}/{loop=…}/
// {muted=…} enthalten (je Token max. einmal, beliebige Reihenfolge);
// Lücken zwischen den Innern reines Leerraum; ≥2 Innere. Sonst null
// (Komplex bleibt literal — spiegelt die Server-Seite exakt, s.
// _subfig_inners in services/fig_labels.py).
function _parseSubfigInners(innerText) {
  const inners = [];
  let rest = innerText;
  for (;;) {
    const m = /^!\[([^\]]*)\]\(([^)\s!]+)\)/.exec(rest);
    if (!m) return null;
    rest = rest.slice(m[0].length);
    let height = null;
    let label = null;
    const vid = { autoplay: null, controls: null, loop: null, muted: null };
    for (let ti = 0; ti < 6; ti++) {
      const am = /^[ \t]*(?:\r?\n[ \t]*)?\{([^{}]*)\}/.exec(rest);
      if (!am) break;
      const tok = am[1].trim();
      rest = rest.slice(am[0].length);
      const sm = tok.match(/^#fig:([\p{L}0-9_-]+)$/u);
      const hm = tok.match(/^\.?height=([\d.]+)([a-z]*)$/);
      const vm = tok.match(/^\.?(autoplay|controls|loop|muted)=(true|false)$/i);
      if (sm) {
        if (label !== null) return null; // Duplikat-Label
        label = sm[1];
      } else if (hm) {
        if (hm[2] !== '' && hm[2] !== 'px') return null;
        if (height !== null) return null; // Duplikat-Height
        height = parseFloat(hm[1]);
      } else if (vm && isVideoSrc(m[2])) {
        if (vid[vm[1]] !== null) return null; // Duplikat
        vid[vm[1]] = vm[2].toLowerCase() === 'true';
      } else {
        return null; // unbekanntes Token
      }
    }
    inners.push({ alt: m[1], src: m[2], height, label, ...vid });
    if (rest === '') break;
    const gm = /^[ \t\r\n]+/.exec(rest);
    if (!gm) return null; // keine reine Leerraum-Lücke → ungültig
    rest = rest.slice(gm[0].length);
  }
  return inners.length >= 2 ? inners : null;
}

// Code-LaTeX (z. B. Pseudo-Code): $…$-Paare in <pre><code> als Inline-Formel
// rendern — auf der finalen DOM (NACH dem Syntax-Highlighting; hljs ändert
// textContent nicht, nur die Spans → Offsets bleiben gültig).
//
// WICHTIG: hljs zerteilt Paare gern über mehrere Text-Nodes/Spans — z. B.
// Pseudo-Code voller $-Zeichen wird als Ruby auto-detected ($x, $: … werden
// zu hljs-Variable-Spans, $ am Zeilenende schluckt das \n). Daher
// node-basiertes, deterministisches Ersetzen OHNE splitText und OHNE
// Lösch-Walk zwischen Nodes (beides bricht an solchen Span-Strukturen):
//   1. Alle Text-Nodes mitsamt globalen Offsets EINMAL (vor jeglicher
//      Änderung) sammeln.
//   2. Jeden Node an den Math-Grenzen (Paar-Start/-Ende) seines Bereichs
//      zerlegen: Nicht-Math-Abschnitte bleiben Text-Nodes im ursprünglichen
//      (hljs-)Span → Highlighting bleibt erhalten; Math-Abschnitte fallen
//      weg — und am Paar-START wird genau EIN KaTeX-Span eingesetzt (im
//      Node, der den Start enthält; das Paar wird damit exakt einmal
//      gerendert).
// Paare, die nicht nach Math aussehen (s. _codeMathLooksLikeMath), bleiben
// literal. Idempotent: gerendertes KaTeX enthält keine $-Paare.
function applyCodeMath(root) {
  root.querySelectorAll('pre code').forEach((code) => {
    const full = code.textContent;
    const re = /\$([^$\n]+?)\$/g;
    const pairs = [];
    let m;
    while ((m = re.exec(full)) !== null) {
      if (_codeMathLooksLikeMath(m[1])) {
        pairs.push({ a: m.index, b: m.index + m[0].length, latex: m[1].trim() });
      } else {
        re.lastIndex = m.index + 1; // kein Math-Paar → ab der nächsten $-Position weitersuchen
      }
    }
    if (pairs.length === 0) return;

    const doc = code.ownerDocument;
    const nodes = [];
    let off = 0;
    const walker = doc.createTreeWalker(code, NodeFilter.SHOW_TEXT);
    let tn;
    while ((tn = walker.nextNode())) {
      nodes.push({ node: tn, start: off, end: off + tn.nodeValue.length });
      off += tn.nodeValue.length;
    }

    for (const t of nodes) {
      const node = t.node;
      if (!node.parentNode) continue; // Defensive: nicht mehr im Baum
      const text = node.nodeValue;
      // Grenzen = Node-Ränder + alle Paar-Grenzen im Inneren des Nodes
      const bounds = [t.start, t.end];
      for (const p of pairs) {
        if (p.a > t.start && p.a < t.end) bounds.push(p.a);
        if (p.b > t.start && p.b < t.end) bounds.push(p.b);
      }
      bounds.sort((x, y) => x - y);

      const parts = [];
      for (let i = 0; i + 1 < bounds.length; i++) {
        const a = bounds[i];
        const b = bounds[i + 1];
        if (a === b) continue;
        const inMath = pairs.some((p) => p.a <= a && a < p.b);
        if (inMath) {
          const starter = pairs.find((p) => p.a === a);
          if (starter) parts.push({ kind: 'math', latex: starter.latex });
          // Math-Text selbst fällt weg (wird durch den KaTeX-Span ersetzt)
        } else {
          parts.push({ kind: 'text', text: text.slice(a - t.start, b - t.start) });
        }
      }

      const parent = node.parentNode;
      const ref = node.nextSibling;
      parent.removeChild(node);
      for (const part of parts) {
        let el;
        if (part.kind === 'text') {
          el = doc.createTextNode(part.text);
        } else {
          el = doc.createElement('span');
          el.className = 'aicampus-code-math';
          el.innerHTML = renderLatexInline(part.latex);
        }
        parent.insertBefore(el, ref);
      }
    }
  });
}

// Slide-Mode: Reveal's Highlight-Plugin baut alle Code-Blöcke beim
// Initialisieren neu (hljs.highlightElement = innerHTML-Reset) → Code-LaTeX
// darf erst NACH dem Highlight-Pass gesetzt werden: "ready" hooken (feuert
// nach Plugin-Init). Der Quelltext (inkl. $-Paaren) ist zu dem Zeitpunkt
// unverändert (hljs ändert nur die Spans).
function aicampusWireCodeMath(reveal) {
  reveal.on('ready', () => applyCodeMath(reveal.getRevealElement()));
}

// ─── LaTeX-Fragmente (\fragment{…} bzw. \htmlClass{fragment…}) ───────────
// Formelteile schrittweise einblenden (Slides): \fragment{…} (Kurzform)
// bzw. \htmlClass{fragment}{…}, mit ID: \fragment{id}{…} bzw.
// \htmlClass{fragment:id}{…} (ID-Gruppe = gleichzeitig). KaTeX's
// htmlClass setzt die Klassen ROH auf den Wrapper-Span — aber „fragment"
// wäre Reveal's Fragment-Klasse (falsche Semantik), und „fragment:label"
// ist keine gültige CSS-Klasse → deshalb hier umschreiben auf eigene,
// inerte Klassen. _applyFragmentMarkers (Phase 1b) übernimmt die Spans
// dann als normale Fragment-Hosts (group/normal) und entfernt die
// Klassen; „enclosing“ (KaTeX-Styling) bleibt. Im Skript (ohne slideMode)
// bleiben die Spans inaktive Elemente — kein visueller Unterschied.
const KATEX_FRAG_CLASS = 'aicampus-katex-frag';
const KATEX_FRAG_ID_PREFIX = 'aicampus-katex-fragid-';

function _katexFragRewrite(latex) {
  return latex
    // Kurzform \fragment{…}: zuerst die ID-Variante (zwei Argumente:
    // {id} + {content}, erkennbar am { direkt nach der ID-Gruppe), dann
    // die plain-Variante als reiner Prefix-Tausch (Content bleibt
    // unangetastet, auch mit geschachtelten Klammern).
    .replace(/\\fragment\{([\p{L}0-9_-]+)\}(?=\{)/gu, '\\htmlClass{fragment:$1}')
    .replace(/\\fragment\{/g, '\\htmlClass{fragment}{')
    .replace(/\\htmlClass\{fragment:([\p{L}0-9_-]+)\}/gu, '\\htmlClass{' + KATEX_FRAG_CLASS + ' ' + KATEX_FRAG_ID_PREFIX + '$1}')
    .replace(/\\htmlClass\{fragment\}/g, '\\htmlClass{' + KATEX_FRAG_CLASS + '}');
}

// KaTeX-Trust-Funktion (ersetzt trust:false): erlaubt NUR \htmlClass mit
// genau unseren Fragment-Klassen und \href mit den internen
// Xref-/Zitations-URLs aus Formeln (KATEX_HREF_TRUST_RE, Schritte 5/6 von
// renderMarkdown) — alle anderen Trust-Kommandos (\htmlClass mit fremden
// Klassen, \href mit externen URLs, \url, \includegraphics,
// \htmlId/\htmlStyle/\htmlData) bleiben abgelehnt (roter
// Unsupported-Command-Text, Rest der Formel rendert normal).
const KATEX_FRAG_TRUST_RE = new RegExp('^' + KATEX_FRAG_CLASS + '( ' + KATEX_FRAG_ID_PREFIX + '[\\p{L}0-9_-]+)?$', 'u');
// Striktes Allow-List der von _mathXrefsToLatex generierten hrefs:
// In-Page-Anker (#eq:, #ref-…) und kursinterne Routen. Alles andere
// (z. B. LLM-generiertes \href) bleibt untrusted.
const KATEX_HREF_TRUST_RE =
  /^(?:#eq:[\p{L}0-9_-]+|#ref-[\p{L}0-9_-]+|\/courses\/[\w-]+\/(?:script#eq:[\p{L}0-9_-]+|references#ref-[\p{L}0-9_-]+|slides\/[\w-]+\/present#\/\d+\/\d+))$/u;
function _katexTrust(ctx) {
  if (!ctx) return false;
  if (ctx.command === '\\htmlClass') return KATEX_FRAG_TRUST_RE.test(String(ctx.class || ''));
  if (ctx.command === '\\href') return KATEX_HREF_TRUST_RE.test(String(ctx.url || ''));
  return false;
}

// Syntax-Highlighting (hljs, global aus base.html): alle Sprachen mit
// language-<lang>-Klasse; ohne/unknown Sprache = Auto-Detection. Die
// übrigen Attribute (data-line-numbers für Slides) überleben den Rebuild;
// das class-Attribut wird neu gesetzt (Sprache + hljs-Markierung).
// Im Slide-Modus überstreicht das Reveal-Highlight-Plugin das Ergebnis
// idempotent (gleiche hljs-Version) und baut zusätzlich die
// data-line-numbers-Tabelle/Fragmente auf.
function highlightCodeBlocks(html) {
  // <pre> darf Attribute tragen (data-id für {#aaid:…}-Code-Blöcke) — die
  // überleben den Rebuild unverändert (preAttrs).
  return html.replace(/<pre([^>]*)><code([^>]*)>([\s\S]*?)<\/code><\/pre>/g, (match, preAttrs, attrs, code) => {
    const decoded = code
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .replace(/&amp;/g, '&')
      .replace(/&#39;/g, "'")
      .replace(/&quot;/g, '"');

    const otherAttrs = attrs.replace(/\s*class="[^"]*"/, '').trim();
    const langMatch = /class="language-([\w+-]+)"/.exec(attrs);
    const language = langMatch ? langMatch[1] : null;

    if (typeof hljs !== 'undefined') {
      let highlighted = null;
      try {
        const src = decoded.trim();
        if (src) {
          highlighted = (language && hljs.getLanguage(language))
            ? hljs.highlight(src, { language }).value
            : hljs.highlightAuto(src).value;
        }
      } catch (e) {
        highlighted = null; // Fallback unten: escaped, unverändert
      }
      if (highlighted !== null) {
        const cls = (language ? `language-${language} ` : '') + 'hljs';
        return `<pre${preAttrs}><code${otherAttrs ? ` ${otherAttrs}` : ''} class="${cls}">${highlighted}</code></pre>`;
      }
    }

    const escaped = decoded
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');

    return `<pre${preAttrs}><code${attrs}>${escaped}</code></pre>`;
  });
}

function decodeTextEntities(html) {
  const parts = html.split(/(<pre[^>]*>[\s\S]*?<\/pre>|<span class="katex(?:-display)?">[\s\S]*?<\/span>|<div class="mermaid-diagram">[\s\S]*?<\/div>|<[^>]*>)/g);
  return parts.map((part, i) => {
    if (i % 2 === 0) {
      return part.replace(/&lt;/g, '<').replace(/&gt;/g, '>');
    }
    return part;
  }).join('');
}

// ─── Mermaid Diagram Rendering ──────────────────────────────────────────

/**
 * Mermaid-Flowcharts: Knotentext mit Sonderzeichen (z.B. `C[H(X) = log2(n)]`)
 * ist ohne Anführungszeichen nicht parsebar. Für Flowcharts werden
 * unquoted [..]/{..}-Labels automatisch angeführt: C["H(X) = log2(n)"].
 * Bereits angeführte Labels sowie andere Diagrammtypen
 * (Sequence, Class, ER, …) bleiben unverändert.
 */
function sanitizeFlowchartLabels(text) {
  const firstLine = text.split('\n').map(l => l.trim()).find(l => l && !l.startsWith('%%')) || '';
  if (!/^(graph|flowchart)\b/i.test(firstLine)) return text;
  return text
    .replace(/\b([A-Za-z0-9_][A-Za-z0-9_-]*)\[([^\[\]"]*)\]/g, (m, id, label) => {
      return /[(){}<>]/.test(label) ? `${id}["${label}"]` : m;
    })
    .replace(/\b([A-Za-z0-9_][A-Za-z0-9_-]*)\{([^\{\}"]*)\}/g, (m, id, label) => {
      return /[(){}<>]/.test(label) ? `${id}{"${label}"}` : m;
    });
}

async function renderMermaid(diagramText) {
  if (typeof mermaid === 'undefined') {
    return `<pre class="text-orange-500 bg-orange-50 p-2 rounded">Mermaid not loaded</pre>`;
  }

  diagramText = sanitizeFlowchartLabels(diagramText);

  try {
    const { svg: rawSvg } = await mermaid.render(`mermaid-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, diagramText);
    // securityLevel 'loose' rendert HTML in Labels ungefiltert, und die SVG-
    // Einsetzung in renderMarkdown (Schritt 11) erfolgt NACH dem zentralen
    // DOMPurify-Pass (Schritt 10) — hier daher das SVG selbst sanitisieren
    // (entfernt on*-Handler/javascript:-URIs).
    // DOMPurify-Default würde Flowchart-Labels komplett verlieren:
    // Mermaid rendert sie in <foreignObject> (nicht in der Default-
    // Allow-Liste), und den HTML-Kindern darin (Labels, KaTeX-MathML) wird
    // der SVG→HTML-Namenraumwechsel verweigert, außer der Elterntag steht
    // in HTML_INTEGRATION_POINTS (Keys müssen lowercase sein).
    const svg = typeof DOMPurify !== 'undefined' ? DOMPurify.sanitize(rawSvg, {
      ADD_TAGS: ['foreignObject'],
      HTML_INTEGRATION_POINTS: { 'annotation-xml': true, foreignobject: true, desc: true, title: true },
    }) : rawSvg;
    const temp = document.createElement('div');
    temp.innerHTML = svg;
    const svgEl = temp.querySelector('svg');
    const viewBox = svgEl?.getAttribute('viewBox');
    const parts = viewBox?.split(/\s+/);
    const svgWidth = parts && parts.length >= 3 ? parseFloat(parts[2]) : 800;
    svgEl?.setAttribute('width', `${Math.round(svgWidth)}px`);
    svgEl?.removeAttribute('height');
    svgEl?.setAttribute('style', 'max-width:100%;height:auto;display:block');
    return `<div class="mermaid-diagram flex justify-center">${temp.innerHTML}</div>`;
  } catch (e) {
    return `<pre class="text-red-500 bg-red-50 p-2 rounded">Mermaid Error: ${escapeHtml(e.message)}\n\n${escapeHtml(diagramText)}</pre>`;
  }
}

/**
 * Create a Markdown editor with a toggle between edit and preview mode.
 */
function createMarkdownEditor(containerId, options = {}) {
  const container = document.getElementById(containerId);
  if (!container) {
    console.warn(`createMarkdownEditor: Container #${containerId} not found`);
    return null;
  }

  const textarea = (options.textareaId
    ? (document.getElementById(options.textareaId) || null)
    : container.querySelector('textarea'));
  if (!textarea) {
    console.warn(`createMarkdownEditor: No textarea found in #${containerId}`);
    return null;
  }

  // CodeMirror (falls geladen): Markdown-Editor mit Syntax-Highlighting.
  // Helliges Design: CM-Default-Theme (weißer Hintergrund) — das
  // One-Dark-Theme greift nur für explizit damit initialisierte Code-Editoren.
  // Hinweis: Solange der Editor aktiv ist, enthält das versteckte Textarea
  // nicht den Dokumentinhalt — Werte immer über getValue()/cm.getValue()
  // lesen (cm.save() schreibt sauberen Text zurück ins Textarea).
  const useCM = (typeof CodeMirror !== 'undefined') &&
    !!(CodeMirror.modes && (CodeMirror.modes.gfm || CodeMirror.modes.markdown)) &&
    (options.codeMirror !== false);
  // CM5 versteckt das Original-Textarea (display:none) — ein "required"-
  // Attribut darauf bricht die native Formularvalidierung ("is not
  // focusable"). Wir entfernen es und geben die Pflichtigkeit via
  // editor.required an die Seite weiter (dort per JS validieren).
  const wasRequired = textarea.hasAttribute('required');
  let cm = null;
  if (useCM) {
    textarea.removeAttribute('required');
    // GFM/Markdown-Basismode + AICampus-Annotationen-Overlay (Formeln,
    // Boxen, Labels, Referenzen) — s. codemirror-mode-aicampus.js.
    const baseMode = CodeMirror.modes.gfm ? 'gfm' : 'markdown';
    // Wichtig: Modus-NAMENSSTRING übergeben, nicht die Factory-Funktion aus
    // CodeMirror.modes — eine Funktion als Mode-Spec löst CM5 stumm auf
    // "text/plain" auf (kein Highlighting, kein Fehler).
    const mode = CodeMirror.modes['aicampus-markdown'] ? 'aicampus-markdown' : baseMode;
    // Feste Höhe = Originalhöhe des Textareas (CM5-Default wäre 300 px);
    // bei größeren Inhalten scrollt der Editor intern wie das alte Textarea.
    const wrapperHeight = textarea.offsetHeight || 300;
    cm = CodeMirror.fromTextArea(textarea, {
      mode: mode,
      lineNumbers: true,
      lineWrapping: true,
      matchBrackets: true,
      indentUnit: 2,
      // Find & Replace (Search-Addons, s. base.html):
      // Ctrl-F persistent Suchdialog (sucht beim Tippen mit), F3/Shift-F3
      // nächste/vorige Übereinstimmung, Ctrl-Shift-F Ersetzen, Ctrl-Shift-R
      // alle ersetzen (Standard-Keymap), Ctrl-L zur Zeile springen.
      extraKeys: {
        "Ctrl-F": "findPersistent",
        "F3": "findNext",
        "Shift-F3": "findPrev",
      },
      // Dialog-Texte der Search-Addons (Standard: Englisch) ins Deutsche
      phrases: {
        "Search:": "Suchen:",
        "(Use /re/ syntax for regexp search)": "(regulärer Ausdruck: /re/)",
        "Replace:": "Ersetzen:",
        "Replace all:": "Alle ersetzen:",
        "Replace with:": "Ersetzen durch:",
        "With:": "Durch:",
        "Replace?": "Ersetzen?",
        "Yes": "Ja",
        "No": "Nein",
        "All": "Alle",
        "Stop": "Stopp",
        "Jump to line:": "Zur Zeile:",
        "(Use line:column or scroll% syntax)": "(Zeile:Spalte oder scroll%)",
      },
    });
    const wrapper = cm.getWrapperElement();
    wrapper.classList.add('md-codemirror', 'w-full');
    wrapper.style.height = wrapperHeight + 'px';
    // Optional: Höhe anpassbar (Ziehhgriff unten rechts). Der Griff
    // verlangt overflow != visible; der Inhalt passt sich der neuen Höhe
    // automatisch an (CM6-ResizeObserver). resizableKey: Höhe pro Seite
    // merken (localStorage, überlebt Reloads).
    if (options.resizable) {
      wrapper.style.overflow = "auto";
      wrapper.style.resize = "vertical";
      if (options.resizableKey) {
        const stored = localStorage.getItem(options.resizableKey);
        if (stored) wrapper.style.height = stored + "px";
        wrapper.addEventListener("resize", () => {
          localStorage.setItem(options.resizableKey, String(wrapper.offsetHeight));
        });
      }
    }
  }

  // resizable ohne CodeMirror: natives Textarea-Resize (overflow ist
  // bereits auto).
  if (options.resizable && !cm) {
    textarea.style.resize = "vertical";
    if (options.resizableKey) {
      const stored = localStorage.getItem(options.resizableKey);
      if (stored) textarea.style.height = stored + "px";
      textarea.addEventListener("resize", () => {
        localStorage.setItem(options.resizableKey, String(textarea.offsetHeight));
      });
    }
  }

  const previewDiv = document.createElement('div');
  previewDiv.className = 'markdown-preview-area hidden min-h-[200px] border border-gray-300 rounded-lg p-4 bg-white overflow-y-auto';

  const toggleBtn = document.createElement('button');
  toggleBtn.type = 'button';
  toggleBtn.className = 'text-gray-500 hover:text-gray-700 text-sm px-3 py-1.5 rounded-lg border border-gray-300 hover:bg-gray-50 transition inline-flex items-center gap-1.5';
  toggleBtn.innerHTML = '<span>👁️</span> <span>Preview</span>';

  // Preview unterhalb des (CodeMirror-)Editors einhängen. CM6 (via Shim)
  // zieht das Textarea in den .ta-cm-Wrapper, daher relativ zum
  // Editor-Element selbst einhängen (textarea.parentNode wäre der Wrapper).
  // (Seiten mit externalToggle — z. B. Skript-Edit-Modus — dürfen das
  // previewDiv danach in ihr eigenes Layout verschieben.)
  (cm ? cm.getWrapperElement() : textarea).insertAdjacentElement('afterend', previewDiv);

  const anchorId = options.buttonAnchor;
  // externalToggle: die Seite steuert den Modus selbst (setMode()/eigene
  // Buttons, z. B. die Edit/Preview-Gruppe im Skript-Edit-Modus) → kein
  // internes Preview-Button.
  if (!options.externalToggle) {
    if (anchorId) {
      const anchorEl = document.getElementById(anchorId);
      if (anchorEl) {
        anchorEl.appendChild(toggleBtn);
      } else {
        toggleBtn.style.marginTop = '0.5rem';
        previewDiv.insertAdjacentElement('beforebegin', toggleBtn);
      }
    } else {
      toggleBtn.style.marginTop = '0.5rem';
      previewDiv.insertAdjacentElement('beforebegin', toggleBtn);
    }
  }

  let isPreview = false;

  // Markdown-Optionen für Preview + Auto-Update (sectionId → globale Nummerierung)
  const mdRenderOptions = {
    preview: true,
    sectionId: options.sectionId || null,
    bibliography: !!options.bibliography, // Ganze Dokumente: „Quellen“-Liste in der Preview
    sourceLines: !!options.livePreview,   // Split-View: data-src-line für den Scroll-Sync
  };

  // Preview rendern; options.onPreviewRender wird danach aufgerufen
  // (z. B. um Skript-Fragen-Markierungen im Edit-Modus neu anzuwenden)
  // getValue() ist hier eine Closure auf die unten definierte Konstante —
  // renderPreview wird erst nach deren Initialisierung aufgerufen.
  const renderPreview = async () => {
    await renderMarkdown(getValue(), previewDiv, mdRenderOptions).catch(() => {});
    if (options.onPreviewRender) options.onPreviewRender();
  };

  // Edit/Preview-Umschalten — intern (Button) oder per setMode(), wenn die
  // Seite den Modus selbst steuert (externalToggle).
  function setMode(mode) {
    const next = mode === 'preview';
    if (next === isPreview) return;
    isPreview = next;
    if (isPreview) {
      if (cm) cm.getWrapperElement().classList.add('hidden');
      else textarea.classList.add('hidden');
      previewDiv.classList.remove('hidden');
      if (toggleBtn.isConnected) {
        toggleBtn.innerHTML = '<span>✏️</span> <span>Edit</span>';
        toggleBtn.classList.add('bg-blue-50', 'border-blue-300', 'text-blue-700');
      }
      renderPreview();
    } else {
      previewDiv.classList.add('hidden');
      if (cm) {
        cm.getWrapperElement().classList.remove('hidden');
        cm.refresh(); // Größen neu berechnen (Wrapper war versteckt)
      } else {
        textarea.classList.remove('hidden');
      }
      if (toggleBtn.isConnected) {
        toggleBtn.innerHTML = '<span>👁️</span> <span>Preview</span>';
        toggleBtn.classList.remove('bg-blue-50', 'border-blue-300', 'text-blue-700');
      }
    }
  }
  toggleBtn.addEventListener('click', () => setMode(isPreview ? 'edit' : 'preview'));

  // Sauberer Wert, unabhängig vom Editorzustand
  const getValue = () => (cm ? cm.getValue() : textarea.value);

  let updateTimeout = null;
  const onValueChanged = () => {
    // livePreview: auch im Edit-Modus aktuell halten (z. B. die
    // Preview/Editor-Split-Ansicht im Skript-Edit-Modus) — aber nur,
    // solange die Preview tatsächlich sichtbar ist (versteckte Preview
    // nicht bei jedem Tippen neu rendern)
    if (isPreview || (options.livePreview && previewDiv.offsetParent !== null)) {
      clearTimeout(updateTimeout);
      updateTimeout = setTimeout(renderPreview, 300);
    }
    if (options.onValueChange) {
      options.onValueChange(getValue());
    }
  };
  if (cm) {
    cm.on('change', onValueChanged);
  } else {
    textarea.addEventListener('input', onValueChanged);
  }

  // Optional: direkt im Preview-Modus starten (z. B. für LLM-generierte Inhalte)
  if (options.startInPreview) {
    setMode('preview');
  }

  return {
    textarea,
    cm,
    previewDiv,
    toggleBtn,
    isPreview: () => isPreview,
    getValue,
    required: wasRequired,
    renderPreview,
    setMode,
    setValue(v) {
      if (cm) cm.setValue(v);
      else textarea.value = v;
    },
  };
}

// ─── Xref-Hover-Previews (data-xref-tip) ────────────────────────────────
// Beim Hovern auf eine aufgelöste Referenz (@eq:/@fig:/@code:/@box:/@sec:)
// zeigt ein einzelnes Tooltip-Element eine Vorschau des Zielobjekts an:
// Gleichungen gerendert per KaTeX, Code in <pre>, Boxen/Abschnitte/Abb.
// als (serverseitig auf ~320 Zeichen gekürzter) Text. Die Previews selbst
// liefert die Ref-Map (Feld "preview" pro Label); das Attribut
// data-xref-tip ist JSON {k: kind, p: preview} ("-escaped fürs HTML).
let _xrefTipEl = null;
let _xrefTipBound = false;
let _xrefTipTimer = null; // ausstehende Show (Delay)
let _xrefTipPending = null; // Anker, für den gelayt wird
let _xrefTipShown = null; // Anker, dessen Vorschau gerade sichtbar ist

function _ensureXrefTipEl() {
  if (_xrefTipEl) return _xrefTipEl;
  const el = document.createElement('div');
  el.id = 'aicampus-xref-tip';
  el.innerHTML =
    '<div class="aicampus-xref-tip-title"></div><div class="aicampus-xref-tip-body"></div>';
  document.body.appendChild(el);
  _xrefTipEl = el;
  return el;
}

// Tooltip-Text mit optionalen Formeln: Rest HTML-escaped, $…$-Spans (und
// $$…$$-Blöcke) per KaTeX gerendert — falls die Lib geladen ist, sonst
// Rohtext der Formel. (Wichtig für Box-Previews mit Inline-Math.)
function _xrefTipRenderText(text) {
  const out = [];
  let last = 0;
  const re = /\$\$([\s\S]+?)\$\$|\$([^$\n]+?)\$/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(escapeHtml(text.slice(last, m.index)));
    if (typeof katex !== "undefined") {
      out.push(m[1] != null ? renderLatexBlock(m[1]) : renderLatexInline(m[2]));
    } else {
      out.push(escapeHtml(m[1] != null ? m[1] : m[2]));
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(escapeHtml(text.slice(last)));
  return out.join("");
}

function _xrefTipSetContent(anchor) {
  let data = null;
  try {
    data = JSON.parse(anchor.getAttribute('data-xref-tip') || '');
  } catch (e) {
    data = null;
  }
  if (!data || typeof data.p !== 'string' || !data.p) return false;
  const tip = _ensureXrefTipEl();
  tip.querySelector('.aicampus-xref-tip-title').textContent = anchor.textContent.trim();
  const body = tip.querySelector('.aicampus-xref-tip-body');
  if (data.k === 'eq') {
    // LaTeX-Vorschau: KaTeX falls geladen, sonst Rohtext.
    if (typeof katex !== 'undefined') {
      body.innerHTML = renderLatexBlock(data.p);
    } else {
      body.innerHTML = '<pre class="aicampus-xref-tip-code">' + escapeHtml(data.p) + '</pre>';
    }
  } else if (data.k === 'code') {
    body.innerHTML =
      '<pre class="aicampus-xref-tip-code"><code>' + escapeHtml(data.p) + '</code></pre>';
  } else {
    // fig / box / sec: Text mit optionalen Formeln (KaTeX, pre-wrap im CSS).
    body.innerHTML =
      '<span class="aicampus-xref-tip-text">' + _xrefTipRenderText(data.p) + '</span>';
  }
  return true;
}

function _xrefTipHide() {
  if (_xrefTipTimer) {
    clearTimeout(_xrefTipTimer);
    _xrefTipTimer = null;
  }
  _xrefTipPending = null;
  _xrefTipShown = null;
  if (_xrefTipEl) _xrefTipEl.style.display = 'none';
}

function _xrefTipPosition(anchor) {
  const r = anchor.getBoundingClientRect();
  const tip = _xrefTipEl;
  // Sichtbarkeits-Trick: display:block + hidden → messen → positionieren → zeigen.
  tip.style.display = 'block';
  tip.style.visibility = 'hidden';
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;
  let left = r.left;
  left = Math.max(8, Math.min(left, window.innerWidth - tw - 8));
  let top = r.bottom + 6;
  if (top + th > window.innerHeight - 8) {
    top = r.top - th - 6; // Platz unten nicht ausreichend → über den Anker
  }
  if (top < 8) top = 8;
  tip.style.left = left + 'px';
  tip.style.top = top + 'px';
  tip.style.visibility = 'visible';
}

function _xrefTipShow(anchor) {
  if (!_xrefTipSetContent(anchor)) return;
  _xrefTipPosition(anchor);
}

function _bindXrefTip() {
  if (_xrefTipBound || typeof document === 'undefined' || !document.body) return;
  _xrefTipBound = true;
  _ensureXrefTipEl();
  document.addEventListener('mouseover', (e) => {
    const a =
      e.target && e.target.closest ? e.target.closest('a.aicampus-xref[data-xref-tip]') : null;
    if (a === _xrefTipPending) return; // bereits in Bearbeitung
    if (_xrefTipTimer) {
      clearTimeout(_xrefTipTimer);
      _xrefTipTimer = null;
    }
    // Vorschau eines anderen Ankers ist nicht mehr passend → sofort aus
    // (vorher setzen, weil _xrefTipHide() auch _xrefTipPending zurücksetzt).
    if (_xrefTipShown && _xrefTipShown !== a) _xrefTipHide();
    _xrefTipPending = a;
    if (!a) return;
    _xrefTipTimer = setTimeout(() => {
      _xrefTipTimer = null;
      if (_xrefTipPending === a) {
        _xrefTipShow(a);
        _xrefTipShown = a;
      }
    }, 350);
  });
  // Touch-Long-Press = Preview (über emuliertes mouseover): das native
  // Link-Kontextmenü (iOS-Callout / Android-Menü) nur unterdrücken, wenn
  // für diesen Anker gerade eine Vorschau aktiv ist oder ansteht — sonst
  // bleibt „Link in neuem Tab öffnen“ etc. für normale Links erhalten.
  document.addEventListener("contextmenu", (e) => {
    const a =
      e.target && e.target.closest ? e.target.closest('a.aicampus-xref[data-xref-tip]') : null;
    if (a && (a === _xrefTipShown || a === _xrefTipPending)) e.preventDefault();
  });
  // Beim Scrollen (capture: auch Scroll-Container) / Resizen / Klicken ausblenden.
  document.addEventListener('scroll', _xrefTipHide, true);
  window.addEventListener('resize', _xrefTipHide);
  document.addEventListener('click', _xrefTipHide);
}

if (typeof document !== 'undefined' && document.body) {
  _bindXrefTip();
} else if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', _bindXrefTip);
}

// ─── Quellen-Deep-Links (#ref-{key}) ──────────────────────────────────
// In-Page-Anker in der Quellenliste scrollen den Eintrag in die
// Viewport-Mitte (statt Standard-Top-Align, das unter der sticky Nav-Bar
// verschwindet). Kurzer Ring-Highlight wie beim Deep-Link im Quellen-Tab.
function _bindRefAnchorScroll() {
  document.addEventListener('click', (e) => {
    const a = e.target && e.target.closest ? e.target.closest('a[href^="#ref-"]') : null;
    if (!a) return;
    const el = document.getElementById(a.getAttribute('href').slice(1));
    if (!el) return;
    e.preventDefault();
    history.replaceState(null, '', a.getAttribute('href'));
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.classList.add('ring-2', 'ring-blue-400', 'rounded-md');
    setTimeout(() => el.classList.remove('ring-2', 'ring-blue-400', 'rounded-md'), 2000);
  });
}

if (typeof document !== 'undefined' && document.body) {
  _bindRefAnchorScroll();
} else if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', _bindRefAnchorScroll);
}
