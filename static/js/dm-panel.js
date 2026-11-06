/* ──────────────────────────────────────────────────────────────────
   DM-Panel: Nachrichten-Popup in der Navbar (1:1 + Gruppenchats).

   Links Chat-Liste, rechts Verlauf (Analog zum Kurs-Forum). Realtime:
   Polling (Liste 15 s, aktiver Chat 4 s) + SSE-Event "dm_message"
   vom Notification-Stream (Hook: window.dmOnSseEvent, vgl. base.html).
   Mobil: Fullscreen an den Seitenkanten, Master-Detail über die
   .mobile-viewing-Klasse (CSS in main.css).
   ────────────────────────────────────────────────────────────────── */
(function () {
  const panel = document.getElementById("dm-panel");
  if (!panel) return;

  const currentUserId = Number(panel.dataset.userId) || 0;
  const $ = (id) => document.getElementById(id);

  const state = {
    open: false,
    chats: [],
    activeChatId: null,
    pendingChat: null, // gerade angelegter 1:1-Chat (noch ohne Nachrichten,
    // daher noch nicht in der Liste — so lange nicht aus der Liste werfen)
    lastMsgId: 0,
    readUpTo: 0,
    renderedIds: new Set(),
    msgTimer: null,
    pollingMsgs: false,
    dialogMode: null, // "create" | "manage" | "view"
    selectedUserIds: [],
    searchTimer: null,
    pinned: false, // Desktop: Panel unten rechts angeheftet (bleibt beim
    // Seitenwechsel offen — Zustand in localStorage)
    pinSize: { w: 420, h: 540 },
  };

  const PIN_KEY = "aicampus-dm-pin";
  function loadPinState() {
    try {
      const s = JSON.parse(localStorage.getItem(PIN_KEY) || "null");
      if (s && typeof s === "object") return s;
    } catch { /* kaputt/privat → ignorieren */ }
    return null;
  }
  function savePinState() {
    try {
      localStorage.setItem(PIN_KEY, JSON.stringify({
        pinned: state.pinned,
        w: state.pinSize.w,
        h: state.pinSize.h,
        chatId: state.activeChatId,
      }));
    } catch { /* privat → kein Remembering */ }
  }
  function applyPinStyle() {
    panel.classList.toggle("dm-pinned", state.pinned);
    panel.style.width = state.pinned ? state.pinSize.w + "px" : "";
    panel.style.height = state.pinned ? state.pinSize.h + "px" : "";
    updateNarrow();
    const btn = $("dm-pin-btn");
    if (btn) btn.classList.toggle("text-blue-600", state.pinned);
    if (btn) btn.classList.toggle("bg-blue-50", state.pinned);
  }
  // Angeheftet + schmal: Master-Detail (Liste ODER Verlauf), sonst wäre
  // der Verlauf unter ~560px unbrauchbar schmal (analog Forum/Mobilmodus).
  // Nur messen, wenn das Panel sichtbar ist — hidden hat offsetWidth 0
  // und würde fälschlich "narrow" melden (Bug beim Restore).
  function updateNarrow() {
    if (!state.open || panel.offsetWidth === 0) return;
    panel.classList.toggle("narrow", state.pinned && panel.offsetWidth < 560);
  }
  window.dmTogglePin = () => {
    state.pinned = !state.pinned;
    applyPinStyle();
    savePinState();
  };

  async function api(url, opts) {
    const res = await fetch(url, Object.assign({ credentials: "same-origin" }, opts));
    if (!res.ok) {
      let msg = "Fehler (" + res.status + ")";
      try {
        const d = await res.json();
        if (d.detail) msg = typeof d.detail === "string" ? d.detail : "Fehler (" + res.status + ")";
      } catch { /* nicht-JSON */ }
      throw new Error(msg);
    }
    return res.json();
  }

  // ─── Öffnen / Schließen ─────────────────────────────────────────

  function setPanelOpen(open) {
    if (open === state.open) return;
    state.open = open;
    panel.classList.toggle("hidden", !open);
    const btn = $("dm-btn");
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (!open) state.pendingChat = null;
    if (open) {
      // Andere Navbar-Menus schließen (Klick aufs Bubble-Icon kommt sonst
      // für deren Outside-Handler "von außerhalb").
      if (typeof closeNotifMenu === "function") closeNotifMenu();
      if (typeof closeUserMenu === "function") closeUserMenu();
      if (typeof closeMobileMenu === "function") closeMobileMenu();
      loadChats().catch(() => {});
      // Nachrichten, die während der Schließphase im aktiven Chat
      // reinkamen: sofort nachladen, statt auf den ersten 4-s-Poll-Tick
      // zu warten (Badge wäre sonst schneller als der Verlauf).
      if (state.activeChatId != null) pollActiveMessages();
      updateNarrow(); // erst jetzt sichtbar → Breite wirklich messbar
    } else if (state.dialogMode) {
      dmCloseNewChat();
    }
    scheduleMsgPoll();
  }

  window.dmTogglePanel = () => setPanelOpen(!state.open);
  window.dmClosePanel = () => setPanelOpen(false);
  window.dmOpenPanel = (chatId) => {
    setPanelOpen(true);
    if (chatId) selectChat(Number(chatId));
  };

  // ─── Chat-Liste + Badge ─────────────────────────────────────────

  async function loadChats() {
    const chats = await api("/api/dm/chats");
    // Geöffneter, sichtbarer Chat zählt als gesehen — wer die Konversation
    // direkt vor sich hat, soll beim Empfang keine rote Badge sehen, auch
    // wenn der Read-State-Commit noch ein Tick hinterherhinkt.
    if (state.open && state.activeChatId != null && !document.hidden) {
      const active = chats.find((c) => c.id === state.activeChatId);
      if (active) active.unread_count = 0;
    }
    state.chats = chats;
    updateBadge();
    if (state.open) {
      renderChatList();
      const active = state.chats.find((c) => c.id === state.activeChatId);
      if (state.activeChatId != null && !active) {
        const pending = state.pendingChat && state.pendingChat.id === state.activeChatId;
        if (!pending) {
          // Aktiver Chat ist weg (gelöscht oder leere 1:1-Chats, die die
          // Liste ausblendet) → zurück zur Liste
          state.activeChatId = null;
          state.pendingChat = null;
          state.readUpTo = 0;
          panel.classList.remove("mobile-viewing");
          savePinState(); // sonst öffnet ein Seitenwechsel den weggefallenen Chat wieder
          showChatEmpty();
          scheduleMsgPoll();
        }
        // pending: Chat erst gerade angelegt, erste Nachricht noch offen —
        // Konversation bleibt offen, bis der Chat in der Liste auftaucht.
      } else if (active) {
        state.pendingChat = null;
        renderChatHeader(active);
      }
    }
  }

  function updateBadge() {
    const total = state.chats.reduce((sum, c) => sum + (c.unread_count || 0), 0);
    const badge = $("dm-badge");
    if (!badge) return;
    badge.textContent = total > 99 ? "99+" : String(total);
    badge.classList.toggle("hidden", total === 0);
    badge.classList.toggle("flex", total > 0);
  }

  function dmAvatarHtml(user, cls) {
    if (user && user.avatar) return `<img src="${escHtml(user.avatar)}" alt="" class="${cls} object-cover">`;
    return `<span class="${cls} bg-gray-200 flex items-center justify-center text-sm">👤</span>`;
  }

  function renderChatList() {
    const list = $("dm-chat-list");
    if (!list) return;
    if (!state.chats.length) {
      list.innerHTML =
        '<p class="text-sm text-gray-400 text-center py-8 px-4">Noch keine Chats.<br>Starte oben einen neuen!</p>';
      return;
    }
    list.innerHTML = state.chats
      .map((c) => {
        const avatar = c.is_group
          ? `<span class="w-10 h-10 rounded-full bg-blue-100 text-blue-700 flex items-center justify-center shrink-0">👥</span>`
          : dmAvatarHtml(c.other, "w-10 h-10 rounded-full shrink-0");
        let preview;
        if (c.last_message) {
          const mine = c.last_message.user_id === currentUserId;
          const prefix = mine ? "Du: " : c.is_group ? escHtml((c.last_message.name || "").split(" ")[0]) + ": " : "";
          preview = `<span class="text-xs text-gray-500 truncate">${prefix}${escHtml(c.last_message.content)}</span>`;
        } else {
          preview = '<span class="text-xs text-gray-400 italic truncate">Noch keine Nachrichten</span>';
        }
        const badge = c.unread_count
          ? `<span class="shrink-0 min-w-[18px] h-[18px] px-1 flex items-center justify-center rounded-full bg-red-500 text-white text-[11px] font-bold">${c.unread_count > 99 ? "99+" : c.unread_count}</span>`
          : "";
        const active = c.id === state.activeChatId;
        return `
          <button type="button" data-chat-id="${c.id}" onclick="dmSelectChat(${c.id})"
                  class="dm-chat-row w-full flex items-center gap-2.5 px-3 py-2.5 text-left transition ${active ? "bg-blue-50" : "hover:bg-gray-100"}">
            ${avatar}
            <span class="flex-1 min-w-0">
              <span class="flex items-center justify-between gap-2">
                <span class="text-sm font-semibold text-gray-800 truncate">${escHtml(c.name)}</span>
                <span class="text-[11px] text-gray-400 shrink-0">${escHtml(c.last_message ? c.last_message.label : "")}</span>
              </span>
              <span class="flex items-center gap-2">
                <span class="flex-1 min-w-0">${preview}</span>
                ${badge}
              </span>
            </span>
          </button>`;
      })
      .join("");
  }

  window.dmSelectChat = (id) => selectChat(id);

  // Nur die Active-Zeile umfärben statt die ganze Liste neu zu rendern:
  // Ein innerHTML-Reset würde den gerade geklickten Button aus dem DOM
  // reißen, bevor der Klick weiterbubbt — der Outside-Click-Handler würde
  // dann "Klick außerhalb" melden und das Panel schließen.
  function markActiveRow() {
    document.querySelectorAll("#dm-chat-list .dm-chat-row").forEach((row) => {
      row.classList.toggle("bg-blue-50", Number(row.dataset.chatId) === state.activeChatId);
      row.classList.toggle("hover:bg-gray-100", Number(row.dataset.chatId) !== state.activeChatId);
    });
  }

  // Mobil: von der Konversation zurück zur Chat-Liste.
  window.dmBackToList = () => {
    state.activeChatId = null;
    state.readUpTo = 0;
    panel.classList.remove("mobile-viewing");
    savePinState(); // sonst öffnet ein Seitenwechsel den alten Chat wieder
    showChatEmpty();
    scheduleMsgPoll();
  };

  // ─── Chat-Verlauf ───────────────────────────────────────────────

  function showChatEmpty() {
    $("dm-chat-title").textContent = "";
    $("dm-chat-subtitle").textContent = "";
    $("dm-chat-avatar").innerHTML = "";
    $("dm-chat-menu-btn").classList.add("hidden");
    $("dm-messages").classList.add("hidden");
    $("dm-composer").classList.add("hidden");
    $("dm-chat-empty").classList.remove("hidden");
  }

  function renderChatHeader(chat) {
    $("dm-chat-title").textContent = chat.name;
    $("dm-chat-subtitle").textContent = chat.is_group
      ? chat.members.map((m) => m.name).join(", ")
      : (chat.other && chat.other.username ? "@" + chat.other.username : "");
    $("dm-chat-avatar").innerHTML = chat.is_group
      ? '<span class="w-9 h-9 rounded-full bg-blue-100 text-blue-700 flex items-center justify-center">👥</span>'
      : dmAvatarHtml(chat.other, "w-9 h-9 rounded-full");
    const hasMenu = !!(chat.is_group || chat.can_manage);
    $("dm-chat-menu-btn").classList.toggle("hidden", !hasMenu);
    if (hasMenu) {
      $("dm-menu-members").classList.toggle("hidden", !chat.is_group);
      $("dm-menu-rename").classList.toggle("hidden", !chat.can_manage);
    }
  }

  // ⋯-Menü → Mitglieder-Dialog (verwalten, wenn Ersteller, sonst nur ansehen).
  window.dmMenuMembers = () => {
    const chat = state.chats.find((c) => c.id === state.activeChatId);
    if (!chat) return;
    $("dm-chat-menu").classList.add("hidden");
    dmOpenNewChat(chat.can_manage ? "manage" : "view");
  };

  async function selectChat(chatId, chatDict) {
    state.activeChatId = chatId;
    state.pendingChat = chatDict || null;
    state.lastMsgId = 0;
    state.readUpTo = 0;
    state.renderedIds = new Set();
    savePinState(); // angeheftetes Fenster: Chat überlebt Seitenwechsel
    panel.classList.add("mobile-viewing");
    markActiveRow();
    const chat = chatDict || state.chats.find((c) => c.id === chatId);
    renderChatHeader(chat || { name: "…", is_group: false, members: [], can_manage: false });
    $("dm-chat-empty").classList.add("hidden");
    $("dm-messages").classList.remove("hidden");
    $("dm-composer").classList.remove("hidden");
    $("dm-messages").innerHTML = "";
    scheduleMsgPoll();
    try {
      const msgs = await api(`/api/dm/chats/${chatId}/messages`);
      if (state.activeChatId !== chatId) return; // in der Zeit auf anderen Chat gewechselt
      appendMessages(msgs, true);
      markChatRead();
    } catch (e) {
      showToast(e.message, "error");
    }
  }

  function dmMessageEl(m) {
    const el = document.createElement("div");
    el.className = "dm-message flex gap-3 group";
    el.innerHTML = `
      ${dmAvatarHtml(m, "w-9 h-9 rounded-full shrink-0 mt-0.5")}
      <span class="flex-1 min-w-0">
        <span class="flex items-center gap-2 flex-wrap">
          <span class="text-sm font-semibold text-gray-800">${escHtml(m.name)}${m.is_self ? ' <span class="text-gray-400 font-normal">(Du)</span>' : ""}</span>
          <span class="text-xs text-gray-400">${new Date(m.created_at).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" })}</span>
        </span>
        <span class="dm-content block text-sm text-gray-700 break-words mt-0.5"></span>
      </span>
    `;
    const md = el.querySelector(".dm-content");
    // Dasselbe Markdown-Rendering wie Forum/Skript (breaks: Zeilenumbrüche bleiben).
    el._renderDone = renderMarkdown(m.content, md, { breaks: true }).catch((e) => {
      console.warn("DM: Markdown-Rendering fehlgeschlagen", e);
      md.textContent = m.content;
    });
    return el;
  }

  function dmIsNearBottom() {
    const list = $("dm-messages");
    return list.scrollHeight - list.scrollTop - list.clientHeight < 120;
  }

  function scrollToBottom() {
    const list = $("dm-messages");
    list.scrollTop = list.scrollHeight;
  }

  function appendMessages(msgs, forceScroll) {
    const list = $("dm-messages");
    const fresh = msgs.filter((m) => !state.renderedIds.has(m.id));
    if (!fresh.length) return;
    const stick = forceScroll || dmIsNearBottom();
    for (const m of fresh) {
      state.renderedIds.add(m.id);
      list.appendChild(dmMessageEl(m));
    }
    state.lastMsgId = Math.max(state.lastMsgId, ...fresh.map((m) => m.id));
    if (stick) {
      // Erst nach dem Markdown-Rendering scrollen (sonst "Aufbläh"-Effekt,
      // wie im Forum): auf jedes Render-Promise warten, dann neu scrollen.
      Array.from({ length: fresh.length }).forEach((_, i) => {
        const el = list.children[list.children.length - fresh.length + i];
        if (el && el._renderDone) el._renderDone.then(() => scrollToBottom());
        else scrollToBottom();
      });
    }
  }

  // Gelesen-Stand vorrücken (nur vor, nie zurück; serverseitig wird der
  // Stand geupsert). Dazu: eigene dm_message-Benachrichtigungen des Chats
  // werden serverseitig beim Message-Fetch als gelesen markiert (Auto-Read).
  function markChatRead() {
    if (state.activeChatId == null || state.lastMsgId <= state.readUpTo) return;
    state.readUpTo = state.lastMsgId;
    fetch(`/api/dm/chats/${state.activeChatId}/read?up_to_id=${state.readUpTo}`, {
      method: "POST",
      credentials: "same-origin",
    }).catch(() => {});
    const chat = state.chats.find((c) => c.id === state.activeChatId);
    if (chat && chat.unread_count) {
      chat.unread_count = 0;
      renderChatList();
      updateBadge();
    }
  }

  // ─── Polling + SSE ──────────────────────────────────────────────

  async function pollActiveMessages() {
    if (document.hidden || state.pollingMsgs || state.activeChatId == null) return;
    state.pollingMsgs = true;
    try {
      const after = state.lastMsgId ? `?after_id=${state.lastMsgId}` : "";
      const chatId = state.activeChatId;
      const msgs = await api(`/api/dm/chats/${chatId}/messages${after}`);
      if (state.activeChatId !== chatId) return;
      const fresh = msgs.filter((m) => !state.renderedIds.has(m.id));
      if (fresh.length) {
        appendMessages(msgs, false);
        markChatRead();
      }
    } catch { /* kein Netz — nächster Tick */ } finally {
      state.pollingMsgs = false;
    }
  }

  function scheduleMsgPoll() {
    clearInterval(state.msgTimer);
    if (state.open && state.activeChatId != null) {
      state.msgTimer = setInterval(pollActiveMessages, 4000);
    }
  }

  // Vom Notification-Stream (base.html) hierher weitergeleitet.
  window.dmOnSseEvent = (p) => {
    if (!p || !p.event) return;
    if (p.event === "dm_message") {
      if (!document.hidden) loadChats().catch(() => {});
      // Sofort nachladen, statt den 4-s-Timer zurückzusetzen und zu warten.
      if (state.open && p.chat_id === state.activeChatId) pollActiveMessages();
    } else if (p.event === "read") {
      if (!document.hidden) loadChats().catch(() => {});
    }
  };

  // ─── Senden ─────────────────────────────────────────────────────

  async function dmSend() {
    const input = $("dm-input");
    const content = input.value.trim();
    if (!content || state.activeChatId == null) return;
    input.value = "";
    input.style.height = "";
    try {
      const msg = await api(`/api/dm/chats/${state.activeChatId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      });
      appendMessages([msg], true);
    } catch (e) {
      input.value = content;
      showToast(e.message, "error");
    }
  }

  // ─── Neuer Chat / Mitglieder-Dialog ─────────────────────────────

  function dmOpenNewChat(mode) {
    if (!state.open) setPanelOpen(true);
    state.dialogMode = mode;
    state.selectedUserIds = [];
    $("dm-new-chat").classList.remove("hidden");
    $("dm-new-chat-title").textContent =
      mode === "create" ? "Neuer Chat" : mode === "manage" ? "Mitglieder verwalten" : "Mitglieder";
    $("dm-search-input").value = "";
    $("dm-group-name").value = "";
    $("dm-group-name-row").classList.add("hidden");
    $("dm-search-input").classList.toggle("hidden", mode === "view");
    $("dm-create-chat-btn").classList.add("hidden");
    $("dm-member-list").classList.toggle("hidden", mode === "create");
    $("dm-member-list").innerHTML =
      mode === "view" ? '<p class="text-sm text-gray-400 text-center py-4">Lade Mitglieder …</p>' : "";
    $("dm-dialog-hint").textContent =
      mode === "create"
        ? "Person suchen. Eine Auswahl = Direktnachricht, mehrere = Gruppe."
        : mode === "manage"
        ? "Mitglieder entfernen oder neue suchen."
        : "";
    $("dm-search-results").innerHTML = "";
    $("dm-search-results").classList.toggle("hidden", mode === "view");
    if (mode !== "create") renderMemberList();
    if (mode !== "view") $("dm-search-input").focus();
    updateCreateButton();
  }

  window.dmOpenNewChat = (mode) => dmOpenNewChat(mode || "create");
  window.dmCloseNewChat = () => {
    state.dialogMode = null;
    $("dm-new-chat").classList.add("hidden");
  };

  function renderMemberList() {
    const chat = state.chats.find((c) => c.id === state.activeChatId);
    const list = $("dm-member-list");
    if (!chat) { list.innerHTML = ""; return; }
    const canManage = chat.can_manage;
    list.innerHTML = chat.members
      .map((m) => {
        const self = m.id === currentUserId;
        const removeBtn =
          canManage && !self
            ? `<button type="button" onclick="dmRemoveMember(${m.id})" class="text-gray-300 hover:text-red-500 transition" title="Entfernen">✕</button>`
            : "";
        return `
          <div class="flex items-center gap-2.5 px-2 py-1.5">
            ${dmAvatarHtml(m, "w-8 h-8 rounded-full shrink-0")}
            <span class="flex-1 min-w-0 text-sm truncate">${escHtml(m.name)}${self ? ' <span class="text-gray-400">(Du)</span>' : ""}</span>
            ${removeBtn}
          </div>`;
      })
      .join("");
  }

  window.dmRemoveMember = async (userId) => {
    if (state.activeChatId == null) return;
    try {
      const res = await api(`/api/dm/chats/${state.activeChatId}/members/${userId}`, { method: "DELETE" });
      if (res.deleted) {
        dmCloseNewChat();
        await loadChats();
      } else {
        renderMemberList();
        const chat = state.chats.find((c) => c.id === state.activeChatId);
        if (chat) renderChatHeader(chat);
      }
    } catch (e) {
      showToast(e.message, "error");
    }
  };

  window.dmAddMember = async (userId) => {
    if (state.activeChatId == null) return;
    try {
      await api(`/api/dm/chats/${state.activeChatId}/members`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId }),
      });
      await loadChats();
      renderMemberList();
      const chat = state.chats.find((c) => c.id === state.activeChatId);
      if (chat) renderChatHeader(chat);
    } catch (e) {
      showToast(e.message, "error");
    }
  };

  window.dmRenameChat = async () => {
    const chat = state.chats.find((c) => c.id === state.activeChatId);
    if (!chat) return;
    const name = prompt("Neuer Name:", chat.name);
    if (name == null || !name.trim() || name.trim() === chat.name) return;
    try {
      await api(`/api/dm/chats/${chat.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim() }),
      });
      await loadChats();
    } catch (e) {
      showToast(e.message, "error");
    }
  };

  window.dmLeaveChat = async () => {
    const chat = state.chats.find((c) => c.id === state.activeChatId);
    if (!chat) return;
    if (!confirm(`${chat.is_group ? "Gruppe" : "Chat"} „${chat.name}“ wirklich verlassen?`)) return;
    try {
      const res = await api(`/api/dm/chats/${chat.id}/members/${currentUserId}`, { method: "DELETE" });
      if (res.deleted) {
        state.activeChatId = null;
        panel.classList.remove("mobile-viewing");
        savePinState();
        await loadChats();
        showChatEmpty();
      } else {
        await loadChats();
      }
    } catch (e) {
      showToast(e.message, "error");
    }
  };

  function dmToggleChatMenu() {
    $("dm-chat-menu").classList.toggle("hidden");
  }
  window.dmToggleChatMenu = dmToggleChatMenu;

  // ─── User-Suche + Erstellen ─────────────────────────────────────

  async function dmSearchUsers() {
    const q = $("dm-search-input").value.trim();
    const results = $("dm-search-results");
    if (!q) {
      results.innerHTML = '<p class="text-sm text-gray-400 text-center py-3">Namen oder @username eingeben …</p>';
      return;
    }
    try {
      const data = await api(`/api/users/search?q=${encodeURIComponent(q)}`);
      results.innerHTML = data.users.length
        ? data.users
            .map((u) => {
              const selected = state.selectedUserIds.includes(u.id);
              const inChat = state.dialogMode === "manage";
              const action = inChat
                ? `<button type="button" onclick="dmAddMember(${u.id})" class="text-blue-600 hover:text-blue-800 text-sm font-semibold" title="Hinzufügen">+</button>`
                : `<input type="checkbox" ${selected ? "checked" : ""} class="w-4 h-4 accent-blue-600 pointer-events-none">`;
              return `
                <div class="flex items-center gap-2.5 px-2 py-1.5 rounded-lg hover:bg-gray-100 transition cursor-pointer ${selected ? "bg-blue-50" : ""}"
                     onclick="dmToggleUserRow(${u.id}, this)">
                  ${dmAvatarHtml(u, "w-8 h-8 rounded-full shrink-0")}
                  <span class="flex-1 min-w-0">
                    <span class="block text-sm font-medium truncate">${escHtml(u.name)}</span>
                    <span class="block text-xs text-gray-400 truncate">@${escHtml(u.username)}</span>
                  </span>
                  ${action}
                </div>`;
            })
            .join("")
        : '<p class="text-sm text-gray-400 text-center py-3">Keine Treffer.</p>';
    } catch {
      results.innerHTML = '<p class="text-sm text-red-500 text-center py-3">Suche fehlgeschlagen.</p>';
    }
  }

  // Klick auf die ganze Zeile (nicht nur das Häkchen) toggelt die Auswahl.
  window.dmToggleUserRow = (userId, row) => {
    const i = state.selectedUserIds.indexOf(userId);
    const next = i === -1;
    const cb = row.querySelector("input[type=checkbox]");
    if (cb) cb.checked = next;
    if (next && i === -1) state.selectedUserIds.push(userId);
    if (!next && i !== -1) state.selectedUserIds.splice(i, 1);
    row.classList.toggle("bg-blue-50", next);
    updateCreateButton();
  };

  window.updateCreateButton = function updateCreateButton() {
    const btn = $("dm-create-chat-btn");
    const n = state.selectedUserIds.length;
    if (state.dialogMode !== "create") return;
    btn.classList.toggle("hidden", n < 1);
    btn.disabled = n < 1 || (n >= 2 && !$("dm-group-name").value.trim());
    btn.textContent = n === 1 ? "Chat starten" : "Gruppe erstellen";
    $("dm-group-name-row").classList.toggle("hidden", n < 2);
  };

  window.dmCreateChat = async () => {
    const ids = state.selectedUserIds;
    if (!ids.length) return;
    try {
      let chat;
      if (ids.length === 1) {
        chat = await api("/api/dm/chats", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ member_id: ids[0] }),
        });
      } else {
        const name = $("dm-group-name").value.trim();
        if (!name) { showToast("Bitte einen Gruppen-Name eingeben.", "warning"); return; }
        chat = await api("/api/dm/chats/group", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, member_ids: ids }),
        });
      }
      dmCloseNewChat();
      await loadChats();
      selectChat(chat.id, chat);
    } catch (e) {
      showToast(e.message, "error");
    }
  };

  // Einstiegspunkt für Profil-/Mitglieder-/Forums-Buttons.
  window.dmStartChat = async (userId) => {
    try {
      const chat = await api("/api/dm/chats", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ member_id: userId }),
      });
      setPanelOpen(true);
      await loadChats();
      selectChat(chat.id, chat);
    } catch (e) {
      showToast(e.message, "error");
    }
  };

  // ─── Init ───────────────────────────────────────────────────────

  (function init() {
    const composer = $("dm-composer");
    if (composer) composer.addEventListener("submit", (e) => { e.preventDefault(); dmSend(); });

    const input = $("dm-input");
    if (input) {
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          dmSend();
        }
      });
      input.addEventListener("input", () => {
        input.style.height = "";
        input.style.height = Math.min(input.scrollHeight, 120) + "px";
      });
    }

    const search = $("dm-search-input");
    if (search) {
      search.addEventListener("input", () => {
        clearTimeout(state.searchTimer);
        state.searchTimer = setTimeout(dmSearchUsers, 250);
      });
      search.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); dmSearchUsers(); }
      });
    }

    // Dialog-Backdrop: Klick darauf schließt den Dialog.
    const dialog = $("dm-new-chat");
    if (dialog) {
      dialog.addEventListener("click", (e) => {
        if (e.target === dialog) dmCloseNewChat();
      });
    }

    // Chat-Menü (⋯): Klick außerhalb schließt es.
    document.addEventListener("click", (e) => {
      const menu = $("dm-chat-menu");
      if (menu && !menu.classList.contains("hidden") &&
          !menu.contains(e.target) && !e.target.closest("#dm-chat-menu-btn")) {
        menu.classList.add("hidden");
      }
      // Panel: Klick außerhalb (auch auf die Navbar) schließt es —
      // außer angeheftet (bleibt per Definition beim Klicken stehen).
      // composedPath() = Event-Pfad vom Dispatch-Zeitpunkt, bleibt valide,
      // wenn der Handler das DOM zwischenzeitlich neu gerendert hat.
      if (!state.open || state.pinned) return;
      const wrap = $("dm-wrap");
      const path = typeof e.composedPath === "function" ? e.composedPath() : [e.target];
      if (!path.includes(panel) && !(wrap && path.includes(wrap))) setPanelOpen(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (state.dialogMode) dmCloseNewChat();
      else if (state.open && !state.pinned) setPanelOpen(false);
    });

    // Resize-Handler (linke obere Ecke, nur angeheftet): ziehen vergrößert
    // nach links/unten. Pointer-Capture, damit das Ziehen außerhalb des
    // kleinen Handle-Bereichs nicht abbricht.
    const resizeEl = $("dm-resize");
    if (resizeEl) {
      resizeEl.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const startX = e.clientX, startY = e.clientY;
        const startW = panel.offsetWidth, startH = panel.offsetHeight;
        try { resizeEl.setPointerCapture(e.pointerId); } catch { /* ignorieren */ }
        const move = (ev) => {
          const w = Math.min(960, Math.max(320, startW + (startX - ev.clientX)));
          const h = Math.min(window.innerHeight - 80, Math.max(320, startH + (startY - ev.clientY)));
          state.pinSize = { w, h };
          panel.style.width = w + "px";
          panel.style.height = h + "px";
          updateNarrow();
        };
        const up = () => {
          resizeEl.removeEventListener("pointermove", move);
          resizeEl.removeEventListener("pointerup", up);
          resizeEl.removeEventListener("pointercancel", up);
          savePinState();
        };
        resizeEl.addEventListener("pointermove", move);
        resizeEl.addEventListener("pointerup", up);
        resizeEl.addEventListener("pointercancel", up);
      });
    }
    // Viewport-Änderung (Browserfenster): Narrow-Schwelle neu prüfen.
    window.addEventListener("resize", updateNarrow);

    // Badge-Poll (15 s): läuft immer — der Ungelesen-Zähler in der Navbar
    // soll aktuell sein, auch wenn das Panel zu ist. SSE (dm_message)
    // aktualisiert zusätzlich sofort; Polling ist der Fallback.
    setInterval(() => { if (!document.hidden) loadChats().catch(() => {}); }, 15000);
    loadChats().catch(() => {});

    // Angeheftetes Fenster über Seitenwechsel (Voll-Reload) hinweg halten:
    // Zustand (Pin, Größe, Chat) aus localStorage wiederherstellen und das
    // Panel direkt wieder aufmachen.
    const saved = loadPinState();
    if (saved && saved.pinned && window.matchMedia("(min-width: 640px)").matches) {
      state.pinned = true;
      if (saved.w && saved.h) state.pinSize = { w: saved.w, h: saved.h };
      applyPinStyle();
      setPanelOpen(true);
      const chatId = Number(saved.chatId) || null;
      if (chatId) {
        // Erst prüfen, dass der Chat noch existiert (leere 1:1-Chats
        // verschwinden aus der Liste) → sonst stummer 404-Toast.
        api("/api/dm/chats").then((chats) => {
          if (state.open && state.activeChatId == null &&
              chats.some((c) => c.id === chatId)) {
            selectChat(chatId);
          }
        }).catch(() => {});
      }
      return; // Deep-Link überschreibt einen angehefteten Zustand nicht
    }

    // Deep-Link (z. B. aus einer Benachrichtigung): /messages?chat=<id>
    const chatId = new URLSearchParams(window.location.search).get("chat");
    if (chatId) dmOpenPanel(chatId);
  })();
})();
