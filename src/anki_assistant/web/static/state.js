/* In-memory state, selectors, and universal text helpers.
   Loaded first; everything here is global on purpose (no bundler, no modules).
   Display transforms (renderField, hideClozes, renderMarkdown…) live in display.js. */

"use strict";

/* ---------- theme (specs/review.md#theme) ---------- */

const THEME_KEY = "anki-theme"; // same key as the inline script in index.html

function isTheme(slug) {
  return THEMES.some((t) => t.slug === slug);
}

function storedTheme() {
  try {
    const t = localStorage.getItem(THEME_KEY);
    return isTheme(t) ? t : null;
  } catch (e) {
    return null; // private mode: the picker works for this session only
  }
}

function systemMode() {
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

/* The theme actually painted: the user's stored choice if any, else the default for
   whichever of light/dark the OS asks for. */
function currentTheme() {
  return S.theme || DEFAULT_THEME[systemMode()];
}

function applyTheme() {
  document.documentElement.setAttribute("data-theme", currentTheme());
}

function setTheme(slug) {
  S.theme = isTheme(slug) ? slug : null;
  try {
    if (S.theme) localStorage.setItem(THEME_KEY, S.theme);
    else localStorage.removeItem(THEME_KEY);
  } catch (e) {
    /* not stored, still applied */
  }
  applyTheme();
}

/* Follow the OS while the user has expressed no preference of their own. */
if (window.matchMedia) {
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const onChange = () => {
    if (!S.theme) {
      applyTheme();
      draw();
    }
  };
  if (mq.addEventListener) mq.addEventListener("change", onChange);
  else if (mq.addListener) mq.addListener(onChange);
}

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
  // column 3 (Source)
  corpus: null, // { deck, inherited_from, sources: [] }
  corpusLoading: false,
  srcForm: null, // { kind, target, pages, note, copyInherited } while the add form is open
  // workspace (specs/workspace.md) — null when closed; see workspace.js for the shape
  ws: null,
  undoAvailable: false, // « Undo last validation » (GET /api/workspace/undo)
  chatStatus: null, // { configured, model }
  // misc
  models: null, // { modelName: [fieldNames] }
  refocus: null,
  theme: storedTheme(), // an Omarchy slug once chosen; null = follow the OS's light/dark
};

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

/* ---------- question state ---------- */

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

function sourceById(id) {
  return (((S.corpus || {}).sources) || []).find((s) => s.id === id) || null;
}

/* Source kinds and their auto-detection from a target (specs/sources.md#source-entry). */
const SOURCE_KINDS = ["obsidian", "pdf", "web"];

function detectKind(target) {
  const t = String(target || "").trim();
  if (/^https?:\/\/\S+$/i.test(t)) return "web";
  return /\.pdf$/i.test(t) ? "pdf" : "obsidian";
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
