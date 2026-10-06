/* RAT dashboard — vanilla JS: state, API client, renderers, charts. */
"use strict";

/* ---------------- helpers ---------------- */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const NF = new Intl.NumberFormat("en-US");
const fmt = (n) => NF.format(Math.round(n ?? 0));
const dec = (n) => { n = n ?? 0; return n > 0 && n < 1 ? n.toFixed(4) : NF.format(Math.round(n * 100) / 100); };
const pct = (x) => ((x ?? 0) * 100).toFixed(1) + "%";
const fmtDate = (ts) => new Date(ts * 1000).toLocaleDateString(undefined,
  { year: "numeric", month: "short", day: "numeric" });
const dateToTs = (s) => Math.floor(new Date(s + "T00:00:00").getTime() / 1000);
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

async function api(path, { method = "GET", body, form } = {}) {
  const opts = { method, headers: {} };
  if (form) opts.body = form;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch("/api" + path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) throw new Error((data && data.error) || res.status + " " + res.statusText);
  return data;
}

function toast(msg, kind) {
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function loading(on) {
  $("loadingbar").classList.toggle("hidden", !on);
  const d = $("dash");
  if (!d.classList.contains("hidden")) d.classList.toggle("loading", on);
}

function downloadCSV(name, header, rows) {
  const cell = (v) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const csv = [header.join(","), ...rows.map((r) => r.map(cell).join(","))].join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ---------------- chart library (CDN, with CSS fallback) ---------------- */
let chartLib = null;
const chartReady = new Promise((resolve) => {
  const s = document.createElement("script");
  s.src = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js";
  s.async = true;
  s.onload = () => { chartLib = window.Chart; resolve(true); };
  s.onerror = () => { chartLib = false; resolve(false); };
  setTimeout(() => { if (chartLib === null) { chartLib = false; resolve(false); } }, 6000);
  document.head.appendChild(s);
});

const PALETTE = ["#4f46e5", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444",
  "#8b5cf6", "#14b8a6", "#f97316", "#64748b", "#db2777"];

function destroyChart(name) {
  if (S.charts[name]) { S.charts[name].destroy(); delete S.charts[name]; }
}

/* ---------------- state ---------------- */
const S = {
  repos: [], repo: null, authors: [],
  author: "all", since: null, until: null, commits: null,
  object: "", type: "dir",
  sort: "churn", q: "", offset: 0, limit: 100,
  data: null, charts: {},
  cmSelected: new Set(), cmPage: 1, cmQ: "", cmRows: [], cmTotal: 0,
  poll: null,
};

/* ---------------- rail: repositories ---------------- */
const repoMeta = (r) => [
  `${fmt(r.commit_count)} commits`,
  r.source_type === "url" ? r.source : "zip upload",
  r.has_mailmap ? "mailmap applied" : null,
].filter(Boolean).join(" · ");

function renderRail() {
  const ul = $("repo-list");
  ul.innerHTML = "";
  $("repo-empty").classList.toggle("hidden", S.repos.length > 0);
  for (const r of S.repos) {
    const li = document.createElement("li");
    li.className = "repo-item" + (S.repo && r.id === S.repo.id ? " selected" : "");
    const status = r.status === "ready" ? `${fmt(r.commit_count)} commits`
      : r.status === "error" ? '<span class="err">ingestion failed</span>'
        : `ingesting ${Math.round((r.progress || 0) * 100)}%`;
    li.innerHTML = `<div class="name">${esc(r.name)}</div><div class="meta">${status}</div>`
      + (r.status === "ingesting"
        ? `<div class="progress"><i style="width:${Math.round((r.progress || 0) * 100)}%"></i></div>` : "");
    const del = document.createElement("button");
    del.className = "del";
    del.type = "button";
    del.title = "Remove repository";
    del.textContent = "\u00d7";
    del.onclick = (ev) => { ev.stopPropagation(); confirmDelete(del, r.id); };
    li.appendChild(del);
    li.onclick = () => selectRepo(r.id);
    ul.appendChild(li);
  }
}

function confirmDelete(btn, id) {
  if (btn.dataset.confirm) { deleteRepo(id); return; }
  btn.dataset.confirm = "1";
  btn.textContent = "Delete?";
  btn.classList.add("confirm");
  setTimeout(() => {
    delete btn.dataset.confirm;
    btn.textContent = "\u00d7";
    btn.classList.remove("confirm");
  }, 2600);
}

async function deleteRepo(id) {
  try {
    await api(`/repos/${id}`, { method: "DELETE" });
    toast("Repository removed", "ok");
  } catch (e) { toast(e.message, "error"); }
  if (S.repo && S.repo.id === id) { S.repo = null; S.data = null; }
  await loadRepos();
}

async function loadRepos(selectId) {
  S.repos = await api("/repos");
  if (S.repo) S.repo = S.repos.find((r) => r.id === S.repo.id) || null;
  renderRail();
  if (selectId) await selectRepo(selectId);
  else if (!S.repo && S.repos.length) {
    const d = S.repos.find((r) => r.status === "ready") || S.repos[0];
    await selectRepo(d.id);
  } else {
    renderState();
  }
  managePoll();
}

function managePoll() {
  const any = S.repos.some((r) => r.status === "ingesting");
  if (any && !S.poll) S.poll = setInterval(refreshStatuses, 2000);
  if (!any && S.poll) { clearInterval(S.poll); S.poll = null; }
}

async function refreshStatuses() {
  const prev = S.repo ? { id: S.repo.id, status: S.repo.status } : null;
  try {
    S.repos = await api("/repos");
    if (S.repo) S.repo = S.repos.find((r) => r.id === S.repo.id) || null;
    renderRail();
    if (S.repo && prev && prev.status === "ingesting" && S.repo.status !== "ingesting") {
      renderState();
      if (S.repo.status === "ready") { await loadAuthors(); await loadDash(); }
    } else {
      renderState();
    }
  } catch (e) { /* transient network error while polling */ }
  managePoll();
}

async function selectRepo(id) {
  S.repo = S.repos.find((r) => r.id === id) || null;
  if (!S.repo) { renderState(); return; }
  S.author = "all"; S.since = null; S.until = null; S.commits = null;
  S.object = ""; S.type = "dir"; S.q = ""; S.sort = "churn"; S.offset = 0;
  S.data = null; S.authors = [];
  $("f-time").value = "all";
  $("time-custom").classList.add("hidden");
  $("f-since").value = ""; $("f-until").value = "";
  $("files-q").value = ""; $("files-sort").value = "churn";
  renderRail(); renderState(); renderCrumbs(); renderCommitsChip();
  if (S.repo.status === "ready") {
    await loadAuthors();
    await loadDash();
  }
}

function renderState() {
  const ready = S.repo && S.repo.status === "ready";
  $("filterbar").classList.toggle("hidden", !ready);
  const target = !S.repo ? "state-empty"
    : S.repo.status === "ingesting" ? "state-ingest"
      : S.repo.status === "error" ? "state-error" : "dash";
  for (const x of ["state-empty", "state-ingest", "state-error", "dash"]) {
    $(x).classList.toggle("hidden", x !== target);
  }
  if (!S.repo) return;
  $("repo-name").textContent = S.repo.name;
  $("repo-meta").textContent = repoMeta(S.repo);
  if (target === "state-ingest") {
    $("ingest-name").textContent = S.repo.name;
    const p = Math.round((S.repo.progress || 0) * 100);
    $("ingest-bar").style.width = p + "%";
    $("ingest-pct").textContent = p + "%";
  }
  if (target === "state-error") $("error-msg").textContent = S.repo.message || "Unknown error";
}

/* ---------------- add repository ---------------- */
function setTab(kind) {
  $("tab-zip").classList.toggle("active", kind === "zip");
  $("tab-url").classList.toggle("active", kind === "url");
  $("pane-zip").classList.toggle("hidden", kind !== "zip");
  $("pane-url").classList.toggle("hidden", kind !== "url");
}

async function addRepo(kind) {
  let opts;
  if (kind === "zip") {
    const f = $("zip-file").files[0];
    if (!f) { toast("Choose a .zip file first", "error"); return; }
    const form = new FormData();
    form.append("file", f);
    opts = { form };
  } else {
    const url = $("url-input").value.trim();
    if (!url) { toast("Enter a repository URL", "error"); return; }
    opts = { body: { url } };
  }
  loading(true);
  try {
    const out = await api("/repos", { method: "POST", ...opts });
    $("zip-file").value = "";
    $("url-input").value = "";
    toast(kind === "zip" ? "Upload received — ingesting\u2026" : "Clone started — ingesting\u2026", "ok");
    await loadRepos(out.id);
  } catch (e) { toast(e.message, "error"); }
  finally { loading(false); }
}

/* ---------------- authors ---------------- */
async function loadAuthors() {
  if (!S.repo) return;
  const out = await api(`/repos/${S.repo.id}/authors`);
  S.authors = out.authors || [];
  const sel = $("f-author");
  sel.innerHTML = '<option value="all">All authors</option>'
    + S.authors.map((a) =>
      `<option value="${a.id}">${esc(a.name)} \u2039${esc(a.email)}\u203a</option>`).join("");
  sel.value = S.author;
}

function openMerge() {
  if (!S.repo) return;
  if (S.authors.length < 2) { toast("Need at least two authors to merge", "error"); return; }
  $("modal-merge").classList.remove("hidden");
  $("mg-target").innerHTML = S.authors.map((a) =>
    `<option value="${a.id}">${esc(a.name)} \u2039${esc(a.email)}\u203a (${fmt(a.commits)} commits)</option>`).join("");
  $("mg-sources").innerHTML = S.authors.map((a) =>
    `<label><input type="checkbox" value="${a.id}">
       <span>${esc(a.name)}</span><span class="mail">\u2039${esc(a.email)}\u203a</span></label>`).join("");
}

async function applyMerge() {
  const target = Number($("mg-target").value);
  const sources = [...$("mg-sources").querySelectorAll("input:checked")].map((i) => Number(i.value));
  if (!sources.length) { toast("Select at least one author to merge", "error"); return; }
  if (sources.includes(target)) { toast("The target author cannot also be a source", "error"); return; }
  try {
    await api(`/repos/${S.repo.id}/authors/merge`, { method: "POST", body: { target, sources } });
    $("modal-merge").classList.add("hidden");
    toast(`Merged ${sources.length} author${sources.length > 1 ? "s" : ""}`, "ok");
    await loadAuthors();
    await loadDash();
  } catch (e) { toast(e.message, "error"); }
}

/* ---------------- filters ---------------- */
function applyTime() {
  const v = $("f-time").value;
  if (v === "all") {
    S.since = null; S.until = null;
    $("time-custom").classList.add("hidden");
  } else if (v === "custom") {
    $("time-custom").classList.remove("hidden");
    S.since = $("f-since").value ? dateToTs($("f-since").value) : null;
    S.until = $("f-until").value ? dateToTs($("f-until").value) + 86400 : null;
  } else {
    $("time-custom").classList.add("hidden");
    S.since = Math.floor(Date.now() / 1000) - Number(v) * 86400;
    S.until = null;
  }
  S.offset = 0;
  loadDash();
}

function resetFilters() {
  S.author = "all"; S.since = null; S.until = null; S.commits = null; S.q = "";
  S.sort = "churn"; S.offset = 0; S.object = ""; S.type = "dir";
  $("f-author").value = "all";
  $("f-time").value = "all";
  $("time-custom").classList.add("hidden");
  $("f-since").value = ""; $("f-until").value = "";
  $("files-q").value = ""; $("files-sort").value = "churn";
  renderCommitsChip();
  renderCrumbs();
  loadDash();
}

function renderCommitsChip() {
  const b = $("f-commits");
  b.textContent = S.commits ? `${fmt(S.commits.length)} commits selected` : "All commits";
  b.classList.toggle("active", !!S.commits);
}

function filterPayload() {
  const p = {
    object: S.object, type: S.type, sort: S.sort, q: S.q,
    files_limit: S.limit, files_offset: S.offset,
  };
  if (S.author !== "all") p.author = S.author;
  if (S.since != null) p.since = S.since;
  if (S.until != null) p.until = S.until;
  if (S.commits) p.commits = S.commits.join(",");
  return p;
}

/* ---------------- object navigation ---------------- */
function navigate(path, type) {
  S.object = path;
  S.type = type;
  S.offset = 0;
  renderCrumbs();
  loadDash();
}

function renderCrumbs() {
  const c = $("crumbs");
  c.innerHTML = "";
  const addBtn = (label, path) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.onclick = () => navigate(path, "dir");
    c.appendChild(b);
  };
  addBtn("root", "");
  const parts = S.object ? S.object.split("/") : [];
  let acc = "";
  parts.forEach((part, i) => {
    const sep = document.createElement("span");
    sep.className = "sep";
    sep.textContent = "/";
    c.appendChild(sep);
    acc = acc ? acc + "/" + part : part;
    const last = i === parts.length - 1;
    if (last && S.type === "file") {
      const span = document.createElement("span");
      span.className = "current";
      span.textContent = part;
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "FILE";
      span.appendChild(tag);
      c.appendChild(span);
    } else if (last) {
      const span = document.createElement("span");
      span.className = "current";
      span.textContent = part;
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "DIR";
      span.appendChild(tag);
      c.appendChild(span);
    } else {
      addBtn(part, acc);
    }
  });
}

/* ---------------- dashboard ---------------- */
async function loadDash() {
  if (!S.repo || S.repo.status !== "ready") return;
  loading(true);
  try {
    S.data = await api(`/repos/${S.repo.id}/metrics`, { method: "POST", body: filterPayload() });
    renderDash();
  } catch (e) { toast(e.message, "error"); }
  finally { loading(false); }
}

function renderDash() {
  renderCards();
  renderObjStrip();
  renderDirs();
  renderFiles();
  renderAuthors();
  renderTimeline();
}

function renderCards() {
  const d = S.data;
  const o = d.object;
  const active = d.authors.filter((a) => a.modifications > 0).length;
  const cards = [
    ["Commits in set", fmt(d.h_count), "", "|H| — non-merge commits reachable from HEAD, matching the filters"],
    ["Active authors", fmt(active), "", "authors with at least one modification of the object in H"],
    ["Added", fmt(o.added), "pos", "l+ — lines added"],
    ["Removed", fmt(o.removed), "neg", "l\u2212 — lines removed"],
    ["Growth", (o.growth >= 0 ? "+" : "") + fmt(o.growth), o.growth >= 0 ? "pos" : "neg", "\u03b4 = l+ \u2212 l\u2212"],
    ["Churn", fmt(o.churn), "", "\u03bb = l+ + l\u2212"],
  ];
  $("cards").innerHTML = cards.map(([label, value, cls, title]) =>
    `<div class="card" title="${esc(title)}"><div class="label">${label}</div>
       <div class="value ${cls}">${value}</div></div>`).join("");
}

function renderObjStrip() {
  const o = S.data.object;
  const stats = [
    ["l+", fmt(o.added), "lines added"],
    ["l\u2212", fmt(o.removed), "lines removed"],
    ["\u03b4", fmt(o.growth), "growth = l+ \u2212 l\u2212"],
    ["\u03bb", fmt(o.churn), "churn = l+ + l\u2212"],
    ["n", fmt(o.modifications), "modifications — commits that changed this object"],
    ["\u03b7", dec(o.modification_frequency), "modification frequency = n / |H|"],
    ["\u03c1", dec(o.churn_rate), "churn rate = \u03bb / |H|"],
  ];
  $("obj-strip").innerHTML =
    `<span class="obj-name">${esc(S.object || "root")}</span>`
    + `<span class="crumbs"><span class="tag">${S.type === "file" ? "FILE" : "DIR"}</span></span>`
    + stats.map(([sym, val, title]) =>
      `<span class="stat" title="${esc(title)}"><span class="sym">${sym}</span><b>${val}</b></span>`).join("");
}

const HEAD_COLS = `<th title="lines added">l+</th><th title="lines removed">l\u2212</th>
  <th title="growth \u03b4 = l+ \u2212 l\u2212">\u03b4</th>
  <th title="churn \u03bb = l+ + l\u2212">\u03bb</th>
  <th title="modifications n">n</th>
  <th title="\u03b7 = n/|H|">\u03b7</th><th title="\u03c1 = \u03bb/|H|">\u03c1</th>`;

function metricCells(r) {
  return `<td class="pos">${fmt(r.added)}</td><td class="neg">${fmt(r.removed)}</td>
    <td>${fmt(r.growth)}</td><td class="num-strong">${fmt(r.churn)}</td>
    <td>${fmt(r.modifications)}</td>
    <td>${r.modification_frequency == null ? "\u2013" : dec(r.modification_frequency)}</td>
    <td>${r.churn_rate == null ? "\u2013" : dec(r.churn_rate)}</td>`;
}

function renderDirs() {
  const panel = $("panel-dirs");
  if (S.type !== "dir" || !S.data.dirs) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  $("dirs-title").textContent = S.object ? `Directory: ${S.object}` : "Repository root";
  const { dirs, files } = S.data.dirs;
  let html = "";
  if (dirs.length) {
    html += `<div class="table-title">Sub-directories</div>
      <table class="grid"><thead><tr><th>Directory</th>${HEAD_COLS}</tr></thead><tbody>`
      + dirs.map((d) => `<tr class="clickable" data-path="${esc(d.path)}" data-type="dir">
          <td class="path">${esc(d.path)}/</td>${metricCells(d)}</tr>`).join("")
      + `</tbody></table>`;
  }
  if (files.length) {
    html += `<div class="table-title">Files here</div>
      <table class="grid"><thead><tr><th>File</th>${HEAD_COLS}</tr></thead><tbody>`
      + files.map((f) => `<tr class="clickable" data-path="${esc(f.path)}" data-type="file">
          <td class="path">${esc(f.path)}</td>${metricCells(f)}</tr>`).join("")
      + `</tbody></table>`;
  }
  if (!dirs.length && !files.length) {
    html = `<p class="empty-note">No files or directories under this object for the current filter.</p>`;
  }
  $("dirs-body").innerHTML = html;
  $("dirs-body").querySelectorAll("tr.clickable").forEach((tr) => {
    tr.onclick = () => navigate(tr.dataset.path, tr.dataset.type);
  });
}

function renderFiles() {
  const panel = $("panel-files");
  if (S.type === "file") { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  const f = S.data.files;
  if (!f.rows.length) {
    $("files-body").innerHTML = `<p class="empty-note">`
      + (S.q ? "No files match this path filter." : "No changed files under this object for the current filter.")
      + `</p>`;
    $("files-pager").innerHTML = "";
    return;
  }
  $("files-body").innerHTML = `<table class="grid"><thead><tr><th>File</th>${HEAD_COLS}</tr></thead><tbody>`
    + f.rows.map((r) => `<tr class="clickable" data-path="${esc(r.path)}">
        <td class="path">${esc(r.path)}</td>${metricCells(r)}</tr>`).join("")
    + `</tbody></table>`;
  $("files-body").querySelectorAll("tr.clickable").forEach((tr) => {
    tr.onclick = () => navigate(tr.dataset.path, "file");
  });
  const from = S.offset + 1, to = S.offset + f.rows.length;
  $("files-pager").innerHTML =
    `<span>Showing <b>${fmt(from)}\u2013${fmt(to)}</b> of <b>${fmt(f.total)}</b> files</span>
     <button id="pg-prev" ${S.offset <= 0 ? "disabled" : ""}>\u2039 Prev</button>
     <button id="pg-next" ${to >= f.total ? "disabled" : ""}>Next \u203a</button>`;
  $("pg-prev").onclick = () => { S.offset = Math.max(0, S.offset - S.limit); loadDash(); };
  $("pg-next").onclick = () => { if (to < f.total) { S.offset += S.limit; loadDash(); } };
}

function renderAuthors() {
  const rows = (S.data.authors || []).filter((a) => a.churn > 0 || a.modifications > 0);
  if (!rows.length) {
    $("authors-body").innerHTML = `<p class="empty-note">No author activity for the current filter.</p>`;
    renderOwnership([]);
    return;
  }
  $("authors-body").innerHTML = `<table class="grid"><thead><tr>
      <th>Author</th><th title="modifications n">n</th><th title="churn \u03bb">\u03bb</th>
      <th title="ownership \u03c9 = \u03bb_author / \u03bb_object">\u03c9</th></tr></thead><tbody>`
    + rows.map((a) => `<tr>
        <td>${esc(a.name)}<span class="muted small"> \u2039${esc(a.email)}\u203a</span></td>
        <td>${fmt(a.modifications)}</td><td class="num-strong">${fmt(a.churn)}</td>
        <td><span class="meter"><i style="width:${Math.min(100, a.ownership * 100).toFixed(1)}%"></i></span>${pct(a.ownership)}</td>
      </tr>`).join("")
    + `</tbody></table>`;
  renderOwnership(rows);
}

/* ---------------- charts ---------------- */
function fillGaps(points, bucket) {
  if (points.length < 2) return points;
  if (bucket === "month") {
    const byT = new Map(points.map((p) => [p.t, p]));
    const [y0, m0] = points[0].t.split("-").map(Number);
    const [y1, m1] = points[points.length - 1].t.split("-").map(Number);
    const out = [];
    for (let y = y0, m = m0; y < y1 || (y === y1 && m <= m1); m === 12 ? (m = 1, y++) : m++) {
      const key = `${y}-${String(m).padStart(2, "0")}`;
      out.push(byT.get(key) || { t: key, added: 0, removed: 0, churn: 0 });
    }
    return out;
  }
  const byT = new Map(points.map((p) => [p.t, p]));
  const out = [];
  for (let t = points[0].t; t <= points[points.length - 1].t; t += 86400) {
    out.push(byT.get(t) || { t, added: 0, removed: 0, churn: 0 });
  }
  return out;
}

async function renderTimeline() {
  const t = S.data.timeline;
  destroyChart("timeline");
  $("timeline-sub").textContent =
    `${t.bucket === "month" ? "monthly" : "daily"} buckets \u00b7 ${S.object || "root"}`;
  const points = fillGaps(t.points, t.bucket);
  const cv = $("chart-timeline"), fb = $("chart-timeline-fb");
  if (!points.length) {
    cv.classList.add("hidden"); fb.classList.remove("hidden");
    fb.innerHTML = `<p class="empty-note">No changes for the current filter.</p>`;
    return;
  }
  const hasLib = window.Chart || await chartReady;
  if (!hasLib) {
    cv.classList.add("hidden"); fb.classList.remove("hidden");
    fb.innerHTML = cssTimeline(points, t.bucket);
    return;
  }
  cv.classList.remove("hidden"); fb.classList.add("hidden");
  const labels = points.map((p) => (t.bucket === "month" ? p.t : fmtDate(p.t)));
  S.charts.timeline = new Chart(cv.getContext("2d"), {
    type: "bar",
    data: {
      labels,
      datasets: [
        { label: "added l+", data: points.map((p) => p.added), backgroundColor: "rgba(22,163,74,.8)", borderRadius: 2, maxBarThickness: 26 },
        { label: "removed l\u2212", data: points.map((p) => p.removed), backgroundColor: "rgba(220,38,38,.75)", borderRadius: 2, maxBarThickness: 26 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 250 },
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 12, usePointStyle: true } },
        tooltip: { callbacks: { label: (c) => ` ${c.dataset.label}: ${fmt(c.parsed.y)}` } },
      },
      scales: {
        x: { ticks: { maxTicksLimit: 12, autoSkip: true, maxRotation: 0 }, grid: { display: false } },
        y: { beginAtZero: true, ticks: { callback: (v) => fmt(v) } },
      },
    },
  });
}

