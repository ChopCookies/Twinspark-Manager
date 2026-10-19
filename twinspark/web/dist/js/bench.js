/* TwinSpark Manager — "Benchmarks" page (master plan §2.1). Loaded after app.js, before client.js (which must come last).
   On node A: the stored results, each against the exact revision it measured.
   Through the workstation client: also start runs and compare two revisions. The runs go from this computer
   through the gateway, like your apps' requests; the finished result is stored on node A.
   Everything dynamic goes through esc(). */
"use strict";

Object.assign(ICON, {
  bench: '<path d="M4 20V11M10 20V5M16 20v-6M21 20H3" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round"/>',
});
NAV.splice(Math.max(0, NAV.findIndex(n => n[0] === "planner") + 1), 0, ["bench", "Benchmarks"]);
ROUTES.push([/^\/bench$/, () => viewBench()]);
ROUTES.push([/^\/bench\/([^/]+)$/, (m) => viewBenchResult(safeDecode(m[1]))]);

const BENCH_PRESETS = [
  ["max-throughput", "Max throughput", "double the load until throughput stops growing: the knee"],
  ["latency-slo", "Latency target", "the highest concurrency that still meets a TTFT and TPOT target"],
  ["context", "Context ladder", "throughput at each context length up to the deployed limit, plus retrieval"],
  ["quality", "Quality check", "retrieval of facts hidden in a long prompt, and a tool call round trip"],
  ["fixed", "Fixed levels", "the same concurrency levels every time: for comparing revisions"],
];
const benchLive = (s) => ["pending", "running"].includes(s);
const benchState = (s) => tag(s, { completed: "good", running: "info", pending: "info", invalid: "warn", failed: "bad", cancelled: "muted" }[s] || "muted");
/** "1, 4,16" → [1,4,16]; null when empty; throws on anything but positive integers */
function benchList(text, max = 1048576) {
  const t = String(text || "").trim();
  if (!t) return null;
  const out = t.split(/[\s,]+/).filter(Boolean).map(Number);
  if (out.some(n => !Number.isInteger(n) || n < 1 || n > max)) throw new Error(`use whole numbers from 1 to ${max}, separated by commas`);
  return out;
}
function benchNum(text, { min = 1, max = 1e9, int = true, name = "value" } = {}) {
  const t = String(text ?? "").trim();
  if (!t) return undefined;
  const n = Number(t);
  if (!isFinite(n) || n < min || n > max || (int && !Number.isInteger(n))) throw new Error(`${name}: ${int ? "a whole number" : "a number"} from ${min} to ${max}`);
  return n;
}
function benchPointsTable(points, withContext = false) {
  if (!points || !points.length) return empty("No measured levels");
  return `<div class="table-wrap"><table><thead><tr>${withContext ? "<th>Context</th>" : ""}<th>Concurrency</th><th>Requests</th><th>Output tok/s</th><th>TTFT p50 / p95</th><th>TPOT p50 / p95</th><th>Errors</th></tr></thead><tbody>
    ${points.map(p => `<tr>${withContext ? `<td>${num(p.context)}</td>` : ""}<td>${num(p.concurrency)}</td><td>${num(p.requests)}</td><td><b>${num(p.output_tok_s, 1)}</b></td>
      <td>${num(p.ttft_ms?.p50)} / ${num(p.ttft_ms?.p95)} ms</td><td>${num(p.tpot_ms?.p50, 1)} / ${num(p.tpot_ms?.p95, 1)} ms</td>
      <td>${p.error_rate ? tag(Math.round(p.error_rate * 100) + " %", "bad") : "0"}</td></tr>`).join("")}
  </tbody></table></div>`;
}
/** output tok/s per concurrency as bars; two series for a comparison */
function benchBars(rows, series) {
  const max = Math.max(1e-9, ...rows.flatMap(r => series.map(s => r[s.key] || 0)));
  return `<div class="bench-bars">${rows.map(r => `<div class="bench-bar-row"><span class="mono small">c${esc(r.concurrency)}</span><div class="bench-bar-set">${series.map(s => {
    const v = r[s.key];
    return `<div class="bench-bar" title="${esc(s.label)}: ${esc(num(v, 1))} tok/s"><i style="width:${v ? Math.max(1, (v / max) * 100).toFixed(1) : 0}%;background:${s.color}"></i><span class="small">${num(v, 0)}</span></div>`;
  }).join("")}</div></div>`).join("")}</div>
  ${series.length > 1 ? `<div class="legend small">${series.map(s => `<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join("")}</div>` : ""}`;
}

async function benchRunForm() {
  const st = await GET("/api/v1/status").catch(() => ({}));
  const aliases = [...new Set((st.routes || []).map(r => r.alias))];
  const preset = BENCH_PRESETS[0][0];
  const vals = await formBox("Run a benchmark", [
    { id: "alias", label: "Model (gateway alias)", type: aliases.length ? "select" : "", value: aliases[0] || "default", options: aliases.map(a => [a, a]) },
    { id: "preset", label: "What to measure", type: "select", value: preset, options: BENCH_PRESETS.map(p => [p[0], `${p[1]} — ${p[2]}`]) },
    { id: "input_tokens", label: "Prompt tokens per request", value: "1024", help: "Every request gets a fresh prompt, so the prefix cache does not flatter the numbers." },
    { id: "output_tokens", label: "Generated tokens per request", value: "128", help: "Exact: the model is asked to generate this many tokens and not stop early." },
    { id: "max_concurrency", label: "Highest concurrency", value: "64" },
    { id: "levels", label: "Fixed levels (fixed preset)", value: "1,4,16" },
    { id: "slo_ttft_ms", label: "TTFT target, ms (latency target)", value: "2000" },
    { id: "slo_tpot_ms", label: "TPOT target, ms (latency target)", value: "100" },
    { id: "contexts", label: "Context lengths (context ladder; empty = 8k … the deployed limit)", value: "" },
    { id: "warm", label: "Warm cache: send the same prompt every time (measures the prefix cache on purpose)", type: "check", value: false },
  ], {
    label: "Start", wide: true,
    intro: `<p class="small muted">The run goes from this computer through the gateway, like your apps' requests: it measures what they get, including the network in between. Requests from apps during the run make it slower, and a model switch on node A makes it invalid.</p>`,
    validate: (v) => {
      v.body = {
        alias: v.alias || "default", preset: v.preset, cache: v.warm ? "warm" : "cold",
        input_tokens: benchNum(v.input_tokens, { min: 16, max: 1048576, name: "prompt tokens" }),
        output_tokens: benchNum(v.output_tokens, { min: 1, max: 32768, name: "generated tokens" }),
        max_concurrency: benchNum(v.max_concurrency, { min: 1, max: 256, name: "highest concurrency" }),
        slo_ttft_ms: benchNum(v.slo_ttft_ms, { min: 1, max: 600000, int: false, name: "TTFT target" }),
        slo_tpot_ms: benchNum(v.slo_tpot_ms, { min: 1, max: 60000, int: false, name: "TPOT target" }),
        levels: v.preset === "fixed" ? benchList(v.levels, 256) : undefined,
        contexts: v.preset === "context" ? benchList(v.contexts) : undefined,
      };
      return true;
    },
  });
  if (!vals) return;
  const r = await POST("/api/v1/client/bench", vals.body);
  toast(`benchmark ${r.id} started`);
  route();
}

