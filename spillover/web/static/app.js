/* Spillover UI — vanilla JS SPA. No frameworks, no CDN: the tool runs offline. */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const state = {
  accounts: [],
  authStatus: {},
  interrupted: [],
  demo: false,
  route: "accounts",
  rebalance: { step: 1, source: null, summary: null, files: [], selected: new Set(), plan: null, jobId: null },
  dup: { data: null, open: -1, keep: {}, deleted: new Set() },
  takeout: { items: [], summary: null, dests: new Set() },
  photos: { alias: null, session_id: null, items: [], dests: new Set() },
};

/* ---------- helpers ---------- */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n >= 1024 ** 3) return trimNum(n / 1024 ** 3) + " GB";
  if (n >= 1024 ** 2) return trimNum(n / 1024 ** 2) + " MB";
  if (n >= 1024) return trimNum(n / 1024) + " KB";
  return n + " B";
}

function trimNum(x) {
  const v = parseFloat(x.toFixed(2));
  return v >= 100 ? Math.round(v).toString() : v.toString();
}

function fmtBytesShort(n) {
  n = Number(n) || 0;
  if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(1) + " GB";
  if (n >= 1024 ** 2) return Math.round(n / 1024 ** 2) + " MB";
  return Math.round(n / 1024) + " KB";
}

function pct(used, total) {
  if (!total) return 0;
  return Math.min(100, (used / total) * 100);
}

function quotaClass(p) {
  return p >= 95 ? "critical" : p >= 85 ? "hot" : "";
}

function kindOf(mime) {
  if (mime.startsWith("video/")) return "Video";
  if (mime.startsWith("image/")) return "Photo";
  if (mime.startsWith("audio/")) return "Audio";
  if (mime.includes("pdf") || mime.includes("document") || mime.includes("text")) return "Doc";
  if (mime.includes("zip") || mime.includes("archive")) return "Archive";
  return "File";
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

function toast(msg, kind = "ok") {
  const root = $("#toast-root");
  const el = document.createElement("div");
  el.className = "toast" + (kind === "error" ? " error" : "");
  el.innerHTML = `<span class="dot" aria-hidden="true"></span><span>${esc(msg)}</span>`;
  root.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

/** Poll a /api/jobs/{id} job until it finishes. Resolves the job object. */
async function pollJob(job_id, intervalMs = 900, onProgress = null) {
  for (;;) {
    const job = await api(`/api/jobs/${job_id}`);
    if (onProgress && job.progress) onProgress(job.progress);
    if (job.status !== "running") return job;
    await new Promise(r => setTimeout(r, intervalMs));
  }
}

function confirmModal({ title, body, warn, confirmLabel, danger = false }) {
  return new Promise(resolve => {
    const root = $("#modal-root");
    root.innerHTML = `
      <div class="modal-backdrop" data-close>
        <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
          <h2 id="modal-title">${esc(title)}</h2>
          <p>${body}</p>
          ${warn ? `<div class="warn-box">${warn}</div>` : ""}
          <div class="modal-actions">
            <button class="btn btn-secondary" data-close>Cancel</button>
            <button class="btn ${danger ? "btn-danger" : "btn-primary"}" id="modal-ok">${esc(confirmLabel)}</button>
          </div>
        </div>
      </div>`;
    const close = v => { root.innerHTML = ""; document.removeEventListener("keydown", onKey); resolve(v); };
    const onKey = e => { if (e.key === "Escape") close(false); };
    document.addEventListener("keydown", onKey);
    const backdrop = root.querySelector(".modal-backdrop");
    backdrop.addEventListener("click", e => { if (e.target === backdrop) close(false); });
    $$(".modal [data-close]", root).forEach(b => b.addEventListener("click", () => close(false)));
    $("#modal-ok", root).addEventListener("click", () => close(true));
    $("#modal-ok", root).focus();
  });
}

/* ---------- router ---------- */

const TITLES = { accounts: "Accounts", rebalance: "Rebalance", duplicates: "Duplicates", takeout: "Takeout", ledger: "Ledger" };

function navigate(route) {
  location.hash = "#/" + route;
}

function currentRoute() {
  const m = location.hash.match(/^#\/(\w+)/);
  return TITLES[m?.[1]] ? m[1] : "accounts";
}

function render() {
  const route = currentRoute();
  state.route = route;
  $$(".nav-item[data-route]").forEach(b => {
    const on = b.dataset.route === route;
    b.classList.toggle("active", on);
    if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  });
  $$(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + route));
  $("#view-name").textContent = route;
  $("#topbar-actions").innerHTML = "";
  ({ accounts: renderAccounts, rebalance: renderRebalance,
     duplicates: renderDuplicates, takeout: renderTakeout, ledger: renderLedger })[route]();
}

window.addEventListener("hashchange", render);

/* ---------- sidebar ---------- */

function renderSidebarTotal() {
  const el = $("#storage-total");
  const withQuota = state.accounts.filter(a => a.quota && a.quota.limit);
  if (!withQuota.length) { el.innerHTML = ""; return; }
  const used = withQuota.reduce((s, a) => s + (a.quota.usage || 0), 0);
  const total = withQuota.reduce((s, a) => s + (a.quota.limit || 0), 0);
  const p = pct(used, total);
  el.innerHTML = `
    <div class="label">Pooled total</div>
    <div class="value">${fmtBytesShort(used)} <span class="of">of ${fmtBytesShort(total)}</span></div>
    <div class="quota-bar ${quotaClass(p)}" role="img" aria-label="${p.toFixed(0)} percent used"><span style="width:${p}%"></span></div>
    <div class="sub">${p.toFixed(0)}% used · ${fmtBytesShort(total - used)} free</div>`;
}

/* ---------- accounts view ---------- */

async function refreshAccounts() {
  try {
    state.accounts = await api("/api/accounts");
  } catch (e) {
    toast("Could not load accounts: " + e.message, "error");
    state.accounts = [];
  }
  try {
    state.authStatus = await api("/api/auth/status");
  } catch (e) { state.authStatus = {}; }
  try {
    state.interrupted = await api("/api/transfers/interrupted");
  } catch (e) { state.interrupted = []; }
  renderSidebarTotal();
}

function authPill(alias) {
  const s = state.authStatus[alias];
  if (!s) return "";
  if (s.needs_reconnect)
    return `<span class="pill pill-danger"><span class="dot"></span>Reconnect needed</span>`;
  if (!s.ok)
    return `<span class="pill pill-warn"><span class="dot"></span>Auth error</span>`;
  return "";
}

function acctCard(a) {
  const q = a.quota || {};
  const err = q.error;
  const p = q.limit ? pct(q.usage, q.limit) : 0;
  return `
  <article class="card card-pad-sm acct-card">
    <div class="acct-top">
      <div class="acct-id">
        <div class="acct-alias">${esc(a.alias)}</div>
        <div class="acct-email" title="${esc(a.email || "")}">${esc(a.email || "—")}</div>
      </div>
      ${p >= 95 ? `<span class="pill pill-danger"><span class="dot"></span>Full</span>`
        : p >= 85 ? `<span class="pill pill-warn"><span class="dot"></span>Nearly full</span>` : ""}
      ${authPill(a.alias)}
    </div>
    ${err ? `<p style="color:var(--danger);font-size:13px">Couldn’t read quota: ${esc(err)}</p>` : `
    <div class="acct-quota">
      <div class="quota-row"><span class="used">${fmtBytes(q.usage)}</span><span class="total">of ${fmtBytes(q.limit)}</span></div>
      <div class="quota-bar ${quotaClass(p)}" role="img" aria-label="${esc(a.alias)} storage ${p.toFixed(0)} percent used"><span style="width:${p}%"></span></div>
      <div class="free"><strong>${fmtBytes(q.free ?? 0)}</strong> free</div>
    </div>`}
    <div class="acct-actions">
      ${state.authStatus[a.alias]?.needs_reconnect
        ? `<button class="btn btn-primary btn-sm" data-reconnect="${esc(a.alias)}">Reconnect</button>` : ""}
      <button class="btn btn-secondary btn-sm" data-rebalance="${esc(a.alias)}">Rebalance from here</button>
      <button class="btn btn-danger-outline btn-sm" data-disconnect="${esc(a.alias)}" title="Sign out and remove this account from Spillover">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="margin-right:4px"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
        Sign out
      </button>
    </div>
  </article>`;
}

/* ---------- getting started guide ---------- */

const SETUP_STEPS = [
  {
    title: "Create a Google Cloud project",
    lines: ["Name it anything — “Spillover” or “My Drive Tools”. Click Create."],
    links: [["console.cloud.google.com/projectcreate", "https://console.cloud.google.com/projectcreate"]],
  },
  {
    title: "Enable the APIs",
    lines: ["Open each one and click Enable:"],
    links: [
      ["Google Drive API", "https://console.cloud.google.com/apis/library/drive.googleapis.com"],
      ["Photos Picker API", "https://console.cloud.google.com/apis/library/photospicker.googleapis.com"],
    ],
  },
  {
    title: "Configure OAuth & Add Test Users",
    lines: [
      "In Google Auth Platform (or APIs & Services › OAuth consent screen), choose External, enter an App name and your email, then click Save/Create.",
      "After creating, go to “Audience” in the left sidebar.",
      "Inside the Audience page, scroll down to the “Test users” section.",
      "Click “+ Add users”, enter your Gmail address (and any other accounts you want to connect), then click Save.",
    ],
    links: [
      ["Google Auth Platform › Audience", "https://console.cloud.google.com/auth/audience"],
      ["APIs & Services › OAuth consent screen", "https://console.cloud.google.com/apis/credentials/consent"],
    ],
  },
  {
    title: "Create credentials",
    lines: [
      "Click Clients in the left sidebar (or APIs & Services › Credentials).",
      "Click Create Credentials › OAuth client ID.",
      "Application type: Desktop app. Name it anything, click Create, then Download JSON from the popup.",
    ],
    links: [
      ["Google Auth Platform › Clients", "https://console.cloud.google.com/auth/clients"],
      ["APIs & Services › Credentials", "https://console.cloud.google.com/apis/credentials"],
    ],
  },
  {
    title: "Save the credentials file",
    lines: ["Rename the download to client_secret.json and move it to the path below."],
    path: true,
  },
];

const HOW_IT_WORKS = [
  "Sign in with Google — Google’s own login page opens in your browser.",
  "Pick the account and click Allow to grant Drive access.",
  "Spillover can then scan that account’s storage and move files between accounts.",
  "Everything stays on this machine. Spillover is only a bridge to the Drive API.",
  "Connect as many accounts as you like — add each one as a test user first.",
];

const REVOKE_OPTIONS = [
  {
    title: "From Spillover",
    lines: ["Click Sign out on an account card. That deletes its refresh token from your keychain."],
  },
  {
    title: "From Google directly",
    lines: ["Find the app under the name you gave your Cloud project and click Remove Access. The token stops working at once."],
    links: [["myaccount.google.com/permissions", "https://myaccount.google.com/permissions"]],
  },
  {
    title: "Delete the Cloud project",
    lines: ["Shut the project down and every credential it ever issued dies with it."],
    links: [["console.cloud.google.com/iam-admin/settings", "https://console.cloud.google.com/iam-admin/settings"]],
  },
];

function sgItems(lines) {
  return lines.map(l => `<li>${l}</li>`).join("");
}

function sgList(lines) {
  // A one-line step reads as prose; bullets only help when there are several.
  return lines.length === 1 ? `<p class="sg-p">${lines[0]}</p>` : `<ul>${sgItems(lines)}</ul>`;
}

function sgLinks(links) {
  return (links || []).map(([label, href]) =>
    `<a class="sg-link" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(label)} ↗</a>`).join("");
}

/**
 * The guide renders in three shapes depending on setup state:
 * forced-open (no client_secret.json — nothing else works yet),
 * banner (credentials present, no accounts), collapsed (returning user).
 */
function setupGuideMarkup(s) {
  const forced = !s.exists;
  const banner = s.exists && !s.accounts;
  const open = forced || banner;
  const steps = SETUP_STEPS.map((st, i) => `
    <li class="sg-step">
      <span class="sg-n" aria-hidden="true">${i + 1}</span>
      <div class="sg-step-body">
        <h5>${esc(st.title)}</h5>
        ${sgList(st.lines.map(esc))}
        ${st.path ? `
          <div class="sg-path">
            <code id="sg-path-text">${esc(s.path)}</code>
            <button class="btn btn-secondary btn-sm" id="sg-copy-path" type="button">Copy path</button>
          </div>
          <pre class="sg-cmd">mkdir -p ~/.spillover
mv ~/Downloads/client_secret.json ~/.spillover/client_secret.json</pre>` : ""}
        ${sgLinks(st.links)}
      </div>
    </li>`).join("");

  return `
  <section class="setup-guide${forced ? " forced" : ""}" id="setup-guide" aria-label="Getting started">
    <details${open ? " open" : ""}>
      <summary class="sg-summary">
        <span class="sg-titles">
          <span class="sg-title">${forced ? "First-time setup" : banner ? "Almost ready" : "Setup guide"}</span>
          <span class="sg-sub">${forced
            ? "Spillover runs locally and is not Google-verified, so it uses your own Cloud project. About 2 minutes, once."
            : banner ? "client_secret.json is in place — sign in below to connect your first account."
            : "How the Cloud project, the consent screen and your keychain fit together."}</span>
        </span>
        <span class="sg-toggle" aria-hidden="true"></span>
      </summary>
      <div class="sg-body">
        <details class="sg-item" open>
          <summary>Connect your Google accounts</summary>
          <ol class="sg-steps">${steps}</ol>
          <button class="btn btn-primary" id="setup-done-btn" type="button">I’ve saved client_secret.json — Reload</button>
        </details>
        <details class="sg-item">
          <summary>How it works</summary>
          <ol class="sg-lines">${sgItems(HOW_IT_WORKS.map(esc))}</ol>
        </details>
        <details class="sg-item">
          <summary>Remove access when you’re done</summary>
          <div class="sg-revoke">
            ${REVOKE_OPTIONS.map(o => `
              <div class="sg-opt">
                <h5>${esc(o.title)}</h5>
                ${sgList(o.lines.map(esc))}
                ${sgLinks(o.links)}
              </div>`).join("")}
            <p class="sg-foot">Your files are never modified or deleted unless you run a transfer yourself. The Ledger keeps a local record of every file Spillover has touched.</p>
          </div>
        </details>
      </div>
    </details>
  </section>`;
}

function wireSetupGuide(setup) {
  const copy = $("#sg-copy-path");
  if (copy && setup) copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(setup.client_secret_path);
      copy.textContent = "Copied";
      setTimeout(() => { copy.textContent = "Copy path"; }, 1800);
    } catch (e) {
      toast("Could not reach the clipboard — select the path and copy it manually", "error");
    }
  });
  const reload = $("#setup-done-btn");
  if (reload) reload.addEventListener("click", () => location.reload());
}

