/* The workspace overlay (specs/workspace.md) and its conversation (specs/chat.md):
   state helpers, rendering, the chat stream landing on the cards, validation and undo.
   Loaded after render.js, before app.js. `draw()` appends `wsOverlay()` when S.ws is set;
   app.js forwards the `ws-*` clicks, inputs and keys here. */

"use strict";

const MAX_CARDS = 50; // same cap as chat.py (specs/workspace.md#how-notes-enter)

/* ---------------- state ---------------- */

/* One card of the workspace. `versions[0]` is v0 (Anki) for an existing note; a draft note has
   no v0. `vi` is the shown version. */
function cardFromNote(n, parentWid) {
  const isRoot = S.ws.cards.length === 0;
  return {
    wid: "w" + S.ws.nextWid++,
    noteId: n.note_id,
    parentWid: parentWid || null,
    model: n.model,
    tags: (n.tags || []).slice(),
    originalTags: (n.tags || []).slice(),
    deck: n.deck || S.ws.deck,
    flaggedClozes: flaggedClozes(n),
    ankiFlagged: !!n.flagged,
    reason: n.reason || "",
    anchors: null, // [source id] once fetched
    versions: [{ fields: Object.assign({}, n.fields || {}), by: "anki", rationale: "" }],
    vi: 0,
    active: true,
    deleted: false,
    // The flag after validation (specs/workspace.md#vocabulary): the root opens resolved,
    // a note Claude brought in opens as Anki holds it.
    flag: flagState(!isRoot && !!n.flagged, n.fields || {}),
    moveTo: null,
    revealed: false,
    editing: null, // field name while a textarea is open
  };
}

function draftCard(spec) {
  return {
    wid: "w" + S.ws.nextWid++,
    noteId: null,
    parentWid: spec.parentWid || null,
    model: spec.model,
    tags: (spec.tags || []).slice(),
    originalTags: [],
    deck: spec.deck || S.ws.deck,
    flaggedClozes: [],
    ankiFlagged: false,
    reason: "",
    anchors: (spec.anchors || []).slice(),
    versions: [{ fields: Object.assign({}, spec.fields || {}), by: "claude", rationale: spec.rationale || "" }],
    vi: 0,
    active: true,
    deleted: false,
    flag: flagState(false, spec.fields || {}),
    moveTo: null,
    revealed: false,
    editing: null,
  };
}

function wsCard(wid) {
  return S.ws ? S.ws.cards.find((c) => c.wid === wid) || null : null;
}

function wsCardByNote(noteId) {
  return S.ws ? S.ws.cards.find((c) => c.noteId === noteId) || null : null;
}

function wsRoot() {
  return S.ws ? wsCard(S.ws.rootWid) : null;
}

/* Walk up the parent chain and return the wid of the nearest ancestor that has a noteId,
   or null when there is none. Used by split so that grandchildren inherit scheduling from
   the original existing note rather than from the intermediate draft. */
function wsAncestorWithNote(card) {
  let c = card;
  const seen = {};
  while (c) {
    if (seen[c.wid]) return null; // cycle guard
    seen[c.wid] = true;
    if (c.noteId) return c.wid;
    c = c.parentWid ? wsCard(c.parentWid) : null;
  }
  return null;
}

function shownVersion(c) {
  return c.versions[c.vi];
}

function shownFields(c) {
  return shownVersion(c).fields;
}

/* An existing note's card is changed when it is not on v0 or carries a state; a draft always is. */
/* The flag toggle of a card. `on` is the flag after validation; `comment` is what goes to
   « Back Extra » when it is on; `initial` is the comment's starting text (the field's plain
   text), so that an untouched field is not rewritten; `initial_on` is the opening state, which
   the close confirmation compares against. */
function flagState(on, fields) {
  const text = plainText(fields[REASON_FIELD] || "");
  return { on: on, comment: text, initial: text, initialOn: on };
}

function commentChanged(c) {
  return c.flag.comment !== c.flag.initial;
}

/* Whether the card is in the plan (specs/workspace.md#what-is-written). */
function cardChanged(c) {
  return planCard(c) !== null;
}

/* Whether the user did something by hand on this card: what the × confirmation counts. */
function cardDirty(c) {
  if (!c.noteId) return true;
  return (
    c.vi > 0 ||
    c.deleted ||
    !!c.moveTo ||
    tagsChanged(c) ||
    shownModel(c) !== c.model ||
    c.flag.on !== c.flag.initialOn ||
    (c.flag.on && commentChanged(c))
  );
}

function tagsChanged(c) {
  return c.tags.join(" ") !== c.originalTags.join(" ");
}

/* Counts for the « Valider » label, derived from the plan so that they never disagree. */
function wsChanges() {
  const out = { edited: 0, created: 0, deleted: 0, kept: 0, deferred: 0, moved: 0, total: 0 };
  (S.ws ? S.ws.cards : []).forEach((c) => {
    const p = planCard(c);
    if (!p) return;
    out.total++;
    if (p.action === "create") {
      out.created++;
      if (p.defer) out.deferred++; // created flagged: counts as both
    } else if (p.action === "delete") out.deleted++;
    else if (p.action === "edit") {
      out.edited++;
      if (p.defer) out.deferred++; // edited and still flagged: counts as both
    } else if (p.action === "defer") out.deferred++;
    else if (p.action === "keep") out.kept++;
    if (p.move_to) out.moved++;
  });
  return out;
}

function wsDirty() {
  return (S.ws ? S.ws.cards : []).filter(cardDirty).length;
}

/* Fetch a card's anchors once; used for the chips and passed on to fragments. */
function loadCardAnchors(card) {
  if (!card.noteId || card.anchors !== null) return;
  API.anchors(card.noteId)
    .then((a) => {
      card.anchors = ((a && a.anchors) || []).map((x) => x.source_id);
      draw();
    })
    .catch(() => {
      card.anchors = [];
    });
}

