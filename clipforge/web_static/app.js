/* Buildless local interface. Platform connections go through the local server. */
"use strict";

const icons = {
  brand: '<path d="M7 3h18v9L7 30V3Z"/><path d="M7 3v9h18M7 12l18 18H7"/>',
  studio: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  clips: '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="m10 9 5 3-5 3V9ZM5 7h3M5 17h3M16 7h3M16 17h3"/>',
  settings: '<path d="m9 3-.5 2.2-2 .9-2-.7-2 3.4L4 10.5v2.2l-1.5 1.7 2 3.4 2-.7 2 .9.5 2.2h4l.5-2.2 2-.9 2 .7 2-3.4-1.5-1.7v-2.2L20 8.8l-2-3.4-2 .7-2-.9L13.5 3H9Z"/><circle cx="11.3" cy="11.5" r="3"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"/>',
  sparkles: '<path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4L12 3ZM20 3v4M18 5h4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  play: '<path d="m9 5 11 7-11 7V5Z"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/>',
  "arrow-right": '<path d="M5 12h14m-5-5 5 5-5 5"/>',
  folder: '<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v2M3 7h17l-3 12H3V7Z"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.2 7a8 8 0 0 1 13.2-2L20 7M4 17l1.6 2A8 8 0 0 0 19 17"/>',
  film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7v.2"/>',
  youtube: '<rect x="3" y="5" width="18" height="14" rx="4"/><path d="m10 9 5 3-5 3V9Z"/>',
  instagram: '<rect x="3" y="3" width="18" height="18" rx="5"/><circle cx="12" cy="12" r="4"/><path d="M17.5 6.5h.1"/>',
  tiktok: '<path d="M14 3v12a5 5 0 1 1-4-4.9V15a1.5 1.5 0 1 0 1 1.4V3h3Zm0 0c.5 3 2.2 4.4 5 4.8v3c-2.1-.2-3.7-1-5-2.1"/>',
  twitch: '<path d="M5 3h16v12l-5 5h-4l-3 3v-3H5V3Zm0 0L2 6v14h5M10 7v6M16 7v6"/>',
  link: '<path d="m9 15 6-6M7 14l-2 2a3.5 3.5 0 0 0 5 5l4-4a3.5 3.5 0 0 0 0-5M10 12a3.5 3.5 0 0 1 0-5l4-4a3.5 3.5 0 0 1 5 5l-2 2"/>',
  upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  mountain: '<path d="m2 20 7-13 5 9 4-6 4 10H2ZM6 13l3 2 2-2"/><circle cx="17" cy="5" r="2"/>',
  scissors: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="m8 8 12 12M8 16 20 4"/>',
  crosshair: '<circle cx="12" cy="12" r="7"/><path d="M12 2v5M12 17v5M2 12h5M17 12h5"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  alert: '<path d="M10.4 4 2 19h20L13.6 4a1.8 1.8 0 0 0-3.2 0ZM12 9v4M12 16v.3"/>',
  spinner: '<path d="M21 12a9 9 0 1 1-9-9"/>',
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const icon = (name, className = "") => `<svg viewBox="0 0 ${name === "brand" ? "32 35" : "24 24"}" aria-hidden="true" class="${esc(className)}">${icons[name] || icons.film}</svg>`;
const state = {token: "", settings: {}, capabilities: {}, sources: [], jobs: [], clips: [], links: [], sourceId: null, clipId: null, attachLinkId: null, attachPendingUpload: false, socialPlatform: null, socialCreator: null, connected: false, sourceSignature: "", clipSignature: "", jobSignature: "", linkSignature: "", uploading: false, metadataDirty: false};
let pollTimer;
let healthBusy = false;
const storyState = {projects: [], capabilities: {}, project: null, dirty: false, busy: false, loading: false, signature: "", ideasJob: null};
const storyVoiceState = {voices: [], catalog: [], loaded: false, loading: false, engineReady: false, engineMessage: "", previewJob: null, pendingVoice: null, pendingInstalls: new Set()};

function paintIcons(root = document) {
  $$('[data-icon]', root).forEach((el) => { el.innerHTML = icon(el.dataset.icon); });
}

function toast(message, error = false) {
  const el = document.createElement("div");
  el.className = `toast${error ? " error" : ""}`;
  const symbol = document.createElement("span");
  symbol.innerHTML = icon(error ? "alert" : "check");
  const text = document.createElement("span");
  text.textContent = message;
  const dismiss = document.createElement("button");
  dismiss.textContent = "×";
  dismiss.setAttribute("aria-label", "Dismiss notification");
  dismiss.addEventListener("click", () => el.remove());
  el.append(symbol, text, dismiss);
  $("#toast-container").append(el);
  setTimeout(() => el.remove(), error ? 12000 : 6500);
}

function showError(selector, error) {
  const target = $(selector);
  target.textContent = typeof error === "string" ? error : error.message;
  target.hidden = false;
}

function clearError(selector) { $(selector).hidden = true; }

async function api(path, body, method) {
  const options = {method: method || (body === undefined ? "GET" : "POST"), headers: {"Accept": "application/json"}, cache: "no-store"};
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.headers["X-ClipForge-Token"] = state.token;
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); } catch { throw new Error("The local server returned an unreadable response. Check the ClipForge terminal."); }
  if (!response.ok) throw new Error(typeof data.error === "string" ? data.error : `The request could not be completed (${response.status}).`);
  return data;
}

function mediaUrl(value, expectedKind) {
  if (typeof value !== "string") return "";
  try {
    const url = new URL(value, location.origin);
    return url.origin === location.origin && url.pathname.startsWith(`/media/${expectedKind}/`) ? url.pathname + url.search : "";
  } catch { return ""; }
}

function externalUrl(value) {
  try { const url = new URL(value); return url.protocol === "https:" && !url.username && !url.password ? url.href : ""; }
  catch { return ""; }
}

function platformName(platform) { return {instagram: "Instagram", tiktok: "TikTok", youtube: "YouTube", twitch: "Twitch", kick: "Kick", direct: "Direct video"}[platform] || "Video link"; }

function duration(value, precise = false) {
  const seconds = Math.max(0, Number(value) || 0);
  const tenths = Math.floor(seconds * 10 + 0.00001);
  const whole = Math.floor(tenths / 10);
  const hours = Math.floor(whole / 3600);
  const mins = Math.floor((whole % 3600) / 60);
  const secs = whole % 60;
  if (precise) return `${hours ? `${hours}:` : ""}${hours ? String(mins).padStart(2, "0") : mins}:${String(secs).padStart(2, "0")}${tenths % 10 ? `.${tenths % 10}` : ""}`;
  return hours ? `${hours}:${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}` : `${mins}:${String(secs).padStart(2, "0")}`;
}

function bytes(value) {
  const size = Number(value) || 0;
  if (size > 1024 ** 3) return `${(size / 1024 ** 3).toFixed(1)} GB`;
  if (size > 1024 ** 2) return `${(size / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(size / 1024))} KB`;
}

function parseTime(value) {
  const text = String(value).trim();
  if (!/^(?:\d+:){0,2}\d+(?:\.\d+)?$/.test(text)) return NaN;
  const parts = text.split(":").map(Number);
  if (parts.length > 1 && parts.slice(1).some((part) => part >= 60)) return NaN;
  return parts.reduce((total, part) => total * 60 + part, 0);
}

function activeJobs() { return state.jobs.filter((job) => ["queued", "running"].includes(String(job.state).toLowerCase())); }
function isApproved(clip) { return ["PREPARED", "UPLOADED", "PROCESSING", "READY", "PUBLISHED"].includes(String(clip.state).toUpperCase()); }
function currentClip() { return state.clips.find((clip) => clip.id === state.clipId); }
function currentSource() { return state.sources.find((source) => source.id === state.sourceId); }
function sampleSource(source) { return Boolean(source?.is_demo || /synthetic.*sample|sample.*synthetic|clipforge.*sample/i.test(source?.name || "")); }
function availableSource(source) { return Boolean(source && source.available !== false && !source.source_deleted); }