async function renderAccounts() {
  const v = $("#view-accounts");
  await refreshAccounts();
  let setup = null;
  try { setup = await api("/api/setup"); } catch (e) { setup = null; }
  const ready = !!setup && setup.client_secret_exists;
  const guide = setup ? setupGuideMarkup({
    exists: setup.client_secret_exists,
    path: setup.client_secret_path,
    accounts: state.accounts.length,
  }) : "";
  const cards = state.accounts.map(acctCard).join("");
  const withQ = state.accounts.filter(a => a.quota && a.quota.limit);
  const used = withQ.reduce((s, a) => s + (a.quota.usage || 0), 0);
  const tot = withQ.reduce((s, a) => s + (a.quota.limit || 0), 0);
  const kick = withQ.length
    ? `${withQ.length} accounts · ${fmtBytesShort(used)} of ${fmtBytesShort(tot)} pooled`
    : "no accounts connected yet";
  const n = (state.interrupted || []).length;
  const resumeBanner = n ? `
    <div class="resume-banner" role="alert">
      <div>
        <strong>${n} interrupted transfer${n > 1 ? "s" : ""}</strong>
        <div class="sub">A previous run crashed or was stopped. Every file is tracked, so nothing is lost and nothing will be uploaded twice.</div>
      </div>
      <button class="btn btn-primary btn-sm" id="resume-btn">Resume transfers</button>
    </div>
    <div id="resume-progress" style="margin-bottom:16px"></div>` : "";
  v.innerHTML = `
    <div class="view-head">
      <div class="kicker">${esc(kick)}</div>
      <h2>Accounts</h2>
      <p>Every account you connect here. Spillover only ever asks for Drive access — Gmail is never touched.</p>
    </div>
    ${guide}
    ${resumeBanner}
    <div class="acct-grid">
      ${cards}
      <div class="card connect-card" id="connect-card">
        <h3>Connect an account</h3>
        <p>Sign in with your Google account. Spillover only asks for Drive access — nothing else.</p>
        ${ready ? `
        <button class="btn btn-google" id="connect-btn" style="margin:12px 0;display:inline-flex;align-items:center;gap:8px;font-size:14px;padding:10px 20px">
          <svg width="18" height="18" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#34A853" d="M10.53 28.59a14.5 14.5 0 0 1 0-9.18l-7.98-6.19a24.07 24.07 0 0 0 0 21.56l7.98-6.19z"/><path fill="#FBBC05" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>
          Sign in with Google
        </button>
        <div style="margin-top:8px">
          <label class="checkbox-row" style="font-size:12px;color:var(--ink-tertiary)"><input type="checkbox" id="custom-alias-check"> Set a custom alias</label>
          <div id="alias-input-wrap" style="display:none;margin-top:6px">
            <input class="input" id="new-alias" placeholder="e.g. backup-3…" aria-label="Account alias" spellcheck="false" autocomplete="off" style="max-width:200px">
          </div>
        </div>
        ` : `<p class="connect-locked">Sign-in unlocks as soon as <code>client_secret.json</code> is saved — the guide above shows where it goes.</p>`}
      </div>
    </div>`;

  wireSetupGuide(setup);

  const aliasCheck = $("#custom-alias-check");
  if (aliasCheck) aliasCheck.addEventListener("change", (e) => {
    $("#alias-input-wrap").style.display = e.target.checked ? "block" : "none";
    if (e.target.checked) $("#new-alias").focus();
  });
  const connectBtn = $("#connect-btn");
  if (connectBtn) connectBtn.addEventListener("click", async () => {
    const alias = $("#custom-alias-check").checked ? ($("#new-alias").value.trim() || "") : "";
    const btn = $("#connect-btn");
    btn.disabled = true;
    const orig = btn.innerHTML;
    btn.innerHTML = "Waiting for Google sign-in…";
    try {
      toast("A Google sign-in window will open — pick your account and click Allow.");
      const res = await api("/api/accounts", { method: "POST", body: JSON.stringify({ alias }) });
      toast(`Connected ${res.alias} (${res.email})`);
      renderAccounts();
    } catch (e) {
      toast(e.message, "error");
      btn.disabled = false;
      btn.innerHTML = orig;
    }
  });

  $$("[data-rebalance]", v).forEach(b => b.addEventListener("click", () => {
    state.rebalance = { step: 1, source: b.dataset.rebalance, summary: null, files: [], selected: new Set(), plan: null, jobId: null };
    navigate("rebalance");
  }));
  const resumeBtn = $("#resume-btn");
  if (resumeBtn) resumeBtn.addEventListener("click", () => {
    resumeBtn.disabled = true;
    runWithProgress($("#resume-progress"), "/api/resume", {}, (s) => {
      if (s) { toast(`Resume finished: ${s.done} completed`); renderAccounts(); }
    });
  });
  $$("[data-reconnect]", v).forEach(b => b.addEventListener("click", async () => {
    const alias = b.dataset.reconnect;
    b.disabled = true;
    const orig = b.textContent;
    b.textContent = "Waiting for sign-in…";
    try {
      toast("A Google sign-in window opened — approve access there.");
      const { job_id } = await api(
        `/api/accounts/${encodeURIComponent(alias)}/reconnect`, { method: "POST" });
      const job = await pollJob(job_id);
      if (job.status === "done") {
        toast(`Reconnected ${alias}`);
        renderAccounts();
      } else {
        throw new Error(job.error || "Reconnect failed");
      }
    } catch (e) {
      toast(e.message, "error");
      b.disabled = false;
      b.textContent = orig;
    }
  }));
  $$("[data-disconnect]", v).forEach(b => b.addEventListener("click", async () => {
    const alias = b.dataset.disconnect;
    const ok = await confirmModal({
      title: `Disconnect ${alias}?`,
      body: "This only removes Spillover’s access on this machine. Nothing is deleted from the Google account itself.",
      confirmLabel: "Disconnect",
    });
    if (!ok) return;
    try {
      await api(`/api/accounts/${encodeURIComponent(alias)}`, { method: "DELETE" });
      toast(`Disconnected ${alias}`);
      renderAccounts();
    } catch (e) { toast(e.message, "error"); }
  }));
}

