let pipeline = [];
let summary = {};
let filtered = [];
let charts = {};

const stageOrder = ["Discovery", "Preclinical", "Early Phase 1", "Phase 1", "Phase 1/2", "Phase 2", "Phase 2/3", "Phase 3", "Phase 4", "Approved/Authorized", "Unknown"];

function byCountDesc(entries) { return entries.sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0]))); }
function countBy(rows, key) {
  return rows.reduce((acc, row) => {
    const val = row[key] || "Unknown";
    acc[val] = (acc[val] || 0) + 1;
    return acc;
  }, {});
}
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[c]));
}
function fmtDate(value) {
  if (!value) return "–";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
function countObjectTotal(obj) { return Object.values(obj || {}).reduce((a, b) => a + Number(b || 0), 0); }
function renderAiPanel() {
  const ai = summary.ai_intelligence || {};
  const matching = ai.candidate_matching || {};
  const staging = ai.stage_classification || {};
  const matched = Number(matching.matched_source_records || 0);
  const aliases = Number(matching.aliases_detected || 0);
  document.getElementById("aiMatchingMetric").textContent = `${matched.toLocaleString()} merged / ${aliases.toLocaleString()} aliases`;
  document.getElementById("aiMatchingNote").textContent = matching.method || "alias rules + normalized matching";
  const conf = staging.confidence_counts || {};
  const high = Number(conf.high || 0);
  const medium = Number(conf.medium || 0);
  const low = Number(conf.low || 0);
  document.getElementById("aiStageMetric").textContent = `${high.toLocaleString()} high · ${medium.toLocaleString()} medium · ${low.toLocaleString()} low`;
  document.getElementById("aiStageNote").textContent = staging.method || "registry phase mapping + text classification";
  const bullets = ai.weekly_summary || [];
  document.getElementById("aiSummaryList").innerHTML = bullets.slice(0, 5).map(b => `<li>${escapeHtml(b)}</li>`).join("") || `<li class="subtle">No AI summary available.</li>`;
}

function uniqueSorted(key) {
  return [...new Set(pipeline.map(r => r[key]).filter(Boolean))].sort((a, b) => String(a).localeCompare(String(b)));
}
function fillSelect(id, values) {
  const el = document.getElementById(id);
  values.forEach(v => {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v;
    el.appendChild(opt);
  });
}
function stageClass(stage) {
  const s = String(stage || "unknown").toLowerCase().replace(/[^a-z0-9]+/g, "-");
  return `stage-${s}`;
}

function updateKpis() {
  document.getElementById("kpiTotal").textContent = filtered.length.toLocaleString();
  document.getElementById("kpiPreclinical").textContent = filtered.filter(r => ["Discovery", "Preclinical"].includes(r.stage)).length.toLocaleString();
  document.getElementById("kpiClinical").textContent = filtered.filter(r => (r.stage_order ?? -1) >= 3).length.toLocaleString();
  document.getElementById("kpiDiseases").textContent = new Set(filtered.map(r => r.disease)).size.toLocaleString();
  document.getElementById("recordCount").textContent = `${filtered.length.toLocaleString()} candidate records shown`;
}

function chartConfig(type, labels, values) {
  return {
    type,
    data: { labels, datasets: [{ data: values, borderWidth: 1 }] },
    options: {
      responsive: true,
      plugins: { legend: { display: type === "doughnut", position: "bottom" } },
      scales: type === "bar" ? { y: { beginAtZero: true, ticks: { precision: 0 } } } : undefined,
    }
  };
}
function renderCharts() {
  Object.values(charts).forEach(c => c.destroy());
  const stageCounts = countBy(filtered, "stage");
  const stageLabels = stageOrder.filter(p => stageCounts[p]).concat(Object.keys(stageCounts).filter(p => !stageOrder.includes(p)));
  charts.stage = new Chart(document.getElementById("stageChart"), chartConfig("bar", stageLabels, stageLabels.map(l => stageCounts[l])));

  const diseaseEntries = byCountDesc(Object.entries(countBy(filtered, "disease"))).slice(0, 14);
  charts.disease = new Chart(document.getElementById("diseaseChart"), chartConfig("bar", diseaseEntries.map(e => e[0]), diseaseEntries.map(e => e[1])));

  const platformEntries = byCountDesc(Object.entries(countBy(filtered, "platform"))).slice(0, 10);
  charts.platform = new Chart(document.getElementById("platformChart"), chartConfig("doughnut", platformEntries.map(e => e[0]), platformEntries.map(e => e[1])));
}

function renderTable() {
  const tbody = document.querySelector("#pipelineTable tbody");
  tbody.innerHTML = filtered.map(row => {
    const candidate = escapeHtml(row.candidate || row.id);
    const link = row.source_url ? `<a href="${escapeHtml(row.source_url)}" target="_blank" rel="noopener">${candidate}</a>` : `<strong>${candidate}</strong>`;
    const trialText = (row.supporting_trials || []).slice(0, 4).map(escapeHtml).join(", ");
    const evidence = row.supporting_trial_count ? `${row.supporting_trial_count} trial(s)<br><span class="subtle">${trialText}</span>` : `<span class="subtle">curated / preclinical source</span>`;
    return `<tr>
      <td>${link}<br><span class="subtle">${escapeHtml(row.id || "")}</span>${(row.candidate_aliases || []).length ? `<br><span class="subtle">Aliases: ${escapeHtml((row.candidate_aliases || []).slice(0,3).join(", "))}</span>` : ""}</td>
      <td>${escapeHtml(row.disease || "")}</td>
      <td><span class="badge ${stageClass(row.stage)}">${escapeHtml(row.stage || "Unknown")}</span><br><span class="subtle">AI confidence: ${escapeHtml(row.ai_stage_confidence || "–")}</span></td>
      <td>${escapeHtml(row.platform || "Unclassified")}</td>
      <td>${escapeHtml(row.developer || "Unknown")}</td>
      <td>${escapeHtml(row.status || "")}</td>
      <td>${evidence}</td>
      <td>${escapeHtml(row.source || "")}<br><span class="subtle">${fmtDate(row.last_update)}</span>${(row.sources_seen || []).length > 1 ? `<br><span class="subtle">${escapeHtml((row.sources_seen || []).length)} sources matched</span>` : ""}</td>
    </tr>`;
  }).join("");
}

function renderLists() {
  const recent = summary.recent_updates || [];
  document.getElementById("recentList").innerHTML = recent.slice(0, 12).map(r => {
    const text = `${escapeHtml(r.candidate)} · ${escapeHtml(r.disease)} · ${escapeHtml(r.stage)}`;
    const source = `${escapeHtml(r.source || "")} · ${fmtDate(r.last_update)}`;
    return `<li>${r.source_url ? `<a href="${escapeHtml(r.source_url)}" target="_blank" rel="noopener">${text}</a>` : `<strong>${text}</strong>`}<br><span class="subtle">${source}</span></li>`;
  }).join("") || `<li class="subtle">No recent updates available.</li>`;

  const sources = summary.sources || [];
  document.getElementById("sourceList").innerHTML = sources.map(s => {
    return `<li><a href="${escapeHtml(s.url || "#")}" target="_blank" rel="noopener">${escapeHtml(s.name || "Source")}</a><br><span class="subtle">${escapeHtml(s.status || "")}</span></li>`;
  }).join("");
}

function applyFilters() {
  const q = document.getElementById("searchBox").value.trim().toLowerCase();
  const disease = document.getElementById("diseaseFilter").value;
  const stage = document.getElementById("stageFilter").value;
  const platform = document.getElementById("platformFilter").value;
  filtered = pipeline.filter(row => {
    const hay = [row.candidate, row.disease, row.platform, row.developer, row.source, row.status, ...(row.supporting_trials || [])].join(" ").toLowerCase();
    return (!q || hay.includes(q)) && (!disease || row.disease === disease) && (!stage || row.stage === stage) && (!platform || row.platform === platform);
  });
  updateKpis();
  renderCharts();
  renderTable();
}

function downloadCsv() {
  const headers = ["candidate","disease","stage","platform","developer","status","last_update","source","source_url","supporting_trial_count","supporting_trials"];
  const lines = [headers.join(",")].concat(filtered.map(row => headers.map(h => {
    let v = row[h];
    if (Array.isArray(v)) v = v.join("; ");
    return `"${String(v ?? "").replace(/"/g, '""')}"`;
  }).join(",")));
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "vaccine-pipeline-candidates.csv";
  a.click();
  URL.revokeObjectURL(url);
}

async function init() {
  const [pipelineResp, summaryResp] = await Promise.all([
    fetch("data/pipeline.json", { cache: "no-store" }),
    fetch("data/summary.json", { cache: "no-store" })
  ]);
  pipeline = await pipelineResp.json();
  summary = await summaryResp.json();
  filtered = [...pipeline];
  document.getElementById("lastUpdated").textContent = fmtDate(summary.generated_at);
  fillSelect("diseaseFilter", uniqueSorted("disease"));
  fillSelect("stageFilter", stageOrder.filter(s => pipeline.some(r => r.stage === s)).concat(uniqueSorted("stage").filter(s => !stageOrder.includes(s))));
  fillSelect("platformFilter", uniqueSorted("platform"));
  ["searchBox","diseaseFilter","stageFilter","platformFilter"].forEach(id => document.getElementById(id).addEventListener("input", applyFilters));
  document.getElementById("resetBtn").addEventListener("click", () => {
    document.getElementById("searchBox").value = "";
    document.getElementById("diseaseFilter").value = "";
    document.getElementById("stageFilter").value = "";
    document.getElementById("platformFilter").value = "";
    applyFilters();
  });
  document.getElementById("downloadCsv").addEventListener("click", downloadCsv);
  updateKpis();
  renderCharts();
  renderTable();
  renderLists();
  renderAiPanel();
}

init().catch(err => {
  console.error(err);
  document.body.insertAdjacentHTML("afterbegin", `<div class="error">Failed to load dashboard data: ${escapeHtml(err.message)}</div>`);
});
