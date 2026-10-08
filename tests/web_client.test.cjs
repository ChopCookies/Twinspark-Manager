/* Run with node --test tests/web_client.test.cjs. No browser dependencies. */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

test("monitoring distinguishes unavailable readings and stale data from zero", () => {
  const c = client();
  const missing = c.run("systemNode('A', {}, [], 10)");
  assert.match(missing, /waiting for agent/);
  assert.match(missing, /Unavailable on this device/);
  assert.doesNotMatch(missing, /0 GiB|0<small>%/);
  const zero = c.run("systemNode('A', {cpu_pct:0, gpu:{power_w:0}, stale:true, error:'<offline>'}, [], 10)");
  assert.match(zero, /stale \/ disconnected/);
  assert.match(zero, /0<small>%/);
  assert.match(zero, /&lt;offline&gt;/);
});

test("maintenance failure offers recovery and escapes node errors", () => {
  const c = client();
  const result = c.run("maintenanceProgress({state:'failed', phase:'update', order:['B','A'], index:0, error:'<unsafe text>'})");
  assert.match(result, /Recheck progress/);
  assert.match(result, /Release after repair/);
  assert.match(result, /&lt;unsafe text&gt;/);
});

test("a hand-typed percent sign in the address bar cannot break routing", () => {
  const c = client();
  assert.equal(c.run("safeDecode('%E0%A4%A')"), "%E0%A4%A");
  assert.equal(c.run("safeDecode('my%20profile')"), "my profile");
});

function client() {
  const elements = new Map(), events = new Map(), storage = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { value: "", textContent: "", innerHTML: "", disabled: false });
    return elements.get(id);
  };
  const context = vm.createContext({
    console, URLSearchParams, TextDecoder, setTimeout, clearInterval, setInterval,
    sessionStorage: { getItem: k => storage.get(k), setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k) },
    window: { addEventListener: (name, fn) => events.set(name, fn) },
    document: { addEventListener() {}, querySelector: element },
  });
  for (const file of ["app.js", "start.js", "remote.js"]) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../twinspark/web/src/js", file), "utf8"), context, { filename: file });
  }
  return { context, element, events, storage, run: code => vm.runInContext(code, context) };
}

test("keyless sessions navigate; locked sessions ignore navigation", () => {
  const c = client();
  c.run("var navigations = 0; route = () => navigations++; S.unlocked = true;");
  c.events.get("hashchange")();
  assert.equal(c.run("navigations"), 1);
  c.run("S.unlocked = false");
  c.events.get("hashchange")();
  assert.equal(c.run("navigations"), 1);
});

test("loading a recipe file clears the URL and preserves its contents for review", async () => {
  const c = client();
  c.context.recipeBytes = new TextEncoder().encode('name: example\ncommand: vllm serve org/model');
  c.run("toast = () => {}; document.querySelector('#paste-url').value = 'https://old/recipe';");
  await c.run("loadRecipeFile({name:'recipe.yaml', size:recipeBytes.byteLength, arrayBuffer:async()=>recipeBytes.buffer})");
  assert.equal(c.element("#paste-url").value, "");
  assert.match(c.element("#paste-text").value, /vllm serve org\/model/);
  await assert.rejects(c.run("loadRecipeFile({size:512*1024+1})"), /too large/);
});

test("late recipe file reads cannot change a different view", async () => {
  const c = client();
  c.context.recipeBytes = new TextEncoder().encode("new recipe");
  await c.run("loadRecipeFile({size:10, arrayBuffer:async()=>{S.gen++; return recipeBytes.buffer;}})");
  assert.equal(c.element("#paste-text").value, "");
});

test("split composition validates distinct names before creating a snapshot", async () => {
  const c = client();
  c.run(`
    var calls = [], destination;
    api = async (method, path, body) => { calls.push({path,body}); return {name:body.name}; };
    formBox = async (title, fields, options) => {
      const values = {name:'duo',node_a:'chat',node_b:'coder',alias_a:'default',alias_b:'default'};
      try { await options.validate(values); } catch (e) { if (!e.message.includes('different')) throw e; }
      if (calls.length) throw new Error('created before validation');
      values.alias_b = 'code'; await options.validate(values); return values;
    };
    go = hash => { destination = hash; };
  `);
  await c.run("splitFlow([{name:'chat',model:'org/chat',topology:'single-a'},{name:'coder',model:'org/code',topology:'single-b'}])");
  const calls = JSON.parse(c.run("JSON.stringify(calls)"));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/api/v1/profiles/compose/split");
  assert.equal(calls[0].body.alias_b, "code");
  assert.equal(c.run("destination"), "#/profiles/duo");
});

