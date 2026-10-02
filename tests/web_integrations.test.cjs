/* Run with node --test tests/web_integrations.test.cjs. */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function client() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: "", innerHTML: "", textContent: "", disabled: false,
      classList: { add() {}, remove() {} },
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    console, URL, URLSearchParams, TextEncoder, setTimeout, setInterval, clearInterval,
    sessionStorage: { getItem: () => null },
    window: { addEventListener() {} },
    document: { addEventListener() {}, querySelector: element },
  });
  const run = code => vm.runInContext(code, context);
  run(fs.readFileSync(path.join(__dirname, "../twinspark/web/src/js/app.js"), "utf8"));
  run("var handlers, calls = [], destination; onAct = h => { handlers = h; }; busy = (btn, fn) => fn(); go = value => { destination = value; };");
  return { context, element, run };
}

function setup(c, status = "serving") {
  c.context.catalog = {
    gateway: { suggested_base_url: "http://192.168.1.10:8000/v1", auth_required: true },
    clients: [{ id: "new-harness", title: "New harness", description: "Works with local models", requirements: ["Requires tool calling"], docs_url: "https://example.com/setup" }],
    aliases: [
      { alias: "coder", profile: "duo", revision_id: "r2", topology: "split", node: "A", configured_tools: true, tool_parser: "model-parser", context_length: 65536, status },
      { alias: "helper", profile: "duo", revision_id: "r2", topology: "split", node: "B", configured_tools: false, context_length: 32768, status },
    ],
  };
  c.run("api = async (method,path,body) => { calls.push({method,path,body}); if (path === '/api/v1/integrations') return catalog; if (method === 'POST') return {job_id:'compat-1'}; return {files:[{name:'connect.json',content:'{\"key\":\"${GATEWAY_API_KEY}\"}'}],notes:['Set your gateway key locally']}; };");
}

test("catalog drives client selection and exports optional split aliases with placeholders", async () => {
  const c = client(); setup(c);
  await c.run("viewIntegrations()");
  assert.match(c.element("#main").innerHTML, /New harness/);
  assert.match(c.element("#integration-client-info").innerHTML, /Requires tool calling/);
  assert.equal(c.element("#integration-alias").value, "coder");
  c.element("#integration-secondary").value = "helper";
  c.element("#integration-secondary").onchange();
  await c.run("handlers['integration-export'](null)");
  const calls = JSON.parse(c.run("JSON.stringify(calls)"));
  const exported = new URL(calls[1].path, "http://controller");
  assert.equal(exported.searchParams.get("client"), "new-harness");
  assert.equal(exported.searchParams.get("alias"), "coder");
  assert.equal(exported.searchParams.get("secondary_alias"), "helper");
  assert.equal(exported.searchParams.get("base_url"), "http://192.168.1.10:8000/v1");
  assert.match(c.element("#integration-files").innerHTML, /GATEWAY_API_KEY/);
  assert.match(c.element("#integration-files").innerHTML, /data-copy/);
  assert.match(c.element("#integration-files").innerHTML, /Download file/);
});

test("planned aliases allow setup generation and block compatibility runs until serving", async () => {
  const c = client(); setup(c, "planned");
  await c.run("viewIntegrations()");
  assert.equal(c.element("#integration-check-button").disabled, true);
  assert.match(c.element("#integration-recipe").innerHTML, /Activate this recipe/);
  await c.run("handlers['integration-check'](null)");
  assert.equal(c.run("calls.length"), 1);
  assert.match(c.element("#integration-error").textContent, /Activate this recipe/);
  await c.run("handlers['integration-export'](null)");
  assert.match(c.element("#integration-files").innerHTML, /connect.json/);
});

test("checks use the selected alias and navigate to the returned job", async () => {
  const c = client(); setup(c);
  await c.run("viewIntegrations()");
  c.element("#integration-alias").value = "helper";
  c.element("#integration-alias").onchange();
  await c.run("handlers['integration-check'](null)");
  const calls = JSON.parse(c.run("JSON.stringify(calls)"));
  assert.deepEqual(calls[1], { method: "POST", path: "/api/v1/integrations/check", body: { alias: "helper" } });
  assert.equal(c.run("destination"), "#/jobs/compat-1");
});

