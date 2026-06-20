"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
  videos: [],
  selectedId: null,
  settings: { has_api_key: false, selected_models: [], frame_count: 8, api_key_source: null },
  availableModels: [],
  sessionCost: 0,
  detail: null, // current video's full payload
  filter: "all", // triage filter: all | needs_review | confident | reviewed
  analyzing: new Set(), // ids currently being analyzed (for live badge state)
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
};

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = `${res.status}`;
    try { const j = await res.json(); msg = j.detail || JSON.stringify(j); } catch {}
    throw new Error(msg);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, 3200);
}

function fmtCost(c) {
  if (!c) return "$0.000000";
  return "$" + Number(c).toFixed(6);
}
function fmtDuration(s) {
  if (!s) return "0:00";
  const m = Math.floor(s / 60), sec = Math.round(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}
function fmtSize(b) {
  if (!b) return "";
  const mb = b / 1048576;
  return mb >= 1 ? `${mb.toFixed(0)} MB` : `${(b / 1024).toFixed(0)} KB`;
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------
async function loadSettings() {
  state.settings = await api("/api/settings");
  renderSettingsUI();
  renderActiveModelChips();
}

// Friendly display names for the Auto trio; falls back to the slug's last segment.
const FRIENDLY_MODELS = {
  "perceptron/perceptron-mk1": "Perceptron Mk1",
  "google/gemini-2.5-flash": "Gemini 2.5 Flash",
  "qwen/qwen3-vl-235b-a22b-instruct": "Qwen3-VL 235B",
  "qwen/qwen3.6-plus": "Qwen3.6 Plus",
  "z-ai/glm-4.6v": "GLM 4.6V",
  "minimax/minimax-m3": "MiniMax M3",
};
function friendlyModel(m) { return FRIENDLY_MODELS[m] || m.split("/").pop(); }

function effectiveModels() {
  const s = state.settings;
  return (s.mode === "custom" ? s.selected_models : s.auto_models) || [];
}

function renderSettingsUI() {
  const s = state.settings;
  $("#frameCount").value = s.frame_count;
  $("#frameCountOut").textContent = s.frame_count;
  const mode = s.mode || "auto";
  document.querySelectorAll('input[name="mode"]').forEach((r) => { r.checked = r.value === mode; });
  $("#customModelsView").hidden = mode !== "custom";
  $("#autoModelsView").hidden = mode === "custom";
  const policy = s.review_policy || "majority";
  document.querySelectorAll('input[name="reviewPolicy"]').forEach((r) => { r.checked = r.value === policy; });
  const imode = s.input_mode || "video";
  document.querySelectorAll('input[name="inputMode"]').forEach((r) => { r.checked = r.value === imode; });
  $("#frameCountField").hidden = imode !== "frames";
  renderAutoModelsView();
  renderSelectedModelTags();
  renderModelPicker();
}

function renderAutoModelsView() {
  const wrap = $("#autoModelsView");
  wrap.innerHTML = "";
  (state.settings.auto_models || []).forEach((m) => {
    const row = el("div", "auto-model-row");
    row.append(el("span", "auto-dot"));
    row.append(el("span", "am-name", friendlyModel(m)));
    row.append(el("span", "am-id", m));
    wrap.append(row);
  });
}

function renderSelectedModelTags() {
  const wrap = $("#selectedModelTags");
  wrap.innerHTML = "";
  (state.settings.selected_models || []).forEach((m) => {
    const tag = el("span", "tag");
    tag.append(document.createTextNode(m));
    const x = el("button", null, "✕");
    x.onclick = () => {
      state.settings.selected_models = state.settings.selected_models.filter((y) => y !== m);
      renderSelectedModelTags();
      renderModelPicker();
    };
    tag.append(x);
    wrap.append(tag);
  });
}

function renderActiveModelChips() {
  const wrap = $("#activeModelChips");
  wrap.innerHTML = "";
  const pill = $("#modePill");
  const mode = state.settings.mode || "auto";
  if (pill) { pill.textContent = mode === "custom" ? "Custom" : "Auto"; pill.className = "mode-pill " + mode; }
  const models = effectiveModels();
  if (!models.length) {
    wrap.append(el("span", "model-chip", "No models selected"));
    return;
  }
  models.forEach((m) => wrap.append(el("span", "model-chip", friendlyModel(m))));
}

function renderModelPicker() {
  const picker = $("#modelPicker");
  const filter = ($("#modelSearch").value || "").toLowerCase();
  if (!state.availableModels.length) {
    if (!picker.querySelector(".model-opt"))
      picker.innerHTML = '<p class="hint">Click "Load video models" to choose from the video→text models. You can also type a custom model name below.</p>';
    return;
  }
  picker.innerHTML = "";
  const selected = new Set(state.settings.selected_models || []);
  const items = state.availableModels.filter((m) =>
    !filter || (m.id + " " + (m.name || "")).toLowerCase().includes(filter)
  );
  items.slice(0, 200).forEach((m) => {
    const opt = el("label", "model-opt");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = selected.has(m.id);
    cb.onchange = () => {
      const set = new Set(state.settings.selected_models || []);
      if (cb.checked) set.add(m.id); else set.delete(m.id);
      state.settings.selected_models = [...set];
      renderSelectedModelTags();
    };
    const body = el("div");
    body.append(el("div", "mo-name", m.name || m.id));
    body.append(el("div", "mo-id", m.id));
    const price = el("div", "mo-price");
    const pIn = m.prompt_price ? `$${(Number(m.prompt_price) * 1e6).toFixed(2)}/M in` : "";
    const pOut = m.completion_price ? ` · $${(Number(m.completion_price) * 1e6).toFixed(2)}/M out` : "";
    price.textContent = pIn + pOut;
    opt.append(cb, body, price);
    picker.append(opt);
  });
  if (!items.length) picker.innerHTML = '<p class="hint">No models match your filter.</p>';
}

async function loadModels() {
  try {
    toast("Loading video models…");
    const { models } = await api("/api/models");
    state.availableModels = models;
    toast(`Loaded ${models.length} video→text models you can compare.`);
    renderModelPicker();
  } catch (e) {
    toast("Could not load models: " + e.message, true);
  }
}

async function saveSettings() {
  const mode = (document.querySelector('input[name="mode"]:checked') || {}).value || "auto";
  const reviewPolicy = (document.querySelector('input[name="reviewPolicy"]:checked') || {}).value || "majority";
  const inMode = (document.querySelector('input[name="inputMode"]:checked') || {}).value || "video";
  const body = {
    mode,
    review_policy: reviewPolicy,
    input_mode: inMode,
    selected_models: state.settings.selected_models,
    frame_count: Number($("#frameCount").value),
  };
  try {
    state.settings = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    renderSettingsUI();
    renderActiveModelChips();
    toast("Settings saved.");
    closeSettings();
    // Review policy / models may change the triage — refresh library + detail.
    await loadVideos();
    if (state.selectedId) {
      state.detail = await api(`/api/videos/${state.selectedId}`);
      renderDetail();
    }
  } catch (e) {
    toast("Save failed: " + e.message, true);
  }
}

function openSettings() { $("#settingsModal").hidden = false; }
function closeSettings() { $("#settingsModal").hidden = true; }

// ---------------------------------------------------------------------------
// Scoreboard (per-model accuracy vs. human reviews)
// ---------------------------------------------------------------------------
function openScoreboard() { $("#scoreboardModal").hidden = false; loadScoreboard(); }
function closeScoreboard() { $("#scoreboardModal").hidden = true; }

async function loadScoreboard() {
  const body = $("#scoreboardBody");
  const summary = $("#scoreboardSummary");
  body.innerHTML = '<p class="hint">Loading…</p>';
  let data;
  try {
    data = await api("/api/scoreboard");
  } catch (e) {
    body.innerHTML = `<p class="hint" style="color:var(--low)">Could not load: ${escapeHtml(e.message)}</p>`;
    return;
  }
  if (!data.reviewed_videos) {
    summary.textContent = "";
    body.innerHTML = '<div class="empty-state small"><span class="empty-icon">📊</span><p>No reviewed clips yet.<br>Accuracy is measured against your saved reviews — review a few clips, then check back.</p></div>';
    return;
  }
  summary.innerHTML = `Based on <b>${data.reviewed_videos}</b> human-reviewed clip(s). Ranked by species match rate.`;

  const pct = (x) => x == null ? "—" : Math.round(x * 100) + "%";
  const bar = (x) => {
    if (x == null) return "—";
    const w = Math.round(x * 100);
    const hue = x >= 0.8 ? "var(--high)" : x >= 0.5 ? "var(--medium)" : "var(--low)";
    return `<div class="sb-bar"><span style="width:${w}%;background:${hue}"></span></div><span class="sb-pct">${w}%</span>`;
  };

  const rows = data.models.map((m, i) => `
    <tr>
      <td class="sb-rank">${i + 1}</td>
      <td><div class="sb-model">${escapeHtml(friendlyModel(m.model))}</div><div class="sb-id">${escapeHtml(m.model)}</div></td>
      <td class="sb-n">${m.n_scored}${m.errors ? ` <span class="sb-err" title="${m.errors} failed/rate-limited run(s)">+${m.errors}⚠</span>` : ""}</td>
      <td class="sb-rate">${bar(m.species_match_rate)}</td>
      <td class="sb-rate">${bar(m.behavior_match_rate)}</td>
      <td class="sb-cost">${m.cost_per_correct != null ? fmtCost(m.cost_per_correct) : "—"}</td>
      <td class="sb-lat">${m.avg_latency_ms ? (m.avg_latency_ms / 1000).toFixed(1) + "s" : "—"}</td>
    </tr>`).join("");

  body.innerHTML = `
    <table class="scoreboard">
      <thead><tr>
        <th>#</th><th>Model</th><th>Reviews</th>
        <th>Species match</th><th>Behavior match</th><th>$ / correct</th><th>Avg latency</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ---------------------------------------------------------------------------
// Video library
// ---------------------------------------------------------------------------
async function loadVideos() {
  const { videos } = await api("/api/videos");
  state.videos = videos;
  renderVideoList();
}

function consensusBadge(c) {
  if (!c || !c.n_succeeded) return el("span", "badge none", "not analyzed");
  const labels = { high: "high conf", medium: "medium", low: "low conf", none: "no result" };
  const b = el("span", "badge " + c.level, labels[c.level] || c.level);
  return b;
}

// Triage states (must match backend workflow_status).
// Visual language: green ✓ = a HUMAN reviewed it (the only "approved" look);
// blue "AI" = the model's auto-accepted guess (not human-approved);
// amber ⚑ = needs a human; grey = not analyzed.
const STATUS_META = {
  not_analyzed: { label: "Not analyzed", cls: "none" },
  needs_review: { label: "Needs review", cls: "needs" },
  confident: { label: "AI confident", cls: "confident" },
  reviewed: { label: "Reviewed", cls: "reviewed" },
};

function statusBadge(v) {
  if (state.analyzing.has(v.id) || v.status === "processing")
    return el("span", "badge processing", "analyzing…");
  const st = v.workflow_status || "not_analyzed";
  let text;
  if (st === "reviewed" && v.final_species) text = `✓ Reviewed · ${v.final_species}`;
  else if (st === "reviewed") text = "✓ Reviewed";
  else if (st === "confident" && v.consensus && v.consensus.majority_species) text = `AI · ${v.consensus.majority_species}`;
  else if (st === "needs_review") text = "⚑ Needs review";
  else text = "Not analyzed";
  const b = el("span", "badge " + STATUS_META[st].cls);
  b.textContent = text;
  return b;
}

function renderFilterTabs(counts) {
  const wrap = $("#filterTabs");
  wrap.innerHTML = "";
  const tabs = [
    { key: "all", label: "All", n: state.videos.length },
    { key: "needs_review", label: "⚑ Needs review", n: counts.needs_review || 0 },
    { key: "confident", label: "AI confident", n: counts.confident || 0 },
    { key: "reviewed", label: "✓ Reviewed", n: counts.reviewed || 0 },
  ];
  tabs.forEach((t) => {
    const b = el("button", "filter-tab" + (state.filter === t.key ? " active" : "") + (t.key === "needs_review" && t.n ? " attention" : ""));
    b.innerHTML = `${t.label} <span class="ft-count">${t.n}</span>`;
    b.onclick = () => { state.filter = t.key; renderVideoList(); };
    wrap.append(b);
  });
}

function renderVideoList() {
  const list = $("#videoList");
  list.innerHTML = "";
  $("#videoCount").textContent = state.videos.length;
  const nUnprocessed = state.videos.filter((v) => !v.n_analyses).length;
  $("#batchBar").hidden = nUnprocessed === 0;
  const cap = state.settings.max_batch || 10;
  const hint = $("#batchHint");
  if (hint) {
    hint.textContent = nUnprocessed > cap ? `${nUnprocessed} waiting · ${cap} per batch` : `${nUnprocessed} waiting`;
  }

  // Tally workflow states for the triage tabs.
  const counts = {};
  state.videos.forEach((v) => { counts[v.workflow_status] = (counts[v.workflow_status] || 0) + 1; });
  renderFilterTabs(counts);

  if (!state.videos.length) {
    list.append(el("p", "hint", "No videos yet. Upload some to begin."));
    return;
  }

  const shown = state.videos.filter((v) => state.filter === "all" || v.workflow_status === state.filter);
  if (!shown.length) {
    const msgs = {
      needs_review: "Nothing needs review 🎉 — every analyzed clip is confident or done.",
      confident: "No confident clips yet.",
      reviewed: "No clips reviewed yet.",
    };
    list.append(el("p", "hint", msgs[state.filter] || "Nothing here."));
    return;
  }

  shown.forEach((v) => {
    const item = el("div", "video-item" + (v.id === state.selectedId ? " active" : ""));
    const thumb = el("img", "vi-thumb");
    thumb.src = `/api/videos/${v.id}/thumb`;
    thumb.onerror = () => { thumb.style.visibility = "hidden"; };
    const body = el("div", "vi-body");
    body.append(el("div", "vi-name", v.original_name));
    const sub = el("div", "vi-sub");
    sub.textContent = `${fmtDuration(v.duration)} · ${fmtSize(v.size_bytes)}`;
    body.append(sub);
    const badges = el("div", "vi-badges");
    badges.append(statusBadge(v));
    if (v.cost_usd) badges.append(el("span", "badge none", fmtCost(v.cost_usd)));
    body.append(badges);

    const del = el("button", "vi-del", "🗑");
    del.title = "Delete video";
    del.onclick = (ev) => { ev.stopPropagation(); deleteVideo(v.id); };

    item.append(thumb, body, del);
    item.onclick = () => selectVideo(v.id);
    list.append(item);
  });
}

async function deleteVideo(id) {
  if (!confirm("Delete this video and its analyses?")) return;
  try {
    await api(`/api/videos/${id}`, { method: "DELETE" });
    if (state.selectedId === id) { state.selectedId = null; state.detail = null; renderDetail(); }
    await loadVideos();
    toast("Video deleted.");
  } catch (e) { toast("Delete failed: " + e.message, true); }
}

// ---------------------------------------------------------------------------
// Upload
// ---------------------------------------------------------------------------
async function uploadFiles(fileList) {
  const files = [...fileList].filter((f) => f.type.startsWith("video/") || /\.(mp4|mov|avi|mkv|webm)$/i.test(f.name));
  if (!files.length) { toast("No video files selected.", true); return; }
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  toast(`Uploading ${files.length} video(s)… extracting frames…`);
  try {
    await api("/api/videos", { method: "POST", body: fd });
    await loadVideos();
    toast(`Uploaded ${files.length} video(s).`);
  } catch (e) {
    toast("Upload failed: " + e.message, true);
  }
}

// ---------------------------------------------------------------------------
// Detail / player
// ---------------------------------------------------------------------------
async function selectVideo(id) {
  state.selectedId = id;
  renderVideoList();
  $("#playerEmpty").hidden = true;
  $("#playerPane").hidden = false;
  const player = $("#player");
  player.src = `/api/videos/${id}/file`;
  player.load();
  try {
    state.detail = await api(`/api/videos/${id}`);
  } catch (e) {
    toast("Failed to load video: " + e.message, true);
    return;
  }
  renderDetail();
}

function renderDetail() {
  const d = state.detail;
  if (!d) {
    $("#playerEmpty").hidden = false;
    $("#playerPane").hidden = true;
    $("#reviewEmpty").hidden = false;
    $("#reviewPane").hidden = true;
    return;
  }
  const v = d.video;
  const meta = $("#playerMeta");
  meta.innerHTML = "";
  meta.append(el("span", null, `📹 ${v.original_name}`));
  meta.append(el("span", null, `⏱ ${fmtDuration(v.duration)}`));
  if (v.width) meta.append(el("span", null, `📐 ${v.width}×${v.height}`));
  if (d.totals.total_tokens)
    meta.append(el("span", null, `🪙 ${d.totals.total_tokens.toLocaleString()} tokens · ${fmtCost(d.totals.cost_usd)}`));

  renderConsensus(d.consensus, !!(d.review && d.review.decision));
  renderModelCards(d);
  renderReview(d);
}

function renderConsensus(c, reviewed) {
  const banner = $("#consensusBanner");
  if (!c || !c.n_succeeded) { banner.hidden = true; return; }
  banner.hidden = false;

  // Banner is driven by whether human review is needed (the triage signal),
  // not just the raw confidence level.
  let cls, icon, title;
  if (reviewed) {
    cls = "high"; icon = "✓"; title = "Reviewed by a human";
  } else if (c.needs_review) {
    cls = "low"; icon = "⚑"; title = "Needs human review — models disagree";
  } else {
    cls = "confident"; icon = "🤖"; title = "AI confident — auto-accepted (confirm to finalize)";
  }
  banner.className = "consensus-banner " + cls;

  const parts = [];
  if (c.majority_species) parts.push(`Species: <b>${escapeHtml(c.majority_species)}</b> (${Math.round(c.species_agreement * 100)}% agree)`);
  if (c.majority_behavior) parts.push(`Behavior: <b>${escapeHtml(c.majority_behavior)}</b> (${Math.round(c.behavior_agreement * 100)}% agree)`);
  parts.push(`${c.n_succeeded}/${c.n_models} models responded`);

  banner.innerHTML = "";
  banner.append(el("span", "cb-icon", icon));
  const txt = el("div", "cb-text");
  txt.append(el("div", "cb-title", title));
  txt.append(el("div", "cb-sub", parts.join(" &nbsp;·&nbsp; ")));
  banner.append(txt);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function renderModelCards(d) {
  const wrap = $("#modelCards");
  wrap.innerHTML = "";
  const majSpecies = d.consensus && d.consensus.majority_species
    ? normSpecies(d.consensus.majority_species) : null;
  // If a human review exists, highlight the models that matched their final call.
  const reviewedSpecies = d.review && d.review.final_species_common
    ? normSpecies(d.review.final_species_common) : null;

  // Show exactly what was analyzed. Only an UNanalyzed video falls back to the
  // currently-selected models as "not analyzed yet" placeholders — this avoids
  // ever mixing a prior run's models with the current selection.
  const byModel = {};
  d.analyses.forEach((a) => { byModel[a.model] = a; });
  const modelsToShow = d.analyses.length
    ? d.analyses.map((a) => a.model)
    : effectiveModels();

  if (!modelsToShow.length) {
    wrap.append(el("p", "hint", "Pick models under “Change…”, then click “Analyze this video”."));
    return;
  }

  modelsToShow.forEach((model) => {
    const a = byModel[model];
    const card = el("div", "model-card");
    const head = el("div", "mc-head");
    const left = el("div");
    const nameEl = el("div", "mc-model", friendlyModel(model));
    nameEl.title = model;
    left.append(nameEl);
    head.append(left);

    if (!a) {
      // Not analyzed yet
      head.append(el("span", "badge none", "pending"));
      card.append(head);
      card.append(el("p", "hint", "Not analyzed yet."));
      wrap.append(card);
      return;
    }

    if (a.status === "error") {
      card.classList.add("error");
      head.append(el("span", "badge low", "error"));
      card.append(head);
      card.append(el("div", "mc-error", "⚠ " + (a.error || "Unknown error")));
      card.append(metaRow(a));
      wrap.append(card);
      return;
    }

    // Mark the model(s) the human agreed with (after review).
    const matchedReview = reviewedSpecies && normSpecies(a.species_common) === reviewedSpecies;
    if (matchedReview) {
      card.classList.add("matched");
      head.append(el("span", "mc-matchtag", "✓ matches your review"));
    } else if (majSpecies) {
      // Otherwise show whether it agrees with the model consensus.
      const agree = normSpecies(a.species_common) === majSpecies;
      const dot = el("span", "mc-match " + (agree ? "agree" : "disagree"));
      dot.title = agree ? "Agrees with consensus" : "Differs from consensus";
      head.append(dot);
    }
    card.append(head);

    card.append(el("div", "mc-species", escapeHtml(a.species_common || "—")));
    if (a.species_scientific) card.append(el("div", "mc-sci", escapeHtml(a.species_scientific)));

    const beh = el("div", "mc-behavior");
    beh.innerHTML = `<span class="label">Behavior</span> ${escapeHtml(a.behavior || "—")}`
      + (a.count ? ` · ${a.count} indiv.` : "");
    card.append(beh);
    if (a.behavior_detail) card.append(el("div", "mc-detail", escapeHtml(a.behavior_detail)));
    if (a.notes) card.append(el("div", "mc-detail", "📝 " + escapeHtml(a.notes)));

    if (a.confidence != null) {
      const bar = el("div", "mc-conf-bar");
      const fill = el("span");
      fill.style.width = Math.round(a.confidence * 100) + "%";
      bar.append(fill);
      const wrapConf = el("div");
      wrapConf.append(el("div", "mc-detail", `Model confidence: ${Math.round(a.confidence * 100)}%`));
      wrapConf.append(bar);
      card.append(wrapConf);
    }

    card.append(metaRow(a));

    const useBtn = el("button", "btn small mc-use", "↳ Use this");
    useBtn.onclick = () => fillReviewFrom(a);
    card.append(useBtn);

    wrap.append(card);
  });
}

function metaRow(a) {
  const m = el("div", "mc-meta");
  m.append(el("span", null, `${(a.total_tokens || 0).toLocaleString()} tok`));
  m.append(el("span", null, `${a.latency_ms ? (a.latency_ms / 1000).toFixed(1) + "s" : "—"}`));
  m.append(el("span", "cost", fmtCost(a.cost_usd)));
  return m;
}

function normSpecies(s) {
  if (!s) return "";
  return s.toLowerCase().replace(/[^a-z0-9 ]/g, "").trim().replace(/s$/, "");
}

// ---------------------------------------------------------------------------
// Analysis
// ---------------------------------------------------------------------------
async function analyzeCurrent() {
  if (!state.selectedId) return;
  if (!state.settings.has_api_key) { toast("Analysis isn't configured yet — see README (.env setup).", true); return; }
  if (!effectiveModels().length) { toast("No models selected — open “Change…” to pick some.", true); openSettings(); return; }

  const btn = $("#analyzeBtn");
  btn.disabled = true; btn.textContent = "Analyzing… ⏳";
  setCardsLoading();
  // mark processing in list
  const v = state.videos.find((x) => x.id === state.selectedId);
  if (v) { v.status = "processing"; renderVideoList(); }

  try {
    state.detail = await api(`/api/videos/${state.selectedId}/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ models: effectiveModels() }),
    });
    recomputeSessionCost();
    renderDetail();
    await loadVideos();
    const c = state.detail.consensus;
    toast(c.flagged ? "Done — flagged for human review." : "Done — high confidence consensus.");
  } catch (e) {
    toast("Analysis failed: " + e.message, true);
  } finally {
    btn.disabled = false; btn.textContent = "🔬 Analyze this video";
  }
}

