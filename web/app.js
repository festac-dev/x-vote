"use strict";

// ----------------------------------------------------------------- helpers

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const state = {
  sessionId: null,
  competitionId: null,
  overview: null,
  leaderboard: null,
  leaderboardView: "confirmed",
  lbSearch: "",
  lbMethod: "",
  lbSort: "rank",
  projects: [],
  coverage: [],
  events: [],
  eventsPage: 1,
  eventsTotal: 0,
  drillProject: "",
  eventsPageSize: 25,
  polls: [],
  snapshots: [],
  templates: [],
  agents: [],
  exports: [],
  settings: null,
  fixtures: false,
  agentsPaused: false,
  error: "",
  wizard: null,
};

const SECTION_LOADERS = {
  overview: loadOverview,
  competitions: loadCompetitions,
  newcheck: loadWizard,
  sources: loadSources,
  events: loadEventsPanel,
  leaderboard: () => loadLeaderboard(state.leaderboardView),
  agents: loadAgents,
  review: loadReview,
  exports: loadExports,
  settings: loadSettings,
};

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function fmtNumber(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return esc(value);
  return number.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function fmtDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return esc(value);
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function fmtDuration(seconds) {
  if (!seconds || seconds < 0) return "0s";
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

function humanStatus(value) {
  return String(value || "unknown").replaceAll("_", " ");
}

function statusKind(value) {
  const status = String(value || "");
  if (["verified", "eligible", "complete_for_scope", "ok", "finalized"].includes(status)) return "ok";
  if (
    ["pending_review", "pending_eligibility_review", "eligibility_unknown", "disputed", "inconclusive",
     "partially_complete", "manually_supplied", "provisional", "collecting", "in_review"].includes(status)
  ) return "warn";
  if (
    ["ineligible", "rejected", "failed", "source_unavailable", "permission_restricted", "rate_limited"].includes(status)
  ) return "bad";
  if (["not_individually_verifiable", "not_started", "unknown"].includes(status)) return "info";
  return "neutral";
}

function badge(value, extra = "") {
  if (!value) return "";
  return `<span class="badge ${extra || statusKind(value)}">${esc(humanStatus(value))}</span>`;
}

function methodLabel(method) {
  return {
    native_poll: "Poll", reply: "Reply", like: "Like", repost: "Repost", quote: "Quote",
    mention: "Mention", hashtag: "Hashtag", keyword: "Keyword", manual_total: "Manual total",
    ocr_total: "OCR total", external_aggregate: "External aggregate", custom: "Custom",
  }[method] || humanStatus(method);
}

function toast(message, isError = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast${isError ? " error" : ""}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => node.classList.add("hidden"), isError ? 7000 : 3800);
}

function setError(message) {
  state.error = message || "";
  renderBanner();
}

function renderBanner() {
  const node = $("#global-banner");
  const isFixture = Boolean(state.overview?.fixture_mode);
  const banner = state.overview?.test_data_banner || "SYNTHETIC TEST DATA — NOT LIVE X RESULTS";
  if (state.error) {
    node.className = "banner error";
    node.textContent = state.error;
    node.classList.remove("hidden");
  } else if (isFixture) {
    node.className = "banner testdata";
    node.textContent = `${banner} · For evaluation and tests only.`;
    node.classList.remove("hidden");
  } else {
    node.className = "banner hidden";
    node.textContent = "";
  }
}

async function api(path, { method = "GET", body = null } = {}) {
  const headers = { "X-Requested-With": "x-vote" };
  if (body !== null) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { method, headers, body: body === null ? undefined : JSON.stringify(body) });
  const type = response.headers.get("content-type") || "";
  let payload;
  try {
    payload = type.includes("json") ? await response.json() : await response.text();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const message = payload?.error?.message || payload?.error?.code || `Request failed (${response.status})`;
    throw new Error(message);
  }
  return payload;
}

async function guard(fn, successMessage) {
  try {
    setError("");
    const result = await fn();
    if (successMessage) toast(successMessage);
    return result;
  } catch (error) {
    toast(error.message || String(error), true);
    return null;
  }
}

function askConfirm({ title = "Confirm", message = "", requireText = "" }) {
  return new Promise((resolve) => {
    const dialog = $("#confirm-dialog");
    $("#confirm-title").textContent = title;
    $("#confirm-message").textContent = message;
    const wrap = $("#confirm-input-wrap");
    const input = $("#confirm-input");
    if (requireText) {
      wrap.classList.remove("hidden");
      input.value = "";
      $("#confirm-message").textContent = `${message} Type “${requireText}” to continue.`;
    } else {
      wrap.classList.add("hidden");
      input.value = "";
    }
    const onClose = () => {
      dialog.removeEventListener("close", onClose);
      resolve(dialog.returnValue === "ok" && (!requireText || input.value.trim() === requireText));
    };
    dialog.addEventListener("close", onClose);
    dialog.showModal();
  });
}

// -------------------------------------------------------------- boot / nav

async function boot() {
  bindGlobalEvents();
  const [settings, templates] = await Promise.all([
    guard(() => api("/api/settings")),
    guard(() => api("/api/templates")),
  ]);
  if (!settings) return;
  state.settings = settings;
  state.templates = templates?.templates || [];
  const saved = sessionStorage.getItem("xvote.session");
  if (saved && settings.sessions.some((session) => session.session_id === saved)) {
    await enterSession(saved);
  } else {
    showWelcome();
  }
}

function showWelcome() {
  state.sessionId = null;
  state.competitionId = null;
  state.overview = null;
  sessionStorage.removeItem("xvote.session");
  $("#mainnav").classList.add("hidden");
  $$(".panel").forEach((panel) => panel.classList.add("hidden"));
  $("#welcome").classList.remove("hidden");
  $("#session-chip").textContent = "No session";
  renderBanner();
}

function updateNavVisibility() {
  const hasSession = Boolean(state.sessionId);
  $("#mainnav").classList.toggle("hidden", !hasSession);
  const competitionSections = ["sources", "events", "leaderboard", "agents", "review", "exports"];
  $$("#mainnav .navlink").forEach((link) => {
    const section = link.dataset.section;
    const needsCompetition = competitionSections.includes(section);
    link.classList.toggle("hidden", !hasSession || (needsCompetition && !state.competitionId));
  });
}

async function enterSession(sessionId) {
  state.sessionId = sessionId;
  sessionStorage.setItem("xvote.session", sessionId);
  const overview = await guard(() => api(`/api/sessions/${sessionId}/overview`));
  if (!overview) {
    showWelcome();
    return;
  }
  state.overview = overview;
  state.fixtures = Boolean(overview.fixture_mode);
  state.competitionId = overview.competition?.competition_id || null;
  $("#session-chip").textContent = `Session ${sessionId.slice(-8)}`;
  $("#session-chip").title = sessionId;
  updateNavVisibility();
  renderBanner();
  if (state.competitionId) {
    await refreshProjects();
  }
  showSection("overview");
}

async function refreshSession() {
  if (!state.sessionId) return;
  const overview = await guard(() => api(`/api/sessions/${state.sessionId}/overview`));
  if (!overview) return;
  state.overview = overview;
  state.fixtures = Boolean(overview.fixture_mode);
  state.competitionId = overview.competition?.competition_id || state.competitionId;
  updateNavVisibility();
  renderBanner();
  if (state.competitionId) await refreshProjects();
}

function showSection(name) {
  if (!state.sessionId) {
    showWelcome();
    return;
  }
  $$(".panel").forEach((panel) => panel.classList.add("hidden"));
  const target = $(`#panel-${name}`);
  if (!target) return;
  target.classList.remove("hidden");
  $$("#mainnav .navlink").forEach((link) => link.classList.toggle("is-active", link.dataset.section === name));
  updateNavVisibility();
  const loader = SECTION_LOADERS[name];
  if (loader) guard(loader);
}