test("export errors retain selected values and can be retried", async () => {
  const c = client(); setup(c);
  await c.run("viewIntegrations()");
  c.element("#integration-url").value = "http://spark-manager:8000/v1";
  c.element("#integration-url").oninput();
  c.run("api = async () => { throw new ApiError('Context length has not been measured', 422); };");
  await c.run("handlers['integration-export'](null)");
  assert.equal(c.element("#integration-url").value, "http://spark-manager:8000/v1");
  assert.equal(c.element("#integration-client").value, "new-harness");
  assert.match(c.element("#integration-error").textContent, /not been measured/);
  c.run("api = async () => ({files:[{name:'retry.json',content:'{}'}]});");
  await c.run("handlers['integration-export'](null)");
  assert.match(c.element("#integration-files").innerHTML, /retry.json/);
});

test("selection changes and navigation suppress stale connection files", async () => {
  const c = client(); setup(c);
  await c.run("viewIntegrations()");
  c.run("var finish; api = () => new Promise(resolve => { finish = resolve; });");
  const pending = c.run("handlers['integration-export'](null)");
  c.element("#integration-alias").value = "helper";
  c.element("#integration-alias").onchange();
  c.run("finish({files:[{name:'old.json',content:'old model'}]})");
  await pending;
  assert.equal(c.element("#integration-files").innerHTML, "");
  c.run("api = async () => { S.gen++; return {files:[{name:'late.json',content:'late'}]}; };");
  await c.run("handlers['integration-export'](null)");
  assert.equal(c.element("#integration-files").innerHTML, "");
});

test("same alias pairs and credential-bearing URLs fail before export", async () => {
  const c = client(); setup(c);
  await c.run("viewIntegrations()");
  c.element("#integration-secondary").value = "coder";
  await c.run("handlers['integration-export'](null)");
  assert.match(c.element("#integration-error").textContent, /different connections/);
  c.element("#integration-secondary").value = "";
  for (const address of ["javascript:alert(1)", "http://user:password@host/v1", "https://host/v1?key=secret", "https://host/v1#key"]) {
    c.element("#integration-url").value = address;
    await c.run("handlers['integration-export'](null)");
    assert.match(c.element("#integration-error").textContent, /without a key/);
  }
  assert.equal(c.run("calls.length"), 1);
});

test("compatibility results escape output and simulations do not earn a passed status", () => {
  const c = client();
  const html = c.run("jobHtml({job_id:'check-1',kind:'compatibility',state:'completed',steps:[],payload:{alias:'coder',profile:'duo',revision_id:'r2',dry_run:true,checks:[{name:'chat',status:'pass',message:'<simulated>'},{name:'tools',status:'unsupported',message:'Parser missing'}]}})");
  assert.match(html, /Check connection coder/);
  assert.match(html, /Simulation/);
  assert.match(html, />Simulated</);
  assert.match(html, />Unsupported</);
  assert.match(html, /&lt;simulated&gt;/);
  assert.doesNotMatch(html, />Passed</);
  assert.match(html, /Check again/);
  assert.doesNotMatch(html, /Switch to prepared revision|Serving\./);
  const actual = c.run("compatibilityChecksHtml([{name:'chat',status:'pass'},{name:'tools',status:'fail'}],false)");
  assert.match(actual, />Passed</);
  assert.match(actual, />Failed</);
});

test("historical checks identify their recipe revision and planned context stays unknown", () => {
  const c = client();
  const html = c.run("integrationAliasHtml({alias:'<coder>',profile:'duo',revision_id:'r2',status:'planned',configured_tools:false,context_length:null,latest_check:{state:'completed',dry_run:false,revision_id:'r1',checks:[{name:'chat',status:'pass'}]}})");
  assert.match(html, /&lt;coder&gt;/);
  assert.match(html, /not measured/);
  assert.match(html, /Recorded for revision r1/);
  assert.match(html, /Live check/);
  assert.match(html, /choose a parser/);
  const invalidDocs = c.run("integrationClientHtml({title:'Bad link',docs_url:'javascript:alert(1)'})");
  assert.doesNotMatch(invalidDocs, /javascript:|href=/);
  const automaticContext = c.run("integrationAliasHtml({alias:'coder',profile:'duo',context_length:'auto',context_source:'recipe',status:'planned'})");
  assert.match(automaticContext, /Auto — not measured/);
  assert.doesNotMatch(automaticContext, /— tokens/);
});

