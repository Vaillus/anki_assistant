/* Display transforms: field rendering, cloze hiding, Markdown + math, text helpers.
   Loaded after state.js (needs esc, unescapeHtml); before render.js and ws-render.js. */

"use strict";

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

/* ---------- text helpers ---------- */

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

function typesetMath() {
  if (window.MathJax && MathJax.typesetPromise) {
    MathJax.typesetClear && MathJax.typesetClear();
    MathJax.typesetPromise().catch(() => {});
  }
}

/* ---------- chat markdown + math ---------- */

/* marked configuration (runs once, on first call). */
let _markedReady = false;
function ensureMarked() {
  if (_markedReady || !window.marked) return;
  _markedReady = true;
  marked.use({ breaks: true, gfm: true });
}

/* Render Markdown with LaTeX math protection for assistant chat messages.
   Math expressions are extracted before Markdown processing and restored after,
   with dollar-sign delimiters converted to backslash ones that MathJax recognizes. */
function renderMarkdown(text) {
  if (!window.marked) return linkify(text);
  ensureMarked();

  var placeholders = [];
  var src = String(text || "");

  function protect(re, display) {
    src = src.replace(re, function (m) {
      var i = placeholders.length;
      placeholders.push({ text: m, display: display });
      return display
        ? "\n\n<div data-mph=\"" + i + "\"></div>\n\n"
        : "<span data-mph=\"" + i + "\"></span>";
    });
  }

  // Display math first (greedy), then inline — order matters.
  protect(/\$\$([\s\S]+?)\$\$/g, true);
  protect(/\\\[[\s\S]+?\\\]/g, true);
  protect(/\[\$\$\][\s\S]+?\[\/\$\$\]/g, true);
  protect(/(?<![\\$])\$(?!\$|\s)([^$]+?)(?<!\s)\$/g, false);
  protect(/\\\([\s\S]+?\\\)/g, false);
  protect(/\[\$\][\s\S]+?\[\/\$\]/g, false);

  var html = marked.parse(src);

  // Restore math, normalising dollar delimiters to backslash ones.
  html = html.replace(/<(?:div|span) data-mph="(\d+)"><\/(?:div|span)>/g, function (_, id) {
    var p = placeholders[Number(id)];
    var m = p.text;
    if (m.startsWith("$$") && m.endsWith("$$")) return "\\[" + m.slice(2, -2) + "\\]";
    if (m.startsWith("$") && m.endsWith("$")) return "\\(" + m.slice(1, -1) + "\\)";
    return m;
  });

  // External links open in a new tab (citation <a> tags already have target="_blank").
  html = html.replace(/<a href="(https?:\/\/[^"]*)"(?![^>]*target=)/g,
    '<a href="$1" target="_blank" rel="noopener"');

  return html;
}