/* Add an existing note as a card (once: a note id maps to one card). */
function addNoteCard(n, parentWid) {
  const existing = wsCardByNote(n.note_id);
  if (existing) return existing;
  const card = cardFromNote(n, parentWid);
  S.ws.cards.push(card);
  loadCardAnchors(card);
  return card;
}

function addDraftCard(spec) {
  const card = draftCard(spec);
  S.ws.cards.push(card);
  return card;
}

function openWorkspace(noteId) {
  if (S.ws) return;
  const n = noteId ? noteById(noteId) : null;
  if (noteId && !n) return;
  if (!noteId && !S.deck) return;
  if (noteId) S.selNote = noteId;
  S.ws = {
    rootWid: n ? "w1" : null,
    deck: (n && n.deck) || S.deck,
    nextWid: 1,
    cards: [],
    clearReason: true,
    chat: [],
    chatSources: [],
    chatDraft: "",
    chatBusy: false,
    applying: false,
    report: null,
    rejected: [], // « w3 v2 » per dropped version, told to Claude in the history
  };
  if (n) addNoteCard(n);
  S.refocus = "chat";
  draw();
}

/* Discard everything (specs/workspace.md#opening-and-closing). */
async function closeWorkspace(force) {
  if (!S.ws || S.ws.applying) return;
  const n = wsDirty();
  if (!force && n > 0) {
    const ok = window.confirm(
      n + " carte(s) modifiée(s) non validée(s) seront perdues. Fermer quand même ?",
    );
    if (!ok) return;
  }
  const root = wsRoot();
  S.ws = null;
  draw();
  await afterDecision({ resolvedId: root ? root.noteId : null, keepSelection: true });
}

/* Turn the flag on or off. Turning it on brings the comment textarea up, focused. */
function toggleFlag(card) {
  card.flag.on = !card.flag.on;
  if (card.flag.on && hasReasonField(card)) S.refocus = "ws-comment-" + card.wid;
}

/* A draft always has one as far as the client knows — a proposal may have left the field
   out — and the server checks the note type at validation (specs/workspace.md#body). An
   existing note is judged on its shown version: after a change of note type its fields are
   the target type's, which may not have the field. */
function hasReasonField(card) {
  return !card.noteId || Object.prototype.hasOwnProperty.call(shownFields(card), REASON_FIELD);
}

/* ---------------- versions ---------------- */

/* `model` is the note type the version is written for; omitted, the version keeps the shown
   version's type. A type differing from the card's original sticks to the version, so that a
   later retouch never sends Basic fields under the Cloze type (specs/workspace.md#editing). */
function pushVersion(card, fields, by, rationale, model) {
  var effective = model || shownModel(card);
  var v = { fields: Object.assign({}, fields), by: by, rationale: rationale || "" };
  if (effective !== card.model) v.model = effective;
  card.versions.push(v);
  card.vi = card.versions.length - 1;
  card.deleted = false; // a rewrite supersedes a deletion (specs/chat.md#proposal-tools)
}

/* When a proposal omits `model`, infer it from the fields: if the field names don't match
   `fallback` but do match exactly one known type, use that type.  Otherwise keep `fallback`.
   Covers the case where Claude splits a Cloze into Basic cards without passing `model`. */
function inferModel(fields, fallback) {
  if (!fields || !S.models) return fallback;
  var keys = Object.keys(fields);
  if (!keys.length) return fallback;
  var fbFields = fieldNames(fallback);
  if (fbFields.length && keys.every((k) => fbFields.indexOf(k) >= 0)) return fallback;
  // Fields don't match the fallback type — look for a type whose fields are a superset.
  var match = null;
  for (var name in S.models) {
    var mf = S.models[name];
    if (mf.length && keys.every((k) => mf.indexOf(k) >= 0)) {
      if (match) return fallback; // ambiguous: two types match, keep the fallback
      match = name;
    }
  }
  return match || fallback;
}

/* The effective model for a card: the shown version's model override, or the card's original. */
function shownModel(c) {
  var v = shownVersion(c);
  return v.model || c.model;
}

function showVersion(card, delta) {
  card.vi = Math.max(0, Math.min(card.versions.length - 1, card.vi + delta));
  card.editing = null;
}

/* « invalider »: drop the shown version. v0 stays; a draft with no version left disappears. */
function invalidateVersion(card) {
  if (card.noteId && card.vi === 0) return;
  const label = card.wid + " v" + versionNumber(card, card.vi);
  card.versions.splice(card.vi, 1);
  S.ws.rejected.push(label);
  if (!card.versions.length) {
    S.ws.cards = S.ws.cards.filter((c) => c !== card);
    return;
  }
  card.vi = Math.max(0, Math.min(card.vi - 1, card.versions.length - 1));
  card.editing = null;
}

/* Shown to the user: v0 = Anki, v1… = proposals; a draft's first version is v1. */
function versionNumber(card, i) {
  return card.noteId ? i : i + 1;
}

/* A keystroke on v0 copies it into a new version first (v0 is Anki's, read-only). */
function editField(card, name, value) {
  let v = shownVersion(card);
  if (v.by === "anki") {
    v = { fields: Object.assign({}, v.fields), by: "user", rationale: "" };
    if (shownModel(card) !== card.model) v.model = shownModel(card);
    card.versions.push(v);
    card.vi = card.versions.length - 1;
  }
  v.fields[name] = value;
  v.edited = true;
}

/* ---------------- proposals landing on the workspace ---------------- */

/* `target` is a workspace id or an Anki note id in digits; an absent note is fetched and added
   (specs/chat.md#proposal-tools). Throws with a French message when it cannot. */
async function resolveTarget(target) {
  const t = String(target === null || target === undefined ? "" : target).trim();
  const byWid = wsCard(t);
  if (byWid) return byWid;
  if (!/^\d+$/.test(t)) throw new Error("cible inconnue : " + t);
  const nid = Number(t);
  const byNote = wsCardByNote(nid);
  if (byNote) return byNote;
  const notes = await lookupNotes([nid]);
  if (!notes.length) throw new Error("note introuvable : #" + t);
  return addNoteCard(notes[0]);
}

