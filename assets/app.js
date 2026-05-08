let pipeline = [];
let summary = {};
let filtered = [];
let charts = {};

const phaseOrder = ["Early Phase 1", "Phase 1", "Phase 1/2", "Phase 2", "Phase 2/3", "Phase 3", "Phase 4", "Not Applicable", "Unknown"];
const activeStatuses = new Set(["RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION", "NOT_YET_RECRUITING"]);

function byCountDesc(entries) { return entries.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])); }
function countBy(rows, key) {
  return rows.reduce((acc, row) => {
    const val = row[key] || "Unknown";
    acc[val] = (acc[val] || 0) + 1;
    return acc;
  }, {});
}
function fmtDate(value) {
  if (!value) return "–";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[c]));
}
function uniqueSorted(key) {
  return [...new Set(pipeline.map(r => r[key]).filter(Boolean))].sort((a, b) => a.localeCompare(b));
}
function fillSelect(id, values) {
  const el = document.getElementById(id);
  values.forEach(v => {
    const opt = document.createElement("option");
    opt.value = v; opt.textContent = v;
    el.appendChild(opt);
  });
}

function updateKpis() {
  document.getElementById("kpiTotal").textContent = filtered.length.toLocaleString();
  document.getElementById("kpiActive").textContent = filtered.filter(r => activeStatuses.has(r.overall_status)).length.toLocaleString();
  document.getElementById("kpiDiseases").textContent = new Set(filtered.map(r => r.disease)).size.toLocaleString();
  document.getElementById("kpiPlatforms").textContent = new Set(filtered.map(r => r.platform)).size.toLocaleString();
  document.getElementById("recordCount").textContent = `${filtered.length.toLocaleString()} records shown`;
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
  const phaseCounts = countBy(filtered, "phase");
  const phaseLabels = phaseOrder.filter(p => phaseCounts[p]).concat(Object.keys(phaseCounts).filter(p => !phaseOrder.includes(p)));
  charts.phase = new Chart(document.getElementById("phaseChart"), chartConfig("bar", phaseLabels, phaseLabels.map(l => phaseCounts[l])));

  const diseaseEntries = byCountDesc(Object.entries(countBy(filtered, "disease"))).slice(0, 12);
  charts.disease = new Chart(document.getElementById("diseaseChart"), chartConfig("bar", diseaseEntries.map(e => e[0]), diseaseEntries.map(e => e[1])));

  const platformEntries = byCountDesc(Object.entries(countBy(filtered, "platform"))).slice(0, 10);
  charts.platform = new Chart(document.getElementById("platformChart"), chartConfig("doughnut", platformEntries.map(e => e[0]), platformEntries.map(e => e[1])));
}

function renderTable() {
  const tbody = document.querySelector("#pipelineTable tbody");
  tbody.innerHTML = filtered.map(row => {
    const title = escapeHtml(row.candidate || row.title || row.id);
    const subtitle = escapeHtml(row.title || "");
    const url = row.url ? `<a href="${escapeHtml(row.url)}" target="_blank" rel="noopener">${title}</a>` : `<strong>${title}</strong>`;
    return `<tr>
      <td>${url}<br><span class="subtle">${subtitle}</span><br><span class="subtle">${escapeHtml(row.id || "")}</span></td>
      <td>${escapeHtml(row.disease || "")}</td>
      <td><span class="badge">${escapeHtml(row.phase || "Unknown")}</span></td>
      <td>${escapeHtml(row.platform || "Unclassified")}</td>
      <td>${escapeHtml(row.overall_status || "")}</td>
      <td>${escapeHtml(row.lead_sponsor || "")}</td>
      <td>${escapeHtml((row.countries || []).join(", "))}</td>
      <td>${fmtDate(row.last_update)}</td>
    </tr>`;
  }).join("");
}

function renderRecentAndSources() {
  const recent = document.getElementById("recentList");
  const recentRows = (summary.recent_updates || pipeline.slice().sort((a,b)=>String(b.last_update).localeCompare(String(a.last_update))).slice(0, 12));
  recent.innerHTML = recentRows.slice(0, 12).map(r => `<li>${r.url ? `<a href="${escapeHtml(r.url)}" target="_blank" rel="noopener">${escapeHtml(r.candidate || r.title)}</a>` : escapeHtml(r.candidate || r.title)}<br><span class="subtle">${escapeHtml(r.disease)} · ${escapeHtml(r.phase)} · updated ${fmtDate(r.last_update)}</span></li>`).join("");

  const sourceList = document.getElementById("sourceList");
  sourceList.innerHTML = (summary.sources || []).map(s => `<li><a href="${escapeHtml(s.url)}" target="_blank" rel="noopener">${escapeHtml(s.name)}</a><br><span class="subtle">${escapeHtml(s.status || "")}</span></li>`).join("");
}

function applyFilters() {
  const q = document.getElementById("searchBox").value.trim().toLowerCase();
  const disease = document.getElementById("diseaseFilter").value;
  const phase = document.getElementById("phaseFilter").value;
  const platform = document.getElementById("platformFilter").value;
  filtered = pipeline.filter(r => {
    const text = [r.id, r.title, r.official_title, r.candidate, r.disease, r.platform, r.lead_sponsor, (r.countries || []).join(" "), (r.interventions || []).join(" ")].join(" ").toLowerCase();
    return (!q || text.includes(q)) && (!disease || r.disease === disease) && (!phase || r.phase === phase) && (!platform || r.platform === platform);
  });
  updateKpis(); renderCharts(); renderTable();
}

function downloadCsv() {
  const headers = ["id","candidate","title","disease","phase","platform","overall_status","lead_sponsor","countries","last_update","url"];
  const csv = [headers.join(",")].concat(filtered.map(row => headers.map(h => {
    const v = Array.isArray(row[h]) ? row[h].join("; ") : (row[h] ?? "");
    return `"${String(v).replaceAll('"','""')}"`;
  }).join(","))).join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = "vaccine_pipeline_filtered.csv"; a.click();
  URL.revokeObjectURL(url);
}

async function init() {
  const [p, s] = await Promise.all([fetch("data/pipeline.json"), fetch("data/summary.json")]);
  pipeline = await p.json();
  summary = await s.json();
  filtered = pipeline.slice();
  document.getElementById("lastUpdated").textContent = fmtDate(summary.generated_at);
  fillSelect("diseaseFilter", uniqueSorted("disease"));
  fillSelect("phaseFilter", uniqueSorted("phase"));
  fillSelect("platformFilter", uniqueSorted("platform"));
  ["searchBox", "diseaseFilter", "phaseFilter", "platformFilter"].forEach(id => document.getElementById(id).addEventListener("input", applyFilters));
  document.getElementById("resetBtn").addEventListener("click", () => {
    document.getElementById("searchBox").value = "";
    document.getElementById("diseaseFilter").value = "";
    document.getElementById("phaseFilter").value = "";
    document.getElementById("platformFilter").value = "";
    applyFilters();
  });
  document.getElementById("downloadCsv").addEventListener("click", downloadCsv);
  updateKpis(); renderCharts(); renderTable(); renderRecentAndSources();
}

init().catch(err => {
  console.error(err);
  document.body.insertAdjacentHTML("afterbegin", `<div style="padding:1rem;background:#fff3cd;color:#7a4b00">Failed to load dashboard data: ${escapeHtml(err.message)}</div>`);
});
