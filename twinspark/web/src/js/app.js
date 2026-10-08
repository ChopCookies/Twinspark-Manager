/* TwinSpark Manager — web GUI.
   Vanilla single-page app served same-origin by the management API. The key is
   kept in sessionStorage for this tab only. Every dynamic string goes through
   esc() before it reaches innerHTML. */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const S = { key: sessionStorage.getItem("tsm_key") || "", timers: [], gen: 0, version: "", unlocked: false, filters: {} };

/* ============================== icons ============================== */
const ICON = {
  system: '<path d="M3 4h18v13H3zM8 21h8M12 17v4M6 12h3l2-5 3 7 2-4h2" stroke="currentColor" stroke-width="1.7" fill="none"/>',
  updates: '<path d="M20 7a9 9 0 1 0 1 9M20 3v5h-5M12 7v5l3 2" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round"/>',
  dashboard: '<path d="M3 13h8V3H3zm10 8h8V11h-8zM3 21h8v-6H3zm10-18v6h8V3z"/>',
  profiles: '<path d="M4 6h16M4 12h16M4 18h10" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/>',
  cookbook: '<path d="M6 3h11a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6zm0 0v18M9 7h7M9 11h7" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linejoin="round"/>',
  integrations: '<path d="M8 8H4v8h4M16 8h4v8h-4M8 12h8M10 5l-2 7 2 7M14 5l2 7-2 7" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  files: '<path d="M4 7c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zm0 0v10c0 1.7 3.6 3 8 3s8-1.3 8-3V7M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" stroke="currentColor" stroke-width="1.8" fill="none"/>',
  mods: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.1-.4-.4-2.1z" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linejoin="round"/>',
  planner: '<path d="M12 3v9l6.4 6.4A9 9 0 1 1 12 3z" stroke="currentColor" stroke-width="1.8" fill="none"/><path d="M15 3.5A9 9 0 0 1 20.5 9H15z" fill="currentColor"/>',
  diagnostics: '<path d="M3 12h4l2-6 4 12 2-6h6" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  jobs: '<path d="M9 5h11M9 12h11M9 19h11M4 5h.01M4 12h.01M4 19h.01" stroke="currentColor" stroke-width="2.2" fill="none" stroke-linecap="round"/>',
  logs: '<path d="M4 4h16v16H4zM8 9l3 3-3 3m5 0h4" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  copy: '<path d="M8 8h11v13H8zM5 16V3h11" stroke="currentColor" stroke-width="1.8" fill="none"/>',
  play: '<path d="M7 4l13 8-13 8z" fill="currentColor"/>',
  stop: '<path d="M6 6h12v12H6z" fill="currentColor"/>',
  pin: '<path d="M9 3h6l-1 6 4 4H6l4-4zM12 13v8" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linejoin="round"/>',
};
const icon = (n) => `<svg viewBox="0 0 24 24" aria-hidden="true">${ICON[n] || ""}</svg>`;
const NAV = [
  ["dashboard", "Dashboard"], ["system", "System status"], ["updates", "Updates"], ["profiles", "Profiles"], ["cookbook", "Cookbook"],
  ["integrations", "Integrations"], ["files", "Model files"], ["mods", "Mods"], ["planner", "Planner"],
  ["diagnostics", "Diagnostics"], ["jobs", "Jobs"], ["logs", "Logs"],
];

/* ============================== API ============================== */
class ApiError extends Error {
  constructor(msg, status, detail) { super(msg); this.status = status; this.detail = detail; }
}
async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (S.key) opts.headers["x-api-key"] = S.key;
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(path, opts); }
  catch (e) { throw new ApiError("controller not reachable", 0); }
  if (r.status === 401) { lock(S.key ? "Session key rejected — unlock again." : ""); throw new ApiError("invalid management key", 401); }
  const data = r.status === 204 ? null : await r.json().catch(() => null);
  if (!r.ok) {
    let d = data && data.detail;
    let msg = "HTTP " + r.status;
    if (typeof d === "string") msg = d;
    else if (Array.isArray(d)) msg = d.map(x => `${(x.loc || []).slice(1).join(".")}: ${x.msg}`).join("; ");
    else if (d && d.message) msg = d.message;
    else if (d) msg = JSON.stringify(d);
    throw new ApiError(msg, r.status, d);
  }
  return data;
}
/** A plain-text resource (saved logs) with the same key and error handling as api(). */
async function apiText(path) {
  let r;
  try { r = await fetch(path, { headers: S.key ? { "x-api-key": S.key } : {} }); }
  catch (e) { throw new ApiError("controller not reachable", 0); }
  if (r.status === 401) { lock(S.key ? "Session key rejected — unlock again." : ""); throw new ApiError("invalid management key", 401); }
  if (!r.ok) { const d = await r.json().catch(() => null); throw new ApiError((d && d.detail) || "HTTP " + r.status, r.status); }
  return r.text();
}
const GET = (p) => api("GET", p);
const POST = (p, b) => api("POST", p, b === undefined ? {} : b);
const PUT = (p, b) => api("PUT", p, b);
const DEL = (p) => api("DELETE", p);
const qs = (o) => { const p = new URLSearchParams(); Object.entries(o || {}).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== "") p.set(k, v); }); const s = p.toString(); return s ? "?" + s : ""; };
const enc = encodeURIComponent;

/* ============================== formatting ============================== */
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
/** A recipe-supplied link, only when it is http(s): never javascript: or data: from an imported file. */
function webUrl(u) { return typeof u === "string" && /^https?:\/\//i.test(u) ? u : ""; }
function gib(v, d = 1) { return v == null || isNaN(v) ? "—" : Number(v).toFixed(d) + " GiB"; }
function bytes(n) { if (n == null) return "—"; const u = ["B", "KiB", "MiB", "GiB", "TiB"]; let i = 0, v = n; while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; } return v.toFixed(i >= 3 ? 1 : 0) + " " + u[i]; }
function num(n, d = 0) { return n == null || n === "" || isNaN(n) ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }); }
function sha(s, n = 10) { return s ? String(s).replace(/^sha256:/, "").slice(0, n) : "—"; }
function toTs(t) { if (t == null) return null; return typeof t === "number" ? t * 1000 : Date.parse(t); }
function ago(t) {
  const ms = toTs(t); if (!ms) return "—";
  let d = Math.max(0, (Date.now() - ms) / 1000);
  if (d < 60) return Math.round(d) + "s ago";
  if (d < 3600) return Math.round(d / 60) + "m ago";
  if (d < 86400) return Math.round(d / 3600) + "h ago";
  return Math.round(d / 86400) + "d ago";
}
function dur(a, b) {
  const x = toTs(a), y = b ? toTs(b) : Date.now(); if (!x || !y) return "";
  const s = Math.max(0, (y - x) / 1000);
  return s < 60 ? s.toFixed(0) + "s" : s < 3600 ? Math.floor(s / 60) + "m" + String(Math.floor(s % 60)).padStart(2, "0") + "s" : (s / 3600).toFixed(1) + "h";
}
function when(t) { const ms = toTs(t); return ms ? new Date(ms).toLocaleString() : "—"; }
const tag = (t, c = "") => `<span class="tag ${c}">${esc(t)}</span>`;
const dot = (c = "", pulse = false) => `<span class="dot ${c} ${pulse ? "pulse" : ""}"></span>`;
const VERIF_HELP = {
  verified: "Ran on the TwinSpark maintainers' two DGX Sparks (date and details in the recipe's source note)",
  community: "Published and measured by its author on two DGX Sparks; not run by the TwinSpark maintainers",
  experimental: "Derived or untested: compare `tsm plan` with the source before the first activation",
};
function verifTag(v) {
  return `<span title="${esc(VERIF_HELP[v] || "")}">${tag(v, { verified: "good", community: "info", experimental: "warn" }[v] || "muted")}</span>`;
}
function levelClass(l) { return { SAFE: "good", LOW: "warn", CRITICAL: "bad" }[l] || "muted"; }
function empty(title, text = "") { return `<div class="empty"><b>${esc(title)}</b>${esc(text)}</div>`; }
function codeBlock(text, cls = "") {
  return `<div class="code-block"><button class="btn sm ghost copy" data-copy>${icon("copy")}Copy</button><pre class="code ${cls}">${esc(text)}</pre></div>`;
}
function spark(values, color = "#6aa7ff") {
  const pts = (values || []).map((v, i) => [i, v]).filter(p => p[1] != null);
  if (pts.length < 2) return `<svg class="spark" viewBox="0 0 100 34"></svg>`;
  const n = values.length - 1 || 1;
  const max = Math.max(...pts.map(p => p[1]), 1e-9), min = Math.min(0, ...pts.map(p => p[1]));
  const xy = pts.map(([i, v]) => [(i / n) * 100, 32 - ((v - min) / (max - min || 1)) * 30]);
  const line = xy.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  const area = line + ` L${xy[xy.length - 1][0].toFixed(1)} 34 L${xy[0][0].toFixed(1)} 34 Z`;
  return `<svg class="spark" viewBox="0 0 100 34" preserveAspectRatio="none"><path d="${area}" fill="${color}" opacity=".12"/><path d="${line}" fill="none" stroke="${color}" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>`;
}

/* ============================== feedback ============================== */
function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind; el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "bad" ? 7000 : 3800);
}
function fail(e) { console.error(e); toast(e.message || String(e), "bad"); }

