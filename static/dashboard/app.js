const POLL_MS = 2500;
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let STATE = null;
let HEALTH = null;
let VIEW = location.hash.replace("#", "") || "dashboard";

// ---------- formatting ----------
const inr = (v, dp = 2) =>
  v === null || v === undefined || Number.isNaN(v)
    ? "—"
    : (v < 0 ? "-" : "") + "₹" + Math.abs(v).toLocaleString("en-IN", { minimumFractionDigits: dp, maximumFractionDigits: dp });
const num = (v, dp = 2) => (v === null || v === undefined ? "—" : Number(v).toFixed(dp));
const cls = (v) => (v === null || v === undefined ? "" : v >= 0 ? "pos" : "neg");
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ageText = (t) => (!t ? "—" : t.age_s < 1 ? "just now" : Math.round(t.age_s) + "s ago");
const dotFor = (t) => (!t ? "dead" : t.stale ? "stale" : "fresh");

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const text = await res.text();
  let body;
  try { body = text ? JSON.parse(text) : {}; } catch { body = { detail: text }; }
  if (!res.ok) throw new Error(body.detail || `${res.status}`);
  return body;
}

// ---------- table helper (one renderer, not twelve) ----------
function table(el, columns, rows, opts = {}) {
  if (!rows.length) {
    el.innerHTML = `<tbody><tr><td class="empty">${esc(opts.empty || "Nothing yet.")}</td></tr></tbody>`;
    return;
  }
  const head = columns.map((c) => `<th class="${c.num ? "num" : ""}">${esc(c.label)}</th>`).join("");
  const body = rows
    .map((r) => {
      const tds = columns
        .map((c) => {
          const v = c.render ? c.render(r) : r[c.key];
          return `<td class="${c.num ? "num " : ""}${c.cls ? c.cls(r) : ""}">${v ?? "—"}</td>`;
        })
        .join("");
      return `<tr class="${opts.rowClass ? opts.rowClass(r) : ""}">${tds}</tr>`;
    })
    .join("");
  el.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>`;
}

// ---------- views ----------
function setView(v) {
  VIEW = v;
  location.hash = v;
  $$(".view").forEach((s) => (s.hidden = s.id !== "view-" + v));
  $$(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  // Spot / prev-range / VIX belong to the Dashboard only. The feed badge and
  // Refresh stay everywhere, since knowing the feed is alive matters on every
  // view.
  const onDash = v === "dashboard";
  $(".spot").hidden = !onDash;
  $(".range").hidden = !onDash;
  $(".topbar").classList.toggle("compact", !onDash);
  render();
}

function renderTop() {
  const s = STATE?.spot;
  $("#spot-val").textContent = s ? num(s.ltp) : "—";
  $("#spot-dot").className = "dot " + dotFor(s);
  $("#tick-age").textContent = "last tick: " + ageText(s);
  $("#prev-high").textContent = num(STATE?.prev_range?.high);
  $("#prev-low").textContent = num(STATE?.prev_range?.low);
  $("#vix").textContent = STATE?.vix ? num(STATE.vix.ltp) : "—";

  const badge = $("#feed-badge");
  if (HEALTH) {
    const f = HEALTH.feed;
    badge.textContent = f.connected
      ? `feed: live${HEALTH.stale_key_count ? ` · ${HEALTH.stale_key_count} stale` : ""}`
      : "feed: reconnecting (poll fallback)";
    badge.className = "badge " + (f.connected ? (HEALTH.stale_key_count ? "warn" : "ok") : "warn");
    $("#auth-banner").hidden = !!HEALTH.broker_authenticated;
  }

  const killed = !!STATE?.kill_switch?.enabled;
  $("#kill-banner").hidden = !killed;
  const kb = $("#kill-btn");
  kb.textContent = killed ? "✅ Release kill" : "🛑 Kill switch";
  kb.className = killed ? "btn btn-ghost" : "btn btn-danger";

  const t = STATE?.ledger?.totals;
  $("#side-net").textContent = t ? inr(t.net_pnl, 0) : "—";
  $("#side-net").className = cls(t?.net_pnl);
  $("#side-trades").textContent = t ? t.count : "—";
  $("#side-open").textContent = (STATE?.strategies || []).filter((s) => s.status === "live").length;
}

function renderDashboard() {
  const strats = STATE.strategies || [];
  const live = strats.filter((s) => s.status === "live");
  const openMtm = live.reduce((a, s) => a + (s.mtm.combined_exit || 0), 0);
  const t = STATE.ledger.totals;

  $("#kpis").innerHTML = [
    kpi("NIFTY", STATE.spot ? num(STATE.spot.ltp) : "—", STATE.spot ? ageText(STATE.spot) : "no tick"),
    kpi("Open P&L", inr(openMtm, 0), `${live.length} live`, cls(openMtm)),
    kpi("Closed today", inr(t.net_pnl, 0), `${t.count} trades, net of charges`, cls(t.net_pnl)),
    kpi("Charges today", inr(t.total_charges, 0), "brokerage + taxes"),
    kpi("Kill switch", STATE.kill_switch.enabled ? "ARMED" : "open", STATE.kill_switch.enabled ? "orders refused" : "orders allowed"),
  ].join("");

  table(
    $("#dash-table"),
    [
      { label: "Strategy", render: (s) => esc(s.name) },
      { label: "Mode", render: (s) => modePill(s.mode) },
      { label: "Status", render: (s) => statusPill(s) },
      { label: "Legs", render: (s) => s.mtm.legs.map((l) => esc(l.symbol)).join("<br>") || "—" },
      { label: "P&L (exit)", num: true, cls: (s) => cls(s.mtm.combined_exit), render: (s) => inr(s.mtm.combined_exit, 0) },
      { label: "Slippage", num: true, render: (s) => (s.mtm.slippage != null ? inr(s.mtm.slippage, 0) : "—") },
      { label: "Target / SL", num: true, render: (s) => `${inr(s.profit_target, 0)} / ${inr(-s.loss_limit, 0)}` },
    ],
    strats,
    { empty: "No strategies configured." }
  );

  renderLedgerTable($("#dash-ledger"));
}

const kpi = (label, val, sub, klass = "") =>
  `<div class="kpi"><div class="kpi-label">${esc(label)}</div>
   <div class="kpi-val ${klass}">${val}</div><div class="kpi-sub">${esc(sub || "")}</div></div>`;

const modePill = (m) => `<span class="pill ${m === "live" ? "pill-live-mode" : "pill-paper"}">${esc(m)}</span>`;
const statusPill = (s) =>
  `<span class="pill pill-${esc(s.status)}">${esc(s.status)}</span>` +
  (s.detail ? `<div class="small muted">${esc(s.detail)}</div>` : "");

function renderStrategies() {
  const host = $("#strategy-cards");
  const strats = STATE.strategies || [];
  if (!strats.length) { host.innerHTML = `<div class="card empty">No strategies.</div>`; return; }

  host.innerHTML = strats.map(strategyCard).join("");
}

function strategyCard(s) {
  const m = s.mtm;
  const live = s.status === "live";
  const plan = s.plan;

  const legRows = live
    ? m.legs
        .map(
          (l) => `<tr>
            <td>${esc(l.symbol)}</td><td class="num">${l.qty}</td>
            <td class="num">${num(l.avg_entry)}</td><td class="num">${num(l.ltp)}</td>
            <td class="num">${num(l.exit_price)}</td>
            <td class="num ${cls(l.mtm_exit)}">${inr(l.mtm_exit, 0)}</td></tr>`
        )
        .join("")
    : plan && !plan.error
    ? ["ce", "pe"]
        .map((k) => {
          const t = plan[k].tick;
          return `<tr><td>${esc(plan[k].symbol)}</td><td class="num">-${plan.qty_per_leg}</td>
            <td class="num">—</td><td class="num">${t ? num(t.ltp) : "—"}</td>
            <td class="num">${t && t.ask ? num(t.ask) : "—"}</td><td class="num">—</td></tr>`;
        })
        .join("")
    : "";

  return `<div class="card strategy" data-sid="${esc(s.id)}">
    <div class="strategy-head">
      <span class="strategy-name">${esc(s.name)}</span>
      ${modePill(s.mode)} ${statusPill(s)}
      <div class="grow"></div>
      ${live
        ? `<button class="btn btn-danger-outline btn-small" data-action="exit" data-sid="${esc(s.id)}">Square off</button>`
        : `<button class="btn btn-primary btn-small" data-action="enter" data-sid="${esc(s.id)}">Enter now</button>`}
    </div>

    ${live
      ? `<div class="mtm-hero">
           <span class="mtm-big ${cls(m.combined_exit)}">${inr(m.combined_exit, 0)}</span>
           <div class="mtm-meta">
             <span>at LTP <b>${inr(m.combined_ltp, 0)}</b></span>
             <span>slippage <b>${inr(m.slippage, 0)}</b></span>
             <span>peak <b>${inr(s.trigger.peak_day, 0)}</b></span>
             <span>worst <b>${inr(s.trigger.trough_day, 0)}</b></span>
             ${s.trigger.trail_sl != null ? `<span>trailing SL <b>${inr(s.trigger.trail_sl, 0)}</b></span>` : ""}
             ${s.trigger.lock_floor != null ? `<span>locked floor <b>${inr(s.trigger.lock_floor, 0)}</b></span>` : ""}
           </div>
         </div>`
      : plan && plan.error
      ? `<div class="empty">Cannot plan entry: ${esc(plan.error)}</div>`
      : plan
      ? `<div class="muted small">Planned for ${esc(plan.expiry)} · CE ${plan.ce_strike} / PE ${plan.pe_strike} · ${plan.qty_per_leg} per leg</div>`
      : `<div class="empty">Resolving strikes…</div>`}

    ${legRows
      ? `<div class="table-wrap"><table>
          <thead><tr><th>Leg</th><th class="num">Qty</th><th class="num">Entry</th>
          <th class="num">LTP</th><th class="num">Exit at</th><th class="num">P&L</th></tr></thead>
          <tbody>${legRows}</tbody></table></div>`
      : ""}

    <div class="cfg-grid" data-cfg="${esc(s.id)}">
      <label>Lots <input type="number" min="1" data-k="lots" value="${s.lots}"></label>
      <label>Target ₹ <input type="number" step="250" data-k="profit_target" value="${s.profit_target}"></label>
      <label>Stop ₹ <input type="number" step="250" data-k="loss_limit" value="${s.loss_limit}"></label>
      <label>Mode
        <select data-k="mode">
          <option value="paper" ${s.mode === "paper" ? "selected" : ""}>paper</option>
          <option value="live" ${s.mode === "live" ? "selected" : ""}>LIVE</option>
        </select>
      </label>
      <label>Basis
        <select data-k="loss_limit_basis">
          <option value="exit" ${s.loss_limit_basis === "exit" ? "selected" : ""}>exit price</option>
          <option value="ltp" ${s.loss_limit_basis === "ltp" ? "selected" : ""}>LTP</option>
        </select>
      </label>
      <label class="cfg-check"><input type="checkbox" data-k="trail_enabled" ${s.trail_enabled ? "checked" : ""}> Trailing stop</label>
      <label>Trail from ₹ <input type="number" step="250" data-k="trail_activate_at" value="${s.trail_activate_at}"></label>
      <label>Trail by ₹ <input type="number" step="250" data-k="trail_by" value="${s.trail_by}"></label>
      <label class="cfg-check"><input type="checkbox" data-k="lock_profit_enabled" ${s.lock_profit_enabled ? "checked" : ""}> Profit lock</label>
      <label>Lock at ₹ <input type="number" step="250" data-k="lock_profit_trigger" value="${s.lock_profit_trigger}"></label>
      <label>Floor ₹ <input type="number" step="250" data-k="lock_profit_lock_at" value="${s.lock_profit_lock_at}"></label>
      <label class="cfg-check"><input type="checkbox" data-k="auto_entry_enabled" ${s.auto_entry_enabled ? "checked" : ""}> Auto entry</label>
      <label>At <input type="time" data-k="auto_entry_time" value="${esc(s.auto_entry_time)}"></label>
      <label>Expiry
        <select data-k="auto_entry_expiry">
          ${["weekly_current", "weekly_next", "monthly"]
            .map((e) => `<option value="${e}" ${s.auto_entry_expiry === e ? "selected" : ""}>${e}</option>`)
            .join("")}
        </select>
      </label>
      <label>Square off <input type="time" data-k="eod_squareoff_time" value="${esc(s.eod_squareoff_time)}"></label>
      <label>VIX max <input type="number" step="0.5" data-k="vix_max" value="${s.vix_max}"></label>
    </div>
    <div class="row-actions" style="margin-top:10px">
      <button class="btn btn-primary btn-small" data-action="save-cfg" data-sid="${esc(s.id)}">Save settings</button>
      <span class="small muted" data-saved="${esc(s.id)}"></span>
    </div>
  </div>`;
}

function renderLedgerTable(el) {
  const rows = STATE.ledger.closed || [];
  table(
    el,
    [
      { label: "Exit", render: (r) => esc((r.exit_time || "").slice(11, 19)) },
      { label: "Strategy", render: (r) => esc(r.strategy_name || r.strategy_id) },
      { label: "Mode", render: (r) => modePill(r.mode) },
      { label: "Legs", render: (r) => (r.legs || []).map((l) => esc(l.symbol)).join("<br>") },
      { label: "Gross", num: true, cls: (r) => cls(r.gross_pnl), render: (r) => inr(r.gross_pnl, 0) },
      { label: "Charges", num: true, render: (r) => inr(r.total_charges, 0) },
      { label: "Net", num: true, cls: (r) => cls(r.net_pnl), render: (r) => inr(r.net_pnl, 0) },
      { label: "Reason", render: (r) => esc(r.exit_reason) },
    ],
    rows,
    { empty: "No closed trades today." }
  );
}

function renderLedger() {
  const t = STATE.ledger.totals;
  $("#ledger-totals").innerHTML = `
    <span>Trades <b>${t.count}</b></span>
    <span>Gross <b class="${cls(t.gross_pnl)}">${inr(t.gross_pnl)}</b></span>
    <span>Charges <b>${inr(t.total_charges)}</b></span>
    <span>Net <b class="${cls(t.net_pnl)}">${inr(t.net_pnl)}</b></span>`;
  renderLedgerTable($("#ledger-table"));
}

function renderActive() {
  table(
    $("#active-table"),
    [
      { label: "Kind", render: (i) => esc(i.kind) },
      { label: "Name", render: (i) => esc(i.name) },
      { label: "State", render: (i) => esc(i.state) },
      { label: "Mode", render: (i) => modePill(i.mode) },
      { label: "Legs", render: (i) => i.legs.map(esc).join("<br>") || "—" },
      { label: "P&L", num: true, cls: (i) => cls(i.mtm), render: (i) => (i.mtm != null ? inr(i.mtm, 0) : "—") },
    ],
    STATE.active || [],
    { empty: "Nothing armed or live. No order can be placed right now.", rowClass: (i) => (i.concerning ? "danger" : "") }
  );
}

async function renderActivity() {
  const { events } = await api("/api/activity?limit=60");
  table(
    $("#activity-table"),
    [
      { label: "Time", render: (e) => esc((e.ts || "").slice(11, 19)) },
      { label: "Event", render: (e) => esc(e.event) },
      { label: "Detail", render: (e) => esc(e.symbol || e.reason || e.detail || e.sid || "") },
      { label: "Context", render: (e) => esc(e.context || "") },
    ],
    events,
    { empty: "No activity recorded." }
  );
}

async function renderAutoLogin() {
  const al = await api("/api/auto-login");
  const w = al.watchdog || {};

  $("#autologin-status").innerHTML = `
    <span>Scheduler <b class="${al.enabled ? "pos" : ""}">${al.enabled ? "on" : "off"}</b></span>
    <span>Credentials <b class="${al.configured ? "pos" : "neg"}">${al.configured ? "complete" : "missing"}</b></span>
    <span>Last attempt <b>${esc(w.last_result || "never")}</b></span>
    ${w.consecutive_failures ? `<span>Failures <b class="neg">${w.consecutive_failures}</b></span>` : ""}
    <span>Session <b class="${al.broker_authenticated ? "pos" : "neg"}">${al.broker_authenticated ? "logged in" : "logged out"}</b></span>`;

  const help = [];
  if (!al.configured) {
    help.push(`Add to <code>.env</code>: <b>${al.missing.join(", ")}</b>.`);
  }
  help.push(`Redirect URI in use: <code>${esc(al.redirect_uri)}</code> — this exact string must be registered in your Upstox app.`);
  $("#al-help").innerHTML = help.join("<br>");
}

async function renderSettingsExtras() {
  renderAutoLogin().catch((e) => console.error(e));
  const [hc, tg] = await Promise.all([api("/api/health-check"), api("/api/telegram")]);

  $("#health-when").textContent = hc.ran_at ? `last run ${hc.ran_at.replace("T", " ")}` : "not run yet";
  table(
    $("#health-table"),
    [
      {
        label: "",
        render: (c) => ({ ok: "✅", warn: "⚠️", fail: "❌" }[c.level] || "•"),
      },
      { label: "Check", render: (c) => esc(c.name) },
      { label: "Detail", render: (c) => esc(c.detail) },
    ],
    hc.checks || [],
    { empty: "Not run yet — press Run now.", rowClass: (c) => (c.level === "fail" ? "danger" : "") }
  );

  $("#telegram-status").innerHTML = tg.configured
    ? `<span>Status <b class="pos">configured</b></span>
       <span>Chat <b>${esc(tg.chat_id)}</b></span>
       <span>Token <b>${esc(tg.token_hint || "")}</b></span>
       <span>Sent <b>${tg.sent}</b></span>
       ${tg.last_error ? `<span>Last error <b class="neg">${esc(tg.last_error)}</b></span>` : ""}`
    : `<span><b class="neg">Not configured</b> — you will not be alerted on entries, exits or failures.</span>`;
}

function renderSettings() {
  renderSettingsExtras().catch((e) => console.error(e));
  const h = HEALTH || {};
  $("#settings-broker").innerHTML = `
    <div class="totals">
      <span>Authenticated <b class="${h.broker_authenticated ? "pos" : "neg"}">${h.broker_authenticated ? "yes" : "no"}</b></span>
      <span>Auto-login <b>${esc(h.auto_login?.last_result || "—")}</b></span>
    </div>
    ${h.broker_authenticated ? "" : `<a class="btn btn-primary btn-small" href="/api/auth/login">Re-authenticate</a>`}`;

  $("#settings-feed").innerHTML = `
    <div class="totals">
      <span>WebSocket <b class="${h.feed?.connected ? "pos" : "neg"}">${h.feed?.connected ? "connected" : "down"}</b></span>
      <span>Reconnects <b>${h.feed?.reconnect_count ?? "—"}</b></span>
      <span>Subscribed <b>${h.subscribed_key_count ?? "—"}</b></span>
      <span>Stale <b class="${h.stale_key_count ? "neg" : "pos"}">${h.stale_key_count ?? "—"}</b></span>
    </div>`;

  // A single root cause (an expired token, say) produces one error per poll.
  // Collapsing identical consecutive entries keeps the panel readable instead
  // of showing forty copies of the same line.
  const collapsed = [];
  for (const e of h.recent_errors || []) {
    const prev = collapsed[collapsed.length - 1];
    if (prev && prev.where === e.where && prev.reason === e.reason) {
      prev.count++;
      prev.first_ts = e.ts;
    } else {
      collapsed.push({ ...e, count: 1, first_ts: e.ts });
    }
  }

  table(
    $("#errors-table"),
    [
      {
        label: "When",
        render: (e) =>
          new Date(e.ts * 1000).toLocaleTimeString() +
          (e.count > 1 ? `<div class="small muted">×${e.count} since ${new Date(e.first_ts * 1000).toLocaleTimeString()}</div>` : ""),
      },
      { label: "Where", render: (e) => esc(e.where) },
      { label: "Reason", render: (e) => esc(e.reason) },
    ],
    collapsed.slice(0, 20),
    { empty: "No errors logged." }
  );
}

// ---------- live TOTP ----------
// Counts down locally and only refetches when the 30s window rolls, so the
// secret never leaves the server and we aren't hitting the API every second.
const TOTP = { code: null, left: 0, available: true };

async function totpFetch() {
  try {
    const r = await api("/api/auto-login/totp");
    TOTP.code = r.code;
    TOTP.left = r.seconds_left;
    TOTP.available = true;
  } catch (e) {
    TOTP.available = false;
    TOTP.code = null;
  }
  totpPaint();
}

function totpPaint() {
  const targets = [
    { code: $("#totp-code"), bar: $("#totp-bar") },
    { code: $("#banner-totp-code"), bar: $("#banner-totp-bar") },
  ];
  const wrap = $("#banner-totp");
  if (wrap) wrap.hidden = !TOTP.available;

  const hint = $("#totp-hint");
  const panel = $("#totp-panel");
  if (panel) panel.hidden = false;

  for (const t of targets) {
    if (!t.code) continue;
    t.code.textContent = TOTP.available && TOTP.code ? TOTP.code : "------";
    if (t.bar) {
      const pct = Math.max(0, Math.min(100, (TOTP.left / 30) * 100));
      t.bar.style.width = pct + "%";
      t.bar.classList.toggle("urgent", TOTP.left <= 5);
    }
  }

  if (hint) {
    hint.textContent = TOTP.available
      ? `expires in ${TOTP.left}s — refreshes automatically`
      : "no TOTP secret configured (set UPSTOX_TOTP_SECRET in .env)";
  }
}

function totpTick() {
  if (!TOTP.available) return;
  TOTP.left -= 1;
  if (TOTP.left <= 0) {
    totpFetch();   // window rolled, pull the new code
    return;
  }
  totpPaint();
}

async function copyTotp(btn) {
  if (!TOTP.code) return;
  let ok = false;
  try {
    // Only available on HTTPS/localhost; a plain-HTTP deployment lands in
    // the fallback below rather than silently doing nothing.
    await navigator.clipboard.writeText(TOTP.code);
    ok = true;
  } catch (e) {
    try {
      const ta = document.createElement("textarea");
      ta.value = TOTP.code;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand("copy");
      document.body.removeChild(ta);
    } catch (e2) {
      ok = false;
    }
  }
  const label = btn.textContent;
  btn.classList.add("copied");
  btn.textContent = ok ? "copied" : TOTP.code;
  setTimeout(() => {
    btn.classList.remove("copied");
    btn.textContent = TOTP.code || label;
  }, 1000);
  if (!ok) {
    const hint = $("#totp-hint");
    if (hint) hint.textContent = "couldn't copy automatically — select the number and copy manually";
  }
}

// ---------- IPO ----------
async function renderIpo() {
  const [wl, scan, led] = await Promise.all([
    api("/api/ipo/watchlist"),
    api("/api/ipo/scan"),
    api("/api/ipo/ledger?limit=50"),
  ]);

  const s = wl.summary;
  $("#ipo-kpis").innerHTML = [
    kpi("Watching", s.total, `${s.armed} armed`),
    kpi("Near breakout", s.near, "within 1% of listing high", s.near ? "pos" : ""),
    kpi("Triggered", s.triggered, "broke the listing high"),
    kpi("Scan", scan.status.status, scan.status.cached_count != null ? `${scan.status.cached_count} listings cached` : ""),
  ].join("");

  table(
    $("#ipo-table"),
    [
      { label: "Symbol", render: (r) => `<b>${esc(r.symbol)}</b><div class="small muted">${esc(r.name || "")}</div>` },
      { label: "Listed", render: (r) => `${esc(r.listing_date)}<div class="small muted">${r.days_since_listing}d ago</div>` },
      { label: "Listing H", num: true, render: (r) => num(r.listing_high) },
      { label: "Listing L", num: true, render: (r) => num(r.listing_low) },
      { label: "LTP", num: true, render: (r) => (r.ltp != null ? num(r.ltp) : "—") },
      {
        label: "vs High", num: true,
        cls: (r) => cls(r.distance_high_pct),
        render: (r) => (r.distance_high_pct != null ? r.distance_high_pct.toFixed(2) + "%" : "—"),
      },
      { label: "Target", num: true, render: (r) => num(r.target_price) },
      { label: "Stop", num: true, render: (r) => num(r.stop_price) },
      { label: "Buy ₹", num: true, render: (r) => inr(r.buy_amount_inr, 0) },
      { label: "Status", render: (r) => `<span class="pill pill-${r.status === "armed" ? "live" : r.status === "triggered" ? "error" : "idle"}">${esc(r.status)}</span>` },
      {
        label: "",
        render: (r) =>
          `<button class="btn btn-ghost btn-small" data-action="ipo-toggle" data-sym="${esc(r.symbol)}" data-to="${r.status === "armed" ? "disarmed" : "armed"}">${r.status === "armed" ? "Disarm" : "Arm"}</button>
           <button class="btn btn-ghost btn-small" data-action="ipo-remove" data-sym="${esc(r.symbol)}">✕</button>`,
      },
    ],
    wl.entries,
    { empty: "Nothing on the watchlist yet. Add a recently listed symbol above." }
  );

  const st = scan.status;
  $("#ipo-scan-status").textContent =
    st.status === "running"
      ? `scanning ${st.checked}/${st.total} — ${st.current || ""} (${st.found} found)`
      : st.status === "done"
      ? `last scan found ${st.found} recent listings`
      : st.status === "error"
      ? `error: ${st.error}`
      : st.cached_at
      ? `cached ${st.cached_at} · ${st.cached_count} listings`
      : "not run yet";

  table(
    $("#ipo-scan-table"),
    [
      { label: "Symbol", render: (r) => esc(r.symbol) },
      { label: "Name", render: (r) => esc((r.name || "").slice(0, 34)) },
      { label: "Listed", render: (r) => esc(r.listing_date) },
      { label: "Listing H", num: true, render: (r) => num(r.listing_high) },
      { label: "Eligible", render: (r) => (r.eligible ? `<span class="pill pill-live">eligible</span>` : `<span class="pill pill-idle">broke ${esc(r.break_date || "")}</span>`) },
      { label: "", render: (r) => `<button class="btn btn-primary btn-small" data-action="ipo-add-sym" data-sym="${esc(r.symbol)}">Add</button>` },
    ],
    (scan.results || []).slice(0, 60),
    { empty: "No scan results. Run a scan to find recent listings." }
  );

  table(
    $("#ipo-ledger-table"),
    [
      { label: "When", render: (r) => esc((r.ts || "").replace("T", " ").slice(0, 16)) },
      { label: "Symbol", render: (r) => esc(r.symbol) },
      { label: "Break", num: true, render: (r) => num(r.break_price) },
      { label: "vs High", num: true, cls: (r) => cls(r.breakout_pct), render: (r) => r.breakout_pct + "%" },
      { label: "Entry", num: true, render: (r) => num(r.entry_price) },
      { label: "Stop", num: true, render: (r) => num(r.stop_price) },
      { label: "Target", num: true, render: (r) => num(r.target_price) },
      { label: "Qty", num: true, render: (r) => r.qty },
      { label: "Notional", num: true, render: (r) => inr(r.notional, 0) },
    ],
    led.records || [],
    { empty: "No breakouts recorded yet." }
  );
}

function render() {
  if (!STATE) return;
  renderTop();
  if (VIEW === "ipo") renderIpo().catch((e) => console.error(e));
  else if (VIEW === "dashboard") renderDashboard();
  else if (VIEW === "strategies") renderStrategies();
  else if (VIEW === "ledger") renderLedger();
  else if (VIEW === "active") renderActive();
  else if (VIEW === "activity") renderActivity().catch(() => {});
  else if (VIEW === "settings") renderSettings();
}

async function refresh() {
  try {
    const [state, health] = await Promise.all([api("/api/state"), api("/api/health")]);
    STATE = state;
    HEALTH = health;
    render();
  } catch (e) {
    $("#feed-badge").textContent = "dashboard: fetch failed";
    $("#feed-badge").className = "badge bad";
    console.error(e);
  }
}

// State arrives over SSE; /api/health is polled separately since it's cheap
// and changes slowly. If the stream can't be established we fall back to
// polling state too, so the dashboard still works behind a proxy that
// buffers event streams.
let sse = null;
let sseAlive = false;

function connectStream() {
  try {
    sse = new EventSource("/api/stream");
  } catch (e) {
    return;
  }
  sse.onmessage = (ev) => {
    try {
      const payload = JSON.parse(ev.data);
      if (payload.error) return;
      sseAlive = true;
      STATE = payload;
      render();
    } catch (e) {
      console.error("bad stream frame", e);
    }
  };
  sse.onerror = () => {
    sseAlive = false;
    // EventSource reconnects on its own; the poll fallback covers the gap.
  };
}

async function pollHealth() {
  try {
    HEALTH = await api("/api/health");
    if (!sseAlive) STATE = await api("/api/state");
    render();
  } catch (e) {
    console.error(e);
  }
}

// ---------- actions ----------
document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-action], [data-view]");
  if (!btn) return;

  if (btn.dataset.view) return setView(btn.dataset.view);
  const a = btn.dataset.action;
  const sid = btn.dataset.sid;

  try {
    if (a === "refresh") await refresh();
    else if (a === "theme") {
      const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("theme", next); } catch {}
    } else if (a === "toggle-kill" || a === "release-kill") {
      const armed = !!STATE?.kill_switch?.enabled;
      if (!armed && !confirm("Arm the kill switch? All new orders will be refused.")) return;
      await api("/api/kill-switch", { method: "POST", body: JSON.stringify({ enabled: !armed }) });
      await refresh();
    } else if (a === "enter") {
      const s = STATE.strategies.find((x) => x.id === sid);
      if (s.mode === "live" && !confirm(`${s.name} is in LIVE mode. Place REAL orders now?`)) return;
      btn.disabled = true;
      await api(`/api/strategies/${sid}/enter`, { method: "POST" });
      await refresh();
    } else if (a === "exit") {
      if (!confirm("Square off this position now?")) return;
      btn.disabled = true;
      await api(`/api/strategies/${sid}/exit`, { method: "POST" });
      await refresh();
    } else if (a === "enter-all") {
      if (!confirm("Enter every enabled strategy now?")) return;
      await api("/api/enter-all", { method: "POST" });
      await refresh();
    } else if (a === "exit-all") {
      if (!confirm("Square off ALL open positions now?")) return;
      await api("/api/exit-all", { method: "POST" });
      await refresh();
    } else if (a === "ipo-add" || a === "ipo-add-sym") {
      const symbol = a === "ipo-add-sym" ? btn.dataset.sym : $("#ipo-symbol").value.trim();
      if (!symbol) return alert("Enter an NSE symbol");
      const body = {
        symbol,
        buy_amount_inr: Number($("#ipo-amount").value || 200000),
        force: a === "ipo-add-sym" ? false : $("#ipo-force").checked,
      };
      btn.disabled = true;
      await api("/api/ipo/watchlist", { method: "POST", body: JSON.stringify(body) });
      $("#ipo-symbol").value = "";
      await renderIpo();
    } else if (a === "ipo-remove") {
      if (!confirm(`Remove ${btn.dataset.sym} from the watchlist?`)) return;
      await api(`/api/ipo/watchlist/${btn.dataset.sym}`, { method: "DELETE" });
      await renderIpo();
    } else if (a === "ipo-toggle") {
      await api(`/api/ipo/watchlist/${btn.dataset.sym}/status`, {
        method: "POST",
        body: JSON.stringify({ status: btn.dataset.to }),
      });
      await renderIpo();
    } else if (a === "ipo-scan") {
      if (!confirm("Scan the full NSE equity list for recent listings? This takes several minutes.")) return;
      await api("/api/ipo/scan", { method: "POST", body: JSON.stringify({ max_days_since_listing: 400 }) });
      await renderIpo();
    } else if (a === "ipo-scan-cancel") {
      await api("/api/ipo/scan/cancel", { method: "POST" });
      await renderIpo();
    } else if (a === "copy-totp") {
      await copyTotp(btn);
    } else if (a === "al-test") {
      if (!confirm("Run the automated login now?\n\nUpstox locks the login after a few bad TOTP codes, so only do this if the code above matched your authenticator.")) return;
      btn.disabled = true;
      $("#al-msg").textContent = "logging in…";
      try {
        await api("/api/auto-login/test", { method: "POST" });
        $("#al-msg").textContent = "✅ logged in successfully";
      } catch (e) {
        $("#al-msg").textContent = "❌ " + e.message;
      }
      await renderAutoLogin();
    } else if (a === "al-toggle") {
      const cur = await api("/api/auto-login");
      await api("/api/auto-login/enable", { method: "POST", body: JSON.stringify({ enabled: !cur.enabled }) });
      $("#al-msg").textContent = "changed for this run — set AUTO_LOGIN_ENABLED in .env to persist";
      await renderAutoLogin();
    } else if (a === "run-health") {
      btn.disabled = true;
      const announce = $("#health-announce").checked;
      await api(`/api/health-check?announce=${announce}`, { method: "POST" });
      await renderSettingsExtras();
    } else if (a === "tg-save") {
      await api("/api/telegram", {
        method: "POST",
        body: JSON.stringify({ token: $("#tg-token").value || null, chat_id: $("#tg-chat").value || null }),
      });
      $("#tg-token").value = "";
      await renderSettingsExtras();
    } else if (a === "tg-test") {
      await api("/api/telegram/test", { method: "POST" });
      alert("Test message sent — check Telegram.");
      await renderSettingsExtras();
    } else if (a === "tg-clear") {
      if (!confirm("Clear Telegram credentials? Alerts will stop.")) return;
      await api("/api/telegram", { method: "POST", body: JSON.stringify({ clear: true }) });
      await renderSettingsExtras();
    } else if (a === "save-cfg") {
      const grid = document.querySelector(`[data-cfg="${sid}"]`);
      const patch = {};
      grid.querySelectorAll("[data-k]").forEach((el) => {
        patch[el.dataset.k] = el.type === "checkbox" ? el.checked : el.value;
      });
      if (patch.mode === "live" && !confirm("Switch this strategy to LIVE (real orders)?")) return;
      await api(`/api/strategies/${sid}/config`, { method: "POST", body: JSON.stringify(patch) });
      const note = document.querySelector(`[data-saved="${sid}"]`);
      if (note) { note.textContent = "Saved"; setTimeout(() => (note.textContent = ""), 2000); }
      await refresh();
    }
  } catch (e) {
    alert(e.message);
  } finally {
    btn.disabled = false;
  }
});

// Don't let a poll overwrite a field mid-edit.
let editing = false;
document.addEventListener("focusin", (e) => { if (e.target.closest(".cfg-grid")) editing = true; });
document.addEventListener("focusout", () => setTimeout(() => (editing = false), 150));

const origRenderStrategies = renderStrategies;
renderStrategies = function () { if (!editing) origRenderStrategies(); };

window.addEventListener("hashchange", () => setView(location.hash.replace("#", "") || "dashboard"));

setView(VIEW);
refresh().then(connectStream);
setInterval(pollHealth, 5000);

// live TOTP: fetch once, then count down locally
totpFetch();
setInterval(totpTick, 1000);