async function benchCompareForm() {
  const profiles = (await GET("/api/v1/profiles?summary=true")).filter(p => p.pinned);
  if (profiles.length < 1) { toast("pin a profile first: only pinned revisions can be compared", "bad"); return; }
  const opts = profiles.map(p => [p.name, `${p.name} (${p.latest?.label || "latest"})`]);
  const st = await GET("/api/v1/status").catch(() => ({}));
  const vals = await formBox("Compare two revisions", [
    { id: "a", label: "First", type: "select", value: opts[0][0], options: opts },
    { id: "a_rev", label: "First: revision (label or id)", value: "latest" },
    { id: "b", label: "Second", type: "select", value: (opts[1] || opts[0])[0], options: opts },
    { id: "b_rev", label: "Second: revision (label or id)", value: "latest" },
    { id: "levels", label: "Concurrency levels", value: "1,4,16" },
    { id: "input_tokens", label: "Prompt tokens per request", value: "1024" },
    { id: "output_tokens", label: "Generated tokens per request", value: "128" },
    { id: "restore", label: `Switch back to ${st.active ? st.active.profile : "the current model"} afterwards`, type: "check", value: true },
  ], {
    label: "Switch and measure", wide: true,
    intro: `<p>This <b>switches node A's model twice</b> (and back, if ticked): apps are without a model while each switch runs. A switch is aborted rather than cutting off requests that are still running.</p>`,
    validate: (v) => {
      if (v.a === v.b && v.a_rev === v.b_rev) throw new Error("choose two different revisions");
      v.body = {
        alias: "default", restore: v.restore, levels: benchList(v.levels, 256) || [1, 4, 16],
        a: { profile: v.a, revision: v.a_rev || "latest" }, b: { profile: v.b, revision: v.b_rev || "latest" },
        input_tokens: benchNum(v.input_tokens, { min: 16, max: 262144, name: "prompt tokens" }),
        output_tokens: benchNum(v.output_tokens, { min: 1, max: 8192, name: "generated tokens" }),
      };
      const alias = profiles.find(p => p.name === v.a)?.alias;
      if (alias) v.body.alias = alias;
      return true;
    },
  });
  if (!vals) return;
  const r = await POST("/api/v1/client/bench/compare", vals.body);
  toast(`comparison ${r.id} started`);
  route();
}

