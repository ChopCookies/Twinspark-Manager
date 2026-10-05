/* TwinSpark Manager — "Get started" checklist.
   Loaded after app.js; adds one route and one nav entry. Everything dynamic goes through esc(). */
"use strict";

Object.assign(ICON, {
  start: '<path d="M5 21V4m0 0h11l-2 4 2 4H5" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
});
NAV.unshift(["start", "Get started"]);
ROUTES.unshift([/^\/start$/, () => viewStart()]);

const START_MARK = { done: "✓", todo: "", warn: "!", blocked: "✗", optional: "–" };
const START_TAG = { done: ["done", "good"], todo: ["next", "info"], warn: ["check", "warn"], blocked: ["blocked", "bad"], optional: ["optional", "muted"] };

function startStep(s, isNext) {
  let [label, cls] = START_TAG[s.status] || ["", "muted"];
  if (s.status === "todo" && !isNext) [label, cls] = [s.required ? "later" : "suggested", "muted"];
  const fix = s.fix || {};
  const actions = [];
  if (fix.href) actions.push(`<a class="btn ${isNext ? "primary" : ""} sm" href="${esc(fix.href)}">${esc(fix.label || "Open")}</a>`);
  return `<div class="card start-step ${esc(s.status)} ${isNext ? "accent" : ""}">
    <div class="start-mark ${esc(s.status)}" aria-hidden="true">${START_MARK[s.status] ?? ""}</div>
    <div class="grow">
      <div class="title">${esc(s.title)} ${tag(label, cls)}${s.required ? "" : tag("not required", "muted")}</div>
      <div class="small muted">${esc(s.detail)}</div>
      ${fix.command ? codeBlock(fix.command) : ""}
    </div>
    <div class="row">${actions.join("")}</div>
  </div>`;
}

function startHtml(ob) {
  const pct = ob.total ? Math.round(100 * ob.done / ob.total) : 100;
  let html = `<div class="view"><div class="view-head"><div><h1>Get started</h1>
    <p>${ob.complete ? "Everything required is done. Your cluster is ready." : `${ob.done} of ${ob.total} required steps done — the highlighted step is next.`}</p></div>
    <div class="row"><a class="btn" href="#/diagnostics/doctor">Full health check</a></div></div>
    <div class="progress-line" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><i style="width:${pct}%"></i></div>`;
  if (ob.demo) html += `<div class="callout info"><div><b>This is <span class="mono">tsm demo</span>.</b> Two simulated Sparks run on this computer. The commands below are what you would run on real Sparks; nothing here needs them (and <span class="mono">sudo</span> commands would act on this computer).</div></div>`;
  else if (ob.dry_run) html += `<div class="callout warn"><div><b>Dry-run mode.</b> Activations are simulated and nothing starts on the Sparks. That is the safe way to rehearse; use the “Real containers” step when you are ready.</div></div>`;
  html += `<div class="start-list">${ob.steps.map(s => startStep(s, s.id === ob.next)).join("")}</div></div>`;
  return html;
}

async function viewStart() {
  const g = S.gen;
  const draw = async () => render(g, startHtml(await GET("/api/v1/system/onboarding")));
  onAct({});
  await draw();
  every(() => draw().catch(() => { }), 10000);
}
