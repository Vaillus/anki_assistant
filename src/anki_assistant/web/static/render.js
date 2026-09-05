/* Full re-render of the page from S. Same approach as the prototype: one draw()
   rebuilding #app, plus a narrower refreshChatLog() used while a reply streams in. */

"use strict";

const FOLD = 4000; // source text fold, per specs/sources.md

function draw() {
  const root = document.getElementById("app");
  if (!root) return;
  const scroll = captureScroll(root);
  root.innerHTML =
    '<div class="app">' + deckColumn() + queueColumn() + rightColumn() + "</div>";
  restoreScroll(root, scroll);
  if (S.refocus) {
    const el = root.querySelector('[data-focus="' + S.refocus + '"]');
    S.refocus = null;
    if (el) {
      el.focus();
      if (el.setSelectionRange && typeof el.value === "string") {
        el.setSelectionRange(el.value.length, el.value.length);
      }
    }
  }
  const log = root.querySelector("#chat-log");
  if (log) log.scrollTop = log.scrollHeight;
}

function captureScroll(root) {
  const out = {};
  root.querySelectorAll("[data-scroll]").forEach((el) => {
    out[el.getAttribute("data-scroll")] = el.scrollTop;
  });
  return out;
}

function restoreScroll(root, scroll) {
  root.querySelectorAll("[data-scroll]").forEach((el) => {
    const key = el.getAttribute("data-scroll");
    if (scroll[key]) el.scrollTop = scroll[key];
  });
}

/* ---------------- column 1 — decks ---------------- */

function deckColumn() {
  const decks = visibleDecks();
  const rows = decks.length
    ? decks.map(deckRow).join("")
    : '<div class="empty">Aucun deck flagué 🎉</div>';
  return (
    '<div class="col" data-scroll="decks">' +
    '<div class="col-head"><h2>Decks</h2><span class="grow"></span>' +
    '<button class="ghost" data-act="alldecks">' +
    (S.showAllDecks ? "flagués seuls" : "tous") +
    "</button></div>" +
    rows +
    "</div>"
  );
}

function deckRow(d) {
  const own = d.flagged_own || 0;
  const total = d.flagged_total || 0;
  const kinds = (d.source_kinds || [])
    .map((k) => '<span class="kind ' + esc(k) + '" title="' + esc(k) + '">' + esc(k.charAt(0)) + "</span>")
    .join("");
  return (
    '<div class="deck' +
    (d.name === S.deck ? " sel" : "") +
    (total ? "" : " dim") +
    '" data-act="deck" data-deck="' +
    esc(d.name) +
    '" title="' +
    esc(d.name) +
    '" style="padding-left:' +
    (12 + (d.depth || 0) * 14) +
    'px">' +
    '<span class="name">' +
    esc(d.leaf || d.name) +
    "</span>" +
    kinds +
    (own ? '<span class="badge">' + own + "</span>" : "") +
    (total > own ? '<span class="badge sub">' + total + "</span>" : "") +
    "</div>"
  );
}

/* ---------------- column 2 — queue ---------------- */

function queueColumn() {
  const head =
    '<div class="col-head"><h2>' +
    (S.deck ? esc(String(S.deck).split("::").pop()) : "File") +
    "</h2>" +
    (S.notes
      ? '<span class="grow"></span><span class="muted small">' +
        (S.notes.flagged || 0) +
        " flaguée(s) / " +
        (S.notes.total || 0) +
        '</span><button class="ghost" data-act="onlyflagged">' +
        (S.onlyFlagged ? "voir toutes" : "flaguées seules") +
        "</button>"
      : "") +
    "</div>";

  let body;
  if (!S.deck) body = '<div class="empty">← choisis un deck</div>';
  else if (!S.notes) body = '<div class="empty">chargement…</div>';
  else {
    const vis = visibleNotes();
    body = vis.length
      ? vis.map(noteCard).join("")
      : '<div class="empty">Rien à revoir ici 🎉</div>';
  }
  return (
    '<div class="col" data-scroll="queue">' + head + errorBanner() + body + "</div>"
  );
}

function errorBanner() {
  if (!S.error) return "";
  return (
    '<div class="banner"><span class="grow">' +
    esc(S.error) +
    '</span><button class="ghost" data-act="dismiss-error">×</button></div>'
  );
}

