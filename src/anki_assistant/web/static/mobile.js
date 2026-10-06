/* Phone app page (specs/mobile.md#phone-page): display, phone store (IndexedDB), sync,
   media prefetch, service worker. The phone-queue rules and the phone actions are in
   mobile-queue.js (global MQ); hideClozes and typesetMath come from display.js. */

"use strict";

const STATE = MQ.emptyState();
const UI = {
  currentId: null, // card on screen; kept first in the queue while it is still in it
  revealed: false,
  shownAt: 0,
  syncing: false,
  offline: false,
  notice: null, // { text, danger }
  storage: "loading", // "idb" | "memory"
  logCount: 0,
  cardKey: null, // what the card area shows, so it is redrawn only when it changes
  scrolledFor: null, // card and side the card area was last scrolled to the top for
};

const SYNC_TIMEOUT_MS = 20000;
const MEDIA_CACHE = "anki-m-media"; // same name as in the service worker
const KV_KEYS = ["batch", "pending", "answers", "burials", "lastSync"];
const EASES = [
  [1, "again"],
  [2, "hard"],
  [3, "good"],
  [4, "easy"],
];

function escText(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function hhmm(ms) {
  const d = new Date(ms);
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

/* ---------- phone store (IndexedDB) ---------- */

let db = null;

function openDb() {
  return new Promise((resolve, reject) => {
    let req;
    try {
      req = indexedDB.open("anki-mobile", 1);
    } catch (e) {
      reject(e);
      return;
    }
    req.onupgradeneeded = () => {
      const d = req.result;
      if (!d.objectStoreNames.contains("kv")) d.createObjectStore("kv");
      if (!d.objectStoreNames.contains("log")) d.createObjectStore("log", { keyPath: "action_id" });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
    req.onblocked = () => reject(new Error("database blocked"));
  });
}

function reqDone(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function loadStore() {
  try {
    db = await openDb();
    const tx = db.transaction(["kv", "log"], "readonly");
    const kv = tx.objectStore("kv");
    const values = await Promise.all(KV_KEYS.map((k) => reqDone(kv.get(k))));
    UI.logCount = await reqDone(tx.objectStore("log").count());
    KV_KEYS.forEach((k, i) => {
      if (values[i] !== undefined && values[i] !== null) STATE[k] = values[i];
    });
    UI.storage = "idb";
  } catch (e) {
    db = null;
    UI.storage = "memory";
    UI.notice = {
      text: "Phone storage unavailable (" + (e && e.message ? e.message : e) +
        "): actions are kept only until this page closes.",
      danger: true,
    };
  }
}

/* Write the given state keys, plus a review-log entry to add or delete, in one transaction. */
function save(keys, logPut, logDelete) {
  if (!db) return Promise.resolve();
  return new Promise((resolve, reject) => {
    let tx;
    try {
      tx = db.transaction(["kv", "log"], "readwrite");
      const kv = tx.objectStore("kv");
      for (const k of keys) kv.put(STATE[k], k);
      if (logPut) tx.objectStore("log").put(logPut);
      if (logDelete) tx.objectStore("log").delete(logDelete);
    } catch (e) {
      reject(e);
      return;
    }
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error("transaction aborted"));
  });
}

async function persist(keys, logPut, logDelete) {
  try {
    await save(keys, logPut, logDelete);
  } catch (e) {
    UI.notice = {
      text: "Could not save on the phone (" + (e && e.message ? e.message : e) +
        "): keep this page open until the next sync.",
      danger: true,
    };
  }
}

/* ---------- theme (specs/theme.md#picker, same key as the main app) ---------- */

const THEME_KEY = "anki-theme";

function storedTheme() {
  try {
    const t = localStorage.getItem(THEME_KEY);
    return THEMES.some((x) => x.slug === t) ? t : null;
  } catch (e) {
    return null;
  }
}

let chosenTheme = storedTheme();

function systemMode() {
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

function applyTheme() {
  document.documentElement.setAttribute("data-theme", chosenTheme || DEFAULT_THEME[systemMode()]);
}

function setTheme(slug) {
  chosenTheme = THEMES.some((x) => x.slug === slug) ? slug : null;
  try {
    if (chosenTheme) localStorage.setItem(THEME_KEY, chosenTheme);
    else localStorage.removeItem(THEME_KEY);
  } catch (e) {
    /* applied for this session only */
  }
  applyTheme();
}

if (window.matchMedia) {
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const onChange = () => {
    if (!chosenTheme) applyTheme();
  };
  if (mq.addEventListener) mq.addEventListener("change", onChange);
  else if (mq.addListener) mq.addListener(onChange);
}

function themePicker() {
  const opt = (t) =>
    `<option value="${t.slug}"${t.slug === chosenTheme ? " selected" : ""}>${escText(t.label)}</option>`;
  const group = (mode) =>
    `<optgroup label="${mode}">${THEMES.filter((t) => t.mode === mode).map(opt).join("")}</optgroup>`;
  return (
    `<label>theme <select id="m-theme">` +
    `<option value=""${chosenTheme ? "" : " selected"}>auto (follow the phone)</option>` +
    group("dark") +
    group("light") +
    `</select></label>`
  );
}

/* ---------- rendering ---------- */

function queueNow() {
  return MQ.buildQueue(STATE, Date.now(), UI.currentId);
}

function render() {
  const q = queueNow();
  const card = q.queue[0] || null;
  if (!card || card.card_id !== UI.currentId) {
    UI.currentId = card ? card.card_id : null;
    UI.revealed = false;
    UI.shownAt = Date.now();
  }
  renderHead(q);
  renderNotice();
  renderCard(q, card);
  renderBar(card);
}

function renderHead(q) {
  const batch = STATE.batch;
  const parts = [];
  parts.push(`<span class="m-deck">${escText(batch ? batch.deck : "anki")}</span>`);
  if (batch) {
    const c = q.counts;
    parts[0] +=
      ` <span class="m-counts" title="new · learning · review">` +
        `<span class="m-new">${c.new}</span> <span class="m-learn">${c.learn}</span> ` +
        `<span class="m-review">${c.review}</span></span>`;
  }
  if (STATE.pending.length) parts.push(`<span class="m-pending">${STATE.pending.length} pending</span>`);
  let sync;
  let cls = "";
  if (UI.syncing) {
    sync = "⟳ syncing…";
    cls = " busy";
  } else if (UI.offline) {
    sync = "⟳ offline";
    cls = " offline";
  } else {
    sync = "⟳ " + (STATE.lastSync ? hhmm(STATE.lastSync) : "sync");
  }
  document.getElementById("m-head").innerHTML =
    `<div class="m-title">${parts.join(" · ")}</div>` +
    `<button class="m-sync${cls}" data-act="sync"${UI.syncing ? " disabled" : ""}>${sync}</button>`;
}

function renderNotice() {
  const el = document.getElementById("m-notice");
  if (!UI.notice) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  el.className = "m-notice" + (UI.notice.danger ? " danger" : "");
  el.innerHTML = `<span>${escText(UI.notice.text)}</span><button data-act="dismiss">ok</button>`;
}

function fieldsHtml(fields, named) {
  return (fields || [])
    .filter((f) => f.html && f.html.trim())
    .map(
      (f) =>
        (named ? `<div class="field-name">${escText(f.name)}</div>` : "") +
        `<div class="field-val">${f.html}</div>`,
    )
    .join("");
}

function foot() {
  const where = UI.storage === "idb" ? "on this phone" : "in memory only";
  const synced = STATE.lastSync ? new Date(STATE.lastSync).toLocaleString() : "never";
  return (
    `<div class="m-foot">${themePicker()}` +
    `<span>· review log: ${UI.logCount} answers ${where} · last sync ${escText(synced)}</span></div>`
  );
}

function emptyHtml(q) {
  if (!STATE.batch) return `<div class="m-empty"><b>No cards yet</b>sync with the Mac.</div>`;
  if (q.expired) return `<div class="m-empty"><b>Batch used up</b>sync to get more cards.</div>`;
  const next = q.later.length ? `next card back at ${hhmm(q.later[0].at)}` : "";
  return `<div class="m-empty"><b>Done for today</b>${next}</div>`;
}

function renderCard(q, card) {
  const flag = card ? MQ.flagOf(STATE, card) : 0;
  const key = card
    ? `${card.card_id}|${UI.revealed}|${flag}`
    : `empty|${!!STATE.batch}|${q.expired}|${q.later.length ? q.later[0].at : ""}`;
  const keyWithFoot = key + "|" + UI.logCount + "|" + STATE.lastSync + "|" + chosenTheme;
  if (keyWithFoot === UI.cardKey) return;
  UI.cardKey = keyWithFoot;
  const el = document.getElementById("m-card");
  if (!card) {
    el.innerHTML = emptyHtml(q) + foot();
    return;
  }
  const meta =
    `<div class="m-meta">${escText(card.deck)} · ${escText(card.kind)}` +
    (flag ? ` · <span class="m-flag">⚑ flagged</span>` : "") +
    `</div>`;
  let html;
  if (!UI.revealed) {
    const question = (card.question || []).map((f) => ({
      name: f.name,
      html: card.cloze ? hideClozes(f.html, [card.cloze]) : f.html,
    }));
    html = meta + fieldsHtml(question, false) + `<div class="m-hint">tap to show the answer</div>`;
  } else {
    html = meta + fieldsHtml(card.question, false) + "<hr>" + fieldsHtml(card.answer, true);
  }
  el.innerHTML = html + foot();
  if (UI.scrolledFor !== `${card.card_id}|${UI.revealed}`) {
    UI.scrolledFor = `${card.card_id}|${UI.revealed}`;
    el.scrollTop = 0;
  }
  typesetMath();
}

function renderBar(card) {
  const bar = document.getElementById("m-bar");
  if (!card) {
    bar.innerHTML = toolsRow(null);
    return;
  }
  let top;
  if (!UI.revealed) {
    top = `<button class="m-show primary" data-act="show">show answer</button>`;
  } else {
    const again = MQ.answersOf(STATE, card.card_id).length > 0;
    const labels = card.outcome_labels || [];
    top =
      `<div class="m-row four">` +
      EASES.map(
        ([ease, name]) =>
          `<button class="m-ease e${ease}" data-act="answer" data-ease="${ease}">` +
          `<span class="lbl">${name}</span>` +
          `<span class="out">${escText(again ? "—" : labels[ease - 1] || "—")}</span></button>`,
      ).join("") +
      `</div>`;
  }
  bar.innerHTML = top + toolsRow(card);
}

function toolsRow(card) {
  const flagged = card ? MQ.flagOf(STATE, card) : 0;
  const canUndo = !!MQ.lastUndoable(STATE) && !UI.syncing;
  const dis = card ? "" : " disabled";
  return (
    `<div class="m-row four">` +
    `<button class="m-tool${flagged ? " on" : ""}" data-act="flag"${dis}>⚑ ${flagged ? "flagged" : "flag"}</button>` +
    `<button class="m-tool" data-act="undo"${canUndo ? "" : " disabled"}>↶ undo</button>` +
    `<button class="m-tool" data-act="bury"${dis}>bury</button>` +
    `<button class="m-tool" data-act="suspend"${dis}>suspend</button>` +
    `</div>`
  );
}

/* ---------- phone actions ---------- */

function currentCard() {
  const card = queueNow().queue[0];
  return card && card.card_id === UI.currentId ? card : null;
}

async function onAnswer(ease) {
  const card = currentCard();
  if (!card || !UI.revealed) return;
  const entry = MQ.answer(STATE, card, ease, Date.now(), UI.shownAt);
  UI.currentId = null;
  await persist(["pending", "answers"], entry, null);
  UI.logCount += 1;
  render();
}

async function onFlag() {
  const card = currentCard();
  if (!card) return;
  MQ.toggleFlag(STATE, card, Date.now());
  await persist(["pending"]);
  render();
}

async function onBury() {
  const card = currentCard();
  if (!card) return;
  MQ.bury(STATE, card, Date.now());
  UI.currentId = null;
  await persist(["burials"]);
  render();
}

async function onSuspend() {
  const card = currentCard();
  if (!card) return;
  MQ.suspend(STATE, card, Date.now());
  UI.currentId = null;
  await persist(["pending"]);
  render();
}

async function onUndo() {
  if (UI.syncing) return; // the last action may be on its way to Anki
  const r = MQ.undo(STATE);
  if (!r) return;
  await persist(["pending", "answers", "burials"], null, r.logActionId);
  if (r.logActionId) UI.logCount = Math.max(0, UI.logCount - 1);
  UI.currentId = r.card_id;
  UI.revealed = false;
  UI.shownAt = Date.now();
  UI.cardKey = null;
  render();
}

/* ---------- sync (specs/mobile.md#phone-page § Sync on the phone) ---------- */

async function sync() {
  if (UI.syncing) return;
  UI.syncing = true;
  render();
  const sent = STATE.pending.map((p) => ({
    id: p.id,
    kind: p.kind,
    card_id: p.card_id,
    at: p.at,
    ease: p.ease === undefined ? null : p.ease,
    time_ms: p.time_ms || 0,
  }));
  const ctrl = window.AbortController ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), SYNC_TIMEOUT_MS) : null;
  try {
    const r = await fetch("/api/mobile/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actions: sent }),
      cache: "no-store",
      signal: ctrl ? ctrl.signal : undefined,
    });
    if (!r.ok) {
      let detail = "";
      try {
        detail = (await r.json()).detail || "";
      } catch (e) {
        /* not JSON */
      }
      throw Object.assign(new Error(`sync failed: HTTP ${r.status}${detail ? " — " + detail : ""}`), {
        http: true,
      });
    }
    const data = await r.json();
    const dropped = MQ.applySync(STATE, data, Date.now());
    await persist(KV_KEYS);
    UI.offline = false;
    if (dropped.length) {
      const reasons = {};
      for (const d of dropped) reasons[d.reason] = (reasons[d.reason] || 0) + 1;
      const why = Object.entries(reasons)
        .map(([k, n]) => `${k.replace(/_/g, " ")} ×${n}`)
        .join(", ");
      UI.notice = {
        text: `${dropped.length} action${dropped.length > 1 ? "s" : ""} not applied to Anki (${why}).`,
        danger: false,
      };
    } else if (UI.notice && UI.notice.sync) {
      UI.notice = null;
    }
    prefetchMedia((STATE.batch && STATE.batch.media) || []);
  } catch (e) {
    UI.offline = true;
    if (e && e.http) UI.notice = { text: e.message, danger: true, sync: true };
  } finally {
    if (timer) clearTimeout(timer);
    UI.syncing = false;
    UI.cardKey = null;
    render();
  }
}

/* Python's quote(name, safe=""), as render.py writes picture URLs. */
function mediaUrl(name) {
  const enc = encodeURIComponent(name).replace(
    /[!'()*]/g,
    (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase(),
  );
  return "/api/mobile/media/" + enc;
}

/* Download every picture of the batch not cached yet; forget those it no longer lists. */
async function prefetchMedia(names) {
  if (!window.caches) return;
  try {
    const cache = await caches.open(MEDIA_CACHE);
    const wanted = new Set(names.map((n) => new URL(mediaUrl(n), location.href).href));
    for (const req of await cache.keys()) {
      if (!wanted.has(req.url)) await cache.delete(req);
    }
    for (const url of wanted) {
      if (await cache.match(url)) continue;
      try {
        const r = await fetch(url);
        if (r.ok) await cache.put(url, r);
      } catch (e) {
        /* offline again: the next sync retries */
      }
    }
  } catch (e) {
    /* Cache API unavailable: pictures show only while online */
  }
}

/* ---------- events and boot ---------- */

document.addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-act]");
  if (btn) {
    if (btn.disabled) return;
    const act = btn.dataset.act;
    if (act === "show") {
      UI.revealed = true;
      render();
    } else if (act === "answer") onAnswer(Number(btn.dataset.ease));
    else if (act === "flag") onFlag();
    else if (act === "undo") onUndo();
    else if (act === "bury") onBury();
    else if (act === "suspend") onSuspend();
    else if (act === "sync") sync();
    else if (act === "dismiss") {
      UI.notice = null;
      render();
    }
    return;
  }
  // A tap on the card shows the answer (not on the theme picker or a link).
  if (ev.target.closest("#m-card") && !ev.target.closest(".m-foot, a, select, label")) {
    if (UI.currentId !== null && !UI.revealed) {
      UI.revealed = true;
      render();
    }
  }
});

document.addEventListener("change", (ev) => {
  if (ev.target.id === "m-theme") {
    setTheme(ev.target.value);
    UI.cardKey = null;
    render();
  }
});

/* Keys: a convenience for testing on the Mac. */
document.addEventListener("keydown", (ev) => {
  if (ev.target.closest && ev.target.closest("input, textarea, select")) return;
  if (ev.key === " ") {
    ev.preventDefault();
    if (UI.currentId !== null && !UI.revealed) {
      UI.revealed = true;
      render();
    }
  } else if (["1", "2", "3", "4"].includes(ev.key)) onAnswer(Number(ev.key));
  else if (ev.key === "u") onUndo();
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    render();
    sync();
  }
});
window.addEventListener("online", () => sync());
// Same-day returns and the rollover change the queue with time.
setInterval(() => {
  if (document.visibilityState === "visible") render();
}, 30000);

async function boot() {
  applyTheme();
  await loadStore();
  render();
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/m/sw.js", { scope: "/m" }).catch(() => {
      /* no offline page: everything else still works while online */
    });
  }
  sync();
}

boot();