function navigate() {
  const page = ["studio", "clips", "stories", "settings"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "studio";
  $$(".page").forEach((el) => { el.hidden = el.id !== `page-${page}`; el.classList.toggle("active", !el.hidden); });
  $$("[data-page]").forEach((el) => { el.classList.toggle("active", el.dataset.page === page); if (el.dataset.page === page) el.setAttribute("aria-current", "page"); else el.removeAttribute("aria-current"); });
  $("#breadcrumb-page").textContent = {studio: "Studio", clips: "Your clips", stories: "AI Story Generator", settings: "Settings"}[page];
  document.title = `ClipForge · ${{studio: "Studio", clips: "Your clips", stories: "AI Story Generator", settings: "Settings"}[page]}`;
  if (page === "clips") renderClips();
  if (page === "stories" && state.connected) refreshStories();
}

function emptyState(symbol, title, description, suggestion = "") {
  return `<div class="empty-state"><span class="empty-icon">${icon(symbol)}</span><div><strong>${esc(title)}</strong><p>${esc(description)}</p></div>${suggestion ? `<span class="empty-suggestion">${esc(suggestion)} ${icon("arrow-right")}</span>` : ""}</div>`;
}

function renderSources() {
  const visibleSources = state.sources.filter((source) => !source.source_deleted);
  $("#source-count").textContent = visibleSources.length;
  if (!visibleSources.length) {
    $("#source-list").innerHTML = emptyState("film", "A blank canvas, for now.", "Add your first video above to get things rolling.", "Your next idea goes here");
    return;
  }
  $("#source-list").innerHTML = visibleSources.map((source) => {
    const available = availableSource(source);
    const url = available ? mediaUrl(source.url, "source") : "";
    return `<article class="source-row${available ? "" : " source-unavailable"}"><div class="source-thumb">${url ? `<video src="${esc(url)}#t=0.1" muted preload="metadata" aria-hidden="true" tabindex="-1"></video>` : icon("film")}</div><div class="source-info"><h3 title="${esc(source.name)}">${esc(source.name)}${sampleSource(source) ? '<span class="sample-badge">SAMPLE</span>' : ""}${available ? "" : `<span class="badge">${source.source_deleted ? "Source deleted" : "Source unavailable"}</span>`}</h3><p>${available ? `${esc(duration(source.duration_s))} <span aria-hidden="true">·</span> ${esc(source.width)} × ${esc(source.height)} <span aria-hidden="true">·</span> ${esc(bytes(source.bytes))}${source.has_audio === false ? " · No audio" : ""}` : "Finished clips are kept. Import the video again to create more clips."}</p></div><div class="source-actions">${available ? `<button class="button subtle small" type="button" data-create="${esc(source.id)}">${icon("scissors")} Create clip <span aria-hidden="true">↗</span></button>` : ""}${source.managed_file && available ? `<button class="text-button danger-text" type="button" data-source-delete="${esc(source.id)}" aria-label="Delete source video: ${esc(source.name)}" title="${source.can_delete_file ? "Delete the source file and keep finished clips" : "Wait for this source's jobs to finish before deleting it"}"${source.can_delete_file ? "" : " disabled"}>${icon("trash")} Delete source</button>` : ""}</div></article>`;
  }).join("");
}

function renderLinks() {
  $("#link-count").textContent = state.links.length;
  if (!state.links.length) { $("#link-list").innerHTML = emptyState("link", "Bring a link along.", "Paste a supported public video link to download it to this computer."); return; }
  $("#link-list").innerHTML = state.links.map((link) => {
    const url = externalUrl(link.url);
    const ready = link.state === "ready" && state.sources.some((source) => source.id === link.source_id && availableSource(source));
    const importing = link.state === "importing";
    const canImport = !ready && !importing && link.can_download;
    return `<article class="link-card"><span class="link-platform-icon">${icon(icons[link.platform] ? link.platform : "link")}</span><div class="link-card-main"><div class="link-card-title"><strong>${esc(platformName(link.platform))}</strong><span class="badge${ready ? " green" : importing ? " blue" : ""}">${ready ? "Ready to clip" : importing ? "Importing" : link.state === "failed" ? "Import needs attention" : canImport ? "Ready to import" : "Video file needed"}</span></div><p class="link-url">${esc(link.url)}</p><p class="link-message">${esc(link.message || (ready ? "Your video is ready to clip." : canImport ? "Import the source video onto this computer, then create your clips." : "Attach an authorized local video file to use this source."))}</p><div class="link-card-actions">${url ? `<a class="text-button" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open original ↗</a>` : ""}${ready ? `<button class="text-button" type="button" data-create="${esc(link.source_id)}">Create clip ↗</button>` : ""}${canImport ? `<button class="button subtle small" type="button" data-link-retry="${esc(link.id)}">${icon(link.state === "failed" ? "refresh" : "download")} ${link.state === "failed" ? "Retry import" : "Import video"}</button>` : ""}${!ready && !importing ? `<button class="text-button" type="button" data-attach="${esc(link.id)}">${icon("folder")} Attach a video</button>` : ""}<button class="text-button danger-text link-delete" type="button" data-link-delete="${esc(link.id)}" aria-label="Delete saved link: ${esc(link.url)}" title="${importing ? "Wait for the import to finish" : "Delete this saved link"}"${importing ? " disabled" : ""}>${icon("trash")} Delete</button></div></div></article>`;
  }).join("");
}

function updateAttachSources() {
  const select = $("#attach-source-select");
  const selected = select.value;
  const sources = state.sources.filter((source) => availableSource(source) && !sampleSource(source));
  select.innerHTML = '<option value="">Choose a source…</option>' + sources.map((source) => `<option value="${esc(source.id)}">${esc(source.name)} · ${esc(duration(source.duration_s))}</option>`).join("");
  if (sources.some((source) => source.id === selected)) select.value = selected;
  select.disabled = !sources.length;
  $("#attach-submit").disabled = select.disabled;
}

function openAttach(linkId) {
  const link = state.links.find((item) => item.id === linkId);
  if (!link) return;
  state.attachLinkId = linkId;
  $("#attach-link-name").textContent = `Attach the matching video file for ${link.url}`;
  updateAttachSources();
  clearError("#attach-error");
  $("#attach-dialog").showModal();
}

async function attachSource(sourceId) {
  if (!state.attachLinkId) return;
  const source = state.sources.find((item) => item.id === sourceId);
  if (source && !availableSource(source)) throw new Error("This source video is unavailable. Import its file again before attaching it.");
  await api(`/api/links/${encodeURIComponent(state.attachLinkId)}/attach`, {source_id: sourceId});
  state.attachLinkId = null;
}

function jobLabel(job) {
  const status = String(job.state).toLowerCase();
  if (status === "completed") return "Complete";
  if (status === "failed") return "Needs attention";
  if (status === "interrupted") return "Interrupted";
  if (status === "cancelled") return "Cancelled";
  if (status === "queued") return "In the queue";
  if (job.cancel_requested) return "Stopping safely";
  const stage = String(job.stage || "Working");
  const labels = {preparing: "Preparing video", resolving: "Finding video", downloading: "Downloading video", merging: "Combining audio & video", verifying: "Checking video", transcribing: "Transcribing speech", selecting: "Finding moments", rendering: "Rendering clips", metadata: "Writing clip details"};
  if (labels[stage]) return labels[stage];
  return stage.charAt(0).toUpperCase() + stage.slice(1).replace(/_/g, " ");
}

function remainingTime(value) {
  const seconds = Math.max(1, Math.ceil(value));
  if (seconds < 60) return `${Math.ceil(seconds / 5) * 5} sec`;
  if (seconds < 3600) return `${Math.ceil(seconds / 60)} min`;
  return `${(Math.ceil(seconds / 360) / 10).toFixed(1)} hr`;
}

function timingLabel(job) {
  if (job.state === "queued") return "Time estimate starts when this job begins.";
  const elapsed = typeof job.elapsed_s === "number" && Number.isFinite(job.elapsed_s) ? `${duration(job.elapsed_s)} elapsed` : "";
  if (job.state !== "running") return elapsed;
  if (job.cancel_requested) return [elapsed, "Stopping and cleaning up…"].filter(Boolean).join(" · ");
  const eta = job.eta_seconds;
  let estimate = "Estimating time…";
  if (job.timing_status === "stalled") estimate = "Waiting for progress · estimate paused";
  else if (job.timing_status === "longer_than_expected") estimate = "Taking longer than previous jobs · updating estimate";
  if (!["stalled", "longer_than_expected"].includes(job.timing_status) && typeof eta === "number" && Number.isFinite(eta) && eta >= 0) {
    const low = job.eta_min_seconds;
    const high = job.eta_max_seconds;
    const range = typeof low === "number" && typeof high === "number" && Number.isFinite(low) && Number.isFinite(high) && low >= 0 && high >= low && remainingTime(low) !== remainingTime(high);
    estimate = eta < 1 ? "Finishing this step…" : `~${range ? `${remainingTime(low)}–${remainingTime(high)}` : remainingTime(eta)} left in this step`;
    if (job.estimate_source === "history") estimate += " · based on previous jobs";
  }
  return [elapsed, estimate].filter(Boolean).join(" · ");
}

function renderJobs() {
  const active = activeJobs();
  $("#job-count").textContent = state.jobs.length;
  $("#workshop-status").textContent = active.length ? `${active.length} ${active.length === 1 ? "job" : "jobs"} in progress` : "All caught up";
  $("#workshop-dot").style.background = active.length ? "#e8a16c" : "#a2af91";
  $("#save-settings").disabled = active.length > 0;
  $("#settings-save-note").textContent = active.length ? "Settings can be saved when the current jobs finish." : "Settings are saved to your local workspace.";
  if (!state.jobs.length) {
    $("#job-list").innerHTML = emptyState("scissors", "Nothing in the works yet.", "Your clip jobs will appear here as you create them.");
    return;
  }
  const ordered = state.jobs.slice().sort((a, b) => Number(["queued", "running"].includes(b.state)) - Number(["queued", "running"].includes(a.state)) || String(b.created_at || "").localeCompare(String(a.created_at || "")));
  $("#job-list").innerHTML = ordered.slice(0, 12).map((job) => {
    const status = String(job.state).toLowerCase();
    const running = ["queued", "running"].includes(status);
    const failed = ["failed", "interrupted"].includes(status);
    const canRetry = (failed || status === "cancelled") && (["manual", "ai"].includes(job.mode) || String(job.kind).startsWith("story"));
    const downloadTotal = Number(job.download_total_bytes) || 0;
    const downloadBytes = Number(job.download_bytes) || 0;
    const downloadProgress = job.mode === "link_import" && job.stage === "downloading" && Number.isFinite(downloadTotal) && Number.isFinite(downloadBytes) && downloadTotal > 0 && downloadBytes >= 0 && downloadBytes <= downloadTotal;
    const stageProgress = typeof job.stage_percent === "number" && Number.isFinite(job.stage_percent) && job.stage_percent >= 0 && job.stage_percent <= 100;
    const measured = downloadProgress || stageProgress;
    const progress = downloadProgress ? downloadBytes / downloadTotal * 100 : stageProgress ? job.stage_percent : 0;
    const progressLabel = `${jobLabel(job)}${downloadProgress ? ` · ${bytes(downloadBytes)} / ${bytes(downloadTotal)}` : measured ? ` · ${Math.floor(progress)}% of this step` : ""}`;
    const clipId = Array.isArray(job.clip_ids) ? job.clip_ids.find((id) => state.clips.some((clip) => clip.id === id)) : null;
    const canCancel = running && (job.can_cancel || job.cancel_requested);
    return `<article class="job-row"><span class="job-icon${running ? " active" : failed ? " failed" : ""}">${icon(running ? "spinner" : failed ? "alert" : status === "cancelled" ? "close" : "check", running ? "loading-spinner" : "")}</span><div class="job-main"><h3>${esc(job.title || job.source_name || "Studio job")}</h3><p>${esc(job.message || job.source_name || jobLabel(job))}</p><p class="job-timing">${esc(timingLabel(job))}</p></div><div class="job-actions">${running ? `<div class="job-progress"><span>${esc(progressLabel)}</span><div class="progress-track${measured ? "" : " indeterminate"}" role="progressbar" aria-label="${esc(jobLabel(job))}" aria-valuemin="0" aria-valuemax="100"${measured ? ` aria-valuenow="${Math.floor(progress)}"` : ' aria-valuetext="Working; progress is not yet known"'}><span${measured ? ` style="width:${progress}%"` : ""}></span></div></div>` : canRetry ? `<button class="button subtle small" type="button" data-retry="${esc(job.id)}">${icon("refresh")} Retry</button>` : clipId ? `<button class="text-button" type="button" data-review="${esc(clipId)}">Review clip ↗</button>` : failed ? '<a class="text-button" href="#settings">Check settings ↗</a>' : `<span class="job-state">${esc(jobLabel(job))}</span>`}${canCancel ? `<button class="text-button" type="button" data-job-cancel="${esc(job.id)}" aria-label="Cancel task: ${esc(job.title || job.source_name || "Studio job")}"${job.cancel_requested ? " disabled" : ""}>${icon("close")} ${job.cancel_requested ? "Stopping…" : "Cancel"}</button>` : ""}${job.can_delete ? `<button class="text-button danger-text" type="button" data-job-delete="${esc(job.id)}" aria-label="Delete workshop item: ${esc(job.title || job.source_name || "Studio job")}">${icon("trash")} Delete</button>` : ""}</div></article>`;
  }).join("");
}

function clipBadge(clip) {
  if (clip.submitted) return '<span class="badge blue">Publishing started</span>';
  if (isApproved(clip)) return `<span class="badge green">${icon("check")} Approved</span>`;
  return '<span class="badge">Needs review</span>';
}

function renderClips() {
  $("#clip-count").textContent = state.clips.length;
  $("#clips-summary").textContent = `${state.clips.length} ${state.clips.length === 1 ? "clip" : "clips"} in your collection`;
  const filter = $("#clip-filter").value;
  const query = $("#clip-search").value.trim().toLocaleLowerCase();
  const sort = $("#clip-sort").value;
  const clips = state.clips.filter((clip) => (filter === "all" || (filter === "review" ? !isApproved(clip) && !clip.submitted : isApproved(clip) || clip.submitted)) && (!query || String(clip.title || "Untitled clip").toLocaleLowerCase().includes(query)));
  if (query || filter !== "all") $("#clips-summary").textContent = `${clips.length} of ${state.clips.length} clips shown`;
  if (!clips.length) {
    $("#clip-grid").innerHTML = emptyState("clips", state.clips.length ? "No clips in this view." : "Your best moments belong here.", state.clips.length ? "Try another filter to see the rest of your collection." : "Head to the Studio, add a video, and create your first clip.");
    return;
  }
  const compare = sort === "title" ? (a, b) => String(a.title || "").localeCompare(String(b.title || "")) : sort === "shortest" ? (a, b) => Number(a.duration_s || 0) - Number(b.duration_s || 0) : sort === "oldest" ? (a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")) : (a, b) => String(b.created_at || "").localeCompare(String(a.created_at || ""));
  $("#clip-grid").innerHTML = clips.slice().sort(compare).map((clip) => {
    const url = mediaUrl(clip.video_url, "clip");
    return `<article class="clip-card"><button class="clip-thumbnail" type="button" data-review="${esc(clip.id)}" aria-label="Review ${esc(clip.title)}">${url ? `<video src="${esc(url)}#t=0.1" muted preload="metadata" aria-hidden="true" tabindex="-1"></video>` : ""}<span class="clip-play">${icon("play")}</span><span class="clip-duration">${esc(duration(clip.duration_s))}</span></button><div class="clip-card-body"><h3>${esc(clip.title || "Untitled clip")}</h3><div class="clip-card-details"><span>${clip.mode === "story" ? "AI-generated story" : clip.mode === "ai" ? "Selected by local AI" : "Picked by you"}</span><span>${esc(clip.width || 1080)} × ${esc(clip.height || 1920)}</span></div><div class="clip-card-footer">${clipBadge(clip)}<button class="text-button" type="button" data-review="${esc(clip.id)}">Review & export ↗</button></div><div class="clip-card-actions">${!clip.is_demo ? `<button class="text-button" type="button" data-youtube-review="${esc(clip.id)}">${icon("youtube")} ${clip.publication ? "YouTube status" : isApproved(clip) ? "Upload to YouTube" : "Review for YouTube"}</button>` : ""}<button class="text-button danger-text" type="button" data-clip-delete="${esc(clip.id)}" aria-label="Delete clip: ${esc(clip.title || "Untitled clip")}" title="${esc(clip.delete_message || "Delete this finished clip from your computer")}"${clip.can_delete === false ? " disabled" : ""}>${icon("trash")} Delete clip</button></div></div></article>`;
  }).join("");
}

function renderHealth() {
  const capabilities = state.capabilities;
  const tools = [{key: "ffmpeg", name: "FFmpeg", purpose: "Video rendering"}, {key: "ffprobe", name: "FFprobe", purpose: "Video inspection"}, {key: "link_import", name: "Link importer · yt-dlp", purpose: "Local YouTube, Twitch and Kick imports"}, {key: "speech", name: "Whisper", purpose: "AI speech captions"}, {key: "ollama", name: "Ollama", purpose: "AI clip selection"}];
  $("#health-list").innerHTML = tools.map((tool) => {
    const info = capabilities[tool.key] || {};
    return `<article class="health-card${info.ok ? " ok" : ""}"><div class="health-card-top"><h3>${esc(tool.name)}</h3>${icon(info.ok ? "check" : "alert")}</div><p>${esc(info.message || tool.purpose)}</p><span class="health-state">${info.ok ? "Ready to use" : "Setup needed"}${["speech", "ollama"].includes(tool.key) ? " · optional" : ""}</span></article>`;
  }).join("");
  const renderReady = capabilities.ffmpeg?.ok && capabilities.ffprobe?.ok;
  const aiReady = capabilities.speech?.ok && capabilities.ollama?.ok;
  $("#setup-summary").textContent = renderReady ? (aiReady ? "Your studio is ready" : "Ready to create manual clips") : "Let's get your studio ready";
  $("#setup-detail").textContent = renderReady ? (aiReady ? "Video tools and local AI are connected." : "Add local AI in Settings whenever you're ready.") : "Set up FFmpeg and FFprobe in Settings.";
  $("#ai-readiness").textContent = aiReady ? "Your local AI tools are ready." : "Local AI setup is incomplete. Check Whisper and Ollama in Settings before starting.";
  $("#link-import-status").textContent = capabilities.link_import?.message || "Link imports download the source video to this computer. Check setup for importer readiness.";
  ["instagram", "tiktok", "twitch"].forEach((platform) => { const info = capabilities[platform]; $("#" + platform + "-setup-status").textContent = info?.message || "Save your settings, then check setup to inspect the credential file."; });
  $("#youtube-setup-status").textContent = capabilities.youtube?.message || "Save your settings and check setup, then connect through Google.";
  $("#youtube-authorize").disabled = !capabilities.youtube?.ok;
}

function fillSettings() {
  $$("[name]", $("#settings-form")).forEach((input) => {
    const [group, key] = input.name.split(".");
    const value = state.settings[group]?.[key];
    if (input.type === "checkbox") input.checked = Boolean(value);
    else if (value !== undefined && value !== null) input.value = String(value);
    else input.value = "";
  });
  updateCaptionPreset();
}

function updateCaptionPreset() {
  const size = $("#settings-form").elements["render.font_size"].value;
  $("#caption-preset").value = ["42", "56", "72"].includes(size) ? size : "custom";
}

async function refreshHealth(showToast = false) {
  if (healthBusy) return;
  healthBusy = true;
  const button = $("#check-health");
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `${icon("spinner", "loading-spinner")} Checking…`;
  try {
    const data = await api("/api/health");
    state.capabilities = data.capabilities || data;
    renderHealth();
    if (showToast) toast("Setup check complete.");
  } catch (error) { toast(error.message, true); }
  finally { healthBusy = false; button.disabled = false; button.innerHTML = original; }
}

function applyLibrary(data) {
  state.sources = Array.isArray(data.sources) ? data.sources : [];
  state.jobs = Array.isArray(data.jobs) ? data.jobs : [];
  state.clips = Array.isArray(data.clips) ? data.clips : [];
  state.links = Array.isArray(data.links) ? data.links : [];
  if (state.clipId && !currentClip()) clearSelectedClip();
  const sourceSignature = JSON.stringify(state.sources);
  const jobSignature = JSON.stringify(state.jobs);
  const clipSignature = JSON.stringify(state.clips);
  const linkSignature = JSON.stringify(state.links);
  const sourcesChanged = sourceSignature !== state.sourceSignature;
  if (sourcesChanged) {
    state.sourceSignature = sourceSignature;
    renderSources();
    if ($("#attach-dialog").open) updateAttachSources();
    if ($("#create-dialog").open && !availableSource(currentSource())) {
      $("#source-video").pause();
      $("#source-video").removeAttribute("src");
      $("#source-video").load();
      $("#create-submit").disabled = true;
      showError("#create-error", "This source video is unavailable. Import it again to create more clips. Your finished clips are kept.");
    }
  }
  if (jobSignature !== state.jobSignature) { state.jobSignature = jobSignature; renderJobs(); if ($("#review-dialog").open && currentClip()) updateReviewState(); }
  if (clipSignature !== state.clipSignature) { state.clipSignature = clipSignature; renderClips(); if ($("#review-dialog").open && currentClip()) updateReviewState(); }
  if (sourcesChanged || linkSignature !== state.linkSignature) { state.linkSignature = linkSignature; renderLinks(); }
}

async function refreshLibrary() {
  try {
    const data = await api("/api/library");
    applyLibrary(data);
    state.connected = true;
    $("#connection-banner").hidden = true;
  } catch {
    state.connected = false;
    $("#connection-banner").hidden = false;
  }
}

async function poll() {
  await refreshLibrary();
  if (location.hash === "#stories") {
    await refreshStories();
    if (storyVoiceState.loaded || storyVoiceState.previewJob) await refreshStoryVoiceLibrary();
  }
  pollTimer = setTimeout(poll, activeJobs().length || state.uploading ? 2000 : 8000);
}

function scheduleFastPoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(poll, 800);
}

function setUploadBusy(busy) {
  state.uploading = busy;
  $("#choose-video").disabled = busy;
  $("#try-demo").disabled = busy;
  $("#import-path").disabled = busy;
  $("#paste-link").disabled = busy;
  $("#upload-progress").hidden = !busy;
}

function uploadVideo(file) {
  if (!file || state.uploading) return;
  setUploadBusy(true);
  $("#upload-message").textContent = `Importing ${file.name}…`;
  $("#upload-percent").textContent = "0%";
  $("#upload-meter").value = 0;
  const request = new XMLHttpRequest();
  request.open("POST", `/api/sources?name=${encodeURIComponent(file.name)}`);
  request.setRequestHeader("Content-Type", "application/octet-stream");
  request.setRequestHeader("X-ClipForge-Token", state.token);
  request.setRequestHeader("Accept", "application/json");
  request.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable) return;
    const percent = Math.round(event.loaded / event.total * 100);
    $("#upload-meter").value = percent;
    $("#upload-percent").textContent = `${percent}%`;
    if (percent === 100) $("#upload-message").textContent = "Checking your video…";
  });
  request.addEventListener("load", async () => {
    setUploadBusy(false);
    $("#video-file").value = "";
    let data;
    try { data = JSON.parse(request.responseText); } catch { toast("The video could not be imported. Check the ClipForge terminal.", true); return; }
    if (request.status < 200 || request.status >= 300) { toast(data.error || "The video could not be imported.", true); return; }
    const source = data.source || data;
    try { if (state.attachLinkId) await attachSource(source.id); }
    catch (error) { toast(`The video was imported, but its link could not be attached: ${error.message}`, true); }
    await refreshLibrary();
    toast("Video imported. Let's find your moment.");
    if (source.id) openCreate(source.id);
  });
  request.addEventListener("error", () => { setUploadBusy(false); toast("The local connection was interrupted. Keep ClipForge running, then try importing again.", true); });
  request.addEventListener("abort", () => { setUploadBusy(false); toast("Import stopped."); });
  request.send(file);
}

