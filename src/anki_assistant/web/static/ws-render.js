/* Workspace overlay and chat pane HTML rendering (specs/workspace.md, specs/chat.md).
   Loaded after ws-state.js, before workspace.js. All globals — no bundler. */

"use strict";

/* ---------------- workspace overlay ---------------- */

function wsOverlay() {
  if (!S.ws) return "";
  return (
    '<div class="ws-backdrop"></div>' +
    '<div class="ws" role="dialog" aria-modal="true">' +
    '<div class="ws-cards" data-scroll="ws-cards">' +
    wsHeadHtml() +
    wsReportHtml() +
    wsCardsHtml() +
    "</div>" +
    '<div class="ws-chat">' +
    chatPane() +
    "</div></div>"
  );
}

function wsHeadHtml() {
  const ws = S.ws;
  const root = wsRoot();
  const ch = wsChanges();
  const parts = [];
  if (ch.edited) parts.push(ch.edited + " edited");
  if (ch.created) parts.push(ch.created + " created");
  if (ch.deleted) parts.push(ch.deleted + " deleted");
  if (ch.kept) parts.push(ch.kept + " kept");
  if (ch.deferred) parts.push(ch.deferred + " deferred");
  if (ch.moved) parts.push(ch.moved + " moved");
  const disabled = !ch.total || ws.applying || ws.chatBusy ? " disabled" : "";
  return (
    '<div class="ws-head">' +
    "<b>" +
    (root && root.noteId ? esc(short(root.noteId)) : "workspace") +
    "</b>" +
    '<span class="tag">' +
    esc(ws.deck) +
    "</span>" +
    '<span class="grow"></span>' +
    '<label class="check small"><input type="checkbox" data-input="ws-clear-reason"' +
    (ws.clearReason ? " checked" : "") +
    "> clear Back Extra</label>" +
    '<button class="primary" data-act="ws-validate"' +
    disabled +
    ">" +
    (ws.applying ? "Applying…" : "Apply" + (parts.length ? " · " + parts.join(" · ") : "")) +
    "</button>" +
    '<button class="ghost icon" data-act="ws-close" title="Close (Esc)" aria-label="Close">×</button>' +
    "</div>"
  );
}

function wsReportHtml() {
  const r = S.ws.report;
  if (!r) return "";
  return (
    '<div class="banner"><div class="grow">' +
    "<b>Validation failed.</b> " +
    (r.nothing_written
      ? "Nothing was written."
      : r.rolled_back
        ? "Everything was rolled back."
        : "Some writes could not be rolled back.") +
    (r.errors || []).map((x) => "<br>" + esc(x)).join("") +
    '</div><button class="ghost" data-act="ws-dismiss-report">×</button></div>'
  );
}

/* Root first, then arrival order; each fragment right after its parent,
   recursively so that a fragment of a fragment nests under it (option A).
   Each entry carries `lastChild` so the vertical connector knows when to stop. */
function wsOrderedCards() {
  const cards = S.ws.cards;
  const out = [];
  const byParent = {};
  cards.forEach((c) => {
    if (c.parentWid && wsCard(c.parentWid)) (byParent[c.parentWid] = byParent[c.parentWid] || []).push(c);
  });
  function emit(wid, depth) {
    const children = byParent[wid] || [];
    children.forEach((f, i) => {
      out.push({ card: f, fragment: true, depth: depth, lastChild: i === children.length - 1 });
      emit(f.wid, depth + 1);
    });
  }
  cards.forEach((c) => {
    if (c.parentWid && wsCard(c.parentWid)) return;
    out.push({ card: c, fragment: false, depth: 0, lastChild: false });
    emit(c.wid, 1);
  });
  return out;
}

function wsCardsHtml() {
  const ordered = wsOrderedCards();
  return ordered
    .map((x, idx) => {
      const lines = [];
      if (x.fragment) {
        for (let d = 1; d <= x.depth; d++) {
          let continues = false;
          for (let j = idx + 1; j < ordered.length; j++) {
            if (ordered[j].depth < d) break;
            if (ordered[j].depth === d) { continues = true; break; }
          }
          const tap = d === x.depth;
          if (tap || continues) {
            lines.push({ depth: d, continues: continues, tap: tap });
          }
        }
      }
      return wsCardHtml(x.card, x.fragment, x.depth, lines);
    })
    .join("");
}

