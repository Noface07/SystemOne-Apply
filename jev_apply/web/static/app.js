// jev-apply UI: a small no-build single-page app over the local JSON API (jev_apply/web/server.py).
// Every value from job pages or files is escaped by `html` before it reaches the page.

const TOKEN = document.querySelector('meta[name="jev-token"]').content;
const main = document.getElementById("main");
const state = { system: null, tracks: [], timers: [], streams: [], search: null, selected: new Map(), filter: "fit", sort: "newest", submit: true };

// ---- helpers --------------------------------------------------------------------------------------------------

// Each page visit gets its own AbortController. A page's reads still in flight when you move on are cancelled and
// never resolve, so a slow page can't draw itself over the one you went to. Writes are never cancelled.
let nav = new AbortController();
const NEVER = new Promise(() => {});

async function api(path, { method = "GET", body } = {}) {
  const signal = method === "GET" ? nav.signal : undefined;
  let res;
  try {
    res = await fetch(`/api${path}`, {
      method,
      signal,
      headers: { "X-Jev-Token": TOKEN, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    if (signal?.aborted) return NEVER;
    throw e;
  }
  if (signal?.aborted) return NEVER;
  if (res.status === 401 && !sessionStorage.getItem("jev-reloaded")) {
    sessionStorage.setItem("jev-reloaded", "1"); // the app restarted with a new session token: pick it up
    location.reload();
    return new Promise(() => {});
  }
  if (res.ok) sessionStorage.removeItem("jev-reloaded");
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `${res.status} ${res.statusText}`);
  }
  const data = await res.json();
  return signal?.aborted ? NEVER : data;
}

// Polled views redraw only when what they show has changed: an identical redraw every second resets hover,
// focus, text selection and open <details>, which is what made the live pages flicker.
function changed(slot, value) {
  const now = JSON.stringify(value);
  if (state.drawn?.[slot] === now) return false;
  (state.drawn ||= {})[slot] = now;
  return true;
}

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const raw = (s) => ({ __html: s });
function out(v) {
  if (v == null || v === false) return "";
  if (Array.isArray(v)) return v.map(out).join("");
  if (typeof v === "object" && "__html" in v) return v.__html;
  return esc(v);
}
function html(strings, ...vals) {
  return raw(strings.reduce((acc, s, i) => acc + s + (i < vals.length ? out(vals[i]) : ""), ""));
}
const mount = (el, view) => {
  el.innerHTML = view.__html;
  if (el === main) {
    main.classList.remove("leaving");
    if (view !== LOADER) main.__nav = nav; // this visit's page has drawn: no loader needed
  }
  return el;
};
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "#");

function every(ms, fn) { fn(); const id = setInterval(fn, ms); state.timers.push(id); return id; }
function clearTimers() {
  state.timers.forEach(clearInterval); state.timers = [];
  state.streams.forEach((es) => es.close()); state.streams = [];
}

function toast(message, color = "var(--accent)") {
  const el = document.createElement("div");
  el.style.setProperty("--c", color);
  el.textContent = message;
  document.getElementById("toast").append(el);
  setTimeout(() => el.remove(), 4200);
}

const STAGE_NAMES = { draft: "Drafting answers", learn: "Reading résumés", judge: "Benchmark judge" };
const MODEL_NAMES = { clef: "Clef-Flash", laya: "Laya", typesafe: "Jev", llm: "the chat model" };
const TRACK_NAMES = { dotnet: ".NET", genai: "GenAI", automation: "Automation" };
const trackName = (id) => { const n = String(id || "").split("/").pop(); return TRACK_NAMES[n] || (n ? n[0].toUpperCase() + n.slice(1) : "—"); };
const trackKey = (id) => String(id || "").split("/").pop();
const trackChip = (id) => (id ? html`<span class="chip t-${trackKey(id)}"><i></i>${trackName(id)}</span>` : html`<span class="chip plain">—</span>`);

const STATUS = {
  submitted: "Submitted", applied: "Applied", review: "Left for you", ready_to_check: "Ready to check",
  ready_to_submit: "Submit yourself", stopped: "Stopped", failed: "Failed", deferred: "Deferred",
  running: "Running", waiting: "Waiting", not_run: "Not run", needs: "Needs you", closed: "Closed",
  planning: "Planning", starting: "Starting", done: "Done",
};
function statusKey(s) {
  const t = String(s || "").toLowerCase();
  if (t.startsWith("submitted") || t.startsWith("applied")) return "submitted";
  if (t.startsWith("ready to submit")) return "ready_to_submit";
  return t.replace(/[^a-z]+/g, "_").replace(/^_|_$/g, "") || "waiting";
}
const statusChip = (s, live = false) => {
  const k = statusKey(s);
  return html`<span class="chip s-${k} ${live ? "live" : ""}"><i></i>${STATUS[k] || s}</span>`;
};

function ago(iso) {
  if (!iso) return "";
  const t = new Date(iso.length > 10 ? iso : iso + "T00:00:00").getTime();
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)}d ago`;
  return new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
const icon = {
  spark: raw('<svg viewBox="0 0 24 24"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6"/></svg>'),
  send: raw('<svg viewBox="0 0 24 24"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4z"/></svg>'),
  chat: raw('<svg viewBox="0 0 24 24"><path d="M4 5h16v11H8l-4 4z"/></svg>'),
  cpu: raw('<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/></svg>'),
  link: raw('<svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5"/></svg>'),
  play: raw('<svg viewBox="0 0 24 24"><path d="M6 4l14 8-14 8z"/></svg>'),
  stop: raw('<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>'),
  search: raw('<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-5-5"/></svg>'),
  inbox: raw('<svg viewBox="0 0 24 24"><path d="M3 13h5l2 3h4l2-3h5M5 5h14l2 8v6H3v-6z"/></svg>'),
  check: raw('<svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-11"/></svg>'),
  x: raw('<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>'),
};

function modal(view, wire) {
  const el = document.getElementById("modal");
  mount(el, html`<div role="dialog" aria-modal="true">${view}</div>`);
  el.hidden = false;
  const close = () => { el.hidden = true; el.innerHTML = ""; };
  el.onclick = (e) => { if (e.target === el || e.target.closest("[data-close]")) close(); };
  wire?.(el, close);
  return close;
}

// ---- shell ----------------------------------------------------------------------------------------------------

async function refreshShell() {
  try {
    const [system, ov] = await Promise.all([api("/system"), api("/overview")]);
    state.system = system;
    const q = $("#nav-questions");
    q.hidden = !ov.open_questions;
    q.textContent = ov.open_questions;
    $("#nav-live").hidden = !ov.active_batch;
    state.waiting = ov.waiting_for_answers || [];
    state.rerunReady = ov.rerun_ready || 0;
    q.classList.toggle("pulse", state.waiting.length > 0);
    noticeWaiting(state.waiting);
    api("/queue").then((qd) => { const n = $("#nav-inbox"); n.hidden = !qd.items.length; n.textContent = qd.items.length; }).catch(() => {});
    state.activeBatch = ov.active_batch;
    const clef = system.clef;
    const model = system.backend === "clef" ? "Clef-Flash 9B" : system.backend === "laya" ? "Laya 441M" : system.backend;
    const running = clef ? clef.running : !system.problem;
    mount($("#model-badge"), html`
      <a class="card" href="#/settings" title="Switch decision model" style="padding:12px;display:block;text-decoration:none">
        <div class="muted" style="font-size:11.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase">Decision model</div>
        <div style="display:flex;align-items:center;gap:8px;margin-top:6px;font-weight:600">${icon.cpu}${model}</div>
        <div style="margin-top:8px">${system.problem ? html`<span class="chip s-stopped"><i></i>Needs setup</span>`
          : clef ? html`<span class="chip ${running ? "s-submitted live" : "s-deferred"}"><i></i>${running ? "Loaded" : "Starts with a batch"}</span>`
          : html`<span class="chip s-submitted"><i></i>Ready</span>`}</div>
      </a>`);
  } catch (e) { /* the page still works; the badge waits for the next refresh */ }
}

// A run waiting for your answers: say so once per question set, here and as a desktop notification.
function noticeWaiting(waiting) {
  const key = waiting.map((w) => `${w.batch}:${w.n}:${w.questions.join("|")}`).join(";");
  if (!key || key === state.waitingKey) { state.waitingKey = key || state.waitingKey; return; }
  state.waitingKey = key;
  const n = waiting.reduce((a, w) => a + w.questions.length, 0);
  const text = `A run is waiting for your answer${n > 1 ? `s to ${n} questions` : ""}: ${waiting[0].questions[0]}`;
  toast(`${text} (Questions)`, "var(--amber)");
  try {
    if (window.Notification && Notification.permission === "granted") {
      const note = new Notification("jev-apply needs an answer", { body: text, tag: "jev-waiting" });
      note.onclick = () => { window.focus(); location.hash = "#/questions"; };
    }
  } catch { /* notifications unavailable */ }
  if (onPage("questions") && !document.activeElement?.matches?.("input, textarea")) questions();
}

function askNotifications() {
  try { if (window.Notification && Notification.permission === "default") Notification.requestPermission(); } catch { /* no notifications */ }
}

function rerunBanner(jobs) {
  if (!jobs?.length) return "";
  return html`<div class="banner ok rerun-banner"><span>${icon.play}</span>
    <span><b>${jobs.length} job${jobs.length > 1 ? "s" : ""} stopped on questions you've now answered.</b> <span class="muted">Run ${jobs.length > 1 ? "them" : "it"} again and your answers fill in.</span></span>
    <button class="btn sm primary" id="rerun-ready" style="margin-left:auto">${icon.play}Re-run ${jobs.length > 1 ? "them" : "it"}</button></div>`;
}

function wireRerun(jobs) {
  const b = $("#rerun-ready");
  if (b) b.onclick = () => confirmBatch(jobs.map((j) => j.url), `Re-run ${jobs.length} job${jobs.length > 1 ? "s" : ""}?`);
}

const pages = { "": overview, find, batch: batchPage, inbox: inboxPage, questions, profile: profilePage, history, insights: insightsPage, settings };

async function route() {
  clearTimers();
  main.onclick = null; // page-level click handlers (the batch page) belong to the page you left
  nav.abort();
  const mine = (nav = new AbortController());
  state.drawn = {};
  const [name, arg] = location.hash.replace(/^#\/?/, "").split("/");
  $$("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.page === (name || "overview")));
  const page = pages[name] || overview;
  main.scrollTo?.(0, 0);
  // The page you left fades at once (and takes no clicks); if the new one hasn't drawn within 150 ms, a loader
  // takes its place, so a slow page never leaves the previous one standing.
  main.classList.add("leaving");
  const slow = setTimeout(() => { if (mine === nav && main.__nav !== mine) mount(main, LOADER); }, 150);
  try { await page(arg); } catch (e) {
    if (mine === nav) mount(main, html`<div class="card empty">${icon.x}Couldn't load this page: ${e.message}</div>`);
  } finally {
    if (mine === nav) { clearTimeout(slow); main.classList.remove("leaving"); } // e.g. "stay on this page" in Profile
  }
}
const LOADER = html`<div class="page-loading" role="status" aria-live="polite"><span class="spinner"></span>Loading…</div>
  <div class="grid g4" style="margin-top:16px">${[1, 2, 3, 4].map(() => html`<div class="card kpi"><div class="sk" style="width:55%"></div><div class="sk" style="height:28px;margin-top:12px;width:35%"></div></div>`)}</div>
  <div class="card" style="margin-top:16px">${[1, 2, 3].map(() => html`<div class="sk" style="height:18px;margin:10px 0"></div>`)}</div>`;
