/* TwinSpark Manager — "Keys & usage" page (master plan §4.1). Loaded after app.js, before client.js.
   One inference key per app, limited to aliases, expirable, revocable; tokens per key and alias per day.
   A new key is shown once; node A keeps only its hash. Everything dynamic goes through esc(). */
"use strict";

Object.assign(ICON, {
  keys: '<path d="M14 10a4 4 0 1 1-1.2-2.8L21 15v3h-3v-2h-2v-2h-2zM7.5 11.5h.01" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
});
NAV.splice(Math.max(0, NAV.findIndex(n => n[0] === "integrations") + 1), 0, ["keys", "Keys & usage"]);
ROUTES.push([/^\/keys$/, () => viewKeys()]);

const keyState = (s) => tag(s, { active: "good", expired: "warn", revoked: "muted" }[s] || "muted");
function keyDate(t) { return t ? new Date(t * 1000).toLocaleDateString() : "—"; }

function keysTable(keys) {
  if (!keys.length) return empty("No keys yet", " — the gateway is open only on a loopback listener without one.");
  return `<div class="table-wrap"><table><thead><tr><th>Name</th><th>App</th><th>Key</th><th>Aliases</th><th>State</th><th>Created</th><th>Expires</th><th>Last used</th><th></th></tr></thead><tbody>
    ${keys.map(k => `<tr data-key="key:${esc(k.id)}"><td><b>${esc(k.name)}</b>${k.migrated ? `<div class="sub">the key from before 0.7</div>` : ""}</td><td>${esc(k.app || "")}</td>
      <td class="mono">${esc(k.prefix)}…</td><td>${(k.aliases || []).length ? k.aliases.map(a => tag(a, "info")).join(" ") : `<span class="muted">all</span>`}</td>
      <td>${keyState(k.state)}</td><td class="nowrap">${esc(keyDate(k.created_at))}</td><td class="nowrap">${esc(keyDate(k.expires_at))}</td><td class="nowrap">${esc(ago(k.last_used_at))}</td>
      <td class="nowrap">${k.state === "active" ? `<button class="btn sm danger" data-act="key-revoke" data-id="${esc(k.id)}" data-name="${esc(k.name)}" data-migrated="${k.migrated ? 1 : ""}">Revoke</button>` : `<button class="btn sm" data-act="key-delete" data-id="${esc(k.id)}">Delete</button>`}</td></tr>`).join("")}
  </tbody></table></div>`;
}

/** per key: requests and tokens over the period, as bars; per day: completion tokens as a sparkline */
function usageHtml(u) {
  if (!u || !(u.rows || []).length) return empty("No requests in this period");
  const days = [...new Set(u.rows.map(r => r.day))].sort();
  const perDay = days.map(d => u.rows.filter(r => r.day === d).reduce((n, r) => n + r.completion_tokens, 0));
  const byKey = {};
  for (const r of u.rows) {
    const k = byKey[r.key_id + "|" + r.alias] = byKey[r.key_id + "|" + r.alias] || { key: r.key_id, alias: r.alias, requests: 0, prompt: 0, completion: 0, estimated: 0 };
    k.requests += r.requests; k.prompt += r.prompt_tokens; k.completion += r.completion_tokens; k.estimated += r.estimated_requests;
  }
  const rows = Object.values(byKey).sort((a, b) => b.completion + b.prompt - a.completion - a.prompt);
  const max = Math.max(1, ...rows.map(r => r.prompt + r.completion));
  return `<div class="tiles" style="margin-bottom:12px">${tile("Completion tokens per day", perDay[perDay.length - 1], "", perDay, "#6aa7ff")}${tile("Requests", rows.reduce((n, r) => n + r.requests, 0), "", null)}</div>
    <div class="table-wrap"><table><thead><tr><th>Key</th><th>Alias</th><th class="num">Requests</th><th class="num">Prompt tokens</th><th class="num">Completion tokens</th><th></th></tr></thead><tbody>
    ${rows.map(r => `<tr><td>${esc((u.names || {})[r.key] || r.key)}</td><td class="mono">${esc(r.alias)}</td><td class="num">${num(r.requests)}</td><td class="num">${num(r.prompt)}</td><td class="num">${num(r.completion)}${r.estimated ? `<div class="sub">${num(r.estimated)} estimated</div>` : ""}</td>
      <td style="min-width:120px"><div class="bench-bar"><i style="width:${((r.prompt + r.completion) / max * 100).toFixed(1)}%;background:#6aa7ff"></i></div></td></tr>`).join("")}
    </tbody></table></div>
    <p class="small muted">Counted by the gateway from each response's usage. A stream that did not ask for <span class="mono">stream_options.include_usage</span> is counted by its events (estimated, no prompt tokens).</p>`;
}

