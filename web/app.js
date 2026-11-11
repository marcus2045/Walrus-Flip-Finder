// Walrus Flip Finder - front end
// Python fetches prices, checks alerts and saves price history in the background (app.py).
// This file asks Python for the latest copy, then filters, sorts and draws it.

const POLL_MS = 10 * 1000;   // ask Python for its latest prices (no download - cheap)
const MAX_ROWS = 300;        // drawing thousands of rows makes the page laggy

const state = {
  rows: [],
  byId: {},
  watch: {},          // item id -> { target, direction, triggered }
  sortKey: "profit",
  sortDir: -1,        // -1 = biggest first, 1 = smallest first
  rev: null,          // Python bumps this whenever prices or the tax change
  seenAlerts: 0,      // newest alert time we've already popped up
};

const api = () => window.pywebview.api;
const $ = (id) => document.getElementById(id);

// 1234567 -> "1.23M", 4500 -> "4.5k"
function fmt(n) {
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (abs >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return n.toFixed(1);
}

// "1.5m" -> 1500000, "250k" -> 250000, "1,200" -> 1200. Returns null if it's not a number.
function parseAmount(text) {
  const m = text.trim().toLowerCase().replace(/,/g, "").match(/^(\d*\.?\d+)\s*([kmb]?)$/);
  if (!m) return null;
  const mult = { "": 1, k: 1e3, m: 1e6, b: 1e9 }[m[2]];
  return Number(m[1]) * mult;
}

// textContent (not innerHTML) means item names can never inject HTML.
function cell(text, className = "") {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

// Item name cell you can click to open its price chart.
function nameCell(id, name) {
  const td = cell(name, "link");
  td.title = "Show price chart";
  td.onclick = () => openChart(id, name);
  return td;
}

// ---------- getting data from Python ----------

function applyData(res) {
  $("status").classList.toggle("error", !!res.error);
  if (res.error) {
    $("status").textContent = "Couldn't reach Hypixel: " + res.error;
  } else if (res.updated) {
    $("status").textContent = "Updated " + new Date(res.updated).toLocaleTimeString();
  }
  $("toastWarn").classList.toggle("hidden", res.toasts);
  $("taxText").textContent = res.tax + "%";
  showNewAlerts(res.alerts);

  // Only redraw if the rows actually changed (stops the table flickering every poll).
  // rev changes on every new download AND when you change the tax.
  if (res.rev === state.rev) return;
  state.rev = res.rev;
  state.rows = res.rows;
  state.byId = Object.fromEntries(res.rows.map((r) => [r.id, r]));
  render();
  if (chartState.id) loadChart();   // keep an open chart up to date
}

async function poll() {
  applyData(await api().get_flips());
  // Triggered/armed status can change in Python, so refresh it too.
  state.watch = await api().get_watchlist();
  renderWatch();
}

async function refreshNow() {
  $("status").textContent = "Updating…";
  applyData(await api().refresh_now());
}

// Small pop-up in the corner of the app for each new alert.
function showNewAlerts(alerts) {
  for (const a of alerts) {
    if (a.time <= state.seenAlerts) continue;
    state.seenAlerts = a.time;
    const div = document.createElement("div");
    div.className = "popup";
    div.textContent = "🔔 " + a.text;
    div.onclick = () => div.remove();
    $("popups").appendChild(div);
    setTimeout(() => div.remove(), 15000);
  }
}

// ---------- All flips tab ----------

function filteredRows() {
  const search = $("search").value.trim().toLowerCase();
  const minVol = Number($("minVol").value);
  const budget = Number($("budget").value) || Infinity;   // empty box = no limit
  return state.rows.filter((r) =>
    r.weekly >= minVol &&
    r.buy <= budget &&
    (!search || r.name.toLowerCase().includes(search))
  );
}

function render() {
  const rows = filteredRows();
  rows.sort((a, b) => {
    const x = a[state.sortKey], y = b[state.sortKey];
    return (typeof x === "string" ? x.localeCompare(y) : x - y) * state.sortDir;
  });

  renderBest(rows);

  const body = $("rows");
  body.replaceChildren();
  for (const r of rows.slice(0, MAX_ROWS)) {
    const tr = document.createElement("tr");
    const star = document.createElement("button");
    star.className = "star" + (r.id in state.watch ? " on" : "");
    star.textContent = "★";
    star.title = r.id in state.watch ? "Remove from watchlist" : "Add to watchlist";
    star.onclick = () => toggleWatch(r.id);
    const starCell = document.createElement("td");
    starCell.appendChild(star);

    tr.append(
      starCell,
      nameCell(r.id, r.name),
      cell(fmt(r.buy), "num"),
      cell(fmt(r.sell), "num"),
      cell(fmt(r.profit), "num " + (r.profit >= 0 ? "pos" : "neg")),
      cell(r.margin.toFixed(1) + "%", "num " + (r.margin >= 0 ? "pos" : "neg")),
      cell(fmt(r.weekly), "num"),
    );
    body.appendChild(tr);
  }

  $("empty").classList.toggle("hidden", rows.length > 0);
  $("count").textContent = rows.length > MAX_ROWS
    ? `Showing top ${MAX_ROWS} of ${rows.length} items`
    : `${rows.length} items`;

  document.querySelectorAll("th[data-key]").forEach((th) => {
    const on = th.dataset.key === state.sortKey;
    th.classList.toggle("sorted", on);
    th.classList.toggle("asc", on && state.sortDir === 1);
    th.classList.toggle("desc", on && state.sortDir === -1);
  });

  renderWatch();
}

// "Best flip" = highest total profit per week if you only got 1% of the volume.
function renderBest(rows) {
  let best = null, bestScore = 0;
  for (const r of rows) {
    const score = r.profit * r.weekly * 0.01;
    if (score > bestScore) { best = r; bestScore = score; }
  }
  $("best").classList.toggle("hidden", !best);
  if (!best) return;
  $("bestName").textContent = best.name;
  $("bestInfo").textContent =
    `Buy ${fmt(best.buy)} → sell ${fmt(best.sell)} · ${fmt(best.profit)} profit each · ` +
    `${fmt(best.weekly)} traded a week`;
}

// ---------- Watchlist tab ----------

function renderWatch() {
  const ids = Object.keys(state.watch);
  $("watchCount").textContent = ids.length;
  $("watchEmpty").classList.toggle("hidden", ids.length > 0);

  // Don't rebuild the table while you're typing a target - it would wipe the box.
  if ($("watchRows").contains(document.activeElement)) return;

  const body = $("watchRows");
  body.replaceChildren();
  ids.sort((a, b) => (state.byId[a]?.name || a).localeCompare(state.byId[b]?.name || b));

  for (const id of ids) {
    const w = state.watch[id];
    const r = state.byId[id];
    const tr = document.createElement("tr");

    // Alert controls: [above/below] [target box] [Set]
    const dir = document.createElement("select");
    dir.innerHTML = '<option value="above">above</option><option value="below">below</option>';
    dir.value = w.direction;
    const target = document.createElement("input");
    target.placeholder = "e.g. 1.5m";
    target.className = "target";
    target.value = w.target ? fmt(w.target) : "";
    const set = document.createElement("button");
    set.textContent = "Set";
    const save = () => saveAlert(id, target, dir);
    set.onclick = save;
    target.onkeydown = (e) => { if (e.key === "Enter") save(); };
    const controls = document.createElement("div");   // flex goes on a div, not the <td>,
    controls.className = "alert-cell";                  // or the table lines break
    controls.append(dir, target, set);
    const alertCell = document.createElement("td");
    alertCell.appendChild(controls);

    let status = "No alert", statusClass = "muted";
    if (w.target && w.triggered) { status = "🔔 Hit!"; statusClass = "pos"; }
    else if (w.target) { status = "Waiting…"; statusClass = ""; }

    const remove = document.createElement("button");
    remove.className = "remove";
    remove.textContent = "✕";
    remove.title = "Remove from watchlist";
    remove.onclick = () => toggleWatch(id);
    const removeCell = document.createElement("td");
    removeCell.appendChild(remove);

    tr.append(
      nameCell(id, r ? r.name : id),
      cell(r ? fmt(r.sell) : "—", "num"),
      cell(r ? fmt(r.profit) : "—", "num " + (r && r.profit >= 0 ? "pos" : "neg")),
      alertCell,
      cell(status, statusClass),
      removeCell,
    );
    body.appendChild(tr);
  }
}

async function saveAlert(id, input, dir) {
  const text = input.value.trim();
  const amount = text ? parseAmount(text) : null;
  if (text && amount === null) {
    input.classList.add("bad");   // red box: not a number we understand
    return;
  }
  input.classList.remove("bad");
  await api().set_alert(id, amount, dir.value);
  input.blur();   // stop "typing" so the table can update
  state.watch = await api().get_watchlist();
  renderWatch();
}

async function toggleWatch(id) {
  await api().toggle_watch(id);
  state.watch = await api().get_watchlist();
  render();
}

// ---------- price chart ----------

let chart = null;                            // the Chart.js object, so we can destroy it before redrawing
const chartState = { id: null, name: "", hours: 24 };

function openChart(id, name) {
  chartState.id = id;
  chartState.name = name;
  $("chartName").textContent = name;
  $("chartModal").classList.remove("hidden");
  loadChart();
}

function closeChart() {
  chartState.id = null;
  $("chartModal").classList.add("hidden");
  if (chart) { chart.destroy(); chart = null; }
}

function chartMessage(text) {
  $("chartMsg").textContent = text;
  $("chartMsg").classList.toggle("hidden", !text);
}

// Short time labels for the x axis: "14:00" for 24h, "Tue 14:00" for 7 days.
function timeLabel(ms, hours) {
  const d = new Date(ms);
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return hours > 24 ? d.toLocaleDateString([], { weekday: "short" }) + " " + hm : hm;
}

async function loadChart() {
  const { id, hours } = chartState;
  document.querySelectorAll(".range").forEach((b) =>
    b.classList.toggle("active", Number(b.dataset.hours) === hours));

  if (typeof Chart === "undefined") {
    $("chartStats").replaceChildren();
    chartMessage("The chart library couldn't load - check your internet connection and restart the app.");
    return;
  }

  const res = await api().get_history(id, hours);
  if (id !== chartState.id || hours !== chartState.hours) return;   // you clicked something else meanwhile
  if (res.error) {
    chartMessage("Couldn't read price history: " + res.error);
    return;
  }

  const pts = res.points;   // [[time_ms, buy, sell], ...]
  renderChartStats(pts);
  chartMessage(pts.length < 2
    ? "Not enough history yet. Prices are saved every 5 minutes while the app runs - check back soon."
    : "");

  // Read the theme colours from style.css so the chart matches the app.
  const css = getComputedStyle(document.documentElement);
  const color = (name) => css.getPropertyValue(name).trim();
  const now = Date.now();

  if (chart) chart.destroy();   // Chart.js can't draw a new chart on a canvas that's still in use
  chart = new Chart($("chartCanvas"), {
    type: "line",
    data: {
      datasets: [
        { label: "Sell offer", data: pts.map((p) => ({ x: p[0], y: p[2] })), borderColor: color("--gold") },
        { label: "Buy order", data: pts.map((p) => ({ x: p[0], y: p[1] })), borderColor: color("--accent") },
      ],
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,          // fill .chart-wrap instead of keeping a fixed shape
      interaction: { mode: "index", intersect: false },   // hover anywhere to see both prices
      elements: { point: { radius: 0, hitRadius: 6 }, line: { borderWidth: 2, tension: 0.2 } },
      scales: {
        x: {
          // A plain number axis (milliseconds), so no extra date library is needed.
          // min/max = the whole range, so you can see history building up.
          type: "linear",
          min: now - hours * 3600e3,
          max: now,
          ticks: {
            stepSize: hours > 24 ? 24 * 3600e3 : 3 * 3600e3,
            callback: (v) => timeLabel(v, hours),
            color: color("--muted"),
          },
          grid: { color: color("--line") },
        },
        y: {
          ticks: { callback: (v) => fmt(v), color: color("--muted") },
          grid: { color: color("--line") },
        },
      },
      plugins: {
        legend: { labels: { color: color("--text"), boxHeight: 2 } },
        tooltip: {
          callbacks: {
            title: (items) => new Date(items[0].parsed.x).toLocaleString([], {
              weekday: "short", hour: "2-digit", minute: "2-digit" }),
            label: (ctx) => `${ctx.dataset.label}: ${fmt(ctx.parsed.y)}`,
          },
        },
      },
    },
  });
}

// "Now / Low / High / Change" line above the chart (all for the sell offer price).
function renderChartStats(pts) {
  const box = $("chartStats");
  box.replaceChildren();
  if (pts.length < 2) return;
  const sells = pts.map((p) => p[2]);
  const first = sells[0], last = sells[sells.length - 1];
  const change = (last - first) / first * 100;
  const stats = [
    ["Sell now", fmt(last), ""],
    ["Low", fmt(Math.min(...sells)), ""],
    ["High", fmt(Math.max(...sells)), ""],
    ["Change", (change >= 0 ? "+" : "") + change.toFixed(1) + "%", change >= 0 ? "pos" : "neg"],
  ];
  for (const [label, value, cls] of stats) {
    const span = document.createElement("span");
    const b = document.createElement("b");
    b.textContent = value;
    if (cls) b.className = cls;
    span.append(label + " ", b);
    box.appendChild(span);
  }
}

document.querySelectorAll(".range").forEach((b) => b.addEventListener("click", () => {
  chartState.hours = Number(b.dataset.hours);
  loadChart();
}));
$("chartClose").addEventListener("click", closeChart);
// Clicking the dark background (not the box itself) closes it, and so does Esc.
$("chartModal").addEventListener("click", (e) => { if (e.target === $("chartModal")) closeChart(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && chartState.id) closeChart(); });

// ---------- Settings tab ----------

async function loadSettings() {
  const s = await api().get_settings();
  $("taxInput").value = s.tax;
  $("defaultTax").textContent = s.default_tax;
  $("trayToggle").checked = s.close_to_tray && s.tray_available;
  $("trayToggle").disabled = !s.tray_available;
  $("trayWarn").classList.toggle("hidden", s.tray_available);
  $("historyInfo").textContent = s.history_ok
    ? `Prices for every item are saved every ${s.history_every_min} minutes and kept for ` +
      `${s.keep_days} days. The history file is ${s.history_mb} MB.`
    : "Price history is off - the database couldn't be opened.";
}

async function saveSettings() {
  const tax = Number($("taxInput").value);
  const res = await api().set_settings(tax, $("trayToggle").checked);
  $("settingsMsg").classList.toggle("neg", !res.ok);
  $("settingsMsg").textContent = res.ok ? "Saved ✓" : res.error;
  if (res.ok) applyData(await api().get_flips());   // profits change straight away
  setTimeout(() => { $("settingsMsg").textContent = ""; }, 3000);
}

$("saveSettings").addEventListener("click", saveSettings);
$("taxInput").addEventListener("keydown", (e) => { if (e.key === "Enter") saveSettings(); });
$("trayToggle").addEventListener("change", saveSettings);

// ---------- buttons, tabs, sorting ----------

document.querySelectorAll("th[data-key]").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.key;
    if (state.sortKey === key) {
      state.sortDir *= -1;
    } else {
      state.sortKey = key;
      state.sortDir = key === "name" ? 1 : -1;   // names A-Z, numbers biggest first
    }
    render();
  });
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    // Each tab button's data-tab matches a page with id "tab-<name>".
    document.querySelectorAll(".tab-page").forEach((page) =>
      page.classList.toggle("hidden", page.id !== "tab-" + tab.dataset.tab));
    if (tab.dataset.tab === "settings") loadSettings();
  });
});

["search", "minVol", "budget"].forEach((id) => $(id).addEventListener("input", render));
$("refresh").addEventListener("click", refreshNow);
$("testToast").addEventListener("click", async () => {
  const ok = await api().test_notification();
  if (!ok) $("toastWarn").classList.remove("hidden");
});

// pywebview fires this once the Python Api is ready to be called.
window.addEventListener("pywebviewready", async () => {
  state.seenAlerts = Date.now();   // don't pop up old alerts from before the app opened
  state.watch = await api().get_watchlist();
  await refreshNow();
  setInterval(poll, POLL_MS);
});