/* ---------- rebalance view ---------- */

const STEPS = ["Source", "Scan", "Select", "Plan", "Run"];

function stepsHtml(current) {
  return `<div class="steps" role="list" aria-label="Rebalance steps">` + STEPS.map((s, i) => {
    const n = i + 1;
    const cls = n < current ? "done" : n === current ? "current" : "";
    return `<div class="step ${cls}" role="listitem" aria-current="${n === current ? "step" : "false"}">
      <span class="n">${n < current ? "✓" : n}</span><span>${s}</span></div>`;
  }).join("") + `</div>`;
}

function renderRebalance() {
  const v = $("#view-rebalance");
  const r = state.rebalance;
  v.innerHTML = `
    <div class="view-head">
      <div class="kicker">${r.source ? esc(r.source) + " → pool" : "copy · verify · trash · ledger"}</div>
      <h2>Rebalance</h2>
      <p>Move files from a full account to emptier ones. Every move is verified by checksum before the original is trashed — and trashed means recoverable for 30 days.</p></div>
    ${stepsHtml(r.step)}
    <div id="rb-body"></div>`;
  renderRbStep();
}

function renderRbStep() {
  const body = $("#rb-body");
  const r = state.rebalance;
  if (r.step === 1) return rbStepSource(body);
  if (r.step === 2) return rbStepScan(body);
  if (r.step === 3) return rbStepSelect(body);
  if (r.step === 4) return rbStepPlan(body);
  return rbStepRun(body);
}

function rbStepSource(body) {
  const r = state.rebalance;
  const opts = state.accounts.map(a => {
    const q = a.quota || {};
    const p = q.limit ? pct(q.usage, q.limit).toFixed(0) : "?";
    return `<option value="${esc(a.alias)}" ${a.alias === r.source ? "selected" : ""}>${esc(a.alias)} — ${p}% used</option>`;
  }).join("");
  body.innerHTML = `
    <div class="card panel"><div class="panel-head"><h3>Which account is full?</h3></div>
      <div class="field" style="max-width:420px">
        <label for="rb-source">Source account</label>
        <select class="select" id="rb-source">${opts || "<option value=''>No accounts connected</option>"}</select>
        <span class="hint">Files move out of this account. Destinations are picked automatically from the accounts with the most free space.</span>
      </div>
      <div style="margin-top:20px"><button class="btn btn-primary" id="rb-next" ${opts ? "" : "disabled"}>Continue to scan</button></div>
    </div>`;
  $("#rb-next").addEventListener("click", () => {
    r.source = $("#rb-source").value;
    r.step = 2; r.summary = null; r.files = [];
    renderRebalance();
  });
}

function rbStepScan(body) {
  const r = state.rebalance;
  if (r.summary) {
    const s = r.summary;
    const exts = (s.top_extensions || []).slice(0, 5).map(([ext, b]) =>
      `<span class="pill">${esc(ext)} · ${fmtBytesShort(b)}</span>`).join(" ");
    body.innerHTML = `
      <div class="card panel"><div class="panel-head"><h3>Scan complete</h3><span class="spacer"></span>
        <button class="btn btn-secondary btn-sm" id="rb-rescan">Scan again</button></div>
        <div class="summary-strip">
          <div class="stat"><div class="k">Files found</div><div class="v">${s.quota_files}</div></div>
          <div class="stat"><div class="k">Counts toward quota</div><div class="v">${fmtBytesShort(s.quota_bytes)}</div></div>
          <div class="stat"><div class="k">Largest file</div><div class="v">${s.largest?.length ? fmtBytesShort(s.largest[0].size) : "—"}</div></div>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px">${exts}</div>
        <button class="btn btn-primary" id="rb-next">Choose files to move</button>
      </div>`;
    $("#rb-rescan").addEventListener("click", () => { r.summary = null; renderRebalance(); });
    $("#rb-next").addEventListener("click", async () => {
      try {
        r.files = await api(`/api/accounts/${encodeURIComponent(r.source)}/files`);
        r.step = 3;
        renderRebalance();
      } catch (e) { toast(e.message, "error"); }
    });
    return;
  }
  body.innerHTML = `
    <div class="card panel"><div class="panel-head"><h3>Scan ${esc(r.source)}’s Drive</h3></div>
      <p style="color:var(--ink-subtle);margin-bottom:20px;max-width:560px">Reads the file list and sizes. Nothing is downloaded — this just maps what’s there so you can pick what moves.</p>
      <div id="scan-progress"></div>
      <button class="btn btn-primary btn-lg" id="rb-scan">Start scan</button>
    </div>`;
  $("#rb-scan").addEventListener("click", async () => {
    const btn = $("#rb-scan");
    btn.disabled = true;
    btn.textContent = "Scanning…";
    try {
      const { job_id } = await api(`/api/accounts/${encodeURIComponent(r.source)}/scan`, { method: "POST" });
      const box = $("#scan-progress");
      const timer = setInterval(async () => {
        try {
          const job = await api(`/api/jobs/${job_id}`);
          const p = job.progress || {};
          box.innerHTML = `<div class="progress-line"><div class="quota-bar"><span style="width:${p.pct || 0}%"></span></div>
            <div class="progress-current">${esc(p.stage || "")}</div></div>`;
          if (job.status === "done") {
            clearInterval(timer);
            r.summary = job.result;
            renderRebalance();
          } else if (job.status === "error") {
            clearInterval(timer);
            toast(job.error || "Scan failed", "error");
            btn.disabled = false; btn.textContent = "Start scan";
          }
        } catch (e) { clearInterval(timer); toast(e.message, "error"); }
      }, 900);
    } catch (e) {
      toast(e.message, "error");
      btn.disabled = false; btn.textContent = "Start scan";
    }
  });
}

