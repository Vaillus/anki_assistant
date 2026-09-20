/* Event wiring, queue decisions, keyboard, boot.
   Loaded last; source-tab data logic is in sources-tab.js. */

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

/* ---------------- create deck (specs/review.md#creating-a-deck) ---------------- */

async function submitNewDeck() {
  if (!S.newDeck) return;
  var name = (S.newDeck.value || "").trim();
  if (!name) { S.newDeck.error = "Nom vide"; draw(); return; }
  var exists = (S.decks || []).some(function (d) { return d.name === name; });
  if (exists) { S.newDeck.error = "Ce paquet existe déjà"; draw(); return; }
  S.busy = true;
  S.newDeck.error = "";
  draw();
  try {
    await API.createDeck(name);
    S.newDeck = null;
    S.busy = false;
    await loadDecks();
    S.showAllDecks = true;
    await selectDeck(name);
  } catch (e) {
    S.busy = false;
    if (e.status === 409) S.newDeck.error = "Ce paquet existe déjà";
    else S.newDeck.error = e.message;
    draw();
  }
}

async function deleteDeck(name) {
  if (!confirm("Supprimer le paquet « " + name + " » et toutes ses cartes ?")) return;
  S.busy = true;
  draw();
  try {
    await API.deleteDeck(name);
    if (S.deck === name) { S.deck = null; S.notes = null; S.corpus = null; }
    S.busy = false;
    await loadDecks();
    draw();
  } catch (e) {
    S.busy = false;
    S.error = e.message;
    draw();
  }
}

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
  } else if (act === "open-ws") {
    openWorkspace(null);
  } else if (act === "undo") {
    undoLastValidation();
  } else if (act === "dismiss-error") {
    S.error = "";
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
  } else if (act === "open-pdf") {
    API.openSourceFile(el.getAttribute("data-id")).catch(() => {});
  } else if (act === "new-deck") {
    S.newDeck = { value: "", error: "" };
    S.refocus = "new-deck";
    draw();
  }
});

document.addEventListener("input", (e) => {
  const el = e.target.closest("[data-input]");
  if (!el) return;
  const key = el.getAttribute("data-input");
  if (key === "theme") {
    setTheme(el.value);
    return;
  }
  if (key === "new-deck") {
    if (S.newDeck) S.newDeck.value = el.value;
    return;
  }
  if (S.ws) {
    wsInput(key, el);
    return;
  }
  if (!S.srcForm) return;
  if (key === "src-target") {
    S.srcForm.target = el.value;
    // Only auto-switch kind on a positive match (URL or .pdf); partial text like "attention"
    // must not force the kind back to obsidian when the user already picked pdf.
    const detected = detectKind(el.value);
    if (detected !== "obsidian" && detected !== S.srcForm.kind) {
      S.srcForm.kind = detected;
      const sel = document.querySelector('[data-input="src-kind"]');
      if (sel) sel.value = detected;
      S.refocus = "src-target";
      draw();
    }
    const kind = S.srcForm.kind;
    if (kind === "obsidian") lookupVaultNotes(el.value.trim());
    else if (kind === "pdf") lookupVaultPdfs(el.value.trim());
  } else if (key === "src-kind") {
    S.srcForm.kind = el.value;
    draw();
  } else if (key === "src-pages") {
    S.srcForm.pages = el.value;
  } else if (key === "src-note") {
    S.srcForm.note = el.value;
  }
});

/* A workspace field textarea losing focus goes back to its rendered form. */
document.addEventListener("focusout", (e) => {
  const el = e.target;
  if (el && el.getAttribute && el.getAttribute("data-input") === "ws-field") wsBlur(el);
});

/* ---------------- context menu (specs/review.md#creating-a-deck) ---------------- */

document.addEventListener("contextmenu", function (e) {
  var el = e.target.closest(".deck[data-deck]");
  if (!el || S.ws) return;
  e.preventDefault();
  // Remove any existing menu
  var old = document.getElementById("ctx-menu");
  if (old) old.remove();
  var deckName = el.getAttribute("data-deck");
  var menu = document.createElement("div");
  menu.id = "ctx-menu";
  menu.className = "ctx-menu";
  menu.innerHTML =
    '<div class="ctx-item" data-act="ctx-new-child" data-deck="' + esc(deckName) + '">' +
    "Nouveau sous-paquet</div>" +
    '<div class="ctx-item danger" data-act="ctx-delete-deck" data-deck="' + esc(deckName) + '">' +
    "Supprimer le paquet</div>";
  menu.style.left = e.clientX + "px";
  menu.style.top = e.clientY + "px";
  document.body.appendChild(menu);
  function dismiss() {
    menu.remove();
    document.removeEventListener("click", dismiss, true);
    document.removeEventListener("contextmenu", dismiss, true);
  }
  setTimeout(function () {
    document.addEventListener("click", dismiss, true);
    document.addEventListener("contextmenu", dismiss, true);
  }, 0);
  menu.addEventListener("click", function (ev) {
    var item = ev.target.closest("[data-act]");
    if (!item) return;
    dismiss();
    var act = item.getAttribute("data-act");
    var deck = item.getAttribute("data-deck");
    if (act === "ctx-new-child") {
      S.newDeck = { value: deck + "::", error: "" };
      S.refocus = "new-deck";
      draw();
    } else if (act === "ctx-delete-deck") {
      deleteDeck(deck);
    }
  });
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
      !e.shiftKey &&
      inField &&
      active.getAttribute("data-input") === "chat"
    ) {
      e.preventDefault();
      sendChat();
    }
    return;
  }

  if (inField && active.getAttribute("data-input") === "new-deck") {
    if (e.key === "Enter") { e.preventDefault(); submitNewDeck(); }
    else if (e.key === "Escape") { e.preventDefault(); S.newDeck = null; draw(); }
    return;
  }
  if (inField) return;
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