async function refreshProjects() {
  if (!state.sessionId || !state.competitionId) return;
  const response = await guard(() => api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/leaderboard?view=confirmed`));
  if (!response) return;
  state.leaderboard = response.leaderboard;
  state.projects = response.leaderboard.entries.map((entry) => ({
    id: entry.project_id,
    name: entry.name,
    external_id: entry.external_id,
  }));
  fillProjectSelects();
}

function fillProjectSelects() {
  for (const select of [$("#events-project")]) {
    if (!select) continue;
    const current = select.value;
    select.innerHTML = `<option value="">All projects</option>` +
      state.projects.map((project) => `<option value="${esc(project.id)}">${esc(project.name)}</option>`).join("");
    if (state.projects.some((project) => project.id === current)) select.value = current;
  }
}

// ---------------------------------------------------------------- overview

async function loadOverview() {
  if (!state.sessionId) return;
  $("#overview-content").innerHTML = `<p class="loading">Loading overview…</p>`;
  await refreshSession();
  const overview = state.overview;
  if (!overview) return;
  if (!overview.competition) {
    $("#overview-content").innerHTML = `
      <div class="card">
        <h2>No competition configured</h2>
        <p>Create a check to define projects, sources, methods, eligibility and ranking rules.
        Nothing is collected until you review and confirm the configuration.</p>
        <div class="actions">
          <button type="button" class="button button-primary" data-action="goto-newcheck">New check</button>
          <button type="button" class="button" data-action="demo">Run synthetic demo</button>
        </div>
      </div>`;
    return;
  }
  const competition = overview.competition;
  const lb = state.leaderboard || {};
  const scoreLabel = lb.score_label || "confirmed votes";
  $("#overview-content").innerHTML = `
    <div class="badges">
      ${badge(overview.collection_status)}
      ${badge(competition.status)}
      ${overview.fixture_mode ? `<span class="badge accent">test data</span>` : ""}
    </div>
    <div class="tile-grid">
      <div class="tile"><span class="tile-label">Observed activity</span><span class="tile-value">${fmtNumber(overview.total_observed_activity)}</span><span class="tile-note">raw activity — never valid votes</span></div>
      <div class="tile ok"><span class="tile-label">Confirmed</span><span class="tile-value">${fmtNumber(overview.confirmed_votes)}</span><span class="tile-note">${fmtNumber(overview.confirmed_individual)} individual · ${fmtNumber(overview.confirmed_aggregate)} aggregate</span></div>
      <div class="tile warn"><span class="tile-label">Provisional</span><span class="tile-value">${fmtNumber(overview.provisional_votes)}</span><span class="tile-note">unresolved, not confirmed</span></div>
      <div class="tile bad"><span class="tile-label">Rejected / late</span><span class="tile-value">${fmtNumber(overview.rejected_or_late_activity)}</span><span class="tile-note">excluded by rules</span></div>
      <div class="tile warn"><span class="tile-label">Pending review</span><span class="tile-value">${fmtNumber(overview.pending_review)}</span><span class="tile-note">${fmtNumber(overview.ambiguous)} ambiguous project(s)</span></div>
      <div class="tile"><span class="tile-label">Projects</span><span class="tile-value">${fmtNumber(competition.project_count)}</span><span class="tile-note">${esc(competition.methods.map(methodLabel).join(", "))}</span></div>
      <div class="tile"><span class="tile-label">Sources</span><span class="tile-value">${fmtNumber(competition.source_count)}</span><span class="tile-note">coverage: ${esc(humanStatus(overview.collection_status))}</span></div>
      <div class="tile"><span class="tile-label">Session</span><span class="tile-value">${fmtDuration(overview.session_duration_seconds)}</span><span class="tile-note">cleanup: ${esc(overview.cleanup_status)}</span></div>
    </div>
    <div class="card">
      <div class="section-head">
        <div>
          <p class="eyebrow">Ranking status</p>
          <h2>${esc(competition.name)}</h2>
        </div>
        <div class="actions">
          <button type="button" class="button button-primary" data-action="collect">Collect now</button>
          <button type="button" class="button" data-action="goto-leaderboard">Open leaderboard</button>
          <button type="button" class="button" data-action="capture-snapshot">Capture snapshot</button>
        </div>
      </div>
      <p class="meta">Metric: <strong>${esc(lb.metric || "confirmed votes")}</strong> · Score column: ${esc(scoreLabel)} · Finalization: ${badge(competition.status)}</p>
      <p class="meta">Window: ${fmtDate(competition.starts_at)} → ${fmtDate(competition.ends_at)} (${esc(competition.timezone)}) · Ruleset ${esc(competition.ruleset_version)}</p>
      <p class="metric-detail">${esc(lb.formula || "Ranking formula is available on the leaderboard.")}</p>
      <p class="meta">Last collection: ${fmtDate(overview.last_collection_at)} · Last calculated: ${fmtDate(overview.last_calculated_at)}</p>
      ${overview.limitations?.length ? `<details><summary>Limitations and coverage warnings (${overview.limitations.length})</summary><ul class="warning-list">${overview.limitations.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></details>` : ""}
    </div>
    ${overview.exports?.length ? `<div class="card"><h2>Exports</h2>${overview.exports.map((record) => renderExportRecord(record)).join("")}</div>` : ""}
  `;
}

// ------------------------------------------------------------ competitions

async function loadCompetitions() {
  if (!state.sessionId) return;
  const container = $("#competitions-content");
  container.innerHTML = `<p class="loading">Loading competitions…</p>`;
  const response = await guard(() => api(`/api/sessions/${state.sessionId}/competitions`));
  if (!response) return;
  if (!response.competitions.length) {
    container.innerHTML = `<div class="card"><p>No competition configured.</p>
      <button type="button" class="button button-primary" data-action="goto-newcheck">New check</button></div>`;
    return;
  }
  container.innerHTML = response.competitions.map((competition) => `
    <div class="card">
      <div class="badges">${badge(competition.status)}<span class="badge info">ruleset ${esc(competition.ruleset_version)}</span></div>
      <h2>${esc(competition.name)}</h2>
      <p class="meta">${esc(competition.description || "No description.")}</p>
      <div class="form-grid">
        <div class="field"><span>Window</span><div>${fmtDate(competition.starts_at)} → ${fmtDate(competition.ends_at)}</div></div>
        <div class="field"><span>Timezone</span><div>${esc(competition.timezone_name)}</div></div>
        <div class="field"><span>Methods</span><div>${esc(competition.methods.map(methodLabel).join(", "))}</div></div>
        <div class="field"><span>Ranking</span><div>${esc(competition.ranking.mode)}${competition.ranking.combine_confirmed_with_aggregates ? " (combined approved)" : " (aggregates separate)"}</div></div>
        <div class="field"><span>Projects</span><div>${competition.projects.map((p) => esc(p.name)).join(", ")}</div></div>
        <div class="field"><span>Sources</span><div>${competition.sources.length} configured</div></div>
      </div>
      <details><summary>Advanced configuration editor</summary>
        <p class="meta">Edits are validated and re-evaluate existing evidence under a new ruleset version; nothing is re-collected. Use it for identifiers, source methods, weights and rules not exposed in the wizard.</p>
        <textarea class="advanced-config" data-id="${esc(competition.competition_id)}" rows="22" spellcheck="false">${esc(JSON.stringify(competition, null, 2))}</textarea>
        <button type="button" class="button button-primary" data-action="save-config" data-id="${esc(competition.competition_id)}">Save configuration</button>
      </details>
    </div>`).join("");
}

// ------------------------------------------------------------------ wizard

const WIZARD_STEPS = ["Details", "Projects", "Sources", "Methods", "Eligibility", "Evidence", "Ranking", "Review"];

function defaultDraft() {
  return {
    name: "",
    description: "",
    timezone_name: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
    starts_at: "",
    ends_at: "",
    projects: [{ name: "", aliases: "", external_id: "", usernames: "", hashtags: "", keywords: "", domains: "" }],
    sources: [{ source_type: "x_post", value: "", label: "", project_name: "" }],
    methods: ["reply"],
    eligibility: {
      first_or_last: "first",
      one_vote_per_account: true,
      edits_affect_eligibility: true,
      deleted_content_remains_eligible: false,
      allow_multiple_projects: false,
      conflicting_votes_require_review: true,
      unresolved_provisional: true,
      require_support_statement: true,
      manual_overrides: true,
    },
    evidence: {
      require_source_url: true,
      require_timestamp: true,
      require_identity_for_votes: false,
      manual_requires_review: true,
      minimum_confidence: "low",
    },
    ranking: {
      mode: "confirmed",
      combine_confirmed_with_aggregates: false,
      weights: {},
      custom_formula: "",
    },
    collection: {
      frequency_seconds: 0,
      permitted_duration_seconds: 0,
      storage_mode: "auto",
      snapshot_enabled: true,
    },
  };
}

async function loadWizard() {
  if (!state.wizard) {
    state.wizard = { step: 0, draft: defaultDraft(), notes: [], blocked: false, createAnother: false };
  }
  renderWizard();
}

function wizardStepValid(step) {
  const draft = state.wizard.draft;
  const errors = [];
  if (step === 0) {
    if (!draft.name.trim()) errors.push("Competition name is required.");
    if (!draft.ends_at) errors.push("Voting deadline is required.");
  }
  if (step === 1) {
    const names = draft.projects.map((p) => p.name.trim()).filter(Boolean);
    if (!names.length) errors.push("At least one project is required.");
    if (new Set(names.map((n) => n.toLowerCase())).size !== names.length) errors.push("Project names must be unique.");
  }
  if (step === 2) {
    if (!draft.sources.some((s) => parsePostRef(s.value) || s.value.trim())) errors.push("At least one source is required.");
    for (const source of draft.sources) {
      if (!source.value.trim() && !source.label.trim()) continue;
      if (source.source_type === "x_post" && !parsePostRef(source.value)) errors.push(`Invalid X post reference: ${source.value}`);
    }
  }
  if (step === 3 && !draft.methods.length) errors.push("At least one voting method is required.");
  if (step === 6) {
    if (draft.ranking.mode === "custom" && !draft.ranking.custom_formula.trim()) errors.push("A custom formula is required.");
    const mixed = draft.methods.includes("native_poll") &&
      draft.methods.some((m) => !["native_poll", "manual_total", "external_aggregate", "ocr_total"].includes(m));
    if (mixed && draft.ranking.mode === "confirmed" && !draft.ranking.combine_confirmed_with_aggregates) {
      // This is allowed: counts remain separate. Only a note is shown.
    }
  }
  return errors;
}

function renderWizard(message = "") {
  const container = $("#wizard");
  const wizard = state.wizard;
  const step = wizard.step;
  const draft = wizard.draft;
  container.innerHTML = `
    <div class="stepbar">${WIZARD_STEPS.map((label, index) =>
      `<button type="button" data-action="wizard-step" data-step="${index}" class="${index === step ? "is-active" : index < step ? "is-done" : ""}">${index + 1}. ${label}</button>`
    ).join("")}</div>
    <div class="wizard-panel">
      ${message ? `<div class="banner error">${esc(message)}</div>` : ""}
      ${wizard.notes.length ? `<ul class="warning-list">${wizard.notes.map((note) => `<li>${esc(note)}</li>`).join("")}</ul>` : ""}
      ${renderWizardStep(step, draft)}
      <div class="wizard-actions">
        <button type="button" class="button" data-action="wizard-prev" ${step === 0 ? "disabled" : ""}>Back</button>
        <div class="actions">
          <button type="button" class="button" data-action="wizard-cancel">Cancel</button>
          ${step < WIZARD_STEPS.length - 1
            ? `<button type="button" class="button button-primary" data-action="wizard-next">Continue</button>`
            : `<button type="button" class="button button-primary" data-action="wizard-create">Create competition</button>`}
        </div>
      </div>
    </div>`;
}

function renderWizardStep(step, draft) {
  if (step === 0) {
    return `
      <h2>Competition details</h2>
      <p class="meta">Set the window, timezone and public framing. Timestamps use the selected timezone.</p>
      ${state.templates.length ? `
        <div class="template-box">
          <label class="field"><span>Start from a template (optional)</span>
            <select id="wiz-template">
              <option value="">Custom configuration</option>
              ${state.templates.map((template) => `<option value="${esc(template.id)}">${esc(template.name)}</option>`).join("")}
            </select>
          </label>
          <button type="button" class="button" data-action="wizard-template">Apply template defaults</button>
          <p class="fineprint">Templates only prefill methods, source type and rule defaults. Review everything before collection.</p>
        </div>` : ""}
      <div class="form-grid">
        <label class="field wide"><span>Name</span><input id="wiz-name" type="text" value="${esc(draft.name)}" placeholder="e.g. Community choice awards"></label>
        <label class="field wide"><span>Description (optional)</span><textarea id="wiz-description" placeholder="What is being voted on?">${esc(draft.description)}</textarea></label>
        <label class="field"><span>Timezone</span><input id="wiz-timezone" type="text" value="${esc(draft.timezone_name)}" placeholder="Africa/Lagos"></label>
        <label class="field"><span>Starts at</span><input id="wiz-starts" type="datetime-local" value="${esc(draft.starts_at)}"></label>
        <label class="field"><span>Voting deadline</span><input id="wiz-ends" type="datetime-local" value="${esc(draft.ends_at)}"></label>
        <label class="field wide"><span>Plain-language assist (deterministic draft)</span><textarea id="wiz-interpret" placeholder="Count votes for Alpha, Beta and Gamma from this X post, include replies, likes, reposts and the poll, and close at 8 PM West Africa Time."></textarea></label>
      </div>
      <div class="actions">
        <button type="button" class="button" data-action="wizard-interpret">Interpret instruction</button>
        <span class="hint">Draft only. Nothing is activated without the review step.</span>
      </div>`;
  }
  if (step === 1) {
    return `
      <h2>Projects</h2>
      <p class="meta">Names are matched with word boundaries. Aliases, usernames, hashtags, keywords and domains are optional matching aids; ambiguous matches go to review, never silent confirmation.</p>
      <div id="wiz-projects">
        ${draft.projects.map((project, index) => `
          <div class="project-row" data-index="${index}">
            <label class="field"><span>Project name</span><input class="wiz-project-name" type="text" value="${esc(project.name)}"></label>
            <label class="field"><span>Aliases (comma-separated)</span><input class="wiz-project-aliases" type="text" value="${esc(project.aliases)}"></label>
            <label class="field"><span>External ID (optional)</span><input class="wiz-project-external" type="text" value="${esc(project.external_id)}"></label>
            <button type="button" class="button button-small" data-action="wizard-remove-project" data-index="${index}">Remove</button>
            <div class="project-extra">
              <label class="field"><span>Usernames</span><input class="wiz-project-usernames" type="text" value="${esc(project.usernames)}" placeholder="alpha_project, alpha_team"></label>
              <label class="field"><span>Hashtags</span><input class="wiz-project-hashtags" type="text" value="${esc(project.hashtags)}" placeholder="VoteAlpha"></label>
              <label class="field"><span>Keywords</span><input class="wiz-project-keywords" type="text" value="${esc(project.keywords)}" placeholder="Alpha campaign"></label>
              <label class="field"><span>Domains</span><input class="wiz-project-domains" type="text" value="${esc(project.domains)}" placeholder="alpha.example"></label>
            </div>
          </div>`).join("")}
      </div>
      <button type="button" class="button" data-action="wizard-add-project">Add project</button>`;
  }
  if (step === 2) {
    return `
      <h2>Sources</h2>
      <p class="meta">Use X post URLs or bare numeric post IDs. For project-specific like/repost checks, associate the post with a project.</p>
      <div id="wiz-sources">
        ${draft.sources.map((source, index) => `
          <div class="source-row" data-index="${index}">
            <label class="field"><span>URL or post ID</span><input class="wiz-source-value" type="text" value="${esc(source.value)}"></label>
            <label class="field"><span>Type</span>
              <select class="wiz-source-type">${["x_post", "replies", "likes", "reposts", "poll", "quotes", "mentions", "hashtags", "keywords", "manual", "external", "ocr"].map((type) =>
                `<option value="${type}" ${source.source_type === type ? "selected" : ""}>${humanStatus(type)}</option>`).join("")}
              </select>
            </label>
            <label class="field"><span>Project (optional)</span>
              <select class="wiz-source-project"><option value="">Campaign / all projects</option>${draft.projects.filter((p) => p.name.trim()).map((p) =>
                `<option value="${esc(p.name)}" ${source.project_name === p.name ? "selected" : ""}>${esc(p.name)}</option>`).join("")}
              </select>
            </label>
            <button type="button" class="button button-small" data-action="wizard-remove-source" data-index="${index}">Remove</button>
          </div>`).join("")}
      </div>
      <button type="button" class="button" data-action="wizard-add-source">Add source</button>
      <p class="fineprint">Unavailable polls or failed adapters are reported as explicit limitations, never fabricated or manually replaced without a labelled manual import.</p>`;
  }
  if (step === 3) {
    const methods = [
      ["native_poll", "Native poll (anonymous aggregate)"], ["reply", "Replies"],
      ["like", "Likes"], ["repost", "Reposts"], ["quote", "Quote posts"],
      ["mention", "Mentions"], ["hashtag", "Hashtags"], ["keyword", "Keywords"],
      ["manual_total", "Manual totals"], ["ocr_total", "OCR totals"], ["external_aggregate", "External aggregates"],
    ];
    return `
      <h2>Voting methods</h2>
      <div class="choice-grid">${methods.map(([value, label]) =>
        `<label class="choice"><input type="checkbox" class="wiz-method" value="${value}" ${draft.methods.includes(value) ? "checked" : ""}> ${esc(label)}</label>`).join("")}</div>
      <p class="fineprint">Poll aggregates are anonymous and never presented as individually verified. Like/repost counts are observed accounts, not unique humans.</p>`;
  }
  if (step === 4) {
    return `
      <h2>Eligibility and duplicates</h2>
      <div class="choice-grid">
        <label class="choice">One vote per account <input id="wiz-one-vote" type="checkbox" ${draft.eligibility.one_vote_per_account ? "checked" : ""}></label>
        <label class="choice">Allow multiple projects <input id="wiz-multiple-projects" type="checkbox" ${draft.eligibility.allow_multiple_projects ? "checked" : ""}></label>
        <label class="choice">Conflicting projects require review <input id="wiz-conflict-review" type="checkbox" ${draft.eligibility.conflicting_votes_require_review ? "checked" : ""}></label>
        <label class="choice">Unresolved stays provisional <input id="wiz-unresolved" type="checkbox" ${draft.eligibility.unresolved_provisional ? "checked" : ""}></label>
        <label class="choice">Require explicit support statement <input id="wiz-support" type="checkbox" ${draft.eligibility.require_support_statement ? "checked" : ""}></label>
        <label class="choice">Manual overrides allowed <input id="wiz-overrides" type="checkbox" ${draft.eligibility.manual_overrides ? "checked" : ""}></label>
        <label class="choice">Edited content requires re-review <input id="wiz-edits" type="checkbox" ${draft.eligibility.edits_affect_eligibility ? "checked" : ""}></label>
        <label class="choice">Keep deleted content eligible <input id="wiz-deleted" type="checkbox" ${draft.eligibility.deleted_content_remains_eligible ? "checked" : ""}></label>
      </div>
      <label class="field"><span>When an account submits more than once</span>
        <select id="wiz-first-last">
          <option value="first" ${draft.eligibility.first_or_last === "first" ? "selected" : ""}>Keep first valid submission</option>
          <option value="last" ${draft.eligibility.first_or_last === "last" ? "selected" : ""}>Keep last valid submission</option>
          <option value="all" ${draft.eligibility.first_or_last === "all" ? "selected" : ""}>Evaluate all separately (per method)</option>
        </select>
      </label>`;
  }
  if (step === 5) {
    return `
      <h2>Evidence and verification</h2>
      <div class="choice-grid">
        <label class="choice">Require source URL <input id="wiz-source-url" type="checkbox" ${draft.evidence.require_source_url ? "checked" : ""}></label>
        <label class="choice">Require timestamp <input id="wiz-timestamp" type="checkbox" ${draft.evidence.require_timestamp ? "checked" : ""}></label>
        <label class="choice">Require identity for votes <input id="wiz-identity" type="checkbox" ${draft.evidence.require_identity_for_votes ? "checked" : ""}></label>
        <label class="choice">Manual imports require review <input id="wiz-manual-review" type="checkbox" ${draft.evidence.manual_requires_review ? "checked" : ""}></label>
      </div>
      <label class="field"><span>Minimum confidence</span>
        <select id="wiz-confidence">${["low", "moderate", "high"].map((level) =>
          `<option value="${level}" ${draft.evidence.minimum_confidence === level ? "selected" : ""}>${level}</option>`).join("")}
        </select>
      </label>
      <p class="fineprint">Lower-confidence observations remain visible but do not silently become confirmed votes.</p>`;
  }
  if (step === 6) {
    const mixedAggregate = draft.methods.some((m) => ["native_poll", "manual_total", "external_aggregate", "ocr_total"].includes(m));
    const mixedIndividual = draft.methods.some((m) => !["native_poll", "manual_total", "external_aggregate", "ocr_total"].includes(m));
    const mixed = mixedAggregate && mixedIndividual;
    return `
      <h2>Ranking</h2>
      <label class="field"><span>Ranking mode</span>
        <select id="wiz-ranking-mode">
          ${[["confirmed", "Confirmed votes"], ["provisional", "Provisional counts"], ["raw", "Raw activity"],
             ["unique_account", "Unique observed accounts"], ["weighted", "Weighted score"], ["custom", "Custom formula"], ["separate", "Separate counts only"]].map(([value, label]) =>
            `<option value="${value}" ${draft.ranking.mode === value ? "selected" : ""}>${label}</option>`).join("")}
        </select>
      </label>
      ${mixed ? `
        <div class="metric-banner">
          <p class="metric-title">This competition mixes aggregate and individual measures.</p>
          <p class="metric-detail">By default they remain separate: the score ranks confirmed individual events and aggregate totals are shown alongside. Nothing is silently summed.</p>
          <label class="choice"><input id="wiz-combine" type="checkbox" ${draft.ranking.combine_confirmed_with_aggregates ? "checked" : ""}> Explicitly approve a combined confirmed sum (individual events + source-reported aggregates)</label>
        </div>` : ""}
      <div class="form-grid">
        <label class="field"><span>Reply weight (weighted mode)</span><input id="wiz-weight-reply" type="number" min="0" step="0.1" value="${draft.ranking.weights.reply ?? 1}"></label>
        <label class="field"><span>Like weight</span><input id="wiz-weight-like" type="number" min="0" step="0.1" value="${draft.ranking.weights.like ?? 1}"></label>
        <label class="field"><span>Repost weight</span><input id="wiz-weight-repost" type="number" min="0" step="0.1" value="${draft.ranking.weights.repost ?? 1}"></label>
        <label class="field"><span>Poll weight</span><input id="wiz-weight-poll" type="number" min="0" step="0.1" value="${draft.ranking.weights.native_poll ?? 1}"></label>
      </div>
      <label class="field"><span>Custom formula (safe arithmetic: confirmed, provisional, raw, accounts, poll, manual, ocr, external, method names)</span>
        <input id="wiz-formula" type="text" value="${esc(draft.ranking.custom_formula)}" placeholder="confirmed + poll * 0.5">
      </label>
      <p class="fineprint">Every leaderboard states the metric, denominator, sources, coverage and finalization status. A weighted score is never labelled as a number of votes.</p>
      <h3>Collection and temporary storage (advanced)</h3>
      <div class="form-grid">
        <label class="field"><span>Collection frequency (seconds, 0 = manual only)</span><input id="wiz-frequency" type="number" min="0" step="1" value="${esc(draft.collection.frequency_seconds)}"></label>
        <label class="field"><span>Permitted collection duration (seconds, 0 = no limit)</span><input id="wiz-duration" type="number" min="0" step="1" value="${esc(draft.collection.permitted_duration_seconds)}"></label>
      </div>
      <label class="choice"><input id="wiz-snapshot" type="checkbox" ${draft.collection.snapshot_enabled ? "checked" : ""}> Capture a ranking snapshot after each collection</label>
      <p class="fineprint">Frequency is a saved policy preference; collection is still operator-started. Choose the active session storage on the welcome screen; all storage remains temporary and is deleted by cleanup.</p>`;
  }
  return renderWizardReview(draft);
}

function renderWizardReview(draft) {
  const config = buildWizardConfig(draft);
  const warnings = [];
  if (config.ranking.combine_confirmed_with_aggregates) {
    warnings.push("The combined confirmed sum mixes anonymous aggregates with individually evidenced activity. This is explicitly approved in the configuration.");
  }
  if (config.methods.includes("native_poll")) {
    warnings.push("Native poll votes are anonymous source-reported aggregates; they cannot be individually verified.");
  }
  if (config.methods.some((m) => ["like", "repost"].includes(m))) {
    warnings.push("Like/repost activity counts observed accounts, never unique humans.");
  }
  return `
    <h2>Review and confirm</h2>
    <p class="meta">Check the configuration carefully. Collection cannot start until you confirm.</p>
    <div class="review-json"><pre>${esc(JSON.stringify(config, null, 2))}</pre></div>
    ${warnings.length ? `<ul class="warning-list">${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
    <ul class="warning-list">
      <li>Empty sources or methods are reported as unavailable — never fabricated.</li>
      <li>Live collection requires an authorized-use acknowledgement in the environment.</li>
      <li>Anything unconfirmed remains provisional and is shown separately.</li>
    </ul>
    <label class="choice"><input id="wiz-confirm" type="checkbox"> I reviewed the projects, sources, methods, rules and ranking formula.</label>`;
}

function parsePostRef(value) {
  const text = String(value || "").trim();
  if (/^\d{3,25}$/.test(text)) {
    return { post_id: text, url: `https://x.com/i/web/status/${text}` };
  }
  const match = text.match(/(?:x\.com|twitter\.com)\/(?:[^/\s]+\/)?status(?:es)?\/(\d{3,25})/i);
  if (match) {
    return {
      post_id: match[1],
      url: /^https?:\/\//i.test(text) ? text : `https://${text.replace(/^\/+/, "")}`,
    };
  }
  return null;
}

function buildWizardConfig(draft) {
  const list = (value) => String(value || "").split(",").map((item) => item.trim()).filter(Boolean);
  const projects = draft.projects
    .filter((project) => project.name.trim())
    .map((project) => ({
      name: project.name.trim(),
      aliases: list(project.aliases),
      external_id: project.external_id.trim(),
      usernames: list(project.usernames),
      hashtags: list(project.hashtags),
      keywords: list(project.keywords),
      domains: list(project.domains),
    }));
  const projectIdByName = new Map(projects.map((p) => [p.name, ""]));
  const sources = draft.sources
    .filter((source) => source.value.trim() || source.label.trim())
    .map((source) => {
      const reference = parsePostRef(source.value);
      return {
        source_type: source.source_type,
        post_id: reference ? reference.post_id : "",
        url: reference ? reference.url : (/^https?:\/\//.test(source.value) ? source.value : ""),
        label: source.label || (reference ? `X post ${reference.post_id}` : humanStatus(source.source_type)),
        methods: draft.methods,
        project_name: source.project_name,
      };
    });
  const methods = [...draft.methods];
  return {
    name: draft.name.trim(),
    description: draft.description.trim(),
    timezone_name: draft.timezone_name.trim() || "UTC",
    starts_at: draft.starts_at ? `${draft.starts_at}:00` : "",
    ends_at: draft.ends_at ? `${draft.ends_at}:00` : "",
    projects,
    sources: sources.map((source) => ({ ...source, project_name: source.project_name || "" })),
    methods,
    eligibility: {
      ...draft.eligibility,
      allowed_methods: methods,
    },
    evidence: { ...draft.evidence },
    ranking: {
      mode: draft.ranking.mode,
      weights: draft.ranking.weights,
      custom_formula: draft.ranking.custom_formula,
      combine_confirmed_with_aggregates: draft.ranking.combine_confirmed_with_aggregates,
    },
    collection: { ...draft.collection },
  };
}

function readWizardStep() {
  const draft = state.wizard.draft;
  const step = state.wizard.step;
  if (step === 0) {
    draft.name = $("#wiz-name").value.trim();
    draft.description = $("#wiz-description").value.trim();
    draft.timezone_name = $("#wiz-timezone").value.trim();
    draft.starts_at = $("#wiz-starts").value;
    draft.ends_at = $("#wiz-ends").value;
  } else if (step === 1) {
    draft.projects = $$(".project-row").map((row) => ({
      name: $(".wiz-project-name", row).value.trim(),
      aliases: $(".wiz-project-aliases", row).value.trim(),
      external_id: $(".wiz-project-external", row).value.trim(),
      usernames: $(".wiz-project-usernames", row).value.trim(),
      hashtags: $(".wiz-project-hashtags", row).value.trim(),
      keywords: $(".wiz-project-keywords", row).value.trim(),
      domains: $(".wiz-project-domains", row).value.trim(),
    }));
  } else if (step === 2) {
    draft.sources = $$(".source-row").map((row) => ({
      value: $(".wiz-source-value", row).value.trim(),
      source_type: $(".wiz-source-type", row).value,
      project_name: $(".wiz-source-project", row).value,
      label: "",
    }));
  } else if (step === 3) {
    draft.methods = $$(".wiz-method").filter((box) => box.checked).map((box) => box.value);
  } else if (step === 4) {
    draft.eligibility.one_vote_per_account = $("#wiz-one-vote").checked;
    draft.eligibility.allow_multiple_projects = $("#wiz-multiple-projects").checked;
    draft.eligibility.conflicting_votes_require_review = $("#wiz-conflict-review").checked;
    draft.eligibility.unresolved_provisional = $("#wiz-unresolved").checked;
    draft.eligibility.require_support_statement = $("#wiz-support").checked;
    draft.eligibility.manual_overrides = $("#wiz-overrides").checked;
    draft.eligibility.edits_affect_eligibility = $("#wiz-edits").checked;
    draft.eligibility.deleted_content_remains_eligible = $("#wiz-deleted").checked;
    draft.eligibility.first_or_last = $("#wiz-first-last").value;
  } else if (step === 5) {
    draft.evidence.require_source_url = $("#wiz-source-url").checked;
    draft.evidence.require_timestamp = $("#wiz-timestamp").checked;
    draft.evidence.require_identity_for_votes = $("#wiz-identity").checked;
    draft.evidence.manual_requires_review = $("#wiz-manual-review").checked;
    draft.evidence.minimum_confidence = $("#wiz-confidence").value;
  } else if (step === 6) {
    draft.ranking.mode = $("#wiz-ranking-mode").value;
    const combine = $("#wiz-combine");
    draft.ranking.combine_confirmed_with_aggregates = combine ? combine.checked : false;
    const weights = {};
    for (const [method, id] of [["reply", "wiz-weight-reply"], ["like", "wiz-weight-like"], ["repost", "wiz-weight-repost"], ["native_poll", "wiz-weight-poll"]]) {
      const field = $(`#${id}`);
      if (field) weights[method] = Number(field.value || 1);
    }
    draft.ranking.weights = draft.ranking.mode === "weighted" ? weights : {};
    draft.ranking.custom_formula = $("#wiz-formula").value.trim();
    draft.collection.frequency_seconds = Number($("#wiz-frequency").value || 0);
    draft.collection.permitted_duration_seconds = Number($("#wiz-duration").value || 0);
    draft.collection.snapshot_enabled = $("#wiz-snapshot").checked;
  }
}

function applyWizardTemplate() {
  const selected = $("#wiz-template");
  if (!selected || !selected.value) {
    toast("Choose a template first.", true);
    return;
  }
  const template = state.templates.find((item) => item.id === selected.value);
  if (!template) {
    toast("Template not found.", true);
    return;
  }
  readWizardStep();
  const draft = state.wizard.draft;
  const defaults = defaultDraft();
  draft.methods = [...(template.methods || [])];
  draft.sources = [{
    source_type: template.source_type || "x_post",
    value: "",
    label: "",
    project_name: "",
  }];
  draft.eligibility = { ...defaults.eligibility, ...(template.eligibility || {}) };
  draft.evidence = { ...defaults.evidence, ...(template.evidence || {}) };
  draft.ranking = { ...defaults.ranking, ...(template.ranking || {}) };
  draft.collection = { ...defaults.collection, ...(template.collection || {}) };
  state.wizard.notes = [
    `Template "${template.name}" applied as a draft.`,
    ...(template.warnings || []),
  ];
  renderWizard();
  toast(`Template "${template.name}" applied. Review all fields.`);
}

async function wizardInterpret() {
  readWizardStep();
  const instruction = $("#wiz-interpret").value.trim();
  if (!instruction) {
    toast("Enter a plain-language instruction first.", true);
    return;
  }
  const response = await guard(() => api(`/api/sessions/${state.sessionId}/interpret`, {
    method: "POST",
    body: { instruction, competition_name: state.wizard.draft.name },
  }));
  if (!response) return;
  const interpretation = response.interpretation.competition;
  const draft = state.wizard.draft;
  draft.name = interpretation.name || draft.name;
  draft.description = interpretation.description || draft.description;
  draft.timezone_name = interpretation.timezone_name || draft.timezone_name;
  draft.starts_at = interpretation.starts_at ? interpretation.starts_at.slice(0, 16) : draft.starts_at;
  draft.ends_at = interpretation.ends_at ? interpretation.ends_at.slice(0, 16) : draft.ends_at;
  draft.projects = (interpretation.projects || []).map((project) => ({
    name: project.name,
    aliases: (project.aliases || []).join(", "),
    external_id: project.external_id || "",
  }));
  if (!draft.projects.length) draft.projects = [{ name: "", aliases: "", external_id: "" }];
  draft.sources = (interpretation.sources || []).map((source) => ({
    source_type: source.source_type || "x_post",
    value: source.post_id || source.url || "",
    label: source.label || "",
    project_name: "",
  }));
  if (!draft.sources.length) draft.sources = [{ source_type: "x_post", value: "", label: "", project_name: "" }];
  draft.methods = interpretation.methods || draft.methods;
  draft.ranking.mode = interpretation.ranking?.mode || "confirmed";
  draft.ranking.combine_confirmed_with_aggregates = Boolean(interpretation.ranking?.combine_confirmed_with_aggregates);
  state.wizard.notes = [...(response.assumptions || []), ...(response.ambiguities || [])];
  renderWizard();
  toast("Draft configuration applied. Review every assumption before confirming.");
}

function wizardNext() {
  readWizardStep();
  const errors = wizardStepValid(state.wizard.step);
  if (errors.length) {
    renderWizard(errors.join(" "));
    return;
  }
  state.wizard.step = Math.min(WIZARD_STEPS.length - 1, state.wizard.step + 1);
  renderWizard();
}

function wizardCreate() {
  readWizardStep();
  const errors = wizardStepValid(6);
  if (!$("#wiz-confirm")?.checked) errors.push("Confirm the configuration review before creating the competition.");
  if (errors.length) {
    renderWizard(errors.join(" "));
    return;
  }
  const config = buildWizardConfig(state.wizard.draft);
  const cleaned = { ...config };
  guard(async () => {
    const response = await api(`/api/sessions/${state.sessionId}/competitions`, {
      method: "POST",
      body: { config: cleaned, confirmed: true },
    });
    state.competitionId = response.competition.competition_id;
    state.wizard = null;
    await refreshSession();
    showSection("overview");
    return response;
  }, "Competition created. Review the configuration, then collect.");
}

// ------------------------------------------------------------------ sources

async function loadSources() {
  if (!state.competitionId) return;
  const container = $("#sources-content");
  container.innerHTML = `<p class="loading">Loading sources…</p>`;
  const [coverageResponse, leaderboardResponse] = await Promise.all([
    guard(() => api(`/api/sessions/${state.sessionId}/coverage`)),
    guard(() => api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/leaderboard?view=confirmed`)),
  ]);
  if (!coverageResponse) return;
  state.coverage = coverageResponse.coverage;
  if (leaderboardResponse) {
    state.leaderboard = leaderboardResponse.leaderboard;
    state.projects = leaderboardResponse.leaderboard.entries.map((entry) => ({ id: entry.project_id, name: entry.name }));
    fillProjectSelects();
  }
  const sources = state.leaderboard?.sources || [];
  container.innerHTML = `
    <div class="badges">
      ${badge(state.leaderboard?.coverage_status)}
      <span class="badge info">${esc(state.leaderboard?.sources?.length || 0)} source(s)</span>
      ${state.overview?.fixture_mode ? `<span class="badge accent">test data</span>` : ""}
    </div>
    ${sources.length ? sources.map((source) => renderSourceCard(source)).join("") : `<div class="empty">No sources configured.</div>`}
    ${renderManualImportCard()}
    ${state.leaderboard?.limitations?.length ? `<div class="card"><h2>Coverage warnings</h2><ul class="warning-list">${state.leaderboard.limitations.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>` : ""}
  `;
}

function renderSourceCard(source) {
  const coverage = state.coverage.find((item) => item.source_id === source.source_id) || {};
  const attempts = coverage.adapter_attempts || [];
  return `
    <details class="source-card">
      <summary>
        ${badge(source.collection_status)}
        <strong>${esc(source.label || source.source_id)}</strong>
        <span class="badge info">${esc(humanStatus(source.source_type))}</span>
        ${source.manually_supplied ? `<span class="badge warn">manual</span>` : ""}
        <span class="badge ${source.individual_voters_identifiable ? "ok" : "info"}">${source.individual_voters_identifiable ? "individual voters observable" : "aggregate only"}</span>
      </summary>
      <div class="source-body">
        <p class="meta">Methods: ${esc((source.methods || []).map(methodLabel).join(", "))} · Post: ${esc(source.post_id || "—")} · Last checked: ${fmtDate(source.last_checked)}</p>
        <p class="meta">Exact counts: ${source.exact_counts_available ? "available" : "not available"} · Partial: ${source.partial ? "yes" : "no"} · Manually supplied: ${source.manually_supplied ? "yes" : "no"}</p>
        ${coverage.post_author_username ? `<p class="meta">Post author: @${esc(coverage.post_author_username)} · observed ${fmtDate(coverage.post_metrics_collected_at)}</p>` : ""}
        ${Object.keys(coverage.post_metrics || {}).length ? `<p class="meta">Post engagement (context only, never votes): ${Object.entries(coverage.post_metrics).map(([key, value]) => `${esc(key)} ${fmtNumber(value)}`).join(" · ")}</p>` : ""}
        ${source.unavailable_methods?.length ? `<p class="meta">Unavailable methods: ${esc(source.unavailable_methods.map(methodLabel).join(", "))}</p>` : ""}
        ${coverage.limitations?.length ? `<ul class="warning-list">${coverage.limitations.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>` : ""}
        ${attempts.length ? `
          <details><summary>Adapter attempts (${attempts.length})</summary>
            <div class="scroll"><table class="data-table">
              <thead><tr><th>Adapter</th><th>Method</th><th>Status</th><th>Code</th><th>Detail</th><th class="num">Rows</th></tr></thead>
              <tbody>${attempts.map((attempt) => `<tr>
                <td>${esc(attempt.adapter)}</td><td>${esc(methodLabel(attempt.method))}</td>
                <td>${badge(attempt.status, attempt.status === "success" ? "ok" : attempt.status === "failed" ? "bad" : "info")}</td>
                <td>${esc(attempt.error_code || "—")}</td><td>${esc(attempt.message || "—")}</td>
                <td class="num">${fmtNumber(attempt.result_count)}</td></tr>`).join("")}</tbody>
            </table></div>
          </details>` : ""}
      </div>
    </details>`;
}

function renderManualImportCard() {
  return `
    <div class="card">
      <h2>Manual evidence import</h2>
      <p class="meta">CSV or JSON records with provenance. Manually supplied aggregates require review before they are confirmed. Label the source honestly.</p>
      <label class="field"><span>Source label</span><input id="import-label" type="text" value="Operator-supplied aggregate"></label>
      <label class="field"><span>Payload (CSV or JSON)</span><textarea id="import-payload" placeholder='[{"project":"Gamma","votes":15,"method":"manual_total","source_url":"https://example.com/post/1","confidence":"moderate"}]'></textarea></label>
      <div class="actions">
        <button type="button" class="button" data-action="manual-import" data-format="auto">Import records</button>
        <button type="button" class="button" data-action="ocr-import">Import as OCR (low confidence)</button>
      </div>
    </div>`;
}

async function manualImport(asOcr = false) {
  if (!state.competitionId) return;
  const payload = $("#import-payload").value.trim();
  const label = $("#import-label").value.trim() || "Operator-supplied aggregate";
  if (!payload) {
    toast("Paste CSV or JSON records first.", true);
    return;
  }
  await guard(async () => {
    const path = asOcr ? "ocr-text" : "import";
    const body = asOcr
      ? { text: payload, confidence: "low", source_label: label }
      : { payload, format: "auto", source_label: label, source_type: "manual" };
    const result = await api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/${path}`, { method: "POST", body });
    await loadSources();
    return result;
  }, asOcr ? "OCR evidence imported for review." : "Manual records imported for review.");
}

// ------------------------------------------------------------------- events

async function loadEventsPanel() {
  if (state.drillProject) {
    fillProjectSelects();
    $("#events-project").value = state.drillProject;
    state.drillProject = "";
  }
  await loadEvents(1);
  await loadPolls();
}

function eventQuery() {
  const params = new URLSearchParams();
  params.set("page", String(state.eventsPage));
  params.set("page_size", String(state.eventsPageSize));
  const search = $("#events-search").value.trim();
  if (search) params.set("search", search);
  const status = $("#events-status").value;
  if (status) params.set("status", status);
  const method = $("#events-method").value;
  if (method) params.set("method", method);
  const project = $("#events-project").value;
  if (project) params.set("project", project);
  if ($("#events-superseded").checked) params.set("include_superseded", "true");
  return params.toString();
}

async function loadEvents(page = state.eventsPage) {
  if (!state.competitionId) return;
  state.eventsPage = page;
  const response = await guard(() => api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/events?${eventQuery()}`));
  if (!response) return;
  state.events = response.events;
  state.eventsTotal = response.total;
  $("#events-meta").textContent = `${response.total} event(s) · ruleset ${response.ruleset_version} · ${response.stale_ruleset_events} stale event(s) excluded`;
  $("#events-page").textContent = `Page ${response.page} of ${Math.max(1, Math.ceil(response.total / response.page_size))}`;
  $("#events-prev").disabled = response.page <= 1;
  $("#events-next").disabled = response.page * response.page_size >= response.total;
  renderEventsTable(response.events);
}

function renderEventsTable(events) {
  const table = $("#events-table");
  if (!events.length) {
    table.innerHTML = `<tbody><tr><td class="empty">No events match these filters.</td></tr></tbody>`;
    return;
  }
  table.innerHTML = `
    <thead><tr>
      <th>Observed</th><th>Method</th><th>Project</th><th>Status</th><th>Account / evidence</th>
      <th>Confidence</th><th>Decision reason</th><th class="num">Ruleset</th>
    </tr></thead>
    <tbody>${events.map((event) => `<tr>
      <td>${fmtDate(event.event_timestamp || event.collected_at)}</td>
      <td>${esc(methodLabel(event.voting_method))}</td>
      <td class="project">${esc(event.project_name || "—")}</td>
      <td>${badge(event.eligibility_status)} ${badge(event.verification_status)}</td>
      <td class="excerpt">${event.normalized_value?.text_excerpt ? esc(event.normalized_value.text_excerpt) : esc(event.account_id || event.normalized_value?.author_username || "—")}
        ${event.source_url ? `<br><a href="${esc(event.source_url)}" target="_blank" rel="noopener noreferrer">source</a>` : ""}
        ${event.normalized_value?.timestamp_is_collection_time ? `<br><span class="muted">timestamp = collection time</span>` : ""}
      </td>
      <td>${badge(event.confidence_label, "info")}</td>
      <td class="excerpt">${esc(event.decision_reason || "—")}
        ${event.limitations?.length ? `<details><summary>Limitations</summary><ul class="warning-list">${event.limitations.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></details>` : ""}
      </td>
      <td class="num">${esc(event.ruleset_version)}</td>
    </tr>`).join("")}</tbody>`;
}

async function loadPolls() {
  if (!state.competitionId) return;
  const response = await guard(() => api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/polls`));
  if (!response) return;
  state.polls = response.polls;
  const block = $("#polls-block");
  if (!state.polls.length) {
    block.innerHTML = "";
    return;
  }
  block.innerHTML = `
    <div class="card">
      <h2>Poll and aggregate observations</h2>
      <p class="meta">These are source-reported aggregate counts, never individually verified voters. Poll options are stored separately from individual events.</p>
      <div class="scroll"><table class="data-table">
        <thead><tr><th>Project</th><th>Option</th><th class="num">Votes</th><th>Status</th><th>Source</th><th>Observed</th><th>Actions</th></tr></thead>
        <tbody>${state.polls.map((poll) => `<tr>
          <td>${esc(poll.project_id ? (state.projects.find((p) => p.id === poll.project_id)?.name || poll.project_id) : "unmapped")}</td>
          <td>${esc(poll.option_label)}</td>
          <td class="num">${fmtNumber(poll.votes)}</td>
          <td>${badge(poll.verification_status)} ${poll.manually_supplied ? `<span class="badge warn">manual</span>` : `<span class="badge info">anonymous</span>`}</td>
          <td>${esc(poll.source_tool || "—")}${poll.source_url ? `<br><a href="${esc(poll.source_url)}" target="_blank" rel="noopener noreferrer">source</a>` : ""}</td>
          <td>${fmtDate(poll.collected_at)}</td>
          <td>
            <div class="actions">
              <button type="button" class="button button-small" data-action="poll-review" data-id="${esc(poll.aggregate_id)}" data-decision="approve">Approve</button>
              <button type="button" class="button button-small" data-action="poll-review" data-id="${esc(poll.aggregate_id)}" data-decision="reject">Reject</button>
              <button type="button" class="button button-small" data-action="poll-review" data-id="${esc(poll.aggregate_id)}" data-decision="inconclusive">Inconclusive</button>
            </div>
          </td>
        </tr>`).join("")}</tbody>
      </table></div>
      <ul class="warning-list">${response.limitations.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>
    </div>`;
}

// -------------------------------------------------------------- leaderboard

async function loadLeaderboard(view = state.leaderboardView) {
  if (!state.competitionId) return;
  state.leaderboardView = view;
  renderViewTabs();
  const response = await guard(() => api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/leaderboard?view=${encodeURIComponent(view)}`));
  if (!response) return;
  const lb = response.leaderboard;
  state.leaderboard = lb;
  state.projects = lb.entries.map((entry) => ({ id: entry.project_id, name: entry.name, external_id: entry.external_id }));
  fillProjectSelects();
  renderLeaderboardMeta(lb);
  renderLeaderboardFilters(lb);
  renderLeaderboardTable(lb);
  await Promise.all([loadSnapshots(), loadTimeline()]);
}

function renderViewTabs() {
  const views = [["confirmed", "Confirmed"], ["provisional", "Provisional"], ["raw", "Raw activity"],
    ["method", "Method"], ["account", "Accounts"], ["weighted", "Weighted"]];
  $("#lb-views").innerHTML = views.map(([value, label]) =>
    `<button type="button" data-action="lb-view" data-view="${value}" class="${value === state.leaderboardView ? "is-active" : ""}">${label}</button>`).join("");
}

function renderLeaderboardMeta(lb) {
  $("#leaderboard-meta").innerHTML = `
    <div class="metric-banner">
      <p class="metric-title">${esc(lb.metric_description)}</p>
      <p class="metric-detail">Ranking metric: <strong>${esc(lb.metric)}</strong> · Score column: <strong>${esc(lb.score_label)}</strong></p>
      <p class="metric-detail">${esc(lb.formula)}</p>
      <div class="badges">
        ${badge(lb.coverage_status)}
        <span class="badge info">${esc(lb.finalization_status)}</span>
        <span class="badge">${esc(lb.mode)}</span>
        ${lb.aggregates_ranked_separately ? `<span class="badge warn">aggregates separate</span>` : `<span class="badge warn">combined formula approved</span>`}
        ${lb.is_test_data ? `<span class="badge accent">test data</span>` : ""}
        ${lb.denominator ? `<span class="badge">denominator ${fmtNumber(lb.denominator)}</span>` : ""}
      </div>
      <p class="meta">${esc(lb.denominator_kind || "No vote share denominator applies to this view.")} · Generated ${fmtDate(lb.generated_at)} · Ruleset ${esc(lb.ruleset_version)}</p>
    </div>
    <div class="totalsplit">
      <div class="split"><strong>${fmtNumber(lb.confirmed_individual_total)}</strong><span>confirmed individual</span></div>
      <div class="split"><strong>${fmtNumber(lb.confirmed_aggregate_total)}</strong><span>source-reported aggregates</span></div>
      <div class="split"><strong>${fmtNumber(lb.total_unresolved)}</strong><span>unresolved (pending + ambiguous)</span></div>
      <div class="split"><strong>${fmtNumber(lb.total_confirmed)}</strong><span>confirmed total across projects</span></div>
    </div>
    <div class="card"><h2>Result status</h2>
      <p class="meta">Collecting accepts more evidence. Provisional is a review-stage label. Finalized blocks further collection until reopened.</p>
      <div class="inline-controls">
        <label class="field"><span>Status</span>
          <select id="lb-status">${["collecting", "provisional", "finalized"].map((status) => `<option value="${status}" ${lb.finalization_status === status ? "selected" : ""}>${humanStatus(status)}</option>`).join("")}</select>
        </label>
        <button type="button" class="button" data-action="set-status">Update status</button>
      </div>
    </div>
    ${lb.limitations?.length ? `<div class="card"><h2>Limitations</h2><ul class="warning-list">${lb.limitations.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>` : ""}
    <div class="card"><h2>Sources in this leaderboard</h2>
      <div class="scroll"><table class="data-table">
        <thead><tr><th>Source</th><th>Type</th><th>Methods</th><th>Status</th><th>Observation model</th><th>Last checked</th></tr></thead>
        <tbody>${(lb.sources || []).map((source) => `<tr>
          <td>${esc(source.label || source.source_id)}</td>
          <td>${esc(humanStatus(source.source_type))}</td>
          <td>${esc((source.methods || []).map(methodLabel).join(", "))}</td>
          <td>${badge(source.collection_status)}</td>
          <td>${source.individual_voters_identifiable ? "individual events observable" : "anonymous aggregate only"}${source.manually_supplied ? " · manual" : ""}</td>
          <td>${fmtDate(source.last_checked)}</td>
        </tr>`).join("")}</tbody>
      </table></div>
    </div>`;
}

function methodBreakdown(entry) {
  const parts = [];
  for (const [method, count] of Object.entries(entry.breakdown || {})) {
    if (count) parts.push(`${methodLabel(method)} ${fmtNumber(count)}`);
  }
  for (const [method, count] of Object.entries(entry.aggregate_breakdown || {})) {
    if (count) parts.push(`${methodLabel(method)} ${fmtNumber(count)} <span class="badge info">agg</span>`);
  }
  return parts.join("<br>") || "—";
}

function renderLeaderboardFilters(lb) {
  const methods = new Set();
  for (const entry of lb.entries) {
    for (const [method, count] of Object.entries(entry.breakdown || {})) {
      if (count) methods.add(method);
    }
    for (const [method, count] of Object.entries(entry.aggregate_breakdown || {})) {
      if (count) methods.add(method);
    }
  }
  const select = $("#lb-method-filter");
  select.innerHTML = `<option value="">All methods</option>` + [...methods].sort().map((method) =>
    `<option value="${esc(method)}">${esc(methodLabel(method))}</option>`).join("");
  if (!methods.has(state.lbMethod)) state.lbMethod = "";
  select.value = state.lbMethod;
  $("#lb-search").value = state.lbSearch;
  $("#lb-sort").value = state.lbSort;
}

function filteredLeaderboardEntries(lb) {
  const query = state.lbSearch.trim().toLocaleLowerCase();
  const entries = lb.entries.filter((entry) => {
    const matchesSearch = !query ||
      String(entry.name || "").toLocaleLowerCase().includes(query) ||
      String(entry.external_id || "").toLocaleLowerCase().includes(query);
    const matchesMethod = !state.lbMethod ||
      Boolean(Number((entry.breakdown || {})[state.lbMethod] || 0)) ||
      Boolean(Number((entry.aggregate_breakdown || {})[state.lbMethod] || 0));
    return matchesSearch && matchesMethod;
  });
  if (state.lbSort === "name") {
    entries.sort((left, right) => String(left.name).localeCompare(String(right.name)));
  }
  return entries;
}

function renderLeaderboardTable(lb) {
  const table = $("#leaderboard-table");
  if (!lb.entries.length) {
    table.innerHTML = `<tbody><tr><td class="empty">No projects configured.</td></tr></tbody>`;
    return;
  }
  const entries = filteredLeaderboardEntries(lb);
  if (!entries.length) {
    table.innerHTML = `<tbody><tr><td class="empty">No projects match these filters.</td></tr></tbody>`;
    return;
  }
  const rankCounts = new Map();
  for (const entry of lb.entries) {
    rankCounts.set(entry.rank, (rankCounts.get(entry.rank) || 0) + 1);
  }
  const ties = new Set([...rankCounts].filter(([, count]) => count > 1).map(([rank]) => rank));
  table.innerHTML = `
    <caption class="meta">Showing ${entries.length} of ${lb.entries.length} project(s). Score = ${esc(lb.score_label)}. Rankings are recomputed on every evidence, eligibility, duplicate or rule change.</caption>
    <thead><tr>
      <th>Rank</th><th>Project</th><th class="num">Score</th><th class="num">Confirmed individual</th>
      <th class="num">Aggregate</th><th class="num">Provisional</th><th class="num">Unresolved</th>
      <th class="num">Rejected / late</th><th class="num">Vote share</th><th>By method</th><th>Confidence</th><th class="num">Accounts</th><th>Coverage</th>
    </tr></thead>
    <tbody>${entries.map((entry) => `<tr>
      <td class="rank">${entry.rank}${ties.has(entry.rank) ? ` <span class="badge warn">tie</span>` : ""}</td>
      <td class="project"><button type="button" class="project-link" data-action="drill-project" data-id="${esc(entry.project_id)}">${esc(entry.name)}</button>${entry.external_id ? `<br><span class="muted">${esc(entry.external_id)}</span>` : ""}</td>
      <td class="num"><strong>${fmtNumber(entry.score)}</strong></td>
      <td class="num">${fmtNumber(entry.confirmed_individual)}</td>
      <td class="num">${fmtNumber(entry.confirmed_aggregate)}</td>
      <td class="num">${fmtNumber(entry.provisional_votes)}</td>
      <td class="num">${fmtNumber(entry.unresolved_count)}</td>
      <td class="num">${fmtNumber(entry.rejected_or_late_activity)}</td>
      <td class="num">${entry.vote_share === null || entry.vote_share === undefined ? "—" : `${entry.vote_share.toFixed(1)}%`}</td>
      <td>${methodBreakdown(entry)}</td>
      <td>${Object.entries(entry.confidence_breakdown || {}).filter(([, count]) => count).map(([label, count]) => `${badge(label, "info")} ${fmtNumber(count)}`).join("<br>") || "—"}</td>
      <td class="num">${fmtNumber(entry.account_count)}</td>
      <td>${badge(entry.coverage_status)}${entry.limitations?.length ? `<details><summary>Notes</summary><ul class="warning-list">${entry.limitations.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></details>` : ""}</td>
    </tr>`).join("")}</tbody>
    <tfoot><tr>
      <td colspan="2">Totals</td>
      <td class="num">${fmtNumber(lb.entries.reduce((sum, entry) => sum + Number(entry.score || 0), 0))}</td>
      <td class="num">${fmtNumber(lb.confirmed_individual_total)}</td>
      <td class="num">${fmtNumber(lb.confirmed_aggregate_total)}</td>
      <td class="num">${fmtNumber(lb.entries.reduce((sum, entry) => sum + Number(entry.provisional_votes || 0), 0))}</td>
      <td class="num">${fmtNumber(lb.total_unresolved)}</td>
      <td class="num">${fmtNumber(lb.entries.reduce((sum, entry) => sum + Number(entry.rejected_or_late_activity || 0), 0))}</td>
      <td colspan="5"></td>
    </tr></tfoot>`;
}

async function loadSnapshots() {
  if (!state.competitionId) return;
  const response = await guard(() => api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/snapshots`));
  if (!response) return;
  state.snapshots = response.snapshots;
  const container = $("#snapshots-content");
  if (!state.snapshots.length) {
    container.innerHTML = "";
    return;
  }
  container.innerHTML = `<div class="card"><h2>Ranking history</h2>
    <div class="scroll"><table class="data-table">
      <thead><tr><th>Captured</th><th>View</th><th>Ruleset</th><th>Coverage</th><th>Counts</th></tr></thead>
      <tbody>${state.snapshots.slice().reverse().map((snapshot) => `<tr>
        <td>${fmtDate(snapshot.created_at)}</td>
        <td>${esc(snapshot.view)}</td>
        <td>${esc(snapshot.ruleset_version)}</td>
        <td>${badge(snapshot.coverage_status)}</td>
        <td>${Object.entries(snapshot.counts).map(([projectId, count]) => {
          const project = state.projects.find((item) => item.id === projectId);
          return `${esc(project?.name || projectId)}: ${fmtNumber(count)}`;
        }).join(" · ")}</td>
      </tr>`).join("")}</tbody>
    </table></div></div>`;
}

async function loadTimeline() {
  if (!state.competitionId) return;
  const response = await guard(() => api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/timeline`));
  if (!response) return;
  const container = $("#snapshots-content");
  container.insertAdjacentHTML("beforeend", `
    <details class="card"><summary><strong>Observation timeline</strong> (${response.events.length} entries)</summary>
      ${response.events.length ? `<div class="scroll"><table class="data-table">
        <thead><tr><th>Time</th><th>Type</th><th>Summary</th><th>Source</th></tr></thead>
        <tbody>${response.events.slice(-50).reverse().map((item) => `<tr>
          <td>${fmtDate(item.timestamp)}</td><td>${esc(humanStatus(item.type))}</td>
          <td>${esc(item.summary)}</td><td>${esc(item.source_id || "—")}</td>
        </tr>`).join("")}</tbody>
      </table></div>` : `<p class="empty">No observations yet.</p>`}
      <ul class="warning-list">${(response.limitations || []).map((item) => `<li>${esc(item)}</li>`).join("")}</ul>
    </details>`);
}

// ------------------------------------------------------------------- agents

async function loadAgents() {
  const response = await guard(() => api(`/api/sessions/${state.sessionId}/agents`));
  if (!response) return;
  state.agents = response.tasks;
  const settings = state.settings || await guard(() => api("/api/settings"));
  if (settings) state.settings = settings;
  const container = $("#agents-content");
  container.innerHTML = `
    <div class="metric-banner">
      <p class="metric-title">Bounded, auditable agents</p>
      <p class="metric-detail">Deterministic checks run without AI. Model suggestions never change counts or rules. Tasks are limited by depth, concurrency, retries and timeouts; unchanged inputs are skipped.</p>
      <p class="meta">AI mode: ${esc(settings?.acquisition?.ai_mode || "none")} · configured: ${settings?.acquisition?.ai_configured ? "yes" : "no"} · new work: ${state.agentsPaused ? "paused" : "running"}</p>
    </div>
    ${state.agents.length ? `<div class="card"><h2>Tasks</h2>${state.agents.map((task) => `
      <div class="agent-row">
        <div><strong>${esc(task.agent_role)}</strong><br><span class="muted">${esc(task.task_id)}${task.parent_task_id ? ` · parent ${esc(task.parent_task_id)}` : ""}</span></div>
        <div>${badge(task.status)}<br><span class="muted">${esc(task.started_at || "not started")} · ${fmtNumber(task.duration_ms)} ms · ${fmtNumber(task.attempts)} attempt(s)</span></div>
        <div class="muted">${esc(task.confidence)}</div>
        <div>${esc(task.requested_operation)}${task.error_code ? `<br><span class="badge bad">${esc(task.error_code)}</span>` : ""}
          ${task.resource_usage ? `<br><span class="muted">${esc(JSON.stringify(task.resource_usage))}</span>` : ""}
          ${task.limitations?.length ? `<details><summary>Limitations</summary><ul class="warning-list">${task.limitations.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></details>` : ""}
          ${task.result && Object.keys(task.result).length ? `<details><summary>Structured output</summary><pre class="review-json">${esc(JSON.stringify(task.result, null, 2))}</pre></details>` : ""}
        </div>
      </div>`).join("")}</div>` : `<div class="empty">No agent tasks yet. Run agents after collecting data.</div>`}`;
}

// ------------------------------------------------------------------- review

async function loadReview() {
  if (!state.competitionId) return;
  const container = $("#review-content");
  container.innerHTML = `<p class="loading">Loading unresolved evidence…</p>`;
  const response = await guard(() => api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/events?page_size=200`));
  if (!response) return;
  const unresolved = response.events.filter((event) =>
    ["pending_review", "disputed", "inconclusive"].includes(event.verification_status) ||
    ["pending_eligibility_review", "eligibility_unknown"].includes(event.eligibility_status)
  );
  if (!unresolved.length) {
    container.innerHTML = `<div class="empty">No unresolved events. Confirmed and rejected events remain in Vote Events.</div>`;
    return;
  }
  container.innerHTML = `
    <div class="card">
      <div class="section-head">
        <p class="meta">${unresolved.length} unresolved event(s). Bulk actions are audit-logged and require confirmation.</p>
        <div class="actions">
          <button type="button" class="button button-small" data-action="review-select-all">Select all</button>
          <button type="button" class="button button-small" data-action="bulk-approve">Approve selected</button>
        </div>
      </div>
    </div>
    ${unresolved.map((event) => renderReviewItem(event)).join("")}`;
  fillReviewProjectSelects();
}

function renderReviewItem(event) {
  const suggestion = event.agent_suggestion?.classification
    ? `<span class="badge info">agent suggestion: ${esc(event.agent_suggestion.classification)} (${esc(event.agent_suggestion.confidence)}) — not a decision</span>`
    : "";
  return `
    <div class="review-item" data-event-id="${esc(event.event_id)}">
      <h3>
        <input type="checkbox" class="review-checkbox" value="${esc(event.event_id)}" aria-label="Select event">
        ${esc(event.project_name || "No project")} · ${esc(methodLabel(event.voting_method))}
        ${badge(event.eligibility_status)} ${badge(event.verification_status)} ${badge(event.confidence_label, "info")}
      </h3>
      <p class="meta">${fmtDate(event.event_timestamp)} · ${esc(event.source_tool || "unknown tool")} · ${esc(event.event_id)} · ruleset ${esc(event.ruleset_version)}</p>
      ${suggestion}
      <div class="review-evidence">${esc(event.normalized_value?.text_excerpt || "(no text; account activity only)")}</div>
      <p class="meta">Reason: ${esc(event.decision_reason || "—")}</p>
      ${event.limitations?.length ? `<ul class="warning-list">${event.limitations.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>` : ""}
      <div class="review-actions">
        <select class="review-project"><option value="">Keep project</option></select>
        <input class="review-note" type="text" placeholder="Decision note (audit trail)" maxlength="500">
        <button type="button" class="button button-small" data-action="review-event" data-decision="approve">Approve</button>
        <button type="button" class="button button-small" data-action="review-event" data-decision="reassign">Reassign</button>
        <button type="button" class="button button-small" data-action="review-event" data-decision="inconclusive">Inconclusive</button>
        <button type="button" class="button button-small button-danger" data-action="review-event" data-decision="reject">Reject</button>
      </div>
    </div>`;
}

function fillReviewProjectSelects() {
  $$(".review-project").forEach((select) => {
    select.innerHTML = `<option value="">Keep project</option>` +
      state.projects.map((project) => `<option value="${esc(project.id)}">${esc(project.name)}</option>`).join("");
  });
}

async function reviewEvent(button) {
  const item = button.closest(".review-item");
  const eventId = item.dataset.eventId;
  const decision = button.dataset.decision;
  const projectId = $(".review-project", item).value;
  const note = $(".review-note", item).value.trim();
  if (decision === "reassign" && !projectId) {
    toast("Choose a project before reassigning.", true);
    return;
  }
  await guard(async () => {
    await api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/events/${eventId}/review`, {
      method: "POST",
      body: { action: decision, project_id: projectId, note },
    });
    await loadReview();
    await refreshSession();
    return true;
  }, "Review decision recorded.");
}

async function bulkApprove() {
  const ids = $$(".review-checkbox:checked").map((box) => box.value);
  if (!ids.length) {
    toast("Select at least one event.", true);
    return;
  }
  const confirmed = await askConfirm({
    title: "Approve selected events",
    message: `Approve ${ids.length} event(s)? This is recorded in the audit trail.`,
    requireText: "APPROVE",
  });
  if (!confirmed) return;
  await guard(async () => {
    await api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/bulk-review`, {
      method: "POST",
      body: { event_ids: ids, action: "approve", confirm: true, note: "Bulk approval from evidence review" },
    });
    await loadReview();
    await refreshSession();
    return true;
  }, `${ids.length} event(s) approved.`);
}

// ------------------------------------------------------------------ exports

async function loadExports() {
  if (!state.competitionId) return;
  const kind = $("#export-kind").value;
  const response = await guard(() => api(`/api/sessions/${state.sessionId}/export-fields?kind=${encodeURIComponent(kind)}`));
  if (!response) return;
  const sensitive = new Set(response.sensitive_fields || []);
  $("#export-fields").innerHTML = `
    <span class="field-group">Optional field selection (defaults to all non-sensitive fields)</span>
    ${response.fields.map((field) => `
      <label><input type="checkbox" class="export-field" value="${esc(field)}" ${sensitive.has(field) ? "" : "checked"}>
        ${esc(field)}${sensitive.has(field) ? ` <span class="badge warn">sensitive</span>` : ""}</label>`).join("")}`;
  $("#export-result").innerHTML = (state.overview?.exports || []).slice().reverse().map(renderExportRecord).join("") ||
    `<p class="empty">No exports yet.</p>`;
}

function renderExportRecord(record) {
  const url = `/api/sessions/${state.sessionId}/exports/${encodeURIComponent(record.filename)}`;
  return `<div class="export-result-item">
    <strong>${esc(record.kind)}</strong> · ${esc(record.format)} · ${fmtNumber(record.size_bytes)} bytes<br>
    <span class="meta">${fmtDate(record.exported_at)}${record.sensitive_included?.length ? ` · sensitive: ${esc(record.sensitive_included.join(", "))}` : " · standard fields"}</span><br>
    <a href="${url}" download>Download ${esc(record.filename)}</a>
  </div>`;
}

async function createExport() {
  if (!state.competitionId) return;
  const fields = $$(".export-field:checked").map((box) => box.value);
  const runExport = async () => {
    const response = await api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/export`, {
      method: "POST",
      body: {
        kind: $("#export-kind").value,
        format: $("#export-format").value,
        fields: fields.length ? fields : null,
        confirm_sensitive: $("#export-sensitive").checked && $("#export-confirm").checked,
        view: state.leaderboardView === "confirmed" ? "confirmed" : "confirmed",
      },
    });
    await loadExports();
    return response;
  };
  if ($("#export-sensitive").checked && !$("#export-confirm").checked) {
    toast("Confirm sensitive export before continuing.", true);
    return;
  }
  await guard(runExport, "Export written locally. Review it before sharing.");
}

// ----------------------------------------------------------------- settings

async function loadSettings() {
  const [settings, templates] = await Promise.all([
    guard(() => api("/api/settings")),
    guard(() => api("/api/templates")),
  ]);
  if (!settings) return;
  state.settings = settings;
  state.templates = templates?.templates || [];
  const acquisition = settings.acquisition || {};
  $("#settings-content").innerHTML = `
    <div class="metric-banner">
      <p class="metric-title">Authorized sessions only</p>
      <p class="metric-detail">${esc(acquisition.compliance?.note || "Respect tool licenses, X's applicable terms and access controls, and self-hosted instance policies.")}</p>
      <p class="meta">Live-collection acknowledgement: <strong>${acquisition.authorized_use_acknowledged ? "provided" : "not provided"}</strong> · AI mode: ${esc(acquisition.ai_mode)}</p>
      ${!acquisition.authorized_use_acknowledged ? `<p class="meta">Set the configured environment variable to “yes” before live collection. Fixture and manual imports are unaffected.</p>` : ""}
    </div>
    <div class="card">
      <h2>Adapters</h2>
      <p class="meta">Capabilities are probed against the installed tool versions. Unknown commands are reported, never assumed.</p>
      <div class="scroll"><table class="data-table">
        <thead><tr><th>Adapter</th><th>Auth tier</th><th>Available</th><th>Version</th><th>Capabilities</th><th>Commands</th><th>Licence / compliance</th></tr></thead>
        <tbody>${(settings.adapters || []).map((adapter) => `<tr>
          <td><strong>${esc(adapter.name)}</strong><br><span class="muted">${esc(adapter.unavailable_reason || "")}</span></td>
          <td>${esc(adapter.auth_tier)}</td>
          <td>${adapter.available ? `<span class="badge ok">yes</span>` : `<span class="badge info">unavailable</span>`}</td>
          <td>${esc(adapter.version || "—")}</td>
          <td>${Object.entries(adapter.capabilities || {}).map(([key, value]) => `${esc(key.slice(0, 10))}: ${value ? "yes" : "no"}`).join("<br>")}</td>
          <td>${Object.entries(adapter.commands || {}).map(([key, value]) => `${esc(key)}: ${value === true ? "confirmed" : value === false ? "missing" : "unknown"}`).join("<br>") || "—"}</td>
          <td class="excerpt">${esc(adapter.license_note || "")}<br><span class="muted">${esc(adapter.compliance_note || "")}</span></td>
        </tr>`).join("")}</tbody>
      </table></div>
    </div>
    <div class="card">
      <h2>Acquisition configuration</h2>
      <p class="meta">Credentials come from environment variables only and are never displayed or serialized.</p>
      <div class="form-grid">
        <div class="field"><span>Preference order</span><div>${esc((acquisition.preference_order || []).join(" → "))}</div></div>
        <div class="field"><span>Concurrency</span><div>${fmtNumber(acquisition.max_concurrency)}</div></div>
        <div class="field"><span>Per-task timeout</span><div>${fmtNumber(acquisition.per_task_timeout_seconds)}s</div></div>
        <div class="field"><span>Retry limit</span><div>${fmtNumber(acquisition.retry_limit)}</div></div>
        <div class="field"><span>Agent concurrency / depth</span><div>${fmtNumber(acquisition.agents_max_concurrent)} / ${fmtNumber(acquisition.agents_max_depth)}</div></div>
        <div class="field"><span>Session root</span><div class="code">${esc(settings.session_root)}</div></div>
        <div class="field"><span>Export root</span><div class="code">${esc(settings.export_root)}</div></div>
      </div>
    </div>
    <div class="card">
      <h2>Sessions</h2>
      <p class="meta">Temporary sessions are deleted on normal exit. Exports live outside cleanup.</p>
      <div class="scroll"><table class="data-table">
        <thead><tr><th>Session</th><th>Created</th><th>Fixture</th><th></th></tr></thead>
        <tbody>${(settings.sessions || []).map((session) => `<tr>
          <td class="code">${esc(session.session_id)}</td>
          <td>${fmtDate(session.created_at)}</td>
          <td>${session.fixture_mode ? `<span class="badge accent">test data</span>` : "live/manual"}</td>
          <td><button type="button" class="button button-small button-danger" data-action="delete-session" data-id="${esc(session.session_id)}">Delete</button></td>
        </tr>`).join("")}</tbody>
      </table></div>
      <p class="fineprint">${esc(settings.credentials_note || "")}</p>
    </div>`;
}

// -------------------------------------------------------------- global bind

function bindGlobalEvents() {
  $("#btn-welcome-demo").addEventListener("click", runDemo);
  $("#btn-demo").addEventListener("click", runDemo);
  $("#btn-new-session").addEventListener("click", async () => {
    const storageMode = $("#welcome-storage")?.value || "auto";
    const response = await guard(() => api("/api/sessions", { method: "POST", body: { storage_mode: storageMode } }));
    if (!response) return;
    await enterSession(response.session.session_id);
    showSection("newcheck");
  });
  $$("#mainnav .navlink").forEach((link) => {
    link.addEventListener("click", () => showSection(link.dataset.section));
  });
  $("#btn-collect").addEventListener("click", collectNow);
  $("#btn-snapshot-top").addEventListener("click", captureSnapshot);
  $("#btn-capture-snapshot").addEventListener("click", captureSnapshot);
  $("#btn-compare-snapshots").addEventListener("click", compareSnapshots);
  $("#btn-goto-newcheck").addEventListener("click", () => showSection("newcheck"));
  $("#btn-refresh-sources").addEventListener("click", () => guard(loadSources));
  $("#btn-refresh-events").addEventListener("click", () => guard(loadEventsPanel));
  $("#btn-refresh-review").addEventListener("click", () => guard(loadReview));
  $("#btn-run-agents").addEventListener("click", runAgents);
  $("#btn-pause-agents").addEventListener("click", toggleAgentsPause);
  $("#btn-cancel-agents").addEventListener("click", cancelAgents);
  $("#btn-cleanup").addEventListener("click", cleanupAll);
  $("#export-button").addEventListener("click", createExport);
  $("#export-kind").addEventListener("change", () => guard(loadExports));
  $("#export-sensitive").addEventListener("change", () => {
    if ($("#export-sensitive").checked) {
      $("#export-confirm").checked = false;
      $("#export-confirm").closest("label").classList.remove("hidden");
    }
  });
  $("#events-search").addEventListener("input", debounce(() => guard(loadEvents(1)), 300));
  $("#lb-search").addEventListener("input", debounce(() => {
    state.lbSearch = $("#lb-search").value;
    if (state.leaderboard) renderLeaderboardTable(state.leaderboard);
  }, 200));
  $("#lb-method-filter").addEventListener("change", () => {
    state.lbMethod = $("#lb-method-filter").value;
    if (state.leaderboard) renderLeaderboardTable(state.leaderboard);
  });
  $("#lb-sort").addEventListener("change", () => {
    state.lbSort = $("#lb-sort").value;
    if (state.leaderboard) renderLeaderboardTable(state.leaderboard);
  });
  ["#events-status", "#events-method", "#events-project", "#events-superseded"].forEach((selector) => {
    $(selector).addEventListener("change", () => guard(loadEvents(1)));
  });
  $("#events-prev").addEventListener("click", () => guard(loadEvents(Math.max(1, state.eventsPage - 1))));
  $("#events-next").addEventListener("click", () => guard(loadEvents(state.eventsPage + 1)));

  document.addEventListener("click", (event) => {
    const target = event.target.closest("[data-action]");
    if (!target) return;
    const action = target.dataset.action;
    const handlers = {
      demo: runDemo,
      "goto-newcheck": () => showSection("newcheck"),
      "goto-leaderboard": () => showSection("leaderboard"),
      collect: collectNow,
      "capture-snapshot": captureSnapshot,
      "wizard-step": () => {
        readWizardStep();
        state.wizard.step = Number(target.dataset.step);
        renderWizard();
      },
      "wizard-next": wizardNext,
      "wizard-prev": () => {
        readWizardStep();
        state.wizard.step = Math.max(0, state.wizard.step - 1);
        renderWizard();
      },
      "wizard-cancel": () => {
        state.wizard = null;
        showSection("overview");
      },
      "wizard-interpret": wizardInterpret,
      "wizard-template": applyWizardTemplate,
      "wizard-create": wizardCreate,
      "wizard-add-project": () => {
        readWizardStep();
        state.wizard.draft.projects.push({ name: "", aliases: "", external_id: "", usernames: "", hashtags: "", keywords: "", domains: "" });
        renderWizard();
      },
      "wizard-remove-project": () => {
        readWizardStep();
        if (state.wizard.draft.projects.length > 1) {
          state.wizard.draft.projects.splice(Number(target.dataset.index), 1);
          renderWizard();
        }
      },
      "wizard-add-source": () => {
        readWizardStep();
        state.wizard.draft.sources.push({ source_type: "x_post", value: "", label: "", project_name: "" });
        renderWizard();
      },
      "wizard-remove-source": () => {
        readWizardStep();
        if (state.wizard.draft.sources.length > 1) {
          state.wizard.draft.sources.splice(Number(target.dataset.index), 1);
          renderWizard();
        }
      },
      "manual-import": () => manualImport(false),
      "ocr-import": () => manualImport(true),
      "poll-review": () => reviewPoll(target),
      "lb-view": () => guard(() => loadLeaderboard(target.dataset.view)),
      "drill-project": () => drillProject(target.dataset.id),
      "save-config": () => saveAdvancedConfig(target.dataset.id),
      "set-status": setCompetitionStatus,
      "review-event": () => reviewEvent(target),
      "review-select-all": () => {
        const boxes = $$(".review-checkbox");
        const allChecked = boxes.every((box) => box.checked);
        boxes.forEach((box) => { box.checked = !allChecked; });
      },
      "bulk-approve": bulkApprove,
      "delete-session": () => deleteSession(target.dataset.id),
    };
    const handler = handlers[action];
    if (handler) handler();
  });
}

function debounce(fn, wait) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

async function runDemo() {
  await guard(async () => {
    const response = await api("/api/demo", { method: "POST", body: {} });
    await enterSession(response.session_id);
    toast(response.note || "Synthetic demo loaded. All data is test data.");
    return response;
  });
}

async function collectNow() {
  if (!state.competitionId) {
    toast("Create a competition first.", true);
    return;
  }
  await guard(async () => {
    const report = await api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/collect`, {
      method: "POST",
      body: { run_agents: false },
    });
    await refreshSession();
    await loadOverview();
    toast(`Collection ${humanStatus(report.status)}; ${report.events_observed ?? 0} new observation(s).`);
    return report;
  });
}

