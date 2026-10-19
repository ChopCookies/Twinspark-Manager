/* The Benchmarks page. Run with node --test tests/web_bench.test.cjs. No browser dependencies. */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const JS = path.join(__dirname, "../twinspark/web/src/js");

function page(responses = {}) {
  const elements = new Map(), storage = new Map(), requests = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, { value: "", textContent: "", innerHTML: "", disabled: false, classList: { add() {}, remove() {}, toggle() {} }, focus() {} });
    return elements.get(id);
  };
  const fetch = async (url, opts = {}) => {
    requests.push({ url, method: opts.method || "GET", body: opts.body });
    const r = responses[url.split("?")[0]] ?? { status: 404, body: { detail: "Not Found" } };
    return { status: r.status ?? 200, ok: (r.status ?? 200) < 400, headers: { get: () => null }, json: async () => r.body, text: async () => JSON.stringify(r.body) };
  };
  const context = vm.createContext({
    console, URL, URLSearchParams, TextEncoder, clearTimeout, clearInterval, fetch,
    setTimeout: (fn, ms) => setTimeout(fn, ms).unref(), setInterval: (fn, ms) => setInterval(fn, ms).unref(),
    location: { hash: "", pathname: "/", search: "" }, history: { replaceState() {} },
    sessionStorage: { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k) },
    window: { addEventListener() {} },
    document: { addEventListener() {}, querySelector: element, title: "" },
  });
  for (const file of ["app.js", "start.js", "remote.js", "bench.js", "keys.js", "playground.js", "client.js"]) {
    vm.runInContext(fs.readFileSync(path.join(JS, file), "utf8"), context, { filename: file });
  }
  return { element, requests, run: code => vm.runInContext(code, context) };
}

const STORED = [{ id: "benchmark-1", profile: "glm", label: "r2", preset: "max-throughput", alias: "default", valid: true, created_at: Date.now() / 1000,
  summary: { text: "peak 1,234 output tok/s <script>" } },
{ id: "benchmark-2", profile: "glm", label: "r1", preset: "fixed", alias: "default", valid: false, invalid_reason: "the model behind default changed", created_at: Date.now() / 1000, summary: {} }];

test("the page is in the navigation and routed after the planner", () => {
  const p = page();
  const nav = JSON.parse(p.run("JSON.stringify(NAV.map(n => n[0]))"));
  assert.equal(nav[nav.indexOf("planner") + 1], "bench");
  assert.ok(p.run("ROUTES.some(([re]) => re.test('/bench/benchmark-1'))"));
});

test("lists and numbers in the run form are checked before anything is sent", () => {
  const p = page();
  assert.deepEqual(JSON.parse(p.run("JSON.stringify(benchList(' 1, 4,16 '))")), [1, 4, 16]);
  assert.equal(p.run("benchList('')"), null);
  assert.throws(() => p.run("benchList('1,0')"), /whole numbers from 1/);
  assert.throws(() => p.run("benchList('4,1.5', 256)"), /from 1 to 256/);
  assert.equal(p.run("benchNum('2000', {int:false})"), 2000);
  assert.equal(p.run("benchNum(' ')"), undefined);
  assert.throws(() => p.run("benchNum('8', {min:16, name:'prompt tokens'})"), /prompt tokens: a whole number from 16/);
});

test("on node A the page lists stored results and says where runs start", async () => {
  const p = page({ "/api/v1/benchmarks": { body: STORED }, "/api/v1/profiles": { body: [{ name: "glm", pinned: true }] } });
  p.run("S.unlocked = true; S.client = false;");
  await p.run("viewBench()");
  const html = p.element("#main").innerHTML;
  assert.match(html, /tsm bench run max-throughput/);
  assert.doesNotMatch(html, /data-act="bench-run"/);                 // nothing runs from node A's own page
  assert.match(html, /peak 1,234 output tok\/s &lt;script&gt;/);
  assert.match(html, /invalid<\/span> the model behind default changed/);
  assert.ok(!p.requests.some(r => r.url.includes("/api/v1/client/")));
});