const onPage = (prefix) => location.hash.replace(/^#\/?/, "").startsWith(prefix);

// ---- overview -------------------------------------------------------------------------------------------------

async function overview() {
  mount(main, html`<div class="grid g4">${[1, 2, 3, 4].map(() => html`<div class="card kpi"><div class="sk" style="width:50%"></div><div class="sk" style="height:30px;margin-top:12px;width:40%"></div></div>`)}</div>`);
  const [ov, system] = await Promise.all([api("/overview"), api("/system")]);
  const hour = new Date().getHours();
  const hello = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const days = [...Array(14)].map((_, i) => {
    const d = new Date(); d.setDate(d.getDate() - 13 + i);
    const key = d.toISOString().slice(0, 10);
    return { key, n: ov.by_day[key] || 0, label: d.toLocaleDateString(undefined, { day: "numeric" }) };
  });
  const week = days.slice(-7).reduce((a, d) => a + d.n, 0);
  const max = Math.max(1, ...days.map((d) => d.n));
  const tracks = Object.entries(ov.by_track).sort((a, b) => b[1] - a[1]);
  const total = Math.max(1, tracks.reduce((a, [, n]) => a + n, 0));
  const clef = system.clef;
  mount(main, html`
    <div class="page-head">
      <div><h1>${hello}${system.name ? `, ${system.name}` : ""}</h1>
        <p class="sub">${ov.applications} applications sent so far · ${week} in the last 7 days</p></div>
      <div style="display:flex;gap:10px">
        <a class="btn" href="#/questions">${icon.inbox}Questions${ov.open_questions ? ` (${ov.open_questions})` : ""}</a>
        <a class="btn primary" href="#/find">${icon.search}Find new jobs</a>
      </div>
    </div>
    ${ov.active_batch ? html`<a class="banner" href="#/batch/${ov.active_batch}" style="text-decoration:none">
      <span class="chip s-running live"><i></i>Running</span><b>A batch is applying right now.</b><span class="muted">Watch it live →</span></a>` : ""}
    <div class="grid g4">
      <div class="card kpi" style="--kpi-glow:rgba(52,211,153,.25)"><div class="label">${icon.send}Applications</div><div class="value">${ov.applications}</div><div class="hint">${ov.unconfirmed} without a confirmation page</div></div>
      <div class="card kpi"><div class="label">${icon.spark}Last 7 days</div><div class="value">${week}</div><div class="hint">${days.slice(-1)[0].n} today</div></div>
      <a class="card kpi" href="#/questions" style="text-decoration:none;--kpi-glow:rgba(251,191,36,.22)"><div class="label">${icon.chat}Open questions</div><div class="value">${ov.open_questions}</div><div class="hint">${ov.open_questions ? "Answer once, reused on every form" : "Nothing waiting on you"}</div></a>
      <div class="card kpi" style="--kpi-glow:rgba(45,212,191,.22)"><div class="label">${icon.cpu}Decision model</div>
        <div class="value" style="font-size:22px;margin-top:10px">${system.backend === "clef" ? "Clef-Flash" : system.backend}</div>
        <div class="hint">${clef ? (clef.running ? "Loaded on the GPU" : clef.memory_free_gb != null ? `${clef.memory_free_gb} GB free · needs ${clef.memory_needed_gb}` : "Starts with a batch") : system.problem || "Ready"}</div></div>
    </div>
    <div class="grid g2" style="margin-top:16px">
      <div class="card">
        <div class="card-head"><h2>Applications per day</h2><p class="sub">last 14 days</p></div>
        <div class="bars">${days.map((d) => html`<div class="bar"><div class="fill" data-n="${d.n}" style="height:${(d.n / max) * 100}%"></div><span class="day">${d.label}</span></div>`)}</div>
      </div>
      <div class="card">
        <div class="card-head"><h2>By résumé</h2><p class="sub">which résumé went out</p></div>
        <div class="split">${tracks.map(([t, n]) => html`<span class="t-${trackKey(t)}" style="flex:${n / total};--c:var(--${trackKey(t) in TRACK_NAMES ? trackKey(t) : "gray"})"></span>`)}</div>
        <div class="legend">${tracks.map(([t, n]) => html`<div class="t-${trackKey(t)}"><i style="--c:var(--${trackKey(t) in TRACK_NAMES ? trackKey(t) : "gray"})"></i>${t === "unknown" ? "Not recorded (older runs)" : `${trackName(t)} résumé`}<b>${n}</b></div>`)}</div>
      </div>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="card-head"><h2>Recent applications</h2><a class="btn ghost sm" href="#/history">All history →</a></div>
      <div class="list">${ov.recent.length ? ov.recent.map((a) => html`
        <div class="row"><div style="min-width:0"><div class="title">${a.title}</div>
          <div class="meta">${a.company}<span class="faint">·</span>${ago(a.at)}${trackChip(a.track)}</div></div>
          <div style="display:flex;gap:8px;align-items:center">${statusChip(a.status)}<a class="btn ghost sm" href="${safeUrl(a.url)}" target="_blank" rel="noopener">${icon.link}</a></div></div>`)
        : html`<div class="empty">${icon.send}No applications yet. Find jobs to get started.</div>`}</div>
    </div>`);
}

// ---- live streams ---------------------------------------------------------------------------------------------

// A search or a batch is followed over server-sent events: the whole state once, then only what changed. Streams
// close when you leave the page (route → clearTimers).
function stream(path, { message, end }) {
  const es = new EventSource(`/api${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(TOKEN)}`);
  state.streams.push(es);
  es.onmessage = (e) => { try { message(JSON.parse(e.data)); } catch (err) { console.error(err); } };
  const finish = () => { es.close(); end?.(); };
  es.addEventListener("end", finish);
  es.addEventListener("gone", finish);
  es.onerror = () => {
    // The browser reconnects by itself after a dropped connection. Closed for good: the app restarted (new
    // token: api() reloads the page) or the search/batch is gone.
    if (es.readyState === EventSource.CLOSED) api("/system").catch(() => {});
  };
  return es;
}

// Patch a list in place: one element per key, in `keys` order. An element is redrawn only when its content
// (sig) changed, and moved only when it's out of place, so nothing flickers, and open panels, focus and hover stay.
function reconcile(parent, keys, sigOf, htmlOf) {
  const have = new Map([...parent.children].map((el) => [el.dataset.k, el]));
  keys.forEach((k, i) => {
    const sig = sigOf(k);
    let el = have.get(k);
    if (!el || el.dataset.sig !== sig) {
      const tpl = document.createElement("template");
      tpl.innerHTML = htmlOf(k).__html.trim();
      const fresh = tpl.content.firstElementChild;
      fresh.dataset.k = k;
      fresh.dataset.sig = sig;
      if (el) el.replaceWith(fresh); else parent.append(fresh);
      el = fresh;
    }
    have.delete(k);
    if (parent.children[i] !== el) parent.insertBefore(el, parent.children[i] || null);
  });
  have.forEach((el) => el.remove());
}

function setHtml(el, view) { if (el && el.__last !== view.__html) { el.__last = view.__html; el.innerHTML = view.__html; } }

// ---- find jobs ------------------------------------------------------------------------------------------------

const FORM_KEY = "jev-find-form";
const SOURCE_NAMES = { linkedin: "LinkedIn", hiringcafe: "hiring.cafe", board: "company board" };
const WORKPLACE_NAMES = { onsite: "On-site", hybrid: "Hybrid", remote: "Remote" };

function saveForm() { try { localStorage.setItem(FORM_KEY, JSON.stringify(state.findForm)); } catch { /* private window */ } }

async function find() {
  if (!state.findForm) {
    const [defaults, system, tracks] = await Promise.all([api("/search/defaults"), api("/system"), api("/tracks")]);
    state.tracks = tracks;
    let kept = null;
    try { kept = JSON.parse(localStorage.getItem(FORM_KEY) || "null"); } catch { kept = null; }
    state.findForm = {
      roles: defaults.roles, locations: ["India"], posted: "week", workplaces: [],
      maxYears: system.years != null ? Math.floor(system.years) : 2, easy: false, senior: false,
      sources: { linkedin: true, hiringcafe: false },
      ...(kept && Array.isArray(kept.roles) ? kept : {}),
      suggestions: defaults.suggestions, boards: defaults.boards,
    };
  }
  renderFind();
  if (state.search?.id && state.search.state !== "done") followSearch(state.search.id);
}

function chipBox(list, attr, placeholder) {
  return html`<div class="tags" data-box="${attr}">${list.map((w, i) => html`<span class="chip">${w}<button data-del="${i}" title="Remove">×</button></span>`)}
    <input type="text" placeholder="${placeholder}" data-add></div>`;
}

function renderFind() {
  const f = state.findForm;
  const suggest = Object.entries(f.suggestions || {}).map(([t, words]) => [t, words.filter((w) => !f.roles.includes(w))]).filter(([, w]) => w.length);
  mount(main, html`
    <div class="page-head"><div><h1>Find jobs</h1><p class="sub">Name the roles you'd take. Every job that fits your filters is listed; each one shows which résumé fits it best and what it asks that your résumés don't.</p></div></div>
    <div class="card">
      <label class="field"><span>Roles</span>${chipBox(f.roles, "roles", "Add a role, then Enter (e.g. Solutions Engineer)")}</label>
      ${suggest.length ? html`<div class="suggest"><span class="muted">From your résumés:</span>${suggest.map(([t, words]) => words.map((w) => html`<button class="chip t-${trackKey(t)}" data-suggest="${w}" title="Add this role">+ ${w}</button>`))}</div>` : ""}
      <div class="filters">
        <label class="field" style="min-width:220px;flex:1"><span>Locations</span>${chipBox(f.locations, "locations", "City or country, then Enter")}</label>
        <label class="field"><span>Posted</span><div class="seg" id="posted">${["day", "week", "month"].map((p) => html`<button data-p="${p}" class="${f.posted === p ? "on" : ""}">${{ day: "24 hours", week: "Past week", month: "Past month" }[p]}</button>`)}</div></label>
        <label class="field"><span>Workplace <span class="faint">(none = any)</span></span><div class="seg" id="workplace">${Object.entries(WORKPLACE_NAMES).map(([k, l]) => html`<button data-w="${k}" class="${f.workplaces.includes(k) ? "on" : ""}">${l}</button>`)}</div></label>
        <label class="field" style="width:120px"><span>Max years asked</span><input type="number" id="years" min="0" max="20" step="1" value="${f.maxYears ?? ""}"></label>
      </div>
      <div class="filters">
        <span class="muted" style="font-size:12.5px;font-weight:600">Sources</span>
        <label class="toggle"><input type="checkbox" data-src="linkedin" ${f.sources.linkedin ? "checked" : ""}>LinkedIn</label>
        <label class="toggle" title="Opens hiringcafe.com in a background tab of your Chrome to read its search results"><input type="checkbox" data-src="hiringcafe" ${f.sources.hiringcafe ? "checked" : ""}>hiring.cafe <span class="faint">(uses your Chrome)</span></label>
        <span class="sep"></span>
        <label class="toggle"><input type="checkbox" id="easy" ${f.easy ? "checked" : ""}>Easy Apply only</label>
        <label class="toggle"><input type="checkbox" id="senior" ${f.senior ? "checked" : ""}>Include senior titles</label>
        <span style="flex:1"></span>
        ${f.boards ? html`<button class="btn" id="boards">Company boards</button>` : ""}
        <button class="btn primary" id="go">${icon.search}Search</button>
      </div>
      <div id="prefs-panel"></div>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="card-head"><div><h2>Paste job links</h2><p class="sub">LinkedIn, Greenhouse, Lever or Ashby links, one per line (or any job page). Each is checked like a search result: years, keyword coverage, résumé.</p></div>
        <button class="btn" id="links-go">${icon.search}Check links</button></div>
      <textarea id="links" rows="3" placeholder="https://www.linkedin.com/jobs/view/4471912205/&#10;https://job-boards.greenhouse.io/acme/jobs/123">${state.linksText || ""}</textarea>
    </div>
    <div id="boards-panel"></div>
    <div id="results" style="margin-top:16px"></div>
    <div id="dock-slot"></div>`);

  $$("[data-box]").forEach((box) => {
    const list = f[box.dataset.box];
    $$("[data-del]", box).forEach((b) => (b.onclick = (e) => { e.preventDefault(); list.splice(+b.dataset.del, 1); saveForm(); renderFind(); }));
    const add = $("[data-add]", box);
    add.onkeydown = (e) => {
      if (e.key !== "Enter" || !add.value.trim()) return;
      e.preventDefault();
      add.value.split(/[;|]/).map((v) => v.trim()).filter((v) => v && !list.includes(v)).forEach((v) => list.push(v));
      saveForm(); renderFind();
      $(`[data-box="${box.dataset.box}"] [data-add]`)?.focus();
    };
  });
  $$("[data-suggest]").forEach((b) => (b.onclick = () => { f.roles.push(b.dataset.suggest); saveForm(); renderFind(); }));
  $$("#posted button").forEach((b) => (b.onclick = () => { f.posted = b.dataset.p; saveForm(); $$("#posted button").forEach((x) => x.classList.toggle("on", x === b)); }));
  $$("#workplace button").forEach((b) => (b.onclick = () => {
    const w = b.dataset.w;
    f.workplaces = f.workplaces.includes(w) ? f.workplaces.filter((x) => x !== w) : [...f.workplaces, w];
    b.classList.toggle("on", f.workplaces.includes(w)); saveForm();
  }));
  $$("[data-src]").forEach((i) => (i.onchange = () => { f.sources[i.dataset.src] = i.checked; saveForm(); }));
  $("#years").oninput = (e) => { f.maxYears = e.target.value === "" ? null : +e.target.value; saveForm(); };
  $("#easy").onchange = (e) => { f.easy = e.target.checked; saveForm(); };
  $("#senior").onchange = (e) => { f.senior = e.target.checked; saveForm(); };
  $("#go").onclick = startSearch;
  $("#links").oninput = (e) => (state.linksText = e.target.value);
  $("#links-go").onclick = async () => {
    const urls = (state.linksText || "").split(/[\s,]+/).filter((u) => /^https?:\/\//i.test(u));
    if (!urls.length) return toast("Paste at least one job link (starting with http).", "var(--amber)");
    try {
      const { id } = await api("/links", { method: "POST", body: { urls } });
      newSearch(id, "reading");
      state.filter = "all";
      followSearch(id);
    } catch (e) { toast(e.message, "var(--red)"); }
  };
  if ($("#boards")) $("#boards").onclick = () => { state.boardsOpen = !state.boardsOpen; renderBoards(); };
  renderPrefs();
  renderBoards();
  renderResults();
}

function newSearch(id, phase = "searching") {
  state.search = { id, state: phase, progress: [0, 0], problems: [], jobs: new Map(), order: [] };
  state.boardJobs = [];
  state.selected.clear();
  renderResults();
}

async function startSearch() {
  const f = state.findForm;
  const sources = Object.entries(f.sources).filter(([, on]) => on).map(([k]) => k);
  if (!f.roles.length) return toast("Add at least one role to search for.", "var(--amber)");
  if (!sources.length) return toast("Switch on LinkedIn or hiring.cafe.", "var(--amber)");
  try {
    const { id } = await api("/search", { method: "POST", body: {
      roles: f.roles, locations: f.locations, posted: f.posted, workplaces: f.workplaces, max_years: f.maxYears,
      easy_apply: f.easy, include_senior: f.senior, sources } });
    newSearch(id);
    if (f.sources.hiringcafe) toast("hiring.cafe opens in a background tab of your Chrome.");
    followSearch(id);
  } catch (e) { toast(e.message, "var(--red)"); }
}

function followSearch(id) {
  stream(`/search/${id}/events`, {
    message: (m) => {
      const s = state.search;
      if (!s || s.id !== id) return;
      if (m.full) { s.jobs = new Map(); s.order = []; }
      for (const j of m.jobs) {
        if (!s.jobs.has(j.key)) s.order.push(j.key);
        s.jobs.set(j.key, j);
      }
      Object.assign(s, { state: m.state, progress: m.progress, problems: m.problems, params: m.params });
      if (m.state === "done" && !s.announced) {
        s.announced = true;
        const all = [...s.jobs.values()];
        toast(`${all.filter((j) => j.fit).length} good fits out of ${all.length} jobs (${all.filter((j) => j.fresh).length} posted in the last 24 hours).`, "var(--green)");
      }
      scheduleResults();
    },
  });
}

let resultsFrame = 0;
function scheduleResults() { if (!resultsFrame) resultsFrame = requestAnimationFrame(() => { resultsFrame = 0; updateResults(); }); }

async function renderBoards() {
  const slot = $("#boards-panel");
  if (!slot) return;
  if (!state.boardsOpen) return (slot.innerHTML = "");
  let b;
  try { b = await api("/boards"); } catch (e) { return toast(e.message, "var(--red)"); }
  const logo = { greenhouse: "GH", lever: "LV", ashby: "AS" };
  mount(slot, html`<div class="card" style="margin-top:16px">
    <div class="card-head"><div><h2>Company boards</h2><p class="sub">Companies' own careers pages on Greenhouse, Lever or Ashby: no login needed, often jobs LinkedIn doesn't show. A job matches when its title names one of your roles. ${b.own ? html`Saved in <span class="mono">${b.file}</span>.` : html`These are examples; your first change saves your own list.`}</p></div>
      <button class="btn primary" id="scan-boards" ${b.boards.length ? "" : "disabled"}>${icon.search}Scan ${b.boards.length} board${b.boards.length === 1 ? "" : "s"}</button></div>
    <div class="board-list">${b.boards.map((x) => html`<span class="board"><b>${logo[x.provider]}</b><a href="${safeUrl(x.url)}" target="_blank" rel="noopener">${x.token}</a><button data-delboard="${x.entry}" title="Remove">×</button></span>`)}</div>
    <div style="display:flex;gap:8px;margin-top:14px"><input type="text" id="board-new" placeholder="Paste a careers-page link (job-boards.greenhouse.io/acme, jobs.lever.co/acme, jobs.ashbyhq.com/acme) or greenhouse:acme"><button class="btn" id="board-add">Add</button></div>
  </div>`);
  $("#scan-boards").onclick = scanBoards;
  const add = async () => {
    const v = $("#board-new").value.trim();
    if (!v) return;
    $("#board-add").disabled = true;
    try {
      const res = await api("/boards", { method: "POST", body: { board: v } });
      toast(`Added ${res.added}: ${res.open_jobs} open job${res.open_jobs === 1 ? "" : "s"} there now.`, "var(--green)");
      renderBoards();
    } catch (e) { toast(e.message, "var(--red)"); $("#board-add").disabled = false; }
  };
  $("#board-add").onclick = add;
  $("#board-new").onkeydown = (e) => { if (e.key === "Enter") add(); };
  $$("[data-delboard]", slot).forEach((x) => (x.onclick = async () => {
    try { await api(`/boards?entry=${encodeURIComponent(x.dataset.delboard)}`, { method: "DELETE" }); renderBoards(); } catch (e) { toast(e.message, "var(--red)"); }
  }));
}

async function scanBoards() {
  toast("Reading your company boards…");
  try {
    const res = await api("/boards/scan", { method: "POST", body: { roles: state.findForm.roles, max_years: state.findForm.maxYears } });
    if (!state.search) newSearch(null, "done");
    state.boardJobs = res.jobs.map((j) => ({ ...j, key: j.url, fit: true, state: "read", board: true, provider: "board" }));
    toast(`${res.jobs.length} jobs from company boards.`, "var(--green)");
    renderResults();
  } catch (e) { toast(e.message, "var(--red)"); }
}

function allJobs() {
  const s = state.search;
  return s ? [...s.order.map((k) => s.jobs.get(k)), ...(state.boardJobs || [])] : [];
}

function jobGroups(jobs) {
  return {
    fit: jobs.filter((j) => j.fit),
    fresh: jobs.filter((j) => j.fresh && !j.known && !j.dismissed),
    all: jobs,
    hidden: jobs.filter((j) => (j.state === "read" && !j.fit) || j.known || j.dismissed),
  };
}

// While a search runs, rows keep their place (24-hour jobs first, then as found) so nothing jumps around; when it
// finishes they're sorted: newest first, or best match.
function ordered(list, busy) {
  const fresh = (j) => (j.fresh ? 0 : 1);
  if (busy) return [...list].sort((a, b) => fresh(a) - fresh(b));
  if (state.sort === "match") return [...list].sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
  return [...list].sort((a, b) => fresh(a) - fresh(b) || (b.score ?? -1) - (a.score ?? -1) || (a.age_hours ?? 1e9) - (b.age_hours ?? 1e9));
}

function renderResults() {
  const box = $("#results");
  if (!box) return;
  if (!state.search) {
    mount(box, html`<div class="card empty">${icon.search}Add your roles and search. Every job lists the years it asks, the skills it names and the résumé that fits it best.</div>`);
    return renderDock();
  }
  mount(box, html`<div class="card"><div id="res-head"></div><div id="res-body"></div></div>`);
  box.onclick = async (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    if (t.dataset.f) { state.filter = t.dataset.f; updateResults(); }
    else if (t.dataset.sort) { state.sort = t.dataset.sort; updateResults(); }
    else if (t.id === "stop-search") {
      t.disabled = true;
      try { await api(`/search/${state.search.id}/stop`, { method: "POST" }); toast("Stopping: what's found so far stays listed."); }
      catch (err) { t.disabled = false; toast(err.message, "var(--red)"); }
    }
    else if (t.id === "pick-fits") { jobGroups(allJobs()).fit.forEach((j) => state.selected.set(j.key || j.url, j)); updateResults(true); renderDock(); }
    else if (t.id === "pick-none") { state.selected.clear(); updateResults(true); renderDock(); }
    else if (t.dataset.dismiss || t.dataset.undismiss) {
      const key = t.dataset.dismiss || t.dataset.undismiss;
      const job = allJobs().find((x) => (x.key || x.url) === key);
      const undo = !!t.dataset.undismiss;
      try {
        await api("/dismiss", { method: "POST", body: { key, title: job?.title, company: job?.company, undo } });
        Object.assign(job, undo ? { dismissed: false, why_not: null, fit: !job.known && !job.closed } : { dismissed: true, why_not: "dismissed by you", fit: false });
        state.selected.delete(key);
        toast(undo ? "Shown again." : "Dismissed: it won't come back in searches or autopilot.", undo ? "var(--accent)" : "var(--gray)");
        updateResults(); renderDock();
      } catch (err) { toast(err.message, "var(--red)"); }
    }
  };
  box.onchange = (e) => {
    const c = e.target.closest(".pick");
    if (!c) return;
    const job = allJobs().find((j) => (j.key || j.url) === c.dataset.key);
    c.checked ? state.selected.set(c.dataset.key, job) : state.selected.delete(c.dataset.key);
    renderDock();
  };
  if (!box.__wired) box.addEventListener("toggle", (e) => {
    const d = e.target.closest?.("[data-why]");
    if (!d) return;
    state.whyOpen ||= new Set();
    d.open ? state.whyOpen.add(d.dataset.why) : state.whyOpen.delete(d.dataset.why);
  }, true);
  box.__wired = true;
  updateResults();
}

function updateResults(force = false) {
  const head = $("#res-head"), body = $("#res-body");
  const s = state.search;
  if (!head || !body || !s) return;
  const jobs = allJobs();
  const groups = jobGroups(jobs);
  const busy = s.state !== "done";
  const [done, total] = s.progress || [0, 0];
  const phase = { searching: "Searching LinkedIn", "searching hiring.cafe": "Searching hiring.cafe", stopping: "Stopping…" }[s.state] || `Reading jobs ${done}/${total}`;
  setHtml(head, html`
    <div class="card-head" style="flex-wrap:wrap">
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <div class="seg" id="filter">${[["fit", "Good fits"], ["fresh", "Last 24 hours"], ["all", "All"], ["hidden", "Hidden"]].map(([k, l]) => html`<button data-f="${k}" class="${state.filter === k ? "on" : ""}">${l} <span class="faint">${groups[k].length}</span></button>`)}</div>
        <div class="seg">${[["newest", "Newest first"], ["match", "Best match"]].map(([k, l]) => html`<button data-sort="${k}" class="${(state.sort || "newest") === k ? "on" : ""}">${l}</button>`)}</div>
        ${busy && s.id ? html`<span class="chip s-running live"><i></i>${phase}</span>` : ""}
      </div>
      <div style="display:flex;gap:8px">${groups.fit.length ? html`<button class="btn sm" id="pick-fits">Select all good fits</button>` : ""}<button class="btn sm ghost" id="pick-none">Clear</button>${busy && s.id ? html`<button class="btn sm danger" id="stop-search" ${s.state === "stopping" ? "disabled" : ""}>${icon.stop}Stop search</button>` : ""}</div>
    </div>
    ${busy && total ? html`<div class="progress" style="margin-bottom:14px"><span style="width:${(done / total) * 100}%"></span></div>` : ""}
    ${busy && s.id ? html`<p class="muted" style="font-size:12.5px;margin:-4px 0 12px">Rows stay in place while jobs are read; they're sorted when the search finishes.</p>` : ""}
    ${(s.problems || []).map((p) => html`<div class="banner warn">${p}</div>`)}`);
  const shown = ordered(groups[state.filter] || jobs, busy);
  if (!shown.length) { setHtml(body, html`<div class="empty">${busy ? "Looking…" : "Nothing here."}</div>`); return; }
  if (!$("#res-rows", body)) {
    body.__last = null;
    mount(body, html`<table class="results"><thead><tr><th></th><th>Match</th><th>Role</th><th>Posted</th><th>Asks</th><th>Keyword coverage</th><th>Résumé</th><th></th></tr></thead><tbody id="res-rows"></tbody></table>`);
  }
  const byKey = new Map(shown.map((j) => [j.key || j.url, j]));
  const sel = (k) => (state.selected.has(k) ? "1" : "0") + (state.whyOpen?.has(k) ? "1" : "0");
  reconcile($("#res-rows", body), [...byKey.keys()], (k) => (force ? Math.random() : "") + sel(k) + JSON.stringify(byKey.get(k)), (k) => jobRow(byKey.get(k)));
}

function posted(j) {
  if (j.age_hours == null) return j.posted ? ago(j.posted) : "";
  if (j.age_hours < 1) return "just now";
  if (j.age_hours < 24) return `${Math.round(j.age_hours)}h ago`;
  return `${Math.round(j.age_hours / 24)}d ago`;
}

function jobRow(j) {
  const key = j.key || j.url;
  const dim = j.state === "read" && !j.fit;
  const source = SOURCE_NAMES[j.board ? "board" : j.provider] || "";
  return html`<tr class="${dim ? "dim" : ""}">
    <td><input type="checkbox" class="pick" data-key="${key}" ${state.selected.has(key) ? "checked" : ""} ${j.known ? "disabled" : ""}></td>
    <td>${j.score != null ? html`<span class="score" style="--s:${j.score}">${j.score}</span>` : j.state === "listed" ? html`<div class="sk" style="width:30px"></div>` : ""}</td>
    <td><div class="t"><a href="${safeUrl(j.url)}" target="_blank" rel="noopener">${j.title || j.url}</a></div>
      <div class="muted" style="font-size:12.5px">${j.company}${j.location ? ` · ${j.location}` : ""}${source ? ` · ${source}` : ""}${j.query ? html` · <span class="faint">for “${j.query}”</span>` : ""}</div>
      <div class="row-chips">
        ${j.fresh ? html`<span class="chip fresh"><i></i>New</span>` : ""}
        ${j.salary ? html`<span class="chip s-submitted"><i></i>${j.salary}</span>` : ""}
        ${j.known ? html`<span class="chip s-submitted"><i></i>Already applied</span>` : ""}
        ${j.account ? html`<span class="chip plain" title="${j.portal} asks you to create an account; the run waits for you to sign in once">Needs an account</span>` : ""}
        ${j.why_not ? html`<span class="chip s-deferred"><i></i>${j.why_not}</span>` : ""}
        ${j.recent_company ? html`<span class="chip s-review"><i></i>Applied to ${j.company} recently</span>` : ""}
      </div></td>
    <td class="mono" style="white-space:nowrap">${posted(j) || html`<span class="faint">—</span>`}</td>
    <td class="mono" style="white-space:nowrap">${j.state === "listed" ? html`<div class="sk" style="width:40px"></div>` : j.min_years != null ? `${j.min_years}+ yrs` : html`<span class="faint">—</span>`}</td>
    <td>${j.coverage ? html`<div class="cov">${j.coverage.pct == null ? html`<span class="faint">No skills named</span>` : html`<b>${j.coverage.pct}%</b> covered`}</div>
        <div class="skills">${j.coverage.covered.slice(0, 6).map((k) => html`<span class="chip cov-yes" title="On your résumé">${k}</span>`)}${j.coverage.related.slice(0, 4).map((k) => html`<span class="chip cov-near" title="Related to your résumé">${k}</span>`)}${j.coverage.missing.slice(0, 5).map((k) => html`<span class="chip cov-no" title="Not on your résumé">${k}</span>`)}</div>`
      : html`<div class="skills">${(j.skills || []).slice(0, 7).map((k) => html`<span class="chip plain">${k}</span>`)}</div>`}
      ${j.reasons?.length ? html`<details class="why" data-why="${key}" ${state.whyOpen?.has(key) ? "open" : ""}><summary>Why this score</summary><ul>${j.reasons.map((r) => html`<li class="${r.good ? "good" : "bad"}">${r.text}</li>`)}</ul></details>` : ""}</td>
    <td>${j.track ? trackChip(j.track) : j.state === "listed" ? html`<div class="sk" style="width:60px"></div>` : ""}</td>
    <td style="white-space:nowrap"><a class="btn ghost sm" href="${safeUrl(j.url)}" target="_blank" rel="noopener" title="Open the posting">${icon.link}</a>
      ${j.board ? "" : j.dismissed ? html`<button class="btn ghost sm" data-undismiss="${key}" title="Show it again">Undo</button>` : html`<button class="btn ghost sm" data-dismiss="${key}" title="Not interested: hide it from searches and autopilot">${icon.x}</button>`}</td></tr>`;
}

function renderDock() {
  const slot = $("#dock-slot");
  if (!slot) return;
  const n = state.selected.size;
  if (!n) return (slot.innerHTML = "");
  const tracks = {};
  state.selected.forEach((j) => { if (j.track) tracks[j.track] = (tracks[j.track] || 0) + 1; });
  mount(slot, html`<div class="dock">
    <b>${n} job${n > 1 ? "s" : ""} selected</b>
    <span style="display:flex;gap:6px">${Object.entries(tracks).map(([t, c]) => html`<span class="chip t-${trackKey(t)}"><i></i>${trackName(t)} ${c}</span>`)}</span>
    <label class="toggle"><input type="checkbox" id="dock-submit" ${state.submit ? "checked" : ""}>Auto-submit complete ones</label>
    <button class="btn primary" id="dock-go">${icon.play}Start applying</button></div>`);
  $("#dock-submit").onchange = (e) => (state.submit = e.target.checked);
  $("#dock-go").onclick = () => confirmBatch([...state.selected.values()].map((j) => j.url));
}

function confirmBatch(urls, title = "Start applying?") {
  modal(html`
    <h2 style="font-size:18px">${title}</h2>
    <p class="muted">${urls.length} job${urls.length > 1 ? "s" : ""} will open one by one in your Chrome, with ${MODEL_NAMES[state.system?.backend] || "the decision model"} choosing the answers and the best résumé for each.</p>
    <div class="banner ${state.submit ? "warn" : ""}" style="margin:14px 0 0">${state.submit
      ? html`<span>${icon.send}</span><span><b>Auto-submit is on.</b> Only complete applications are sent, never twice. Anything unsure, a login or a captcha is left for you.</span>`
      : html`<span>${icon.check}</span><span>Auto-submit is off: every application stops before Submit for you to review.</span>`}</div>
    <div class="actions"><button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="confirm-go">${icon.play}Start</button></div>`,
  (el, close) => {
    $("#confirm-go", el).onclick = async () => {
      askNotifications(); // so a run waiting for your answer can tell you, even in another window
      try {
        const { id } = await api("/batches", { method: "POST", body: { urls, submit: state.submit } });
        close();
        state.selected.clear();
        toast("Batch started. Watch it here.", "var(--green)");
        location.hash = `#/batch/${id}`;
      } catch (e) { toast(e.message, "var(--red)"); }
    };
  });
}

