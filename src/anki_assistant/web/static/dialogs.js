/* <dialog> editors: edit, split, create, move, delete-confirm.
   Each textarea holds the RAW Anki value; the preview under it uses the JS port
   of render.py (renderField). Esc closes them (native <dialog> behaviour). */

"use strict";

function openModal(title, bodyNode, buttons) {
  const dlg = document.createElement("dialog");

  const head = document.createElement("div");
  head.className = "dlg-head";
  const h3 = document.createElement("h3");
  h3.textContent = title;
  head.appendChild(h3);

  const body = document.createElement("div");
  body.className = "dlg-body";
  body.appendChild(bodyNode);

  const foot = document.createElement("div");
  foot.className = "dlg-foot";
  const err = document.createElement("span");
  err.className = "grow small";
  err.style.color = "var(--danger)";
  foot.appendChild(err);

  const ctx = {
    dialog: dlg,
    close: () => dlg.close(),
    setError: (m) => {
      err.textContent = m || "";
    },
    setBusy: (b) => {
      foot.querySelectorAll("button").forEach((x) => {
        x.disabled = !!b;
      });
    },
  };

  (buttons || []).forEach((b) => {
    const btn = document.createElement("button");
    btn.className = b.cls || "";
    btn.textContent = b.label;
    btn.addEventListener("click", () => b.onClick(ctx));
    foot.appendChild(btn);
  });

  dlg.appendChild(head);
  dlg.appendChild(body);
  dlg.appendChild(foot);
  document.body.appendChild(dlg);
  dlg.addEventListener("close", () => dlg.remove());
  dlg.showModal();
  const first = body.querySelector("textarea, input, select");
  if (first) first.focus();
  return ctx;
}

/* Runs an API call from a dialog: busy state, error inline, then the usual refresh. */
async function fromDialog(ctx, fn, opts) {
  ctx.setError("");
  ctx.setBusy(true);
  try {
    await fn();
    ctx.close();
    await afterDecision(opts || {});
  } catch (e) {
    ctx.setError((e && e.message) || "erreur");
  } finally {
    ctx.setBusy(false);
  }
}

function labelled(text, node) {
  const wrap = document.createElement("div");
  const lab = document.createElement("div");
  lab.className = "field-name";
  lab.textContent = text;
  wrap.appendChild(lab);
  wrap.appendChild(node);
  return wrap;
}

function checkbox(text, checked) {
  const lab = document.createElement("label");
  lab.className = "check";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = !!checked;
  lab.appendChild(input);
  lab.appendChild(document.createTextNode(" " + text));
  return { el: lab, input, checked: () => input.checked };
}

/* ---------------- note editor ---------------- */

function emptyFieldsFor(model, fallback) {
  const names = fieldNames(model);
  const out = {};
  (names.length ? names : Object.keys(fallback || {})).forEach((n) => {
    out[n] = "";
  });
  return out;
}

/* opts: { title, model, fields, tags, showModel, showTags, onRemove } */
function buildNoteEditor(opts) {
  const el = document.createElement("div");
  el.className = "editor";
  let model = opts.model;
  const values = Object.assign({}, opts.fields || {});
  const boxes = {};

  const head = document.createElement("div");
  head.className = "editor-head";
  if (opts.title) {
    const t = document.createElement("b");
    t.textContent = opts.title;
    head.appendChild(t);
  }

  let modelSelect = null;
  if (opts.showModel) {
    modelSelect = document.createElement("select");
    const names = Object.keys(S.models || {});
    (names.length ? names : [model]).forEach((m) => {
      const o = document.createElement("option");
      o.value = m;
      o.textContent = m;
      if (m === model) o.selected = true;
      modelSelect.appendChild(o);
    });
    modelSelect.addEventListener("change", () => {
      snapshot();
      model = modelSelect.value;
      const next = emptyFieldsFor(model, values);
      Object.keys(next).forEach((n) => {
        if (values[n] !== undefined) next[n] = values[n];
      });
      Object.keys(values).forEach((n) => delete values[n]);
      Object.assign(values, next);
      rebuild();
    });
    head.appendChild(modelSelect);
  } else if (model) {
    const t = document.createElement("span");
    t.className = "tag";
    t.textContent = model;
    head.appendChild(t);
  }

  const spacer = document.createElement("span");
  spacer.className = "grow";
  head.appendChild(spacer);

  if (opts.onRemove) {
    const rm = document.createElement("button");
    rm.className = "ghost small";
    rm.textContent = "retirer";
    rm.addEventListener("click", () => opts.onRemove(el));
    head.appendChild(rm);
  }
  el.appendChild(head);

  const fieldsWrap = document.createElement("div");
  el.appendChild(fieldsWrap);

  let tagsInput = null;
  if (opts.showTags) {
    tagsInput = document.createElement("input");
    tagsInput.className = "tags-edit";
    tagsInput.value = tagsToString(opts.tags);
    el.appendChild(labelled("tags (séparés par des espaces)", tagsInput));
  }

  function snapshot() {
    Object.keys(boxes).forEach((n) => {
      values[n] = boxes[n].value;
    });
  }

  function rebuild() {
    fieldsWrap.innerHTML = "";
    Object.keys(boxes).forEach((k) => delete boxes[k]);
    Object.keys(values).forEach((name) => {
      const block = document.createElement("div");
      block.className = "field-edit";
      const lab = document.createElement("div");
      lab.className = "field-name";
      lab.textContent = name;
      const ta = document.createElement("textarea");
      ta.value = values[name] || "";
      ta.spellcheck = false;
      const prev = document.createElement("div");
      prev.className = "preview";
      prev.innerHTML = renderField(ta.value);
      ta.addEventListener("input", () => {
        values[name] = ta.value;
        prev.innerHTML = renderField(ta.value);
      });
      block.appendChild(lab);
      block.appendChild(ta);
      block.appendChild(prev);
      fieldsWrap.appendChild(block);
      boxes[name] = ta;
    });
  }
  rebuild();

  return {
    el,
    read: () => {
      snapshot();
      const fields = {};
      Object.keys(values).forEach((n) => {
        fields[n] = values[n] || "";
      });
      return { model, fields, tags: tagsInput ? parseTags(tagsInput.value) : opts.tags || [] };
    },
    setDisabled: (b) => {
      el.classList.toggle("off", !!b);
      el.querySelectorAll("textarea, input, select").forEach((x) => {
        if (x.type !== "checkbox") x.disabled = !!b;
      });
    },
  };
}