function drillProject(projectId) {
  if (!projectId) return;
  state.drillProject = projectId;
  showSection("events");
}

async function saveAdvancedConfig(competitionId) {
  const editor = document.querySelector(`.advanced-config[data-id="${competitionId}"]`);
  if (!editor) return;
  let config;
  try {
    config = JSON.parse(editor.value);
  } catch (error) {
    toast(`Invalid JSON: ${error.message}`, true);
    return;
  }
  const confirmed = await askConfirm({
    title: "Apply advanced configuration",
    message:
      "This validates the configuration, creates a new ruleset version and re-evaluates existing evidence without re-collecting. Continue?",
    requireText: "UPDATE",
  });
  if (!confirmed) return;
  await guard(async () => {
    await api(`/api/sessions/${state.sessionId}/competitions/${competitionId}`, {
      method: "PUT",
      body: { config },
    });
    await loadCompetitions();
    await refreshSession();
    if (state.competitionId === competitionId) {
      await loadLeaderboard(state.leaderboardView);
    }
    return true;
  }, "Configuration updated; evidence re-evaluated.");
}

async function setCompetitionStatus() {
  const status = $("#lb-status").value;
  if (
    status === "finalized" &&
    !confirm("Finalize these results? Collection will be blocked until the status is changed back.")
  ) {
    return;
  }
  await guard(async () => {
    await api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/status`, {
      method: "POST",
      body: { status },
    });
    await refreshSession();
    await loadLeaderboard(state.leaderboardView);
    return true;
  }, `Result status set to ${humanStatus(status)}.`);
}

async function captureSnapshot() {
  if (!state.competitionId) return;
  await guard(async () => {
    await api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/snapshots`, {
      method: "POST",
      body: {},
    });
    await loadSnapshots();
    return true;
  }, "Snapshot captured.");
}

