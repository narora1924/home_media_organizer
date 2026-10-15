const inputDir = document.querySelector("#input-dir");
const outputDir = document.querySelector("#output-dir");
const toolStatus = document.querySelector("#tool-status");
const modal = document.querySelector("#modal");
const modalList = document.querySelector("#modal-list");
const modalCount = document.querySelector("#modal-count");

const state = {
  tools: null,
  scan: null,
  resolve: null,
  compare: null,
  plan: null,
  deletedCount: 0,
};

document.querySelector("#folder-form").addEventListener("submit", (event) => {
  event.preventDefault();
  startScan();
});

document.querySelectorAll("[data-browse]").forEach((button) => {
  button.addEventListener("click", () => browse(button.dataset.browse));
});

document.querySelector("#mark-ignore").addEventListener("click", () => markExtras("ignore"));
document.querySelector("#mark-delete").addEventListener("click", () => markExtras("delete"));
document.querySelector("#dup-filter").addEventListener("input", filterDuplicates);
document.querySelector("#apply-duplicates").addEventListener("click", () => submitDuplicates(false));
document.querySelector("#modal-cancel").addEventListener("click", closeModal);
document.querySelector("#modal-confirm").addEventListener("click", () => {
  closeModal();
  submitDuplicates(true);
});
document.querySelector("#continue-plan").addEventListener("click", buildPlan);
document.querySelector("#start-run").addEventListener("click", startRun);
document.querySelector("#ignore-filter").addEventListener("input", () => filterCards("#ignore-list", "#ignore-filter"));
document.querySelector("#plan-filter").addEventListener("input", () => filterCards("#plan-list", "#plan-filter"));

for (const id of ["#back-folders", "#back-from-compare", "#back-from-plan", "#finish-over"]) {
  document.querySelector(id).addEventListener("click", startOver);
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeModal();
});

loadTools();

function showStep(number) {
  for (let index = 1; index <= 5; index += 1) {
    document.querySelector(`#step-${index}`).hidden = index !== number;
    const tab = document.querySelector(`.steps li[data-step="${index}"]`);
    tab.classList.toggle("active", index === number);
    tab.classList.toggle("done", index < number);
  }
}

function startOver() {
  if (state.deletedCount > 0) {
    const leave = window.confirm("Some input files were already deleted. Start over from the folder step?");
    if (!leave) return;
  }
  state.scan = null;
  state.resolve = null;
  state.compare = null;
  state.plan = null;
  state.deletedCount = 0;
  clearError(1);
  showStep(1);
}

async function loadTools() {
  try {
    state.tools = await getJson("/api/tools");
    const ffmpeg = state.tools.ffmpeg_found ? `ffmpeg: ${state.tools.ffmpeg}` : "ffmpeg was not found on PATH";
    const ffprobe = state.tools.ffprobe_found ? `ffprobe: ${state.tools.ffprobe}` : "ffprobe was not found on PATH";
    toolStatus.textContent = `${ffmpeg}. ${ffprobe}.`;
  } catch (error) {
    toolStatus.textContent = error.message;
  }
}

async function browse(kind) {
  clearError(1);
  const initial = kind === "input" ? inputDir.value : outputDir.value;
  try {
    const data = await postJson("/api/browse", { kind, initial });
    if (!data.path) return;
    if (kind === "input") inputDir.value = data.path;
    else outputDir.value = data.path;
  } catch (error) {
    showError(1, error.message);
  }
}

async function startScan() {
  clearError(1);
  const button = document.querySelector("#scan-button");
  button.disabled = true;
  const progress = document.querySelector("#scan-progress");
  progress.hidden = false;
  renderProgress(progress, { done: 0, total: 0, message: "Counting every file by type, then checking photos and videos for duplicates. Other files are listed and not copied." });
  try {
    const started = await postJson("/api/scan", { input_dir: inputDir.value.trim() });
    state.scan = await poll(started.job_id, (job) => renderProgress(progress, job));
    state.deletedCount = 0;
    renderDuplicates();
    showStep(2);
  } catch (error) {
    showError(1, error.message);
  } finally {
    button.disabled = false;
    progress.hidden = true;
  }
}

function renderDuplicates() {
  const scan = state.scan;
  const list = document.querySelector("#dup-list");
  if (!scan.groups.length) {
    list.innerHTML = `<div class="file"><strong>No duplicate media files.</strong><span class="meta">Each photo and video has different contents.</span></div>`;
  } else {
    list.innerHTML = scan.groups.map((group) => groupHtml(group)).join("");
    list.querySelectorAll('input[type="radio"]').forEach((radio) => {
      radio.addEventListener("change", updateDuplicateSummary);
    });
    list.querySelectorAll("select").forEach((select) => {
      select.addEventListener("change", updateDuplicateSummary);
    });
  }
  document.querySelector("#other-list").innerHTML = renderOtherFiles(scan.other_files || []);
  const errors = document.querySelector("#scan-errors");
  if (scan.errors && scan.errors.length) {
    errors.hidden = false;
    errors.innerHTML = `<strong>${scan.errors.length} files could not be read.</strong><div>${scan.errors.map((item) => `<div class="path">${esc(item.path)} — ${esc(item.error)}</div>`).join("")}</div>`;
  } else {
    errors.hidden = true;
    errors.innerHTML = "";
  }
  document.querySelector("#dup-filter").value = "";
  updateDuplicateSummary();
}

