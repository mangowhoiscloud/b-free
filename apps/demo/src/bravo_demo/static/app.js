// Demo chat client (§8.10). Reconnects with the last message id after any close (1012 at cutover),
// resends unacknowledged writes with the same idempotency key, and shows which environment serves it.
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const ROOM_RE = /^[a-z0-9][a-z0-9-]{0,39}$/;
  const state = {
    room: null, ws: null, lastId: 0, env: null, limits: null, retryMs: 0, closedByUs: false,
    messages: new Map(),   // id -> message
    pending: new Map(),    // idem_key -> {idem_key, author, body, attachment, status}
    attachment: null,      // {file, idem_key, uploaded}
    dbOk: true, connected: false, hadConnection: false,
  };

  // crypto.randomUUID needs a secure context; phones on plain http LAN addresses do not have one.
  function uuid4() {
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
  }
  function store(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* blocked */ } }
  function load(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
  function myName() { return load("bravo.name") || ""; }
  function fmtBytes(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.ceil(n / 1024) + " KB"; }
  function envLabel(env) { return env ? ((i18nLang === "ja" && env.label_ja) ? env.label_ja : env.label) : "…"; }

  let toastTimer = null;
  function toast(text) {
    const el = $("toast"); el.textContent = text; el.classList.remove("hidden");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.add("hidden"), 4000);
  }
  function banner(text, kind) {
    const el = $("banner");
    if (!text) { el.classList.add("hidden"); return; }
    el.textContent = text; el.classList.toggle("err", kind === "err"); el.classList.remove("hidden");
  }

  function renderBadges() {
    $("env-text").textContent = t("envPrefix") + ": " + envLabel(state.env);
    const live = $("live");
    live.classList.remove("off", "wait");
    if (!state.connected) { live.classList.add("off"); $("live-text").textContent = state.hadConnection ? t("reconnecting") : t("connecting"); }
    else if (!state.dbOk) { live.classList.add("wait"); $("live-text").textContent = t("waiting"); }
    else { $("live-text").textContent = t("live"); }
    $("attach-label").classList.toggle("disabled", !(state.limits && state.limits.attachments));
  }

  function attachmentNode(att) {
    const box = document.createElement("div"); box.className = "att";
    if (att.deleted) { box.textContent = t("attachDeleted"); return box; }
    const url = "/api/attachments/" + encodeURIComponent(att.id);
    if (att.content_type && att.content_type.startsWith("image/")) {
      const img = document.createElement("img"); img.src = url; img.alt = att.filename; img.loading = "lazy";
      box.appendChild(img);
    }
    const a = document.createElement("a"); a.href = url; a.target = "_blank"; a.rel = "noopener";
    a.textContent = att.filename + " (" + fmtBytes(att.size_bytes) + ")";
    box.appendChild(a);
    return box;
  }

  function messageNode(m, opts) {
    const mine = m.author === myName();
    const el = document.createElement("div");
    el.className = "msg" + (mine ? " mine" : "") + (opts.pending ? " pending" : "") + (opts.failed ? " failed" : "");
    const who = document.createElement("div"); who.className = "who"; who.textContent = mine ? t("me") : m.author;
    const bubble = document.createElement("div"); bubble.className = "bubble"; bubble.textContent = m.body;
    if (m.attachment) bubble.appendChild(attachmentNode(m.attachment));
    const meta = document.createElement("div"); meta.className = "meta";
    if (opts.pending) {
      const s = document.createElement("span"); s.textContent = opts.failed ? t("failed") : t("sending"); meta.appendChild(s);
      if (opts.failed && opts.retryable) {
        const b = document.createElement("button"); b.type = "button"; b.className = "retry"; b.textContent = t("retry");
        b.addEventListener("click", () => resend(opts.idem_key)); meta.appendChild(b);
      }
    } else {
      const d = new Date(m.created_at);
      meta.textContent = d.toLocaleTimeString(i18nLang === "ja" ? "ja-JP" : "ko-KR", { hour: "2-digit", minute: "2-digit" });
    }
    el.append(who, bubble, meta);
    return el;
  }

  function render() {
    const list = $("list");
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
    list.replaceChildren();
    const ids = Array.from(state.messages.keys()).sort((a, b) => a - b);
    for (const id of ids) list.appendChild(messageNode(state.messages.get(id), {}));
    for (const p of state.pending.values()) {
      list.appendChild(messageNode({ author: p.author, body: p.body, attachment: p.attachment },
        { pending: true, failed: p.status === "failed", retryable: p.retryable, idem_key: p.idem_key }));
    }
    if (!ids.length && !state.pending.size) {
      const e = document.createElement("p"); e.className = "empty"; e.textContent = t("empty"); list.appendChild(e);
    }
    if (nearBottom || state.pending.size) list.scrollTop = list.scrollHeight;
    renderBadges();
  }

  function addMessages(items) {
    let changed = false;
    for (const m of items) {
      if (!state.messages.has(m.id)) { state.messages.set(m.id, m); changed = true; }
      if (m.id > state.lastId) state.lastId = m.id;
      if (state.pending.delete(m.idem_key)) changed = true;
    }
    // Keep the view bounded on long demos.
    if (state.messages.size > 500) {
      const ids = Array.from(state.messages.keys()).sort((a, b) => a - b);
      for (const id of ids.slice(0, state.messages.size - 500)) state.messages.delete(id);
    }
    if (changed) render();
  }

  function transmit(p) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({ type: "send", idem_key: p.idem_key, author: p.author, body: p.body,
        attachment_id: p.attachment ? p.attachment.id : null }));
    }
  }
  function resend(idemKey) {
    const p = state.pending.get(idemKey); if (!p) return;
    p.status = "sending"; render(); transmit(p);
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = proto + "//" + location.host + "/ws?room=" + encodeURIComponent(state.room) + "&after=" + state.lastId;
    const ws = new WebSocket(url);
    state.ws = ws;
    ws.onopen = () => { state.retryMs = 0; };
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === "hello") {
        const prev = state.env;
        state.env = msg.env; state.limits = msg.limits; state.connected = true; state.dbOk = true;
        if (prev && prev.id !== msg.env.id) {
          toast(t("envChanged", { env: envLabel(msg.env) }));
          const b = $("env"); b.classList.remove("flash"); void b.offsetWidth; b.classList.add("flash");
        } else if (state.hadConnection) { toast(t("reconnected")); }
        state.hadConnection = true; banner(null);
        // Resend anything not acknowledged before the close, with the same idempotency keys
        // (also writes that already showed the retry notice: the key prevents duplicates).
        for (const p of state.pending.values()) {
          if (p.status === "sending" || (p.status === "failed" && p.retryable)) { p.status = "sending"; transmit(p); }
        }
        render();
      } else if (msg.type === "messages") {
        addMessages(msg.items);
      } else if (msg.type === "ack") {
        state.pending.delete(msg.idem_key);
        if (!Array.from(state.pending.values()).some((p) => p.status === "failed")) banner(null);
        addMessages([msg.message]);
        render();
      } else if (msg.type === "nack") {
        const p = state.pending.get(msg.idem_key); if (!p) return;
        if (msg.code === "write_timeout" || msg.code === "busy" || msg.code === "db_error") {
          p.status = "failed"; p.retryable = true; banner(t("delayed"));
        } else if (msg.code === "attachment_missing") {
          p.status = "failed"; p.retryable = false; banner(t("attachMissing"), "err");
        } else { p.status = "failed"; p.retryable = false; banner(t("invalid"), "err"); }
        render();
      } else if (msg.type === "status") {
        const was = state.dbOk; state.dbOk = !!msg.db_ok;
        if (was !== state.dbOk) { banner(state.dbOk ? null : t("dbWaiting")); renderBadges(); }
      }
    };
    ws.onclose = (ev) => {
      state.connected = false; state.ws = null; renderBadges();
      if (state.closedByUs) return;
      // 1012 (service restart, cutover): reconnect almost at once. Otherwise back off up to 5 s.
      const base = ev.code === 1012 ? 150 : Math.min(5000, Math.max(500, state.retryMs * 2 || 500));
      state.retryMs = base;
      setTimeout(connect, base + Math.floor(Math.random() * 250));
    };
  }

  async function uploadAttachment() {
    const a = state.attachment;
    if (!a || a.uploaded) return a && a.uploaded;
    banner(t("attachUploading"));
    let res;
    try {
      res = await fetch("/api/rooms/" + encodeURIComponent(state.room) + "/attachments", {
        method: "PUT",
        headers: { "Idempotency-Key": a.idem_key, "X-Filename": encodeURIComponent(a.file.name),
          "X-Author": encodeURIComponent(myName()), "Content-Type": a.file.type || "application/octet-stream" },
        body: a.file,
      });
    } catch (e) { banner(t("attachFailed"), "err"); return null; }
    let doc = {}; try { doc = await res.json(); } catch (e) { /* empty */ }
    if (res.ok) { a.uploaded = doc.attachment; banner(null); return a.uploaded; }
    if (res.status === 423) banner(t("attachPaused"), "err");
    else if (res.status === 413) banner(t("attachTooBig", { max: fmtBytes(doc.max_bytes || 0) }), "err");
    else if (res.status === 503) banner(t("delayed"));
    else banner(t("attachFailed"), "err");
    return null; // the same idempotency key is reused on the next try
  }

  function setAttachment(file) {
    if (!file) { state.attachment = null; $("attach-chip").classList.add("hidden"); $("file").value = ""; return; }
    const max = state.limits ? state.limits.max_attachment_bytes : 0;
    if (max && file.size > max) { banner(t("attachTooBig", { max: fmtBytes(max) }), "err"); $("file").value = ""; return; }
    state.attachment = { file, idem_key: uuid4(), uploaded: null };
    $("attach-name").textContent = file.name + " (" + fmtBytes(file.size) + ")";
    $("attach-chip").classList.remove("hidden");
  }

  async function onSubmit(ev) {
    ev.preventDefault();
    if (!myName()) { askName(); return; }
    const input = $("text");
    let body = input.value.trim();
    let att = null;
    if (state.attachment) {
      $("send").disabled = true;
      att = await uploadAttachment();
      $("send").disabled = false;
      if (!att) return;
      if (!body) body = att.filename;
    }
    if (!body) return;
    const p = { idem_key: uuid4(), author: myName(), body: body.slice(0, 2000), attachment: att, status: "sending" };
    state.pending.set(p.idem_key, p);
    input.value = ""; setAttachment(null); render(); transmit(p);
    // No answer within the write wait (8 s) plus slack: show the retry notice.
    const waitMs = ((state.limits && state.limits.write_wait_s) || 8) * 1000 + 1500;
    setTimeout(() => {
      const q = state.pending.get(p.idem_key);
      if (q && q.status === "sending") { q.status = "failed"; q.retryable = true; banner(t("delayed")); render(); }
    }, waitMs);
  }

  function askName() {
    const dlg = $("name-dialog");
    $("name-input").value = myName();
    if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
  }

  function init() {
    const room = (params.get("room") || "").toLowerCase();
    applyI18n();
    $("lang").addEventListener("click", () => { toggleLang(); render(); });
    $("composer").addEventListener("submit", onSubmit);
    $("file").addEventListener("change", (e) => setAttachment(e.target.files[0]));
    $("attach-clear").addEventListener("click", () => setAttachment(null));
    $("name-form").addEventListener("submit", () => {
      const v = $("name-input").value.trim().slice(0, 40);
      if (v) { store("bravo.name", v); render(); }
    });
    fetch("/api/config", { cache: "no-store" }).then((r) => r.json()).then((cfg) => {
      state.room = ROOM_RE.test(room) ? room : cfg.room_default;
      state.env = cfg.env; state.limits = cfg.limits;
      render(); connect();
    }).catch(() => { state.room = ROOM_RE.test(room) ? room : "consult-1"; connect(); });
    if (!myName()) askName();
    render();
  }
  init();
})();
