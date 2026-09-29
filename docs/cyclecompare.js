/**
 * cyclecompare.js — cycle-over-cycle comparison for three Overview charts.
 *
 *   Who Funds Oregon Campaigns  → donor-type composition, this cycle vs a past one
 *   Who Funds Oregon's Parties  → party composition, same treatment
 *   Monthly Cash Flow           → cumulative raised, aligned by month-in-cycle
 *
 * Each chart keeps its existing "Trend" view and gains a "Compare" view; the
 * originals are untouched so the filter toolbar keeps working as before.
 *
 * A cycle is Dec of the pre-election year → Nov of the election year, the same
 * definition the cycle preset buttons use.
 */
"use strict";

const CC_MONTHS = 24;                       // a full cycle
const CC_PALETTE = ["#2a78d6", "#eb6834"];  // validated categorical slots 1 & 2

/** ["YYYY-MM", …] for the cycle ending in `year`, in order. */
function ccCycleMonths(year) {
  const out = [];
  let y = year - 2, m = 12;
  for (let i = 0; i < CC_MONTHS; i++) {
    out.push(`${y}-${String(m).padStart(2, "0")}`);
    m += 1;
    if (m > 12) { m = 1; y += 1; }
  }
  return out;
}

function ccCurrentCycle() {
  const now = new Date();
  let y = now.getFullYear();
  if (y % 2 !== 0) y += 1;
  else if (now.getMonth() >= 11) y += 2;
  return y;
}

/**
 * How many months of the current cycle the data actually covers.
 *
 * Counted rather than looked up, because the newest month is often not IN the
 * window. Every December the cycle rolls over while reporting still ends in
 * November, and an exact lookup misses — a fallback of "the whole cycle" would
 * then compare a cycle that has barely started against a finished one and
 * label it complete. Floors at 1 so the comparison is visibly empty instead.
 */
function ccElapsed(year, latestMonth) {
  const months = ccCycleMonths(year);
  const n = months.filter(m => m <= latestMonth).length;
  return Math.min(Math.max(n, 1), months.length);
}

const ccFmt$ = v => "$" + Math.round(v).toLocaleString("en-US");
const ccPct = v => `${v.toFixed(1)}%`;

// ── Composition (donor type / party) ────────────────────────────────────────

/** {type: total} over a cycle, from a {month: [{type,total}]} blob. */
function ccSumByType(byMonth, year) {
  const want = new Set(ccCycleMonths(year));
  const out = {};
  for (const [m, rows] of Object.entries(byMonth || {})) {
    if (!want.has(m)) continue;
    for (const r of rows || []) {
      // Merge the "(out of state)" variants — composition is about who gives,
      // not where they live; the trend chart already splits that out.
      const t = (r.type || "").replace(/ \(out of state\)$/, "");
      out[t] = (out[t] || 0) + (r.total || 0);
    }
  }
  return out;
}

/** Percentage composition, sorted by the past cycle's share. `sum` is year → {type: total}. */
function ccComposition(sum_, cycleA, cycleB) {
  const a = sum_(cycleA);
  const b = sum_(cycleB);
  const sum = o => Object.values(o).reduce((x, y) => x + y, 0) || 1;
  const [ta, tb] = [sum(a), sum(b)];
  const types = [...new Set([...Object.keys(a), ...Object.keys(b)])]
    .sort((x, y) => (b[y] || 0) / tb - (b[x] || 0) / tb);
  return {
    types,
    a: types.map(t => 100 * (a[t] || 0) / ta),
    b: types.map(t => 100 * (b[t] || 0) / tb),
    rawA: types.map(t => a[t] || 0),
    rawB: types.map(t => b[t] || 0),
  };
}

