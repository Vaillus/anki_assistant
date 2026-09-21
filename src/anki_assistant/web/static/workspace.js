/* Workspace chat stream, source proposals, validation, undo, and event dispatch.
   Loaded after ws-render.js, before app.js. State helpers are in ws-state.js;
   HTML rendering is in ws-render.js. */

"use strict";

/* ---------------- chat log refresh ---------------- */

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

/* ---------------- chat send/stream ---------------- */

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
        const entry = { type: "added", count: 0, error: "" };
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
        reply.error = d.detail || "error";
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

/* ---------------- source proposals ---------------- */

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
      if (!target) throw new Error("provide a target");
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
      if (!name) throw new Error("provide a note name");
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
      throw new Error("unknown proposal: " + p.kind);
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
    p.error = e.status === 409 ? "modified since, cannot revert" : e.message;
    S.busy = false;
    draw();
  }
}

/* ---------------- validation ---------------- */

async function validateWorkspace() {
  if (!S.ws || S.ws.applying || S.ws.chatBusy) return;
  const ws = S.ws;
  const plan = planFromWs();
  if (!plan.cards.length) return;
  const deleting = ws.cards.filter((c) => c.noteId && c.deleted);
  if (deleting.length) {
    const ok = window.confirm(
      "Permanently delete " +
        deleting.length +
        " note(s): " +
        deleting.map((c) => short(c.noteId)).join(", ") +
        "?",
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
      "A proposed source has not been applied: notes anchored to it will lose that anchor. Apply anyway?",
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
  ws.report = report || { ok: false, errors: ["empty response"], created: {} };
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
    S.ws.chatDraft = "split " + card.wid + ": ";
    S.refocus = "chat";
    return draw();
  }
  if (act === "ws-remove" && card) {
    removeCard(card);
    return draw();
  }
  if (!card) return undefined;
  if (act === "ws-toggle") {
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
