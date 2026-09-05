/* Event wiring, decisions, chat streaming, keyboard, boot.
   Loaded last: state.js → api.js → render.js → dialogs.js → app.js. */

"use strict";

/* ---------------- loading ---------------- */

async function loadDecks() {
  try {
    S.decks = (await API.decks()) || [];
  } catch (e) {
    S.error = e.message;
  }
}

async function loadNotes() {
  const deck = S.deck;
  if (!deck) return;
  try {
    const n = await API.notes(deck);
    if (S.deck !== deck) return;
    S.notes = n || { deck, total: 0, flagged: 0, notes: [] };
    S.error = "";
    selectFirstFlagged();
  } catch (e) {
    if (S.deck !== deck) return;
    S.error = e.message;
    S.notes = { deck, total: 0, flagged: 0, notes: [] };
  }
  draw();
}

async function loadCorpus() {
  const deck = S.deck;
  if (!deck) return;
  S.corpusLoading = true;
  draw();
  try {
    const c = await API.corpus(deck);
    if (S.deck !== deck) return;
    S.corpus = c || { deck, inherited_from: null, sources: [] };
  } catch (e) {
    if (S.deck !== deck) return;
    S.corpus = { deck, inherited_from: null, sources: [] };
    S.error = e.message;
  } finally {
    S.corpusLoading = false;
  }
  draw();
}

async function selectDeck(name) {
  if (S.deck === name) return;
  S.deck = name;
  S.notes = null;
  S.selNote = null;
  S.revealed = {};
  S.corpus = null;
  S.srcForm = null;
  S.srcExpanded = {};
  S.error = "";
  // The conversation lives per deck and is dropped when the deck changes.
  S.chat = [];
  S.chatRefs = [];
  S.chatDraft = "";
  draw();
  await Promise.all([loadNotes(), loadCorpus()]);
}

/* ---------------- selection ---------------- */

function selectFirstFlagged() {
  const vis = visibleNotes();
  const first = vis.find((n) => n.flagged) || vis[0];
  S.selNote = first ? first.note_id : null;
}

function selectNextFlagged(fromIndex) {
  const vis = visibleNotes();
  if (!vis.length) {
    S.selNote = null;
    return;
  }
  const idx = Math.max(0, Math.min(fromIndex, vis.length - 1));
  const next = vis.slice(idx).find((n) => n.flagged) || vis.find((n) => n.flagged) || vis[idx];
  S.selNote = next ? next.note_id : null;
}

function moveSelection(delta) {
  const vis = visibleNotes();
  if (!vis.length) return;
  const i = vis.findIndex((n) => n.note_id === S.selNote);
  const next = i < 0 ? 0 : Math.max(0, Math.min(vis.length - 1, i + delta));
  S.selNote = vis[next].note_id;
  draw();
  const el = document.querySelector('.note[data-note="' + S.selNote + '"]');
  if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
}

/* ---------------- decisions ---------------- */

/* Called after every write: re-fetch the queue and the deck counts, then move on. */
async function afterDecision(opts) {
  const o = opts || {};
  const vis = visibleNotes();
  const target = o.resolvedId || S.selNote;
  const prevIndex = Math.max(
    0,
    vis.findIndex((n) => n.note_id === target),
  );
  S.busy = true;
  draw();
  try {
    const res = await Promise.all([API.notes(S.deck), API.decks()]);
    S.notes = res[0] || S.notes;
    S.decks = res[1] || S.decks;
    S.revealed = {};
    S.error = "";
  } catch (e) {
    S.error = e.message;
  }
  S.busy = false;
  if (!o.keepSelection || !noteById(S.selNote)) selectNextFlagged(prevIndex);
  draw();
}

async function applyDecision(key, noteId) {
  const note = noteById(noteId);
  if (!note || S.busy) return;
  if (key === "skip") return moveSelection(1);
  if (key === "edit") return openEditDialog(note);
  if (key === "split") return openSplitDialog(note);
  if (key === "create") return openCreateDialog(note);
  if (key === "move") return openMoveDialog(note);
  if (key === "delete") return openDeleteDialog(note);
  if (key !== "keep") return;
  S.busy = true;
  S.error = "";
  draw();
  try {
    await API.keep(noteId);
    S.busy = false;
    await afterDecision({ resolvedId: noteId });
  } catch (e) {
    S.error = e.message;
    S.busy = false;
    draw();
  }
}

/* ---------------- sources ---------------- */

function entryOf(s) {
  const e = { kind: s.kind, target: s.target };
  if (s.pages) e.pages = s.pages;
  if (s.note) e.note = s.note;
  return e;
}

function currentEntries() {
  return (((S.corpus || {}).sources) || []).map(entryOf);
}

async function saveSources(entries) {
  await API.putSources(S.deck, entries);
  await loadCorpus();
  await loadDecks();
  draw();
}

async function removeSource(i) {
  const entries = currentEntries();
  if (i < 0 || i >= entries.length) return;
  entries.splice(i, 1);
  try {
    await saveSources(entries);
  } catch (e) {
    S.error = e.message;
    draw();
  }
}

