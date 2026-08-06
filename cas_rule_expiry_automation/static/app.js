/* Shared helpers for Live + Backtest pages */
const $ = (id) => document.getElementById(id);

function msg(t, cls) {
  const el = $("msg");
  if (!el) return;
  el.textContent = t || "";
  el.className = cls || "";
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({ ok: false, error: "bad json" }));
  if (!res.ok || data.ok === false) throw new Error(data.error || res.statusText);
  return data;
}

function setModeBadges(live) {
  const bLive = $("b-live");
  const kiteMode = $("kite-mode");
  if (bLive) {
    bLive.textContent = live ? "LIVE ORDERS" : "PAPER";
    bLive.className = "badge " + (live ? "live" : "warn");
  }
  if (kiteMode) {
    // PAPER still streams real Kite data — only orders are dry-run
    kiteMode.textContent = live ? "ORDERS ON" : "PAPER";
    kiteMode.className = "kite-mode " + (live ? "live" : "offline");
  }
  const modeBig = $("mode-big");
  if (modeBig) {
    modeBig.textContent = live
      ? "LIVE — real Zerodha MARKET orders"
      : "PAPER — real Kite ticks, dry-run MARKET (no orders sent)";
    modeBig.className = "mode-banner " + (live ? "live" : "paper");
  }
}

function toggleCollapse(id, forceOpen) {
  const el = $(id);
  if (!el) return;
  const open =
    forceOpen === true ? true : forceOpen === false ? false : !el.classList.contains("open");
  el.classList.toggle("open", open);
  if (id === "kite-panel") {
    const btn = $("kite-btn");
    if (btn) btn.classList.toggle("open", open);
  }
}

function toggleKitePanel() {
  toggleCollapse("kite-panel");
  if ($("kite-panel") && $("kite-panel").classList.contains("open") && $("access_token")) {
    $("access_token").focus();
  }
}

function toggleAdvKeys() {
  const block = $("adv-keys");
  if (!block) return;
  const open = !block.classList.contains("open");
  block.classList.toggle("open", open);
  const tog = $("adv-toggle");
  if (tog) {
    tog.textContent = open ? "Hide API key & secret" : "Show API key & secret (masked)";
  }
}
