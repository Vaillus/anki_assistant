/* Full re-render of the three columns from S. Same approach as the prototype: one draw()
   rebuilding #app; the workspace overlay (workspace.js) is appended when S.ws is set, and a
   narrower refreshChatLog() is used while a reply streams in. */

"use strict";

const FOLD = 4000; // source text fold, per specs/sources.md

function draw() {
  const root = document.getElementById("app");
  if (!root) return;
  const scroll = captureScroll(root);
  root.innerHTML =
    '<div class="app">' + deckColumn() + queueColumn() + rightColumn() + "</div>" + wsOverlay();
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
    '<div class="col-head">' +
    '<img class="logo" src="/static/star.svg" alt="" width="20" height="20">' +
    "<h2>Decks</h2><span class=\"grow\"></span>" +
    '<button class="ghost" data-act="alldecks">' +
    (S.showAllDecks ? "flagués seuls" : "tous") +
    "</button>" +
    themePicker() +
    "</div>" +
    rows +
    "</div>"
  );
}

/* specs/review.md#theme — every Omarchy palette, grouped by mode. The selection
   repaints on change, so the list doubles as a way to try them on. */
function themePicker() {
  const cur = currentTheme();
  const group = (mode, label) =>
    '<optgroup label="' +
    label +
    '">' +
    THEMES.filter((t) => t.mode === mode)
      .map(
        (t) =>
          '<option value="' +
          t.slug +
          '"' +
          (t.slug === cur ? " selected" : "") +
          ">" +
          esc(t.label) +
          "</option>",
      )
      .join("") +
    "</optgroup>";
  return (
    '<select class="chip theme" data-input="theme" title="Thème" aria-label="Thème">' +
    group("dark", "sombre") +
    group("light", "clair") +
    "</select>"
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
    (S.undoAvailable
      ? '<button class="ghost small" data-act="undo" title="remettre la dernière validation en place"' +
        (S.busy ? " disabled" : "") +
        ">Annuler la dernière validation</button>"
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
      ? '<span class="badge" title="' + esc(flagInfo) + '">⚑' + flaggedLabels(n) + "</span>"
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
    (n.flagged
      ? '<button class="ghost" data-act="keep" data-note="' +
        n.note_id +
        '" title="lever le flag sans rien changer (g)"' +
        (S.busy ? " disabled" : "") +
        ">garder</button>"
      : "") +
    "</div>" +
    fieldsHtml(n) +
    reasonHtml(n) +
    revealHtml(n) +
    '<div class="open-hint muted small">cliquer ou Entrée : ouvrir l\'espace de travail</div>' +
    "</div>"
  );
}

/* « c2 » per flagged card, appended to the flag badge. Empty for non-cloze models with one card. */
function flaggedLabels(n) {
  const ns = flaggedClozes(n);
  if (!ns.length || (ns.length === 1 && nCards(n) <= 1)) return "";
  return " " + ns.map((k) => "c" + k).join(" ");
}

/* Question state toggle (specs/review.md#question-state). */
function revealHtml(n) {
  if (!hasHiddenClozes(n)) return "";
  const hidden = isHidden(n);
  return (
    '<div class="reveal"><button class="ghost" data-act="reveal" data-note="' +
    n.note_id +
    '" title="Espace">' +
    (hidden ? "Révéler la réponse" : "Masquer à nouveau") +
    "</button>" +
    (hidden
      ? '<span class="muted small">la note telle que vue au moment du flag</span>'
      : "") +
    "</div>"
  );
}

/* The reason, below the fields — the only place « Back Extra » is shown in the queue. */
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
  const hidden = isHidden(n);
  const ns = hidden ? flaggedClozes(n) : [];
  return names
    .map((name) => {
      if (name === REASON_FIELD) return "";
      const v = hidden ? hideClozes(html[name], ns) : html[name];
      if (!v) return "";
      return (
        '<div class="field-name">' + esc(name) + '</div><div class="field-val">' + v + "</div>"
      );
    })
    .join("");
}

/* ---------------- column 3 — Source ---------------- */

function rightColumn() {
  const head = '<div class="col-head"><h2>Source</h2></div>';
  return '<div class="col">' + head + sourcePane() + "</div>";
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