let rbTableState = { q: "", minSize: 50 * 1024 ** 2, kind: "", sort: "size-desc" };

function rbStepSelect(body) {
  const r = state.rebalance;
  const files = r.files;
  body.innerHTML = `
    <div class="card panel">
      <div class="panel-head"><h3>Select files to move</h3><span class="spacer"></span>
        <span class="count" id="rb-count"></span></div>
      <div class="toolbar">
        <input class="input grow" id="rb-q" placeholder="Search files…" aria-label="Search files" autocomplete="off" spellcheck="false">
        <select class="select" id="rb-minsize" aria-label="Minimum size">
          <option value="0">Any size</option>
          <option value="${50 * 1024 ** 2}" selected>≥ 50 MB</option>
          <option value="${500 * 1024 ** 2}">≥ 500 MB</option>
          <option value="${1024 ** 3}">≥ 1 GB</option>
        </select>
        <select class="select" id="rb-kind" aria-label="File kind">
          <option value="">All kinds</option>
          <option value="video/">Videos</option>
          <option value="image/">Photos</option>
          <option value="audio/">Audio</option>
        </select>
      </div>
      <div class="table-wrap"><table class="data" id="rb-table">
        <thead><tr>
          <th scope="col" style="width:36px"><input type="checkbox" id="rb-all" aria-label="Select all files" style="accent-color:var(--primary)"></th>
          <th scope="col" class="sortable" data-sort="name">Name</th><th scope="col">Kind</th>
          <th scope="col" class="sortable num" data-sort="size" aria-sort="descending">Size</th><th scope="col">Modified</th>
        </tr></thead>
        <tbody></tbody>
      </table></div>
      <div style="display:flex;gap:8px;margin-top:16px;align-items:center">
        <button class="btn btn-secondary" id="rb-back">Back</button>
        <span class="spacer" style="flex:1"></span>
        <span class="count" id="rb-selcount"></span>
        <button class="btn btn-primary" id="rb-plan" disabled>Create plan</button>
      </div>
    </div>`;

  const q = $("#rb-q"), minsize = $("#rb-minsize"), kind = $("#rb-kind");
  q.value = rbTableState.q; minsize.value = String(rbTableState.minSize); kind.value = rbTableState.kind;

  function filtered() {
    let list = files.filter(f =>
      (!rbTableState.q || f.name.toLowerCase().includes(rbTableState.q.toLowerCase())) &&
      f.size >= rbTableState.minSize &&
      (!rbTableState.kind || f.mimeType.startsWith(rbTableState.kind)));
    const [key, dir] = rbTableState.sort.split("-");
    list.sort((a, b) => dir === "desc"
      ? (key === "size" ? b.size - a.size : b.name.localeCompare(a.name))
      : (key === "size" ? a.size - b.size : a.name.localeCompare(b.name)));
    return list;
  }

  function draw() {
    const list = filtered();
    const tb = $("#rb-table tbody");
    const rows = list.slice(0, 400);
    tb.innerHTML = rows.map(f => `
      <tr data-id="${esc(f.id)}" class="${r.selected.has(f.id) ? "selected" : ""}">
        <td><input type="checkbox" data-check="${esc(f.id)}" ${r.selected.has(f.id) ? "checked" : ""} aria-label="Select ${esc(f.name)}" style="accent-color:var(--primary)"></td>
        <td><div class="fname" title="${esc(f.name)}">${esc(f.name)}</div><div class="fmeta">${esc(f.id.slice(0, 12))}…</div></td>
        <td><span class="kind-tag">${kindOf(f.mimeType)}</span></td>
        <td class="num">${fmtBytes(f.size)}</td>
        <td style="color:var(--ink-tertiary);font-size:12px">${esc((f.modifiedTime || "").slice(0, 10))}</td>
      </tr>`).join("") ||
      `<tr><td colspan="5"><div class="empty-state"><h3>No files match</h3><p>Try widening the size filter or clearing the search.</p></div></td></tr>`;
    $("#rb-count").textContent = `${list.length} files${list.length > 400 ? " (showing 400)" : ""}`;
    updateSel();
    $$("[data-check]", tb).forEach(c => c.addEventListener("change", () => {
      c.checked ? r.selected.add(c.dataset.check) : r.selected.delete(c.dataset.check);
      c.closest("tr").classList.toggle("selected", c.checked);
      updateSel();
    }));
  }

  function updateSel() {
    const bytes = [...r.selected].reduce((s, id) => {
      const f = files.find(x => x.id === id);
      return s + (f ? f.size : 0);
    }, 0);
    $("#rb-selcount").textContent = r.selected.size ? `${r.selected.size} selected · ${fmtBytes(bytes)}` : "";
    $("#rb-plan").disabled = !r.selected.size;
  }

  q.addEventListener("input", () => { rbTableState.q = q.value; draw(); });
  minsize.addEventListener("change", () => { rbTableState.minSize = +minsize.value; draw(); });
  kind.addEventListener("change", () => { rbTableState.kind = kind.value; draw(); });
  $$("#rb-table th.sortable").forEach(th => {
    th.setAttribute("tabindex", "0");
    th.setAttribute("role", "button");
    th.setAttribute("aria-label", `Sort by ${th.textContent.trim()}`);
    const toggle = () => {
      const k = th.dataset.sort;
      rbTableState.sort = rbTableState.sort === k + "-desc" ? k + "-asc" : k + "-desc";
      draw();
      $$("#rb-table th.sortable").forEach(o => {
        if (o === th) o.setAttribute("aria-sort", rbTableState.sort.endsWith("desc") ? "descending" : "ascending");
        else o.removeAttribute("aria-sort");
      });
    };
    th.addEventListener("click", toggle);
    th.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });
  });
  $("#rb-all").addEventListener("change", e => {
    filtered().slice(0, 400).forEach(f => e.target.checked ? r.selected.add(f.id) : r.selected.delete(f.id));
    draw();
  });
  $("#rb-back").addEventListener("click", () => { r.step = 2; renderRebalance(); });
  $("#rb-plan").addEventListener("click", async () => {
    const btn = $("#rb-plan");
    btn.disabled = true; btn.textContent = "Planning…";
    try {
      r.plan = await api("/api/plan", {
        method: "POST",
        body: JSON.stringify({ source: r.source, file_ids: [...r.selected] }),
      });
      r.step = 4;
      renderRebalance();
    } catch (e) {
      toast(e.message, "error");
      btn.disabled = false; btn.textContent = "Create plan";
    }
  });
  draw();
}