function updateSelectionDuration() {
  const form = $("#create-form");
  const start = parseTime(form.elements.start_s.value);
  const end = parseTime(form.elements.end_s.value);
  const length = end - start;
  $("#selection-duration").textContent = Number.isFinite(length) && length > 0 ? `${Number(length.toFixed(1))} seconds selected` : "Choose a valid range";
  $("#selection-duration").style.color = Number.isFinite(length) && length >= 1 && length <= 180 ? "" : "#b98359";
}

function setMode(mode) {
  $("#create-form").elements.mode.value = mode;
  $$("[data-mode]").forEach((button) => { button.classList.toggle("selected", button.dataset.mode === mode); button.setAttribute("aria-pressed", String(button.dataset.mode === mode)); });
  $("#manual-fields").hidden = mode !== "manual";
  $("#ai-fields").hidden = mode !== "ai";
  $("#create-form").elements.start_s.required = mode === "manual";
  $("#create-form").elements.end_s.required = mode === "manual";
  $("#create-submit").innerHTML = `${icon(mode === "ai" ? "sparkles" : "scissors")} ${mode === "ai" ? "Find my clips" : "Create clip"} <span aria-hidden="true">↗</span>`;
}

function updateFraming() {
  const framing = $("#create-form").elements.framing.value;
  const choices = {center_crop: ["Fill the frame", "Crop the center to fill a 9:16 vertical video."], fit: ["Keep the whole picture", "Fit your full video inside a 9:16 black canvas."], blur: ["A softer backdrop", "Keep the full picture with a blurred background."]};
  const selected = choices[framing] || choices.center_crop;
  $("#framing-label").textContent = selected[0];
  $("#framing-description").textContent = selected[1];
  $("#framing-visual").className = `framing-visual ${framing}`;
}

function openCreate(sourceId) {
  const source = state.sources.find((item) => item.id === sourceId);
  if (!availableSource(source)) { toast("This source is no longer available. Import it again.", true); return; }
  state.sourceId = sourceId;
  const form = $("#create-form");
  form.reset();
  $("#create-submit").disabled = false;
  form.elements.title.value = sampleSource(source) ? "My first ClipForge clip" : String(source.name || "New clip").replace(/\.[^.]+$/, "").slice(0, 85);
  form.elements.end_s.value = duration(Math.min(Number(source.duration_s) || 15, 30), true);
  form.elements.start_s.value = "0:00";
  form.elements.creator_credit.value = sampleSource(source) ? "ClipForge synthetic demonstration" : "";
  form.elements.contribution.value = "Original video selected, reframed and edited into a short clip.";
  form.querySelector(".rights-details").open = !sampleSource(source);
  $("#permission-field").hidden = true;
  form.elements.permission_reference.required = false;
  setMode("manual");
  updateFraming();
  updateSelectionDuration();
  $("#source-video").src = mediaUrl(source.url, "source");
  $("#create-source-name").textContent = source.name;
  $("#create-source-info").textContent = `${duration(source.duration_s)} · ${source.width} × ${source.height} · ${bytes(source.bytes)}${source.has_audio === false ? " · No audio" : ""}`;
  $("#sample-note").hidden = !sampleSource(source);
  const aiButton = $('[data-mode="ai"]');
  aiButton.disabled = sampleSource(source);
  aiButton.title = sampleSource(source) ? "The sample has no speech. Use manual mode." : "Use local Whisper and Ollama to choose clips";
  clearError("#create-error");
  $("#create-dialog").showModal();
  setTimeout(() => form.elements.title.focus(), 20);
}

function updateReviewState() {
  const clip = currentClip();
  if (!clip) return;
  $("#review-status").innerHTML = state.metadataDirty ? '<span class="badge">Unsaved details · save before approving</span>' : clipBadge(clip);
  const locked = Boolean(clip.submitted);
  const youtubeSubmitted = Boolean(clip.publication);
  $$("input,textarea,button", $("#metadata-form")).forEach((input) => { input.disabled = locked; });
  $("#approval-panel").hidden = (isApproved(clip) && !state.metadataDirty) || locked;
  $("#approve-clip").disabled = !$("#review-confirm").checked || locked || state.metadataDirty;
  const publish = $("#open-publish");
  const resume = $("#resume-publish");
  const publication = clip.publication;
  let publicationText = "Connect your channel in Settings to publish from here.";
  if (state.metadataDirty) publicationText = "Save your updated details, then review and approve the clip before publishing.";
  else if (clip.is_demo) publicationText = "The synthetic sample is for trying the studio. Publishing this sample is disabled.";
  else if (youtubeSubmitted) {
    const status = typeof publication === "object" && publication ? String(publication.state || publication.status || "submitted").replace(/_/g, " ") : "submitted";
    publicationText = `YouTube: ${status}. ${clip.resume_message || "Submitted clips are locked. Refresh publishing status in Settings for updates."}`;
  } else if (!isApproved(clip)) publicationText = "Watch the clip and approve it before publishing.";
  else if (state.settings.upload?.enabled && state.capabilities.youtube?.ok) publicationText = `Upload to ${state.settings.upload.target_channel_id || "your configured channel"} with ${state.settings.upload.privacy_status || "private"} visibility. Upload now or choose when YouTube should publish it.`;
  $("#publication-summary").textContent = publicationText;
  publish.disabled = youtubeSubmitted || clip.is_demo || !isApproved(clip) || !state.settings.upload?.enabled || !state.capabilities.youtube?.ok || state.metadataDirty;
  publish.hidden = youtubeSubmitted;
  resume.hidden = !clip.can_resume;
  resume.disabled = !clip.can_resume || !state.settings.upload?.enabled;
  $("#setup-youtube").hidden = Boolean(clip.publication || (state.settings.upload?.enabled && state.capabilities.youtube?.ok));
  $("#delete-review-clip").disabled = clip.can_delete === false;
  $("#delete-review-clip").title = clip.delete_message || "Delete this finished clip from your computer";
  renderSocialPanels(clip);
}

function renderSocialPanels(clip) {
  $("#social-publication-panels").innerHTML = ["instagram", "tiktok"].map((platform) => {
    const record = clip.social_publications?.[platform];
    const name = platformName(platform);
    const pending = activeJobs().find((job) => job.platform === platform && job.target_clip_id === clip.id);
    let message = "Enable publishing and configure your private credentials file in Settings.";
    if (pending) message = `${name} has an action in progress. Follow its result in the workshop before making another request.`;
    else if (record) message = record.message || `Status: ${String(record.state || "submitted").replace(/_/g, " ")}. Check status before taking further action.`;
    else if (clip.is_demo) message = "Publishing the synthetic setup sample is disabled. Use your own authorized video.";
    else if (state.metadataDirty) message = "Save your clip details and approve the updated clip before publishing.";
    else if (!isApproved(clip)) message = "Review and approve this clip before publishing.";
    else if (state.settings[platform]?.enabled) message = state.capabilities[platform]?.ok ? "Check the live account and publishing options, then confirm your post." : (state.capabilities[platform]?.message || "Check platform setup in Settings before publishing.");
    const allowed = !record && !pending && !clip.is_demo && isApproved(clip) && !state.metadataDirty && state.settings[platform]?.enabled && state.capabilities[platform]?.ok;
    return `<section class="publication-panel"><div class="publication-heading">${icon(platform)}<strong>${esc(name)}</strong>${record ? `<span class="badge blue">${esc(String(record.state || "submitted").replace(/_/g, " "))}</span>` : ""}</div><p>${esc(message)}</p>${record ? `<div class="button-row"><button class="button subtle small" type="button" data-social-refresh="${esc(platform)}"${pending ? " disabled" : ""}>${icon("refresh")} Check status</button>${platform === "instagram" && record.can_finish ? `<button class="button primary small" type="button" data-social-finish="instagram"${pending ? " disabled" : ""}>Finish Instagram publishing</button>` : ""}</div>` : `<button class="button subtle small" type="button" data-social-publish="${esc(platform)}"${allowed ? "" : " disabled"}>${pending ? "Request in progress" : `Publish to ${esc(name)}`} <span aria-hidden="true">↗</span></button>`}</section>`;
  }).join("");
}

function updateSocialDisclosures() {
  const fields = $("#social-form").elements;
  const paid = fields.brand_content.value === "true";
  const own = fields.brand_organic.value === "true";
  const message = paid && own ? "Your video will be labeled Paid partnership and Promotional content." : paid ? "Your video will be labeled Paid partnership. Paid partnerships cannot use private (Only me) visibility." : own ? "Your video will be labeled Promotional content." : "Choose both commercial content disclosures for this video.";
  $("#brand-disclosure-message").textContent = message;
  $("#music-consent-copy").innerHTML = `By posting, I agree to TikTok's <a href="https://www.tiktok.com/legal/page/global/music-usage-confirmation/en" target="_blank" rel="noopener noreferrer">Music Usage Confirmation</a>${paid ? ' and <a href="https://www.tiktok.com/legal/page/global/bc-policy/en" target="_blank" rel="noopener noreferrer">Branded Content Policy</a>' : ""}.`;
  fields.music_usage_confirmed.checked = false;
}

