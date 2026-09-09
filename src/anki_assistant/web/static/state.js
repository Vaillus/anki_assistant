/* In-memory state + small helpers shared by the other scripts.
   Loaded first; everything here is global on purpose (no bundler, no modules). */

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
  srcExpanded: {}, // index -> bool
  srcForm: null, // { kind, target, pages, note, copyInherited } while the add form is open
  // workspace (specs/workspace.md) — null when closed; see workspace.js for the shape
  ws: null,
  undoAvailable: false, // « Annuler la dernière validation » (GET /api/workspace/undo)
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

/* JS port of render.py::render_field — display only, never sent back to Anki. */
const _CTX_RE = /^\s*<div\s+class\s*=\s*"context"\s*>([\s\S]*?)<\/div\s*>/i;

function renderField(raw) {
  let t = String(raw === null || raw === undefined ? "" : raw);
  // Extract context header before stripping tags so we can re-wrap it.
  let ctx = "";
  const cm = _CTX_RE.exec(t);
  if (cm) {
    ctx = cm[1];
    t = t.slice(cm[0].length);
  }
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
  let out = t.replace(/\n/g, "<br>");
  if (ctx) {
    const ctxText = esc(unescapeHtml(ctx.replace(/<[^>]+>/g, "")).trim());
    out = `<span class="context">${ctxText}</span><br>${out}`;
  }
  return out;
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

function sourceById(id) {
  return (((S.corpus || {}).sources) || []).find((s) => s.id === id) || null;
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
