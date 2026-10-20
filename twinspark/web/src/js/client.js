/* TwinSpark Manager — workstation client mode.
   Loaded after app.js. When this page is served by `tsm client` (the app on your own computer), the GUI
   gains a Connection page, the sidebar shows the three connection states, node pages explain a dropped
   connection instead of failing, and Integrations can configure and run Hermes on this computer.
   Served by node A itself, none of this is active (GET /api/v1/client/info answers 404 there).
   Everything dynamic goes through esc(). */
"use strict";

Object.assign(ICON, {
  connection: '<path d="M7 17a4 4 0 0 1 0-8h2M15 9h2a4 4 0 0 1 0 8h-2M8 13h8" stroke="currentColor" stroke-width="1.9" fill="none" stroke-linecap="round"/>',
});
ROUTES.push([/^\/connection$/, () => viewConnection()]);

/** The session key `tsm client` put in the address (#k=…): kept for this tab, then removed from the address bar. */
function clientKeyFromFragment(hash) {
  const m = /^#k=([A-Za-z0-9_-]{16,128})$/.exec(hash || "");
  return m ? m[1] : "";
}
(function takeFragmentKey() {
  if (typeof location === "undefined") return;
  const key = clientKeyFromFragment(location.hash);
  if (!key) return;
  S.key = key;
  try { sessionStorage.setItem("tsm_key", key); } catch { /* private window: the key lives in this page only */ }
  if (typeof history !== "undefined") history.replaceState(null, "", location.pathname + location.search + "#/");
})();

const CLIENT_LOCKED = "This tab's client session ended (the TwinSpark client was restarted or closed). Open the address `tsm client` printed — it ends in #k=… — or paste the part after #k= here.";

/** Called by boot(): find out whether this page talks to the workstation client, before the navigation is built. */
async function clientBoot() {
  if (S.client === undefined) {
    try { S.client = await GET("/api/v1/client/info"); }
    catch (e) { S.client = null; }
  }
  if (!S.client) return;
  if (!NAV.some(n => n[0] === "connection")) NAV.unshift(["connection", "Connection"]);
  document.title = "TwinSpark client";
  const foot = $("#client-states");
  if (foot) foot.classList.remove("hidden");
  try { S.clientStatus = await GET("/api/v1/client/status"); } catch (e) { S.clientStatus = null; }
  const home = !location.hash || location.hash === "#/" || location.hash === "#";
  if (home && !(S.clientStatus && S.clientStatus.states.client_connected) && typeof history !== "undefined") {
    history.replaceState(null, "", location.pathname + location.search + "#/connection");
  }
}

/* ---- the three states ---------------------------------------------------------------------------- */
function clientPhase(st) {
  if (!st) return { cls: "bad", text: "client stopped", pulse: false };
  const t = (st.transport || {}).state;
  if (st.states.client_connected) return (st.node || {}).busy ? { cls: "warn", text: "switching…", pulse: true } : { cls: "good", text: (st.connection || {}).name || "connected", pulse: false };
  if (st.phase === "connecting") return { cls: "warn", text: "connecting…", pulse: true };
  if (st.phase === "connected" && ["connecting", "reconnecting"].includes(t)) return { cls: "warn", text: "reconnecting…", pulse: true };
  if (st.phase === "needs-action") return { cls: "warn", text: "action needed", pulse: false };
  if (st.phase === "error" || (st.phase === "connected" && t && t !== "up" && t !== "direct")) return { cls: "bad", text: "connection lost", pulse: false };
  return { cls: "", text: "not connected", pulse: false };
}
/** [[label, ok, detail], …] for the three facts the brief keeps apart. */
function clientStates(st) {
  const s = (st && st.states) || {}, node = (st && st.node) || {}, model = node.model || {};
  const conn = (st && st.connection) || null, t = (st && st.transport) || {};
  const linkDetail = !conn ? "No Spark chosen yet." : s.client_connected
    ? (t.state === "direct" ? `Direct to ${conn.direct_url || conn.host}` : `SSH to ${conn.ssh_user ? conn.ssh_user + "@" : ""}${conn.host} — forwards up`)
    : (st.message || `Not connected to ${conn.host}.`);
  const agents = Object.entries(node.agents || {}).sort();
  const agentDetail = !s.client_connected ? "Checked once connected." : !agents.length ? "Node A reported no agents." :
    agents.map(([n, a]) => `node ${n}: ${a.version || "?"}${a.runtime_mode ? " (" + a.runtime_mode + ")" : ""}${a.reachable === false ? " — not reachable" : a.ok ? "" : " — version differs"}`).join(" · ");
  const act = model.active;
  const modelDetail = !s.client_connected ? "Checked once connected." : model.ready
    ? `${act ? act.profile : "?"} serving ${(model.aliases || []).join(", ")}${model.dry_run ? " (dry run: simulated)" : ""}`
    : act ? `${act.profile} is active but not serving yet` : "No model is active. Switch to a recipe on Profiles.";
  return [["Client connected", !!s.client_connected, linkDetail], ["Agents compatible", !!s.agents_compatible, agentDetail], ["Model ready", !!s.model_ready, modelDetail]];
}
function clientStatesHtml(st, compact = false) {
  return clientStates(st).map(([label, ok, detail]) => compact
    ? `<div class="client-state" title="${esc(detail)}">${dot(ok ? "good" : "")}<span>${esc(label)}</span></div>`
    : `<div class="item" data-key="${esc(label)}"><div class="l"><div class="t">${dot(ok ? "good" : "")} ${esc(label)} ${tag(ok ? "yes" : "no", ok ? "good" : "muted")}</div><div class="d">${esc(detail)}</div></div></div>`).join("");
}