async function openSocial(platform) {
  const clip = currentClip();
  if (!clip || clip.is_demo || !isApproved(clip) || state.metadataDirty || clip.social_publications?.[platform]) return;
  state.socialPlatform = platform;
  state.socialCreator = null;
  const name = platformName(platform);
  const form = $("#social-form");
  form.reset();
  clearError("#social-error");
  $("#social-dialog-title").textContent = `Publish to ${name}`;
  $("#social-creator-icon").innerHTML = icon(platform);
  $("#social-creator-name").textContent = "Checking creator account…";
  $("#social-creator-message").textContent = `Fetching your account and publishing options from ${name}.`;
  $("#social-loading").hidden = false;
  $("#social-fields").hidden = true;
  $("#social-fields").disabled = true;
  $("#social-submit").disabled = true;
  $("#social-submit").innerHTML = `${icon(platform)} Publish to ${esc(name)}`;
  $("#instagram-options").hidden = platform !== "instagram";
  $("#tiktok-options").hidden = platform !== "tiktok";
  form.elements.caption.value = (clip.description || clip.title || "").slice(0, 2200).replace(/[\uD800-\uDBFF]$/, "");
  $("#social-caption-count").textContent = `${form.elements.caption.value.length} / 2200`;
  $$("input,select", $("#tiktok-options")).forEach((input) => { input.disabled = platform !== "tiktok"; input.required = platform === "tiktok" && (input.tagName === "SELECT" || input.name === "music_usage_confirmed"); });
  form.elements.share_to_feed.disabled = platform !== "instagram";
  $("#social-confirm-copy").textContent = `I reviewed the full clip and authorize posting it to the ${name} account shown above.`;
  updateSocialDisclosures();
  $("#social-dialog").showModal();
  try {
    const data = await api(`/api/social/${platform}/creator`);
    if (!$("#social-dialog").open || state.socialPlatform !== platform) return;
    const creator = data.creator || data;
    state.socialCreator = creator;
    const identity = [creator.nickname, creator.username ? `@${String(creator.username).replace(/^@/, "")}` : "", creator.account_id ? `ID ${creator.account_id}` : ""].filter(Boolean).join(" · ");
    $("#social-creator-name").textContent = identity || `${name} creator account`;
    $("#social-creator-message").textContent = creator.message || "Account checked. Review the options and confirm when you're ready.";
    if (platform === "tiktok") {
      const options = Array.isArray(creator.privacy_level_options) ? creator.privacy_level_options.filter((value) => typeof value === "string") : [];
      form.elements.privacy_level.innerHTML = '<option value="">Choose a privacy option…</option>' + options.map((value) => `<option value="${esc(value)}">${esc({PUBLIC_TO_EVERYONE: "Everyone", MUTUAL_FOLLOW_FRIENDS: "Friends · mutual followers", FOLLOWER_OF_CREATOR: "Followers", SELF_ONLY: "Only me · private"}[value] || value.replace(/_/g, " "))}</option>`).join("");
      ["comment", "duet", "stitch"].forEach((kind) => { form.elements[`allow_${kind}`].disabled = Boolean(creator[`${kind}_disabled`]); form.elements[`allow_${kind}`].checked = false; $(`#${kind}-restriction`).hidden = !creator[`${kind}_disabled`]; });
      if (!options.length) throw new Error("TikTok returned no publishing privacy options for this account. Check platform eligibility and app authorization.");
      if (Number(creator.max_video_post_duration_sec) > 0 && Number(clip.duration_s) > Number(creator.max_video_post_duration_sec)) throw new Error(`This creator can post videos up to ${creator.max_video_post_duration_sec} seconds. Create a shorter clip before publishing.`);
      if (creator.client_audited === false) $("#social-creator-message").textContent += " This client is unaudited; only private posting is available, subject to TikTok's account restrictions.";
    }
    $("#social-fields").hidden = false;
    $("#social-fields").disabled = false;
    $("#social-submit").disabled = false;
  } catch (error) { showError("#social-error", error); $("#social-creator-name").textContent = "Publishing setup needs attention"; $("#social-creator-message").textContent = "Complete setup and account authorization before posting."; }
  finally { $("#social-loading").hidden = true; }
}

async function refreshSocial(platform, button) {
  const clip = currentClip();
  if (!clip) return;
  const restore = startAction(button, "Checking…");
  clearError("#review-error");
  try { await api(`/api/clips/${encodeURIComponent(clip.id)}/social/${platform}/refresh`, {}); await refreshLibrary(); updateReviewState(); scheduleFastPoll(); toast(`${platformName(platform)} status check requested.`); }
  catch (error) { showError("#review-error", error); }
  finally { restore(); }
}

function openReview(clipId) {
  const clip = state.clips.find((item) => item.id === clipId);
  if (!clip) { toast("This clip is not available yet. Wait for the render to finish.", true); return false; }
  if ($("#review-dialog").open && state.clipId === clipId) { updateReviewState(); return true; }
  if ($("#review-dialog").open && state.metadataDirty) { toast("Save or close your current clip review before opening another clip.", true); return false; }
  state.clipId = clipId;
  state.metadataDirty = false;
  const form = $("#metadata-form");
  form.elements.title.value = clip.title || "";
  form.elements.description.value = clip.description || "";
  form.elements.tags.value = Array.isArray(clip.tags) ? clip.tags.join(", ") : "";
  $("#clip-video").src = mediaUrl(clip.video_url, "clip");
  $("#review-spec").textContent = `${clip.width || 1080} × ${clip.height || 1920} · ${duration(clip.duration_s)} · MP4`;
  const downloadUrl = mediaUrl(clip.download_url || clip.video_url, "clip");
  if (downloadUrl) {
    const parsed = new URL(downloadUrl, location.origin);
    parsed.searchParams.set("download", "1");
    $("#download-clip").href = parsed.pathname + parsed.search;
  } else $("#download-clip").removeAttribute("href");
  const captionUrl = mediaUrl(clip.caption_url, "caption");
  $("#download-caption").hidden = !captionUrl;
  if (captionUrl) $("#download-caption").href = captionUrl;
  $("#clip-reason").hidden = !clip.reason;
  $("#clip-reason").textContent = clip.reason || "";
  $("#review-confirm").checked = false;
  clearError("#review-error");
  updateReviewState();
  $("#review-dialog").showModal();
  return true;
}

function clearSelectedClip() {
  ["#publish-dialog", "#social-dialog", "#review-dialog"].forEach((selector) => { if ($(selector).open) $(selector).close(); });
  const video = $("#clip-video");
  video.pause();
  video.removeAttribute("src");
  video.load();
  ["#download-clip", "#download-caption"].forEach((selector) => $(selector).removeAttribute("href"));
  state.clipId = null;
  state.metadataDirty = false;
}

function openDeleteClip(clipId) {
  const clip = state.clips.find((item) => item.id === clipId);
  if (!clip || clip.can_delete === false) { toast(clip?.delete_message || "This clip is not available to delete right now.", true); return; }
  $("#delete-clip-form").elements.clip_id.value = clip.id;
  $("#delete-clip-name").textContent = clip.title || "Untitled clip";
  clearError("#delete-clip-error");
  $("#delete-clip-dialog").showModal();
}

function openPublish() {
  const clip = currentClip();
  if (!clip || clip.publication || clip.is_demo || !isApproved(clip) || state.metadataDirty || !state.settings.upload?.enabled || !state.capabilities.youtube?.ok) return;
  $("#publish-form").reset();
  $("#publish-submit").innerHTML = `${icon("youtube")} Upload to YouTube`;
  $("#publish-destination").textContent = `Upload “${clip.title}” to channel ${state.settings.upload.target_channel_id}. Saved visibility: ${state.settings.upload.privacy_status || "private"}.`;
  $("#schedule-timezone").textContent = `Time zone: ${Intl.DateTimeFormat().resolvedOptions().timeZone || "your browser's local time"}. YouTube publishes a scheduled upload automatically after it has finished processing. Leave at least 60 minutes before publishing; your workspace may require more.`;
  clearError("#publish-error");
  $("#publish-dialog").showModal();
}

function configureSourceCleanup(kind, sourceId, checked) {
  const source = state.sources.find((item) => item.id === sourceId);
  const available = source?.managed_file && availableSource(source);
  const checkbox = $(`#delete-${kind}-form`).elements.delete_source;
  $(`#delete-${kind}-source-option`).hidden = !available;
  checkbox.disabled = !available || !source.can_delete_file;
  checkbox.checked = available && source.can_delete_file && checked;
  $(`#delete-${kind}-source-label`).textContent = available
    ? `Also permanently delete the source video from this computer (${bytes(source.bytes)}). Finished clips are kept.`
    : "";
  $(`#delete-${kind}-source-note`).textContent = available && !source.can_delete_file
    ? "Source video cleanup is available after the current tasks finish." : "";
}

async function retryJob(jobId, button) {
  button.disabled = true;
  try { await api(`/api/jobs/${encodeURIComponent(jobId)}/retry`, {}); toast("Job added back to the queue."); await refreshLibrary(); scheduleFastPoll(); }
  catch (error) { toast(error.message, true); button.disabled = false; }
}

function startAction(button, text) {
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `${icon("spinner", "loading-spinner")} ${esc(text)}`;
  return () => { button.innerHTML = original; button.disabled = false; };
}

async function youtubeAction(action, button) {
  const restore = startAction(button, "Working…");
  try {
    await api(`/api/youtube/${action}`, {});
    toast(action === "authorize" ? "Channel connection started. Complete Google's consent in the browser window." : action === "revoke" ? "Disconnect requested. Follow the result in the workshop." : "Publishing status check started.");
    await refreshLibrary();
    scheduleFastPoll();
  } catch (error) { toast(error.message, true); }
  finally { restore(); }
}

