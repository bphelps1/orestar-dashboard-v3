/**
 * compare.js — historical cycle comparison for the Overview tab.
 *
 * Compares the marquee races against prior cycles:
 *   • Governor, by party
 *   • Speaker, Senate President and both Majority Leaders, split by tenure
 *     (assets/leadership_tenures.json)
 *   • the four legislative caucus PACs
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
  "House Majority Leader": "#eb6834",    // slot 2
  "Senate Majority Leader": "#eda100",   // slot 4
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
let leadershipTenures = null;

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

const LEADER_POSTS = [
  "Speaker of the House", "House Majority Leader",
  "Senate President", "Senate Majority Leader",
];

/**
 * Dated holders of the four leadership posts (assets/leadership_tenures.json).
 *
 * The file is the record; the filer index is only a tripwire. filer_index
 * carries leadership_role from the weekly leadership refresh, so when someone
 * new holds a post the file does not know about yet, say so in the console
 * instead of guessing when they took over.
 */
async function loadLeadership() {
  if (leadershipTenures) return leadershipTenures;
  try {
    const r = await fetch("assets/leadership_tenures.json");
    leadershipTenures = r.ok ? await r.json() : { tenures: [] };
  } catch { leadershipTenures = { tenures: [] }; }

  const today = new Date().toISOString().slice(0, 10);
  for (const f of (typeof filerIndex !== "undefined" && filerIndex) || []) {
    const pos = LEADER_POSTS.find(p => p.toLowerCase() === (f.leadership_role || "").trim().toLowerCase());
    if (!pos) continue;
    const now = holdersOn(leadershipTenures.tenures, pos, today);
    if (!now.some(t => t.slug === f.slug)) {
      console.warn(`[compare] ${f.name} now holds ${pos}; add the date to assets/leadership_tenures.json`);
    }
  }
  return leadershipTenures;
}

/**
 * Who held `position` on `day` ("YYYY-MM-DD"): every entry sharing the latest
 * start on or before it, so co-Speakers both come back. A vacancy entry
 * (candidate null) comes back as [].
 */
function holdersOn(tenures, position, day) {
  let best = undefined;
  for (const t of tenures) {
    if (t.position !== position || (t.start !== null && t.start > day)) continue;
    if (best === undefined || (t.start || "") > (best || "")) best = t.start;
  }
  if (best === undefined) return [];
  return tenures.filter(t => t.position === position && t.start === best && t.candidate);
}

/**
 * One cycle of a post: {total, parts:[{candidate, start, months, amount}]}.
 *
 * Leaders change mid-cycle often (the Senate had three majority leaders in
 * 2024), so a cycle is not credited to one person. Each month goes to whoever
 * held the post on its 15th, and only that holder's money for that month
 * counts. Money raised before taking the post, or for a statewide run after
 * leaving it, stays out.
 */
function tenureCycle(tenures, position, year, timelines) {
  const { start, end } = cycleWindow(year);
  const parts = new Map();
  let total = 0;
  for (let y = +start.slice(0, 4), m = +start.slice(5); ; ) {
    const month = `${y}-${String(m).padStart(2, "0")}`;
    if (month > end) break;
    for (const h of holdersOn(tenures, position, `${month}-15`)) {
      const amt = (timelines[h.slug] || [])
        .filter(e => e.month === month).reduce((a, e) => a + (e.contributions || 0), 0);
      const p = parts.get(h.candidate) || { candidate: h.candidate, start: h.start, months: 0, amount: 0 };
      p.months += 1; p.amount += amt; total += amt;
      parts.set(h.candidate, p);
    }
    if (++m > 12) { m = 1; y += 1; }
  }
  return { total, parts: [...parts.values()] };
}

/**
 * "Kate Lieber 18 mo, $256,225 → Kathleen Taylor 5 mo, $347,768". Successive
 * holders are joined by an arrow; co-holders, who share a start, by "&".
 */
function tenureLabel(parts, year) {
  if (parts.length === 1) return parts[0].candidate;
  const out = [];
  parts.forEach((p, i) => {
    const text = `${p.candidate} ${p.months} mo, ${cmpFmt$(deflate(p.amount, year))}`;
    if (i && p.start === parts[i - 1].start) out[out.length - 1] += ` & ${text}`;
    else out.push(text);
  });
  return out.join(" → ");
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
    const lt = await loadLeadership();
    const tl = await fetchTimelines([...new Set(lt.tenures.filter(t => t.slug).map(t => t.slug))]);
    // Each chamber's pair sits together in the legend; dashed = majority leader.
    return {
      cycles,
      series: LEADER_POSTS.map(pos => {
        const first = (lt.first_cycle || {})[pos] || 0;
        const per = cycles.map(y => y < first ? null : tenureCycle(lt.tenures, pos, y, tl));
        return {
          name: pos,
          color: CMP_COLORS[pos],
          dashed: pos.endsWith("Majority Leader"),
          // null, not 0: before the history starts there is nothing to plot.
          data: per.map((c, i) => c ? Math.round(deflate(c.total, cycles[i])) : null),
          labels: per.map((c, i) => c ? tenureLabel(c.parts, cycles[i]) : ""),
        };
      }),
    };
  }

  // Caucus PACs: one series per caucus, following it across committees (the
  // House Republicans have used three). CAUCUS_PACS lives in caucusdonors.js.
  // Party carries the hue; Senate is dashed, matching the Leadership view.
  const slugs = [...new Set(CAUCUS_PACS.flatMap(c => cycles.map(y => c.committee(y))))];
  const tl = await fetchTimelines(slugs);
  const name = slug => ((filerIndex || []).find(f => f.slug === slug) || {}).name || slug;
  return {
    cycles,
    series: CAUCUS_PACS.map(c => ({
      name: c.caucus,
      color: CAUCUS_COLOR[c.party],
      dashed: c.caucus.startsWith("Senate"),
      data: cycles.map(y => Math.round(deflate(sumCycle(tl[c.committee(y)], y), y))),
      labels: cycles.map(y => name(c.committee(y))),
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
    // No fixed icon: each entry draws its series' own line, so a dashed series
    // is dashed in the legend too, and same-hue pairs stay apart there.
    legend: { data: series.map(s => s.name), top: 0, itemWidth: 28,
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
      extraCssText: "max-width:360px;white-space:normal",
      // Name who held the post, so a mid-cycle hand-off is visible on hover.
      formatter: ps => [ps[0] && ps[0].axisValue].concat(ps
        .filter(p => p.value !== null && p.value !== undefined)
        .map(p => {
          const s = series[p.seriesIndex];
          const who = s.labels && s.labels[p.dataIndex];
          return `${p.marker}${p.seriesName}: <b>${cmpFmt$(p.value)}</b>` +
            (who ? `<br/><span style="color:#718096;font-size:11px;margin-left:14px">${esc(who)}</span>` : "");
        })).join("<br/>"),
    },
    series: series.map(s => ({
      name: s.name, type: "line", data: s.data,
      smooth: false,
      // Dashed = majority leader / Senate caucus, so pairs read apart without colour.
      lineStyle: { width: 2, color: s.color, type: s.dashed ? "dashed" : "solid" },  // 2px lines per spec
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
    const setNote = document.getElementById("cmp-set-note");
    if (setNote) setNote.hidden = cmpSeriesSet !== "leadership";
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