test("preparation progress shows the simulation limit and exact-revision switch", () => {
  const c = client();
  const html = c.run("jobHtml({job_id:'prepare-1', kind:'prepare', state:'completed', steps:[], payload:{profile:'duo', revision:'r2',dry_run:true}})");
  assert.match(html, /Preparation complete/);
  assert.match(html, /real hardware still needs testing/);
  assert.match(html, /Switch to prepared revision/);
  assert.doesNotMatch(html, /<b>Serving\./);
});

test("connection errors keep the login gate closed without saving the key", async () => {
  const c = client();
  c.run("api = async () => { throw new ApiError('controller not reachable', 0); }; var booted = false; boot = () => { booted = true; };");
  await c.run("unlock('candidate-key')");
  assert.equal(c.run("booted"), false);
  assert.equal(c.run("S.key"), "");
  assert.equal(c.storage.has("tsm_key"), false);
  assert.equal(c.element("#gate-err").textContent, "controller not reachable");
});

test("recipe import saves the reviewed snapshot without fetching the URL again", async () => {
  const c = client();
  c.run(`
    var calls = [], destination;
    api = async (method, path, body) => {
      calls.push({ method, path, body });
      if (path.endsWith('import-recipe')) return { draft: { name: 'original', simple: { model: 'org/reviewed', topology: 'tp2' }, advanced: { mods: [] }, source: { ref: body.url } }, report: {} };
      return { name: body.name };
    };
    modal = async options => {
      const input = { value: 'renamed', reportValidity: () => true };
      await options.actions[1].validate({ querySelector: () => input });
      return { value: 'go' };
    };
    toast = () => {}; go = hash => { destination = hash; };
  `);
  await c.run("previewImport({url:'https://example.com/recipe.yaml'})");
  const calls = JSON.parse(c.run("JSON.stringify(calls)"));
  assert.deepEqual(calls.map(c => c.path), ["/api/v1/cookbook/import-recipe", "/api/v1/profiles"]);
  assert.equal(calls[0].body.preview, true);
  assert.equal(calls[1].body.name, "renamed");
  assert.equal(calls[1].body.simple.model, "org/reviewed");
  assert.equal(calls[1].body.source.ref, "https://example.com/recipe.yaml");
  assert.equal(c.run("destination"), "#/profiles/renamed");
});

test("automatic import prepares the reviewed snapshot with a stable request ID", async () => {
  const c = client();
  c.run(`
    var calls = [], destination;
    api = async (method, path, body) => {
      calls.push({path,body});
      if (path.endsWith('import-recipe')) return {draft:{name:'recipe',simple:{model:'org/reviewed'},advanced:{mods:[]}},report:{}};
      return {job_id:'integration-1'};
    };
    modal = async options => {
      const root = {querySelector:()=>({value:'my-recipe',reportValidity:()=>true})};
      // Repeating after an uncertain response must not start a second job.
      await options.actions[2].validate(root); await options.actions[2].validate(root);
      return {value:'prepared'};
    };
    toast = () => {}; go = hash => { destination = hash; };
  `);
  await c.run("previewImport({url:'https://example.com/recipe.yaml'})");
  const calls = JSON.parse(c.run("JSON.stringify(calls)"));
  assert.deepEqual(calls.map(x => x.path), ['/api/v1/cookbook/import-recipe','/api/v1/cookbook/integrate','/api/v1/cookbook/integrate']);
  assert.equal(calls[1].body.request_id, calls[2].body.request_id);
  assert.equal(calls[1].body.draft.simple.model, 'org/reviewed');
  assert.equal(calls[1].body.draft.name, 'my-recipe');
  assert.equal(c.run('destination'), '#/jobs/integration-1');
});