function benchRunCard(r) {
  if (r.kind === "compare") {
    const sides = r.sides || [];
    const series = [{ key: "a_output_tok_s", label: sides[0] ? `${sides[0].profile} ${sides[0].revision}` : "first", color: "#6aa7ff" },
      { key: "b_output_tok_s", label: sides[1] ? `${sides[1].profile} ${sides[1].revision}` : "second", color: "#9d7bff" }];
    return `<div class="card" data-key="bench:${esc(r.id)}"><div class="card-head"><h2>Comparison <span class="mono small muted">${esc(r.id)}</span></h2><div class="chip-row">${benchState(r.state)}${benchLive(r.state) ? `<button class="btn sm danger" data-act="bench-cancel" data-id="${esc(r.id)}">Cancel</button>` : ""}</div></div>
      <p class="small">${esc(r.message || "")}</p>
      ${r.current && r.current.points?.length ? `<p class="small muted">measured so far: ${r.current.points.map(p => `c${esc(p.concurrency)} ${esc(num(p.output_tok_s, 0))} tok/s`).join(" · ")}</p>` : ""}
      ${(r.table || []).length ? benchBars(r.table, series) : ""}
      ${sides.map(s => `<p class="small">${esc(s.profile)} ${esc(s.revision)}: ${s.valid ? "" : tag("invalid", "warn") + " "}${esc(s.summary?.text || "")}${s.result_id ? ` · <a href="#/bench/${enc(s.result_id)}">stored</a>` : ""}</p>`).join("")}</div>`;
  }
  const pts = r.points || [];
  return `<div class="card" data-key="bench:${esc(r.id)}"><div class="card-head"><h2>${esc((BENCH_PRESETS.find(p => p[0] === r.preset) || [, r.preset])[1])} · ${esc(r.alias)} <span class="mono small muted">${esc(r.id)}</span></h2><div class="chip-row">${benchState(r.state)}${benchLive(r.state) ? `<button class="btn sm danger" data-act="bench-cancel" data-id="${esc(r.id)}">Cancel</button>` : ""}</div></div>
    <p class="small">${esc(r.message || "")}</p>
    ${pts.length ? benchBars(pts, [{ key: "output_tok_s", label: "output tok/s", color: "#6aa7ff" }]) : ""}
    ${r.uploaded ? `<p class="small">Stored on node A: <a href="#/bench/${enc(r.uploaded)}">${esc(r.uploaded)}</a></p>` : ""}
    ${r.upload_error ? `<p class="small err">${esc(r.upload_error)}</p>` : ""}</div>`;
}