/** ping() in client mode: the client's own status, so the pill tells a stopped client from a lost Spark. */
async function clientPing() {
  const pill = $("#conn");
  let st = null;
  try { st = await GET("/api/v1/client/status"); } catch (e) { st = null; }
  S.clientStatus = st;
  const p = clientPhase(st);
  pill.innerHTML = `<a href="#/connection" class="pill-link">${dot(p.cls, p.pulse)} ${esc(p.text)}</a>`;
  pill.className = "pill " + (p.cls === "good" ? "ok" : p.cls === "bad" ? "bad" : "");
  const node = (st && st.node) || {};
  $("#version").textContent = st ? `client v${st.client_version}${node.version ? " · node A v" + node.version : ""}` : "";
  if (st && node.version) S.version = node.version;
  const foot = $("#client-states");
  if (foot) foot.innerHTML = st ? clientStatesHtml(st, true) : "";
}

/** A node page that failed because of the client, not because of node A (see the X-TwinSpark-Client header). */
function clientErrorHtml(e) {
  const creds = e.client === "credentials";
  return `<div class="view"><div class="view-head"><div><h1>${creds ? "Node A refused the stored key" : "Not connected to the Sparks"}</h1><p>${esc(e.message)}</p></div></div>
    <div class="callout ${creds ? "warn" : "info"}"><div>${creds ? "The management key saved on this computer no longer matches node A (it was rotated or set up again). Paste the current one on the Connection page." : "This page comes from node A. The client on this computer keeps the connection; it is down or not made yet."} The Sparks keep serving either way.</div></div>
    <div class="row"><a class="btn primary" href="#/connection">Open Connection</a><button class="btn" onclick="route()">Retry</button></div></div>`;
}