async function lookupNotes(ids) {
  const missing = ids.filter((id) => !wsCardByNote(id));
  if (!missing.length) return [];
  if (S.ws.cards.length + missing.length > MAX_CARDS) {
    throw new Error("espace de travail plein (" + MAX_CARDS + " cartes)");
  }
  return (await API.lookup(missing)) || [];
}

/* Proposals that write into sources.json or the vault on click, not at validation. They stay
   in the log as cards with « Appliquer » (specs/chat.md#proposal-tools). */
const SOURCE_PROPOSALS = ["add_source", "create_source", "edit_source"];

function isSourceProposal(kind) {
  return SOURCE_PROPOSALS.indexOf(kind) >= 0;
}

/* One proposal event → a version, fragment cards, a draft card or a badge. Returns the
   pointer text shown in the log. Source proposals are not handled here (they stay inline). */
async function landProposal(input, kind) {
  const inp = input || {};
  if (kind === "edit") {
    const card = await resolveTarget(inp.target);
    const newModel = inp.model || null;
    const modelChanged = newModel && newModel !== shownModel(card);
    // When the note type changes, fields are complete (different schema); otherwise merge.
    const fields = modelChanged
      ? Object.assign({}, inp.fields || {})
      : Object.assign({}, shownFields(card), inp.fields || {});
    pushVersion(card, fields, "claude", inp.rationale, newModel);
    if (Array.isArray(inp.tags)) card.tags = inp.tags.slice();
    return "→ carte " + card.wid;
  }
  if (kind === "split") {
    const card = await resolveTarget(inp.target);
    // The mother always dies: all pieces become fragment cards.
    card.deleted = true;
    // When Claude intended to keep the original (first fragment on the existing note),
    // turn it into an extra fragment card instead.
    const pieces = (inp.new_notes || []).slice();
    if (inp.original !== null && inp.original !== undefined) {
      const origFields = (inp.original && inp.original.fields) || {};
      const origModel = (inp.original && inp.original.model) || inferModel(origFields, shownModel(card));
      const origModelChanged = origModel !== shownModel(card);
      // When the original changes type, fields are complete (different schema); otherwise merge.
      pieces.unshift({
        fields: origModelChanged
          ? Object.assign({}, origFields)
          : Object.assign({}, shownFields(card), origFields),
        model: origModelChanged ? origModel : null,
      });
    }
    const made = pieces.map((nn) =>
      addDraftCard({
        parentWid: card.wid,
        model: nn.model || inferModel(nn.fields, shownModel(card)),
        fields: nn.fields || {},
        tags: card.tags,
        deck: card.deck,
        anchors: card.anchors || [],
        rationale: inp.rationale,
      }),
    );
    return "→ " + made.length + " cartes";
  }
  if (kind === "create") {
    const root = wsRoot();
    const card = addDraftCard({
      parentWid: null,
      model: inp.model || (root && root.model),
      fields: inp.fields || {},
      tags: root ? root.tags : [],
      deck: S.ws.deck,
      anchors: inp.source_ids || (root && root.anchors) || [],
      rationale: inp.rationale,
    });
    return "→ carte " + card.wid;
  }
  if (kind === "move") {
    const card = await resolveTarget(inp.target);
    card.moveTo = inp.deck || null;
    return "→ carte " + card.wid + " → " + esc(inp.deck || "?");
  }
  throw new Error("proposition inconnue : " + kind);
}

async function landAdded(noteIds) {
  const notes = await lookupNotes((noteIds || []).map(Number));
  notes.forEach((n) => addNoteCard(n));
  return notes.length;
}

/* ---------------- chat ---------------- */

function cardsForServer() {
  return S.ws.cards.map((c) => ({
    wid: c.wid,
    note_id: c.noteId,
    fields: shownFields(c),
    deck: c.deck,
    model: shownModel(c),
    tags: c.tags,
    active: c.active,
    original_fields: c.noteId && c.vi > 0 ? c.versions[0].fields : null,
    flagged_clozes: c.flaggedClozes,
    reason: c.reason,
    anchor_ids: c.anchors || [],
    deleted: c.deleted,
    keep: (planCard(c) || {}).action === "keep",
    defer: c.flag.on && !c.deleted,
    comment: c.flag.on ? c.flag.comment : "",
    move_to: c.moveTo,
    parent_wid: c.parentWid,
  }));
}

/* Prior proposals, reads and additions are summarised into the assistant text
   (specs/chat.md#api); dropped versions are told once, on the last assistant message. */
function historyForServer() {
  const out = [];
  const msgs = S.ws.chat;
  const lastAssistant = msgs.map((m) => m.who).lastIndexOf("assistant");
  msgs.forEach((m, i) => {
    const bits = [];
    if (m.parts) {
      m.parts.forEach((part) => {
        if (part.type === "text") bits.push(textWithMarkers(part));
        else if (part.type === "reading") bits.push("[lecture: " + (part.tool || "?") + (part.summary ? " → " + part.summary : "") + "]");
        else if (part.type === "added") bits.push("[ajout: " + part.count + " notes]");
      });
    } else {
      bits.push(textWithMarkers(m));
    }
    if ((m.sources || []).length) {
      bits.push("[sources : " + m.sources.map((s) => "[" + s.n + "] " + s.url).join(", ") + "]");
    }
    (m.proposals || []).forEach((p) => {
      if (p.landed) bits.push("[proposition: " + p.kind + " " + p.landed + "]");
      else if (isSourceProposal(p.kind)) {
        bits.push("[proposition: " + p.kind + (p.applied ? " (appliquée)" : "") + "]");
      }
    });
    if (i === lastAssistant) {
      S.ws.rejected.forEach((r) => bits.push("[version rejetée : " + r + "]"));
    }
    const content = bits.filter((x) => x && x.trim()).join("\n");
    if (!content.trim()) return;
    out.push({ role: m.who === "user" ? "user" : "assistant", content });
  });
  return out;
}

