/* Run with: node tests/studio_ui_checks.js
 * These checks exercise real UI decisions without a browser or live API calls.
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "clipforge/web_static/app.js"), "utf8");
const html = fs.readFileSync(path.join(root, "clipforge/web_static/index.html"), "utf8");
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
assert.equal(ids.length, new Set(ids).size, "HTML IDs must be unique");
for (const match of source.matchAll(/\$\("#([a-zA-Z][a-zA-Z0-9_-]*)"/g)) {
  assert(ids.includes(match[1]), `UI references missing element #${match[1]}`);
}

function element() {
  const fields = new Map();
  return {
    value: "", textContent: "", innerHTML: "", style: {}, hidden: false,
    disabled: false, checked: false, open: false, attributes: new Map(), listeners: new Map(),
    elements: new Proxy({}, {get(_target, key) { if (!fields.has(key)) fields.set(key, element()); return fields.get(key); }}),
    addEventListener(type, callback) { if (!this.listeners.has(type)) this.listeners.set(type, []); this.listeners.get(type).push(callback); },
    append() {}, remove() {}, focus() {}, pause() { this.paused = true; }, load() { this.loaded = true; },
    querySelectorAll() { return []; }, querySelector() { return element(); },
    showModal() { this.open = true; }, close() { this.open = false; }, reset() { this.resetCount = (this.resetCount || 0) + 1; },
    setAttribute(key, value) { this.attributes.set(key, value); }, removeAttribute(key) { this.attributes.delete(key); },
  };
}

const nodes = new Map();
function node(selector) { if (!nodes.has(selector)) nodes.set(selector, element()); return nodes.get(selector); }
const context = vm.createContext({
  document: {querySelector: node, querySelectorAll: () => [], createElement: element},
  window: {addEventListener() {}},
  location: {origin: "http://localhost:8000", hash: "studio"},
  URL, Number, String, Math, Intl,
  setTimeout() {}, clearTimeout() {},
  fetch() { throw new Error("UI checks must never make live requests"); },
});
vm.runInContext(source.replace(/\ninit\(\);\s*$/, ""), context, {filename: "app.js"});
function run(code) { return vm.runInContext(code, context); }

function checkTiming() {
  assert.equal(run('timingLabel({state:"running",elapsed_s:90,eta_seconds:null})'), "1:30 elapsed · Estimating time…");
  for (const eta of ["NaN", "Infinity", "-2", '"180"']) {
    assert.match(run(`timingLabel({state:"running",eta_seconds:${eta}})`), /Estimating time/);
  }
  assert.match(run('timingLabel({state:"running",eta_seconds:180,eta_min_seconds:120,eta_max_seconds:240})'), /~2 min–4 min left in this step/);
  assert.match(run('timingLabel({state:"running",eta_seconds:180,estimate_source:"history"})'), /previous jobs/);
  assert.match(run('timingLabel({state:"running",timing_status:"stalled",eta_seconds:180})'), /estimate paused/);
  assert.match(run('timingLabel({state:"running",timing_status:"longer_than_expected"})'), /Taking longer/);
  assert.match(run('timingLabel({state:"running",cancel_requested:true})'), /Stopping and cleaning up/);
  assert.equal(run('timingLabel({state:"completed",elapsed_s:90,eta_seconds:180})'), "1:30 elapsed");
  assert.match(run('timingLabel({state:"queued",eta_seconds:180})'), /when this job begins/);
}

function checkCollection() {
  node("#clip-filter").value = "all";
  node("#clip-sort").value = "title";
  node("#clip-search").value = "";
  run('state.clips=[{id:"2",title:"Zoo",can_delete:false,delete_message:"Upload active"},{id:"1",title:"Apple <title>",state:"PREPARED"}];renderClips()');
  let cards = node("#clip-grid").innerHTML;
  assert(cards.indexOf("Apple &lt;title&gt;") < cards.indexOf("Zoo"));
  assert.match(cards, /data-clip-delete="2"[^>]*disabled/);
  assert.match(cards, /Upload to YouTube/);
  node("#clip-search").value = "ZOO";
  run("renderClips()");
  cards = node("#clip-grid").innerHTML;
  assert(!cards.includes("Apple"));
  assert(cards.includes("Zoo"));
  assert.equal(node("#clips-summary").textContent, "1 of 2 clips shown");
}

function checkProgress() {
  run('state.jobs=[{id:"j",state:"running",mode:"ai",stage:"rendering",can_cancel:true,stage_percent:0,eta_seconds:null}];renderJobs()');
  assert.match(node("#job-list").innerHTML, /aria-valuenow="0"/);
  assert.match(node("#job-list").innerHTML, /data-job-cancel="j"/);
  for (const percent of ["null", "NaN", "Infinity", "-1", "101"]) {
    run(`state.jobs=[{id:"j",state:"running",mode:"ai",stage:"rendering",stage_percent:${percent},progress:65}];renderJobs()`);
    assert.match(node("#job-list").innerHTML, /progress-track indeterminate/);
    assert(!node("#job-list").innerHTML.includes("aria-valuenow="));
  }
  run('state.jobs=[{id:"j",state:"running",mode:"link_import",stage:"downloading",download_bytes:200,download_total_bytes:100}];renderJobs()');
  assert.match(node("#job-list").innerHTML, /progress-track indeterminate/);
  run('state.jobs=[{id:"j",state:"cancelled",mode:"ai",can_delete:true,cancel_requested:true}];renderJobs()');
  assert(!node("#job-list").innerHTML.includes("data-job-cancel="), "Terminal jobs must not keep a stale stopping button");
  assert.match(node("#job-list").innerHTML, /data-retry="j"/);
  assert.match(node("#job-list").innerHTML, /data-job-delete="j"/);
}

function checkReviewAndYouTube() {
  run('state.settings={upload:{enabled:true,target_channel_id:"my-channel",privacy_status:"private"}};state.capabilities={youtube:{ok:true}};state.clips=[{id:"approved",title:"Ready clip",state:"PREPARED",video_url:"/media/clip/approved"},{id:"review",title:"Unreviewed clip",state:"REVIEW_REQUIRED"}];state.clipId=null;state.metadataDirty=false');
  assert.equal(run('openReview("approved")'), true);
  node("#metadata-form").elements.title.value = "Unsaved title";
  run("state.metadataDirty=true");
  assert.equal(run('openReview("approved")'), true);
  assert.equal(node("#metadata-form").elements.title.value, "Unsaved title", "Reopening current review must preserve unsaved details");
  run("openPublish()");
  assert.equal(node("#publish-dialog").open, false, "Dirty details must never open the upload dialog");
  assert.equal(run('openReview("review")'), false, "Opening another clip must not discard dirty metadata");
  run("state.metadataDirty=false;openPublish()");
  assert.equal(node("#publish-dialog").open, true);
  assert.match(node("#publish-destination").textContent, /my-channel/);
  node("#publish-dialog").close();
  node("#review-dialog").close();
  run('openReview("review");openPublish()');
  assert.equal(node("#publish-dialog").open, false, "Unreviewed clips cannot open upload form");
  node("#review-dialog").close();
  run('state.capabilities.youtube.ok=false;openReview("approved");openPublish()');
  assert.equal(node("#publish-dialog").open, false, "Missing YouTube setup cannot open upload form");
  assert.equal(node("#setup-youtube").hidden, false);
}

async function checkDeletion() {
  run("bindEvents()");
  run('state.clips=[{id:"local",title:"Local only",state:"PREPARED",can_delete:true,video_url:"/media/clip/local"}];state.sources=[];state.clipId="local";state.metadataDirty=false');
  run('openDeleteClip("local")');
  assert.equal(node("#delete-clip-dialog").open, true, "Deleting a finished clip must not require its source");
  const calls = [];
  context.stubApi = async (endpoint, body) => { calls.push({endpoint, body}); return {deleted: "local", bytes_freed: 1024}; };
  run("api=stubApi;refreshLibrary=async()=>{}");
  const submit = node("#delete-clip-form").listeners.get("submit")[0];
  await submit({preventDefault() {}, currentTarget: node("#delete-clip-form")});
  assert.equal(calls[0].endpoint, "/api/clips/local/delete");
  assert.equal(JSON.stringify(calls[0].body), '{"confirmed":true}');
  assert.equal(node("#delete-clip-dialog").open, false);
  assert.equal(node("#review-dialog").open, false);
  assert.equal(run("state.clipId"), null);
  assert.equal(node("#clip-video").paused, true);
}

(async () => {
  checkTiming();
  checkCollection();
  checkProgress();
  checkReviewAndYouTube();
  await checkDeletion();
  console.log("Studio UI checks passed: timing, measured progress, cancellation, search/sort, review gates and source-independent clip deletion.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