function noteCard(n) {
  const sel = n.note_id === S.selNote;
  const flagInfo = (n.flag_colors || []).join(", ");
  return (
    '<div class="note' +
    (sel ? " sel" : "") +
    (n.flagged ? "" : " unflagged") +
    '" data-act="note" data-note="' +
    n.note_id +
    '">' +
    '<div class="note-top">' +
    (n.flagged
      ? '<span class="badge" title="' + esc(flagInfo) + '">⚑</span>'
      : '<span class="badge zero">·</span>') +
    '<span class="tag">' +
    short(n.note_id) +
    " · " +
    esc(n.model) +
    " · " +
    nCards(n) +
    " carte(s)" +
    ((n.tags || []).length ? " · " + esc((n.tags || []).join(" ")) : "") +
    "</span><span class=\"grow\"></span>" +
    '<button class="ghost" data-act="ref" data-note="' +
    n.note_id +
    '" title="ajouter la note au contexte du chat">→ chat</button>' +
    "</div>" +
    reasonHtml(n) +
    fieldsHtml(n) +
    (sel ? actionsHtml(n) : "") +
    "</div>"
  );
}

function reasonHtml(n) {
  if (!n.flagged || !n.reason) return "";
  return (
    '<div class="reason"><span class="reason-label">raison du flag</span>' +
    nl2br(n.reason) +
    "</div>"
  );
}

function fieldsHtml(n) {
  const html = n.fields_html || {};
  const names = Object.keys(html);
  return names
    .map((name) => {
      const v = html[name];
      if (!v) return "";
      return (
        '<div class="field-name">' +
        esc(name) +
        '</div><div class="field-val' +
        (name === REASON_FIELD ? " small" : "") +
        '">' +
        v +
        "</div>"
      );
    })
    .join("");
}

function actionsHtml(n) {
  const dis = S.busy ? " disabled" : "";
  return (
    '<div class="actions">' +
    ACTIONS.map(
      (a) =>
        '<button class="' +
        a.cls +
        '" data-act="decision" data-decision="' +
        a.key +
        '" data-note="' +
        n.note_id +
        '"' +
        dis +
        ">" +
        a.label +
        "</button>",
    ).join("") +
    (S.busy ? '<span class="muted small">…</span>' : "") +
    "</div>"
  );
}

/* ---------------- column 3 — Source / Chat ---------------- */

function rightColumn() {
  const head =
    '<div class="col-head"><div class="tabs">' +
    '<button class="' +
    (S.tab === "source" ? "on" : "") +
    '" data-act="tab" data-tab="source">Source</button>' +
    '<button class="' +
    (S.tab === "chat" ? "on" : "") +
    '" data-act="tab" data-tab="chat">Chat</button>' +
    "</div></div>";
  return '<div class="col">' + head + (S.tab === "source" ? sourcePane() : chatPane()) + "</div>";
}

/* ---------- Source tab ---------- */

function sourcePane() {
  if (!S.deck) return '<div class="pane"><div class="empty">Choisis un deck</div></div>';
  if (S.corpusLoading && !S.corpus)
    return '<div class="pane"><div class="empty">chargement…</div></div>';
  const c = S.corpus;
  const sources = (c && c.sources) || [];
  let body = "";
  if (c && c.inherited_from) {
    body +=
      '<div class="banner info">Corpus hérité de <b>' +
      esc(c.inherited_from) +
      "</b></div>";
  }
  body += sources.length
    ? sources.map(sourceRow).join("")
    : '<div class="empty">Aucune source pour ce deck.</div>';
  body += S.srcForm ? sourceForm() : '<button data-act="add-src">+ ajouter une source</button>';
  return '<div class="pane" data-scroll="source">' + body + "</div>";
}

function sourceRow(s, i) {
  const meta = [];
  if (s.pages) meta.push("pages " + esc(s.pages));
  if (s.note) meta.push(esc(s.note));
  if (s.n_pages) meta.push(s.n_pages + " p.");
  const inherited = s.on_deck && S.deck && s.on_deck !== S.deck;
  const text = String(s.text || "");
  const expanded = !!S.srcExpanded[i];
  const shown = expanded ? text : fold(text, FOLD);
  return (
    '<div class="source">' +
    '<div class="source-head">' +
    '<span class="kind ' +
    esc(s.kind) +
    '">' +
    esc(s.kind) +
    "</span> <b>" +
    esc(s.target) +
    "</b>" +
    (meta.length ? ' <span class="muted small">· ' + meta.join(" · ") + "</span>" : "") +
    '<span class="grow"></span>' +
    (s.uri ? '<a href="' + esc(s.uri) + '" class="small">ouvrir ↗</a> ' : "") +
    '<button class="ghost small" data-act="remove-src" data-i="' +
    i +
    '">retirer</button>' +
    "</div>" +
    (inherited ? '<div class="muted small">héritée de ' + esc(s.on_deck) + "</div>" : "") +
    (s.exists === false ? '<div class="warn">⚠ fichier introuvable</div>' : "") +
    (s.warning ? '<div class="warn">⚠ ' + esc(s.warning) + "</div>" : "") +
    (text ? '<pre class="excerpt">' + esc(shown) + "</pre>" : '<div class="muted small">(aucun texte extrait)</div>') +
    (text.length > FOLD
      ? '<button class="ghost small" data-act="expand-src" data-i="' +
        i +
        '">' +
        (expanded ? "replier" : "afficher plus (" + text.length + " car.)") +
        "</button>"
      : "") +
    (s.truncated ? '<div class="muted small">texte tronqué côté serveur</div>' : "") +
    "</div>"
  );
}