async function newKeyFlow() {
  const st = await GET("/api/v1/status").catch(() => ({}));
  const aliases = [...new Set((st.routes || []).map(r => r.alias))];
  const vals = await formBox("New inference key", [
    { id: "name", label: "Name (who or what uses it)", value: "", placeholder: "OpenClaw on the laptop" },
    { id: "aliases", label: "Only these aliases (comma-separated; empty = all)", value: "", placeholder: aliases.join(", ") || "default, coder" },
    { id: "days", label: "Expires after (days; empty = never)", value: "" },
    { id: "app", label: "App (optional)", value: "" },
  ], { label: "Create",
    validate: (v) => {
      v.body = { name: v.name, aliases: v.aliases ? v.aliases.split(",").map(s => s.trim()).filter(Boolean) : [],
        expires_in_days: v.days ? Number(v.days) : null, app: v.app || null };
      if (!v.name) throw new Error("give the key a name");
      if (v.days && (!Number.isInteger(v.body.expires_in_days) || v.body.expires_in_days < 1)) throw new Error("expiry: whole days, 1 or more");
      return true;
    } });
  if (!vals) return;
  const r = await POST("/api/v1/keys", vals.body);
  await modal({ title: `Key for ${r.name}`, wide: true,
    body: `<p><b>Copy it now: it is shown this once.</b> Node A keeps only its hash; if it is lost, revoke it and create another.</p>${codeBlock(r.key)}
      <p class="small muted">${r.aliases.length ? "Only for " + esc(r.aliases.join(", ")) : "For every alias"}${r.expires_at ? " · expires " + esc(keyDate(r.expires_at)) : ""}. Use it as the API key of the OpenAI-compatible endpoint.</p>`,
    actions: [{ label: "I copied it", value: true, cls: "primary" }] });
}

async function viewKeys() {
  const g = S.gen;
  const days = Number(sessionStorage.getItem("tsm_usage_days") || 30);
  const draw = async () => {
    const fresh = newest();
    const [keys, u] = await Promise.all([GET("/api/v1/keys"), GET("/api/v1/usage" + qs({ days }))]);
    if (!fresh()) return;
    renderLive(g, `<div class="view" data-live="keys"><div class="view-head"><div><p class="eyebrow">Access · usage</p><h1>Keys &amp; usage</h1><p>One key per app: limited to the aliases it needs, revocable without touching the others.</p></div>
      <div class="row"><button class="btn primary" data-act="key-new">New key</button></div></div>
      <div class="card">${keysTable(keys)}</div>
      <div class="card"><div class="card-head"><h2>Tokens per key and alias</h2><div class="row"><select id="usage-days" style="width:auto">${[7, 30, 90].map(d => `<option value="${d}" ${d === days ? "selected" : ""}>last ${d} days</option>`).join("")}</select><button class="btn sm" data-act="usage-csv">CSV</button></div></div>${usageHtml(u)}</div></div>`, "keys");
    const sel = $("#usage-days"); if (sel) sel.onchange = () => { sessionStorage.setItem("tsm_usage_days", sel.value); route(); };
  };
  onAct({
    "key-new": async () => { await newKeyFlow(); draw(); },
    "key-revoke": async (el) => {
      const warn = el.dataset.migrated ? " This is the key from before 0.7: apps configured with the old inference key stop working." : "";
      if (!await confirmBox(`Revoke ${el.dataset.name}?`, `Requests with this key get 401 from now on; every other key keeps working.${esc(warn)}`, { label: "Revoke", danger: true })) return;
      await POST(`/api/v1/keys/${enc(el.dataset.id)}/revoke`); toast("revoked"); draw();
    },
    "key-delete": async (el) => { await DEL(`/api/v1/keys/${enc(el.dataset.id)}`); toast("deleted"); draw(); },
    "usage-csv": async () => downloadIntegrationFile({ name: `twinspark-usage-${days}d.csv`, content: await apiText(`/api/v1/usage?days=${days}&format=csv`) }),
  });
  await draw(); poll(draw, 15000);
}