// ---- batches --------------------------------------------------------------------------------------------------

const OUTCOMES = [["submitted", "Submitted", "var(--green)"], ["review", "Left for you", "var(--amber)"], ["stopped", "Stopped", "var(--red)"], ["deferred", "Deferred / not run", "var(--gray)"]];
function outcome(counts, k) {
  const c = (x) => counts[x] || 0;
  if (k === "deferred") return c("deferred") + c("not_run") + c("closed");
  if (k === "stopped") return c("stopped") + c("failed");
  if (k === "review") return c("review") + c("ready_to_submit") + c("ready_to_check");
  return c(k);
}

async function batchPage(id) {
  if (id) return batchDetail(id);
  mount(main, html`<div class="page-head"><div><h1>Batches</h1><p class="sub">Every batch you started, newest first. Open one to follow it live or read how it went.</p></div>
      <a class="btn primary" href="#/find">${icon.search}Find jobs</a></div>
    <div class="card"><div class="list" id="batch-list"><div class="sk" style="height:40px"></div></div></div>`);
  every(3000, async () => {
    const list = await api("/batches");
    if (!changed("batches", list)) return;
    const box = $("#batch-list");
    if (!box) return;
    if (!list.length) return setHtml(box, html`<div class="empty">${icon.play}No batch yet. Pick jobs in Find jobs and press Start applying.</div>`);
    reconcile(box, list.map((b) => b.id), (k) => JSON.stringify(list.find((b) => b.id === k)), (k) => batchRow(list.find((b) => b.id === k)));
  });
}

function batchRow(b) {
  const running = ["starting", "planning", "running"].includes(b.state);
  const when = new Date(b.started);
  return html`<a class="row batch-row" href="#/batch/${b.id}" style="text-decoration:none;color:inherit">
    <div style="min-width:0"><div class="title">${when.toLocaleDateString(undefined, { day: "numeric", month: "short" })} · ${when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}
      <span class="faint" style="font-weight:400">· ${b.jobs} job${b.jobs === 1 ? "" : "s"}${b.submit === true ? " · auto-submit" : b.submit === false ? " · review before submit" : ""}</span></div>
      <div class="meta">${statusChip(b.state, running)}${running ? b.phase : ""}
        ${OUTCOMES.map(([k, l, c]) => (outcome(b.counts, k) ? html`<span class="chip plain" style="color:${c}">${l} ${outcome(b.counts, k)}</span>` : ""))}</div></div>
    <span class="muted">${running ? "Watch live →" : "Open →"}</span></a>`;
}