test("integration progress exposes cancellation, retry and exact prepared pins", () => {
  const c = client();
  const running = c.run("jobHtml({job_id:'i1',kind:'integration',state:'running',steps:[],payload:{profile:'duo'}})");
  assert.match(running, /pinning/);
  assert.match(running, /data-act="cancel"/);
  assert.doesNotMatch(running, /prepared-activate/);
  const failed = c.run("jobHtml({job_id:'i1',kind:'integration',state:'failed',steps:[],error:'<offline>',payload:{profile:'duo'}})");
  assert.match(failed, /Retry preparation/);
  assert.match(failed, /&lt;offline&gt;/);
  assert.doesNotMatch(failed, /Switch to prepared revision/);
  const ready = c.run("jobHtml({job_id:'i1',kind:'integration',state:'completed',steps:[],payload:{profile:'duo',dry_run:true,resolved:{model:'org/a@123',image:'image@sha256:123',secondary:{model:'org/b@456',image:'image@sha256:456'}}}})");
  assert.match(ready, /real hardware still needs testing/);
  assert.match(ready, /Switch to prepared revision/);
  assert.match(ready, /Node A/); assert.match(ready, /Node B/);
});

test("automatic preparation ignores profile responses arriving after navigation", async () => {
  const c = client();
  c.run(`
    var calls = [], destination;
    busy = (btn, fn) => fn(); toast = () => {}; go = hash => { destination = hash; };
    api = async (method, path) => { calls.push(path); S.gen++; return {draft:{name:'duo'}}; };
  `);
  await c.run("automateRecipe('duo', null)");
  assert.equal(c.run("calls.length"), 1);
  assert.equal(c.run("destination"), undefined);
});

test("recipe update review shows retained conflicts and escapes source changes", () => {
  const c = client();
  const html = c.run("recipeUpdateHtml({profile:'chat',previous_sha256:'a'.repeat(64),sha256:'b'.repeat(64),preserved:['simple.context_length'],conflicts:['simple.context_length'],changes:[{field:'image_hint',before:'old',after:'<new>'}]})");
  assert.match(html, /separate profile/);
  assert.match(html, /Both you and upstream changed/);
  assert.match(html, /Your values are retained/);
  assert.match(html, /&lt;new&gt;/);
  assert.doesNotMatch(html, /<new>/);
});

test("source update preparation uses its merged snapshot without a second fetch", async () => {
  const c = client();
  c.run(`
    var calls = [], destination;
    busy = (btn, fn) => fn(); toast = () => {}; go = hash => { destination = hash; };
    api = async (method,path,body) => {
      calls.push({path,body});
      if (path.includes('/updates')) return {updates:[{profile:'chat',status:'changed',previous_sha256:'aaa',sha256:'bbb',preserved:[],conflicts:[],changes:[],preview:{draft:{name:'chat-update',simple:{model:'org/new'},advanced:{mods:[]}},report:{}}}]};
      return {job_id:'updated-job'};
    };
    modal = async options => {
      await options.actions[2].validate({querySelector:()=>({value:'chat-update',reportValidity:()=>true})});
      return {value:'prepared'};
    };
  `);
  await c.run("checkRecipeUpdate('chat', null)");
  const calls = JSON.parse(c.run('JSON.stringify(calls)'));
  assert.deepEqual(calls.map(x => x.path), ['/api/v1/cookbook/updates?profile=chat','/api/v1/cookbook/integrate']);
  assert.equal(calls[1].body.draft.simple.model, 'org/new');
  assert.equal(c.run('destination'), '#/jobs/updated-job');
});

test("late recipe previews cannot open over a different view", async () => {
  const c = client();
  c.run("api = async () => { S.gen++; return {}; }; var opened = false; importDraft = () => { opened = true; };");
  await c.run("previewImport({text:'command: vllm serve org/model'})");
  assert.equal(c.run("opened"), false);
});

test("search combines words with the selected filter and clear restores all rows", () => {
  const c = client();
  c.run(`
    libraryToolbar('test', '', [['all', 'All'], ['tp2', 'TP2']]);
    document.querySelector('#test-filter').value = 'all';
    bindLibrary('test', [
      { name: 'glm-fast', model: 'org/glm', quantization: 'nvfp4', topology: 'tp2' },
      { name: 'glm-small', model: 'org/glm', quantization: 'bf16', topology: 'single-a' },
      { name: 'qwen', model: 'org/qwen', quantization: 'nvfp4', topology: 'tp2' }
    ], row => row.name, (row, filter) => filter === 'all' || row.topology === filter, 'empty');
  `);
  const search = c.element("#test-search"), filter = c.element("#test-filter");
  search.value = "GLM nvfp4"; search.oninput();
  assert.equal(c.element("#test-count").textContent, "1 of 3 shown");
  assert.equal(c.element("#test-items").innerHTML, "glm-fast");
  search.value = ""; search.oninput(); filter.value = "tp2"; filter.onchange();
  assert.equal(c.element("#test-count").textContent, "2 of 3 shown");
  search.focus = () => {};
  c.element("#test-clear").onclick();
  assert.equal(c.element("#test-count").textContent, "3 of 3 shown");
  assert.equal(c.element("#test-clear").disabled, true);
});

