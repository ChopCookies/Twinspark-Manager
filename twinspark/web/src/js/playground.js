/* TwinSpark Manager — "Playground" page (master plan §4.1). Loaded after app.js, before client.js.
   Chat with a served alias, or two side by side, through /api/v1/playground (the management session; no
   app key needed). Shows time to first token and tokens per second as an app sees them, and the same
   request as curl for an app's key. Everything dynamic goes through esc(). */
"use strict";

Object.assign(ICON, {
  playground: '<path d="M4 5h16v11H9l-5 4zM8 9h8M8 12h5" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
});
NAV.splice(Math.max(0, NAV.findIndex(n => n[0] === "integrations")), 0, ["playground", "Playground"]);
ROUTES.push([/^\/playground$/, () => viewPlayground()]);

/** Split a server-sent-events buffer into the data payloads of complete events, and the unfinished rest. */
function sseEvents(buffer) {
  const out = [];
  let rest = buffer.replace(/\r\n/g, "\n"), i;
  while ((i = rest.indexOf("\n\n")) >= 0) {
    const block = rest.slice(0, i); rest = rest.slice(i + 2);
    const data = block.split("\n").filter(l => l.startsWith("data:")).map(l => l.slice(5).replace(/^ /, "")).join("\n");
    if (data) out.push(data);
  }
  return { events: out, rest };
}

/** TTFT, decode rate and totals from timestamps (ms) and the stream's usage, when it sent one. */
function playgroundStats({ t0, first, end, usage, chunks }) {
  const completion = usage?.completion_tokens ?? chunks;
  const decodeS = first != null && end > first ? (end - first) / 1000 : 0;
  return {
    ttft_ms: first == null ? null : Math.round(first - t0),
    tok_s: decodeS > 0 && completion > 1 ? Math.round((completion - 1) / decodeS * 10) / 10 : null,
    completion_tokens: completion, prompt_tokens: usage?.prompt_tokens ?? null,
    total_ms: Math.round(end - t0), estimated: !usage,
  };
}

/** The request as curl, for an app: the key comes from the environment, never from this page. */
function playgroundCurl(baseUrl, body) {
  const base = (baseUrl || "http://<node-a>:8000/v1").replace(/\/+$/, "");
  const json = JSON.stringify({ ...body, stream: false, stream_options: undefined }, null, 2).replace(/'/g, "'\\''");
  return `curl ${base}/chat/completions \\\n  -H "Authorization: Bearer $TWINSPARK_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '${json}'`;
}

function playgroundRequest(alias, messages, params) {
  const body = { model: alias, messages, stream: true, stream_options: { include_usage: true } };
  for (const [k, v] of Object.entries(params || {})) if (v !== "" && v !== null && v !== undefined && Number.isFinite(+v)) body[k] = +v;
  return body;
}

/** One streamed answer. onDelta(text, reasoning) on every piece; resolves to {text, reasoning, stats}. */
async function streamChat(body, onDelta, signal) {
  const headers = { "Content-Type": "application/json" };
  if (S.key) headers["x-api-key"] = S.key;
  const t0 = performance.now();
  let r;
  try { r = await fetch("/api/v1/playground/v1/chat/completions", { method: "POST", headers, body: JSON.stringify(body), signal }); }
  catch (e) { if (e.name === "AbortError") throw e; throw new ApiError("controller not reachable", 0); }
  if (r.status === 401) throw unauthorized(r);
  if (!r.ok) {
    const data = await r.json().catch(() => null);
    throw new ApiError(data?.error?.message || data?.detail || `HTTP ${r.status}`, r.status, data);
  }
  const reader = r.body.getReader(), dec = new TextDecoder();
  let buf = "", text = "", reasoning = "", first = null, usage = null, chunks = 0, error = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    const parsed = sseEvents(buf + dec.decode(value, { stream: true }));
    buf = parsed.rest;
    for (const data of parsed.events) {
      if (data === "[DONE]") continue;
      let j; try { j = JSON.parse(data); } catch (_) { continue; }
      if (j.error) { error = j.error.message || String(j.error); continue; }
      if (j.usage) usage = j.usage;
      const delta = j.choices?.[0]?.delta || {};
      const piece = delta.content || "", think = delta.reasoning_content || delta.reasoning || "";
      if (!piece && !think) continue;
      if (first === null) first = performance.now();
      chunks++; text += piece; reasoning += think;
      onDelta(text, reasoning);
    }
  }
  const stats = playgroundStats({ t0, first, end: performance.now(), usage, chunks });
  if (error) throw new ApiError(error, 502);
  return { text, reasoning, stats };
}