function batchDetail(id) {
  mount(main, html`
    <a class="back" href="#/batch">← All batches</a>
    <div class="page-head" id="b-head"></div>
    <div class="grid g4" style="margin-bottom:16px" id="b-kpis"></div>
    <div class="jobs" id="b-jobs"></div>
    <details class="card" style="margin-top:16px" ${state.consoleOpen ? "open" : ""} id="console-box">
      <summary style="cursor:pointer;font-weight:600">Live log <span class="faint" id="log-n"></span></summary>
      <div class="console" id="console" style="margin-top:12px"></div>
    </details>`);
  $("#console-box").ontoggle = (e) => (state.consoleOpen = e.target.open);
  let b = null;
  main.onclick = (e) => {
    if (!b) return;
    if (e.target.closest("#stop")) modal(html`<h2 style="font-size:18px">Stop this batch?</h2><p class="muted">The job in progress stops where it is (nothing half-submitted is sent). Jobs not reached stay unapplied.</p>
      <div class="actions"><button class="btn ghost" data-close>Keep going</button><button class="btn danger" id="really">${icon.stop}Stop batch</button></div>`,
      (el, close) => ($("#really", el).onclick = async () => { await api(`/batches/${id}/stop`, { method: "POST" }); close(); toast("Batch stopped."); }));
    if (e.target.closest("#rerun")) {
      const urls = b.jobs.filter((j) => ["stopped", "failed", "review", "not_run"].includes(statusKey(j.state))).map((j) => j.url);
      if (!urls.length) return toast("Nothing to rerun: every job finished.", "var(--green)");
      confirmBatch(urls, `Rerun ${urls.length} job${urls.length > 1 ? "s" : ""}?`);
    }
  };
  stream(`/batches/${id}/events`, {
    message: (m) => {
      if (m.full || !b) b = { ...m, jobs: [], log: [] };
      const byN = new Map(b.jobs.map((j) => [j.n, j]));
      m.jobs.forEach((j) => byN.set(j.n, j));
      b = { ...b, ...m, jobs: [...byN.values()].sort((x, y) => x.n - y.n), log: m.full ? m.log_add : [...b.log, ...m.log_add].slice(-2000) };
      patchBatch(b, m.log_add, m.full);
    },
    end: () => refreshShell(),
  });
}

function patchBatch(b, newLines, full) {
  const finished = ["done", "failed", "stopped"].includes(b.state);
  const counts = {};
  b.jobs.forEach((j) => { const k = statusKey(j.state); counts[k] = (counts[k] || 0) + 1; });
  const doneN = b.jobs.filter((j) => !["waiting", "running"].includes(j.state)).length;
  const pct = Math.round((doneN / Math.max(1, b.jobs.length)) * 100);
  setHtml($("#b-head"), html`
    <div class="live-head">
      <div class="ring" style="--p:${pct}"><b>${doneN}/${b.jobs.length}</b></div>
      <div><h1>${finished ? "Batch finished" : "Applying…"}</h1>
        <p class="sub">${statusChip(b.state, !finished)} ${b.phase} · started ${ago(b.started)}${b.submit === true ? " · auto-submit on" : b.submit === false ? " · auto-submit off" : ""}</p></div>
    </div>
    <div style="display:flex;gap:8px">${finished ? html`<button class="btn" id="rerun">${icon.play}Rerun stopped jobs</button>` : html`<button class="btn danger" id="stop">${icon.stop}Stop</button>`}</div>`);
  setHtml($("#b-kpis"), html`${OUTCOMES.map(([k, l, c]) => html`
    <div class="card kpi" style="--kpi-glow:color-mix(in srgb, ${c} 25%, transparent)"><div class="label">${l}</div><div class="value" style="color:${c}">${outcome(counts, k)}</div></div>`)}`);
  const byN = new Map(b.jobs.map((j) => [String(j.n), j]));
  reconcile($("#b-jobs"), [...byN.keys()], (k) => JSON.stringify(byN.get(k)), (k) => jobCard(byN.get(k)));
  const c = $("#console");
  if (c) {
    const atBottom = c.scrollTop + c.clientHeight >= c.scrollHeight - 30;
    if (full) c.innerHTML = "";
    const frag = document.createDocumentFragment();
    newLines.forEach((l) => {
      const d = document.createElement("div");
      d.className = /submitted|SUBMITTED/.test(l) ? "ok" : /needs you|stopped|failed|Traceback/i.test(l) ? "bad" : /^\[\d+\/\d+\]/.test(l) ? "hl" : "";
      d.textContent = l || " ";
      frag.append(d);
    });
    c.append(frag);
    while (c.childElementCount > 2000) c.firstElementChild.remove();
    if (atBottom || full) c.scrollTop = c.scrollHeight;
    $("#log-n").textContent = `(${b.log_n ?? b.log.length} lines)`;
  }
}

function jobCard(j) {
  const k = statusKey(j.state);
  const last = (j.events || []).filter((e) => !/^(résumé|submitted|review|stopped|failed|deferred|ready to submit)\b/.test(e)).slice(-1)[0];
  return html`<div class="job ${k}">
    <div class="top"><span class="n">#${j.n}</span>${statusChip(j.state, k === "running")}</div>
    <div class="who">${j.title || j.url.replace(/^https?:\/\/(www\.)?/, "").slice(0, 60)}${j.company ? html`<small>${j.company}</small>` : ""}</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">${j.track ? trackChip(j.track.includes("/") ? j.track : `data/${j.track}`) : ""}
      ${j.seconds ? html`<span class="chip plain">${j.seconds}s</span>` : ""}
      <a class="btn ghost sm" href="${safeUrl(j.url)}" target="_blank" rel="noopener" style="margin-left:auto">${icon.link}</a></div>
    ${j.waiting ? html`<div class="needs waiting-answer">Waiting for your answer: ${j.waiting.split(" | ").join(" · ")} <a href="#/questions">Answer now →</a></div>`
      : j.needs ? html`<div class="needs">${j.needs}</div>` : last && k !== "waiting" ? html`<div class="ev">${last}</div>` : ""}
    ${j.result && !j.needs ? html`<div class="ev">${j.result}</div>` : ""}
  </div>`;
}

// ---- questions ------------------------------------------------------------------------------------------------

async function questions() {
  if (state.qTab === "bank") return answerBank();
  const [list, rerun, ov] = await Promise.all([api("/questions"), api("/rerun"), api("/overview")]);
  const waitingQs = (ov.waiting_for_answers || []).flatMap((w) => w.questions);
  const isWaiting = (q) => waitingQs.some((w) => q.question.startsWith(w.slice(0, 78)));
  const open = list.filter((q) => !q.answer).sort((a, b) => isWaiting(b) - isWaiting(a)); // a waiting run first
  const done = list.filter((q) => q.answer);
  const tab = state.qTab || "open";
  const shown = tab === "open" ? open : done;
  mount(main, html`
    <div class="page-head"><div><h1>Questions</h1><p class="sub">Answer once: every later form that asks the same question fills it itself.</p></div>
      <div class="seg" id="qtab"><button data-t="open" class="${tab === "open" ? "on" : ""}">Open <span class="faint">${open.length}</span></button><button data-t="bank" class="${tab === "bank" ? "on" : ""}">Answer bank</button></div></div>
    ${waitingQs.length ? html`<div class="banner warn"><span>${icon.chat}</span><span><b>A run is waiting for ${waitingQs.length > 1 ? "these answers" : "this answer"}.</b> <span class="muted">Answer below and it carries on by itself.</span></span></div>` : ""}
    ${rerunBanner(rerun.jobs)}
    <div class="qs">${shown.length ? shown.map((q) => html`
      <div class="card q ${isWaiting(q) && !q.answer ? "waiting-q" : ""}" data-i="${q.index}" data-q="${q.question}">
        ${isWaiting(q) && !q.answer ? html`<span class="chip s-review live" style="margin-bottom:8px"><i></i>A run is waiting for this</span>` : ""}
        <div class="text">${q.question}</div>
        ${q.urls?.length ? html`<div class="muted" style="font-size:12.5px">Asked on ${q.urls.length} job${q.urls.length > 1 ? "s" : ""}: ${q.urls.slice(0, 3).map((u, i) => html`<a href="${safeUrl(u)}" target="_blank" rel="noopener">${i ? ", " : ""}${u.replace(/^https?:\/\/(www\.)?/, "").split("?")[0].slice(0, 46)}</a>`)}</div>` : ""}
        ${q.options?.length
          ? html`<div class="opts">${q.options.map((o) => html`<button data-o="${o}" class="${q.answer === o ? "on" : ""}">${o}</button>`)}</div>`
          : html`<div class="answer-row"><input type="text" value="${q.answer}" placeholder="Your answer…" data-a><button class="btn primary" data-save>${icon.check}Save</button></div>`}
      </div>`) : html`<div class="card empty">${icon.check}${tab === "open" ? "No open questions. Every form can finish without you." : "No answers yet."}</div>`}</div>`);
  $$("#qtab button").forEach((b) => (b.onclick = () => { state.qTab = b.dataset.t; questions(); }));
  wireRerun(rerun.jobs);
  $$(".q").forEach((card) => {
    const save = async (answer) => {
      try {
        await api(`/questions/${card.dataset.i}`, { method: "POST", body: { answer, question: card.dataset.q } });
        card.classList.add("saved");
        toast("Saved. Later forms will use it.", "var(--green)");
        refreshShell();
        setTimeout(() => onPage("questions") && questions(), 500);
      } catch (e) { toast(e.message, "var(--red)"); }
    };
    $$("[data-o]", card).forEach((b) => (b.onclick = () => save(b.dataset.o)));
    const input = $("[data-a]", card);
    if (input) {
      input.onkeydown = (e) => { if (e.key === "Enter") save(input.value); };
      $("[data-save]", card).onclick = () => save(input.value);
    }
  });
}

async function answerBank() {
  const bank = await api("/answers");
  const q = (state.bankQ || "").toLowerCase();
  const shown = bank.filter((a) => !q || `${a.question} ${a.answer}`.toLowerCase().includes(q));
  const used = bank.filter((a) => a.times_used).length;
  mount(main, html`
    <div class="page-head"><div><h1>Answer bank</h1><p class="sub">${bank.length} saved answers · ${used} used by a run so far. Every form that asks the same question gets the same answer.</p></div>
      <div class="seg" id="qtab"><button data-t="open">Open questions</button><button data-t="bank" class="on">Answer bank <span class="faint">${bank.length}</span></button></div></div>
    <div class="card">
      <div class="toolbar"><input type="text" id="bank-q" placeholder="Search questions and answers…" value="${state.bankQ || ""}"><span class="muted" style="margin-left:auto">${shown.length} shown · most used first</span></div>
      <div class="bank">${shown.map((a) => html`
        <div class="bank-item" data-id="${a.id}">
          <div class="bank-q"><b>${a.question}</b>
            <div class="meta muted" style="font-size:12px;margin-top:3px"><span class="chip plain">${a.source}</span>${a.track ? trackChip(a.track) : ""}
              ${a.times_used ? html`<span class="chip s-submitted"><i></i>Used ${a.times_used}× · last ${ago(a.last_used)}</span>` : html`<span class="chip s-deferred"><i></i>Not used yet</span>`}</div></div>
          ${a.options?.length
            ? html`<div class="opts">${a.options.map((o) => html`<button data-bo="${o}" class="${a.answer === o ? "on" : ""}">${o}</button>`)}</div>`
            : html`<textarea rows="${Math.min(6, Math.max(1, Math.ceil(String(a.answer).length / 90)))}" data-ba>${a.answer}</textarea>`}
          <div class="actions-row" style="margin-top:8px"><button class="btn sm primary" data-bsave>${icon.check}Save</button><button class="btn sm ghost danger" data-bdel>${icon.x}${a.id.startsWith("questions:") ? "Clear (ask me again)" : "Delete"}</button></div>
        </div>`)}</div>
      ${shown.length ? "" : html`<div class="empty">Nothing matches.</div>`}
    </div>`);
  $$("#qtab button").forEach((b) => (b.onclick = () => { state.qTab = b.dataset.t; questions(); }));
  $("#bank-q").oninput = (e) => { state.bankQ = e.target.value; const pos = e.target.selectionStart; answerBank().then(() => { const el = $("#bank-q"); el.focus(); el.setSelectionRange(pos, pos); }); };
  $$(".bank-item").forEach((item) => {
    const id = item.dataset.id;
    const save = async (answer) => {
      try { await api(`/answers/${encodeURIComponent(id)}`, { method: "PUT", body: { answer } }); toast("Saved. Later forms use it.", "var(--green)"); answerBank(); }
      catch (e) { toast(e.message, "var(--red)"); }
    };
    $$("[data-bo]", item).forEach((b) => (b.onclick = () => save(b.dataset.bo)));
    $("[data-bsave]", item).onclick = () => { const t = $("[data-ba]", item); if (t) save(t.value); };
    $("[data-bdel]", item).onclick = async () => {
      if (!confirm("Remove this answer? Forms that ask it will ask you again.")) return;
      try { await api(`/answers/${encodeURIComponent(id)}`, { method: "DELETE" }); refreshShell(); answerBank(); } catch (e) { toast(e.message, "var(--red)"); }
    };
  });
}

// ---- history --------------------------------------------------------------------------------------------------

async function history() {
  const tab = state.hTab || "apps";
  const [apps, runs] = await Promise.all([api("/applications"), tab === "runs" ? api("/runs") : Promise.resolve([])]);
  const f = state.hFilter || (state.hFilter = { q: "", track: "", status: "" });
  const draw = () => {
    const q = f.q.toLowerCase();
    const rows = (tab === "apps" ? apps : runs).filter((a) =>
      (!q || JSON.stringify(a).toLowerCase().includes(q)) && (!f.track || a.track === f.track) && (!f.status || statusKey(a.status) === f.status));
    const tracks = [...new Set([...apps, ...runs].map((a) => a.track).filter(Boolean))];
    mount(main, html`
      <div class="page-head"><div><h1>History</h1><p class="sub">${apps.length} applications sent · every run's report, field by field</p></div>
        <div class="seg" id="htab"><button data-t="apps" class="${tab === "apps" ? "on" : ""}">Applications</button><button data-t="runs" class="${tab === "runs" ? "on" : ""}">All runs</button></div></div>
      <div class="card">
        <div class="toolbar">
          <input type="text" id="hq" placeholder="Search company, role, link…" value="${f.q}">
          <select id="ht"><option value="">Every résumé</option>${tracks.map((t) => html`<option value="${t}" ${f.track === t ? "selected" : ""}>${trackName(t)}</option>`)}</select>
          ${tab === "runs" ? html`<select id="hs"><option value="">Every result</option>${["submitted", "review", "stopped", "failed"].map((s) => html`<option value="${s}" ${f.status === s ? "selected" : ""}>${STATUS[s]}</option>`)}</select>` : ""}
          <span class="muted" style="margin-left:auto">${rows.length} shown</span>
        </div>
        ${tab === "apps" ? html`<table class="data"><thead><tr><th>When</th><th>Role</th><th>Company</th><th>Résumé</th><th>Status</th><th></th></tr></thead><tbody>
          ${rows.map((a) => html`<tr class="${a.run ? "click" : ""}" data-run="${a.run || ""}"><td class="muted" style="white-space:nowrap">${ago(a.at)}</td><td style="font-weight:600">${a.title}</td><td>${a.company}</td><td>${trackChip(a.track)}</td><td>${statusChip(a.status)}${a.receipt ? html` <span class="chip plain" title="A receipt of the confirmation page is saved">receipt</span>` : ""}</td>
            <td><a class="btn ghost sm" href="${safeUrl(a.url)}" target="_blank" rel="noopener" data-stop>${icon.link}</a></td></tr>`)}</tbody></table>`
        : html`<table class="data"><thead><tr><th>When</th><th>Job</th><th>Résumé</th><th>Result</th><th>Filled</th><th>Why it stopped</th></tr></thead><tbody>
          ${rows.map((r) => html`<tr class="click" data-run="${r.id}"><td class="muted" style="white-space:nowrap">${ago(r.at)}</td><td class="mono" style="font-size:12px">${r.job || (r.url || "").slice(0, 50)}</td><td>${trackChip(r.track)}</td><td>${statusChip(r.status)}</td><td>${r.fills}</td><td class="muted" style="font-size:12.5px;max-width:360px">${r.reason || r.left[0] || ""}</td></tr>`)}</tbody></table>`}
        ${rows.length ? "" : html`<div class="empty">Nothing matches.</div>`}
      </div>`);
    $$("#htab button").forEach((b) => (b.onclick = () => { state.hTab = b.dataset.t; history(); }));
    $("#hq").oninput = (e) => { f.q = e.target.value; const pos = e.target.selectionStart; draw(); const el = $("#hq"); el.focus(); el.setSelectionRange(pos, pos); };
    $("#ht").onchange = (e) => { f.track = e.target.value; draw(); };
    if ($("#hs")) $("#hs").onchange = (e) => { f.status = e.target.value; draw(); };
    $$("tr[data-run]").forEach((tr) => tr.dataset.run && (tr.onclick = (e) => { if (!e.target.closest("[data-stop]")) openRun(tr.dataset.run); }));
  };
  draw();
}

