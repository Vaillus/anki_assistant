/* Workspace data model and state helpers (specs/workspace.md).
   Loaded after render.js, before ws-render.js. All globals — no bundler. */

"use strict";

const MAX_CARDS = 50; // same cap as chat.py (specs/workspace.md#how-notes-enter)

/* ---------------- state ---------------- */

/* One card of the workspace. `versions[0]` is v0 (Anki) for an existing note; a draft note has
   no v0. `vi` is the shown version. */
function cardFromNote(n, parentWid) {
  const isRoot = S.ws.cards.length === 0;
  return {
    wid: String(S.ws.nextWid++),
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
    versions: [{ fields: Object.assign({}, n.fields || {}), by: "anki" }],
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
    wid: String(S.ws.nextWid++),
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
    versions: [{ fields: Object.assign({}, spec.fields || {}), by: "claude" }],
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
    rootWid: n ? "1" : null,
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
    rejected: [], // « 3 v2 » per dropped version, told to Claude in the history
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
      n + " modified card(s) not yet applied will be lost. Close anyway?",
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
function pushVersion(card, fields, by, model) {
  var effective = model || shownModel(card);
  var v = { fields: Object.assign({}, fields), by: by };
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

/* « retirer »: remove the card and its descendant fragments from the workspace. */
function removeCard(card) {
  const toRemove = new Set();
  function collect(wid) {
    toRemove.add(wid);
    S.ws.cards.forEach((c) => {
      if (c.parentWid === wid) collect(c.wid);
    });
  }
  collect(card.wid);
  S.ws.cards = S.ws.cards.filter((c) => !toRemove.has(c.wid));
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
    v = { fields: Object.assign({}, v.fields), by: "user" };
    if (shownModel(card) !== card.model) v.model = shownModel(card);
    card.versions.push(v);
    card.vi = card.versions.length - 1;
  }
  v.fields[name] = value;
  v.edited = true;
}

/* ---------------- proposals landing on the workspace ---------------- */

/* `target` is a workspace number or an Anki note id in digits; an absent note is fetched and
   added (specs/chat.md#proposal-tools). */
async function resolveTarget(target) {
  const t = String(target === null || target === undefined ? "" : target).trim();
  const byWid = wsCard(t);
  if (byWid) return byWid;
  if (!/^\d+$/.test(t)) throw new Error("unknown target: " + t);
  const nid = Number(t);
  const byNote = wsCardByNote(nid);
  if (byNote) return byNote;
  const notes = await lookupNotes([nid]);
  if (!notes.length) throw new Error("note not found: #" + t);
  return addNoteCard(notes[0]);
}

async function lookupNotes(ids) {
  const missing = ids.filter((id) => !wsCardByNote(id));
  if (!missing.length) return [];
  if (S.ws.cards.length + missing.length > MAX_CARDS) {
    throw new Error("workspace full (" + MAX_CARDS + " cards)");
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
    pushVersion(card, fields, "claude", newModel);
    if (Array.isArray(inp.tags)) card.tags = inp.tags.slice();
    return "→ card " + card.wid;
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
    });
    return "→ card " + card.wid;
  }
  if (kind === "move") {
    const card = await resolveTarget(inp.target);
    card.moveTo = inp.deck || null;
    return "→ card " + card.wid + " → " + esc(inp.deck || "?");
  }
  throw new Error("unknown proposal: " + kind);
}

async function landAdded(noteIds) {
  const notes = await lookupNotes((noteIds || []).map(Number));
  notes.forEach((n) => addNoteCard(n));
  return notes.length;
}

/* ---------------- chat serialisation ---------------- */

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
        else if (part.type === "reading") bits.push("[read: " + (part.tool || "?") + (part.summary ? " → " + part.summary : "") + "]");
        else if (part.type === "added") bits.push("[added: " + part.count + " notes]");
      });
    } else {
      bits.push(textWithMarkers(m));
    }
    if ((m.sources || []).length) {
      bits.push("[sources: " + m.sources.map((s) => "[" + s.n + "] " + s.url).join(", ") + "]");
    }
    (m.proposals || []).forEach((p) => {
      if (p.landed) bits.push("[proposal: " + p.kind + " " + p.landed + "]");
      else if (isSourceProposal(p.kind)) {
        bits.push("[proposal: " + p.kind + (p.applied ? " (applied)" : "") + "]");
      }
    });
    if (i === lastAssistant) {
      S.ws.rejected.forEach((r) => bits.push("[rejected version: " + r + "]"));
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

/* ---------------- validation plan ---------------- */

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