/* The reply text with its [n] citation markers inlined, as replayed to the LLM. */
function textWithMarkers(m) {
  const text = m.text || "";
  let out = "";
  let at = 0;
  (m.cites || []).forEach((c) => {
    const pos = Math.min(Math.max(c.pos, at), text.length);
    out += text.slice(at, pos) + "[" + c.n + "]";
    at = pos;
  });
  return out + text.slice(at);
}

let wsLogRefreshQueued = false;
function scheduleLogRefresh() {
  if (wsLogRefreshQueued) return;
  wsLogRefreshQueued = true;
  requestAnimationFrame(() => {
    wsLogRefreshQueued = false;
    refreshChatLog();
  });
}

/* Pixels from the bottom within which the log still counts as « at the bottom ». */
const LOG_STICK_PX = 40;

/* Narrow refresh used while a reply streams in, so the textarea keeps focus. The log follows
   the reply only while the reader is at the bottom; scrolled up, they stay where they are. */
function refreshChatLog() {
  const log = document.getElementById("chat-log");
  if (!log) return;
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight <= LOG_STICK_PX;
  const top = log.scrollTop;
  log.innerHTML = chatLogHtml();
  log.scrollTop = atBottom ? log.scrollHeight : top;
  typesetMath();
}

function scrollChatLogToBottom() {
  const log = document.getElementById("chat-log");
  if (log) log.scrollTop = log.scrollHeight;
}

async function sendChat() {
  if (!S.ws) return;
  const ws = S.ws;
  const text = String(ws.chatDraft || "").trim();
  if (!text || ws.chatBusy) return;
  if (S.chatStatus && S.chatStatus.configured === false) return;
  const messages = historyForServer();
  messages.push({ role: "user", content: text });

  ws.chat.push({ who: "user", text });
  const reply = {
    who: "assistant",
    parts: [],
    sources: [],
    proposals: [],
    streaming: true,
    error: "",
  };
  ws.chat.push(reply);
  ws.chatDraft = "";
  ws.chatBusy = true;
  draw();
  scrollChatLogToBottom();

  function currentTextPart() {
    const last = reply.parts[reply.parts.length - 1];
    if (last && last.type === "text") return last;
    const part = { type: "text", text: "", cites: [] };
    reply.parts.push(part);
    return part;
  }

  // Landing a proposal may fetch a note; keep the order of arrival with a promise chain.
  let landing = Promise.resolve();
  const later = (fn) => {
    landing = landing.then(fn).catch(() => {});
  };
  try {
    const payload = {
      deck: ws.deck,
      cards: cardsForServer(),
      source_ids: ws.chatSources.slice(),
      flagged_count: (S.notes && S.notes.flagged) || 0,
      messages,
    };
    await streamChat(payload, (name, data) => {
      const d = data || {};
      if (name === "text") {
        currentTextPart().text += d.delta || "";
        scheduleLogRefresh();
      } else if (name === "citation") {
        const tp = currentTextPart();
        tp.cites.push({ pos: tp.text.length, n: d.n, cited: d.cited_text || "" });
        if (!reply.sources.some((s) => s.n === d.n)) {
          reply.sources.push({ n: d.n, url: d.url || "", title: d.title || "" });
        }
        scheduleLogRefresh();
      } else if (name === "reading") {
        reply.parts.push({ type: "reading", tool: d.tool || "?", input: d.input || {}, summary: d.summary || "" });
        scheduleLogRefresh();
      } else if (name === "added") {
        const entry = { type: "added", count: 0, rationale: d.rationale || "", error: "" };
        reply.parts.push(entry);
        later(async () => {
          if (S.ws !== ws) return;
          try {
            entry.count = await landAdded(d.note_ids || []);
          } catch (e) {
            entry.error = e.message;
          }
          draw();
        });
      } else if (name === "proposal") {
        const p = {
          id: d.id,
          kind: d.kind,
          input: d.input || {},
          sourceId: d.source_id || null,
          name: (d.input || {}).name || "",
          target: (d.input || {}).target || "",
          applied: false,
          landed: "",
          error: "",
        };
        reply.proposals.push(p);
        if (!isSourceProposal(p.kind)) {
          later(async () => {
            if (S.ws !== ws) return;
            try {
              p.landed = await landProposal(p.input, p.kind);
            } catch (e) {
              p.error = e.message;
            }
            draw();
          });
        }
        scheduleLogRefresh();
      } else if (name === "error") {
        reply.error = d.detail || "erreur";
        scheduleLogRefresh();
      }
    });
  } catch (e) {
    reply.error = e.message;
  }
  await landing;
  reply.streaming = false;
  ws.chatBusy = false;
  if (S.ws === ws) {
    S.refocus = "chat";
    draw();
  }
}

function attachSource(id) {
  if (!S.ws || !id || !sourceById(id) || S.ws.chatSources.indexOf(id) >= 0) return;
  S.ws.chatSources.push(id);
  draw();
}

function detachSource(id) {
  if (!S.ws) return;
  S.ws.chatSources = S.ws.chatSources.filter((x) => x !== id);
  draw();
}