async function openRun(id) {
  const el = document.getElementById("drawer");
  el.hidden = false;
  mount(el, html`<div><div class="sk" style="width:60%;height:22px"></div></div>`);
  el.onclick = (e) => { if (e.target === el || e.target.closest("[data-close]")) { el.hidden = true; el.innerHTML = ""; } };
  try {
    const r = await api(`/runs/${id}`);
    mount(el, html`<div>
      <div class="card-head"><div><h2 style="font-size:18px">${r.title || r.job || "Run"}</h2>${r.company ? html`<div class="muted">${r.company}</div>` : ""}<p class="sub">${new Date(r.at).toLocaleString()} · ${Math.round(r.elapsed_s || 0)}s</p></div><button class="btn ghost" data-close>${icon.x}</button></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px">${statusChip(r.status)}${trackChip(r.track)}<a class="btn sm" href="${safeUrl(r.url)}" target="_blank" rel="noopener">${icon.link}Open posting</a></div>
      ${r.reason ? html`<div class="banner warn">${r.reason}</div>` : ""}
      ${r.left_for_you.length || r.asked.length ? html`<div class="card" style="margin-bottom:14px"><h2>Left for you</h2><ul>${[...r.left_for_you.map((x) => `${x.what} (${x.why})`), ...r.asked.filter((a) => !a.answered).map((a) => a.question)].map((x) => html`<li>${x}</li>`)}</ul></div>` : ""}
      ${r.receipt ? html`<div class="card" style="margin-bottom:14px"><div class="card-head"><h2>Receipt</h2>${r.receipt.confirmed ? html`<span class="chip s-submitted"><i></i>The site confirmed it</span>` : html`<span class="chip s-review"><i></i>No confirmation text seen</span>`}</div>
        <p class="muted" style="font-size:12.5px;margin-top:0">What the page showed right after Submit · ${new Date(r.receipt.at).toLocaleString()} · <a href="${safeUrl(r.receipt.url)}" target="_blank" rel="noopener">${String(r.receipt.url || "").slice(0, 60)}</a></p>
        ${r.receipt.screenshot ? html`<img class="receipt-img" id="receipt-img" alt="Screenshot of the confirmation page">` : ""}
        <details style="margin-top:8px"><summary class="muted" style="cursor:pointer">Page text</summary><pre class="console" style="max-height:220px">${r.receipt.text}</pre></details></div>` : ""}
      ${r.claude.length ? html`<div class="card" style="margin-bottom:14px"><h2 style="margin-bottom:10px">Claude <span class="faint">${r.claude.length} call${r.claude.length > 1 ? "s" : ""}</span></h2>
        <table class="data"><thead><tr><th>Drafted</th><th>Time</th><th>Tokens in / out</th><th>Cost</th></tr></thead><tbody>${r.claude.map((c) => html`<tr><td style="max-width:300px">${c.question || c.stage}${c.ok === false ? html` <span class="chip s-stopped"><i></i>failed</span>` : ""}</td><td>${c.latency_ms ? `${(c.latency_ms / 1000).toFixed(1)}s` : "—"}</td><td class="mono">${c.input_tokens ?? "—"} / ${c.output_tokens ?? "—"}</td><td class="mono">${c.cost_usd != null ? `$${c.cost_usd.toFixed(4)}` : "—"}</td></tr>`)}</tbody></table></div>` : ""}
      <div class="card" style="margin-bottom:14px"><h2 style="margin-bottom:10px">Fields filled <span class="faint">${r.fields.length}</span></h2>
        ${r.fields.length ? html`<table class="data"><tbody>${r.fields.map((f) => html`<tr><td style="max-width:300px">${f.label}</td><td class="mono" style="font-size:12.5px">${f.value}</td><td><span class="chip plain">${f.source}</span></td><td>${f.confirmed ? icon.check : ""}</td></tr>`)}</tbody></table>` : html`<p class="muted">No fields typed (the site had them filled, or the run stopped first).</p>`}</div>
      <div class="card"><h2 style="margin-bottom:6px">Every step <span class="faint">${r.steps.length}</span></h2>
        <div class="timeline">${r.steps.map((s) => html`<div><span class="faint mono">#${s.step} ${s.kind}</span><span>${s.action}${s.typed ? html` → <span class="mono">${s.typed}</span>` : ""}${s.source ? html` <span class="faint">(${s.source})</span>` : ""}</span><span class="faint">${s.p != null && s.p < 1 ? `${Math.round(s.p * 100)}%` : ""}</span></div>`)}</div></div>
    </div>`);
    const img = $("#receipt-img");
    if (img) {
      const res = await fetch(`/api/runs/${encodeURIComponent(id)}/receipt.png`, { headers: { "X-Jev-Token": TOKEN } });
      if (res.ok) img.src = URL.createObjectURL(await res.blob());
    }
  } catch (e) { mount(el, html`<div><div class="empty">${e.message}</div><button class="btn" data-close>Close</button></div>`); }
}

// ---- system ---------------------------------------------------------------------------------------------------

async function settings() {
  if (!main.querySelector(".models")) mount(main, html`<div class="page-head"><div><h1>System</h1><p class="sub">Checking each decision model…</p></div></div><div class="models">${[1, 2, 3, 4].map(() => html`<div class="model"><div class="sk" style="width:50%"></div><div class="sk" style="width:80%;margin-top:10px"></div></div>`)}</div>`);
  const [system, tracks, models, settingsList] = await Promise.all([api("/system"), api("/tracks"), api("/models"), api("/settings")]);
  const c = system.clef;
  mount(main, html`
    <div class="page-head"><div><h1>System</h1><p class="sub">What a batch uses. Saved to .env and data/&lt;track&gt;/, so the terminal uses the same.</p></div></div>
    <div class="card-head"><h2>Decision model</h2><p class="sub">Picks every answer the fixed rules can't settle. Switching applies to the next batch.</p></div>
    <div class="models" id="models">${models.map((m) => html`
      <button class="model ${m.current ? "on" : ""}" data-id="${m.id}" ${m.current ? raw('aria-pressed="true"') : ""}>
        <div class="model-top"><b>${m.name}</b>${m.current ? html`<span class="chip s-submitted"><i></i>In use</span>` : m.problem ? html`<span class="chip s-deferred"><i></i>Needs setup</span>` : html`<span class="chip plain"><i></i>Ready</span>`}</div>
        <div class="muted" style="font-size:12.5px">${m.maker}</div>
        <p>${m.about}</p>
        ${m.problem ? html`<div class="need">${m.problem}</div>` : ""}
      </button>`)}</div>
    <div id="sys-extras"></div>
    <div class="card-head" style="margin-top:26px"><h2>Keys &amp; connections</h2><p class="sub">Saved to .env on this computer. Keys are never shown again, only their last 4 characters.</p></div>
    <div class="keys">${Object.entries(settingsList.reduce((g, s) => ((g[s.group] = g[s.group] || []).push(s), g), {})).map(([group, items]) => html`
      <div class="card" data-group="${group}">
        <div class="card-head"><h2>${group}</h2><button class="btn sm primary" data-savegroup="${group}">${icon.check}Save</button></div>
        ${items.map((s) => html`<div class="frow"><span class="flabel">${s.label}</span><span class="finput">
          ${s.secret
            ? html`<span style="display:flex;gap:8px"><input type="password" autocomplete="off" data-key="${s.key}" data-secret placeholder="${s.set ? `Set (${s.hint}). Type to replace` : "Not set"}">${s.set ? html`<button class="btn ghost sm" data-clear="${s.key}">Clear</button>` : ""}</span>`
            : html`<input type="text" data-key="${s.key}" value="${s.value}" placeholder="${((s.help.match(/Default (\S+)/) || [])[1] || "").replace(/[.;,)]+$/, "")}">`}
          <small>${s.help} <span class="mono faint">${s.key}</span></small></span></div>`)}
      </div>`)}</div>
    <div class="grid g2" style="margin-top:16px">
      <div class="card">
        <div class="card-head"><h2>Model details</h2>${system.problem ? html`<span class="chip s-stopped"><i></i>Needs setup</span>` : html`<span class="chip s-submitted"><i></i>Ready</span>`}</div>
        <dl class="kv">
          <dt>Backend</dt><dd>${{ clef: "Cloudflare Clef-Flash 9B (local, llama-server)", laya: "Laya 441M (local, in the batch process)", typesafe: "TypeSafe Jev (hosted API)", llm: "Chat model (OpenAI-compatible API)" }[system.backend] || system.backend}</dd>
          ${c ? html`<dt>Server</dt><dd>${c.running ? html`<span class="chip s-submitted live"><i></i>Running</span>` : html`<span class="chip s-deferred"><i></i>Starts with each batch</span>`} <span class="mono faint">${c.base}</span></dd>
            <dt>Model file</dt><dd class="mono" style="font-size:12.5px">${c.path}</dd>
            <dt>Memory</dt><dd>${c.memory_free_gb != null ? html`${c.memory_free_gb} GB free of the ${c.memory_needed_gb} GB Clef needs ${c.memory_free_gb >= c.memory_needed_gb ? html`<span class="chip s-submitted"><i></i>OK</span>` : html`<span class="chip s-stopped"><i></i>Close some apps</span>`}` : "—"}</dd>` : ""}
          <dt>Drafting</dt><dd>${system.drafting === "claude-code" ? "Claude (Claude Code) writes open answers only" : system.drafting || "Off: open questions are asked"}</dd>
          ${system.problem ? html`<dt>Problem</dt><dd style="color:var(--red)">${system.problem}</dd>` : ""}
        </dl>
      </div>
      <div class="card">
        <div class="card-head"><h2>How it stays safe</h2></div>
        <ul class="muted" style="margin:0;padding-left:18px;display:grid;gap:7px">
          <li>Only <b>complete</b> applications are submitted, and never the same job twice (data/applied.json).</li>
          <li>Values come from your profile, your saved answers or drafts; a model only <b>chooses</b> among them.</li>
          <li>Unsure answers, logins and captchas are left for you; nothing is guessed.</li>
          <li>At most one application per company every 14 days.</li>
          <li>This page works only on this computer, with a session token.</li>
        </ul>
      </div>
    </div>
    <h2 style="margin:24px 0 12px">Résumé tracks</h2>
    <div class="grid g3">${tracks.map((t) => html`
      <div class="card"><div class="card-head">${trackChip(t.id)}<span class="mono faint" style="font-size:12px">${t.id}</span></div>
        <div style="font-weight:600">${t.resume || "No résumé file"}</div>
        <p class="muted" style="font-size:13px">${t.headline}</p>
        <div class="skills">${t.skills.slice(0, 18).map((s) => html`<span class="chip plain">${s}</span>`)}</div></div>`)}</div>`);
  state.tracks = tracks;
  state.system = system;
  renderSystemExtras();
  const saveSettings = async (values, what) => {
    try {
      await api("/settings", { method: "PUT", body: { values } });
      toast(`${what} saved to .env.`, "var(--green)");
      refreshShell();
      settings();
    } catch (e) { toast(e.message, "var(--red)"); }
  };
  $$("[data-savegroup]").forEach((b) => (b.onclick = () => {
    const card = b.closest("[data-group]");
    const values = {};
    $$("[data-key]", card).forEach((i) => {
      if (i.dataset.secret !== undefined) { if (i.value.trim()) values[i.dataset.key] = i.value.trim(); } // empty: keep the key you have
      else values[i.dataset.key] = i.value;
    });
    saveSettings(values, b.dataset.savegroup);
  }));
  $$("[data-clear]").forEach((b) => (b.onclick = () => {
    if (confirm(`Remove ${b.dataset.clear} from .env?`)) saveSettings({ [b.dataset.clear]: "" }, b.dataset.clear);
  }));
  $$("#models .model").forEach((b) => (b.onclick = () => {
    const m = models.find((x) => x.id === b.dataset.id);
    if (m.current) return;
    const go = async () => {
      try {
        await api("/models", { method: "POST", body: { id: m.id } });
        toast(`${m.name} is now the decision model. The next batch uses it.`, "var(--green)");
        refreshShell();
        settings();
      } catch (e) { toast(e.message, "var(--red)"); }
    };
    if (!m.problem) return go();
    modal(html`<h2 style="font-size:18px">Switch to ${m.name}?</h2>
      <p class="muted">It isn't ready yet: <b>${m.problem}</b>. Batches will stop before opening any form until that's fixed.</p>
      <div class="actions"><button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="sw">Switch anyway</button></div>`,
      (el, close) => ($("#sw", el).onclick = () => { close(); go(); }));
  }));
}

// ---- profile --------------------------------------------------------------------------------------------------
// A form generated from the profile's own shape: scalars become inputs, {value, about} a field with its help,
// lists of words tags, lists of objects cards, number tables (skill_years) editable rows, "_note" keys help text.

const P = { track: null, data: null, saved: null, apply: {}, open: null };
const TRACK_ONLY = new Set(["_readme", "headline", "skills", "experience", "documents"]);
const SECTION_NAMES = {
  personal: "Personal", links: "Links", headline: "Headline", work: "Work", compensation: "Compensation",
  preferences: "Preferences", work_authorization: "Work authorization", application: "Application",
  education: "Education", school: "School", academics: "Academics", experience: "Experience", skills: "Skills",
  skill_years: "Years per skill", languages: "Languages", certifications: "Certifications",
  self_identification: "Self-identification", documents: "Documents",
};
const humanize = (k) => String(k).replace(/_/g, " ").replace(/\b(ctc|lpa|url|id)\b/gi, (m) => m.toUpperCase()).replace(/^./, (c) => c.toUpperCase());
const isVA = (v) => v && typeof v === "object" && !Array.isArray(v) && "value" in v && Object.keys(v).every((k) => ["value", "about"].includes(k));
const isTable = (v) => v && typeof v === "object" && !Array.isArray(v) && Object.entries(v).filter(([k]) => !k.startsWith("_")).length > 3
  && Object.entries(v).every(([k, x]) => k.startsWith("_") || typeof x === "number");
const getAt = (obj, path) => path.reduce((o, k) => (o == null ? o : o[k]), obj);
function setAt(obj, path, value) {
  let o = obj;
  path.slice(0, -1).forEach((k) => (o = o[k]));
  o[path[path.length - 1]] = value;
}
const pathAttr = (path) => JSON.stringify(path); // `html` escapes it once for the attribute
const dirty = () => P.data && JSON.stringify(P.data) !== P.saved;

async function profilePage(arg) {
  if (dirty() && P.track && arg && decodeURIComponent(arg) !== P.track && !confirm("Leave without saving your changes?")) return;
  state.tracks = state.tracks.length ? state.tracks : await api("/tracks");
  const track = arg ? decodeURIComponent(arg) : P.track || state.tracks[0]?.id;
  if (!track) return mount(main, html`<div class="card empty">No profile found in data/.</div>`);
  if (track !== P.track || !P.data) {
    const res = await api(`/profile?track=${encodeURIComponent(track)}`);
    Object.assign(P, { track, data: res.profile, saved: JSON.stringify(res.profile), path: res.path, docs: res.documents, apply: {} });
  }
  drawProfile();
}

function drawProfile() {
  const d = P.data;
  const sections = [...new Set([...Object.keys(d).filter((k) => !k.startsWith("_")), "documents"])]; // always a place for the résumé
  P.open = P.open && sections.includes(P.open) ? P.open : sections[0];
  mount(main, html`
    <div class="page-head"><div><h1>Profile</h1><p class="sub">What every form is filled from. <span class="mono faint">${P.path}</span></p></div>
      <div style="display:flex;gap:8px;align-items:center">
        <div class="seg" id="ptracks">${state.tracks.map((t) => html`<button data-t="${t.id}" class="${t.id === P.track ? "on" : ""}">${trackName(t.id)}</button>`)}</div>
        <button class="btn sm" id="newtrack" title="A new résumé track, e.g. for embedded or data roles">+ New track</button></div></div>
    ${d._readme ? html`<p class="muted" style="margin:-8px 0 16px;font-size:13px">${d._readme}</p>` : ""}
    <div class="profile">
      <nav class="psections">${sections.map((k) => html`<button data-s="${k}" class="${k === P.open ? "on" : ""}">${SECTION_NAMES[k] || humanize(k)}${TRACK_ONLY.has(k) ? html`<span class="faint" title="Belongs to this résumé only"> ·</span>` : ""}</button>`)}
        <button class="add-section" id="addsection">+ Add section</button></nav>
      <div class="card psection">
        <div class="card-head"><h2>${SECTION_NAMES[P.open] || humanize(P.open)}</h2>
          <div style="display:flex;gap:8px;align-items:center">${TRACK_ONLY.has(P.open) ? html`<span class="chip plain"><i></i>This résumé only</span>` : html`<span class="chip plain"><i></i>Shared fact</span>`}
            ${P.open !== "documents" ? html`<button class="btn ghost sm" id="delsection" title="Remove this section">${icon.x}</button>` : ""}</div></div>
        <div class="pform">${P.open === "documents" ? documentsEditor() : editor([P.open], d[P.open], P.open)}</div>
      </div>
    </div>
    <div id="psave"></div>`);
  wireProfile();
  drawSaveBar();
}

function editor(path, value, key) {
  const del = path.length > 1 ? path : null; // a section itself is removed from its header
  if (isVA(value)) return fieldRow(path.concat("value"), value.value, key, value.about, del);
  if (isTable(value)) return tableEditor(path, value);
  if (Array.isArray(value)) {
    if (value.every((x) => x === null || typeof x !== "object")) return fieldRow(path, value, key, undefined, del);
    return html`<div class="items">${value.map((item, i) => html`
      <div class="item"><div class="item-head"><b>${item.title || item.degree || item.company || item.institution || `#${i + 1}`}${item.company && item.title ? html` <span class="muted">· ${item.company}</span>` : ""}</b>
        <button class="btn ghost sm" data-remove="${pathAttr(path.concat(i))}">${icon.x}Remove</button></div>
        ${editor(path.concat(i), item, null)}</div>`)}
      <button class="btn sm" data-additem="${pathAttr(path)}">+ Add ${humanize(key || "item").replace(/s$/, "")}</button></div>`;
  }
  if (value && typeof value === "object") {
    return html`<div class="group">${Object.entries(value).map(([k, v]) =>
      k.startsWith("_") ? html`<div class="note">${v}</div>`
      : v && typeof v === "object" && !isVA(v) && !Array.isArray(v) && !isTable(v)
        ? html`<fieldset><legend>${humanize(k)}<button class="legend-x" data-delfield="${pathAttr(path.concat(k))}" title="Remove ${humanize(k)}">×</button></legend>${editor(path.concat(k), v, k)}</fieldset>`
        : editor(path.concat(k), v, k))}
      <button class="btn ghost sm addfield" data-addfield="${pathAttr(path)}">+ Add field</button></div>`;
  }
  return fieldRow(path, value, key, undefined, del);
}

function fieldRow(path, value, key, help, del) {
  const label = humanize(key ?? path[path.length - 2] ?? "");
  const p = pathAttr(path);
  let input;
  if (typeof value === "boolean") input = html`<label class="toggle"><input type="checkbox" data-path="${p}" data-type="bool" ${value ? "checked" : ""}>${value ? "Yes" : "No"}</label>`;
  else if (typeof value === "number") input = html`<input type="number" step="any" data-path="${p}" data-type="number" value="${value}">`;
  else if (Array.isArray(value)) input = html`<div class="tags" data-tags="${p}">${value.map((w, i) => html`<span class="chip">${w}<button data-deltag="${i}" title="Remove">×</button></span>`)}<input type="text" placeholder="Add, then Enter" data-addtag></div>`;
  else if (String(value ?? "").length > 90 || /summary|about|note/.test(String(key))) input = html`<textarea data-path="${p}" data-type="text" rows="4">${value ?? ""}</textarea>`;
  else input = html`<input type="text" data-path="${p}" data-type="${value === null ? "null" : "text"}" value="${value ?? ""}" placeholder="${value === null ? "empty" : ""}">`;
  return html`<div class="frow"><span class="flabel">${label}</span><span class="finput">${input}${help ? html`<small>${help}</small>` : ""}</span>
    ${del ? html`<button class="btn ghost sm fdel" data-delfield="${pathAttr(del)}" title="Remove this field">${icon.x}</button>` : html`<span></span>`}</div>`;
}

function documentsEditor() {
  const docs = P.docs || {};
  const kinds = Object.keys(docs);
  const size = (n) => (n == null ? "" : n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1e3)} KB`);
  return html`<div class="docs">
    ${kinds.length ? "" : html`<div class="banner warn">This track has no résumé yet: batches can't attach one. Upload it below.</div>`}
    ${kinds.map((k) => { const d = docs[k]; return html`
      <div class="doc">
        <div class="doc-icon">${k === "resume" ? "CV" : k.slice(0, 2).toUpperCase()}</div>
        <div style="min-width:0;flex:1">
          <div style="font-weight:600">${k === "resume" ? "Résumé" : humanize(k)} ${d.exists ? html`<span class="chip s-submitted"><i></i>On disk · ${size(d.size)}</span>` : html`<span class="chip s-stopped"><i></i>File missing</span>`}</div>
          <div class="mono muted" style="font-size:12px;overflow:hidden;text-overflow:ellipsis">${d.path}</div>
          <div class="muted" style="font-size:12.5px">${d.about}</div>
        </div>
        <button class="btn sm" data-upload="${k}">Replace file</button>
        <button class="btn ghost sm" data-deldoc="${k}" title="Stop using this document">${icon.x}</button>
      </div>`; })}
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      ${docs.resume ? (docs.resume.exists ? html`<button class="btn primary" id="learn-cv">${icon.spark}Draft profile from this résumé</button>` : "") : html`<button class="btn primary" data-upload="resume">Upload résumé</button>`}
      <button class="btn" data-upload="">+ Add another document (cover letter…)</button>
    </div>
    <p class="muted" style="font-size:12.5px">PDF, Word or text, up to 15 MB. Files are kept in ${P.track}/documents/; the agent attaches the résumé to every application on this track.</p>
    <input type="file" id="docfile" accept=".pdf,.docx,.doc,.txt,.md" hidden>
  </div>`;
}

async function uploadDocument(kind) {
  if (dirty()) return toast("Save or undo your other changes first, then upload.", "var(--amber)");
  if (!kind) {
    const name = prompt("What is this document? (e.g. cover_letter, portfolio)", "cover_letter");
    if (!name) return;
    kind = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  }
  const input = $("#docfile");
  input.value = "";
  input.onchange = async () => {
    const file = input.files[0];
    if (!file) return;
    try {
      const res = await fetch(`/api/documents?track=${encodeURIComponent(P.track)}&kind=${encodeURIComponent(kind)}&filename=${encodeURIComponent(file.name)}`,
        { method: "PUT", headers: { "X-Jev-Token": TOKEN }, body: await file.arrayBuffer() });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      await reloadProfile();
      toast(`${file.name} is now this track's ${kind === "resume" ? "résumé" : humanize(kind)}.`, "var(--green)");
      state.tracks = await api("/tracks");
    } catch (e) { toast(e.message, "var(--red)"); }
  };
  input.click();
}