function wsCardHtml(c, isFragment, depth, lines) {
  const v = shownVersion(c);
  const n = c.versions.length;
  const cls =
    "ws-card" +
    (c.active ? "" : " off") +
    (c.deleted ? " deleted" : "") +
    (isFragment ? " fragment" : "") +
    (c.noteId ? "" : " draft");
  const indentPx = isFragment ? depth * 32 : 0;
  const plan = planCard(c);
  const badges =
    (c.deleted ? "" : flagToggleHtml(c)) +
    (c.deleted ? '<span class="badge state">deleted</span>' : "") +
    (plan && plan.action === "keep" ? '<span class="badge state ok">kept</span>' : "") +
    (c.moveTo ? '<span class="badge state">→ ' + esc(c.moveTo) + "</span>" : "");
  const versions =
    n > 1
      ? '<span class="ws-versions">' +
        '<button class="ghost" data-act="ws-prev" data-wid="' + c.wid + '"' + (c.vi === 0 ? " disabled" : "") + ">←</button>" +
        '<span class="tag">v' + versionNumber(c, c.vi) + " / " + versionNumber(c, n - 1) + "</span>" +
        '<button class="ghost" data-act="ws-next" data-wid="' + c.wid + '"' + (c.vi === n - 1 ? " disabled" : "") + ">→</button>" +
        "</span>"
      : "";
  const canInvalidate = !(c.noteId && c.vi === 0);
  const splitBtn = !c.deleted
    ? '<button class="ghost small" data-act="ws-split" data-wid="' + c.wid + '" title="ask Claude to split this card">✂</button>'
    : "";
  const actions =
    (canInvalidate
      ? '<button class="ghost small" data-act="ws-invalidate" data-wid="' + c.wid + '" title="discard this version">discard</button>'
      : "") +
    (c.noteId
      ? '<button class="ghost small' + (c.deleted ? "" : " dangerish") + '" data-act="ws-delete" data-wid="' + c.wid + '">' +
        (c.deleted ? "restore" : "delete") +
        "</button>"
      : "") +
    (c.noteId && !c.deleted ? movePickerHtml(c) : "") +
    '<button class="ghost small" data-act="ws-remove" data-wid="' + c.wid + '" title="remove this card from the workspace">remove</button>' +
    splitBtn;
  const effectiveModel = shownModel(c);
  const modelChanged = c.noteId && effectiveModel !== c.model;
  const parentLabel = isFragment && c.parentWid ? " · fragment of " + c.parentWid : "";
  const identity =
    '<span class="tag">' +
    (c.noteId ? esc(short(c.noteId)) : "draft") +
    " · " +
    esc(effectiveModel || "") +
    (modelChanged ? ' <b title="type changed (was ' + esc(c.model) + ')">⇄</b>' : "") +
    (c.deck && c.deck !== S.ws.deck ? " · " + esc(c.deck) : "") +
    (c.tags.length ? " · " + esc(c.tags.join(" ")) : "") +
    esc(parentLabel) +
    "</span>";
  const meta =
    v.by === "claude" || v.by === "user" || v.edited
      ? '<div class="ws-version">' +
        (v.by === "claude" ? "proposed by Claude" : "edited version") +
        (v.by === "claude" && v.edited ? " · edited" : "") +
        (v.rationale ? " — " + esc(v.rationale) : "") +
        "</div>"
      : "";
  var connectorHtml = "";
  if (lines && lines.length) {
    lines.forEach(function (ln) {
      var leftPx = (ln.depth - 1) * 32 + 12 - indentPx;
      var vCls = "ws-tree-vert" + (ln.continues ? " cont" : "") + (ln.tap ? " end" : "");
      connectorHtml += '<span class="' + vCls + '" style="left:' + leftPx + 'px"></span>';
      if (ln.tap) {
        connectorHtml += '<span class="ws-tree-tap" style="left:' + leftPx + 'px"></span>';
      }
    });
  }

  return (
    '<div class="' + cls + '" data-wid="' + c.wid + '"' + (indentPx ? ' style="margin-left:' + indentPx + 'px"' : "") + ">" +
    connectorHtml +
    '<div class="ws-card-head" data-act="ws-toggle" data-wid="' + c.wid + '" title="' +
    (c.active ? "active — click to exclude from next message" : "inactive — click to include") +
    '">' +
    '<span class="ws-dot' + (c.active ? " on" : "") + '"></span>' +
    '<span class="ws-wid">' + esc(c.wid) + "</span>" +
    identity +
    badges +
    '<span class="grow"></span>' +
    versions +
    actions +
    "</div>" +
    meta +
    wsFieldsHtml(c) +
    (c.flag.on && !c.deleted
      ? wsCommentHtml(c)
      : c.noteId && c.reason
        ? '<div class="reason"><span class="reason-label">flag reason' + clozeLabels(c) + "</span>" + nl2br(c.reason) + "</div>"
        : !c.noteId && plainText(shownFields(c)[REASON_FIELD] || "")
          ? '<div class="reason"><span class="reason-label">' + esc(REASON_FIELD) + "</span>" + nl2br(plainText(shownFields(c)[REASON_FIELD])) + "</div>"
          : "") +
    wsRevealHtml(c) +
    "</div>"
  );
}