function rbStepPlan(body) {
  const r = state.rebalance;
  const p = r.plan;
  if (!p || !p.assignments.length) {
    body.innerHTML = `<div class="card panel"><div class="empty-state">
      <h3>Nothing to move</h3><p>No files fit the destination accounts’ free space, or nothing was selected.</p>
      <button class="btn btn-secondary" id="rb-back">Back to selection</button></div></div>`;
    $("#rb-back").addEventListener("click", () => { r.step = 3; renderRebalance(); });
    return;
  }
  const dests = Object.entries(p.per_dest).map(([alias, d]) => {
    const acct = state.accounts.find(a => a.alias === alias);
    const free = acct?.quota?.free || 0;
    const after = free - d.bytes;
    return `<div class="plan-dest">
      <div class="head"><span class="alias">${esc(alias)}</span>
        <span class="meta">${d.files} files · ${fmtBytes(d.bytes)}</span></div>
      <div class="quota-bar"><span style="width:${pct(d.bytes, free)}%"></span></div>
      <div style="font-size:12px;color:var(--ink-tertiary);margin-top:8px" class="num">${fmtBytes(after)} free after</div>
    </div>`;
  }).join("");
  const unplaced = (p.unplaced || []).slice(0, 8).map(f =>
    `<div style="font-size:13px;color:var(--ink-subtle);padding:4px 0" class="num">${esc(f.name)} · ${fmtBytes(f.size)}</div>`).join("");

  const warns = p.shared_warnings || [];
  const shareLabel = w => w.level === "not_owned" ? "Not owned"
    : w.level === "unknown" ? "Sharing unknown" : "Shared";
  const sharePill = w => w.level === "not_owned" ? "pill-danger" : "pill-warn";
  const warnBlock = warns.length ? `
    <div class="plan-unplaced" style="border-color:rgba(245,165,36,.4);border-style:solid">
      <strong>${p.shared_blocked_count} file(s) won’t move by default</strong> — shared with others, owned by someone else, or from a scan that predates sharing info:
      ${warns.slice(0, 6).map(w => `
        <div class="share-warn">
          <span class="pill ${sharePill(w)}"><span class="dot"></span>${shareLabel(w)}</span>
          <span class="fname">${esc(w.name)}</span><span class="num">${fmtBytes(w.size)}</span>
          <div class="reason">${esc(w.reason)}</div>
        </div>`).join("")}
      ${warns.length > 6 ? `<div style="font-size:12px;color:var(--ink-tertiary)">…and ${warns.length - 6} more</div>` : ""}
      <label class="checkbox-row" style="margin-top:12px">
        <input type="checkbox" id="rb-include-shared" ${p.include_shared ? "checked" : ""}>
        Move these too — I understand share links break and collaborators lose access
      </label>
      <div class="hint" id="rb-shared-hint" style="margin-top:4px"></div>
    </div>` : "";

  body.innerHTML = `
    <div class="card panel">
      <div class="panel-head"><h3>Plan preview</h3><span class="spacer"></span>
        <span class="pill pill-accent"><span class="dot"></span>Dry run — nothing moves yet</span></div>
      <div class="summary-strip">
        <div class="stat"><div class="k">Files</div><div class="v">${p.assignments.length}</div></div>
        <div class="stat"><div class="k">Total size</div><div class="v">${fmtBytesShort(p.total_bytes)}</div></div>
        <div class="stat"><div class="k">Destinations</div><div class="v">${Object.keys(p.per_dest).length}</div></div>
        <div class="stat"><div class="k">Won’t fit</div><div class="v">${(p.unplaced || []).length}</div></div>
      </div>
      <div class="plan-dests">${dests}</div>
      ${(p.unplaced || []).length ? `<div class="plan-unplaced"><strong>${p.unplaced.length} file(s) won’t fit</strong> in the free space available:${unplaced}</div>` : ""}
      ${warnBlock}
      <div style="display:flex;gap:8px;margin-top:20px">
        <button class="btn btn-secondary" id="rb-back">Back</button>
        <span style="flex:1"></span>
        <button class="btn btn-primary btn-lg" id="rb-run">Review &amp; run</button>
      </div>
    </div>`;
  $("#rb-back").addEventListener("click", () => { r.step = 3; renderRebalance(); });
  $("#rb-run").addEventListener("click", () => { r.step = 5; renderRebalance(); });
  const incShared = $("#rb-include-shared");
  if (incShared) {
    const hint = $("#rb-shared-hint");
    const updateHint = () => {
      r.plan.include_shared = incShared.checked;
      hint.textContent = incShared.checked
        ? `${p.shared_blocked_count} flagged file(s) will be moved.`
        : `${p.shared_blocked_count} flagged file(s) will be skipped.`;
    };
    incShared.addEventListener("change", updateHint);
    updateHint();
  }
}

function rbStepRun(body) {
  const r = state.rebalance;
  const p = r.plan;
  if (!p) { r.step = 1; renderRebalance(); return; }
  body.innerHTML = `
    <div class="card panel">
      <div class="panel-head"><h3>Run the moves</h3></div>
      <div class="summary-strip">
        <div class="stat"><div class="k">Files</div><div class="v">${p.assignments.length}</div></div>
        <div class="stat"><div class="k">Total size</div><div class="v">${fmtBytesShort(p.total_bytes)}</div></div>
        <div class="stat"><div class="k">From</div><div class="v" style="font-size:16px">${esc(p.source)}</div></div>
      </div>
      <div id="run-progress"></div>
      <div style="display:flex;gap:8px;margin-top:20px">
        <button class="btn btn-secondary" id="rb-back">Back to plan</button>
        <span style="flex:1"></span>
        <button class="btn btn-primary btn-lg" id="rb-exec">Move ${p.assignments.length} files</button>
      </div>
    </div>`;
  $("#rb-back").addEventListener("click", () => { r.step = 4; renderRebalance(); });
  $("#rb-exec").addEventListener("click", async () => {
    const skippedN = p.include_shared ? 0 : (p.shared_blocked_count || 0);
    const ok = await confirmModal({
      title: `Move ${p.assignments.length} files?`,
      body: `Each file is downloaded, checksummed, uploaded to its destination, and verified — <strong>only then</strong> is the original moved to ${esc(p.source)}’s trash.`,
      warn: skippedN
        ? `This moves real files. ${skippedN} shared file(s) will be <strong>skipped</strong> (not moved). Trashed originals stay recoverable for 30 days, and every move is recorded in the ledger.`
        : "This moves real files. Trashed originals stay recoverable for 30 days, and every move is recorded in the ledger.",
      confirmLabel: `Move ${p.assignments.length} files`,
      danger: true,
    });
    if (!ok) return;
    runWithProgress($("#run-progress"), "/api/run", { plan: p }, (s) => {
      if (s) toast(`Moved ${s.done} of ${p.assignments.length} files — see the ledger`);
      r.step = 1; r.plan = null; r.selected = new Set();
    });
  });
}

async function runWithProgress(box, url, payload, onDone) {
  box.innerHTML = `<div class="progress-line"><div class="row"><span>Starting…</span><span class="pct">0%</span></div>
    <div class="quota-bar"><span style="width:0%"></span></div><div class="progress-current"></div></div>`;
  const bar = $(".quota-bar > span", box);
  const rowLabel = $(".row span:first-child", box);
  const pctEl = $(".pct", box);
  const cur = $(".progress-current", box);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith("data:")) continue;
        const ev = JSON.parse(line.slice(5));
        if (ev.type === "progress") {
          const pc = Math.round((ev.done / ev.total) * 100);
          bar.style.width = pc + "%";
          pctEl.textContent = pc + "%";
          rowLabel.textContent = ev.status === "failed" ? `Failed: ${ev.name}`
            : ev.status === "skipped" ? `Skipped: ${ev.name}` : `Moving files…`;
          cur.textContent = `${ev.done}/${ev.total} · ${ev.name} → ${ev.dest_alias}${ev.note ? " · " + ev.note : ""}`;
        } else if (ev.type === "done") {
          const s = ev.stats;
          bar.style.width = "100%"; pctEl.textContent = "100%";
          rowLabel.textContent = "Done";
          const skippedBit = s.skipped ? ` · ${s.skipped} skipped` : "";
          cur.textContent = `${s.done} moved · ${s.failed} failed${skippedBit} · ${fmtBytes(s.bytes_moved || s.bytes || 0)}`;
          toast(`Done: ${s.done} moved${s.failed ? `, ${s.failed} failed` : ""}${s.skipped ? `, ${s.skipped} skipped` : ""}`);
          if (onDone) onDone(s);
        } else if (ev.type === "error") {
          throw new Error(ev.error);
        }
      }
    }
  } catch (e) {
    box.innerHTML += `<p style="color:var(--danger);font-size:13px;margin-top:12px">Failed: ${esc(e.message)}</p>`;
  }
}

/* ---------- takeout view ---------- */