async function saveNewSource() {
  const f = S.srcForm;
  if (!f) return;
  const target = String(f.target || "").trim();
  if (!target) {
    f.error = "Indique une cible.";
    draw();
    return;
  }
  const inherited = !!(S.corpus && S.corpus.inherited_from);
  const base = !inherited || f.copyInherited !== false ? currentEntries() : [];
  const entry = { kind: f.kind === "pdf" ? "pdf" : "obsidian", target };
  if (String(f.pages || "").trim()) entry.pages = String(f.pages).trim();
  if (String(f.note || "").trim()) entry.note = String(f.note).trim();
  f.saving = true;
  f.error = "";
  draw();
  try {
    await saveSources(base.concat([entry]));
    S.srcForm = null;
    draw();
  } catch (e) {
    f.saving = false;
    f.error = e.message;
    draw();
  }
}

const lookupVaultNotes = debounce(async (query) => {
  if (!query || query.length < 2) return;
  try {
    const names = await API.vaultNotes(query);
    const dl = document.getElementById("vault-notes");
    if (!dl) return;
    dl.innerHTML = (names || [])
      .slice(0, 50)
      .map((n) => '<option value="' + esc(n) + '"></option>')
      .join("");
  } catch (e) {
    /* autocomplete is best-effort */
  }
}, 250);

/* ---------------- chat ---------------- */

function chatContextIds() {
  const ids = [];
  const sel = selectedNote();
  if (sel) ids.push(sel.note_id);
  S.chatRefs.forEach((id) => {
    if (ids.indexOf(id) < 0) ids.push(id);
  });
  return ids;
}

/* Prior proposals are not replayed; they are summarised into the assistant text. */
function historyForServer() {
  const out = [];
  S.chat.forEach((m) => {
    const summary = (m.proposals || [])
      .map(
        (p) =>
          "[proposition: " +
          (p.kind || "?") +
          ((p.input || {}).note_id ? " " + short(p.input.note_id) : "") +
          "]",
      )
      .join(" ");
    const content = [m.text || "", summary].filter((x) => x && x.trim()).join("\n");
    if (!content.trim()) return;
    out.push({ role: m.who === "user" ? "user" : "assistant", content });
  });
  return out;
}

let logRefreshQueued = false;
function scheduleLogRefresh() {
  if (logRefreshQueued) return;
  logRefreshQueued = true;
  requestAnimationFrame(() => {
    logRefreshQueued = false;
    refreshChatLog();
  });
}

async function sendChat() {
  const text = String(S.chatDraft || "").trim();
  if (!text || S.chatBusy || !S.deck) return;
  if (S.chatStatus && S.chatStatus.configured === false) return;
  const ids = chatContextIds();
  const messages = historyForServer();
  messages.push({ role: "user", content: text });

  S.chat.push({ who: "user", text, refs: ids });
  const reply = { who: "assistant", text: "", proposals: [], streaming: true, error: "" };
  S.chat.push(reply);
  S.chatDraft = "";
  S.chatBusy = true;
  draw();

  try {
    const payload = {
      deck: S.deck,
      note_ids: ids,
      flagged_count: (S.notes && S.notes.flagged) || 0,
      messages,
    };
    await streamChat(payload, (name, data) => {
      const d = data || {};
      if (name === "text") {
        reply.text += d.delta || "";
        scheduleLogRefresh();
      } else if (name === "proposal") {
        reply.proposals.push({
          id: d.id,
          kind: d.kind,
          input: d.input || {},
          applied: false,
          error: "",
        });
        scheduleLogRefresh();
      } else if (name === "error") {
        reply.error = d.detail || "erreur";
        scheduleLogRefresh();
      }
    });
  } catch (e) {
    reply.error = e.message;
  }
  reply.streaming = false;
  S.chatBusy = false;
  S.refocus = "chat";
  draw();
}

async function applyProposal(mi, pi) {
  const msg = S.chat[mi];
  const p = msg && msg.proposals && msg.proposals[pi];
  if (!p || p.applied || S.busy) return;
  const input = p.input || {};
  p.error = "";
  S.busy = true;
  draw();
  try {
    if (p.kind === "edit") {
      const body = { fields: input.fields || {}, unflag: true };
      if (input.tags) body.tags = input.tags;
      await API.patch(input.note_id, body);
    } else if (p.kind === "split") {
      await API.split(input.note_id, {
        original: input.original ? { fields: input.original.fields || {} } : null,
        new_notes: (input.new_notes || []).map((n) => ({
          model: n.model,
          fields: n.fields || {},
        })),
      });
    } else if (p.kind === "create") {
      const sel = selectedNote();
      await API.create({
        deck: S.deck,
        model: input.model || (sel && sel.model),
        fields: input.fields || {},
        tags: (sel && sel.tags) || [],
      });
    } else if (p.kind === "move") {
      await API.move(input.note_id, input.deck);
    } else {
      throw new Error("proposition inconnue : " + p.kind);
    }
    p.applied = true;
    S.busy = false;
    await afterDecision({ resolvedId: input.note_id, keepSelection: p.kind === "create" });
  } catch (e) {
    p.error = e.message;
    S.busy = false;
    draw();
  }
}