test("through the client the page starts runs and shows this session's progress", async () => {
  const runs = [{ id: "bench-1", kind: "run", state: "running", preset: "latency-slo", alias: "default<b>", message: "latency-slo: concurrency 8",
    points: [{ concurrency: 1, output_tok_s: 100 }, { concurrency: 8, output_tok_s: 640 }], uploaded: null, upload_error: null },
  { id: "compare-1", kind: "compare", state: "completed", message: "done", sides: [{ profile: "a", revision: "r1", valid: true, summary: { text: "c1: 90" }, result_id: "benchmark-9" }, { profile: "b", revision: "r2", valid: true, summary: { text: "c1: 120" } }],
    table: [{ concurrency: 1, a_output_tok_s: 90, b_output_tok_s: 120 }] }];
  const p = page({ "/api/v1/benchmarks": { body: [] }, "/api/v1/client/bench": { body: { presets: [], runs } }, "/api/v1/profiles": { body: [] } });
  p.run("S.unlocked = true; S.client = true;");
  await p.run("viewBench()");
  const html = p.element("#main").innerHTML;
  assert.match(html, /data-act="bench-run"/);
  assert.match(html, /data-act="bench-compare"/);
  assert.match(html, /Latency target · default&lt;b&gt;/);
  assert.match(html, /data-act="bench-cancel" data-id="bench-1"/);
  assert.match(html, /width:100.0%;background:#6aa7ff/);             // the biggest level fills the bar
  assert.match(html, /a r1.*b r2/s);
  assert.match(html, /href="#\/bench\/benchmark-9"/);
});

test("a stored result shows every level and escapes what the client sent", async () => {
  const result = { id: "benchmark-1", profile: "glm", label: "r2", revision_id: "rev-x", preset: "context", alias: "default", evidence: "measured-by-client", valid: true,
    summary: { text: "2 context lengths" },
    result: { seed: 7, options: { input_tokens: 1024, output_tokens: 128, cache: "cold" }, max_model_len: 40000, client_version: "0.7.0",
      points: [{ concurrency: 1 }], ladder: [{ context: 8192, concurrency: 1, requests: 2, output_tok_s: 50, ttft_ms: { p50: 900, p95: 950 }, tpot_ms: { p50: 20, p95: 22 }, error_rate: 0 }],
      needles: [{ context_tokens: 8128, found: 2, needles: 3, score: 0.67 }], checks: [{ id: "tools", title: "<img src=x>", status: "fail", detail: "no tool call" }],
      telemetry: { A: { min_mem_available_gib: 12.5, mean_power_w: 80, energy_wh: 1.2 } } } };
  const p = page({ "/api/v1/benchmarks/benchmark-1": { body: result } });
  p.run("S.unlocked = true;");
  await p.run("viewBenchResult('benchmark-1')");
  const html = p.element("#main").innerHTML;
  assert.match(html, /<th>Context<\/th>/);
  assert.match(html, /8,192/);
  assert.match(html, /2 \/ 3/);
  assert.match(html, /&lt;img src=x&gt;/);
  assert.match(html, /lowest free memory 12.5 GiB/);
  assert.match(html, /measured-by-client · client 0.7.0/);
});

test("the plan says which measurements back the forecast", () => {
  const p = page();
  const html = p.run(`forecastBacking({ fingerprinted: true,
    kv_source: { A: { source: "measured", note: "measured on node A from vLLM's KV pool on 2 start(s)" }, B: { source: "estimated", note: "the recipe measured its KV size with other settings (kv_dtype changed)" } },
    calibration: { A: { samples: 2, worst_gib: 101.2, median_gib: 100.1, spread_gib: 1.1, margin_gib: 1.6 }, B: null } })`);
  assert.match(html, /What backs this forecast/);
  assert.match(html, /2 starts<\/span> worst 101.2 GiB, median 100.1 GiB, spread 1.1 GiB, safety margin 1.6 GiB/);
  assert.match(html, /\+0.5 GiB until 3 starts/);
  assert.match(html, /estimated \(formula\)<\/span> <span class="muted">the recipe measured its KV size with other settings \(kv_dtype changed\)/);
  assert.match(html, /no start with exactly these settings/);
  assert.equal(p.run("forecastBacking({})"), "");
});

test("a split deployment shows each half with its own restart", () => {
  const p = page();
  const html = p.run(`halvesHtml({ busy: false, active: { profile: "duo", revision_id: "rev-2" }, halves: [
    { node: "A", name: "duo-a", model: "org/a", model_revision: "a".repeat(40), aliases: ["chat"], revision_id: "rev-1", label: "r1", since: 0, state: "serving", metrics: { ok: true, decode_tok_s: 42, kv_cache_usage_pct: 12, "requests-running": 1, "requests-waiting": 0 } },
    { node: "B", name: "duo-b", model: "org/<b>", model_revision: "b".repeat(40), aliases: ["code"], revision_id: "rev-2", label: "r2", since: 0, state: "down", reason: "switch failed: <oom>" }] })`);
  assert.match(html, /kept across later revisions/);
  assert.match(html, /data-act="half-restart" data-node="A" data-aliases="chat"/);
  assert.match(html, /Start this half/);
  assert.match(html, /org\/&lt;b&gt;/);
  assert.match(html, /switch failed: &lt;oom&gt;/);
  assert.equal(p.run("halvesHtml({ active: null, halves: [] })"), "");
});

test("the System page shows the last day from node A's history", () => {
  const p = page();
  const html = p.run(`historyCard({ ts: [0, 3600, 7200], decode_tok_s: [10, 50, 30], kv_cache_usage_pct: [5, 9, 7], ttft_p95_ms: [null, 400, 300], "A.mem_available_gib": [60, 40.5, 55] }, ["A"])`);
  assert.match(html, /Last 2 h/);
  assert.match(html, /Decode \(peak 50\)/);
  assert.match(html, /Node A MemAvailable \(low 40.5 GiB\)/);
  assert.equal(p.run("historyCard({ ts: [] }, [])"), "");
});

test("the dashboard says how requests are routed and what answers while switching", () => {
  const p = page();
  const html = p.run(`routingHtml({ balance: "least-inflight", affinity: true, weights: { A: 2 }, fallbacks: { default: { alias: "coder" }, chat: { url: "https://x.example/v1", model: "<m>" } } })`);
  assert.match(html, /least-inflight · node A × 2 · session affinity on/);
  assert.match(html, /alias <span class="mono">coder<\/span>/);
  assert.match(html, /\(&lt;m&gt;\)/);
  assert.match(html, /data-act="routing"/);
  assert.equal(p.run("routingHtml(undefined)"), "");
});

test("the keys page shows state and aliases, never more than the prefix", () => {
  const p = page();
  const html = p.run(`keysTable([{ id: "default", name: "default", prefix: "abc123", aliases: [], state: "active", migrated: true, created_at: 1 },
    { id: "key-1", name: "<laptop>", app: "openclaw", prefix: "tsk_abcdef", aliases: ["coder"], state: "revoked", created_at: 1 }])`);
  assert.match(html, /the key from before 0.7/);
  assert.match(html, /data-act="key-revoke" data-id="default" data-name="default" data-migrated="1"/);
  assert.match(html, /&lt;laptop&gt;/);
  assert.match(html, /data-act="key-delete" data-id="key-1"/);
  assert.match(html, /tsk_abcdef…/);
  const u = p.run(`usageHtml({ names: { "key-1": "laptop" }, rows: [
    { day: "2026-10-18", key_id: "key-1", alias: "coder", requests: 2, prompt_tokens: 100, completion_tokens: 50, estimated_requests: 1 },
    { day: "2026-10-19", key_id: "key-1", alias: "coder", requests: 1, prompt_tokens: 10, completion_tokens: 5, estimated_requests: 0 }] })`);
  assert.match(u, /<td>laptop<\/td><td class="mono">coder<\/td><td class="num">3<\/td><td class="num">110<\/td><td class="num">55/);
  assert.match(u, /1 estimated/);
  assert.match(p.run("usageHtml({ rows: [] })"), /No requests/);
});