test("download saves the exported text through a temporary URL with a local filename", async () => {
  const c = client();
  let saved, clicked = false, removed = false, revoked;
  const link = { click() { clicked = true; }, remove() { removed = true; } };
  c.context.Blob = Blob;
  c.context.URL = class extends URL {
    static createObjectURL(blob) { saved = blob; return "blob:temporary"; }
    static revokeObjectURL(href) { revoked = href; }
  };
  c.context.setTimeout = fn => fn();
  c.context.document.createElement = () => link;
  c.context.document.body = { appendChild() {} };
  c.run("downloadIntegrationFile({name:'templates/client.json',content:'{\"apiKey\":\"${GATEWAY_API_KEY}\"}'})");
  assert.equal(await saved.text(), '{"apiKey":"${GATEWAY_API_KEY}"}');
  assert.equal(link.download, "client.json");
  assert.equal(clicked, true);
  assert.equal(removed, true);
  assert.equal(revoked, "blob:temporary");
});

test("a late check response cannot navigate away from a new screen", async () => {
  const c = client(); setup(c);
  await c.run("viewIntegrations()");
  c.run("api = async () => { S.gen++; return {job_id:'late-check'}; };");
  await c.run("handlers['integration-check'](null)");
  assert.equal(c.run("destination"), undefined);
});

function reportFile(report, size) {
  const text = typeof report === "string" ? report : JSON.stringify(report);
  return { name: "results.json", size: size ?? Buffer.byteLength(text), text: async () => text };
}

test("evaluation import binds the explicit revision and sends numeric metrics without secrets or samples", async () => {
  const c = client(); setup(c);
  await c.run("viewIntegrations()");
  assert.equal(c.element("#evaluation-revision").value, "r2");
  c.element("#evaluation-revision").value = "r1-exact";
  c.element("#evaluation-file").files = [reportFile({
    results: { gsm8k: { "exact_match,strict-match": 0.42, stderr: 0, alias: "gsm8k" } },
    config: { model_args: { model: "coder", api_key: "private-key" }, limit: 20, token: "private-config" },
    samples: [{ prompt: "private prompt", answer: "private answer" }],
  })];
  c.element("#evaluation-file").onchange();
  assert.equal(c.element("#evaluation-record-button").disabled, false);
  await c.run("handlers['evaluation-record'](null)");
  const calls = JSON.parse(c.run("JSON.stringify(calls)"));
  assert.deepEqual(calls[1], {
    method: "POST", path: "/api/v1/integrations/evaluations", body: {
      alias: "coder", revision_id: "r1-exact", results: {
        results: { gsm8k: { "exact_match,strict-match": 0.42, stderr: 0 } },
        config: { model_args: { model: "coder" }, limit: 20 },
      },
    },
  });
  assert.doesNotMatch(JSON.stringify(calls), /private-key|private-config|private prompt|private answer/);
  assert.equal(c.run("destination"), "#/jobs/compat-1");
});

test("evaluation import rejects mismatched aliases and oversized files before posting", async () => {
  const c = client(); setup(c);
  await c.run("viewIntegrations()");
  c.element("#evaluation-file").files = [reportFile({ results: { gsm8k: { score: 0.3 } }, config: { model_args: "model=helper,api_key=private-key" } })];
  await c.run("handlers['evaluation-record'](null)");
  assert.match(c.element("#evaluation-error").textContent, /alias differs/);
  let read = false;
  c.element("#evaluation-file").files = [{ size: 1024 * 1024 + 1, text: async () => { read = true; return "{}"; } }];
  await c.run("handlers['evaluation-record'](null)");
  assert.match(c.element("#evaluation-error").textContent, /too large/);
  assert.equal(read, false);
  assert.equal(c.run("calls.length"), 1);
});

test("evaluation validation errors retain the file and revision for retry", async () => {
  const c = client(); setup(c);
  await c.run("viewIntegrations()");
  const file = reportFile({ results: { gsm8k: { score: 0.3 } } });
  c.element("#evaluation-file").files = [file];
  c.element("#evaluation-revision").value = "older-r1";
  c.run("api = async () => { throw new ApiError('Choose the exact recipe revision',422); };");
  await c.run("handlers['evaluation-record'](null)");
  assert.match(c.element("#evaluation-error").textContent, /exact recipe revision/);
  assert.equal(c.element("#evaluation-revision").value, "older-r1");
  assert.equal(c.element("#evaluation-file").files[0], file);
  c.run("api = async () => ({job_id:'evaluation-1'});");
  await c.run("handlers['evaluation-record'](null)");
  assert.equal(c.run("destination"), "#/jobs/evaluation-1");
});