/* Source proposals write into the vault on click, not at validation (specs/chat.md). */
async function applySourceProposal(mi, pi) {
  const msg = S.ws && S.ws.chat[mi];
  const p = msg && msg.proposals && msg.proposals[pi];
  if (!p || p.applied || S.busy) return;
  const input = p.input || {};
  p.error = "";
  S.busy = true;
  draw();
  try {
    if (p.kind === "add_source") {
      const target = String(p.target != null ? p.target : input.target || "").trim();
      if (!target) throw new Error("indique une cible");
      const entry = {
        target,
        kind: SOURCE_KINDS.indexOf(input.kind) >= 0 ? input.kind : detectKind(target),
        anchor_note_ids: input.anchor_note_ids || [],
        id: p.sourceId || "",
      };
      if (entry.kind === "pdf" && input.pages) entry.pages = String(input.pages);
      if (input.note) entry.note = String(input.note);
      await API.addSource(S.ws.deck, entry);
      refreshAnchorsOf(input.anchor_note_ids || []);
    } else if (p.kind === "create_source") {
      const name = String(p.name != null ? p.name : input.name || "").trim();
      if (!name) throw new Error("indique un nom de note");
      await API.createSourceNote({
        deck: S.ws.deck,
        name,
        content: input.content || "",
        anchor_note_ids: input.anchor_note_ids || [],
        id: p.sourceId || null,
      });
      refreshAnchorsOf(input.anchor_note_ids || []);
    } else if (p.kind === "edit_source") {
      await API.patchSourceText(input.source_id, { old: input.old || "", new: input.new || "" });
    } else {
      throw new Error("proposition inconnue : " + p.kind);
    }
    p.applied = true;
    S.busy = false;
    await loadCorpus();
  } catch (e) {
    p.error = e.message;
    S.busy = false;
    draw();
  }
}

/* The « + corpus » button on a cited source under a reply (specs/sources.md). */
async function addCitedSource(el) {
  if (!S.ws || S.busy) return;
  const url = el.getAttribute("data-url");
  if (!url) return;
  S.busy = true;
  draw();
  try {
    await API.addSource(S.ws.deck, { target: url, kind: "web" });
    S.busy = false;
    await loadCorpus();
  } catch (e) {
    S.error = e.message;
    S.busy = false;
    draw();
  }
}

/* A source proposal that anchored notes changed their anchors server-side: re-fetch them. */
function refreshAnchorsOf(noteIds) {
  noteIds.forEach((id) => {
    const c = wsCardByNote(id);
    if (c) {
      c.anchors = null;
      loadCardAnchors(c);
    }
  });
}

async function revertSourceProposal(mi, pi) {
  const msg = S.ws && S.ws.chat[mi];
  const p = msg && msg.proposals && msg.proposals[pi];
  if (!p || !p.applied || p.kind !== "edit_source" || S.busy) return;
  const input = p.input || {};
  p.error = "";
  S.busy = true;
  draw();
  try {
    await API.patchSourceText(input.source_id, { old: input.new || "", new: input.old || "" });
    p.applied = false;
    S.busy = false;
    await loadCorpus();
  } catch (e) {
    p.error = e.status === 409 ? "modifiée depuis, annulation impossible" : e.message;
    S.busy = false;
    draw();
  }
}

/* ---------------- validation ---------------- */

/* One card's entry in the plan of specs/workspace.md#what-is-written, or null when the
   card is not in it. The comment travels only when the user changed it. */
function planCard(c) {
  const comment = c.flag.on && commentChanged(c) ? c.flag.comment : null;
  // A deleted draft is not written — it was never in Anki, so there is nothing to create or
  // delete. Check deleted *before* the draft branch so a split-then-delete does not produce
  // a ghost create.
  if (c.deleted && !c.noteId) return null;
  if (!c.noteId) {
    // For scheduling inheritance, resolve to the nearest ancestor with a noteId.
    // parentWid is the visual parent (may be a draft); the server needs an existing note.
    const schedParent = wsAncestorWithNote(c);
    return {
      wid: c.wid,
      action: "create",
      parent_wid: schedParent,
      deck: c.deck,
      model: shownModel(c),
      fields: shownFields(c),
      tags: c.tags,
      source_ids: c.anchors || [],
      defer: c.flag.on,
      comment: comment,
    };
  }
  if (c.deleted) return { wid: c.wid, action: "delete", note_id: c.noteId };
  if (c.vi > 0 || tagsChanged(c)) {
    const card = { wid: c.wid, action: "edit", note_id: c.noteId, fields: shownFields(c), move_to: c.moveTo };
    if (tagsChanged(c)) card.tags = c.tags;
    const em = shownModel(c);
    if (em !== c.model) card.model = em;
    if (c.flag.on) {
      card.defer = true;
      if (comment !== null) card.comment = comment;
    }
    return card;
  }
  if (c.flag.on) {
    if (!c.ankiFlagged || comment !== null || c.moveTo) {
      return { wid: c.wid, action: "defer", note_id: c.noteId, comment: comment, move_to: c.moveTo };
    }
    return null; // flagged in Anki, stays so, nothing to say: left alone
  }
  if (c.ankiFlagged || c.moveTo) return { wid: c.wid, action: "keep", note_id: c.noteId, move_to: c.moveTo };
  return null;
}

function planFromWs() {
  const cards = S.ws.cards.map(planCard).filter(Boolean);
  return { deck: S.ws.deck, clear_reason: !!S.ws.clearReason, cards };
}

async function validateWorkspace() {
  if (!S.ws || S.ws.applying || S.ws.chatBusy) return;
  const ws = S.ws;
  const plan = planFromWs();
  if (!plan.cards.length) return;
  const deleting = ws.cards.filter((c) => c.noteId && c.deleted);
  if (deleting.length) {
    const ok = window.confirm(
      "Supprimer définitivement " +
        deleting.length +
        " note(s) : " +
        deleting.map((c) => short(c.noteId)).join(", ") +
        " ?",
    );
    if (!ok) return;
  }
  const unapplied = ws.chat.some((m) =>
    (m.proposals || []).some(
      (p) => (p.kind === "add_source" || p.kind === "create_source") && !p.applied,
    ),
  );
  if (unapplied) {
    const ok = window.confirm(
      "Une source proposée n'a pas été appliquée : les notes ancrées dessus perdront cette ancre. Valider quand même ?",
    );
    if (!ok) return;
  }
  ws.applying = true;
  ws.report = null;
  draw();
  let report;
  try {
    report = await API.wsApply(plan);
  } catch (e) {
    ws.applying = false;
    // A refused plan (422) or a transport error: the server wrote nothing.
    ws.report = { ok: false, errors: [e.message], rolled_back: false, nothing_written: true, created: {} };
    draw();
    return;
  }
  ws.applying = false;
  if (report && report.ok) {
    S.undoAvailable = !!report.undo_available;
    const root = wsRoot();
    S.ws = null;
    draw();
    await afterDecision({ resolvedId: root ? root.noteId : null });
    return;
  }
  // Failure: keep the workspace open, give created drafts their id, show the report.
  ws.report = report || { ok: false, errors: ["réponse vide"], created: {} };
  Object.keys(ws.report.created || {}).forEach((wid) => {
    const c = wsCard(wid);
    if (c) {
      c.noteId = ws.report.created[wid];
      c.versions[0].by = "anki";
      c.originalTags = c.tags.slice();
    }
  });
  draw();
}

