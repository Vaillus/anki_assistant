// PROTOTYPE — throwaway. Three variants of the review screen, switched with ?variant=.
// Shared: data fetching + in-memory state. Not shared: layout. Chat is a stub.

const VARIANTS = {
  A: { name: "Trois colonnes (decks · file · source/chat)", render: renderA },
  B: { name: "Focus une note + tiroir chat", render: renderB },
  C: { name: "Dashboard decks + console", render: renderC },
};
const S = {
  decks: [], deck: null, notes: null, source: null,
  onlyFlagged: true, selNote: null, refs: [], chat: [], showAllDecks: false,
  tab: "source", drawer: false, idx: 0, bannerOpen: true, lastAct: null,
};
const $ = (h) => { const t = document.createElement("template"); t.innerHTML = h.trim(); return t.content.firstChild; };
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const api = (u) => fetch(u).then((r) => r.json());

function variant() { return new URLSearchParams(location.search).get("variant") || "A"; }
function setVariant(v) { const p = new URLSearchParams(location.search); p.set("variant", v); history.replaceState(null, "", "?" + p); draw(); }
function cycle(d) { const ks = Object.keys(VARIANTS); setVariant(ks[(ks.indexOf(variant()) + d + ks.length) % ks.length]); }

async function selectDeck(name) {
  S.deck = name; S.notes = null; S.source = null; S.selNote = null; S.idx = 0; draw();
  const [n, s] = await Promise.all([api(`/api/notes?deck=${encodeURIComponent(name)}`), api(`/api/source?deck=${encodeURIComponent(name)}`)]);
  S.notes = n; S.source = s;
  const first = visibleNotes()[0]; S.selNote = first ? first.note_id : null; draw();
}
function visibleNotes() { if (!S.notes) return []; return S.onlyFlagged ? S.notes.notes.filter((n) => n.flagged) : S.notes.notes; }
function noteById(id) { return S.notes?.notes.find((n) => n.note_id === id); }
function addRef(id) { if (!S.refs.includes(id)) S.refs.push(id); draw(); }
function sendChat(text) {
  if (!text.trim()) return;
  const ctx = [S.selNote, ...S.refs].filter((x, i, a) => x && a.indexOf(x) === i);
  S.chat.push({ who: "user", text, refs: [...S.refs] });
  S.chat.push({ who: "assistant", text: `[stub] Je répondrais ici avec en contexte : deck « ${S.deck} », ${S.source ? "la source " + S.source.kind + " « " + S.source.target + " »" : "aucune source"}, et ${ctx.length} note(s) : ${ctx.map((i) => "#" + String(i).slice(-4)).join(" ")}.` });
  S.refs = []; draw();
}
const short = (id) => "#" + String(id).slice(-4);
const ACTIONS = ["Garder", "Modifier", "Splitter", "Créer", "Déplacer", "Supprimer", "Passer"];
function actionsHtml(n) {
  return `<div class="actions">${ACTIONS.map((a) => `<button data-act="${a}" data-on="${n.note_id}" class="${a === "Garder" ? "primary" : ""} ${a === "Supprimer" ? "danger" : ""}">${a}</button>`).join("")}</div>${S.lastAct?.note === n.note_id ? `<div class="toast">[stub] « ${S.lastAct.act} » sur ${short(n.note_id)} — rien n'est écrit dans Anki dans ce prototype.</div>` : ""}`;
}
const flaggedDecks = () => S.decks.filter((d) => d.flagged_total > 0);

function noteFields(n, cls = "") {
  return Object.entries(n.fields).map(([k, v]) => v ? `<div class="field-name">${esc(k)}</div><div class="field-val ${cls} ${k === "Back Extra" ? "small" : ""}">${v}</div>` : "").join("");
}
function chipsHtml() { return S.refs.map((id) => `<span class="ref-chip">${short(id)} <b data-unref="${id}">×</b></span>`).join(""); }
function chatLog(cls = "") {
  return S.chat.map((m) => `<div class="msg ${m.who} ${cls}"><div class="who">${m.who === "user" ? "toi" : "claude"}${m.refs?.length ? " · " + m.refs.map(short).join(" ") : ""}</div>${esc(m.text)}</div>`).join("") || `<div class="empty">Sélectionne une note, puis pose ta question.<br>« → chat » sur une note l'ajoute au contexte.</div>`;
}
function sourceHtml() {
  if (!S.deck) return `<div class="empty">Choisis un deck</div>`;
  if (!S.source) return `<div class="empty">Aucune source pour ce deck.<br><button>+ associer une source</button></div>`;
  const s = S.source;
  return `<div><span class="src-kind ${s.kind}">${s.kind}</span> <b>${esc(s.target)}</b>${s.note ? ` <span class="muted">· ${esc(s.note)}</span>` : ""}${s.inherited ? `<div class="muted" style="font-size:12px">héritée de ${esc(s.on_deck)}</div>` : ""}${s.exists ? "" : `<div class="muted" style="font-size:12px">⚠ fichier introuvable (démo)</div>`}<div style="margin:8px 0"><a href="${s.uri}">ouvrir ↗</a></div>${s.excerpt ? `<pre class="excerpt">${esc(s.excerpt)}</pre>` : `<div class="muted" style="font-size:12px">${s.kind === "pdf" ? "Extrait PDF : à venir (passage ciblé par pages)." : ""}</div>`}</div>`;
}