/* « · c2 » — the clozes that carried the flag in Anki, for the callout's label. */
function clozeLabels(c) {
  return c.flaggedClozes.length ? " · " + c.flaggedClozes.map((k) => "c" + k).join(" ") : "";
}

/* The ⚑ toggle of the card head (specs/workspace.md#card-head). */
function flagToggleHtml(c) {
  let title;
  if (c.flag.on) title = "will stay flagged on apply — click to resolve";
  else if (c.ankiFlagged) title = "flag will be cleared on apply — click to keep it";
  else title = "not flagged — click to flag for later review";
  return (
    '<button class="badge flag-toggle' + (c.flag.on ? " on" : "") + '" data-act="ws-flag" data-wid="' + c.wid +
    '" title="' + title + '" aria-pressed="' + (c.flag.on ? "true" : "false") + '">⚑' +
    (c.flag.on ? " to review" : "") +
    "</button>"
  );
}

/* The reason callout of a card whose flag is on: the comment to be written
   (specs/workspace.md#body). */
function wsCommentHtml(c) {
  if (!hasReasonField(c)) {
    return (
      '<div class="reason flagged"><span class="reason-label">to review' + clozeLabels(c) + "</span>" +
      "no " + esc(REASON_FIELD) + " field: the flag will be set without a comment</div>"
    );
  }
  return (
    '<div class="reason flagged"><span class="reason-label">flag reason' + clozeLabels(c) + " · will be written</span>" +
    '<textarea data-input="ws-comment" data-wid="' + c.wid + '" data-focus="ws-comment-' + c.wid + '" rows="2"' +
    ' placeholder="why this note needs further review">' + esc(c.flag.comment) + "</textarea></div>"
  );
}

function movePickerHtml(c) {
  const decks = (S.decks || []).map((d) => d.name);
  return (
    '<select class="ws-move small" data-input="ws-move" data-wid="' + c.wid + '" title="move to another deck">' +
    '<option value=""' + (c.moveTo ? "" : " selected") + ">" + (c.moveTo ? "don't move" : "move…") + "</option>" +
    decks
      .filter((d) => d !== c.deck)
      .map((d) => '<option value="' + esc(d) + '"' + (c.moveTo === d ? " selected" : "") + ">" + esc(d) + "</option>")
      .join("") +
    "</select>"
  );
}

/* v0 of a flagged note is shown in question state (specs/workspace.md#body). */
function wsHidden(c) {
  if (!c.noteId || c.vi !== 0 || c.revealed || !c.flaggedClozes.length) return false;
  const html = Object.values(shownFields(c)).map(renderField).join("");
  return c.flaggedClozes.some((k) => html.indexOf('<span class="cloze" data-n="' + k + '"') >= 0);
}

/* « Back Extra » is skipped: the reason callout is the only place it appears. */
function wsFieldsHtml(c) {
  const fields = shownFields(c);
  const hidden = wsHidden(c);
  return Object.keys(fields)
    .map((name) => {
      if (name === REASON_FIELD) return "";
      const raw = fields[name];
      let body;
      if (c.editing === name) {
        body =
          '<textarea class="ws-edit" data-input="ws-field" data-focus="ws-edit" data-wid="' + c.wid + '" data-field="' + esc(name) + '" spellcheck="false">' +
          esc(raw) +
          "</textarea>";
      } else {
        let html = renderField(raw);
        if (hidden) html = hideClozes(html, c.flaggedClozes);
        body =
          '<div class="field-val' + (c.deleted ? "" : " editable") + '" data-act="ws-edit" data-wid="' + c.wid + '" data-field="' + esc(name) + '" title="click to edit raw value">' +
          (html || '<span class="muted">(empty)</span>') +
          "</div>";
      }
      return '<div class="field-name">' + esc(name) + "</div>" + body;
    })
    .join("");
}