function sourceForm() {
  const f = S.srcForm || {};
  const inherited = !!(S.corpus && S.corpus.inherited_from);
  return (
    '<div class="src-form">' +
    "<b>Nouvelle source</b>" +
    (S.srcForm.error ? '<div class="banner">' + esc(S.srcForm.error) + "</div>" : "") +
    "<label>cible (note du vault ou chemin de PDF)</label>" +
    '<input data-input="src-target" data-focus="src-target" list="vault-notes" value="' +
    esc(f.target || "") +
    '" placeholder="Allocation sur des angles disjoints">' +
    '<datalist id="vault-notes"></datalist>' +
    "<label>type</label>" +
    '<select data-input="src-kind">' +
    '<option value="obsidian"' +
    (f.kind === "pdf" ? "" : " selected") +
    ">obsidian</option>" +
    '<option value="pdf"' +
    (f.kind === "pdf" ? " selected" : "") +
    ">pdf</option>" +
    "</select>" +
    "<label>pages (pdf, ex. 12-19)</label>" +
    '<input data-input="src-pages" value="' +
    esc(f.pages || "") +
    '">' +
    "<label>note</label>" +
    '<input data-input="src-note" value="' +
    esc(f.note || "") +
    '">' +
    (inherited
      ? '<div class="muted small" style="margin-top:8px">Enregistrer écrit sur <b>' +
        esc(S.deck) +
        "</b> : ce deck n'héritera plus de <b>" +
        esc(S.corpus.inherited_from) +
        "</b>.</div>" +
        '<label class="check" style="margin-top:6px"><input type="checkbox" data-input="src-copy"' +
        (f.copyInherited === false ? "" : " checked") +
        "> copier les sources héritées d'abord</label>"
      : "") +
    '<div class="row" style="margin-top:10px;justify-content:flex-end">' +
    '<button data-act="src-cancel">Annuler</button>' +
    '<button class="primary" data-act="src-save"' +
    (f.saving ? " disabled" : "") +
    ">Enregistrer</button>" +
    "</div></div>"
  );
}

/* ---------- Chat tab ---------- */