async function undoLastValidation() {
  if (S.busy) return;
  S.busy = true;
  S.error = "";
  draw();
  try {
    await API.wsUndo();
    S.undoAvailable = false;
    S.busy = false;
    await afterDecision({ keepSelection: true });
  } catch (e) {
    S.error = e.message;
    if (e.status === 409) S.undoAvailable = false;
    S.busy = false;
    draw();
  }
}

/* ---------------- events (called from app.js) ---------------- */

function wsClick(act, el, e) {
  const ws = S.ws;
  const card = el.getAttribute("data-wid") ? wsCard(el.getAttribute("data-wid")) : null;
  if (act === "ws-close") return closeWorkspace(false);
  if (act === "ws-validate") return validateWorkspace();
  if (act === "ws-send") return sendChat();
  if (act === "ws-apply-src") {
    return applySourceProposal(Number(el.getAttribute("data-mi")), Number(el.getAttribute("data-pi")));
  }
  if (act === "ws-revert-src") {
    return revertSourceProposal(Number(el.getAttribute("data-mi")), Number(el.getAttribute("data-pi")));
  }
  if (act === "ws-add-cited") return addCitedSource(el);
  if (act === "attach-src") return attachSource(el.getAttribute("data-src"));
  if (act === "detach-src") return detachSource(el.getAttribute("data-src"));
  if (act === "ws-dismiss-report") {
    ws.report = null;
    return draw();
  }
  if (act === "ws-split" && card && !card.deleted) {
    S.ws.chatDraft = "scinde " + card.wid + " : ";
    S.refocus = "chat";
    return draw();
  }
  if (!card) return undefined;
  if (act === "ws-toggle") {
    // The head toggles activation, unless the click landed on one of its controls.
    if (e.target.closest("button, select, input, label")) return undefined;
    card.active = !card.active;
  } else if (act === "ws-prev") showVersion(card, -1);
  else if (act === "ws-next") showVersion(card, 1);
  else if (act === "ws-invalidate") invalidateVersion(card);
  else if (act === "ws-delete") card.deleted = !card.deleted;
  else if (act === "ws-flag") toggleFlag(card);
  else if (act === "ws-reveal") card.revealed = !card.revealed;
  else if (act === "ws-edit") {
    if (card.deleted) return undefined;
    card.editing = el.getAttribute("data-field");
    S.refocus = "ws-edit";
  } else return undefined;
  return draw();
}

function wsInput(key, el) {
  const ws = S.ws;
  if (!ws) return;
  if (key === "chat") {
    ws.chatDraft = el.value;
  } else if (key === "attach-src-menu") {
    attachSource(el.value);
  } else if (key === "ws-clear-reason") {
    ws.clearReason = !!el.checked;
  } else if (key === "ws-field") {
    const card = wsCard(el.getAttribute("data-wid"));
    if (card) editField(card, el.getAttribute("data-field"), el.value);
  } else if (key === "ws-comment") {
    const card = wsCard(el.getAttribute("data-wid"));
    if (card) card.flag.comment = el.value; // no redraw, as for a field
  } else if (key === "ws-move") {
    const card = wsCard(el.getAttribute("data-wid"));
    if (card) {
      card.moveTo = el.value || null;
      draw();
    }
  } else if (key === "psrc-name" || key === "psrc-target") {
    const msg = ws.chat[Number(el.getAttribute("data-mi"))];
    const p = msg && msg.proposals && msg.proposals[Number(el.getAttribute("data-pi"))];
    if (p) p[key === "psrc-name" ? "name" : "target"] = el.value;
  }
}

/* A field textarea losing focus goes back to its rendered form. */
function wsBlur(el) {
  if (!S.ws || el.getAttribute("data-input") !== "ws-field") return;
  const card = wsCard(el.getAttribute("data-wid"));
  if (card && card.editing === el.getAttribute("data-field")) {
    card.editing = null;
    draw();
  }
}

/* ---------------- rendering ---------------- */

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
  if (ch.edited) parts.push(ch.edited + " modifiée(s)");
  if (ch.created) parts.push(ch.created + " créée(s)");
  if (ch.deleted) parts.push(ch.deleted + " supprimée(s)");
  if (ch.kept) parts.push(ch.kept + " gardée(s)");
  if (ch.deferred) parts.push(ch.deferred + " à revoir");
  if (ch.moved) parts.push(ch.moved + " déplacée(s)");
  const disabled = !ch.total || ws.applying || ws.chatBusy ? " disabled" : "";
  return (
    '<div class="ws-head">' +
    "<b>" +
    (root && root.noteId ? esc(short(root.noteId)) : "espace de travail") +
    "</b>" +
    '<span class="tag">' +
    esc(ws.deck) +
    "</span>" +
    '<span class="grow"></span>' +
    '<label class="check small"><input type="checkbox" data-input="ws-clear-reason"' +
    (ws.clearReason ? " checked" : "") +
    "> vider Back Extra</label>" +
    '<button class="primary" data-act="ws-validate"' +
    disabled +
    ">" +
    (ws.applying ? "Validation…" : "Valider" + (parts.length ? " · " + parts.join(" · ") : "")) +
    "</button>" +
    '<button class="ghost icon" data-act="ws-close" title="Fermer (Esc)" aria-label="Fermer">×</button>' +
    "</div>"
  );
}