async function reloadProfile() {
  const res = await api(`/profile?track=${encodeURIComponent(P.track)}`);
  Object.assign(P, { data: res.profile, saved: JSON.stringify(res.profile), docs: res.documents });
  drawProfile();
}

function addFieldModal(path) {
  modal(html`<h2 style="font-size:18px">Add a field</h2>
    <p class="muted" style="font-size:13px">Anything a form might ask: a portfolio link, a second phone, a licence. The description helps the decision model know when to use it.</p>
    <div style="display:grid;gap:12px;margin-top:12px">
      <label class="field"><span>Name</span><input type="text" id="nf-name" placeholder="e.g. portfolio, driving licence"></label>
      <label class="field"><span>Kind</span><select id="nf-kind"><option value="text">Text</option><option value="number">Number</option><option value="bool">Yes / No</option><option value="list">List of words</option><option value="group">Group of fields</option></select></label>
      <label class="field" id="nf-value-row"><span>Value</span><input type="text" id="nf-value"></label>
      <label class="field"><span>Description <span class="faint">(optional, recommended)</span></span><input type="text" id="nf-about" placeholder="e.g. Portfolio URL: my GitHub, for 'portfolio' or 'work samples' fields"></label>
    </div>
    <div class="actions"><button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="nf-add">Add</button></div>`,
  (el, close) => {
    const kind = $("#nf-kind", el);
    kind.onchange = () => { $("#nf-value-row", el).hidden = kind.value === "group"; $("#nf-value", el).type = kind.value === "number" ? "number" : "text"; };
    $("#nf-name", el).focus();
    $("#nf-add", el).onclick = () => {
      const name = $("#nf-name", el).value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
      const parent = getAt(P.data, path);
      if (!name) return toast("Give the field a name.", "var(--amber)");
      if (name in parent) return toast(`There's already a field called ${humanize(name)}.`, "var(--amber)");
      const raw = $("#nf-value", el).value;
      const about = $("#nf-about", el).value.trim();
      let v = { text: raw, number: raw === "" ? null : Number(raw), bool: /^(y|yes|true|1)$/i.test(raw), list: raw.split(",").map((x) => x.trim()).filter(Boolean), group: {} }[kind.value];
      parent[name] = about && kind.value !== "group" ? { value: v, about } : v;
      close();
      drawProfile();
    };
  });
}

function newTrackModal() {
  if (dirty()) return toast("Save or undo your changes first.", "var(--amber)");
  modal(html`<h2 style="font-size:18px">New résumé track</h2>
    <p class="muted" style="font-size:13px">For another kind of role with its own résumé (embedded, data, QA…). It starts with your shared facts (personal details, work, pay, years per skill); you add its headline, skills and résumé.</p>
    <div style="display:grid;gap:12px;margin-top:12px">
      <label class="field"><span>Name</span><input type="text" id="nt-name" placeholder="e.g. embedded"></label>
      <label class="field"><span>Start from</span><select id="nt-base">${state.tracks.map((t) => html`<option value="${t.id}" ${t.id === P.track ? "selected" : ""}>${trackName(t.id)}</option>`)}</select></label>
    </div>
    <div class="actions"><button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="nt-go">Create</button></div>`,
  (el, close) => {
    $("#nt-name", el).focus();
    $("#nt-go", el).onclick = async () => {
      try {
        const { track } = await api("/tracks", { method: "POST", body: { name: $("#nt-name", el).value, base: $("#nt-base", el).value } });
        close();
        state.tracks = await api("/tracks");
        state.findForm = null; // the search page picks the new track up
        P.data = null;
        P.open = "documents";
        toast(`Track ${trackName(track)} created. Add its résumé, headline and skills.`, "var(--green)");
        location.hash = `#/profile/${encodeURIComponent(track)}`;
      } catch (e) { toast(e.message, "var(--red)"); }
    };
  });
}

function tableEditor(path, value) {
  const rows = Object.entries(value);
  return html`<div class="ktable">${rows.map(([k, v]) => k.startsWith("_") ? html`<div class="note">${v}</div>` : html`
      <div class="krow"><span>${k}</span><input type="number" step="0.5" min="0" data-path="${pathAttr(path.concat(k))}" data-type="number" value="${v}"><button class="btn ghost sm" data-delkey="${pathAttr(path.concat(k))}" title="Remove">${icon.x}</button></div>`)}
    <div class="krow add"><input type="text" placeholder="Skill, e.g. Kubernetes" data-newkey><input type="number" step="0.5" min="0" placeholder="years" data-newval><button class="btn sm" data-addkey="${pathAttr(path)}">Add</button></div></div>`;
}

function wireProfile() {
  $$("#ptracks button").forEach((b) => (b.onclick = () => {
    if (dirty() && !confirm("Switch track without saving your changes?")) return;
    P.data = null;
    location.hash = `#/profile/${encodeURIComponent(b.dataset.t)}`;
  }));
  $$(".psections button[data-s]").forEach((b) => (b.onclick = () => { P.open = b.dataset.s; drawProfile(); }));
  $("#newtrack").onclick = newTrackModal;
  $("#addsection").onclick = () => {
    const name = (prompt("New section name (e.g. publications, patents, volunteering)") || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
    if (!name) return;
    if (name in P.data) return toast("That section exists already.", "var(--amber)");
    P.data[name] = {};
    P.open = name;
    drawProfile();
  };
  if ($("#delsection")) $("#delsection").onclick = () => {
    if (!confirm(`Remove the whole ${SECTION_NAMES[P.open] || humanize(P.open)} section? (Undo is available until you save.)`)) return;
    delete P.data[P.open];
    P.open = null;
    drawProfile();
  };
  $$("[data-addfield]").forEach((b) => (b.onclick = (e) => { e.preventDefault(); addFieldModal(JSON.parse(b.dataset.addfield)); }));
  $$("[data-delfield]").forEach((b) => (b.onclick = (e) => {
    e.preventDefault();
    const path = JSON.parse(b.dataset.delfield);
    const parent = getAt(P.data, path.slice(0, -1));
    Array.isArray(parent) ? parent.splice(path[path.length - 1], 1) : delete parent[path[path.length - 1]];
    drawProfile();
  }));
  $$("[data-upload]").forEach((b) => (b.onclick = () => uploadDocument(b.dataset.upload)));
  if ($("#learn-cv")) $("#learn-cv").onclick = learnFromResume;
  $$("[data-deldoc]").forEach((b) => (b.onclick = async () => {
    if (dirty()) return toast("Save or undo your other changes first.", "var(--amber)");
    if (!confirm("Stop using this document? (The file stays in the documents folder.)")) return;
    try {
      await api(`/documents?track=${encodeURIComponent(P.track)}&kind=${encodeURIComponent(b.dataset.deldoc)}`, { method: "DELETE" });
      await reloadProfile();
    } catch (e) { toast(e.message, "var(--red)"); }
  }));
  $$("[data-path]").forEach((el) => (el[el.dataset.type === "bool" ? "onchange" : "oninput"] = () => {
    const path = JSON.parse(el.dataset.path);
    const t = el.dataset.type;
    let v = t === "bool" ? el.checked : el.value;
    if (t === "number") v = el.value === "" ? null : Number(el.value);
    if (t === "null" && v === "") v = null;
    setAt(P.data, path, v);
    if (t === "bool") el.parentElement.lastChild.textContent = v ? "Yes" : "No";
    drawSaveBar();
  }));
  $$("[data-tags]").forEach((box) => {
    const path = JSON.parse(box.dataset.tags);
    $$("[data-deltag]", box).forEach((b) => (b.onclick = (e) => { e.preventDefault(); getAt(P.data, path).splice(+b.dataset.deltag, 1); drawProfile(); }));
    const add = $("[data-addtag]", box);
    add.onkeydown = (e) => {
      if (e.key !== "Enter" || !add.value.trim()) return;
      e.preventDefault();
      getAt(P.data, path).push(add.value.trim());
      drawProfile();
      $(`[data-tags="${CSS.escape(box.dataset.tags)}"] [data-addtag]`)?.focus();
    };
  });
  $$("[data-remove]").forEach((b) => (b.onclick = (e) => {
    e.preventDefault();
    const path = JSON.parse(b.dataset.remove);
    getAt(P.data, path.slice(0, -1)).splice(path[path.length - 1], 1);
    drawProfile();
  }));
  $$("[data-additem]").forEach((b) => (b.onclick = (e) => {
    e.preventDefault();
    const list = getAt(P.data, JSON.parse(b.dataset.additem));
    const blank = (x) => (Array.isArray(x) ? [] : x && typeof x === "object" ? Object.fromEntries(Object.entries(x).map(([k, v]) => [k, blank(v)])) : typeof x === "number" ? null : typeof x === "boolean" ? false : "");
    list.push(list.length ? blank(list[0]) : {});
    drawProfile();
  }));
  $$("[data-delkey]").forEach((b) => (b.onclick = (e) => {
    e.preventDefault();
    const path = JSON.parse(b.dataset.delkey);
    delete getAt(P.data, path.slice(0, -1))[path[path.length - 1]];
    drawProfile();
  }));
  $$("[data-addkey]").forEach((b) => (b.onclick = (e) => {
    e.preventDefault();
    const row = b.closest(".krow");
    const k = $("[data-newkey]", row).value.trim();
    const v = $("[data-newval]", row).value;
    if (!k || v === "") return toast("Give the skill and its years.", "var(--amber)");
    getAt(P.data, JSON.parse(b.dataset.addkey))[k] = Number(v);
    drawProfile();
  }));
}

function changedSections() {
  const before = JSON.parse(P.saved);
  return Object.keys({ ...before, ...P.data }).filter((k) => JSON.stringify(before[k]) !== JSON.stringify(P.data[k]));
}

function drawSaveBar() {
  const slot = $("#psave");
  $("#nav-dirty").hidden = !dirty();
  if (!slot) return;
  if (!dirty()) return (slot.innerHTML = "");
  const changed = changedSections();
  const shared = changed.filter((k) => !TRACK_ONLY.has(k));
  const others = state.tracks.filter((t) => t.id !== P.track);
  mount(slot, html`<div class="dock">
    <b>Unsaved: ${changed.map((k) => SECTION_NAMES[k] || humanize(k)).join(", ")}</b>
    ${shared.length && others.length ? html`<span class="muted">Also save shared facts to</span>${others.map((t) => html`<label class="toggle"><input type="checkbox" data-apply="${t.id}" ${P.apply[t.id] ? "checked" : ""}>${trackName(t.id)}</label>`)}` : ""}
    <button class="btn ghost" id="pundo">Undo</button>
    <button class="btn primary" id="psave-go">${icon.check}Save</button></div>`);
  $$("[data-apply]", slot).forEach((c) => (c.onchange = () => (P.apply[c.dataset.apply] = c.checked)));
  $("#pundo").onclick = () => { P.data = JSON.parse(P.saved); drawProfile(); };
  $("#psave-go").onclick = async () => {
    try {
      const apply_to = Object.keys(P.apply).filter((k) => P.apply[k]);
      const res = await api(`/profile?track=${encodeURIComponent(P.track)}`, { method: "PUT", body: { profile: P.data, apply_to } });
      P.saved = JSON.stringify(P.data);
      const extra = Object.keys(res.saved).filter((t) => t !== P.track && res.saved[t].length);
      toast(`Saved ${trackName(P.track)}${extra.length ? ` and ${extra.map(trackName).join(", ")}` : ""}. The old version is in .backups/.`, "var(--green)");
      drawSaveBar();
      refreshShell();
    } catch (e) { toast(e.message, "var(--red)"); }
  };
}

window.addEventListener("beforeunload", (e) => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } });

