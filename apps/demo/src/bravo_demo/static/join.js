"use strict";
(async function () {
  applyI18n();
  document.getElementById("lang").addEventListener("click", () => { toggleLang(); render(); });
  let cfg = null;
  function envLabel() {
    if (!cfg) return "…";
    return (i18nLang === "ja" && cfg.env.label_ja) ? cfg.env.label_ja : cfg.env.label;
  }
  function render() { document.getElementById("env-text").textContent = envLabel(); }
  try {
    const res = await fetch("/api/config", { cache: "no-store" });
    cfg = await res.json();
    const a = document.getElementById("join-url");
    a.href = cfg.join_url; a.textContent = cfg.join_url;
    document.getElementById("qr").src = "/qr.svg?room=" + encodeURIComponent(cfg.room_default);
    render();
  } catch (e) { /* the QR image still works */ }
})();