function ccRenderComposition(elId, sum_, cycleA, cycleB, elapsedNote) {
  const el = document.getElementById(elId);
  if (!el) return;
  const { types, a, b, rawA, rawB } = ccComposition(sum_, cycleA, cycleB);
  const inst = echarts.getInstanceByDom(el);
  if (inst) inst.dispose();
  const chart = echarts.init(el, null, { renderer: "svg" });
  chart.setOption({
    grid: { left: 178, right: 40, top: 30, bottom: 24 },
    legend: { top: 0, data: [`${cycleA} cycle${elapsedNote}`, `${cycleB} cycle`] },
    tooltip: {
      trigger: "axis", axisPointer: { type: "shadow" },
      formatter: p => p.map((x, i) =>
        `${x.marker}${x.seriesName}: <b>${ccPct(x.value)}</b> ` +
        `(${ccFmt$(x.seriesIndex === 0 ? rawA[x.dataIndex] : rawB[x.dataIndex])})`).join("<br/>"),
    },
    xAxis: { type: "value", axisLabel: { formatter: "{value}%", color: "#718096" },
             splitLine: { lineStyle: { color: "#edf2f7" } } },
    yAxis: { type: "category", data: types, inverse: true,
             axisLabel: { color: "#4a5568" }, axisLine: { lineStyle: { color: "#e2e8f0" } } },
    series: [
      { name: `${cycleA} cycle${elapsedNote}`, type: "bar", data: a,
        itemStyle: { color: CC_PALETTE[0], borderRadius: [0, 4, 4, 0] }, barGap: "10%" },
      { name: `${cycleB} cycle`, type: "bar", data: b,
        itemStyle: { color: CC_PALETTE[1], borderRadius: [0, 4, 4, 0] } },
    ],
  });
  chart.resize();
}

// ── Cash flow, aligned by month-in-cycle ────────────────────────────────────

function ccCumulative(timeline, year, limit) {
  const months = ccCycleMonths(year);
  const by = {};
  for (const e of timeline || []) by[e.month] = (by[e.month] || 0) + (e.contributions || 0);
  let run = 0;
  return months.slice(0, limit).map(m => (run += by[m] || 0));
}

function ccRenderCashFlow(elId, timeline, cycleA, cycleB, elapsed) {
  const el = document.getElementById(elId);
  if (!el) return;
  const inst = echarts.getInstanceByDom(el);
  if (inst) inst.dispose();
  const chart = echarts.init(el, null, { renderer: "svg" });

  const aCum = ccCumulative(timeline, cycleA, elapsed);          // this cycle so far
  const bCum = ccCumulative(timeline, cycleB, elapsed);          // same point last time
  const bFull = ccCumulative(timeline, cycleB, CC_MONTHS);       // where it ended

  chart.setOption({
    grid: { left: 76, right: 24, top: 30, bottom: 36 },
    legend: { top: 0, data: [`${cycleA} so far`, `${cycleB} same point`, `${cycleB} full cycle`] },
    tooltip: { trigger: "axis", valueFormatter: ccFmt$ },
    xAxis: {
      type: "category",
      data: Array.from({ length: CC_MONTHS }, (_, i) => `M${i + 1}`),
      axisLabel: { color: "#718096" }, axisLine: { lineStyle: { color: "#e2e8f0" } },
    },
    yAxis: {
      type: "value", splitLine: { lineStyle: { color: "#edf2f7" } },
      axisLabel: { color: "#718096",
        formatter: v => v >= 1e6 ? `$${(v / 1e6).toFixed(0)}M`
                            : v > 0 ? `$${Math.round(v / 1e3)}K` : "$0" },
    },
    series: [
      // Faded first so the like-for-like pair reads on top of it.
      { name: `${cycleB} full cycle`, type: "line", data: bFull, smooth: false,
        lineStyle: { width: 2, color: CC_PALETTE[1], opacity: 0.28 },
        itemStyle: { color: CC_PALETTE[1], opacity: 0.28 }, symbol: "none" },
      { name: `${cycleB} same point`, type: "line", data: bCum, smooth: false,
        lineStyle: { width: 2, color: CC_PALETTE[1] },
        itemStyle: { color: CC_PALETTE[1], borderColor: "#fff", borderWidth: 2 }, symbolSize: 8 },
      { name: `${cycleA} so far`, type: "line", data: aCum, smooth: false,
        lineStyle: { width: 2, color: CC_PALETTE[0] },
        itemStyle: { color: CC_PALETTE[0], borderColor: "#fff", borderWidth: 2 }, symbolSize: 8 },
    ],
  });
  chart.resize();
}

// ── Wiring ──────────────────────────────────────────────────────────────────

let ccWired = false;

/**
 * What the comparison charts are comparing.
 *
 *   statewide — every committee; all three charts
 *   filer     — one committee's own money; donor mix and cash flow only, since
 *               "who funds the parties" is not a question about one committee
 *   none      — several committees at once; no comparison at all, because a
 *               single pair of bars cannot say which of them moved
 */
let ccScope = { mode: "statewide", profile: null };

const ccByMonth = () => ccScope.mode === "filer"
  ? (ccScope.profile.by_contributor_type_by_month || {})
  : ((typeof byTypeDataGlobal !== "undefined" && byTypeDataGlobal || {}).by_month || {});