function renderTakeout() {
  const v = $("#view-takeout");
  const t = state.takeout;
  v.innerHTML = `
    <div class="view-head">
      <div class="kicker">Google Photos · manual route</div>
      <h2>Takeout</h2>
      <p>Google Photos has no API for your existing library, so this is the manual route: export from <a href="https://takeout.google.com" target="_blank" rel="noopener">takeout.google.com</a>, unzip it, then point Spillover at the folder. Media is distributed into your other accounts’ Drive storage.</p></div>
    <div class="takeout-grid">
      <div class="card panel">
        <div class="panel-head"><h3>1 · Read the export</h3></div>
        <div class="field">
          <label for="to-dir">Takeout folder path</label>
          <input class="input" id="to-dir" placeholder="/home/you/takeout" spellcheck="false" autocomplete="off">
          <span class="hint">The folder containing the .zip parts or the extracted “Takeout” directory.</span>
        </div>
        <div style="margin-top:16px"><button class="btn btn-primary" id="to-ingest">Read export</button></div>
        <div id="to-summary" style="margin-top:16px"></div>
      </div>
      <div class="card panel">
        <div class="panel-head"><h3>2 · Distribute</h3></div>
        <div id="to-dests"><p style="color:var(--ink-tertiary);font-size:13px">Read an export first, then pick destination accounts here.</p></div>
        <div id="to-progress"></div>
      </div>
      <div class="card panel panel-wide">
        <div class="panel-head"><h3>Photos picker</h3>
          <span class="pill pill-accent"><span class="dot"></span>No export needed</span></div>
        <p style="color:var(--ink-subtle);font-size:13px;max-width:640px;margin-bottom:16px">
          Skip the Takeout download entirely: Spillover opens Google’s own photo picker,
          you select what to move, and it copies your picks into your other accounts’ Drive storage.
          It can only ever see the items you pick — never the rest of your library.
          Google’s Photos API has no delete, so you’ll remove the originals by hand afterwards.
        </p>
        <div id="ph-body"></div>
      </div>
    </div>
    <div class="card panel" id="to-albums" hidden>
      <div class="panel-head"><h3>Albums found</h3></div>
      <div class="album-list" id="to-album-list"></div>
    </div>`;

  $("#to-ingest").addEventListener("click", async () => {
    const dir = $("#to-dir").value.trim();
    if (!dir) { toast("Enter the Takeout folder path", "error"); return; }
    const btn = $("#to-ingest");
    btn.disabled = true; btn.textContent = "Reading…";
    try {
      const info = await api("/api/takeout/ingest", { method: "POST", body: JSON.stringify({ dir }) });
      t.items = info.items; t.summary = info;
      $("#to-summary").innerHTML = `
        <div class="summary-strip" style="margin:0">
          <div class="stat"><div class="k">Media files</div><div class="v">${info.files}</div></div>
          <div class="stat"><div class="k">Total size</div><div class="v">${fmtBytesShort(info.bytes)}</div></div>
          <div class="stat"><div class="k">Albums</div><div class="v">${Object.keys(info.albums).length}</div></div>
        </div>`;
      $("#to-albums").hidden = false;
      $("#to-album-list").innerHTML = Object.entries(info.albums).map(([name, n]) => `
        <div class="album-row">
          <span class="thumb" aria-hidden="true"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="1.6"/><path d="m21 15-4.5-4.5L6 21"/></svg></span>
          <span class="aname">${esc(name)}</span>
          <span class="ameta">${n} items</span>
        </div>`).join("");
      const others = state.accounts.filter(a => a.alias !== "main");
      $("#to-dests").innerHTML = `
        <div class="field"><label>Destination accounts</label>
        ${(others.length ? others : state.accounts).map(a => `
          <label class="checkbox-row"><input type="checkbox" data-dest="${esc(a.alias)}" ${t.dests.has(a.alias) ? "checked" : ""}> ${esc(a.alias)}</label>`).join("")}
        </div>
        <div style="margin-top:16px"><button class="btn btn-primary" id="to-go">Distribute ${info.files} files</button></div>`;
      $$("#to-dests [data-dest]").forEach(c => c.addEventListener("change", () =>
        c.checked ? t.dests.add(c.dataset.dest) : t.dests.delete(c.dataset.dest)));
      $("#to-go").addEventListener("click", async () => {
        if (!t.dests.size) { toast("Pick at least one destination", "error"); return; }
        const items = t.items.map(i => ({ name: i.name, path: i.path, album: i.album }));
        runWithProgress($("#to-progress"), "/api/takeout/distribute",
          { items, dests: [...t.dests] }, () => toast("Takeout distributed — originals still need manual deletion in Google Photos"));
      });
    } catch (e) { toast(e.message, "error"); }
    btn.disabled = false; btn.textContent = "Read export";
  });

  /* ---------- photos picker flow ---------- */
  const ph = state.photos;
  const phBody = $("#ph-body");

  function phStepCreate() {
    const opts = state.accounts.map(a =>
      `<option value="${esc(a.alias)}" ${a.alias === ph.alias ? "selected" : ""}>${esc(a.alias)}</option>`).join("");
    phBody.innerHTML = `
      <div class="field" style="max-width:420px">
        <label for="ph-src">Which account’s Photos library?</label>
        <select class="select" id="ph-src">${opts || "<option value=''>No accounts connected</option>"}</select>
        <span class="hint">You’ll pick from this account’s Google Photos. In the next step, open the picker link while signed in to Google as this same account — picks from any other account won’t come through.</span>
      </div>
      <div style="margin-top:16px"><button class="btn btn-primary" id="ph-create" ${opts ? "" : "disabled"}>Create picker link</button></div>`;
    $("#ph-create").addEventListener("click", async () => {
      ph.alias = $("#ph-src").value;
      const btn2 = $("#ph-create");
      btn2.disabled = true; btn2.textContent = "Creating…";
      try {
        const s = await api("/api/photos/session",
          { method: "POST", body: JSON.stringify({ alias: ph.alias }) });
        ph.session_id = s.session_id;
        phStepPick(s);
      } catch (e) {
        toast(e.message, "error");
        btn2.disabled = false; btn2.textContent = "Create picker link";
      }
    });
  }

  function phStepPick(s) {
    const acct = state.accounts.find(a => a.alias === ph.alias);
    const email = acct && acct.email ? acct.email : ph.alias;
    phBody.innerHTML = `
      <p style="font-size:13px;color:var(--ink-subtle);margin-bottom:4px">
        <strong style="color:var(--ink)">Step 1 ·</strong> open this link in a browser window signed in to Google as
        <strong style="color:var(--ink)">${esc(email)}</strong>, then select the photos/videos to move:</p>
      <a class="picker-link" href="${esc(s.picker_uri)}" target="_blank" rel="noopener">${esc(s.picker_uri)}</a>
      <p style="font-size:12px;color:var(--ink-tertiary);margin-top:6px">Google shows that account’s photo library. If you pick while signed in as a different account, the transfer can’t see your picks.</p>
      <p style="font-size:13px;color:var(--ink-subtle)">
        <strong style="color:var(--ink)">Step 2 ·</strong> come back here and press the button once you’ve finished selecting.</p>
      <div style="display:flex;gap:8px;margin-top:12px">
        <button class="btn btn-secondary" id="ph-back">Start over</button>
        <button class="btn btn-primary" id="ph-check">I’ve finished selecting</button>
      </div>
      <div id="ph-picked" style="margin-top:16px"></div>`;
    $("#ph-back").addEventListener("click", () => {
      ph.session_id = null; ph.items = []; phStepCreate();
    });
    $("#ph-check").addEventListener("click", async () => {
      const btn2 = $("#ph-check");
      btn2.disabled = true; btn2.textContent = "Checking…";
      const pickedEl = $("#ph-picked");
      pickedEl.innerHTML = `<p style="color:var(--ink-tertiary);font-size:13px">Waiting for Google to confirm your picks (up to 45 s)…</p>`;
      try {
        const items = await api(
          `/api/photos/session/${encodeURIComponent(ph.alias)}/${encodeURIComponent(ph.session_id)}/items`);
        ph.items = items;
        pickedEl.innerHTML = "";
        if (!items.length) {
          toast("Nothing selected yet — pick some items in the Google picker first", "error");
          btn2.disabled = false; btn2.textContent = "I’ve finished selecting";
          return;
        }
        phStepTransfer();
      } catch (e) {
        pickedEl.innerHTML = `
          <div class="warn-box" style="margin-top:12px">
            <strong>Google couldn’t attach your picks to this session.</strong>
            <p style="font-size:13px;margin:8px 0 0">${esc(e.message)}</p>
          </div>`;
        btn2.disabled = false; btn2.textContent = "Try again";
      }
    });
  }

  function phStepTransfer() {
    const others = state.accounts.filter(a => a.alias !== ph.alias);
    const list = ph.items.slice(0, 8).map(i =>
      `<div style="font-size:13px;padding:4px 0"><span class="kind-tag">${esc(i.type || "item")}</span>
       <span style="margin-left:8px">${esc(i.filename)}</span></div>`).join("");
    phBody.innerHTML = `
      <p style="font-size:14px"><strong>${ph.items.length} item(s) selected</strong></p>
      <div style="margin:8px 0 16px">${list}
        ${ph.items.length > 8 ? `<div style="font-size:12px;color:var(--ink-tertiary)">…and ${ph.items.length - 8} more</div>` : ""}</div>
      <div class="field"><label>Destination accounts</label>
        ${(others.length ? others : state.accounts).map(a => `
          <label class="checkbox-row"><input type="checkbox" data-phdest="${esc(a.alias)}"
            ${ph.dests.has(a.alias) ? "checked" : ""}> ${esc(a.alias)}</label>`).join("")}
        <span class="hint">Items are spread across the accounts you tick.</span>
      </div>
      <div class="field" style="margin-top:12px">
        <label class="checkbox-row"><input type="checkbox" id="ph-custom-folder"> Save to a custom folder</label>
        <div id="ph-folder-input" style="display:none;margin-top:8px">
          <input class="input" id="ph-folder-name" placeholder="e.g. Vacation 2026, Family Photos…" value="" style="width:100%;max-width:360px">
          <span class="hint">Files will be saved to <strong>Spillover › [your folder name]</strong> on the destination Drive.</span>
        </div>
      </div>
      <div style="display:flex;gap:8px;margin-top:16px">
        <button class="btn btn-secondary" id="ph-back2">Back</button>
        <button class="btn btn-primary btn-lg" id="ph-go">Transfer ${ph.items.length} items</button>
      </div>
      <div id="ph-progress"></div>
      <div id="ph-done"></div>`;
    $$("#ph-body [data-phdest]").forEach(c => c.addEventListener("change", () =>
      c.checked ? ph.dests.add(c.dataset.phdest) : ph.dests.delete(c.dataset.phdest)));
    $("#ph-custom-folder").addEventListener("change", (e) => {
      $("#ph-folder-input").style.display = e.target.checked ? "block" : "none";
      if (e.target.checked) $("#ph-folder-name").focus();
    });
    $("#ph-back2").addEventListener("click", phStepCreate);
    $("#ph-go").addEventListener("click", () => {
      if (!ph.dests.size) { toast("Pick at least one destination", "error"); return; }
      const folderName = $("#ph-custom-folder").checked ? ($("#ph-folder-name").value.trim() || "") : "";
      runWithProgress($("#ph-progress"), "/api/photos/transfer",
        { alias: ph.alias, session_id: ph.session_id, dest_aliases: [...ph.dests], folder: folderName },
        (s) => {
          if (!s) return;
          $("#ph-done").innerHTML = `
            <div class="manifest-box" role="status">
              <strong>${s.done} item(s) copied and verified.</strong><br>
              Google’s Photos API has no delete endpoint, so the originals are still in
              Google Photos on <strong>${esc(ph.alias)}</strong>. Delete them by hand there —
              only after you’ve confirmed the copies.<br>
              A deletion checklist was saved to
              <span class="picker-link" style="margin:8px 0 0">${esc(s.manifest || "the photo manifests folder")}</span>
            </div>`;
          ph.items = []; ph.session_id = null;
        });
    });
  }

  phStepCreate();
}