function playgroundStatsHtml(st) {
  if (!st) return "";
  const parts = [["Time to first token", st.ttft_ms == null ? "—" : `${num(st.ttft_ms)} ms`], ["Decode", st.tok_s == null ? "—" : `${st.tok_s} tok/s`],
    ["Tokens", `${num(st.completion_tokens)}${st.estimated ? " (counted)" : ""}${st.prompt_tokens != null ? ` · prompt ${num(st.prompt_tokens)}` : ""}`], ["Total", `${(st.total_ms / 1000).toFixed(1)} s`]];
  return `<div class="pg-stats">${parts.map(([k, v]) => `<span><small>${esc(k)}</small><b>${esc(v)}</b></span>`).join("")}</div>`;
}

function playgroundTurns(column) {
  return column.turns.map(t => t.role === "user" ? `<div class="pg-msg user"><div class="pg-role">You</div><div class="pg-text">${esc(t.content)}</div></div>`
    : `<div class="pg-msg assistant"><div class="pg-role">${esc(column.alias)}</div>${t.reasoning ? `<details class="pg-think"><summary>Reasoning</summary><div class="pg-text">${esc(t.reasoning)}</div></details>` : ""}<div class="pg-text">${esc(t.content)}</div>${t.error ? `<div class="callout warn"><div>${esc(t.error)}</div></div>` : ""}${playgroundStatsHtml(t.stats)}</div>`).join("")
    || `<div class="empty small">Ask something to see how ${esc(column.alias || "the model")} answers and how fast.</div>`;
}