function bindEvents() {
  window.addEventListener("hashchange", navigate);
  $("#health-shortcut").addEventListener("click", () => { location.hash = "settings"; });
  $("#open-setup").addEventListener("click", () => { location.hash = "settings"; });
  $("#choose-video").addEventListener("click", () => { state.attachLinkId = null; state.attachPendingUpload = false; $("#video-file").click(); });
  $("#video-file").addEventListener("cancel", () => { state.attachLinkId = null; state.attachPendingUpload = false; });
  $("#video-file").addEventListener("change", (event) => uploadVideo(event.target.files[0]));
  const dropzone = $("#dropzone");
  let dragDepth = 0;
  dropzone.addEventListener("dragenter", (event) => { event.preventDefault(); dragDepth++; dropzone.classList.add("drag-over"); });
  dropzone.addEventListener("dragover", (event) => { event.preventDefault(); event.dataTransfer.dropEffect = state.uploading ? "none" : "copy"; });
  dropzone.addEventListener("dragleave", (event) => { event.preventDefault(); dragDepth--; if (!dragDepth) dropzone.classList.remove("drag-over"); });
  dropzone.addEventListener("drop", (event) => { event.preventDefault(); dragDepth = 0; dropzone.classList.remove("drag-over"); state.attachLinkId = null; state.attachPendingUpload = false; if (event.dataTransfer.files.length > 1) toast("We'll start with the first video. You can add more afterward."); uploadVideo(event.dataTransfer.files[0]); });
  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("drop", (event) => event.preventDefault());
  $("#try-demo").addEventListener("click", async (event) => {
    const restore = startAction(event.currentTarget, "Opening sample…");
    try { const data = await api("/api/demo", {}); const source = data.source || data; await refreshLibrary(); if (source.id) openCreate(source.id); }
    catch (error) { toast(error.message, true); }
    finally { restore(); }
  });
  $("#source-list").addEventListener("click", (event) => {
    const create = event.target.closest("[data-create]");
    const remove = event.target.closest("[data-source-delete]");
    if (create) openCreate(create.dataset.create);
    if (remove) {
      const source = state.sources.find((item) => item.id === remove.dataset.sourceDelete);
      if (!source?.managed_file || !availableSource(source) || !source.can_delete_file) return;
      $("#delete-source-form").elements.source_id.value = source.id;
      $("#delete-source-name").textContent = `${source.name} · ${bytes(source.bytes)}`;
      clearError("#delete-source-error");
      $("#delete-source-dialog").showModal();
    }
  });
  $("#delete-source-cancel").addEventListener("click", () => $("#delete-source-dialog").close());
  $("#delete-source-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const sourceId = event.currentTarget.elements.source_id.value;
    clearError("#delete-source-error");
    const restore = startAction($("#delete-source-submit"), "Deleting source…");
    try {
      await api(`/api/sources/${encodeURIComponent(sourceId)}/delete`, {confirmed: true});
      $("#delete-source-dialog").close();
      await refreshLibrary();
      toast("Source video deleted. Your finished clips are kept.");
    } catch (error) { showError("#delete-source-error", error); }
    finally { restore(); }
  });
  $("#paste-link").addEventListener("click", () => { $("#link-form").reset(); clearError("#link-error"); $("#link-dialog").showModal(); });
  $("#link-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError("#link-error");
    const url = externalUrl(event.currentTarget.elements.url.value.trim());
    if (!url) { showError("#link-error", "Enter a complete HTTPS video or page URL without a username or password."); return; }
    const restore = startAction($("#link-submit"), "Adding link…");
    try { const data = await api("/api/links", {url, rights_confirmed: event.currentTarget.elements.rights_confirmed.checked}); $("#link-dialog").close(); await refreshLibrary(); scheduleFastPoll(); toast(data.link?.state === "importing" ? "Video import queued. Follow its progress in Linked videos." : "Link saved. Check its status in Linked videos and attach a file if needed."); }
    catch (error) { showError("#link-error", error); }
    finally { restore(); }
  });
  $("#link-list").addEventListener("click", async (event) => {
    const create = event.target.closest("[data-create]");
    const attach = event.target.closest("[data-attach]");
    const retry = event.target.closest("[data-link-retry]");
    const remove = event.target.closest("[data-link-delete]");
    if (remove) {
      const linkId = remove.dataset.linkDelete;
      const link = state.links.find((item) => item.id === linkId);
      if (!link || link.state === "importing") return;
      $("#delete-link-form").elements.link_id.value = linkId;
      $("#delete-link-url").textContent = link.url;
      configureSourceCleanup("link", link.source_id, true);
      clearError("#delete-link-error");
      $("#delete-link-dialog").showModal();
      return;
    }
    if (create) openCreate(create.dataset.create);
    if (attach) openAttach(attach.dataset.attach);
    if (retry) {
      const restore = startAction(retry, "Queuing…");
      try { await api(`/api/links/${encodeURIComponent(retry.dataset.linkRetry)}/retry`, {}); await refreshLibrary(); scheduleFastPoll(); toast("Video import queued. Follow its progress in the workshop."); }
      catch (error) { toast(error.message, true); }
      finally { restore(); }
    }
  });
  $("#delete-link-cancel").addEventListener("click", () => $("#delete-link-dialog").close());
  $("#delete-link-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const linkId = event.currentTarget.elements.link_id.value;
    clearError("#delete-link-error");
    const restore = startAction($("#delete-link-submit"), "Deleting…");
    try {
      const deleteSource = event.currentTarget.elements.delete_source.checked;
      await api(`/api/links/${encodeURIComponent(linkId)}/delete`, {confirmed: true, delete_source: deleteSource});
      if (state.attachLinkId === linkId) { state.attachLinkId = null; state.attachPendingUpload = false; if ($("#attach-dialog").open) $("#attach-dialog").close(); }
      $("#delete-link-dialog").close();
      await refreshLibrary();
      toast(deleteSource ? "Saved link and source video deleted from this computer. Finished clips kept." : "Saved link deleted. Video files are kept.");
    } catch (error) { showError("#delete-link-error", error); }
    finally { restore(); }
  });
  $("#attach-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError("#attach-error");
    const sourceId = event.currentTarget.elements.source_id.value;
    const restore = startAction($("#attach-submit"), "Attaching…");
    try { await attachSource(sourceId); $("#attach-dialog").close(); await refreshLibrary(); toast("Video attached. You can now create clips from this source."); }
    catch (error) { showError("#attach-error", error); }
    finally { restore(); }
  });
  $("#attach-upload").addEventListener("click", () => { state.attachPendingUpload = true; $("#attach-dialog").close(); $("#video-file").click(); });
  $("#job-list").addEventListener("click", (event) => {
    const retry = event.target.closest("[data-retry]");
    const review = event.target.closest("[data-review]");
    const remove = event.target.closest("[data-job-delete]");
    const cancel = event.target.closest("[data-job-cancel]");
    if (retry) retryJob(retry.dataset.retry, retry);
    if (review) openReview(review.dataset.review);
    if (cancel) {
      const job = state.jobs.find((item) => item.id === cancel.dataset.jobCancel);
      if (!job?.can_cancel || job.cancel_requested) return;
      $("#cancel-job-form").elements.job_id.value = job.id;
      $("#cancel-job-name").textContent = job.title || job.source_name || "Studio job";
      clearError("#cancel-job-error");
      $("#cancel-job-dialog").showModal();
    }
    if (remove) {
      const job = state.jobs.find((item) => item.id === remove.dataset.jobDelete);
      if (!job?.can_delete) return;
      $("#delete-job-form").elements.job_id.value = job.id;
      $("#delete-job-name").textContent = job.title || job.source_name || "Studio job";
      configureSourceCleanup("job", job.source_id, false);
      clearError("#delete-job-error");
      $("#delete-job-dialog").showModal();
    }
  });
  $("#cancel-job-keep").addEventListener("click", () => $("#cancel-job-dialog").close());
  $("#cancel-job-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const jobId = event.currentTarget.elements.job_id.value;
    const restore = startAction($("#cancel-job-submit"), "Stopping…");
    clearError("#cancel-job-error");
    try {
      await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {confirmed: true});
      $("#cancel-job-dialog").close();
      await refreshLibrary();
      scheduleFastPoll();
      toast("Cancellation requested. ClipForge will stop this task and clean up temporary files.");
    } catch (error) { showError("#cancel-job-error", error); }
    finally { restore(); }
  });
  $("#delete-job-cancel").addEventListener("click", () => $("#delete-job-dialog").close());
  $("#delete-job-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const fields = event.currentTarget.elements;
    const deleteSource = fields.delete_source.checked;
    const restore = startAction($("#delete-job-submit"), "Deleting…");
    clearError("#delete-job-error");
    try {
      await api(`/api/jobs/${encodeURIComponent(fields.job_id.value)}/delete`, {confirmed: true, delete_source: deleteSource});
      $("#delete-job-dialog").close();
      await refreshLibrary();
      toast(deleteSource ? "Workshop item and source video deleted from this computer. Finished clips kept." : "Workshop item deleted. Your videos and clips are kept.");
    } catch (error) { showError("#delete-job-error", error); }
    finally { restore(); }
  });
  $("#clip-grid").addEventListener("click", (event) => {
    const remove = event.target.closest("[data-clip-delete]");
    const youtube = event.target.closest("[data-youtube-review]");
    const review = event.target.closest("[data-review]");
    if (remove) openDeleteClip(remove.dataset.clipDelete);
    else if (youtube) { if (openReview(youtube.dataset.youtubeReview)) openPublish(); }
    else if (review) openReview(review.dataset.review);
  });
  $("#clip-filter").addEventListener("change", renderClips);
  $("#clip-search").addEventListener("input", renderClips);
  $("#clip-sort").addEventListener("change", renderClips);
  $("#delete-review-clip").addEventListener("click", () => { if (state.clipId) openDeleteClip(state.clipId); });
  $("#delete-clip-cancel").addEventListener("click", () => $("#delete-clip-dialog").close());
  $("#delete-clip-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const clipId = event.currentTarget.elements.clip_id.value;
    clearError("#delete-clip-error");
    const restore = startAction($("#delete-clip-submit"), "Deleting clip…");
    // Release the open preview before Windows removes the underlying media.
    const selected = state.clipId === clipId;
    if (selected) { $("#clip-video").pause(); $("#clip-video").removeAttribute("src"); $("#clip-video").load(); }
    try {
      const result = await api(`/api/clips/${encodeURIComponent(clipId)}/delete`, {confirmed: true});
      $("#delete-clip-dialog").close();
      if (state.clipId === clipId) clearSelectedClip();
      await refreshLibrary();
      toast(`Clip deleted from this computer${result.bytes_freed > 0 ? ` · ${bytes(result.bytes_freed)} freed` : ""}.`);
    } catch (error) { showError("#delete-clip-error", error); if (selected && currentClip()) $("#clip-video").src = mediaUrl(currentClip().video_url, "clip"); }
    finally { restore(); }
  });
  $("#import-path").addEventListener("click", () => { clearError("#path-error"); $("#path-dialog").showModal(); });
  $("#path-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError("#path-error");
    const restore = startAction($("#path-submit"), "Checking video…");
    try { const data = await api("/api/import", {path: event.currentTarget.elements.path.value.trim()}); const source = data.source || data; await refreshLibrary(); $("#path-dialog").close(); toast("Video added to your sources."); if (source.id) openCreate(source.id); }
    catch (error) { showError("#path-error", error); }
    finally { restore(); }
  });
  $$("[data-mode]").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
  const createForm = $("#create-form");
  createForm.elements.start_s.addEventListener("input", updateSelectionDuration);
  createForm.elements.end_s.addEventListener("input", updateSelectionDuration);
  createForm.elements.framing.addEventListener("change", updateFraming);
  createForm.elements.rights_category.addEventListener("change", (event) => { const required = event.target.value !== "original"; $("#permission-field").hidden = !required; createForm.elements.permission_reference.required = required; });
  createForm.addEventListener("invalid", (event) => { const details = event.target.closest("details"); if (details) details.open = true; }, true);
  $("#set-start").addEventListener("click", () => { createForm.elements.start_s.value = duration(Math.floor(($("#source-video").currentTime || 0) * 10) / 10, true); updateSelectionDuration(); });
  $("#set-end").addEventListener("click", () => { createForm.elements.end_s.value = duration(Math.floor(($("#source-video").currentTime || 0) * 10) / 10, true); updateSelectionDuration(); });
  createForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError("#create-error");
    const fields = createForm.elements;
    const source = currentSource();
    const start = parseTime(fields.start_s.value);
    const end = parseTime(fields.end_s.value);
    if (!availableSource(source)) { showError("#create-error", "Choose an available source video first. Import a deleted source again to make more clips."); return; }
    if (fields.mode.value === "manual" && (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end - start < 1 || end - start > 180 || end > Number(source.duration_s) + .05)) { showError("#create-error", "Choose a valid range within your video, between 1 second and 3 minutes long. Use mm:ss or seconds."); return; }
    const restore = startAction($("#create-submit"), "Starting…");
    const payload = {source_id: source.id, mode: fields.mode.value, title: fields.title.value.trim(), framing: fields.framing.value, start_s: fields.mode.value === "manual" ? start : 0, end_s: fields.mode.value === "manual" ? end : 0, caption_text: fields.mode.value === "manual" ? fields.caption_text.value.trim() : "", rights_category: fields.rights_category.value, permission_reference: fields.rights_category.value === "original" ? "Original recording owned by me" : fields.permission_reference.value.trim(), creator_credit: fields.creator_credit.value.trim(), contribution: fields.contribution.value.trim()};
    try { await api("/api/jobs", payload); $("#create-dialog").close(); location.hash = "studio"; toast(fields.mode.value === "ai" ? "Local AI is on the job. Follow its progress in the workshop." : "Your clip is in the workshop. We'll show it here when it's ready."); await refreshLibrary(); scheduleFastPoll(); setTimeout(() => $("#jobs-title").scrollIntoView({behavior: "smooth", block: "center"}), 100); }
    catch (error) { showError("#create-error", error); }
    finally { restore(); }
  });
  $("#metadata-form").addEventListener("input", () => { state.metadataDirty = true; $("#review-confirm").checked = false; updateReviewState(); });
  $("#metadata-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError("#review-error");
    const clip = currentClip();
    if (!clip) return;
    const form = event.currentTarget;
    const restore = startAction($("#save-metadata"), "Saving…");
    try {
      await api(`/api/clips/${encodeURIComponent(clip.id)}`, {title: form.elements.title.value.trim(), description: form.elements.description.value, tags: form.elements.tags.value.split(",").map((tag) => tag.trim()).filter(Boolean)});
      state.metadataDirty = false;
      $("#review-confirm").checked = false;
      await refreshLibrary();
      updateReviewState();
      toast("Clip details saved. Review and approve the updated clip.");
    } catch (error) { showError("#review-error", error); }
    finally { restore(); updateReviewState(); }
  });
  $("#review-confirm").addEventListener("change", updateReviewState);
  $("#social-publication-panels").addEventListener("click", async (event) => {
    const publish = event.target.closest("[data-social-publish]");
    const refresh = event.target.closest("[data-social-refresh]");
    const finish = event.target.closest("[data-social-finish]");
    if (publish) openSocial(publish.dataset.socialPublish);
    if (refresh) refreshSocial(refresh.dataset.socialRefresh, refresh);
    if (finish) {
      const clip = currentClip();
      const record = clip?.social_publications?.instagram;
      if (!record?.can_finish) return;
      if (!window.confirm(`Finish publishing the previously uploaded “${clip.title}” to Instagram account ${record.account_id || "shown in the saved publication"}, using its saved caption and feed choice?`)) return;
      const restore = startAction(finish, "Finishing…");
      clearError("#review-error");
      try { await api(`/api/clips/${encodeURIComponent(clip.id)}/social/instagram/finish`, {confirmed: true}); await refreshLibrary(); updateReviewState(); scheduleFastPoll(); toast("Instagram finish request submitted. Check its saved status for the result."); }
      catch (error) { showError("#review-error", error); }
      finally { restore(); }
    }
  });
  const socialForm = $("#social-form");
  socialForm.elements.caption.addEventListener("input", () => { $("#social-caption-count").textContent = `${socialForm.elements.caption.value.length} / 2200`; });
  socialForm.elements.brand_content.addEventListener("change", updateSocialDisclosures);
  socialForm.elements.brand_organic.addEventListener("change", updateSocialDisclosures);
  socialForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError("#social-error");
    const clip = currentClip();
    const creator = state.socialCreator;
    const platform = state.socialPlatform;
    const fields = socialForm.elements;
    if (!clip || !creator || !["instagram", "tiktok"].includes(platform)) return;
    const caption = fields.caption.value.trim();
    if (!caption || caption.length > 2200) { showError("#social-error", "Enter a caption between 1 and 2200 characters."); return; }
    const payload = {confirmed: fields.confirmed.checked, caption};
    if (platform === "instagram") { payload.account_id = String(creator.account_id || ""); payload.share_to_feed = fields.share_to_feed.checked; }
    else {
      if (fields.brand_content.value === "true" && fields.privacy_level.value === "SELF_ONLY") { showError("#social-error", "A paid partnership cannot use Only me visibility. Choose another allowed privacy option, or correct the partnership disclosure if it does not apply."); return; }
      Object.assign(payload, {creator_username: String(creator.username || ""), privacy_level: fields.privacy_level.value, disable_comment: !fields.allow_comment.checked || fields.allow_comment.disabled, disable_duet: !fields.allow_duet.checked || fields.allow_duet.disabled, disable_stitch: !fields.allow_stitch.checked || fields.allow_stitch.disabled, brand_content_toggle: fields.brand_content.value === "true", brand_organic_toggle: fields.brand_organic.value === "true", is_aigc: fields.is_aigc.value === "true", music_usage_confirmed: fields.music_usage_confirmed.checked});
    }
    const restore = startAction($("#social-submit"), "Submitting…");
    try { await api(`/api/clips/${encodeURIComponent(clip.id)}/social/${platform}/publish`, payload); $("#social-dialog").close(); await refreshLibrary(); updateReviewState(); scheduleFastPoll(); toast(`${platformName(platform)} publishing requested. Follow the saved status before taking any further action.`); }
    catch (error) { showError("#social-error", error); await refreshLibrary(); updateReviewState(); if (currentClip()?.social_publications?.[platform]) { $("#social-submit").disabled = true; $("#social-fields").disabled = true; } }
    finally { restore(); if (currentClip()?.social_publications?.[platform]) $("#social-submit").disabled = true; }
  });
  $("#approve-clip").addEventListener("click", async (event) => {
    const clip = currentClip();
    if (!clip || !$("#review-confirm").checked) return;
    clearError("#review-error");
    const restore = startAction(event.currentTarget, "Approving…");
    try { await api(`/api/clips/${encodeURIComponent(clip.id)}/approve`, {reviewed: true}); await refreshLibrary(); updateReviewState(); toast("Clip approved. Your moment is ready to share."); }
    catch (error) { showError("#review-error", error); }
    finally { restore(); updateReviewState(); }
  });
  $("#check-health").addEventListener("click", () => refreshHealth(true));
  $("#caption-preset").addEventListener("change", (event) => {
    if (event.target.value !== "custom") $("#settings-form").elements["render.font_size"].value = event.target.value;
  });
  $("#settings-form").elements["render.font_size"].addEventListener("input", updateCaptionPreset);
  $("#settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {};
    $$("[name]", event.currentTarget).forEach((input) => { const [group, key] = input.name.split("."); payload[group] ||= {}; payload[group][key] = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value.trim(); });
    if (!payload.speech.language) payload.speech.language = null;
    const restore = startAction($("#save-settings"), "Saving…");
    try { const data = await api("/api/settings", payload); state.settings = data.settings || payload; fillSettings(); toast("Your studio settings are saved."); await refreshHealth(); }
    catch (error) { toast(error.message, true); }
    finally { restore(); renderJobs(); }
  });
  $("#youtube-authorize").addEventListener("click", (event) => youtubeAction("authorize", event.currentTarget));
  $("#setup-youtube").addEventListener("click", () => {
    if ($("#review-dialog").open) $("#review-dialog").close();
    location.hash = "settings";
    $("#youtube-settings").open = true;
    $("#youtube-settings").scrollIntoView({behavior: "smooth", block: "start"});
  });
  $("#youtube-poll").addEventListener("click", (event) => youtubeAction("poll", event.currentTarget));
  $("#youtube-revoke").addEventListener("click", (event) => {
    if (window.confirm("Disconnect YouTube and remove stored authorization and owned-channel API data? Your local videos will remain.")) youtubeAction("revoke", event.currentTarget);
  });
  $("#resume-publish").addEventListener("click", async (event) => {
    const clip = currentClip();
    if (!clip?.can_resume) return;
    if (!window.confirm(`Resume the existing upload for “${clip.title}” using its saved video, channel, audience, disclosure and schedule? ${clip.resume_message || ""}`)) return;
    const restore = startAction(event.currentTarget, "Resuming…");
    clearError("#review-error");
    try { await api(`/api/clips/${encodeURIComponent(clip.id)}/resume`, {confirmed: true}); toast("Resuming your saved YouTube upload. Follow the result in the workshop."); await refreshLibrary(); scheduleFastPoll(); }
    catch (error) { showError("#review-error", error); }
    finally { restore(); updateReviewState(); }
  });
  $("#open-publish").addEventListener("click", openPublish);
  $("#publish-form").elements.schedule.addEventListener("change", (event) => {
    $("#publish-submit").innerHTML = `${icon("youtube")} ${event.target.value ? "Upload & schedule" : "Upload to YouTube"}`;
  });
  $("#publish-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const clip = currentClip();
    if (!clip) return;
    clearError("#publish-error");
    const fields = event.currentTarget.elements;
    let schedule = null;
    if (fields.schedule.value) {
      const date = new Date(fields.schedule.value);
      if (!Number.isFinite(date.getTime())) { showError("#publish-error", "Choose a valid publishing time."); return; }
      const components = fields.schedule.value.split(/[-T:]/).map(Number);
      const sameWallTime = (candidate) => candidate.getFullYear() === components[0] && candidate.getMonth() + 1 === components[1] && candidate.getDate() === components[2] && candidate.getHours() === components[3] && candidate.getMinutes() === components[4];
      if (!sameWallTime(date)) { showError("#publish-error", "This local time does not exist because the clocks change. Choose a time outside the clock change."); return; }
      for (let minutes = 1; minutes <= 180; minutes++) {
        if (sameWallTime(new Date(date.getTime() + minutes * 60000)) || sameWallTime(new Date(date.getTime() - minutes * 60000))) {
          showError("#publish-error", "This local time occurs twice because the clocks change. Choose an unambiguous time outside the clock change.");
          return;
        }
      }
      schedule = date.toISOString();
    }
    const restore = startAction($("#publish-submit"), "Starting upload…");
    try { await api(`/api/clips/${encodeURIComponent(clip.id)}/publish`, {confirmed: fields.confirmed.checked, made_for_kids: fields.made_for_kids.value === "true", contains_synthetic_media: fields.contains_synthetic_media.value === "true", schedule}); $("#publish-dialog").close(); toast("YouTube upload started. Follow the result in the workshop."); await refreshLibrary(); updateReviewState(); scheduleFastPoll(); }
    catch (error) { showError("#publish-error", error); }
    finally { restore(); }
  });
  $$("dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => { if (event.target !== dialog) return; const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); });
    dialog.addEventListener("close", () => { $$("video", dialog).forEach((video) => video.pause()); if (dialog.id === "attach-dialog" && !state.attachPendingUpload) state.attachLinkId = null; });
  });
}