function chatPane() {
  const configured = !S.chatStatus || S.chatStatus.configured !== false;
  const sel = selectedNote();
  const chips =
    (sel ? '<span class="chip">note ' + short(sel.note_id) + " (sélection)</span>" : "") +
    S.chatRefs
      .filter((id) => !sel || id !== sel.note_id)
      .map(
        (id) =>
          '<span class="chip">' +
          short(id) +
          ' <b data-act="unref" data-note="' +
          id +
          '">×</b></span>',
      )
      .join("");
  const box = configured
    ? '<div class="chips">' +
      chips +
      "</div>" +
      '<textarea data-input="chat" data-focus="chat" placeholder="Demande une reformulation, un split, une vérification… (⌘/Ctrl+Entrée pour envoyer)"' +
      (S.chatBusy ? " disabled" : "") +
      ">" +
      esc(S.chatDraft) +
      "</textarea>" +
      '<div class="row" style="justify-content:flex-end;margin-top:4px">' +
      (S.chatStatus && S.chatStatus.model
        ? '<span class="tag grow">' + esc(S.chatStatus.model) + "</span>"
        : '<span class="grow"></span>') +
      '<button class="primary" data-act="send"' +
      (S.chatBusy ? " disabled" : "") +
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

function chatLogHtml() {
  if (!S.chat.length) {
    return (
      '<div class="empty">Sélectionne une note, puis pose ta question.<br>' +
      "« → chat » sur une note l'ajoute au contexte.</div>"
    );
  }
  return S.chat.map(msgHtml).join("");
}

function msgHtml(m, mi) {
  const who = m.who === "user" ? "toi" : "claude";
  const refs = (m.refs || []).length ? " · " + m.refs.map(short).join(" ") : "";
  const body =
    esc(m.text || "").replace(/\n/g, "<br>") +
    (m.streaming ? '<span class="cursor">▍</span>' : "");
  const props = (m.proposals || []).map((p, pi) => proposalHtml(p, mi, pi)).join("");
  return (
    '<div class="msg ' +
    (m.who === "user" ? "user" : "assistant") +
    '"><div class="who">' +
    who +
    refs +
    "</div>" +
    body +
    props +
    (m.error ? '<div class="banner">' + esc(m.error) + "</div>" : "") +
    "</div>"
  );
}

const PROPOSAL_LABEL = { edit: "edit", split: "split", create: "create", move: "move" };

function proposalHtml(p, mi, pi) {
  const input = p.input || {};
  const note = input.note_id ? noteById(input.note_id) : null;
  let diff = "";
  if (p.kind === "edit") {
    diff = fieldsDiffHtml(note, input.fields || {});
    if (input.tags) {
      diff +=
        '<div class="muted small">tags → ' + esc((input.tags || []).join(" ")) + "</div>";
    }
  } else if (p.kind === "split") {
    diff =
      (input.original === null
        ? '<div class="muted small">l\'original est supprimé</div>'
        : fieldsDiffHtml(note, (input.original && input.original.fields) || {})) +
      (input.new_notes || [])
        .map(
          (nn, i) =>
            '<div class="diff"><div class="muted small">nouvelle note ' +
            (i + 1) +
            (nn.model ? " · " + esc(nn.model) : "") +
            "</div>" +
            newFieldsHtml(nn.fields || {}) +
            "</div>",
        )
        .join("");
  } else if (p.kind === "create") {
    diff =
      '<div class="muted small">nouvelle note' +
      (input.model ? " · " + esc(input.model) : "") +
      " dans " +
      esc(S.deck || "") +
      "</div>" +
      newFieldsHtml(input.fields || {});
  } else if (p.kind === "move") {
    diff =
      '<div class="diff"><div class="before">' +
      esc((note && note.deck) || "?") +
      '</div><div class="after">' +
      esc(input.deck || "?") +
      "</div></div>";
  }
  return (
    '<div class="proposal' +
    (p.applied ? " applied" : "") +
    '">' +
    '<div class="proposal-head"><span class="pkind">' +
    esc(PROPOSAL_LABEL[p.kind] || p.kind || "?") +
    "</span>" +
    (input.note_id ? '<span class="tag">' + short(input.note_id) + "</span>" : "") +
    '<span class="grow"></span>' +
    (p.applied
      ? '<span class="applied-mark">appliqué ✓</span>'
      : '<button class="primary" data-act="apply" data-mi="' +
        mi +
        '" data-pi="' +
        pi +
        '"' +
        (S.busy ? " disabled" : "") +
        ">Appliquer</button>") +
    "</div>" +
    (input.rationale ? '<div class="rationale">' + nl2br(input.rationale) + "</div>" : "") +
    '<div class="diff">' +
    diff +
    "</div>" +
    (p.error ? '<div class="banner">' + esc(p.error) + "</div>" : "") +
    "</div>"
  );
}

function newFieldsHtml(fields) {
  return Object.keys(fields)
    .map(
      (name) =>
        '<div class="field-name">' +
        esc(name) +
        '</div><div class="after">' +
        renderField(fields[name]) +
        "</div>",
    )
    .join("");
}

function fieldsDiffHtml(note, newFields) {
  const cur = (note && note.fields) || {};
  const names = Object.keys(cur).concat(
    Object.keys(newFields).filter((n) => !(n in cur)),
  );
  const changed = [];
  const same = [];
  names.forEach((name) => {
    if (name in newFields && String(newFields[name]) !== String(cur[name] || "")) {
      changed.push(name);
    } else {
      same.push(name);
    }
  });
  let out = changed
    .map(
      (name) =>
        '<div class="field-name">' +
        esc(name) +
        '</div><div class="before">' +
        renderField(cur[name] || "") +
        '</div><div class="after">' +
        renderField(newFields[name]) +
        "</div>",
    )
    .join("");
  if (!changed.length) out = '<div class="muted small">aucun champ modifié</div>';
  if (same.length) {
    out +=
      "<details><summary>" +
      same.length +
      " champ(s) inchangé(s)</summary>" +
      same
        .map(
          (name) =>
            '<div class="field-name">' +
            esc(name) +
            '</div><div class="field-val small">' +
            renderField(cur[name] || "") +
            "</div>",
        )
        .join("") +
      "</details>";
  }
  return out;
}

/* Narrow refresh used while a reply streams in, so the textarea keeps focus. */
function refreshChatLog() {
  const log = document.getElementById("chat-log");
  if (!log) return;
  log.innerHTML = chatLogHtml();
  log.scrollTop = log.scrollHeight;
}
