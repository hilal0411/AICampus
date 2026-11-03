// CodeMirror 6 Setup — lädt den CM6-Kern (jsDelivr-CDN, per Importmap
// pinned Versionen) und verbindet ihn mit dem Shim
// (static/js/codemirror6-shim.js).
//
// Alle Templates nutzen weiterhin die CM5-kompatible API
// (CodeMirror.fromTextArea + Editor-Methoden); die Übersetzung auf CM6
// passiert hier.
//
// Kein CSS-Link für den Core: @codemirror/view injiziert die Basistyle
// zur Laufzeit (style-mod). Eigene Regeln (Rahmen, Fallback-Textarea,
// Such-Treffer) stehen in main.css.
//
// Versions-Pinning: ALLE Pakete (inkl. der internen @lezer/*-Parser) werden
// über das INLINE-Importmap in base.html (JSON: templates/codemirror6-
// importmap.json) auf exakt eine URL pro Paket gemappt.
// WICHTIG: nie einzelne +esm-/CDN-URLs direkt hier importieren — jsDelivr
// löst dann die internen Bare-Imports auf (ggf. neuere) Versionen auf und
// es landen zwei Instanzen von @codemirror/state im Graphen
// ("Unrecognized extension value" / broken instanceof).
// Neue Sprachen: Bare-Specifier hier importieren + Eintrag ins Importmap.

