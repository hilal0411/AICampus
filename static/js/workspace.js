/* ═══════════════════════════════════════════════════════════════
   WorkspaceUI — geteilte Datei-Baum + Editor-Komponente für
   Workspace-Aufgaben.

   Verwendet von:
   - templates/tutor/task_detail_workspace.html  (Tutor/Prof: volle Rechte)
   - templates/student/task_solve_workspace.html (Student: eingeschränkt)

   Initialisierung im Template:
     const wsUI = WorkspaceUI.init({
       treeEl, editorEl, apiBase, ...   // s. init() unten
     });

   Konzepte:
   - Zugriffsklassen (explizit pro Datei UND Ordner, im Baum markiert):
       ✏️ edit      — Tutor + Student schreiben (Standard)
       🔒 readonly  — Student nur lesen (geteilt: 1 Kopie für alle)
       👤 hidden    — Student sieht es nie (nur Tutor + Korrektur)
     Effektive Klasse = restriktivste von (eigene explizite, alle
     Ordner-Vorfahren). Das Backend liefert je Datei die effektive Klasse
     (`access`) + die explizite (`file_access`); Ordner-Klassen kommen als
     `folders`-Liste {path, access (effektiv), own (explizit)}.
   - Init-Artefakte (.init.sh/.init_hidden.sh-Ergebnisse, mit `init: true`
     vom Backend): am realen Pfad im Baum, read-only (📦-Marker),
     entstehen/ändern sich nur per neuem Init-Build.
   - Ordner werden on-disk + in der DB verwaltet (folderApi): anlegen /
     verschieben / löschen via /folders-Endpoints — auch LEERE Ordner
     bleiben persistiert (Disk-Verzeichnis + explizite Zeile) und sichtbar.
   - Move/Rename = Backend-Endpoint /files/move (Dateien) bzw.
     /folders/move (Tutor: Ordner inkl. Zugriffsklassen in einem Call).
   - Reihenfolge: Dateien, Ordner und [init]-Artefakte sind je Ordner per
     Drag & Drop sortierbar (rein Anzeige — Struktur unverändert). Tutor:
     global persistiert (sort_order / task_workspace_orders via /reorder);
     Student: nur eigene Ansicht (localStorage pro User+Task, localOrderKey).
     Drop im selben Ordner = neu ordnen; in anderen Ordner = Move (Gates).
   ═══════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  // ─── Zugriffsklassen (explizit, entspricht dem Backend-Modell) ──
  const ACCESS = {
    edit:     { icon: "✏️", label: "Editierbar — Tutor + Student können schreiben" },
    readonly: { icon: "🔒", label: "Read-only — Student darf nur lesen (geteilt für alle)" },
    hidden:   { icon: "👤", label: "Versteckt — Student sieht es nie (nur Tutor + Korrektur)" },
  };
  const ACCESS_RANK = { edit: 0, readonly: 1, hidden: 2 };
  const ACCESS_BY_RANK = ["edit", "readonly", "hidden"];

  // Play/Stop-Icons für .sh-Skripte (SVG statt Emoji — geräteunabhängige
  // Darstellung).
  const WPLAY_SVG = '<svg viewBox="0 0 24 24" class="h-4 w-4" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
  const WSTOP_SVG = '<svg viewBox="0 0 24 24" class="h-4 w-4" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>';

  function accessLabel(a) {
    if (a === "readonly") return "🔒 read-only";
    if (a === "hidden") return "👤 versteckt";
    return "✏️ editierbar";
  }

  // System-Skripte mit fester Zugriffs-Klasse (spiegelt system_file_access
  // im Backend): diese können im Kontextmenü nicht umgestellt werden.
  // Alle sechs stehen immer in der Wurzel (weiche Punkt-Konvention: 👤
  // beginnt mit ".").
  const SYSTEM_FILE_ACCESS = {
    "run.sh": "readonly",
    ".init.sh": "hidden",
    "test.sh": "readonly",
    ".init_hidden.sh": "hidden",
    ".test_private.sh": "hidden",
    ".test_solution.sh": "hidden",
  };
  function systemAccessOf(path) {
    const p = String(path).replace(/\\/g, "/").replace(/^\/+/, "");
    return SYSTEM_FILE_ACCESS[p] || null;
  }

  // ─── kleine Helper (mit Fallback, da Page-Globals erst im Body) ──
  function esc(s) {
    if (typeof escapeHtml === "function") return escapeHtml(s);
    return String(s).replace(/[&<>"']/g, c => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }
  function toast(msg, type) {
    if (typeof showToast === "function") showToast(msg, type);
  }
  function fmtBytes(n) {
    if (n == null) return "";
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }
  function fileIcon(p) {
    const ext = p.slice(p.lastIndexOf(".")).toLowerCase();
    if (ext === ".py" || ext === ".pyw") return "🐍";
    if ([".c", ".cpp", ".cc", ".h", ".hpp"].includes(ext)) return "⚙️";
    if (ext === ".asm") return "🔧";
    if ([".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp"].includes(ext)) return "🖼️";
    if ([".csv", ".json"].includes(ext)) return "🗃️";
    if ([".sh", ".conf"].includes(ext)) return "📜";
    return "📄";
  }

  // ─── CodeMirror-Modi je Dateiendung ────────────────────────────
  const CM_MODES = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript",
    ".html": "htmlmixed", ".htm": "htmlmixed",
    ".xml": "xml", ".svg": "xml",
    ".css": "text/css",
    ".md": "gfm", ".markdown": "gfm",
    ".json": "application/json",
    ".sh": "shell", ".bash": "shell",
    ".yml": "yaml", ".yaml": "yaml",
    ".c": "text/x-csrc", ".h": "text/x-csrc",
    ".cpp": "text/x-c++src", ".cc": "text/x-c++src", ".hpp": "text/x-c++src",
    ".asm": "text/x-nasm",
    ".conf": "text/x-nginx-conf", ".nginx": "text/x-nginx-conf",
    ".sql": "text/x-sql",
    ".ini": "text/x-ini",
    ".toml": "text/x-toml",
    ".txt": "text/plain",
  };
  function cmModeForPath(p) {
    const dot = String(p).lastIndexOf(".");
    if (dot < 0) return null;
    const m = CM_MODES[p.slice(dot).toLowerCase()];
    // Modi sind je nach Registration unter CodeMirror.modes (Namens-Key,
    // z.B. "clike") ODER CodeMirror.mimeModes (MIME-Key, z.B. "text/x-c++src")
    // verfügbar — beide Tabellen prüfen.
    if (!m || !window.CodeMirror) return null;
    const known = (CodeMirror.modes && CodeMirror.modes[m]) ||
                  (CodeMirror.mimeModes && CodeMirror.mimeModes[m]);
    return known ? m : null;
  }

  // ─── Medien-Erkennung (Preview statt Editor) ────────────────────
  const IMG_EXTS = [".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp"];
  const VIDEO_EXTS = [".mp4", ".webm", ".ogv", ".mov", ".m4v"];
  const AUDIO_EXTS = [".mp3", ".wav", ".ogg", ".oga", ".m4a", ".flac", ".aac"];

  function mediaKind(path, contentType) {
    const p = String(path).toLowerCase();
    const ends = exts => exts.some(e => p.endsWith(e));
    if (ends(IMG_EXTS) || (contentType || "").startsWith("image/")) return "image";
    if (ends(VIDEO_EXTS)) return "video";
    if (ends(AUDIO_EXTS)) return "audio";
    if (p.endsWith(".pdf")) return "pdf";
    if ((contentType || "").includes("octet-stream")) return "binary";
    return null;
  }

  function encPath(p) {
    return String(p).split("/").map(encodeURIComponent).join("/");
  }

  // ─── Kontextmenü (global, max. 1 offen) ─────────────────────────
  let ctxMenu = null, ctxEsc = null, ctxClick = null;
  function hideCtxMenu() {
    if (ctxMenu) { ctxMenu.remove(); ctxMenu = null; }
    if (ctxEsc) { document.removeEventListener("keydown", ctxEsc); ctxEsc = null; }
    if (ctxClick) { document.removeEventListener("click", ctxClick, true); ctxClick = null; }
  }
  function showCtxMenu(x, y, items) {
    hideCtxMenu();
    const menu = document.createElement("div");
    menu.className = "ws-ctx-menu";
    items.forEach(it => {
      if (it.sep) {
        const s = document.createElement("div");
        s.className = "ws-ctx-sep";
        menu.appendChild(s);
        return;
      }
      const el = document.createElement("div");
      el.className = "ws-ctx-item" + (it.danger ? " ws-ctx-danger" : "") +
        (it.header ? " ws-ctx-header" : "");
      el.textContent = it.label;
      el.title = it.label;
      if (!it.header && it.fn) el.onclick = () => { hideCtxMenu(); it.fn(); };
      menu.appendChild(el);
    });
    document.body.appendChild(menu);
    const r = menu.getBoundingClientRect();
    menu.style.left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8)) + "px";
    menu.style.top = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + "px";
    ctxMenu = menu;
    ctxEsc = e => { if (e.key === "Escape") hideCtxMenu(); };
    ctxClick = e => { if (ctxMenu && !ctxMenu.contains(e.target)) hideCtxMenu(); };
    // Listener erst nach dem klick, der das Menü geöffnet hat
    setTimeout(() => {
      document.addEventListener("keydown", ctxEsc);
      document.addEventListener("click", ctxClick, true);
    }, 0);
  }

  // ─── Init ──────────────────────────────────────────────
  // opts:
  //   treeEl, editorEl      HTMLElement (Pflicht)
  //   mediaEl               HTMLElement | null (Medien-Preview-Bereich)
  //   apiBase               string | null (null = Task noch nicht gespeichert;
  //                         nachträglich setzbar per setApiBase — „Neu"-Seite)
  //   mainFile              Ausgangs-Main-Datei ("" = keine)
  //   allowMain             ⭐-Marker + Main-Datei setzbar (Kontextmenü)
  //   readOnlyCheck         fn(path) -> bool
  //   canCreate / canDelete / canMove / allowBulk   boolesche Rechte
  //   canUpload             Upload: Kontextmenü-Items „Dateien/Ordner
  //                         hochladen“ + Drag & Drop von Dateien/Ordnern
  //                         aus dem OS direkt auf den Dateibaum
  //   canReorder            Drag & Drop ändert die Reihenfolge (Dateien + Ordner)
  //   localOrderKey         localStorage-Key für die LOKALE Reihenfolge
  //                         (Student: eigene Ansicht, pro User+Task); Tutor:
  //                         weglassen (global via /reorder-API)
  //   canSetAccess          Kontextmenü bietet „Zugriff“ (Tutor: true)
  //   folderMove            Ordner-Move = ein API-Call /folders/move (Tutor)
  //   folderApi             Ordner werden on-disk verwaltet (Student: true):
  //                         anlegen/möbeln/löschen via /folders-Endpoints,
  //                         auch wenn leer (bleibt nach Reload sichtbar)
  //   moveGate              fn(src, dst) -> {ok, reason?}
  //   onRunScript(path) / onStopScript(path)
  //                         ▶/⏹-Button an .sh-Dateien im Baum (Student:
  //                         Terminal-Run, s. task_solve_workspace.html)
  //   saveStateEl           optionales HTMLElement (Auto-Save-Status)
  //   emptyMsg / noTaskMsg  Platzhalter im Baum
  //   onMainFileChange(p) / onFilesLoaded(files)
  //   onViewChanged(view)   fn("editor"|"media") — die Editor-Fläche hat
  //                         gerade den Fokus (Template nutzt das für
  //                         Terminal-/Preview-Views; default: ignoriert)
  //   onChanged()             fn — der Dateibaum wurde geändert (Datei
  //                         angelegt/gelöscht/bewegt, Zugriff oder Reihen-
  //                         folge geändert; alle Operationen enden in
  //                         refresh()). Template nutzt das, um den
  //                         Task-Auto-Save zu triggern (Versions-Update);
  //                         Student: null (ignoriert)
  function init(opts) {
    const {
      treeEl, editorEl, mediaEl = null, apiBase: _apiBase = null,
      mainFile = "", allowMain = false,
      readOnlyCheck = () => false,
      canCreate = false, canDelete = false, canMove = false, allowBulk = false,
      canUpload = false,
      canReorder = false, canSetAccess = false, folderMove = false,
      folderApi = false,
      localOrderKey = null,
      moveGate = () => ({ ok: true }),
      onRunScript = null, onStopScript = null,
      runDisabled = null,  // fn() -> bool: ▶-Buttons deaktiviert (z. B.
                           // Container nicht gestartet); beim Render
                           // und vom Template nach Status-Wechseln abgefragt
      saveStateEl = null,
      emptyMsg = "(keine Dateien)", noTaskMsg = null,
      onMainFileChange = null, onFilesLoaded = null,
      onViewChanged = null, onChanged = null,
    } = opts;
    // Mutabel: auf der „Neue Aufgabe"-Seite null (Task noch nicht angelegt) —
    // nach dem ersten Save per setApiBase() setzen, dann lädt refresh()
    // den Baum ohne Reload.
    let apiBase = _apiBase;

    const state = {
      files: [],            // [{path, size, is_binary, access, file_access, init}]
      folders: [],          // Ordner [{path, access, own}] — own: null =
                           // impliziter on-disk-Ordner ohne Klassen-Zeile
      folderMap: {},        // path → explizite Klasse ("readonly"/"hidden")
      folderOrder: {},      // path → Anzeige-Order (Ordner + [init]-Artefakte, Backend)
      localOrder: {},       // Student: lokale Reihenfolge je Ordner (localStorage)
      currentFile: null,
      fileFocused: true,    // Zeilen-Highlight: true, solange das Verzeichnis
                            // „den Fokus“ hat (Student: Terminal/Preview-Views
                            // entziehen ihn per setFileFocused; global genau
                            // ein fokussiertes Element über alle Listen)
      currentViewKind: "editor",  // "editor" | "media" — Ansicht der Current-Datei
      dirty: false,
      mainFile: String(mainFile || ""),
      collapsed: new Set(), // zugeklappte Ordner (Pfad ohne Slash)
      extraDirs: new Set(), // client-seitige Ordner (nicht persistiert)
      mediaUrl: null,
      saveTimer: null,
      suppressChange: false, // true während programatischem cm.setValue
      dragPath: null,
      dragType: null,       // "file" | "dir" (während des Drags gesetzt)
      dragRejected: null,   // letzter Ablehnungs-Grund (→ Fehlermeldung beim dragend)
      dragMulti: false,     // Drag zieht die ganze Mehrfachauswahl mit
      selected: new Set(),  // Mehrfachauswahl (Datei-Pfade; markiert ab 2)
      selAnchor: null,      // Anker-Pfad für Shift-Bereichsauswahl
      visibleFiles: [],     // sichtbare Datei-Pfade in Baum-Reihenfolge
      scriptRuns: new Set(),  // laufende .sh-Skripte (▶→⏹ im Baum)
    };

    // ── Lokale Reihenfolge (Student: nur eigene Ansicht) ──────────
    // {"<ordner>": {"dirs": [Namen], "files": [Namen]}} — nur explizit
    // per Drag geänderte Gruppen; neue Einträge sortieren sich ans Ende.
    function loadLocalOrder() {
      if (!localOrderKey) return {};
      try {
        const raw = localStorage.getItem(localOrderKey);
        const o = raw ? JSON.parse(raw) : null;
        return (o && typeof o === "object") ? o : {};
      } catch (err) { return {}; }
    }
    function saveLocalOrder() {
      if (!localOrderKey) return;
      try { localStorage.setItem(localOrderKey, JSON.stringify(state.localOrder)); }
      catch (err) { /* ignore (z. B. Quota/Privatmodus) */ }
    }
    state.localOrder = loadLocalOrder();

    // ── Effektive Zugriffs-Klassen (client-seitig; Primärquelle ist das
    //    Backend-Feld `access` — diese Berechnung dient als Fallback +
    //    für Ordner/Probe-Pfade) ────────────────────────────────────
    // [init]-Erkennung: das Backend flaggt Init-Artefakte (init: true) —
    // sie liegen am realen Pfad und sind read-only.
    // [init]-Ordner: Manifest-Eintrag aus dem Vor-/Nach-Diff des letzten
    // Init-Builds (state.folders-Zeile mit init-Flag) — exakt die Ordner,
    // die .init.sh angelegt hat (auch leere). Explizite Tutor-Zeilen
    // gewinnen (dann kein init-Flag → editierbar).
    function isInitDir(path) {
      path = String(path || "");
      if (!path) return false;
      return state.folders.some(fd => fd.path === path && fd.init);
    }
    // Auch für Pfade, die IN einem [init]-Ordner liegen: neue
    // Dateien/Ordner (Anlegen/Upload) oder Move-Ziele — der Pfad
    // existiert noch nicht, aber ein Vorfahr-Ordner ist [init].
    function isInitPath(path) {
      const f = state.files.find(x => x.path === String(path || ""));
      if (f) return !!f.init;
      let q = String(path || "");
      while (q) {
        if (isInitDir(q)) return true;
        q = q.includes("/") ? q.slice(0, q.lastIndexOf("/")) : "";
      }
      return false;
    }
    function ancestorRank(path) {
      let rank = 0;
      let parent = dirOf(path);
      while (parent) {
        rank = Math.max(rank, ACCESS_RANK[state.folderMap[parent] || "edit"] || 0);
        parent = dirOf(parent);
      }
      return rank;
    }
    function effWithAncestors(path, own) {
      const ownRank = ACCESS_RANK[own || "edit"] || 0;
      return ACCESS_BY_RANK[Math.max(ownRank, ancestorRank(path))];
    }
    function effectiveAccess(path) {
      const p = String(path || "").replace(/\/+$/, "");
      if (!p) return "edit";
      const f = state.files.find(x => x.path === p);
      if (f) return f.access || effWithAncestors(p, f.file_access);
      return effWithAncestors(p, state.folderMap[p]);
    }

    const cm = window.CodeMirror.fromTextArea(editorEl, {
      // Explizit „text/plain“: ohne Mode würde CM5 den ERSTEN registrierten
      // Mode als Default nehmen (hier „python“) — irreführend vor der ersten
      // Datei. openFile() setzt pro Datei den korrekten Mode.
      mode: "text/plain",
      theme: "oneDark",
      lineNumbers: true,
      autoCloseBrackets: true,
      indentUnit: 4,
      tabSize: 4,
      lineWrapping: true,
      matchBrackets: true,
    });

    // ── Trenner Tree/Editor (ziehen → Baum/Editor vergrößern) ──────
    // Orientation folgt dem Layout-Breakpoint (sm = 640 px):
    // Desktop (Zeilen) ändert die Breite des Tree-Panels,
    // Mobile (Spalten) seine Höhe. Doppelklick = Standardgröße.
    // Scoped auf DIESE Instanz (Tree-Wrapper statt Dokument): Task-Seiten
    // können neben dem Live-Workspace mehrere read-only Abgabe-Workspaces
    // betten — gleiche IDs, jede Instanz findet ihre eigenen Elemente.
    const instanceWrap = treeEl ? treeEl.closest(".ws-tree-editor-wrap") : null;
    const splitHandle = instanceWrap
      ? instanceWrap.querySelector("#ws-split-handle") : null;
    const splitPanel = splitHandle && treeEl ? treeEl.parentElement : null;
    const splitWrap = splitHandle ? splitHandle.closest(".ws-tree-editor-wrap") : null;
    const SPLIT_MIN = 180;
    if (splitHandle && splitPanel && splitWrap) {
      // Klick/Doppelklick auf den (leeren) Handle würde sonst einen
      // Text-Caret im Browser platzieren → blinkender Cursor im Trenner.
      splitHandle.addEventListener("mousedown", (e) => e.preventDefault());
      splitHandle.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        const horizontal = window.innerWidth >= 640;
        const rect = splitPanel.getBoundingClientRect();
        const startX = e.clientX, startY = e.clientY;
        const startSize = horizontal ? rect.width : rect.height;
        const maxSize = () => {
          const r = splitWrap.getBoundingClientRect();
          return Math.floor((horizontal ? r.width : r.height) * 0.6);
        };
        let raf = 0;
        function onMove(ev) {
          const delta = (horizontal ? ev.clientX : ev.clientY)
                        - (horizontal ? startX : startY);
          const size = Math.max(SPLIT_MIN, Math.min(startSize + delta, maxSize()));
          if (horizontal) splitPanel.style.width = size + "px";
          else splitPanel.style.height = size + "px";
          if (!raf) raf = requestAnimationFrame(() => { raf = 0; cm.refresh(); });
        }
        function onUp() {
          splitHandle.removeEventListener("pointermove", onMove);
          splitHandle.removeEventListener("pointerup", onUp);
          splitHandle.removeEventListener("pointercancel", onUp);
          splitHandle.classList.remove("ws-split-dragging");
          document.body.classList.remove("ws-split-drag", "ws-split-drag-h", "ws-split-drag-v");
          cm.refresh();
          // Editor-Fläche hat sich geändert (Split-Handle) — das feuert
          // KEIN Fenster-Resize: Layout-Listener (z. B. Student-Terminal
          // → xterm neu fitten) explizit informieren.
          document.dispatchEvent(new CustomEvent("ws-layout-changed"));
        }
        splitHandle.setPointerCapture(e.pointerId);
        splitHandle.classList.add("ws-split-dragging");
        document.body.classList.add("ws-split-drag",
                                     horizontal ? "ws-split-drag-h" : "ws-split-drag-v");
        splitHandle.addEventListener("pointermove", onMove);
        splitHandle.addEventListener("pointerup", onUp);
        splitHandle.addEventListener("pointercancel", onUp);
      });
      splitHandle.addEventListener("dblclick", () => {
        splitPanel.style.width = "";
        splitPanel.style.height = "";
        cm.refresh();
      });
      // Inline-Maß beim Breakpoint-Übergang verwerfen (sonst bliebe
      // z. B. die Desktop-Breite im Mobile-Layout hängen).
      window.addEventListener("resize", () => {
        if (window.innerWidth < 640) splitPanel.style.width = "";
        else splitPanel.style.height = "";
      });
    }

    // ── Vollbild (Tree + Editor füllt das ganze Browser-Fenster) ─────
    // Toggle-Button in der Tree-Toolbar (beide Templates). ESC beendet
    // den Modus — das CodeMirror-Search-Dialog konsumiert ESC vor uns
    // (e_stop → stopPropagation), daher keine Kollision.
    const fsBtn = instanceWrap
      ? (instanceWrap.querySelector("#ws-fullscreen-btn") ||
         instanceWrap.querySelector("#ws-files-fullscreen-btn"))
      : null;
    const fsWrap = fsBtn ? fsBtn.closest(".ws-tree-editor-wrap") : null;
    function setFullscreen(on) {
      if (!fsWrap) return;
      fsWrap.classList.toggle("ws-fullscreen", on);
      document.body.classList.toggle("ws-fs-lock", on);
      if (fsBtn) {
        fsBtn.classList.toggle("ws-tree-btn-active", on);
        fsBtn.title = on ? "Vollbild beenden (Esc)" : "Vollbild";
      }
      // CodeMirror bemerkt Größenänderungen nicht selbst → Refresh.
      requestAnimationFrame(() => cm.refresh());
      // Wie beim Split-Handle: Layout-Change ohne Fenster-Resize.
      document.dispatchEvent(new CustomEvent("ws-layout-changed"));
    }
    if (fsBtn && fsWrap) {
      fsBtn.addEventListener("click", () =>
        setFullscreen(!fsWrap.classList.contains("ws-fullscreen")));
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && fsWrap.classList.contains("ws-fullscreen"))
          setFullscreen(false);
      });
    }

    // ── Status ─────────────────────────────────────────────────────
    function setSaveState(txt) {
      if (saveStateEl) saveStateEl.textContent = txt || "";
    }

    // ── Baum-Bau (Dateien + persistierte Ordner + client-seitige Ordner) ──
    function buildTree() {
      const root = {};
      const ensureDir = parts => {
        let node = root;
        parts.forEach(part => {
          if (!node[part] || node[part].file) node[part] = {};
          node = node[part];
        });
      };
      state.extraDirs.forEach(d => ensureDir(d.split("/")));
      state.folders.forEach(fd => ensureDir(fd.path.split("/")));
      state.files.forEach(f => {
        const parts = f.path.split("/");
        let node = root;
        parts.forEach((part, i) => {
          if (!node[part] || node[part].file) node[part] = {};
          node = node[part];
          if (i === parts.length - 1) {
            node.file = {
              size: f.size,
              binary: !!f.is_binary,
              fullPath: f.path,
              sort_order: f.sort_order == null ? null : f.sort_order,
              access: f.access || null,
              file_access: f.file_access == null ? null : f.file_access,
              init: !!f.init,
            };
          }
        });
      });
      return root;
    }

    function allDirPaths() {
      const dirs = new Set();
      state.files.forEach(f => {
        const parts = f.path.split("/");
        let p = "";
        for (let i = 0; i < parts.length - 1; i++) {
          p = p ? p + "/" + parts[i] : parts[i];
          dirs.add(p);
        }
      });
      state.folders.forEach(fd => dirs.add(fd.path));
      state.extraDirs.forEach(d => dirs.add(d));
      return Array.from(dirs).sort();
    }

    // ── Drag & Drop ───────────────────────────────────────────────
    function dirOf(p) {
      return p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "";
    }
    // Datei-Reihenfolge: sort_order (null = letzter Platz), dann alphabetisch.
    function cmpFileOrder(a, b) {
      const oa = a.sort_order == null ? Number.POSITIVE_INFINITY : a.sort_order;
      const ob = b.sort_order == null ? Number.POSITIVE_INFINITY : b.sort_order;
      return (oa - ob) || a.fullPath.localeCompare(b.fullPath);
    }
    // ── Gruppen-Reihenfolge (Dateien + Ordner je Ordner) ──────────
    // Anzeige: lokale Student-Ordnung (localStorage) > gepflegte
    // sort_order (Dateien: DB; Ordner/[init]-Artefakte: folder_order)
    // > alphabetisch.
    function localPos(dir, kind, name) {
      const saved = state.localOrder[dir] && state.localOrder[dir][kind];
      if (!Array.isArray(saved)) return null;
      const i = saved.indexOf(name);
      return i === -1 ? null : i;
    }
    function cmpGroup(dir, kind, defaultCmp) {
      return (a, b) => {
        const pa = localPos(dir, kind, a.name);
        const pb = localPos(dir, kind, b.name);
        if (pa !== null || pb !== null) {
          const va = pa === null ? Number.MAX_SAFE_INTEGER : pa;
          const vb = pb === null ? Number.MAX_SAFE_INTEGER : pb;
          if (va !== vb) return va - vb;
        }
        return defaultCmp(a, b);
      };
    }
    // Ordner direkt unter dir in der Grund-Reihenfolge (Namen).
    function currentDirNames(dir) {
      return allDirPaths()
        .filter(d => dirOf(d) === dir &&
                     (effectiveAccess(d) !== "hidden" || canSetAccess))
        .sort((a, b) => {
          const oa = state.folderOrder[a], ob = state.folderOrder[b];
          const va = oa == null ? Number.MAX_SAFE_INTEGER : oa;
          const vb = ob == null ? Number.MAX_SAFE_INTEGER : ob;
          if (va !== vb) return va - vb;
          return a.localeCompare(b);
        })
        .map(d => d.split("/").pop());
    }
    // Dateien direkt unter dir in der Grund-Reihenfolge (Namen).
    function currentFileNames(dir) {
      return state.files
        .filter(f => dirOf(f.path) === dir)
        .sort((a, b) => {
          const oa = a.sort_order == null ? Number.MAX_SAFE_INTEGER : a.sort_order;
          const ob = b.sort_order == null ? Number.MAX_SAFE_INTEGER : b.sort_order;
          return (oa - ob) || a.path.localeCompare(b.path);
        })
        .map(f => f.path.split("/").pop());
    }
    // Aktuelle Anzeigereihenfolge einer Gruppe (lokal vor global).
    function currentGroupNames(dir, kind) {
      const base = kind === "dirs" ? currentDirNames(dir) : currentFileNames(dir);
      const saved = state.localOrder[dir] && state.localOrder[dir][kind];
      if (!Array.isArray(saved) || !saved.length) return base;
      const pos = new Map(saved.map((n, i) => [n, i]));
      return base.slice().sort((a, b) => {
        const ia = pos.has(a) ? pos.get(a) : Number.MAX_SAFE_INTEGER;
        const ib = pos.has(b) ? pos.get(b) : Number.MAX_SAFE_INTEGER;
        return ia - ib;  // stabil → Gleichstand behält Grund-Reihenfolge
      });
    }
    // Fehlertext für abgelehnte Drop-Ziele („Bereits dort.“ bleibt still).
    function rejectReason(gate) {
      return (gate.reason && gate.reason !== "Bereits dort.") ? gate.reason : null;
    }
    // Ordner mit Inhalt (Dateien oder persistierter Zugriffs-Klasse) sind
    // verschiebbar. Student: zusätzlich nur effektiv editierbare.
    function canDragDir(dir) {
      if (!canMove) return false;
      if (isInitDir(dir)) return false;  // .init.sh-Ergebnis: read-only
      const hasContent = state.files.some(f => f.path.startsWith(dir + "/")) ||
        state.folders.some(fd => fd.path === dir || fd.path.startsWith(dir + "/"));
      if (!hasContent) return false;
      if (folderMove) return true;
      // Student: Gate über Subtree-Dateien bzw. Probe-Pfad (leerer Ordner)
      const sub = filesInDir(dir);
      if (sub.length) return !!moveGate(sub[0].path, sub[0].path).ok;
      return !!moveGate(dir + "/__.probe", dir + "/__.probe").ok;
    }
    function dropGateDir(srcDir, dstDir) {
      const curParent = srcDir.includes("/") ? srcDir.slice(0, srcDir.lastIndexOf("/")) : "";
      if (dstDir === curParent) return { ok: false, reason: "Bereits dort." };
      if (dstDir === srcDir || dstDir.startsWith(srcDir + "/")) {
        return { ok: false, reason: "Ein Ordner kann nicht in sich selbst verschoben werden." };
      }
      // Zugriffs-Klassen-Check über eine repräsentative Datei (alle
      // wechseln gleich) — z. B. blockiert der Student-MoveGate Moves
      // aus 🔒/👤-Bereichen.
      const sub = filesInDir(srcDir);
      if (sub.length) {
        const f = sub[0];
        const subPath = f.path.slice(srcDir.length + 1);
        return moveGate(f.path, dstDir ? dstDir + "/" + subPath : subPath);
      }
      return { ok: true };
    }
    function dropGate(src, dstDir) {
      const name = src.split("/").pop();
      const dst = dstDir ? dstDir + "/" + name : name;
      if (dst === src) return { ok: false, reason: "Bereits dort." };
      if (dstDir && src.startsWith(dstDir + "/")) {
        return { ok: false, reason: "Datei liegt bereits in diesem Ordner." };
      }
      return moveGate(src, dst);
    }

    // ── Mehrfachauswahl (Strg/Cmd = Toggle, Shift = Bereich) ─────────
    function clearSelection() {
      state.selected = new Set();
      state.selAnchor = null;
    }
    // Auswahl ohne .init.sh-Ergebnisse (read-only, per Init-Build erzeugt).
    function bulkTargets() {
      return [...state.selected].filter(p => {
        const f = state.files.find(x => x.path === p);
        return !(f && f.init);
      });
    }
    // Gate für Bulk-Move: alle gewählten Dateien müssen ins Ziel dürfen
    // (Dateien, die bereits dort liegen, blockieren nicht).
    function dropGateMulti(dstDir) {
      for (const s of bulkTargets()) {
        const g = dropGate(s, dstDir);
        if (!g.ok && g.reason !== "Bereits dort." &&
            g.reason !== "Datei liegt bereits in diesem Ordner.") return g;
      }
      return { ok: true };
    }

    // Verschieben ohne Bestätigungs-Popup (sichtbar im Baum, per ↺/Undo nicht
    // nötig — Ziel ist beim Drop explizit gewählt).
    function doMove(src, dstDir, multi) {
      if (multi) {
        moveSelectedTo(dstDir);
        return;
      }
      const gate = dropGate(src, dstDir);
      if (!gate.ok) {
        if (gate.reason && gate.reason !== "Bereits dort.") toast(gate.reason, "warning");
        return;
      }
      const name = src.split("/").pop();
      const dst = dstDir ? dstDir + "/" + name : name;
      moveFile(src, dst);
    }

    // Ordner verschieben: Gate-Check über alle Subtree-Dateien, dann
    // je nach Rolle: Tutor = ein Call /folders/move (Disk + Datei-Rows +
    // Ordner-Klassen-Zeilen ziehen mit); Student = sequentiell jede Datei
    // (Ordner-Klassen ändert ein Student nie). Leere client-seitige Ordner
    // (extraDirs) werden nur umgemappt, ohne API-Call.
    async function moveDirImpl(srcDir, newDir) {
      // Auch on-disk-Ordner (implizite state.folders-Einträge) blockieren
      // den Zielnamen — sonst würde der Ordner in sich selbst wandern.
      if (state.files.some(f => f.path === newDir || f.path.startsWith(newDir + "/")) ||
          state.folders.some(fd => fd.path === newDir)) {
        toast("„" + newDir + "“ existiert bereits — bitte umbenennen.", "error");
        return;
      }
      if (folderMove) {
        const persisted = filesInDir(srcDir).length > 0 ||
          state.folders.some(fd => fd.path === srcDir || fd.path.startsWith(srcDir + "/"));
        if (persisted) {
          try {
            const res = await fetch(apiBase + "/folders/move", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              credentials: "same-origin",
              body: JSON.stringify({ src: srcDir, dst: newDir }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || res.status);
          } catch (err) {
            toast("Ordner-Verschieben fehlgeschlagen: " + err.message, "error");
            return;
          }
        }
      } else {
        const inDir = filesInDir(srcDir);
        // On-disk-Ordner (z. B. via Terminal/mkdir) ohne Dateien: ein
        // Call verschiebt den Ordner im Volume. Reine extraDirs-Ordner
        // (nur client-seitig) werden unten nur umgemappt.
        const onDisk = state.folders.some(fd => fd.path === srcDir && !fd.own);
        if (!inDir.length && onDisk) {
          try {
            const res = await fetch(apiBase + "/folders/move", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              credentials: "same-origin",
              body: JSON.stringify({ src: srcDir, dst: newDir }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || res.status);
          } catch (err) {
            toast("Ordner-Verschieben fehlgeschlagen: " + err.message, "error");
            return;
          }
        } else {
          for (const f of inDir) {
            const sub = f.path.slice(srcDir.length + 1);
            const g = moveGate(f.path, newDir + "/" + sub);
            if (!g.ok) {
              toast(g.reason || ("„" + f.path + "“ kann nicht verschoben werden."), "warning");
              return;
            }
          }
          for (const f of inDir) {
            const sub = f.path.slice(srcDir.length + 1);
            if (!await moveFile(f.path, newDir + "/" + sub)) return;
          }
        }
      }
      // client-seitige (leere) Ordner des Subtrees neu mappen
      Array.from(state.extraDirs).forEach(d => {
        if (d === srcDir) {
          state.extraDirs.delete(d);
          state.extraDirs.add(newDir);
        } else if (d.startsWith(srcDir + "/")) {
          state.extraDirs.delete(d);
          state.extraDirs.add(newDir + d.slice(srcDir.length));
        }
      });
      state.extraDirs.delete(srcDir);  // Quell-Ordner wird leer → entfernen
      await refresh();
    }

    async function doMoveDir(srcDir, dstDir) {
      const gate = dropGateDir(srcDir, dstDir);
      if (!gate.ok) {
        if (gate.reason && gate.reason !== "Bereits dort.") toast(gate.reason, "warning");
        return;
      }
      const name = srcDir.split("/").pop();
      await moveDirImpl(srcDir, dstDir ? dstDir + "/" + name : name);
    }

    // Sortierung: src in dir vor/nach target einsortieren (Gruppe
    // "files"|"dirs"). Student: nur eigene Ansicht (localStorage, kein
    // API-Call); Tutor: global per /reorder persistiert.
    async function reorderInDir(dir, kind, src, target, before) {
      const srcName = src.split("/").pop();
      const tName = target.split("/").pop();
      const without = currentGroupNames(dir, kind).filter(n => n !== srcName);
      let idx = without.indexOf(tName);
      if (idx === -1) return;
      if (!before) idx += 1;
      const ordered = without.slice(0, idx).concat(srcName, without.slice(idx));
      if (localOrderKey) {
        state.localOrder[dir] = Object.assign({}, state.localOrder[dir], {
          [kind]: ordered,
        });
        saveLocalOrder();
        renderTree();
        return;
      }
      const full = n => (dir ? dir + "/" + n : n);
      // Tutor: leere client-seitige Ordner (extraDirs) haben keine
      // Backend-Präsenz → filtern (behalten die Grund-Position).
      const send = kind === "dirs"
        ? ordered.filter(n => !state.extraDirs.has(full(n))).map(full)
        : ordered.map(full);
      if (!send.length) return;
      try {
        const res = await fetch(apiBase + "/reorder", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ order: send }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.status);
        await refresh();
      } catch (err) {
        toast("Sortierung fehlgeschlagen: " + err.message, "error");
        await refresh();
      }
    }
    // Ordner auf Datei-Zeile im selben Ordner: die Ordner-Gruppe steht
    // immer über den Dateien → Ordner ans Ende der Gruppe.
    function reorderDirToEnd(dir, srcDir) {
      const others = currentGroupNames(dir, "dirs")
        .filter(n => n !== srcDir.split("/").pop());
      if (!others.length) return;
      const last = others[others.length - 1];
      reorderInDir(dir, "dirs", srcDir, dir ? dir + "/" + last : last, false);
    }

    // ── Zeilen ────────────────────────────────────────────────────────────
    function fileRow(name, meta, path, depth) {
      const div = document.createElement("div");
      const isInit = !!meta.init;
      const acc = isInit ? "readonly" : (meta.access || effectiveAccess(path));
      const accDef = ACCESS[acc] || ACCESS.edit;
      const isMain = allowMain && path === state.mainFile;
      const active = path === state.currentFile && state.fileFocused;
      const ro = readOnlyCheck(path);
      // ▶/⏹ an .sh-Dateien (Template-Callback: Terminal-Run)
      const isSh = !!(onRunScript && /\.sh$/i.test(name));
      const runActive = isSh && state.scriptRuns.has(path);
      // Container nicht gestartet (z. B. Cold-Review) → ▶ blockiert
      // (Analog zum Terminal-„+“; ⏹ laufender Skripte bleibt möglich).
      const runBlocked = isSh && !runActive &&
        !!(runDisabled && runDisabled());
      // Alle Dateien sind per Handle ziehbar; Zugriffs-Klassen/skriptfeste
      // Pfade blockt der Drop-Gate (mit Fehlermeldung, nicht ohne Handle).
      const draggable = !!(canMove || canReorder);
      // Mehrfachauswahl (blau) hat Vorrang vor der Aktive-Markierung;
      // bei einer einzelnen Auswahl bleibt die normale Aktive-Färbung.
      const multiSel = state.selected.size > 1 && state.selected.has(path);
      div.className = "ws-row ws-row-file px-1 py-1.5 rounded flex items-center gap-1 " +
        (multiSel ? "bg-blue-100 text-blue-900"
                  : active ? "bg-indigo-100 text-indigo-900"
                           : "text-gray-700 hover:bg-gray-200");
      // Handle bleibt linksbündig; die Tiefe-Einrückung sitzt am Icon.
      div.style.paddingLeft = "2px";
      div.dataset.path = path;
      div.title = isInit
        ? ".init.sh-Ergebnis — read-only (wird per neuem Init-Build neu erzeugt)"
        : (acc !== "edit" ? accDef.label : "");
      div.innerHTML =
        '<span class="ws-drag' + (draggable ? "" : " ws-drag-off") + '"' +
        (draggable ? ' title="Ziehen: verschieben / sortieren"' : "") + ">⠿</span>" +
        '<span class="shrink-0"' + (depth ? ' style="margin-left:' + (depth * 14) + 'px"' : "") + ">" + fileIcon(path) + "</span>" +
        (isInit
          ? '<span title=".init.sh-Ergebnis — read-only">📦</span>'
          : '<span title="' + esc(accDef.label) + '">' + accDef.icon + "</span>") +
        (isSh
          ? '<button type="button" class="ws-runbtn shrink-0 select-none inline-flex items-center justify-center h-5 w-5 rounded text-white ' +
            (runActive ? "bg-red-600 hover:bg-red-700"
                       : runBlocked ? "bg-gray-300 cursor-not-allowed"
                                    : "bg-green-600 hover:bg-green-700") +
            '" data-path="' + esc(path) + '"' +
            (runBlocked ? " disabled" : "") + ' title="' +
            (runActive ? "Skript stoppen"
                       : runBlocked ? "Container läuft nicht — erst ▶ Starten"
                                    : "Skript ausführen") + '">' +
            (runActive ? WSTOP_SVG : WPLAY_SVG) + "</button>"
          : "") +
        '<span class="truncate flex-1">' + esc(name) + "</span>" +
        (isMain ? '<span class="shrink-0" title="Main-Datei (Editor-Fokus)">⭐</span>' : "") +
        '<span class="text-[10px] text-gray-400 shrink-0">' + fmtBytes(meta.size) + "</span>";
      div.onclick = e => {
        // Strg/Cmd = Auswahl umschalten, Shift = Bereich vom Anker,
        // Einfach-Klick = öffnen (Auswahl = nur diese Datei).
        if (e.ctrlKey || e.metaKey) {
          if (state.selected.has(path)) state.selected.delete(path);
          else state.selected.add(path);
          state.selAnchor = path;
          renderTree();
          return;
        }
        if (e.shiftKey) {
          const list = state.visibleFiles;
          const a = state.selAnchor ? list.indexOf(state.selAnchor) : -1;
          const b = list.indexOf(path);
          if (a !== -1 && b !== -1) {
            const lo = Math.min(a, b), hi = Math.max(a, b);
            state.selected = new Set(list.slice(lo, hi + 1));
          } else {
            state.selected = new Set([path]);
          }
          state.selAnchor = path;
          renderTree();
          return;
        }
        state.selected = new Set([path]);
        state.selAnchor = path;
        openFile(path);
      };
      div.oncontextmenu = e => showFileMenu(e, path);
      // Drag-Handle (nicht die ganze Zeile → Long-Press auf Touch-Geräten
      // triggert weiterhin das Kontextmenü statt Drag & Drop).
      if (draggable) {
        const handle = div.querySelector(".ws-drag");
        handle.draggable = true;
        handle.ondragstart = e => {
          state.dragPath = path;
          state.dragType = "file";
          state.dragRejected = null;
          state.dragMulti = state.selected.size > 1 && state.selected.has(path);
          e.dataTransfer.effectAllowed = "move";
          try {
            e.dataTransfer.setData("text/plain",
              state.dragMulti ? [...state.selected].join("\n") : path);
          } catch (err) { /* IE */ }
          e.stopPropagation();
        };
        handle.ondragend = () => {
          const rejected = state.dragRejected;
          state.dragPath = null;
          state.dragType = null;
          state.dragMulti = false;
          state.dragRejected = null;
          clearDropMarks();
          if (rejected) toast(rejected, "warning");
        };
      }
      if (isSh) {
        const rb = div.querySelector(".ws-runbtn");
        rb.onclick = e => {
          e.stopPropagation();
          if (state.scriptRuns.has(path)) {
            if (onStopScript) onStopScript(path);
          } else if (onRunScript) onRunScript(path);
        };
      }
      // Drop-Target: in den Ordner dieser Datei ziehen (auch über Dateien in
      // geöffneten Ordnern) oder — wenn sortierbar und im selben Ordner —
      // vor/nach der Datei einsortieren (obere/halbe Zeile = davor, untere = danach).
      div.ondragover = e => {
        if (externalFileDrag(e)) {
          // Externer File-Drag: Upload in den Ordner dieser Datei.
          if (!uploadGate(dirOf(path))) return;
          e.preventDefault();
          e.stopPropagation();
          e.dataTransfer.dropEffect = "copy";
          treeEl.classList.remove("ws-tree-droproot");
          div.classList.add("ws-drop-ok");
          return;
        }
        if (!state.dragPath) return;
        if (!state.dragMulti && state.dragPath === path) return;
        const src = state.dragPath;
        const myDir = dirOf(path);
        // Ordner-Drag: Datei-Zeile im selben Ordner = Ordner-Gruppe neu
        // ordnen (Ordner stehen über allen Dateien → ans Ende); sonst
        // Drop-Target „in den Ordner dieser Datei“.
        if (state.dragType === "dir") {
          if (canReorder && dirOf(src) === myDir) {
            e.preventDefault();
            e.stopPropagation();
            e.dataTransfer.dropEffect = "move";
            treeEl.classList.remove("ws-tree-droproot");
            div.classList.add("ws-drop-after");
            return;
          }
          const gd = dropGateDir(src, myDir);
          if (!gd.ok) { state.dragRejected = rejectReason(gd); return; }
          state.dragRejected = null;
          e.preventDefault();
          e.stopPropagation();
          e.dataTransfer.dropEffect = "move";
          treeEl.classList.remove("ws-tree-droproot");
          div.classList.add("ws-drop-ok");
          return;
        }
        // Mehrfach-Drag: alle gewählten Dateien müssen ins Ziel dürfen.
        if (state.dragMulti) {
          const gm = dropGateMulti(myDir);
          if (!gm.ok) { state.dragRejected = rejectReason(gm); return; }
          state.dragRejected = null;
          e.preventDefault();
          e.stopPropagation();
          e.dataTransfer.dropEffect = "move";
          treeEl.classList.remove("ws-tree-droproot");
          div.classList.add("ws-drop-ok");
          return;
        }
        if (canReorder && dirOf(src) === myDir) {
          e.preventDefault();
          e.stopPropagation();
          e.dataTransfer.dropEffect = "move";
          treeEl.classList.remove("ws-tree-droproot");
          const r = div.getBoundingClientRect();
          const before = (e.clientY - r.top) < r.height / 2;
          div.classList.toggle("ws-drop-before", before);
          div.classList.toggle("ws-drop-after", !before);
          return;
        }
        const g = dropGate(src, myDir);
        if (!g.ok) { state.dragRejected = rejectReason(g); return; }
        state.dragRejected = null;
        e.preventDefault();
        e.stopPropagation();
        e.dataTransfer.dropEffect = "move";
        treeEl.classList.remove("ws-tree-droproot");
        div.classList.add("ws-drop-ok");
      };
      div.ondragleave = () => div.classList.remove("ws-drop-ok", "ws-drop-before", "ws-drop-after");
      div.ondrop = e => {
        if (externalFileDrag(e)) {
          e.preventDefault();
          e.stopPropagation();
          clearDropMarks();
          const dt = e.dataTransfer;
          collectDroppedFiles(dt).then(entries => {
            if (entries.length) return uploadEntries(entries, dirOf(path), { noPrompt: true });
            toast("Keine Dateien zum Hochladen erkannt.", "warning");
          }).catch(err => {
            console.error(err);
            toast("Upload fehlgeschlagen: " + (err && err.message ? err.message : err), "error");
          });
          return;
        }
        if (!state.dragPath) return;
        const multi = state.dragMulti;
        if (!multi && state.dragPath === path) return;
        e.preventDefault();
        e.stopPropagation();
        const src = state.dragPath;
        const dt = state.dragType;
        const myDir = dirOf(path);
        const r = div.getBoundingClientRect();
        const before = (e.clientY - r.top) < r.height / 2;
        state.dragPath = null;
        state.dragType = null;
        state.dragMulti = false;
        clearDropMarks();
        if (dt === "dir") {
          if (canReorder && dirOf(src) === myDir) reorderDirToEnd(myDir, src);
          else doMoveDir(src, myDir);
          return;
        }
        if (!multi && canReorder && dirOf(src) === myDir) reorderInDir(myDir, "files", src, path, before);
        else doMove(src, myDir, multi);
      };
      return div;
    }

    function dirRow(name, path, depth) {
      const div = document.createElement("div");
      const isInit = isInitDir(path);
      const acc = effectiveAccess(path);
      const accDef = ACCESS[acc] || ACCESS.edit;
      const open = !state.collapsed.has(path);
      // Jeder Ordner ist per Handle ziehbar (Reihenfolge = immer erlaubt;
      // Moves blockt der Drop-Gate mit Fehlermeldung, nicht der Handle).
      // [init]-Ordner (.init.sh-Ergebnisse) sind read-only → kein Drag.
      const dirDraggable = !isInit && !!(canMove || canReorder);
      div.className = "ws-row ws-row-dir px-1 py-1.5 flex items-center gap-1 text-gray-500 hover:bg-gray-100 rounded cursor-pointer";
      // Handle bleibt linksbündig; die Tiefe-Einrückung sitzt am Icon.
      div.style.paddingLeft = "2px";
      div.dataset.path = path;
      div.title = isInit
        ? ".init.sh-Ergebnis — read-only (wird per neuem Init-Build neu erzeugt)"
        : (acc !== "edit" ? accDef.label : "");
      div.innerHTML =
        '<span class="ws-drag' + (dirDraggable ? "" : " ws-drag-off") + '"' +
        (dirDraggable ? ' title="Ziehen: Ordner verschieben / sortieren"' : "") + ">⠿</span>" +
        '<span class="shrink-0"' + (depth ? ' style="margin-left:' + (depth * 14) + 'px"' : "") + '>' + (open ? "📂" : "📁") + "</span>" +
        (isInit
          ? '<span title=".init.sh-Ergebnis — read-only">📦</span>'
          : '<span title="' + esc(accDef.label) + '">' + accDef.icon + "</span>") +
        '<span class="truncate flex-1">' + esc(name) + "/</span>";
      div.onclick = () => {
        clearSelection();
        if (state.collapsed.has(path)) state.collapsed.delete(path);
        else state.collapsed.add(path);
        renderTree();
      };
      div.oncontextmenu = e => showDirMenu(e, path);
      if (dirDraggable) {
        const handle = div.querySelector(".ws-drag");
        handle.draggable = true;
        handle.ondragstart = e => {
          state.dragPath = path;
          state.dragType = "dir";
          state.dragRejected = null;
          e.dataTransfer.effectAllowed = "move";
          try { e.dataTransfer.setData("text/plain", path); } catch (err) { /* IE */ }
          e.stopPropagation();
        };
        handle.ondragend = () => {
          const rejected = state.dragRejected;
          state.dragPath = null;
          state.dragType = null;
          state.dragRejected = null;
          clearDropMarks();
          if (rejected) toast(rejected, "warning");
        };
      }
      // Drop-Target (Ordner) — für Datei- UND Ordner-Drags
      div.ondragover = e => {
        if (externalFileDrag(e)) {
          // Externer File-Drag: Upload direkt in diesen Ordner.
          if (!uploadGate(path)) return;
          e.preventDefault();
          e.stopPropagation();
          e.dataTransfer.dropEffect = "copy";
          treeEl.classList.remove("ws-tree-droproot");
          div.classList.add("ws-drop-ok");
          return;
        }
        if (!state.dragPath) return;
        const src = state.dragPath;
        if (src === path) return;
        // Ordner-Drag im selben Ordner: neu ordnen (halbe Zeile =
        // davor/danach) — rein Anzeige, ohne Move-Gates.
        if (state.dragType === "dir" && canReorder && dirOf(src) === dirOf(path)) {
          e.preventDefault();
          e.stopPropagation();
          e.dataTransfer.dropEffect = "move";
          treeEl.classList.remove("ws-tree-droproot");
          const r = div.getBoundingClientRect();
          const before = (e.clientY - r.top) < r.height / 2;
          div.classList.toggle("ws-drop-before", before);
          div.classList.toggle("ws-drop-after", !before);
          return;
        }
        const gate = state.dragType === "dir" ? dropGateDir(src, path)
                    : (state.dragMulti ? dropGateMulti(path) : dropGate(src, path));
        if (!gate.ok) { state.dragRejected = rejectReason(gate); return; }
        state.dragRejected = null;
        e.preventDefault();
        e.stopPropagation();
        e.dataTransfer.dropEffect = "move";
        treeEl.classList.remove("ws-tree-droproot");
        div.classList.add("ws-drop-ok");
      };
      div.ondragleave = () => div.classList.remove("ws-drop-ok", "ws-drop-before", "ws-drop-after");
      div.ondrop = e => {
        e.preventDefault();
        e.stopPropagation();
        if (externalFileDrag(e)) {
          clearDropMarks();
          const dtx = e.dataTransfer;
          collectDroppedFiles(dtx).then(entries => {
            if (entries.length) return uploadEntries(entries, path, { noPrompt: true });
            else toast("Keine Dateien zum Hochladen erkannt.", "warning");
          }).catch(err => {
            console.error(err);
            toast("Upload fehlgeschlagen: " + (err && err.message ? err.message : err), "error");
          });
          return;
        }
        const src = state.dragPath;
        const dt = state.dragType;
        const multi = state.dragMulti;
        state.dragPath = null;
        state.dragType = null;
        state.dragMulti = false;
        clearDropMarks();
        if (!src || src === path) return;
        if (dt === "dir") {
          if (canReorder && dirOf(src) === dirOf(path)) {
            const r = div.getBoundingClientRect();
            const before = (e.clientY - r.top) < r.height / 2;
            reorderInDir(dirOf(path), "dirs", src, path, before);
          } else {
            doMoveDir(src, path);
          }
          return;
        }
        doMove(src, path, multi);
      };
      return div;
    }

    function clearDropMarks() {
      treeEl.querySelectorAll(".ws-drop-ok,.ws-drop-before,.ws-drop-after")
        .forEach(el => el.classList.remove("ws-drop-ok", "ws-drop-before", "ws-drop-after"));
      treeEl.classList.remove("ws-tree-droproot");
    }

    // Wurzel des Baums = Drop-Target für „nach Wurzel verschieben“.
    // Greift überall dort, wo keine Zeile den Drop selbst übernommen hat
    // (leerer Bereich, Zeilen ohne gültiges Ziel) — so lässt sich auch
    // zuverlässig aus Ordner in die Wurzel ziehen.
    treeEl.ondragover = e => {
      if (externalFileDrag(e)) {
        // Upload in die Wurzel (leerer Bereich). Über einer Zeile
        // entscheidet die Zeile selbst: akzeptiert → stopPropagation,
        // abgelehnt (read-only) → kein preventDefault → kein Drop.
        if (e.target && e.target.closest && e.target.closest(".ws-row")) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
        treeEl.classList.add("ws-tree-droproot");
        return;
      }
      if (!state.dragPath) return;
      const src = state.dragPath;
      const gate = state.dragType === "dir" ? dropGateDir(src, "")
                  : (state.dragMulti ? dropGateMulti("") : dropGate(src, ""));
      if (!gate.ok) {
        // Zeilen-Targets haben den Gate-Check schon gemacht (bubbling): ihren
        // (spezifischeren) Fehlertext behalten, falls die Wurzel kein Ziel ist.
        state.dragRejected = rejectReason(gate) || state.dragRejected;
        return;
      }
      state.dragRejected = null;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      treeEl.classList.add("ws-tree-droproot");
    };
    treeEl.ondragleave = e => {
      if (!treeEl.contains(e.relatedTarget)) treeEl.classList.remove("ws-tree-droproot");
    };
    treeEl.ondrop = e => {
      e.preventDefault();
      if (externalFileDrag(e)) {
        clearDropMarks();
        const dtx = e.dataTransfer;
        collectDroppedFiles(dtx).then(entries => {
          if (entries.length) return uploadEntries(entries, "", { noPrompt: true });
          toast("Keine Dateien zum Hochladen erkannt.", "warning");
        }).catch(err => {
          console.error(err);
          toast("Upload fehlgeschlagen: " + (err && err.message ? err.message : err), "error");
        });
        return;
      }
      const src = state.dragPath;
      const dt = state.dragType;
      const multi = state.dragMulti;
      state.dragPath = null;
      state.dragType = null;
      state.dragMulti = false;
      clearDropMarks();
      if (!src) return;
      if (dt === "dir") doMoveDir(src, "");
      else doMove(src, "", multi);
    };

    // Leerer Baum-Bereich anklicken bzw. Escape: Auswahl aufheben.
    treeEl.onclick = e => {
      if (e.target !== treeEl || !state.selected.size) return;
      clearSelection();
      renderTree();
    };
    document.addEventListener("keydown", e => {
      if (e.key !== "Escape" || !state.selected.size || ctxMenu) return;
      const t = e.target;
      if (t && (editorEl.contains(t) || t.isContentEditable ||
                /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || ""))) return;
      clearSelection();
      renderTree();
    });

    function renderTree() {
      const top = treeEl.scrollTop;
      treeEl.innerHTML = "";
      if (!apiBase) {
        treeEl.innerHTML = '<div class="text-gray-400 text-xs p-1">' +
          esc(noTaskMsg || emptyMsg) + "</div>";
        treeEl.scrollTop = top;
        return;
      }
      if (!state.files.length && !state.extraDirs.size && !state.folders.length) {
        treeEl.innerHTML = '<div class="text-gray-400 text-xs p-1">' + esc(emptyMsg) + "</div>";
        treeEl.scrollTop = top;
        return;
      }
      const root = buildTree();
      state.visibleFiles = [];
      const frag = document.createDocumentFragment();
      const walk = (node, parentPath, depth) => {
        const dirs = [];
        const filesList = [];
        Object.keys(node).forEach(k => {
          const child = node[k];
          const childPath = parentPath ? parentPath + "/" + k : k;
          if (child.file) filesList.push({ name: k, meta: child.file, path: childPath });
          else dirs.push({ name: k, node: child, path: childPath });
        });
        // Ordner zuerst, dann Dateien — jede Gruppe in der gepflegten
        // Reihenfolge (lokal > sort_order > alphabetisch).
        dirs.sort(cmpGroup(parentPath, "dirs", (a, b) => {
          const oa = state.folderOrder[a.path], ob = state.folderOrder[b.path];
          const va = oa == null ? Number.MAX_SAFE_INTEGER : oa;
          const vb = ob == null ? Number.MAX_SAFE_INTEGER : ob;
          if (va !== vb) return va - vb;
          return a.name.localeCompare(b.name);
        }));
        filesList.sort(cmpGroup(parentPath, "files", (a, b) => cmpFileOrder(a.meta, b.meta)));
        dirs.forEach(d => {
          // 👤-Ordner: für Nicht-Tutoren (keine canSetAccess) unsichtbar.
          if (effectiveAccess(d.path) === "hidden" && !canSetAccess) return;
          frag.appendChild(dirRow(d.name, d.path, depth));
          if (!state.collapsed.has(d.path)) walk(d.node, d.path, depth + 1);
        });
        filesList.forEach(f => {
          if (effectiveAccess(f.path) === "hidden" && !canSetAccess) return;
          state.visibleFiles.push(f.path);  // Reihenfolge für Shift-Bereich
          frag.appendChild(fileRow(f.name, f.meta, f.path, depth));
        });
      };
      walk(root, "", 0);
      treeEl.appendChild(frag);
      treeEl.scrollTop = top;
    }

    // ── Kontextmenüs ──────────────────────────────────────────────
    // Ziel-Ordner für „Verschieben nach:“ (Kontextmenü; Touch-Fallback für
    // Drag & Drop). isDir: src ist ein Ordner — das Gate prüft über den
    // Drop-Gate (inkl. Zugriffs-Klassen-Check der Subtree-Dateien).
    function moveDestDirs(src, isDir) {
      const out = [];
      const name = src.split("/").pop();
      const srcParent = src.includes("/") ? src.slice(0, src.lastIndexOf("/")) : "";
      const seen = new Set();
      const gateDir = (dir) => {
        if (isDir) return dropGateDir(src, dir);
        const dst = dir ? dir + "/" + name : name;
        return moveGate(src, dst);
      };
      const add = (label, dir) => {
        if (dir === srcParent || seen.has(dir)) return;
        const gate = gateDir(dir);
        if (!gate.ok) return;
        seen.add(dir);
        out.push({ label: label, path: dir });
      };
      add("(Wurzel)", "");
      allDirPaths().forEach(d => {
        if (d === src || d.startsWith(src + "/")) return; // nicht in/unter sich selbst
        add(d + "/", d);
      });
      return out;
    }

    // Ziel-Ordner für Bulk-Moves: nur Ziele, in die ALLE gewählten Dateien
    // dürfen und in denen mindestens eine noch nicht liegt.
    function moveDestDirsBulk() {
      const out = [];
      const seen = new Set();
      const add = (label, dir) => {
        if (seen.has(dir)) return;
        if (!dropGateMulti(dir).ok) return;
        if (!bulkTargets().some(p => dropGate(p, dir).ok)) return;
        seen.add(dir);
        out.push({ label: label, path: dir });
      };
      add("(Wurzel)", "");
      allDirPaths().forEach(d => add(d + "/", d));
      return out;
    }

    function showFileMenu(e, path) {
      e.preventDefault();
      e.stopPropagation();
      if (state.selected.size > 1 && state.selected.has(path)) {
        showBulkFileMenu(e);
        return;
      }
      if (state.selected.size > 1) {
        // Rechtsklick außerhalb der Auswahl → Auswahl auf diese Datei.
        state.selected = new Set([path]);
        state.selAnchor = path;
        renderTree();
      }
      const f = state.files.find(x => x.path === path);
      const isInit = !!(f && f.init);
      const items = [];
      items.push({ label: "📂 Öffnen", fn: () => openFile(path) });
      if (isInit) {
        // .init.sh-Ergebnis: read-only, nichts weiter zu verwalten.
        showCtxMenu(e.clientX, e.clientY, items);
        return;
      }
      if (canSetAccess) {
        items.push({ sep: true });
        const fixed = systemAccessOf(path);
        if (fixed) {
          // System-Skript: feste Klasse, keine Auswahl-Items.
          items.push({ label: "🔐 Zugriff: " + accessLabel(fixed) +
                            " (System-Skript — festgelegt)", header: true });
        } else {
          const own = f ? f.file_access : null;
          items.push({ label: "🔐 Zugriff (aktuell: " + accessLabel(effectiveAccess(path)) + ")", header: true });
          items.push({ label: (own == null ? "✓ " : "✏️ ") + "Editierbar",
                      fn: () => setAccess(path, false, null) });
          items.push({ label: (own === "readonly" ? "✓ " : "🔒 ") + "Read-only",
                      fn: () => setAccess(path, false, "readonly") });
          items.push({ label: (own === "hidden" ? "✓ " : "👤 ") + "Versteckt",
                      fn: () => setAccess(path, false, "hidden") });
        }
      }
      if (canMove && moveGate(path, path).ok) {
        items.push({ label: "✏️ Umbenennen …", fn: () => renameFile(path) });
        const dirs = moveDestDirs(path);
        if (dirs.length) {
          items.push({ sep: true });
          items.push({ label: "Verschieben nach:", header: true });
          dirs.forEach(d => items.push({
            label: "→ " + d.label,
            fn: () => {
              const dst = d.path ? d.path + "/" + path.split("/").pop() : path.split("/").pop();
              moveFile(path, dst);
            },
          }));
        }
      }
      if (allowMain && effectiveAccess(path) === "edit") {
        if (state.mainFile !== path) {
          items.push({ label: "⭐ Als Main-Datei setzen", fn: () => setMainFile(path) });
        }
      }
      if (canDelete && !readOnlyCheck(path)) {
        items.push({ sep: true });
        items.push({ label: "🗑 Löschen", danger: true, fn: () => deleteFile(path) });
      }
      if (items.length) showCtxMenu(e.clientX, e.clientY, items);
    }

    // Kontextmenü für eine Mehrfachauswahl (Bulk-Verschieben/-Löschen).
    function showBulkFileMenu(e) {
      const sel = [...state.selected];
      const items = [{ label: sel.length + " Dateien ausgewählt", header: true }];
      if (canMove) {
        const dirs = moveDestDirsBulk();
        if (dirs.length) {
          items.push({ sep: true });
          items.push({ label: "Verschieben nach:", header: true });
          dirs.forEach(d => items.push({
            label: "→ " + d.label,
            fn: () => moveSelectedTo(d.path),
          }));
        }
      }
      if (canDelete) {
        const deletable = bulkTargets().filter(p => !readOnlyCheck(p));
        if (deletable.length) {
          items.push({ sep: true });
          const skipped = sel.length - deletable.length;
          items.push({
            label: "🗑 " + deletable.length +
                   (skipped ? " von " + sel.length : "") + " Datei(en) löschen",
            danger: true,
            fn: () => deleteSelected(deletable, skipped),
          });
        }
      }
      showCtxMenu(e.clientX, e.clientY, items);
    }

    function showDirMenu(e, dirPath) {
      e.preventDefault();
      e.stopPropagation();
      if (isInitDir(dirPath)) {
        // .init.sh-Ergebnis: read-only, nichts weiter zu verwalten.
        showCtxMenu(e.clientX, e.clientY, [
          { label: "📦 .init.sh-Ergebnis — read-only", header: true }]);
        return;
      }
      const items = [];
      // Anlegen nur in editierbaren Bereichen (Tutor: überall — sein
      // readOnlyCheck deckt nur Init-Pfade ab).
      if (canCreate && (canSetAccess || effectiveAccess(dirPath) === "edit")) {
        items.push({ label: "＋ Neue Datei …", fn: () => newFileIn(dirPath) });
        items.push({ label: "＋ Neuer Ordner …", fn: () => newFolderIn(dirPath) });
      }
      if (canUpload && uploadGate(dirPath)) {
        if (items.length) items.push({ sep: true });
        items.push({ label: "📤 Dateien hochladen …", fn: () => uploadTo(dirPath, "files") });
        items.push({ label: "📤 Ordner hochladen …", fn: () => uploadTo(dirPath, "folders") });
      }
      if (canSetAccess) {
        if (items.length) items.push({ sep: true });
        const own = state.folderMap[dirPath] || null;
        items.push({ label: "🔐 Zugriff (aktuell: " + accessLabel(effectiveAccess(dirPath)) + ")", header: true });
        items.push({ label: (own == null ? "✓ " : "✏️ ") + "Editierbar",
                    fn: () => setAccess(dirPath, true, null) });
        items.push({ label: (own === "readonly" ? "✓ " : "🔒 ") + "Read-only",
                    fn: () => setAccess(dirPath, true, "readonly") });
        items.push({ label: (own === "hidden" ? "✓ " : "👤 ") + "Versteckt",
                    fn: () => setAccess(dirPath, true, "hidden") });
      }
      if (canMove && canDragDir(dirPath)) {
        const dirs = moveDestDirs(dirPath, true);
        if (dirs.length) {
          if (items.length) items.push({ sep: true });
          items.push({ label: "Verschieben nach:", header: true });
          dirs.forEach(d => items.push({
            label: "→ " + d.label,
            fn: () => doMoveDir(dirPath, d.path),
          }));
        }
      }
      if (allowBulk && dirOpGate(dirPath).ok) {
        if (items.length) items.push({ sep: true });
        items.push({ label: "✏️ Ordner umbenennen …", fn: () => renameDir(dirPath) });
        items.push({ label: "🗑 Ordner löschen", danger: true, fn: () => deleteDir(dirPath) });
      }
      if (items.length) showCtxMenu(e.clientX, e.clientY, items);
    }

    // Zugriffs-Klasse setzen (Datei: null = erben / Ordner: null = edit).
    async function setAccess(path, isFolder, access) {
      try {
        const res = await fetch(apiBase + "/access", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ path: path, is_folder: isFolder, access: access }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.status);
        await refresh();
        return true;
      } catch (err) {
        toast("Zugriffs-Klasse nicht geändert: " + err.message, "error");
        return false;
      }
    }

    // ── Datei-Operationen ─────────────────────────────────────────
    function validRelPath(p) {
      return !!p && !p.startsWith("/") && !p.split("/").includes("..");
    }

    // ── Baum-Änderungserkennung (onChanged) ─────────────────────
    // Signatur des Server-Baumzustands (Dateien + Ordner + Order).
    // Der erste Refresh setzt nur die Basis (ein bloßes Laden der Seite
    // zählt nicht als Änderung); jede echte Änderung (alle Datei-
    // Operationen enden in refresh()) feuert den Callback genau einmal.
    let lastTreeSig = null;
    function treeSignature() {
      const byPath = (a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0);
      return JSON.stringify([
        [...state.files].sort(byPath),
        [...state.folders].sort(byPath),
        state.folderOrder,
      ]);
    }

    async function refresh(openPath) {
      if (!apiBase) {
        renderTree();
        return;
      }
      try {
        // no-store: nach Moves/Deletes sonst die gecachte (alte) Liste
        // zurückkommen und Dateien scheinbar „dupliziert“ wirken.
        const res = await fetch(apiBase + "/files", { credentials: "same-origin", cache: "no-store" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.status);
        state.files = data.files || [];
        state.folders = data.folders || [];
        state.folderOrder = data.folder_order || {};
        state.folderMap = {};
        // Nur explizite Klassen in die Map (implizite on-disk-Ordner
        // haben own: null und sind per Default editierbar).
        state.folders.forEach(fd => { if (fd.own) state.folderMap[fd.path] = fd.own; });
        // Mehrfachauswahl auf noch existierende Dateien verjüngen.
        if (state.selected.size) {
          state.selected = new Set(
            [...state.selected].filter(p => state.files.some(f => f.path === p)));
          if (state.selAnchor && !state.files.some(f => f.path === state.selAnchor)) {
            state.selAnchor = null;
          }
        }
        renderTree();
        if (onFilesLoaded) onFilesLoaded(state.files);
        const sig = treeSignature();
        if (lastTreeSig !== null && sig !== lastTreeSig && onChanged) onChanged();
        lastTreeSig = sig;
        if (openPath) {
          await openFile(openPath);
          return;
        }
        // (Neu)öffnen nur, wenn die aktuell geöffnete Datei fehlt
        if (!state.currentFile || !state.files.some(f => f.path === state.currentFile)) {
          const main = state.mainFile || "";
          const target = (state.files.some(f => f.path === main) && main) ||
            (state.files[0] && state.files[0].path);
          if (target) await openFile(target);
        }
      } catch (err) {
        treeEl.innerHTML =
          '<div class="text-red-500 text-xs p-1">Dateien nicht ladbar: ' +
          esc(err.message) + "</div>";
      }
    }

    async function openFile(path) {
      if (!apiBase) return;
      if (path === state.currentFile) {
        // Datei neu fokussieren (z. B. Rückkehr aus Terminal/Preview):
        // Highlight wiederherstellen + View dorthin wechseln, ohne
        // Netzwerk-Request — sonst bliebe die andere View sichtbar.
        if (!state.fileFocused) { state.fileFocused = true; renderTree(); }
        if (onViewChanged) onViewChanged(state.currentViewKind);
        if (state.currentViewKind === "editor") cm.focus();
        return;
      }
      if (state.dirty) {
        // Auto-Save: aktuelle Datei beim Wechsel speichern (statt verwerfen).
        if (!await saveFile()) return;  // Speichern fehlgeschlagen → bleiben
      }
      try {
        const res = await fetch(apiBase + "/files/" + encPath(path), { credentials: "same-origin", cache: "no-store" });
        if (!res.ok) {
          let detail = res.status;
          try { detail = (await res.json()).detail || detail; } catch (err) { /* ignore */ }
          throw new Error(detail);
        }
        const ct = res.headers.get("Content-Type") || "";
        const kind = mediaKind(path, ct);
        if (kind && mediaEl) {
          await showMediaView(kind, path, res);
          return;
        }
        if (kind) {
          toast("Binärdatei — kann nicht im Text-Editor geöffnet werden.", "warning");
          return;
        }
        const text = await res.text();
        closeMediaView();
        state.currentFile = path;
        state.currentViewKind = "editor";
        state.fileFocused = true;
        state.dirty = false;
        if (state.saveTimer) { clearTimeout(state.saveTimer); state.saveTimer = null; }
        cm.setOption("readOnly", readOnlyCheck(path) ? "nocursor" : false);
        // setValue feuert ein "change"-Event → würde die Datei fälschlich
        // dirty markieren und den 3-sec-Autosave (PUT) starten.
        state.suppressChange = true;
        cm.setValue(text);
        state.suppressChange = false;
        // Immer Mode setzen (Fallback „text/plain"): sonst bliebe der Mode
        // der vorherigen Datei bei unbekannter Extension hängen.
        const mode = cmModeForPath(path);
        cm.setOption("mode", mode || "text/plain");
        setSaveState("");
        renderTree();
        if (onViewChanged) onViewChanged("editor");
      } catch (err) {
        toast("Datei nicht geöffnet: " + err.message, "error");
      }
    }

    // Liefert true, wenn gespeichert (oder nichts zu speichern) ist.
    async function saveFile() {
      if (state.saveTimer) { clearTimeout(state.saveTimer); state.saveTimer = null; }
      if (!state.currentFile || !state.dirty) return true;
      if (!apiBase) return false;
      if (readOnlyCheck(state.currentFile)) {
        toast("Diese Datei ist read-only (vom Tutor verwaltet).", "warning");
        return false;
      }
      setSaveState("⏳ Speichern …");
      try {
        const res = await fetch(apiBase + "/files/" + encPath(state.currentFile), {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ content: cm.getValue() }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.status);
        state.dirty = false;
        setSaveState("✓ gespeichert " + new Date().toLocaleTimeString());
        setTimeout(() => setSaveState(""), 3000);
        await refresh();
        return true;
      } catch (err) {
        setSaveState("");
        toast("Speichern fehlgeschlagen: " + err.message, "error");
        return false;
      }
    }

    async function createFile(path) {
      path = String(path || "").trim();
      if (!validRelPath(path)) {
        toast("Ungültiger Pfad (relativ, ohne „..“).", "error");
        return false;
      }
      if (readOnlyCheck(path)) {
        toast("Dieser Bereich ist read-only — dort kann keine Datei angelegt werden.", "warning");
        return false;
      }
      if (state.files.some(f => f.path === path)) {
        toast("Datei existiert bereits.", "error");
        return false;
      }
      if (state.folders.some(fd => fd.path === path)) {
        toast("Ordner existiert bereits.", "error");
        return false;
      }
      try {
        const res = await fetch(apiBase + "/files", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ path: path, content: "" }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.status);
        await refresh(path);
        return true;
      } catch (err) {
        toast("Anlegen fehlgeschlagen: " + err.message, "error");
        return false;
      }
    }

    async function deleteFile(path) {
      if (!confirm("„" + path + "“ wirklich löschen?")) return false;
      return deleteFileQuiet(path);
    }

    async function deleteFileQuiet(path, doRefresh = true) {
      try {
        // Vor dem DELETE den Autosave abbrechen — sonst erreicht ein
        // 3-sec-altes PUT den Server nach dem DELETE und legt die Datei
        // direkt wieder an (daher „mehrfach löschen“).
        if (state.currentFile === path) {
          if (state.saveTimer) { clearTimeout(state.saveTimer); state.saveTimer = null; }
          state.dirty = false;
        }
        const res = await fetch(apiBase + "/files/" + encPath(path), {
          method: "DELETE", credentials: "same-origin",
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.status);
        if (state.currentFile === path) clearEditorState();
        if (state.mainFile === path) setMainFileState("");
        if (doRefresh) await refresh();  // deleteDir aktualisiert am Ende selbst
        return true;
      } catch (err) {
        toast("Löschen fehlgeschlagen: " + err.message, "error");
        return false;
      }
    }

    // Mehrfachauswahl: nacheinander in dstDir verschieben (Dateien, die
    // bereits dort liegen, und read-only Init-Dateien werden übersprungen).
    async function moveSelectedTo(dstDir) {
      const toMove = [];
      for (const s of bulkTargets()) {
        const g = dropGate(s, dstDir);
        if (g.ok) { toMove.push(s); continue; }
        if (g.reason === "Bereits dort." ||
            g.reason === "Datei liegt bereits in diesem Ordner.") continue;
        toast(g.reason || "Nicht möglich.", "warning");
        return;
      }
      if (!toMove.length) return;
      for (const s of toMove) {
        const n = s.split("/").pop();
        if (!await moveFile(s, dstDir ? dstDir + "/" + n : n)) return;
      }
      clearSelection();
      renderTree();
    }

    async function deleteSelected(paths, skipped) {
      const lines = paths.slice(0, 10).map(p => "  • " + p).join("\n");
      const more = paths.length > 10 ? "\n  … (" + (paths.length - 10) + " weitere)" : "";
      const extra = skipped ? "\n" + skipped + " read-only/Init-Datei(en) werden übersprungen." : "";
      if (!confirm(paths.length + " Datei(en) wirklich löschen?\n" + lines + more + extra)) return;
      let ok = 0;
      for (const p of paths) {
        if (await deleteFileQuiet(p, false)) ok++;
      }
      clearSelection();
      await refresh();
      if (ok < paths.length) toast("Nur " + ok + " von " + paths.length + " Dateien gelöscht.", "warning");
    }

    async function moveFile(src, dst) {
      try {
        const res = await fetch(apiBase + "/files/move", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ src: src, dst: dst }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.status);
        if (state.currentFile === src) state.currentFile = dst;
        if (state.mainFile === src) setMainFileState(dst);
        await refresh();
        return true;
      } catch (err) {
        toast("Verschieben fehlgeschlagen: " + err.message, "error");
        return false;
      }
    }

    async function uploadFile(file, path) {
      path = String(path || "").trim();
      if (state.files.some(f => f.path === path)) {
        toast("Datei existiert bereits.", "error");
        return false;
      }
      if (state.folders.some(fd => fd.path === path)) {
        toast("Ordner existiert bereits.", "error");
        return false;
      }
      try {
        const fd = new FormData();
        fd.append("file", file);
        fd.append("path", String(path || "").trim());
        const res = await fetch(apiBase + "/upload", {
          method: "POST", credentials: "same-origin", body: fd,
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || res.status);
        toast("Upload fertig — Sync läuft im Hintergrund.", "success");
        await refresh();
        return true;
      } catch (err) {
        toast("Upload fehlgeschlagen: " + err.message, "error");
        return false;
      }
    }

    // ── Upload: mehrere Dateien / komplette Ordner ────────────────
    // Einträge: [{file: File, relPath: string}] — relPath trägt die
    // relative Ordner-Struktur (webkitRelativePath vom <input> bzw.
    // fullPath des Drag-Entry); bei einer einfachen Mehrfachauswahl
    // ist relPath nur der Dateiname. Zielpfad = Zielordner + relPath.
    const uploadInputs = {};
    function uploadPickerInput(kind) {
      if (!uploadInputs[kind]) {
        const inp = document.createElement("input");
        inp.type = "file";
        inp.className = "hidden";
        if (kind === "folders") inp.setAttribute("webkitdirectory", "");
        else inp.multiple = true;
        document.body.appendChild(inp);
        uploadInputs[kind] = inp;
      }
      return uploadInputs[kind];
    }

    function entriesFromFileList(list) {
      const out = [];
      Array.from(list || []).forEach(f => out.push({
        file: f,
        relPath: f.webkitRelativePath || f.name,
      }));
      return out;
    }

    // Drag & Drop: Ordner-Struktur per FileSystemEntry-API rekursiv
    // sammeln (readEntries liefert max. 100 Einträge pro Batch).
    function collectEntry(entry, out) {
      if (entry.isFile) {
        return new Promise(resolve => {
          entry.file(f => {
            out.push({ file: f, relPath: entry.fullPath.replace(/^\/+/, "") || f.name });
            resolve();
          }, () => resolve());
        });
      }
      if (entry.isDirectory) {
        const kids = [];
        return new Promise(resolve => {
          const reader = entry.createReader();
          const readBatch = () => reader.readEntries(bs => {
            kids.push.apply(kids, bs);
            if (bs.length) readBatch();
            else resolve();
          }, () => resolve());
          readBatch();
        }).then(() => Promise.all(kids.map(k => collectEntry(k, out))));
      }
      return Promise.resolve();
    }

    function collectDroppedFiles(dataTransfer) {
      let items = [];
      try { items.push.apply(items, dataTransfer.items || []); } catch (err) { /* ignore */ }
      items = items.filter(it => it && it.kind === "file");
      if (!items.length) {
        // Fallback ohne items-API: webkitRelativePath (Chromium/Firefox)
        const files = dataTransfer.files || [];
        return Promise.resolve(Array.from(files).map(f => ({
          file: f, relPath: f.webkitRelativePath || f.name,
        })));
      }
      const out = [];
      const jobs = items.map(it => {
        let entry = null;
        try { entry = it.webkitGetAsEntry ? it.webkitGetAsEntry() : null; }
        catch (err) { entry = null; }
        if (!entry) {
          // Kein lesbares Entry (defekter Drag-Eintrag) → flache Datei.
          const f = it.getAsFile ? it.getAsFile() : null;
          if (f) out.push({ file: f, relPath: f.name });
          return Promise.resolve();
        }
        return collectEntry(entry, out);
      });
      return Promise.all(jobs).then(() => out);
    }

    // Externer Drag (Dateien/Ordner aus dem OS) statt interner Move:
    // interne Drags tragen text/plain + state.dragPath, externe „Files“.
    function externalFileDrag(e) {
      if (state.dragPath || !e.dataTransfer) return false;
      const types = e.dataTransfer.types;
      if (!types) return false;
      try {
        for (let i = 0; i < types.length; i++) {
          if (types[i] === "Files") return true;
        }
      } catch (err) { /* ignore */ }
      return false;
    }

    // Upload-Ziel-Gate: read-only/versteckte Bereiche + Tutor-Init-Pfade
    // nicht; Tutoren (canSetAccess) dürfen sonst überall (Zugriffs-
    // Klassen bestimmen nur die Studenten-Sicht).
    function uploadGate(dir) {
      if (readOnlyCheck(dir)) return false;
      return canSetAccess || effectiveAccess(dir) === "edit";
    }

    async function uploadEntries(entries, targetDir, opts) {
      if (!apiBase) {
        toast(noTaskMsg || "Upload derzeit nicht verfügbar.", "error");
        return;
      }
      if (!entries || !entries.length) return;
      opts = opts || {};
      const hasStructure = entries.some(e => e.relPath && e.relPath !== e.file.name);
      let gateDir, paths;
      if (entries.length === 1 && !hasStructure && !opts.noPrompt) {
        // Einzeldatei ohne Struktur: genauen Zielpfad prompten
        // (etabliertes Single-File-UX, Vorschlag „data/train.csv“).
        const f = entries[0].file;
        const def = (targetDir ? targetDir : "data") + "/" + f.name;
        const p = prompt("Zielpfad für den Upload (Vorschlag: " + def + ")", def);
        if (p == null) return;
        const path = p.trim();
        if (!path) return;
        if (!validRelPath(path)) {
          toast("Ungültiger Pfad (relativ, kein „..“).", "error");
          return;
        }
        paths = [path];
        gateDir = dirOf(path);
      } else {
        // Mehrere Dateien oder Ordner-Struktur: ZielORDNER (vorgegeben
        // per Kontextmenü/Drag & Drop, sonst prompten; leer = Wurzel).
        let dir = String(targetDir || "").trim().replace(/\/+$/, "");
        if (dir === "" && !opts.noPrompt) {
          const p = prompt("Zielordner für den Upload (leer = Wurzel):", "");
          if (p == null) return;
          dir = p.trim().replace(/\/+$/, "");
        }
        if (dir && !validRelPath(dir)) {
          toast("Ungültiger Ordnerpfad (relativ, kein „..“).", "error");
          return;
        }
        gateDir = dir;
        paths = entries.map(e => {
          const rel = (e.relPath && e.relPath !== e.file.name) ? e.relPath : e.file.name;
          return dir ? dir + "/" + rel : rel;
        });
      }
      if (!uploadGate(gateDir || "")) {
        toast("Dieser Bereich ist read-only — dort kann nichts hochgeladen werden.", "warning");
        return;
      }
      // Konflikte vorab erkennen: existierende Datei/Ordner am Zielpfad
      // werden übersprungen statt überschrieben (konsistent bei Tutor
      // UND Student — Überschreiben geht bewusst über „Löschen + neu“).
      const isConflict = p => state.files.some(f => f.path === p) ||
                              state.folders.some(f => f.path === p);
      const skipped = new Set(paths.filter(isConflict));
      if (skipped.size) {
        const list = [...skipped].slice(0, 5).map(x => "  • " + x).join("\n") +
          (skipped.size > 5 ? "\n  …" : "");
        if (!confirm(skipped.size +
          (skipped.size === 1 ? " Datei existiert bereits" : " Dateien existieren bereits") +
          " und werden übersprungen:\n" + list + "\n\nDen Rest jetzt hochladen?")) {
          return;
        }
      }
      let ok = 0;
      const errors = [];
      for (let i = 0; i < paths.length; i++) {
        const path = paths[i];
        if (skipped.has(path)) continue;
        try {
          const fd = new FormData();
          fd.append("file", entries[i].file);
          fd.append("path", path);
          const res = await fetch(apiBase + "/upload", {
            method: "POST", credentials: "same-origin", body: fd,
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(data.detail || res.status);
          ok++;
        } catch (err) {
          errors.push(path + " — " + err.message);
        }
      }
      await refresh();
      const total = paths.length - skipped.size;
      const skipMsg = skipped.size
        ? ", " + skipped.size + " übersprungen (existieren bereits)" : "";
      if (errors.length) {
        toast("Upload: " + ok + " von " + total + " Datei(en) geladen" + skipMsg +
              " — Fehler: " + errors[0] +
              (errors.length > 1 ? " (\u002b" + (errors.length - 1) + " weitere)" : ""), "error");
      } else {
        toast("Upload fertig — " + ok + " Datei(en) geladen" + skipMsg + ".", "success");
      }
    }

    // Picker starten: kind = "files" (mehrere Dateien) | "folders"
    // (kompletter Ordner mit Struktur, webkitdirectory).
    function uploadTo(targetDir, kind) {
      if (!apiBase) {
        toast(noTaskMsg || "Erst die Aufgabe speichern.", "warning");
        return;
      }
      const inp = uploadPickerInput(kind);
      inp.onchange = () => {
        const entries = entriesFromFileList(inp.files);
        inp.value = "";
        if (entries.length) uploadEntries(entries, targetDir || "");
      };
      inp.click();
    }

    // Drop-down für den Upload-Toolbar-Button: Dateien | Ordner.
    // Der Browser-Picker kann beides nicht in einem Dialog
    // (multiple vs. webkitdirectory), daher zwei Einträge.
    function uploadMenu(x, y, targetDir) {
      showCtxMenu(x, y, [
        { label: "📄 Dateien hochladen …", fn: () => uploadTo(targetDir, "files") },
        { label: "📁 Ordner hochladen … (inkl. Struktur)",
          fn: () => uploadTo(targetDir, "folders") },
      ]);
    }

    // ── Main-Datei ────────────────────────────────────────────────
    function setMainFileState(p) {
      state.mainFile = String(p || "");
    }
    function setMainFile(p) {
      p = String(p || "").trim();
      if (p && !validRelPath(p)) {
        toast("Ungültiger Pfad für die Main-Datei.", "error");
        return;
      }
      setMainFileState(p);
      renderTree();
      if (onMainFileChange) onMainFileChange(p);
      // Main-Datei ist nicht Teil des /files-Zustands → die Baum-Signatur
      // bemerkt den Wechsel nicht; explizit melden (das Feld selbst
      // wird über das Task-Payload gespeichert).
      if (onChanged) onChanged();
    }
    function getMainFile() {
      return state.mainFile;
    }

    // ── Editor-Reset / Medien ─────────────────────────────────────
    function clearEditorState() {
      state.currentFile = null;
      state.currentViewKind = "editor";
      state.dirty = false;
      if (state.saveTimer) { clearTimeout(state.saveTimer); state.saveTimer = null; }
      closeMediaView();
      cm.setOption("readOnly", false);
      state.suppressChange = true;
      cm.setValue("");
      state.suppressChange = false;
      setSaveState("");
      renderTree();
      if (onViewChanged) onViewChanged("editor");
    }

    // ── Versionen-Restore ───────────────────────────────────────────
    // Der Server-Zustand wurde gerade durch eine Version ersetzt →
    // Dateibereich vollständig neu laden: ungespeicherten Editor-Stand
    // verwerfen (sonst würde er beim nächsten Datei-Wechsel per Auto-Save
    // auf den wiederhergestellten Stand zurückschreiben) und die Datei
    // erneut öffnen (die vorherige, falls sie noch existiert, sonst
    // Main- bzw. erste Datei — refresh() öffnet sie selbst).
    async function restoreReload() {
      if (!apiBase) return;
      const keep = state.currentFile;
      clearEditorState();
      await refresh();
      if (keep && state.files.some(f => f.path === keep)) {
        await openFile(keep);
      }
    }

    async function showMediaView(kind, path, res) {
      const blob = await res.blob();
      if (state.mediaUrl) URL.revokeObjectURL(state.mediaUrl);
      const url = URL.createObjectURL(blob);
      state.mediaUrl = url;
      state.currentFile = path;
      state.currentViewKind = "media";
      state.fileFocused = true;
      state.dirty = false;
      if (state.saveTimer) { clearTimeout(state.saveTimer); state.saveTimer = null; }
      setSaveState("");
      const name = path.split("/").pop();
      cm.getWrapperElement().style.display = "none";
      let mediaHtml, label;
      if (kind === "image") {
        label = "🖼 Bild";
        mediaHtml = '<img src="' + url + '" alt="' + esc(name) + '" class="max-w-full h-auto rounded shadow-sm">';
      } else if (kind === "video") {
        label = "🎬 Video";
        mediaHtml = '<video src="' + url + '" controls class="max-w-full rounded shadow-sm"></video>';
      } else if (kind === "audio") {
        label = "🎧 Audio";
        mediaHtml = '<div class="w-full bg-white border border-gray-200 rounded-lg p-4"><audio src="' + url + '" controls class="w-full"></audio></div>';
      } else if (kind === "pdf") {
        label = "📄 PDF";
        mediaHtml = '<iframe src="' + url + '" title="' + esc(name) + '" class="w-full h-full bg-white rounded shadow-sm"></iframe>';
      } else {
        label = "📦 Binärdatei";
        mediaHtml = '<div class="text-center text-gray-500 text-sm bg-white border border-gray-200 rounded-lg p-6">' +
          "Diese Datei kann nicht als Vorschau angezeigt werden." +
          '<div class="mt-3"><a href="' + url + '" download="' + esc(name) + '" ' +
          'class="text-blue-700 hover:text-blue-900 border border-blue-300 hover:border-blue-500 px-3 py-2 rounded-lg transition text-sm inline-block">⬇️ Datei herunterladen</a></div></div>';
      }
      mediaEl.classList.remove("hidden");
      mediaEl.innerHTML =
        '<div class="h-full overflow-auto p-3 flex flex-col gap-2">' +
        '<div class="flex items-center gap-2 text-xs text-gray-500">' +
        "<span>" + label + "</span>" +
        '<span class="font-mono truncate">' + esc(path) + "</span>" +
        (kind !== "binary"
          ? '<a href="' + url + '" download="' + esc(name) + '" ' +
            'class="ml-auto text-blue-600 hover:underline shrink-0">⬇️ Download</a>' : "") +
        "</div>" +
        '<div class="flex-1 min-h-[200px] flex items-start justify-center bg-gray-100 rounded-lg p-2 overflow-auto">' +
        mediaHtml +
        "</div>" +
        "</div>";
      if (kind === "pdf") {
        const frame = mediaEl.querySelector("iframe");
        if (frame) frame.style.height = "500px";
      }
      renderTree();
      if (onViewChanged) onViewChanged("media");
    }

    function closeMediaView() {
      if (!mediaEl) {
        if (state.mediaUrl) { URL.revokeObjectURL(state.mediaUrl); state.mediaUrl = null; }
        if (onViewChanged) onViewChanged("editor");
        return;
      }
      if (!mediaEl.classList.contains("hidden")) {
        mediaEl.classList.add("hidden");
        mediaEl.innerHTML = "";
      }
      cm.getWrapperElement().style.display = "";
      if (state.mediaUrl) {
        URL.revokeObjectURL(state.mediaUrl);
        state.mediaUrl = null;
      }
      if (onViewChanged) onViewChanged("editor");
    }

    // ── Neue Datei / Ordner ───────────────────────────────────────
    function newFileIn(dir) {
      const prefix = dir ? dir + "/" : "";
      const name = prompt("Dateiname in " + (dir || "Wurzel") + ":", "neue_datei.py");
      if (!name || !name.trim()) return;
      const n = name.trim();
      if (n.includes("/") || n.includes("\\")) {
        toast("Nur ein Dateiname (keine Pfadtrenner) — für Unterverzeichnisse erst einen Ordner anlegen.", "error");
        return;
      }
      createFile(prefix + n);
    }

    async function newFolderIn(dir) {
      const prefix = dir ? dir + "/" : "";
      const name = prompt("Name des neuen Ordners in " + (dir || "Wurzel") + ":", "neuer_ordner");
      if (!name || !name.trim()) return;
      const n = name.trim().replace(/[/\\]/g, "");
      if (!n) return;
      const p = prefix + n;
      if (readOnlyCheck(p)) {
        toast("Dieser Bereich ist read-only — dort kann kein Ordner angelegt werden.", "warning");
        return;
      }
      if (state.files.some(f => f.path === p)) {
        toast("Datei existiert bereits.", "error");
        return;
      }
      if (state.folders.some(fd => fd.path === p) || state.extraDirs.has(p)) {
        toast("Ordner existiert bereits.", "error");
        return;
      }
      if (state.files.some(f => f.path.startsWith(p + "/"))) {
        toast("Ordner existiert bereits.", "error");
        return;
      }
      if (folderApi) {
        // On-disk anlegen (persistiert im Volume — bleibt auch leer
        // nach Reload sichtbar).
        try {
          const res = await fetch(apiBase + "/folders", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "same-origin",
            body: JSON.stringify({ path: p }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || res.status);
        } catch (err) {
          toast("Ordner-Anlage fehlgeschlagen: " + err.message, "error");
          return;
        }
        await refresh();
        toast("Ordner angelegt.", "success");
        return;
      }
      state.extraDirs.add(p);
      // alle Eltern ebenfalls sichtbar machen
      const parts = p.split("/");
      for (let i = 1; i < parts.length; i++) state.extraDirs.add(parts.slice(0, i).join("/"));
      renderTree();
      toast("Ordner angelegt (bleibt dauerhaft, sobald eine Datei darin liegt " +
            "oder eine Zugriffs-Klasse gesetzt wird).", "success");
    }

    // ── Ordner-Umbenennen / -Löschen (Bulk über Datei-Moves) ──────
    function filesInDir(dir) {
      return state.files.filter(f => f.path.startsWith(dir + "/"));
    }

    // Gate für Ordner-Umbenennen/-Löschen: Ordner mit read-only-Inhalt
    // (Init-Ergebnisse, 🔒/👤-Dateien) dürfen nicht umgebaut werden
    // (gilt für beide Rollen). Zusätzlich nur für Student: Ordner mit
    // fester Zugriffs-Klasse (eigene Zeile oder im Subtree) — die
    // Klassen-Zeilen würden beim Umbenauen/-Löschen sonst auf dem alten
    // Pfad bleiben (Student hat keine /access-Route).
    function dirOpGate(dir) {
      const blocked = filesInDir(dir).filter(f => readOnlyCheck(f.path));
      if (blocked.length) {
        return { ok: false, reason: "Enthält " + blocked.length +
          " read-only Datei(en) — kann nicht gelöscht/umbenannt werden." };
      }
      if (!canSetAccess) {
        if (effectiveAccess(dir) !== "edit") {
          return { ok: false, reason: "Ordner ist read-only/versteckt — als Student nicht editierbar." };
        }
        if (state.folders.some(fd => fd.own &&
          (fd.path === dir || fd.path.startsWith(dir + "/")))) {
          return { ok: false, reason: "Ein Unterordner hat eine fixe Zugriffs-Klasse — als Student nicht editierbar." };
        }
      }
      return { ok: true };
    }

    async function renameDir(dir) {
      if (isInitDir(dir)) return;  // .init.sh-Ergebnis: read-only
      const gate = dirOpGate(dir);
      if (!gate.ok) { toast(gate.reason, "warning"); return; }
      const name = dir.split("/").pop();
      const parent = dir.slice(0, dir.length - name.length);
      const nn = prompt("Neuer Name für Ordner „" + dir + "“:", name);
      if (!nn) return;
      const n = nn.trim().replace(/[/\\]/g, "");
      if (!n || n === name) return;
      const newDir = parent + n;
      if (state.files.some(f => f.path === newDir || f.path.startsWith(newDir + "/")) ||
          state.folders.some(fd => fd.path === newDir)) {
        toast("„" + newDir + "“ existiert bereits.", "error");
        return;
      }
      const inDir = filesInDir(dir);
      const hasRow = state.folders.some(fd => fd.path === dir || fd.path.startsWith(dir + "/"));
      if (!inDir.length && !hasRow) {
        if (state.extraDirs.has(dir)) {
          state.extraDirs.delete(dir);
          state.extraDirs.add(newDir);
          renderTree();
        }
        return;
      }
      if (!confirm("„" + dir + "“ in „" + newDir + "“ umbenennen?\n" +
        inDir.length + " Datei(en) werden verschoben (Zugriffs-Klassen bleiben erhalten).")) return;
      await moveDirImpl(dir, newDir);
    }

    async function deleteDir(dir) {
      if (isInitDir(dir)) return;  // .init.sh-Ergebnis: read-only
      const gate = dirOpGate(dir);
      if (!gate.ok) { toast(gate.reason, "warning"); return; }
      const inDir = filesInDir(dir);
      const rows = state.folders.filter(fd => fd.own &&
        (fd.path === dir || fd.path.startsWith(dir + "/")));
      // Impliziter Eintrag = Ordner existiert on-disk (im Volume).
      const onDisk = state.folders.some(fd => fd.path === dir && !fd.own);
      if (!inDir.length && !rows.length && !onDisk && !state.extraDirs.has(dir)) return;
      if (!confirm("Ordner „" + dir + "“ löschen?\n" +
        inDir.length + " Datei(en) werden endgültig entfernt.")) return;
      if (folderApi) {
        // Ein Call entfernt den ganzen Subtree (Disk + DB-Rows: Dateien +
        // Ordner-Klassen) — auch wenn der Ordner nur eine Zeile ist.
        try {
          const res = await fetch(apiBase + "/folders/" + encPath(dir), {
            method: "DELETE", credentials: "same-origin",
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || res.status);
        } catch (err) {
          toast("Ordner-Löschung fehlgeschlagen: " + err.message, "error");
          return;
        }
      } else {
        for (const f of inDir) {
          if (!await deleteFileQuiet(f.path, false)) return;
        }
        // Persistierte Zugriffs-Klassen des (nun leeren) Ordners entfernen:
        for (const r of rows) {
          if (!await setAccess(r.path, true, null)) return;
        }
      }
      // Client-seitige (leere) Ordner des Subtrees bereinigen:
      Array.from(state.extraDirs).forEach(d => {
        if (d === dir || d.startsWith(dir + "/")) state.extraDirs.delete(d);
      });
      await refresh();
    }

    function renameFile(path) {
      const name = path.split("/").pop();
      const dir = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
      const nn = prompt("Neuer Name für „" + name + "“:", name);
      if (!nn) return;
      const n = nn.trim().replace(/[/\\]/g, "");
      if (!n || n === name) return;
      const dst = dir ? dir + "/" + n : n;
      if (state.files.some(f => f.path === dst)) {
        toast("„" + dst + "“ existiert bereits.", "error");
        return;
      }
      const gate = moveGate(path, dst);
      if (!gate.ok) { toast(gate.reason || "Nicht möglich.", "warning"); return; }
      moveFile(path, dst);
    }

    // ── Editor-Events ─────────────────────────────────────────────
    // Auto-Save: 3 s nach der letzten Änderung (auch beim Dateiwechsel wird
    // über openFile→saveFile sofort gespeichert).
    cm.on("change", () => {
      if (state.suppressChange) return;
      if (!apiBase || cm.getOption("readOnly")) return;
      if (!state.dirty) {
        state.dirty = true;
        setSaveState("● ungespeichert");
      }
      if (state.saveTimer) clearTimeout(state.saveTimer);
      state.saveTimer = setTimeout(() => saveFile(), 3000);
    });

    // Seitenwechsel/Tab zu: ausstehende Änderungen per keepalive-PUT retten.
    window.addEventListener("beforeunload", () => {
      if (!apiBase || !state.dirty || !state.currentFile) return;
      if (readOnlyCheck(state.currentFile)) return;
      try {
        fetch(apiBase + "/files/" + encPath(state.currentFile), {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          keepalive: true,
          body: JSON.stringify({ content: cm.getValue() }),
        });
      } catch (err) { /* ignore */ }
    });

    document.addEventListener("keydown", e => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        saveFile();
      }
    });

    return {
      get files() { return state.files; },
      get folders() { return state.folders; },
      get currentFile() { return state.currentFile; },
      get dirty() { return state.dirty; },
      accessOf: effectiveAccess,
      isInitPath,
      // ▶/⏹-Zustand je .sh-Datei (Template: Terminal-Run Start/Stop)
      setScriptRun: (path, running) => {
        path = String(path || "");
        if (running) state.scriptRuns.add(path);
        else state.scriptRuns.delete(path);
        treeEl.querySelectorAll(".ws-runbtn").forEach(b => {
          if (b.dataset.path !== path) return;
          const on = state.scriptRuns.has(path);
          b.innerHTML = on ? WSTOP_SVG : WPLAY_SVG;
          b.title = on ? "Skript stoppen" : "Skript ausführen";
          b.classList.toggle("bg-red-600", on);
          b.classList.toggle("hover:bg-red-700", on);
          b.classList.toggle("bg-green-600", !on);
          b.classList.toggle("hover:bg-green-700", !on);
        });
      },
      // ▶-Buttons blocken/entblocken (Container nicht gestartet) — ohne
      // Baum-Rerender; laufende Skripte (⏹) bleiben betätigbar.
      setRunDisabled: (blocked) => {
        treeEl.querySelectorAll(".ws-runbtn").forEach(b => {
          if (b.classList.contains("bg-red-600")) return;  // ⏹ läuft
          if (blocked) {
            b.disabled = true;
            b.classList.remove("bg-green-600", "hover:bg-green-700");
            b.classList.add("bg-gray-300", "cursor-not-allowed");
            b.title = "Container läuft nicht — erst ▶ Starten";
          } else if (b.disabled) {
            b.disabled = false;
            b.classList.remove("bg-gray-300", "cursor-not-allowed");
            b.classList.add("bg-green-600", "hover:bg-green-700");
            b.title = "Skript ausführen";
          }
        });
      },
      refresh,
      // „Neue Aufgabe"-Seite: nach der Direktanlage (erster Auto-Save) den
      // Files-API-Basis setzen, der Init-Zeit null war — Baum lädt dann
      // ohne Reload (s. task_detail_workspace.html, _wsActivate).
      setApiBase: (url) => { apiBase = url || null; },
      openFile,
      saveFile,
      createFile,
      deleteFile,
      moveFile,
      uploadFile,
      uploadEntries,
      uploadTo,
      uploadMenu,
      newFileIn,
      newFolderIn,
      getMainFile,
      setMainFile,
      getCurrentFile: () => state.currentFile,
      // Verzeichnis-Fokus an/aus (Student-Template: global genau ein
      // fokussiertes Element über Verzeichnis/Terminals/Ports — wird in
      // wsSetView gesteuert; andere Konsumenten rufen es nie → Default
      // true bleibt, Verhalten unverändert).
      setFileFocused: (on) => {
        on = !!on;
        if (state.fileFocused !== on) { state.fileFocused = on; renderTree(); }
      },
      isDirty: () => state.dirty,
      clearEditorState,
      restoreReload,
      closeMediaView,
      // CodeMirror neu vermessen (nach Unhide, z. B. Rückkehr aus
      // Terminal-/Preview-View)
      refreshEditor: () => { try { cm.refresh(); } catch (err) { /* ignore */ } },
      // für Tests/Debug
      _state: state,
    };
  }

  window.WorkspaceUI = {
    ACCESS,
    ACCESS_RANK,
    accessLabel,
    fmtBytes,
    fileIcon,
    cmModeForPath,
    mediaKind,
    init,
  };
})();
