/* TwinSpark Manager — "Remote" page: fix a headless node without a monitor.
   Loaded after app.js. Adds one nav entry and the #/remote[/A|B] route. Everything dynamic goes through esc().
   The terminal needs the bundled xterm.js (vendor/); without it the rest of the page still works. */
"use strict";

Object.assign(ICON, {
  remote: '<path d="M3 5h18v11H3zM8 20h8M12 16v4M7 9l2.5 2L7 13m4.5 0H15" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
});
NAV.splice(Math.max(0, NAV.findIndex(n => n[0] === "diagnostics") + 1), 0, ["remote", "Remote"]);
// The docs are not part of the installed package: link to them on GitHub (the path also names the file in a checkout).
const REMOTE_DOC = '<a href="https://github.com/ChopCookies/Twinspark-Manager/blob/main/docs/remote-management.md" target="_blank" rel="noopener noreferrer">docs/remote-management.md</a>';
ROUTES.push([/^\/remote(?:\/([A-Za-z]))?$/, (m) => viewRemote(m[1] ? m[1].toUpperCase() : null)]);

const REMOTE_FEATURES = [
  ["terminal", "Terminal", "a recorded shell on the node, as the TwinSpark user"],
  ["reboot", "Reboot", "restart the node from here"],
  ["poweroff", "Power off", "shut the node down from here"],
  ["boot_next", "Boot once from network / USB", "a rescue boot that does not change the boot order"],
  ["wol", "Wake-on-LAN setting", "turn Wake-on-LAN on for a network port"],
];
const REMOTE_SOURCES = ["agent", "controller", "privd", "terminal", "docker", "kernel", "previous-boot", "ssh", "network", "containerd", "networkd", "nvidia"];
const R = { ws: null, term: null, fit: null, ro: null, watch: null, node: null, ov: null, reach: null };

/* keys a phone keyboard lacks; each is sent to the remote shell exactly like the real key */
const TERM_KEYS = { Esc: "\x1b", Tab: "\t", "Ctrl-C": "\x03", "Ctrl-D": "\x04", "Ctrl-L": "\x0c", "↑": "\x1b[A", "↓": "\x1b[B", "←": "\x1b[D", "→": "\x1b[C" };
const remoteCmd = (feature) => `sudo tsm remote enable ${feature.replace(/_/g, "-")}`;
const remoteOn = (n, f) => !!(n && n.policy && n.policy[f]);
const remoteDur = (s) => { if (!s) return "—"; const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return (d ? d + "d " : "") + h + "h " + m + "m"; };

