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
  return {
    wid: "w" + S.ws.nextWid++,
    noteId: n.note_id,
    parentWid: parentWid || null,
    model: n.model,
    tags: (n.tags || []).slice(),
    originalTags: (n.tags || []).slice(),
    deck: n.deck || S.ws.deck,
    flaggedClozes: flaggedClozes(n),
    reason: n.reason || "",
    anchors: null, // [source id] once fetched
    versions: [{ fields: Object.assign({}, n.fields || {}), by: "anki", rationale: "" }],
    vi: 0,
    active: true,
    deleted: false,
    keep: false,
    defer: null, // { comment } when marked « à revoir » (specs/workspace.md#vocabulary)
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
    reason: "",
    anchors: (spec.anchors || []).slice(),
    versions: [{ fields: Object.assign({}, spec.fields || {}), by: "claude", rationale: spec.rationale || "" }],
    vi: 0,
    active: true,
    deleted: false,
    keep: false,
    defer: null,
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

function shownVersion(c) {
  return c.versions[c.vi];
}

function shownFields(c) {
  return shownVersion(c).fields;
}

/* An existing note's card is changed when it is not on v0 or carries a state; a draft always is. */
function cardChanged(c) {
  if (!c.noteId) return true;
  return (
    c.vi > 0 || c.deleted || c.keep || !!c.defer || !!c.moveTo || tagsChanged(c) || shownModel(c) !== c.model
  );
}

function tagsChanged(c) {
  return c.tags.join(" ") !== c.originalTags.join(" ");
}

/* Counts for the « Valider » label and the close confirmation. */
function wsChanges() {
  const out = { edited: 0, created: 0, deleted: 0, kept: 0, deferred: 0, moved: 0 };
  (S.ws ? S.ws.cards : []).forEach((c) => {
    if (!c.noteId) {
      out.created++;
      if (c.defer) out.deferred++; // created flagged: counts as both
    } else if (c.deleted) out.deleted++;
    else {
      if (c.defer) out.deferred++; // an edited + deferred card counts once, as « à revoir »
      else if (c.vi > 0 || tagsChanged(c)) out.edited++;
      else if (c.keep) out.kept++;
      if (c.moveTo) out.moved++;
    }
  });
  out.total = (S.ws ? S.ws.cards : []).filter(cardChanged).length;
  return out;
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
  const n = noteById(noteId);
  if (!n || S.ws) return;
  S.selNote = noteId;
  S.ws = {
    rootWid: "w1",
    deck: n.deck || S.deck,
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
  addNoteCard(n);
  S.refocus = "chat";
  draw();
}

/* Discard everything (specs/workspace.md#opening-and-closing). */
async function closeWorkspace(force) {
  if (!S.ws || S.ws.applying) return;
  const n = wsChanges().total;
  if (!force && n > 0) {
    const ok = window.confirm(
      n + " changement(s) non validé(s) seront perdus. Fermer quand même ?",
    );
    if (!ok) return;
  }
  const root = wsRoot();
  S.ws = null;
  draw();
  await afterDecision({ resolvedId: root ? root.noteId : null, keepSelection: true });
}

/* Mark a card « à revoir » — or unmark it. The comment starts as the plain text of
   « Back Extra » as v0 holds it (the shown version, for a draft), so that the existing
   reason is completed rather than lost. */
function toggleDefer(card) {
  if (card.defer) {
    card.defer = null;
    return;
  }
  const fields = card.noteId ? card.versions[0].fields : shownFields(card);
  card.defer = { comment: plainText(fields[REASON_FIELD] || "") };
  card.keep = false;
  if (hasReasonField(card)) S.refocus = "ws-comment-" + card.wid;
}

function hasReasonField(card) {
  return Object.prototype.hasOwnProperty.call(shownFields(card), REASON_FIELD);
}

/* ---------------- versions ---------------- */

function pushVersion(card, fields, by, rationale, model) {
  var v = { fields: Object.assign({}, fields), by: by, rationale: rationale || "" };
  if (model) v.model = model;
  card.versions.push(v);
  card.vi = card.versions.length - 1;
  card.deleted = false; // a rewrite supersedes a deletion (specs/chat.md#proposal-tools)
  card.keep = false;
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
    if (shownVersion(card).model) v.model = shownVersion(card).model;
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
    pushVersion(card, fields, "claude", inp.rationale, modelChanged ? newModel : null);
    if (Array.isArray(inp.tags)) card.tags = inp.tags.slice();
    return "→ carte " + card.wid;
  }
  if (kind === "split") {
    const card = await resolveTarget(inp.target);
    if (inp.original === null || inp.original === undefined) {
      card.deleted = true;
      card.keep = false;
    } else {
      pushVersion(
        card,
        Object.assign({}, shownFields(card), (inp.original && inp.original.fields) || {}),
        "claude",
        inp.rationale,
      );
    }
    const made = (inp.new_notes || []).map((nn) =>
      addDraftCard({
        parentWid: card.wid,
        model: nn.model || card.model,
        fields: nn.fields || {},
        tags: card.tags,
        deck: card.deck,
        anchors: card.anchors || [],
        rationale: inp.rationale,
      }),
    );
    return "→ " + (made.length + (inp.original ? 1 : 0)) + " cartes";
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
    keep: c.keep,
    defer: !!c.defer,
    comment: c.defer ? c.defer.comment : "",
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
    const bits = [m.text || ""];
    (m.reads || []).forEach((r) => {
      bits.push("[lecture: " + (r.tool || "?") + (r.summary ? " → " + r.summary : "") + "]");
    });
    (m.added || []).forEach((a) => bits.push("[ajout: " + a.count + " notes]"));
    (m.proposals || []).forEach((p) => {
      if (p.landed) bits.push("[proposition: " + p.kind + " " + p.landed + "]");
      else if (p.kind === "create_source" || p.kind === "edit_source") {
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

let wsLogRefreshQueued = false;
function scheduleLogRefresh() {
  if (wsLogRefreshQueued) return;
  wsLogRefreshQueued = true;
  requestAnimationFrame(() => {
    wsLogRefreshQueued = false;
    refreshChatLog();
  });
}

/* Narrow refresh used while a reply streams in, so the textarea keeps focus. */
function refreshChatLog() {
  const log = document.getElementById("chat-log");
  if (!log) return;
  log.innerHTML = chatLogHtml();
  log.scrollTop = log.scrollHeight;
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
  const reply = { who: "assistant", text: "", reads: [], added: [], proposals: [], streaming: true, error: "" };
  ws.chat.push(reply);
  ws.chatDraft = "";
  ws.chatBusy = true;
  draw();

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
        reply.text += d.delta || "";
        scheduleLogRefresh();
      } else if (name === "reading") {
        reply.reads.push({ tool: d.tool || "?", input: d.input || {}, summary: d.summary || "" });
        scheduleLogRefresh();
      } else if (name === "added") {
        const entry = { count: 0, rationale: d.rationale || "", error: "" };
        reply.added.push(entry);
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
          applied: false,
          landed: "",
          error: "",
        };
        reply.proposals.push(p);
        if (p.kind !== "create_source" && p.kind !== "edit_source") {
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
    if (p.kind === "create_source") {
      const name = String(p.name != null ? p.name : input.name || "").trim();
      if (!name) throw new Error("indique un nom de note");
      await API.createSourceNote({
        deck: S.ws.deck,
        name,
        content: input.content || "",
        anchor_note_ids: input.anchor_note_ids || [],
        id: p.sourceId || null,
      });
      (input.anchor_note_ids || []).forEach((id) => {
        const c = wsCardByNote(id);
        if (c) {
          c.anchors = null;
          loadCardAnchors(c);
        }
      });
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

/* The plan of specs/workspace.md#what-is-written: deleted → kept → edited → deferred, each
   maybe moved; an edited card may carry the `defer` modifier. */
function planFromWs() {
  const cards = [];
  S.ws.cards.forEach((c) => {
    if (!c.noteId) {
      cards.push({
        wid: c.wid,
        action: "create",
        parent_wid: c.parentWid,
        deck: c.deck,
        model: c.model,
        fields: shownFields(c),
        tags: c.tags,
        source_ids: c.anchors || [],
        defer: !!c.defer,
        comment: c.defer ? c.defer.comment : null,
      });
    } else if (c.deleted) {
      cards.push({ wid: c.wid, action: "delete", note_id: c.noteId });
    } else if (c.vi > 0 || tagsChanged(c)) {
      const card = { wid: c.wid, action: "edit", note_id: c.noteId, fields: shownFields(c), move_to: c.moveTo };
      if (tagsChanged(c)) card.tags = c.tags;
      const em = shownModel(c);
      if (em !== c.model) card.model = em;
      if (c.defer) {
        card.defer = true;
        card.comment = c.defer.comment;
      }
      cards.push(card);
    } else if (c.defer) {
      cards.push({ wid: c.wid, action: "defer", note_id: c.noteId, comment: c.defer.comment, move_to: c.moveTo });
    } else if (c.keep || c.moveTo) {
      cards.push({ wid: c.wid, action: "keep", note_id: c.noteId, move_to: c.moveTo });
    }
  });
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
    (m.proposals || []).some((p) => p.kind === "create_source" && !p.applied),
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
    ws.report = { ok: false, errors: [e.message], rolled_back: false, created: {} };
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
  if (act === "attach-src") return attachSource(el.getAttribute("data-src"));
  if (act === "detach-src") return detachSource(el.getAttribute("data-src"));
  if (act === "ws-dismiss-report") {
    ws.report = null;
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
  else if (act === "ws-delete") {
    card.deleted = !card.deleted;
    if (card.deleted) {
      card.keep = false;
      card.defer = null;
    }
  } else if (act === "ws-keep") {
    card.keep = !card.keep;
    if (card.keep) card.defer = null;
  } else if (act === "ws-defer") toggleDefer(card);
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
    if (card && card.defer) card.defer.comment = el.value; // no redraw, as for a field
  } else if (key === "ws-move") {
    const card = wsCard(el.getAttribute("data-wid"));
    if (card) {
      card.moveTo = el.value || null;
      draw();
    }
  } else if (key === "psrc-name") {
    const msg = ws.chat[Number(el.getAttribute("data-mi"))];
    const p = msg && msg.proposals && msg.proposals[Number(el.getAttribute("data-pi"))];
    if (p) p.name = el.value;
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
    (r.rolled_back ? "Tout a été remis en place." : "Certaines écritures n'ont pas pu être annulées.") +
    (r.errors || []).map((x) => "<br>" + esc(x)).join("") +
    '</div><button class="ghost" data-act="ws-dismiss-report">×</button></div>'
  );
}

/* Root first, then arrival order; each fragment right after its parent. */
function wsOrderedCards() {
  const cards = S.ws.cards;
  const out = [];
  const byParent = {};
  cards.forEach((c) => {
    if (c.parentWid && wsCard(c.parentWid)) (byParent[c.parentWid] = byParent[c.parentWid] || []).push(c);
  });
  cards.forEach((c) => {
    if (c.parentWid && wsCard(c.parentWid)) return;
    out.push({ card: c, fragment: false });
    (byParent[c.wid] || []).forEach((f) => out.push({ card: f, fragment: true }));
  });
  return out;
}

function wsCardsHtml() {
  return wsOrderedCards()
    .map((x) => wsCardHtml(x.card, x.fragment))
    .join("");
}

function wsCardHtml(c, isFragment) {
  const v = shownVersion(c);
  const n = c.versions.length;
  const cls =
    "ws-card" +
    (c.active ? "" : " off") +
    (c.deleted ? " deleted" : "") +
    (isFragment ? " fragment" : "") +
    (c.noteId ? "" : " draft");
  const badges =
    (c.flaggedClozes.length && c.noteId
      ? '<span class="badge">⚑ ' + c.flaggedClozes.map((k) => "c" + k).join(" ") + "</span>"
      : "") +
    (c.deleted ? '<span class="badge state">supprimée</span>' : "") +
    (c.keep && !c.deleted ? '<span class="badge state ok">gardée</span>' : "") +
    (c.defer && !c.deleted ? '<span class="badge state later">à revoir</span>' : "") +
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
  const actions =
    (canInvalidate
      ? '<button class="ghost small" data-act="ws-invalidate" data-wid="' + c.wid + '" title="retirer cette version">invalider</button>'
      : "") +
    (c.noteId
      ? '<button class="ghost small' + (c.deleted ? "" : " dangerish") + '" data-act="ws-delete" data-wid="' + c.wid + '">' +
        (c.deleted ? "restaurer" : "supprimer") +
        "</button>"
      : "") +
    (c.noteId && !c.deleted && !c.defer && c.vi === 0 && !tagsChanged(c)
      ? '<button class="ghost small" data-act="ws-keep" data-wid="' + c.wid + '" title="résoudre sans changement">' +
        (c.keep ? "ne pas garder" : "garder") +
        "</button>"
      : "") +
    (!c.deleted && !c.keep
      ? '<button class="ghost small" data-act="ws-defer" data-wid="' + c.wid + '" title="' +
        (c.noteId ? "garder le flag et noter un commentaire, sans résoudre" : "créer la note flaguée, avec un commentaire") +
        '">' +
        (c.defer ? "ne pas différer" : "différer") +
        "</button>"
      : "") +
    (c.noteId && !c.deleted ? movePickerHtml(c) : "");
  const effectiveModel = shownModel(c);
  const modelChanged = c.noteId && effectiveModel !== c.model;
  const identity =
    '<span class="tag">' +
    (c.noteId ? esc(short(c.noteId)) : "brouillon") +
    " · " +
    esc(effectiveModel || "") +
    (modelChanged ? ' <b title="type changé (était ' + esc(c.model) + ')">⇄</b>' : "") +
    (c.deck && c.deck !== S.ws.deck ? " · " + esc(c.deck) : "") +
    (c.tags.length ? " · " + esc(c.tags.join(" ")) : "") +
    "</span>";
  const meta =
    v.by === "claude" || v.by === "user" || v.edited
      ? '<div class="ws-version">' +
        (v.by === "claude" ? "proposée par Claude" : "version éditée") +
        (v.by === "claude" && v.edited ? " · éditée" : "") +
        (v.rationale ? " — " + esc(v.rationale) : "") +
        "</div>"
      : "";
  return (
    '<div class="' + cls + '" data-wid="' + c.wid + '">' +
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
    (c.defer && !c.deleted
      ? wsCommentHtml(c)
      : c.noteId && c.reason
        ? '<div class="reason"><span class="reason-label">raison du flag</span>' + nl2br(c.reason) + "</div>"
        : "") +
    wsRevealHtml(c) +
    "</div>"
  );
}

/* The reason callout of a deferred card: the comment to be written (specs/workspace.md#body). */
function wsCommentHtml(c) {
  if (!hasReasonField(c)) {
    return (
      '<div class="reason defer"><span class="reason-label">à revoir</span>' +
      "pas de champ " + esc(REASON_FIELD) + " : le flag sera posé sans commentaire</div>"
    );
  }
  return (
    '<div class="reason defer"><span class="reason-label">raison du flag · sera écrite</span>' +
    '<textarea data-input="ws-comment" data-wid="' + c.wid + '" data-focus="ws-comment-' + c.wid + '" rows="2"' +
    ' placeholder="pourquoi cette note reste à revoir">' + esc(c.defer.comment) + "</textarea></div>"
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
   but the « vider Back Extra » toggle and a deferred card's comment writes it
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
      '<textarea data-input="chat" data-focus="chat" placeholder="Demande une reformulation, un split, une vérification… (⌘/Ctrl+Entrée pour envoyer)"' +
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
    return (
      '<div class="empty">Pose ta question sur cette note.<br>' +
      "Les propositions de Claude apparaissent comme versions et cartes à gauche ; rien n'est écrit avant « Valider ».</div>"
    );
  }
  return S.ws.chat.map(msgHtml).join("");
}

function msgHtml(m, mi) {
  const who = m.who === "user" ? "toi" : "claude";
  const body = esc(m.text || "").replace(/\n/g, "<br>") + (m.streaming ? '<span class="cursor">▍</span>' : "");
  const reads = (m.reads || [])
    .map((r) => '<div class="reading">lit : ' + esc(r.tool || "?") + (r.summary ? " → " + esc(r.summary) : "") + "</div>")
    .join("");
  const added = (m.added || [])
    .map(
      (a) =>
        '<div class="reading">' +
        (a.error ? "ajout refusé : " + esc(a.error) : "ajoute : " + a.count + " note(s)") +
        (a.rationale ? " — " + esc(a.rationale) : "") +
        "</div>",
    )
    .join("");
  const props = (m.proposals || []).map((p, pi) => proposalHtml(p, mi, pi)).join("");
  return (
    '<div class="msg ' + (m.who === "user" ? "user" : "assistant") + '"><div class="who">' + who + "</div>" +
    reads +
    added +
    body +
    props +
    (m.error ? '<div class="banner">' + esc(m.error) + "</div>" : "") +
    "</div>"
  );
}

/* Card proposals are one pointer line; source proposals keep their inline card. */
function proposalHtml(p, mi, pi) {
  const input = p.input || {};
  if (p.kind !== "create_source" && p.kind !== "edit_source") {
    return (
      '<div class="ws-pointer">' +
      (p.error ? "proposition " + esc(p.kind) + " refusée : " + esc(p.error) : p.landed ? esc(p.kind) + " " + p.landed : esc(p.kind) + " …") +
      "</div>"
    );
  }
  let diff = "";
  if (p.kind === "create_source") {
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
    '<div class="proposal-head"><span class="pkind">' + (p.kind === "create_source" ? "nouvelle source" : "source") + "</span>" +
    '<span class="grow"></span>' + head + "</div>" +
    (input.rationale ? '<div class="rationale">' + nl2br(input.rationale) + "</div>" : "") +
    '<div class="diff">' + diff + "</div>" +
    (p.error ? '<div class="banner">' + esc(p.error) + "</div>" : "") +
    "</div>"
  );
}