async function viewPlayground() {
  const g = S.gen;
  const info = await GET("/api/v1/integrations");
  if (!current(g)) return;
  const aliases = (info.aliases || []).filter(a => a.status === "serving");
  const state = S.playground || (S.playground = { compare: false, columns: [{ alias: "", turns: [] }, { alias: "", turns: [] }], system: "", temperature: "", max_tokens: "1024" });
  state.columns.forEach((c, i) => { if (!aliases.some(a => a.alias === c.alias)) c.alias = (aliases[i] || aliases[0])?.alias || ""; });
  const base = (S.client && S.clientStatus?.endpoint?.base_url) || info.gateway?.suggested_base_url || "";
  const head = `<div class="view-head"><div><p class="eyebrow">Try a model</p><h1>Playground</h1><p>Chat with what is being served, as an app would: streamed through the gateway, with time to first token and tokens per second.</p></div></div>`;
  if (!aliases.length) {
    render(g, `<div class="view">${head}${empty("Nothing is being served", "Switch to a profile first — the Playground talks to the models that are running.")}</div>`);
    return;
  }
  const options = (sel) => aliases.map(a => `<option value="${esc(a.alias)}" ${a.alias === sel ? "selected" : ""}>${esc(a.alias)} · ${esc(a.profile || "")}</option>`).join("");
  const cols = state.compare ? state.columns : state.columns.slice(0, 1);
  if (!render(g, `<div class="view">${head}
    <div class="card"><div class="form pg-form">
      ${cols.map((c, i) => `<label class="field"><span>${state.compare ? (i ? "Right" : "Left") : "Model"}</span><select data-col="${i}" class="pg-alias">${options(c.alias)}</select></label>`).join("")}
      <label class="field"><span>Temperature</span><input id="pg-temp" inputmode="decimal" placeholder="the profile's" value="${esc(state.temperature)}"/></label>
      <label class="field"><span>Max tokens</span><input id="pg-max" inputmode="numeric" value="${esc(state.max_tokens)}"/></label>
    </div>
    <label class="check" style="margin-top:8px"><input type="checkbox" id="pg-compare" ${state.compare ? "checked" : ""} ${aliases.length < 2 && !state.compare ? "" : ""}/> Two side by side (the same question to both)</label>
    <details style="margin-top:8px" ${state.system ? "open" : ""}><summary>System prompt</summary><textarea id="pg-system" rows="3" spellcheck="false">${esc(state.system)}</textarea></details></div>
    <div class="grid ${state.compare ? "cols-2" : ""} pg-cols">${cols.map((c, i) => `<div class="card pg-col"><div class="card-head"><h2 class="mono">${esc(c.alias)}</h2><div class="row"><button class="btn sm ghost" data-act="pg-curl" data-col="${i}">Copy as curl</button><button class="btn sm ghost" data-act="pg-clear" data-col="${i}">Clear</button></div></div><div class="pg-log" id="pg-log-${i}">${playgroundTurns(c)}</div></div>`).join("")}</div>
    <div class="card pg-input"><textarea id="pg-msg" rows="3" placeholder="Message — Ctrl+Enter sends"></textarea>
      <div class="row end"><button class="btn" data-act="pg-stop" disabled>Stop</button><button class="btn primary" data-act="pg-send">Send</button></div></div>
  </div>`)) return;

  const remember = () => {
    state.temperature = $("#pg-temp").value.trim(); state.max_tokens = $("#pg-max").value.trim(); state.system = $("#pg-system").value;
  };
  $$(".pg-alias").forEach(el => { el.onchange = () => { state.columns[+el.dataset.col].alias = el.value; state.columns[+el.dataset.col].turns = []; remember(); viewPlayground(); }; });
  $("#pg-compare").onchange = (e) => { state.compare = e.target.checked; remember(); viewPlayground(); };
  $("#pg-msg").onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $('[data-act="pg-send"]').click(); } };
  let running = null;
  const messagesFor = (c) => {
    const sys = $("#pg-system").value.trim();
    return [...(sys ? [{ role: "system", content: sys }] : []), ...c.turns.filter(t => !t.error).map(t => ({ role: t.role, content: t.content }))];
  };
  onAct({
    "pg-send": async () => {
      const text = $("#pg-msg").value.trim();
      if (!text || running) return;
      remember();
      const params = { temperature: state.temperature, max_tokens: state.max_tokens };
      running = new AbortController();
      $('[data-act="pg-stop"]').disabled = false; $('[data-act="pg-send"]').disabled = true;
      $("#pg-msg").value = "";
      await Promise.all(cols.map(async (c, i) => {
        c.turns.push({ role: "user", content: text });
        const body = playgroundRequest(c.alias, messagesFor(c), params);
        const turn = { role: "assistant", content: "", reasoning: "" };
        c.turns.push(turn);
        const log = () => { const el = $(`#pg-log-${i}`); if (el && current(g)) { el.innerHTML = playgroundTurns(c); el.scrollTop = el.scrollHeight; } };
        log();
        try {
          const res = await streamChat(body, (t, r) => { turn.content = t; turn.reasoning = r; log(); }, running.signal);
          Object.assign(turn, { content: res.text, reasoning: res.reasoning, stats: res.stats });
        } catch (e) {
          turn.error = e.name === "AbortError" ? "stopped" : e.message;
        }
        log();
      }));
      running = null;
      if (current(g)) { $('[data-act="pg-stop"]').disabled = true; $('[data-act="pg-send"]').disabled = false; $("#pg-msg").focus(); }
    },
    "pg-stop": () => { if (running) running.abort(); },
    "pg-clear": (el) => { state.columns[+el.dataset.col].turns = []; viewPlayground(); },
    "pg-curl": async (el) => {
      const c = cols[+el.dataset.col];
      remember();
      const last = [...c.turns].reverse().find(t => t.role === "user");
      const body = playgroundRequest(c.alias, [...messagesFor(c).filter(m => m.role === "system"), { role: "user", content: last?.content || "Hello!" }], { temperature: state.temperature, max_tokens: state.max_tokens });
      const text = playgroundCurl(base, body);
      try { await navigator.clipboard.writeText(text); toast("curl command copied — set TWINSPARK_API_KEY to an app key (Keys & usage)", "good"); }
      catch (_) { await modal({ title: "The request as curl", wide: true, body: codeBlock(text) }); }
    },
  });
}