/* ---------- terminal ---------- */
function closeTerminal(reason) {
  const { ws, term, ro, watch } = R;
  R.ws = R.term = R.fit = R.ro = R.watch = null;
  if (watch) clearInterval(watch);
  if (ro) { try { ro.disconnect(); } catch { /* already gone */ } }
  if (ws) { try { ws.onclose = null; ws.close(); } catch { /* already closed */ } }
  if (term) { try { term.dispose(); } catch { /* already disposed */ } }
  const box = $("#r-termbox");
  if (box) box.innerHTML = "";
  setTermState(reason || "", false);
}
function setTermState(text, live) {
  const el = $("#r-termstate");
  if (el) el.textContent = text || "";
  const open = $("[data-act=term-open]"), close = $("[data-act=term-close]");
  if (open) open.classList.toggle("hidden", !!live);
  if (close) close.classList.toggle("hidden", !live);
  const wrap = $("#r-termwrap");
  if (wrap) wrap.classList.toggle("hidden", !live);
}
async function openTerminal(node) {
  if (typeof Terminal === "undefined" || typeof FitAddon === "undefined") throw new Error("the terminal widget did not load (vendor/xterm.js) — reload the page");
  closeTerminal();
  setTermState("connecting…", true);
  const box = $("#r-termbox");
  const term = new Terminal({ cursorBlink: true, fontSize: 14, scrollback: 5000, fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", theme: { background: "#050a14", foreground: "#d7e3f4", cursor: "#6aa7ff" } });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(box);
  fit.fit();
  let t;
  try { t = await POST("/api/v1/remote/terminal/ticket", { node, cols: term.cols, rows: term.rows }); }
  catch (e) { term.dispose(); closeTerminal(); throw e; }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}${t.ws_path}?ticket=${enc(t.ticket)}`);
  ws.binaryType = "arraybuffer";
  R.ws = ws; R.term = term; R.fit = fit;
  const text = new TextEncoder();
  let ended = false;
  ws.onopen = () => { setTermState("connected · recorded on the node · Ctrl-D or “exit” ends it", true); term.focus(); };
  ws.onmessage = (ev) => {
    if (typeof ev.data !== "string") { term.write(new Uint8Array(ev.data)); return; }
    let f; try { f = JSON.parse(ev.data); } catch { return; }
    if (f.type === "hello") setTermState(`session ${f.session} · ${f.recorded ? "recorded on the node" : "not recorded"} · idle limit ${Math.round((f.idle_s || 0) / 60)} min`, true);
    else if (f.type === "notice") term.write(`\r\n\x1b[33m[tsm] ${String(f.text).replace(/[\x00-\x1f\x7f]/g, " ")}\x1b[0m\r\n`);
    else if (f.type === "exit") { ended = true; term.write(`\r\n\x1b[2m[tsm] session ended${f.reason ? " — " + String(f.reason).replace(/[\x00-\x1f\x7f]/g, " ") : ""}\x1b[0m\r\n`); }
    else if (f.type === "error") { ended = true; term.write(`\r\n\x1b[31m${String(f.error).replace(/[\x00-\x1f\x7f]/g, " ")}\x1b[0m\r\n`); }
  };
  ws.onclose = () => {
    if (R.ws !== ws) return;
    setTermState(ended ? "session ended" : "connection closed", false);
    $("#r-termwrap")?.classList.remove("hidden");
    $("[data-act=term-open]")?.classList.remove("hidden");
    $("[data-act=term-close]")?.classList.add("hidden");
  };
  term.onData((d) => { if (ws.readyState === 1) ws.send(text.encode(d)); });
  term.onBinary((d) => { if (ws.readyState === 1) ws.send(Uint8Array.from(d, c => c.charCodeAt(0))); });
  term.onResize(({ cols, rows }) => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: "resize", cols, rows })); });
  R.ro = new ResizeObserver(() => { try { fit.fit(); } catch { /* hidden */ } });
  R.ro.observe(box);
  // leaving the Remote page ends the session (a forgotten shell is a risk, and the socket costs the node a PTY)
  R.watch = setInterval(() => { if (!location.hash.startsWith("#/remote") || !S.unlocked) closeTerminal(); }, 1000);
}

/* ---------- sections ---------- */
function remoteStatusHtml(n, ov) {
  const me = n.node === ov.controller_node;
  const head = `<div class="card-head"><h2>Node ${esc(n.node)} ${me ? tag("runs the controller", "info") : ""} ${n.reachable ? tag("online", "good") : tag("not reachable", "bad")}</h2>
    <button class="btn sm" data-act="reach">Check reachability</button></div>`;
  let body;
  if (n.reachable) {
    const oob = [n.wake_configured ? "Wake-on-LAN" : null, n.plug_configured ? "smart plug" : null].filter(Boolean);
    body = `<div class="kv"><b>Host</b><span>${esc(n.hostname)}</span><b>Up for</b><span>${esc(remoteDur(n.uptime_s))}</span>
      <b>Kernel</b><span class="mono">${esc(n.kernel)}</span><b>TwinSpark</b><span>${esc(n.version)}</span>
      <b>Privileged helper</b><span>${n.privd ? tag("running", "good") : tag("missing", "bad") + ` <span class="small muted">power, boot and Wake-on-LAN controls need it: sudo systemctl start twinspark-privd</span>`}</span>
      <b>Terminal service</b><span>${n.terminal_service ? tag("running", "good") : (remoteOn(n, "terminal") ? tag("not running", "bad") + ` <span class="small muted">sudo systemctl enable --now twinspark-terminal</span>` : tag("off", "muted"))}</span>
      <b>Out-of-band power</b><span>${esc(oob.join(" · ") || "none configured")}</span></div>`;
    if ((n.power_pending || []).length) body += `<div class="callout warn" style="margin-top:12px"><div><b>${esc(n.power_pending.join(", "))} is scheduled.</b> It will happen in a moment — use Cancel below to stop it.</div></div>`;
    if (n.policy && n.policy.error) body += `<div class="callout warn" style="margin-top:12px"><div><b>The remote-management policy on this node is ignored:</b> ${esc(n.policy.error)}. Everything is off until it is fixed.</div></div>`;
  } else {
    body = `<div class="callout bad"><div><b>No answer from node ${esc(n.node)}.</b> ${esc(n.error || "")}</div></div>`;
  }
  const reach = R.reach && R.reach.node === n.node ? remoteReachHtml(R.reach) : "";
  const feats = n.reachable ? `<div class="section-title" style="margin-top:16px">What is switched on</div><div class="list">${REMOTE_FEATURES.map(([f, label, help]) => {
    const on = remoteOn(n, f);
    return `<div class="item"><div class="grow"><b>${esc(label)}</b> ${on ? tag("on", "good") : tag("off", "muted")}<div class="small muted">${esc(help)}</div>
      ${on ? "" : `<div class="small muted" style="margin-top:4px">Turn it on at the node: <span class="mono">${esc(remoteCmd(f))}</span></div>`}</div></div>`;
  }).join("")}</div>` : "";
  return `<div class="card">${head}${body}${reach}${feats}</div>`;
}
function remoteReachHtml(r) {
  const cls = r.verdict === "ok" ? "good" : r.verdict === "host_down" ? "bad" : "warn";
  return `<div class="callout ${cls}" style="margin-top:12px"><div><b>${esc(r.summary)}</b>
    <div class="small muted">agent port ${r.agent_port_open ? "open" : "closed"} · SSH ${r.ssh_port_open ? "open" : "closed"} · terminal port ${r.terminal_port_open ? "open" : "closed"}</div>
    ${r.steps.length ? `<ol class="small" style="margin:8px 0 0 18px;padding:0">${r.steps.map(s => `<li>${esc(s)}</li>`).join("")}</ol>` : ""}</div></div>`;
}
function remoteTerminalHtml(n) {
  const on = remoteOn(n, "terminal");
  return `<div class="card"><div class="card-head"><h2>Terminal</h2>
    <div class="row"><button class="btn primary sm" data-act="term-open" ${on && n.terminal_service ? "" : "disabled"}>Open terminal</button><button class="btn sm hidden" data-act="term-close">Disconnect</button></div></div>
    ${on ? (n.terminal_service ? "" : `<div class="callout warn"><div>The terminal is switched on but its service is not running. On the node: <span class="mono">sudo systemctl enable --now twinspark-terminal</span></div></div>`)
      : `<div class="callout info"><div>The terminal is off on node ${esc(n.node)}. It is a shell as the TwinSpark user, recorded on the node. To allow it, run on that node: <span class="mono">${esc(remoteCmd("terminal"))}</span></div></div>`}
    <p id="r-termstate" class="small muted" role="status"></p>
    <div id="r-termwrap" class="hidden"><div id="r-termbox" class="term-box"></div>
      <div class="row term-keys" aria-label="Extra keys for touch screens">${Object.keys(TERM_KEYS).map(k => `<button class="btn sm" type="button" data-act="term-key" data-key="${esc(k)}">${esc(k)}</button>`).join("")}</div></div>
    <p class="small muted">Leaving this page ends the session. For work that must survive a dropped connection start <span class="mono">tmux</span> first. Everything typed is recorded on the node (Recordings below).</p></div>`;
}
function remoteLogsHtml(node) {
  return `<div class="card"><div class="card-head"><h2>Logs</h2>
    <div class="row"><select id="r-src" aria-label="Log source" style="width:auto">${REMOTE_SOURCES.map(s => `<option ${s === (S.filters.rsrc || "agent") ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>
    <select id="r-lines" aria-label="Lines" style="width:auto">${[100, 300, 1000].map(n => `<option ${n === 300 ? "selected" : ""}>${n}</option>`).join("")}</select>
    <input id="r-grep" placeholder="filter…" aria-label="Filter" style="width:150px" maxlength="100"/>
    <label class="check"><input type="checkbox" id="r-auto"/> auto-refresh</label>
    <button class="btn sm" data-act="logs">Load</button></div></div>
    <div id="r-logbody"><div class="small muted">Pick a source and press Load. “kernel” and “previous-boot” are the first places to look after a crash.</div></div></div>`;
}
function remotePowerHtml(n, ov) {
  const dis = (ok) => ok ? "" : "disabled";
  const need = (f) => remoteOn(n, f) ? "" : `<div class="small muted">Off on this node: <span class="mono">${esc(remoteCmd(f))}</span></div>`;
  const plug = n.plug_configured ? `<div class="item"><div class="grow"><b>Smart plug</b> ${(n.plug_actions || []).map(a => tag(a, "muted")).join(" ")}
      <div class="small muted">Works when the node is hung or off. Off/Cycle cut power without a shutdown.</div></div>
      <div class="row">${(n.plug_actions || []).includes("on") ? `<button class="btn sm" data-act="plug-on">Switch on</button>` : ""}
      ${(n.plug_actions || []).includes("off") ? `<button class="btn sm danger" data-act="plug-off">Cut power</button>` : ""}
      <button class="btn sm danger" data-act="plug-cycle">Power-cycle</button></div></div>`
    : `<div class="item"><div class="grow"><b>Smart plug</b> ${tag("not configured", "muted")}<div class="small muted">Add nodes.${esc(n.node)}.plug to controller.yaml (${REMOTE_DOC}) to power-cycle a hung node.</div></div></div>`;
  const wake = n.wake_configured && n.node !== ov.controller_node ? `<div class="item"><div class="grow"><b>Wake-on-LAN</b> ${tag(n.mac || "", "muted")}
      <div class="small muted">Sends a magic packet from this machine. It cannot be acknowledged; if the node does not appear in a minute or two, it is not working on that port.</div></div>
      <button class="btn sm" data-act="wake">Send packet</button></div>`
    : `<div class="item"><div class="grow"><b>Wake-on-LAN</b> ${tag(n.node === ov.controller_node ? "this node runs the controller" : "not configured", "muted")}
      <div class="small muted">${n.node === ov.controller_node ? "Wake node A from node B or your laptop (tsm wake)." : `Add nodes.${esc(n.node)}.wake (mac, broadcast) to controller.yaml.`}</div></div></div>`;
  return `<div class="card"><div class="card-head"><h2>Power &amp; recovery</h2></div><div class="list">
    <div class="item"><div class="grow"><b>Reboot</b><div class="small muted">Clean restart after a short delay. Stops the running model.</div>${need("reboot")}</div>
      <div class="row"><button class="btn sm" data-act="reboot" ${dis(n.reachable && remoteOn(n, "reboot") && n.privd)}>Reboot…</button></div></div>
    <div class="item"><div class="grow"><b>Power off</b><div class="small muted">Shuts down. You will need Wake-on-LAN, the smart plug or the power button to bring it back.</div>${need("poweroff")}</div>
      <div class="row"><button class="btn sm danger" data-act="poweroff" ${dis(n.reachable && remoteOn(n, "poweroff") && n.privd)}>Power off…</button></div></div>
    <div class="item"><div class="grow"><b>Cancel pending reboot / power-off</b></div>
      <div class="row"><button class="btn sm" data-act="cancel" ${dis(n.reachable && (n.power_pending || []).length > 0)}>Cancel</button></div></div>
    <div class="item"><div class="grow"><b>Boot once from network / USB</b><div class="small muted">Rescue boot: the next start only. Needs a PXE server — see “tsm netboot” in ${REMOTE_DOC}.</div>${need("boot_next")}</div>
      <div class="row"><button class="btn sm" data-act="boot" ${dis(n.reachable && remoteOn(n, "boot_next") && n.privd)}>Choose…</button></div></div>
    <div class="item"><div class="grow"><b>Wake-on-LAN on the node</b><div class="small muted">Lets the node be woken by a magic packet. Test it once while you can reach the machine.</div>${need("wol")}</div>
      <div class="row"><button class="btn sm" data-act="wol" ${dis(n.reachable && remoteOn(n, "wol") && n.privd)}>Set…</button></div></div>
    ${wake}${plug}</div></div>`;
}
function remoteSupportHtml(n) {
  return `<div class="card"><div class="card-head"><h2>Support bundle &amp; recordings</h2>
    <div class="row"><button class="btn sm" data-act="bundle" ${n.reachable ? "" : "disabled"}>Download support bundle</button><button class="btn sm" data-act="recordings" ${remoteOn(n, "terminal") || n.terminal_service ? "" : "disabled"}>Terminal recordings</button></div></div>
    <p class="small muted">The bundle is one .tar.gz with logs, versions, disk, network, GPU and Docker state. Secrets are masked and the vault is listed by slot name only — still, look it over before you share it.</p><div id="r-recs"></div></div>`;
}

async function remoteDraw(g, node) {
  const ov = await GET("/api/v1/remote/overview");
  if (!current(g)) return;
  R.ov = ov;
  const n = ov.nodes[node];
  if (!n) return;
  $("#r-status").innerHTML = remoteStatusHtml(n, ov);
  $("#r-power").innerHTML = remotePowerHtml(n, ov);
  const t = $("#r-term");
  // the terminal card is only rebuilt when the node's capabilities change, never while a session is open
  const key = `${remoteOn(n, "terminal")}|${n.terminal_service}`;
  if (t.dataset.key !== key && !R.ws) { t.innerHTML = remoteTerminalHtml(n); t.dataset.key = key; }
  $("#r-support").dataset.key || ($("#r-support").innerHTML = remoteSupportHtml(n), $("#r-support").dataset.key = "1");
}

async function viewRemote(node) {
  const g = S.gen;
  closeTerminal();
  const ov = await GET("/api/v1/remote/overview");
  const names = Object.keys(ov.nodes).sort();
  if (!names.length) { render(g, `<div class="view">${empty("No nodes configured", "Run tsm setup first.")}</div>`); return; }
  node = node && ov.nodes[node] ? node : (names.find(x => x !== ov.controller_node) || names[0]);
  R.node = node; R.reach = null;
  const tabs = names.map(x => `<a href="#/remote/${esc(x)}" class="${x === node ? "active" : ""}">Node ${esc(x)}${x === ov.controller_node ? " (controller)" : ""}</a>`).join("");
  render(g, `<div class="view"><div class="view-head"><div><h1>Remote</h1>
    <p>Fix a headless Spark without a monitor: shell, logs, power, recovery. Features are switched on per node with <span class="mono">sudo tsm remote enable …</span>.</p></div></div>
    <div class="tabs">${tabs}</div>
    <div id="r-status"></div><div id="r-term" style="margin-top:16px"></div><div id="r-logs" style="margin-top:16px">${remoteLogsHtml(node)}</div>
    <div id="r-power" style="margin-top:16px"></div><div id="r-support" style="margin-top:16px"></div></div>`);
  await remoteDraw(g, node);
  if (!ov.nodes[node].reachable) await remoteReach(g, node);
  every(() => remoteDraw(g, node).catch(() => { }), 10000);

  const loadLogs = async () => {
    const src = $("#r-src").value, lines = parseInt($("#r-lines").value, 10), grep = $("#r-grep").value.trim();
    S.filters.rsrc = src;
    const body = $("#r-logbody");
    try {
      const r = await GET(`/api/v1/remote/${enc(node)}/logs` + qs({ source: src, lines, grep }));
      if (!current(g)) return;
      const stick = !body.firstElementChild || body.querySelector("pre") === null || (() => { const p = body.querySelector("pre"); return p.scrollHeight - p.scrollTop - p.clientHeight < 40; })();
      body.innerHTML = (r.note ? `<div class="callout warn"><div>${esc(r.note)}</div></div>` : "") +
        codeBlock(r.lines.length ? r.lines.join("\n") : "(no lines)", "log") + `<div class="small muted">${num(r.count)} lines · via ${esc(r.via)}</div>`;
      const p = body.querySelector("pre"); if (p && stick) p.scrollTop = p.scrollHeight;
    } catch (e) { if (current(g)) body.innerHTML = `<div class="callout warn"><div>${esc(e.message)}</div></div>`; }
  };
  $("#r-auto").onchange = (e) => { if (e.target.checked) { loadLogs(); every(() => { if ($("#r-auto")?.checked) loadLogs(); }, 5000); } };

  const phrase = (verb) => `${verb} ${node}`;
  const power = async (action) => {
    const n = R.ov.nodes[node];
    const warn = [];
    if (node === R.ov.controller_node) warn.push(`Node ${esc(node)} runs the controller: this page, the CLI and the inference gateway go away until it is back.`);
    if (R.ov.active) warn.push(`The active model <b>${esc(R.ov.active)}</b> will stop.`);
    const ok = await confirmBox(`${action === "reboot" ? "Reboot" : "Power off"} node ${node}?`,
      `<p>${action === "reboot" ? "The node restarts in a few seconds." : "The node shuts down in a few seconds. You will need Wake-on-LAN, the smart plug or the power button to start it again."}</p>${warn.map(w => `<p class="small">${w}</p>`).join("")}`,
      { label: action === "reboot" ? "Reboot" : "Power off", danger: true, typed: phrase(action.toUpperCase()) });
    if (!ok) return;
    let force = false;
    try { await POST(`/api/v1/remote/${enc(node)}/power`, { action, confirm: phrase(action.toUpperCase()), delay_s: 5 }); }
    catch (e) {
      if (e.status === 409 && /busy/.test(e.message) && await confirmBox("The cluster is busy", `<p>${esc(e.message)}</p><p>Do it anyway?</p>`, { label: "Do it anyway", danger: true })) force = true;
      else throw e;
    }
    if (force) await POST(`/api/v1/remote/${enc(node)}/power`, { action, confirm: phrase(action.toUpperCase()), delay_s: 5, force: true });
    toast(`${action} scheduled on node ${node}`);
    remoteDraw(g, node).catch(() => { });
  };
  const plugAct = async (action) => {
    const confirm = action === "on" ? "" : phrase("CUT POWER");
    if (action !== "on" && !await confirmBox(`Cut power to node ${node}?`, `<p>The machine switches off <b>without a shutdown</b>. Files being written can be lost. Use this when the node does not answer at all.</p>`, { label: action === "cycle" ? "Power-cycle" : "Cut power", danger: true, typed: confirm })) return;
    try { await POST(`/api/v1/remote/${enc(node)}/plug`, { action, confirm }); }
    catch (e) {
      if (e.status === 409 && /busy/.test(e.message) && await confirmBox("The cluster is busy", `<p>${esc(e.message)}</p><p>Do it anyway?</p>`, { label: "Do it anyway", danger: true })) await POST(`/api/v1/remote/${enc(node)}/plug`, { action, confirm, force: true });
      else throw e;
    }
    toast(`plug: ${action}`);
  };
  onAct({
    "term-open": () => openTerminal(node).catch(e => { fail(e); }),
    "term-close": () => closeTerminal("disconnected"),
    "term-key": (el) => { const seq = TERM_KEYS[el.dataset.key]; if (seq && R.ws && R.ws.readyState === 1) R.ws.send(new TextEncoder().encode(seq)); if (R.term) R.term.focus(); },
    reach: () => remoteReach(g, node, true),
    logs: loadLogs,
    reboot: () => power("reboot"),
    poweroff: () => power("poweroff"),
    cancel: async () => { const r = await POST(`/api/v1/remote/${enc(node)}/power/cancel`); toast(r.cancelled.length ? `cancelled: ${r.cancelled.join(", ")}` : "nothing was pending"); await remoteDraw(g, node); },
    wake: async () => { const r = await POST(`/api/v1/remote/${enc(node)}/wake`); toast(`magic packet sent to ${r.broadcast}`); },
    "plug-on": () => plugAct("on"), "plug-off": () => plugAct("off"), "plug-cycle": () => plugAct("cycle"),
    boot: () => bootDialog(node),
    wol: () => wolDialog(node),
    bundle: (btn) => downloadBundle(node, btn),
    recordings: () => recordingsList(g, node),
    "rec-get": (el) => downloadRecording(node, el.dataset.name),
  });
}

async function remoteReach(g, node, announce = false) {
  const r = await GET(`/api/v1/remote/${enc(node)}/reach`);
  if (!current(g)) return;
  R.reach = r;
  if (R.ov) $("#r-status").innerHTML = remoteStatusHtml(R.ov.nodes[node], R.ov);
  if (announce) toast(r.verdict === "ok" ? `node ${node} answers` : r.summary, r.verdict === "ok" ? "" : "bad");
}

async function bootDialog(node) {
  const b = await GET(`/api/v1/remote/${enc(node)}/boot`);
  if (!b.available) { await modal({ title: "Boot entries", body: `<p>${esc(b.reason || "not available")}</p>` }); return; }
  const entries = b.entries.map(e => [e.num, `${e.num} · ${e.label} · ${e.kind}${e.num === b.current ? " (booted from now)" : ""}`]);
  const f = await formBox(`Boot node ${node} once from…`, [{
    id: "target", label: "Next boot only", type: "select", value: "network",
    options: [["network", "Network (the firmware's IPv4 PXE entry)"], ...entries, ["clear", "Nothing — clear an earlier choice"]],
    help: `Currently set for the next boot: ${b.next || "nothing"}`,
  }], { label: "Apply", intro: "The normal boot order is not changed. After choosing, reboot the node (Reboot above)." });
  if (!f) return;
  if (f.target === "clear") { await POST(`/api/v1/remote/${enc(node)}/boot/clear`); toast("next-boot choice cleared"); return; }
  const r = await POST(`/api/v1/remote/${enc(node)}/boot/next`, { target: f.target });
  toast(`next boot: ${r.next} ${r.label || ""} — now reboot the node`);
}
async function wolDialog(node) {
  const w = R.ov.nodes[node];
  const ifs = (w.interfaces || []).filter(i => i.wol_supported);
  if (!ifs.length) { await modal({ title: "Wake-on-LAN", body: `<p>No network port on node ${esc(node)} reports Wake-on-LAN support (or ethtool is missing: <span class="mono">sudo apt install ethtool</span>).</p>` }); return; }
  const f = await formBox(`Wake-on-LAN on node ${node}`, [
    { id: "iface", label: "Port", type: "select", options: ifs.map(i => [i.name, `${i.name} · ${i.mac || "?"} · now ${i.wol || "?"}`]), value: ifs[0].name },
    { id: "mode", label: "Wake on", type: "select", options: [["g", "magic packet (on)"], ["d", "off"]], value: "g" },
  ], { label: "Apply", intro: "The setting lasts until the next reboot. To keep it, add wakeonlan: true for that port in netplan (docs/remote-management.md)." });
  if (!f) return;
  const r = await POST(`/api/v1/remote/${enc(node)}/wol/set`, { iface: f.iface, mode: f.mode });
  toast(`${r.interface}: wake-on = ${r.mode}`);
}
async function fetchBlob(path, btn, name) {
  await busy(btn, async () => {
    const r = await fetch(path, { headers: S.key ? { "x-api-key": S.key } : {} });
    if (!r.ok) { const d = await r.json().catch(() => null); throw new Error((d && d.detail) || "HTTP " + r.status); }
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name || (r.headers.get("content-disposition") || "").replace(/.*filename="?([^";]+)"?.*/, "$1") || "download";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  });
}
async function downloadBundle(node, btn) { await fetchBlob(`/api/v1/remote/${enc(node)}/bundle`, btn); toast("support bundle downloaded"); }
async function downloadRecording(node, name) {
  const r = await GET(`/api/v1/remote/${enc(node)}/recordings/${enc(name)}`);
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([r.cast], { type: "text/plain" }));
  a.download = name.endsWith(".cast") ? name : name + ".cast";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}
async function recordingsList(g, node) {
  const el = $("#r-recs");
  const r = await GET(`/api/v1/remote/${enc(node)}/recordings`);
  if (!current(g)) return;
  el.innerHTML = r.recordings.length
    ? `<div class="list" style="margin-top:12px">${r.recordings.map(x => `<div class="item"><div class="grow"><span class="mono">${esc(x.name)}</span> <span class="small muted">${esc(bytes(x.size))} · ${esc(when(x.modified))}</span></div><button class="btn sm" data-act="rec-get" data-name="${esc(x.name)}">Download</button></div>`).join("")}</div>
       <p class="small muted">Play with <span class="mono">asciinema play file.cast</span>.</p>`
    : `<p class="small muted" style="margin-top:12px">No recordings yet.</p>`;
}