test("evaluation file errors hide raw JSON and require a pinned revision", async () => {
  const c = client(); setup(c);
  await c.run("viewIntegrations()");
  c.element("#evaluation-file").files = [reportFile('{"api_key":"secret-key", bad}')];
  await c.run("handlers['evaluation-record'](null)");
  assert.match(c.element("#evaluation-error").textContent, /not valid JSON/);
  assert.doesNotMatch(c.element("#evaluation-error").textContent, /secret-key/);
  c.element("#evaluation-file").files = [reportFile({ results: { gsm8k: { alias: "label" } } })];
  await c.run("handlers['evaluation-record'](null)");
  assert.match(c.element("#evaluation-error").textContent, /no numeric/);
  c.element("#evaluation-revision").value = "";
  await c.run("handlers['evaluation-record'](null)");
  assert.match(c.element("#evaluation-error").textContent, /exact pinned recipe revision/);
  assert.equal(c.run("calls.length"), 1);
});

test("changing model while a report reads prevents attribution to another alias", async () => {
  const c = client(); setup(c);
  await c.run("viewIntegrations()");
  let finish;
  c.element("#evaluation-file").files = [{ size: 100, text: () => new Promise(resolve => { finish = resolve; }) }];
  const pending = c.run("handlers['evaluation-record'](null)");
  c.element("#integration-alias").value = "helper";
  c.element("#integration-alias").onchange();
  finish('{"results":{"gsm8k":{"score":0.4}}}');
  await pending;
  assert.equal(c.run("calls.length"), 1);
  assert.equal(c.run("destination"), undefined);
  assert.equal(c.element("#evaluation-revision").value, "r2");
});

test("evaluation jobs show plain numeric values and reported evidence without a verified badge", () => {
  const c = client();
  const html = c.run("jobHtml({job_id:'evaluation-1',kind:'evaluation',state:'completed',steps:[],payload:{alias:'coder',profile:'duo',revision_id:'r1',source:'lm-evaluation-harness',evidence:'reported',verified:false,sample_limit:0.1,metrics:{'<task>':{'accuracy':0.123456789,'private text':'secret'}}}})");
  assert.match(html, /Evaluation report coder/);
  assert.match(html, /Reported, not independently verified/);
  assert.match(html, /&lt;task&gt;/);
  assert.match(html, /0\.123456789/);
  assert.match(html, /0\.1/);
  assert.doesNotMatch(html, /private text|secret|Switch to prepared revision|Passed/);
});

test("startup and remote modules coexist with integrations navigation and route dispatch", () => {
  const c = client();
  for (const script of ["start.js", "remote.js"]) c.run(fs.readFileSync(path.join(__dirname, "../twinspark/web/src/js", script), "utf8"));
  const nav = JSON.parse(c.run("JSON.stringify(NAV.map(([id]) => id))"));
  assert.equal(nav[0], "start");
  for (const id of ["start", "remote", "integrations", "cookbook"]) assert.equal(nav.filter(value => value === id).length, 1);
  c.run("var visited = []; viewStart = () => visited.push('start'); viewRemote = node => visited.push('remote:' + node); viewIntegrations = () => visited.push('integrations');");
  for (const destination of ["/start", "/remote/B", "/integrations"]) {
    c.context.routePath = destination;
    c.run("{ const [pattern, view] = ROUTES.find(([pattern]) => pattern.test(routePath)); view(routePath.match(pattern)); }");
  }
  assert.deepEqual(JSON.parse(c.run("JSON.stringify(visited)")), ["start", "remote:B", "integrations"]);
});

test("HTML loads merged GUI modules and terminal assets in dependency order", () => {
  const source = path.join(__dirname, "../twinspark/web/src");
  const html = fs.readFileSync(path.join(source, "index.html"), "utf8");
  const scripts = [...html.matchAll(/<script src="([^"]+)"/g)].map(match => match[1]);
  for (const script of scripts) assert.equal(fs.existsSync(path.join(source, script)), true, script);
  assert.equal(scripts[0], "js/app.js");
  assert.ok(scripts.indexOf("js/start.js") > scripts.indexOf("js/app.js"));
  assert.ok(scripts.indexOf("js/remote.js") > scripts.indexOf("vendor/xterm.js"));
  assert.ok(scripts.indexOf("js/remote.js") > scripts.indexOf("vendor/addon-fit.js"));
  const css = fs.readFileSync(path.join(source, "css/style.css"), "utf8");
  assert.doesNotMatch(css, /^(<<<<<<<|=======|>>>>>>>)/m);
  assert.match(css, /\.integration-details/);
  assert.match(css, /\.start-step/);
  assert.match(css, /\.term-box/);
  assert.match(css, /\.recipe-changes td/);
});