async function compareSnapshots() {
  if (state.snapshots.length < 2) {
    toast("At least two snapshots are needed to compare.", true);
    return;
  }
  const latest = state.snapshots[state.snapshots.length - 1];
  const result = await guard(() => api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/snapshots/${latest.snapshot_id}/compare`, {
    method: "POST",
    body: {},
  }));
  if (!result) return;
  const rankChanges = Object.entries(result.rank_changes || {});
  const countChanges = Object.entries(result.count_changes || {});
  const name = (projectId) => state.projects.find((project) => project.id === projectId)?.name || projectId;
  const container = $("#snapshots-content");
  container.insertAdjacentHTML("afterbegin", `
    <div class="card">
      <h2>Snapshot comparison</h2>
      <p class="meta">${fmtDate(result.from)} → ${fmtDate(result.to)} · ruleset changed: ${result.ruleset_changed ? "yes" : "no"} · coverage changed: ${result.coverage_changed ? "yes" : "no"}</p>
      <div class="totalsplit">
        <div class="split"><strong>${rankChanges.length}</strong><span>rank changes</span></div>
        <div class="split"><strong>${countChanges.length}</strong><span>count changes</span></div>
      </div>
      ${rankChanges.length ? `<ul class="warning-list">${rankChanges.map(([projectId, change]) => `<li>${esc(name(projectId))}: rank ${esc(change.from)} → ${esc(change.to)}</li>`).join("")}</ul>` : ""}
      ${countChanges.length ? `<ul class="warning-list">${countChanges.map(([projectId, change]) => `<li>${esc(name(projectId))}: ${fmtNumber(change.from)} → ${fmtNumber(change.to)} (${change.delta > 0 ? "+" : ""}${fmtNumber(change.delta)})</li>`).join("")}</ul>` : ""}
      ${!rankChanges.length && !countChanges.length ? `<p class="empty">No ranking or count changes between these snapshots.</p>` : ""}
    </div>`);
}

async function reviewPoll(button) {
  const id = button.dataset.id;
  const decision = button.dataset.decision;
  await guard(async () => {
    await api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/polls/${id}/review`, {
      method: "POST",
      body: { action: decision, note: "Reviewed from aggregate panel" },
    });
    await loadPolls();
    await refreshSession();
    return true;
  }, "Aggregate review recorded.");
}