// ---------------- Variant A ----------------
function renderA(root) {
  const decks = S.showAllDecks ? S.decks : flaggedDecks();
  root.innerHTML = `<div class="A">
    <div class="col"><div class="col-head"><h2>Decks</h2><button class="ghost" id="a-alldecks">${S.showAllDecks ? "flagués seuls" : "tous"}</button></div>
      ${decks.map((d) => `<div class="deck ${d.name === S.deck ? "sel" : ""} ${d.flagged_total ? "" : "dim"}" data-deck="${esc(d.name)}" style="padding-left:${12 + d.depth * 14}px"><span class="name">${esc(d.leaf)}</span>${d.source ? `<span class="src-kind ${d.source}">${d.source[0]}</span>` : ""}${d.flagged_own ? `<span class="badge">${d.flagged_own}</span>` : ""}${d.flagged_total > d.flagged_own ? `<span class="badge sub">${d.flagged_total}</span>` : ""}</div>`).join("")}
    </div>
    <div class="col"><div class="col-head"><h2>${S.deck ? esc(S.deck.split("::").pop()) : "File"}</h2>${S.notes ? `<span class="muted">${S.notes.flagged} flaguées / ${S.notes.total}</span><button class="ghost" id="a-only">${S.onlyFlagged ? "voir toutes" : "flaguées seules"}</button>` : ""}</div>
      ${!S.deck ? `<div class="empty">← choisis un deck</div>` : !S.notes ? `<div class="empty">chargement…</div>` : visibleNotes().map((n) => `<div class="note ${n.note_id === S.selNote ? "sel" : ""} ${n.flagged ? "" : "unflagged"}" data-note="${n.note_id}">
        <div class="note-top">${n.flagged ? `<span class="badge">⚑</span>` : `<span class="badge zero">·</span>`}<span class="tag">${short(n.note_id)} · ${esc(n.model)} · ${n.n_cards} carte(s)</span><span style="flex:1"></span><button class="ghost" data-ref="${n.note_id}">→ chat</button></div>
        ${noteFields(n)}${n.note_id === S.selNote ? actionsHtml(n) : ""}</div>`).join("")}
    </div>
    <div class="col"><div class="col-head"><div class="tabs"><button class="${S.tab === "source" ? "on" : ""}" data-tab="source">Source</button><button class="${S.tab === "chat" ? "on" : ""}" data-tab="chat">Chat</button></div></div>
      ${S.tab === "source" ? `<div class="pane">${sourceHtml()}</div>` : `<div class="chat"><div class="log">${chatLog()}</div><div class="box"><div class="chips">${S.selNote ? `<span class="ref-chip">note ${short(S.selNote)} (sélection)</span>` : ""}${chipsHtml()}</div><textarea id="a-in" placeholder="Demande une reformulation, un split, une vérif…"></textarea><div style="text-align:right;margin-top:4px"><button class="primary" id="a-send">Envoyer</button></div></div></div>`}
    </div></div>`;
  root.querySelector("#a-alldecks").onclick = () => { S.showAllDecks = !S.showAllDecks; draw(); };
  root.querySelector("#a-only")?.addEventListener("click", () => { S.onlyFlagged = !S.onlyFlagged; draw(); });
  root.querySelectorAll("[data-tab]").forEach((b) => (b.onclick = () => { S.tab = b.dataset.tab; draw(); }));
  root.querySelector("#a-send")?.addEventListener("click", () => sendChat(root.querySelector("#a-in").value));
  wireCommon(root);
}

