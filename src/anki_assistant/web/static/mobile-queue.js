/* Phone queue and phone actions (specs/mobile.md#phone-queue, #phone-page). No DOM, no
   storage, no network: every function takes the phone state and the current instant, so
   it runs in node as well as in the page.

   Phone state (what the page keeps in the phone store):
     batch     the last batch from the Mac (specs/mobile.md#api), or null
     pending   pending actions, oldest first: { id, kind, card_id, at, ease?, time_ms? }
     answers   answers given since the batch: { action_id, card_id, at, ease, outcome }
     burials   { card_id, day, at }: hidden while `day` is today
     lastSync  instant of the last successful sync (ms), or null
   Review-log entries are returned to the caller, which keeps them in their own store. */

(function (root) {
  "use strict";

  const DAY_S = 86400;
  const HOUR_MS = 3600 * 1000;
  /* Time spent on one answer is capped like Anki's default maximum answer time. */
  const MAX_TIME_MS = 60 * 1000;

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  /* The Anki day of an instant, in the phone's local time: the date of (instant − rollover). */
  function ankiDay(ms, rolloverHour) {
    const d = new Date(ms - rolloverHour * HOUR_MS);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  /* `YYYY-MM-DD` + n days, calendar arithmetic. */
  function addDays(day, n) {
    const [y, m, d] = day.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
  }

  /* Instant at which the Anki day after `ms` starts (local rollover hour). */
  function nextRollover(ms, rolloverHour) {
    const [y, m, d] = ankiDay(ms, rolloverHour).split("-").map(Number);
    return new Date(y, m - 1, d + 1, rolloverHour, 0, 0, 0).getTime();
  }

  function rollover(state) {
    return state.batch && typeof state.batch.rollover_hour === "number"
      ? state.batch.rollover_hour
      : 4;
  }

  function today(state, now) {
    return ankiDay(now, rollover(state));
  }

  function cards(state) {
    return (state.batch && state.batch.cards) || [];
  }

  function cardById(state, cardId) {
    return cards(state).find((c) => c.card_id === cardId) || null;
  }

  function answersOf(state, cardId) {
    return (state.answers || []).filter((a) => a.card_id === cardId).sort((a, b) => a.at - b.at);
  }

  function isSuspended(state, cardId) {
    return (state.pending || []).some((p) => p.card_id === cardId && p.kind === "suspend");
  }

  function isBuried(state, cardId, day) {
    return (state.burials || []).some((b) => b.card_id === cardId && b.day === day);
  }

  /* The card's flag as the phone sees it: the batch's, overridden by its last flag/unflag. */
  function flagOf(state, card) {
    let flag = card.flag || 0;
    for (const p of state.pending || []) {
      if (p.card_id !== card.card_id) continue;
      if (p.kind === "flag") flag = 2;
      else if (p.kind === "unflag") flag = 0;
    }
    return flag;
  }

  /* Where an answered card stands (specs/mobile.md#phone-queue, rule 2):
       { kind: "soon", at }   back at `at` (outcome under a day)
       { kind: "day", day }   back on Anki day `day`, inside the window
       { kind: "gone" }       not until the next sync */
  function returnOf(state, card) {
    const given = answersOf(state, card.card_id);
    if (given.length !== 1) return { kind: "gone" };
    const a = given[0];
    if (a.outcome === null || a.outcome === undefined) return { kind: "gone" };
    if (a.outcome < DAY_S) return { kind: "soon", at: a.at + a.outcome * 1000 };
    const days = state.batch.days || [];
    const day = addDays(ankiDay(a.at, rollover(state)), Math.round(a.outcome / DAY_S));
    if (!days.length || day > days[days.length - 1]) return { kind: "gone" };
    return { kind: "day", day: day, at: a.at };
  }

  /* What the answers given on Anki day `day` used of that day's limits
     (specs/mobile.md#phone-queue): a card's first answer counts by its batch kind (review or
     new; a learning card counts nothing); a later answer counts as a review when the previous
     outcome brought the card back on a later day, as nothing when it was a same-day return. */
  function answeredOn(state, day) {
    const used = { review: 0, new: 0 };
    const roll = rollover(state);
    const byCard = new Map();
    for (const a of state.answers || []) {
      if (!byCard.has(a.card_id)) byCard.set(a.card_id, []);
      byCard.get(a.card_id).push(a);
    }
    for (const [cardId, given] of byCard) {
      given.sort((a, b) => a.at - b.at);
      const card = cardById(state, cardId);
      given.forEach((a, i) => {
        if (ankiDay(a.at, roll) !== day) return;
        if (i === 0) {
          if (card && card.kind === "review") used.review += 1;
          else if (card && card.kind === "new") used.new += 1;
          return;
        }
        const previous = given[i - 1].outcome;
        if (previous !== null && previous !== undefined && previous >= DAY_S) used.review += 1;
      });
    }
    return used;
  }

  /* Anki's review order for the cut (specs/mobile.md#phone-queue): due day first, or deck
     first then due day when the preset sorts reviews deck by deck; ties keep batch order. */
  function reviewOrder(state) {
    const byDeck = state.batch && state.batch.review_order === "deck";
    return (a, b) => {
      if (byDeck && a.card.deck_rank !== b.card.deck_rank) {
        return (a.card.deck_rank || 0) - (b.card.deck_rank || 0);
      }
      if (a.due !== b.due) return a.due < b.due ? -1 : 1;
      return a.rank - b.rank;
    };
  }

  /* The phone queue now.
       queue    cards to show, in order (queue[0] is the current card)
       later    same-day returns not due yet: [{ card, at }], soonest first
       leftToday  queue + the `later` returns due before the next rollover
       counts   leftToday split like Anki: { new, learn, review }, summing to leftToday
       expired  today is past the window's last day
     Each day shows at most its quota of reviews and of new cards, minus what was answered
     that day: the share's reviews, the earlier days' reviews not reached and the cards
     returning that day are sorted in Anki's review order and cut; new cards in batch order.
     `head` (optional card id) is put first when it is in the queue (undo). */
  function buildQueue(state, now, head) {
    const batch = state.batch;
    if (!batch) {
      const counts = { new: 0, learn: 0, review: 0 };
      return { queue: [], later: [], leftToday: 0, counts, expired: false, today: null };
    }
    const t = today(state, now);
    const days = batch.days || [];
    const expired = days.length > 0 && t > days[days.length - 1];
    const visible = (c) => !isSuspended(state, c.card_id) && !isBuried(state, c.card_id, t);

    const soon = [];
    const later = [];
    const share = [];
    const returning = [];
    (batch.cards || []).forEach((c, rank) => {
      if (!visible(c)) return;
      if (answersOf(state, c.card_id).length === 0) {
        if (days[c.day] !== undefined && days[c.day] <= t) share.push(c);
        return;
      }
      const r = returnOf(state, c);
      if (r.kind === "soon") (r.at <= now ? soon : later).push({ card: c, at: r.at });
      else if (r.kind === "day" && r.day <= t) {
        returning.push({ card: c, day: r.day, at: r.at, rank: rank });
      }
    });
    soon.sort((a, b) => a.at - b.at);
    later.sort((a, b) => a.at - b.at);
    returning.sort((a, b) => (a.day < b.day ? -1 : a.day > b.day ? 1 : a.at - b.at));

    /* The day's limits: kept holds the review and new cards that fit. */
    const k = days.indexOf(t);
    const quota = k >= 0 && batch.quota ? batch.quota[k] : null;
    let kept = null;
    if (quota) {
      const used = answeredOn(state, t);
      const rankOf = new Map((batch.cards || []).map((c, i) => [c.card_id, i]));
      const reviews = share
        .filter((c) => c.kind === "review")
        .map((c) => ({ card: c, due: c.due_day || days[c.day], rank: rankOf.get(c.card_id) }))
        .concat(returning.map((r) => ({ card: r.card, due: r.day, rank: r.rank })));
      reviews.sort(reviewOrder(state));
      const news = share.filter((c) => c.kind === "new");
      kept = new Set(
        reviews
          .slice(0, Math.max(0, quota.review - used.review))
          .map((r) => r.card.card_id)
          .concat(news.slice(0, Math.max(0, quota.new - used.new)).map((c) => c.card_id))
      );
    }
    const fits = (c) => kept === null || c.kind === "learn" || kept.has(c.card_id);
    const shown = share.filter(fits);
    const back = returning.filter((r) => kept === null || kept.has(r.card.card_id));

    let queue = soon.map((s) => s.card).concat(shown, back.map((r) => r.card));
    if (head !== undefined && head !== null) {
      const i = queue.findIndex((c) => c.card_id === head);
      if (i > 0) queue = [queue[i]].concat(queue.slice(0, i), queue.slice(i + 1));
    }
    const cutoff = nextRollover(now, rollover(state));
    const laterToday = later.filter((l) => l.at < cutoff).length;
    const leftToday = queue.length + laterToday;
    /* specs/mobile.md#phone-queue § Left today: same-day returns are learning, returns on a
       later day are reviews, the unanswered share counts by its batch kind. */
    const counts = { new: 0, learn: soon.length + laterToday, review: back.length };
    for (const c of shown) {
      if (c.kind === "new") counts.new += 1;
      else if (c.kind === "learn") counts.learn += 1;
      else counts.review += 1;
    }
    return { queue, later, leftToday, counts, expired, today: t };
  }

  /* ---------- phone actions: each mutates `state` and returns what the caller needs ---------- */

  function newId() {
    const c = root.crypto;
    if (c && c.randomUUID) return c.randomUUID();
    // RFC 4122 v4 from Math.random, for an insecure context (not over tailscale serve).
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (ch) => {
      const r = (Math.random() * 16) | 0;
      return (ch === "x" ? r : (r & 0x3) | 0x8).toString(16);
    });
  }

  /* Answer `card` with `ease` (1–4). `shownAt` is when the card was displayed. Returns the
     review-log entry to append. */
  function answer(state, card, ease, now, shownAt, id) {
    const actionId = id || newId();
    const timeMs = Math.max(0, Math.min(MAX_TIME_MS, now - (shownAt || now)));
    const first = answersOf(state, card.card_id).length === 0;
    const outcome = first && card.outcomes ? card.outcomes[ease - 1] : null;
    state.pending.push({
      id: actionId,
      kind: "answer",
      card_id: card.card_id,
      at: now,
      ease: ease,
      time_ms: timeMs,
    });
    state.answers.push({
      action_id: actionId,
      card_id: card.card_id,
      at: now,
      ease: ease,
      outcome: outcome === undefined ? null : outcome,
    });
    return { action_id: actionId, card_id: card.card_id, ease: ease, answered_at: now, time_ms: timeMs };
  }

  /* Flag or unflag, whichever changes the card's current state. Returns the action. */
  function toggleFlag(state, card, now, id) {
    const kind = flagOf(state, card) ? "unflag" : "flag";
    const action = { id: id || newId(), kind: kind, card_id: card.card_id, at: now };
    state.pending.push(action);
    return action;
  }

  function suspend(state, card, now, id) {
    const action = { id: id || newId(), kind: "suspend", card_id: card.card_id, at: now };
    state.pending.push(action);
    return action;
  }

  function bury(state, card, now) {
    const burial = { card_id: card.card_id, day: today(state, now), at: now };
    state.burials.push(burial);
    return burial;
  }

  /* What undo would take back: the most recent of the last pending action and the last
     burial, or null. */
  function lastUndoable(state) {
    const p = state.pending.length ? state.pending[state.pending.length - 1] : null;
    const b = state.burials.length ? state.burials[state.burials.length - 1] : null;
    if (!p && !b) return null;
    if (p && (!b || p.at >= b.at)) return { type: "action", item: p };
    return { type: "burial", item: b };
  }

  /* Take back the last thing done. Returns { card_id, logActionId } (logActionId: the
     review-log entry to delete, for an answer) or null when there is nothing to undo. */
  function undo(state) {
    const last = lastUndoable(state);
    if (!last) return null;
    if (last.type === "burial") {
      state.burials.pop();
      return { card_id: last.item.card_id, logActionId: null };
    }
    const action = state.pending.pop();
    if (action.kind === "answer") {
      state.answers = state.answers.filter((a) => a.action_id !== action.id);
      return { card_id: action.card_id, logActionId: action.id };
    }
    return { card_id: action.card_id, logActionId: null };
  }

  /* Apply a successful sync response. `sent` is not needed: the response lists every id it
     processed. Returns the dropped list ([{ id, reason }]). */
  function applySync(state, response, now) {
    const done = new Set(response.applied || []);
    for (const d of response.dropped || []) done.add(d.id);
    state.pending = state.pending.filter((p) => !done.has(p.id));
    state.batch = response.batch;
    const stillPending = new Set(state.pending.filter((p) => p.kind === "answer").map((p) => p.id));
    state.answers = state.answers.filter((a) => stillPending.has(a.action_id));
    const t = today(state, now);
    state.burials = state.burials.filter((b) => b.day >= t);
    state.lastSync = now;
    return response.dropped || [];
  }

  function emptyState() {
    return { batch: null, pending: [], answers: [], burials: [], lastSync: null };
  }

  const api = {
    MAX_TIME_MS,
    ankiDay,
    addDays,
    nextRollover,
    today,
    cardById,
    answersOf,
    flagOf,
    isSuspended,
    returnOf,
    answeredOn,
    buildQueue,
    newId,
    answer,
    toggleFlag,
    suspend,
    bury,
    lastUndoable,
    undo,
    applySync,
    emptyState,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.MQ = api;
})(typeof self !== "undefined" ? self : globalThis);