function groupHtml(group) {
  const rows = group.files.map((file, index) => {
    const keep = index === 0 ? "checked" : "";
    return `<li data-path="${esc(file.path)}">
      <label><input type="radio" name="keep-${group.hash}" value="${esc(file.path)}" ${keep}> Keep</label>
      <select aria-label="What to do with this extra copy">
        <option value="ignore">Ignore</option>
        <option value="delete">Delete</option>
      </select>
      <code>${esc(file.path)}</code>
    </li>`;
  }).join("");
  return `<article class="group">
    <header>
      <h3>${group.count} copies</h3>
      <p>${formatBytes(group.size)} · ${group.hash.slice(0, 12)}</p>
    </header>
    <ul class="paths">${rows}</ul>
  </article>`;
}

function updateDuplicateSummary() {
  const scan = state.scan;
  const decisions = collectDecisions();
  const deletes = decisions.filter((item) => item.action === "delete").length;
  const ignores = decisions.filter((item) => item.action === "ignore").length;
  document.querySelector("#dup-summary").innerHTML = [
    countLine(scan.type_counts || [], scan.file_count),
    pill(`${scan.media_count || 0} media files checked for duplicates`),
    pill(`${scan.other_count || 0} not media, shown below`),
    pill(`${scan.duplicate_group_count} duplicate groups`),
    pill(`${scan.extra_copy_count} extra copies`),
    pill(`${ignores} extras will be ignored`),
    pill(`${deletes} extras marked for deletion`),
  ].join("");
}

function markExtras(action) {
  document.querySelectorAll("#dup-list select").forEach((select) => {
    select.value = action;
  });
  updateDuplicateSummary();
}

function filterDuplicates() {
  const query = document.querySelector("#dup-filter").value.trim().toLowerCase();
  let shown = 0;
  const groups = document.querySelectorAll("#dup-list .group");
  groups.forEach((group) => {
    const match = !query || group.textContent.toLowerCase().includes(query);
    group.hidden = !match;
    if (match) shown += 1;
  });
  const note = document.querySelector("#dup-filter-note");
  note.textContent = query ? `Showing ${shown} of ${groups.length} groups` : "";
}

function collectDecisions() {
  const decisions = [];
  document.querySelectorAll("#dup-list .group").forEach((group) => {
    const selected = group.querySelector('input[type="radio"]:checked');
    group.querySelectorAll("li").forEach((row) => {
      const path = row.dataset.path;
      if (selected && path === selected.value) decisions.push({ path, action: "keep" });
      else decisions.push({ path, action: row.querySelector("select").value });
    });
  });
  return decisions;
}

async function submitDuplicates(confirmDelete) {
  clearError(2);
  const decisions = collectDecisions();
  const pendingDeletes = decisions.filter((item) => item.action === "delete");
  if (pendingDeletes.length && !confirmDelete) {
    openDeleteModal(pendingDeletes);
    return;
  }
  const button = document.querySelector("#apply-duplicates");
  button.disabled = true;
  const progress = document.querySelector("#dup-progress");
  progress.hidden = false;
  renderProgress(progress, { done: 0, total: 0, message: "Saving your duplicate choices, then comparing kept media files with the output folder" });
  try {
    const result = await postJson("/api/duplicates/resolve", {
      decisions,
      confirm_delete: confirmDelete,
    });
    if (result.needs_confirm) {
      openDeleteModal(result.delete_paths.map((path) => ({ path })));
      return;
    }
    state.resolve = result;
    state.deletedCount = result.deleted_count || 0;
    progress.hidden = true;
    await startCompare();
  } catch (error) {
    showError(2, error.message);
  } finally {
    button.disabled = false;
    progress.hidden = true;
  }
}

function openDeleteModal(items) {
  modalCount.textContent = `${items.length} ${items.length === 1 ? "file" : "files"} will be deleted from the input folder. This cannot be undone.`;
  modalList.innerHTML = items.map((item) => `<div class="file"><code>${esc(item.path)}</code></div>`).join("");
  modal.hidden = false;
}

function closeModal() {
  modal.hidden = true;
}