// ---------------- Variant B ----------------
function renderB(root) {
  const vis = visibleNotes(); S.idx = Math.min(S.idx, Math.max(0, vis.length - 1)); const cur = vis[S.idx]; if (cur) S.selNote = cur.note_id;
  root.innerHTML = `<div class="B">
    <div class="bar"><select id="b-deck"><option value="">— choisir un deck —</option>${flaggedDecks().map((d) => `<option value="${esc(d.name)}" ${d.name === S.deck ? "selected" : ""}>${"  ".repeat(d.depth)}${esc(d.leaf)}  (${d.flagged_own}${d.flagged_total > d.flagged_own ? " / " + d.flagged_total : ""})</option>`).join("")}</select>
      ${S.notes ? `<span class="progress">${vis.length ? S.idx + 1 : 0} / ${vis.length}</span><button class="ghost" id="b-only">${S.onlyFlagged ? "toutes les notes" : "flaguées seules"}</button>` : ""}<span style="flex:1"></span><button id="b-drawer" class="${S.drawer ? "primary" : ""}">💬 Chat${S.refs.length ? " (" + S.refs.length + ")" : ""}</button></div>
    <div class="body">
      <div class="rail">${vis.map((n, i) => `<div class="dot ${n.flagged ? "on" : "un"} ${i === S.idx ? "cur" : ""}" data-idx="${i}" title="${short(n.note_id)}">${i + 1}</div>`).join("")}</div>
      <div class="stage"><div class="sheet">
        ${!S.deck ? `<div class="empty">Choisis un deck dans la barre</div>` : !S.notes ? `<div class="empty">chargement…</div>` : !cur ? `<div class="empty">Rien à revoir ici 🎉</div>` : `
        <div class="nav"><button id="b-prev">← précédente</button><span class="tag">${short(cur.note_id)} · ${esc(cur.model)} · ${cur.n_cards} carte(s) ${cur.flagged ? "· ⚑ " + cur.flag_colors.join(",") : ""}</span><button id="b-next">suivante →</button></div>
        <div class="card">${noteFields(cur)}</div>
        <div class="nav" style="justify-content:center;gap:8px"><button class="primary" data-ref="${cur.note_id}">→ ajouter au chat</button><button disabled>Garder</button><button disabled>Modifier</button><button disabled>Splitter</button><button disabled>Déplacer</button><button disabled>Supprimer</button><button disabled>Passer</button></div>
        <details class="source" ${S.source ? "open" : ""}><summary>Source du deck ${S.source ? `<span class="src-kind ${S.source.kind}">${S.source.kind}</span> <b>${esc(S.source.target)}</b>` : `<span class="muted">— aucune</span>`}</summary><div class="inner">${sourceHtml()}</div></details>`}
      </div></div>
      <div class="drawer ${S.drawer ? "open" : ""}"><div class="log">${chatLog()}</div><div class="box"><div class="chips">${cur ? `<span class="ref-chip">note ${short(cur.note_id)} (affichée)</span>` : ""}${chipsHtml()}</div><textarea id="b-in" placeholder="…"></textarea><div style="text-align:right;margin-top:4px"><button class="primary" id="b-send">Envoyer</button></div></div></div>
    </div></div>`;
  root.querySelector("#b-deck").onchange = (e) => e.target.value && selectDeck(e.target.value);
  root.querySelector("#b-only")?.addEventListener("click", () => { S.onlyFlagged = !S.onlyFlagged; S.idx = 0; draw(); });
  root.querySelector("#b-drawer").onclick = () => { S.drawer = !S.drawer; draw(); };
  root.querySelector("#b-prev")?.addEventListener("click", () => { S.idx = Math.max(0, S.idx - 1); draw(); });
  root.querySelector("#b-next")?.addEventListener("click", () => { S.idx = Math.min(vis.length - 1, S.idx + 1); draw(); });
  root.querySelectorAll("[data-idx]").forEach((d) => (d.onclick = () => { S.idx = +d.dataset.idx; draw(); }));
  root.querySelector("#b-send")?.addEventListener("click", () => sendChat(root.querySelector("#b-in").value));
  wireCommon(root);
}