/* ---------- ledger view ---------- */

/* ---------- duplicates view ---------- */

/** Groups with this session's trashed copies removed, totals recomputed. */
function dupGroups(d) {
  const groups = [];
  for (const g of d.duplicates) {
    const copies = g.copies.filter(c => !state.dup.deleted.has(c.alias + "/" + c.file_id));
    if (copies.length < 2 || new Set(copies.map(c => c.alias)).size < 2) continue;
    groups.push({
      name: g.name, size: g.size, copies, hash_verified: g.hash_verified,
      wasted_bytes: g.size * (copies.length - 1),
    });
  }
  return {
    groups,
    wasted: groups.reduce((s, g) => s + g.wasted_bytes, 0),
    scanned: d.scanned_aliases || [],
  };
}

function newestCopyIndex(g) {
  let best = 0;
  g.copies.forEach((c, i) => {
    if (String(c.modifiedTime) > String(g.copies[best].modifiedTime)) best = i;
  });
  return best;
}

function dupDetailRow(g, i) {
  const keep = state.dup.keep[i] ?? newestCopyIndex(g);
  const doomed = g.copies.filter((_, j) => j !== keep);
  const bytes = doomed.reduce((s, c) => s + c.size, 0);
  const risky = doomed.filter(c => c.shared || !c.owned_by_me).length;
  return `
    <tr class="dup-detail"><td colspan="5"><div class="dup-detail-inner">
      <p class="dup-lead">Keep one copy. The others move to Google’s trash, where Drive holds them for 30 days.</p>
      <div class="copy-list">
        ${g.copies.map((c, j) => `
          <div class="copy${j === keep ? " keeping" : ""}">
            <label class="copy-hit">
              <input type="radio" name="keep-${i}" value="${j}" ${j === keep ? "checked" : ""}>
              <span class="copy-id">
                <span class="copy-alias">${esc(c.alias)}</span>
                <span class="copy-sub">${c.modifiedTime ? esc(String(c.modifiedTime).slice(0, 10)) : "date unknown"} · id ${esc(String(c.file_id).slice(0, 8))}</span>
              </span>
              ${g.hash_verified ? `<span class="pill pill-success"><span class="dot"></span>Hash verified</span>` : ""}
              ${!c.owned_by_me ? `<span class="pill pill-danger"><span class="dot"></span>Not yours</span>`
                : c.shared ? `<span class="pill pill-warn"><span class="dot"></span>Shared</span>` : ""}
              ${j === keep ? `<span class="pill pill-quiet" aria-hidden="true">Keep</span>` : ""}
            </label>
            <a class="drive-link" target="_blank" rel="noopener noreferrer"
               href="https://drive.google.com/file/d/${encodeURIComponent(c.file_id)}/view">View in Drive ↗</a>
          </div>`).join("")}
      </div>
      <div class="dup-detail-foot">
        <span class="foot-note">${doomed.length ? `${doomed.length} cop${doomed.length > 1 ? "ies" : "y"} to trash · ${fmtBytes(bytes)}${risky ? ` · ${risky} shared or not owned by you` : ""}` : "Select a copy to keep"}</span>
        <button class="btn btn-danger btn-sm" data-del="${i}" ${doomed.length ? "" : "disabled"}>Move to trash</button>
      </div>
    </div></td></tr>`;
}

async function renderDuplicates() {
  const v = $("#view-duplicates");
  v.innerHTML = `
    <div class="view-head">
      <div class="kicker">cached scans · no google calls</div>
      <h2>Duplicates</h2>
      <p>The same file sitting in two or more accounts wastes exactly the space you are trying to free. This reads scans you have already run, so it is instant.</p>
    </div>
    <div class="dup-loading" role="status">Reading cached scans…</div>`;

  let d;
  try {
    if (!state.dup.data) state.dup.data = await api("/api/duplicates");
    d = state.dup.data;
  } catch (e) {
    v.innerHTML = `
      <div class="view-head"><div class="kicker">cached scans</div><h2>Duplicates</h2></div>
      <div class="card panel"><div class="empty-state">
        <h3>Could not read the scans</h3><p>${esc(e.message)}</p>
        <button class="btn btn-secondary" id="dup-retry">Try again</button>
      </div></div>`;
    $("#dup-retry").addEventListener("click", renderDuplicates);
    return;
  }

  const { groups, wasted, scanned } = dupGroups(d);
  const badge = $("#dup-count");
  badge.hidden = !groups.length;
  badge.textContent = groups.length;

  if (!scanned.length) {
    v.innerHTML = `
      <div class="view-head">
        <div class="kicker">nothing scanned yet</div>
        <h2>Duplicates</h2>
        <p>The same file sitting in two or more accounts wastes exactly the space you are trying to free.</p>
      </div>
      <div class="card panel"><div class="empty-state">
        <div class="icon" aria-hidden="true">
          <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="3" width="13" height="13" rx="2.5"/><path d="M16 20.5H5.5A2.5 2.5 0 0 1 3 18V7.5"/></svg>
        </div>
        <h3>Scan your accounts first</h3>
        <p>Duplicates are found from cached scans. Run a scan in Rebalance, then come back — this view never calls Google on its own.</p>
        <button class="btn btn-primary" id="dup-go">Go to Rebalance</button>
      </div></div>`;
    $("#dup-go").addEventListener("click", () => navigate("rebalance"));
    return;
  }

  if (!groups.length) {
    v.innerHTML = `
      <div class="view-head">
        <div class="kicker">${scanned.length} accounts scanned · nothing duplicated</div>
        <h2>Duplicates</h2>
        <p>The same file sitting in two or more accounts wastes exactly the space you are trying to free.</p>
      </div>
      <div class="card panel"><div class="empty-state">
        <h3>No duplicates across ${esc(scanned.join(", "))}</h3>
        <p>Every file found here exists in one account only. Re-scan after you move things, since new copies land in the destination.</p>
        <button class="btn btn-secondary" id="dup-reread">Re-read scans</button>
      </div></div>`;
    $("#dup-reread").addEventListener("click", () => { state.dup.data = null; renderDuplicates(); });
    return;
  }

  const unscanned = state.accounts.map(a => a.alias).filter(a => !scanned.includes(a));
  v.innerHTML = `
    <div class="view-head">
      <div class="kicker">${groups.length} groups · ${fmtBytesShort(wasted)} recoverable</div>
      <h2>Duplicates</h2>
      <p>The same file sitting in two or more accounts wastes exactly the space you are trying to free. Sorted by the space each group gives back.</p>
    </div>
    <div class="summary-strip">
      <div class="stat"><div class="k">Recoverable now</div><div class="v">${fmtBytesShort(wasted)}</div></div>
      <div class="stat"><div class="k">Duplicate groups</div><div class="v">${groups.length}</div></div>
      <div class="stat"><div class="k">Accounts scanned</div><div class="v">${scanned.length}<small> ${esc(scanned.join(" · "))}</small></div></div>
    </div>
    ${unscanned.length ? `<div class="dup-partial">Not scanned yet: ${esc(unscanned.join(", "))} — duplicates there stay invisible until you scan them.</div>` : ""}
    <div class="card panel">
      <div class="toolbar">
        <span class="count">Showing ${Math.min(groups.length, 200)} of ${d.total_groups} groups</span>
        <span class="grow"></span>
        <button class="btn btn-secondary btn-sm" id="dup-reread">Re-read scans</button>
      </div>
      <div class="table-wrap"><table class="data">
        <thead><tr>
          <th scope="col">File</th><th scope="col" class="num">Size</th>
          <th scope="col">Found in</th><th scope="col" class="num">Wasted</th>
          <th scope="col"><span class="sr-only">Action</span></th>
        </tr></thead>
        <tbody id="dup-body">
          ${groups.map((g, i) => `
            <tr class="dup-row${state.dup.open === i ? " open" : ""}">
              <td><div class="fname" title="${esc(g.name)}">${esc(g.name)}</div></td>
              <td class="num">${fmtBytes(g.size)}</td>
              <td class="dup-acct">${g.copies.map(c => `<span class="acct-chip">${esc(c.alias)}</span>`).join("")}</td>
              <td class="num">${fmtBytes(g.wasted_bytes)}</td>
              <td class="dup-action"><button class="btn btn-secondary btn-sm" data-open="${i}"
                aria-expanded="${state.dup.open === i}">${state.dup.open === i ? "Hide" : "Review"}</button></td>
            </tr>
            ${state.dup.open === i ? dupDetailRow(g, i) : ""}`).join("")}
        </tbody>
      </table></div>
    </div>`;

  $("#dup-reread").addEventListener("click", () => { state.dup.data = null; renderDuplicates(); });

  $$("button[data-open]", v).forEach(b => b.addEventListener("click", () => {
    const i = Number(b.dataset.open);
    state.dup.open = state.dup.open === i ? -1 : i;
    renderDuplicates();
  }));

  $$('input[type=radio][name^="keep-"]', v).forEach(r => r.addEventListener("change", () => {
    state.dup.keep[state.dup.open] = Number(r.value);
    renderDuplicates();
  }));

  $$("button[data-del]", v).forEach(b => b.addEventListener("click", async () => {
    const i = Number(b.dataset.del);
    const g = groups[i];
    const keep = state.dup.keep[i] ?? newestCopyIndex(g);
    const doomed = g.copies.filter((_, j) => j !== keep);
    const risky = doomed.filter(c => c.shared || !c.owned_by_me);
    const go = await confirmModal({
      title: `Trash ${doomed.length} cop${doomed.length > 1 ? "ies" : "y"} of ${g.name}?`,
      body: `Kept in <strong>${esc(g.copies[keep].alias)}</strong>. Trashing ${fmtBytes(doomed.reduce((s, c) => s + c.size, 0))} from ${doomed.map(c => esc(c.alias)).join(", ")}. Drive holds trashed files for 30 days.`,
      warn: risky.length ? `${risky.length} of these ${risky.length > 1 ? "copies are" : "copy is"} shared with other people or owned by someone else — they lose access too.` : "",
      confirmLabel: "Move to trash",
      danger: true,
    });
    if (!go) return;
    b.disabled = true;
    b.textContent = "Trashing…";
    let ok = 0;
    for (const c of doomed) {
      try {
        await api(`/api/accounts/${encodeURIComponent(c.alias)}/files/${encodeURIComponent(c.file_id)}`,
          { method: "DELETE" });
        state.dup.deleted.add(c.alias + "/" + c.file_id);
        ok++;
      } catch (e) {
        toast(`${c.alias}: ${e.message}`, "error");
      }
    }
    if (ok) toast(`Trashed ${ok} cop${ok > 1 ? "ies" : "y"} · ${fmtBytesShort(g.size * ok)} freed`);
    renderDuplicates();
  }));
}