test("invalid or duplicate overrides are not silently ignored", () => {
  const c = client();
  c.element("#paste-text").value = "command: vllm serve org/model";
  for (const input of ["ctx 8192", "ctx=8192\nctx=16384", "=8192"]) {
    c.element("#paste-over").value = input;
    assert.throws(() => c.run("pasteBody()"), /Override line/);
  }
  c.element("#paste-over").value = "ctx=8192\nutil=0.85\nmode=auto";
  assert.deepEqual(JSON.parse(c.run("JSON.stringify(pasteBody().overrides)")), { ctx: 8192, util: 0.85, mode: "auto" });
});

test("pasting text alongside a URL requires choosing the intended source", () => {
  const c = client();
  c.element("#paste-text").value = "command: vllm serve org/model";
  c.element("#paste-url").value = "https://example.com/recipe.yaml";
  assert.throws(() => c.run("pasteBody()"), /either a recipe URL or pasted text/);
});

test("get-started checklist highlights the next step, escapes node errors and shows commands", () => {
  const c = client();
  const html = c.run(`startHtml(${JSON.stringify({
    done: 1, total: 3, complete: false, next: "link", dry_run: true,
    steps: [
      { id: "nodes", title: "Both Sparks are connected", status: "done", detail: "A and B reachable", required: true, fix: null },
      { id: "link", title: "QSFP link", status: "todo", detail: "<img src=x onerror=alert(1)>", required: true,
        fix: { command: "sudo tsm rdma --apply", href: "#/diagnostics/link", label: "Run a link test" } },
      { id: "live", title: "Real containers", status: "todo", detail: "dry-run on A", required: false, fix: null },
      { id: "recipe", title: "Import a recipe", status: "todo", detail: "later", required: true, fix: null },
    ],
  })})`);
  assert.match(html, /1 of 3 required steps done/);
  assert.match(html, /Dry-run mode/);
  assert.match(html, /sudo tsm rdma --apply/);
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /&lt;img src=x/);
  assert.equal((html.match(/class="card start-step [a-z]+ accent"/g) || []).length, 1);   // exactly one highlighted step
  assert.match(html, />suggested</);                                                        // optional todo is not "next"
  assert.match(html, />later</);
  const demo = c.run(`startHtml(${JSON.stringify({ done: 0, total: 1, complete: false, next: null, dry_run: true, demo: true, steps: [] })})`);
  assert.match(demo, /tsm demo/);
  assert.doesNotMatch(demo, /Dry-run mode/);
});

test("navigation lists Get started first and routes to it", () => {
  const c = client();
  assert.equal(c.run("NAV[0][0]"), "start");
  assert.ok(c.run("ROUTES[0][0].test('/start')"));
});

// ---- Remote page -------------------------------------------------------------------------------
const NODE_B = {
  node: "B", reachable: true, hostname: "spark-b", uptime_s: 93784, kernel: "6.11.0-nvidia", version: "0.4.1",
  privd: true, terminal_service: true, wake_configured: true, plug_configured: true, mac: "aa:bb:cc:dd:ee:ff",
  plug_actions: ["off", "on"], power_pending: [],
  policy: { terminal: true, reboot: false, poweroff: false, boot_next: false, wol: false, error: null },
  interfaces: [{ name: "enP7s7", mac: "aa:bb:cc:00:00:01", wol: "d", wol_supported: "pumbg" }],
};
const OV = { controller_node: "A", active: "qwen3", nodes: { A: { ...NODE_B, node: "A" }, B: NODE_B } };

test("remote page lists the switches and tells the operator the exact command to turn one on", () => {
  const c = client();
  const html = c.run(`remoteStatusHtml(${JSON.stringify(NODE_B)}, ${JSON.stringify(OV)})`);
  assert.match(html, /Node B/);
  assert.match(html, /online/);
  assert.match(html, /sudo tsm remote enable reboot/);
  assert.match(html, /sudo tsm remote enable boot-next/);
  assert.doesNotMatch(html, /sudo tsm remote enable terminal/, "the terminal is already on");
  assert.match(c.run(`remoteStatusHtml(${JSON.stringify({ ...NODE_B, node: "A" })}, ${JSON.stringify(OV)})`), /runs the controller/);
});