function reasonToggle(note) {
  if (!note || !note.reason) return null;
  if (!(REASON_FIELD in (note.fields || {}))) return null;
  return checkbox("vider « " + REASON_FIELD + " » (la raison du flag)", true);
}

/* ---------------- edit ---------------- */

function openEditDialog(note) {
  const wrap = document.createElement("div");
  const ed = buildNoteEditor({
    model: note.model,
    fields: note.fields || {},
    tags: note.tags || [],
    showModel: false,
    showTags: true,
  });
  wrap.appendChild(ed.el);
  const clear = reasonToggle(note);
  if (clear) wrap.appendChild(clear.el);

  openModal("Modifier " + short(note.note_id), wrap, [
    { label: "Annuler", onClick: (c) => c.close() },
    {
      label: "Enregistrer",
      cls: "primary",
      onClick: (c) => {
        const data = ed.read();
        if (clear && clear.checked()) data.fields[REASON_FIELD] = "";
        fromDialog(
          c,
          () => API.patch(note.note_id, { fields: data.fields, tags: data.tags, unflag: true }),
          { resolvedId: note.note_id },
        );
      },
    },
  ]);
}

/* ---------------- split ---------------- */

function openSplitDialog(note) {
  const wrap = document.createElement("div");

  const orig = buildNoteEditor({
    title: "Original " + short(note.note_id),
    model: note.model,
    fields: note.fields || {},
    tags: note.tags || [],
    showModel: false,
    showTags: true,
  });
  wrap.appendChild(orig.el);

  const clear = reasonToggle(note);
  if (clear) wrap.appendChild(clear.el);

  const del = checkbox("supprimer l'original (son historique est perdu)", false);
  del.input.addEventListener("change", () => {
    orig.setDisabled(del.checked());
    if (clear) clear.input.disabled = del.checked();
  });
  const delRow = document.createElement("div");
  delRow.style.margin = "6px 0 14px";
  delRow.appendChild(del.el);
  wrap.appendChild(delRow);

  const fragWrap = document.createElement("div");
  wrap.appendChild(fragWrap);
  const frags = [];

  function addFragment() {
    const ed = buildNoteEditor({
      title: "Nouvelle note " + (frags.length + 1),
      model: note.model,
      fields: emptyFieldsFor(note.model, note.fields),
      tags: note.tags || [],
      showModel: true,
      showTags: true,
      onRemove: (el) => {
        const i = frags.findIndex((f) => f.el === el);
        if (i >= 0) frags.splice(i, 1);
        el.remove();
      },
    });
    frags.push(ed);
    fragWrap.appendChild(ed.el);
  }
  addFragment();

  const add = document.createElement("button");
  add.textContent = "+ ajouter une note";
  add.addEventListener("click", addFragment);
  wrap.appendChild(add);

  openModal("Splitter " + short(note.note_id), wrap, [
    { label: "Annuler", onClick: (c) => c.close() },
    {
      label: "Splitter",
      cls: "primary",
      onClick: (c) => {
        const newNotes = frags
          .map((f) => f.read())
          .filter((d) => Object.keys(d.fields).some((k) => (d.fields[k] || "").trim()))
          .map((d) => ({ model: d.model, fields: d.fields, tags: d.tags }));
        if (!newNotes.length) {
          c.setError("Ajoute au moins une nouvelle note non vide.");
          return;
        }
        const send = () => {
          let original = null;
          if (!del.checked()) {
            const data = orig.read();
            if (clear && clear.checked()) data.fields[REASON_FIELD] = "";
            original = { fields: data.fields, tags: data.tags };
          }
          fromDialog(c, () => API.split(note.note_id, { original, new_notes: newNotes }), {
            resolvedId: note.note_id,
          });
        };
        if (del.checked()) {
          openConfirm(
            "Supprimer l'original ?",
            "La note " +
              short(note.note_id) +
              " sera supprimée après la création des " +
              newNotes.length +
              " nouvelle(s) note(s). Sans retour en arrière.",
            "Supprimer et splitter",
            send,
          );
        } else {
          send();
        }
      },
    },
  ]);
}