function cssTimeline(points, bucket) {
  const max = Math.max(...points.map((p) => Math.max(p.added, p.removed)), 1);
  const cols = points.map((p) => {
    const label = bucket === "month" ? p.t : fmtDate(p.t);
    return `<div class="cf-col" title="${label} \u2014 added ${fmt(p.added)}, removed ${fmt(p.removed)}">
      <div class="half"><span class="bar" style="height:${(p.added / max * 100).toFixed(1)}%"></span></div>
      <div class="half down"><span class="bar" style="height:${(p.removed / max * 100).toFixed(1)}%"></span></div>
    </div>`;
  }).join("");
  const first = bucket === "month" ? points[0].t : fmtDate(points[0].t);
  const last = bucket === "month" ? points[points.length - 1].t : fmtDate(points[points.length - 1].t);
  return `<div class="cf-tl">${cols}</div>
    <div class="cf-axis"><span>${first}</span><span>${last}</span></div>`;
}

async function renderOwnership(rows) {
  destroyChart("ownership");
  const legend = $("own-legend");
  const cv = $("chart-ownership"), fb = $("chart-ownership-fb");
  const total = rows.reduce((s, r) => s + r.churn, 0);
  if (!total) {
    legend.innerHTML = "";
    cv.classList.add("hidden"); fb.classList.remove("hidden");
    fb.innerHTML = `<p class="empty-note">No churn for the current filter.</p>`;
    return;
  }
  const top = rows.slice(0, 8);
  const rest = rows.slice(8);
  const items = top.map((r, i) => ({ name: r.name, value: r.churn, color: PALETTE[i % PALETTE.length] }));
  if (rest.length) {
    items.push({ name: `others (${rest.length})`, value: rest.reduce((s, r) => s + r.churn, 0), color: "#94a3b8" });
  }
  legend.innerHTML = items.map((it) =>
    `<div class="entry"><span class="dot" style="background:${it.color}"></span>
       ${esc(it.name)} <span class="pct">${pct(it.value / total)}</span></div>`).join("");
  const hasLib = window.Chart || await chartReady;
  if (!hasLib) {
    cv.classList.add("hidden"); fb.classList.remove("hidden");
    fb.innerHTML = `<div class="cf-own">`
      + items.map((it) => `<i style="width:${(it.value / total * 100).toFixed(2)}%;background:${it.color}" title="${esc(it.name)} ${pct(it.value / total)}"></i>`).join("")
      + `</div><p class="muted small" style="margin:8px 0 0">Chart library unavailable — showing the share bar instead.</p>`;
    return;
  }
  cv.classList.remove("hidden"); fb.classList.add("hidden");
  S.charts.ownership = new Chart(cv.getContext("2d"), {
    type: "doughnut",
    data: {
      labels: items.map((i) => i.name),
      datasets: [{
        data: items.map((i) => i.value),
        backgroundColor: items.map((i) => i.color),
        borderWidth: 2, borderColor: "#fff",
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: "62%", animation: { duration: 250 },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (c) => ` ${c.label}: ${fmt(c.parsed)} (${pct(c.parsed / total)})` } },
      },
    },
  });
}