function wsRevealHtml(c) {
  if (!c.noteId || c.vi !== 0 || !c.flaggedClozes.length) return "";
  const html = Object.values(shownFields(c)).map(renderField).join("");
  if (!c.flaggedClozes.some((k) => html.indexOf('<span class="cloze" data-n="' + k + '"') >= 0)) return "";
  return (
    '<div class="reveal"><button class="ghost" data-act="ws-reveal" data-wid="' + c.wid + '">' +
    (c.revealed ? "Hide again" : "Reveal answer") +
    "</button>" +
    (c.revealed ? "" : '<span class="muted small">the note as seen when flagged</span>') +
    "</div>"
  );
}

/* ---------------- conversation pane ---------------- */

function chatPane() {
  const ws = S.ws;
  const configured = !S.chatStatus || S.chatStatus.configured !== false;
  const box = configured
    ? '<div class="chips">' +
      sourceChipsHtml() +
      "</div>" +
      '<textarea data-input="chat" data-focus="chat" placeholder="Ask for a rewrite, a split, a fact-check… (Enter to send, Shift+Enter for a line break)"' +
      (ws.chatBusy ? " disabled" : "") +
      ">" +
      esc(ws.chatDraft) +
      "</textarea>" +
      '<div class="row" style="justify-content:flex-end;margin-top:4px">' +
      (S.chatStatus && S.chatStatus.model
        ? '<span class="tag grow">' + esc(S.chatStatus.model) + "</span>"
        : '<span class="grow"></span>') +
      '<button class="primary" data-act="ws-send"' +
      (ws.chatBusy ? " disabled" : "") +
      ">Send</button></div>"
    : '<div class="banner">Add ANTHROPIC_API_KEY to .env and restart anki-web</div>' +
      '<textarea disabled placeholder="chat unavailable"></textarea>';
  return (
    '<div class="chat">' +
    '<div class="log" id="chat-log" data-scroll="chat">' +
    chatLogHtml() +
    "</div>" +
    '<div class="box">' +
    box +
    "</div></div>"
  );
}

/* Attached sources as chips with ×, the root's anchors as « ⚓ joindre … », the rest of the
   corpus under « + source » (specs/chat.md#how-source-text-enters-context). */
function sourceChipsHtml() {
  const sources = ((S.corpus || {}).sources) || [];
  if (!sources.length) return '<span class="muted small">no sources in the corpus</span>';
  const attached = S.ws.chatSources.filter((id) => sourceById(id));
  const root = wsRoot();
  const anchored = (root && root.anchors) || [];
  const attachedHtml = attached
    .map(
      (id) =>
        '<span class="chip src" title="source attached to context">' +
        esc(sourceById(id).target) +
        ' <b data-act="detach-src" data-src="' + esc(id) + '">×</b></span>',
    )
    .join("");
  const anchorHtml = anchored
    .filter((id) => sourceById(id) && attached.indexOf(id) < 0)
    .map(
      (id) =>
        '<button class="chip anchor" data-act="attach-src" data-src="' + esc(id) + '" title="anchored to root — attach its text to context">⚓ attach ' +
        esc(sourceById(id).target) +
        "</button>",
    )
    .join("");
  const rest = sources.filter((s) => attached.indexOf(s.id) < 0 && anchored.indexOf(s.id) < 0);
  const menu = rest.length
    ? '<select class="chip menu" data-input="attach-src-menu" title="attach a corpus source">' +
      '<option value="">+ source</option>' +
      rest
        .map((s) => '<option value="' + esc(s.id) + '">' + esc(s.target) + (s.exists === false ? " ⚠" : "") + "</option>")
        .join("") +
      "</select>"
    : "";
  return attachedHtml + anchorHtml + menu;
}