/* ---------------- create ---------------- */

function openCreateDialog(note) {
  const models = Object.keys(S.models || {});
  const model = (note && note.model) || models[0] || "Basic";
  // « addNote in the same deck (sibling) »: the note's own deck, not the deck node
  // the user happens to have selected (which may be an ancestor).
  const deck = (note && note.deck) || S.deck;
  const wrap = document.createElement("div");
  const info = document.createElement("div");
  info.className = "muted small";
  info.textContent = "Deck : " + (deck || "");
  wrap.appendChild(info);
  const ed = buildNoteEditor({
    model,
    fields: emptyFieldsFor(model, note && note.fields),
    tags: (note && note.tags) || [],
    showModel: true,
    showTags: true,
  });
  wrap.appendChild(ed.el);

  openModal("Créer une note", wrap, [
    { label: "Annuler", onClick: (c) => c.close() },
    {
      label: "Créer",
      cls: "primary",
      onClick: (c) => {
        const data = ed.read();
        if (!Object.keys(data.fields).some((k) => (data.fields[k] || "").trim())) {
          c.setError("La note est vide.");
          return;
        }
        fromDialog(
          c,
          () =>
            API.create({
              deck,
              model: data.model,
              fields: data.fields,
              tags: data.tags,
            }),
          // Créer leaves the original flagged and selected.
          { keepSelection: true },
        );
      },
    },
  ]);
}

/* ---------------- move ---------------- */

function openMoveDialog(note) {
  const wrap = document.createElement("div");
  const search = document.createElement("input");
  search.placeholder = "filtrer les decks…";
  search.style.width = "100%";
  wrap.appendChild(search);

  const list = document.createElement("div");
  list.className = "deck-list";
  wrap.appendChild(list);

  let chosen = note.deck || S.deck || "";
  function paint() {
    const needle = search.value.trim().toLowerCase();
    const names = (S.decks || [])
      .map((d) => d.name)
      .filter((n) => !needle || n.toLowerCase().indexOf(needle) >= 0);
    list.innerHTML = names
      .map(
        (n) =>
          '<div class="opt' +
          (n === chosen ? " on" : "") +
          '" data-deck-opt="' +
          esc(n) +
          '">' +
          esc(n) +
          "</div>",
      )
      .join("");
    list.querySelectorAll("[data-deck-opt]").forEach((el) => {
      el.addEventListener("click", () => {
        chosen = el.getAttribute("data-deck-opt");
        paint();
      });
    });
  }
  search.addEventListener("input", paint);
  paint();

  openModal("Déplacer " + short(note.note_id), wrap, [
    { label: "Annuler", onClick: (c) => c.close() },
    {
      label: "Déplacer",
      cls: "primary",
      onClick: (c) => {
        if (!chosen) {
          c.setError("Choisis un deck.");
          return;
        }
        fromDialog(c, () => API.move(note.note_id, chosen), { resolvedId: note.note_id });
      },
    },
  ]);
}

/* ---------------- confirmations ---------------- */

function openConfirm(title, message, confirmLabel, onConfirm) {
  const body = document.createElement("div");
  body.textContent = message;
  openModal(title, body, [
    { label: "Annuler", onClick: (c) => c.close() },
    {
      label: confirmLabel,
      cls: "danger solid",
      onClick: (c) => {
        c.close();
        onConfirm();
      },
    },
  ]);
}

function openDeleteDialog(note) {
  const body = document.createElement("div");
  body.textContent =
    "Supprimer définitivement la note " +
    short(note.note_id) +
    " et ses " +
    nCards(note) +
    " carte(s) ? Sans retour en arrière.";
  openModal("Supprimer " + short(note.note_id), body, [
    { label: "Annuler", onClick: (c) => c.close() },
    {
      label: "Supprimer",
      cls: "danger solid",
      onClick: (c) => fromDialog(c, () => API.remove(note.note_id), { resolvedId: note.note_id }),
    },
  ]);
}
