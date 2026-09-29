/**
 * compare.js — historical cycle comparison for the Overview tab.
 *
 * Compares the marquee races against prior cycles:
 *   • Governor, by party
 *   • Speaker of the House / Senate President (data/leadership_history.json)
 *   • Future PAC, House Builders and SDLF
 *
 * A cycle runs Dec of the pre-election year → Nov of the election year, the
 * same window the Overview cycle presets use, so the two agree.
 *
 * Toggles: real vs nominal dollars, and which cycle to highlight.
 */
"use strict";

// Categorical slots from the validated reference palette (fixed order, never
// cycled). Blue/red land on the party series, which is also the conventional
// reading; identity is still carried by the legend and direct labels, never by
// colour alone.
const CMP_COLORS = {
  Democrat: "#2a78d6",     // slot 1
  Republican: "#e34948",   // slot 8
  Other: "#eda100",        // slot 4
  "Speaker of the House": "#2a78d6",
  "Senate President": "#1baf7a",   // slot 3
  "Future PAC, House Builders": "#2a78d6",
  "SDLF": "#eb6834",       // slot 2
};

// CPI-U, annual average, US city average (1982-84 = 100).
// Published values through 2024; 2025–2026 are estimates and are labelled as
// such in the UI so a "real dollars" figure is never quietly presented as
// firmer than it is.
const CPI = {
  2006: 201.6, 2008: 215.303, 2010: 218.056, 2012: 229.594, 2014: 236.736,
  2016: 240.007, 2018: 251.107, 2020: 258.811, 2022: 292.655, 2024: 313.689,
  2026: 327.0,
};
const CPI_ESTIMATED_FROM = 2025;
const CPI_BASE_YEAR = 2024;          // "real" figures are in 2024 dollars

let cmpChart = null;
let cmpMode = "nominal";             // 'nominal' | 'real'
let cmpSeriesSet = "governor";       // 'governor' | 'leadership' | 'caucus'
let leadershipHistory = null;

const cmpFmt$ = v => "$" + Math.round(v).toLocaleString("en-US");

function cycleWindow(year) {
  return { start: `${year - 2}-12`, end: `${year}-11` };   // YYYY-MM
}

function deflate(amount, year) {
  if (cmpMode !== "real") return amount;
  const cpi = CPI[year];
  if (!cpi) return amount;
  return amount * (CPI[CPI_BASE_YEAR] / cpi);
}

/** Sum a filer_detail timeline over one cycle window. */
function sumCycle(timeline, year) {
  const { start, end } = cycleWindow(year);
  let total = 0;
  for (const e of timeline || []) {
    const m = e.month || "";
    if (m >= start && m <= end) total += e.contributions || 0;
  }
  return total;
}

async function fetchTimelines(slugs) {
  if (!slugs.length) return {};
  const sb = await getSupabase();
  const { data, error } = await sb
    .from("filer_detail")
    .select("slug, detail")
    .in("slug", slugs);
  if (error) { console.warn("[compare]", error.message); return {}; }
  const out = {};
  for (const r of data || []) out[r.slug] = (r.detail || {}).timeline || [];
  return out;
}

/** The cycle currently under way (same rule as the Overview cycle presets). */
function currentCycleYear() {
  const now = new Date();
  let y = now.getFullYear();
  if (y % 2 !== 0) y += 1;
  else if (now.getMonth() >= 11) y += 2;
  return y;
}

/**
 * Current Speaker / Senate President, read live from the filer index.
 *
 * filer_index carries leadership_role, populated from data/leadership_roles.json
 * and refreshed weekly by leadership-refresh.yml — so when the Speaker or
 * President changes, this chart follows without anyone editing a file. The
 * supplied spreadsheet only runs through 2024, which is why the current cycle
 * has to come from live data rather than the history.
 */
function currentLeadersFromIndex(cycle) {
  const WANT = {
    "speaker of the house": "Speaker of the House",
    "senate president": "Senate President",
  };
  const out = [];
  for (const f of (typeof filerIndex !== "undefined" && filerIndex) || []) {
    const pos = WANT[(f.leadership_role || "").trim().toLowerCase()];
    if (!pos || !f.slug) continue;
    out.push({
      cycle, position: pos,
      candidate: f.candidate_name || f.name,
      slug: f.slug, committee: f.name, match: "live",
    });
  }
  return out;
}

async function loadLeadership() {
  if (leadershipHistory) return leadershipHistory;
  try {
    // Served from docs/assets — data/ at the repo root is not web-served.
    const r = await fetch("assets/leadership_history.json");
    leadershipHistory = r.ok ? await r.json() : { leaders: [] };
  } catch { leadershipHistory = { leaders: [] }; }

  // Fold in whoever currently holds these roles, for any cycle the historical
  // file doesn't cover yet.
  const cycle = currentCycleYear();
  const have = new Set(leadershipHistory.leaders
    .filter(l => l.cycle === cycle).map(l => l.position));
  for (const l of currentLeadersFromIndex(cycle)) {
    if (!have.has(l.position)) leadershipHistory.leaders.push(l);
  }
  return leadershipHistory;
}