async function startCompare() {
  clearError(3);
  showStep(3);
  const progress = document.querySelector("#compare-progress");
  progress.hidden = false;
  renderProgress(progress, { done: 0, total: 0, message: "Comparing kept media files with the output folder. Files already saved there will be skipped." });
  document.querySelector("#continue-plan").disabled = true;
  try {
    const started = await postJson("/api/compare", { output_dir: outputDir.value.trim() });
    state.compare = await poll(started.job_id, (job) => renderProgress(progress, job));
    renderCompare();
  } catch (error) {
    showError(3, error.message);
  } finally {
    progress.hidden = true;
    document.querySelector("#continue-plan").disabled = false;
  }
}

function renderCompare() {
  const compare = state.compare;
  const resolve = state.resolve || {};
  document.querySelector("#compare-summary").innerHTML = [
    countLine(compare.type_counts, compare.file_count, "media files in this step"),
    countLine(compare.ignored_type_counts, compare.ignored_count, "already saved"),
    countLine(compare.remaining_type_counts, compare.remaining_count, "still to place"),
    pill(`${compare.output_file_count} files already in the output folder`),
  ].join("");
  const notes = [];
  if (resolve.ignored_count) {
    const copies = resolve.ignored_count === 1 ? "copy was" : "copies were";
    notes.push(`${resolve.ignored_count} duplicate ${copies} ignored and left in the input folder.`);
  }
  if (resolve.deleted_count) {
    const copies = resolve.deleted_count === 1 ? "copy was" : "copies were";
    notes.push(`${resolve.deleted_count} duplicate ${copies} deleted.`);
  }
  if (resolve.delete_errors && resolve.delete_errors.length) {
    notes.push(`${resolve.delete_errors.length} deletes failed.`);
  }
  document.querySelector("#resolve-note").textContent = notes.join(" ");
  const list = document.querySelector("#ignore-list");
  if (!compare.ignored.length) {
    list.innerHTML = `<div class="file"><strong>No input files were already in the output folder.</strong></div>`;
  } else {
    list.innerHTML = compare.ignored.map((file) => fileRow(file.path, `${formatBytes(file.size)} · ${file.sha256.slice(0, 12)}`)).join("");
  }
  const errors = document.querySelector("#compare-errors");
  const problems = [...(compare.errors || []), ...(resolve.delete_errors || [])];
  if (problems.length) {
    errors.hidden = false;
    errors.innerHTML = problems.map((item) => `<div class="path">${esc(item.path)} — ${esc(item.error)}</div>`).join("");
  } else {
    errors.hidden = true;
  }
  document.querySelector("#ignore-filter").value = "";
}

async function buildPlan() {
  clearError(4);
  showStep(4);
  const progress = document.querySelector("#plan-progress");
  progress.hidden = false;
  renderProgress(progress, { done: 0, total: 0, message: "Reading the date in each media file and choosing its year and month folder. Nothing is copied yet." });
  document.querySelector("#start-run").disabled = true;
  try {
    const started = await postJson("/api/plan");
    state.plan = await poll(started.job_id, (job) => renderProgress(progress, job));
    renderPlan();
  } catch (error) {
    showError(4, error.message);
  } finally {
    progress.hidden = true;
  }
}

function renderPlan() {
  const plan = state.plan;
  document.querySelector("#plan-summary").innerHTML = [
    countLine(plan.type_counts, plan.file_count, "files will be written"),
    actionLine(plan.action_counts),
    pill(`${plan.skipped_count} skipped`),
  ].join("");
  const list = document.querySelector("#plan-list");
  if (!plan.items.length) {
    list.innerHTML = `<div class="file"><strong>Nothing to copy.</strong><span class="meta">Every supported file was a duplicate, already organized, or unsupported.</span></div>`;
  } else {
    list.innerHTML = plan.items.map((item) => fileRow(
      item.source,
      `${item.action_label} · ${dateSourceLabel(item.date_source)} ${item.date.replace("T", " ")} · ${item.destination}`,
    )).join("");
  }
  document.querySelector("#skip-summary").innerHTML = pill(`${plan.skipped_count} skipped`);
  const skipped = document.querySelector("#skip-list");
  skipped.innerHTML = plan.skipped.length
    ? plan.skipped.map((item) => fileRow(item.path, item.reason)).join("")
    : `<div class="file"><span class="meta">No unsupported files.</span></div>`;
  const runButton = document.querySelector("#start-run");
  if (!plan.count) {
    runButton.disabled = true;
    runButton.textContent = "Nothing to copy";
  } else if (plan.needs_ffmpeg && !plan.ffmpeg_found) {
    runButton.disabled = true;
    runButton.textContent = "ffmpeg is required";
    showError(4, "ffmpeg was not found on PATH. Copying JPG and MP4 files that need conversion cannot start until ffmpeg is installed.");
  } else {
    runButton.disabled = false;
    runButton.textContent = `Copy ${plan.count} files`;
  }
  document.querySelector("#plan-filter").value = "";
}