async function runAgents() {
  if (!state.competitionId) return;
  await guard(async () => {
    const response = await api(`/api/sessions/${state.sessionId}/competitions/${state.competitionId}/agents/run`, {
      method: "POST",
      body: {},
    });
    await loadAgents();
    toast(`${response.tasks?.length || 0} agent task(s) completed.`);
    return response;
  });
}

async function toggleAgentsPause() {
  await guard(async () => {
    const desired = !state.agentsPaused;
    const response = await api(`/api/sessions/${state.sessionId}/agents/pause`, {
      method: "POST",
      body: { paused: desired },
    });
    state.agentsPaused = Boolean(response.paused);
    $("#btn-pause-agents").textContent = state.agentsPaused ? "Resume new work" : "Pause new work";
    toast(response.note || (state.agentsPaused ? "New agent work paused." : "New agent work resumed."));
    await loadAgents();
    return response;
  });
}

async function cancelAgents() {
  await guard(async () => {
    const response = await api(`/api/sessions/${state.sessionId}/agents/cancel`, { method: "POST", body: {} });
    state.agentsPaused = false;
    $("#btn-pause-agents").textContent = "Pause new work";
    toast(response.note || "Cancellation requested.");
    await loadAgents();
    return response;
  });
}

async function deleteSession(sessionId) {
  const confirmed = await askConfirm({
    title: "Delete session",
    message: "Delete this temporary session and all collected evidence? Exports remain on disk.",
    requireText: "DELETE",
  });
  if (!confirmed) return;
  await guard(async () => {
    await api(`/api/sessions/${sessionId}`, { method: "DELETE" });
    if (sessionId === state.sessionId) showWelcome();
    else await loadSettings();
    return true;
  }, "Session deleted.");
}

async function cleanupAll() {
  const confirmed = await askConfirm({
    title: "Clean up all sessions",
    message: "Delete every temporary session, evidence directory and session database? Exports remain on disk.",
    requireText: "CLEANUP",
  });
  if (!confirmed) return;
  await guard(async () => {
    const response = await api("/api/cleanup", { method: "POST", body: { confirm: true } });
    showWelcome();
    toast(`Cleanup requested for ${Object.keys(response.results || {}).length} session(s).`);
    return response;
  });
}

boot();