async function viewBench() {
  const g = S.gen;
  const profile = sessionStorage.getItem("tsm_bench_profile") || "";
  const draw = async () => {
    const fresh = newest();
    const [stored, local, profiles] = await Promise.all([
      GET("/api/v1/benchmarks" + qs({ limit: 60, profile })),
      S.client ? GET("/api/v1/client/bench").catch(() => null) : Promise.resolve(null),
      GET("/api/v1/profiles?summary=true").catch(() => []),
    ]);
    if (!fresh()) return;
    const runs = local ? local.runs : [];
    const head = `<div class="view-head"><div><p class="eyebrow">Measure · compare · keep the evidence</p><h1>Benchmarks</h1><p>Throughput and latency measured through the gateway, like your apps' requests, and kept against the exact revision that served them.</p></div>
      ${S.client ? `<div class="row"><button class="btn primary" data-act="bench-run">${icon("play")}Run a benchmark</button><button class="btn" data-act="bench-compare">Compare two revisions</button></div>` : ""}</div>`;
    const how = S.client ? "" : `<div class="card"><h2>Running a benchmark</h2><p class="small muted">Runs start where the requests come from: in the TwinSpark client on your workstation (this page, with a Run button), or on node A:</p>${codeBlock("tsm bench run max-throughput --alias default\ntsm bench run latency-slo --slo-ttft-ms 2000 --slo-tpot-ms 80\ntsm bench run fixed --levels 1,4,16   # the same levels every time, to compare revisions")}</div>`;
    const table = stored.length ? `<div class="table-wrap"><table><thead><tr><th>When</th><th>Profile · revision</th><th>Preset</th><th>Model</th><th>Result</th></tr></thead><tbody>
      ${stored.map(b => `<tr class="clickable" data-key="stored:${esc(b.id)}" data-act="bench-open" data-id="${esc(b.id)}"><td class="nowrap">${esc(ago(b.created_at))}</td><td>${esc(b.profile || "—")} <span class="mono small">${esc(b.label || "")}</span></td><td>${esc(b.preset)}</td><td>${esc(b.alias || "")}</td>
        <td>${b.valid ? "" : tag("invalid", "warn") + " "}${esc(b.summary?.text || b.invalid_reason || "")}</td></tr>`).join("")}
      </tbody></table></div>` : empty("No stored results yet", S.client ? " — run one with the button above." : "");
    const filter = `<select id="bench-profile" style="width:auto"><option value="">all profiles</option>${profiles.map(p => `<option value="${esc(p.name)}" ${p.name === profile ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select>`;
    renderLive(g, `<div class="view" data-live="bench">${head}${how}
      ${runs.length ? `<h2 class="section-title">This session</h2>${runs.map(benchRunCard).join("")}` : ""}
      <div class="card"><div class="card-head"><h2>Stored on node A</h2>${filter}</div>
      <p class="small muted">Evidence class <b>measured by the client</b>: real requests through the gateway, reported by the client that sent them. They appear in the revision's validation record.</p>${table}</div></div>`, "bench");
    const sel = $("#bench-profile"); if (sel) sel.onchange = () => { sessionStorage.setItem("tsm_bench_profile", sel.value); route(); };
    return true;
  };
  onAct({
    "bench-open": (el) => go("#/bench/" + enc(el.dataset.id)),
    "bench-run": () => benchRunForm(),
    "bench-compare": () => benchCompareForm(),
    "bench-cancel": async (el) => { await POST(`/api/v1/client/bench/${enc(el.dataset.id)}/cancel`); toast("cancelling"); draw(); },
  });
  await draw(); poll(draw, S.client ? 2500 : 15000);
}

async function viewBenchResult(id) {
  const b = await GET("/api/v1/benchmarks/" + enc(id));
  const res = b.result || {};
  const ctx = (res.ladder || []).length > 0;
  const tele = Object.entries(res.telemetry || {});
  const o = res.options || {};
  render(S.gen, `<div class="view"><div class="view-head"><div><div class="crumbs"><a href="#/bench">Benchmarks</a> /</div><h1>${esc(b.profile || "?")} <span class="mono">${esc(b.label || "")}</span> · ${esc(b.preset)}</h1><p>${esc(b.summary?.text || "")}</p></div>
      <div class="row">${b.profile ? `<a class="btn" href="#/profiles/${enc(b.profile)}">Profile →</a>` : ""}</div></div>
    ${b.valid ? "" : `<div class="callout warn"><b>Not a valid measurement:</b> ${esc(b.invalid_reason || "")}</div>`}
    <div class="card"><div class="kv">
      <b>Revision</b><span class="mono">${esc(b.revision_id || "—")}</span>
      <b>Model alias</b><span>${esc(b.alias || "—")}</span>
      <b>Evidence</b><span>${esc(b.evidence || "")} · client ${esc(res.client_version || "?")}</span>
      <b>Workload</b><span>${num(o.input_tokens)} prompt / ${num(o.output_tokens)} generated tokens per request · ${esc(o.cache || "cold")} cache · seed ${esc(res.seed ?? "—")}</span>
      <b>Deployed context</b><span>${num(res.max_model_len)} tokens</span>
      <b>When</b><span>${esc(when(res.started_at))} · took ${esc(dur(res.started_at, res.finished_at))}</span>
    </div></div>
    ${(res.points || []).length ? `<div class="card"><h2>Output throughput</h2>${benchBars(ctx ? res.ladder.map(p => ({ ...p, concurrency: `${p.concurrency} @ ${num(p.context)}` })) : res.points, [{ key: "output_tok_s", label: "output tok/s", color: "#6aa7ff" }])}</div>
      <div class="card"><h2>Every level</h2>${benchPointsTable(ctx ? res.ladder : res.points, ctx)}</div>` : ""}
    ${(res.needles || []).length ? `<div class="card"><h2>Retrieval in long prompts</h2><div class="table-wrap"><table><thead><tr><th>Context</th><th>Found</th><th>Score</th></tr></thead><tbody>${res.needles.map(n => `<tr><td>${num(n.context_tokens)}</td><td>${num(n.found)} / ${num(n.needles)}</td><td>${tag(Math.round((n.score || 0) * 100) + " %", n.score >= 1 ? "good" : n.score >= 0.6 ? "warn" : "bad")}</td></tr>`).join("")}</tbody></table></div></div>` : ""}
    ${(res.checks || []).length ? `<div class="card"><h2>Tool calls</h2>${res.checks.map(c => `<p class="small">${tag(c.status, { pass: "good", warn: "warn", fail: "bad" }[c.status] || "muted")} ${esc(c.title || c.id || "")}: ${esc(c.detail || "")}</p>`).join("")}</div>` : ""}
    ${tele.length ? `<div class="card"><h2>Nodes during the run</h2><div class="kv">${tele.map(([n, t]) => `<b>Node ${esc(n)}</b><span>lowest free memory ${gib(t.min_mem_available_gib)} · mean GPU power ${num(t.mean_power_w)} W${t.energy_wh != null ? ` · ${num(t.energy_wh, 1)} Wh` : ""}</span>`).join("")}</div></div>` : ""}
  </div>`);
}
