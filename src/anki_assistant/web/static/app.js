/* Event wiring, queue decisions, keyboard, boot.
   Loaded last: state.js → api.js → render.js → workspace.js → app.js. */

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
  if (S.deck === name || S.ws) return;
  S.deck = name;
  S.notes = null;
  S.selNote = null;
  S.revealed = {};
  S.corpus = null;
  S.srcForm = null;
  S.srcExpanded = {};
  S.error = "";
  draw();
  await Promise.all([loadNotes(), loadCorpus()]);
}

async function refreshUndoStatus() {
  try {
    const s = await API.undoStatus();
    S.undoAvailable = !!(s && s.available);
  } catch (e) {
    S.undoAvailable = false;
  }
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

/* ---------------- decisions (specs/review.md#decisions) ---------------- */

/* Called after every write and after a workspace closes: re-fetch the queue and the deck
   counts, then move on to the next flagged note. */
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

/* Garder: the flag was a false alarm; clear it, change nothing else. */
async function keepNote(noteId) {
  const note = noteById(noteId);
  if (!note || S.busy || S.ws) return;
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
  const entry = { kind: SOURCE_KINDS.indexOf(f.kind) >= 0 ? f.kind : detectKind(target), target };
  if (entry.kind === "pdf" && String(f.pages || "").trim()) entry.pages = String(f.pages).trim();
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

/* ---------------- delegated events ---------------- */

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const act = el.getAttribute("data-act");
  const noteId = el.getAttribute("data-note") ? Number(el.getAttribute("data-note")) : null;

  // Everything the workspace owns, plus the source chips it shares with nobody now.
  if (act.indexOf("ws-") === 0 || act === "attach-src" || act === "detach-src") {
    wsClick(act, el, e);
    return;
  }
  if (S.ws) return; // the page behind the overlay is inert

  if (act === "deck") {
    selectDeck(el.getAttribute("data-deck"));
  } else if (act === "alldecks") {
    S.showAllDecks = !S.showAllDecks;
    draw();
  } else if (act === "onlyflagged") {
    S.onlyFlagged = !S.onlyFlagged;
    if (!noteById(S.selNote)) selectFirstFlagged();
    draw();
  } else if (act === "note") {
    // A hidden cloze reveals; anything else opens the workspace on the note.
    const cloze = e.target.closest(".cloze.hidden");
    S.selNote = noteId;
    if (cloze) {
      toggleReveal(noteId);
      draw();
    } else {
      openWorkspace(noteId);
    }
  } else if (act === "reveal") {
    e.stopPropagation();
    toggleReveal(noteId);
    draw();
  } else if (act === "keep") {
    e.stopPropagation();
    keepNote(noteId);
  } else if (act === "undo") {
    undoLastValidation();
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
  }
});

document.addEventListener("input", (e) => {
  const el = e.target.closest("[data-input]");
  if (!el) return;
  const key = el.getAttribute("data-input");
  if (key === "theme") {
    setTheme(el.value); // reachable from behind the overlay, so it comes before the guards
    return;
  }
  if (S.ws) {
    wsInput(key, el);
    return;
  }
  if (!S.srcForm) return;
  if (key === "src-target") {
    S.srcForm.target = el.value;
    // kind auto-detected from the target (specs/sources.md#source-entry)
    const kind = detectKind(el.value);
    const changed = kind !== S.srcForm.kind;
    S.srcForm.kind = kind;
    const sel = document.querySelector('[data-input="src-kind"]');
    if (sel) sel.value = kind;
    if (changed) {
      // the pages field only exists for a pdf: redraw, keeping the caret in the target
      S.refocus = "src-target";
      draw();
    }
    if (kind === "obsidian") lookupVaultNotes(el.value.trim());
  } else if (key === "src-kind") {
    S.srcForm.kind = el.value;
    draw();
  } else if (key === "src-pages") {
    S.srcForm.pages = el.value;
  } else if (key === "src-note") {
    S.srcForm.note = el.value;
  } else if (key === "src-copy") {
    S.srcForm.copyInherited = el.checked;
  }
});

/* A workspace field textarea losing focus goes back to its rendered form. */
document.addEventListener("focusout", (e) => {
  const el = e.target;
  if (el && el.getAttribute && el.getAttribute("data-input") === "ws-field") wsBlur(el);
});

/* ---------------- keyboard ---------------- */

document.addEventListener("keydown", (e) => {
  const active = document.activeElement;
  const tag = active && active.tagName;
  const inField = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";

  if (S.ws) {
    // specs/workspace.md#keyboard
    if (e.key === "Escape") {
      e.preventDefault();
      if (inField) active.blur(); // a field first gives the focus back, a second Esc closes
      else closeWorkspace(false);
      return;
    }
    if (
      e.key === "Enter" &&
      (e.metaKey || e.ctrlKey) &&
      inField &&
      active.getAttribute("data-input") === "chat"
    ) {
      e.preventDefault();
      sendChat();
    }
    return;
  }

  if (inField) return; // every key belongs to the field
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  if (e.key === "j" || e.key === "ArrowDown") {
    e.preventDefault();
    moveSelection(1);
  } else if (e.key === "k" || e.key === "ArrowUp") {
    e.preventDefault();
    moveSelection(-1);
  } else if (e.key === "g") {
    if (S.selNote) keepNote(S.selNote);
  } else if (e.key === "p") {
    moveSelection(1);
  } else if (e.key === "Enter") {
    if (S.selNote) {
      e.preventDefault();
      openWorkspace(S.selNote);
    }
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
  refreshUndoStatus().then(draw);
}

boot();