/** modal({title, body, actions:[{label, value, cls}], wide}) → Promise<{value, root}> */
function modal({ title, body = "", actions = [{ label: "Close", value: null }], wide = false, onOpen }) {
  return new Promise((resolve) => {
    const previousFocus = document.activeElement;
    const shell = $("#shell"), wasInert = shell.inert;
    shell.inert = true;
    let pending = false, closed = false;
    const back = document.createElement("div");
    back.className = "modal-back";
    back.innerHTML = `<div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}" tabindex="-1">
      <h2>${esc(title)}</h2><div class="modal-body">${body}</div><p class="err modal-error hidden" role="alert"></p>
      <div class="modal-actions">${actions.map((a, i) => `<button class="btn ${a.cls || ""}" data-i="${i}" type="button">${esc(a.label)}</button>`).join("")}</div></div>`;
    const close = (value) => {
      if (pending || closed) return;
      closed = true; back.remove(); shell.inert = wasInert;
      document.removeEventListener("keydown", onKey);
      if (previousFocus?.isConnected) previousFocus.focus();
      resolve({ value, root: back });
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); close(null); }
      if (e.key !== "Tab") return;
      const focusable = $$("button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href]", back);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (!first) { e.preventDefault(); $(".modal", back).focus(); }
      else if (e.shiftKey && (document.activeElement === first || !back.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (document.activeElement === last || !back.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
    };
    back.addEventListener("click", async (e) => {
      if (pending) return;
      if (e.target === back) return close(null);
      const b = e.target.closest("[data-i]");
      if (b) {
        const a = actions[+b.dataset.i];
        const error = $(".modal-error", back);
        error.classList.add("hidden");
        pending = true;
        const buttons = $$("[data-i]", back);
        buttons.forEach(btn => { btn.disabled = true; });
        b.setAttribute("aria-busy", "true");
        try {
          const valid = !a.validate || await a.validate(back);
          pending = false;
          if (valid) close(a.value === undefined ? a.label : a.value);
        } catch (err) {
          error.textContent = err.message || String(err); error.classList.remove("hidden");
        } finally {
          pending = false; buttons.forEach(btn => { btn.disabled = false; }); b.removeAttribute("aria-busy");
          if (!closed) b.focus();
        }
      }
      const c = e.target.closest("[data-copy]"); if (c) copyFrom(c);
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(back);
    const first = back.querySelector("input,textarea,select,button"); if (first) first.focus();
    if (onOpen) onOpen(back);
  });
}
async function confirmBox(title, text, { label = "Confirm", danger = false, typed = null } = {}) {
  const body = `<div>${text}</div>` + (typed ? `<label class="field"><span>Type <b class="mono">${esc(typed)}</b> to confirm</span><input id="typed" autocomplete="off"/></label>` : "");
  const { value } = await modal({
    title, body,
    actions: [{ label: "Cancel", value: false }, {
      label, value: true, cls: danger ? "danger" : "primary",
      validate: (root) => {
        if (!typed || root.querySelector("#typed").value === typed) return true;
        toast(`type ${typed} to confirm`, "bad"); return false;
      },
    }],
  });
  return value === true;
}
/** form modal: fields [{id,label,value,placeholder,help,type,options}] → values or null */
async function formBox(title, fields, { label = "OK", intro = "", wide = false, validate = null } = {}) {
  const body = (intro ? `<div>${intro}</div>` : "") + fields.map(f => {
    if (f.type === "check") return `<label class="check"><input type="checkbox" id="f-${f.id}" ${f.value ? "checked" : ""}/> ${esc(f.label)}</label>`;
    if (f.type === "select") return `<label class="field"><span>${esc(f.label)}</span><select id="f-${f.id}">${f.options.map(o => `<option value="${esc(o[0])}" ${o[0] === f.value ? "selected" : ""}>${esc(o[1])}</option>`).join("")}</select>${f.help ? `<span class="help">${esc(f.help)}</span>` : ""}</label>`;
    if (f.type === "textarea") return `<label class="field"><span>${esc(f.label)}</span><textarea id="f-${f.id}" rows="${f.rows || 8}" placeholder="${esc(f.placeholder || "")}">${esc(f.value || "")}</textarea>${f.help ? `<span class="help">${esc(f.help)}</span>` : ""}</label>`;
    return `<label class="field"><span>${esc(f.label)}</span><input id="f-${f.id}" value="${esc(f.value ?? "")}" placeholder="${esc(f.placeholder || "")}" autocomplete="off"/>${f.help ? `<span class="help">${esc(f.help)}</span>` : ""}</label>`;
  }).join("");
  let values = null;
  const { value } = await modal({
    title, body, wide,
    actions: [{ label: "Cancel", value: false }, {
      label, value: true, cls: "primary",
      validate: async (root) => {
        values = {};
        fields.forEach(f => { const el = root.querySelector("#f-" + f.id); values[f.id] = f.type === "check" ? el.checked : el.value.trim(); });
        return validate ? await validate(values) : true;
      },
    }],
  });
  return value === true ? values : null;
}
function copyFrom(btn) {
  const pre = btn.parentElement.querySelector("pre, textarea");
  const text = pre ? (pre.value ?? pre.textContent) : "";
  navigator.clipboard?.writeText(text).then(() => toast("copied"), () => toast("copy failed — select the text manually", "bad"));
}
async function busy(btn, fn) {
  if (btn) { btn.disabled = true; btn.dataset.label = btn.innerHTML; btn.innerHTML = `<span class="spin"></span> ${btn.textContent.trim()}`; }
  try { return await fn(); }
  finally { if (btn && btn.isConnected) { btn.disabled = false; btn.innerHTML = btn.dataset.label; } }
}

/* ============================== shell ============================== */
function lock(msg = "") {
  S.unlocked = false; S.gen++; clearInterval(pingTimer);
  S.key = ""; sessionStorage.removeItem("tsm_key");
  S.timers.forEach(clearInterval); S.timers = [];
  $("#shell").classList.add("hidden"); $("#gate").classList.remove("hidden");
  $("#gate-err").textContent = msg; $("#gate-key").focus();
}
async function unlock(key) {
  S.key = key;
  try { await GET("/api/v1/status"); }
  catch (e) { S.key = ""; $("#gate-err").textContent = e.status === 401 ? "Incorrect API key. Please try again." : e.message; return; }
  sessionStorage.setItem("tsm_key", key);
  $("#gate").classList.add("hidden"); $("#shell").classList.remove("hidden");
  boot();
}
async function ping() {
  const pill = $("#conn");
  try {
    const h = await GET("/api/v1/health");
    S.version = h.version;
    $("#version").textContent = "v" + (h.version || "?");
    pill.innerHTML = h.busy ? `${dot("warn", true)} switching…` : `${dot("good")} online`;
    pill.className = "pill ok";
  } catch (e) { pill.innerHTML = `${dot("bad")} offline`; pill.className = "pill bad"; }
}
let pingTimer = null;
function boot() {
  S.unlocked = true;
  $("#nav").innerHTML = NAV.map(([id, label]) => `<a href="#/${id}" data-nav="${id}">${icon(id)}${esc(label)}</a>`).join("");
  ping(); clearInterval(pingTimer); pingTimer = setInterval(ping, 6000);
  route();
}

/* ============================== router ============================== */
/** decodeURIComponent that never throws on a hand-typed "%" in the address bar */
function safeDecode(v) { try { return decodeURIComponent(v); } catch { return String(v); } }
const ROUTES = [
  [/^\/?$/, () => viewDashboard()],
  [/^\/dashboard$/, () => viewDashboard()],
  [/^\/system$/, () => viewSystem()],
  [/^\/updates$/, () => viewUpdates()],
  [/^\/profiles$/, () => viewProfiles()],
  [/^\/profiles\/([^/]+)(?:\/(\w+))?$/, (m) => viewProfile(safeDecode(m[1]), m[2] || "overview")],
  [/^\/cookbook(?:\/(\w+))?$/, (m) => viewCookbook(m[1] || "builtin")],
  [/^\/integrations$/, () => viewIntegrations()],
  [/^\/files$/, () => viewFiles()],
  [/^\/mods$/, () => viewMods()],
  [/^\/planner$/, () => viewPlanner()],
  [/^\/diagnostics(?:\/(\w+))?$/, (m) => viewDiagnostics(m[1] || "doctor")],
  [/^\/jobs$/, () => viewJobs()],
  [/^\/jobs\/([^/]+)$/, (m) => viewJob(safeDecode(m[1]))],
  [/^\/audit$/, () => viewAudit()],
  [/^\/logs$/, () => viewLogs()],
];
function every(fn, ms) { const g = S.gen; const t = setInterval(() => { if (g !== S.gen) return clearInterval(t); fn(); }, ms); S.timers.push(t); }
function current(g) { return g === S.gen; }
async function route() {
  if (!S.unlocked) return;
  S.timers.forEach(clearInterval); S.timers = []; S.gen++;
  const path = (location.hash || "#/").slice(1);
  const top = path.split("/")[1] || "dashboard";
  $$("#nav a").forEach(a => {
    const active = a.dataset.nav === top || (top === "audit" && a.dataset.nav === "jobs");
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  const main = $("#main");
  main.innerHTML = `<div class="view"><div class="loading"><span class="spin"></span></div></div>`;
  main.onclick = null; main.onchange = null; main.oninput = null;
  for (const [re, fn] of ROUTES) {
    const m = path.match(re);
    if (m) {
      const g = S.gen;
      try { await fn(m); }
      catch (e) {
        if (!current(g)) return;
        const missing = e && e.status === 404;
        main.innerHTML = `<div class="view"><div class="view-head"><div><h1>${missing ? "Not found" : "Something went wrong"}</h1><p>${esc(e.message)}</p></div></div>
          <div class="row">${missing ? "" : `<button class="btn" onclick="route()">Retry</button>`}<a class="btn ${missing ? "primary" : ""}" href="#/dashboard">Dashboard</a><a class="btn" href="#/profiles">Profiles</a><a class="btn" href="#/jobs">Jobs</a></div></div>`;
      }
      return;
    }
  }
  main.innerHTML = `<div class="view">${empty("Not found", path)}<div class="row"><a class="btn primary" href="#/dashboard">Back to the dashboard</a></div></div>`;
}
function go(hash) { if (location.hash === hash) route(); else location.hash = hash; }
/** render into #main only if the user has not navigated away meanwhile */
function render(g, html) { if (current(g)) $("#main").innerHTML = html; return current(g); }

/* ---- live views -------------------------------------------------------------------------------
   Polled views patch the page in place instead of replacing #main: replacing it restarted the view's
   fade-in on every refresh, dropped keyboard focus and reset a chosen profile. Unchanged nodes stay,
   text and attributes are updated, rows with a data-key keep their identity, a <select> keeps the
   option the user picked while it still exists, and an element whose action is in flight is left alone. */
function morphAttrs(a, b) {
  for (const { name } of [...a.attributes]) if (!b.hasAttribute(name)) a.removeAttribute(name);
  for (const { name, value } of [...b.attributes]) if (a.getAttribute(name) !== value) a.setAttribute(name, value);
}
function sameNode(a, b) {
  return a.nodeType === b.nodeType && (a.nodeType !== 1 || (a.tagName === b.tagName &&
    (a.getAttribute("data-key") || "") === (b.getAttribute("data-key") || "")));
}
function morphEl(a, b) {
  if (a.dataset && a.dataset.pending) return;               // e.g. a Stop button showing its spinner
  const keep = a.tagName === "SELECT" ? a.value : null;
  morphAttrs(a, b);
  if (a.tagName === "TEXTAREA") return;                     // never overwrite text being typed
  morphChildren(a, b);
  if (keep !== null && [...a.options].some(o => o.value === keep)) a.value = keep;
}
function morphChildren(parent, src) {
  const keyed = new Map();
  for (const n of parent.childNodes) if (n.nodeType === 1 && n.hasAttribute("data-key")) keyed.set(n.getAttribute("data-key"), n);
  let i = 0;
  for (const nb of [...src.childNodes]) {
    let cur = parent.childNodes[i] || null;
    const key = nb.nodeType === 1 ? nb.getAttribute("data-key") : null;
    if (key && keyed.has(key)) {
      const match = keyed.get(key); keyed.delete(key);
      if (match !== cur) { parent.insertBefore(match, cur); cur = match; }
    }
    if (cur && sameNode(cur, nb)) {
      if (cur.nodeType === 1) morphEl(cur, nb); else if (cur.nodeValue !== nb.nodeValue) cur.nodeValue = nb.nodeValue;
    } else parent.insertBefore(nb, cur);
    i++;
  }
  while (parent.childNodes.length > i) parent.removeChild(parent.lastChild);
}
/** Like render(), but patches a view already showing under the same live key (see above). */
function renderLive(g, html, key) {
  if (!current(g)) return false;
  const main = $("#main"), cur = main.firstElementChild;
  if (cur && cur.dataset.live === key && main.children.length === 1) {
    const t = document.createElement("template"); t.innerHTML = html.trim();
    const next = t.content.firstElementChild;
    if (next && t.content.childNodes.length === 1 && next.tagName === cur.tagName) { morphEl(cur, next); return true; }
  }
  main.innerHTML = html;
  return true;
}
/** Call fn every ms after the previous call has finished (polls never overlap). Stops on navigation or when fn
    returns false; a failed poll keeps the last good view and tries again. */
function poll(fn, ms) {
  const g = S.gen;
  const tick = async () => {
    if (!current(g)) return;
    let again = true;
    try { again = (await fn()) !== false; } catch (e) { /* transient: keep what is shown */ }
    if (again && current(g)) S.timers.push(setTimeout(tick, ms));
  };
  S.timers.push(setTimeout(tick, ms));
}
/** A ticket for one load: a slower, older request must not overwrite what a newer one already showed. */
function newest() { const n = (S.seq = (S.seq || 0) + 1); return () => n === S.seq; }
/** event delegation on #main: handlers keyed by data-act */
function onAct(handlers) {
  const main = $("#main");
  main.onclick = async (e) => {
    const c = e.target.closest("[data-copy]"); if (c) { copyFrom(c); return; }
    const el = e.target.closest("[data-act]"); if (!el || !main.contains(el)) return;
    const h = handlers[el.dataset.act]; if (!h || el.dataset.pending) return;
    e.preventDefault();
    el.dataset.pending = "true";
    try { await h(el, e); } catch (err) { fail(err); }
    finally { delete el.dataset.pending; }
  };
}

/* Search/filter state survives navigation without storing recipe data or keys. */
function libraryToolbar(id, placeholder, options) {
  const state = S.filters[id] || (S.filters[id] = { query: "", filter: "all" });
  return `<div class="library-toolbar card flat">
    <label class="field library-search"><span>Search</span><input type="search" id="${id}-search" placeholder="${esc(placeholder)}" value="${esc(state.query)}" autocomplete="off"/></label>
    <label class="field"><span>Filter</span><select id="${id}-filter">${options.map(([value, label]) => `<option value="${esc(value)}" ${state.filter === value ? "selected" : ""}>${esc(label)}</option>`).join("")}</select></label>
    <button class="btn ghost" data-act="clear-filters" id="${id}-clear">Clear</button>
    <span class="small muted library-count" id="${id}-count" role="status" aria-live="polite"></span>
  </div>`;
}
function bindLibrary(id, rows, card, matches, noItems) {
  const search = $("#" + id + "-search"), filter = $("#" + id + "-filter"), state = S.filters[id];
  const draw = () => {
    state.query = search.value; state.filter = filter.value;
    const terms = state.query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const visible = rows.filter(r => {
      const text = [r.name, r.title, r.model, r.description, r.topology, r.quantization, ...(r.tags || [])].join(" ").toLowerCase();
      return terms.every(term => text.includes(term)) && matches(r, state.filter);
    });
    $("#" + id + "-items").innerHTML = visible.length ? visible.map(card).join("") :
      `<div class="card library-empty">${rows.length ? empty("No matches", "Try another search or clear the filters.") : noItems}</div>`;
    $("#" + id + "-count").textContent = `${visible.length} of ${rows.length} shown`;
    $("#" + id + "-clear").disabled = !state.query && state.filter === "all";
  };
  search.oninput = draw; filter.onchange = draw;
  $("#" + id + "-clear").onclick = () => { search.value = ""; filter.value = "all"; draw(); search.focus(); };
  draw();
}

/* ============================== dashboard ============================== */
async function viewDashboard() {
  const g = S.gen;
  const draw = async () => {
    const fresh = newest();
    const [st, series, profs] = await Promise.all([
      GET("/api/v1/status"), GET("/api/v1/system/metrics/series").catch(() => ({})),
      GET("/api/v1/profiles?summary=true").catch(() => []),
    ]);
    let job = null;
    if (st.current_job) job = await GET("/api/v1/jobs/" + enc(st.current_job)).catch(() => null);
    if (!fresh()) return;
    if (!profs.length && !st.active && !S.startSeen) { S.startSeen = true; go("#/start"); return; }
    renderLive(g, dashboardHtml(st, series, profs, job), "dashboard");
  };
  onAct({
    activate: async (el) => {
      const name = $("#qs-profile").value; if (!name) return;
      const job = await busy(el, () => POST(`/api/v1/profiles/${enc(name)}/activate`));
      toast(`switching to ${name}`); go("#/jobs/" + enc(job.job_id));
    },
    stop: async (el) => { if (await stopFlow(el)) draw().catch(() => { }); },
    cancel: async (el) => { const r = await POST(`/api/v1/jobs/${enc(el.dataset.id)}/cancel`); toast(r.note); },
  });
  await draw();
  poll(draw, 5000);
}
/** Drain and stop the active model (dashboard, profile list and profile page share this). */
async function stopFlow(el) {
  if (!await confirmBox("Stop the model?", "Requests are drained first, then the containers on both nodes stop. The gateway answers 503 until something is activated again.", { label: "Stop", danger: true })) return false;
  const job = await busy(el, () => POST("/api/v1/stop"));
  if (job && job.state === "failed") { toast("stop failed: " + (job.error || "see the job"), "bad"); return false; }
  toast("stopped", "good");
  return true;
}

function dashboardHtml(st, series, profs, job) {
  const act = st.active, det = st.active_detail || {}, m = st.metrics || {};
  const obs = det.observed || {}, kv = obs.kv || {};
  const pinned = profs.filter(p => p.pinned);
  const inc = (st.watchdog || {}).last_incident;
  const routes = st.routes || [];
  const down = routes.find(r => r.down_reason);
  let html = `<div class="view" data-live="dashboard"><div class="view-head"><div><h1>Dashboard</h1><p>Both Sparks at a glance — refreshes every 5 s.</p></div>
    <div class="row">
      <select id="qs-profile" style="width:auto;min-width:220px">${pinned.length ? pinned.map(p => `<option value="${esc(p.name)}" ${p.active ? "selected" : ""}>${esc(p.name)}${p.active ? " (active)" : ""}</option>`).join("") : `<option value="">no pinned profiles</option>`}</select>
      <button class="btn primary" data-act="activate" ${pinned.length && !st.busy ? "" : "disabled"}>${icon("play")}Switch</button>
      <button class="btn danger" data-act="stop" ${act && !st.busy ? "" : "disabled"}>${icon("stop")}Stop</button>
    </div></div>`;

  if (job) html += jobBanner(job);
  if (!act && !job) html += `<div class="callout info"><div><b>New here?</b> The <a href="#/start">Get started</a> checklist shows what is set up and what is next.</div></div>`;
  if (["running", "failed"].includes(st.maintenance?.state)) html += `<div class="callout warn"><div><b>Cluster reserved for maintenance</b> · ${esc(st.maintenance.phase)}${st.maintenance.error ? ` · ${esc(st.maintenance.error)}` : ""} <a href="#/updates">Open update progress</a></div></div>`;
  if (down) html += `<div class="callout bad"><div><b>Model is down:</b> ${esc(down.down_reason)}${inc && inc.action ? ` — ${esc(inc.action)}` : ""}</div></div>`;
  else if (inc && Date.now() / 1000 - inc.at < 3600) html += `<div class="callout warn"><div><b>Incident ${esc(ago(inc.at))}:</b> ${esc(inc.problem)} → ${esc(inc.action)}</div></div>`;

  // active deployment
  if (act) {
    html += `<div class="card accent"><div class="hero"><div class="grow">
        <div class="title">${dot("good")} <a href="#/profiles/${enc(act.profile)}">${esc(act.profile)}</a> ${tag(act.label || "", "muted")} ${(act.aliases || [act.alias]).map(a => tag("model: " + a, "info")).join(" ")}</div>
        <div class="model">${esc(det.model || "")}@${esc(sha(det.model_revision, 12))} · ${esc(det.topology || "")}</div>
        <div class="model faint">${esc(det.image || "")}</div></div>
        <div class="kv" style="min-width:260px">
          <b>Serving since</b><span>${esc(ago(act.since))}</span>
          <b>Context</b><span>${obs.max_model_len ? num(obs.max_model_len) + " tokens" : esc(det.context_length ?? "—")}</span>
          <b>KV pool</b><span>${kv.kv_cache_tokens ? num(kv.kv_cache_tokens) + " tokens" : "—"}${kv.max_concurrency ? ` <span class="muted">(${num(kv.max_concurrency, 1)}× at ${num(kv.max_concurrency_at_tokens)})</span>` : ""}</span>
          <b>Load time</b><span>${obs.load_seconds ? Math.round(obs.load_seconds) + " s" : "—"}</span>
        </div></div>
      <div class="tiles" style="margin-top:16px">
        ${tile("Decode", m.decode_tok_s, "tok/s", series.decode_tok_s, "#6aa7ff", 1)}
        ${tile("Prefill", m.prefill_tok_s, "tok/s", series.prefill_tok_s, "#9d7bff", 0)}
        ${tile("Running / waiting", m.ok ? `${num(m["requests-running"])} / ${num(m["requests-waiting"])}` : null, "", series["requests-running"], "#4ade80")}
        ${tile("KV cache used", m.kv_cache_usage_pct, "%", series.kv_cache_usage_pct, "#fbbf24", 1)}
        ${tile("TTFT (avg)", m.avg_ttft_ms, "ms", null)}
        ${tile("Inter-token (avg)", m.avg_itl_ms, "ms", null)}
        ${m.spec_mean_accept_len ? tile("Spec. acceptance", m.spec_accept_len_now ?? m.spec_mean_accept_len, "tok/step", null) : ""}
        ${m.prefix_hit_rate_pct != null ? tile("Prefix-cache hits", m.prefix_hit_rate_pct, "%", null) : ""}
      </div>
      ${m.ok === false || !st.metrics ? `<p class="small muted" style="margin:10px 0 0">Serving metrics appear after the first scrape (every ${esc(10)} s).</p>` : ""}
    </div>`;
  } else {
    html += `<div class="card"><div class="empty"><b>Nothing is serving</b>${pinned.length ? "Pick a profile above and switch." : "Import a recipe from the Cookbook, pin it, then switch."}</div>
      <div class="row" style="justify-content:center">${pinned.length ? "" : `<a class="btn primary" href="#/cookbook">Open the cookbook</a>`}</div></div>`;
  }

  // nodes
  html += `<div class="grid cols-2">`;
  for (const n of Object.keys(st.nodes || {}).sort()) {
    const hw = st.nodes[n] || {}, t = (st.telemetry || {})[n] || {};
    html += nodeCard(n, hw, t);
  }
  html += `</div>`;

  // routes
  html += `<div class="card"><div class="card-head"><h2>Gateway routes</h2><span class="small muted">OpenAI-compatible endpoint · port 8000 by default</span></div>`;
  html += routes.length ? `<div class="table-wrap"><table><thead><tr><th>Model name</th><th>Status</th><th>Serves</th><th class="num">In flight</th><th class="num">Requests</th><th class="num">Errors</th><th class="num">Max len</th></tr></thead><tbody>
    ${routes.map(r => `<tr data-key="route:${esc(r.alias)}"><td class="mono">${esc(r.alias)}</td><td>${tag(r.status, r.status === "serving" ? "good" : r.status === "down" ? "bad" : "warn")}${r.down_reason ? `<div class="sub">${esc(r.down_reason)}</div>` : ""}</td>
      <td>${esc(r.model || "—")}</td><td class="num">${num(r.inflight)}</td><td class="num">${num(r.requests)}</td>
      <td class="num">${num(r.errors)}${r.last_error ? `<div class="sub">${esc(r.last_error)}</div>` : ""}</td><td class="num">${num(r.max_model_len)}</td></tr>`).join("")}
    </tbody></table></div>` : empty("No routes", "The gateway returns 404 until a model is activated.");
  html += `</div>`;
  if (st.staging && st.staging.length) html += `<div class="callout info">Staging weights in the background: ${st.staging.map(s => `<span class="mono">${esc(s)}</span>`).join(", ")} — <a href="#/jobs">jobs</a></div>`;
  return html + `</div>`;
}
function tile(k, v, unit, series, color, digits = 0) {
  return `<div class="tile"><div class="k">${esc(k)}</div><div class="v">${v == null ? "—" : (typeof v === "number" ? num(v, digits) : esc(v))}${v != null && unit ? `<small>${esc(unit)}</small>` : ""}</div>${series ? spark(series, color) : ""}</div>`;
}
function nodeCard(n, hw, t) {
  if (!hw || hw.error) return `<div class="card"><div class="card-head"><h2>Node ${esc(n)}</h2>${tag("unreachable", "bad")}</div><div class="muted small">${esc((hw && hw.error) || t.error || "no facts yet — agent not reachable at startup")}</div></div>`;
  const total = t.mem_total_gib ?? null;
  const avail = t.mem_available_gib;
  const cache = t.page_cache_gib;
  const used = total && avail != null ? Math.max(0, total - avail) : null;
  const pct = total ? Math.min(100, used / total * 100) : 0;
  const cpct = total && cache != null ? Math.max(0, Math.min(100 - pct, cache / total * 100)) : 0;
  const lvl = t.level || "";
  const warns = [];
  if (t.stale || t.error || (t.at && Date.now() / 1000 - t.at > 30)) warns.push(tag("stale sample", "warn"));
  if (hw.runtime_mode === "dry-run") warns.push(tag("dry-run", "warn"));
  if (hw.desktop_running) warns.push(tag(`desktop ${gib(hw.desktop_rss_gib)}`, "warn"));
  if (hw.privd_available === false) warns.push(tag("no privd", "warn"));
  if ((hw.foreign_inference || []).length) warns.push(tag("foreign vLLM", "bad"));
  return `<div class="card"><div class="card-head"><h2>Node ${esc(n)} <span class="muted small mono">${esc(hw.hostname || "")}</span></h2><div class="chip-row">${lvl ? tag(lvl, levelClass(lvl)) : ""}${warns.join("")}</div></div>
    ${hostTiles(t)}
    <div class="row between small"><span>Shared memory · ${gib(used)} used of ${gib(total)}</span><span class="muted">${gib(avail)} available</span></div>
    <div class="meter big" style="margin:6px 0"><i style="width:${pct}%" class="${lvl === "CRITICAL" ? "bad" : lvl === "LOW" ? "warn" : ""}"></i><i class="cache" style="left:${pct}%;width:${cpct}%"></i></div>
    <div class="legend"><span><i style="background:var(--accent)"></i>in use</span><span><i style="background:rgba(143,155,186,.5)"></i>page cache ${gib(cache)} (dropped before each launch)</span></div>
    <div class="kv" style="margin-top:12px">
      <b>GPU</b><span>${esc(hw.gpu_name || "—")} · driver ${esc(hw.driver_version || "—")} · CUDA ${esc(hw.cuda_version || "—")}</span>
      <b>RoCE</b><span class="mono">${esc((hw.rdma_active || []).join(", ") || "—")}</span>
      <b>Disk</b><span>${gib(hw.disk_free_gib, 0)} free <span class="muted mono">${esc(hw.hf_cache_dir || "")}</span></span>
      <b>Agent</b><span>${esc(hw.agent_version || "?")}${hw.agent_version && S.version && hw.agent_version !== S.version ? " " + tag("version mismatch", "warn") : ""} · ${esc(hw.kernel || "")}</span>
    </div></div>`;
}

/* ============================== host monitoring & maintenance ============================== */
function hostTiles(t, history = []) {
  const gpu = t.gpu || {};
  return `<div class="tiles host-tiles">
    ${tile("CPU", t.cpu_pct, "%", history.map(x => x.cpu_pct), "#6aa7ff", 1)}
    ${tile("GPU", gpu.utilization_pct, "%", history.map(x => x.gpu?.utilization_pct), "#9d7bff", 1)}
    ${tile("GPU sensor power", gpu.power_w, "W", history.map(x => x.gpu?.power_w), "#fbbf24", 1)}
    ${tile("GPU temperature", gpu.temperature_c, "°C", null, "", 0)}
  </div>`;
}
function systemNode(n, t, history, interval) {
  const stale = t.stale || t.error || (t.at && Date.now() / 1000 - t.at > Math.max(30, interval * 3));
  const gpu = t.gpu || {};
  const used = t.mem_total_gib != null && t.mem_available_gib != null ? Math.max(0, t.mem_total_gib - t.mem_available_gib) : null;
  const networks = Object.entries(t.network || {});
  return `<article class="card system-node"><div class="card-head"><h2>Node ${esc(n)}</h2>${tag(t.demo ? "sample data" : stale ? "stale / disconnected" : t.at ? "live" : "waiting for agent", t.demo || stale ? "warn" : t.at ? "good" : "muted")}</div>
    <p class="small muted">${t.at ? `Last sample ${esc(ago(t.at))}` : "Metrics appear after the agent's first samples."}${t.error ? ` · ${esc(t.error)}` : ""}</p>
    ${hostTiles(t, history)}
    <div class="memory-summary"><div><span class="eyebrow">Shared CPU + GPU memory</span><p><b>${gib(used)}</b> used <span class="muted">/ ${gib(t.mem_total_gib)}</span></p></div><div><b>${gib(t.mem_available_gib)}</b><span class="small muted">available to workloads</span></div></div>
    ${spark(history.map(x => x.mem_total_gib != null && x.mem_available_gib != null ? x.mem_total_gib - x.mem_available_gib : null))}
    <div class="kv"><b>Swap used</b><span>${gib(t.swap_used_gib)}</span><b>GPU memory sensor</b><span>${gpu.memory_used_mib != null ? `${gib(gpu.memory_used_mib / 1024)} / ${gib(gpu.memory_total_mib == null ? null : gpu.memory_total_mib / 1024)}` : "Unavailable on this device"}</span><b>Wall electricity</b><span>External meter required</span></div>
    <p class="small muted">Spark shares memory between CPU and GPU. These are not separate pools of RAM and VRAM. Power above is a GPU sensor reading, not whole-system electricity use.</p>
    <h3>Network activity</h3>${networks.length ? `<div class="table-wrap"><table><thead><tr><th>Interface</th><th>Receive</th><th>Send</th><th>Link</th></tr></thead><tbody>${networks.map(([name, v]) => `<tr><td class="mono">${esc(name)}<div class="sub">${esc(v.state)}</div></td><td>${v.rx_bytes_s == null ? "—" : bytes(v.rx_bytes_s) + "/s"}</td><td>${v.tx_bytes_s == null ? "—" : bytes(v.tx_bytes_s) + "/s"}</td><td>${v.speed_mbps == null ? "—" : num(v.speed_mbps / 1000, 1) + " Gb/s"}</td></tr>`).join("")}</tbody></table></div>` : `<p class="muted small">Waiting for interface counters.</p>`}
    <p class="small muted">Rates need two samples. OS interface counters may exclude traffic that bypasses the kernel, including RDMA.</p></article>`;
}
async function viewSystem() {
  const g = S.gen;
  render(g, `<div class="view"><div class="view-head"><div><h1>System status</h1><p>Compare both nodes while you experiment.</p></div><a class="btn" href="#/updates">Updates & reboots</a></div><div id="system-content"></div></div>`);
  let loading = false;
  const draw = async () => {
    if (loading) return; loading = true;
    try {
      const [data, status] = await Promise.all([GET("/api/v1/system/telemetry"), GET("/api/v1/status")]);
      if (!current(g)) return;
      const names = [...new Set([...Object.keys(status.nodes || {}), ...Object.keys(data.nodes)])].sort();
      $("#system-content").innerHTML = `<p class="small muted">Sampled every ${num(data.interval_s)} s · recent history stays in memory · — means unavailable, not zero.</p>` + (names.length ? `<div class="grid cols-2">${names.map(n => systemNode(n, data.nodes[n] || {}, data.history[n] || [], data.interval_s)).join("")}</div>` : empty("No nodes connected", "Configure node A and B to see their system readings."));
    } catch (e) { if (current(g)) $("#system-content").innerHTML = `<div class="callout warn">${esc(e.message)} · Retrying automatically.</div>`; }
    finally { loading = false; }
  };
  await draw(); every(draw, 5000);
}
function maintenanceProgress(s) {
  if (s.state === "idle") return empty("No maintenance running", "Check the nodes to review available package updates and their permissions.");
  const nodes = s.order || [];
  return `<div class="card-head"><h2>${s.dry_run ? "Simulated maintenance" : "Maintenance progress"}</h2>${tag(s.state, s.state === "completed" ? "good" : s.state === "failed" ? "bad" : "info")}</div>
    <div class="maintenance-steps">${nodes.map((n, i) => `<div class="maintenance-step ${i === s.index && s.state === "running" ? "current" : ""}"><b>Node ${esc(n)}</b><span>${esc(s.nodes?.[n]?.state || (i === s.index ? s.phase : "waiting"))}</span></div>`).join("")}</div>
    <p>Current step: <b>${esc(s.phase)}</b> · started ${esc(when(s.started_at))}</p>
    ${s.previous ? `<p class="small muted">Restore after checks: ${esc(s.previous.profile)} · ${esc(sha(s.previous.revision_id))}</p>` : ""}
    ${s.waiting ? `<p class="small muted">${esc(s.waiting)}</p>` : ""}
    ${s.error ? `<div class="callout bad">${esc(s.error)}</div>` : ""}
    ${s.state === "failed" ? `<p class="small">The cluster stays reserved. Repair the reported issue on the node, then recheck. Recheck polls the existing update; it does not repeat a failed installation. Release only succeeds when workers have stopped and the nodes pass checks.</p><div class="row"><button class="btn" data-act="resume">Recheck progress</button><button class="btn" data-act="release">Release after repair</button></div>` : ""}`;
}
async function viewUpdates() {
  const g = S.gen; let plan = null, state = {};
  render(g, `<div class="view"><div class="view-head"><div><h1>Updates & reboots</h1><p>One coordinated maintenance run for both Sparks.</p></div><button class="btn" data-act="check">Check nodes</button></div>
    <ol class="recipe-steps" aria-label="Maintenance workflow"><li><span>1</span><div><b>Drain & stop</b><small>Finish requests, then stop the model.</small></div></li><li><span>2</span><div><b>Update B, then A</b><small>Install, reboot, and verify each node.</small></div></li><li><span>3</span><div><b>Restore</b><small>Start the exact revision that was serving.</small></div></li></ol>
    <div class="callout info"><div>Automatic installation and reboots are opt-in for each run. The model is unavailable during maintenance. OS and driver updates use configured package repositories; optional firmware uses the node's configured fwupd remotes. TwinSpark itself and recipe container images are not upgraded by this workflow.</div></div>
    <div id="maintenance-plan"></div><div class="card" id="maintenance-progress"></div></div>`);
  const draw = async () => {
    try { const next = await GET("/api/v1/system/maintenance"); if (!current(g)) return; state = next; $("#maintenance-progress").innerHTML = maintenanceProgress(state); }
    catch (e) { if (current(g)) $("#maintenance-progress").innerHTML = `<div class="callout warn">${esc(e.message)} · Reconnecting. Maintenance resumes from its saved checkpoint.</div>`; }
  };
  onAct({
    check: async el => {
      const next = await busy(el, () => POST("/api/v1/system/maintenance/plan")); if (!current(g)) return; plan = next;
      $("#maintenance-plan").innerHTML = `<div class="grid cols-2">${Object.entries(plan.nodes).map(([n, p]) => `<div class="card"><div class="card-head"><h2>Node ${esc(n)}</h2>${tag(p.dry_run ? "simulation" : p.enabled ? "opted in" : "disabled", p.enabled ? "good" : "warn")}</div>${p.error ? `<p>${esc(p.error)}</p>` : `<p class="small">Firmware ${p.allow_firmware ? "permitted" : "disabled"} · ${p.reboot_required ? "reboot pending" : "no reboot flagged"}</p><details><summary>Package preview (cached repository metadata)</summary>${codeBlock(p.package_preview || "No preview available")}</details>`}</div>`).join("")}</div>
        <div class="card"><p>${esc(plan.note)}</p>${!plan.ready ? `<p class="small muted">Enable the root-owned maintenance policy on each node first. See the maintenance setup guide in the project documentation. No node is opted in automatically.</p>` : ""}<button class="btn primary" data-act="start" ${plan.ready ? "" : "disabled"}>Review automatic update</button></div>`;
    },
    start: async () => {
      if (!plan?.ready) return;
      const firmwareAllowed = Object.values(plan.nodes).every(n => n.allow_firmware);
      await modal({ title: "Update and reboot both nodes?", body: `<p>Serving will stop. TwinSpark will install OS and driver packages, reboot each node, verify health, and restore the saved model revision.</p><p>Package versions are resolved again at installation time. This is an automatic maintenance run, not a recurring schedule.</p><label class="check"><input id="update-optin" type="checkbox"> I authorize automatic installation and reboots for this run.</label><label class="check"><input id="update-firmware" type="checkbox" ${firmwareAllowed ? "" : "disabled"}> Include firmware updates${firmwareAllowed ? "" : " (disabled by node policy)"}</label>`, actions: [{ label: "Cancel" }, { label: "Install & reboot", cls: "primary", validate: async root => {
        if (!$("#update-optin", root).checked) throw new Error("Select the installation and reboot opt-in to continue.");
        await POST("/api/v1/system/maintenance/start", { confirm: "UPDATE AND REBOOT", firmware: $("#update-firmware", root).checked });
        await draw(); return true;
      } }] });
    },
    resume: async () => { await POST("/api/v1/system/maintenance/resume"); await draw(); },
    release: async () => { if (await confirmBox("Release the maintenance hold?", "Use this after repairing the nodes. TwinSpark will check both nodes and leave model activation to you.", { label: "Check & release" })) { await POST("/api/v1/system/maintenance/release"); await draw(); } },
  });
  await draw(); every(draw, 3000);
}
function jobBanner(job) {
  const step = (job.steps || []).slice(-1)[0] || {};
  const p = Math.round((step.progress || 0) * 100);
  return `<div class="card accent"><div class="row between"><div><b>${esc(job.kind === "stage" ? "Staging" : job.kind === "recovery" ? "Recovering" : "Switching to")} ${esc(job.payload.profile || job.payload.repo || "")}</b>
      <span class="muted"> · ${esc(step.stage || "starting")} · ${esc(dur(job.created_at))}</span></div>
      <div class="row"><a class="btn sm" href="#/jobs/${enc(job.job_id)}">Details</a>${cleaningUp(job) ? "" : `<button class="btn sm danger" data-act="cancel" data-id="${esc(job.job_id)}">Cancel</button>`}</div></div>
    <div class="small" style="margin-top:6px;color:var(--ink-2)">${esc(step.message || "")}</div>
    <div class="progress-line ${p ? "" : "indeterminate"}"><i style="width:${p}%"></i></div></div>`;
}

/* ============================== profiles ============================== */
async function viewProfiles() {
  const g = S.gen;
  const [rows, st] = await Promise.all([GET("/api/v1/profiles?summary=true"), GET("/api/v1/status")]);
  const item = (p) => {
    const state = p.active ? tag("active", "good") : p.pinned ? tag("pinned " + (p.latest?.label || ""), "info") : tag("draft — not pinned", "warn");
    return `<div class="item ${p.active ? "active" : ""}"><div class="l">
        <div class="t"><a href="#/profiles/${enc(p.name)}">${esc(p.name)}</a> ${state} ${p.draft_differs ? tag("unpinned edits", "warn") : ""} ${verifTag(p.verification)} ${p.latest?.known_good ? tag("known-good", "good") : ""}</div>
        <div class="d mono">${esc(p.model)}${p.secondary_model ? " + " + esc(p.secondary_model) : ""} · ${esc(p.topology)} · ${esc(p.quantization)}${p.mods.length ? " · mods: " + esc(p.mods.join(", ")) : ""}</div>
        ${p.description ? `<div class="d">${esc(p.description)}</div>` : ""}
        ${(p.warnings || []).filter(w => !w.startsWith("not pinned")).map(w => `<div class="d" style="color:var(--warn)">⚠ ${esc(w)}</div>`).join("")}
      </div><div class="actions">
        ${p.active ? `<button class="btn sm danger" data-act="stop" ${st.busy ? "disabled" : ""}>${icon("stop")}Stop</button>` : p.pinned ? `<button class="btn sm primary" data-act="activate" data-name="${esc(p.name)}" ${st.busy ? "disabled" : ""}>${icon("play")}Switch</button>` : ""}
        <button class="btn sm ${p.pinned ? "" : "primary"}" data-act="pin" data-name="${esc(p.name)}">${icon("pin")}${p.pinned ? "Re-pin" : "Pin"}</button>
        <a class="btn sm" href="#/profiles/${enc(p.name)}/plan">Plan</a>
      </div></div>`;
  };
  if (!render(g, `<div class="view"><div class="view-head"><div><p class="eyebrow">Your experiments</p><h1>Profiles</h1><p>Keep a baseline, duplicate it, and try a new configuration. Pin a draft when it is ready to run.</p></div>
    <div class="row"><a class="btn" href="#/cookbook">Import from cookbook</a><button class="btn" data-act="split">Combine two recipes</button><button class="btn" data-act="new">New from JSON</button></div></div>
    ${libraryToolbar("profiles", "Search model, profile or topology…", [["all", "All profiles"], ["active", "Active"], ["draft", "Not pinned"], ["pinned", "Pinned"], ["edited", "Unpinned edits"]])}
    <div class="list" id="profiles-items"></div></div>`)) return;
  bindLibrary("profiles", rows, item, (p, filter) => filter === "all" ||
    (filter === "active" && p.active) || (filter === "draft" && !p.pinned) ||
    (filter === "pinned" && p.pinned) || (filter === "edited" && p.draft_differs),
    empty("Your first experiment starts with a recipe", "Open the Cookbook to import a configuration, then adjust it here."));
  onAct({
    split: () => splitFlow(rows),
    stop: async (el) => { if (await stopFlow(el)) route(); },
    activate: (el) => activateFlow(el.dataset.name, "latest", el),
    pin: (el) => pinFlow(el.dataset.name).then(ok => ok && route()),
    new: async () => {
      const tmpl = JSON.stringify({
        name: "my-model", description: "", simple: { model: "org/model", quantization: "nvfp4", topology: "tp2", context_length: 131072, concurrency: 4, thinking: false, tool_calling: false, api_alias: "default" },
        behaviour: {}, advanced: { kv_dtype: "fp8", gpu_memory_utilization: 0.85 }, image_hint: "ghcr.io/org/image:tag", distributed_backend: "mp",
      }, null, 2);
      const v = await formBox("New profile from JSON", [{ id: "json", label: "ProfileDraft", type: "textarea", value: tmpl, rows: 18 }], { label: "Create", wide: true });
      if (!v) return;
      let doc; try { doc = JSON.parse(v.json); } catch (e) { throw new Error("invalid JSON: " + e.message); }
      const p = await POST("/api/v1/profiles", doc); toast("created " + p.name, "good"); go("#/profiles/" + enc(p.name));
    },
  });
}

async function activateFlow(name, revision = "latest", btn = null) {
  const job = await busy(btn, () => POST(`/api/v1/profiles/${enc(name)}/activate${qs({ revision })}`));
  toast(`switching to ${name}`); go("#/jobs/" + enc(job.job_id));
}
async function splitFlow(rows) {
  const choices = rows.filter(p => p.topology !== "split").map(p => [p.name, `${p.name} · ${p.model}`]);
  if (!choices.length) throw new Error("Import a recipe first, then combine two profiles.");
  const v = await formBox("Two models across both nodes", [
    { id: "name", label: "Combined profile name", value: "dual-model" },
    { id: "node_a", label: "Recipe on node A", type: "select", options: choices },
    { id: "alias_a", label: "Model name for node A", value: "default" },
    { id: "node_b", label: "Recipe on node B", type: "select", options: choices, value: choices[Math.min(1, choices.length - 1)][0] },
    { id: "alias_b", label: "Model name for node B", value: "secondary" },
  ], { label: "Combine", intro: "Each node gets its own model, image, settings and API model name. The recipes are copied into this profile; later edits of the originals stay independent.",
    validate: async (values) => {
      if (values.alias_a === values.alias_b) throw new Error("Use different model names for A and B.");
      return await POST("/api/v1/profiles/compose/split", values);
    } });
  if (v) go("#/profiles/" + enc(v.name));
}
async function prepareFlow(name, btn) {
  const job = await busy(btn, () => POST(`/api/v1/profiles/${enc(name)}/prepare`));
  toast("checking recipe and staging weights…"); go("#/jobs/" + enc(job.job_id));
}
function recipeRequestId() {
  return globalThis.crypto?.randomUUID?.() || `recipe-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
async function automateRecipe(name, btn) {
  const g = S.gen;
  const job = await busy(btn, async () => {
    const p = await GET(`/api/v1/profiles/${enc(name)}`);
    if (!current(g)) return null;
    const pins = await askMissingImages(p.draft);
    if (pins === null) return null;
    return POST('/api/v1/cookbook/integrate', {draft:p.draft, existing:true, pins, request_id:recipeRequestId()});
  });
  if (job && current(g)) { toast("pinning and preparing recipe…"); go("#/jobs/" + enc(job.job_id)); }
}
async function checkRecipeUpdate(name, btn) {
  const g = S.gen;
  const data = await busy(btn, () => GET('/api/v1/cookbook/updates' + qs({profile:name})));
  if (!current(g)) return;
  const update = data.updates[0];
  if (update.status === 'changed') await importDraft({...update.preview, update});
  else toast(update.message || 'Recipe source is up to date', update.status === 'error' ? 'bad' : 'good');
}
/* ---- images: a recipe without image_hint needs the person to choose one before anything starts ---- */
const needsImage = (d) => !!d && !d.image_hint && !d.identity;
const IMAGE_HELP = "A registry reference is pinned to its multi-arch digest; a local tag is pinned to its image ID, which must be identical on both nodes. TwinSpark never picks an installed image on its own: a vLLM build without this model's support or patches fails much later.";
function imageFieldsHtml(d) {
  const parts = [["a", d, d.secondary ? "Container image for node A" : "Container image"], ...(d.secondary ? [["b", d.secondary, "Container image for node B"]] : [])].filter(([, x]) => needsImage(x));
  if (!parts.length) return "";
  const src = webUrl(d.source?.url);
  return `<div class="callout warn"><div><b>This recipe names no vLLM image.</b> Choose a compatible one before preparing${src ? ` — its source lists the image its author used: <a href="${esc(src)}" target="_blank" rel="noopener noreferrer">${esc(src)}</a>` : ""}. Import only does not need it.</div></div>
    ${parts.map(([k, , label]) => `<label class="field"><span>${esc(label)}</span><input id="imp-image-${k}" placeholder="ghcr.io/org/image:tag or a local tag"/><span class="help">${esc(IMAGE_HELP)}</span></label>`).join("")}`;
}
/** pins for /cookbook/integrate from the review dialog's image fields; null (and a message) when one is missing */
function imagePins(root, d) {
  const pins = {};
  for (const [k, x] of [["a", d], ["b", d.secondary]]) {
    if (!needsImage(x)) continue;
    const input = $(`#imp-image-${k}`, root), v = (input?.value || "").trim();
    if (!v) { input?.setCustomValidity("Choose an image first, or use Import only"); input?.reportValidity(); input?.addEventListener("input", () => input.setCustomValidity(""), { once: true }); return null; }
    if (k === "a") pins.image = v; else pins.secondary = { image: v };
  }
  return pins;
}
/** Ask for the images a draft lacks; {} when nothing is missing, null when the person cancels. */
async function askMissingImages(d) {
  const missing = [["image", d, d.secondary ? "Container image for node A" : "Container image"], ...(d.secondary ? [["image_b", d.secondary, "Container image for node B"]] : [])].filter(([, x]) => needsImage(x));
  if (!missing.length) return {};
  const src = webUrl(d.source?.url);
  const v = await formBox("Choose the vLLM image", missing.map(([id, , label]) => ({ id, label, value: "", placeholder: "ghcr.io/org/image:tag or a local tag", help: IMAGE_HELP })),
    { label: "Pin & prepare", intro: `<div class="small">This recipe names no image${src ? `; its source lists the one its author used: <a href="${esc(src)}" target="_blank" rel="noopener noreferrer">${esc(src)}</a>` : ""}.</div>`,
      validate: async (values) => { for (const [id] of missing) if (!(values[id] || "").trim()) throw new Error("Enter an image (registry reference or local tag)."); return values; } });
  if (!v) return null;
  const pins = {};
  if (v.image) pins.image = v.image.trim();
  if (v.image_b) pins.secondary = { image: v.image_b.trim() };
  return pins;
}
async function pinFlow(name) {
  const p = await GET(`/api/v1/profiles/${enc(name)}`);
  const d = p.draft || {}; const id = d.identity;
  const mustA = needsImage(d), mustB = d.secondary && needsImage(d.secondary);
  const fields = [
    { id: "model_ref", label: "Model revision", value: "", placeholder: id ? `keep ${sha(id.model_revision, 12)} (or: main / a 40-char sha)` : "main (or a branch / 40-char sha)", help: `Resolved against the Hub for ${d.simple?.model || "the model"} — the exact commit gets recorded.` },
    { id: "image", label: mustA ? "Container image (required: the recipe names none)" : d.secondary ? "Container image for node A" : "Container image", value: "", placeholder: d.image_hint || (id ? `keep ${id.image}` : "ghcr.io/org/image:tag or local tag"), help: IMAGE_HELP },
  ];
  if (mustB) fields.push({ id: "image_b", label: "Container image for node B (required: the recipe names none)", value: "", placeholder: "ghcr.io/org/image:tag or local tag", help: IMAGE_HELP });
  fields.push({ id: "note", label: "Note (optional)", value: "" });
  const v = await formBox(`Pin ${name}`, fields, { label: "Pin",
    intro: `<div class="small muted">Pinning turns the working draft into an immutable revision you can switch to. Extra models (drafters) are pinned too.${d.secondary ? " Both models are pinned together." : ""}</div>`,
    validate: async (values) => {
      if (mustA && !(values.image || "").trim()) throw new Error("This recipe names no image: enter one (registry reference or local tag).");
      if (mustB && !(values.image_b || "").trim()) throw new Error("Enter the image for node B.");
      return values;
    } });
  if (!v) return false;
  const body = {}; if (v.model_ref) body.model_ref = v.model_ref; if (v.image) body.image = v.image.trim(); if (v.note) body.note = v.note;
  if (v.image_b) body.secondary = { image: v.image_b.trim() };
  toast("pinning — resolving commit and image…");
  const r = await POST(`/api/v1/profiles/${enc(name)}/pin`, body);
  await modal({
    title: `Pinned ${name} ${r.revision.label}`, body: `<div class="kv"><b>Model</b><span class="mono">${esc(r.resolved.model)}</span><b>Image</b><span class="mono">${esc(r.resolved.image)}</span><b>Source</b><span>${esc(r.resolved.image_source)}</span>${(r.resolved.extra_models || []).map(x => `<b>Extra</b><span class="mono">${esc(x)}</span>`).join("")}</div>${(r.notes || []).length ? `<ul class="plain">${r.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}`,
    actions: [{ label: "Close", value: null }],
  });
  return true;
}

async function viewProfile(name, tab) {
  const g = S.gen;
  const [p, st] = await Promise.all([GET(`/api/v1/profiles/${enc(name)}`), GET("/api/v1/status")]);
  const d = p.draft || (p.revisions.length ? p.revisions[p.revisions.length - 1].draft : null);
  const last = p.revisions[p.revisions.length - 1];
  const isActive = st.active && st.active.profile === name;
  const hasEdits = last && JSON.stringify({ ...d, identity: null }) !== JSON.stringify({ ...last.draft, identity: null });
  const tabs = [["overview", "Overview"], ["settings", "Settings"], ["revisions", `Revisions (${p.revisions.length})`], ["plan", "Launch plan"]];
  const head = `<div class="view-head"><div><div class="crumbs"><a href="#/profiles">Profiles</a> /</div>
      <h1>${esc(name)} ${isActive ? tag("active", "good") : last ? tag("pinned " + last.label, "info") : tag("draft", "warn")} ${d ? verifTag(d.verification) : ""}</h1><p>${esc(p.description || d?.description || "")}</p></div>
    <div class="row">
      ${isActive ? `<button class="btn danger" data-act="stop" ${st.busy ? "disabled" : ""}>${icon("stop")}Stop</button>` : last ? `<button class="btn primary" data-act="activate" ${st.busy ? "disabled" : ""}>${icon("play")}Switch to latest</button>` : ""}
      <button class="btn" data-act="prepare" ${st.busy ? "disabled" : ""}>${last && !hasEdits ? "Prepare recipe" : "Pin &amp; prepare"}</button>
      <button class="btn ${last ? "" : "primary"}" data-act="pin">${icon("pin")}${last ? "Re-pin" : "Pin"}</button>
      <button class="btn" data-act="duplicate">Duplicate</button>
      ${d?.source?.receipt ? '<button class="btn" data-act="recipe-update">Check recipe updates</button>' : ''}
      <button class="btn danger" data-act="delete" ${isActive ? "disabled" : ""}>Delete</button>
    </div></div>
    ${hasEdits ? `<div class="callout warn"><div><b>Your draft has unpinned changes.</b> Re-pin to run these settings. Switching to the latest revision uses the previously pinned settings.</div></div>` : ""}
    <div class="tabs">${tabs.map(([id, l]) => `<a href="#/profiles/${enc(name)}/${id}" class="${tab === id ? "active" : ""}">${esc(l)}</a>`).join("")}</div>`;
  const common = {
    'recipe-update': el => checkRecipeUpdate(name, el),
    prepare: async (el) => {
      if (!last || hasEdits) await automateRecipe(name, el);
      else await prepareFlow(name, el);
    },
    activate: (el) => activateFlow(name, "latest", el),
    stop: async (el) => { if (await stopFlow(el)) route(); },
    pin: () => pinFlow(name).then(ok => ok && route()),
    duplicate: async () => {
      const v = await formBox("Duplicate profile", [{ id: "n", label: "New name", value: name + "-copy" }], { label: "Duplicate" });
      if (!v) return; await POST(`/api/v1/profiles/${enc(name)}/duplicate${qs({ new_name: v.n })}`); go("#/profiles/" + enc(v.n));
    },
    delete: async () => {
      if (!await confirmBox(`Delete ${name}?`, "The profile and its revisions are removed. Model files stay on disk (Model files → delete).", { label: "Delete", danger: true, typed: name })) return;
      await DEL(`/api/v1/profiles/${enc(name)}`); toast("deleted " + name, "good"); go("#/profiles");
    },
  };
  let body = "";
  if (tab === "overview") body = await profileOverview(name, p, d);
  else if (tab === "settings") body = profileSettings(name, p, d);
  else if (tab === "revisions") body = profileRevisions(name, p, isActive, st);
  else if (tab === "plan") { body = await profilePlan(name, p, S.planRef || ""); S.planRef = null; }
  if (!render(g, `<div class="view">${head}${body}</div>`)) return;
  onAct({ ...common, ...profileActions(name, p, d) });
  if (tab === "plan") bindPlanSrc(name, p);
}

async function profileOverview(name, p, d) {
  if (!d) return empty("No draft");
  const fit = await GET(`/api/v1/profiles/${enc(name)}/fit`).catch(() => null);
  const s = d.simple, a = d.advanced, b = d.behaviour, src = d.source || {};
  const warnings = [];
  if (s.thinking && !b.reasoning_parser) warnings.push("thinking is on but no reasoning parser is set");
  if (s.tool_calling && !b.tool_call_parser) warnings.push("tool calling is on but no tool-call parser is set");
  let html = "";
  if (d.secondary) {
    const second = d.secondary;
    html += `<div class="card accent"><h2>Two independent models</h2><div class="kv"><b>Node A</b><span class="mono">${esc(s.model)} · ${esc(s.api_alias)}</span><b>Node B</b><span class="mono">${esc(second.simple.model)} · ${esc(second.simple.api_alias)}</span><b>B image</b><span class="mono">${esc(second.identity?.image || second.image_hint || "not pinned")}</span><b>B context</b><span>${esc(second.simple.context_length)} tokens</span></div><p class="small muted">Quick settings apply to A. Edit the secondary section in Full draft for B. Prepare checks both nodes and stages each model on its assigned node.</p>${(second.source?.requirements || []).length ? `<h3>Node B requirements</h3><ul class="plain">${second.source.requirements.map(r => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}</div>`;
  }
  if (!p.revisions.length) html += `<div class="callout warn"><div><b>Not pinned yet.</b> Pin resolves the model commit and the image digest (and checks a local image is identical on both nodes). Then it can be switched to.</div></div>`;
  if (warnings.length) html += `<div class="callout warn"><div>${warnings.map(esc).join("<br>")}</div></div>`;
  html += `<div class="grid cols-2"><div class="card"><h2>What gets served</h2><div class="kv">
    <b>Model</b><span class="mono">${esc(s.model)}${d.identity ? "@" + esc(sha(d.identity.model_revision, 12)) : ""}</span>
    <b>Image</b><span class="mono">${esc(d.identity ? d.identity.image + " @ " + sha(d.identity.image_digest, 12) + " (" + d.identity.image_source + ")" : (d.image_hint || "—"))}</span>
    <b>Topology</b><span>${esc(s.topology)} · ${esc(d.distributed_backend)} · ${esc(s.quantization)}</span>
    <b>Context</b><span>${esc(s.context_length)} ${s.context_length === "auto" ? '<span class="muted">(vLLM picks the largest that fits)</span>' : "tokens"}</span>
    <b>Parallel seqs</b><span>${esc(a.max_num_seqs || s.concurrency)}</span>
    <b>Model names</b><span>${[s.api_alias, ...(s.extra_aliases || [])].map(x => tag(x, "info")).join(" ")}</span>
    <b>Thinking / tools</b><span>${s.thinking ? "on" : "off"} / ${s.tool_calling ? "on" : "off"} <span class="muted mono">${esc([b.reasoning_parser, b.tool_call_parser].filter(Boolean).join(" · "))}</span></span>
    <b>KV cache</b><span>${esc(a.kv_dtype || "auto")}${a.block_size ? " · block " + esc(a.block_size) : ""}${a.prefix_cache ? " · prefix cache" : ""}</span>
    ${a.speculative_config ? `<b>Speculative</b><span class="mono">${esc(JSON.stringify(a.speculative_config))}</span>` : ""}
    ${a.mods.length ? `<b>Mods</b><span>${a.mods.map(m => tag(m, "muted")).join(" ")} <a href="#/mods" class="small">manage</a></span>` : ""}
    ${a.extra_models.length ? `<b>Extra models</b><span class="mono">${a.extra_models.map(esc).join("<br>")}</span>` : ""}
    ${a.extra_vllm_args.length ? `<b>Raw vLLM args</b><span class="mono">${esc(a.extra_vllm_args.join(" "))}</span>` : ""}
    ${Object.keys(a.env || {}).length ? `<b>Env</b><span class="mono">${Object.entries(a.env).map(([k, v]) => esc(k + "=" + v)).join("<br>")}</span>` : ""}
  </div></div>`;
  html += `<div class="card"><h2>Memory</h2>${fitHtml(fit)}</div></div>`;
  if (src.title || src.url || src.ref || (src.requirements || []).length || (src.notes || []).length) {
    html += `<div class="card"><h2>Recipe provenance</h2><div class="kv">
      ${src.title ? `<b>Recipe</b><span>${esc(src.title)}</span>` : ""}
      ${src.url || src.ref ? `<b>Source</b><span>${/^https?:/.test(src.url || src.ref || "") ? `<a href="${esc(src.url || src.ref)}" target="_blank" rel="noopener">${esc(src.url || src.ref)}</a>` : esc(src.url || src.ref)}</span>` : ""}
      ${src.author ? `<b>Author</b><span>${esc(src.author)}</span>` : ""}
      ${src.measured && Object.keys(src.measured).length ? `<b>Measured</b><span>${Object.entries(src.measured).map(([k, v]) => tag(`${k.replace(/_/g, " ")}: ${v}`, "muted")).join(" ")}</span>` : ""}
    </div>
    ${(src.requirements || []).length ? `<div class="section-title">Needs</div><ul class="plain">${src.requirements.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    ${(src.notes || []).length ? `<div class="section-title">Notes</div><ul class="plain">${src.notes.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}</div>`;
  }
  return html;
}
function fitHtml(fit) {
  if (!fit) return empty("Unavailable");
  if (fit.nodes) return Object.entries(fit.nodes).map(([node, detail]) => `<div class="section-title">Node ${esc(node)} · ${esc(detail.model)}</div>${fitHtml(detail)}`).join('<hr class="sep">');
  let h = "";
  if (!fit.known) h += `<p class="muted small">${esc(fit.note || "model size unknown")}</p>`;
  else {
    const pool = fit.kv_pool_gib_per_node, w = fit.weights_gib_per_node, total = fit.budget.mem_total;
    h += `<div class="row between small"><span>${fit.fits ? tag("fits", "good") : tag("does not fit", "bad")} ${fit.full_concurrency_fits ? "" : tag("fewer parallel full-length sequences", "warn")}</span><span class="muted">gpu_memory_utilization ${esc(fit.gpu_memory_utilization)}</span></div>
      <div class="meter big" style="margin:10px 0 4px"><i style="width:${Math.min(100, w / total * 100)}%"></i><i class="cache" style="left:${Math.min(100, w / total * 100)}%;width:${Math.min(100, pool / total * 100)}%;background:rgba(74,222,128,.55)"></i></div>
      <div class="legend"><span><i style="background:var(--accent)"></i>weights ${gib(w)}/node</span><span><i style="background:rgba(74,222,128,.7)"></i>KV pool ≈ ${gib(pool)}/node</span></div>
      <div class="kv" style="margin-top:12px"><b>≈ KV tokens</b><span>${num(fit.est_kv_tokens)} <span class="muted">(${esc(fit.kv_bytes_per_token_source)} KV size)</span></span>
      <b>Full-length seqs</b><span>≈ ${num(fit.est_full_context_seqs, 1)} at ${num(fit.context_length)} tokens</span>
      <b>Headroom</b><span>${gib(fit.headroom_gib)} ${tag(fit.level, levelClass(fit.level))}</span></div>`;
  }
  const o = fit.observed;
  if (o && (o.kv || o.max_model_len)) {
    h += `<hr class="sep"><div class="small"><b>Last successful run:</b> ${o.kv && o.kv.kv_cache_tokens ? num(o.kv.kv_cache_tokens) + " KV tokens" : ""}${o.max_model_len ? ` · max_model_len ${num(o.max_model_len)}` : ""}${o.load_seconds ? ` · loaded in ${Math.round(o.load_seconds)} s` : ""} <span class="muted">(${esc(ago(o.at))})</span></div>`;
  }
  return h;
}

function profileSettings(name, p, d) {
  if (!d) return empty("No draft");
  const s = d.simple, a = d.advanced, b = d.behaviour;
  const spec = a.speculative_config || {};
  const f = (id, label, value, help = "", ph = "") => `<label class="field"><span>${esc(label)}</span><input id="q-${id}" value="${esc(value ?? "")}" placeholder="${esc(ph)}"/>${help ? `<span class="help">${esc(help)}</span>` : ""}</label>`;
  return `<div class="grid cols-2"><div class="card"><div class="card-head"><h2>Quick settings</h2><span class="small muted">edits the working draft · pin afterwards</span></div>
    <div class="form">
      ${f("ctx", "Context length", s.context_length, "tokens, or auto")}
      ${f("seqs", "Parallel sequences", a.max_num_seqs || s.concurrency)}
      ${f("util", "GPU memory utilization", a.gpu_memory_utilization ?? "", "empty = derived from the node's reserve", "auto")}
      ${f("kv", "KV cache dtype", a.kv_dtype ?? "", "", "auto")}
      ${f("mbt", "Max batched tokens", a.max_num_batched_tokens ?? "", "", "vLLM default")}
      ${f("spec", "Speculative tokens", spec.num_speculative_tokens ?? "", spec.method ? `method ${spec.method}` : "no speculative config", "—")}
      ${f("aliases", "Extra model names", (s.extra_aliases || []).join(", "), `primary: ${s.api_alias}`)}
      ${f("temp", "Temperature", b.temperature ?? "", "", "model default")}
    </div>
    <div class="row" style="margin-top:14px">
      <label class="check"><input type="checkbox" id="q-think" ${s.thinking ? "checked" : ""}/> thinking</label>
      <label class="check"><input type="checkbox" id="q-tools" ${s.tool_calling ? "checked" : ""}/> tool calling</label>
      <label class="check"><input type="checkbox" id="q-eager" ${a.eager_mode ? "checked" : ""}/> eager (no CUDA graphs)</label>
      <label class="check"><input type="checkbox" id="q-prefix" ${a.prefix_cache ? "checked" : ""}/> prefix cache</label>
    </div>
    <div class="row end" style="margin-top:14px"><button class="btn" data-act="quick-save">Save draft</button><button class="btn primary" data-act="quick-save-pin">Save &amp; pin</button></div>
    ${b.manage_thinking_kwarg === false ? `<p class="small muted" style="margin:10px 0 0">This recipe passes its own chat-template kwargs; the thinking switch only changes the label, edit the raw args in JSON for the real toggle.</p>` : ""}
  </div>
  <div class="card"><div class="card-head"><h2>Full draft (JSON)</h2><div class="row"><button class="btn sm" data-act="fmt">Format</button><button class="btn sm primary" data-act="json-save">Save draft</button></div></div>
    <textarea id="draft-json" rows="26" spellcheck="false">${esc(JSON.stringify(d, null, 2))}</textarea>
    <p class="small muted" style="margin:8px 0 0">Everything a recipe can express: raw vLLM args, env, mods, extra models, parsers. Host, port and parallel wiring are always set by TwinSpark.</p></div></div>`;
}

function profileRevisions(name, p, isActive, st) {
  if (!p.revisions.length) return `<div class="card">${empty("No revisions yet", "Pin the profile to create r1.")}</div>`;
  const revs = [...p.revisions].reverse();
  return `<div class="card"><div class="card-head"><h2>Revisions</h2><div class="row"><select id="cmp-a" style="width:auto">${revs.map((r, i) => `<option ${i === Math.min(1, revs.length - 1) ? "selected" : ""}>${esc(r.label)}</option>`).join("")}</select><span class="muted">→</span><select id="cmp-b" style="width:auto">${revs.map((r, i) => `<option ${i === 0 ? "selected" : ""}>${esc(r.label)}</option>`).join("")}</select><button class="btn sm" data-act="compare">Compare</button></div></div>
    <div class="table-wrap"><table><thead><tr><th>Rev</th><th>Created</th><th>Model commit</th><th>Image</th><th>vLLM</th><th>State</th><th></th></tr></thead><tbody>
    ${revs.map(r => `<tr><td><b>${esc(r.label)}</b></td><td>${esc(when(r.created_at))}</td><td class="mono">${esc(sha(r.identity.model_revision, 12))}</td>
      <td class="mono">${esc(r.identity.image)}<div class="sub">${esc(sha(r.identity.image_digest, 16))} · ${esc(r.identity.image_source)}</div></td><td>${esc(r.identity.vllm_version)}</td>
      <td>${r.known_good ? tag("known-good", "good") : ""} ${r.pinned ? tag("preferred", "info") : ""} ${isActive && st.active.revision_id === r.revision_id ? tag("serving", "good") : ""}</td>
      <td class="actions"><button class="btn sm" data-act="rev-activate" data-rev="${esc(r.label)}" ${st.busy ? "disabled" : ""}>Switch</button><button class="btn sm ghost" data-act="rev-prefer" data-rev="${esc(r.label)}">Prefer</button><a class="btn sm ghost" href="#/profiles/${enc(name)}/plan" data-act="rev-plan" data-rev="${esc(r.label)}">Plan</a></td></tr>`).join("")}
    </tbody></table></div><div id="cmp-out"></div></div>`;
}

async function profilePlan(name, p, ref) {
  const revs = p.revisions.map(r => r.label).reverse();
  const src = ref || (revs.length ? "latest" : "draft");
  let data;
  try {
    data = src === "draft" ? await GET(`/api/v1/profiles/${enc(name)}/draft/launch-plan`) : await GET(`/api/v1/profiles/${enc(name)}/revisions/${enc(src)}/launch-plan`);
  } catch (e) { return `<div class="callout bad"><div>${esc(e.message)}</div></div>`; }
  const plan = data.plan;
  let h = `<div class="card"><div class="card-head"><h2>Exact commands</h2><div class="row"><span class="small muted">render</span><select id="plan-src" style="width:auto"><option value="draft" ${src === "draft" ? "selected" : ""}>working draft</option>${revs.length ? `<option value="latest" ${src === "latest" ? "selected" : ""}>latest revision</option>` : ""}${revs.map(r => `<option value="${esc(r)}" ${src === r ? "selected" : ""}>${esc(r)}</option>`).join("")}</select></div></div>`;
  if (data.unpinned) h += `<div class="callout warn" style="margin-bottom:12px"><div>Draft — the model sha and image digest below are placeholders until you pin.</div></div>`;
  h += `<div class="kv" style="margin-bottom:12px"><b>Backend</b><span>${esc(plan.backend)}</span><b>GPU memory util.</b><span>${Object.entries(plan.node_utilization || {}).map(([n,v]) => `Node ${esc(n)}: ${esc(v)}`).join(" · ") || esc(plan.gpu_memory_utilization)}</span><b>Routes</b><span>${Object.entries(plan.routes).map(([a, u]) => `${tag(a, "info")} → <span class="mono">${esc(plan.route_models?.[a] || plan.served_model_name)} @ ${esc(u.join(", "))}</span>`).join("<br>")}</span></div>`;
  if (plan.notes.length) h += `<ul class="plain" style="margin-bottom:12px">${plan.notes.map(n => `<li class="${n.startsWith("WARNING") ? "" : "muted"}" ${n.startsWith("WARNING") ? 'style="color:var(--warn)"' : ""}>${esc(n)}</li>`).join("")}</ul>`;
  plan.start_order.forEach((wave, i) => {
    wave.forEach(cname => {
      const c = plan.containers.find(x => x.name === cname);
      h += `<div class="section-title">Wave ${i + 1} · node ${esc(c.node)} · ${esc(c.role)}${plan.wave_delays_s[i] && i < plan.start_order.length - 1 ? ` · then wait ${esc(plan.wave_delays_s[i])} s` : ""}</div>${codeBlock(prettyCmd(data.commands[cname]), "wrap")}`;
    });
  });
  return h + `</div>`;
}
function prettyCmd(cmd) { return cmd.replace(/ (?=--?[a-zA-Z])/g, " \\\n  ").replace(/ (-e|-v|--label) \\\n  /g, " $1 "); }

function profileActions(name, p, d) {
  const saveDraft = async (draft, andPin) => {
    await PUT(`/api/v1/profiles/${enc(name)}/draft`, draft);
    toast("draft saved", "good");
    if (andPin) { const ok = await pinFlow(name); if (ok) return go(`#/profiles/${enc(name)}/overview`); }
    route();
  };
  const quick = () => {
    const x = JSON.parse(JSON.stringify(d));
    const v = (id) => $("#q-" + id).value.trim();
    const ctx = v("ctx"); x.simple.context_length = ctx === "auto" ? "auto" : parseInt(ctx, 10);
    const seqs = parseInt(v("seqs"), 10); if (seqs) { x.simple.concurrency = Math.min(seqs, 512); x.advanced.max_num_seqs = seqs; }
    x.advanced.gpu_memory_utilization = v("util") && v("util") !== "auto" ? parseFloat(v("util")) : null;
    x.advanced.kv_dtype = v("kv") || null;
    x.advanced.max_num_batched_tokens = v("mbt") ? parseInt(v("mbt"), 10) : null;
    const sp = v("spec");
    if (x.advanced.speculative_config && sp) x.advanced.speculative_config.num_speculative_tokens = parseInt(sp, 10);
    x.simple.extra_aliases = v("aliases") ? v("aliases").split(",").map(s => s.trim()).filter(Boolean) : [];
    x.behaviour.temperature = v("temp") ? parseFloat(v("temp")) : null;
    x.simple.thinking = $("#q-think").checked; x.simple.tool_calling = $("#q-tools").checked;
    x.advanced.eager_mode = $("#q-eager").checked; x.advanced.prefix_cache = $("#q-prefix").checked;
    return x;
  };
  return {
    "quick-save": () => saveDraft(quick(), false),
    "quick-save-pin": () => saveDraft(quick(), true),
    fmt: () => { const t = $("#draft-json"); try { t.value = JSON.stringify(JSON.parse(t.value), null, 2); } catch (e) { toast("invalid JSON: " + e.message, "bad"); } },
    "json-save": () => { let doc; try { doc = JSON.parse($("#draft-json").value); } catch (e) { throw new Error("invalid JSON: " + e.message); } return saveDraft(doc, false); },
    "rev-activate": (el) => activateFlow(name, el.dataset.rev, el),
    "rev-prefer": async (el) => { await POST(`/api/v1/profiles/${enc(name)}/revisions/${enc(el.dataset.rev)}/pin`); toast(`${el.dataset.rev} is now the preferred revision`, "good"); route(); },
    "rev-plan": (el) => { S.planRef = el.dataset.rev; go(`#/profiles/${enc(name)}/plan`); },
    compare: async () => {
      const r = await GET(`/api/v1/profiles/${enc(name)}/compare${qs({ a: $("#cmp-a").value, b: $("#cmp-b").value })}`);
      const rows = Object.entries(r.changed || {});
      $("#cmp-out").innerHTML = `<div class="section-title">${esc($("#cmp-a").value)} → ${esc($("#cmp-b").value)}</div>` + (rows.length ? `<div class="table-wrap"><table><thead><tr><th>Setting</th><th>From</th><th>To</th></tr></thead><tbody>${rows.map(([k, v]) => `<tr><td class="mono">${esc(k)}</td><td class="mono">${esc(JSON.stringify(v.from))}</td><td class="mono">${esc(JSON.stringify(v.to))}</td></tr>`).join("")}</tbody></table></div>` : `<p class="muted small">identical</p>`);
    },
  };
}
function bindPlanSrc(name, p) {
  const sel = $("#plan-src"); if (!sel) return;
  sel.onchange = async () => {
    const g = S.gen; const html = await profilePlan(name, p, sel.value);
    if (!current(g)) return;
    const card = sel.closest(".card"); card.insertAdjacentHTML("afterend", html); card.remove(); bindPlanSrc(name, p);
  };
}

/* ============================== integrations ============================== */
function compatibilityChecksHtml(checks, dryRun = false) {
  if (!checks?.length) return `<p class="muted small">No checks recorded yet.</p>`;
  const labels = { models: "Model discovery", chat: "Conversation", streaming: "Streaming replies", tools: "Tool calling", structured: "Structured replies", json_schema: "Structured replies", responses: "Responses API" };
  return `<div class="list">${checks.map(check => {
    const raw = String(check.status || "pending").toLowerCase();
    const status = dryRun && ["ok", "pass", "passed"].includes(raw) ? "simulated" : raw;
    const label = { ok: "Passed", pass: "Passed", passed: "Passed", fail: "Failed", failed: "Failed", unsupported: "Unsupported", simulated: "Simulated", skipped: "Skipped", pending: "Pending", running: "Checking" }[status] || status;
    const cls = ["ok", "pass", "passed"].includes(status) ? "good" : ["fail", "failed"].includes(status) ? "bad" : ["unsupported", "skipped"].includes(status) ? "warn" : status === "simulated" ? "info" : "muted";
    return `<div class="item"><div class="l"><div class="t">${esc(labels[check.name] || String(check.name || "Check").replace(/[_-]/g, " "))} ${tag(label, cls)}</div><div class="d">${esc(check.message || "")}</div></div></div>`;
  }).join("")}</div>`;
}
function integrationAliasHtml(alias) {
  if (!alias) return empty("No model selected", "Activate a recipe to make a model available to clients.");
  const toolsEnabled = Array.isArray(alias.configured_tools) ? alias.configured_tools.length > 0 : !!alias.configured_tools;
  const configured = Array.isArray(alias.configured_tools) ? alias.configured_tools.join(", ") || "Off" : alias.configured_tools ? "Enabled" : "Off";
  const context = typeof alias.context_length === "number" && Number.isFinite(alias.context_length) && alias.context_length > 0 ? num(alias.context_length) + " tokens" + (alias.context_source === "observed" ? " · observed" : alias.context_source === "recipe" ? " · recipe setting" : "") : "Auto — not measured";
  const latest = alias.latest_check;
  return `<div class="card"><div class="card-head"><h2>Your recipe connection</h2>${tag(alias.status || "unknown", alias.status === "serving" ? "good" : "muted")}</div>
    <div class="kv"><b>Model name for clients</b><span class="mono">${esc(alias.alias)}</span><b>Profile</b><span><a href="#/profiles/${enc(alias.profile)}">${esc(alias.profile)}</a></span><b>Revision</b><span class="mono">${esc(alias.revision_id || "Not pinned")}</span><b>Runs on</b><span>${esc(alias.node ? `Node ${alias.node}` : alias.topology || "—")}</span><b>Context length</b><span>${esc(context)}</span><b>Tool calling</b><span>${esc(configured)}</span><b>Tool parser</b><span>${esc(alias.tool_parser || "Not configured")}</span></div>
    ${alias.ambiguous ? '<p class="small muted">Several profiles use this model name. Activate the intended profile or give the profiles distinct aliases before exporting its connection.</p>' : ""}
    ${!toolsEnabled || !alias.tool_parser ? '<p class="small muted">Agent workflows usually need tool calling. Enable it and choose a parser that matches the model in the recipe before testing.</p>' : ""}
    <div class="section-title">Latest compatibility check</div>${latest ? `<p class="small">${tag(latest.dry_run ? "Simulation" : "Live check", latest.dry_run ? "info" : "muted")} ${tag(latest.state || "unknown", latest.state === "failed" ? "bad" : "muted")}${latest.revision_id && latest.revision_id !== alias.revision_id ? ` <span class="muted">Recorded for revision ${esc(latest.revision_id)}. Check this revision again.</span>` : ""}</p>${compatibilityChecksHtml(latest.checks, latest.dry_run)}` : '<p class="small muted">Run a compatibility check to see what this recipe can support.</p>'}</div>`;
}
function integrationClientHtml(client) {
  if (!client) return empty("No clients available");
  let docs = "";
  try { if (new URL(client.docs_url).protocol === "https:") docs = `<a href="${esc(client.docs_url)}" target="_blank" rel="noopener noreferrer">Setup guide ↗</a>`; } catch (_) { }
  return `<h2>${esc(client.title)}</h2><p>${esc(client.description || "")}</p>${client.requirements?.length ? `<ul class="plain">${client.requirements.map(r => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}${docs ? `<p class="small">${docs}</p>` : ""}`;
}
function integrationBaseUrl(value) {
  let url;
  try { url = new URL(value.trim()); } catch (_) { throw new Error("Enter a full gateway address, such as http://192.168.1.10:8000/v1."); }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error("Use an http or https gateway address without a key, password, query or fragment.");
  return url.toString().replace(/\/+$/, "");
}
function integrationFilesHtml(result) {
  return `<div class="card"><h2>Connection files</h2><p class="small muted">Save the files where the client runs and follow its setup guide. Replace environment placeholders there with your own values.</p>${(result.notes || []).map(n => `<p class="small">${esc(n)}</p>`).join("")}
    ${(result.files || []).map((file, i) => `<div class="integration-file"><div class="row between"><b class="mono">${esc(file.name)}</b><button class="btn sm" data-act="integration-download" data-index="${i}">Download file</button></div>${codeBlock(file.content)}</div>`).join("")}</div>`;
}
function downloadIntegrationFile(file) {
  const href = URL.createObjectURL(new Blob([file.content], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = href; link.download = String(file.name || "connection.txt").split(/[\\/]/).pop();
  document.body.appendChild(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}
async function readEvaluationReport(file) {
  if (!file) throw new Error("Choose an LM Evaluation Harness results JSON file.");
  if (file.size > 1024 * 1024) throw new Error("Report too large (1 MiB maximum). Use the results JSON without sample logs.");
  let document;
  try { document = JSON.parse(await file.text()); }
  catch (_) { throw new Error("The file is not valid JSON. Choose the harness results file."); }
  if (!document || typeof document !== "object" || Array.isArray(document) || !document.results || typeof document.results !== "object" || Array.isArray(document.results)) throw new Error("Expected an LM Evaluation Harness results object.");
  const results = Object.create(null);
  for (const [task, values] of Object.entries(document.results)) {
    if (!values || typeof values !== "object" || Array.isArray(values)) throw new Error("Each evaluation task must contain metric values.");
    const metrics = Object.create(null);
    for (const [name, value] of Object.entries(values)) if (typeof value === "number") {
      if (!Number.isFinite(value)) throw new Error("Evaluation metric values must be finite numbers.");
      metrics[name] = value;
    }
    if (Object.keys(metrics).length) results[task] = metrics;
  }
  if (!Object.keys(results).length) throw new Error("The report contains no numeric evaluation metrics.");
  // Retain only attribution and sample count from the client configuration.
  const config = {};
  const sourceConfig = document.config || {};
  let args = sourceConfig.model_args;
  if (typeof args === "string") args = Object.fromEntries(args.split(",").filter(x => x.includes("=")).map(x => { const i = x.indexOf("="); return [x.slice(0, i).trim(), x.slice(i + 1).trim()]; }));
  if (args && typeof args === "object" && Object.hasOwn(args, "model")) {
    if (typeof args.model !== "string") throw new Error("The reported model alias must be plain text.");
    config.model_args = { model: args.model };
  }
  if (typeof sourceConfig.limit === "number" && Number.isFinite(sourceConfig.limit)) config.limit = sourceConfig.limit;
  return { results, config };
}
function evaluationMetricsHtml(payload) {
  const rows = Object.entries(payload.metrics || {}).flatMap(([task, metrics]) => Object.entries(metrics || {}).filter(([, value]) => typeof value === "number" && Number.isFinite(value)).map(([metric, value]) => `<tr><td>${esc(task)}</td><td class="mono">${esc(metric)}</td><td class="mono num">${esc(String(value))}</td></tr>`));
  return `<div class="callout warn"><div><b>Reported, not independently verified.</b> ${esc(payload.note || "Imported client results. Recipe attribution and hardware execution have not been verified.")}</div></div><div class="card"><h2>Evaluation results</h2><div class="kv"><b>Model connection</b><span class="mono">${esc(payload.alias)}</span><b>Recipe revision</b><span class="mono">${esc(payload.revision_id)}</span><b>Source</b><span>${esc(payload.source || "lm-evaluation-harness")}</span><b>Sample limit</b><span>${payload.sample_limit == null ? "Not reported" : esc(String(payload.sample_limit))}</span></div>${rows.length ? `<div class="table-wrap integration-actions"><table><thead><tr><th>Task</th><th>Metric</th><th class="num">Value</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>` : '<p class="small muted">No numeric metrics recorded.</p>'}</div>`;
}
async function viewIntegrations() {
  const g = S.gen;
  const catalog = await GET("/api/v1/integrations");
  if (!current(g)) return;
  const aliases = catalog.aliases || [], clients = catalog.clients || [];
  const state = S.integrations || (S.integrations = {});
  if (!aliases.some(a => a.alias === state.alias)) state.alias = (aliases.find(a => a.status === "serving") || aliases[0])?.alias || "";
  if (!aliases.some(a => a.alias === state.secondaryAlias) || state.secondaryAlias === state.alias) state.secondaryAlias = "";
  if (!clients.some(c => c.id === state.client)) state.client = clients[0]?.id || "";
  if (!state.baseUrl) state.baseUrl = catalog.gateway?.suggested_base_url || "";
  if (!render(g, `<div class="view"><div class="view-head"><div><p class="eyebrow">Connect · check · experiment</p><h1>Integrations</h1><p>Connect your recipes to coding agents, harnesses and other model clients.</p></div><button class="btn" data-act="integration-refresh">Refresh connections</button></div>
    <div class="card"><div class="form"><label class="field"><span>Model connection</span><select id="integration-alias">${aliases.map(a => `<option value="${esc(a.alias)}" ${a.alias === state.alias ? "selected" : ""}>${esc(a.alias)} · ${esc(a.profile)}</option>`).join("")}</select></label><label class="field"><span>Second model (optional)</span><select id="integration-secondary"><option value="">None</option>${aliases.map(a => `<option value="${esc(a.alias)}" ${a.alias === state.secondaryAlias ? "selected" : ""}>${esc(a.alias)} · ${esc(a.profile)}</option>`).join("")}</select><span class="help">For clients that separate planning and coding, or use a smaller helper model.</span></label><label class="field"><span>Client or harness</span><select id="integration-client">${clients.map(c => `<option value="${esc(c.id)}" ${c.id === state.client ? "selected" : ""}>${esc(c.title)}</option>`).join("")}</select></label><label class="field"><span>Gateway address</span><input id="integration-url" type="url" autocomplete="off" spellcheck="false" value="${esc(state.baseUrl)}" placeholder="http://192.168.1.10:8000/v1"/><span class="help">Use the manager's address as seen from the client machine.</span></label></div>
    ${catalog.gateway?.auth_required ? '<p class="small muted">The connection files use an API key placeholder. Set your gateway key on the client machine.</p>' : ""}<p class="err hidden" id="integration-error" role="alert"></p><div class="row end integration-actions"><button class="btn" id="integration-check-button" data-act="integration-check" ${aliases.find(a => a.alias === state.alias)?.status !== "serving" ? "disabled" : ""}>Run compatibility check</button><button class="btn primary" id="integration-export-button" data-act="integration-export" ${!state.alias || !state.client ? "disabled" : ""}>Create connection files</button></div></div>
    ${!aliases.length ? `<div class="callout info"><div><b>No model connections yet.</b> Prepare and activate a profile to connect a client. <a href="#/profiles">Open profiles →</a></div></div>` : ""}
    <div class="grid integration-details"><div id="integration-recipe" class="integration-recipes"></div><div class="card" id="integration-client-info"></div></div><div id="integration-files" aria-live="polite"></div>
    <div class="card"><h2>Record evaluation results</h2><p class="small muted">Attach an LM Evaluation Harness report to the exact recipe revision you tested. Results are recorded as reported evidence; only numeric metrics and the sample limit are saved.</p><div class="form"><label class="field"><span>Exact recipe revision</span><input id="evaluation-revision" autocomplete="off" maxlength="128" spellcheck="false"/><span class="help">Prefilled from the selected model. Enter an older revision ID when that is what you tested.</span></label><label class="field"><span>Results JSON (up to 1 MiB)</span><input id="evaluation-file" type="file" accept=".json,application/json"/></label></div><p class="err hidden" id="evaluation-error" role="alert"></p><div class="row end integration-actions"><button class="btn" id="evaluation-record-button" data-act="evaluation-record" disabled>Record results</button></div></div></div>`)) return;
  $("#integration-alias").value = state.alias; $("#integration-secondary").value = state.secondaryAlias; $("#integration-client").value = state.client; $("#integration-url").value = state.baseUrl;
  $("#evaluation-revision").value = aliases.find(a => a.alias === state.alias)?.revision_id || "";
  let epoch = 0, exported = null, pendingRequests = 0, evaluationEpoch = 0, evaluationPending = false;
  const selected = () => ({ alias: $("#integration-alias").value, secondary_alias: $("#integration-secondary").value, client: $("#integration-client").value, base_url: $("#integration-url").value });
  const display = () => {
    const values = selected(); Object.assign(state, { alias: values.alias, secondaryAlias: values.secondary_alias, client: values.client, baseUrl: values.base_url });
    const primary = aliases.find(a => a.alias === values.alias), secondary = aliases.find(a => a.alias === values.secondary_alias);
    $("#integration-recipe").innerHTML = integrationAliasHtml(primary) + (secondary && secondary !== primary ? integrationAliasHtml(secondary) : "") + (primary?.status !== "serving" ? '<p class="small muted">Connection files can be prepared now. Activate this recipe to run model compatibility checks.</p>' : "");
    $("#integration-check-button").disabled = pendingRequests > 0 || primary?.status !== "serving";
    $("#integration-export-button").disabled = pendingRequests > 0 || !values.alias || !values.client;
    $("#evaluation-record-button").disabled = evaluationPending || !values.alias || !$("#evaluation-revision").value.trim() || !$("#evaluation-file").files?.length;
    $("#integration-client-info").innerHTML = integrationClientHtml(clients.find(c => c.id === values.client));
  };
  const invalidate = () => { epoch++; exported = null; $("#integration-files").innerHTML = ""; $("#integration-error").classList.add("hidden"); display(); };
  const evaluationChanged = () => { evaluationEpoch++; $("#evaluation-error").classList.add("hidden"); display(); };
  $("#integration-alias").onchange = () => { $("#evaluation-revision").value = aliases.find(a => a.alias === selected().alias)?.revision_id || ""; $("#evaluation-file").value = ""; evaluationChanged(); invalidate(); };
  $("#integration-secondary").onchange = invalidate; $("#integration-client").onchange = invalidate; $("#integration-url").oninput = invalidate;
  $("#evaluation-revision").oninput = evaluationChanged; $("#evaluation-file").onchange = evaluationChanged;
  const request = async (btn, fn) => {
    const token = ++epoch;
    pendingRequests++; display();
    $("#integration-error").classList.add("hidden");
    try { await busy(btn, () => fn(token)); }
    catch (error) { if (current(g) && token === epoch) { $("#integration-error").textContent = error.message || String(error); $("#integration-error").classList.remove("hidden"); } }
    finally { pendingRequests--; if (current(g)) display(); }
  };
  onAct({
    "integration-refresh": () => route(),
    "integration-export": btn => request(btn, async token => {
      const values = selected();
      if (!values.alias || !values.client) throw new Error("Choose a model connection and a client first.");
      if (values.secondary_alias && values.alias === values.secondary_alias) throw new Error("Choose different connections for the main and second model.");
      values.base_url = integrationBaseUrl(values.base_url);
      const result = await GET("/api/v1/integrations/export" + qs(values));
      if (!current(g) || token !== epoch) return;
      exported = result; $("#integration-files").innerHTML = integrationFilesHtml(result);
    }),
    "integration-check": btn => request(btn, async token => {
      const values = selected();
      if (aliases.find(a => a.alias === values.alias)?.status !== "serving") throw new Error("Activate this recipe before running compatibility checks.");
      const job = await POST("/api/v1/integrations/check", { alias: values.alias });
      if (current(g) && token === epoch) go("#/jobs/" + enc(job.job_id));
    }),
    "integration-download": btn => { const file = exported?.files?.[Number(btn.dataset.index)]; if (file) downloadIntegrationFile(file); },
    "evaluation-record": async btn => {
      const token = ++evaluationEpoch, alias = selected().alias, revision = $("#evaluation-revision").value.trim(), file = $("#evaluation-file").files?.[0];
      evaluationPending = true; display(); $("#evaluation-error").classList.add("hidden");
      try {
        await busy(btn, async () => {
          if (!alias || !revision) throw new Error("Choose a model and its exact pinned recipe revision.");
          const results = await readEvaluationReport(file);
          if (!current(g) || token !== evaluationEpoch) return;
          if (results.config.model_args && results.config.model_args.model !== alias) throw new Error("The report's model alias differs from the selected connection. Choose the model and revision used for this evaluation.");
          const body = { alias, revision_id: revision, results };
          if (new TextEncoder().encode(JSON.stringify(body)).byteLength > 1024 * 1024) throw new Error("Report too large (1 MiB maximum). Use fewer tasks or the results JSON without sample logs.");
          const job = await POST("/api/v1/integrations/evaluations", body);
          if (current(g) && token === evaluationEpoch) go("#/jobs/" + enc(job.job_id));
        });
      } catch (error) { if (current(g) && token === evaluationEpoch) { $("#evaluation-error").textContent = error.message || String(error); $("#evaluation-error").classList.remove("hidden"); } }
      finally { evaluationPending = false; if (current(g)) display(); }
    },
  });
  display();
}

/* ============================== jobs ============================== */
const STAGES = ["validating", "resolving", "downloading", "syncing", "draining", "stopping", "reclaiming", "starting-cluster", "loading", "testing", "routing", "healthy"];
const STAGE_HELP = {
  validating: "memory fit + launch plan", resolving: "preflight on both nodes, image, mods, RDMA", downloading: "weights from the Hub (skipped when present)",
  syncing: "copy to the other node over QSFP", draining: "let in-flight requests finish", stopping: "stop the previous model",
  reclaiming: "drop page cache, wait for memory", "starting-cluster": "worker first, then head", loading: "weights, compile, CUDA graphs",
  testing: "real completion through the API", routing: "gateway switches the model names", healthy: "done",
};
async function viewJob(id) {
  const g = S.gen;
  const draw = async () => {
    const fresh = newest();
    const job = await GET(`/api/v1/jobs/${enc(id)}`);
    if (!fresh()) return true;
    if (!renderLive(g, jobHtml(job), "job:" + id)) return false;
    return ["running", "pending"].includes(job.state);
  };
  onAct({
    cancel: async () => { const r = await POST(`/api/v1/jobs/${enc(id)}/cancel`); toast(r.note); },
    logs: () => go("#/logs"),
    "compatibility-retry": async (el) => {
      const previous = await GET(`/api/v1/jobs/${enc(id)}`);
      if (!current(g)) return;
      const job = await busy(el, () => POST("/api/v1/integrations/check", { alias: previous.payload.alias }));
      if (current(g)) go("#/jobs/" + enc(job.job_id));
    },
    "prepared-activate": async (el) => {
      const job = await GET(`/api/v1/jobs/${enc(id)}`);
      await activateFlow(job.payload.profile, job.profile_revision, el);
    },
    "integration-retry": async (el) => {
      const job = await busy(el, () => POST(`/api/v1/cookbook/integration/${enc(id)}/retry`, {request_id:recipeRequestId()}));
      go("#/jobs/" + enc(job.job_id));
    },
    "evidence-log": async (el) => {
      const text = await busy(el, () => apiText(`/api/v1/jobs/${enc(id)}/evidence/${enc(el.dataset.file)}`));
      const href = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
      const a = document.createElement("a"); a.href = href; a.download = `${id}-${el.dataset.file}`; document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(href), 1000);
    },
  });
  if (await draw()) poll(draw, 1500);
}
const PHASE_ICON = { image: "◫", checkpoint: "▣", drafter: "▤", verify: "✓", copy: "⇄" };
function phasesHtml(items) {
  if (!items || !items.length) return "";
  const cls = { done: "good", reused: "info", failed: "bad", skipped: "muted", running: "warn" };
  const word = { done: "done", reused: "reused", failed: "failed", skipped: "skipped", running: "in progress" };
  return `<div class="card"><h2>Work items</h2><p class="small muted">Image pulls, main weights, drafter weights, checksum verification and copies over QSFP — each with the exact revision.</p>
    <div class="list">${items.map(x => `<div class="item" data-key="phase:${esc(x.key)}"><div class="l"><div class="t">${esc(PHASE_ICON[x.kind] || "•")} ${esc(x.label)} ${tag(word[x.state] || x.state, cls[x.state] || "muted")}</div>
      <div class="d">${esc(x.detail || "")}${x.ref ? ` <span class="mono faint" title="${esc(x.ref)}">${esc(x.ref.length > 90 ? x.ref.slice(0, 90) + "…" : x.ref)}</span>` : ""}</div>
      ${x.state === "running" && x.progress != null ? `<div class="progress-line"><i style="width:${Math.round(x.progress * 100)}%"></i></div>` : ""}</div></div>`).join("")}</div></div>`;
}
function evidenceHtml(ev) {
  if (!ev || !(ev.containers || []).length) return "";
  const exitText = (c) => c.error ? `no evidence: ${c.error}` : c.status === "missing" ? "no such container (it never started, or was already removed)" :
    [c.status, c.exit_code != null && c.status !== "running" ? `exit code ${c.exit_code}` : "", c.oom_killed ? "killed by the kernel OOM killer" : ""].filter(Boolean).join(", ");
  return `<div class="card"><div class="card-head"><h2>Startup evidence</h2><span class="small muted">saved before cleanup removed the containers · ${esc(ev.profile || "")} ${esc(ev.revision_id || "")}</span></div>
    <p class="small muted">Image ${(ev.images || []).map(i => `<span class="mono">${esc(i)}</span>`).join(", ") || "—"}. Logs are redacted, at most ${bytes(ev.limits?.max_log_chars)} each (the end is kept); the newest ${esc(ev.limits?.kept_jobs ?? "")} failed jobs keep theirs. An exit code 1 does not rule out a GPU memory failure — compare the kernel messages and memory below.</p>
    ${ev.containers.map(c => { const m = c.memory || {}; return `<div class="evidence" data-key="ev:${esc(c.node)}:${esc(c.name)}">
      <div class="row between"><b>Node ${esc(c.node)} · <span class="mono">${esc(c.name)}</span> ${c.role ? tag(c.role, "muted") : ""}</b>${c.file ? `<button class="btn sm" data-act="evidence-log" data-file="${esc(c.file)}">Download log (${bytes(c.bytes)}${c.log_truncated ? ", end of a longer log" : ""})</button>` : ""}</div>
      <div class="kv"><b>State</b><span>${esc(exitText(c))}</span>
        <b>Memory after the failure</b><span>${m.mem_available_gib != null ? `${gib(m.mem_available_gib)} available of ${gib(m.mem_total_gib)} · page cache ${gib(m.page_cache_gib)} · swap used ${gib(m.swap_used_gib)}` : "—"}</span>
        <b>NVIDIA kernel messages</b><span>${c.kernel_gpu_mem_errors ? `<span style="color:var(--bad)">${c.kernel_count_at_least ? "at least " : ""}${num(c.kernel_gpu_mem_errors)} out-of-memory message(s) since the container started</span>${c.kernel_note ? `<div class="sub">${esc(c.kernel_note)}</div>` : ""}` : c.kernel_gpu_mem_errors === 0 ? `no out-of-memory messages${c.kernel_note ? `<div class="sub">${esc(c.kernel_note)}</div>` : ""}` : esc(c.kernel_note || "not read")}</span></div>
      ${c.first_error ? `<div class="section-title">First error</div>${codeBlock(c.first_error, "log")}` : ""}
      ${c.final_error ? `<details><summary>Final error</summary>${codeBlock(c.final_error, "log")}</details>` : ""}
      ${(c.kernel_samples || []).length ? `<details><summary>Kernel messages (samples)</summary>${codeBlock(c.kernel_samples.join("\n"), "log")}</details>` : ""}
    </div>`; }).join("")}</div>`;
}
/** A failed start stays "running" while its logs are saved and its containers removed: nothing to cancel then. */
function cleaningUp(job) { return ["running", "pending"].includes(job.state) && (job.steps || []).some(s => s.status === "failed"); }
function jobHtml(job) {
  const running = ["running", "pending"].includes(job.state);
  const activation = ["activation", "rollback", "recovery"].includes(job.kind);
  const preparation = ["prepare", "integration"].includes(job.kind);
  const compatibility = job.kind === "compatibility";
  const evaluation = job.kind === "evaluation";
  const done = Object.fromEntries((job.steps || []).map(s => [s.stage, s]));
  const stages = activation ? STAGES : preparation ? [...(job.kind === "integration" ? ["pinning"] : []), ...STAGES.slice(0, 4)] : (job.steps || []).map(s => s.stage);
  const pl = job.payload || {};
  const title = activation ? `${job.kind === "activation" ? "Switch to" : job.kind === "rollback" ? "Roll back to" : "Recover"} ${pl.profile || ""} ${pl.label || ""}` : preparation ? `Prepare ${pl.profile || ""} ${pl.revision || ""}` : compatibility ? `Check connection ${pl.alias || ""}` : evaluation ? `Evaluation report ${pl.alias || ""}` : job.kind === "stage" ? `Stage ${pl.repo || ""}@${sha(pl.revision, 8)}` : job.kind;
  let h = `<div class="view" data-live="job:${esc(job.job_id)}"><div class="view-head"><div><div class="crumbs"><a href="#/jobs">Jobs</a> / <span class="mono">${esc(job.job_id)}</span></div>
    <h1>${esc(title)} ${tag(job.state, { completed: "good", failed: "bad", running: "info", cancelled: "muted" }[job.state] || "muted")}</h1><p>started ${esc(when(job.created_at))} · ${esc(dur(job.created_at, running ? null : job.updated_at))}</p></div>
    <div class="row">${running && !cleaningUp(job) ? `<button class="btn danger" data-act="cancel">Cancel</button>` : ""}${activation ? `<button class="btn" data-act="logs">Container logs</button>` : ""}${compatibility || evaluation ? '<a class="btn" href="#/integrations">Integrations</a>' : ""}${pl.profile ? `<a class="btn" href="#/profiles/${enc(pl.profile)}">Profile</a>` : ""}</div></div>`;
  if (cleaningUp(job)) {
    h += `<div class="callout warn"><div><b>${esc(job.error || "failed")}</b><div style="margin-top:6px"><span class="spin"></span> ${esc(job.rollback || "cleaning up")}</div></div></div>`;
  } else if (job.state === "failed") {
    h += `<div class="callout bad"><div><b>${esc(job.error || "failed")}</b>${(job.guidance || []).length ? `<ul>${job.guidance.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}${job.rollback ? `<div style="margin-top:6px"><b>Rollback:</b> ${esc(job.rollback)}</div>` : ""}</div></div>`;
  } else if (job.state === "completed" && activation) {
    h += `<div class="callout good"><div><b>Serving.</b> ${pl.kv && pl.kv.kv_cache_tokens ? `KV pool ${num(pl.kv.kv_cache_tokens)} tokens. ` : ""}${pl.max_model_len ? `max_model_len ${num(pl.max_model_len)}.` : ""}</div></div>`;
  }
  if (preparation) h += `<div class="callout ${job.state === "completed" ? "good" : "info"}"><div><b>${job.state === "completed" ? "Preparation complete." : job.state === "failed" ? "Preparation stopped." : "Preparing recipe."}</b> Images, patches and weights are checked without switching the current deployment.${pl.dry_run ? " This was a simulation; real hardware still needs testing." : ""}${job.state === "completed" ? '<div class="row" style="margin-top:10px"><button class="btn primary" data-act="prepared-activate">Switch to prepared revision</button></div>' : job.kind === "integration" && job.state === "failed" ? '<div class="row" style="margin-top:10px"><button class="btn" data-act="integration-retry">Retry preparation</button><a class="btn" href="#/mods">Check patches</a></div>' : ""}</div></div>`;
  if (job.kind === "integration" && pl.resolved) h += `<div class="card"><h2>Recorded pins</h2><div class="kv">${[pl.resolved, pl.resolved.secondary].filter(Boolean).map((r,i) => `<b>${i ? "Node B" : pl.resolved.secondary ? "Node A" : "Model"}</b><span class="mono">${esc(r.model)}<br>${esc(r.image)}</span>`).join("")}</div>${(pl.pin_notes || []).map(n => `<p class="small muted">${esc(n)}</p>`).join("")}</div>`;
  if (compatibility) h += `<div class="callout ${pl.dry_run ? "info" : ""}"><div><b>${pl.dry_run ? "Simulation — hardware compatibility still needs testing." : "Checks against the model connection."}</b> ${running ? "Results appear as each check finishes. " : ""}Run the client's own test after connecting.${pl.revision_id ? `<div class="small">Recipe revision: <span class="mono">${esc(pl.revision_id)}</span></div>` : ""}</div></div><div class="card"><div class="card-head"><h2>Compatibility results</h2>${!running ? '<button class="btn sm" data-act="compatibility-retry">Check again</button>' : ""}</div>${compatibilityChecksHtml(pl.checks, pl.dry_run)}</div>`;
  if (evaluation) h += evaluationMetricsHtml(pl);
  for (const w of pl.warnings || []) h += `<div class="callout warn"><div>${esc(w)}</div></div>`;
  if (!compatibility) h += `<div class="card"><div class="timeline">`;
  for (const st of compatibility ? [] : stages) {
    const s = done[st];
    const cls = s ? s.status : "pending";
    const p = s && s.status === "running" ? Math.round((s.progress || 0) * 100) : 0;
    h += `<div class="step ${esc(cls)}" data-key="stage:${esc(st)}"><div class="ic">${cls === "ok" ? "✓" : cls === "failed" ? "✕" : ""}</div>
      <div class="name">${esc(st)}</div>
      <div class="msg">${s ? esc(s.message || "") : `<span class="faint">${esc(STAGE_HELP[st] || "")}</span>`}${s && s.status === "running" ? `<div class="progress-line ${p ? "" : "indeterminate"}"><i style="width:${p}%"></i></div>` : ""}</div>
      <div class="dur">${s ? esc(dur(s.started_at, s.finished_at)) : ""}</div></div>`;
  }
  if (!compatibility) h += `</div></div>`;
  h += phasesHtml(pl.phases);
  if (pl.evidence_error) h += `<div class="callout warn"><div>${esc(pl.evidence_error)}</div></div>`;
  h += evidenceHtml(pl.startup_evidence);
  const excerpt = (job.steps || []).map(s => s.log_excerpt).filter(Boolean).pop();
  // the evidence card already shows each rank's errors; the step's excerpt stays when it has none (agent unreachable)
  const evShown = ((pl.startup_evidence || {}).containers || []).some(c => c && (c.first_error || c.final_error));
  if (excerpt && !evShown) h += `<div class="card"><h2>Log excerpt</h2>${codeBlock(excerpt, "log")}</div>`;
  if (pl.memory_estimate) {
    const m = pl.memory_estimate;
    h += `<div class="card"><h2>Memory estimate per node</h2><div class="kv"><b>Weights</b><span>${gib(m.model_weights)}</span><b>KV cache (requested)</b><span>${gib(m.kv_cache)}</span><b>Activations + graphs</b><span>${gib(m.activations + m.cuda_graphs)}</span><b>OS + runtime reserve</b><span>${gib(m.system_usage + m.runtime + m.control_plane + m.safety_reserve)}</span><b>Node total</b><span>${gib(m.mem_total)}</span></div>
      <p class="small muted">An estimate of what is requested, not a measurement: vLLM sizes the KV pool from what is left after loading, and the startup peak (weight loading, the first prefill of max_num_batched_tokens, CUDA graphs) can be higher than the steady state.</p></div>`;
  }
  return h + `</div>`;
}

async function viewJobs() {
  const g = S.gen;
  const kind = sessionStorage.getItem("tsm_jobs_kind") || "";
  const draw = async () => {
    const fresh = newest();
    const jobs = await GET("/api/v1/jobs" + qs({ limit: 60, kind }));
    if (!fresh()) return;
    renderLive(g, `<div class="view" data-live="jobs"><div class="view-head"><div><h1>Jobs</h1><p>Recipe preparation, connection checks and deployment changes — newest first.</p></div>
      <div class="row"><select id="job-kind" style="width:auto">${["", "integration", "prepare", "compatibility", "evaluation", "activation", "rollback", "recovery", "stage", "stop"].map(k => `<option value="${k}" ${k === kind ? "selected" : ""}>${k || "all kinds"}</option>`).join("")}</select><a class="btn" href="#/audit">Audit log</a></div></div>
      <div class="card">${jobs.length ? `<div class="table-wrap"><table><thead><tr><th>Job</th><th>Kind</th><th>Target</th><th>State</th><th>Stage</th><th>Started</th><th>Took</th></tr></thead><tbody>
      ${jobs.map(j => `<tr class="clickable" data-key="job:${esc(j.job_id)}" data-act="open" data-id="${esc(j.job_id)}"><td class="mono">${esc(j.job_id)}</td><td>${esc(j.kind)}</td><td>${esc(j.payload.alias || (j.payload.profile ? j.payload.profile + " " + (j.payload.label || "") : j.payload.repo || ""))}</td>
        <td>${tag(j.state, { completed: "good", failed: "bad", running: "info" }[j.state] || "muted")}${j.error ? `<div class="sub">${esc(j.error.slice(0, 120))}</div>` : ""}</td><td>${esc(j.stage || "")}</td><td>${esc(ago(j.created_at))}</td><td>${esc(dur(j.created_at, ["running", "pending"].includes(j.state) ? null : j.updated_at))}</td></tr>`).join("")}
      </tbody></table></div>` : empty("No jobs yet")}</div></div>`);
    const sel = $("#job-kind"); if (sel) sel.onchange = () => { sessionStorage.setItem("tsm_jobs_kind", sel.value); route(); };
  };
  onAct({ open: (el) => go("#/jobs/" + enc(el.dataset.id)) });
  await draw(); poll(draw, 5000);
}
async function viewAudit() {
  const rows = await GET("/api/v1/audit?limit=300");
  render(S.gen, `<div class="view"><div class="view-head"><div><div class="crumbs"><a href="#/jobs">Jobs</a> /</div><h1>Audit log</h1><p>Every change made through the API, CLI or the controller itself.</p></div></div>
    <div class="card"><div class="table-wrap"><table><thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Resource</th><th>Detail</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td class="nowrap">${esc(when(r.ts))}</td><td>${esc(r.actor)}</td><td class="mono">${esc(r.action)}</td><td class="mono">${esc(r.resource)}</td><td class="mono small">${esc(JSON.stringify(r.detail)).slice(0, 300)}</td></tr>`).join("")}
    </tbody></table></div></div></div>`);
}

/* ============================== cookbook ============================== */
async function viewCookbook(tab) {
  const g = S.gen;
  const tabs = [["builtin", "Built-in recipes"], ["community", "Community (GitHub)"], ["paste", "Paste / URL"], ["updates", "Recipe updates"]];
  const head = `<div class="view-head"><div><p class="eyebrow">Discover · adapt · experiment</p><h1>Cookbook</h1><p>A starting point for your two Sparks. Browse a recipe, make it your own, and keep each experiment reproducible.</p></div><a class="btn" href="#/profiles">Your profiles →</a></div>
    <ol class="recipe-steps" aria-label="Recipe workflow"><li><span>1</span><div><b>Import a recipe</b><small>Review settings and requirements</small></div></li><li><span>2</span><div><b>Make it yours</b><small>Adjust and pin a profile</small></div></li><li><span>3</span><div><b>Plan, then run</b><small>Check both nodes before switching</small></div></li></ol>
    <div class="tabs">${tabs.map(([id, l]) => `<a href="#/cookbook/${id}" class="${tab === id ? "active" : ""}">${esc(l)}</a>`).join("")}</div>`;
  const acts = {
    import: async (el) => {
      const name = el.dataset.name;
      const pv = await busy(el, () => GET(`/api/v1/cookbook/recipes/${enc(name)}`));
      if (current(g)) await importDraft(pv, el.dataset.suggest || name);
    },
    details: async (el) => {
      const r = await GET(`/api/v1/cookbook/recipes/${enc(el.dataset.name)}`);
      await modal({ title: r.title || r.name, wide: true, body: reportHtml(r.report, r.draft) + `<div class="section-title">Recipe file · ${esc(r.file)}</div>${codeBlock(r.text)}` });
    },
    preview: (el) => busy(el, () => previewImport({ url: el.dataset.url })),
    refresh: () => viewCookbookCommunity(g, head, true),
    "paste-preview": (el) => busy(el, () => previewImport(pasteBody(), $("#paste-name").value.trim())),
  };
  onAct(acts);
  if (tab === 'updates') return viewRecipeUpdates(g, head);
  if (tab === "community") return viewCookbookCommunity(g, head, false);
  if (tab === "paste") {
    render(g, `<div class="view">${head}<div class="card"><div class="form" style="grid-template-columns:2fr 1fr">
      <label class="field"><span>Recipe URL (GitHub blob/raw or any https)</span><input id="paste-url" placeholder="https://github.com/eugr/spark-vllm-docker/blob/main/recipes/….yaml"/></label>
      <label class="field"><span>Profile name (optional)</span><input id="paste-name" placeholder="from the recipe"/></label></div>
      <label class="field" style="margin-top:12px"><span>Open a recipe file</span><input id="recipe-file" type="file" accept=".yaml,.yml,.json"/><span class="help">YAML or JSON, up to 512 KiB. You can review and edit it below.</span></label>
      <label class="field" style="margin-top:12px"><span>…or paste an eugr YAML recipe / TwinSpark JSON</span><textarea id="paste-text" rows="16" spellcheck="false" placeholder="name: …&#10;container: vllm-node&#10;command: |&#10;  vllm serve org/model --tensor-parallel-size 2 …"></textarea></label>
      <label class="field" style="margin-top:12px"><span>Template overrides (eugr defaults, one key=value per line — like run-recipe -e)</span><textarea id="paste-over" rows="3" spellcheck="false" placeholder="max_model_len=262144"></textarea></label>
      <div class="row end" style="margin-top:12px"><button class="btn primary" data-act="paste-preview">Preview mapping</button></div></div></div>`);
    $("#recipe-file").onchange = async (event) => {
      try { await loadRecipeFile(event.target.files[0]); } catch (e) { toast(e.message, "bad"); }
    };
    return;
  }
  const data = await GET("/api/v1/cookbook");
  const card = (r) => r.error ? `<div class="card danger"><b>${esc(r.name)}</b><p class="small">${esc(r.error)}</p></div>` : `<div class="card recipe">
    <div class="row between"><div class="row">${verifTag(r.verification)} ${tag(r.topology, "muted")} ${tag(r.quantization, "muted")} ${tag(r.format, "muted")}</div>${(r.imported_as || []).map(n => `<a href="#/profiles/${enc(n)}">${tag("→ " + n, "good")}</a>`).join(" ")}</div>
    <h2 style="margin:10px 0 4px">${esc(r.title)}</h2><div class="mono small muted">${esc(r.model)}</div>
    <p class="small" style="color:var(--ink-2)">${esc(r.description)}</p>
    ${Object.keys(r.measured || {}).length ? `<div class="measured">${Object.entries(r.measured).slice(0, 5).map(([k, v]) => tag(`${k.replace(/_/g, " ")}: ${v}`, "muted")).join("")}</div>` : ""}
    ${(r.requirements || []).length ? `<details><summary>Needs (${r.requirements.length})</summary><ul class="plain">${r.requirements.map(x => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}
    ${(r.notes || []).length ? `<details><summary>Notes (${r.notes.length})</summary><ul class="plain">${r.notes.map(x => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}
    <div class="row end" style="margin-top:12px">${webUrl(r.url) ? `<a class="btn sm ghost" href="${esc(r.url)}" target="_blank" rel="noopener">Source</a>` : ""}<button class="btn sm" data-act="details" data-name="${esc(r.name)}">Details</button><button class="btn sm primary" data-act="import" data-name="${esc(r.name)}" data-suggest="${esc((r.imported_as || []).length ? r.name + "-2" : r.name)}">Import</button></div></div>`;
  if (!render(g, `<div class="view">${head}
    ${libraryToolbar("recipes", "Search model, recipe or quantization…", [["all", "All topologies"], ...[...new Set(data.recipes.map(r => r.topology).filter(Boolean))].sort().map(t => [t, t])])}
    <div class="grid cols-2" id="recipes-items"></div></div>`)) return;
  bindLibrary("recipes", data.recipes, card, (r, filter) => filter === "all" || r.topology === filter, empty("No recipes available"));
}
async function viewCookbookCommunity(g, head, refresh) {
  render(g, `<div class="view">${head}<div class="card"><div class="loading"><span class="spin"></span> reading GitHub…</div></div></div>`);
  const data = await GET("/api/v1/cookbook/community" + qs({ refresh: refresh ? "true" : "" }));
  render(g, `<div class="view">${head}${data.sources.map(src => `<div class="card"><div class="card-head"><h2 class="mono">${esc(src.source)}</h2><button class="btn sm" data-act="refresh">Refresh</button></div>
    ${src.error ? `<div class="callout warn"><div>${esc(src.error)}</div></div>` : src.files.length ? `<div class="table-wrap"><table><tbody>${src.files.map(f => `<tr><td class="mono">${esc(f.name)}</td><td class="num small muted">${bytes(f.size)}</td><td class="actions">${f.url ? `<a class="btn sm ghost" href="${esc(f.url)}" target="_blank" rel="noopener">GitHub</a>` : ""}<button class="btn sm primary" data-act="preview" data-url="${esc(f.download_url || f.url)}" data-name="${esc(f.name)}">Preview &amp; import</button></td></tr>`).join("")}</tbody></table></div>` : empty("No recipe files")}
    </div>`).join("")}<p class="small muted">Add more folders with <span class="mono">recipe_sources</span> in controller.yaml (owner/repo:path[@ref]).</p></div>`);
}
async function viewRecipeUpdates(g, head) {
  let updates = [];
  const draw = async () => {
    if (!current(g)) return;
    render(g, `<div class="view">${head}<div class="card"><div class="loading"><span class="spin"></span> checking imported recipe sources…</div></div></div>`);
    try {
      const data = await GET('/api/v1/cookbook/updates');
      if (!current(g)) return;
      updates = data.updates;
      render(g, `<div class="view">${head}<div class="card"><div class="card-head"><h2>Imported recipe sources</h2><button class="btn" data-act="updates-check">Check again</button></div>
        <p class="small muted">Local settings are retained. Updates create a separate experiment; your existing profiles and running deployment stay in place. Conflicts are shown before import.</p>
        ${updates.length ? `<div class="table-wrap"><table><thead><tr><th>Profile</th><th>Source status</th><th>Changes</th><th></th></tr></thead><tbody>${updates.map((u,i) => `<tr><td><a href="#/profiles/${enc(u.profile)}">${esc(u.profile)}</a><div class="sub mono">${esc(u.ref || 'No tracked source')}</div></td><td>${tag({current:'Up to date',changed:'Update found',error:'Check failed',untracked:'Local recipe'}[u.status] || u.status,{current:'good',changed:'info',error:'warn'}[u.status] || 'muted')}</td><td>${u.status === 'changed' ? `${num(u.changes.length)} settings${u.conflicts.length ? ` · ${num(u.conflicts.length)} conflicts kept local` : ''}` : esc(u.message || '')}</td><td>${u.status === 'changed' ? `<button class="btn sm primary" data-act="update-review" data-index="${i}">Review &amp; prepare</button>` : ''}</td></tr>`).join('')}</tbody></table></div>` : empty('No imported recipes','Import a recipe from the Cookbook or a URL to track its source.')}</div></div>`);
    } catch (e) {
      if (current(g)) render(g, `<div class="view">${head}<div class="callout warn">${esc(e.message)} <button class="btn" data-act="updates-check">Try again</button></div></div>`);
    }
  };
  onAct({ 'updates-check': draw, 'update-review': el => {
    const update = updates[Number(el.dataset.index)];
    return importDraft({...update.preview, update});
  } });
  await draw();
}
function recipeSettingLabel(field) {
  const node = field.startsWith('secondary.') ? 'Node B · ' : '';
  const key = field.replace(/^secondary\./, '');
  const labels = {
    'description':'Description', 'simple.model':'Model', 'simple.context_length':'Context length',
    'simple.api_alias':'API model name', 'simple.topology':'Node arrangement',
    'simple.quantization':'Model precision', 'identity.model_repo':'Model repository',
    'identity.model_revision':'Model commit', 'identity.image':'Container image',
    'identity.image_digest':'Image version', 'image_hint':'Container image',
    'advanced.mods':'Patches', 'advanced.extra_models':'Extra models',
    'advanced.gpu_memory_utilization':'GPU memory allocation',
  };
  return node + (labels[key] || key.replace(/_/g, ' ').replace(/\./g, ' / '));
}
function recipeChangeValue(value) {
  if (value == null) return '—';
  return esc(typeof value === 'string' ? value : typeof value === 'number' ? num(value, 6) : JSON.stringify(value));
}
function recipeUpdateHtml(update) {
  if (!update) return '';
  return `<div class="callout info"><div>Updated source for <b>${esc(update.profile)}</b> · ${esc(sha(update.previous_sha256))} → ${esc(sha(update.sha256))}. This becomes a separate profile.</div></div>
    ${update.preserved.length ? `<p class="small">Your adjustments are retained: ${update.preserved.map(p => esc(recipeSettingLabel(p))).join(', ')}.</p>` : ''}
    ${update.conflicts.length ? `<div class="callout warn"><div><b>Both you and upstream changed these settings.</b> Your values are retained: ${update.conflicts.map(p => esc(recipeSettingLabel(p))).join(', ')}. Review the complete profile before preparing.</div></div>` : ''}
    ${update.changes.length ? `<details open><summary>Settings changed (${update.changes.length})</summary><div class="table-wrap recipe-changes"><table><thead><tr><th>Setting</th><th>Current</th><th>Updated</th></tr></thead><tbody>${update.changes.map(c => `<tr><td>${esc(recipeSettingLabel(c.field))}</td><td class="mono small">${recipeChangeValue(c.before)}</td><td class="mono small">${recipeChangeValue(c.after)}</td></tr>`).join('')}</tbody></table></div></details>` : '<p class="small muted">Source content changed; your effective settings are retained.</p>'}`;
}
function pasteBody() {
  const url = $("#paste-url").value.trim(), text = $("#paste-text").value;
  if (url && text.trim()) throw new Error("Use either a recipe URL or pasted text. Clear the other field to continue.");
  const overrides = {};
  $("#paste-over").value.split("\n").forEach((line, index) => {
    const l = line.trim(); if (!l) return;
    const i = l.indexOf("="), k = l.slice(0, i).trim();
    if (i <= 0 || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(k)) throw new Error(`Override line ${index + 1}: use key=value.`);
    if (Object.hasOwn(overrides, k)) throw new Error(`Override line ${index + 1}: ${k} is already set.`);
    const v = l.slice(i + 1).trim();
    Object.defineProperty(overrides, k, { value: /^-?\d+(\.\d+)?$/.test(v) ? Number(v) : v, enumerable: true });
  });
  return url ? { url, overrides } : { text, overrides };
}
async function loadRecipeFile(file) {
  if (!file) return;
  if (file.size > 512 * 1024) throw new Error("Recipe is too large. Use a YAML or JSON file under 512 KiB.");
  const g = S.gen;
  const text = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
  if (!current(g)) return;
  $("#paste-text").value = text;
  $("#paste-url").value = "";
  toast(`Loaded ${file.name} — preview before importing`);
}
function reportHtml(rep, draft) {
  rep = rep || {}; const s = draft.simple, a = draft.advanced;
  const mapped = Object.entries(rep.mapped || {});
  return `<div class="kv"><b>Model</b><span class="mono">${esc(s.model)}</span><b>Topology</b><span>${esc(s.topology)} · ${esc(draft.distributed_backend)}</span><b>Context</b><span>${esc(s.context_length)}</span><b>Image</b><span class="mono">${esc(draft.image_hint || "—")}</span>${a.mods.length ? `<b>Mods</b><span>${a.mods.map(m => tag(m, "muted")).join(" ")}</span>` : ""}</div>
    ${draft.secondary ? `<div class="section-title">Model on node B · ${esc(draft.secondary.simple.api_alias)}</div>${reportHtml({}, draft.secondary)}` : ""}
    ${mapped.length ? `<div class="section-title">Mapped to settings</div><div class="chip-row">${mapped.map(([k, v]) => tag(`--${k}${v === true ? "" : " " + (typeof v === "object" ? JSON.stringify(v) : v)}`, "info")).join("")}</div>` : ""}
    ${(rep.raw || []).length ? `<div class="section-title">Kept verbatim (raw vLLM args)</div><pre class="code wrap">${esc(rep.raw.join(" "))}</pre>` : ""}
    ${(rep.dropped || []).length ? `<div class="section-title">Dropped — TwinSpark sets these</div><div class="chip-row">${rep.dropped.map(x => tag(x, "muted")).join("")}</div>` : ""}
    ${(rep.notes || []).length ? `<div class="section-title">Notes</div><ul class="plain">${rep.notes.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    ${Object.keys(a.env || {}).length ? `<details><summary>Environment (${Object.keys(a.env).length})</summary><pre class="code">${esc(Object.entries(a.env).map(([k, v]) => k + "=" + v).join("\n"))}</pre></details>` : ""}`;
}
async function previewImport(body, suggested) {
  const g = S.gen;
  if (!body.url && !(body.text || "").trim()) throw new Error("give a URL or paste a recipe");
  const pv = await POST("/api/v1/cookbook/import-recipe", { ...body, preview: true, profile_name: suggested || undefined });
  if (current(g)) await importDraft(pv);
}
async function importDraft(pv, suggested) {
  const name = pv.draft.name;
  let created, prepared;
  const requestId = recipeRequestId();
  const snapshot = root => {
    const input = $("#imp-name", root); input.value = input.value.trim();
    return input.reportValidity() ? {...pv.draft, name:input.value} : null;
  };
  const { value } = await modal({
    title: "Review and import", wide: true,
    body: `${pv.exists ? `<div class="callout warn"><div>A profile named <b>${esc(name)}</b> exists — choose another name.</div></div>` : ""}
      <label class="field"><span>Profile name</span><input id="imp-name" required maxlength="63" pattern="[a-z0-9][a-z0-9._\\-]{0,62}" value="${esc(suggested || (pv.exists ? name.slice(0, 61) + "-2" : name))}"/><span class="help">Lowercase letters, numbers, dots, underscores and hyphens.</span></label>
      <div class="callout info"><div>Import &amp; prepare automatically pins the models and image, checks both nodes, and downloads missing weights. The current deployment keeps serving. You can also import only and customize first. Required patches must already be installed.</div></div>
      ${recipeUpdateHtml(pv.update)}
      ${imageFieldsHtml(pv.draft)}
      ${reportHtml(pv.report, pv.draft)}
      ${(pv.draft.source?.requirements || []).length ? `<div class="section-title">Requirements</div><ul class="plain">${pv.draft.source.requirements.map(r => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
      <details><summary>All profile settings</summary>${codeBlock(JSON.stringify(pv.draft, null, 2))}</details>`,
    actions: [{ label: "Cancel", value: null }, { label: "Import only", value: "go", validate: async (root) => {
      const draft = snapshot(root); if (!draft) return false;
      // Import the reviewed snapshot, not a URL that may have changed since preview.
      created = await POST("/api/v1/profiles", draft);
      return true;
    } }, { label: "Import & prepare", value: "prepared", cls: "primary", validate: async root => {
      const draft = snapshot(root); if (!draft) return false;
      const pins = imagePins(root, draft); if (pins === null) return false;
      prepared = await POST("/api/v1/cookbook/integrate", {draft, pins, request_id:requestId});
      return true;
    } }],
  });
  if (value === "prepared") { toast("recipe imported — preparing automatically"); go("#/jobs/" + enc(prepared.job_id)); return; }
  if (value !== "go") return;
  toast(`Imported ${created.name} — ready to customize`, "good"); go("#/profiles/" + enc(created.name));
}

/* ============================== model files ============================== */
/** One cache cell: complete or not, who put it there, and whether checksums were verified — three different facts. */
function cacheCell(x) {
  if (!x.complete) return tag("partial", "warn") + `<div class="sub">files missing — staging completes it</div>`;
  const state = x.verified ? tag("✓ checksum verified", "good") : tag("✓ complete", "good");
  const who = x.managed ? (x.verified ? "staged by TwinSpark" : "staged by TwinSpark · checksums not verified")
    : "external cache — reused as is, no new download";
  return `${state}<div class="sub" title="${x.managed ? "" : "Downloaded outside TwinSpark (for example by huggingface-cli or another launcher). Complete files at the pinned revision are used without downloading again; TwinSpark has no checksum record for them."}">${esc(who)}</div>`;
}
async function viewFiles() {
  const g = S.gen;
  const draw = async () => {
    const inv = await GET("/api/v1/models/files");
    const nodes = Object.keys(inv.nodes).sort();
    let h = `<div class="view"><div class="view-head"><div><h1>Model files</h1><p>Hugging Face cache on both Sparks. Staging downloads once and copies to the other node over QSFP.</p></div>
      <div class="row"><button class="btn primary" data-act="stage-new">Stage a model…</button></div></div><div class="grid cols-2">`;
    for (const n of nodes) {
      const d = inv.nodes[n];
      const used = d.total_bytes && d.free_bytes != null ? d.total_bytes - d.free_bytes : 0;
      h += `<div class="card"><div class="card-head"><h2>Node ${esc(n)} disk</h2><span class="mono small muted">${esc(d.hf_home || "")}</span></div>
        ${d.error ? `<div class="callout bad"><div>${esc(d.error)}</div></div>` : `<div class="row between small"><span>${bytes(d.free_bytes)} free</span><span class="muted">of ${bytes(d.total_bytes)}</span></div>
        <div class="meter big" style="margin-top:6px"><i style="width:${d.total_bytes ? used / d.total_bytes * 100 : 0}%" class="${d.free_bytes < 200 * 2 ** 30 ? "warn" : ""}"></i></div>`}
        ${(d.downloads || []).map(t => `<div class="small" style="margin-top:8px">${dot("warn", true)} ${esc(t.detail || t.task_id)}</div>`).join("")}</div>`;
    }
    h += `</div>`;
    if ((inv.missing_for_profiles || []).length) {
      h += `<div class="card"><h2>Needed by profiles, not on disk</h2><div class="list">${inv.missing_for_profiles.map(x => `<div class="item"><div class="l"><div class="t mono">${esc(x.repo)}@${esc(sha(x.revision, 12))}</div><div class="d">${x.profiles.map(esc).join(", ")}</div></div>
        <div class="actions"><button class="btn sm primary" data-act="stage" data-ref="${esc(x.repo + "@" + x.revision)}">Stage to both</button></div></div>`).join("")}</div></div>`;
    }
    h += `<div class="card"><div class="card-head"><h2>Cached models</h2>${inv.staging.length ? tag("staging " + inv.staging.join(", "), "warn") : ""}</div>`;
    if (!inv.models.length) h += empty("Cache is empty", "Stage a model or activate a profile — weights are fetched automatically.");
    else {
      h += `<div class="table-wrap"><table><thead><tr><th>Model / revision</th><th class="num">Size</th>${nodes.map(n => `<th>Node ${esc(n)}</th>`).join("")}<th>Used by</th><th></th></tr></thead><tbody>`;
      for (const m of inv.models) {
        for (const r of m.revisions) {
          const cells = nodes.map(n => {
            const x = r.nodes[n];
            if (!x) return `<td>${tag("missing", "muted")}</td>`;
            return `<td>${cacheCell(x)}</td>`;
          }).join("");
          const onAll = nodes.every(n => r.nodes[n] && r.nodes[n].complete);
          h += `<tr><td><div class="mono">${esc(m.repo)}</div><div class="sub mono">${esc(sha(r.revision, 12))}${r.refs.length ? " · " + esc(r.refs.join(", ")) : ""}</div></td><td class="num">${bytes(r.size_bytes)}</td>${cells}
            <td>${r.active ? tag("serving", "good") + " " : ""}${r.profiles.map(p => `<a href="#/profiles/${enc(p)}">${esc(p)}</a>`).join(", ") || '<span class="muted">—</span>'}</td>
            <td class="actions">${onAll ? "" : `<button class="btn sm" data-act="stage" data-ref="${esc(m.repo + "@" + r.revision)}">Copy to all</button>`}<button class="btn sm danger" data-act="delete" data-repo="${esc(m.repo)}" data-rev="${esc(r.revision)}" ${r.active ? "disabled" : ""}>Delete…</button></td></tr>`;
        }
        if (Object.keys(m.partial_bytes || {}).length) h += `<tr><td colspan="${nodes.length + 4}" class="small muted">${esc(m.repo)}: unfinished download data ${Object.entries(m.partial_bytes).map(([n, b]) => `node ${esc(n)} ${bytes(b)}`).join(", ")}</td></tr>`;
      }
      h += `</tbody></table></div>`;
    }
    render(g, h + `</div></div>`);
  };
  onAct({
    stage: async (el) => { const j = await POST("/api/v1/models/stage", { ref: el.dataset.ref }); toast("staging started"); go("#/jobs/" + enc(j.job_id)); },
    "stage-new": async () => {
      const v = await formBox("Stage a model", [
        { id: "ref", label: "Model", placeholder: "org/model, org/model@branch or org/model@<sha>" },
        { id: "include", label: "Extra files (globs, comma-separated)", placeholder: "e.g. dflash/*", help: "Only inference files are fetched by default (safetensors, configs, tokenizer)." },
        { id: "nodes", label: "Nodes", type: "select", value: "", options: [["", "both"], ["A", "A only"], ["B", "B only"]] },
      ], { label: "Stage" });
      if (!v || !v.ref) return;
      const j = await POST("/api/v1/models/stage", { ref: v.ref, include: v.include ? v.include.split(",").map(s => s.trim()) : null, nodes: v.nodes ? [v.nodes] : null });
      toast("staging started"); go("#/jobs/" + enc(j.job_id));
    },
    delete: async (el) => deleteFlow(el.dataset.repo, el.dataset.rev).then(() => draw()),
  });
  await draw();
  every(() => { if (!document.querySelector(".modal-back")) draw().catch(() => { }); }, 15000);
}
async function deleteFlow(repo, rev) {
  const prev = await POST("/api/v1/models/files/delete", { repo, revision: rev, preview: true });
  const rows = Object.entries(prev.results).map(([n, r]) => `<tr><td>Node ${esc(n)}</td><td>${r.error ? `<span style="color:var(--bad)">${esc(r.error)}</span>` : `frees <b>${bytes(r.freed_bytes)}</b>${r.kept_shared_blobs ? ` · keeps ${r.kept_shared_blobs} file(s) shared with other snapshots` : ""}${r.whole_repo ? " · removes the repo" : ""}${r.dry_run ? " " + tag("dry-run: nothing is deleted", "warn") : ""}`}</td></tr>`).join("");
  const nodes = Object.keys(prev.results).sort();
  const dependents = prev.dependents || [];
  const { value, root } = await modal({
    title: `Delete ${repo}@${sha(rev, 8)}`,
    body: `<table><tbody>${rows}</tbody></table>
      ${dependents.length ? `<div class="callout warn"><div>Profiles pinned to this revision: <b>${dependents.map(esc).join(", ")}</b>. They re-download it on their next activation.</div></div>` : ""}
      <div class="row">${nodes.map(n => `<label class="check"><input type="checkbox" class="del-node" value="${esc(n)}" checked/> node ${esc(n)}</label>`).join("")}</div>`,
    actions: [{ label: "Cancel", value: null }, { label: "Delete", value: "go", cls: "danger", validate: (r) => { r._nodes = $$(".del-node", r).filter(x => x.checked).map(x => x.value); return r._nodes.length > 0; } }],
  });
  if (value !== "go") return;
  const res = await POST("/api/v1/models/files/delete", { repo, revision: rev, nodes: root._nodes, force: dependents.length > 0 });
  const freed = Object.values(res.results).reduce((a, r) => a + (r.freed_bytes || 0), 0);
  const errs = Object.entries(res.results).filter(([, r]) => r.error);
  if (errs.length) toast(errs.map(([n, r]) => `node ${n}: ${r.error}`).join("; "), "bad");
  else toast(Object.values(res.results).some(r => r.dry_run) ? "dry-run agents: nothing deleted" : `freed ${bytes(freed)}`, "good");
}

/* ============================== mods ============================== */
async function viewMods() {
  const g = S.gen;
  const data = await GET("/api/v1/mods");
  const nodes = [...new Set(data.mods.flatMap(m => Object.keys(m.nodes)))].sort();
  let h = `<div class="view"><div class="view-head"><div><h1>Mods</h1><p>Patch directories (eugr format: a folder with <span class="mono">run.sh</span>) applied inside the container right before vLLM starts, in the order the profile lists them.</p></div>
    <div class="row"><label class="btn primary" style="cursor:pointer">Upload mod (.zip / .tar.gz)<input id="mod-file" type="file" accept=".zip,.tar,.tgz,.tar.gz" class="hidden"/></label></div></div>`;
  if ((data.missing || []).length) h += `<div class="callout warn"><div><b>Missing mods</b> — these profiles cannot start until the mod is installed:<ul>${data.missing.map(m => `<li><span class="mono">${esc(m.name)}</span> — ${m.profiles.map(esc).join(", ")}</li>`).join("")}</ul>From a spark-vllm-docker checkout: <span class="mono">tsm mods import-eugr ~/spark-vllm-docker --only ${esc(data.missing.map(m => m.name).join(","))}</span></div></div>`;
  for (const [n, e] of Object.entries(data.errors || {})) h += `<div class="callout bad"><div>node ${esc(n)}: ${esc(e)}</div></div>`;
  h += `<div class="card">${data.mods.length ? `<div class="table-wrap"><table><thead><tr><th>Mod</th>${nodes.map(n => `<th>Node ${esc(n)}</th>`).join("")}<th>Used by</th><th></th></tr></thead><tbody>
    ${data.mods.map(m => `<tr><td><b class="mono">${esc(m.name)}</b>${m.summary ? `<div class="sub">${esc(m.summary)}</div>` : ""}${m.consistent ? "" : `<div class="sub" style="color:var(--warn)">differs between nodes — reinstall</div>`}</td>
      ${nodes.map(n => `<td>${m.nodes[n] && m.nodes[n].present ? tag("✓ " + sha(m.nodes[n].hash, 8), "good") : tag("missing", "bad")}</td>`).join("")}
      <td>${m.profiles.map(p => `<a href="#/profiles/${enc(p)}">${esc(p)}</a>`).join(", ") || '<span class="muted">—</span>'}</td>
      <td class="actions"><button class="btn sm danger" data-act="remove" data-name="${esc(m.name)}">Remove</button></td></tr>`).join("")}
    </tbody></table></div>` : empty("No mods installed", "Upload a zip of a mod folder, or run tsm mods import-eugr on a spark-vllm-docker checkout.")}</div></div>`;
  if (!render(g, h)) return;
  onAct({
    remove: async (el) => {
      if (!await confirmBox(`Remove ${el.dataset.name}?`, "Removed from every node. Profiles that list it will fail preflight until it is reinstalled.", { label: "Remove", danger: true })) return;
      const r = await DEL(`/api/v1/mods/${enc(el.dataset.name)}`);
      const errs = Object.entries((r && r.results) || {}).filter(([, x]) => x && x.error);
      if (errs.length) toast(errs.map(([n, x]) => `node ${n}: ${x.error}`).join("; "), "bad"); else toast("removed", "good");
      route();
    },
  });
  $("#mod-file").onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    try {
      if (f.size > 64 * 2 ** 20) throw new Error("mod archive larger than 64 MiB");
      const v = await formBox("Install mod", [{ id: "name", label: "Mod name", value: f.name.replace(/\.(zip|tar\.gz|tgz|tar)$/i, "") }], { label: "Install on all nodes", intro: `<span class="small muted">${esc(f.name)} · ${bytes(f.size)}. A single top-level folder in the archive is stripped; run.sh must be at the mod's root.</span>` });
      if (!v) return;
      const buf = new Uint8Array(await f.arrayBuffer());
      let bin = ""; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
      const r = await POST("/api/v1/mods", { name: v.name, archive_b64: btoa(bin) });
      const errs = Object.entries(r.results).filter(([, x]) => x.error);
      if (errs.length) toast(errs.map(([n, x]) => `node ${n}: ${x.error}`).join("; "), "bad"); else toast(`installed ${v.name} on ${Object.keys(r.results).join(" + ")}`, "good");
      route();
    } catch (err) { fail(err); } finally { e.target.value = ""; }
  };
}

/* ============================== planner ============================== */
async function viewPlanner() {
  const g = S.gen;
  const profs = await GET("/api/v1/profiles?summary=true");
  render(g, `<div class="view"><div class="view-head"><div><h1>Planner</h1><p>Unified-memory math per node: weights + KV pool + reserve. Uses the real checkpoint size once a profile is pinned, and the recipe's measured KV bytes/token where known.</p></div></div>
    <div class="grid cols-2"><div class="card"><h2>Profile fit</h2><label class="field"><span>Profile</span><select id="pl-prof">${profs.map(p => `<option>${esc(p.name)}</option>`).join("")}</select></label><div id="pl-fit" style="margin-top:12px"></div></div>
    <div class="card"><h2>What-if</h2><div class="form">
      <label class="field"><span>Model (registered spec)</span><input id="pl-repo" placeholder="RedHatAI/GLM-5.3-Flash-NVFP4"/></label>
      <label class="field"><span>Quantization</span><select id="pl-q">${["nvfp4", "mxfp4", "fp8", "bf16", "int4", "awq", "gptq", "autoround"].map(q => `<option>${q}</option>`).join("")}</select></label>
      <label class="field"><span>Topology</span><select id="pl-t">${["tp2", "tp-ep", "pp2", "single-a", "replicated"].map(q => `<option>${q}</option>`).join("")}</select></label>
      <label class="field"><span>KV dtype</span><select id="pl-kv">${["fp8", "auto", "fp8_e4m3", "nvfp4", "bf16"].map(q => `<option>${q}</option>`).join("")}</select></label>
      <label class="field"><span>Parallel sequences</span><input id="pl-c" value="4"/></label>
    </div><div class="row end" style="margin-top:12px"><button class="btn primary" data-act="whatif">Max safe context</button></div><div id="pl-out" style="margin-top:12px"></div></div></div></div>`);
  const showFit = async () => {
    const n = $("#pl-prof")?.value; if (!n) { $("#pl-fit").innerHTML = empty("No profiles"); return; }
    $("#pl-fit").innerHTML = `<span class="spin"></span>`;
    try {
      const f = await GET(`/api/v1/profiles/${enc(n)}/fit`);
      if (current(g)) { $("#pl-fit").innerHTML = fitHtml(f); const p = profs.find(x => x.name === n); if (p && $("#pl-repo") && !$("#pl-repo").value) $("#pl-repo").value = p.model; }
    } catch (e) { $("#pl-fit").innerHTML = `<div class="callout bad"><div>${esc(e.message)}</div></div>`; }
  };
  if ($("#pl-prof")) $("#pl-prof").onchange = showFit;
  showFit();
  onAct({
    whatif: async (el) => {
      const body = { repo: $("#pl-repo").value.trim(), quantization: $("#pl-q").value, topology: $("#pl-t").value, kv_dtype: $("#pl-kv").value, concurrency: parseInt($("#pl-c").value, 10) || 1, contexts: [32768, 65536, 131072, 262144, 524288, 1048576] };
      const r = await busy(el, () => POST("/api/v1/system/context/max-safe", body));
      $("#pl-out").innerHTML = `<div class="table-wrap"><table><thead><tr><th class="num">Context</th><th>Status</th><th class="num">Headroom / node</th></tr></thead><tbody>${r.rows.map(x => `<tr><td class="num">${num(x.context)}</td><td>${tag(x.status, { Safe: "good", Tight: "warn" }[x.status] || "bad")}</td><td class="num">${gib(x.headroom_gib)}</td></tr>`).join("")}</tbody></table></div>`;
    },
  });
}

/* ============================== diagnostics ============================== */
let hlStatus = { nodes: {} };
/** The same one-line remote-access summary as `tsm headless status`. */
function accessSummary(a) {
  const ssh = a.ssh || {}, ts = a.tailscale || {}, serve = ts.serve || {};
  const bits = [ssh.running ? (ssh.starts_at_boot ? "SSH running, starts at boot" : "SSH running, NOT enabled at boot") : "SSH not running"];
  const tsBoot = ["disabled", "masked"].includes((ts.service || {}).enabled) ? ", NOT enabled at boot" : "";
  if (ts.installed) bits.push(`Tailscale ${ts.running ? "up " + (ts.ip || "") : "not running"}${tsBoot}${serve.manager_forwarded ? " · manager forwarded on the tailnet" : serve.manager_forwarded === false ? " · manager NOT forwarded (tsm remote tailscale-serve)" : ""}`);
  else bits.push("Tailscale not installed");
  if (a.default_target) bits.push("boots into " + a.default_target);
  return bits.join("; ");
}
/** A link-test result: simulated or real, what was measured, and failures as "unavailable", never as 0. */
function linkResultHtml(r) {
  const i = r.initiator || {};
  if (i.error) return `<div class="callout bad"><div><b>Link test failed.</b> ${esc(i.error)}</div></div>`;
  const what = r.mode === "tcp" ? `${num(r.streams)} TCP stream(s)` : `RDMA (ib_write_bw) on ${esc(((r.hcas || {}).A || []).join(", ") || "no device")}`;
  const unavailable = (k) => `<div class="tile"><div class="k">${esc(k)}</div><div class="v">unavailable</div></div>`;
  let h = r.simulated ? `<div class="callout warn"><div><b>Simulated.</b> Both nodes run in dry-run: no packet crossed the link, these numbers are made up. Real values need <span class="mono">sudo tsm go-live</span> on both nodes.</div></div>` : "";
  h += `<p class="small muted">${what} · ${num(r.duration_s, 1)} s · node A → node B</p><div class="tiles">`;
  h += i.bandwidth_gbps != null ? tile("Bandwidth", i.bandwidth_gbps, "Gb/s", null, "", 1) : unavailable("Bandwidth");
  if (i.rtt_us_median != null) h += tile("RTT median", i.rtt_us_median, "µs", null, "", 1);
  if (i.rtt_us_p99 != null) h += tile("RTT p99", i.rtt_us_p99, "µs", null, "", 1);
  h += (i.per_hca || []).map(hc => hc.gbps != null ? tile(hc.hca, hc.gbps, "Gb/s", null, "", 1) : unavailable(hc.hca)).join("") + `</div>`;
  if ((r.errors || []).length) h += `<div class="callout bad" style="margin-top:10px"><div><ul class="plain">${r.errors.map(e => `<li>${esc(e)}</li>`).join("")}</ul></div></div>`;
  return h + [i.note, ...(i.notes || [])].filter(Boolean).map(n => `<p class="small muted">${esc(n)}</p>`).join("");
}
async function viewDiagnostics(tab) {
  const g = S.gen;
  const tabs = [["doctor", "Doctor"], ["network", "RDMA & link"], ["headless", "Headless"], ["foreign", "Foreign containers"]];
  const head = `<div class="view-head"><div><h1>Diagnostics</h1><p>Everything a fast, stable dual-Spark deployment depends on — with the fix for each finding.</p></div></div>
    <div class="tabs">${tabs.map(([id, l]) => `<a href="#/diagnostics/${id}" class="${tab === id ? "active" : ""}">${esc(l)}</a>`).join("")}</div>`;
  render(g, `<div class="view">${head}<div class="card"><div class="loading"><span class="spin"></span></div></div></div>`);
  let body = "";
  if (tab === "doctor") {
    const r = await GET("/api/v1/system/doctor");
    const ic = { pass: dot("good"), info: dot(""), warn: dot("warn"), fail: dot("bad") };
    const order = { fail: 0, warn: 1, info: 2, pass: 3 };
    const checks = [...r.checks].sort((a, b) => order[a.status] - order[b.status]);
    body = `<div class="tiles">${["fail", "warn", "info", "pass"].map(s => `<div class="tile"><div class="k">${s === "pass" ? "ok" : s}</div><div class="v" style="color:var(--${{ fail: "bad", warn: "warn", info: "info", pass: "good" }[s]})">${r.summary[s]}</div></div>`).join("")}</div>
      <div class="card"><div class="card-head"><h2>Checks</h2><button class="btn sm" data-act="rerun">Run again</button></div><div class="table-wrap"><table><tbody>
      ${checks.map(c => `<tr><td style="width:18px">${ic[c.status]}</td><td class="nowrap"><b>${esc(c.check)}</b><div class="sub">${esc(c.node)}</div></td><td>${esc(c.detail)}${c.fix && c.status !== "pass" ? `<pre class="code wrap" style="margin-top:6px">${esc(c.fix)}</pre>` : ""}</td></tr>`).join("")}
      </tbody></table></div></div>`;
  } else if (tab === "network") {
    const r = await GET("/api/v1/system/rdma");
    body = `<div class="grid cols-2">${Object.keys(r).sort().map(n => {
      const x = r[n];
      if (x.error) return `<div class="card"><h2>Node ${esc(n)}</h2><div class="callout bad"><div>${esc(x.error)}</div></div></div>`;
      return `<div class="card"><div class="card-head"><h2>Node ${esc(n)} RoCE</h2>${x.matches_config ? tag("matches controller.yaml", "good") : tag("differs from controller.yaml", "warn")}</div>
        <div class="table-wrap"><table><thead><tr><th>Device</th><th>State</th><th class="num">Rate</th><th>Netdev</th><th>RoCE v2 IPv4</th></tr></thead><tbody>${x.devices.map(d => `<tr><td class="mono">${esc(d.hca)}</td><td>${tag(d.state || "?", d.active ? "good" : "muted")}</td><td class="num">${esc(d.rate_gbps ?? "—")}</td><td class="mono">${esc(d.netdevs.join(", "))}</td><td class="mono small">${d.roce_v2_ipv4.map(gd => `${esc(gd.ipv4)} (gid ${esc(gd.index)})`).join("<br>") || "—"}</td></tr>`).join("") || `<tr><td colspan="5" class="muted">no RDMA devices</td></tr>`}</tbody></table></div>
        <div class="section-title">Suggested for nodes.${esc(n)}</div>${codeBlock(x.yaml)}
        ${x.suggestion.note ? `<div class="callout warn" style="margin-top:10px"><div>${esc(x.suggestion.note)}</div></div>` : ""}
        ${x.perftest ? "" : `<p class="small muted">Install <span class="mono">perftest</span> on this node for the RDMA link test.</p>`}</div>`;
    }).join("")}</div>
    <div class="card"><div class="card-head"><h2>Link test (A → B over QSFP)</h2></div><div class="form">
      <label class="field"><span>Mode</span><select id="lt-mode"><option value="tcp">TCP (quick sanity check)</option><option value="rdma">RDMA (ib_write_bw, real NIC speed)</option></select></label>
      <label class="field"><span>Duration (s)</span><input id="lt-dur" value="5"/></label>
      <label class="field"><span>TCP streams</span><input id="lt-streams" value="4"/></label>
      <div class="field"><button class="btn primary" data-act="linktest">Run test</button></div></div><div id="lt-out" style="margin-top:12px"></div></div>`;
  } else if (tab === "headless") {
    const r = hlStatus = await GET("/api/v1/system/headless");
    body = `<div class="card"><div class="card-head"><h2>Desktop vs headless</h2><span class="small muted">desired: ${esc(r.mode || "not set")}</span></div>
      <p class="small" style="color:var(--ink-2);margin-top:0">The GNOME desktop holds a few GiB of unified memory (and GPU contexts) on each Spark. <b>headless-safe</b> boots into multi-user next time; <b>headless-max</b> also stops the display manager now. Needs <span class="mono">twinspark-privd</span> on each node.</p>
      <div class="grid cols-2">${Object.keys(r.nodes).sort().map(n => {
        const v = r.nodes[n];
        if (v.error) return `<div class="card flat"><b>Node ${esc(n)}</b><div class="callout bad" style="margin-top:8px"><div>${esc(v.error)}</div></div></div>`;
        const d = v.desktop;
        return `<div class="card flat"><div class="row between"><b>Node ${esc(n)}</b>${d.desktop_running ? tag("desktop running", "warn") : tag("headless", "good")}</div><div class="kv" style="margin-top:10px">
          <b>Desktop memory</b><span>${gib(d.desktop_rss_gib)}</span><b>Processes</b><span class="small">${esc((d.desktop_processes || []).join(", ") || "—")}</span><b>Boot target</b><span class="mono">${esc(d.default_target || "unknown")}</span><b>privd</b><span>${v.privd_available ? tag("ok", "good") : tag("not installed", "warn")}</span>
          <b>Remote access</b><span class="small">${v.access && !v.access.dry_run ? esc(accessSummary(v.access)) : "not checked (dry-run)"}</span></div></div>`;
      }).join("")}</div>
      <div class="row" style="margin-top:14px"><button class="btn" data-act="headless" data-mode="desktop">Desktop</button><button class="btn" data-act="headless" data-mode="headless-safe">Headless (next boot)</button><button class="btn primary" data-act="headless" data-mode="headless-max">Headless max (now)</button><label class="check"><input type="checkbox" id="hl-now"/> apply right away</label></div><div id="hl-out"></div></div>`;
  } else if (tab === "foreign") {
    const r = await GET("/api/v1/system/foreign");
    const rows = Object.entries(r).flatMap(([n, l]) => Array.isArray(l) ? l.map(c => ({ n, ...c })) : [{ n, error: l.error }]);
    body = `<div class="card"><div class="card-head"><h2>Inference containers not managed by TwinSpark</h2></div>
      <p class="small" style="color:var(--ink-2);margin-top:0">A vLLM started by hand (e.g. eugr's launch-cluster.sh) keeps holding unified memory and the port — stop it before the first TwinSpark switch.</p>
      ${rows.length ? `<div class="table-wrap"><table><thead><tr><th>Node</th><th>Container</th><th>Image</th><th>Status</th><th></th></tr></thead><tbody>${rows.map(c => c.error ? `<tr><td>${esc(c.n)}</td><td colspan="4" style="color:var(--bad)">${esc(c.error)}</td></tr>` : `<tr><td>${esc(c.n)}</td><td class="mono">${esc(c.name)}</td><td class="mono small">${esc(c.image || "")}</td><td>${esc(c.status || "")}</td><td class="actions"><button class="btn sm danger" data-act="foreign-stop" data-node="${esc(c.n)}" data-name="${esc(c.name)}">Stop</button></td></tr>`).join("")}</tbody></table></div>` : empty("None found", "Only TwinSpark-owned containers are running.")}</div>`;
  }
  if (!render(g, `<div class="view">${head}${body}</div>`)) return;
  onAct({
    rerun: () => route(),
    linktest: async (el) => {
      const body = { mode: $("#lt-mode").value, duration_s: parseFloat($("#lt-dur").value) || 5, streams: parseInt($("#lt-streams").value, 10) || 4 };
      const out = $("#lt-out");
      out.innerHTML = `<span class="spin"></span> running…`;
      try {
        const r = await busy(el, () => POST("/api/v1/system/link-test", body));
        if (out.isConnected) out.innerHTML = linkResultHtml(r);
      } catch (e) {
        if (out.isConnected) out.innerHTML = `<div class="callout bad"><div><b>Link test not run.</b> ${esc(e.message)}</div></div>`;
      }
    },
    headless: async (el) => {
      const mode = el.dataset.mode, now = $("#hl-now").checked || mode === "headless-max";
      const access = Object.entries(hlStatus.nodes || {}).map(([n, v]) => [n, v.access || {}]);
      const blind = access.filter(([, a]) => !a.dry_run && !(a.remote_paths || []).length).map(([n]) => n);
      const facts = access.filter(([, a]) => !a.dry_run).map(([n, a]) => `<li>Node ${esc(n)}: ${esc(accessSummary(a))}</li>`).join("");
      const text = now
        ? `<p>The display manager is ${mode === "desktop" ? "started" : "stopped"} immediately on both nodes — anyone using the desktop loses the session.${mode === "headless-max" ? " headless-max always does this, with or without “apply right away”." : ""}</p>${facts ? `<p><b>Ways in without a screen</b></p><ul class="plain">${facts}</ul>` : ""}${blind.length && mode !== "desktop" ? `<div class="callout bad"><div>No SSH-at-boot or Tailscale path found on node ${esc(blind.join(", "))}. Make sure you can reach it another way first (<span class="mono">sudo tsm remote tailscale-serve --apply</span> on node A, <span class="mono">sudo systemctl enable ssh</span>).</div></div>` : ""}`
        : "Changes the default boot target on both nodes; takes effect at the next reboot.";
      if (!await confirmBox(`Apply ${mode}${now ? " now" : ""}?`, text, { label: "Apply", danger: now && mode !== "desktop" })) return;
      const r = await busy(el, () => POST("/api/v1/system/headless", { mode, now }));
      $("#hl-out").innerHTML = `<div class="section-title">Result</div>` + Object.entries(r.results).map(([n, x]) => `<div class="small">node ${esc(n)}: ${x.error ? `<span style="color:var(--bad)">${esc(x.error)}</span>` : x.dry_run ? "dry-run — would run " + esc(x.steps.map(s => s.op).join(", ")) : `${esc(x.effective)}, reclaimed ${gib(x.reclaimed_gib)}`}</div>`).join("");
    },
    "foreign-stop": async (el) => {
      const { node, name } = el.dataset;
      if (!await confirmBox(`Stop ${name} on node ${node}?`, "This container was not started by TwinSpark. Whatever it serves goes away.", { label: "Stop it", danger: true, typed: name })) return;
      await POST("/api/v1/system/foreign/stop", { node, name, confirm: name }); toast("stopped " + name, "good"); route();
    },
  });
}

/* ============================== logs ============================== */
async function viewLogs() {
  const g = S.gen;
  let tail = parseInt(sessionStorage.getItem("tsm_log_tail") || "300", 10);
  let follow = sessionStorage.getItem("tsm_log_follow") !== "0";
  render(g, `<div class="view"><div class="view-head"><div><h1>Logs</h1><p>Container output of the active deployment (or the last one tried) on both nodes.</p></div>
    <div class="row"><input id="log-filter" placeholder="filter lines…" style="width:200px"/><select id="log-tail" style="width:auto">${[100, 300, 1000, 5000].map(n => `<option ${n === tail ? "selected" : ""}>${n}</option>`).join("")}</select><label class="check"><input type="checkbox" id="log-follow" ${follow ? "checked" : ""}/> follow</label></div></div><div id="log-body"><div class="loading"><span class="spin"></span></div></div></div>`);
  const draw = async () => {
    const r = await GET("/api/v1/active/logs" + qs({ tail }));
    if (!current(g)) return;
    const f = ($("#log-filter").value || "").toLowerCase();
    const body = r.containers.length ? r.containers.map(c => {
      let text = c.log || c.error || "";
      if (f) text = text.split("\n").filter(l => l.toLowerCase().includes(f)).join("\n");
      return `<div class="card"><div class="card-head"><h2>Node ${esc(c.node)} · <span class="mono">${esc(c.name)}</span> ${tag(c.role, "muted")}</h2></div>${codeBlock(text, "log")}</div>`;
    }).join("") : `<div class="card">${empty("Nothing running", "Logs show up once a profile is activated.")}</div>`;
    const scrolled = $$("#log-body pre").map(p => p.scrollHeight - p.scrollTop - p.clientHeight < 40);
    $("#log-body").innerHTML = body;
    $$("#log-body pre").forEach((p, i) => { if (scrolled[i] !== false) p.scrollTop = p.scrollHeight; });
  };
  $("#log-tail").onchange = (e) => { tail = parseInt(e.target.value, 10); sessionStorage.setItem("tsm_log_tail", tail); draw(); };
  $("#log-follow").onchange = (e) => { follow = e.target.checked; sessionStorage.setItem("tsm_log_follow", follow ? "1" : "0"); };
  $("#log-filter").oninput = () => draw();
  onAct({});
  await draw();
  every(() => { if (follow) draw().catch(() => { }); }, 4000);
}

/* ============================== start ============================== */
window.addEventListener("hashchange", () => { if (S.unlocked) route(); });
document.addEventListener("DOMContentLoaded", () => {
  $("#gate-form").addEventListener("submit", (e) => { e.preventDefault(); const k = $("#gate-key").value.trim(); if (k) unlock(k); });
  $("#logout").addEventListener("click", () => lock());
  if (S.key) { $("#shell").classList.remove("hidden"); boot(); }
  else {
    // management_auth: none → no key needed
    GET("/api/v1/status").then(() => { S.key = ""; $("#shell").classList.remove("hidden"); boot(); }).catch(() => lock());
  }
});