const ccTimeline = () => ccScope.mode === "filer"
  ? (ccScope.profile.timeline || [])
  : (typeof timelineData !== "undefined" ? timelineData || [] : []);

/** Controls this scope supports, by control id. */
const CC_IN_SCOPE = {
  statewide: ["cc-donortype", "cc-party", "cc-cash"],
  filer:     ["cc-donortype", "cc-cash"],
  none:      [],
};

/**
 * Point the comparisons at a committee, the whole state, or nothing.
 *
 * Controls that fall out of scope snap back to Trend before hiding, so the
 * chart on screen is always the one the visible buttons describe. Controls
 * that stay in scope re-render, since the underlying numbers just changed.
 */
function ccSetScope(mode, profile) {
  ccScope = { mode, profile: profile || null };
  const allowed = CC_IN_SCOPE[mode] || [];
  for (const id of ["cc-donortype", "cc-party", "cc-cash"]) {
    const sel = document.getElementById(id);
    const bar = sel && sel.closest(".cc-bar");
    if (!bar) continue;
    const ok = allowed.includes(id);
    if (!ok) {
      const trend = bar.querySelector('button[data-v="trend"]');
      if (trend && !trend.classList.contains("active")) trend.click();
    } else if (!sel.hidden) {
      sel.dispatchEvent(new Event("change"));   // same cycle, new numbers
    }
    bar.hidden = !ok;
  }
}

function ccBuildControl(boxSel, id, onPick) {
  const box = document.querySelector(boxSel);
  if (!box || box.querySelector(`#${id}`)) return null;
  const cur = ccCurrentCycle();
  const opts = [cur - 2, cur - 4, cur - 6]
    .map(y => `<option value="${y}">vs ${y} cycle</option>`).join("");
  const bar = document.createElement("div");
  bar.className = "cc-bar";
  bar.innerHTML = `
    <div class="cmp-toggle" id="${id}-mode">
      <button data-v="trend" class="active">Trend</button>
      <button data-v="compare">Compare cycles</button>
    </div>
    <select id="${id}" class="cc-select" hidden>${opts}</select>`;
  const h2 = box.querySelector("h2");
  (h2 ? h2.parentNode : box).insertBefore(bar, h2 ? h2.nextSibling : box.firstChild);

  const sel = bar.querySelector(`#${id}`);
  bar.querySelector(`#${id}-mode`).addEventListener("click", e => {
    const b = e.target.closest("button[data-v]");
    if (!b) return;
    bar.querySelectorAll(`#${id}-mode button`).forEach(x => x.classList.toggle("active", x === b));
    const compare = b.dataset.v === "compare";
    sel.hidden = !compare;
    onPick(compare ? +sel.value : null);
  });
  sel.addEventListener("change", () => onPick(+sel.value));
  return sel;
}

function initCycleCompare() {
  // renderTimeline() runs on every filter change; the controls are built once.
  if (ccWired) { ccSetScope("statewide"); return; }
  ccWired = true;

  const cur = ccCurrentCycle();
  const latest = (timelineData || []).map(r => r.month).sort().pop() || "";
  const elapsed = ccElapsed(cur, latest);
  const note = ` (first ${elapsed} mo)`;

  /** Swap the trend chart for the comparison chart, or back. */
  const swap = (origId, hostId, cycle, draw) => {
    const orig = document.getElementById(origId);
    const host = document.getElementById(hostId);
    if (!host) return;
    host.hidden = !cycle;
    if (orig) orig.hidden = !!cycle;
    if (cycle) draw();
    // A chart sized while display:none comes back 0×0, so re-measure on reveal.
    const shown = cycle ? host : orig;
    const inst = shown && echarts.getInstanceByDom(shown);
    if (inst) inst.resize();
  };

  ccBuildControl("#overview-donut-box", "cc-donortype", cycle => {
    swap("chart-contributor-type", "cc-donortype-chart", cycle, () =>
      ccRenderComposition("cc-donortype-chart",
        y => ccSumByType(ccByMonth(), y), cur, cycle, note));
    // That legend's dollar figures are all-time; they don't belong to a
    // two-cycle comparison and would be read as if they did.
    const tot = document.querySelector("#overview-donut-box .stacked-area-legend");
    if (tot) tot.hidden = !!cycle;
  });

  ccBuildControl("#party-fundraising-box", "cc-party", cycle =>
    swap("chart-party-fundraising", "cc-party-chart", cycle, () =>
      ccRenderParty("cc-party-chart", cur, cycle)));

  ccBuildControl("#overview-timeline-box", "cc-cash", cycle =>
    swap("chart-timeline", "cc-cash-chart", cycle, () =>
      ccRenderCashFlow("cc-cash-chart", ccTimeline(), cur, cycle, elapsed)));

  window.addEventListener("resize", () => {
    ["cc-donortype-chart", "cc-party-chart", "cc-cash-chart"].forEach(id => {
      const el = document.getElementById(id);
      const c = el && !el.hidden && echarts.getInstanceByDom(el);
      if (c) c.resize();
    });
  });
}