/* ---------------- delegated events ---------------- */

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (!el || el.closest("dialog")) return;
  const act = el.getAttribute("data-act");
  const noteId = el.getAttribute("data-note") ? Number(el.getAttribute("data-note")) : null;

  if (act === "deck") {
    selectDeck(el.getAttribute("data-deck"));
  } else if (act === "theme") {
    toggleTheme();
    draw();
  } else if (act === "alldecks") {
    S.showAllDecks = !S.showAllDecks;
    draw();
  } else if (act === "onlyflagged") {
    S.onlyFlagged = !S.onlyFlagged;
    if (!noteById(S.selNote)) selectFirstFlagged();
    draw();
  } else if (act === "note") {
    const cloze = e.target.closest(".cloze.hidden");
    if (cloze) toggleReveal(noteId);
    S.selNote = noteId;
    draw();
  } else if (act === "reveal") {
    e.stopPropagation();
    toggleReveal(noteId);
    draw();
  } else if (act === "decision") {
    e.stopPropagation();
    applyDecision(el.getAttribute("data-decision"), noteId);
  } else if (act === "ref") {
    e.stopPropagation();
    if (S.chatRefs.indexOf(noteId) < 0) S.chatRefs.push(noteId);
    S.tab = "chat";
    draw();
  } else if (act === "unref") {
    S.chatRefs = S.chatRefs.filter((id) => id !== noteId);
    draw();
  } else if (act === "tab") {
    S.tab = el.getAttribute("data-tab");
    draw();
  } else if (act === "dismiss-error") {
    S.error = "";
    draw();
  } else if (act === "expand-src") {
    const i = Number(el.getAttribute("data-i"));
    S.srcExpanded[i] = !S.srcExpanded[i];
    draw();
  } else if (act === "remove-src") {
    removeSource(Number(el.getAttribute("data-i")));
  } else if (act === "add-src") {
    S.srcForm = { kind: "obsidian", target: "", pages: "", note: "", copyInherited: true };
    S.refocus = "src-target";
    draw();
  } else if (act === "src-cancel") {
    S.srcForm = null;
    draw();
  } else if (act === "src-save") {
    saveNewSource();
  } else if (act === "send") {
    sendChat();
  } else if (act === "apply") {
    applyProposal(Number(el.getAttribute("data-mi")), Number(el.getAttribute("data-pi")));
  }
});

document.addEventListener("input", (e) => {
  const el = e.target.closest("[data-input]");
  if (!el || el.closest("dialog")) return;
  const key = el.getAttribute("data-input");
  if (key === "chat") {
    S.chatDraft = el.value;
    return;
  }
  if (!S.srcForm) return;
  if (key === "src-target") {
    S.srcForm.target = el.value;
    // kind auto-detected from the target: ends with .pdf → pdf, else obsidian
    S.srcForm.kind = /\.pdf\s*$/i.test(el.value) ? "pdf" : "obsidian";
    const sel = document.querySelector('[data-input="src-kind"]');
    if (sel) sel.value = S.srcForm.kind;
    if (S.srcForm.kind === "obsidian") lookupVaultNotes(el.value.trim());
  } else if (key === "src-kind") {
    S.srcForm.kind = el.value;
  } else if (key === "src-pages") {
    S.srcForm.pages = el.value;
  } else if (key === "src-note") {
    S.srcForm.note = el.value;
  } else if (key === "src-copy") {
    S.srcForm.copyInherited = el.checked;
  }
});

/* ---------------- keyboard ---------------- */

document.addEventListener("keydown", (e) => {
  const active = document.activeElement;
  const tag = active && active.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
    if (
      e.key === "Enter" &&
      (e.metaKey || e.ctrlKey) &&
      active.getAttribute("data-input") === "chat"
    ) {
      e.preventDefault();
      sendChat();
    }
    return; // every other key belongs to the field (Esc closes a dialog natively)
  }
  if (document.querySelector("dialog[open]")) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  if (e.key === "j" || e.key === "ArrowDown") {
    e.preventDefault();
    moveSelection(1);
  } else if (e.key === "k" || e.key === "ArrowUp") {
    e.preventDefault();
    moveSelection(-1);
  } else if (e.key === "g") {
    if (S.selNote) applyDecision("keep", S.selNote);
  } else if (e.key === "p") {
    if (S.selNote) applyDecision("skip", S.selNote);
  } else if (e.key === " ") {
    const sel = selectedNote();
    if (sel && hasHiddenClozes(sel)) {
      e.preventDefault();
      toggleReveal(sel.note_id);
      draw();
    }
  }
});

/* ---------------- boot ---------------- */

async function boot() {
  draw();
  await loadDecks();
  draw();
  API.models()
    .then((m) => {
      S.models = m || {};
    })
    .catch(() => {
      S.models = {};
    });
  API.chatStatus()
    .then((s) => {
      S.chatStatus = s || { configured: false };
      draw();
    })
    .catch(() => {
      S.chatStatus = { configured: false };
      draw();
    });
}

boot();