test("remote page escapes everything that comes from a node", () => {
  const c = client();
  const evil = { ...NODE_B, hostname: "<img src=x onerror=alert(1)>", kernel: "<b>k</b>", policy: { ...NODE_B.policy, error: "<script>x</script>" } };
  const html = c.run(`remoteStatusHtml(${JSON.stringify(evil)}, ${JSON.stringify(OV)})`);
  assert.doesNotMatch(html, /<img src=x|<script>|<b>k<\/b>/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  const down = c.run(`remoteStatusHtml(${JSON.stringify({ node: "B", reachable: false, error: "<svg onload=1>", policy: {} })}, ${JSON.stringify(OV)})`);
  assert.match(down, /not reachable/);
  assert.doesNotMatch(down, /<svg onload/);
  const reach = c.run(`remoteReachHtml(${JSON.stringify({ verdict: "host_down", summary: "<i>x</i>", steps: ["<u>y</u>"], agent_port_open: false, ssh_port_open: false, terminal_port_open: false })})`);
  assert.doesNotMatch(reach, /<i>x|<u>y/);
});

test("remote power buttons stay disabled until the node allows them, and say why", () => {
  const c = client();
  const off = c.run(`remotePowerHtml(${JSON.stringify(NODE_B)}, ${JSON.stringify(OV)})`);
  assert.match(off, /data-act="reboot" disabled/);
  assert.match(off, /data-act="poweroff" disabled/);
  assert.match(off, /sudo tsm remote enable poweroff/);
  const on = { ...NODE_B, policy: { ...NODE_B.policy, reboot: true, poweroff: true, boot_next: true, wol: true } };
  const html = c.run(`remotePowerHtml(${JSON.stringify(on)}, ${JSON.stringify(OV)})`);
  assert.doesNotMatch(html, /data-act="reboot" disabled/);
  assert.match(html, /data-act="plug-cycle"/);
  const noHelper = c.run(`remotePowerHtml(${JSON.stringify({ ...on, privd: false })}, ${JSON.stringify(OV)})`);
  assert.match(noHelper, /data-act="reboot" disabled/, "no privileged helper, no power buttons");
});

test("a node that runs the controller cannot be woken by itself, and a missing plug is explained", () => {
  const c = client();
  const a = c.run(`remotePowerHtml(${JSON.stringify({ ...NODE_B, node: "A", plug_configured: false })}, ${JSON.stringify(OV)})`);
  assert.match(a, /this node runs the controller/);
  assert.doesNotMatch(a, /data-act="wake"/);
  assert.match(a, /nodes\.A\.plug/);
});

test("terminal card explains why it is unavailable and offers touch keys", () => {
  const c = client();
  const off = c.run(`remoteTerminalHtml(${JSON.stringify({ ...NODE_B, policy: { ...NODE_B.policy, terminal: false } })})`);
  assert.match(off, /sudo tsm remote enable terminal/);
  assert.match(off, /data-act="term-open" disabled/);
  const nosvc = c.run(`remoteTerminalHtml(${JSON.stringify({ ...NODE_B, terminal_service: false })})`);
  assert.match(nosvc, /systemctl enable --now twinspark-terminal/);
  const on = c.run(`remoteTerminalHtml(${JSON.stringify(NODE_B)})`);
  assert.doesNotMatch(on, /data-act="term-open" disabled/);
  for (const k of ["Esc", "Tab", "Ctrl-C", "Ctrl-D"]) assert.match(on, new RegExp(`data-key="${k}"`));
  assert.equal(c.run("TERM_KEYS['Ctrl-C']"), "\x03");
});

test("remote tab and route are registered, and look-alike paths do not match", () => {
  const c = client();
  assert.equal(c.run("NAV.some(n => n[0] === 'remote')"), true);
  assert.equal(c.run("ROUTES.some(([re]) => re.test('/remote/B') && re.test('/remote'))"), true);
  assert.equal(c.run("ROUTES.some(([re]) => re.test('/remote/B/../x'))"), false);
});

test("link results say simulated, show failures as unavailable and escape errors", () => {
  const c = client();
  const sim = c.run(`linkResultHtml(${JSON.stringify({ mode: "tcp", simulated: true, streams: 4, duration_s: 5, initiator: { bandwidth_gbps: 41.7, dry_run: true } })})`);
  assert.match(sim, /Simulated\./);
  assert.match(sim, /4 TCP stream\(s\)/);
  const failed = c.run(`linkResultHtml(${JSON.stringify({ mode: "rdma", simulated: false, duration_s: 5, hcas: { A: ["rocep1s0f1", "roceP2p1s0f1"] },
    errors: ["ib_write_bw on roceP2p1s0f1 exited with code 1: <bad>"],
    initiator: { bandwidth_gbps: null, measured_gbps: 111.6, per_hca: [{ hca: "rocep1s0f1", gbps: 111.6 }, { hca: "roceP2p1s0f1", gbps: null }] } })})`);
  assert.doesNotMatch(failed, /Simulated/);
  assert.match(failed, /<div class="k">Bandwidth<\/div><div class="v">unavailable/);
  assert.match(failed, /<div class="k">roceP2p1s0f1<\/div><div class="v">unavailable/);
  assert.doesNotMatch(failed, /<bad>/);
  assert.match(failed, /&lt;bad&gt;/);
  assert.match(c.run(`linkResultHtml({initiator:{error:"connection refused <x>"}})`), /Link test failed\..*&lt;x&gt;/);
});

test("startup evidence shows both ranks, the first error and a download per log", () => {
  const c = client();
  const html = c.run(`evidenceHtml(${JSON.stringify({ profile: "glm", revision_id: "r1", images: ["img@sha256:1"], limits: { max_log_chars: 2000000, kept_jobs: 20 },
    containers: [
      { node: "A", name: "tsm-glm-a", status: "exited", exit_code: 1, memory: { mem_available_gib: 3.2, mem_total_gib: 121.6, page_cache_gib: 1, swap_used_gib: 0 },
        kernel_gpu_mem_errors: 129, first_error: "torch.OutOfMemoryError: <oom>", final_error: "RuntimeError: Engine core initialization failed", file: "A-tsm-glm-a.log", bytes: 4096 },
      { node: "B", name: "tsm-glm-b", error: "agent unreachable" }] })})`);
  assert.match(html, /Node A · <span class="mono">tsm-glm-a/);
  assert.match(html, /exited, exit code 1/);
  assert.match(html, /129 out-of-memory message/);
  assert.match(html, /data-act="evidence-log" data-file="A-tsm-glm-a.log"/);
  assert.match(html, /&lt;oom&gt;/);
  assert.match(html, /no evidence: agent unreachable/);
  assert.match(html, /exit code 1 does not rule out a GPU memory failure/);
  assert.equal(c.run("evidenceHtml(null)"), "");
});

test("work items name image, weights, drafter, copy and verify separately", () => {
  const c = client();
  const html = c.run(`phasesHtml(${JSON.stringify([
    { key: "image:x:A", kind: "image", label: "image ghcr.io/x@sha256:1 on node A", state: "reused", detail: "already on the node" },
    { key: "drafter:d@b:A", kind: "drafter", label: "drafter incoai/d@bbbb on node A", state: "running", detail: "downloading", progress: 0.4, ref: "incoai/d@" + "b".repeat(40) },
    { key: "copy:d@b:A>B", kind: "copy", label: "copy drafter node A → node B over QSFP", state: "failed", detail: "<rsync>" }])})`);
  assert.match(html, /data-key="phase:image:x:A"/);
  assert.match(html, />reused</);
  assert.match(html, /width:40%/);
  assert.match(html, /&lt;rsync&gt;/);
  assert.equal(c.run("phasesHtml([])"), "");
});

test("cache cells keep complete, external and verified apart", () => {
  const c = client();
  assert.match(c.run(`cacheCell({complete:true, managed:false, verified:false})`), /external cache — reused as is, no new download/);
  assert.match(c.run(`cacheCell({complete:true, managed:true, verified:true})`), /checksum verified/);
  assert.match(c.run(`cacheCell({complete:true, managed:true, verified:false})`), /checksums not verified/);
  assert.match(c.run(`cacheCell({complete:false})`), /partial/);
});

test("a recipe without an image asks for one; one with a hint or pin does not", () => {
  const c = client();
  assert.equal(c.run(`needsImage({image_hint:null, identity:null})`), true);
  assert.equal(c.run(`needsImage({image_hint:"ghcr.io/x:y", identity:null})`), false);
  assert.equal(c.run(`needsImage({image_hint:null, identity:{image:"x"}})`), false);
  const html = c.run(`imageFieldsHtml({image_hint:null, identity:null, source:{url:"https://github.com/getrefined/x"}, secondary:{image_hint:null, identity:null}})`);
  assert.match(html, /names no vLLM image/);
  assert.match(html, /id="imp-image-a"/);
  assert.match(html, /id="imp-image-b"/);
  assert.match(html, /github.com\/getrefined\/x/);
  assert.equal(c.run(`imageFieldsHtml({image_hint:"x"})`), "");
});

test("remote access summary matches the CLI wording", () => {
  const c = client();
  const s = c.run(`accessSummary({ssh:{running:true, starts_at_boot:false}, tailscale:{installed:true, running:true, ip:"100.64.0.7", serve:{manager_forwarded:false}}, default_target:"multi-user.target"})`);
  assert.equal(s, "SSH running, NOT enabled at boot; Tailscale up 100.64.0.7 · manager NOT forwarded (tsm remote tailscale-serve); boots into multi-user.target");
});

test("live views carry their key and keyed rows", () => {
  const c = client();
  const job = c.run(`jobHtml(${JSON.stringify({ job_id: "act-0123456789", kind: "activation", state: "running", created_at: 0, steps: [{ stage: "validating", status: "ok" }], payload: { profile: "glm" } })})`);
  assert.match(job, /data-live="job:act-0123456789"/);
  assert.match(job, /data-key="stage:validating"/);
  assert.match(job, /data-key="stage:loading"/);
});

test("recipe links are http(s) only and the boot setting of tailscaled is shown", () => {
  const c = client();
  assert.equal(c.run(`webUrl("javascript:alert(1)")`), "");
  assert.equal(c.run(`webUrl(" data:text/html,x")`), "");
  assert.equal(c.run(`webUrl("https://github.com/x/y")`), "https://github.com/x/y");
  const html = c.run(`imageFieldsHtml({name:"q", source:{url:"javascript:alert(1)"}})`);
  assert.match(html, /names no vLLM image/);
  assert.doesNotMatch(html, /javascript:/);
  assert.match(c.run(`imageFieldsHtml({name:"q", source:{url:"https://example.org/r"}})`), /href="https:\/\/example\.org\/r"/);
  const s = c.run(`accessSummary({ssh:{}, tailscale:{installed:true, running:true, ip:"100.64.0.7", service:{enabled:"disabled"}}})`);
  assert.match(s, /Tailscale up 100\.64\.0\.7, NOT enabled at boot/);
});

test("a failed start being cleaned up has no Cancel, and an excerpt stays when no rank had an error", () => {
  const c = client();
  const base = { job_id: "act-0123456789", kind: "activation", created_at: 0, payload: { profile: "glm" } };
  const cleaning = c.run(`jobHtml(${JSON.stringify({ ...base, state: "running", error: "loading: boom",
    rollback: "saving the startup logs of every rank, then stopping the partial containers",
    steps: [{ stage: "loading", status: "failed", log_excerpt: "tail of the log" }] })})`);
  assert.doesNotMatch(cleaning, /data-act="cancel"/);
  assert.match(cleaning, /saving the startup logs of every rank/);
  const running = c.run(`jobHtml(${JSON.stringify({ ...base, state: "running", steps: [{ stage: "loading", status: "running" }] })})`);
  assert.match(running, /data-act="cancel"/);
  const unreachable = c.run(`jobHtml(${JSON.stringify({ ...base, state: "failed", error: "loading: boom",
    steps: [{ stage: "loading", status: "failed", log_excerpt: "tail of the log" }],
    payload: { profile: "glm", startup_evidence: { containers: [{ node: "B", name: "tsm-b", error: "agent unreachable" }] } } })})`);
  assert.match(unreachable, /Log excerpt/);
  const shown = c.run(`jobHtml(${JSON.stringify({ ...base, state: "failed", error: "loading: boom",
    steps: [{ stage: "loading", status: "failed", log_excerpt: "tail of the log" }],
    payload: { profile: "glm", startup_evidence: { containers: [{ node: "B", name: "tsm-b", first_error: "OOM" }] } } })})`);
  assert.doesNotMatch(shown, /Log excerpt/);
  assert.match(shown, /Startup evidence/);
});