/* ---- Connection page ------------------------------------------------------------------------------ */
const CHECK_MARK = { ok: ["done", "✓"], warn: ["warn", "!"], todo: ["todo", ""], blocked: ["blocked", "✗"] };
function setupChecksHtml(checks) {
  return `<div class="start-list">${(checks || []).map(c => {
    const [cls, mark] = CHECK_MARK[c.status] || ["todo", ""];
    const fix = c.fix || {};
    return `<div class="card start-step ${cls}" data-key="${esc(c.id)}"><div class="start-mark ${cls}" aria-hidden="true">${mark}</div><div class="grow">
      <div class="title">${esc(c.title)} ${c.status === "ok" ? "" : tag(c.status, c.status === "warn" ? "warn" : c.status === "blocked" ? "bad" : "muted")}</div>
      <div class="small muted">${esc(c.detail)}</div>${fix.command ? codeBlock(fix.command) : ""}${fix.href ? `<a class="btn sm" href="${esc(fix.href)}">${esc(fix.label || "Open")}</a>` : ""}</div></div>`;
  }).join("")}</div>`;
}
function connectionWhere(c) {
  if (c.mode === "direct") return c.direct_url || c.host;
  return `${c.ssh_user ? c.ssh_user + "@" : ""}${c.host}${c.ssh_port && c.ssh_port !== 22 ? ":" + c.ssh_port : ""}`;
}
const MODE_LABEL = { ssh: "SSH", tailscale: "Tailscale", direct: "direct HTTPS" };
function needHtml(st) {
  if (st.phase !== "needs-action" && st.phase !== "error") return "";
  const id = st.connection ? st.connection.id : "";
  const what = { "host-key": ["Confirm node A's host key", "Before the first connection the client shows you the SSH host key node A presents, so you can compare it with the Spark itself.", "Check host key"],
    auth: ["SSH login was refused", "Node A did not accept this computer's SSH keys. Enter the password of the SSH user once: the client installs its own key and forgets the password.", "Install key with password"],
    keys: ["The TwinSpark keys are needed", "The client could not read the management key from node A. Paste it, or read it with sudo over SSH.", "Enter keys"] }[st.need];
  if (!what) return `<div class="callout bad" data-key="need"><div><b>Connection failed.</b> ${esc(st.message || "")}<div class="row" style="margin-top:8px"><button class="btn sm" data-act="conn-connect" data-id="${esc(id)}">Try again</button>${st.connection && st.connection.mode !== "direct" ? `<button class="btn sm" data-act="conn-probe" data-id="${esc(id)}">Check setup</button>` : ""}</div></div></div>`;
  return `<div class="callout warn" data-key="need"><div><b>${esc(what[0])}.</b> ${esc(what[1])}${st.message ? `<div class="small muted" style="margin-top:4px">${esc(st.message)}</div>` : ""}<div class="row" style="margin-top:8px"><button class="btn primary sm" data-act="conn-resolve" data-id="${esc(id)}" data-need="${esc(st.need)}">${esc(what[2])}</button></div></div></div>`;
}
function endpointHtml(st) {
  const ep = st.endpoint || {};
  if (!st.states.client_connected || !ep.base_url) return "";
  return `<div class="card" data-key="endpoint"><div class="card-head"><h2>For apps on this computer</h2>${tag("OpenAI-compatible", "info")}</div>
    <div class="kv"><b>Base URL</b><span class="mono">${esc(ep.base_url)}</span><b>API key</b><span>${ep.has_inference_key ? `<button class="btn sm" data-act="conn-reveal">Copy inference key</button>` : '<span class="muted">none stored — paste it under Keys</span>'}</span>
    <b>tsm CLI</b><span class="mono">eval "$(tsm client env ${esc((st.connection || {}).name || "NAME")})"</span></div>
    <p class="small muted">${st.connection && st.connection.mode !== "direct" ? "The address is an SSH forward: keep <span class=\"mono\">tsm client</span> running while apps use it. The local port stays the same between launches, so app settings keep working." : "Apps on this computer reach the gateway directly."} The management key never leaves this client.</p></div>`;
}
function connectionHtml(info, st, probe) {
  const conns = info.connections || [], cur = st.connection;
  const ssh = info.ssh || {};
  let html = `<div class="view" data-live="connection"><div class="view-head"><div><p class="eyebrow">Workstation client</p><h1>Connection</h1><p>Point this client at node A's address. It logs in over SSH (or your tailnet), keeps the forwards up and passes the GUI and your local apps through. Node A keeps running the model on its own.</p></div>
    <div class="row">${cur ? `<button class="btn" data-act="conn-disconnect">Disconnect</button>` : ""}<button class="btn primary" data-act="conn-add">Add a Spark</button></div></div>`;
  if (ssh.error) html += `<div class="callout bad" data-key="ssh"><div><b>No SSH client found.</b> ${esc(ssh.error)}</div></div>`;
  if (cur) {
    const t = st.transport || {};
    html += `<div class="card" data-key="current"><div class="card-head"><h2>${esc(cur.name)} <span class="muted small">${esc(connectionWhere(cur))}</span></h2>${tag(MODE_LABEL[cur.mode] || cur.mode, "muted")}</div>
      ${needHtml(st)}<div class="list">${clientStatesHtml(st)}</div>
      ${(t.forwards || []).length ? `<div class="kv" style="margin-top:12px">${t.forwards.map(f => `<b>${esc(f.label === "api" ? "Management API" : f.label === "gateway" ? "Model gateway" : f.label)}</b><span class="mono">${esc(f.local)} → node A :${esc(f.remote_port)}</span>`).join("")}<b>Tunnel</b><span>${esc(t.state)}${t.since ? " since " + esc(when(t.since)) : ""}${t.restarts ? ` · ${esc(t.restarts)} restarts` : ""}${t.last_error ? ` — ${esc(t.last_error)}` : ""}</span></div>` : ""}
      ${st.states.client_connected && st.node && st.node.compat && st.node.compat.state !== "ok" ? `<div class="callout warn" style="margin-top:12px"><div>${esc(st.node.compat.note || "")}</div></div>` : ""}
      ${st.states.client_connected ? `<div class="row" style="margin-top:12px"><a class="btn primary sm" href="#/dashboard">Open the dashboard</a><a class="btn sm" href="#/integrations">Connect an app</a></div>` : ""}</div>`;
    html += endpointHtml(st);
  }
  html += `<h3 class="section-title">Saved Sparks</h3>`;
  html += conns.length ? `<div class="list" data-key="conns">${conns.map(c => {
    const on = cur && cur.id === c.id;
    return `<div class="item ${on && st.states.client_connected ? "active" : ""}" data-key="${esc(c.id)}"><div class="l"><div class="t">${esc(c.name)} ${tag(MODE_LABEL[c.mode] || c.mode, "muted")}${on ? tag(clientPhase(st).text, clientPhase(st).cls || "muted") : ""}</div>
      <div class="d mono">${esc(connectionWhere(c))}</div><div class="d">${c.last_connected_at ? "last connected " + esc(ago(c.last_connected_at)) : "never connected"}${c.local_gateway_port ? ` · local gateway 127.0.0.1:${esc(c.local_gateway_port)}` : ""}</div></div>
      <div class="row actions">${on && st.phase !== "idle" ? "" : `<button class="btn primary sm" data-act="conn-connect" data-id="${esc(c.id)}">Connect</button>`}${c.mode !== "direct" ? `<button class="btn sm" data-act="conn-probe" data-id="${esc(c.id)}">Check setup</button>` : ""}<button class="btn sm" data-act="conn-keys" data-id="${esc(c.id)}">Keys</button>${c.mode !== "direct" ? `<button class="btn sm" data-act="conn-ports" data-id="${esc(c.id)}">Ports</button>` : ""}<button class="btn ghost sm" data-act="conn-remove" data-id="${esc(c.id)}">Remove</button></div></div>`;
  }).join("")}</div>` : `<div class="card" data-key="conns">${empty("No Spark yet", "Add node A by its address: the LAN IP or hostname, or its Tailscale address.")}<div class="row"><button class="btn primary" data-act="conn-add">Add a Spark</button></div></div>`;
  if (probe) {
    html += `<h3 class="section-title">Setup of ${esc(probe.name)}</h3>`;
    if (probe.error) html += `<div class="callout bad" data-key="probe-err"><div>${esc(probe.error)}</div></div>`;
    html += `<div data-key="probe">${setupChecksHtml(probe.checks)}</div>`;
  }
  html += `<p class="small muted" data-key="foot">Client ${esc(info.version)} · keys stored in ${esc(info.credential_backend === "keyring" ? "your system keychain" : "a private file")} under <span class="mono">${esc(info.home)}</span>${ssh.path ? ` · ssh: <span class="mono">${esc(ssh.path)}</span>` : ""}</p></div>`;
  return html;
}

