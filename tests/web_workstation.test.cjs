/* The GUI in workstation-client mode (served by `tsm client`). Run with node --test tests/web_workstation.test.cjs. */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const JS = path.join(__dirname, "../twinspark/web/src/js");

function page({ hash = "", responses = {} } = {}) {
  const elements = new Map(), storage = new Map(), requests = [];
  const element = id => {
    if (!elements.has(id)) {
      const classes = new Set(id === "#client-states" ? ["hidden"] : []);
      elements.set(id, {
        value: "", textContent: "", innerHTML: "", disabled: false, placeholder: "", className: "",
        classList: { add: c => classes.add(c), remove: c => classes.delete(c), toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)), contains: c => classes.has(c) },
        focus() {}, classes,
      });
    }
    return elements.get(id);
  };
  const loc = { hash, pathname: "/", search: "" };
  const history = { replaceState: (_s, _t, url) => { loc.hash = url.slice(url.indexOf("#")); } };
  // fetch: responses[path] = {status, body, headers}; a function gets the request
  const fetch = async (url, opts = {}) => {
    requests.push({ url, method: opts.method || "GET", headers: opts.headers || {}, body: opts.body });
    let r = responses[url.split("?")[0]] ?? { status: 404, body: { detail: "Not Found" } };
    if (typeof r === "function") r = r(url, opts);
    const headers = new Map(Object.entries(r.headers || {}).map(([k, v]) => [k.toLowerCase(), v]));
    return {
      status: r.status ?? 200, ok: (r.status ?? 200) < 400,
      headers: { get: k => headers.get(k.toLowerCase()) ?? null },
      json: async () => r.body, text: async () => JSON.stringify(r.body),
    };
  };
  const context = vm.createContext({
    console, URL, URLSearchParams, TextEncoder, clearTimeout, clearInterval, fetch,
    // timers the page starts (the status ping) must not keep the test process alive
    setTimeout: (fn, ms) => setTimeout(fn, ms).unref(), setInterval: (fn, ms) => setInterval(fn, ms).unref(),
    location: loc, history,
    sessionStorage: { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k) },
    window: { addEventListener() {} },
    document: { addEventListener() {}, querySelector: element, title: "" },
  });
  for (const file of ["app.js", "start.js", "remote.js", "bench.js", "keys.js", "playground.js", "client.js"]) {
    vm.runInContext(fs.readFileSync(path.join(JS, file), "utf8"), context, { filename: file });
  }
  return { context, element, storage, requests, loc, run: code => vm.runInContext(code, context) };
}

const CONNECTED = {
  client_version: "0.6.0", phase: "connected", need: null, message: null,
  connection: { id: "home", name: "home", mode: "ssh", host: "spark-a.lan", ssh_user: "spark", ssh_port: 22, local_gateway_port: 18800 },
  states: { client_connected: true, agents_compatible: true, model_ready: true },
  transport: { state: "up", forwards: [{ label: "api", local: "127.0.0.1:18801", remote_port: 8443 }, { label: "gateway", local: "127.0.0.1:18800", remote_port: 8000 }] },
  node: { version: "0.6.0", compat: { state: "ok" }, agents: { A: { version: "0.6.0", ok: true, runtime_mode: "live" }, B: { version: "0.6.0", ok: true, runtime_mode: "live" } },
    model: { ready: true, active: { profile: "qwen" }, aliases: ["default"], dry_run: false } },
  endpoint: { base_url: "http://127.0.0.1:18800/v1", manager_url: "http://127.0.0.1:18801", has_inference_key: true },
  credentials: { backend: "keyring", management_key: true, inference_key: true },
};
const IDLE = { ...CONNECTED, phase: "idle", connection: null, states: { client_connected: false, agents_compatible: false, model_ready: false }, transport: { state: "stopped" }, node: {}, endpoint: {} };

test("the session key arrives in the address fragment, is kept for the tab and leaves the address bar", () => {
  const key = "AbCdEfGhIjKlMnOpQrStUvWx-_12";
  const p = page({ hash: "#k=" + key });
  assert.equal(p.run("S.key"), key);
  assert.equal(p.storage.get("tsm_key"), key);
  assert.equal(p.loc.hash, "#/");
  assert.equal(p.run("clientKeyFromFragment('#k=short')"), "");
  assert.equal(p.run("clientKeyFromFragment('#/profiles')"), "");
  assert.equal(p.run("clientKeyFromFragment('#k=" + key + "&x=<script>')"), "");
});