/** Build {cycles:[], series:[{name, color, data:[]}]} for the active toggle. */
async function buildCompareSeries() {
  const cycles = [2010, 2012, 2014, 2016, 2018, 2020, 2022, 2024, 2026];

  if (cmpSeriesSet === "governor") {
    // Governor candidates by party, per cycle, from the filer index.
    const govs = (filerIndex || []).filter(f =>
      f.committee_type === "Candidate Committee" && f.office === "Governor" && f.slug);
    const tl = await fetchTimelines(govs.map(f => f.slug));
    const byParty = { Democrat: [], Republican: [] };
    for (const party of Object.keys(byParty)) {
      byParty[party] = cycles.map(y => {
        // Total raised in that cycle by all of the party's gubernatorial
        // committees — captures the race, not one candidate's committee.
        let t = 0;
        for (const f of govs) {
          if ((f.party || "") !== party) continue;
          t += sumCycle(tl[f.slug], y);
        }
        return Math.round(deflate(t, y));
      });
    }
    return {
      cycles,
      series: Object.entries(byParty).map(([name, data]) =>
        ({ name, color: CMP_COLORS[name], data })),
    };
  }

  if (cmpSeriesSet === "leadership") {
    const lh = await loadLeadership();
    const slugs = [...new Set(lh.leaders.filter(l => l.slug).map(l => l.slug))];
    const tl = await fetchTimelines(slugs);
    const positions = ["Speaker of the House", "Senate President"];
    return {
      cycles,
      series: positions.map(pos => ({
        name: pos,
        color: CMP_COLORS[pos],
        // Co-Speakers (2012) are summed — the source lists both holders.
        data: cycles.map(y => Math.round(deflate(
          lh.leaders.filter(l => l.cycle === y && l.position === pos && l.slug)
                    .reduce((a, l) => a + sumCycle(tl[l.slug], y), 0), y))),
        labels: cycles.map(y =>
          lh.leaders.filter(l => l.cycle === y && l.position === pos)
                    .map(l => l.candidate).join(" & ")),
      })),
    };
  }

  // Caucus committees
  // Match on the full name: a loose prefix also caught "Future Portland PAC".
  const wanted = ["future pac, house builders", "sdlf"];
  const rows = (filerIndex || []).filter(f => {
    const n = (f.name || "").toLowerCase();
    return wanted.some(w => n === w || n.startsWith(w));
  });
  const tl = await fetchTimelines(rows.map(f => f.slug));
  return {
    cycles,
    series: rows.slice(0, 4).map((f, i) => ({
      name: f.name,
      color: CMP_COLORS[f.name] || ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"][i],
      data: cycles.map(y => Math.round(deflate(sumCycle(tl[f.slug], y), y))),
    })),
  };
}

async function renderCompareChart() {
  const el = document.getElementById("cmp-chart");
  if (!el) return;
  const { cycles, series } = await buildCompareSeries();

  if (cmpChart) cmpChart.dispose();
  cmpChart = echarts.init(el, null, { renderer: "svg" });
  cmpChart.setOption({
    grid: { left: 76, right: 24, top: 28, bottom: 40 },
    // Legend always present for >= 2 series, so identity is never colour-alone
    legend: { data: series.map(s => s.name), top: 0, icon: "roundRect",
              textStyle: { color: "#4a5568" } },
    xAxis: {
      type: "category", data: cycles.map(String),
      axisLine: { lineStyle: { color: "#e2e8f0" } },
      axisLabel: { color: "#4a5568" },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: "#edf2f7" } },   // recessive grid
      axisLabel: {
        color: "#718096",
        formatter: v => v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : `$${Math.round(v / 1e3)}K`,
      },
    },
    tooltip: {
      trigger: "axis",
      valueFormatter: cmpFmt$,
      extraCssText: "max-width:320px;white-space:normal",
    },
    series: series.map(s => ({
      name: s.name, type: "line", data: s.data,
      smooth: false,
      lineStyle: { width: 2, color: s.color },       // 2px lines per spec
      itemStyle: { color: s.color, borderColor: "#fff", borderWidth: 2 },
      symbolSize: 9,                                  // >= 8px markers
      emphasis: { focus: "series" },
    })),
  });
  cmpChart.resize();
}

function initCompare() {
  const modeBox = document.getElementById("cmp-mode");
  const setBox = document.getElementById("cmp-set");
  if (!modeBox || !setBox) return;

  setBox.addEventListener("click", e => {
    const b = e.target.closest("button[data-set]");
    if (!b) return;
    cmpSeriesSet = b.dataset.set;
    setBox.querySelectorAll("button").forEach(x => x.classList.toggle("active", x === b));
    renderCompareChart();
  });

  modeBox.addEventListener("click", e => {
    const b = e.target.closest("button[data-mode]");
    if (!b) return;
    cmpMode = b.dataset.mode;
    modeBox.querySelectorAll("button").forEach(x => x.classList.toggle("active", x === b));
    const note = document.getElementById("cmp-note");
    if (note) {
      note.textContent = cmpMode === "real"
        ? `Real dollars, ${CPI_BASE_YEAR} basis (CPI-U; ${CPI_ESTIMATED_FROM}+ estimated)`
        : "Nominal dollars as reported";
    }
    renderCompareChart();
  });

  window.addEventListener("resize", () => cmpChart && cmpChart.resize());
  renderCompareChart();
}