function chatLogHtml() {
  if (!S.ws) return "";
  if (!S.ws.chat.length) {
    const hint = wsRoot()
      ? "Ask your question about this note."
      : "Ask Claude to create cards from the corpus sources.";
    return (
      '<div class="empty">' + hint + "<br>" +
      "Claude's proposals appear as versions and cards on the left; nothing is written until you click Apply.</div>"
    );
  }
  return S.ws.chat.map(msgHtml).join("");
}

const URL_RE = /https?:\/\/[^\s<>"'\]]+/g;

/* Escaped text with its URLs as links opening in a new tab (specs/chat.md#web-tools). */
function linkify(text) {
  let out = "";
  let last = 0;
  String(text || "").replace(URL_RE, (match, offset) => {
    const url = match.replace(/[.,;:!?)]+$/, "");
    out += esc(text.slice(last, offset));
    out += '<a href="' + esc(url) + '" target="_blank" rel="noopener">' + esc(url) + "</a>";
    last = offset + url.length;
    return match;
  });
  return (out + esc(text.slice(last))).replace(/\n/g, "<br>");
}

/* An assistant text segment: citation links embedded in the raw text, then Markdown + math. */
function textSegmentHtml(text, cites, byN) {
  let full = "";
  let at = 0;
  (cites || []).forEach((c) => {
    const pos = Math.min(Math.max(c.pos, at), text.length);
    const s = byN[c.n] || {};
    full +=
      text.slice(at, pos) +
      '<a class="cite" href="' + esc(s.url || "#") + '" target="_blank" rel="noopener" title="' + esc(c.cited || "") + '">[' + c.n + "]</a>";
    at = pos;
  });
  full += text.slice(at);
  return renderMarkdown(full);
}

/* User messages use plain linkify; assistant messages go through Markdown + math. */
function bodyHtml(m) {
  if (m.who === "user") return linkify(m.text || "");
  const byN = {};
  (m.sources || []).forEach((s) => {
    byN[s.n] = s;
  });
  return textSegmentHtml(m.text || "", m.cites || [], byN);
}

function sourcesHtml(m) {
  const sources = m.sources || [];
  if (!sources.length) return "";
  const corpus = ((S.corpus || {}).sources) || [];
  const corpusUrls = new Set(corpus.filter((s) => s.kind === "web").map((s) => s.target));
  const items = sources.map((s) => {
    let host = "";
    try {
      host = new URL(s.url).host.replace(/^www\./, "");
    } catch (e) {
      host = "";
    }
    const inCorpus = corpusUrls.has(s.url);
    const addBtn =
      s.url && !inCorpus
        ? ' <button class="ghost small add-to-corpus" data-act="ws-add-cited" data-url="' +
          esc(s.url) +
          '" data-title="' +
          esc(s.title || "") +
          '">+ corpus</button>'
        : inCorpus
          ? ' <span class="muted small">in corpus</span>'
          : "";
    return (
      '<li><span class="n">[' + s.n + "]</span> " +
      '<a href="' + esc(s.url) + '" target="_blank" rel="noopener">' + esc(s.title || s.url) + "</a>" +
      (s.title && host ? ' <span class="host">' + esc(host) + "</span>" : "") +
      addBtn +
      "</li>"
    );
  });
  return '<ol class="sources">' + items.join("") + "</ol>";
}

function msgHtml(m, mi) {
  const who = m.who === "user" ? "you" : "claude";
  if (!m.parts) {
    const body = bodyHtml(m) + (m.streaming ? '<span class="cursor">▍</span>' : "") + sourcesHtml(m);
    return '<div class="msg ' + (m.who === "user" ? "user" : "assistant") + '"><div class="who">' + who + "</div>" + body + "</div>";
  }
  const byN = {};
  (m.sources || []).forEach((s) => {
    byN[s.n] = s;
  });
  let html = '<div class="msg assistant"><div class="who">' + who + "</div>";
  m.parts.forEach((part) => {
    if (part.type === "text") {
      html += textSegmentHtml(part.text || "", part.cites || [], byN);
    } else if (part.type === "reading") {
      html += '<div class="reading">reading: ' + esc(part.tool || "?") + (part.summary ? " → " + linkify(part.summary) : "") + "</div>";
    } else if (part.type === "added") {
      html +=
        '<div class="reading">' +
        (part.error ? "add refused: " + esc(part.error) : "adding: " + part.count + " note(s)") +
        (part.rationale ? " — " + esc(part.rationale) : "") +
        "</div>";
    }
  });
  if (m.streaming) html += '<span class="cursor">▍</span>';
  html += sourcesHtml(m);
  html += (m.proposals || []).map((p, pi) => proposalHtml(p, mi, pi)).join("");
  if (m.error) html += '<div class="banner">' + esc(m.error) + "</div>";
  html += "</div>";
  return html;
}