const STORY_GENRES = {mystery: "Mystery", horror: "Horror", suspense: "Suspense", science_fiction: "Science fiction", random: "Random"};
const STORY_STAGES = {story_writing: "Writing your story", story_planning: "Planning scenes", story_narrating: "Recording narration", story_audio: "Mixing music and sound", story_rendering: "Animating your story", story_exporting: "Finishing the video", writing: "Writing your story", narrating: "Recording narration", rendering: "Animating your story"};

function storyFields() { return $("#story-form").elements; }
function storyJob(projectId = storyState.project?.id) {
  return activeJobs().find((job) => ["story", "story_rewrite", "story_scene"].includes(job.kind) && (job.project_id || job.payload?.project_id) === projectId);
}
function storyDirty() {
  storyState.dirty = true;
  $("#story-save-state").textContent = "You have unsaved changes. Generate will save them first.";
}
function storyOptions() {
  const fields = storyFields();
  return {idea: fields.idea.value.trim(), genre: fields.genre.value, duration: Number(fields.duration.value), voice: fields.voice.value, captions: fields.captions.value === "true", music: fields.music.value === "true", quality: Number(fields.quality.value), rate: Number(fields.rate.value)};
}
function storySceneOptions() {
  return $$("[data-story-scene]", $("#story-scenes")).map((scene) => {
    const data = {};
    $$("[data-scene-field]", scene).forEach((field) => { data[field.dataset.sceneField] = field.value; });
    const original = storyState.project?.scenes?.find((item) => String(item.id) === scene.dataset.storyScene);
    return {id: scene.dataset.storyScene, narration: data.narration || "", setting: data.setting, backdrop: data.backdrop || original?.backdrop || "night", action: data.action, prop: data.prop || original?.prop || "none", beats: storySceneBeats(scene, original), camera: data.camera, lighting: data.lighting, effect: data.effect, character_ids: original?.character_ids || []};
  });
}
function renderStoryVoices() {
  const select = $("#story-voice");
  const previous = select.value || storyState.project?.options?.voice || "";
  const voices = Array.isArray(storyState.capabilities.voices) ? storyState.capabilities.voices : [];
  const markup = voices.length ? voices.map((voice) => `<option value="${esc(voice.id)}">${esc(voice.name)}${voice.provider ? ` · ${esc(voice.provider)}` : ""}${voice.language ? ` · ${esc(voice.language)}` : ""}</option>`).join("") : '<option value="">No local voice detected</option>';
  if (select.innerHTML !== markup) select.innerHTML = markup;
  if (voices.some((voice) => voice.id === previous)) select.value = previous;
  else if (previous && storyState.project) {
    const missing = document.createElement("option");
    missing.value = previous;
    missing.textContent = "Saved voice is unavailable · choose another";
    select.append(missing);
    select.value = previous;
  }
}
function renderStoryCapabilities() {
  const cap = storyState.capabilities;
  $("#story-setup").classList.toggle("ready", Boolean(cap.ok));
  $("#story-setup-title").textContent = cap.ok ? "Your local story studio is ready" : "A little setup before your first story";
  $("#story-setup-detail").textContent = cap.message || "Check your installed voices, local Ollama model and video tools.";
  renderStoryVoices();
  renderStoryControls();
}
function renderStoryControls() {
  const active = storyJob();
  const locked = storyState.busy || Boolean(active);
  const cap = storyState.capabilities;
  const renderable = cap.ok || (cap.renderer_ready && cap.voices?.length && storyState.project?.scenes?.length);
  $("#story-generate").disabled = locked || !renderable || !state.connected;
  $("#story-save").disabled = locked || !state.connected;
  $("#story-save-scenes").disabled = locked || !state.connected;
  $("#story-rewrite").disabled = locked || !storyState.project || !storyState.capabilities.ollama_ready;
  $("#story-ideas").disabled = storyState.busy || !storyState.capabilities.ollama_ready || Boolean(storyState.ideasJob && activeJobs().some((job) => job.id === storyState.ideasJob));
  $("#story-random").disabled = locked;
  $$("[data-scene-regenerate]", $("#story-scenes")).forEach((button) => { button.disabled = locked || !storyState.capabilities.ollama_ready; });
  $$("textarea, select", $("#story-form")).forEach((field) => { field.disabled = locked; });
  $$("textarea, select, input", $("#story-scenes")).forEach((field) => { field.disabled = locked; });
  $$("[data-beat-add], [data-beat-remove]", $("#story-scenes")).forEach((button) => { button.disabled = locked; });
  $("#story-cancel").hidden = !active?.can_cancel;
  $("#story-cancel").disabled = Boolean(active?.cancel_requested);
  $("#story-cancel").textContent = active?.cancel_requested ? "Stopping…" : "Cancel generation";
  renderStoryVoiceControls();
}
function renderStoryOutput() {
  const project = storyState.project;
  const job = storyJob();
  const url = project?.export_available === false ? "" : mediaUrl(project?.video_url || (project?.clip_id ? `/media/clip/${project.clip_id}` : ""), "clip");
  const video = $("#story-video");
  if (url) {
    const previewUrl = `${url}#t=0.35`;
    if (video.getAttribute("src") !== previewUrl) video.src = previewUrl;
  } else if (video.getAttribute("src")) {
    video.pause(); video.removeAttribute("src"); video.load();
  }
  video.hidden = !url;
  $("#story-preview-art").hidden = Boolean(url);
  $("#story-download").hidden = !url;
  $("#story-download").href = url ? `${url}${url.includes("?") ? "&" : "?"}download=1` : "";
  $("#story-review").hidden = !url || !project?.clip_id;
  $("#story-output-title").textContent = project?.title || "Dark Stickman Stories";
  $("#story-output-detail").textContent = project?.duration_s ? `${duration(project.duration_s)} · Vertical ${project.options?.quality || 720}p · Dark Stickman Stories` : "Original stories. Consistent characters. A little mystery.";
  const warnings = Array.isArray(project?.warnings) ? project.warnings : [];
  $("#story-warnings").hidden = !warnings.length;
  $("#story-warnings").textContent = warnings.join(" ");
  const progress = $("#story-progress");
  progress.hidden = !job && !project?.error;
  if (job) {
    const percent = job.stage_percent;
    const measured = typeof percent === "number" && Number.isFinite(percent) && percent >= 0 && percent <= 100;
    progress.innerHTML = `<strong>${esc(job.cancel_requested ? "Stopping and saving your project…" : job.state === "queued" ? "Waiting in the local queue" : STORY_STAGES[job.stage] || job.message || "Creating your story…")}</strong><p>${esc(timingLabel(job))}</p><div class="progress-track${measured ? "" : " indeterminate"}" role="progressbar" aria-label="Story generation progress"${measured ? ` aria-valuenow="${Math.floor(percent)}" aria-valuemin="0" aria-valuemax="100"` : ' aria-valuetext="Working; time estimate is not yet available"'}><span${measured ? ` style="width:${percent}%"` : ""}></span></div>`;
  } else progress.textContent = project?.error || "";
  renderStoryControls();
}
function storySelectOptions(values, selected) {
  return values.map((value) => `<option value="${esc(value)}"${value === selected ? " selected" : ""}>${esc(value.replace(/_/g, " "))}</option>`).join("");
}
const STORY_ACTIONS = ["walk", "run", "turn", "look", "point", "reach", "open", "close", "pick_up", "hold", "react", "hide", "crouch", "enter", "exit", "idle"];
const STORY_TARGETS = ["none", "left", "center", "right", "window", "door", "prop", "partner"];
const STORY_EXPRESSIONS = ["neutral", "curious", "worried", "surprised", "determined"];
function storySceneBeats(scene, original) {
  const rows = $$("[data-story-beat]", scene);
  if (!rows.length) return scene.dataset.motionEdited === "true" ? [] : (original?.beats || []);
  return rows.map((row) => {
    const fields = {};
    $$("[data-beat-field]", row).forEach((field) => { fields[field.dataset.beatField] = field.value; });
    return {at: Number(fields.at) / 100, character_id: fields.character_id, action: fields.action, target: fields.target, expression: fields.expression};
  });
}
function storyBeatMarkup(scene, beat = {}) {
  const characters = (storyState.project?.characters || []).filter((character) => scene.character_ids?.includes(character.id));
  return `<div class="story-motion-beat" data-story-beat><label>At %<input data-beat-field="at" type="number" min="0" max="100" step="1" value="${esc(Math.round((Number(beat.at) || 0) * 100))}" required></label><label>Character<select data-beat-field="character_id">${characters.map((character) => `<option value="${esc(character.id)}"${character.id === (beat.character_id || scene.character_ids?.[0]) ? " selected" : ""}>${esc(character.name || character.id)}</option>`).join("")}</select></label><label>Action<select data-beat-field="action">${storySelectOptions(STORY_ACTIONS, beat.action || "look")}</select></label><label>Target<select data-beat-field="target">${storySelectOptions(STORY_TARGETS, beat.target || "none")}</select></label><label>Expression<select data-beat-field="expression">${storySelectOptions(STORY_EXPRESSIONS, beat.expression || "neutral")}</select></label><button class="icon-button" type="button" data-beat-remove aria-label="Remove motion beat">${icon("trash")}</button></div>`;
}
function renderStoryEditor() {
  const project = storyState.project;
  const scenes = Array.isArray(project?.scenes) ? project.scenes : [];
  $("#story-editor").hidden = !scenes.length;
  $("#story-characters").innerHTML = (project?.characters || []).map((character) => `<span class="badge">${icon("cpu")} ${esc(character.name || character.id)}${character.accessory && character.accessory !== "none" ? ` · ${esc(character.accessory)}` : ""}</span>`).join("");
  $("#story-scenes").innerHTML = scenes.map((scene, i) => `<article class="story-scene" data-story-scene="${esc(scene.id)}"><div class="story-scene-heading"><h3><span>${String(i + 1).padStart(2, "0")}</span> Scene ${i + 1}</h3><button class="text-button" type="button" data-scene-regenerate="${esc(scene.id)}">${icon("refresh")} Regenerate scene</button></div><label>Narration<textarea data-scene-field="narration" maxlength="1200" rows="3" required>${esc(scene.narration)}</textarea></label><div class="story-scene-options"><label>Setting<select data-scene-field="setting">${storySelectOptions(["bedroom", "hallway", "street", "school", "forest", "rooftop"], scene.setting)}</select></label><label>View / backdrop<select data-scene-field="backdrop">${storySelectOptions(["night", "forest", "city", "desert", "otherworld"], scene.backdrop || "night")}</select></label><label>Main action<select data-scene-field="action">${storySelectOptions(STORY_ACTIONS, scene.action)}</select></label><label>Scene object<select data-scene-field="prop">${storySelectOptions(["none", "phone", "key", "book", "flashlight"], scene.prop || "none")}</select></label><label>Camera<select data-scene-field="camera">${storySelectOptions(["pan", "zoom", "static"], scene.camera)}</select></label><label>Lighting<select data-scene-field="lighting">${storySelectOptions(["normal", "nightvision", "flicker"], scene.lighting)}</select></label><label>Effect<select data-scene-field="effect">${storySelectOptions(["none", "shake", "security"], scene.effect)}</select></label></div><details class="story-motion-editor"><summary>Character motion &amp; expressions</summary><p class="field-hint">Characters move smoothly between these moments. With no custom beats, ClipForge plans motion from the scene. Changing Main action resets custom beats for this scene.</p><div data-story-beats>${(scene.beats || []).map((beat) => storyBeatMarkup(scene, beat)).join("")}</div><button class="text-button" type="button" data-beat-add="${esc(scene.id)}">${icon("plus")} Add motion beat</button><span class="field-hint">Up to 8 moments · 0% is the scene start, 100% is its end.</span></details></article>`).join("");
}
function fillStoryProject(project) {
  storyState.project = project;
  const options = project?.options || {};
  const fields = storyFields();
  fields.idea.value = options.idea || "";
  fields.genre.value = options.genre || "mystery";
  fields.duration.value = String(options.duration || 60);
  fields.quality.value = String(options.quality || 720);
  fields.captions.value = String(options.captions !== false);
  fields.music.value = String(options.music !== false);
  fields.rate.value = String(options.rate || 0);
  fields.batch_count.value = "1";
  renderStoryVoices();
  if (options.voice) fields.voice.value = options.voice;
  storyState.dirty = false;
  $("#story-save-state").textContent = project ? "Saved locally. Your finished exports are kept as you edit." : "Your project and finished video stay on this computer.";
  clearError("#story-error");
  renderStoryEditor(); renderStoryOutput();
}
function renderStoryHistory() {
  $("#story-count").textContent = storyState.projects.length;
  $("#story-history").innerHTML = storyState.projects.length ? storyState.projects.map((project) => `<article class="story-history-card${project.id === storyState.project?.id ? " selected" : ""}"><span class="story-history-icon">${icon("film")}</span><div><h3>${esc(project.title || "Untitled story")}</h3><p>${esc(STORY_GENRES[project.options?.genre || project.genre] || "Dark Stickman Stories")} · ${esc(project.state || "draft")}${project.duration_s ? ` · ${esc(duration(project.duration_s))}` : ""}</p></div><button class="button subtle small" type="button" data-story-open="${esc(project.id)}">Open project ↗</button></article>`).join("") : emptyState("sparkles", "Your first story starts above.", "Save an idea or generate a video to begin your local project history.");
}
function renderStoryIdeas() {
  if (!storyState.ideasJob) return;
  const job = state.jobs.find((item) => item.id === storyState.ideasJob);
  if (!job) return;
  const target = $("#story-idea-results");
  target.hidden = false;
  const result = job.result?.ideas || job.result;
  if (job.state === "completed" && Array.isArray(result)) {
    target.innerHTML = `<strong>Pick an idea to make it yours</strong>${result.slice(0, 5).map((idea, i) => `<button type="button" class="story-idea-option" data-story-idea="${i}">${esc(typeof idea === "string" ? idea : idea.idea || idea.title || "")}</button>`).join("")}`;
  } else target.textContent = ["failed", "cancelled", "interrupted"].includes(job.state) ? (job.message || "Idea generation stopped. You can try again.") : "Your local AI is thinking of five new ideas…";
}
function storyVoiceJob(kind, id) {
  return activeJobs().find((job) => job.kind === kind && (kind === "story_voice_install" ? job.voice_catalog_id === id : job.voice_id === id));
}
function renderStoryVoiceControls() {
  const voice = storyFields().voice.value;
  const installed = storyState.capabilities.voices || [];
  $("#story-preview-voice").disabled = !state.connected || !voice || !installed.some((item) => item.id === voice) || Boolean(storyVoiceState.pendingVoice) || Boolean(storyVoiceJob("story_voice_preview", voice));
  $$("[data-voice-use]").forEach((button) => { button.disabled = storyState.busy || Boolean(storyJob()); });
}
function renderStoryVoicePreview() {
  const job = state.jobs.find((item) => item.id === storyVoiceState.previewJob);
  const panel = $("#story-voice-preview");
  const audio = $("#story-voice-audio");
  panel.hidden = !job && !storyVoiceState.pendingVoice;
  const cancel = $("#story-voice-preview-cancel");
  cancel.hidden = !job?.can_cancel || !["queued", "running"].includes(job.state);
  cancel.disabled = Boolean(job?.cancel_requested);
  if (storyVoiceState.pendingVoice) $("#story-voice-preview-status").textContent = "Starting a short local voice preview…";
  if (!job) { renderStoryVoiceControls(); return; }
  const source = mediaUrl(job.voice_preview_url || job.result?.voice_preview_url, "story-voice");
  if (job.state === "completed" && source) {
    if (audio.getAttribute("src") !== source) {
      audio.src = source;
      if (typeof audio.play === "function") { const playing = audio.play(); if (playing?.catch) playing.catch(() => {}); }
    }
    audio.hidden = false;
    $("#story-voice-preview-status").textContent = "Preview ready. This voice runs locally on your computer.";
  } else {
    audio.hidden = true;
    $("#story-voice-preview-status").textContent = ["failed", "cancelled", "interrupted"].includes(job.state) ? (job.message || "Voice preview stopped. Try another voice or retry.") : `${job.cancel_requested ? "Stopping preview…" : job.state === "queued" ? "Voice preview is waiting in the local queue…" : "Recording a short voice preview…"}${timingLabel(job) ? ` ${timingLabel(job)}` : ""}`;
  }
  renderStoryVoiceControls();
}
function renderStoryVoiceLibrary() {
  $("#story-voice-engine").textContent = storyVoiceState.engineMessage || "Installed voices are ready to preview.";
  const providers = {sapi:"Windows speech", piper:"Piper · local neural", espeak:"eSpeak · local speech"};
  $("#story-installed-voices").innerHTML = storyVoiceState.voices.length ? storyVoiceState.voices.map((voice) => `<article class="story-voice-card"><div class="story-voice-card-heading"><h4>${esc(voice.name || voice.id)}</h4><span class="badge green">Installed</span></div><p>${esc(voice.language || "Local voice")} · ${esc(providers[voice.provider] || voice.provider || "Local speech")}</p><div class="story-voice-card-actions"><button class="button subtle small" type="button" data-voice-preview="${esc(voice.id)}"${storyVoiceState.pendingVoice || storyVoiceJob("story_voice_preview", voice.id) ? " disabled" : ""}>${icon("play")} Preview</button><button class="text-button" type="button" data-voice-use="${esc(voice.id)}">Use narrator ↗</button></div></article>`).join("") : emptyState("cpu", "No installed narrator yet.", "Add a local voice below or install a Windows speech voice.");
  $("#story-voice-catalog").innerHTML = storyVoiceState.catalog.length ? storyVoiceState.catalog.map((voice) => {
    const job = storyVoiceJob("story_voice_install", voice.id);
    const pending = Boolean(job || storyVoiceState.pendingInstalls.has(voice.id));
    const license = externalUrl(voice.license_url);
    const source = externalUrl(voice.source_url);
    const dataset = externalUrl(voice.dataset_url);
    const card = externalUrl(voice.model_card_url);
    const failure = !voice.installed && !job ? state.jobs.find((item) => item.kind === "story_voice_install" && item.voice_catalog_id === voice.id && ["failed", "interrupted", "cancelled"].includes(item.state)) : null;
    const progress = job?.stage_percent;
    const measured = typeof progress === "number" && Number.isFinite(progress) && progress >= 0 && progress <= 100;
    return `<article class="story-voice-card"><div class="story-voice-card-heading"><h4>${esc(voice.name || voice.id)}</h4><span class="badge${voice.installed ? " green" : ""}">${voice.installed ? "Installed" : "Available"}</span></div><p>${esc(voice.language || "Voice")} · ${esc(providers[voice.provider] || voice.provider || "Piper")}${voice.download_bytes ? ` · ~${esc(bytes(voice.download_bytes))} download` : ""}</p><p class="story-voice-license">License: ${license ? `<a href="${esc(license)}" target="_blank" rel="noopener noreferrer">${esc(voice.license || "View license")} ↗</a>` : esc(voice.license || "See source terms")}${source ? ` · <a href="${esc(source)}" target="_blank" rel="noopener noreferrer">Source ↗</a>` : ""}${dataset ? ` · <a href="${esc(dataset)}" target="_blank" rel="noopener noreferrer">Dataset terms ↗</a>` : ""}${card ? ` · <a href="${esc(card)}" target="_blank" rel="noopener noreferrer">Model card ↗</a>` : ""}</p>${voice.attribution ? `<p class="story-voice-attribution">${esc(voice.attribution)}</p>` : ""}${failure ? `<p class="story-voice-failure" role="status">${esc(failure.message || "The voice download stopped. You can download it again.")}</p>` : ""}${job ? `<div class="story-voice-job"><span>${esc(job.cancel_requested ? "Stopping download…" : job.message || "Downloading voice onto this computer…")}</span><p>${esc(timingLabel(job))}</p><div class="progress-track${measured ? "" : " indeterminate"}" role="progressbar" aria-label="Voice download progress"${measured ? ` aria-valuenow="${Math.floor(progress)}" aria-valuemin="0" aria-valuemax="100"` : ' aria-valuetext="Downloading; progress not yet known"'}><span${measured ? ` style="width:${progress}%"` : ""}></span></div></div>` : ""}<div class="story-voice-card-actions">${voice.installed && voice.voice_id ? `<button class="button subtle small" type="button" data-voice-preview="${esc(voice.voice_id)}"${storyVoiceState.pendingVoice ? " disabled" : ""}>${icon("play")} Preview</button><button class="text-button" type="button" data-voice-use="${esc(voice.voice_id)}">Use narrator ↗</button>` : `<button class="button subtle small" type="button" data-voice-install="${esc(voice.id)}"${pending || !storyVoiceState.engineReady ? " disabled" : ""}>${icon(pending ? "spinner" : "download", pending ? "loading-spinner" : "")} ${pending ? "Installing…" : "Download voice"}</button>`}${job?.can_cancel ? `<button class="text-button" type="button" data-voice-job-cancel="${esc(job.id)}"${job.cancel_requested ? " disabled" : ""}>Cancel download</button>` : ""}</div></article>`;
  }).join("") : emptyState("folder", "No catalog voices available.", "Refresh the local voice library or check the setup guide.");
  renderStoryVoicePreview();
}
async function refreshStoryVoiceLibrary(force = false) {
  if (storyVoiceState.loading) return;
  storyVoiceState.loading = true;
  try {
    const data = await api(force ? "/api/story-voices?refresh=1" : "/api/story-voices");
    storyVoiceState.voices = Array.isArray(data.voices) ? data.voices : [];
    storyVoiceState.catalog = Array.isArray(data.catalog) ? data.catalog : [];
    storyVoiceState.engineReady = Boolean(data.engine_ready);
    storyVoiceState.engineMessage = data.engine_message || "";
    storyVoiceState.loaded = true;
    storyState.capabilities.voices = storyVoiceState.voices;
    renderStoryVoices(); renderStoryVoiceLibrary();
    clearError("#story-voice-error");
  } catch (error) { showError("#story-voice-error", error); }
  finally { storyVoiceState.loading = false; }
}
function requestStoryVoiceCancel(jobId) {
  const job = state.jobs.find((item) => item.id === jobId);
  if (!job?.can_cancel || job.cancel_requested) return;
  $("#cancel-job-form").elements.job_id.value = job.id;
  $("#cancel-job-name").textContent = job.title || "Local voice task";
  clearError("#cancel-job-error"); $("#cancel-job-dialog").showModal();
}
async function previewStoryVoice(voice) {
  if (!voice || storyVoiceState.pendingVoice) return;
  storyVoiceState.pendingVoice = voice;
  const audio = $("#story-voice-audio");
  if (typeof audio.pause === "function") audio.pause();
  audio.hidden = true;
  clearError("#story-voice-error"); renderStoryVoicePreview();
  const panel = $("#story-voice-preview");
  if (typeof panel.scrollIntoView === "function") panel.scrollIntoView({behavior:"smooth", block:"center"});
  try {
    const result = await api("/api/story-voices/preview", {voice});
    storyVoiceState.previewJob = (result.job || result).id;
    await refreshLibrary(); scheduleFastPoll();
  } catch (error) { showError("#story-voice-error", error); $("#story-voice-library").open = true; }
  finally { storyVoiceState.pendingVoice = null; renderStoryVoicePreview(); if (storyVoiceState.loaded) renderStoryVoiceLibrary(); }
}
async function installStoryVoice(id) {
  if (storyVoiceState.pendingInstalls.has(id) || storyVoiceJob("story_voice_install", id)) return;
  storyVoiceState.pendingInstalls.add(id); renderStoryVoiceLibrary();
  clearError("#story-voice-error");
  try {
    await api("/api/story-voices/install", {id});
    await refreshLibrary(); scheduleFastPoll();
    toast("Voice download queued. You can follow it here or in the workshop.");
  } catch (error) { showError("#story-voice-error", error); }
  finally { storyVoiceState.pendingInstalls.delete(id); renderStoryVoiceLibrary(); }
}

