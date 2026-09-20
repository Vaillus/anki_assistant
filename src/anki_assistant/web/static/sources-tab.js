/* Source-tab data logic: corpus CRUD, vault autocompletion.
   Loaded after api.js; before app.js (which wires the events that call these). */

"use strict";

function entryOf(s) {
  const e = { kind: s.kind, target: s.target };
  if (s.id) e.id = s.id;
  if (s.pages) e.pages = s.pages;
  if (s.note) e.note = s.note;
  return e;
}

/** Own sources only (on_deck === selected deck). Inherited sources are not sent in PUT. */
function ownEntries() {
  return (((S.corpus || {}).sources) || [])
    .filter((s) => s.on_deck === S.deck)
    .map(entryOf);
}

async function saveSources(entries) {
  await API.putSources(S.deck, entries);
  await loadCorpus();
  await loadDecks();
  draw();
}

async function removeSource(i) {
  const sources = ((S.corpus || {}).sources) || [];
  const src = sources[i];
  if (!src) return;
  if (src.on_deck !== S.deck) return; // inherited — cannot remove from here
  const entries = ownEntries().filter((e) => e.id !== src.id);
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
    f.error = "Provide a target.";
    draw();
    return;
  }
  const base = ownEntries();
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

const lookupVaultPdfs = debounce(async (query) => {
  if (!query || query.length < 2) return;
  try {
    const paths = await API.vaultPdfs(query);
    const dl = document.getElementById("vault-pdfs");
    if (!dl) return;
    dl.innerHTML = (paths || [])
      .slice(0, 50)
      .map((p) => '<option value="' + esc(p) + '"></option>')
      .join("");
  } catch (e) {
    /* autocomplete is best-effort */
  }
}, 250);