/* Card proposals are one pointer line; source proposals keep their inline card. */
function proposalHtml(p, mi, pi) {
  const input = p.input || {};
  if (!isSourceProposal(p.kind)) {
    return (
      '<div class="ws-pointer">' +
      (p.error ? "proposal " + esc(p.kind) + " refused: " + esc(p.error) : p.landed ? esc(p.kind) + " " + p.landed : esc(p.kind) + " …") +
      "</div>"
    );
  }
  let diff = "";
  if (p.kind === "add_source") {
    const target = p.target != null ? p.target : input.target || "";
    const kind = SOURCE_KINDS.indexOf(input.kind) >= 0 ? input.kind : detectKind(target);
    const anchors = input.anchor_note_ids || [];
    const meta = [];
    if (kind === "pdf" && input.pages) meta.push("pages " + esc(input.pages));
    if (input.note) meta.push(esc(input.note));
    diff =
      '<div class="muted small"><span class="kind ' + esc(kind) + '">' + esc(kind) + "</span> " +
      (kind === "web" ? "web page" : kind === "pdf" ? "PDF" : "vault note") +
      " to add to corpus of " + esc(S.ws.deck) +
      (meta.length ? " · " + meta.join(" · ") : "") + "</div>" +
      '<input class="mono src-name" data-input="psrc-target" data-mi="' + mi + '" data-pi="' + pi + '" value="' +
      esc(target) +
      '" placeholder="https://… · vault note · ~/doc.pdf"' +
      (p.applied ? " disabled" : "") +
      ">" +
      (kind === "web" && target
        ? '<div class="small"><a href="' + esc(target) + '" target="_blank" rel="noopener">open ↗</a></div>'
        : "") +
      (anchors.length ? '<div class="muted small">anchor: ' + anchors.map(short).join(" ") + "</div>" : "");
  } else if (p.kind === "create_source") {
    const anchors = input.anchor_note_ids || [];
    diff =
      '<div class="muted small">Obsidian note to create in the vault, added to corpus of ' + esc(S.ws.deck) + "</div>" +
      '<input class="mono src-name" data-input="psrc-name" data-mi="' + mi + '" data-pi="' + pi + '" value="' +
      esc(p.name != null ? p.name : input.name || "") +
      '" placeholder="folder/note name"' +
      (p.applied ? " disabled" : "") +
      ">" +
      (anchors.length ? '<div class="muted small">anchor: ' + anchors.map(short).join(" ") + "</div>" : "") +
      '<pre class="excerpt">' + esc(input.content || "") + "</pre>";
  } else {
    const src = sourceById(input.source_id);
    diff =
      '<div class="muted small">' + (src ? esc(src.target) : "source " + esc(input.source_id || "?")) + "</div>" +
      '<div class="before"><pre class="excerpt">' + esc(input.old || "") + '</pre></div><div class="after"><pre class="excerpt">' + esc(input.new || "") + "</pre></div>";
  }
  const disabled = S.busy ? " disabled" : "";
  const at = ' data-mi="' + mi + '" data-pi="' + pi + '"';
  const head = p.applied
    ? '<span class="applied-mark">applied ✓</span>' +
      (p.kind === "edit_source" ? '<button class="revert" data-act="ws-revert-src"' + at + disabled + ">Revert</button>" : "")
    : '<button class="primary" data-act="ws-apply-src"' + at + disabled + ">Apply</button>";
  return (
    '<div class="proposal' + (p.applied ? " applied" : "") + '">' +
    '<div class="proposal-head"><span class="pkind">' +
    (p.kind === "add_source" ? "add source" : p.kind === "create_source" ? "new source" : "source") +
    "</span>" +
    '<span class="grow"></span>' + head + "</div>" +
    (input.rationale ? '<div class="rationale">' + nl2br(input.rationale) + "</div>" : "") +
    '<div class="diff">' + diff + "</div>" +
    (p.error ? '<div class="banner">' + esc(p.error) + "</div>" : "") +
    "</div>"
  );
}