// ---- inbox: everything that needs you ------------------------------------------------------------------------

async function inboxPage() {
  const [q, rerun] = await Promise.all([api("/queue"), api("/rerun")]);
  const tab = state.inboxTab || "all";
  const shown = q.items.filter((i) => tab === "all" || i.kind === tab);
  const jobsN = q.items.filter((i) => i.kind === "job").length;
  const draftsN = q.items.filter((i) => i.kind === "draft").length;
  const short = (u) => String(u || "").replace(/^https?:\/\/(www\.)?/, "").split("?")[0].slice(0, 60);
  mount(main, html`
    <div class="page-head"><div><h1>Inbox</h1><p class="sub">Everything a run left for you, in one place. Clear an item once you've handled it.</p></div>
      <div class="seg" id="itab">${[["all", "All", q.items.length], ["job", "Jobs to finish", jobsN], ["draft", "Drafts to read", draftsN]].map(([k, l, n]) => html`<button data-t="${k}" class="${tab === k ? "on" : ""}">${l} <span class="faint">${n}</span></button>`)}</div></div>
    ${rerunBanner(rerun.jobs)}
    ${q.open_questions ? html`<a class="banner warn" href="#/questions" style="text-decoration:none">${icon.chat}<b>${q.open_questions} open question${q.open_questions > 1 ? "s" : ""}</b><span class="muted">Answer them once and every later form fills them →</span></a>` : ""}
    <div class="qs">${shown.length ? shown.map((i) => i.kind === "job" ? html`
      <div class="card inbox-item">
        <div class="card-head"><div style="min-width:0"><div style="font-weight:600">${short(i.url)}</div><div class="meta muted" style="font-size:12.5px">${ago(i.at)} ${trackChip(i.track)}</div></div>${statusChip(i.status)}</div>
        ${i.reason ? html`<div class="needs" style="margin-bottom:8px">${i.reason}</div>` : ""}
        ${i.left.length ? html`<ul class="left">${i.left.map((x) => html`<li>${x}</li>`)}</ul>` : ""}
        <div class="actions-row"><a class="btn sm" href="${safeUrl(i.url)}" target="_blank" rel="noopener">${icon.link}Open job</a>
          <button class="btn sm" data-rerun="${i.url}">${icon.play}Run again</button>
          <button class="btn sm ghost" data-run="${i.run}">Report</button>
          <button class="btn sm primary" data-done="${i.id}" style="margin-left:auto">${icon.check}Done</button></div>
      </div>` : html`
      <div class="card inbox-item">
        <div class="card-head"><div style="min-width:0"><div class="muted" style="font-size:12px">Drafted answer · ${short(i.url)} · ${ago(i.at)}</div><div style="font-weight:600">${i.title}</div></div>
          ${i.sent ? html`<span class="chip s-submitted"><i></i>Sent with the application</span>` : html`<span class="chip s-review"><i></i>Waiting for you</span>`}</div>
        <blockquote class="draft">${i.text}</blockquote>
        <div class="actions-row"><a class="btn sm" href="${safeUrl(i.url)}" target="_blank" rel="noopener">${icon.link}Open job</a>
          <button class="btn sm primary" data-done="${i.id}" style="margin-left:auto">${icon.check}Read</button></div>
      </div>`) : html`<div class="card empty">${icon.check}Nothing waiting on you.</div>`}</div>`);
  $$("#itab button").forEach((b) => (b.onclick = () => { state.inboxTab = b.dataset.t; inboxPage(); }));
  $$("[data-done]").forEach((b) => (b.onclick = async () => { await api(`/queue/${encodeURIComponent(b.dataset.done)}`, { method: "POST", body: {} }); refreshShell(); inboxPage(); }));
  wireRerun(rerun.jobs);
  $$("[data-rerun]").forEach((b) => (b.onclick = () => confirmBatch([b.dataset.rerun], "Run this job again?")));
  $$("[data-run]").forEach((b) => (b.onclick = () => openRun(b.dataset.run)));
}

// ---- insights: results by résumé track ---------------------------------------------------------------------

async function insightsPage() {
  const [d, cu] = await Promise.all([api("/insights"), api("/claude-usage")]);
  const color = (t) => `var(--${trackKey(t) in TRACK_NAMES ? trackKey(t) : "gray"})`;
  const weeks = Object.entries(d.weeks);
  const maxWeek = Math.max(1, ...weeks.map(([, w]) => Object.values(w).reduce((a, b) => a + b, 0)));
  const outcomeColor = { submitted: "var(--green)", review: "var(--amber)", stopped: "var(--red)", failed: "var(--red)", ready: "var(--amber)" };
  mount(main, html`
    <div class="page-head"><div><h1>Insights</h1><p class="sub">${d.total_runs} runs · ${d.total_sent} applications sent. Which résumé works, and why runs stop.</p></div></div>
    <div class="grid g3">${d.tracks.map((t) => html`
      <div class="card">
        <div class="card-head">${trackChip(t.track === "unknown" ? null : t.track)}${t.track === "unknown" ? html`<span class="muted" style="font-size:12px">older runs, résumé not recorded</span>` : ""}</div>
        <div style="display:flex;align-items:baseline;gap:10px"><span class="kpi-big">${Math.round(t.rate * 100)}%</span><span class="muted">of ${t.runs} runs submitted</span></div>
        <div class="split" style="margin:12px 0">${Object.entries(t.outcomes).map(([k, n]) => html`<span title="${k}: ${n}" style="flex:${n};--c:${outcomeColor[k] || "var(--gray)"}"></span>`)}</div>
        <div class="legend" style="font-size:12.5px">${Object.entries(t.outcomes).map(([k, n]) => html`<div><i style="--c:${outcomeColor[k] || "var(--gray)"}"></i>${STATUS[statusKey(k)] || k}<b>${n}</b></div>`)}</div>
        ${t.avg_seconds ? html`<p class="muted" style="font-size:12.5px;margin:12px 0 0">About ${Math.round(t.avg_seconds / 60 * 10) / 10} min per job</p>` : ""}
        ${t.reasons.length ? html`<div style="margin-top:12px"><div class="muted" style="font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.06em">Why runs stopped</div>
          <ul class="left">${t.reasons.map(([r, n]) => html`<li>${r} <span class="faint">×${n}</span></li>`)}</ul></div>` : ""}
      </div>`)}</div>
    <div class="grid g2" style="margin-top:16px">
      <div class="card"><div class="card-head"><h2>Applications per week</h2><p class="sub">by résumé</p></div>
        <div class="bars">${weeks.map(([wk, w]) => html`<div class="bar"><div class="stack" style="height:${(Object.values(w).reduce((a, b) => a + b, 0) / maxWeek) * 100}%">${Object.entries(w).map(([t, n]) => html`<span title="${trackName(t)}: ${n}" style="flex:${n};background:${color(t)}"></span>`)}</div><span class="day">${new Date(wk).toLocaleDateString(undefined, { day: "numeric", month: "short" })}</span></div>`)}</div></div>
      <div class="card"><div class="card-head"><h2>Companies</h2><p class="sub">most applied to</p></div>
        <div class="list">${d.companies.map(([c, n]) => html`<div class="row"><span>${c}</span><b>${n}</b></div>`)}</div></div>
    </div>
    <div class="card-head" style="margin-top:26px"><h2>Claude</h2><p class="sub">Last ${cu.days} days, from runs/claude-usage.jsonl: every call jev-apply made to Claude (drafting answers, reading résumés, the benchmark judge).</p></div>
    <div class="grid g4">
      <div class="card kpi"><div class="label">Calls</div><div class="value">${cu.total.calls}</div><div class="hint">${cu.total.failed} failed</div></div>
      <div class="card kpi"><div class="label">Average time</div><div class="value">${cu.total.avg_seconds != null ? html`${cu.total.avg_seconds}<span style="font-size:16px">s</span>` : "—"}</div><div class="hint">per call</div></div>
      <div class="card kpi"><div class="label">Tokens</div><div class="value" style="font-size:24px">${(cu.total.input_tokens / 1000).toFixed(1)}k <span class="faint" style="font-size:14px">in</span> · ${(cu.total.output_tokens / 1000).toFixed(1)}k <span class="faint" style="font-size:14px">out</span></div><div class="hint">${(cu.total.cache_read_tokens / 1000).toFixed(1)}k read from cache</div></div>
      <div class="card kpi"><div class="label">Cost</div><div class="value">$${cu.total.cost_usd.toFixed(2)}</div><div class="hint">as Claude Code reports it (your subscription covers it)</div></div>
    </div>
    <div class="grid g2" style="margin-top:16px">
      <div class="card"><div class="card-head"><h2>By stage</h2></div>
        ${Object.keys(cu.stages).length ? html`<table class="data"><thead><tr><th>Stage</th><th>Calls</th><th>Avg time</th><th>Tokens in / out</th><th>Cost</th></tr></thead><tbody>${Object.entries(cu.stages).map(([k, v]) => html`<tr><td style="font-weight:600">${STAGE_NAMES[k] || k}</td><td>${v.calls}${v.failed ? html` <span class="faint">(${v.failed} failed)</span>` : ""}</td><td>${v.avg_seconds ?? "—"}s</td><td class="mono">${v.input_tokens} / ${v.output_tokens}</td><td class="mono">$${v.cost_usd.toFixed(3)}</td></tr>`)}</tbody></table>` : html`<div class="empty">No Claude calls logged yet. Every call is recorded from now on.</div>`}</div>
      <div class="card"><div class="card-head"><h2>Latest calls</h2></div>
        <div class="list">${cu.recent.slice(0, 10).map((c) => html`<div class="row"><div style="min-width:0"><div class="title" style="font-size:13px">${STAGE_NAMES[c.stage] || c.stage}${c.ok === false ? html` <span class="chip s-stopped"><i></i>${String(c.reason || "failed").slice(0, 50)}</span>` : ""}</div><div class="meta">${ago(c.at)} · ${c.latency_ms ? (c.latency_ms / 1000).toFixed(1) + "s" : "—"} · ${c.input_tokens ?? 0}/${c.output_tokens ?? 0} tokens</div></div><span class="mono faint">${c.cost_usd != null ? "$" + c.cost_usd.toFixed(4) : ""}</span></div>`)}
        ${cu.recent.length ? "" : html`<div class="empty">Nothing yet.</div>`}</div></div>
    </div>`);
}

// ---- system extras: Clef controls, safety settings, autopilot -----------------------------------------------

