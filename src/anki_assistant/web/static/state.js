/* In-memory state + small helpers shared by the other scripts.
   Loaded first; everything here is global on purpose (no bundler, no modules). */

"use strict";

const S = {
  // column 1
  decks: [],
  deck: null,
  showAllDecks: false,
  // column 2
  notes: null, // { deck, total, flagged, notes: Note[] }
  onlyFlagged: true,
  selNote: null,
  revealed: {}, // note id -> true once its flagged clozes are shown (specs/review.md#question-state)
  busy: false,
  error: "",
  // column 3
  tab: "source",
  corpus: null, // { deck, inherited_from, sources: [] }
  corpusLoading: false,
  srcExpanded: {}, // index -> bool
  srcForm: null, // { kind, target, pages, note, copyInherited } while the add form is open
  // chat
  chat: [], // [{ who: "user"|"assistant", text, refs?: [], proposals?: [], streaming?: bool }]
  chatRefs: [], // note ids attached with « → chat »
  chatDraft: "",
  chatStatus: null, // { configured, model }
  chatBusy: false,
  // misc
  models: null, // { modelName: [fieldNames] }
  refocus: null,
};

const ACTIONS = [
  { key: "keep", label: "Garder", cls: "primary" },
  { key: "edit", label: "Modifier", cls: "" },
  { key: "split", label: "Splitter", cls: "" },
  { key: "create", label: "Créer", cls: "" },
  { key: "move", label: "Déplacer", cls: "" },
  { key: "delete", label: "Supprimer", cls: "danger" },
  { key: "skip", label: "Passer", cls: "" },
];

const REASON_FIELD = "Back Extra";

/* ---------- text helpers ---------- */

function esc(s) {
  return String(s === null || s === undefined ? "" : s).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;" })[c],
  );
}

const _unescBox = document.createElement("textarea");
function unescapeHtml(s) {
  _unescBox.innerHTML = String(s === null || s === undefined ? "" : s);
  return _unescBox.value;
}

/* JS port of render.py::render_field — display only, never sent back to Anki. */
function renderField(raw) {
  let t = String(raw === null || raw === undefined ? "" : raw);
  t = t.replace(/<br\s*\/?>/gi, "\n");
  t = t.replace(/<\/(p|div|li)\s*>/gi, "\n");
  t = t.replace(/<img\b[^>]*>/gi, (m) => {
    const alt = /alt\s*=\s*"([^"]*)"/i.exec(m) || /alt\s*=\s*'([^']*)'/i.exec(m);
    return alt ? alt[1] : "[image]";
  });
  t = t.replace(/<[^>]+>/g, "");
  t = unescapeHtml(t);
  t = esc(t.trim());
  t = t.replace(
    /\{\{c(\d+)::([\s\S]*?)(?:::([\s\S]*?))?\}\}/g,
    (_m, n, answer, hint) =>
      `<span class="cloze" data-n="${n}"${hint ? ` data-hint="${hint}"` : ""}>${answer}</span>`,
  );
  return t.replace(/\n/g, "<br>");
}

/* ---------- question state (specs/review.md#question-state) ---------- */

/* Cloze numbers of the flagged cards: card `ord` is the card hiding cloze c{ord+1}. */
function flaggedClozes(n) {
  return ((n && n.flagged_cards) || []).map((c) => (c.ord || 0) + 1);
}

const _CLOZE_SPAN = /<span class="cloze" data-n="(\d+)"( data-hint="([^"]*)")?>[\s\S]*?<\/span>/g;

/* Rendered field HTML with the given clozes replaced by `[…]` / `[hint]`, as on Anki's question side. */
function hideClozes(html, ns) {
  return String(html || "").replace(_CLOZE_SPAN, (m, n, _attr, hint) => {
    if (ns.indexOf(Number(n)) < 0) return m;
    return `<span class="cloze hidden" data-n="${n}">[${hint || "…"}]</span>`;
  });
}

/* True when the note has something to hide: flagged, and a flagged cloze exists in its fields. */
function hasHiddenClozes(n) {
  if (!n || !n.flagged) return false;
  const ns = flaggedClozes(n);
  if (!ns.length) return false;
  const html = Object.values(n.fields_html || {}).join("");
  return ns.some((k) => html.indexOf(`<span class="cloze" data-n="${k}"`) >= 0);
}

function isHidden(n) {
  return hasHiddenClozes(n) && !S.revealed[n.note_id];
}

function toggleReveal(noteId) {
  if (S.revealed[noteId]) delete S.revealed[noteId];
  else S.revealed[noteId] = true;
}

/* Plain text (no markup at all), used for the reason callout and previews. */
function plainText(raw) {
  return unescapeHtml(
    String(raw === null || raw === undefined ? "" : raw)
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<\/(p|div|li)\s*>/gi, "\n")
      .replace(/<[^>]+>/g, ""),
  ).trim();
}

const short = (id) => "#" + String(id).slice(-4);

function nl2br(s) {
  return esc(s).replace(/\n/g, "<br>");
}

function fold(text, limit) {
  const t = String(text || "");
  return t.length > limit ? t.slice(0, limit) : t;
}

/* ---------- state selectors ---------- */

function visibleNotes() {
  const all = (S.notes && S.notes.notes) || [];
  return S.onlyFlagged ? all.filter((n) => n.flagged) : all;
}

function noteById(id) {
  return ((S.notes && S.notes.notes) || []).find((n) => n.note_id === id) || null;
}

function selectedNote() {
  return S.selNote ? noteById(S.selNote) : null;
}

function visibleDecks() {
  const all = S.decks || [];
  return S.showAllDecks ? all : all.filter((d) => (d.flagged_total || 0) > 0);
}

function nCards(n) {
  return (n && n.card_ids && n.card_ids.length) || 0;
}

function fieldNames(model) {
  return (S.models && S.models[model]) || [];
}

function tagsToString(tags) {
  return (tags || []).join(" ");
}

function parseTags(str) {
  return String(str || "")
    .split(/[\s,]+/)
    .map((t) => t.trim())
    .filter(Boolean);
}

function debounce(fn, ms) {
  let t = null;
  return function (...args) {
    clearTimeout(t);
    t = setTimeout(() => fn.apply(null, args), ms);
  };
}
