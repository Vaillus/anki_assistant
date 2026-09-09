/* Thin wrappers over the HTTP API. Every call rejects with an Error carrying the
   server's `detail` (502 AnkiConnect / 503 unreachable / 404 unknown note). */

"use strict";

async function jfetch(url, opts) {
  let res;
  try {
    res = await fetch(url, opts);
  } catch (e) {
    throw new Error("Serveur injoignable (" + (e && e.message ? e.message : "réseau") + ")");
  }
  if (res.status === 204) return null;
  const ct = res.headers.get("content-type") || "";
  let data = null;
  try {
    data = ct.indexOf("application/json") >= 0 ? await res.json() : await res.text();
  } catch (e) {
    data = null;
  }
  if (!res.ok) {
    let detail = data && typeof data === "object" ? data.detail : data;
    if (detail && typeof detail === "object") detail = JSON.stringify(detail);
    if (!detail || typeof detail !== "string") detail = res.status + " " + res.statusText;
    const err = new Error(String(detail).slice(0, 500));
    err.status = res.status;
    throw err;
  }
  return data;
}

function jsonBody(method, body) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

const q = encodeURIComponent;

const API = {
  // review
  decks: () => jfetch("/api/decks"),
  notes: (deck) => jfetch("/api/notes?deck=" + q(deck)),
  note: (id) => jfetch("/api/notes/" + id),
  models: () => jfetch("/api/models"),
  keep: (id) => jfetch("/api/notes/" + id + "/keep", { method: "POST" }),
  patch: (id, body) => jfetch("/api/notes/" + id, jsonBody("PATCH", body)),
  split: (id, body) => jfetch("/api/notes/" + id + "/split", jsonBody("POST", body)),
  create: (body) => jfetch("/api/notes", jsonBody("POST", body)),
  move: (id, deck) => jfetch("/api/notes/" + id + "/move", jsonBody("POST", { deck })),
  remove: (id) => jfetch("/api/notes/" + id, { method: "DELETE" }),

  // sources — the deck goes in the query string, deck names contain "::"
  corpus: (deck) => jfetch("/api/sources/corpus?deck=" + q(deck)),
  putSources: (deck, entries) => jfetch("/api/sources?deck=" + q(deck), jsonBody("PUT", entries)),
  vaultNotes: (query) => jfetch("/api/vault/notes?q=" + q(query)),
  anchors: (noteId) => jfetch("/api/sources/anchors?note_id=" + noteId),
  createSourceNote: (body) => jfetch("/api/sources/notes", jsonBody("POST", body)),
  patchSourceText: (id, body) => jfetch("/api/sources/" + q(id) + "/text", jsonBody("PATCH", body)),

  // chat
  chatStatus: () => jfetch("/api/chat/status"),
};

/* POST /api/chat and parse the SSE response off the ReadableStream.
   EventSource cannot POST, so the framing is parsed by hand.
   `onEvent(name, data)` is called for each `event:`/`data:` pair. */
async function streamChat(body, onEvent) {
  let res;
  try {
    res = await fetch("/api/chat", jsonBody("POST", body));
  } catch (e) {
    throw new Error("Serveur injoignable (" + (e && e.message ? e.message : "réseau") + ")");
  }
  if (!res.ok) {
    let detail = "";
    try {
      const j = await res.json();
      detail = (j && j.detail) || "";
    } catch (e) {
      detail = "";
    }
    const err = new Error(detail || res.status + " " + res.statusText);
    err.status = res.status;
    throw err;
  }
  if (!res.body) throw new Error("Réponse sans flux");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const step = await reader.read();
    if (step.done) break;
    buf += decoder.decode(step.value, { stream: true });
    buf = buf.replace(/\r\n/g, "\n");
    let cut;
    while ((cut = buf.indexOf("\n\n")) >= 0) {
      const raw = buf.slice(0, cut);
      buf = buf.slice(cut + 2);
      const parsed = parseSseChunk(raw);
      if (parsed) onEvent(parsed.event, parsed.data);
    }
  }
  const tail = parseSseChunk(buf);
  if (tail) onEvent(tail.event, tail.data);
}

function parseSseChunk(raw) {
  const lines = String(raw || "").split("\n");
  let event = "message";
  const dataLines = [];
  for (const line of lines) {
    if (!line || line.charAt(0) === ":") continue;
    if (line.indexOf("event:") === 0) event = line.slice(6).trim();
    else if (line.indexOf("data:") === 0) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  if (!dataLines.length) return null;
  let data = null;
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch (e) {
    data = { raw: dataLines.join("\n") };
  }
  return { event, data };
}