function wsReportHtml() {
  const r = S.ws.report;
  if (!r) return "";
  return (
    '<div class="banner"><div class="grow">' +
    "<b>Validation échouée.</b> " +
    (r.nothing_written
      ? "Rien n'a été écrit."
      : r.rolled_back
        ? "Tout a été remis en place."
        : "Certaines écritures n'ont pas pu être annulées.") +
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
  // For each fragment, build the set of tree-line segments to draw. A segment at depth d is
  // drawn only when a later sibling at that depth exists below (the line must pass through
  // this card to reach it) or when this is the card's own depth (the tap).
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
          // Only emit a segment when this is the tap level or the line continues through.
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
    (c.deleted ? '<span class="badge state">supprimée</span>' : "") +
    (plan && plan.action === "keep" ? '<span class="badge state ok">gardée</span>' : "") +
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
    ? '<button class="ghost small" data-act="ws-split" data-wid="' + c.wid + '" title="demander à Claude de scinder cette carte">✂</button>'
    : "";
  const actions =
    (canInvalidate
      ? '<button class="ghost small" data-act="ws-invalidate" data-wid="' + c.wid + '" title="retirer cette version">invalider</button>'
      : "") +
    (c.noteId
      ? '<button class="ghost small' + (c.deleted ? "" : " dangerish") + '" data-act="ws-delete" data-wid="' + c.wid + '">' +
        (c.deleted ? "restaurer" : "supprimer") +
        "</button>"
      : "") +
    (c.noteId && !c.deleted ? movePickerHtml(c) : "") +
    splitBtn;
  const effectiveModel = shownModel(c);
  const modelChanged = c.noteId && effectiveModel !== c.model;
  const parentLabel = isFragment && c.parentWid ? " · fragment de " + c.parentWid : "";
  const identity =
    '<span class="tag">' +
    (c.noteId ? esc(short(c.noteId)) : "brouillon") +
    " · " +
    esc(effectiveModel || "") +
    (modelChanged ? ' <b title="type changé (était ' + esc(c.model) + ')">⇄</b>' : "") +
    (c.deck && c.deck !== S.ws.deck ? " · " + esc(c.deck) : "") +
    (c.tags.length ? " · " + esc(c.tags.join(" ")) : "") +
    esc(parentLabel) +
    "</span>";
  const meta =
    v.by === "claude" || v.by === "user" || v.edited
      ? '<div class="ws-version">' +
        (v.by === "claude" ? "proposée par Claude" : "version éditée") +
        (v.by === "claude" && v.edited ? " · éditée" : "") +
        (v.rationale ? " — " + esc(v.rationale) : "") +
        "</div>"
      : "";
  // Tree connector lines: two separate elements per level inside the card (position: relative).
  // A vertical line (ws-tree-vert) and, at the card's own depth, a horizontal tap (ws-tree-tap).
  // Keeping them separate so that `top: 50%` on the tap resolves against the card, not the line.
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
    (c.active ? "active — cliquer pour exclure du prochain message" : "inactive — cliquer pour inclure") +
    '">' +
    '<span class="ws-dot' + (c.active ? " on" : "") + '"></span>' +
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
        ? '<div class="reason"><span class="reason-label">raison du flag' + clozeLabels(c) + "</span>" + nl2br(c.reason) + "</div>"
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
  if (c.flag.on) title = "restera flaguée à la validation — cliquer pour la résoudre";
  else if (c.ankiFlagged) title = "le flag sera levé à la validation — cliquer pour le garder";
  else title = "sans flag — cliquer pour la flaguer, à revoir plus tard";
  return (
    '<button class="badge flag-toggle' + (c.flag.on ? " on" : "") + '" data-act="ws-flag" data-wid="' + c.wid +
    '" title="' + title + '" aria-pressed="' + (c.flag.on ? "true" : "false") + '">⚑' +
    (c.flag.on ? " à revoir" : "") +
    "</button>"
  );
}

/* The reason callout of a card whose flag is on: the comment to be written
   (specs/workspace.md#body). */
function wsCommentHtml(c) {
  if (!hasReasonField(c)) {
    return (
      '<div class="reason flagged"><span class="reason-label">à revoir' + clozeLabels(c) + "</span>" +
      "pas de champ " + esc(REASON_FIELD) + " : le flag sera posé sans commentaire</div>"
    );
  }
  return (
    '<div class="reason flagged"><span class="reason-label">raison du flag' + clozeLabels(c) + " · sera écrite</span>" +
    '<textarea data-input="ws-comment" data-wid="' + c.wid + '" data-focus="ws-comment-' + c.wid + '" rows="2"' +
    ' placeholder="pourquoi cette note reste à revoir">' + esc(c.flag.comment) + "</textarea></div>"
  );
}

function movePickerHtml(c) {
  const decks = (S.decks || []).map((d) => d.name);
  return (
    '<select class="ws-move small" data-input="ws-move" data-wid="' + c.wid + '" title="déplacer vers un autre deck">' +
    '<option value=""' + (c.moveTo ? "" : " selected") + ">" + (c.moveTo ? "ne pas déplacer" : "déplacer…") + "</option>" +
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

/* « Back Extra » is skipped: the reason callout is the only place it appears, and nothing
   but the « vider Back Extra » toggle and the comment of a card whose flag is on writes it
   (specs/workspace.md#body). */
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
          '<div class="field-val' + (c.deleted ? "" : " editable") + '" data-act="ws-edit" data-wid="' + c.wid + '" data-field="' + esc(name) + '" title="cliquer pour éditer la valeur brute">' +
          (html || '<span class="muted">(vide)</span>') +
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
    (c.revealed ? "Masquer à nouveau" : "Révéler la réponse") +
    "</button>" +
    (c.revealed ? "" : '<span class="muted small">la note telle que vue au moment du flag</span>') +
    "</div>"
  );
}

/* ---------- conversation pane ---------- */

function chatPane() {
  const ws = S.ws;
  const configured = !S.chatStatus || S.chatStatus.configured !== false;
  const box = configured
    ? '<div class="chips">' +
      sourceChipsHtml() +
      "</div>" +
      '<textarea data-input="chat" data-focus="chat" placeholder="Demande une reformulation, un split, une vérification… (Entrée pour envoyer, Shift+Entrée pour un saut de ligne)"' +
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
      ">Envoyer</button></div>"
    : '<div class="banner">Ajoute ANTHROPIC_API_KEY dans .env puis relance anki-web</div>' +
      '<textarea disabled placeholder="chat indisponible"></textarea>';
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
  if (!sources.length) return '<span class="muted small">aucune source dans le corpus</span>';
  const attached = S.ws.chatSources.filter((id) => sourceById(id));
  const root = wsRoot();
  const anchored = (root && root.anchors) || [];
  const attachedHtml = attached
    .map(
      (id) =>
        '<span class="chip src" title="source jointe au contexte">' +
        esc(sourceById(id).target) +
        ' <b data-act="detach-src" data-src="' + esc(id) + '">×</b></span>',
    )
    .join("");
  const anchorHtml = anchored
    .filter((id) => sourceById(id) && attached.indexOf(id) < 0)
    .map(
      (id) =>
        '<button class="chip anchor" data-act="attach-src" data-src="' + esc(id) + '" title="ancrée à la racine — joindre son texte au contexte">⚓ joindre ' +
        esc(sourceById(id).target) +
        "</button>",
    )
    .join("");
  const rest = sources.filter((s) => attached.indexOf(s.id) < 0 && anchored.indexOf(s.id) < 0);
  const menu = rest.length
    ? '<select class="chip menu" data-input="attach-src-menu" title="joindre une source du corpus">' +
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
      ? "Pose ta question sur cette note."
      : "Demande à Claude de créer des cartes depuis les sources du corpus.";
    return (
      '<div class="empty">' + hint + "<br>" +
      "Les propositions de Claude apparaissent comme versions et cartes à gauche ; rien n'est écrit avant « Valider ».</div>"
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

/* Render a text segment with [n] citation markers linking to the source list. */
function textSegmentHtml(text, cites, byN) {
  let out = "";
  let at = 0;
  (cites || []).forEach((c) => {
    const pos = Math.min(Math.max(c.pos, at), text.length);
    const s = byN[c.n] || {};
    out +=
      linkify(text.slice(at, pos)) +
      '<a class="cite" href="' + esc(s.url || "#") + '" target="_blank" rel="noopener" title="' + esc(c.cited || "") + '">[' + c.n + "]</a>";
    at = pos;
  });
  return out + linkify(text.slice(at));
}

function bodyHtml(m) {
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
          ? ' <span class="muted small">dans le corpus</span>'
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
  const who = m.who === "user" ? "toi" : "claude";
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
      html += '<div class="reading">lit : ' + esc(part.tool || "?") + (part.summary ? " → " + linkify(part.summary) : "") + "</div>";
    } else if (part.type === "added") {
      html +=
        '<div class="reading">' +
        (part.error ? "ajout refusé : " + esc(part.error) : "ajoute : " + part.count + " note(s)") +
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
      (p.error ? "proposition " + esc(p.kind) + " refusée : " + esc(p.error) : p.landed ? esc(p.kind) + " " + p.landed : esc(p.kind) + " …") +
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
      (kind === "web" ? "page web" : kind === "pdf" ? "PDF" : "note du vault") +
      " à ajouter au corpus de " + esc(S.ws.deck) +
      (meta.length ? " · " + meta.join(" · ") : "") + "</div>" +
      '<input class="mono src-name" data-input="psrc-target" data-mi="' + mi + '" data-pi="' + pi + '" value="' +
      esc(target) +
      '" placeholder="https://… · note du vault · ~/doc.pdf"' +
      (p.applied ? " disabled" : "") +
      ">" +
      (kind === "web" && target
        ? '<div class="small"><a href="' + esc(target) + '" target="_blank" rel="noopener">ouvrir ↗</a></div>'
        : "") +
      (anchors.length ? '<div class="muted small">ancre : ' + anchors.map(short).join(" ") + "</div>" : "");
  } else if (p.kind === "create_source") {
    const anchors = input.anchor_note_ids || [];
    diff =
      '<div class="muted small">note Obsidian à créer dans le vault, ajoutée au corpus de ' + esc(S.ws.deck) + "</div>" +
      '<input class="mono src-name" data-input="psrc-name" data-mi="' + mi + '" data-pi="' + pi + '" value="' +
      esc(p.name != null ? p.name : input.name || "") +
      '" placeholder="dossier/nom de la note"' +
      (p.applied ? " disabled" : "") +
      ">" +
      (anchors.length ? '<div class="muted small">ancre : ' + anchors.map(short).join(" ") + "</div>" : "") +
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
    ? '<span class="applied-mark">appliqué ✓</span>' +
      (p.kind === "edit_source" ? '<button class="revert" data-act="ws-revert-src"' + at + disabled + ">Annuler</button>" : "")
    : '<button class="primary" data-act="ws-apply-src"' + at + disabled + ">Appliquer</button>";
  return (
    '<div class="proposal' + (p.applied ? " applied" : "") + '">' +
    '<div class="proposal-head"><span class="pkind">' +
    (p.kind === "add_source" ? "ajout de source" : p.kind === "create_source" ? "nouvelle source" : "source") +
    "</span>" +
    '<span class="grow"></span>' + head + "</div>" +
    (input.rationale ? '<div class="rationale">' + nl2br(input.rationale) + "</div>" : "") +
    '<div class="diff">' + diff + "</div>" +
    (p.error ? '<div class="banner">' + esc(p.error) + "</div>" : "") +
    "</div>"
  );
}