test("client mode is detected by asking the client; node A answers 404 and nothing changes", async () => {
  const node = page({ responses: {} });
  await node.run("clientBoot()");
  assert.equal(node.run("S.client"), null);
  assert.equal(node.run("NAV.some(n => n[0] === 'connection')"), false);

  const p = page({ hash: "", responses: {
    "/api/v1/client/info": { body: { client: true, version: "0.6.0", connections: [], credential_backend: "file", home: "/h", ssh: { path: "/usr/bin/ssh" } } },
    "/api/v1/client/status": { body: IDLE },
  } });
  p.loc.hash = "#/";
  await p.run("clientBoot()");
  assert.equal(p.run("NAV[0][0]"), "connection");
  assert.equal(p.loc.hash, "#/connection");          // not connected: start on the Connection page
  assert.equal(p.element("#client-states").classes.has("hidden"), false);
  await p.run("clientBoot()");
  assert.equal(p.run("NAV.filter(n => n[0] === 'connection').length"), 1);
});

test("a 401 from the client says the client session ended, not that node A's key is wrong", async () => {
  const p = page({ responses: { "/api/v1/status": { status: 401, body: { detail: "invalid client session key" }, headers: { "X-TwinSpark-Client": "session" } } } });
  p.run("S.key = 'old-key-from-an-earlier-launch'");
  await assert.rejects(p.run("GET('/api/v1/status')"), e => e.client === "session");
  assert.match(p.element("#gate-err").textContent, /client session ended/);
  assert.equal(p.element("#gate-label").textContent, "Client session key");
  assert.equal(p.run("S.key"), "");
  const node = page({ responses: { "/api/v1/status": { status: 401, body: { detail: "bad key" } } } });
  node.run("S.key = 'wrong'");
  await assert.rejects(node.run("GET('/api/v1/status')"), e => e.status === 401 && e.client === "");
  assert.match(node.element("#gate-err").textContent, /Session key rejected/);
});

test("unlocking with the client's key works while node A is not connected yet", async () => {
  const p = page({ responses: {
    "/api/v1/status": { status: 503, body: { detail: "not connected to the Sparks" }, headers: { "x-twinspark-client": "transport" } },
    "/api/v1/client/info": { body: { client: true, version: "0.6.0", connections: [] } },
    "/api/v1/client/status": { body: IDLE },
  } });
  p.run("route = async () => {}");
  await p.run("unlock('the-client-session-key-123456')");
  assert.equal(p.storage.get("tsm_key"), "the-client-session-key-123456");
  assert.equal(p.run("S.unlocked"), true);
});

