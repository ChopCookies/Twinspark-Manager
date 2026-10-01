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
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../twinspark/web/src/js/app.js"), "utf8"), context);
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