function setCardsLoading() {
  const wrap = $("#modelCards");
  wrap.innerHTML = "";
  effectiveModels().forEach((model) => {
    const card = el("div", "model-card");
    const head = el("div", "mc-head");
    const nameEl = el("div", "mc-model", friendlyModel(model));
    nameEl.title = model;
    head.append(nameEl);
    head.append(el("span", "mc-spinner"));
    card.append(head);
    card.append(el("p", "hint", "Running…"));
    wrap.append(card);
  });
  $("#consensusBanner").hidden = true;
}

async function analyzeAll() {
  const allUnprocessed = state.videos.filter((v) => !v.n_analyses).map((v) => v.id);
  if (!allUnprocessed.length) { toast("Nothing to analyze."); return; }
  if (!state.settings.has_api_key) { toast("Analysis isn't configured yet — see README (.env setup).", true); return; }
  if (!effectiveModels().length) { toast("No models selected — open “Change…” to pick some.", true); openSettings(); return; }

  const cap = state.settings.max_batch || 10;
  const queue = allUnprocessed.slice(0, cap);
  const skipped = allUnprocessed.length - queue.length;

  const btn = $("#analyzeAllBtn");
  btn.disabled = true;
  toast(skipped > 0
    ? `Analyzing ${queue.length} videos in parallel (max ${cap} per batch; ${skipped} left)…`
    : `Analyzing ${queue.length} video(s) in parallel…`);

  // Mark all as in-flight so their badges show "analyzing…" immediately.
  queue.forEach((id) => state.analyzing.add(id));
  renderVideoList();

  let done = 0, failed = 0;
  // Fan out: every video's request runs concurrently (each already runs its
  // models in parallel server-side). The library refreshes as each one finishes.
  await Promise.all(queue.map((id) =>
    api(`/api/videos/${id}/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ models: effectiveModels() }),
    })
      .then((detail) => {
        done++;
        if (state.selectedId === id) { state.detail = detail; renderDetail(); }
      })
      .catch((e) => { failed++; toast(`Failed on one video: ${e.message}`, true); })
      .finally(async () => {
        state.analyzing.delete(id);
        await loadVideos();  // live update: this clip's badge flips as it completes
      })
  ));

  recomputeSessionCost();
  btn.disabled = false;
  toast(`Batch complete — analyzed ${done} video(s)`
    + (failed ? `, ${failed} failed` : "")
    + (skipped > 0 ? `. ${skipped} remaining.` : "."));
}

async function recomputeSessionCost() {
  // Sum cost across all videos from the list endpoint.
  let sum = 0;
  state.videos.forEach((v) => { sum += v.cost_usd || 0; });
  state.sessionCost = sum;
  $("#sessionCostValue").textContent = fmtCost(sum);
}

// ---------------------------------------------------------------------------
// Review
// ---------------------------------------------------------------------------
function renderReview(d) {
  $("#reviewEmpty").hidden = true;
  $("#reviewPane").hidden = false;
  const r = d.review;
  const c = d.consensus;

  const badge = $("#reviewStatusBadge");
  badge.innerHTML = "";
  if (r && r.decision) {
    const labels = { approved: "Reviewed ✓ matches models", corrected: "Reviewed ✎ corrected", reviewed: "Reviewed" };
    badge.append(el("span", "badge " + r.decision, labels[r.decision] || ("Reviewed: " + r.decision)));
  } else if (c && c.needs_review && c.n_succeeded) {
    badge.append(el("span", "badge needs", "⚑ Needs review — models disagree"));
  } else if (c && c.n_succeeded) {
    badge.append(el("span", "badge confident", "🤖 AI confident — not yet human-reviewed"));
  } else {
    badge.append(el("span", "badge none", "Not analyzed yet"));
  }

  // Button reflects whether this is a first save or an update.
  $("#saveReviewBtn").textContent = (r && r.decision) ? "💾 Update review" : "💾 Save review";

  // Prefill: prior review > consensus majority
  $("#rvSpeciesCommon").value = r?.final_species_common || c?.majority_species || "";
  $("#rvSpeciesSci").value = r?.final_species_scientific || "";
  $("#rvBehavior").value = r?.final_behavior || c?.majority_behavior || "";
  $("#rvNotes").value = r?.notes || "";

  // datalist suggestions from the models
  const spSuggest = $("#speciesSuggest"), bhSuggest = $("#behaviorSuggest");
  spSuggest.innerHTML = ""; bhSuggest.innerHTML = "";
  const spSet = new Set(), bhSet = new Set();
  d.analyses.forEach((a) => {
    if (a.species_common) spSet.add(a.species_common);
    if (a.behavior) bhSet.add(a.behavior);
  });
  spSet.forEach((s) => { const o = el("option"); o.value = s; spSuggest.append(o); });
  bhSet.forEach((b) => { const o = el("option"); o.value = b; bhSuggest.append(o); });
}

function fillReviewFrom(a) {
  $("#rvSpeciesCommon").value = a.species_common || "";
  $("#rvSpeciesSci").value = a.species_scientific || "";
  $("#rvBehavior").value = a.behavior || "";
  if (a.notes && !$("#rvNotes").value) $("#rvNotes").value = a.notes;
  toast("Filled review from " + a.model.split("/").pop());
}

async function saveReview() {
  if (!state.selectedId) return;
  const finalSpecies = $("#rvSpeciesCommon").value.trim();
  const c = state.detail && state.detail.consensus;
  // Derive the decision automatically (also the reward signal for future model
  // scoring): "approved" if the human kept the models' consensus species,
  // "corrected" if they changed it, "reviewed" if there was nothing to compare.
  let decision = "reviewed";
  if (c && c.majority_species) {
    decision = normSpecies(finalSpecies) === normSpecies(c.majority_species) ? "approved" : "corrected";
  }
  const body = {
    decision,
    final_species_common: finalSpecies,
    final_species_scientific: $("#rvSpeciesSci").value.trim(),
    final_behavior: $("#rvBehavior").value.trim(),
    notes: $("#rvNotes").value.trim(),
  };
  try {
    state.detail = await api(`/api/videos/${state.selectedId}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    // Re-render the whole detail so the matched model card highlights and the
    // banner flips to "Reviewed", plus refresh the library badges/filter counts.
    renderDetail();
    await loadVideos();
    const msg = {
      approved: "✓ Saved — you confirmed the models. Marked as Reviewed.",
      corrected: "✓ Saved your correction. Marked as Reviewed.",
      reviewed: "✓ Review saved. Marked as Reviewed.",
    };
    toast(msg[decision] || "Review saved.");
  } catch (e) { toast("Save failed: " + e.message, true); }
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
function wire() {
  // Upload — the dropzone is a <label for="fileInput">, so clicking it opens the
  // native file picker with no JS (avoids the re-entrant input.click() bug). We only
  // wire change + drag/drop here.
  const dz = $("#dropzone"), input = $("#fileInput");
  input.onchange = () => { uploadFiles(input.files); input.value = ""; };
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files); });

  // Folder picker — webkitdirectory hands us every file in the chosen folder;
  // uploadFiles() filters that down to videos, so non-video files are ignored.
  const folderInput = $("#folderInput"), folderBtn = $("#folderBtn");
  if (folderBtn && folderInput) {
    folderBtn.onclick = () => folderInput.click();
    folderInput.onchange = () => { uploadFiles(folderInput.files); folderInput.value = ""; };
  }

  // Scoreboard
  $("#scoreboardBtn").onclick = openScoreboard;
  $("#closeScoreboard").onclick = closeScoreboard;
  $("#refreshScoreboard").onclick = loadScoreboard;
  $("#scoreboardModal").addEventListener("click", (e) => { if (e.target.id === "scoreboardModal") closeScoreboard(); });

  // Settings
  $("#settingsBtn").onclick = openSettings;
  $("#closeSettings").onclick = closeSettings;
  $("#saveSettings").onclick = saveSettings;
  $("#changeModelsBtn").onclick = openSettings;
  $("#loadModelsBtn").onclick = loadModels;
  $("#modelSearch").oninput = renderModelPicker;
  $("#frameCount").oninput = (e) => { $("#frameCountOut").textContent = e.target.value; };
  // Live-toggle Auto/Custom views as the radio changes (before Save).
  document.querySelectorAll('input[name="mode"]').forEach((r) => {
    r.onchange = () => {
      state.settings.mode = r.value;
      $("#customModelsView").hidden = r.value !== "custom";
      $("#autoModelsView").hidden = r.value === "custom";
    };
  });
  // Frames slider only applies in frame-sampling mode.
  document.querySelectorAll('input[name="inputMode"]').forEach((r) => {
    r.onchange = () => { $("#frameCountField").hidden = r.value !== "frames"; };
  });
  $("#addCustomModel").onclick = () => {
    const v = $("#customModel").value.trim();
    if (!v) return;
    const set = new Set(state.settings.selected_models || []);
    set.add(v);
    state.settings.selected_models = [...set];
    $("#customModel").value = "";
    renderSelectedModelTags();
    renderModelPicker();
  };
  $("#settingsModal").addEventListener("click", (e) => { if (e.target.id === "settingsModal") closeSettings(); });

  // Analyze
  $("#analyzeBtn").onclick = analyzeCurrent;
  $("#analyzeAllBtn").onclick = analyzeAll;

  // Review
  $("#saveReviewBtn").onclick = () => saveReview();
}

async function init() {
  wire();
  await loadSettings();
  await loadVideos();
  recomputeSessionCost();
}

init();