test("a node page that fails for transport reasons explains the connection instead of an error", async () => {
  const p = page({ responses: { "/api/v1/profiles": { status: 503, body: { detail: "the connection to <spark> is reconnecting" }, headers: { "x-twinspark-client": "transport" } } } });
  const err = await p.run("GET('/api/v1/profiles').catch(e => e)");
  assert.equal(err.client, "transport");
  const html = p.run("clientErrorHtml(" + JSON.stringify({ client: "transport", message: err.message }) + ")");
  assert.match(html, /Not connected to the Sparks/);
  assert.match(html, /&lt;spark&gt;/);
  assert.match(html, /#\/connection/);
  assert.match(p.run("clientErrorHtml({client:'credentials', message:'refused'})"), /refused the stored key/);
});

test("the three states are separate facts", () => {
  const p = page();
  const states = s => JSON.parse(p.run(`JSON.stringify(clientStates(${JSON.stringify(s)}).map(x => x[1]))`));
  assert.deepEqual(states(CONNECTED), [true, true, true]);
  assert.deepEqual(states(IDLE), [false, false, false]);
  const noModel = { ...CONNECTED, states: { client_connected: true, agents_compatible: true, model_ready: false }, node: { ...CONNECTED.node, model: { ready: false, active: null } } };
  assert.deepEqual(states(noModel), [true, true, false]);
  assert.match(p.run(`clientStatesHtml(${JSON.stringify(noModel)})`), /No model is active/);
  const oldAgent = { ...CONNECTED, states: { ...CONNECTED.states, agents_compatible: false }, node: { ...CONNECTED.node, agents: { A: { version: "0.6.0", ok: true }, B: { version: "0.5.1", ok: false } } } };
  assert.match(p.run(`clientStatesHtml(${JSON.stringify(oldAgent)})`), /node B: 0\.5\.1 — version differs/);
  assert.equal(p.run(`clientPhase(${JSON.stringify({ ...CONNECTED, states: { ...CONNECTED.states, client_connected: false }, transport: { state: "reconnecting" } })}).text`), "reconnecting…");
  assert.equal(p.run("clientPhase(null).text"), "client stopped");
  assert.equal(p.run(`clientPhase(${JSON.stringify({ ...IDLE, phase: "needs-action", need: "host-key" })}).text`), "action needed");
});

test("the connection page escapes what node A and the user typed, and offers the next step", () => {
  const p = page();
  const info = { version: "0.6.0", home: "/home/<me>", credential_backend: "file", ssh: { path: "/usr/bin/ssh" },
    connections: [{ id: "x", name: "<b>lab</b>", mode: "ssh", host: "spark\"a", ssh_user: "u", ssh_port: 2222 }] };
  const needs = { ...IDLE, phase: "needs-action", need: "host-key", message: "<check>", connection: info.connections[0] };
  const html = p.run(`connectionHtml(${JSON.stringify(info)}, ${JSON.stringify(needs)}, null)`);
  assert.doesNotMatch(html, /<b>lab<\/b>|<check>|<me>/);
  assert.match(html, /&lt;b&gt;lab&lt;\/b&gt;/);
  assert.match(html, /spark&quot;a:2222/);
  assert.match(html, /data-act="conn-resolve"[^>]*data-need="host-key"/);
  assert.match(html, /Check host key/);
  const auth = p.run(`connectionHtml(${JSON.stringify(info)}, ${JSON.stringify({ ...needs, need: "auth" })}, null)`);
  assert.match(auth, /Install key with password/);
  const connected = p.run(`connectionHtml(${JSON.stringify({ ...info, connections: [CONNECTED.connection] })}, ${JSON.stringify(CONNECTED)}, null)`);
  assert.match(connected, /http:\/\/127\.0\.0\.1:18800\/v1/);
  assert.match(connected, /Copy inference key/);
  assert.doesNotMatch(connected, /data-act="conn-connect" data-id="home"/);   // already connected to it
  const empty = p.run(`connectionHtml(${JSON.stringify({ ...info, connections: [] })}, ${JSON.stringify(IDLE)}, null)`);
  assert.match(empty, /No Spark yet/);
  const probe = p.run(`connectionHtml(${JSON.stringify(info)}, ${JSON.stringify(IDLE)}, {name:'lab', error:null, checks:[{id:'installed', title:'TwinSpark is installed', status:'todo', detail:'<x>', fix:{command:'curl … | sh'}}]})`);
  assert.match(probe, /&lt;x&gt;/);
  assert.match(probe, /start-mark todo/);
});

test("the Hermes card only offers what the state allows and escapes the app's own settings", () => {
  const p = page();
  const data = { endpoint: { base_url: "http://127.0.0.1:18800/v1", alias: "default", context_length: 131072 },
    status: { state: { installed: true, configured: false, reachable: true, compatible: null, running: false, launcher_ok: true, phase: "not-configured" },
      detection: { host: "this computer", version: "0.9", home: "/h/.hermes", profiles: ["default", "<work>"] },
      config: { model: { provider: "openrouter", default: "<model>" } }, notes: ["Hermes does not point at this endpoint yet."], logs: [], error_logs: [] } };
  const html = p.run(`hermesCardHtml({host:'workstation', profile:''}, ${JSON.stringify(data)})`);
  assert.match(html, /not connected/);
  assert.match(html, /&lt;work&gt;/);
  assert.match(html, /&lt;model&gt;/);
  assert.doesNotMatch(html, /<work>|<model>/);
  const disabled = act => new RegExp(`data-hact="${act}" disabled`).test(html);
  assert.equal(disabled("preview"), false);
  assert.equal(disabled("validate"), true);          // not configured yet
  assert.equal(disabled("stop"), true);              // not running
  assert.equal(disabled("launch-gateway"), false);
  const missing = p.run(`hermesCardHtml({host:'node-a', profile:''}, {status:{state:{installed:false, phase:'not-installed'}, detection:{host:'node A', profiles:[]}, config:{}, notes:[]}})`);
  assert.match(missing, /Install Hermes on node A first/);
  assert.match(missing, /data-hact="preview" disabled/);
  const preview = p.run(`previewHtml({files:[{label:'config.yaml', path:'/h/config.yaml', changed:true, diff:'+ base_url: <x>'}], warnings:['<w>'], blocked:[], notes:[]})`);
  assert.match(preview, /&lt;x&gt;/);
  assert.match(preview, /&lt;w&gt;/);
});

test("connecting walks through the host key, then connects", async () => {
  let connects = 0;
  const p = page({ responses: {
    "/api/v1/client/connections/home/connect": () => ({ body: ++connects === 1 ? { ...IDLE, phase: "needs-action", need: "host-key", connection: CONNECTED.connection } : CONNECTED }),
  } });
  p.run("var asked = []; hostKeyFlow = async c => { asked.push(c.id); return true; }; toast = () => {};");
  const st = await p.run(`connectFlow(${JSON.stringify(CONNECTED.connection)})`);
  assert.equal(st.phase, "connected");
  assert.deepEqual(JSON.parse(p.run("JSON.stringify(asked)")), ["home"]);
  assert.equal(p.requests.filter(r => r.url.includes("/connect?wait=true")).length, 2);
  // the user cancels: no endless loop
  const q = page({ responses: { "/api/v1/client/connections/home/connect": { body: { ...IDLE, phase: "needs-action", need: "auth", connection: CONNECTED.connection } } } });
  q.run("passwordFlow = async () => false;");
  const st2 = await q.run(`connectFlow(${JSON.stringify(CONNECTED.connection)})`);
  assert.equal(st2.need, "auth");
  assert.equal(q.requests.length, 1);
});

test("HTML loads the client module last and the GUI has its styles", () => {
  const source = path.join(__dirname, "../twinspark/web/src");
  const html = fs.readFileSync(path.join(source, "index.html"), "utf8");
  const scripts = [...html.matchAll(/<script src="([^"]+)"/g)].map(m => m[1]);
  assert.equal(scripts[scripts.length - 1], "js/client.js");
  assert.match(html, /id="client-states"/);
  assert.match(fs.readFileSync(path.join(source, "css/style.css"), "utf8"), /\.client-states/);
});

test("a switch with running requests asks first; the answer becomes the drain policy", async () => {
  const busy = { routes: [{ alias: "default", inflight: 2, oldest_inflight_s: 95 }], drain: { timeout_s: 30 } };
  const p = page({ responses: { "/api/v1/status": { body: busy } } });
  const asked = [];
  p.context.modalAnswer = "wait";
  p.run("modal = async (o) => { asked.push(o); return { value: modalAnswer }; }");
  p.context.asked = asked;
  const choice = async (pg) => JSON.parse(await pg.run("drainChoice().then(JSON.stringify)"));
  assert.deepEqual(await choice(p), { drain_policy: "wait" });
  assert.match(asked[0].title, /2 requests are still running/);
  assert.match(asked[0].body, /longest for 95 s/);
  p.context.modalAnswer = "force";
  assert.deepEqual(await choice(p), { drain_policy: "force", drain_timeout_s: 0 });
  p.context.modalAnswer = "abort";
  assert.deepEqual(await choice(p), {});
  p.context.modalAnswer = null;
  assert.equal(await choice(p), null);                              // cancelled: no switch
  const idle = page({ responses: { "/api/v1/status": { body: { routes: [{ alias: "default", inflight: 0 }] } } } });
  assert.deepEqual(await choice(idle), {});                         // nothing running: no question
  assert.equal(p.run("activationBody({drain_policy:'wait'}).drain_policy"), "wait");
});