async function startRun() {
  clearError(5);
  showStep(5);
  const progress = document.querySelector("#run-progress");
  renderProgress(progress, { done: 0, total: state.plan.count, message: "Starting conversion and copy. Compatible JPG and MP4 files are copied. Other images become JPG. Other videos become H.264/AAC MP4." });
  document.querySelector("#run-summary").hidden = true;
  document.querySelector("#run-list").innerHTML = "";
  try {
    const started = await postJson("/api/run");
    const result = await poll(started.job_id, (job) => renderProgress(progress, job));
    renderRun(result);
  } catch (error) {
    showError(5, error.message);
  }
}

function renderRun(result) {
  const summary = document.querySelector("#run-summary");
  summary.hidden = false;
  summary.innerHTML = [
    countLine(result.type_counts, result.file_count, "files written"),
    actionLine(result.action_counts),
    pill(`${result.failed_count} failed`),
    pill(`${result.skipped_count} skipped`),
    pill(result.output_dir),
  ].join("");
  document.querySelector("#run-list").innerHTML = result.written.length
    ? result.written.map((item) => fileRow(item.destination, `${item.action_label} · from ${item.source}`)).join("")
    : `<div class="file"><span class="meta">No files were written.</span></div>`;
  const failures = document.querySelector("#run-failures");
  const problems = [...(result.failed || []), ...(result.warnings || [])];
  if (problems.length) {
    failures.hidden = false;
    failures.innerHTML = problems.map((item) => `<div class="path">${esc(item.path)} — ${esc(item.error || item.warning)}</div>`).join("");
  } else {
    failures.hidden = true;
  }
}

function filterCards(listSelector, inputSelector) {
  const query = document.querySelector(inputSelector).value.trim().toLowerCase();
  document.querySelectorAll(`${listSelector} .file`).forEach((card) => {
    card.hidden = Boolean(query) && !card.textContent.toLowerCase().includes(query);
  });
}

function dateSourceLabel(source) {
  return {
    file_created: "File created",
    file_modified: "File modified",
  }[source] || source;
}

function fileRow(path, meta) {
  return `<div class="file"><code>${esc(path)}</code><span class="meta">${esc(meta)}</span></div>`;
}

function countLine(counts, total, label) {
  const pills = [pill(`${total} ${label || "total files"}`)];
  for (const item of counts || []) pills.push(pill(`${item.count} ${item.extension}`));
  return pills.join("");
}

function actionLine(counts) {
  return (counts || []).map((item) => pill(`${item.count} ${item.action}`)).join("");
}

function renderOtherFiles(files) {
  if (!files.length) {
    return `<div class="file"><strong>Every file in this folder is a media file.</strong></div>`;
  }
  const groups = new Map();
  for (const file of files) {
    const extension = file.extension || "(no extension)";
    if (!groups.has(extension)) groups.set(extension, []);
    groups.get(extension).push(file);
  }
  return [...groups.entries()]
    .sort((left, right) => right[1].length - left[1].length || left[0].localeCompare(right[0]))
    .map(([extension, group]) => `<details>
      <summary>${esc(extension)} · ${group.length} ${group.length === 1 ? "file" : "files"}, not copied</summary>
      ${group.map((file) => `<div class="file"><code>${esc(file.path)}</code><span class="meta">${formatBytes(file.size)}</span></div>`).join("")}
    </details>`)
    .join("");
}

function pill(text) {
  return `<span>${esc(text)}</span>`;
}

function renderProgress(node, job) {
  const total = job.total || 0;
  const done = job.done || 0;
  const percent = total ? Math.round((done / total) * 100) : (job.state === "done" ? 100 : 8);
  node.innerHTML = `<div>${esc(job.message || "Working")} · ${done} / ${total}</div><div class="bar"><span style="width:${percent}%"></span></div>`;
}

async function poll(jobId, onProgress) {
  while (true) {
    const job = await getJson(`/api/jobs/${jobId}`);
    onProgress(job);
    if (job.state === "done") return job.result;
    if (job.state === "error") throw new Error(job.error || "The task failed.");
    await sleep(400);
  }
}

async function getJson(url) {
  const response = await fetch(url);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(messageFrom(data, response.statusText));
  return data;
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(messageFrom(data, response.statusText));
  return data;
}

function messageFrom(data, fallback) {
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) return data.detail.map((item) => item.msg || String(item)).join(" ");
  return fallback || "Request failed";
}

function showError(step, message) {
  const node = document.querySelector(`#step${step}-error`);
  node.hidden = false;
  node.textContent = message;
}

function clearError(step) {
  const node = document.querySelector(`#step${step}-error`);
  node.hidden = true;
  node.textContent = "";
}

function formatBytes(size) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = size;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