// ── Party composition ───────────────────────────────────────────────────────

// Party hue carries identity, the step within it carries the cycle. These are
// a shade darker than the trend chart's pastels: there the light step sits on
// top of its dark partner in a stack, here each bar stands alone on the page
// surface, so it has to clear 3:1 on its own. All four pass the validator.
const CC_PARTY_COLOR = {
  Democrat:   ["#1d4ed8", "#5b8ae0"],
  Republican: ["#b91c1c", "#d9694f"],
};

/**
 * Who gives to one party's committees, summed over a cycle.
 *
 * by_party_type is keyed by YEAR, not month, so a cycle is approximated as the
 * two calendar years it mostly covers (2025 + 2026 for the 2026 cycle). The
 * true window starts one month earlier; splitting a yearly figure across
 * months to recover that December would be inventing precision the source
 * doesn't have, and it cannot move a share-of-total read meaningfully.
 */
function ccPartySums(party, year) {
  const src = (window.partyTypeData && window.partyTypeData.by_year) || {};
  const out = {};
  for (const yr of [year - 1, year]) {
    for (const r of (src[String(yr)] || {})[party] || []) {
      const t = (r.type || "").replace(/ \(out of state\)$/, "");
      out[t] = (out[t] || 0) + (r.total || 0);
    }
  }
  return out;
}

/**
 * Both parties, both cycles, on one donor-type axis.
 *
 * Each bar is a share of *that party's* own total, so the question it answers
 * is "what does this party's money look like, and has the mix moved?" — not
 * which party raised more, which the trend chart already shows in dollars.
 */
function ccRenderParty(elId, cycleA, cycleB) {
  const el = document.getElementById(elId);
  if (!el) return;
  const parties = Object.keys(CC_PARTY_COLOR);
  const sums = {};
  for (const p of parties) for (const y of [cycleA, cycleB]) sums[`${p}|${y}`] = ccPartySums(p, y);

  const totals = {};
  for (const [k, o] of Object.entries(sums)) totals[k] = Object.values(o).reduce((a, b) => a + b, 0) || 1;
  const types = [...new Set(Object.values(sums).flatMap(Object.keys))]
    .sort((x, y) => (sums[`Democrat|${cycleB}`][y] || 0) - (sums[`Democrat|${cycleB}`][x] || 0));

  // Four bars per row need room; a fixed 300px box would crush them.
  el.style.height = Math.max(320, types.length * 62) + "px";
  const inst = echarts.getInstanceByDom(el);
  if (inst) inst.dispose();
  const chart = echarts.init(el, null, { renderer: "svg" });

  const series = [];
  for (const p of parties) {
    [cycleA, cycleB].forEach((yr, i) => {
      const k = `${p}|${yr}`;
      series.push({
        name: `${p.slice(0, 3)} ${yr}${i === 0 ? " so far" : ""}`,
        type: "bar",
        data: types.map(t => 100 * (sums[k][t] || 0) / totals[k]),
        itemStyle: { color: CC_PARTY_COLOR[p][i], borderRadius: [0, 4, 4, 0] },
      });
    });
  }

  chart.setOption({
    grid: { left: 178, right: 40, top: 56, bottom: 24 },
    legend: { top: 0, itemGap: 14 },
    // Say plainly what the window is — the source is yearly, not monthly.
    graphic: [{ type: "text", left: 0, top: 30, silent: true,
      style: { text: `Share of each party's own total · cycle read as its two calendar years`,
               fill: "#718096", fontSize: 11 } }],
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" },
               valueFormatter: v => ccPct(v || 0) },
    xAxis: { type: "value", axisLabel: { formatter: "{value}%", color: "#718096" },
             splitLine: { lineStyle: { color: "#edf2f7" } } },
    yAxis: { type: "category", data: types, inverse: true,
             axisLabel: { color: "#4a5568" }, axisLine: { lineStyle: { color: "#e2e8f0" } } },
    series,
  });
  chart.resize();
}