// ── CM6-Core (via Importmap, pinned) ────────────────────────────────
import { EditorState, Compartment, StateField, RangeSetBuilder } from "@codemirror/state";
import { EditorView, Decoration, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentMore, indentLess } from "@codemirror/commands";
import { closeBrackets, closeBracketsKeymap, autocompletion, completeFromList } from "@codemirror/autocomplete";
import { search, searchKeymap, gotoLine } from "@codemirror/search";
import { StreamLanguage, bracketMatching, indentOnInput, HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { tags as t } from "@lezer/highlight";
import { markdown } from "@codemirror/lang-markdown";
import { python } from "@codemirror/lang-python";
import { html } from "@codemirror/lang-html";
import { css } from "@codemirror/lang-css";
import { javascript } from "@codemirror/lang-javascript";
import { json } from "@codemirror/lang-json";
import { cpp } from "@codemirror/lang-cpp";
import { sql } from "@codemirror/lang-sql";
import { yaml } from "@codemirror/lang-yaml";
import { xml } from "@codemirror/lang-xml";
import { oneDark } from "@codemirror/theme-one-dark";
// CM5-Legacy-Modi (StreamLanguage-Definitionen, via Importmap gemappt)
import { shell } from "@codemirror/legacy-modes/mode/shell";
import { nginx } from "@codemirror/legacy-modes/mode/nginx";
import { toml } from "@codemirror/legacy-modes/mode/toml";
import { properties } from "@codemirror/legacy-modes/mode/properties";

// Seit @codemirror/commands 6.11.0 (und analog search/autocomplete) liefern
// diese Pakete ihre Keymaps als ROHE Binding-Arrays statt als keymap.of(...)-
// Extensions. @codemirror/state kann das nicht flatten →
// "Unrecognized extension value in extension set". Die Umwicklung ist in
// einer der beiden Varianten korrekt, damit beide Paketspielarten laufen.
const keymapExt = (bindings) => (Array.isArray(bindings) ? keymap.of(bindings) : bindings);

// ── AICampus-Annotationen (Port von codemirror-mode-aicampus.js) ────────
// StateField statt Overlay-Mode: Ein Scan über das Dokument legt die
// cm-ta-*-Klassen AUF das Markdown-Highlighting. Regelreihenfolge und
// Verhalten stimmen 1:1 mit dem CM5-Overlay überein — inkl. der
// Eigenart, dass ein öffnender Backtick den Rest der Zeile "verschlingt".
const FENCE_OPEN = /^[ \t]*(?:`{3,}|~{3,})/;
const FENCE_CLOSE = /^[ \t]*(?:`{3,}|~{3,})[ \t]*$/;
const BLOCK_MATH_CLOSE = /^\s*[^$]*\$\$/;
const MATH_SAME_LINE = /[^$]*\$\$/;
const MATH_INLINE = /^\$[^$]*\$/;
const RE_STARTBOX = /^@startbox(?::[\w-]+)?/;
const RE_ENDBOX = /^@endbox\b/;
const RE_STARTCOL = /^@startcolumn(?::[0-9]+(?:\.[0-9]+)?)?/;
const RE_NEXTCOL = /^@nextcolumn(?::[0-9]+(?:\.[0-9]+)?)?/;
const RE_ENDCOL = /^@endcolumn\b/;
const RE_LABEL = /^\{[#.][^}\n]*\}/;
const RE_ZOOM = /^\{zoom=\d+(?:\.\d+)?\}/;
const RE_REF = /^@[A-Za-z_][\w-]*:[\w./()#-]+/;
const RE_PLAIN = /^[^$`@{]+/;

const DEC = {
  math: Decoration.mark({ class: "cm-ta-math" }),
  box: Decoration.mark({ class: "cm-ta-box" }),
  label: Decoration.mark({ class: "cm-ta-label" }),
  ref: Decoration.mark({ class: "cm-ta-ref" }),
};

function scanAnnotations(doc) {
  const builder = new RangeSetBuilder();
  let fence = false;
  let blockMath = false;
  // iterLines() liefert pro Zeile nur den TEXT (String) — der Zeilen-
  // Offset wird manuell mitgeführt (vor JEDEM Ausstieg aus der Schleife
  // aktualisieren, nicht nur am Ende).
  let lineFrom = 0;
  for (const text of doc.iterLines()) {
    const lineTo = lineFrom + text.length;
    // Fenced-Codeblock: innen keine Annotationen, auch die schließende
    // Zeile bleibt unmarkiert.
    if (fence) {
      if (FENCE_CLOSE.test(text)) fence = false;
      lineFrom = lineTo + 1;
      continue;
    }
    let p = 0;
    // Mehrzeilige Formel: schließendes $$ suchen — bis dorthin markiert,
    // danach weiter mit den normalen Regeln (Rest der Zeile).
    if (blockMath) {
      const m = BLOCK_MATH_CLOSE.exec(text);
      if (m) {
        builder.add(lineFrom, lineFrom + m[0].length, DEC.math);
        blockMath = false;
        p = m[0].length;
      } else {
        builder.add(lineFrom, lineTo, DEC.math);
        lineFrom = lineTo + 1;
        continue;
      }
    }
    while (p < text.length) {
      const rest = text.slice(p);
      let m;
      // Fence-Öffnung (nur am Zeilenanfang; Labels auf der Fence-Zeile
      // werden weiterhin markiert).
      if (p === 0 && (m = FENCE_OPEN.exec(text))) {
        fence = true;
        p += m[0].length;
        continue;
      }
      // Display-Formel $$…$$ (schließt ggf. auf derselben Zeile)
      if (rest.startsWith("$$")) {
        const close = MATH_SAME_LINE.exec(rest.slice(2));
        if (close) {
          builder.add(lineFrom + p, lineFrom + p + 2 + close[0].length, DEC.math);
          p += 2 + close[0].length;
        } else {
          builder.add(lineFrom + p, lineTo, DEC.math);
          blockMath = true;
          p = text.length;
        }
        continue;
      }
      // Inline-Formel $…$
      if ((m = MATH_INLINE.exec(rest))) {
        builder.add(lineFrom + p, lineFrom + p + m[0].length, DEC.math);
        p += m[0].length;
        continue;
      }
      // Inline-Code: öffnender Backtick verschlingt den Rest der Zeile
      // (CM5-Quirk, bewusst erhalten).
      if (rest[0] === "`") { p = text.length; continue; }
      // Boxen/Spalten (vor der generischen @typ:label-Regel)
      if ((m = RE_STARTBOX.exec(rest))) { builder.add(lineFrom + p, lineFrom + p + m[0].length, DEC.box); p += m[0].length; continue; }
      if ((m = RE_ENDBOX.exec(rest))) { builder.add(lineFrom + p, lineFrom + p + m[0].length, DEC.box); p += m[0].length; continue; }
      if ((m = RE_STARTCOL.exec(rest))) { builder.add(lineFrom + p, lineFrom + p + m[0].length, DEC.box); p += m[0].length; continue; }
      if ((m = RE_NEXTCOL.exec(rest))) { builder.add(lineFrom + p, lineFrom + p + m[0].length, DEC.box); p += m[0].length; continue; }
      if ((m = RE_ENDCOL.exec(rest))) { builder.add(lineFrom + p, lineFrom + p + m[0].length, DEC.box); p += m[0].length; continue; }
      // Labels/Metadaten
      if ((m = RE_LABEL.exec(rest))) { builder.add(lineFrom + p, lineFrom + p + m[0].length, DEC.label); p += m[0].length; continue; }
      if ((m = RE_ZOOM.exec(rest))) { builder.add(lineFrom + p, lineFrom + p + m[0].length, DEC.label); p += m[0].length; continue; }
      // Querverweise
      if ((m = RE_REF.exec(rest))) { builder.add(lineFrom + p, lineFrom + p + m[0].length, DEC.ref); p += m[0].length; continue; }
      // Fließtext: Abschnitte in einem Zug überspringen
      if ((m = RE_PLAIN.exec(rest))) { p += m[0].length; continue; }
      p += 1;
    }
    lineFrom = lineTo + 1;
  }
  return builder.finish();
}

const annotationField = StateField.define({
  create: (state) => scanAnnotations(state.doc),
  update: (value, tr) => (tr.docChanged ? scanAnnotations(tr.state.doc) : value),
  provide: (f) => EditorView.decorations.from(f, (value) => value),
});

// ── NASM x86-64 (Port von codemirror-mode-nasm.js) ───────────────────
const NASM_DIRECTIVES = new Set([
  "db", "dw", "dd", "dq", "dt", "resb", "resw", "resd", "resq", "times",
  "section", "segment", "global", "extern", "align", "alignb", "alignw",
  "alignq", "bits", "use16", "use32", "use64", "string",
  "byte", "word", "dword", "qword", "tbyte", "float", "double",
]);
const NASM_INSTRUCTIONS = new Set([
  "nop", "ret", "iret", "iretd", "iretq", "syscall", "sysret", "int", "hlt",
  "mov", "movabs", "movsx", "movsxd", "movzx", "lea", "xchg", "xadd",
  "push", "pop", "pushf", "popf", "pushfq", "popfq",
  "inc", "dec", "neg", "not",
  "add", "sub", "adc", "sbb", "and", "or", "xor", "test", "cmp",
  "mul", "imul", "div", "idiv",
  "shl", "shr", "sal", "sar", "rol", "ror", "rcl", "rcr",
  "cdq", "cqo", "cbw", "cwde", "cdqe",
  "cmc", "clc", "stc", "cld", "std",
  "enter", "leave", "call", "jmp",
  "jz", "jnz", "je", "jne", "jl", "jle", "jg", "jge",
  "jb", "jbe", "ja", "jae", "js", "jns", "jo", "jno", "jp", "jnp",
  "loop", "loope", "loopne", "loopeq", "loopnz", "loopz",
  "rep", "repe", "repne", "repz", "repnz",
  "stosb", "stosw", "stosd", "stosq",
  "movsb", "movsw", "movsd", "movsq",
  "lodsb", "lodsw", "lodsd", "lodsq",
  "scasb", "scasw", "scasd", "scasq",
  "in", "out", "insb", "insw", "insd", "outsb", "outsw", "outsd",
  "wait", "ud2", "ud1", "cpuid", "rdrand", "rdtsc", "rdtscp",
  "sfence", "lfence", "mfence", "pause",
]);
const NASM_REGISTERS = (function () {
  const s = new Set();
  ["rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp"].forEach((n) => s.add(n));
  for (let i = 8; i <= 15; i++) {
    s.add("r" + i); s.add("r" + i + "b"); s.add("r" + i + "w");
    s.add("r" + i + "d"); s.add("r" + i + "l");
  }
  ["eax", "ebx", "ecx", "edx", "esi", "edi", "ebp", "esp"].forEach((n) => s.add(n));
  ["ax", "bx", "cx", "dx", "si", "di", "bp", "sp", "ip"].forEach((n) => s.add(n));
  ["al", "bl", "cl", "dl", "ah", "bh", "ch", "dh",
   "sil", "dil", "bpl", "spl"].forEach((n) => s.add(n));
  ["cs", "ds", "es", "fs", "gs", "ss"].forEach((n) => s.add(n));
  for (let j = 0; j < 8; j++) s.add("st" + j);
  for (let k = 0; k < 16; k++) s.add("xmm" + k);
  return s;
})();

const nasmLanguage = StreamLanguage.define({
  name: "nasm",
  languageData: () => ({ lineComment: ";", indentUnit: 4 }),
  token(text, state) {
    // Matching immer explizit an der aktuellen Position (der leftmost-
    // Match von exec() auf dem ganzen Rest würde ab dem 2. Vorkommen
    // pro Zeile fehlschlagen).
    function matchAt(pattern) {
      const m = pattern.exec(text.slice(state.pos));
      if (m && m.index === 0) { state.pos += m[0].length; return m[0]; }
      return null;
    }
    if (matchAt(/;.*/)) return "comment";
    if (matchAt(/%[a-zA-Z_][\w.]*/)) return "keyword";
    if (matchAt(/0[xX][0-9a-fA-F]+/)) return "number";
    if (matchAt(/%[01]+/)) return "number";
    if (matchAt(/\d+[dhwbt]?/)) return "number";
    const ident = matchAt(/[a-zA-Z_.$][\w.$-]*/);
    if (ident) {
      // Label: direkt nach dem Bezeichner ein ":" (ggf. nach Leerzeichen)
      const label = /^\s*:/.exec(text.slice(state.pos));
      if (label && label.index === 0) return "variableName";
      const low = ident.toLowerCase();
      if (NASM_DIRECTIVES.has(low)) return "keyword";
      if (NASM_INSTRUCTIONS.has(low)) return "variableName";
      if (NASM_REGISTERS.has(low)) return "number";
      return null; // extern, Abschnitte, Symbole — neutral
    }
    if (matchAt(/[ \t]+/)) return null;
    return null; // StreamLanguage rückt ohne Fortschritt selbst ein Zeichen weiter
  },
});

// ── Modi: CM5-Namen → CM6-Extensions ─────────────────────────────────
function modeName(mode) {
  if (!mode) return null;
  if (typeof mode === "string") return mode;
  if (typeof mode === "object" && mode.name) return mode.name;
  return null;
}

const legacyLanguages = {
  shell: StreamLanguage.define(shell),
  nginx: StreamLanguage.define(nginx),
  toml: StreamLanguage.define(toml),
  // legacy-modes kennt keinen ini-Modus → .ini-Dateien bekommen
  // properties-artiges Highlighting (Key =/ : Wert, #/;/!-Kommentare).
  ini: StreamLanguage.define(properties),
};

function languageFor(mode) {
  switch (modeName(mode)) {
    case "python": return python();
    case "javascript": return javascript();
    case "htmlmixed":
    case "html": return html();
    case "xml": return xml();
    case "text/css":
    case "css": return css();
    case "markdown":
    case "gfm": return markdown();
    case "aicampus-markdown":
      return [markdown(), annotationField];
    case "application/json":
    case "json": return json();
    case "shell": return legacyLanguages.shell;
    case "yaml": return yaml();
    case "clike": return cpp();
    case "text/x-csrc":
    case "text/x-c": return cpp({ name: "c" });
    case "text/x-c++src":
    case "text/x-cpp": return cpp();
    case "text/x-nasm":
    case "nasm": return nasmLanguage;
    case "text/x-nginx-conf":
    case "nginx": return legacyLanguages.nginx;
    case "text/x-sql":
    case "sql": return sql();
    case "text/x-ini":
    case "ini": return legacyLanguages.ini;
    case "text/x-toml":
    case "toml": return legacyLanguages.toml;
    default: return null; // "text/plain" & unbekannte Modi
  }
}

// ── Autocompletion ──────────────────────────────────────────────────────
// autocompletion() (s. _buildExtensions) fragt bei jeder Eingabe alle
// Quellen ab; die Quellen kommen pro Mode über die override-Config:
// - javascript: globalThis (Standardlib + DOM) + Snippets + lokale Namen
//   (im Paket mitgeliefert, läuft über die Language-Data des Modes)
// - sql: Dialekt-Keywords (im Paket mitgeliefert)
// - python / c / c++: das Paket liefert keine Quelle → einfache
//   Keyword-Listen (completeFromList, rein textbasiert)
// - aicampus-markdown / gfm / markdown: AICampus-Quelle (@-Befehle,
//   Referenzen, Box-Typen; Labels: aktuelles Dokument + kursweite Refmaps)

const PYTHON_KEYWORDS = [
  "False", "None", "True", "and", "as", "assert", "async", "await",
  "break", "class", "continue", "def", "del", "elif", "else", "except",
  "finally", "for", "from", "global", "if", "import", "in", "is",
  "lambda", "match", "nonlocal", "not", "or", "pass", "raise", "return",
  "try", "while", "with", "yield",
  // Builtins
  "abs", "all", "any", "bin", "bool", "bytearray", "bytes", "callable",
  "chr", "classmethod", "compile", "complex", "delattr", "dict", "dir",
  "divmod", "enumerate", "eval", "exec", "filter", "float", "format",
  "frozenset", "getattr", "globals", "hasattr", "hash", "help", "hex",
  "id", "input", "int", "isinstance", "issubclass", "iter", "len",
  "list", "locals", "map", "max", "memoryview", "min", "next", "object",
  "oct", "open", "ord", "pow", "print", "property", "range", "repr",
  "reversed", "round", "set", "setattr", "slice", "sorted", "staticmethod",
  "str", "sum", "super", "tuple", "type", "vars", "zip",
];

const CPP_KEYWORDS = [
  // C++17/20-Keywords
  "alignas", "alignof", "asm", "auto", "bool", "break", "case", "catch",
  "char", "char8_t", "char16_t", "char32_t", "class", "const",
  "consteval", "constexpr", "constinit", "const_cast", "continue",
  "co_await", "co_return", "co_yield", "decltype", "default", "delete",
  "do", "double", "dynamic_cast", "else", "enum", "explicit", "export",
  "extern", "false", "float", "for", "friend", "goto", "if", "inline",
  "int", "long", "mutable", "namespace", "new", "noexcept", "nullptr",
  "operator", "private", "protected", "public", "register",
  "reinterpret_cast", "requires", "return", "short", "signed", "sizeof",
  "static", "static_assert", "static_cast", "struct", "switch",
  "template", "this", "thread_local", "throw", "true", "try", "typedef",
  "typeid", "typename", "union", "unsigned", "using", "virtual", "void",
  "volatile", "wchar_t", "while",
  // Häufige Standardbibliothek
  "cin", "cout", "cerr", "clog", "endl", "size_t", "string", "wstring",
  "vector", "map", "set", "unordered_map", "unordered_set", "pair",
  "array", "deque", "stack", "queue", "list", "shared_ptr", "unique_ptr",
  "make_pair", "make_shared", "make_unique", "function", "bind", "begin",
  "end", "sort", "reverse", "min", "max", "swap", "find", "lower_bound",
  "push_back", "pop_back", "emplace_back", "front", "back", "empty",
  "size", "clear", "std", "printf", "scanf", "fprintf", "puts", "malloc",
  "calloc", "realloc", "free", "memcpy", "memset", "strlen", "strcmp",
  "strcat", "snprintf",
];

// Box-Typen (spiegelt CALLOUT_TYPES in markdown-renderer.js)
const TA_BOX_TYPES = [
  "merksatz", "hinweis", "bemerkung", "warnung", "beispiel", "code",
  "definition", "satz", "theorem", "lemma", "proposition", "korollar",
  "beweis", "frage",
];

const TA_COMMANDS = [
  { label: "startbox:", detail: "Box öffnen (Typ: …)" },
  { label: "endbox", detail: "Box schließen" },
  { label: "startcolumn:", detail: "Spalten: erste" },
  { label: "nextcolumn:", detail: "Spalten: nächste" },
  { label: "endcolumn", detail: "Spalten: beenden" },
];

const TA_REF_TYPES = [
  { label: "fig", detail: "Abbildung" },
  { label: "eq", detail: "Gleichung" },
  { label: "tab", detail: "Tabelle" },
  { label: "box", detail: "Box (Satz, Definition, …)" },
  { label: "code", detail: "Code-Block" },
  { label: "task", detail: "Aufgabe" },
  { label: "cite", detail: "Zitation [N]" },
  { label: "citet", detail: "Zitation „Autor (Jahr)“" },
  { label: "citep", detail: "Zitation „(Autor, Jahr)“" },
];

// Kinds mit Refmap-Labels (→ Label-Completion mit Quellen-Preview)
const REF_KINDS = new Set(["fig", "eq", "code", "box", "tab"]);

// Cursor in einem Code-Block? Dort ist @ nur Text → kein AICampus-Completion.
function insideFence(state, pos) {
  let open = false;
  const lineTo = state.doc.lineAt(pos).number;
  for (let i = 1; i <= lineTo; i++) {
    if (FENCE_OPEN.test(state.doc.line(i).text)) open = !open;
  }
  return open;
}

// Alle im Dokument definierten Labels eines Typs ({#fig:label} o. ä.).
function docLabelsFor(state, type) {
  const re = new RegExp("\\{#" + type + ":([\\p{L}0-9_-]+)\\}", "gu");
  const seen = new Set();
  const out = [];
  for (const m of state.doc.toString().matchAll(re)) {
    if (!seen.has(m[1])) { seen.add(m[1]); out.push(m[1]); }
  }
  return out;
}

// ── Kursweite Refmaps für Label-Completion ────────────────────────────
// Die Refmap-Endpoints liefern ALLE beschrifteten Objekte des Kurses
// (Skript + Slides) inkl. Kurz-Previews. Die Completion-Quelle kann nur
// synchron laufen → beide Refmaps einmal pro Seite holen und cachen;
// bis die Antwort da ist (oder auf Nicht-Kurs-Seiten) werden nur die
// dokument-lokalen Labels vorgeschlagen.
let _courseRefData = null;    // { byKind, tasks, references }
let _courseRefPromise = null;

function _courseIdForCompletion() {
  try {
    const cid = (typeof courseId !== "undefined") ? courseId : null;
    return (cid === null || cid === undefined) ? null : cid;
  } catch (e) {
    return null; // TDZ: deklariert, aber noch nicht initialisiert
  }
}

function ensureCourseRefData() {
  if (_courseRefData || _courseRefPromise) return _courseRefPromise;
  const cid = _courseIdForCompletion();
  if (cid === null) return null; // Nicht-Kurs-Seite → keine Kurs-Referenzen
  const opts = { credentials: "same-origin", cache: "no-store" };
  const get = (url) => fetch(url, opts).then(r => (r.ok ? r.json() : null)).catch(() => null);
  _courseRefPromise = Promise.all([
    get(`/api/courses/${cid}/script-refmap`),
    get(`/api/courses/${cid}/slides-refmap`),
  ]).then(([script, slides]) => {
    if (!script && !slides) { _courseRefPromise = null; return null; } // später erneut versuchen
    const byKind = {};
    const addKind = (source, kind, labels) => {
      for (const [key, info] of Object.entries(labels || {})) {
        if (key.slice(0, kind.length + 1) !== kind + ":") continue;
        const preview = info.preview
          ? " · " + String(info.preview).replace(/\s+/g, " ").trim().slice(0, 60)
          : "";
        (byKind[kind] = byKind[kind] || []).push(
          { label: key.slice(kind.length + 1), detail: source + preview });
      }
    };
    for (const kind of ["fig", "eq", "code", "box", "tab"]) {
      addKind("Skript", kind, script && script.labels);
      addKind("Slides", kind, slides && slides.labels);
    }
    _courseRefData = {
      byKind,
      tasks: (script && script.tasks) || {},
      references: (script && script.references) || {},
    };
    return _courseRefData;
  });
  return _courseRefPromise;
}

// AICampus-Markdown: Zwei-Stufen-Completion.
//   "@<Wort>"            → @-Befehle + Referenz-Typen
//   "@<typ>:<label-Präfix>" → Labels dieses Typs (bzw. Box-Typen
//                             nach "@startbox:")
// validFor steuert, welche Zeichen die Liste lokal filtern (statt
// neu zu fragen) — der Wechselpunkt ":" löst damit die 2. Stufe aus.
const aicampusCompletion = (context) => {
  const state = context.state;
  if (insideFence(state, context.pos)) return null;
  // 100 Zeichen vor dem Cursor reichen (Befehle/Labels sind kurz).
  const before = state.sliceDoc(Math.max(0, context.pos - 100), context.pos);

  // Stufe 2: "@typ:label-Präfix" (WICHTIG: u-Flag — ohne es degeneriert
  // \p{L} zu einer Literal-Charclass [p{L}] und nichts würde matchen)
  const ref = /@([\p{L}-]+):([\p{L}0-9_.-]*)$/u.exec(before);
  if (ref) {
    const type = ref[1], prefix = ref[2];
    let options;
    if (type === "startbox") {
      options = TA_BOX_TYPES
        .filter(t => t.startsWith(prefix))
        .map(t => ({ label: t, type: "keyword", detail: "Box-Typ" }));
    } else if (type === "startcolumn" || type === "nextcolumn") {
      options = []; // numerische Spalten-Indizes — kein Completion
    } else if (type === "task") {
      // @task:{id} → Kurs-Aufgaben (Titel aus dem script-refmap)
      const tasks = _courseRefData ? _courseRefData.tasks : {};
      options = Object.values(tasks)
        .filter(t => String(t.id).startsWith(prefix))
        .map(t => ({ label: String(t.id), type: "reference", detail: t.title || "" }));
    } else if (type === "cite" || type === "citet" || type === "citep") {
      // @cite{,t,p}:{key} → BibTeX-Keys der Kurs-Quellen
      const refs = _courseRefData ? _courseRefData.references : {};
      options = Object.values(refs)
        .filter(r => String(r.key).startsWith(prefix))
        .map(r => ({
          label: String(r.key), type: "reference",
          detail: [r.authors, r.year != null ? `(${r.year})` : null,
            r.num != null ? `[${r.num}]` : null].filter(Boolean).join(" "),
        }));
    } else if (REF_KINDS.has(type)) {
      // fig/eq/code/box/tab: zuerst die Labels des aktuellen Dokuments,
      // dann die kursweiten aus beiden Refmaps (Auflösungs-Reihenfolge).
      const seen = new Set();
      options = [];
      const take = (label, detail) => {
        if (seen.has(label) || !label.startsWith(prefix)) return;
        seen.add(label);
        options.push({ label, type: "reference", detail });
      };
      for (const l of docLabelsFor(state, type)) take(l, "dieses Dokument");
      for (const o of (_courseRefData && _courseRefData.byKind[type]) || []) take(o.label, o.detail);
    } else {
      options = []; // unbekannter Typ (z. B. sec) → kein Completion
    }
    if (!options.length) return null;
    return {
      from: context.pos - prefix.length,
      options,
      validFor: /^[\p{L}0-9_.-]*$/u,
    };
  }

  // Stufe 1: "@Wort-Präfix". Vor dem @ darf keine Alphanumerik stehen,
  // sonst ist es wahrscheinlich eine E-Mail-Adresse, keine AICampus-Syntax.
  const word = /(^|[^A-Za-z0-9])@([\p{L}-]*)$/u.exec(before);
  if (word) {
    const prefix = word[2];
    const options = []
      .concat(TA_COMMANDS.map(c => ({ label: c.label, type: "keyword", detail: c.detail })))
      .concat(TA_REF_TYPES.map(r => ({ label: r.label + ":", type: "reference", detail: r.detail })))
      .filter(o => o.label.startsWith(prefix));
    if (!options.length) return null;
    return {
      from: context.pos - prefix.length,
      options,
      validFor: /^[\p{L}-]*$/u,
    };
  }
  return null;
};

// autocompletion()-Config je Mode (Default: die vom Sprache-Paket
// mitgelieferten Quellen — z. B. globalThis für JS, Keywords für SQL).
function autocompletionFor(mode) {
  switch (modeName(mode)) {
    case "aicampus-markdown":
    case "gfm":
    case "markdown":
      ensureCourseRefData(); // Fire-and-forget, einmal pro Seite gecacht
      return autocompletion({
        override: [aicampusCompletion],
        // Nach Auswahl von "fig:" / "startbox:" etc. direkt mit der
        // nächsten Stufe (Labels/Box-Typen) weitervervollständigen.
        activateOnCompletion: (c) => c.label.endsWith(":"),
      });
    case "python":
      return autocompletion({ override: [completeFromList(PYTHON_KEYWORDS)] });
    case "clike":
    case "text/x-csrc":
    case "text/x-c":
    case "text/x-c++src":
    case "text/x-cpp":
      return autocompletion({ override: [completeFromList(CPP_KEYWORDS)] });
    default:
      return autocompletion();
  }
}

// Helles Highlighting für die Editor-Modi ohne One Dark — Farben aus dem
// CM5-Default-Theme (gfm & friends), damit Markdown wieder wie gewohnt
// aussieht (fett-lila Headings, blaue Links, roter Inline-Code, …).
// oneDark-Editoren bleiben unverändert, da oneDark seinen eigenen
// HighlightStyle mitbringt.
const lightHighlight = HighlightStyle.define([
  { tag: t.heading, color: "#708", fontWeight: "bold" },
  { tag: t.strong, fontWeight: "bold" },
  { tag: t.emphasis, fontStyle: "italic" },
  { tag: t.strikethrough, textDecoration: "line-through" },
  { tag: [t.link, t.url], color: "#00c" },
  { tag: t.monospace, color: "#a11" },
  { tag: t.quote, color: "#090" },
  { tag: t.contentSeparator, color: "#999" },
  { tag: t.comment, color: "#a50" },
  { tag: t.string, color: "#a11" },
  { tag: [t.number, t.bool], color: "#164" },
  { tag: t.atom, color: "#990" },
  { tag: [t.keyword, t.operatorKeyword, t.controlKeyword], color: "#708" },
  { tag: t.propertyName, color: "#888" },
  { tag: t.operator, color: "#888" },
  { tag: t.definition(t.variableName), color: "#00f" },
  { tag: t.function(t.variableName), color: "#05a" },
  { tag: t.typeName, color: "#085" },
]);

function themeFor(theme) {
  // One Dark ist das Dark-Theme; "dracula" wird aus Kompatibilität
  // (ältere Template-Stellen) ebenfalls gemappt.
  if (theme === "oneDark" || theme === "one-dark" || theme === "dracula") return oneDark;
  // Achtung: Ein HighlightStyle-Objekt ist selbst KEINE Extension (kein
  // FacetProvider, kein .extension-Getter) — nur der
  // syntaxHighlighting()-Wrapper ist gültig (sonst: "Unrecognized
  // extension value in extension set").
  return syntaxHighlighting(lightHighlight);
}

// ── Fassade: CM5-kompatible Editor-API auf CM6 ───────────────────────
class CM6Editor {
  constructor(ta, cfg = {}, wrapper) {
    this.ta = ta;
    this.cfg = cfg || {};
    this._options = {};
    this._readOnly = this.cfg.readOnly ? true : false;
    // readOnly ist ein Facet und damit pro State immutable — zum
    // Laufzeit-Wechsel (setOption) braucht es ein Compartment.
    this._readOnlyComp = new Compartment();
    this._handlers = { change: [] };
    this._lastValue = ta.value;
    if (!wrapper) {
      wrapper = document.createElement("div");
      wrapper.className = "ta-cm";
      ta.parentNode.insertBefore(wrapper, ta);
    }
    this.wrapper = wrapper;
    if (!wrapper.contains(ta)) wrapper.appendChild(ta);
    // Der Dokumentstand wird bei jeder Änderung in das (versteckte)
    // Textarea synchronisiert — HTMX-Form-Requests funktionieren so
    // auch ohne explizites save().
    this._updateListener = EditorView.updateListener.of((vu) => {
      if (!vu.docChanged) return;
      const value = vu.state.doc.toString();
      if (value === this._lastValue) return; // z. B. State-Swap beim Mode-Wechsel
      this._lastValue = value;
      this.ta.value = value;
      for (const fn of this._handlers.change.slice()) {
        try { fn(this); } catch (err) { console.error(err); }
      }
    });
    this.view = new EditorView({
      parent: wrapper,
      state: EditorState.create({ doc: ta.value, extensions: this._buildExtensions() }),
    });
    ta.style.display = "none";
    // Folien-Editor: Browser-Scroll-Anchoring war auf dem (damals noch
    // sichtbaren) Textarea deaktiviert worden → auf den Scroller übernehmen.
    if (ta.style.overflowAnchor) this.view.scrollDOM.style.overflowAnchor = ta.style.overflowAnchor;
  }

  _buildExtensions() {
    const cfg = this.cfg;
    // setOption("mode") landet in _options — ohne diesen Lookup würde
    // der (im Workspace immer "text/plain") Ursprungsmodus benutzt und
    // das Highlighting nicht wechseln.
    const mode = this._options.mode !== undefined ? this._options.mode : cfg.mode;
    const exts = [
      lineNumbers(),
      highlightActiveLine(),
      highlightActiveLineGutter(),
      history(),              // → Ctrl-Z / Ctrl-Shift-Z funktioniert schrittweise
      keymapExt(historyKeymap),
      keymapExt(defaultKeymap),
      keymapExt(searchKeymap),    // Ctrl-F, F3, Shift-F3, Ctrl-Shift-F
      keymapExt(closeBracketsKeymap),
      // Ctrl-L = "Zur Zeile springen" (CM5-Muskelgedächtnis)
      keymap.of([{ key: "Ctrl-l", run: gotoLine }, { key: "Mod-l", run: gotoLine }]),
      indentOnInput(),
      bracketMatching(),
      closeBrackets(),    // CM5 autoCloseBrackets
      search(),
      autocompletionFor(mode),   // Completion (Ctrl-Space / beim Tippen)
      // Tab/Shift-Tab indenten. Zwingend NACH autocompletionFor(), damit
      // Tab im geöffneten Completion-Menü die Auswahl akzeptiert (das
      // completionKeymap hat dort höchste Priorität) und sonst eingeht.
      keymap.of([
        { key: "Tab", run: indentMore },
        { key: "Shift-Tab", run: indentLess },
      ]),
      this._updateListener,
    ];
    if (cfg.tabSize) exts.push(EditorState.tabSize.of(cfg.tabSize));
    if (cfg.lineWrapping) exts.push(EditorView.lineWrapping);
    exts.push(this._readOnlyComp.of(EditorState.readOnly.of(this._readOnly)));
    const lang = languageFor(mode);
    if (lang) exts.push(lang);
    const theme = themeFor(cfg.theme);
    if (theme) exts.push(theme);
    return exts;
  }

  // ── CM5-kompatible API ────────────────────────────────────────
  getValue() { return this.view.state.doc.toString(); }

  setValue(value) {
    const v = String(value == null ? "" : value);
    if (v === this._lastValue) return;
    // Wichtig: no-op-Skip — sonst würde jeder LLM-Reload/identische
    // setValue() einen Undo-Step erzeugen (das ursprüngliche "Ctrl-Z
    // macht alles auf einmal rückgängig"-Problem).
    this.view.dispatch({
      changes: { from: 0, to: this.view.state.doc.length, insert: v },
      selection: { anchor: 0 },
    });
  }

  // Erstes Inhalt laden (z. B. Folien loadContent()): Solange der Editor
  // leer ist, NEUEN State erstellen statt setValue()-Transaktion —
  // sonst ist die "leer → voll"-Übergang ein Undo-Step und Strg-Z
  // würde den gesamten Inhalt ausradieren.
  setInitialContent(v) {
    v = String(v == null ? "" : v);
    const { state } = this.view;
    if (v.length === 0) return;
    if (state.doc.length !== 0) return this.setValue(v);
    // setState() läuft den Update-Listener NICHT (Plugins + DocView werden
    // direkt neu aufgebaut) → Textarea und change-Handler manuell
    // synchronisieren (CM5-setValue hätte change immer feuern lassen).
    this._lastValue = v;
    this.ta.value = v;
    this.view.setState(EditorState.create({
      doc: v,
      extensions: this._buildExtensions(),
    }));
    for (const fn of this._handlers.change.slice()) {
      try { fn(this); } catch (err) { console.error(err); }
    }
  }

  getCursor() {
    const head = this.view.state.selection.main.head;
    const line = this.view.state.doc.lineAt(head);
    return { line: line.number - 1, ch: head - line.from }; // CM5: 0-basiert
  }

  setCursor(line, ch, opts) {
    const s = this.view.state;
    const n = Math.max(0, Math.min(line | 0, s.doc.lines - 1));
    const ln = s.doc.line(n + 1);
    const pos = Math.max(ln.from, Math.min(ln.to, ln.from + (ch | 0)));
    this.view.dispatch({
      selection: { anchor: pos },
      scrollIntoView: (opts && opts.scroll === false) ? false : true,
    });
  }

  replaceRange(text, from, to) {
    const a = this._indexAt(from);
    const b = to ? this._indexAt(to) : a;
    const t = String(text == null ? "" : text);
    this.view.dispatch({
      changes: { from: a, to: b, insert: t },
      selection: { anchor: a + t.length },
    });
  }

  indexFromPos(pos) { return this._indexAt(pos); }

  lastLine() { return this.view.state.doc.lines - 1; } // CM5: 0-basiert

  on(type, fn) {
    if (type === "change") this._handlers.change.push(fn);
    return this;
  }

  off(type, fn) {
    if (type === "change") {
      const i = this._handlers.change.indexOf(fn);
      if (i >= 0) this._handlers.change.splice(i, 1);
    }
    return this;
  }

  focus() { this.view.focus(); }
  refresh() { /* no-op — CM6 misst über ResizeObserver neu */ }

  getOption(name) {
    if (name === "readOnly") return this._readOnly;
    if (name in this._options) return this._options[name];
    return this.cfg[name];
  }

  setOption(name, value) {
    this._options[name] = value;
    if (name === "readOnly") {
      this._readOnly = value ? true : false;
      // CM6: read-only-States blocken nur User-Interaktion;
      // programmatische setValue() funktioniert weiter (wie in CM5).
      this.view.dispatch({ effects: this._readOnlyComp.reconfigure(EditorState.readOnly.of(this._readOnly)) });
      return;
    }
    if (name === "mode") {
      // State mit neuem Modus neu aufbauen (Dokument + Cursor bleiben).
      const s = this.view.state;
      const doc = s.doc.toString();
      const head = s.selection.main.head;
      this.view.setState(EditorState.create({
        doc,
        selection: { anchor: head },
        extensions: this._buildExtensions(),
      }));
    }
  }

  save() { this.ta.value = this.getValue(); }

  toTextArea() {
    const { ta, wrapper } = this;
    this.save();
    this.view.destroy();
    this.view = null;
    ta.style.display = "";
    if (wrapper.parentNode && wrapper.contains(ta)) {
      wrapper.parentNode.replaceChild(ta, wrapper);
    }
  }

  // Zeile exakt an den oberen Rand des Editors scrollen (Folien-
  // Navigation, Preview-Sync). Wichtig: scroller.scrollTop DIREKT setzen
  // statt CM6s scrollIntoView (Effect oder Methode) — CM6 scrollt dabei
  // ALLE scrollbaren Vorfahren des Scrollers mit (scrollRectIntoView
  // wandert bis doc.body und ruft dort win.scrollBy auf). Auf einer
  // scrollbaren Seite (z.B. Skript) würde die ganze Seite mitscrollen.
  // coordsAtPos liefert auch für Zeilen außerhalb des Viewports
  // (geschätzte Höhen) Koordinaten — darauf basiert auch die interne
  // CM6-Navigation.
  scrollToLineTop(line) {
    const s = this.view.state;
    const n = Math.max(0, Math.min(line | 0, s.doc.lines - 1));
    const from = s.doc.line(n + 1).from;
    const scroller = this.view.scrollDOM;
    if (n === 0) {
      // Dokumentanfang: direkt ans Scroller-Top — coordsAtPos(0) liefert
      // außerhalb des Viewports keine verlässlichen Koordinaten.
      scroller.scrollTop = 0;
      this.view.dispatch({ selection: { anchor: from } });
      return;
    }
    const coords = this.view.coordsAtPos(from);
    if (coords) {
      // Zeilen-Oberrand ans Scroller-Oberrand: scrollTop = Abstand der
      // Zeile zum Dokumentanfang (beide client-Y-Werte verschieben sich
      // beim Scrollen gemeinsam → Differenz ist scrollunabhängig).
      // Wichtig: SETZEN, nicht addieren — sonst addiert sich der Offset
      // bei jedem Sync-Event und der Editor läuft auf den Boden.
      scroller.scrollTop = Math.max(0, coords.top - this.view.documentTop);
    }
    // Selektionsänderung ohne Scroll-Effekt (CM6 scrollt bei
    // programmatischen Selections nicht).
    this.view.dispatch({ selection: { anchor: from } });
  }

  getWrapperElement() { return this.wrapper; }
  getScrollerElement() { return this.view.scrollDOM; }

  _indexAt(pos) {
    const s = this.view.state;
    const n = Math.max(0, Math.min(pos.line | 0, s.doc.lines - 1));
    const ln = s.doc.line(n + 1);
    return Math.max(ln.from, Math.min(ln.to, ln.from + (pos.ch | 0)));
  }
}

// ── Shim verbinden: alle Platzhalter am Ort upgraden ─────────────────
window.CodeMirror.__connect(CM6Editor);