async function renderLedger() {
  const v = $("#view-ledger");
  v.innerHTML = `
    <div class="view-head">
      <div class="kicker" id="lg-kicker">local record · sha256 verified</div>
      <h2>Ledger</h2>
      <p>Every move Spillover has made, recorded locally. Search by filename to find where something went.</p></div>
    <div class="card panel">
      <div class="toolbar">
        <input class="input grow" id="lg-q" placeholder="Search by filename…" aria-label="Search ledger" autocomplete="off" spellcheck="false">
        <button class="btn btn-secondary" id="lg-search">Search</button>
        <button class="btn btn-primary" id="lg-verify">Verify transfers</button>
      </div>
      <div class="table-wrap"><table class="data"><thead><tr>
        <th scope="col">File</th><th scope="col" class="num">Size</th><th scope="col">From → To</th><th scope="col">Status</th><th scope="col">Verified</th><th scope="col">Moved</th>
      </tr></thead><tbody id="lg-body"></tbody></table></div>
    </div>`;

  let ledgerRows = [];

  async function load(q = "") {
    const rows = await api(`/api/ledger?q=${encodeURIComponent(q)}&limit=100`);
    ledgerRows = rows;
    $("#ledger-count").hidden = !rows.length;
    $("#ledger-count").textContent = rows.length;
    const kick = $("#lg-kicker");
    if (kick) kick.textContent = `${rows.length} entr${rows.length === 1 ? "y" : "ies"} · local record`;
    $("#lg-body").innerHTML = rows.map(r => `
      <tr data-fid="${esc(r.dst_file_id || "")}">
        <td><div class="fname" title="${esc(r.filename)}">${esc(r.filename)}</div>
          ${r.note ? `<div class="ledger-row-note" title="${esc(r.note)}">${esc(r.note)}</div>` : ""}</td>
        <td class="num">${fmtBytes(r.size_bytes)}</td>
        <td class="route"><span class="hop src" title="${esc(r.src_alias)}">${esc(r.src_alias)}</span>
          <span class="arrow" aria-hidden="true">→</span> <span class="hop" title="${esc(r.dst_alias)}">${esc(r.dst_alias)}</span></td>
        <td>${r.status === "done"
          ? `<span class="pill pill-success"><span class="dot"></span>Done</span>`
          : r.status === "skipped"
          ? `<span class="pill pill-warn"><span class="dot"></span>Skipped</span>`
          : `<span class="pill pill-danger"><span class="dot"></span>Failed</span>`}</td>
        <td class="verify-cell">${r.status === "done" && r.dst_file_id
          ? `<span style="color:var(--ink-tertiary);font-size:12px">—</span>`
          : ""}</td>
        <td style="color:var(--ink-tertiary);font-size:12px;white-space:nowrap">${esc(r.moved_at || "")}</td>
      </tr>`).join("") ||
      `<tr><td colspan="6"><div class="empty-state"><h3>No moves yet</h3><p>Finished rebalances and Takeout distributions will appear here.</p></div></td></tr>`;
  }

  $("#lg-search").addEventListener("click", () => load($("#lg-q").value.trim()));
  $("#lg-q").addEventListener("keydown", e => { if (e.key === "Enter") load(e.target.value.trim()); });
  $("#lg-verify").addEventListener("click", async () => {
    const btn = $("#lg-verify");
    btn.disabled = true; btn.textContent = "Verifying…";
    const done = ledgerRows.filter(r => r.status === "done" && r.dst_file_id);
    if (!done.length) { toast("No successful transfers to verify", "error"); btn.disabled = false; btn.textContent = "Verify transfers"; return; }
    try {
      const entries = done.map(r => ({ dst_alias: r.dst_alias, dst_file_id: r.dst_file_id, sha256: r.sha256, filename: r.filename }));
      const results = await api("/api/transfers/verify", { method: "POST", body: JSON.stringify({ entries }) });
      const byId = {};
      results.forEach(r => { byId[r.dst_file_id] = r; });
      $$("#lg-body tr").forEach(tr => {
        const fid = tr.dataset.fid;
        if (!fid || !byId[fid]) return;
        const v = byId[fid];
        const cell = tr.querySelector(".verify-cell");
        if (!cell) return;
        if (v.exists && v.hash_match !== false) {
          cell.innerHTML = `<a href="${esc(v.drive_link)}" target="_blank" rel="noopener" class="pill pill-success" style="text-decoration:none"><span class="dot"></span>Verified — view in Drive</a>`;
        } else if (v.exists && v.hash_match === false) {
          cell.innerHTML = `<a href="${esc(v.drive_link)}" target="_blank" rel="noopener" class="pill pill-warn" style="text-decoration:none"><span class="dot"></span>Hash mismatch — view</a>`;
        } else {
          cell.innerHTML = `<span class="pill pill-danger"><span class="dot"></span>Not found on Drive</span>`;
        }
      });
      const verified = results.filter(r => r.exists).length;
      toast(`Verified: ${verified}/${results.length} files confirmed on destination Drive`);
    } catch (e) { toast(e.message, "error"); }
    btn.disabled = false; btn.textContent = "Verify transfers";
  });
  try { await load(); } catch (e) { toast(e.message, "error"); }
}

/* ---------- boot ---------- */

async function boot() {
  try {
    const m = await api("/api/mode");
    state.demo = !!m.demo;
    if (state.demo) {
      $("#mode-pill").innerHTML = `<span class="demo-pill"><span class="dot"></span>Demo data</span>`;
    }
  } catch (e) { /* ignore */ }
  try {
    const v = await api("/api/version");
    if (v && v.version) $("#build-tag").textContent = "build " + v.version;
  } catch (e) { /* ignore */ }
  $$(".nav-item[data-route]").forEach(b =>
    b.addEventListener("click", () => navigate(b.dataset.route)));
  await refreshAccounts();
  render();
  if (!location.hash) location.hash = "#/accounts";
}

document.addEventListener("DOMContentLoaded", boot);