async function renderSystemExtras() {
  const slot = $("#sys-extras");
  if (!slot) return;
  const [clef, auto] = await Promise.all([state.system?.backend === "clef" ? api("/clef") : null, api("/autopilot")]);
  const track = state.safetyTrack || state.tracks[0]?.id;
  const safety = track ? await api(`/safety?track=${encodeURIComponent(track)}`) : [];
  const gpu = clef?.gpu;
  const c = auto.config;
  mount(slot, html`
    ${clef ? html`<div class="card" style="margin-top:16px" id="clef-card">
      <div class="card-head"><h2>Clef server</h2><div style="display:flex;gap:8px;align-items:center">
        <span class="chip ${{ running: "s-submitted live", starting: "s-running live", stopped: "s-deferred" }[clef.state]}"><i></i>${{ running: "Loaded on the GPU", starting: "Loading…", stopped: "Not loaded" }[clef.state]}</span>
        ${clef.state === "running" ? html`<button class="btn sm danger" id="clef-stop">${icon.stop}Stop</button>` : html`<button class="btn sm primary" id="clef-start" ${clef.state === "starting" ? "disabled" : ""}>${icon.play}Start now</button>`}</div></div>
      ${clef.error ? html`<div class="banner warn">${clef.error}</div>` : ""}
      <div class="grid g2">
        <div><div class="meter-label"><span>GPU memory ${gpu ? html`<span class="faint">${gpu.name}</span>` : ""}</span><b>${gpu ? `${(gpu.used_mb / 1024).toFixed(1)} / ${(gpu.total_mb / 1024).toFixed(1)} GB` : "no NVIDIA GPU found"}</b></div>
          <div class="meter"><span style="width:${gpu ? (gpu.used_mb / gpu.total_mb) * 100 : 0}%"></span></div></div>
        <div><div class="meter-label"><span>Memory Windows can spare</span><b>${clef.memory_free_gb ?? "?"} GB · Clef needs ${clef.memory_needed_gb ?? "?"}</b></div>
          <div class="meter ${clef.memory_free_gb != null && clef.memory_free_gb < clef.memory_needed_gb ? "bad" : ""}"><span style="width:${clef.memory_free_gb && clef.memory_needed_gb ? Math.min(100, (clef.memory_free_gb / clef.memory_needed_gb) * 100) : 0}%"></span></div></div>
      </div>
      <p class="muted" style="font-size:12.5px;margin:12px 0 0">Starting it here keeps Clef loaded between batches (each batch then starts instantly). Batches also start it themselves.</p>
    </div>` : ""}
    <div class="card" style="margin-top:16px">
      <div class="card-head"><div><h2>Safety settings</h2><p class="sub">Per résumé track (data/&lt;track&gt;/policy.json). Batches started from the app always run with auto-submit on or off as you choose when starting; the terminal uses these.</p></div>
        <div class="seg" id="saf-track">${state.tracks.map((t) => html`<button data-t="${t.id}" class="${t.id === track ? "on" : ""}">${trackName(t.id)}</button>`)}</div></div>
      <div class="safety">${safety.map((s) => html`<div class="frow"><span class="flabel">${s.label}${s.custom ? html` <span class="chip plain" style="font-size:10.5px">changed</span>` : ""}</span><span class="finput">
        ${typeof s.default === "boolean" ? html`<label class="toggle"><input type="checkbox" data-saf="${s.field}" ${s.value ? "checked" : ""}>${s.value ? "On" : "Off"}</label>`
          : s.field === "drafts" ? html`<select data-saf="${s.field}"><option value="confirm" ${s.value === "confirm" ? "selected" : ""}>Draft with Claude</option><option value="ask" ${s.value === "ask" ? "selected" : ""}>Always ask me</option></select>`
          : html`<input type="number" step="${String(s.default).includes(".") ? "0.05" : "1"}" min="0" ${String(s.default).includes(".") ? 'max="1"' : ""} data-saf="${s.field}" value="${s.value}">`}
        <small>${s.help} Default: ${String(s.default)}.</small></span><span></span></div>`)}</div>
      <div style="display:flex;justify-content:flex-end;margin-top:12px"><button class="btn primary" id="saf-save">${icon.check}Save safety settings</button></div>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="card-head"><div><h2>Autopilot</h2><p class="sub">Every day at a set time: run your saved role search, take the good fits (newest first, then best match) and apply. Your PC must be on and Chrome able to open (Windows Task Scheduler runs it).</p></div>
        ${auto.scheduled ? html`<span class="chip s-submitted"><i></i>Scheduled · next ${auto.scheduled.next_run || ""}</span>` : html`<span class="chip s-deferred"><i></i>Off</span>`}</div>
      <div class="safety">
        <div class="frow"><span class="flabel">On</span><span class="finput"><label class="toggle"><input type="checkbox" id="ap-on" ${c.enabled ? "checked" : ""}>${c.enabled ? "On" : "Off"}</label></span><span></span></div>
        <div class="frow"><span class="flabel">Time</span><span class="finput"><input type="time" id="ap-time" value="${c.time}" style="max-width:160px"></span><span></span></div>
        <div class="frow"><span class="flabel">Tracks</span><span class="finput"><span style="display:flex;gap:14px;flex-wrap:wrap">${state.tracks.map((t) => html`<label class="toggle"><input type="checkbox" data-ap-track="${t.id}" ${!c.tracks.length || c.tracks.includes(t.id) ? "checked" : ""}>${trackName(t.id)}</label>`)}</span></span><span></span></div>
        <div class="frow"><span class="flabel">Jobs per day (max)</span><span class="finput"><input type="number" id="ap-max" min="1" max="50" value="${c.max_jobs}" style="max-width:120px"></span><span></span></div>
        <div class="frow"><span class="flabel">Minimum match score</span><span class="finput"><input type="number" id="ap-min" min="0" max="100" value="${c.min_score}" style="max-width:120px"><small>0-100, as shown in Find jobs. Weaker matches are skipped even if there's room.</small></span><span></span></div>
        <div class="frow"><span class="flabel">Look at jobs posted</span><span class="finput"><select id="ap-posted" style="max-width:200px"><option value="day" ${c.posted === "day" ? "selected" : ""}>In the last 24 hours</option><option value="week" ${c.posted === "week" ? "selected" : ""}>In the last week</option></select></span><span></span></div>
        <div class="frow"><span class="flabel">Auto-submit</span><span class="finput"><label class="toggle"><input type="checkbox" id="ap-submit" ${c.submit ? "checked" : ""}>${c.submit ? "On" : "Off: fill and leave for me"}</label><small>Autopilot runs the saved search you mark "Use for autopilot" in Find jobs (its roles, locations, sources and filters), else each résumé's suggested roles. Jobs posted in the last 24 hours are taken first.</small></span><span></span></div>
      </div>
      ${auto.last ? html`<div class="banner" style="margin-top:12px">Last run ${ago(auto.last.started)}: ${auto.last.found} found, ${auto.last.fits} good fits, ${auto.last.picked.length} picked${auto.last.submitted ? `, ${auto.last.submitted.length} submitted` : ""}${auto.last.dry ? " (preview only)" : ""}.</div>` : ""}
      <div style="display:flex;justify-content:flex-end;margin-top:12px"><button class="btn primary" id="ap-save">${icon.check}Save autopilot</button></div>
    </div>`);
  if ($("#clef-start")) $("#clef-start").onclick = async () => {
    await api("/clef/start", { method: "POST" });
    toast("Loading Clef onto the GPU…");
    const poll = setInterval(async () => {
      const st = await api("/clef").catch(() => null);
      if (!location.hash.startsWith("#/settings") || !st || st.state !== "starting") { clearInterval(poll); renderSystemExtras(); refreshShell(); }
    }, 2500);
    renderSystemExtras();
  };
  if ($("#clef-stop")) $("#clef-stop").onclick = async () => { try { await api("/clef/stop", { method: "POST" }); toast("Clef stopped."); renderSystemExtras(); } catch (e) { toast(e.message, "var(--red)"); } };
  $$("#saf-track button").forEach((b) => (b.onclick = () => { state.safetyTrack = b.dataset.t; renderSystemExtras(); }));
  $$("[data-saf]").forEach((i) => { if (i.type === "checkbox") i.onchange = () => (i.parentElement.lastChild.textContent = i.checked ? "On" : "Off"); });
  $("#saf-save").onclick = async () => {
    const values = {};
    $$("[data-saf]").forEach((i) => (values[i.dataset.saf] = i.type === "checkbox" ? i.checked : i.tagName === "SELECT" ? i.value : Number(i.value)));
    try { await api(`/safety?track=${encodeURIComponent(track)}`, { method: "PUT", body: { values } }); toast(`Safety settings saved for ${trackName(track)}.`, "var(--green)"); renderSystemExtras(); }
    catch (e) { toast(e.message, "var(--red)"); }
  };
  $("#ap-save").onclick = async () => {
    const tracks = $$("[data-ap-track]").filter((i) => i.checked).map((i) => i.dataset.apTrack);
    const values = { enabled: $("#ap-on").checked, time: $("#ap-time").value, tracks, max_jobs: Number($("#ap-max").value), min_score: Number($("#ap-min").value), posted: $("#ap-posted").value, submit: $("#ap-submit").checked };
    try { await api("/autopilot", { method: "PUT", body: { values } }); toast(values.enabled ? `Autopilot scheduled daily at ${values.time}.` : "Autopilot is off.", "var(--green)"); renderSystemExtras(); }
    catch (e) { toast(e.message, "var(--red)"); }
  };
}

// ---- find extras: preferences, saved searches ---------------------------------------------------------------

async function renderPrefs() {
  const slot = $("#prefs-panel");
  if (!slot) return;
  const p = state.prefs = await api("/prefs");
  const tagBox = (key, placeholder) => html`<div class="tags" data-pref="${key}">${p[key].map((w, i) => html`<span class="chip">${w}<button data-delpref="${i}">×</button></span>`)}<input type="text" placeholder="${placeholder}" data-addpref></div>`;
  mount(slot, html`
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:14px">
      <span class="muted" style="font-size:12.5px;font-weight:600">Saved searches</span>
      ${p.saved_searches.map((s, i) => html`<span class="board"><a href="#" data-loadsearch="${i}">${s.name}</a>${s.autopilot ? html`<span class="chip plain" style="font-size:10.5px">autopilot</span>` : ""}<button data-delsearch="${i}">×</button></span>`)}
      <button class="btn sm" id="save-search">+ Save this search</button>
      <button class="btn sm ghost" id="toggle-prefs">${state.prefsOpen ? "Hide" : "Preferences"}: blocked companies, cities…</button>
    </div>
    ${state.prefsOpen ? html`<div class="grid g2" style="margin-top:12px">
      <label class="field"><span>Never apply to these companies</span>${tagBox("blocked_companies", "Company, then Enter")}</label>
      <label class="field"><span>Skip jobs mentioning</span>${tagBox("blocked_keywords", "Word, then Enter (e.g. sales, night shift)")}</label>
      <label class="field"><span>Preferred cities (ranked higher)</span>${tagBox("preferred_cities", "City, then Enter")}</label>
      <label class="field"><span>&nbsp;</span><label class="toggle"><input type="checkbox" id="remote-only" ${p.remote_only ? "checked" : ""}>Remote jobs only</label></label>
    </div><p class="muted" style="font-size:12.5px">Blocked companies and words also stop batches and autopilot, not just this list. Saved as you change them.</p>` : ""}`);
  const save = async (values) => { try { state.prefs = await api("/prefs", { method: "PUT", body: { values } }); renderPrefs(); } catch (e) { toast(e.message, "var(--red)"); } };
  $("#toggle-prefs").onclick = () => { state.prefsOpen = !state.prefsOpen; renderPrefs(); };
  $$("[data-pref]").forEach((box) => {
    const key = box.dataset.pref;
    $$("[data-delpref]", box).forEach((b) => (b.onclick = (e) => { e.preventDefault(); save({ [key]: p[key].filter((_, i) => i !== +b.dataset.delpref) }); }));
    const add = $("[data-addpref]", box);
    add.onkeydown = (e) => { if (e.key === "Enter" && add.value.trim()) { e.preventDefault(); save({ [key]: [...p[key], add.value.trim()] }); } };
  });
  if ($("#remote-only")) $("#remote-only").onchange = (e) => save({ remote_only: e.target.checked });
  $("#save-search").onclick = () => {
    const f = state.findForm;
    modal(html`<h2 style="font-size:18px">Save this search</h2>
      <label class="field" style="margin-top:12px"><span>Name</span><input type="text" id="ss-name" placeholder="e.g. Daily India search"></label>
      <label class="toggle" style="margin-top:12px"><input type="checkbox" id="ss-auto">Use for autopilot</label>
      <div class="actions"><button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="ss-go">Save</button></div>`, (el, close) => {
      $("#ss-name", el).focus();
      $("#ss-go", el).onclick = () => {
        const name = $("#ss-name", el).value.trim();
        if (!name) return;
        const auto = $("#ss-auto", el).checked;
        const sources = Object.entries(f.sources).filter(([, on]) => on).map(([k]) => k);
        const list = (auto ? p.saved_searches.map((s) => ({ ...s, autopilot: false })) : p.saved_searches).filter((s) => s.name !== name);
        list.push({ name, roles: [...f.roles], locations: [...f.locations], posted: f.posted, workplaces: [...f.workplaces], max_years: f.maxYears, easy: f.easy, senior: f.senior, sources, autopilot: auto });
        close();
        save({ saved_searches: list });
        toast(`Saved "${name}".`, "var(--green)");
      };
    });
  };
  $$("[data-loadsearch]").forEach((a) => (a.onclick = (e) => {
    e.preventDefault();
    const s = p.saved_searches[+a.dataset.loadsearch];
    const f = state.findForm;
    const roles = s.roles || Object.values(s.queries || {}).flat(); // older saved searches: words per track
    Object.assign(f, {
      roles: [...new Set(roles)], locations: s.locations || (s.location ? [s.location] : f.locations), posted: s.posted || f.posted,
      workplaces: s.workplaces || [], maxYears: s.max_years ?? f.maxYears, easy: s.easy ?? false, senior: s.senior ?? false,
      sources: s.sources ? { linkedin: s.sources.includes("linkedin"), hiringcafe: s.sources.includes("hiringcafe") } : { linkedin: true, hiringcafe: false },
    });
    saveForm();
    renderFind();
    toast(`Loaded "${s.name}". Press Search.`);
  }));
  $$("[data-delsearch]").forEach((b) => (b.onclick = () => save({ saved_searches: p.saved_searches.filter((_, i) => i !== +b.dataset.delsearch) })));
}

// ---- profile extra: draft from the résumé -------------------------------------------------------------------

async function learnFromResume() {
  if (dirty()) return toast("Save or undo your changes first.", "var(--amber)");
  let id;
  try { ({ id } = await api(`/learn?track=${encodeURIComponent(P.track)}`, { method: "POST", body: { claude: true } })); }
  catch (e) { return toast(e.message, "var(--red)"); }
  const close = modal(html`<h2 style="font-size:18px">Reading your résumé…</h2><p class="muted">Code reads the exact facts (contacts, jobs, dates, skills); Claude fills gaps like job summaries. Nothing changes until you pick what to apply.</p><div class="progress"><span style="width:60%" class="indeterminate"></span></div>`);
  const poll = setInterval(async () => {
    const t = await api(`/learn/${id}`).catch(() => null);
    if (!t || t.state === "reading") return;
    clearInterval(poll);
    close();
    if (t.state === "failed") return toast(t.notes[0] || "Couldn't read the résumé.", "var(--red)");
    const show = (v) => (v && typeof v === "object" ? (Array.isArray(v) ? v.map((x) => (typeof x === "object" ? x.title || x.company || x.degree || "…" : x)).join(", ") : v.value ?? JSON.stringify(v)) : v ?? "—");
    modal(html`<h2 style="font-size:18px">What the résumé says differently</h2>
      <p class="muted" style="font-size:13px">${t.diffs.length} value${t.diffs.length === 1 ? "" : "s"} differ from your ${trackName(P.track)} profile. Tick the ones to take from the résumé. Money, notice period and preferences are never in a résumé, so they're left alone.</p>
      ${t.notes.map((n) => html`<div class="banner warn">${n}</div>`)}
      <div class="learn-list">${t.diffs.map((d, i) => html`<label class="learn-row"><input type="checkbox" class="pick" data-learn="${d.key}" ${i < 0 ? "checked" : ""}>
        <div style="min-width:0"><div class="mono" style="font-size:12px">${d.key}</div><div class="learn-vals"><span class="faint">yours</span> ${String(show(d.current)).slice(0, 160)}<br><span style="color:var(--green)">résumé</span> ${String(show(d.learned)).slice(0, 160)}</div></div></label>`)}</div>
      <div class="actions"><button class="btn ghost" data-close>Close</button><button class="btn primary" id="learn-apply">Apply selected</button></div>`, (el, done) => {
      $("#learn-apply", el).onclick = async () => {
        const keys = $$("[data-learn]", el).filter((c) => c.checked).map((c) => c.dataset.learn);
        if (!keys.length) return toast("Tick at least one value.", "var(--amber)");
        try {
          const res = await api(`/learn-apply?track=${encodeURIComponent(P.track)}`, { method: "POST", body: { keys } });
          Object.assign(P, { data: res.profile, saved: JSON.stringify(res.profile), docs: res.documents });
          done();
          drawProfile();
          toast(`${keys.length} value${keys.length > 1 ? "s" : ""} taken from the résumé. The old profile is in .backups/.`, "var(--green)");
        } catch (e) { toast(e.message, "var(--red)"); }
      };
    });
  }, 1500);
}

// ---- start ----------------------------------------------------------------------------------------------------

window.addEventListener("hashchange", route);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("#drawer").hidden = true; $("#modal").hidden = true; } });
refreshShell();
setInterval(refreshShell, 6000);
route();