async function addConnectionFlow() {
  const body = `<div class="form">
    <label class="field"><span>Name</span><input id="cn-name" value="sparks" maxlength="64" autocomplete="off"/></label>
    <label class="field"><span>Node A's address</span><input id="cn-host" placeholder="192.168.1.20 or spark-a.local or a Tailscale address" maxlength="253" autocomplete="off" spellcheck="false"/><span class="help">The Spark that runs the controller. <button type="button" class="btn ghost sm" id="cn-peers">Find on my tailnet</button></span></label>
    <div id="cn-peer-list"></div>
    <label class="field"><span>SSH user on node A</span><input id="cn-user" placeholder="the user you log in with" maxlength="32" autocomplete="off" spellcheck="false"/></label>
    <label class="field"><span>Connection</span><select id="cn-mode"><option value="">Automatic (Tailscale for tailnet addresses, else SSH)</option><option value="ssh">SSH over the LAN</option><option value="tailscale">SSH over Tailscale</option><option value="direct">Direct HTTPS (a listener you configured with TLS)</option></select></label>
    <label class="field hidden" id="cn-url-field"><span>Direct URL</span><input id="cn-url" placeholder="https://spark-a.example:8443" autocomplete="off" spellcheck="false"/><span class="help">Only for a management listener you exposed deliberately with TLS. SSH needs no listener changes.</span></label>
    <label class="field"><span>SSH port</span><input id="cn-port" value="22" inputmode="numeric" autocomplete="off"/></label></div>
    <p class="small muted">Nothing is changed on the Spark. The first connect shows you its host key to confirm.</p>`;
  let created = null;
  const { value } = await modal({
    title: "Add a Spark", body, wide: true,
    onOpen: (root) => {
      const mode = root.querySelector("#cn-mode"), urlField = root.querySelector("#cn-url-field");
      mode.onchange = () => urlField.classList.toggle("hidden", mode.value !== "direct");
      root.querySelector("#cn-peers").onclick = async (e) => {
        const list = root.querySelector("#cn-peer-list");
        list.innerHTML = '<span class="spin"></span>';
        try {
          const { peers } = await GET("/api/v1/client/tailscale/peers");
          list.innerHTML = peers.length ? `<div class="chip-row">${peers.map(p => `<button type="button" class="btn sm" data-peer="${esc(p.address)}">${dot(p.online ? "good" : "")} ${esc(p.name || p.dns_name)} <span class="mono">${esc(p.address)}</span></button>`).join("")}</div>`
            : '<p class="small muted">No tailnet peers found (Tailscale is not installed or not logged in on this computer).</p>';
          list.onclick = (ev) => { const b = ev.target.closest("[data-peer]"); if (b) { root.querySelector("#cn-host").value = b.dataset.peer; mode.value = ""; urlField.classList.add("hidden"); } };
        } catch (err) { list.innerHTML = `<p class="err">${esc(err.message)}</p>`; }
      };
    },
    actions: [{ label: "Cancel", value: null }, {
      label: "Add and connect", value: "add", cls: "primary",
      validate: async (root) => {
        const v = (id) => root.querySelector(id).value.trim();
        const mode = v("#cn-mode") || null;
        const body = { name: v("#cn-name") || "sparks", host: v("#cn-host"), ssh_user: v("#cn-user"), ssh_port: Number(v("#cn-port") || 22), mode };
        if (mode === "direct") body.direct_url = v("#cn-url");
        if (!body.host && mode !== "direct") throw new Error("Enter node A's address.");
        if (mode === "direct" && !body.direct_url) throw new Error("Enter the https URL of the listener.");
        if (mode === "direct" && !body.host) body.host = body.direct_url.replace(/^https?:\/\//i, "").split(/[/:]/)[0];
        if (mode !== "direct" && !body.ssh_user) throw new Error("Enter the SSH user on node A.");
        if (!Number.isInteger(body.ssh_port) || body.ssh_port < 1 || body.ssh_port > 65535) throw new Error("The SSH port is a number from 1 to 65535.");
        created = await POST("/api/v1/client/connections", body);
        return true;
      },
    }],
  });
  return value === "add" ? created : null;
}

async function hostKeyFlow(conn) {
  const scan = await POST(`/api/v1/client/connections/${enc(conn.id)}/hostkey/scan`);
  if (scan.status === "trusted") return true;
  const changed = scan.status === "changed";
  const rows = (keys) => `<div class="table-wrap"><table><tbody>${keys.map(k => `<tr><td>${esc(k.type)}</td><td class="mono">${esc(k.fingerprint)}</td></tr>`).join("")}</tbody></table></div>`;
  const body = (changed ? `<div class="callout bad"><div><b>The host key changed since you trusted it.</b> That is expected after node A was reinstalled — and it is also what a machine pretending to be node A looks like. Continue only if you know why it changed.</div></div><p class="small muted">Trusted before:</p>${rows(scan.known)}<p class="small muted">Presented now:</p>` : `<p>${esc(conn.host)} presents these SSH host keys. Compare them with what node A itself prints (on its console, or in an SSH session you already trust):</p>`)
    + rows(scan.keys) + codeBlock("for f in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf \"$f\"; done") + '<p class="small muted">Only the keys shown here are stored; if node A presents different ones when you confirm, nothing is trusted.</p>';
  const { value } = await modal({
    title: changed ? "Host key changed" : "Confirm node A's host key", body, wide: true,
    actions: [{ label: "Cancel", value: null }, {
      label: changed ? "Replace the trusted key" : "They match — trust", value: "trust", cls: changed ? "danger" : "primary",
      validate: async () => { await POST(`/api/v1/client/connections/${enc(conn.id)}/hostkey/trust`, { fingerprints: scan.keys.map(k => k.fingerprint), replace: changed }); return true; },
    }],
  });
  return value === "trust";
}

async function passwordFlow(conn, st) {
  const body = `<p>${esc(conn.ssh_user)}@${esc(conn.host)} refused this computer's SSH keys${st && st.message ? ` (${esc(st.message)})` : ""}.</p>
    <p>Enter the SSH password once: the client logs in with it, adds its own key to <span class="mono">~/.ssh/authorized_keys</span> of ${esc(conn.ssh_user)} on node A and forgets the password. It is not stored anywhere.</p>
    <label class="field"><span>Password for ${esc(conn.ssh_user)}@${esc(conn.host)}</span><input id="pw" type="password" autocomplete="off"/></label>
    <p class="small muted">Or add a key yourself (<span class="mono">ssh-copy-id ${esc(conn.ssh_user)}@${esc(conn.host)}</span>) and connect again.</p>`;
  const { value } = await modal({
    title: "Install this client's SSH key", body,
    actions: [{ label: "Cancel", value: null }, {
      label: "Install key", value: "ok", cls: "primary",
      validate: async (root) => {
        const input = root.querySelector("#pw"), password = input.value;
        if (!password) throw new Error("Enter the password.");
        try { await POST(`/api/v1/client/connections/${enc(conn.id)}/key/install`, { password }); }
        finally { input.value = ""; }
        return true;
      },
    }],
  });
  return value === "ok";
}

async function keysFlow(conn, st) {
  const ssh = conn.mode !== "direct";
  const body = `${st && st.message ? `<div class="callout info"><div>${esc(st.message)}</div></div>` : ""}
    <p>On node A, <span class="mono">sudo tsm init --show</span> prints both keys. They are stored ${esc(S.client && S.client.credential_backend === "keyring" ? "in your system keychain" : "in a private file on this computer")} for this connection only.</p>
    <label class="field"><span>management_api_key</span><input id="k-mgmt" type="password" autocomplete="off" spellcheck="false"/><span class="help">Used by this client only, to talk to node A. Never written into an app.</span></label>
    <label class="field"><span>inference_api_key (optional)</span><input id="k-inf" type="password" autocomplete="off" spellcheck="false"/><span class="help">What local apps use for the model endpoint.</span></label>`;
  const actions = [{ label: "Cancel", value: null }];
  if (ssh) actions.push({ label: "Read over SSH with sudo", value: "fetch", validate: async () => { await POST(`/api/v1/client/connections/${enc(conn.id)}/credentials/fetch`, { sudo: true }); return true; } });
  actions.push({
    label: "Save keys", value: "save", cls: "primary",
    validate: async (root) => {
      const management_key = root.querySelector("#k-mgmt").value.trim(), inference_key = root.querySelector("#k-inf").value.trim();
      if (!management_key && !inference_key) throw new Error("Paste at least one key.");
      await PUT(`/api/v1/client/connections/${enc(conn.id)}/credentials`, { management_key: management_key || null, inference_key: inference_key || null });
      return true;
    },
  });
  const { value } = await modal({ title: `Keys for ${conn.name}`, body, actions });
  return value === "fetch" || value === "save";
}

async function resolveNeed(conn, st) {
  if (st.need === "host-key") return hostKeyFlow(conn);
  if (st.need === "auth") return passwordFlow(conn, st);
  if (st.need === "keys") return keysFlow(conn, st);
  return false;
}

/** Connect, and walk the user through whatever node A needs first (host key, key install, API keys). */
async function connectFlow(conn) {
  for (let step = 0; step < 5; step++) {
    const st = await POST(`/api/v1/client/connections/${enc(conn.id)}/connect?wait=true`);
    S.clientStatus = st;
    if (st.phase === "connected") { toast(`connected to ${conn.name}`); return st; }
    if (st.phase !== "needs-action" || !(await resolveNeed(st.connection || conn, st))) return st;
  }
  return null;
}

async function viewConnection() {
  const g = S.gen;
  if (!S.client) { render(g, `<div class="view">${empty("Not available here", "The Connection page belongs to the workstation client (tsm client). This page is served by node A itself.")}</div>`); return; }
  let probe = null;
  const draw = async () => {
    const [info, st] = await Promise.all([GET("/api/v1/client/info"), GET("/api/v1/client/status")]);
    S.client = info; S.clientStatus = st;
    renderLive(g, connectionHtml(info, st, probe), "connection");
    return st;
  };
  const byId = (id) => (S.client.connections || []).find(c => c.id === id);
  onAct({
    "conn-add": async () => { const c = await addConnectionFlow(); if (c) { await draw(); await connectFlow(c); await draw(); } },
    "conn-connect": async (btn) => { const c = byId(btn.dataset.id); if (c) await busy(btn, () => connectFlow(c)); await draw(); },
    "conn-resolve": async (btn) => {
      const st = S.clientStatus, c = (st && st.connection) || byId(btn.dataset.id);
      if (c && await resolveNeed(c, st)) await busy(btn, () => connectFlow(c));
      await draw();
    },
    "conn-disconnect": async (btn) => { await busy(btn, () => POST("/api/v1/client/disconnect")); toast("disconnected — the Sparks keep serving"); await draw(); },
    "conn-probe": async (btn) => {
      const c = byId(btn.dataset.id); if (!c) return;
      const r = await busy(btn, () => POST(`/api/v1/client/connections/${enc(c.id)}/probe`));
      probe = { name: c.name, checks: r.checks, error: r.error }; await draw();
    },
    "conn-ports": async (btn) => {
      const c = byId(btn.dataset.id); if (!c) return;
      const v = await formBox(`Local ports for ${c.name}`, [
        { id: "gw", label: "Model gateway (what apps on this computer use)", value: c.local_gateway_port || "", placeholder: "automatic" },
        { id: "api", label: "Management API", value: c.local_api_port || "", placeholder: "automatic" },
      ], { label: "Save", intro: "The local ends of the SSH forwards. Leave a field empty to pick a free port at the next connect. Changes apply when you connect again; apps configured with the old gateway port need the new one.",
        validate: (vals) => {
          for (const k of ["gw", "api"]) if (vals[k] && !(/^\d+$/.test(vals[k]) && +vals[k] >= 1024 && +vals[k] <= 65535)) throw new Error("A port is a number from 1024 to 65535, or empty.");
          return true;
        } });
      if (!v) return;
      await api("PATCH", `/api/v1/client/connections/${enc(c.id)}`, { local_gateway_port: v.gw ? +v.gw : null, local_api_port: v.api ? +v.api : null });
      toast("ports saved — they apply at the next connect"); await draw();
    },
    "conn-keys": async (btn) => { const c = byId(btn.dataset.id); if (c && await keysFlow(c, null)) { toast("keys saved"); await draw(); } },
    "conn-remove": async (btn) => {
      const c = byId(btn.dataset.id); if (!c) return;
      if (!(await confirmBox(`Remove ${c.name}?`, `This forgets the connection and the keys stored for it on this computer. Nothing changes on the Sparks. Apps configured with 127.0.0.1:${esc(c.local_gateway_port || "…")} stop working until you add it again.`, { label: "Remove", danger: true }))) return;
      await DEL(`/api/v1/client/connections/${enc(c.id)}`); if (probe && probe.name === c.name) probe = null; await draw();
    },
    "conn-reveal": async (btn) => {
      const ep = await busy(btn, () => GET("/api/v1/client/endpoint?reveal=true"));
      if (!ep.api_key) return toast("no inference key stored", "bad");
      navigator.clipboard?.writeText(ep.api_key).then(() => toast("inference key copied"), () => toast("copy failed", "bad"));
    },
  });
  await draw();
  poll(async () => { await draw(); }, 2500);
}

/* ---- Integrations: test from this computer, and the Hermes lifecycle ---------------------------------- */
const HERMES_STATE = [["installed", "Installed"], ["configured", "Configured"], ["reachable", "Reachable"], ["compatible", "Compatible"], ["running", "Running"]];
const HERMES_PHASE = { "not-installed": ["not installed", "muted"], "launcher-broken": ["launcher broken", "bad"], "not-configured": ["not connected", "warn"], unreachable: ["endpoint unreachable", "bad"], running: ["running", "good"], ready: ["ready", "good"], unverified: ["not validated", "info"] };
function checksListHtml(checks) {
  const cls = { pass: "good", fail: "bad", warn: "warn", skip: "muted" };
  return `<div class="list">${(checks || []).map(c => `<div class="item"><div class="l"><div class="t">${dot(cls[c.status] || "")} ${esc(c.title)} ${tag(c.status, cls[c.status] || "muted")} ${tag(c.layer, "muted")}</div><div class="d">${esc(c.detail)}</div></div></div>`).join("")}</div>`;
}
function hermesStateHtml(state) {
  return `<div class="chip-row">${HERMES_STATE.map(([k, label]) => {
    const v = state[k];
    return tag(`${v === true ? "✓" : v === false ? "✗" : "–"} ${label}`, v === true ? "good" : v === false ? "warn" : "muted");
  }).join("")}</div>`;
}
function hermesCardHtml(h, data) {
  const st = data && data.status, det = st ? st.detection : null, state = st ? st.state : null;
  const phase = state ? (HERMES_PHASE[state.phase] || [state.phase, "muted"]) : ["checking…", "muted"];
  const profiles = (det && det.profiles) || [];
  const model = (st && st.config && st.config.model) || null;
  const can = (x) => state && state.installed && x;
  return `<div class="card-head"><h2>Hermes Agent</h2>${tag(phase[0], phase[1])}</div>
    <div class="form"><label class="field"><span>Runs on</span><select data-hfield="host"><option value="workstation" ${h.host === "workstation" ? "selected" : ""}>This computer (recommended)</option><option value="node-a" ${h.host === "node-a" ? "selected" : ""}>Node A (an existing install there)</option></select><span class="help">${h.host === "node-a" ? "Hermes then shares node A's memory with the model." : "Its memory and tools stay off the Sparks; it reaches the model through this client."}</span></label>
    <label class="field"><span>Hermes profile</span><select data-hfield="profile"><option value="">default</option>${profiles.filter(p => p !== "default").map(p => `<option value="${esc(p)}" ${h.profile === p ? "selected" : ""}>${esc(p)}</option>`).join("")}</select></label></div>
    ${state ? hermesStateHtml(state) : '<span class="spin"></span>'}
    ${det ? `<div class="kv" style="margin-top:10px"><b>Where</b><span>${esc(det.host)}${det.version ? " · Hermes " + esc(det.version) : ""}</span><b>Data</b><span class="mono">${esc(det.profile_home || det.home || "—")}</span>${model ? `<b>Main model</b><span class="mono">${esc([model.provider, model.default, model.base_url].filter(Boolean).join(" · ") || "—")}${model.context_length ? " · " + esc(num(model.context_length)) + " tokens" : ""}</span>` : ""}${data.endpoint ? `<b>TwinSpark</b><span class="mono">${esc(data.endpoint.base_url)} · ${esc(data.endpoint.alias)}${data.endpoint.context_length ? " · " + esc(num(data.endpoint.context_length)) + " tokens" : ""}</span>` : ""}</div>` : ""}
    ${st && st.notes && st.notes.length ? `<div class="callout info" style="margin-top:10px"><div><ul>${st.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul></div></div>` : ""}
    ${data && data.error ? `<p class="err">${esc(data.error)}</p>` : ""}
    <div class="row" style="margin-top:12px">
      <button class="btn sm" data-hact="refresh">Detect again</button>
      <button class="btn primary sm" data-hact="preview" ${can(data && data.endpoint) ? "" : "disabled"}>Preview connection</button>
      <button class="btn sm" data-hact="validate" ${can(state && state.configured) ? "" : "disabled"}>Validate</button>
      <button class="btn sm" data-hact="launch-gateway" ${can(state && state.launcher_ok && !state.running) ? "" : "disabled"}>Start gateway</button>
      <button class="btn sm" data-hact="launch-cli" ${can(state && state.launcher_ok) ? "" : "disabled"}>Chat command</button>
      <button class="btn sm" data-hact="restart" ${can(state && state.running) ? "" : "disabled"}>Restart</button>
      <button class="btn sm" data-hact="stop" ${can(state && state.running) ? "" : "disabled"}>Stop</button>
      <button class="btn sm" data-hact="logs" ${can(true) ? "" : "disabled"}>Logs</button>
      <button class="btn ghost sm" data-hact="restore" ${can(true) ? "" : "disabled"}>Restore a backup</button>
      <button class="btn ghost sm" data-hact="disconnect" ${can(state && state.configured) ? "" : "disabled"}>Disconnect from TwinSpark</button>
    </div>
    ${state && !state.installed ? `<p class="small muted" style="margin-top:8px">Install Hermes on ${h.host === "node-a" ? "node A" : "this computer"} first (see the Hermes documentation), then detect again.</p>` : ""}
    <div id="hermes-out" aria-live="polite"></div>`;
}
function previewHtml(pv) {
  return `${(pv.blocked || []).length ? `<div class="callout bad"><div><b>Cannot apply:</b><ul>${pv.blocked.map(b => `<li>${esc(b)}</li>`).join("")}</ul></div></div>` : ""}
    ${(pv.warnings || []).length ? `<div class="callout warn"><div><ul>${pv.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul></div></div>` : ""}
    ${(pv.files || []).map(f => `<p class="small"><b>${esc(f.label || f.path)}</b> <span class="mono muted">${esc(f.path)}</span> ${f.changed ? tag("changes", "info") : tag("unchanged", "muted")}</p>${f.changed ? codeBlock(f.diff) : ""}`).join("")}
    ${(pv.notes || []).length ? `<p class="small muted">${pv.notes.map(esc).join("<br/>")}</p>` : ""}
    <p class="small muted">A backup of both files is taken first; "Restore a backup" puts them back. Secrets are shown masked.</p>`;
}

/** Called by viewIntegrations() in client mode, after it rendered. */
function clientIntegrations(g, state, aliases) {
  const urlInput = $("#integration-url");
  const help = urlInput && urlInput.parentElement.querySelector(".help");
  if (help) help.textContent = "This client's forward of node A's gateway: what apps on this computer use while tsm client runs.";
  const actions = $(".integration-actions");
  if (actions && !$("#integration-local-test")) {
    const b = document.createElement("button");
    b.className = "btn"; b.id = "integration-local-test"; b.type = "button"; b.textContent = "Test from this computer";
    b.onclick = async () => {
      const alias = $("#integration-alias").value, out = $("#integration-files");
      try {
        const r = await busy(b, () => POST("/api/v1/client/integrations/test", { alias, tools: true }));
        if (current(g)) out.innerHTML = `<div class="card"><h2>From this computer</h2><p class="small muted mono">${esc(r.endpoint.base_url)} · ${esc(r.endpoint.alias)}</p>${checksListHtml(r.checks)}</div>`;
      } catch (e) { fail(e); }
    };
    actions.prepend(b);
  }
  const host = document.createElement("div");
  host.className = "card"; host.id = "hermes-card";
  const anchor = $("#integration-files");
  if (!anchor || $("#hermes-card")) return;
  anchor.after(host);
  hermesCard(g, host, aliases);
}

function hermesCard(g, el, aliases) {
  const h = S.hermes || (S.hermes = { host: "workstation", profile: "" });
  let data = null, epoch = 0;
  const body = (extra = {}) => ({ host: h.host, profile: h.profile || null, alias: ($("#integration-alias") || {}).value || null, ...extra });
  const out = (html) => { const o = el.querySelector("#hermes-out"); if (o) o.innerHTML = html; };
  const draw = () => { if (current(g)) el.innerHTML = hermesCardHtml(h, data); };
  const load = async () => {
    const mine = ++epoch;
    data = data ? { ...data, error: null } : null; draw();
    try { const r = await POST("/api/v1/client/integrations/hermes/status", body({ lines: 20 })); if (mine === epoch) data = r; }
    catch (e) { if (mine === epoch) data = { status: null, endpoint: null, error: e.message }; }
    if (mine === epoch) draw();
  };
  el.onchange = (e) => {
    const f = e.target.closest("[data-hfield]"); if (!f) return;
    h[f.dataset.hfield] = f.value; if (f.dataset.hfield === "host") h.profile = "";
    data = null; load();
  };
  el.onclick = async (e) => {
    const b = e.target.closest("[data-hact]"); if (!b || b.disabled || b.dataset.pending) return;
    b.dataset.pending = "true";
    try { await busy(b, () => HERMES_ACTS[b.dataset.hact]()); }
    catch (err) { out(`<p class="err">${esc(err.message || String(err))}</p>`); }
    finally { delete b.dataset.pending; }
  };
  const HERMES_ACTS = {
    refresh: () => load(),
    preview: async () => {
      const r = await POST("/api/v1/client/integrations/hermes/preview", body());
      const pv = r.preview;
      const { value } = await modal({
        title: `Connect Hermes to ${r.endpoint.alias}`, body: previewHtml(pv), wide: true,
        actions: [{ label: "Cancel", value: null }, ...(pv.token && !(pv.blocked || []).length ? [{
          label: "Apply", value: "apply", cls: "primary",
          validate: async () => { await POST("/api/v1/client/integrations/hermes/apply", body({ token: pv.token })); return true; },
        }] : [])],
      });
      if (value === "apply") { toast("Hermes now points at TwinSpark — validate it next"); await load(); }
    },
    validate: async () => {
      out('<p class="small muted">Chat, streaming and a tool call through the configured address…</p>');
      const r = await POST("/api/v1/client/integrations/hermes/validate", body());
      await load(); out(`<h3 class="section-title">Validation</h3>${checksListHtml(r.checks)}`);
    },
    "launch-gateway": async () => { const r = await POST("/api/v1/client/integrations/hermes/launch", body({ mode: "gateway" })); toast(r.detail || "gateway started"); await load(); },
    "launch-cli": async () => { const r = await POST("/api/v1/client/integrations/hermes/launch", body({ mode: "cli" })); out(`<p class="small">${esc(r.detail)}</p>${codeBlock(r.command_line)}`); },
    restart: async () => { await POST("/api/v1/client/integrations/hermes/restart", body()); toast("Hermes restarted"); await load(); },
    stop: async () => { const r = await POST("/api/v1/client/integrations/hermes/stop", body()); toast(r.detail || (r.stopped ? "stopped" : "not running")); await load(); },
    logs: async () => {
      const [all, errs] = await Promise.all([POST("/api/v1/client/integrations/hermes/logs", body({ lines: 200 })), Promise.resolve((data && data.status && data.status.error_logs) || [])]);
      out(`<h3 class="section-title">Hermes log</h3>${codeBlock((all.lines || []).join("\n") || "(empty)")}${errs.length ? `<h3 class="section-title">Errors</h3>${codeBlock(errs.join("\n"))}` : ""}`);
    },
    restore: async () => {
      const { backups } = await POST("/api/v1/client/integrations/hermes/backups", body());
      if (!backups.length) return toast("no backups yet: TwinSpark takes one before every change");
      const pick = await formBox("Restore Hermes settings", [{ id: "backup", label: "Backup", type: "select", value: backups[0].id, options: backups.map(b => [b.id, `${when(b.created)} · before ${b.kind}`]) }],
        { label: "Restore", intro: "Puts config.yaml and .env back as they were. The current files are backed up first." });
      if (!pick) return;
      const r = await POST("/api/v1/client/integrations/hermes/restore", body({ backup_id: pick.backup }));
      toast(`restored ${(r.restored || []).length} file(s)`); await load();
    },
    disconnect: async () => {
      const pv = await POST("/api/v1/client/integrations/hermes/disconnect/preview", body({ restore_previous: true }));
      const { value } = await modal({
        title: "Disconnect Hermes from TwinSpark", body: previewHtml(pv), wide: true,
        actions: [{ label: "Cancel", value: null }, ...(pv.token ? [{ label: "Disconnect", value: "ok", cls: "danger", validate: async () => { await POST("/api/v1/client/integrations/hermes/disconnect", body({ token: pv.token, restore_previous: true })); return true; } }] : [])],
      });
      if (value === "ok") { toast("Hermes no longer uses TwinSpark"); await load(); }
    },
  };
  draw(); load();
}