/* ---------------- commits modal ---------------- */
async function openCommits() {
  if (!S.repo) return;
  $("modal-commits").classList.remove("hidden");
  S.cmSelected = new Set(S.commits || []);
  S.cmPage = 1;
  S.cmQ = "";
  $("cm-q").value = "";
  await loadCommitsPage();
}

async function loadCommitsPage() {
  const out = await api(`/repos/${S.repo.id}/commits?q=${encodeURIComponent(S.cmQ)}&page=${S.cmPage}&per_page=50`);
  S.cmRows = out.rows;
  S.cmTotal = out.total;
  renderCommits();
}

function renderCommits() {
  if (!S.cmRows.length) {
    $("cm-body").innerHTML = `<p class="empty-note" style="padding:10px">No commits match.</p>`;
  } else {
    $("cm-body").innerHTML = `<table class="grid"><thead><tr>
        <th class="row-check"></th><th>SHA</th><th>Date</th><th>Author</th><th>Summary</th>
      </tr></thead><tbody>`
      + S.cmRows.map((c) => `<tr class="clickable" data-id="${c.id}">
          <td class="row-check"><input type="checkbox" ${S.cmSelected.has(c.id) ? "checked" : ""}></td>
          <td class="path">${c.sha.slice(0, 9)}</td>
          <td>${fmtDate(c.committer_ts)}</td>
          <td>${esc(c.author_name)}</td>
          <td style="white-space:normal">${esc(c.summary)}</td></tr>`).join("")
      + `</tbody></table>`;
    $("cm-body").querySelectorAll("tr[data-id]").forEach((tr) => {
      const id = Number(tr.dataset.id);
      tr.onclick = (ev) => {
        let on;
        if (ev.target.tagName === "INPUT") on = ev.target.checked;
        else {
          const cb = tr.querySelector("input");
          cb.checked = !cb.checked;
          on = cb.checked;
        }
        if (on) S.cmSelected.add(id); else S.cmSelected.delete(id);
        updateCmCount();
      };
    });
  }
  const per = 50;
  const from = (S.cmPage - 1) * per + 1;
  const to = Math.min(S.cmPage * per, S.cmTotal);
  $("cm-pager").innerHTML = S.cmTotal
    ? `<span>Showing <b>${fmt(from)}\u2013${fmt(to)}</b> of <b>${fmt(S.cmTotal)}</b> commits</span>
       <button id="cm-prev" ${S.cmPage <= 1 ? "disabled" : ""}>\u2039 Prev</button>
       <button id="cm-next" ${to >= S.cmTotal ? "disabled" : ""}>Next \u203a</button>`
    : "";
  if (S.cmTotal) {
    $("cm-prev").onclick = () => { S.cmPage--; loadCommitsPage(); };
    $("cm-next").onclick = () => { S.cmPage++; loadCommitsPage(); };
  }
  updateCmCount();
}