// ---------------- Variant C ----------------
function renderC(root) {
  if (!S.deck) {
    const fd = flaggedDecks().filter((d) => d.flagged_own > 0);
    root.innerHTML = `<div class="C"><div class="top"><h1>Cartes à revoir</h1><span class="muted">${fd.reduce((a, d) => a + d.flagged_own, 0)} notes flaguées dans ${fd.length} decks</span></div>
      <div class="grid">${fd.sort((a, b) => b.flagged_own - a.flagged_own).map((d) => `<div class="tile" data-deck="${esc(d.name)}"><div class="n">${d.flagged_own}</div><div class="leaf">${esc(d.leaf)}</div><div class="path">${esc(d.name)}</div><div>${d.source ? `<span class="src-kind ${d.source}">${d.source}</span>` : `<span class="muted" style="font-size:11px">sans source</span>`}</div></div>`).join("")}</div>
      <details class="others"><summary>${S.decks.length - fd.length} decks sans carte flaguée</summary><ul>${S.decks.filter((d) => !d.flagged_own).map((d) => `<li data-deck="${esc(d.name)}" style="cursor:pointer">${esc(d.name)}</li>`).join("")}</ul></details></div>`;
    wireCommon(root); return;
  }
  const s = S.source;
  root.innerHTML = `<div class="C">
    <div class="top"><button id="c-back">← decks</button><h1>${esc(S.deck)}</h1>${S.notes ? `<span class="muted">${S.notes.flagged} flaguées / ${S.notes.total}</span>` : ""}</div>
    <div class="deckview">
      <div class="banner ${S.bannerOpen ? "" : "collapsed"}"><div style="min-width:260px">${s ? `<span class="src-kind ${s.kind}">${s.kind}</span> <b>${esc(s.target)}</b>${s.note ? `<div class="muted" style="font-size:12px">${esc(s.note)}</div>` : ""}${s.inherited ? `<div class="muted" style="font-size:12px">héritée de ${esc(s.on_deck)}</div>` : ""}<div><a href="${s.uri}">ouvrir ↗</a> · <a href="#" id="c-toggle">${S.bannerOpen ? "replier" : "déplier"}</a></div>` : `<span class="muted">Aucune source</span> <button>+ associer</button>`}</div>${s?.excerpt ? `<pre class="excerpt">${esc(s.excerpt)}</pre>` : ""}</div>
      <div class="list">${!S.notes ? `<div class="empty">chargement…</div>` : `<table>
        ${S.notes.notes.filter((n) => n.flagged).map((n) => rowC(n)).join("")}
        <tr><td colspan="4" style="padding:0"><details ${S.onlyFlagged ? "" : "open"} id="c-un"><summary style="padding:10px 8px;cursor:pointer" class="muted">${S.notes.total - S.notes.flagged} notes non flaguées ▸</summary><table>${S.notes.notes.filter((n) => !n.flagged).map((n) => rowC(n)).join("")}</table></details></td></tr></table>`}</div>
      <div class="console"><div class="log">${S.chat.map((m) => `<div class="line ${m.who}">${m.who === "user" ? "›" : "≫"} ${esc(m.text)}</div>`).join("") || `<div class="line muted">// console — tape @ pour référencer une note, ou clique « → chat » dans une ligne</div>`}</div>
        <div class="box">${S.selNote ? `<span class="ref-chip">${short(S.selNote)}</span>` : ""}${chipsHtml()}<input id="c-in" placeholder="pose ta question…"><button class="primary" id="c-send">↵</button></div></div>
    </div></div>`;
  root.querySelector("#c-back").onclick = () => { S.deck = null; S.notes = null; S.source = null; draw(); };
  root.querySelector("#c-toggle")?.addEventListener("click", (e) => { e.preventDefault(); S.bannerOpen = !S.bannerOpen; draw(); });
  root.querySelector("#c-un")?.addEventListener("toggle", (e) => { S.onlyFlagged = !e.target.open; });
  const inp = root.querySelector("#c-in");
  root.querySelector("#c-send")?.addEventListener("click", () => sendChat(inp.value));
  inp?.addEventListener("keydown", (e) => { if (e.key === "Enter") sendChat(inp.value); });
  wireCommon(root);
}
function rowC(n) {
  const [k0, v0] = Object.entries(n.fields)[0] || ["", ""];
  return `<tr class="${n.flagged ? "flagged" : "un"} ${n.note_id === S.selNote ? "sel" : ""}" data-note="${n.note_id}"><td class="c0">${n.flagged ? "⚑" : ""}</td><td class="c1"><span class="tag">${short(n.note_id)}<br>${esc(n.model)}</span></td><td>${v0}${n.reason && n.flagged ? `<div class="reason">${n.reason}</div>` : ""}</td><td class="c3"><button class="ghost" data-ref="${n.note_id}">→ chat</button></td></tr>`;
}

// ---------------- shared wiring ----------------
function wireCommon(root) {
  root.querySelectorAll("[data-deck]").forEach((el) => (el.onclick = () => selectDeck(el.dataset.deck)));
  root.querySelectorAll("[data-note]").forEach((el) => (el.onclick = (e) => { if (e.target.closest("[data-ref]")) return; S.selNote = +el.dataset.note; draw(); }));
  root.querySelectorAll("[data-ref]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); addRef(+b.dataset.ref); }));
  root.querySelectorAll("[data-act]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); S.lastAct = { note: +b.dataset.on, act: b.dataset.act }; draw(); }));
  root.querySelectorAll("[data-unref]").forEach((b) => (b.onclick = () => { S.refs = S.refs.filter((i) => i !== +b.dataset.unref); draw(); }));
}
function draw() {
  const v = VARIANTS[variant()] || VARIANTS.A;
  v.render(document.getElementById("app"));
  document.getElementById("sw-label").textContent = `${variant()} — ${v.name}`;
}
document.getElementById("sw-prev").onclick = () => cycle(-1);
document.getElementById("sw-next").onclick = () => cycle(1);
document.addEventListener("keydown", (e) => {
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
  if (e.key === "ArrowLeft") cycle(-1); if (e.key === "ArrowRight") cycle(1);
});
api("/api/decks").then((d) => { S.decks = d; draw(); });
draw();
