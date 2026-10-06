/* Display transforms: field rendering, cloze hiding, Markdown + math, text helpers.
   Loaded after state.js (needs esc, unescapeHtml); before render.js and ws-render.js. */

"use strict";

/* JS port of render.py::render_field — display only, never sent back to Anki. */
const _CTX_RE = /^\s*<div\s+class\s*=\s*"context"\s*>([\s\S]*?)<\/div\s*>/i;
/* A pasted picture is held out of the flatten/escape steps as this marker (NUL is stripped from
   the input, so a note cannot forge it), then swapped for an <img> written here. */
const _PIC_RE = /\u0000(\d+)\u0000/g;

function _imgAttr(tag, name) {
  const re = new RegExp("\\b" + name + "\\s*=\\s*(?:\"([^\"]*)\"|'([^']*)'|([^\\s\">]+))", "i");
  const m = re.exec(tag);
  if (!m) return "";
  return m[1] !== undefined ? m[1] : m[2] !== undefined ? m[2] : m[3];
}

/* `src` when it is a bare file name in Anki's media folder, else null. */
function _mediaName(src) {
  const s = unescapeHtml(src).trim();
  if (!s || s.includes("/") || s.includes("\\") || s === "." || s === "..") return null;
  return s;
}

function _pictureTag(name) {
  return '<img src="/api/media/' + esc(encodeURIComponent(name)) + '" alt="" class="field-img">';
}

function renderField(raw) {
  let t = String(raw === null || raw === undefined ? "" : raw).replace(/\u0000/g, "");
  // Extract context header before stripping tags so we can re-wrap it.
  let ctx = "";
  const cm = _CTX_RE.exec(t);
  if (cm) {
    ctx = cm[1];
    t = t.slice(cm[0].length);
  }
  t = t.replace(/<br\s*\/?>/gi, "\n");
  t = t.replace(/<\/(p|div|li)\s*>/gi, "\n");
  const pictures = [];
  t = t.replace(/<img\b[^>]*>/gi, (m) => {
    const alt = _imgAttr(m, "alt");
    if (alt.trim()) return alt;
    const name = _mediaName(_imgAttr(m, "src"));
    if (name === null) return "[image]";
    pictures.push(name);
    return "\u0000" + (pictures.length - 1) + "\u0000";
  });
  t = t.replace(/<[^>]+>/g, "");
  t = unescapeHtml(t);
  t = esc(t.trim());
  t = t.replace(/\{\{c(\d+)::([\s\S]*?)(?:::([\s\S]*?))?\}\}/g, (_m, n, answer, hint) => {
    // A picture cannot sit inside an attribute.
    const h = hint ? hint.replace(_PIC_RE, "[image]") : "";
    return `<span class="cloze" data-n="${n}"${h ? ` data-hint="${h}"` : ""}>${answer}</span>`;
  });
  t = t.replace(_PIC_RE, (_m, i) => _pictureTag(pictures[Number(i)]));
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