function updateCmCount() {
  const n = S.cmSelected.size;
  $("cm-count").textContent = n ? `${fmt(n)} commit${n === 1 ? "" : "s"} selected` : "No commits selected";
  $("cm-apply").disabled = n === 0;
}

function applyCommits() {
  S.commits = [...S.cmSelected].sort((a, b) => a - b);
  $("modal-commits").classList.add("hidden");
  renderCommitsChip();
  S.offset = 0;
  loadDash();
}

/* ---------------- object search ---------------- */
const searchObjects = debounce(async () => {
  const q = $("obj-q").value.trim();
  const box = $("obj-results");
  if (!q || !S.repo || S.repo.status !== "ready") {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  try {
    const out = await api(`/repos/${S.repo.id}/tree?q=${encodeURIComponent(q)}`);
    const objs = out.objects || [];
    box.innerHTML = objs.length
      ? objs.map((o) => `<div class="item" data-path="${esc(o.path)}" data-type="${o.is_dir ? "dir" : "file"}">
          <span class="tag ${o.is_dir ? "dir" : ""}">${o.is_dir ? "dir" : "file"}</span>
          ${esc(o.path)}</div>`).join("")
      : `<div class="empty">No matching files or directories.</div>`;
    box.classList.remove("hidden");
    box.querySelectorAll(".item").forEach((it) => {
      it.onclick = () => {
        box.classList.add("hidden");
        $("obj-q").value = "";
        navigate(it.dataset.path, it.dataset.type);
      };
    });
  } catch (e) { /* search is best-effort */ }
}, 250);

/* ---------------- wiring ---------------- */
function wire() {
  $("tab-zip").onclick = () => setTab("zip");
  $("tab-url").onclick = () => setTab("url");
  $("zip-upload").onclick = () => addRepo("zip");
  $("url-clone").onclick = () => addRepo("url");
  $("url-input").addEventListener("keydown", (e) => { if (e.key === "Enter") addRepo("url"); });

  $("f-author").onchange = () => { S.author = $("f-author").value; S.offset = 0; loadDash(); };
  $("f-time").onchange = applyTime;
  $("f-since").onchange = applyTime;
  $("f-until").onchange = applyTime;
  $("f-commits").onclick = openCommits;
  $("f-reset").onclick = resetFilters;

  $("obj-q").oninput = searchObjects;

  $("files-sort").onchange = () => { S.sort = $("files-sort").value; S.offset = 0; loadDash(); };
  $("files-q").oninput = debounce(() => { S.q = $("files-q").value.trim(); S.offset = 0; loadDash(); }, 300);
  $("files-csv").onclick = () => {
    const rows = S.data && S.data.files ? S.data.files.rows : [];
    if (!rows.length) { toast("Nothing to export", "error"); return; }
    downloadCSV(`${S.repo.name}-files.csv`,
      ["path", "added", "removed", "growth", "churn", "modifications", "modification_frequency", "churn_rate"],
      rows.map((r) => [r.path, r.added, r.removed, r.growth, r.churn, r.modifications,
        r.modification_frequency, r.churn_rate]));
  };

  $("authors-merge").onclick = openMerge;
  $("mg-cancel").onclick = () => $("modal-merge").classList.add("hidden");
  $("mg-apply").onclick = applyMerge;
  $("authors-csv").onclick = () => {
    const rows = S.data ? S.data.authors : [];
    if (!rows.length) { toast("Nothing to export", "error"); return; }
    downloadCSV(`${S.repo.name}-authors.csv`,
      ["name", "email", "modifications", "churn", "ownership"],
      rows.map((a) => [a.name, a.email, a.modifications, a.churn, a.ownership]));
  };

  $("cm-q").oninput = debounce(() => {
    S.cmQ = $("cm-q").value.trim();
    S.cmPage = 1;
    loadCommitsPage();
  }, 250);
  $("cm-select-page").onclick = () => {
    S.cmRows.forEach((c) => S.cmSelected.add(c.id));
    renderCommits();
  };
  $("cm-clear").onclick = () => { S.cmSelected.clear(); renderCommits(); };
  $("cm-all").onclick = () => {
    S.commits = null;
    $("modal-commits").classList.add("hidden");
    renderCommitsChip();
    loadDash();
  };
  $("cm-apply").onclick = applyCommits;

  document.querySelectorAll("[data-close]").forEach((b) => {
    b.onclick = () => $(b.dataset.close).classList.add("hidden");
  });
  for (const id of ["modal-commits", "modal-merge"]) {
    $(id).onclick = (e) => { if (e.target.id === id) e.target.classList.add("hidden"); };
  }
  document.addEventListener("click", (e) => {
    if (!$("obj-results").contains(e.target) && e.target !== $("obj-q")) {
      $("obj-results").classList.add("hidden");
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      $("modal-commits").classList.add("hidden");
      $("modal-merge").classList.add("hidden");
    }
  });
}

wire();
loadRepos().catch((e) => toast(e.message, "error"));