async function refreshStories() {
  if (storyState.loading) return;
  storyState.loading = true;
  try {
    const data = await api("/api/stories");
    storyState.projects = Array.isArray(data.projects) ? data.projects : [];
    storyState.capabilities = data.capabilities || {};
    renderStoryCapabilities(); renderStoryHistory(); renderStoryIdeas();
    const currentId = storyState.project?.id;
    const latest = storyState.projects.find((project) => project.id === currentId);
    if (latest && !storyState.dirty && !storyState.busy && (latest.updated_at !== storyState.project.updated_at || latest.state !== storyState.project.state)) {
      const detail = await api(`/api/stories/${encodeURIComponent(currentId)}`);
      if (storyState.project?.id === currentId && !storyState.dirty && !storyState.busy) fillStoryProject(detail.project || detail);
    }
    renderStoryOutput();
  } catch (error) {
    $("#story-setup-title").textContent = "Could not check the story studio";
    $("#story-setup-detail").textContent = error.message;
  } finally { storyState.loading = false; }
}
async function openStory(id) {
  if (storyState.dirty && !window.confirm("Open another project and discard unsaved changes?")) return;
  if (storyState.busy) return;
  storyState.busy = true; renderStoryControls();
  try {
    const data = await api(`/api/stories/${encodeURIComponent(id)}`);
    fillStoryProject(data.project || data);
    renderStoryHistory();
    $("#stories-title").scrollIntoView?.({behavior: "smooth", block: "start"});
  } catch (error) { showError("#story-error", error); }
  finally { storyState.busy = false; renderStoryControls(); }
}
async function saveStory() {
  const options = storyOptions();
  if (!options.idea) throw new Error("Describe the story you want to make first.");
  const current = storyState.project;
  const payload = current ? {...options, scenes: storySceneOptions()} : options;
  const data = await api(current ? `/api/stories/${encodeURIComponent(current.id)}/save` : "/api/stories", payload);
  fillStoryProject(data.project || data);
  return storyState.project;
}
async function storyAction(action, button, sceneId = null) {
  if (storyState.busy || storyJob()) return;
  clearError("#story-error");
  if (action !== "save" && !$("#story-form").reportValidity()) return;
  const batchCount = action === "generate" ? Number(storyFields().batch_count.value || 1) : 1;
  const batchOptions = storyOptions();
  if (batchCount > 1 && !storyState.capabilities.ollama_ready) {
    showError("#story-error", "Start your local Ollama model before generating a batch of new stories.");
    return;
  }
  storyState.busy = true;
  const restore = startAction(button, action === "generate" ? "Starting your story…" : "Saving…");
  renderStoryControls();
  try {
    const project = await saveStory();
    if (action !== "save") {
      if (batchCount > 1) {
        const batch = await api("/api/stories/batch", {options: batchOptions, count: batchCount});
        if (batch.projects?.[0]) fillStoryProject(batch.projects[0]);
        toast(`${batchCount} original stories are queued. Each has its own project in Local project history.`);
      } else {
        const suffix = sceneId ? `scenes/${encodeURIComponent(sceneId)}/regenerate` : action;
        await api(`/api/stories/${encodeURIComponent(project.id)}/${suffix}`, {});
        toast(action === "generate" ? "Your story is in the local queue. You can follow it here." : sceneId ? "Rewriting this scene with your local AI." : "Rewriting your script with your local AI.");
      }
      await refreshLibrary(); scheduleFastPoll();
    } else toast("Story project saved on this computer.");
    await refreshStories();
  } catch (error) { showError("#story-error", error); }
  finally { restore(); storyState.busy = false; renderStoryControls(); renderStoryOutput(); }
}
function bindStoryEvents() {
  const form = $("#story-form");
  form.addEventListener("input", storyDirty);
  form.addEventListener("change", storyDirty);
  form.addEventListener("submit", (event) => { event.preventDefault(); storyAction("generate", $("#story-generate")); });
  $("#story-save").addEventListener("click", () => storyAction("save", $("#story-save")));
  $("#story-save-scenes").addEventListener("click", () => storyAction("save", $("#story-save-scenes")));
  $("#story-rewrite").addEventListener("click", () => storyAction("rewrite", $("#story-rewrite")));
  $("#story-scenes").addEventListener("input", storyDirty);
  $("#story-scenes").addEventListener("change", (event) => {
    if (event.target.dataset.sceneField === "action") {
      const scene = event.target.closest("[data-story-scene]");
      if (scene) { $("[data-story-beats]", scene).innerHTML = ""; scene.dataset.motionEdited = "true"; }
    }
    storyDirty();
  });
  $("#story-scenes").addEventListener("click", (event) => {
    const button = event.target.closest("[data-scene-regenerate]");
    if (button) storyAction("regenerate", button, button.dataset.sceneRegenerate);
    const add = event.target.closest("[data-beat-add]");
    const remove = event.target.closest("[data-beat-remove]");
    if ((add || remove) && (storyState.busy || storyJob())) return;
    if (add) {
      const container = add.closest("[data-story-scene]");
      const scene = storyState.project?.scenes?.find((item) => item.id === container.dataset.storyScene);
      const rows = $$("[data-story-beat]", container);
      if (!scene || rows.length >= 8) { toast("Use up to eight motion beats in each scene.", true); return; }
      $("[data-story-beats]", container).insertAdjacentHTML("beforeend", storyBeatMarkup(scene, {at: Math.min(.95, rows.length * .15)}));
      container.dataset.motionEdited = "true"; storyDirty();
    }
    if (remove) { const container = remove.closest("[data-story-scene]"); remove.closest("[data-story-beat]").remove(); container.dataset.motionEdited = "true"; storyDirty(); }
  });
  $("#story-new").addEventListener("click", () => {
    if (storyState.busy || (storyState.dirty && !window.confirm("Start a new project and discard unsaved changes?"))) return;
    fillStoryProject(null); renderStoryHistory(); $("#story-idea").focus();
  });
  $("#story-check-setup").addEventListener("click", async () => { await refreshHealth(); await refreshStories(); });
  $("#story-preview-voice").addEventListener("click", () => previewStoryVoice(storyFields().voice.value));
  $("#story-voice").addEventListener("change", renderStoryVoiceControls);
  $("#story-manage-voices").addEventListener("click", async () => {
    const library = $("#story-voice-library"); library.open = true;
    if (typeof library.scrollIntoView === "function") library.scrollIntoView({behavior:"smooth", block:"start"});
    await refreshStoryVoiceLibrary();
  });
  $("#story-refresh-voices").addEventListener("click", () => refreshStoryVoiceLibrary(true));
  $("#story-voice-library").addEventListener("toggle", () => { if ($("#story-voice-library").open && !storyVoiceState.loaded) refreshStoryVoiceLibrary(); });
  $("#story-voice-library").addEventListener("click", (event) => {
    const install = event.target.closest("[data-voice-install]");
    const preview = event.target.closest("[data-voice-preview]");
    const use = event.target.closest("[data-voice-use]");
    const cancel = event.target.closest("[data-voice-job-cancel]");
    if (install) installStoryVoice(install.dataset.voiceInstall);
    if (preview) previewStoryVoice(preview.dataset.voicePreview);
    if (cancel) requestStoryVoiceCancel(cancel.dataset.voiceJobCancel);
    if (use && !storyState.busy && !storyJob()) {
      storyFields().voice.value = use.dataset.voiceUse; storyDirty(); renderStoryVoiceControls();
      toast("Narrator selected for this story.");
    }
  });
  $("#story-voice-preview-cancel").addEventListener("click", () => requestStoryVoiceCancel(storyVoiceState.previewJob));
  $("#story-history").addEventListener("click", (event) => {
    const button = event.target.closest("[data-story-open]");
    if (button) openStory(button.dataset.storyOpen);
  });
  $("#story-random").addEventListener("click", () => {
    const ideas = ["A boy notices that his bedroom window shows a different world every night.", "Every night, the hallway outside her apartment grows one door longer.", "A school security camera records the same student arriving thirty years before he was born.", "A lonely radio operator receives weather reports from tomorrow.", "A child finds a shadow that moves before its owner does.", "The last train arrives with everyone wearing the same watch, stopped at midnight."];
    storyFields().idea.value = ideas[Math.floor(Math.random() * ideas.length)]; storyDirty();
  });
  $("#story-ideas").addEventListener("click", async () => {
    const restore = startAction($("#story-ideas"), "Thinking…");
    clearError("#story-error");
    try {
      const data = await api("/api/stories/ideas", {genre: storyFields().genre.value, idea: storyFields().idea.value.trim()});
      storyState.ideasJob = (data.job || data).id;
      await refreshLibrary(); renderStoryIdeas(); scheduleFastPoll();
    } catch (error) { showError("#story-error", error); }
    finally { restore(); renderStoryControls(); }
  });
  $("#story-idea-results").addEventListener("click", (event) => {
    const button = event.target.closest("[data-story-idea]");
    if (!button) return;
    const job = state.jobs.find((item) => item.id === storyState.ideasJob);
    const ideas = job?.result?.ideas || job?.result || [];
    const idea = ideas[Number(button.dataset.storyIdea)];
    if (!idea) return;
    storyFields().idea.value = typeof idea === "string" ? idea : idea.idea || idea.title || "";
    storyDirty();
  });
  $("#story-cancel").addEventListener("click", () => {
    const job = storyJob();
    if (!job?.can_cancel || job.cancel_requested) return;
    $("#cancel-job-form").elements.job_id.value = job.id;
    $("#cancel-job-name").textContent = job.title || "Story generation";
    clearError("#cancel-job-error"); $("#cancel-job-dialog").showModal();
  });
  $("#story-review").addEventListener("click", async () => {
    const clipId = storyState.project?.clip_id;
    if (!clipId) return;
    await refreshLibrary(); openReview(clipId);
  });
  window.addEventListener("beforeunload", (event) => {
    if (storyState.dirty) { event.preventDefault(); event.returnValue = ""; }
  });
}

async function init() {
  paintIcons();
  bindEvents();
  bindStoryEvents();
  navigate();
  renderSources();
  renderJobs();
  renderClips();
  renderLinks();
  try {
    const data = await api("/api/bootstrap");
    state.token = data.token;
    state.settings = data.settings || {};
    state.capabilities = data.capabilities || {};
    state.connected = true;
    if (data.version) $("#version").textContent = `v${String(data.version).replace(/^v/, "")}`;
    fillSettings();
    renderHealth();
    applyLibrary(data);
    if (location.hash === "#stories") await refreshStories();
    pollTimer = setTimeout(poll, activeJobs().length ? 2000 : 8000);
  } catch (error) {
    $("#connection-banner").hidden = false;
    $("#connection-banner").textContent = `Could not open your local workspace. ${error.message} Refresh this page after checking the ClipForge terminal.`;
    $("#setup-summary").textContent = "Workspace connection needed";
    $("#setup-detail").textContent = "Keep the local server running, then refresh this page.";
    $("#choose-video").disabled = true;
    $("#try-demo").disabled = true;
    $("#import-path").disabled = true;
    $("#paste-link").disabled = true;
    toast(error.message, true);
  }
}

init();
