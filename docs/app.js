/**
 * app.js — ORESTAR Campaign Finance Dashboard
 *
 * Reads dashboard aggregates and filer details from Supabase and renders:
 *   - Overview: stat cards + donut chart; per-filer or comparison views
 *   - Donors: top donors bar chart + sortable table; filer detail + untapped; pivot compare
 *   - Recipients: top recipients; per-filer top payees
 *   - Timeline: monthly line chart; multi-filer overlay
 *   - Search: fuzzy-searchable recent transactions with filer + date filters
 *
 * No build step. No framework. Works in any modern browser.
 */

"use strict";

// ── ECharts — no global defaults needed (configured per-instance) ────────────
const IS_MOBILE = window.innerWidth <= 768;

/** Tooltip position: constrain horizontally within chart, free vertically */
function tooltipPosition(point, params, dom, rect, size) {
  let x = point[0] + 10;
  if (x + size.contentSize[0] > size.viewSize[0]) x = point[0] - size.contentSize[0] - 10;
  if (x < 0) x = 0;
  return [x, point[1] - size.contentSize[1] / 2];
}

/** Initialize or re-initialize an ECharts instance on a DOM element */
function initEChart(el) {
  let instance = echarts.getInstanceByDom(el);
  if (instance) instance.dispose();
  return echarts.init(el, null, { renderer: 'svg' });
}

/** Resize all active ECharts instances */
window.addEventListener('resize', () => {
  document.querySelectorAll('.echart-container, .echart-container-tall').forEach(el => {
    const instance = echarts.getInstanceByDom(el);
    if (instance) instance.resize();
  });
});

const PALETTE = [
  "#3182ce", "#e53e3e", "#38a169", "#d69e2e", "#805ad5",
  "#dd6b20", "#319795", "#667eea", "#f6ad55",
  "#68d391", "#63b3ed", "#fc8181", "#d6bcfa", "#fbd38d",
];

function hexToRgba(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

// Fixed color map: base type → PALETTE color (consistent across all filter states)
const TYPE_COLOR_MAP = {};
[
  "Individual", "Business Entity", "Political Committee", "Other",
  "Labor Organization", "Candidate & Immediate Family",
  "Unregistered Committee", "Political Party Committee",
].forEach((t, i) => { TYPE_COLOR_MAP[t] = PALETTE[i]; });

// Short display names for donor type labels (used in legends and chart axes)
const TYPE_SHORT_NAMES = {
  "Business Entity": "Business",
  "Political Committee": "Political Cmte",
  "Individual": "Individual",
  "Other": "Other",
  "Labor Organization": "Labor",
  "Candidate & Immediate Family": "Candidate/Family",
  "Unregistered Committee": "Unreg. Cmte",
  "Political Party Committee": "Party Cmte",
};
function shortTypeName(name) { return TYPE_SHORT_NAMES[name] || name; }

function typeColor(label) {
  const base  = label.endsWith(" (out of state)") ? label.slice(0, -15) : label;
  const color = TYPE_COLOR_MAP[base] ?? PALETTE[Object.keys(TYPE_COLOR_MAP).length % PALETTE.length];
  return label.endsWith(" (out of state)") ? hexToRgba(color, 0.45) : color;
}

// ── Chart box DOM helpers ─────────────────────────────────────────────────────

function resetChartBox() {
  const box = document.getElementById("overview-donut-box");
  // Dispose any ECharts instances inside
  box.querySelectorAll('.echart-container, .echart-container-tall, .echart-container-radar').forEach(el => {
    const inst = echarts.getInstanceByDom(el);
    if (inst) inst.dispose();
  });
  // Remove any dynamically added elements (legends, multi-filer divs, radar layout)
  box.querySelectorAll('.stacked-area-legend, .multi-chart-row, .radar-layout').forEach(el => el.remove());
  // Ensure the main chart div exists
  let chartEl = document.getElementById("chart-contributor-type");
  if (!chartEl) {
    chartEl = document.createElement("div");
    chartEl.id = "chart-contributor-type";
    chartEl.className = "echart-container";
    box.appendChild(chartEl);
  }
  chartEl.className = "echart-container";
  chartEl.style.display = "";  // Restore visibility (hidden in multi-filer mode)
}

function buildMultiChartBox(profiles) {
  const box = document.getElementById("overview-donut-box");
  // Dispose existing charts
  box.querySelectorAll('.echart-container, .echart-container-tall').forEach(el => {
    const inst = echarts.getInstanceByDom(el);
    if (inst) inst.dispose();
  });
  // Remove old elements
  const main = document.getElementById("chart-contributor-type");
  if (main) main.remove();
  box.querySelectorAll('.multi-chart-row, .stacked-area-legend').forEach(el => el.remove());

  // Create container for each filer
  const row = document.createElement("div");
  row.className = "multi-chart-row";
  profiles.forEach((p, i) => {
    const unit = document.createElement("div");
    unit.className = "multi-chart-unit";
    const title = document.createElement("div");
    title.className = "multi-chart-title";
    title.textContent = p.name;
    const chartDiv = document.createElement("div");
    chartDiv.id = `chart-type-${i}`;
    chartDiv.className = "echart-container";
    unit.appendChild(title);
    unit.appendChild(chartDiv);
    row.appendChild(unit);
  });
  box.appendChild(row);
}

// ── Stacked Area Chart (Who Funds Oregon Campaigns) ─────────────────────────

function makeStackedAreaChart(containerId, byMonthData, byTypeRows) {
  const el = document.getElementById(containerId);
  if (!el) return null;
  el.className = "echart-container";

  // 1. Compute base types (merge "out of state" variants)
  const baseTypeMap = {};
  const months = Object.keys(byMonthData).filter(m => m >= "2006-01").sort();
  const monthlyBaseData = {};

  for (const month of months) {
    monthlyBaseData[month] = {};
    for (const entry of byMonthData[month]) {
      const base = entry.type.endsWith(" (out of state)") ? entry.type.slice(0, -15) : entry.type;
      if (!baseTypeMap[base]) baseTypeMap[base] = 0;
      baseTypeMap[base] += entry.total;
      if (!monthlyBaseData[month][base]) {
        monthlyBaseData[month][base] = { total: 0, top_donors: [] };
      }
      monthlyBaseData[month][base].total += entry.total;
      if (entry.top_donors) monthlyBaseData[month][base].top_donors.push(...entry.top_donors);
    }
    // Merge/dedupe top donors
    for (const base of Object.keys(monthlyBaseData[month])) {
      const donors = monthlyBaseData[month][base].top_donors;
      const merged = {};
      for (const d of donors) merged[d.name] = (merged[d.name] || 0) + d.total;
      monthlyBaseData[month][base].top_donors = Object.entries(merged)
        .map(([name, total]) => ({ name, total }))
        .sort((a, b) => b.total - a.total);
    }
  }

  // 2. Order base types
  let baseTypes;
  if (byTypeRows && byTypeRows.length) {
    const seen = new Set();
    baseTypes = [];
    for (const r of byTypeRows) {
      const base = r.type.endsWith(" (out of state)") ? r.type.slice(0, -15) : r.type;
      if (!seen.has(base) && baseTypeMap[base]) { seen.add(base); baseTypes.push(base); }
    }
    for (const bt of Object.keys(baseTypeMap)) { if (!baseTypes.includes(bt)) baseTypes.push(bt); }
  } else {
    baseTypes = Object.keys(baseTypeMap).sort((a, b) => baseTypeMap[b] - baseTypeMap[a]);
  }

  // 3. Format month labels
  const labels = months.map(m => {
    const [y, mo] = m.split("-");
    return new Date(+y, +mo - 1).toLocaleDateString("en-US", { month: "short", year: "2-digit" });
  });

  // 4. Compute totals for legend
  const baseTotals = {};
  let grandTotal = 0;
  for (const month of months) {
    for (const base of baseTypes) {
      const val = monthlyBaseData[month]?.[base]?.total || 0;
      baseTotals[base] = (baseTotals[base] || 0) + val;
      grandTotal += val;
    }
  }

  // 5. Build ECharts series
  const series = baseTypes.map(base => {
    const color = TYPE_COLOR_MAP[base] ?? PALETTE[Object.keys(TYPE_COLOR_MAP).length % PALETTE.length];
    return {
      name: base,
      type: 'line',
      stack: 'total',
      areaStyle: { opacity: 0.6 },
      emphasis: { focus: 'series' },
      symbol: 'none',
      lineStyle: { width: 1.5 },
      itemStyle: { color },
      data: months.map(m => Math.round(monthlyBaseData[m]?.[base]?.total || 0)),
    };
  });

  // 6. Create chart
  const chart = initEChart(el);
  const option = {
    tooltip: {
      trigger: 'axis',
      triggerOn: IS_MOBILE ? 'click' : 'mousemove',
      alwaysShowContent: false,
      confine: false,
      axisPointer: { type: 'cross' },
      extraCssText: IS_MOBILE ? 'max-width:260px;font-size:11px;' : '',
      position: tooltipPosition,
      formatter: function(params) {
        if (!params || !params.length) return '';
        const idx = params[0].dataIndex;
        const month = months[idx];
        const data = monthlyBaseData[month] || {};
        const total = params.reduce((s, p) => s + (p.value || 0), 0);
        const [y, mo] = month.split("-");
        const monthLabel = new Date(+y, +mo - 1).toLocaleDateString("en-US", { month: "short", year: "numeric" });

        const sorted = [...params].sort((a, b) => (b.value || 0) - (a.value || 0));
        // On mobile, show only types with values (skip zeros)
        const shown = sorted.filter(p => p.value > 0);
        const fs = IS_MOBILE ? '11px' : '13px';
        let html = `<div style="font-weight:600;margin-bottom:3px;font-size:${fs}">${monthLabel}</div>`;
        shown.forEach(p => {
          const pct = total > 0 ? (p.value / total * 100).toFixed(1) : "0.0";
          const isSelected = selectedDonorTypeGroup === p.seriesName;
          const bg = isSelected ? 'background:rgba(0,0,0,0.06);border-radius:3px;' : '';
          const name = IS_MOBILE ? shortTypeName(p.seriesName) : p.seriesName;
          html += `<div style="display:flex;align-items:center;gap:4px;padding:1px 3px;font-size:${fs};${bg}">
            ${p.marker}<span style="flex:1">${name}</span>
            <span style="font-weight:500">${fmtCompact$(p.value)}</span>
            <span style="color:#999;font-size:10px">${pct}%</span>
          </div>`;
        });
        html += `<div style="border-top:1px solid #eee;margin-top:3px;padding-top:3px;font-weight:600;text-align:right;font-size:${fs}">${fmtCompact$(total)}</div>`;

        // Top donors for selected type
        const maxDonors = IS_MOBILE ? 3 : 5;
        if (selectedDonorTypeGroup && data[selectedDonorTypeGroup]?.top_donors?.length) {
          const donors = data[selectedDonorTypeGroup].top_donors.slice(0, maxDonors);
          const label = IS_MOBILE ? shortTypeName(selectedDonorTypeGroup) : selectedDonorTypeGroup;
          html += `<div style="margin-top:3px;font-weight:600;font-size:10px">Top ${label} Donors</div>`;
          donors.forEach(d => {
            html += `<div style="display:flex;justify-content:space-between;font-size:10px;padding:1px 0">
              <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:160px">${d.name}</span>
              <span style="font-weight:500;margin-left:6px;white-space:nowrap">${fmtCompact$(d.total)}</span>
            </div>`;
          });
        } else if (!selectedDonorTypeGroup) {
          html += `<div style="margin-top:3px;font-size:10px;color:#999;font-style:italic">Tap a colored area for top donors</div>`;
        }
        return html;
      },
    },
    grid: {
      left: IS_MOBILE ? 10 : 20,
      right: IS_MOBILE ? 10 : 20,
      top: 10,
      bottom: IS_MOBILE ? 80 : 40,
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: labels,
      axisLabel: { fontSize: IS_MOBILE ? 9 : 11, rotate: 45 },
      boundaryGap: false,
    },
    yAxis: {
      type: 'value',
      axisLabel: {
        formatter: v => fmtCompact$(v),
        fontSize: IS_MOBILE ? 9 : 12,
      },
    },
    dataZoom: IS_MOBILE ? [
      { type: 'slider', start: 70, end: 100, height: 25, bottom: 5 },
      { type: 'inside' },
    ] : [{ type: 'inside' }],
    series,
  };
  chart.setOption(option);

  // 7. Click handler — use ZRender click since stacked areas with symbol:none
  //    don't fire ECharts series click events. Determine which series was
  //    clicked by checking the y-value against cumulative stack heights.
  function applySelection(typeName, dataIndex) {
    selectedDonorTypeGroup = selectedDonorTypeGroup === typeName ? null : typeName;
    const newSeries = baseTypes.map(base => ({
      name: base,
      areaStyle: {
        opacity: selectedDonorTypeGroup === null ? 0.6
          : selectedDonorTypeGroup === base ? 0.85 : 0.05,
      },
      lineStyle: {
        width: selectedDonorTypeGroup === base ? 2.5 : 1,
        opacity: selectedDonorTypeGroup === null ? 1
          : selectedDonorTypeGroup === base ? 1 : 0.1,
      },
    }));
    chart.setOption({ series: newSeries });
    updateStackedAreaLegendStyles(el.parentElement || el.closest('#overview-donut-box'), baseTypes);
    // Hide then re-show tooltip so formatter re-runs with updated selection
    // (ECharts won't re-render if tooltip is already visible at same position)
    chart.dispatchAction({ type: 'hideTip' });
    if (dataIndex != null) {
      setTimeout(() => {
        chart.dispatchAction({ type: 'showTip', seriesIndex: 0, dataIndex });
      }, 80);
    }
  }

  chart.getZr().on('click', function(params) {
    const pointInGrid = chart.containPixel('grid', [params.offsetX, params.offsetY]);
    if (!pointInGrid) {
      // Clicked outside the grid (margins, title area) — reset selection
      if (selectedDonorTypeGroup) {
        applySelection(selectedDonorTypeGroup, null); // toggle off
      }
      chart.dispatchAction({ type: 'hideTip' });
      return;
    }

    const dataCoord = chart.convertFromPixel({ seriesIndex: 0 }, [params.offsetX, params.offsetY]);
    const dataIndex = Math.round(dataCoord[0]);
    if (dataIndex < 0 || dataIndex >= months.length) return;

    const month = months[dataIndex];
    const mData = monthlyBaseData[month] || {};
    const clickY = dataCoord[1];

    // Check if click is above all stacked data (white space above the chart data)
    let totalAtIndex = 0;
    for (const base of baseTypes) totalAtIndex += mData[base]?.total || 0;

    if (clickY > totalAtIndex) {
      // Clicked above the data — reset selection
      if (selectedDonorTypeGroup) {
        applySelection(selectedDonorTypeGroup, null);
      }
      chart.dispatchAction({ type: 'hideTip' });
      return;
    }

    // Walk up the stack to find which series was clicked
    let cumulative = 0;
    let clickedType = null;
    for (const base of baseTypes) {
      const val = mData[base]?.total || 0;
      cumulative += val;
      if (clickY <= cumulative) {
        clickedType = base;
        break;
      }
    }
    if (!clickedType && baseTypes.length) clickedType = baseTypes[baseTypes.length - 1];

    applySelection(clickedType, dataIndex);
  });

  // Also keep ECharts series click as fallback (works on desktop with hover)
  chart.on('click', function(params) {
    if (params.componentType === 'series') {
      applySelection(params.seriesName, params.dataIndex);
    }
  });

  // 8. Dismiss tooltip when tapping outside chart + legend area
  const chartBox = el.closest("#overview-donut-box") || el.parentElement;
  document.addEventListener("click", function(e) {
    if (chartBox && !chartBox.contains(e.target)) {
      chart.dispatchAction({ type: "hideTip" });
    }
  });

  // 9. Render custom legend (keep existing pattern)
  renderStackedAreaLegend(chart, baseTypes, baseTotals, grandTotal, byTypeRows);

  // 10. Update legend totals when dataZoom changes (pan/zoom/slider)
  function updateLegendForVisibleRange() {
    const opt = chart.getOption();
    const zoom = opt.dataZoom || [];
    let startPct = 0, endPct = 100;
    for (const dz of zoom) {
      if (dz.start != null) startPct = dz.start;
      if (dz.end != null) endPct = dz.end;
    }
    const startIdx = Math.floor(startPct / 100 * months.length);
    const endIdx = Math.ceil(endPct / 100 * months.length);
    const visibleMonths = months.slice(startIdx, endIdx);

    const visTotals = {};
    let visGrand = 0;
    for (const m of visibleMonths) {
      for (const base of baseTypes) {
        const val = monthlyBaseData[m]?.[base]?.total || 0;
        visTotals[base] = (visTotals[base] || 0) + val;
        visGrand += val;
      }
    }

    // Update legend item text
    const box = el.closest("#overview-donut-box") || el.parentElement;
    if (!box) return;
    const items = box.querySelectorAll(".stacked-area-legend-item");
    items.forEach((item, i) => {
      const base = baseTypes[i];
      if (!base) return;
      const total = visTotals[base] || 0;
      const pct = visGrand > 0 ? (total / visGrand * 100).toFixed(1) : "0.0";
      const amtEl = item.querySelector(".legend-type-amount");
      const pctEl = item.querySelector(".legend-type-pct");
      if (amtEl) amtEl.textContent = fmt$(total);
      if (pctEl) pctEl.textContent = `(${pct}%)`;
    });
  }

  chart.on('dataZoom', updateLegendForVisibleRange);

  return chart;
}

function renderStackedAreaLegend(chart, baseTypes, baseTotals, grandTotal, byTypeRows) {
  const el = chart.getDom();
  const box = el.closest("#overview-donut-box") || el.parentElement;
  if (!box) return;
  const existing = box.querySelector(".stacked-area-legend");
  if (existing) existing.remove();

  const legendDiv = document.createElement("div");
  legendDiv.className = "stacked-area-legend";

  baseTypes.forEach((base, i) => {
    const color = TYPE_COLOR_MAP[base] ?? PALETTE[Object.keys(TYPE_COLOR_MAP).length % PALETTE.length];
    const total = baseTotals[base] || 0;
    const pct = grandTotal > 0 ? (total / grandTotal * 100).toFixed(1) : "0.0";
    const item = document.createElement("span");
    item.className = "stacked-area-legend-item";
    item.innerHTML = `<span class="swatch" style="background:${color}"></span>
      <span class="legend-type-name">${esc(shortTypeName(base))}</span>
      <span class="legend-type-amount">${fmt$(total)}</span>
      <span class="legend-type-pct">(${pct}%)</span>`;

    item.addEventListener("click", () => {
      selectedDonorTypeGroup = selectedDonorTypeGroup === base ? null : base;
      // Update chart opacity
      const newSeries = baseTypes.map(bt => ({
        name: bt,
        areaStyle: {
          opacity: selectedDonorTypeGroup === null ? 0.6
            : selectedDonorTypeGroup === bt ? 0.85 : 0.05,
        },
        lineStyle: {
          width: selectedDonorTypeGroup === bt ? 2.5 : 1,
          opacity: selectedDonorTypeGroup === null ? 1
            : selectedDonorTypeGroup === bt ? 1 : 0.1,
        },
      }));
      chart.setOption({ series: newSeries });
      updateStackedAreaLegendStyles(box, baseTypes);
    });

    // Hover tooltip for all-time top donors
    item.addEventListener("mouseenter", () => {
      const typeRow = (byTypeRows || []).find(r => {
        const b = r.type.endsWith(" (out of state)") ? r.type.slice(0, -15) : r.type;
        return b === base;
      });
      if (!typeRow || !typeRow.top_donors || !typeRow.top_donors.length) return;
      // Use a simple title tooltip
      const donorText = typeRow.top_donors.slice(0, 5).map(d => `${d.name}: ${fmt$(d.total)}`).join('\n');
      item.title = `Top ${base} Donors (All Time)\n${donorText}`;
    });

    legendDiv.appendChild(item);
  });

  // Click background to deselect
  legendDiv.addEventListener("click", (e) => {
    if (e.target === legendDiv) {
      selectedDonorTypeGroup = null;
      const newSeries = baseTypes.map(bt => ({
        name: bt,
        areaStyle: { opacity: 0.6 },
        lineStyle: { width: 1.5, opacity: 1 },
      }));
      chart.setOption({ series: newSeries });
      updateStackedAreaLegendStyles(box, baseTypes);
    }
  });

  box.appendChild(legendDiv);
  updateStackedAreaLegendStyles(box, baseTypes);
}

function updateStackedAreaLegendStyles(container, baseTypes) {
  const legendDiv = container.querySelector ? container.querySelector(".stacked-area-legend") : container;
  if (!legendDiv) return;
  const items = legendDiv.querySelectorAll(".stacked-area-legend-item");
  items.forEach((item, i) => {
    const base = baseTypes[i];
    if (selectedDonorTypeGroup === null) {
      item.classList.remove("selected", "dimmed");
    } else if (selectedDonorTypeGroup === base) {
      item.classList.add("selected");
      item.classList.remove("dimmed");
    } else {
      item.classList.remove("selected");
      item.classList.add("dimmed");
    }
  });
}

// Fallback when no monthly data — simple pie chart
function makeSimplePieChart(containerId, typeRows) {
  const el = document.getElementById(containerId);
  if (!el) return null;
  el.className = "echart-container";
  const chart = initEChart(el);
  chart.setOption({
    tooltip: {
      trigger: 'item',
      position: tooltipPosition,
      formatter: '{b}: {c} ({d}%)',
    },
    series: [{
      type: 'pie',
      radius: ['35%', '65%'],
      data: typeRows.map(r => ({
        name: r.type,
        value: Math.round(r.total),
        itemStyle: { color: typeColor(r.type) },
      })),
      label: { fontSize: IS_MOBILE ? 10 : 12 },
      emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.2)' } },
    }],
  });
  return chart;
}

// ── Date-range filter helpers ─────────────────────────────────────────────────

// Returns ["2020","2021",...] for the active date range, or null (= all years).
function yearsInRange() {
  const ds = state.dateStart, de = state.dateEnd;
  if (!ds && !de) return null;
  const sy = ds ? +ds.slice(0, 4) : 2000;
  const ey = de ? +de.slice(0, 4) : new Date().getFullYear();
  const out = [];
  for (let y = sy; y <= ey; y++) out.push(String(y));
  return out;
}

// Filter an array of {month:"YYYY-MM", ...} rows to the active date range.
function filterMonthRows(rows) {
  const sm = state.dateStart ? state.dateStart.slice(0, 7) : null;
  const em = state.dateEnd   ? state.dateEnd.slice(0, 7)   : null;
  if (!sm && !em) return rows;
  return rows.filter(r => (!sm || r.month >= sm) && (!em || r.month <= em));
}

// Cash on hand is an as-of value. With an open-ended date filter the endpoint
// is still the latest available month; otherwise use the selected end month.
function activeCashThroughMonth() {
  return state.dateEnd ? state.dateEnd.slice(0, 7) : null;
}

// Merge per-year {name, total} arrays for the given years (null = all).
// Merges case-insensitively so e.g. "DaVita" and "Davita" don't become
// two separate rows; the first-encountered capitalisation is used for display.
function mergeByYear(byYear, years) {
  const src = years === null ? Object.keys(byYear || {}) : years;
  const totalMap = new Map(); // lowercase key → total
  const nameMap  = new Map(); // lowercase key → display name (first seen)
  src.forEach(yr => {
    (byYear[yr] || []).forEach(d => {
      const key = d.name.toLowerCase();
      totalMap.set(key, (totalMap.get(key) || 0) + d.total);
      if (!nameMap.has(key)) nameMap.set(key, d.name);
    });
  });
  return [...totalMap.entries()]
    .map(([key, total]) => ({ name: nameMap.get(key), total: Math.round(total * 100) / 100 }))
    .sort((a, b) => b.total - a.total);
}

// Same as mergeByYear but uses "type" as the key (contributor-type arrays).
// Sorts by base-type combined total so in-state/out-of-state pairs stay adjacent.
// Also merges top_donors lists across years so tooltips work for filtered views.
function mergeTypeByYear(byYear, years) {
  const src = years === null ? Object.keys(byYear || {}) : years;
  const totalMap = new Map();
  const donorMap = new Map(); // type → Map<name, total>
  src.forEach(yr => {
    (byYear[yr] || []).forEach(d => {
      totalMap.set(d.type, (totalMap.get(d.type) || 0) + d.total);
      if (d.top_donors && d.top_donors.length) {
        if (!donorMap.has(d.type)) donorMap.set(d.type, new Map());
        const dm = donorMap.get(d.type);
        d.top_donors.forEach(donor => {
          dm.set(donor.name, (dm.get(donor.name) || 0) + donor.total);
        });
      }
    });
  });
  const map = totalMap;
  const entries = [...map.entries()]
    .map(([type, total]) => {
      const obj = { type, total: Math.round(total * 100) / 100 };
      if (donorMap.has(type)) {
        obj.top_donors = [...donorMap.get(type).entries()]
          .map(([name, t]) => ({ name, total: Math.round(t * 100) / 100 }))
          .sort((a, b) => b.total - a.total)
          .slice(0, 5);
      }
      return obj;
    });
  // Compute combined total per base type so pairs sort together
  const baseTotals = new Map();
  entries.forEach(e => {
    const base = e.type.endsWith(" (out of state)") ? e.type.slice(0, -15) : e.type;
    baseTotals.set(base, (baseTotals.get(base) || 0) + e.total);
  });
  return entries.sort((a, b) => {
    const baseA = a.type.endsWith(" (out of state)") ? a.type.slice(0, -15) : a.type;
    const baseB = b.type.endsWith(" (out of state)") ? b.type.slice(0, -15) : b.type;
    const diff = (baseTotals.get(baseB) || 0) - (baseTotals.get(baseA) || 0);
    if (diff !== 0) return diff;
    // Same base: in-state before out-of-state
    return (a.type.endsWith(" (out of state)") ? 1 : 0) - (b.type.endsWith(" (out of state)") ? 1 : 0);
  });
}

// Same as mergeTypeByYear but filters by the active date range at month precision.
function mergeTypeByMonth(byMonth) {
  const sm = state.dateStart ? state.dateStart.slice(0, 7) : null;
  const em = state.dateEnd   ? state.dateEnd.slice(0, 7)   : null;
  const keys = Object.keys(byMonth || {})
    .filter(m => (!sm || m >= sm) && (!em || m <= em));
  const totalMap = new Map();
  const donorMap = new Map();
  keys.forEach(mo => {
    (byMonth[mo] || []).forEach(d => {
      totalMap.set(d.type, (totalMap.get(d.type) || 0) + d.total);
      if (d.top_donors && d.top_donors.length) {
        if (!donorMap.has(d.type)) donorMap.set(d.type, new Map());
        const dm = donorMap.get(d.type);
        d.top_donors.forEach(donor => {
          dm.set(donor.name, (dm.get(donor.name) || 0) + donor.total);
        });
      }
    });
  });
  const entries = [...totalMap.entries()]
    .map(([type, total]) => {
      const obj = { type, total: Math.round(total * 100) / 100 };
      if (donorMap.has(type)) {
        obj.top_donors = [...donorMap.get(type).entries()]
          .map(([name, t]) => ({ name, total: Math.round(t * 100) / 100 }))
          .sort((a, b) => b.total - a.total)
          .slice(0, 5);
      }
      return obj;
    });
  const baseTotals = new Map();
  entries.forEach(e => {
    const base = e.type.endsWith(" (out of state)") ? e.type.slice(0, -15) : e.type;
    baseTotals.set(base, (baseTotals.get(base) || 0) + e.total);
  });
  return entries.sort((a, b) => {
    const baseA = a.type.endsWith(" (out of state)") ? a.type.slice(0, -15) : a.type;
    const baseB = b.type.endsWith(" (out of state)") ? b.type.slice(0, -15) : b.type;
    const diff = (baseTotals.get(baseB) || 0) - (baseTotals.get(baseA) || 0);
    if (diff !== 0) return diff;
    return (a.type.endsWith(" (out of state)") ? 1 : 0) - (b.type.endsWith(" (out of state)") ? 1 : 0);
  });
}

// Cash movement is emitted independently from the visible historical totals.
// New data carries the exact server-side movement after dated/ORESTAR-absence
// filters and loan normalization.  Older generated data has no such field, so
// retain the component equation as a backwards-compatible fallback.
function timelineLegacyCashNet(row) {
  return (row?.contributions || 0) + (row?.other_receipts || 0) + (row?.balance_adjustments || 0)
       - (row?.expenditures || 0) - (row?.other_disbursements || 0);
}

function timelineCashNet(row) {
  if (row && typeof row.cash_balance_net === "number" && Number.isFinite(row.cash_balance_net)) {
    return row.cash_balance_net;
  }
  return timelineLegacyCashNet(row);
}

function timelineCashSchemaIsComplete(rows) {
  return Array.isArray(rows) && rows.length > 0 && rows.every(row =>
    row && typeof row.cash_balance_net === "number" && Number.isFinite(row.cash_balance_net));
}

// Return cash held at the end of throughMonth (or at the end of all data).
// The first beginning-balance key is the month when the official opening
// anchor becomes effective. Rows before a later anchor remain visible history
// but do not roll into cash, and a range with no transaction rows still carries
// the position established before its endpoint.
function timelineCashPosition(rows, beginningBalances, throughMonth) {
  // Never mix contracts within one trajectory. A partially cached new file
  // may contain an embedded global anchor in cash_balance_net alongside legacy
  // rows. In that case use legacy components for every row and the separate
  // beginning balance exactly once.
  const exactCashSchema = timelineCashSchemaIsComplete(rows);
  const sortedYears = Object.keys(beginningBalances || {}).sort();
  const firstYear = sortedYears[0] || "";
  const anchorMonth = firstYear ? `${firstYear}-01` : "";
  let position = (!anchorMonth || !throughMonth || anchorMonth <= throughMonth)
    ? (firstYear ? (beginningBalances[firstYear] || 0) : 0)
    : 0;

  for (const row of rows || []) {
    if (!row || !row.month) continue;
    if (anchorMonth && row.month < anchorMonth) continue;
    if (throughMonth && row.month > throughMonth) break;
    position += exactCashSchema
      ? timelineCashNet(row)
      : timelineLegacyCashNet(row);
  }
  return Math.round(position * 100) / 100;
}

// Sum visible contributions/expenditures/count from a (possibly filtered)
// timeline array. Cash on hand uses timelineCashNet so an ORESTAR-nonreturning
// historical row remains visible without moving the current cash balance.
function statsFromTimeline(rows, beginningBalances, fullTimeline, cashThroughMonth) {
  // ORESTAR methodology (empirically verified):
  // contributions = Cash Contribution + In-Kind (ORESTAR "Cash Contributions")
  // expenditures = Cash Expenditure + In-Kind mirrored (ORESTAR "Cash Expenditures")
  // loans_received / loan_payments = separate ORESTAR line items
  // COH = begin + contributions + loans_received + other_receipts
  //       - expenditures - loan_payments - other_disbursements
  const totalIn      = rows.reduce((s, r) => s + (r.contributions || 0), 0);
  const totalInKind  = rows.reduce((s, r) => s + (r.inkind || 0), 0);
  const totalOut     = rows.reduce((s, r) => s + (r.expenditures || 0), 0);
  const count        = rows.reduce((s, r) => s + (r.count || 0), 0);

  // Cash is intentionally independent of the visible component sums. The
  // server has already applied exact-absence, date, loan and adjustment rules.
  const cashSource = fullTimeline || rows;
  const cashOnHand = timelineCashPosition(
    cashSource, beginningBalances, cashThroughMonth,
  );

  return {
    totalIn:     Math.round(totalIn    * 100) / 100,
    totalInKind: Math.round(totalInKind * 100) / 100,
    totalOut:    Math.round(totalOut   * 100) / 100,
    cashOnHand,
    count,
  };
}

// ── Account summary tile definitions ─────────────────────────────────────────
// Historical totals come from held transaction rows. The exact cash lane also
// uses the first ORESTAR opening balance as its anchor and, when paired to the
// same transaction scope, ORESTAR's annual loan treatment. Those differences
// stay explicit rather than rewriting or hiding the underlying transaction
// history.

const CALC_TILE_META = {
  // ── Cash Balance tiles (COH-contributing) ─────────────────────────────────
  beginning_balance: {
    label: "Beginning Balance",
    coh: true,
    orestar_field: "orestar_beginning_balance",
    tip: "<strong>Counted:</strong> The committee's cash position at the start of the selected period.<br><strong>Condition:</strong> Anchored to the first-ever ORESTAR beginning balance for this committee, then rolled forward year-to-year using our transaction data.<br><strong>Meaning:</strong> How much cash the committee started with. Only the very first year's balance comes from ORESTAR; every subsequent year is calculated from the prior year's ending balance.",
  },
  cash_contributions: {
    label: "Cash Contributions",
    orestar_field: "orestar_contributions",
    tip: "<strong>Historical total:</strong> All held Cash Contribution, non-exempt loan, and In-Kind Contribution rows in this period. Rows remain visible even when current ORESTAR evidence says they should not move today's cash balance.<br><strong>Note:</strong> In-kind appears on both sides and loans are also broken out below; neither is counted twice in Net Cash Flow.",
  },
  other_receipts: {
    label: "Other Receipts",
    orestar_field: "orestar_other_receipts",
    tip: "<strong>Historical total:</strong> All held type OR transactions in this period, including records retained for history but excluded from the current cash lane when exact evidence requires it.<br><strong>Meaning:</strong> Refunds, interest, loans classified as other receipts, and other miscellaneous income.",
  },
  loans_received: {
    label: "Loans Received",
    tip: "<strong>Historical breakout:</strong> Held Loan Received (Non-Exempt) rows. These rows are already included in the contribution total above. When ORESTAR's annual loan line is paired to this same transaction snapshot and differs, Net Cash Flow uses that reported amount and Cash Treatment Adjustment exposes the difference.",
  },
  cash_expenditures: {
    label: "Cash Expenditures",
    orestar_field: "orestar_expenditures",
    tip: "<strong>Historical total:</strong> All held Cash Expenditure and non-exempt loan-payment rows, plus in-kind mirrored from contributions. Rows remain visible even when they do not move the current cash lane.",
  },
  loan_payments: {
    label: "Loan Payments",
    tip: "<strong>Historical breakout:</strong> Held Loan Payment (Non-Exempt) rows. These rows are already included in the expenditure total above and are not counted a second time.",
  },
  other_disbursements: {
    label: "Other Disbursements",
    orestar_field: "orestar_other_disbursements",
    tip: "<strong>Historical total:</strong> All held type OD transactions in this period, including retained records that exact current evidence excludes from the cash lane.<br><strong>Meaning:</strong> Miscellaneous payments that are not standard campaign expenditures.",
  },
  balance_adjustments: {
    label: "Balance Adjustments",
    tip: "<strong>Historical total:</strong> Held ORESTAR Cash Balance Adjustment rows. Their signed amounts are included in the raw historical movement before Cash Treatment Adjustment is applied.",
  },
  cash_treatment_adjustment: {
    label: "Cash Treatment Adjustment",
    coh: true,
    compute: d => d.cash_treatment_adjustment,
    tip: "<strong>Reconciliation bridge:</strong> The difference between arithmetic over the historical totals above and the exact cash lane. It captures retained rows ORESTAR no longer returns, undated or pre-anchor history, and paired ORESTAR loan treatment.<br><strong>Identity:</strong> Historical movement + this adjustment = Net Cash Flow.",
  },
  net_cash_flow: {
    label: "Net Cash Flow",
    coh: true,
    subtotal: true,
    compute: d => d.net_cash_flow,
    tip: "<strong>Counted:</strong> Exact cash movement after certified non-returning rows, undated or pre-anchor history, paired loan treatment, and balance adjustments are applied. All held rows remain visible in the historical component totals.<br><strong>Meaning:</strong> How much cash the committee gained or lost during this period.",
  },
  ending_cash_balance: {
    label: "Ending Cash Balance",
    coh: true,
    subtotal: true,
    orestar_field: "orestar_ending",
    compute: d => d.ending_cash_balance,
    tip: "<strong>Counted:</strong> Beginning balance + net cash flow.<br><strong>Meaning:</strong> Our calculated cash position at the end of the period. This should closely match the ORESTAR ending balance if our transaction data is complete.",
  },
  // ── Non-cash items ────────────────────────────────────────────────────────
  inkind_contributions: {
    label: "In-Kind (included above)",
    nonCoh: true,
    tip: "<strong>Shown for transparency.</strong> Already included in Cash Contributions and mirrored in Cash Expenditures. Nets to zero for cash balance purposes.<br><strong>Condition:</strong> Transactions with type 'C' and sub-type 'In-Kind Contribution'.",
  },
  // ── ORESTAR-reported values (for comparison / validation) ─────────────────
  orestar_ending: {
    label: "ORESTAR Ending Balance",
    orestar: true,
    tip: "<strong>Source:</strong> Scraped directly from the ORESTAR account summary page.<br><strong>Comparison:</strong> For All Time, this read is paired with the published app balance from an exact, fingerprinted transaction snapshot. The two timestamps bound the capture window.",
  },
  orestar_discrepancy: {
    label: "Discrepancy at Capture",
    orestar: true,
    subtotal: true,
    tip: "<strong>Counted:</strong> The app balance saved when the ORESTAR page was read, minus ORESTAR's balance from that same capture.<br><strong>Meaning:</strong> New transactions collected later do not change this number. A positive number means the matched app snapshot was higher; negative means lower.",
  },
  // ── ORESTAR-only balance sheet items (cannot be calculated from transactions) ──
  accounts_receivable: {
    label: "Accounts Receivable",
    orestar: true,
    tip: "<strong>Source:</strong> ORESTAR account summary only (not calculable from transactions).<br><strong>Meaning:</strong> Money pledged or owed to the committee that hasn't been received yet — like outstanding pledges or reimbursements expected.",
  },
  accounts_payable: {
    label: "Accounts Payable",
    orestar: true,
    tip: "<strong>Source:</strong> ORESTAR account summary only (not calculable from transactions).<br><strong>Meaning:</strong> Bills the committee owes but hasn't paid yet — like vendor invoices or outstanding obligations.",
  },
  total_outstanding_loans: {
    label: "Outstanding Loans",
    orestar: true,
    tip: "<strong>Source:</strong> ORESTAR account summary only (not calculable from transactions).<br><strong>Meaning:</strong> Total unpaid loan balances — how much the committee still owes on borrowed money.",
  },
  outstanding_personal_expenditures: {
    label: "Personal Expenditures Owed",
    orestar: true,
    tip: "<strong>Source:</strong> ORESTAR account summary only (not calculable from transactions).<br><strong>Meaning:</strong> Money a candidate or treasurer spent personally on behalf of the committee that hasn't been reimbursed yet.",
  },
};

const CALC_GROUPS = [
  {
    title: "Historical Transaction Totals and Cash Position",
    fields: ["beginning_balance", "cash_contributions", "loans_received", "other_receipts",
             "cash_expenditures", "loan_payments", "other_disbursements", "balance_adjustments",
             "cash_treatment_adjustment", "net_cash_flow", "ending_cash_balance"],
  },
  {
    title: "Non-Cash Items",
    fields: ["inkind_contributions"],
  },
  {
    title: "ORESTAR Validation",
    fields: ["orestar_ending", "orestar_discrepancy"],
  },
  {
    title: "ORESTAR Balance Sheet",
    fields: ["accounts_receivable", "accounts_payable", "total_outstanding_loans", "outstanding_personal_expenditures"],
  },
];

/**
 * Build a calculated account summary from the filer's transaction data.
 * Historical components come from held row-by-row transactions. Exact cash
 * additionally uses the first ORESTAR beginning balance as its anchor and any
 * annual loan treatment paired to the same transaction scope, with the bridge
 * exposed in the UI.
 */
/**
 * Build a calculated account summary, optionally filtered to a single year.
 * @param {object} profile - The filer's full profile JSON
 * @param {string} [year] - Optional year string (e.g. "2024") to filter to
 */
function buildCalcSummary(profile, year) {
  const timeline = profile.timeline || [];
  if (!timeline.length && !Object.keys(profile.beginning_balances || {}).length) return null;

  // Filter timeline rows if year is specified
  const rows = year
    ? timeline.filter(r => r.month && r.month.startsWith(year))
    : timeline;

  // Sum transaction-based values from the (filtered) timeline
  let cashContrib = 0, inkind = 0, loansIn = 0, cashExpend = 0, loansOut = 0,
      otherReceipts = 0, otherDisburse = 0, balanceAdj = 0;
  for (const row of rows) {
    cashContrib   += row.contributions       || 0;
    inkind        += row.inkind              || 0;
    loansIn       += row.loans_received      || 0;
    cashExpend    += row.expenditures        || 0;
    loansOut      += row.loan_payments       || 0;
    otherReceipts += row.other_receipts      || 0;
    otherDisburse += row.other_disbursements || 0;
    balanceAdj    += row.balance_adjustments || 0;
  }

  // Beginning/ending positions are as-of calculations over the full cash
  // trajectory. This handles years with no rows, partial-year filtering, and
  // official anchors that begin after older retained history.
  const beginBalances = profile.beginning_balances || {};
  const sortedYears = Object.keys(beginBalances).sort();
  const firstYear = sortedYears[0] || "";
  const firstYearBal = firstYear ? (beginBalances[firstYear] || 0) : 0;
  let beginBal;
  let endingCalc;
  if (year) {
    const priorYearEnd = `${Number(year) - 1}-12`;
    beginBal = year === firstYear
      ? firstYearBal
      : timelineCashPosition(timeline, beginBalances, priorYearEnd);
    endingCalc = timelineCashPosition(timeline, beginBalances, `${year}-12`);
  } else {
    beginBal = firstYearBal;
    endingCalc = timelineCashPosition(timeline, beginBalances, null);
  }

  // ORESTAR-reported values (for comparison / validation)
  // If a specific year is selected, use the per-year ORESTAR data
  const orestarYearly = profile.orestar_yearly || {};
  const orestarYear = year ? (orestarYearly[year] || null) : null;
  const orestarCurrent = profile.orestar_account_summary || {};
  const comparison = profile.orestar_comparison || {};
  const paired = comparison.status === "paired" && comparison.delta_at_capture != null;
  const currentPair = paired && comparison.actionable === true;

  let orestarEnding = null;
  let orestarContrib = null, orestarExpend = null;
  let orestarOR = null, orestarOD = null, orestarBegBal = null;
  let hasOrestar = false;

  if (year && orestarYear) {
    // Per-year ORESTAR data from the yearly scraper
    orestarEnding = orestarYear.ending_cash_balance;
    orestarContrib = orestarYear.contributions;
    orestarExpend = orestarYear.expenditures;
    orestarOR = orestarYear.other_receipts;
    orestarOD = orestarYear.other_disbursements;
    orestarBegBal = orestarYear.beginning_balance;
    hasOrestar = true;
  } else if (!year && orestarCurrent.year) {
    // All-time validation uses the immutable paired value.  The current app
    // balance may contain transactions collected after this ORESTAR page was
    // read, so subtracting one from the other would manufacture a discrepancy.
    orestarEnding = currentPair
      ? comparison.orestar_cash_on_hand
      : (orestarCurrent.ending_cash_balance != null
          ? orestarCurrent.ending_cash_balance : null);
    hasOrestar = true;
  }

  // We currently freeze the all-time cash balance only. Historical pages are
  // useful source values, but late backfills mean today's per-year app totals
  // are not a collection-time pair and must not be labelled discrepancies.
  const comparisonDelta = !year && currentPair
    ? comparison.delta_at_capture
    : null;
  const netCashFlow = Math.round((endingCalc - beginBal) * 100) / 100;
  const historicalNetFlow = Math.round((
    cashContrib + otherReceipts + balanceAdj
    - cashExpend - otherDisburse
  ) * 100) / 100;

  return {
    cash_contributions: Math.round(cashContrib * 100) / 100,
    inkind_contributions: Math.round(inkind * 100) / 100,
    loans_received: Math.round(loansIn * 100) / 100,
    cash_expenditures: Math.round(cashExpend * 100) / 100,
    loan_payments: Math.round(loansOut * 100) / 100,
    other_disbursements: Math.round(otherDisburse * 100) / 100,
    beginning_balance: Math.round(beginBal * 100) / 100,
    other_receipts: Math.round(otherReceipts * 100) / 100,
    balance_adjustments: Math.round(balanceAdj * 100) / 100,
    cash_treatment_adjustment: Math.round((
      netCashFlow - historicalNetFlow
    ) * 100) / 100,
    net_cash_flow: netCashFlow,
    ending_cash_balance: Math.round(endingCalc * 100) / 100,
    // ORESTAR values for validation
    orestar_ending: orestarEnding,
    orestar_discrepancy: comparisonDelta,
    // Per-year ORESTAR breakdown (when available)
    orestar_beginning_balance: orestarBegBal,
    orestar_contributions: orestarContrib,
    orestar_expenditures: orestarExpend,
    orestar_other_receipts: orestarOR,
    orestar_other_disbursements: orestarOD,
    accounts_receivable: (year && orestarYear)
      ? (orestarYear.accounts_receivable || 0)
      : (orestarCurrent.accounts_receivable || 0),
    accounts_payable: (year && orestarYear)
      ? (orestarYear.accounts_payable || 0)
      : (orestarCurrent.accounts_payable || 0),
    total_outstanding_loans: (year && orestarYear)
      ? (orestarYear.total_outstanding_loans || 0)
      : (orestarCurrent.total_outstanding_loans || 0),
    outstanding_personal_expenditures: (year && orestarYear)
      ? (orestarYear.outstanding_personal_expenditures || 0)
      : (orestarCurrent.outstanding_personal_expenditures || 0),
    _has_orestar: hasOrestar,
    _has_orestar_yearly: year && !!orestarYear,
    _orestar_comparable: !year && currentPair,
    // Component totals are not frozen in v1 of the paired snapshot. Showing
    // them is useful; warning on live-vs-captured line items is not.
    _orestar_line_comparable: false,
  };
}

function renderAcctSummary(profile) {
  const grid = document.getElementById("acct-summary-grid");
  const details = document.getElementById("acct-summary-details");
  const controls = document.getElementById("acct-summary-controls");
  const yearSelect = document.getElementById("acct-year-select");
  if (!grid || !details) return;

  if (!profile) {
    details.hidden = true;
    if (controls) controls.hidden = true;
    return;
  }

  // Populate year selector from available years in beginning_balances and orestar_yearly
  if (yearSelect && controls) {
    const balYears = Object.keys(profile.beginning_balances || {});
    const orestarYears = Object.keys(profile.orestar_yearly || {});
    const allYears = [...new Set([...balYears, ...orestarYears])].sort().reverse();

    const prevVal = yearSelect.value;
    yearSelect.innerHTML = '<option value="">All Time</option>';
    for (const yr of allYears) {
      yearSelect.innerHTML += `<option value="${yr}">${yr}</option>`;
    }
    yearSelect.value = prevVal && allYears.includes(prevVal) ? prevVal : "";
    controls.hidden = allYears.length === 0;

    // Wire change handler — use onchange so re-calls naturally replace the old handler
    yearSelect.onchange = () => {
      renderAcctSummaryTiles(profile, yearSelect.value || null);
    };
  }

  details.hidden = false;
  const selectedYear = yearSelect ? (yearSelect.value || null) : null;
  renderAcctSummaryTiles(profile, selectedYear);
}

function renderAcctSummaryTiles(profile, year) {
  const grid = document.getElementById("acct-summary-grid");
  if (!grid) return;

  const calcData = buildCalcSummary(profile, year);
  if (!calcData) {
    grid.innerHTML = "<p>No data available for this year.</p>";
    return;
  }

  const unpublished = balanceUnpublished(profile);
  grid.innerHTML = (unpublished
    ? `<p class="disc-note">${esc(UNPUBLISHED_BALANCE_NOTE)}</p>` : "") +
    CALC_GROUPS.map(group => {
    // Hide ORESTAR validation/balance groups if no ORESTAR data
    if (group.title.startsWith("ORESTAR") && !calcData._has_orestar) return "";
    // No published balance: nothing to validate against, and no cash lane.
    if (unpublished && group.title.startsWith("ORESTAR")) return "";

    const tiles = group.fields.map(field => {
      const meta = CALC_TILE_META[field];
      if (!meta) return "";
      if (unpublished && meta.coh) return "";

      let val;
      if (meta.compute) {
        val = meta.compute(calcData);
      } else {
        val = calcData[field] != null ? calcData[field] : 0;
      }

      // For discrepancy, show N/A if no ORESTAR data
      if (field === "orestar_discrepancy" && val === null) return "";
      if (field === "orestar_ending" && val === null) return "";

      const isSubtotal = meta.subtotal;
      const isOrestar = meta.orestar;
      let cls = "acct-tile";
      if (isSubtotal) cls += " acct-tile-subtotal";
      if (isOrestar) cls += " acct-tile-orestar";
      if (meta.coh) cls += " acct-tile-coh";

      // Color the discrepancy tile by severity
      let extraStyle = "";
      if (field === "orestar_discrepancy" && val !== null) {
        const abs = Math.abs(val);
        const sev = discrepancySeverity(abs);
        if (sev === "red") extraStyle = ' style="border-color:#fc8181;background:#fff5f5"';
        else if (sev === "yellow") extraStyle = ' style="border-color:#ecc94b;background:#fffff0"';
      }

      const helpBtn = meta.tip
        ? `<span class="acct-tile-help" tabindex="0" role="button" aria-label="Info">?<span class="acct-tile-tip">${meta.tip}</span></span>`
        : "";

      // ORESTAR per-tile warning: show if our value differs from ORESTAR by >$1
      let warnHTML = "";
      if (calcData._orestar_line_comparable
          && meta.orestar_field && calcData[meta.orestar_field] != null) {
        const orestarVal = calcData[meta.orestar_field];
        const delta = Math.round((val - orestarVal) * 100) / 100;
        if (Math.abs(delta) > 1) {
          const sign = delta > 0 ? "+" : "";
          warnHTML = `<span class="acct-tile-warn" title="ORESTAR: ${fmt$(orestarVal)} (${sign}${fmt$(delta)} diff)">&#9888;</span>`;
        }
      }

      const displayVal = val != null ? fmt$(val) : "N/A";

      return `<div class="${cls}"${extraStyle}>
        ${helpBtn}
        <div class="acct-tile-label">${esc(meta.label)}${warnHTML}</div>
        <div class="acct-tile-value">${displayVal}</div>
      </div>`;
    }).join("");

    if (!tiles.trim()) return "";

    return `<div class="acct-group">
      <div class="acct-group-title">${esc(group.title)}</div>
      <div class="acct-tile-grid">${tiles}</div>
    </div>`;
  }).join("");

  // Per-year discrepancy warnings with line-item detail
  // Per-year app values have not yet been captured with their matching ORESTAR
  // pages. Keep the legacy diagnostics in the data for audits, but do not
  // render them as current discrepancies in the public UI.
  const disc = profile.orestar_yearly_comparisons;
  if (disc && Object.keys(disc).length > 0) {
    const discYears = Object.keys(disc).sort();
    const showYears = year ? discYears.filter(y => y === year) : discYears;
    if (showYears.length) {
      let discHTML = `<div class="acct-group">
        <div class="acct-group-title">Yearly Comparison (Calculated vs. ORESTAR)</div>
        <div class="disc-table">
          <div class="disc-table-header">
            <span class="disc-col-year">Year</span>
            <span class="disc-col-num">Our End</span>
            <span class="disc-col-num">ORESTAR End</span>
            <span class="disc-col-num disc-col-diff">Δ End</span>
          </div>`;
      for (const yr of showYears) {
        const d = disc[yr];
        const endDisc = d.discrepancy || 0;
        // A certificate year's ORESTAR total omits money that moved without
        // being itemized; ORESTAR records it only in a later opening balance.
        // Its ghost row therefore shows here as a year-end difference that is
        // fully explained — labelled as such rather than styled as an error.
        const restate = d.certificate_restatement;
        const certExplained = restate != null && Math.abs(endDisc - restate) <= 0.01;
        const certClean = restate != null && Math.abs(endDisc) <= 0.01;
        const certTip = d.certificate_year
          ? "Certificate of Limited Contributions and Expenditures year: the committee " +
            "was exempt from itemizing, so ORESTAR's year total does not capture " +
            "everything that moved." +
            (restate != null
              ? ` ORESTAR restates the balance around this year; this balance carries ` +
                `${fmtSignedCents(restate)} of it as a derived restatement row.`
              : "")
          : "";
        // A reconciled year is confirmation, not a small problem. Every year
        // ORESTAR gives a figure for is listed now, so without this the table
        // would style twenty agreeing years as "minor discrepancies".
        const severity = (d.reconciles || certClean) ? "disc-ok"
          : certExplained ? "disc-cert"
          : Math.abs(endDisc) >= 10000 ? "disc-severe"
          : Math.abs(endDisc) >= 1000 ? "disc-warn" : "disc-minor";
        const diffText = (d.reconciles || certClean) ? "✓"
          : (certExplained ? "ⓒ " : "") + (endDisc > 0 ? "+" : "") + fmt$(endDisc);
        const rowId = `disc-detail-${yr}`;
        // Amended originals ORESTAR still counts in this year, named by ID.
        const liveIds = d.live_originals || [];
        const liveItems = ((profile.orestar_live_originals) || [])
          .filter(item => liveIds.includes(item.original_id));
        const liveTip = liveItems.length
          ? `ORESTAR still counts the original transaction ` +
            `${liveItems.map(liveOriginalPairText).join("; ")}. ORESTAR never ` +
            `marked it expired, so this balance counts it too.`
          : "";
        // Transactions ORESTAR deleted in this year, which neither side counts.
        const deletedItems = ((profile.orestar_deletions) || [])
          .filter(item => String(item.year) === String(yr));
        const deletedTip = deletedItems.length
          ? `ORESTAR deleted ${deletedItems.map(deletionText).join("; ")}. ` +
            `Neither ORESTAR nor this balance counts ` +
            `${deletedItems.length === 1 ? "it" : "them"}.`
          : "";
        const rowTip = [certTip, liveTip, deletedTip].filter(Boolean).join(" ");
        discHTML += `<div class="disc-row ${severity}" style="cursor:pointer" data-detail="${rowId}"${rowTip ? ` title="${esc(rowTip)}"` : ""}>
          <span class="disc-col-year">${yr} ▸</span>
          <span class="disc-col-num">${fmt$(d.our_end)}</span>
          <span class="disc-col-num">${fmt$(d.orestar_end)}</span>
          <span class="disc-col-num disc-col-diff">${diffText}</span>
        </div>`;
        // Expandable line-item detail
        discHTML += `<div id="${rowId}" class="disc-detail" hidden>`;
        const lines = [
          { label: "Begin Balance", ours: d.our_begin, theirs: d.orestar_begin, delta: d.delta_begin },
          { label: "Contributions", ours: d.our_contributions, theirs: d.orestar_contributions, delta: d.delta_contributions },
          { label: "Expenditures", ours: d.our_expenditures, theirs: d.orestar_expenditures, delta: d.delta_expenditures },
          { label: "Other Receipts", ours: d.our_other_receipts, theirs: d.orestar_other_receipts, delta: d.delta_other_receipts },
          { label: "Other Disburse", ours: d.our_other_disbursements, theirs: d.orestar_other_disbursements, delta: d.delta_other_disbursements },
        ];
        for (const line of lines) {
          if (line.delta == null) continue;
          const absDelta = Math.abs(line.delta);
          const cls = absDelta >= 1000 ? "disc-severe" : absDelta >= 10 ? "disc-warn" : "disc-minor";
          discHTML += `<div class="disc-detail-row ${cls}">
            <span class="disc-detail-label">${line.label}</span>
            <span class="disc-col-num">${fmt$(line.ours)}</span>
            <span class="disc-col-num">${line.theirs != null ? fmt$(line.theirs) : '—'}</span>
            <span class="disc-col-num disc-col-diff">${line.delta > 0 ? "+" : ""}${fmt$(line.delta)}</span>
          </div>`;
        }
        for (const item of liveItems) {
          discHTML += `<div class="disc-detail-row disc-cert" title="${esc(liveTip)}">
            <span class="disc-detail-label">Live original #${esc(item.original_id)}</span>
            <span class="disc-col-num">${item.amount != null ? esc(fmtCents(item.amount)) : "—"}</span>
            <span class="disc-col-num">amended ${esc((item.amended || []).map(a => `#${a.tran_id}`).join(", "))}</span>
            <span class="disc-col-num disc-col-diff">counted</span>
          </div>`;
        }
        if (restate != null) {
          discHTML += `<div class="disc-detail-row disc-cert" title="${esc(certTip)}">
            <span class="disc-detail-label">Certificate restatement</span>
            <span class="disc-col-num">${restate > 0 ? "+" : ""}${fmt$(restate)}</span>
            <span class="disc-col-num">in opening</span>
            <span class="disc-col-num disc-col-diff">derived</span>
          </div>`;
        }
        discHTML += `</div>`;
      }
      discHTML += `</div></div>`;
      grid.innerHTML += discHTML;

      // Wire expand/collapse
      grid.querySelectorAll("[data-detail]").forEach(row => {
        row.addEventListener("click", () => {
          const detail = document.getElementById(row.dataset.detail);
          if (detail) {
            detail.hidden = !detail.hidden;
            const arrow = row.querySelector(".disc-col-year");
            if (arrow) arrow.textContent = arrow.textContent.replace(/[▸▾]/, detail.hidden ? "▸" : "▾");
          }
        });
      });
    }
  }
}

// ── Global state ─────────────────────────────────────────────────────────────
const state = { selectedFilers: [], dateStart: "", dateEnd: "" };
const filerCache = {};   // slug → Promise<filerDetail>
let filerIndex = [];

// ── Cached static data (fetched once) ────────────────────────────────────────
let summaryData      = null;
let byTypeDataGlobal = null;
let donorsData       = null;
let recipientsData   = null;
let timelineData     = null;
let donorFilerMap    = null; // donor_name_lower → {slug, name, confidence} | {candidates, confidence:"ambiguous"}

let selectedDonorTypeGroup = null;

// ── Active tab ───────────────────────────────────────────────────────────────
let activeTab = "overview";

// ── Donors view mode ─────────────────────────────────────────────────────────
let donorsViewMode = "summary";  // "summary" | "by-year"

// ── Stat card auto-fit ────────────────────────────────────────────────────────
// Binary-searches for the largest font size (px) that keeps each value on one
// line within its card. Runs after the next layout frame so clientWidth is valid.
function fitStatCards() {
  requestAnimationFrame(() => {
    document.querySelectorAll('#stat-cards .card-value').forEach(el => {
      let lo = 12, hi = 26;           // search range in px
      el.style.fontSize = hi + 'px';
      if (el.scrollWidth <= el.clientWidth) return; // already fits
      while (hi - lo > 1) {
        const mid = Math.floor((lo + hi) / 2);
        el.style.fontSize = mid + 'px';
        if (el.scrollWidth <= el.clientWidth) lo = mid; else hi = mid;
      }
      el.style.fontSize = lo + 'px';
    });
  });
}

// ── Utility helpers ───────────────────────────────────────────────────────────

function fmt$(n) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  return "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}

/** Compact dollar format for chart axis ticks: $1.2B, $50M, $500K, $50 */
function fmtCompact$(n) {
  if (n === null || n === undefined || isNaN(n)) return "";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1e9) return sign + "$" + (abs / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
  if (abs >= 1e6) return sign + "$" + (abs / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (abs >= 1e3) return sign + "$" + (abs / 1e3).toFixed(0) + "K";
  return sign + "$" + abs.toFixed(0);
}

function fmtNum(n) {
  return Number(n).toLocaleString("en-US");
}

function showError(msg, scope = null) {
  const el = document.createElement("div");
  el.className = "error-msg";
  if (scope) el.dataset.errorScope = scope;
  el.textContent = "⚠ " + msg;
  document.querySelector("main").prepend(el);
}

function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function loadFilerProfile(slug) {
  if (!filerCache[slug]) {
    filerCache[slug] = DL.getFilerDetail(slug);
  }
  return filerCache[slug];
}

// ── Tab switching ─────────────────────────────────────────────────────────────
// Only the in-page tabs; the others (Explore, Login…) are plain links.
document.querySelectorAll(".tab-btn[data-tab]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => {
      b.classList.remove("active");
      b.setAttribute("aria-selected", "false");
    });
    document.querySelectorAll(".tab-panel").forEach(p => {
      p.classList.remove("active");
      p.hidden = true;
    });
    btn.classList.add("active");
    btn.setAttribute("aria-selected", "true");
    activeTab = btn.dataset.tab;
    const panel = document.getElementById("tab-" + activeTab);
    panel.classList.add("active");
    panel.hidden = false;
    renderActiveTab();
  });
});

let activeTabLoadVersion = 0;
async function renderActiveTab() {
  const tab = activeTab;
  const version = ++activeTabLoadVersion;
  document.querySelectorAll('.error-msg[data-error-scope="tab-load"]').forEach(el => el.remove());
  try {
    await loaders[tab]();
  } catch (err) {
    if (version !== activeTabLoadVersion || tab !== activeTab) return;
    console.error(`Error loading ${tab}:`, err);
    showError(`Could not load data for "${tab}" tab. ${err.message}`, "tab-load");
  }
}

function onStateChange() {
  renderActiveTab();
}

// ── Chart helpers ─────────────────────────────────────────────────────────────

function makeBarChart(containerId, labels, values, label, color = "#3182ce") {
  const el = document.getElementById(containerId);
  if (!el) return null;
  el.className = "echart-container-tall";

  const chart = initEChart(el);
  chart.setOption({
    tooltip: {
      trigger: 'axis',
      position: tooltipPosition,
      axisPointer: { type: 'shadow' },
      formatter: params => params[0] ? `${params[0].name}<br/>${fmt$(params[0].value)}` : '',
    },
    grid: {
      left: IS_MOBILE ? 120 : 180,
      right: 30,
      top: 10,
      bottom: 20,
    },
    xAxis: {
      type: 'value',
      axisLabel: {
        formatter: v => fmtCompact$(v),
        fontSize: IS_MOBILE ? 9 : 12,
      },
    },
    yAxis: {
      type: 'category',
      data: labels.slice().reverse(),
      axisLabel: {
        fontSize: IS_MOBILE ? 10 : 12,
        width: IS_MOBILE ? 110 : 170,
        overflow: 'truncate',
        ellipsis: '...',
      },
    },
    series: [{
      type: 'bar',
      data: values.slice().reverse(),
      itemStyle: {
        color: color,
        borderRadius: [0, 4, 4, 0],
      },
      barMaxWidth: 30,
    }],
  });

  return chart;
}

function makeLineChart(containerId, labels, datasets) {
  const el = document.getElementById(containerId);
  if (!el) return null;
  el.className = "echart-container";

  const chart = initEChart(el);
  const series = datasets.map(ds => ({
    name: ds.label,
    type: 'line',
    data: ds.data,
    symbol: 'none',
    lineStyle: {
      color: ds.borderColor,
      width: 2,
      type: ds.lineType || 'solid',
    },
    itemStyle: { color: ds.borderColor },
    areaStyle: ds.fill ? { color: ds.backgroundColor, opacity: 0.3 } : undefined,
    smooth: ds.tension ? true : false,
  }));

  // Only show legend entries for datasets that opt in (or all if none specify)
  const legendEntries = datasets.filter(d => d.showInLegend !== false).map(d => d.label);
  const hasHiddenLegend = datasets.some(d => d.showInLegend === false);
  const legendRows = Math.ceil(legendEntries.length / (IS_MOBILE ? 2 : 3));
  const legendHeight = legendRows * (IS_MOBILE ? 18 : 22) + (hasHiddenLegend ? 20 : 10);

  chart.setOption({
    tooltip: {
      trigger: 'axis',
      position: tooltipPosition,
      formatter: params => {
        let html = `<div style="font-weight:600;margin-bottom:4px">${params[0]?.axisValue || ''}</div>`;
        params.forEach(p => {
          html += `<div>${p.marker} ${p.seriesName}: <strong>${fmt$(p.value)}</strong></div>`;
        });
        return html;
      },
    },
    legend: {
      data: legendEntries,
      top: 0,
      textStyle: { fontSize: IS_MOBILE ? 9 : 11 },
      itemGap: IS_MOBILE ? 8 : 15,
      padding: [0, 0, 5, 0],
    },
    grid: {
      left: IS_MOBILE ? 10 : 20,
      right: IS_MOBILE ? 10 : 20,
      top: legendHeight,
      bottom: IS_MOBILE ? 80 : 40,
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: labels,
      axisLabel: {
        fontSize: IS_MOBILE ? 9 : 11,
        rotate: 45,
      },
    },
    yAxis: {
      type: 'value',
      axisLabel: {
        formatter: v => fmtCompact$(v),
        fontSize: IS_MOBILE ? 9 : 12,
      },
    },
    dataZoom: IS_MOBILE ? [
      { type: 'slider', start: 70, end: 100, height: 25, bottom: 5 },
      { type: 'inside' },
    ] : [{ type: 'inside' }],
    graphic: hasHiddenLegend ? [{
      type: 'text',
      left: 'center',
      top: legendRows * (IS_MOBILE ? 20 : 24) + 4,
      style: {
        text: 'Solid = Contributions · Dashed = Expenditures',
        fontSize: IS_MOBILE ? 8 : 10,
        fill: '#9ca3af',
      },
    }] : [],
    series,
  });

  return chart;
}

// ── Donor → Filer linking layer ────────────────────────────────────────────────

async function ensureDonorFilerMap() {
  if (donorFilerMap !== null) return;
  try {
    donorFilerMap = await DL.getBlob("donor_filer_map");
  } catch {
    donorFilerMap = {};
  }
}

function lookupDonorFiler(donorName) {
  if (!donorFilerMap || !donorName) return null;
  const key = donorName.toLowerCase().trim();
  return donorFilerMap[key] || null;
}

// Build a filer index lookup by slug (for preview data)
function filerIndexBySlug() {
  const map = {};
  filerIndex.forEach(f => { map[f.slug] = f; });
  return map;
}

// Render a donor name cell, making it a link if it maps to a filer
function renderDonorCell(name) {
  const match = lookupDonorFiler(name);
  if (!match) return esc(name);

  if (match.confidence === "high") {
    return `<a class="donor-filer-link" href="#" data-slug="${esc(match.slug)}"
              data-donor="${esc(name)}" tabindex="0">${esc(name)}</a>`;
  }
  if (match.confidence === "ambiguous" && match.candidates) {
    return `<span class="donor-ambiguous" data-donor="${esc(name)}" tabindex="0"
              title="Multiple filer matches — click to choose">${esc(name)} <span class="ambig-icon">⋯</span></span>`;
  }
  return esc(name);
}

// Preview popover state
let _previewPopover = null;
let _previewTimeout = null;

function hidePreviewPopover() {
  if (_previewPopover) {
    _previewPopover.remove();
    _previewPopover = null;
  }
}

async function showPreviewPopover(slug, anchorEl) {
  hidePreviewPopover();
  const filterKey = donorFilterKey();
  const start = state.dateStart || null;
  const end = state.dateEnd || null;
  const pop = document.createElement("div");
  pop.className = "donor-preview-popover";
  pop.innerHTML = '<div class="preview-loading">Loading…</div>';
  document.body.appendChild(pop);
  _previewPopover = pop;
  const isCurrent = () => {
    if (_previewPopover !== pop) return false;
    if (filterKey !== donorFilterKey()) {
      hidePreviewPopover();
      return false;
    }
    return true;
  };

  // Position near the anchor
  const rect = anchorEl.getBoundingClientRect();
  pop.style.position = "fixed";
  pop.style.left = Math.min(rect.right + 8, window.innerWidth - 320) + "px";
  pop.style.top = Math.max(rect.top - 10, 8) + "px";

  try {
    const profile = await loadFilerProfile(slug);
    if (!isCurrent()) return;

    // Always compute from timeline for consistency
    const hasDate = state.dateStart || state.dateEnd;
    const fullTl = profile.timeline || [];
    const popTlRows = hasDate ? filterMonthRows(fullTl) : fullTl;
    const stats = statsFromTimeline(
      popTlRows, profile.beginning_balances, fullTl,
      hasDate ? activeCashThroughMonth() : null,
    );

    // Match the Donors tab's exact transaction dates for the top-five snippet.
    const donorData = hasDate ? await DL.getDonors({
      start, end, filerIds: donorFilerIds(profile, { slug }),
    }) : { all_time: profile.top_donors };
    if (!isCurrent()) return;
    const donors = normalizeDonorRows(donorData.all_time).slice(0, 5);

    pop.innerHTML = `
      <div class="preview-header">${esc(profile.name)}</div>
      <div class="preview-stat"><span>Contributions:</span><span>${fmt$(stats.totalIn)}</span></div>
      <div class="preview-stat"><span>Expenditures:</span><span>${fmt$(stats.totalOut)}</span></div>
      <div class="preview-stat"><span>Cash on Hand:</span><span>${fmt$(stats.cashOnHand)}</span></div>
      ${donors.length ? '<div class="preview-donors-label">Top 5 Donors</div>' : ''}
      ${donors.map(d => `<div class="preview-donor"><span>${esc(d.name)}</span><span>${fmt$(d.total)}</span></div>`).join("")}
      <div class="preview-action">Click to view full details</div>
    `;

    // Reposition if it goes offscreen
    const popRect = pop.getBoundingClientRect();
    if (popRect.bottom > window.innerHeight) {
      pop.style.top = Math.max(8, window.innerHeight - popRect.height - 8) + "px";
    }
    if (popRect.right > window.innerWidth) {
      pop.style.left = Math.max(8, rect.left - popRect.width - 8) + "px";
    }
  } catch {
    if (isCurrent()) {
      pop.innerHTML = '<div class="preview-loading">Could not load preview</div>';
    }
  }
}

// Ambiguous chooser popover
function showAmbiguousChooser(donorName, anchorEl) {
  hidePreviewPopover();
  const match = lookupDonorFiler(donorName);
  if (!match || match.confidence !== "ambiguous" || !match.candidates) return;

  const pop = document.createElement("div");
  pop.className = "donor-preview-popover donor-chooser";
  pop.innerHTML = `
    <div class="preview-header">Multiple filers match "${esc(donorName)}"</div>
    <div class="chooser-list">
      ${match.candidates.map(c =>
        `<button class="chooser-item" data-slug="${esc(c.slug)}">${esc(c.name)}</button>`
      ).join("")}
    </div>
    <div class="preview-action">Select a filer to view</div>
  `;
  document.body.appendChild(pop);
  _previewPopover = pop;

  const rect = anchorEl.getBoundingClientRect();
  pop.style.position = "fixed";
  pop.style.left = Math.min(rect.right + 8, window.innerWidth - 280) + "px";
  pop.style.top = Math.max(rect.top - 10, 8) + "px";

  pop.addEventListener("click", e => {
    const btn = e.target.closest(".chooser-item");
    if (!btn) return;
    const slug = btn.dataset.slug;
    hidePreviewPopover();
    navigateToFiler(slug);
  });
}

function navigateToFiler(slug) {
  const entry = filerIndex.find(f => f.slug === slug);
  if (!entry) return;
  // Add this filer to selection and switch to overview
  state.selectedFilers = [entry];
  document.querySelectorAll(".chip").forEach(c => c.remove());
  const chipRow = document.getElementById("chip-input-row");
  const input = document.getElementById("filer-search-input");
  const chip = document.createElement("div");
  chip.className = "chip";
  chip.innerHTML =
    `<span class="chip-label">${esc(entry.name)}</span>` +
    `<button class="chip-remove" data-slug="${esc(entry.slug)}" aria-label="Remove ${esc(entry.name)}">×</button>`;
  chipRow.insertBefore(chip, input);
  input.value = "";
  document.getElementById("filter-clear-btn").hidden = false;
  // Switch to overview tab
  document.querySelectorAll(".tab-btn").forEach(b => {
    b.classList.remove("active");
    b.setAttribute("aria-selected", "false");
  });
  document.querySelectorAll(".tab-panel").forEach(p => {
    p.classList.remove("active");
    p.hidden = true;
  });
  const overviewBtn = document.querySelector('.tab-btn[data-tab="overview"]');
  overviewBtn.classList.add("active");
  overviewBtn.setAttribute("aria-selected", "true");
  activeTab = "overview";
  document.getElementById("tab-overview").classList.add("active");
  document.getElementById("tab-overview").hidden = false;
  renderActiveTab();
}

// Global delegated event handlers for donor links and previews
document.addEventListener("click", e => {
  // Handle donor-filer link clicks
  const link = e.target.closest(".donor-filer-link");
  if (link) {
    e.preventDefault();
    hidePreviewPopover();
    navigateToFiler(link.dataset.slug);
    return;
  }
  // Handle ambiguous donor clicks
  const ambig = e.target.closest(".donor-ambiguous");
  if (ambig) {
    e.preventDefault();
    showAmbiguousChooser(ambig.dataset.donor, ambig);
    return;
  }
  // Click outside popover dismisses it
  if (_previewPopover && !_previewPopover.contains(e.target)) {
    hidePreviewPopover();
  }
});

document.addEventListener("mouseenter", e => {
  const link = e.target.closest(".donor-filer-link");
  if (!link) return;
  clearTimeout(_previewTimeout);
  _previewTimeout = setTimeout(() => showPreviewPopover(link.dataset.slug, link), 200);
}, true);

document.addEventListener("mouseleave", e => {
  const link = e.target.closest(".donor-filer-link");
  if (!link) return;
  clearTimeout(_previewTimeout);
  // Delay hide so user can move to popover
  _previewTimeout = setTimeout(() => {
    if (_previewPopover && !_previewPopover.matches(":hover")) {
      hidePreviewPopover();
    }
  }, 300);
}, true);

document.addEventListener("focusin", e => {
  const link = e.target.closest(".donor-filer-link");
  if (link) {
    clearTimeout(_previewTimeout);
    showPreviewPopover(link.dataset.slug, link);
  }
});

document.addEventListener("focusout", e => {
  const link = e.target.closest(".donor-filer-link");
  if (link) {
    clearTimeout(_previewTimeout);
    _previewTimeout = setTimeout(hidePreviewPopover, 300);
  }
});

// ── Sortable table helper ─────────────────────────────────────────────────────

function buildSortableTable(tableId, rows, columns, searchEl = null) {
  // columns: [{key, label, fmt, cls, linkDonor}]
  const table = document.getElementById(tableId);
  if (!table) return;
  const tbody = table.querySelector("tbody");
  let sortCol = columns[columns.length - 1].key;  // default sort: last column (numeric)
  let sortDir = "desc";

  function visible() {
    if (!searchEl || !searchEl.value.trim()) return rows;
    const q = searchEl.value.trim().toLowerCase();
    return rows.filter(r => String(r[columns[0].key] || "").toLowerCase().includes(q));
  }

  function render() {
    const sorted = [...visible()].sort((a, b) => {
      let va = a[sortCol], vb = b[sortCol];
      if (typeof va === "string") va = va.toLowerCase();
      if (typeof vb === "string") vb = vb.toLowerCase();
      if (va < vb) return sortDir === "asc" ? -1 : 1;
      if (va > vb) return sortDir === "asc" ? 1 : -1;
      return 0;
    });
    tbody.innerHTML = sorted.map((row, i) => {
      const cells = columns.map(col => {
        const v = row[col.key];
        let display;
        if (col.linkDonor && v && donorFilerMap) {
          display = renderDonorCell(v);
        } else {
          display = col.fmt ? col.fmt(v) : (v != null ? esc(String(v)) : "—");
        }
        return `<td class="${col.cls || ""}">${display}</td>`;
      }).join("");
      return `<tr><td>${i + 1}</td>${cells}</tr>`;
    }).join("");
  }

  // Header sort listeners
  table.querySelectorAll("th.sortable").forEach(th => {
    th.onclick = () => {
      const col = th.dataset.col;
      if (sortCol === col) {
        sortDir = sortDir === "asc" ? "desc" : "asc";
      } else {
        sortCol = col;
        sortDir = "desc";
      }
      table.querySelectorAll("th.sortable").forEach(t => {
        t.classList.remove("sort-asc", "sort-desc");
      });
      th.classList.add("sort-" + sortDir);
      render();
    };
  });

  if (searchEl) searchEl.oninput = render;

  render();
}

// ── Filer selector ────────────────────────────────────────────────────────────

function initFilerSelector() {
  const input       = document.getElementById("filer-search-input");
  const dropdown    = document.getElementById("filer-dropdown");
  const chipRow     = document.getElementById("chip-input-row");
  const clearBtn    = document.getElementById("filter-clear-btn");
  const dateStartEl = document.getElementById("date-start");
  const dateEndEl   = document.getElementById("date-end");

  // Search by committee name, candidate name, race/office, or filer ID.
  const fuse = new Fuse(filerIndex, {
    keys: [
      { name: "name",            weight: 2 },
      { name: "candidate_name",  weight: 1.5 },
      { name: "office_district", weight: 1 },
      { name: "filer_id",        weight: 0.5 },
    ],
    threshold: 0.3,
  });

  function searchFilers(q) {
    // Digits → exact/prefix filer-ID lookup first ("4792" → Friends of Tina Kotek)
    if (/^\d+$/.test(q)) {
      const exact = filerIndex.filter(f => String(f.filer_id) === q);
      const prefix = filerIndex.filter(f => String(f.filer_id).startsWith(q) && String(f.filer_id) !== q);
      const byId = exact.concat(prefix);
      if (byId.length) return byId.slice(0, 20);
    }
    return fuse.search(q).map(r => r.item).slice(0, 20);
  }

  /** Secondary line in the dropdown: candidate · race · election */
  function filerSubtitle(item) {
    const parts = [item.candidate_name, item.office_district || item.office, item.election]
      .filter(Boolean);
    return parts.length ? `<span class="filer-option-sub">${esc(parts.join(" · "))}</span>` : "";
  }

  let dropdownItems = [];
  let highlightIdx  = -1;

  function renderChips() {
    chipRow.querySelectorAll(".chip").forEach(c => c.remove());
    state.selectedFilers.forEach(entry => {
      const chip = document.createElement("div");
      chip.className = "chip";
      chip.innerHTML =
        `<span class="chip-label">${esc(entry.name)}</span>` +
        `<button class="chip-remove" data-slug="${esc(entry.slug)}" aria-label="Remove ${esc(entry.name)}">×</button>`;
      chipRow.insertBefore(chip, input);
    });
  }

  function addFiler(entry) {
    if (state.selectedFilers.find(f => f.slug === entry.slug)) return;
    state.selectedFilers.push(entry);
    renderChips();
    input.value = "";
    closeDropdown();
    updateClearBtn();
    onStateChange();
  }

  function removeFiler(slug) {
    state.selectedFilers = state.selectedFilers.filter(f => f.slug !== slug);
    renderChips();
    updateClearBtn();
    onStateChange();
  }

  function openDropdown(items) {
    dropdownItems = items;
    highlightIdx  = -1;
    if (items.length) {
      dropdown.innerHTML = items.map((item, i) =>
        `<li role="option" data-slug="${esc(item.slug)}" data-idx="${i}">${esc(item.name)}${filerSubtitle(item)}</li>`
      ).join("");
    } else {
      dropdown.innerHTML = `<li class="no-results">No results</li>`;
    }
    dropdown.hidden = false;
    document.getElementById("filer-combobox").setAttribute("aria-expanded", "true");
  }

  function closeDropdown() {
    dropdown.hidden = true;
    document.getElementById("filer-combobox").setAttribute("aria-expanded", "false");
    highlightIdx = -1;
  }

  function updateHighlight() {
    dropdown.querySelectorAll("li[data-slug]").forEach((li, i) => {
      li.classList.toggle("selected", i === highlightIdx);
    });
  }

  function updateClearBtn() {
    clearBtn.hidden = !(state.selectedFilers.length > 0 || state.dateStart || state.dateEnd);
  }

  input.addEventListener("focus", () => {
    const q = input.value.trim();
    openDropdown(q ? searchFilers(q) : filerIndex.slice(0, 20));
  });

  input.addEventListener("input", () => {
    const q = input.value.trim();
    openDropdown(q ? searchFilers(q) : filerIndex.slice(0, 20));
  });

  input.addEventListener("blur", () => {
    setTimeout(closeDropdown, 150);
  });

  input.addEventListener("keydown", e => {
    const items = dropdown.querySelectorAll("li[data-slug]");
    if (e.key === "ArrowDown") {
      e.preventDefault();
      highlightIdx = Math.min(highlightIdx + 1, items.length - 1);
      updateHighlight();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      highlightIdx = Math.max(highlightIdx - 1, 0);
      updateHighlight();
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (highlightIdx >= 0 && dropdownItems[highlightIdx]) {
        addFiler(dropdownItems[highlightIdx]);
      }
    } else if (e.key === "Escape") {
      closeDropdown();
    } else if (e.key === "Backspace" && input.value === "" && state.selectedFilers.length > 0) {
      const last = state.selectedFilers[state.selectedFilers.length - 1];
      removeFiler(last.slug);
    }
  });

  dropdown.addEventListener("mousedown", e => {
    const li = e.target.closest("li[data-slug]");
    if (!li) return;
    e.preventDefault();
    const idx = parseInt(li.dataset.idx);
    if (!isNaN(idx) && dropdownItems[idx]) {
      addFiler(dropdownItems[idx]);
    }
  });

  chipRow.addEventListener("click", e => {
    const btn = e.target.closest(".chip-remove");
    if (!btn) return;
    removeFiler(btn.dataset.slug);
  });

  // Date inputs update state but don't trigger a re-render — use Apply for that
  dateStartEl.addEventListener("change", () => {
    state.dateStart = dateStartEl.value;
    updateClearBtn();
  });

  dateEndEl.addEventListener("change", () => {
    state.dateEnd = dateEndEl.value;
    updateClearBtn();
  });

  document.getElementById("filter-apply-btn").addEventListener("click", () => {
    onStateChange();
  });

  // updateClearBtn is local to this initialiser, so hand it over explicitly.
  initCyclePresets(dateStartEl, dateEndEl, updateClearBtn);

  clearBtn.addEventListener("click", () => {
    state.selectedFilers = [];
    state.dateStart = "";
    state.dateEnd   = "";
    // Restore default range display
    dateStartEl.value = "2006-01-01";
    dateEndEl.value   = (summaryData && summaryData.date_range_end) || "";
    renderChips();
    updateClearBtn();
    onStateChange();
  });
}

// ── Election-cycle presets ────────────────────────────────────────────────────

/** An Oregon election cycle runs Dec of the pre-election year → Nov of the
 *  election year (2026 cycle = 2024-12-01 … 2026-11-30). */
function cycleRange(electionYear) {
  return { start: `${electionYear - 2}-12-01`, end: `${electionYear}-11-30` };
}

/** The three most recent even-numbered election years, ending with the current
 *  cycle. Derived from today's date so the buttons advance without a code edit. */
function recentCycles(count = 3) {
  const now = new Date();
  // Before December, the cycle ending this even year is still the current one.
  let latest = now.getFullYear();
  if (latest % 2 !== 0) latest += 1;                       // odd year → next even
  else if (now.getMonth() >= 11) latest += 2;              // Dec → next cycle began
  return Array.from({ length: count }, (_, i) => latest - i * 2);
}

function initCyclePresets(dateStartEl, dateEndEl, updateClearBtn) {
  const box = document.getElementById("cycle-presets");
  if (!box) return;
  const years = recentCycles(3);
  box.insertAdjacentHTML("beforeend", years.map(y => {
    const { start, end } = cycleRange(y);
    return `<button class="cycle-btn" data-start="${start}" data-end="${end}" data-year="${y}"
              title="${start} to ${end}">${y}</button>`;
  }).join(""));

  box.addEventListener("click", e => {
    const btn = e.target.closest(".cycle-btn");
    if (!btn) return;
    const active = btn.classList.contains("active");
    box.querySelectorAll(".cycle-btn").forEach(b => b.classList.remove("active"));
    if (active) {                       // clicking the active cycle clears it
      state.dateStart = "";
      state.dateEnd = "";
      dateStartEl.value = "2006-01-01";
      dateEndEl.value = (summaryData && summaryData.date_range_end) || "";
    } else {
      btn.classList.add("active");
      state.dateStart = btn.dataset.start;
      state.dateEnd = btn.dataset.end;
      dateStartEl.value = state.dateStart;
      dateEndEl.value = state.dateEnd;
    }
    updateClearBtn();
    onStateChange();
  });
}

// ── Overview ──────────────────────────────────────────────────────────────────

async function loadOverview() {
  if (!summaryData) summaryData = await DL.getBlob("summary");
  if (!timelineData) {
    timelineData = await DL.getBlob("timeline");
  }

  document.getElementById("last-updated").textContent = summaryData.last_updated
    ? new Date(summaryData.last_updated).toLocaleString() : "—";

  // Pre-fill date inputs with the dataset's full range (only if still blank)
  const dsEl = document.getElementById("date-start");
  const deEl = document.getElementById("date-end");
  if (!dsEl.value) dsEl.value = "2006-01-01";
  if (!deEl.value && summaryData.date_range_end)   deEl.value = summaryData.date_range_end;

  const n = state.selectedFilers.length;

  if (n === 0) {
    // Selected candidates use their own profiles. Statewide chart labels
    // must not block opening an individual or multi-committee overview.
    if (!byTypeDataGlobal) byTypeDataGlobal = await DL.getBlob("by_contributor_type");
    setOverviewTiles("statewide");
    renderOverviewGlobal();
    await loadTimeline();
  } else if (n === 1) {
    const profile = await loadFilerProfile(state.selectedFilers[0].slug);
    renderOverviewSingleFiler(profile);
    await loadTimeline();
  } else {
    const profiles = await Promise.all(state.selectedFilers.map(f => loadFilerProfile(f.slug)));
    renderOverviewMultiFiler(profiles);
    await loadTimeline();
  }
  renderFilerRaceHeader();
}

/**
 * Candidate / race context for the committees currently filtered on.
 *
 * Answers "who is this and what are they running for" without leaving Overview,
 * and for legislative seats lists the rest of the field from the ORESTAR
 * candidate filing roster (activity_snapshot.legislative_map) — the ballot
 * record, not the committee's self-reported election, which goes stale.
 */
function renderFilerRaceHeader() {
  const box = document.getElementById("filer-race-header");
  if (!box) return;
  const sel = state.selectedFilers;
  if (!sel.length || !filerIndex) { box.hidden = true; return; }

  const partyTag = p => {
    const s = (p || "").toLowerCase();
    const cls = s.startsWith("dem") ? "D" : s.startsWith("rep") ? "R" : "other";
    return p ? `<span class="frh-party ${cls}">${cls === "other" ? esc(p) : cls}</span>` : "";
  };

  const lm = (typeof activitySnapshot !== "undefined" && activitySnapshot)
    ? activitySnapshot.legislative_map : null;
  const CHAMBER = { "State Representative": "house", "State Senator": "senate" };

  const rows = sel.map(f => {
    const row = filerIndex.find(r => r.slug === f.slug) || {};
    const office = row.office || "";
    const district = (row.office_district || "").match(/(\d+)\w*\s+District/i);
    const isCand = row.committee_type === "Candidate Committee";

    const bits = [];
    if (row.candidate_name) bits.push(esc(row.candidate_name));
    if (row.office_district) bits.push(esc(row.office_district));
    else if (row.committee_type) bits.push(esc(row.committee_type));
    if (row.filer_id) bits.push(`Filer ID ${esc(row.filer_id)}`);

    // Rest of the field, from the filing roster. Statewide races are keyed by
    // office name (no district); legislative races by district number.
    //
    // Show the field ONLY when this committee is itself in the race. A
    // committee keeps its office/district long after the candidate stops
    // running — "Kate Brown Committee" still reads office=Governor from its
    // 2018 filing — so matching on office or district alone would present a
    // former officeholder as part of the current contest. The roster decides
    // who is running; anything not in it falls through to "not on the ballot".
    let fieldHtml = "";
    const chamber = CHAMBER[office];
    const swEntry = lm && lm.statewide ? lm.statewide[office] : null;
    const distEntry = (lm && chamber && district)
      ? (lm[chamber] || {})[String(parseInt(district[1], 10))]
      : null;
    const entry = swEntry || distEntry;
    const inThisRace = !!(entry && entry.candidates.some(c => c.slug && c.slug === f.slug));
    if (entry && inThisRace) {
      const raceLabel = swEntry ? office : (row.office_district || office);
      fieldHtml = `
        <div class="frh-field">
          <div class="frh-field-label">${esc(lm.election || "Race")} · ${esc(raceLabel)}</div>
          ${entry.candidates.map(c => {
            const me = c.slug && c.slug === f.slug;
            const nm = esc(c.candidate_name || c.name || "");
            const label = me ? `<span class="frh-opp-self">${nm}</span>`
              : (c.slug ? `<a data-slug="${esc(c.slug)}">${nm}</a>` : nm);
            const amt = c.slug ? fmt$(c.raised_cycle)
              : `<span class="frh-opp-none">no committee</span>`;
            return `<div class="frh-opp"><span>${label}${partyTag(c.party)}</span><span>${amt}</span></div>`;
          }).join("")}
          <a class="frh-link" href="#" data-scroll-map>View race map ↓</a>
        </div>`;
    }
    // A candidate committee with no roster entry isn't on the current ballot.
    if (!fieldHtml && isCand && row.office_district) {
      fieldHtml = `<div class="frh-field"><span class="frh-opp-none">Not on the current ballot`
        + `${row.election ? ` — committee reports “${esc(row.election)}”` : ""}</span></div>`;
    }

    return `<div class="frh-row">
        <div class="frh-title">${esc(row.name || f.name)}${partyTag(row.party)}</div>
        ${bits.length ? `<div class="frh-meta">${bits.join(" · ")}</div>` : ""}
        ${fieldHtml}
      </div>`;
  }).join("");

  box.innerHTML = rows;
  box.hidden = false;
  // Opponent links reuse the existing in-page filer navigation
  box.querySelectorAll("a[data-slug]").forEach(a =>
    a.addEventListener("click", e => { e.preventDefault(); selectFilerBySlug(a.dataset.slug); }));
  // The map is on this page now, so scroll to it instead of navigating.
  box.querySelectorAll("a[data-scroll-map]").forEach(a =>
    a.addEventListener("click", e => {
      e.preventDefault();
      document.querySelector(".rc-box")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }));
}

// Statewide cash leaves out Independent Expenditure Filers: ORESTAR publishes
// no balance for them, so their spending is not subtracted as negative cash.
// Said in the Cash on Hand tooltip on the statewide view only.
function setStatewideCashNote(show) {
  const note = document.getElementById("coh-statewide-note");
  if (!note) return;
  const excluded = (show && summaryData && summaryData.global_cash_excludes_independent_filers) || null;
  if (!excluded || !excluded.filers) {
    note.innerHTML = "";
    return;
  }
  const n = excluded.filers;
  const spent = Math.abs(Number(excluded.cash_excluded) || 0);
  note.innerHTML = `<br><strong>Statewide total:</strong> Excludes ${fmtNum(n)} independent ` +
    `expenditure filer${n === 1 ? "" : "s"}. ORESTAR publishes no cash balance for them, so ` +
    `the ${esc(fmt$(spent))} they spent is not subtracted from cash here. Their spending ` +
    `is still counted in Expenditures.`;
}

function renderOverviewGlobal() {
  setStatewideCashNote(true);
  // Always compute stat cards from timeline — ensures consistency with Account Summary
  const hasDate = state.dateStart || state.dateEnd;
  const fullGlobalTl = timelineData || [];
  // New global rows already include every committee's opening anchor in their
  // cash_balance_net trajectory. Legacy data needs the old aggregate anchor.
  const hasExactCashTimeline = timelineCashSchemaIsComplete(fullGlobalTl);
  const globalBeginBal = hasExactCashTimeline
    ? {}
    : (summaryData.global_beginning_balances || {});
  const tlRows = hasDate
    ? filterMonthRows(fullGlobalTl)
    : fullGlobalTl;
  const { totalIn, totalInKind, totalOut, cashOnHand, count } = statsFromTimeline(
    tlRows, globalBeginBal, fullGlobalTl,
    hasDate ? activeCashThroughMonth() : null,
  );
  document.getElementById("stat-contributions").textContent = fmt$(totalIn);
  document.getElementById("stat-inkind").textContent        = fmt$(totalInKind);
  document.getElementById("stat-expenditures").textContent  = fmt$(totalOut);
  document.getElementById("stat-cash-on-hand").textContent  = fmt$(cashOnHand);
  document.getElementById("stat-transactions").textContent  = count ? fmtNum(count) : "—";
  updateCohIndicator(null);  // No single-filer indicator for global view
  const cohNote = document.getElementById("coh-note");
  if (cohNote) cohNote.textContent = "Calculated from transaction data";
  document.getElementById("stat-cards").hidden             = false;
  document.getElementById("filer-comparison-grid").hidden  = true;
  document.getElementById("overview-donut-box").hidden     = false;
  document.getElementById("overview-timeline-box").hidden  = false;

  document.getElementById("overview-donut-title").textContent = "Contributions by Donor Type";

  // byTypeDataGlobal is now {all_time:[...], by_year:{...}, by_month:{...}}
  let byTypeRows;
  if (Array.isArray(byTypeDataGlobal)) {
    byTypeRows = byTypeDataGlobal; // old cached format — show as-is
  } else if (byTypeDataGlobal) {
    if (hasDate && byTypeDataGlobal.by_month) {
      byTypeRows = mergeTypeByMonth(byTypeDataGlobal.by_month);
    } else if (hasDate) {
      byTypeRows = mergeTypeByYear(byTypeDataGlobal.by_year || {}, yearsInRange());
    } else {
      byTypeRows = byTypeDataGlobal.all_time || [];
    }
  } else {
    byTypeRows = [];
  }
  resetChartBox();
  if (byTypeRows.length && byTypeDataGlobal && byTypeDataGlobal.by_month) {
    document.getElementById("overview-donut-title").textContent = "Who Funds Oregon Campaigns";
    // Filter by_month data to the active date range
    let filteredByMonth = byTypeDataGlobal.by_month;
    if (hasDate) {
      const sm = state.dateStart ? state.dateStart.slice(0, 7) : null;
      const em = state.dateEnd ? state.dateEnd.slice(0, 7) : null;
      filteredByMonth = {};
      for (const [m, entries] of Object.entries(byTypeDataGlobal.by_month)) {
        if ((!sm || m >= sm) && (!em || m <= em)) filteredByMonth[m] = entries;
      }
    }
    makeStackedAreaChart("chart-contributor-type", filteredByMonth, byTypeRows);
  } else if (byTypeRows.length) {
    makeSimplePieChart("chart-contributor-type", byTypeRows);
  }

  document.getElementById("filer-comparison-grid").hidden = true;
  renderAcctSummary(null);  // No per-filer account summary in global view
  fitStatCards();

  // Fundraising Pulse and Races to Watch are fixed-period snapshots —
  // hide them when a custom date filter is active
  const pulseEl = document.getElementById("campaign-pulse");
  const racesEl = document.getElementById("races-to-watch");
  if (hasDate) {
    if (pulseEl) pulseEl.hidden = true;
    if (racesEl) racesEl.hidden = true;
  } else {
    loadCampaignPulse();
  }
  loadPartyFundraising();
}

// ── Discrepancy severity thresholds (absolute dollar difference) ──────────
function discrepancySeverity(absDisc) {
  if (absDisc < 500) return "gray";
  if (absDisc <= 2000) return "yellow";
  return "red";
}

function formatTimestamp(value) {
  if (!value) return "unknown";
  const d = typeof value === "number" || /^\d+(\.\d+)?$/.test(String(value))
    ? new Date(Number(value) * 1000)
    : new Date(value);
  if (Number.isNaN(d.getTime())) return "unknown";
  return d.toLocaleString("en-US", {
    hour: "numeric", minute: "2-digit", hour12: true,
    month: "long", day: "numeric", year: "numeric",
  });
}

function updateCohIndicator(profile) {
  const ind = document.getElementById("coh-indicator");
  if (!ind) return;

  // Remove any existing popover
  const existing = ind.parentElement.querySelector(".disc-popover");
  if (existing) existing.remove();

  if (!profile) {
    ind.hidden = true;
    return;
  }

  if (balanceUnpublished(profile)) {
    ind.hidden = false;
    ind.className = "coh-indicator coh-estimated";
    ind.textContent = "IE";
    ind.setAttribute("tabindex", "0");
    ind.setAttribute("title", UNPUBLISHED_BALANCE_NOTE);
    return;
  }

  // Compute COH from timeline (same as stat card) instead of using stored value
  const fullTl = profile.timeline || [];
  const calcCoh = statsFromTimeline(fullTl, profile.beginning_balances).cashOnHand;

  const acct = profile.orestar_account_summary || {};
  const latestOrestar = acct.ending_cash_balance != null ? acct.ending_cash_balance : null;
  const comparison = profile.orestar_comparison || {};
  const paired = comparison.status === "paired"
    && comparison.delta_at_capture != null;
  const comparable = paired && comparison.actionable === true;
  const orestarEnding = comparable ? comparison.orestar_cash_on_hand : latestOrestar;
  const appAtCapture = comparable ? comparison.app_cash_on_hand : null;
  const disc = comparable ? Math.round(comparison.delta_at_capture * 100) / 100 : 0;
  const absDisc = Math.abs(disc);
  const treatmentText = cashTreatmentNoteText(profile);

  // A closed committee gets a plain label, not a warning triangle.
  //
  // ORESTAR keeps issuing an annual Account Summary after a committee stops
  // operating, and those statements are entirely blank — no activity and no
  // cash balance. That is the record saying the committee is finished, so a
  // leftover difference against our transaction history is expected rather
  // than alarming, and flagging it as a discrepancy told readers a committee
  // had a problem when it simply no longer exists.
  //
  // Note this is NOT the same as dormant: a dormant committee has ORESTAR
  // carrying a real balance forward while filing nothing, which IS worth a
  // warning. The zero cash balance is the difference.
  if (profile.closed && pairedSummaryEvidenceIsCurrent(profile)) {
    ind.hidden = false;
    ind.className = "coh-indicator coh-closed";
    ind.textContent = "Closed";
    ind.setAttribute("tabindex", "0");
    const since = profile.closed_since ? ` since ${profile.closed_since}` : "";
    const finalBal = profile.closed_final_balance != null
      ? ` Its last reported balance was ${fmt$(profile.closed_final_balance)}.`
      : "";
    ind.setAttribute(
      "title",
      `ORESTAR reports no activity and a $0.00 balance for this committee${since}.` +
      finalBal +
      (absDisc > 0.01
        ? ` The ${fmt$(absDisc)} shown here is what our transaction history still carries; a closed committee's records often omit the final disbursement.`
        : "") +
      (treatmentText ? ` ${treatmentText}` : "")
    );
    return;
  }

  // Warn only on a paired snapshot.  Today's app balance against an older
  // ORESTAR page is useful context, but it is not evidence of a discrepancy.
  if (comparable && absDisc > 0.01) {
    const severity = discrepancySeverity(absDisc);
    ind.hidden = false;
    ind.className = `coh-indicator coh-warn-${severity}`;
    ind.textContent = "\u26a0";
    ind.setAttribute("tabindex", "0");
    ind.setAttribute("role", "button");
    ind.setAttribute("aria-label", `ORESTAR discrepancy: ${fmt$(absDisc)}`);
    ind.removeAttribute("title");

    // Build rich popover
    const scrapeTs = comparison.captured_at || acct.scrape_ts || 0;

    const popover = document.createElement("div");
    popover.className = "disc-popover";
    popover.setAttribute("role", "tooltip");
    // ORESTAR's summary against ORESTAR's own itemised transactions.
    //
    // Only shown where the coverage survey has confirmed we hold every row
    // ORESTAR reports, because a line difference looks identical whether
    // ORESTAR over-reports or we under-hold, and only the survey can tell
    // those apart. Friends of Ted Ferrioli 2006 is the case: the summary
    // claims $213,961.90 of expenditures against 161 itemised transactions
    // totalling $173,367.64, with exactly one $40,000 row in ORESTAR's list.
    const itemisedNote = (() => {
      const yd = profile.orestar_yearly_comparisons || {};
      const yrs = Object.keys(yd).filter(y => {
        const v = yd[y];
        return v && v.rows_complete && v.summary_vs_itemised != null
               && Math.abs(v.summary_vs_itemised) > 0.01;
      });
      if (!yrs.length) return "";
      yrs.sort();
      const v = yd[yrs[0]];
      const more = yrs.length > 1 ? ` (also ${yrs.slice(1).join(", ")})` : "";
      return `<div class="disc-note">ORESTAR's ${yrs[0]} account summary does not agree with ` +
             `ORESTAR's own itemised transactions for that year${more}: the summary is ` +
             `${fmt$(Math.abs(v.summary_vs_itemised))} ` +
             `${v.summary_vs_itemised > 0 ? "above" : "below"} what its listed transactions ` +
             `total. We hold every row ORESTAR reports for this committee, and the balance ` +
             `above follows that itemised record.</div>`;
    })();

    // ORESTAR's own summaries failing to carry a balance forward.
    //
    // Year N's ending balance should be year N+1's beginning balance. For 444
    // of 6,511 committees it is not, and the jumps have no transactions behind
    // them — the silent years were checked against ORESTAR directly, which
    // reports zero records for them. Hood River County Democrats has eight
    // such jumps summing to $6,192.22: its entire difference, to the cent.
    //
    // "Accounts for" is only claimed when the jumps actually sum to the
    // difference on screen; otherwise the breaks are reported as present
    // without asserting they are the whole story.
    // Breaks that fall at certificate years are no longer unexplained: the
    // balance carries them as restatement rows (see certificateNoteText). Only
    // the remainder is described as a gap, and "rolled forward from ORESTAR's
    // transaction record" is claimed only when nothing was restated \u2014 it
    // would be false for a committee whose balance includes ghost rows.
    const chainNote = (() => {
      const cb = profile.orestar_chain_breaks;
      if (!cb || !cb.boundaries || Math.abs(cb.total || 0) <= 0.01) return "";
      const restated = new Set(((profile.certificate_restatements) || [])
        .map(r => `${(r.boundary || [])[0]}-${(r.boundary || [])[1]}`));
      const detail = cb.detail || [];
      const isRestated = b => restated.has(`${b.from}-${b.to}`);
      const coveredTotal = detail.filter(isRestated).reduce((s, b) => s + b.amount, 0);
      // detail is capped server-side; only claim full coverage when every
      // boundary is actually listed and restated.
      const allCovered = detail.length === cb.boundaries && detail.every(isRestated);
      const remaining = (cb.total || 0) - coveredTotal;
      const explains = Math.abs(Math.abs(remaining) - Math.abs(disc)) <= 1.0;
      const where = detail.filter(b => !isRestated(b)).slice(0, 3)
        .map(b => `${b.from}\u2192${b.to} ${b.amount > 0 ? "+" : "\u2212"}${fmt$(Math.abs(b.amount))}`)
        .join(", ");
      if (allCovered) {
        return `<div class="disc-note">ORESTAR's own account summaries do not carry this ` +
               `committee's balance forward across ${cb.boundaries} year ` +
               `${cb.boundaries === 1 ? "boundary" : "boundaries"}, totalling ` +
               `${fmt$(Math.abs(cb.total))}. Every one falls at a Certificate of Limited ` +
               `Contributions and Expenditures year, when the committee did not itemize, ` +
               `and this balance includes them as restatement rows.</div>`;
      }
      const certPart = Math.abs(coveredTotal) > 0.01
        ? `${fmt$(Math.abs(coveredTotal))} of it falls at Certificate of Limited ` +
          `Contributions and Expenditures years and is included in this balance as ` +
          `restatement rows. The rest \u2014 `
        : "";
      return `<div class="disc-note">ORESTAR's own account summaries do not carry this ` +
             `committee's balance forward across ${cb.boundaries} year ` +
             `${cb.boundaries === 1 ? "boundary" : "boundaries"}, totalling ` +
             `${fmt$(Math.abs(cb.total))}. ${certPart}` +
             `${where ? `${where}, ` : ""}${fmt$(Math.abs(remaining))} with no ` +
             `certificate or transactions in those years to explain it` +
             `${certPart ? " \u2014" : ""} is not restated. ` +
             (explains ? `That accounts for the difference above. ` : ``) +
             (Math.abs(coveredTotal) > 0.01
               ? `</div>`
               : `The balance here is rolled forward from ORESTAR's own transaction record.</div>`);
    })();

    const treatmentNote = treatmentText
      ? `<div class="disc-note">${esc(treatmentText)}</div>`
      : "";

    const appChange = comparison.app_balance_change_since_capture || 0;
    const changeRow = Math.abs(appChange) > 0.01
      ? `<div class="disc-row"><span>App balance change since capture:</span><span>${appChange >= 0 ? "+" : ""}${fmt$(appChange)}</span></div>`
      : "";
    popover.innerHTML = `
      <div class="disc-row"><span>ORESTAR balance at capture:</span><span>${fmt$(orestarEnding)}</span></div>
      <div class="disc-row"><span>App balance at capture:</span><span>${fmt$(appAtCapture)}</span></div>
      <div class="disc-row disc-diff"><span>Difference at capture:</span><span>${disc >= 0 ? "+" : ""}${fmt$(disc)}</span></div>
      <div class="disc-row"><span>App balance now:</span><span>${fmt$(calcCoh)}</span></div>
      ${changeRow}
      ${itemisedNote}
      ${chainNote}
      ${treatmentNote}
      <div class="disc-ts">App snapshot built: ${formatTimestamp(comparison.app_snapshot_created_at)}<br>ORESTAR summary read: ${formatTimestamp(scrapeTs)}</div>
    `;
    popover.hidden = true;
    ind.parentElement.style.position = "relative";
    ind.parentElement.appendChild(popover);

    function showPopover() { popover.hidden = false; }
    function hidePopover() { popover.hidden = true; }
    ind.addEventListener("mouseenter", showPopover);
    ind.addEventListener("mouseleave", hidePopover);
    ind.addEventListener("focus", showPopover);
    ind.addEventListener("blur", hidePopover);
    ind.addEventListener("click", () => { popover.hidden = !popover.hidden; });
  } else if (latestOrestar == null) {
    // Genuinely unchecked: no account summary on file to compare against.
    //
    // This used to test `src === "calculated"`, which is not the same question.
    // Every balance is calculated from transactions — that is the design — so
    // the label is now "calculated" for all 7,268 filers, and every one of them
    // was being told "no ORESTAR beginning balance data scraped yet" while
    // holding a freshly scraped summary. The check has to be whether an ORESTAR
    // figure EXISTS, not what the source is called.
    ind.hidden = false;
    ind.className = "coh-indicator coh-estimated";
    ind.textContent = "EST";
    ind.setAttribute("tabindex", "0");
    ind.title = "No ORESTAR account summary scraped yet, so this balance is unchecked." +
      (treatmentText ? ` ${treatmentText}` : "");
  } else if (!comparable) {
    // Legacy summaries have a timestamp but no saved app-side snapshot.  Do
    // not turn their live difference into a warning while the new paired
    // capture sweep converges.
    ind.hidden = false;
    ind.className = "coh-indicator coh-estimated";
    ind.textContent = "SYNC";
    ind.setAttribute("tabindex", "0");
    const syncTitle = paired
      ? "The app or ORESTAR state has changed since this capture window. A fresh paired summary is required before treating the old difference as current."
      : "ORESTAR summary available, but it has not yet been paired with a matching app transaction snapshot.";
    ind.title = syncTitle + (treatmentText ? ` ${treatmentText}` : "");
  } else if (treatmentText) {
    // A successful evidence-backed adjustment deserves disclosure even when it
    // makes the two balances agree. Silence here left a retained transaction
    // apparently missing from the arithmetic with no explanation.
    ind.hidden = false;
    ind.className = "coh-indicator coh-adjusted";
    ind.textContent = "ADJ";
    ind.setAttribute("tabindex", "0");
    ind.title = treatmentText;
  } else {
    // ORESTAR agrees with the calculation. That is the good case, and it earns
    // silence — same as the multi-filer cards, and what .coh-ok intends.
    ind.hidden = true;
  }
}

function orestarAbsentNoteText(profile) {
  const w = (profile && (profile.orestar_absent || profile.orestar_withdrawn)) || {};
  if (!w.count) return "";
  return `${w.count} transaction${w.count === 1 ? "" : "s"}` +
         `${w.amount ? ` totalling ${fmt$(Math.abs(w.amount))}` : ""} ` +
         `${w.count === 1 ? "is" : "are"} still listed here but no longer returned by ` +
         `ORESTAR's exact transaction search. ORESTAR does not count ` +
         `${w.count === 1 ? "it" : "them"} toward this balance, so neither does the ` +
         `figure shown. The record${w.count === 1 ? " is" : "s are"} kept rather than deleted.`;
}

// Summary-derived loan treatment and closure are safe to present only when the
// summary was captured against this exact canonical transaction scope. Older
// profile JSON can still contain metadata from a prior aggregation, so the UI
// independently fails closed when the pair is stale or unverified.
function pairedSummaryEvidenceIsCurrent(profile) {
  const comparison = (profile && profile.orestar_comparison) || {};
  return comparison.status === "paired"
    && comparison.scope_digest_matches_capture === true
    && comparison.orestar_data_changed_since_capture === false
    && comparison.annual_summary_refresh_needed === false;
}

function pairedAnnualTreatmentEvidenceIsCurrent(profile) {
  return !!profile
    && profile.annual_summary_treatment_basis === "paired_year_digest_v1";
}

// Why a balance can leave out a transaction the committee plainly filed.
//
// Exempt loan principal is held out of cash for years where ORESTAR's paired
// statement reports no exempt loans received (see the exempt-loan block in
// process.py). Committee for SAIF Keeping is the case this exists for:
// ORESTAR lists a $665,242.33 exempt loan in its transaction record and
// reports $128.13 of cash for the same year. A reader who looks that
// transaction up is owed a reason it is missing from the total, rather than
// left to conclude the figure is broken.
function exemptLoanNoteText(profile) {
  if (!pairedAnnualTreatmentEvidenceIsCurrent(profile)) return "";
  const ex = (profile && profile.exempt_loans_excluded) || {};
  const years = Object.keys(ex).sort();
  if (!years.length) return "";
  const total = years.reduce((s, y) => s + (Number(ex[y]) || 0), 0);
  const those = years.length === 1 ? "that year" : "those years";
  return `${fmt$(total)} of exempt loan principal (${years.join(", ")}) is not ` +
         `counted here: ORESTAR's paired account summary for ${those} reports no ` +
         `exempt loans received, so ORESTAR does not treat it as cash either. ` +
         `ORESTAR reported exempt loans inconsistently before about 2013; from ` +
         `2014 on it records them and this balance counts them.`;
}

function nonexemptLoanNoteText(profile) {
  if (!pairedAnnualTreatmentEvidenceIsCurrent(profile)) return "";
  const treatment = (profile && profile.nonexempt_loan_cash_treatment) || {};
  const years = Object.keys(treatment).sort().reverse();
  if (!years.length) return "";
  const parts = years.slice(0, 3).map(year => {
    const item = treatment[year] || {};
    return `${year}: ${fmt$(item.orestar_counted || 0)} counted versus ` +
           `${fmt$(item.transaction_total || 0)} in eligible held rows`;
  });
  const more = years.length > 3
    ? `; plus ${years.length - 3} earlier year${years.length - 3 === 1 ? "" : "s"}`
    : "";
  return `Non-exempt loan cash treatment follows ORESTAR's paired annual ` +
         `summary for this transaction snapshot (${parts.join("; ")}${more}). ` +
         `The filed loan rows remain visible in historical totals.`;
}

// "2013–2015 and 2017" from [2013, 2014, 2015, 2017].
function formatYearRanges(years) {
  const ys = [...new Set(years.map(Number).filter(Number.isFinite))].sort((a, b) => a - b);
  const runs = [];
  for (const y of ys) {
    const last = runs[runs.length - 1];
    if (last && y === last[1] + 1) last[1] = y;
    else runs.push([y, y]);
  }
  const parts = runs.map(([a, b]) => (a === b ? `${a}` : `${a}–${b}`));
  return parts.length <= 1 ? (parts[0] || "")
    : `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

// Certificates of Limited Contributions and Expenditures (see
// scraper/orestar_certificates.py). A committee under a certificate does not
// itemize, so ORESTAR's year totals show nothing while money still moves;
// ORESTAR records the result only by opening a later year at a different
// balance. The pipeline carries each of those restatements as a derived ghost
// row, so this balance follows ORESTAR's figures — and a reader looking at the
// transaction list is owed the reason the balance moved without a filed row.
// Signed dollars WITH cents. fmt$ rounds to whole dollars, which is right for
// totals but turns a -$0.08 restatement into "-$0" — here the cents are the
// explanation.
function fmtSignedCents(v) {
  const n = Number(v) || 0;
  return `${n < 0 ? "\u2212" : "+"}$` +
    Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function certificateNoteText(profile) {
  const rows = (profile && profile.certificate_restatements) || [];
  if (!rows.length) return "";
  const years = Object.keys((profile && profile.orestar_certificates) || {});
  const sum = key => rows.reduce((s, r) => s + (Number(r[key]) || 0), 0);
  const applied = sum("amount");
  const yearDiff = sum("certificate_year_difference");
  const restated = rows.reduce(
    (s, r) => s + (Number(r.orestar_restatement != null ? r.orestar_restatement : r.amount) || 0), 0);
  const parts = rows.slice(0, 4).map(r => `${r.year} ${fmtSignedCents(r.amount)}`);
  const more = rows.length > 4 ? `; plus ${rows.length - 4} more` : "";
  const lead = `This committee held Certificates of Limited Contributions and ` +
    `Expenditures for ${formatYearRanges(years)}, which exempt it from itemizing ` +
    `transactions, so ORESTAR's year totals for those years do not capture ` +
    `everything that moved. ORESTAR records it by restating a later opening ` +
    `balance`;
  const tail = ` as derived restatement rows taken from ORESTAR's own figures; ` +
    `they are not filed transactions.`;
  // Part of a restatement can instead reconcile ORESTAR's certificate-year
  // total with the rows shown here — e.g. a transaction ORESTAR counted in two
  // years. Disclosed whenever present, so nothing is folded in silently.
  if (Math.abs(yearDiff) <= 0.01) {
    return `${lead}: ${fmtSignedCents(applied)} (${parts.join("; ")}${more}). ` +
      `This balance includes those amounts${tail}`;
  }
  return `${lead} by ${fmtSignedCents(restated)}. After reconciling ORESTAR's ` +
    `certificate-year totals with the filed transactions shown here ` +
    `(${fmtSignedCents(yearDiff)} — for example, a transaction ORESTAR counted ` +
    `in two years), this balance applies ${fmtSignedCents(applied)} ` +
    `(${parts.join("; ")}${more})${tail}`;
}

// Amended originals ORESTAR still counts (see _drop_superseded in
// scraper/process.py). Amending a transaction normally expires the original;
// sometimes ORESTAR leaves it live, and its summary then counts both versions.
// The balance follows ORESTAR, so every such pair is named by transaction ID
// for the reader to look up. Cents matter here: these are single rows.
function fmtCents(v) {
  const n = Number(v) || 0;
  return `${n < 0 ? "\u2212" : ""}$` +
    Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function liveOriginalPairText(item) {
  const amended = (item.amended || []).map(a =>
    `#${a.tran_id}` + (a.sub_type && a.sub_type !== item.sub_type ? ` (${a.sub_type})` : ""));
  const when = item.tran_date ? `, ${item.tran_date}` : "";
  const what = [item.amount != null ? fmtCents(item.amount) : "", item.sub_type || ""]
    .filter(Boolean).join(" ");
  return `#${item.original_id} (${what}${when}), amended as ${amended.join(" and ")}`;
}

function liveOriginalNoteText(profile) {
  const items = (profile && profile.orestar_live_originals) || [];
  if (!items.length) return "";
  const one = items.length === 1;
  return `ORESTAR still counts ${one ? "an original transaction" : `${items.length} original transactions`} ` +
    `after ${one ? "it was" : "they were"} amended: ${items.map(liveOriginalPairText).join("; ")}. ` +
    `Amending a transaction normally retires the original, but ORESTAR never marked ` +
    `${one ? "this one" : "these"} expired: its own search still lists ` +
    `${one ? "it" : "them"} next to the amendment, and its account summary still ` +
    `counts ${one ? "it" : "them"}. This balance counts ${one ? "it" : "them"} the same way.`;
}

// Transactions ORESTAR deleted after we had fetched them (_apply_deletions in
// scraper/process.py). A deletion is filed as its own record naming the row it
// deletes; ORESTAR then stops counting the row, and so does this balance. Each
// is named by both IDs, which ORESTAR's transaction history page shows together.
function deletionText(item) {
  const verb = (item.tran_type === "C" || item.tran_type === "OR") ? "from" : "to";
  const what = [item.amount != null ? fmtCents(item.amount) : "", item.sub_type || ""]
    .filter(Boolean).join(" ");
  const who = item.contributor_payee ? ` ${verb} ${item.contributor_payee}` : "";
  const when = item.tran_date ? `, ${item.tran_date}` : "";
  const ids = (item.removed_ids || []).map(id => `#${id}`).join(" and ");
  return `${ids} (${what}${who}${when}), deleted by #${item.deletion_id}` +
    (item.deleted_on ? ` on ${item.deleted_on}` : "");
}

function deletionNoteText(profile) {
  const items = (profile && profile.orestar_deletions) || [];
  if (!items.length) return "";
  const one = items.length === 1;
  return `ORESTAR deleted ${one ? "a transaction" : `${items.length} transactions`} ` +
    `after ${one ? "it was" : "they were"} first filed: ${items.map(deletionText).join("; ")}. ` +
    `ORESTAR no longer counts deleted transactions, so neither does this balance. ` +
    `The deleted ${one ? "row is" : "rows are"} kept in the pipeline's deletion record.`;
}

// Early-era amendment chains (scraper/orestar_amendment_chains.py). In its
// first years ORESTAR's summaries counted every amendment of a transaction,
// less one per deletion, including versions its search now hides as expired
// or deleted. The pipeline applies that only where it reproduces ORESTAR's
// own summary line to the cent, and each chain is named version by version so
// the reader can open ORESTAR's history page for any of them.
function amendmentChainText(chain, bucket) {
  const versions = (chain && chain.versions) || [];
  const original = versions.find(v => v.status === "Original") || versions[0];
  if (!original) return "";
  const amended = versions.filter(v => v.status === "Amended");
  const deleted = versions.filter(v => v.status === "Deleted");
  const verb = (bucket === "C" || bucket === "OR") ? "from" : "to";
  let text = `${fmtCents(original.amount)} ${original.sub_type || ""} ${verb} ` +
    `${original.payee || "unnamed"}, ${original.tran_date}: #${original.tran_id}`;
  if (amended.length) {
    text += `, amended as ` + amended.map(v =>
      `#${v.tran_id}` + (v.amount !== original.amount ? ` (${fmtCents(v.amount)})` : "")).join(", ");
  }
  if (deleted.length) {
    text += `, deleted as ` + deleted.map(v => `#${v.tran_id}`).join(", ");
  }
  return text;
}

function amendmentChainNoteText(profile) {
  const items = (profile && profile.orestar_amendment_chains) || [];
  if (!items.length) return "";
  const parts = items.map(item =>
    `${item.year} ${({C: "contributions", E: "expenditures", OR: "other receipts",
                     OD: "other disbursements"})[item.bucket] || "transactions"}, ` +
    `${fmtSignedCents(item.cash_effect)} to cash (` +
    (item.chains || []).map(c => amendmentChainText(c, item.bucket)).join("; ") + `)`);
  return `In its first years ORESTAR counted every amendment of a transaction, less ` +
    `one per deletion, including versions its search now marks expired or deleted. ` +
    `ORESTAR's own figures for this committee still reflect that, so this balance ` +
    `counts them the same way: ${parts.join("; ")}.`;
}

// Discontinued committees (scraper/orestar_closures.py). After a committee
// files a Discontinuation, ORESTAR's later statements are blank and open at
// $0.00, so the last balance vanishes without a transaction. The balance here
// does the same, and says so.
function closureResetNoteText(profile) {
  const items = (profile && profile.orestar_closure_resets) || [];
  if (!items.length) return "";
  return items.map(r => {
    const closed = fmtCents(r.orestar_prior_ending);
    const tail = Math.abs(Number(r.tail_offset) || 0) > 0.005
      ? ` Our own rows in the blank years already move ${fmtSignedCents(r.tail_offset)}, ` +
        `so ${fmtSignedCents(r.amount)} is applied.`
      : "";
    return `This committee filed a Discontinuation (effective ${r.discontinued_on}). ` +
      `ORESTAR's last statement closed at ${closed} for ${r.boundary[0]}; its ${r.year} ` +
      `statement and every one after it are blank and open at $0.00, with no transaction ` +
      `behind the change. This balance does the same.${tail}`;
  }).join(" ");
}

// ORESTAR publishes no cash balance for an Independent Expenditure Filer: its
// own page script hides the balance section and shows only expenditures.
function balanceUnpublished(profile) {
  return !!profile && profile.balance_published === false;
}

const UNPUBLISHED_BALANCE_NOTE =
  "ORESTAR does not publish a cash balance for independent expenditure filers, " +
  "only their expenditures, so none is shown or compared here.";

function cashTreatmentNoteText(profile) {
  return [
    certificateNoteText(profile),
    closureResetNoteText(profile),
    amendmentChainNoteText(profile),
    liveOriginalNoteText(profile),
    deletionNoteText(profile),
    orestarAbsentNoteText(profile),
    nonexemptLoanNoteText(profile),
    exemptLoanNoteText(profile),
  ].filter(Boolean).join(" ");
}

// Build discrepancy indicator HTML for multi-filer cards (inline)
function cohIndicatorHTML(profile) {
  const acct = profile.orestar_account_summary || {};
  const latestOrestar = acct.ending_cash_balance != null ? acct.ending_cash_balance : null;
  const comparison = profile.orestar_comparison || {};
  const paired = comparison.status === "paired" && comparison.delta_at_capture != null;
  const comparable = paired && comparison.actionable === true;
  const disc = comparable ? Math.round(comparison.delta_at_capture * 100) / 100 : 0;
  const absDisc = Math.abs(disc);
  const treatmentText = cashTreatmentNoteText(profile);
  if (balanceUnpublished(profile)) {
    return `<span class="coh-indicator coh-estimated" tabindex="0" title="${esc(UNPUBLISHED_BALANCE_NOTE)}">IE</span>`;
  }
  // Same rule as updateCohIndicator: a finished committee is labelled, not
  // warned about. Kept in step with that function deliberately — two badges
  // describing the same balance differently is worse than either alone.
  if (profile.closed && pairedSummaryEvidenceIsCurrent(profile)) {
    const since = profile.closed_since ? ` since ${profile.closed_since}` : "";
    const tip = `ORESTAR reports no activity and a $0.00 balance for this committee${since}.` +
                (treatmentText ? ` ${treatmentText}` : "");
    return `<span class="coh-indicator coh-closed" tabindex="0" title="${esc(tip)}">Closed</span>`;
  }
  if (latestOrestar == null) {
    const tip = "No ORESTAR account summary scraped yet, so this balance is unchecked" +
      (treatmentText ? ` | ${treatmentText}` : "");
    return `<span class="coh-indicator coh-estimated" tabindex="0" title="${esc(tip)}">EST</span>`;
  }
  if (!comparable) {
    const syncTip = paired
      ? "The app or ORESTAR state has changed since this capture window; refresh the pair before treating its old difference as current"
      : "ORESTAR summary available, but it has not yet been paired with a matching app transaction snapshot";
    const tip = syncTip + (treatmentText ? ` | ${treatmentText}` : "");
    return `<span class="coh-indicator coh-estimated" tabindex="0" title="${esc(tip)}">SYNC</span>`;
  }
  if (absDisc > 0.01) {
    const severity = discrepancySeverity(absDisc);
    const tsText = formatTimestamp(comparison.captured_at || acct.scrape_ts || 0);
    const appTs = formatTimestamp(comparison.app_snapshot_created_at);
    const tip = `ORESTAR capture: ${fmt$(comparison.orestar_cash_on_hand || 0)} | App snapshot: ${fmt$(comparison.app_cash_on_hand || 0)} | Difference: ${fmt$(disc)} | App built: ${appTs} | ORESTAR read: ${tsText}` +
                (treatmentText ? ` | ${treatmentText}` : "");
    return `<span class="coh-indicator coh-warn-${severity}" tabindex="0" title="${esc(tip)}">\u26a0</span>`;
  }
  if (treatmentText) {
    return `<span class="coh-indicator coh-adjusted" tabindex="0" title="${esc(treatmentText)}">ADJ</span>`;
  }
  return '';
}


/**
 * Show only the tiles that still mean something under the current filter.
 *
 * "Compared with past cycles" and the caucus-PAC donors chart fixed sets of
 * statewide committees, so they never describe a filtered selection. The
 * district map is kept only when the filter is one committee that is actually
 * on this cycle's ballot — then it is lit on that district, which is more use
 * than the statewide default. Anything else (a PAC, a former officeholder,
 * several committees at once) hides it, since a map with nothing to point at
 * is just noise.
 *
 * mode: "statewide" | "filer" | "multi"
 */
function setOverviewTiles(mode, profile) {
  // Fixed sets of statewide committees — never a description of a selection.
  const past = document.getElementById("past-cycles-box");
  if (past) past.hidden = mode !== "statewide";
  const caucus = document.getElementById("caucus-donors-box");
  if (caucus) caucus.hidden = mode !== "statewide";

  const mapBox = document.getElementById("legislative-races-box");
  if (!mapBox) return;
  if (mode === "statewide") {
    mapBox.hidden = false;
    if (typeof rcClearSelection === "function") rcClearSelection();
    return;
  }
  const seat = mode === "filer" && typeof rcDistrictForSlug === "function"
    ? rcDistrictForSlug(profile && profile.slug) : null;
  mapBox.hidden = !seat;
  if (seat && typeof rcSelectDistrict === "function") rcSelectDistrict(seat.chamber, seat.district);
}

function renderOverviewSingleFiler(profile) {
  setStatewideCashNote(false);
  const pulseEl = document.getElementById("campaign-pulse");
  if (pulseEl) pulseEl.hidden = true;
  // The comparisons now read this committee's own money rather than the state's.
  if (typeof ccSetScope === "function") ccSetScope("filer", profile);
  setOverviewTiles("filer", profile);
  const partyBox = document.getElementById("party-fundraising-box");
  if (partyBox) partyBox.hidden = true;
  const racesBox = document.getElementById("races-to-watch");
  if (racesBox) racesBox.hidden = true;
  // Always compute stat cards from timeline — ensures consistency with Account Summary
  const hasDate = state.dateStart || state.dateEnd;
  const tlRows = hasDate
    ? filterMonthRows(profile.timeline || [])
    : (profile.timeline || []);
  const fullFilerTl = profile.timeline || [];
  const { totalIn, totalInKind, totalOut, cashOnHand, count } =
    statsFromTimeline(
      tlRows, profile.beginning_balances, fullFilerTl,
      hasDate ? activeCashThroughMonth() : null,
    );

  document.getElementById("stat-contributions").textContent = fmt$(totalIn);
  document.getElementById("stat-inkind").textContent        = fmt$(totalInKind);
  document.getElementById("stat-expenditures").textContent  = fmt$(totalOut);
  document.getElementById("stat-cash-on-hand").textContent  =
    balanceUnpublished(profile) ? "—" : fmt$(cashOnHand);
  document.getElementById("stat-transactions").textContent  = count ? fmtNum(count) : "—";
  updateCohIndicator(profile);
  const cohNote = document.getElementById("coh-note");
  if (cohNote) {
    cohNote.textContent = balanceUnpublished(profile)
      ? "Not published by ORESTAR for independent expenditure filers"
      : "Calculated from transaction data";
  }
  document.getElementById("stat-cards").hidden             = false;
  document.getElementById("filer-comparison-grid").hidden  = true;
  document.getElementById("overview-donut-box").hidden     = false;
  document.getElementById("overview-timeline-box").hidden  = false;

  document.getElementById("overview-donut-title").textContent =
    `Contributions by Donor Type — ${profile.name}`;

  const byTypeRows = hasDate && profile.by_contributor_type_by_month
    ? mergeTypeByMonth(profile.by_contributor_type_by_month)
    : hasDate
      ? mergeTypeByYear(profile.by_contributor_type_by_year || {}, yearsInRange())
      : (profile.by_contributor_type || []);
  resetChartBox();
  if (byTypeRows.length) {
    if (profile.by_contributor_type_by_month) {
      makeStackedAreaChart("chart-contributor-type", profile.by_contributor_type_by_month, byTypeRows);
    } else {
      makeSimplePieChart("chart-contributor-type", byTypeRows);
    }
  }

  document.getElementById("filer-comparison-grid").hidden = true;
  renderAcctSummary(profile);
  fitStatCards();
}

function renderOverviewMultiFiler(profiles) {
  setStatewideCashNote(false);
  const pulseEl = document.getElementById("campaign-pulse");
  if (pulseEl) pulseEl.hidden = true;
  if (typeof ccSetScope === "function") ccSetScope("none");
  setOverviewTiles("multi");
  const partyBox = document.getElementById("party-fundraising-box");
  if (partyBox) partyBox.hidden = true;
  const racesBox = document.getElementById("races-to-watch");
  if (racesBox) racesBox.hidden = true;
  document.getElementById("stat-cards").hidden             = true;
  document.getElementById("filer-comparison-grid").hidden  = false;
  document.getElementById("overview-donut-box").hidden     = false;
  document.getElementById("overview-timeline-box").hidden  = false;

  document.getElementById("overview-donut-title").textContent =
    "Funding Mix Comparison";

  const hasDate = state.dateStart || state.dateEnd;

  // Build radar chart + table in a two-card layout
  resetChartBox();
  const donutBox = document.getElementById("overview-donut-box");

  // Create the two-card layout wrapper
  let radarLayout = donutBox.querySelector(".radar-layout");
  if (radarLayout) radarLayout.remove();
  radarLayout = document.createElement("div");
  radarLayout.className = "radar-layout";

  // Card 1: Radar chart
  const radarCard = document.createElement("div");
  radarCard.className = "radar-card";
  const radarChartDiv = document.createElement("div");
  radarChartDiv.id = "chart-radar-multi";
  radarChartDiv.className = IS_MOBILE ? "echart-container-radar" : "echart-container";
  radarCard.appendChild(radarChartDiv);
  radarLayout.appendChild(radarCard);

  // Card 2: Table (built later)
  const tableCard = document.createElement("div");
  tableCard.className = "radar-card radar-card-table";
  radarLayout.appendChild(tableCard);

  // Hide the main chart-contributor-type (used for single-filer stacked area)
  const mainChart = document.getElementById("chart-contributor-type");
  if (mainChart) mainChart.style.display = "none";

  donutBox.appendChild(radarLayout);

  const el = radarChartDiv;
  if (el) {
    const chart = initEChart(el);

    // Collect per-filer type data, merge to base types
    const perFilerRows = profiles.map(p => hasDate && p.by_contributor_type_by_month
      ? mergeTypeByMonth(p.by_contributor_type_by_month)
      : hasDate
        ? mergeTypeByYear(p.by_contributor_type_by_year || {}, yearsInRange())
        : (p.by_contributor_type || []));

    // Get all base types across all filers
    const baseTypeSet = new Set();
    perFilerRows.forEach(rows => {
      rows.forEach(r => {
        const base = r.type.endsWith(" (out of state)") ? r.type.slice(0, -15) : r.type;
        baseTypeSet.add(base);
      });
    });
    // Sort by total across all filers
    const baseTotals = {};
    perFilerRows.forEach(rows => {
      rows.forEach(r => {
        const base = r.type.endsWith(" (out of state)") ? r.type.slice(0, -15) : r.type;
        baseTotals[base] = (baseTotals[base] || 0) + r.total;
      });
    });
    const baseTypes = [...baseTypeSet].sort((a, b) => (baseTotals[b] || 0) - (baseTotals[a] || 0));

    // Build percentage data + dollar amounts per filer
    const filerData = profiles.map((p, i) => {
      const rows = perFilerRows[i];
      const byBase = {};
      const topDonorsByBase = {};
      let total = 0;
      rows.forEach(r => {
        const base = r.type.endsWith(" (out of state)") ? r.type.slice(0, -15) : r.type;
        byBase[base] = (byBase[base] || 0) + r.total;
        total += r.total;
        // Merge top donors across in-state/out-of-state
        if (r.top_donors) {
          if (!topDonorsByBase[base]) topDonorsByBase[base] = {};
          r.top_donors.forEach(d => {
            topDonorsByBase[base][d.name] = (topDonorsByBase[base][d.name] || 0) + d.total;
          });
        }
      });
      // Sort and take top 5 per type
      for (const bt of Object.keys(topDonorsByBase)) {
        topDonorsByBase[bt] = Object.entries(topDonorsByBase[bt])
          .sort((a, b) => b[1] - a[1])
          .slice(0, 5)
          .map(([name, amt]) => ({ name, total: amt }));
      }
      return { byBase, topDonorsByBase, total };
    });

    const series = profiles.map((p, i) => {
      const { byBase, total } = filerData[i];
      const color = PALETTE[i % PALETTE.length];
      return {
        name: p.name,
        type: 'radar',
        symbol: 'circle',
        symbolSize: 6,
        lineStyle: { width: 2, color },
        itemStyle: { color },
        areaStyle: { color, opacity: 0.15 },
        data: [{
          value: baseTypes.map(bt => total > 0 ? Math.round(byBase[bt] || 0) / total * 100 : 0),
          name: p.name,
        }],
      };
    });

    chart.setOption({
      tooltip: {
        trigger: 'item',
        position: tooltipPosition,
        formatter: function(params) {
          const name = params.name;
          const values = params.value;
          let html = `<div style="font-weight:600;margin-bottom:4px">${name}</div>`;
          baseTypes.forEach((bt, i) => {
            if (values[i] > 0) {
              html += `<div style="display:flex;justify-content:space-between;gap:12px;font-size:12px">
                <span>${shortTypeName(bt)}</span><span style="font-weight:500">${values[i].toFixed(1)}%</span>
              </div>`;
            }
          });
          return html;
        },
      },
      legend: {
        data: profiles.map(p => p.name),
        bottom: 0,
        itemGap: IS_MOBILE ? 8 : 20,
        textStyle: { fontSize: IS_MOBILE ? 8 : 12 },
      },
      radar: {
        indicator: (() => {
          // Uniform scale: same max on all axes so each ring = same %
          let globalMax = 0;
          series.forEach(s => {
            s.data[0].value.forEach(v => { if (v > globalMax) globalMax = v; });
          });
          const uniformMax = Math.max(Math.ceil(globalMax * 1.15), 1);
          return baseTypes.map(bt => ({ name: shortTypeName(bt), max: uniformMax }));
        })(),
        shape: 'polygon',
        radius: IS_MOBILE ? '35%' : '65%',
        center: ['50%', '48%'],
        axisName: {
          fontSize: IS_MOBILE ? 8 : 11,
          color: '#6b7280',
        },
        splitArea: { areaStyle: { color: ['#fff', '#f9fafb'] } },
        splitLine: { lineStyle: { color: '#e5e7eb' } },
        axisLine: { lineStyle: { color: '#e5e7eb' } },
      },
      series,
    });

    // Build comparison table below the radar chart
    const tableDiv = document.createElement("div");
    tableDiv.className = "radar-compare-table";

    // Build row data for sortable table
    const tableRows = baseTypes.map(bt => {
      const row = { type: bt, amounts: [], donors: [] };
      profiles.forEach((p, i) => {
        row.amounts.push(filerData[i].byBase[bt] || 0);
        row.donors.push(filerData[i].topDonorsByBase[bt] || []);
      });
      return row;
    });

    let sortCol = -1; // -1 = type name, 0+ = filer index
    let sortDir = "desc";

    function renderRadarTable() {
      const sorted = [...tableRows].sort((a, b) => {
        if (sortCol === -1) {
          return sortDir === "asc"
            ? a.type.localeCompare(b.type)
            : b.type.localeCompare(a.type);
        }
        return sortDir === "asc"
          ? a.amounts[sortCol] - b.amounts[sortCol]
          : b.amounts[sortCol] - a.amounts[sortCol];
      });

      let thtml = `<table class="data-table radar-table"><thead><tr>
        <th class="sortable radar-sort" data-col="-1">Donor Type</th>`;
      profiles.forEach((p, i) => {
        const color = PALETTE[i % PALETTE.length];
        thtml += `<th class="sortable radar-sort num" data-col="${i}" style="color:${color}">${esc(p.name)}</th>`;
      });
      thtml += `</tr></thead><tbody>`;

      sorted.forEach(row => {
        thtml += `<tr><td class="radar-table-type">${esc(shortTypeName(row.type))}</td>`;
        profiles.forEach((p, i) => {
          const amt = row.amounts[i];
          const pct = filerData[i].total > 0 ? (amt / filerData[i].total * 100).toFixed(1) : "0.0";
          const donors = row.donors[i];
          const hasDonors = donors.length > 0;
          thtml += `<td class="num${hasDonors ? ' radar-cell-hover' : ''}"${hasDonors ? ` data-bt="${esc(row.type)}" data-fi="${i}"` : ''}>${fmtCompact$(amt)} <span class="radar-pct">${pct}%</span></td>`;
        });
        thtml += `</tr>`;
      });

      thtml += `<tr class="radar-table-total"><td>Total</td>`;
      profiles.forEach((p, i) => {
        thtml += `<td class="num">${fmtCompact$(filerData[i].total)}</td>`;
      });
      thtml += `</tr></tbody></table>`;

      tableDiv.innerHTML = thtml;

      // Mark current sort column
      tableDiv.querySelectorAll(".radar-sort").forEach(th => {
        const col = parseInt(th.dataset.col);
        th.classList.remove("sort-asc", "sort-desc");
        if (col === sortCol) th.classList.add("sort-" + sortDir);
      });

      // Wire sort clicks
      tableDiv.querySelectorAll(".radar-sort").forEach(th => {
        th.style.cursor = "pointer";
        th.addEventListener("click", () => {
          const col = parseInt(th.dataset.col);
          if (col === sortCol) {
            sortDir = sortDir === "desc" ? "asc" : "desc";
          } else {
            sortCol = col;
            sortDir = col === -1 ? "asc" : "desc";
          }
          renderRadarTable();
        });
      });

      // Wire hover tooltips
      wireRadarTooltips();
    }

    // Create a shared tooltip div
    let radarTip = document.getElementById("radar-table-tip");
    if (!radarTip) {
      radarTip = document.createElement("div");
      radarTip.id = "radar-table-tip";
      radarTip.className = "radar-table-tip";
      document.body.appendChild(radarTip);
    }

    function wireRadarTooltips() {
      tableDiv.querySelectorAll(".radar-cell-hover").forEach(cell => {
        const bt = cell.dataset.bt;
        const fi = parseInt(cell.dataset.fi);
        const row = tableRows.find(r => r.type === bt);
        if (!row) return;
        const donors = row.donors[fi] || [];
        if (!donors.length) return;

        cell.addEventListener("mouseenter", () => {
          let html = `<div style="font-weight:600;margin-bottom:4px">Top ${shortTypeName(bt)} Donors</div>`;
          html += `<div style="font-size:11px;color:#6b7280;margin-bottom:4px">${esc(profiles[fi].name)}</div>`;
          donors.forEach(d => {
            html += `<div style="display:flex;justify-content:space-between;gap:12px;font-size:12px;padding:1px 0">
              <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(d.name)}</span>
              <span style="font-weight:500;white-space:nowrap">${fmtCompact$(d.total)}</span>
            </div>`;
          });
          radarTip.innerHTML = html;
          radarTip.hidden = false;
          const rect = cell.getBoundingClientRect();
          let left = rect.left;
          let top = rect.bottom + 6;
          if (left + 260 > window.innerWidth) left = window.innerWidth - 265;
          if (left < 5) left = 5;
          radarTip.style.left = left + "px";
          radarTip.style.top = (top + window.scrollY) + "px";
        });

        cell.addEventListener("mouseleave", () => {
          radarTip.hidden = true;
        });
      });
    }

    tableCard.appendChild(tableDiv);
    renderRadarTable();
  }

  document.getElementById("filer-comparison-grid").innerHTML = profiles.map(p => {
    const hasDate = state.dateStart || state.dateEnd;
    const s = hasDate
      ? statsFromTimeline(
          filterMonthRows(p.timeline || []), p.beginning_balances,
          p.timeline || [], activeCashThroughMonth(),
        )
      : statsFromTimeline(p.timeline || [], p.beginning_balances);
    const tranCount = s.count ? fmtNum(s.count) : "—";
    const cohInd = cohIndicatorHTML(p);
    return `
    <div class="filer-card">
      <div class="filer-card-name">${esc(p.name)}</div>
      <div class="filer-card-stats">
        <div class="filer-card-stat-label">Cash Contributions</div>
        <div class="filer-card-stat-value">${fmt$(s.totalIn)}</div>
        <div class="filer-card-stat-label">In-Kind Received</div>
        <div class="filer-card-stat-value">${fmt$(s.totalInKind)}</div>
        <div class="filer-card-stat-label">Total Expenditures</div>
        <div class="filer-card-stat-value">${fmt$(s.totalOut)}</div>
        <div class="filer-card-stat-label">Cash on Hand ${cohInd}</div>
        <div class="filer-card-stat-value">${balanceUnpublished(p) ? "—" : fmt$(s.cashOnHand)}</div>
        <div class="filer-card-stat-label">Total Transactions</div>
        <div class="filer-card-stat-value">${tranCount}</div>
      </div>
    </div>`;
  }).join("");
  renderAcctSummary(null);  // Hide in multi-filer mode
}

// ── Donors ────────────────────────────────────────────────────────────────────

let donorsLoadVersion = 0;

function donorFilterKey() {
  return JSON.stringify([state.dateStart || null, state.dateEnd || null,
    state.selectedFilers.map(f => [f.slug, f.filer_id ?? null])]);
}

function cleanDonorName(name) {
  if (typeof DN !== "undefined") return DN.display(name);
  const clean = String(name || "").trim().replace(/\s+/g, " ");
  return clean.toLowerCase() === "miscellaneous cash contributions $100 and under"
    ? "Miscellaneous Cash Contributions $100 and under" : clean;
}

function donorRowKey(donor) {
  const name = cleanDonorName(donor.name).toLowerCase();
  // This is a pooled reporting category, never a distinct person or committee.
  if (name === "miscellaneous cash contributions $100 and under") return `name:${name}`;
  return donor.donor_id != null ? `id:${donor.donor_id}` : `name:${name}`;
}

// Normalize before grouping, sorting, or limiting legacy cache/profile rows.
// Keep resolved people with the same display name separate.
function normalizeDonorRows(rows) {
  const merged = new Map();
  (rows || []).forEach(donor => {
    const name = cleanDonorName(donor.name);
    if (!name) return;
    const key = donorRowKey(donor);
    if (!merged.has(key)) merged.set(key, { ...donor, name, cents: 0 });
    merged.get(key).cents += Math.round((Number(donor.total) || 0) * 100);
  });
  return [...merged.values()].map(({ cents, ...donor }) => ({ ...donor, total: cents / 100 }))
    .sort((a, b) => b.total - a.total || a.name.localeCompare(b.name));
}

function normalizeDonorData(data) {
  return {
    all_time: normalizeDonorRows(data?.all_time),
    by_year: Object.fromEntries(Object.entries(data?.by_year || {})
      .map(([year, rows]) => [year, normalizeDonorRows(rows)])),
  };
}

function donorFilerIds(profile, entry) {
  const profileIds = (profile.filer_ids || [])
    .filter(id => id !== null && id !== undefined && String(id).trim());
  const fallback = entry.filer_id ?? filerIndex.find(f => f.slug === entry.slug)?.filer_id;
  const ids = profileIds.length ? profileIds : [fallback];
  const validIds = ids.filter(id => id !== null && id !== undefined && String(id).trim());
  if (!validIds.length) throw new Error(`No filer ID available for ${profile.name || entry.slug}.`);
  return [...new Set(validIds.map(id => String(id).trim()))];
}

async function loadDonors() {
  const version = ++donorsLoadVersion;
  const filterKey = donorFilterKey();
  const isCurrent = () => version === donorsLoadVersion && filterKey === donorFilterKey();
  const selected = state.selectedFilers.map(f => ({ ...f }));
  const start = state.dateStart || null;
  const end = state.dateEnd || null;
  const hasDates = !!(start || end);
  const n = selected.length;
  const viewMode = n > 1 ? "summary" : donorsViewMode;
  let profiles, donorSets;
  // Hide prior results while their replacement loads, including when loading fails.
  document.getElementById("donors-global-view").hidden = true;
  document.getElementById("donors-multi-view").hidden = true;
  try {
    await ensureDonorFilerMap();
    profiles = await Promise.all(selected.map(f => loadFilerProfile(f.slug)));
    if (hasDates) {
      donorSets = await Promise.all(n ? profiles.map((profile, i) => DL.getDonors({
        start, end, filerIds: donorFilerIds(profile, selected[i]),
      })) : [DL.getDonors({ start, end })]);
    } else if (!n) {
      if (!donorsData) donorsData = normalizeDonorData(await DL.getBlob("top_donors"));
      donorSets = [donorsData];
    } else {
      donorSets = profiles.map(profile => ({
        all_time: profile.top_donors,
        by_year: profile.top_donors_by_year,
      }));
    }
  } catch (error) {
    if (!isCurrent()) return;
    throw error;
  }
  if (!isCurrent()) return;
  donorSets = donorSets.map(normalizeDonorData);

  // Attach toggle button listeners once (works for both global and single-filer views)
  const toggleBtns = document.querySelectorAll("#donors-view-toggle .toggle-btn");
  if (toggleBtns[0] && !toggleBtns[0]._listenerAttached) {
    toggleBtns.forEach(btn => {
      btn.addEventListener("click", () => {
        toggleBtns.forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        donorsViewMode = btn.dataset.view;
        renderActiveTab();
      });
      btn._listenerAttached = true;
    });
  }

  if (n === 0) {
    document.getElementById("donors-global-view").hidden      = false;
    document.getElementById("donors-multi-view").hidden       = true;
    document.getElementById("donors-view-toggle").hidden      = false;

    // Hide year selector when date range is active (date range takes precedence)
    document.getElementById("donor-year-group").hidden = viewMode === "by-year" || hasDates;

    const sel = document.getElementById("donor-year");
    if (!hasDates && !sel._listenerAttached) {
      Object.keys(donorsData.by_year || {}).sort().reverse().forEach(yr => {
        sel.insertAdjacentHTML("beforeend", `<option value="${yr}">${yr}</option>`);
      });
      sel.addEventListener("change", () => {
        if (donorsViewMode === "summary" && !state.dateStart && !state.dateEnd) renderDonors(sel.value);
      });
      sel._listenerAttached = true;
    }

    renderGlobalDonorsView(donorSets[0], hasDates, viewMode);

  } else if (n === 1) {
    document.getElementById("donors-global-view").hidden      = false;
    document.getElementById("donors-multi-view").hidden       = true;
    document.getElementById("donors-view-toggle").hidden      = false;
    document.getElementById("donor-year-group").hidden        = true;

    const isByYear = viewMode === "by-year";
    document.getElementById("donors-chart-box").hidden      = isByYear;
    document.getElementById("donors-summary-table").hidden  = isByYear;
    document.getElementById("donors-by-year-view").hidden   = !isByYear;

    if (isByYear) {
      renderDonorsByYear(donorSets[0].by_year, donorSets[0].all_time, hasDates);
    } else {
      renderDonorSummary(donorSets[0].all_time);
    }

  } else {
    // Multi-filer: pivot table
    donorsViewMode = "summary";
    document.getElementById("donors-view-toggle").hidden      = true;
    document.getElementById("donors-global-view").hidden      = true;
    document.getElementById("donors-multi-view").hidden       = false;

    const filerNames = profiles.map(p => p.name);

    const filerDonorMaps = donorSets.map(data => new Map(data.all_time.map(d => [donorRowKey(d), d])));
    // Include donors to any selected committee, even outside the statewide top 1000.
    const selectedDonors = normalizeDonorRows(donorSets.flatMap(data => data.all_time));
    const pivotRows = selectedDonors.map(d => {
      const byFiler = {};
      filerDonorMaps.forEach((map, i) => {
        const hit = map.get(donorRowKey(d));
        if (hit) byFiler[filerNames[i]] = hit.total;
      });
      const total = Object.values(byFiler).reduce((s, v) => s + v, 0);
      return { name: d.name, ...byFiler, total };
    }).filter(row => row.total > 0);

    document.getElementById("donors-multi-title").textContent =
      `Donor Comparison: ${filerNames.join(" vs ")}`;

    let multiSortCol = "total";
    let multiSortDir = "desc";

    const thead = document.getElementById("donors-multi-thead");

    function buildMultiThead() {
      thead.innerHTML = `<tr>
        <th>#</th>
        <th class="sortable" data-col="name">Donor</th>
        ${filerNames.map(n => `<th class="num sortable" data-col="${esc(n)}">${esc(n)}</th>`).join("")}
        <th class="num sortable" data-col="total">Total ($)</th>
      </tr>`;
      thead.querySelectorAll("th.sortable").forEach(th => {
        if (th.dataset.col === multiSortCol) th.classList.add("sort-" + multiSortDir);
        th.addEventListener("click", () => {
          const col = th.dataset.col;
          multiSortDir = multiSortCol === col && multiSortDir === "desc" ? "asc" : "desc";
          multiSortCol = col;
          buildMultiThead();
          renderMultiPivot();
        });
      });
    }

    const multiSearchEl = document.getElementById("donors-multi-search");
    if (multiSearchEl) multiSearchEl.value = "";

    function renderMultiPivot() {
      const q = multiSearchEl ? multiSearchEl.value.trim().toLowerCase() : "";
      const filtered = q ? pivotRows.filter(r => r.name.toLowerCase().includes(q)) : pivotRows;
      const sorted = [...filtered].sort((a, b) => {
        if (multiSortCol === "name") {
          return multiSortDir === "asc"
            ? a.name.localeCompare(b.name)
            : b.name.localeCompare(a.name);
        }
        const va = a[multiSortCol] ?? -1;
        const vb = b[multiSortCol] ?? -1;
        return multiSortDir === "asc" ? va - vb : vb - va;
      });
      const tbody = document.querySelector("#table-donors-multi tbody");
      tbody.innerHTML = sorted.map((row, i) => `<tr>
        <td>${i + 1}</td>
        <td>${donorFilerMap ? renderDonorCell(row.name) : esc(row.name)}</td>
        ${filerNames.map(n => `<td class="num">${row[n] !== undefined ? fmt$(row[n]) : "—"}</td>`).join("")}
        <td class="num">${fmt$(row.total)}</td>
      </tr>`).join("");
    }

    if (multiSearchEl) multiSearchEl.oninput = renderMultiPivot;

    buildMultiThead();
    renderMultiPivot();
  }
}

function renderGlobalDonorsView(data, hasDates, viewMode = donorsViewMode) {
  const isByYear = viewMode === "by-year";
  document.getElementById("donor-year-group").hidden    = isByYear || hasDates;
  document.getElementById("donors-chart-box").hidden    = isByYear;
  document.getElementById("donors-summary-table").hidden = isByYear;
  document.getElementById("donors-by-year-view").hidden = !isByYear;

  if (isByYear) {
    renderDonorsByYear(data.by_year, data.all_time, hasDates);
  } else if (hasDates) {
    renderDonorSummary(data.all_time);
  } else {
    const sel = document.getElementById("donor-year");
    renderDonors(sel.value || "all");
  }
}

function renderDonorsByYear(byYear, allTime, hasDates = false) {
  const years = Object.keys(byYear || {}).sort();

  // Build map: donor name → { [year]: total }
  const donorYearMap = new Map();
  years.forEach(yr => {
    normalizeDonorRows(byYear[yr]).forEach(d => {
      const key = donorRowKey(d);
      if (!donorYearMap.has(key)) donorYearMap.set(key, {});
      donorYearMap.get(key)[yr] = d.total;
    });
  });

  // Rows from allTime (sorted by total desc, capped at 1000); fill in per-year from map
  const allRows = normalizeDonorRows(allTime).slice(0, 1000).map(d => ({
    name: d.name,
    total: d.total,
    ...Object.fromEntries(years.map(yr => [yr, donorYearMap.get(donorRowKey(d))?.[yr] ?? null])),
  }));

  let sortCol = "total";
  let sortDir = "desc";

  const searchEl = document.getElementById("donors-by-year-search");
  if (searchEl) searchEl.value = "";

  const thead = document.getElementById("donors-by-year-thead");
  thead.innerHTML = `<tr>
    <th>#</th>
    <th class="sortable" data-col="name">Donor</th>
    ${years.map(yr => `<th class="num sortable" data-col="${yr}">${yr}</th>`).join("")}
    <th class="num sortable" data-col="total">${hasDates ? "Selected Period" : "All Time"}</th>
  </tr>`;

  function getByYearRows() {
    if (!searchEl || !searchEl.value.trim()) return allRows;
    const q = searchEl.value.trim().toLowerCase();
    return allRows.filter(r => r.name.toLowerCase().includes(q));
  }

  function renderByYearTable() {
    const rows = getByYearRows();
    const sorted = [...rows].sort((a, b) => {
      const va = a[sortCol] ?? -1;
      const vb = b[sortCol] ?? -1;
      if (sortCol === "name") return sortDir === "asc"
        ? a.name.localeCompare(b.name)
        : b.name.localeCompare(a.name);
      return sortDir === "asc" ? va - vb : vb - va;
    });
    const tbody = document.querySelector("#table-donors-by-year tbody");
    tbody.innerHTML = sorted.map((row, i) => `<tr>
      <td>${i + 1}</td>
      <td>${donorFilerMap ? renderDonorCell(row.name) : esc(row.name)}</td>
      ${years.map(yr => `<td class="num">${row[yr] != null ? fmt$(row[yr]) : "—"}</td>`).join("")}
      <td class="num">${fmt$(row.total)}</td>
    </tr>`).join("");
  }

  // Sort listeners (thead is rebuilt each call so no accumulation)
  thead.querySelectorAll("th.sortable").forEach(th => {
    th.addEventListener("click", () => {
      const col = th.dataset.col;
      sortDir = sortCol === col && sortDir === "desc" ? "asc" : "desc";
      sortCol = col;
      thead.querySelectorAll("th.sortable").forEach(t => t.classList.remove("sort-asc", "sort-desc"));
      th.classList.add("sort-" + sortDir);
      renderByYearTable();
    });
  });
  thead.querySelector(`th[data-col="total"]`).classList.add("sort-desc");

  if (searchEl) searchEl.oninput = renderByYearTable;

  renderByYearTable();

  // Scroll to the right so the most recent years are visible first
  const scrollBox = document.querySelector('.by-year-scroll');
  if (scrollBox) {
    setTimeout(() => { scrollBox.scrollLeft = scrollBox.scrollWidth; }, 0);
  }
}

function renderDonors(year) {
  renderDonorSummary(year === "all"
    ? donorsData.all_time
    : donorsData.by_year[year]);
}

function renderDonorSummary(donors) {
  const rows = normalizeDonorRows(donors).slice(0, 1000);
  const top20 = rows.slice(0, 20);
  makeBarChart(
    "chart-top-donors",
    top20.map(r => r.name),
    top20.map(r => r.total),
    "Total Contributions",
    "#3182ce",
  );

  const searchEl = document.getElementById("donors-summary-search");
  if (searchEl) searchEl.value = "";
  buildSortableTable("table-donors", rows, [
    { key: "name",  label: "Donor", linkDonor: true },
    { key: "total", label: "Total ($)", fmt: fmt$, cls: "num" },
  ], searchEl);
}

// ── Recipients ────────────────────────────────────────────────────────────────
// With dates set (a cycle button runs Dec 1 – Nov 30), the rankings come from
// the database for exactly those dates. The top_recipients blob and each
// profile's top_payees_by_year only hold calendar years, and only each year's
// leaders: added up, the 2024 cycle took in all of 2022, and put that year's
// governor's race at the top of it.

let recipientsLoadVersion = 0;

/** "2026 cycle" when the dates are a cycle button's, otherwise the dates. */
function rangeCaption() {
  const start = state.dateStart, end = state.dateEnd;
  if (!start && !end) return "";
  if (start && end) {
    const year = +end.slice(0, 4), cycle = cycleRange(year);
    if (cycle.start === start && cycle.end === end) return `${year} cycle`;
  }
  const day = v => new Date(`${v}T00:00:00`).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  return start && end ? `${day(start)} – ${day(end)}` : start ? `since ${day(start)}` : `through ${day(end)}`;
}

function renderRecipientRows(rows, { valueLabel, color, nameLabel, totalLabel }) {
  const top20 = rows.slice(0, 20);
  makeBarChart("chart-top-recipients", top20.map(r => r.name), top20.map(r => r.total), valueLabel, color);
  buildSortableTable("table-recipients", rows, [
    { key: "name",  label: nameLabel },
    { key: "total", label: totalLabel, fmt: fmt$, cls: "num" },
  ]);
}

async function loadRecipients() {
  const version = ++recipientsLoadVersion;
  const start = state.dateStart || null, end = state.dateEnd || null;
  const ranged = !!(start || end);
  const selected = state.selectedFilers.map(f => ({ ...f }));
  const isCurrent = () => version === recipientsLoadVersion;
  const chartTitle = document.getElementById("recipients-chart-title");
  const tableTitle = document.getElementById("recipients-table-title");
  const caption = ranged ? `, ${rangeCaption()}` : "";
  const failed = error => {
    if (!isCurrent()) return;
    tableTitle.textContent = `Could not load ${ranged ? rangeCaption() : "these figures"}: ${error.message}`;
    renderRecipientRows([], { valueLabel: "", color: "#38a169", nameLabel: "Committee / Candidate", totalLabel: "Total ($)" });
  };

  if (selected.length === 0) {
    const received = { valueLabel: "Total Received", color: "#38a169",
                       nameLabel: "Committee / Candidate", totalLabel: "Total Received ($)" };
    chartTitle.textContent = `Top 20 Recipients (by Contributions Received)${caption}`;
    tableTitle.textContent = `Top 100 Recipients${caption}`;
    // The year selector is for calendar years; dates take its place.
    document.getElementById("recipient-year-group").hidden = ranged;

    if (ranged) {
      tableTitle.textContent += " — loading…";
      let rows;
      try {
        rows = await DL.getRecipients({ start, end });
      } catch (error) {
        return failed(error);
      }
      if (!isCurrent()) return;
      tableTitle.textContent = `Top 100 Recipients${caption}`;
      renderRecipientRows(rows, received);
      return;
    }

    if (!recipientsData) recipientsData = await DL.getBlob("top_recipients");
    if (!isCurrent()) return;
    const sel = document.getElementById("recipient-year");
    if (!sel._listenerAttached) {
      Object.keys(recipientsData.by_year || {}).sort().reverse().forEach(yr => {
        sel.insertAdjacentHTML("beforeend", `<option value="${yr}">${yr}</option>`);
      });
      sel.addEventListener("change", () => renderRecipients(sel.value));
      sel._listenerAttached = true;
    }
    renderRecipients(sel.value || "all");

  } else {
    const profile = await loadFilerProfile(selected[0].slug);
    if (!isCurrent()) return;
    const spent = { valueLabel: "Expenditures", color: "#dd6b20", nameLabel: "Payee", totalLabel: "Total Paid ($)" };
    chartTitle.textContent = `Top Spending by ${profile.name}${caption}`;
    tableTitle.textContent = `Top Spending by ${profile.name}${caption}`;
    let rows = profile.top_payees || [];
    if (ranged) {
      tableTitle.textContent += " — loading…";
      try {
        rows = await DL.getPayees({ filerIds: donorFilerIds(profile, selected[0]), start, end });
      } catch (error) {
        return failed(error);
      }
      if (!isCurrent()) return;
      tableTitle.textContent = `Top Spending by ${profile.name}${caption}`;
    }
    renderRecipientRows(rows, spent);
  }
}

function renderRecipients(year) {
  const rows = year === "all"
    ? recipientsData.all_time
    : (recipientsData.by_year[year] || []);

  const top20 = rows.slice(0, 20);
  makeBarChart(
    "chart-top-recipients",
    top20.map(r => r.name),
    top20.map(r => r.total),
    "Total Received",
    "#38a169",
  );

  buildSortableTable("table-recipients", rows, [
    { key: "name",  label: "Committee / Candidate" },
    { key: "total", label: "Total Received ($)", fmt: fmt$, cls: "num" },
  ]);
}

// ── Timeline ──────────────────────────────────────────────────────────────────

async function loadTimeline() {
  if (!timelineData) {
    timelineData = await DL.getBlob("timeline");
  }

  const n = state.selectedFilers.length;
  const hasDate = state.dateStart || state.dateEnd;

  if (n === 0) {
    renderTimeline("all");
  } else if (n === 1) {
    // Single filer: use same green/amber as global view
    const profile = await loadFilerProfile(state.selectedFilers[0].slug);
    renderTimelineSingleFiler(profile);
  } else {
    const profiles = await Promise.all(state.selectedFilers.map(f => loadFilerProfile(f.slug)));
    renderTimelineMultiFiler(profiles);
  }
}

function renderTimeline(year) {
  // Cycle comparison is a statewide aggregate, so it belongs to this view only.
  if (typeof initCycleCompare === "function") {
    try { initCycleCompare(); } catch (e) { console.warn("[cyclecompare]", e); }
  }
  // Date range takes precedence over year dropdown
  let rows;
  if (state.dateStart || state.dateEnd) {
    rows = filterMonthRows(timelineData);
  } else {
    rows = year === "all"
      ? timelineData.filter(r => r.month >= "2006-01")
      : timelineData.filter(r => r.month.startsWith(year));
  }

  makeLineChart(
    "chart-timeline",
    rows.map(r => r.month),
    [
      {
        label: "Contributions",
        data: rows.map(r => r.contributions || 0),
        borderColor: "#16a34a",
        backgroundColor: "rgba(22,163,74,0.08)",
        fill: true,
        tension: 0.3,
        pointRadius: rows.length > 60 ? 0 : 3,
      },
      {
        label: "Expenditures",
        data: rows.map(r => r.expenditures || 0),
        borderColor: "#d97706",
        backgroundColor: "rgba(217,119,6,0.08)",
        fill: true,
        tension: 0.3,
        pointRadius: rows.length > 60 ? 0 : 3,
      },
    ],
  );
}

function renderTimelineSingleFiler(profile) {
  const sm = state.dateStart ? state.dateStart.slice(0, 7) : null;
  const em = state.dateEnd ? state.dateEnd.slice(0, 7) : null;
  const rows = (profile.timeline || [])
    .filter(t => t.month >= "2006-01")
    .filter(t => (!sm || t.month >= sm) && (!em || t.month <= em));

  makeLineChart(
    "chart-timeline",
    rows.map(r => r.month),
    [
      {
        label: "Contributions",
        data: rows.map(r => r.contributions || 0),
        borderColor: "#16a34a",
        backgroundColor: "rgba(22,163,74,0.08)",
        fill: true,
        tension: 0.3,
        pointRadius: rows.length > 60 ? 0 : 3,
      },
      {
        label: "Expenditures",
        data: rows.map(r => r.expenditures || 0),
        borderColor: "#d97706",
        backgroundColor: "rgba(217,119,6,0.08)",
        fill: true,
        tension: 0.3,
        pointRadius: rows.length > 60 ? 0 : 3,
      },
    ],
  );
}

function renderTimelineMultiFiler(profiles) {
  // Union of all months across profiles, filtered to date range
  const monthSet = new Set();
  profiles.forEach(p => (p.timeline || []).forEach(t => monthSet.add(t.month)));
  const sm = state.dateStart ? state.dateStart.slice(0, 7) : null;
  const em = state.dateEnd   ? state.dateEnd.slice(0, 7)   : null;
  const months = [...monthSet].sort().filter(m => (!sm || m >= sm) && (!em || m <= em));

  const datasets = [];
  profiles.forEach((profile, idx) => {
    const color  = PALETTE[idx % PALETTE.length];
    const byMonth = new Map((profile.timeline || []).map(t => [t.month, t]));

    datasets.push({
      label: `${profile.name}`,
      data: months.map(m => (byMonth.get(m) || {}).contributions || 0),
      borderColor: color,
      backgroundColor: "transparent",
      fill: false,
      tension: 0.3,
      pointRadius: months.length > 60 ? 0 : 3,
      lineType: 'solid',
      showInLegend: true,
    });
    datasets.push({
      label: `${profile.name} (Expend)`,
      data: months.map(m => (byMonth.get(m) || {}).expenditures || 0),
      borderColor: color,
      backgroundColor: "transparent",
      fill: false,
      tension: 0.3,
      pointRadius: months.length > 60 ? 0 : 3,
      lineType: 'dashed',
      showInLegend: false,
    });
  });

  makeLineChart("chart-timeline", months, datasets);
}

// ── Party Fundraising ───────────────────────────────────────────────────
async function loadPartyFundraising() {
  try {
    const data = await DL.getBlob("by_party_type");
    window.partyTypeData = data;   // reused by cyclecompare.js
    if (!data || !data.by_year) return;
    const box = document.getElementById("party-fundraising-box");
    if (!box) return;
    box.hidden = false;

    // Aggregate years by BASE donor type, tracking in-state vs out-of-state separately
    // Respect the active date range filter
    const demIn = {}, demOut = {}, repIn = {}, repOut = {};
    const startYr = state.dateStart ? state.dateStart.slice(0, 4) : "2006";
    const endYr = state.dateEnd ? state.dateEnd.slice(0, 4) : "9999";
    const years = Object.keys(data.by_year).filter(y => y >= startYr && y <= endYr);
    for (const y of years) {
      for (const t of (data.by_year[y]?.Democrat || [])) {
        const isOOS = t.type.endsWith(" (out of state)");
        const base = isOOS ? t.type.slice(0, -15) : t.type;
        if (isOOS) { demOut[base] = (demOut[base] || 0) + t.total; }
        else       { demIn[base]  = (demIn[base] || 0)  + t.total; }
      }
      for (const t of (data.by_year[y]?.Republican || [])) {
        const isOOS = t.type.endsWith(" (out of state)");
        const base = isOOS ? t.type.slice(0, -15) : t.type;
        if (isOOS) { repOut[base] = (repOut[base] || 0) + t.total; }
        else       { repIn[base]  = (repIn[base] || 0)  + t.total; }
      }
    }

    // Get all unique base types, sorted by combined total
    const allTotals = {};
    for (const obj of [demIn, demOut, repIn, repOut]) {
      for (const [k, v] of Object.entries(obj)) allTotals[k] = (allTotals[k] || 0) + v;
    }
    const allTypes = Object.keys(allTotals).sort((a, b) => allTotals[b] - allTotals[a]);

    const displayLabels = allTypes.map(t => shortTypeName(t));

    const el = document.getElementById("chart-party-fundraising");
    if (!el) return;
    el.className = "echart-container-compact";

    // Dispose existing
    const existing = echarts.getInstanceByDom(el);
    if (existing) existing.dispose();

    const chart = echarts.init(el, null, { renderer: 'svg' });
    chart.setOption({
      tooltip: {
        trigger: 'axis',
        position: tooltipPosition,
        axisPointer: { type: 'shadow' },
        formatter: params => {
          if (!params || !params.length) return '';
          let html = `<div style="font-weight:600;margin-bottom:4px">${params[0]?.name || ''}</div>`;
          // Group by party, sum in-state + out-of-state
          let demTotal = 0, repTotal = 0;
          params.forEach(p => {
            if (p.seriesName.startsWith('Dem')) demTotal += p.value || 0;
            if (p.seriesName.startsWith('Rep')) repTotal += p.value || 0;
          });
          params.forEach(p => {
            if (p.value) html += `<div>${p.marker} ${p.seriesName}: <strong>${fmt$(p.value)}</strong></div>`;
          });
          if (demTotal && repTotal) {
            html += `<div style="border-top:1px solid #eee;margin-top:3px;padding-top:3px;font-size:11px;color:#666">
              Dem total: ${fmt$(demTotal)} · Rep total: ${fmt$(repTotal)}</div>`;
          }
          return html;
        },
      },
      legend: {
        data: ['Dem (in-state)', 'Dem (out of state)', 'Rep (in-state)', 'Rep (out of state)'],
        top: 0,
        itemGap: IS_MOBILE ? 10 : 20,
        padding: [0, 0, 8, 0],
        textStyle: { fontSize: IS_MOBILE ? 9 : 11 },
      },
      grid: {
        left: IS_MOBILE ? 10 : 20,
        right: IS_MOBILE ? 10 : 20,
        top: 55,
        bottom: 10,
        containLabel: true,
      },
      xAxis: {
        type: 'category',
        data: displayLabels,
        axisLabel: {
          rotate: 30,
          fontSize: IS_MOBILE ? 9 : 12,
          interval: 0,
        },
      },
      yAxis: {
        type: 'value',
        axisLabel: {
          formatter: v => fmtCompact$(v),
          fontSize: IS_MOBILE ? 9 : 12,
        },
      },
      series: [
        {
          name: 'Dem (in-state)',
          type: 'bar',
          stack: 'dem',
          data: allTypes.map(t => demIn[t] || 0),
          itemStyle: { color: '#2563eb' },
        },
        {
          name: 'Dem (out of state)',
          type: 'bar',
          stack: 'dem',
          data: allTypes.map(t => demOut[t] || 0),
          itemStyle: { color: '#93bbfd' },
        },
        {
          name: 'Rep (in-state)',
          type: 'bar',
          stack: 'rep',
          data: allTypes.map(t => repIn[t] || 0),
          itemStyle: { color: '#dc2626' },
        },
        {
          name: 'Rep (out of state)',
          type: 'bar',
          stack: 'rep',
          data: allTypes.map(t => repOut[t] || 0),
          itemStyle: { color: '#fca5a5' },
        },
      ],
    });
  } catch (e) { console.warn("Party fundraising load failed:", e); }
}

// ── Races to Watch ──────────────────────────────────────────────────────
function renderRacesToWatch() {
  // Built from legislative_map — the same roster the district map uses, so the
  // cards and the map can never disagree. That roster is already the current
  // GENERAL election only, which is what we want now the primary is decided.
  const lm = activitySnapshot && activitySnapshot.legislative_map;
  const el = document.getElementById("races-to-watch");
  const list = document.getElementById("races-list");
  if (!lm || !el || !list) return;

  const races = [];
  for (const [chamber, label] of [["house", "House District"], ["senate", "Senate District"]]) {
    for (const [d, e] of Object.entries(lm[chamber] || {})) {
      races.push({ office: `${label} ${d}`, total: e.total_raised || 0,
                   candidates: e.candidates || [] });
    }
  }
  for (const [office, e] of Object.entries(lm.statewide || {})) {
    races.push({ office, total: e.total_raised || 0, candidates: e.candidates || [] });
  }

  // Top 6 by money — a 2x3 grid rather than a long list.
  const top = races.sort((a, b) => b.total - a.total).slice(0, 6);
  if (!top.length) return;
  el.hidden = false;

  list.innerHTML = top.map(race => {
    const rows = race.candidates.slice(0, 4).map(c => {
      const p = (c.party || "").toLowerCase();
      const cls = p.startsWith("dem") ? "party-d" : p.startsWith("rep") ? "party-r" : "party-other";
      const badge = c.party ? `<span class="pulse-entry-party ${cls}">${esc(c.party.charAt(0))}</span>` : "";
      const nm = esc(c.candidate_name || c.name || "");
      const name = c.slug
        ? `<a href="#" data-slug="${esc(c.slug)}">${nm}</a>`
        : `<span class="race-no-cmte">${nm}</span>`;
      return `<div class="race-row"><span class="race-col-name">${badge} ${name}</span>` +
             `<span class="race-col-raised">${c.slug ? fmt$(c.raised_cycle) : "—"}</span></div>`;
    }).join("");
    const more = race.candidates.length > 4
      ? `<div class="race-more">+${race.candidates.length - 4} more</div>` : "";
    return `<div class="race-card">
        <div class="race-card-header">
          <span class="race-office">${esc(race.office)}</span>
          <span class="race-total">${fmt$(race.total)}</span>
        </div>
        ${rows}${more}
      </div>`;
  }).join("");

  list.querySelectorAll("a[data-slug]").forEach(a =>
    a.addEventListener("click", e => { e.preventDefault(); selectFilerBySlug(a.dataset.slug); }));
}

// ── Fundraising Pulse ────────────────────────────────────────────────────
let activitySnapshot = null;
let pulseCurrentPeriod = "30d";
const PULSE_ROWS = 3;

async function loadCampaignPulse() {
  try {
    activitySnapshot = await DL.getBlob("activity_snapshot");
    // Overview extras: historical comparison, then the district map below it.
    if (typeof initCompare === "function") { try { initCompare(); } catch (e) { console.warn("[compare]", e); } }
    if (typeof initCaucusDonors === "function") initCaucusDonors().catch(e => console.warn("[caucus donors]", e));
    if (typeof initRaceMap === "function") { try { initRaceMap(activitySnapshot); } catch (e) { console.warn("[racemap]", e); } }
    await ensureDonorFilerMap();
    const el = document.getElementById("campaign-pulse");
    if (el) { el.hidden = false; renderPulsePeriod(pulseCurrentPeriod); }
    renderRacesToWatch();
    // The race header needs legislative_map, which only exists once this
    // resolves — re-render so the field appears if Overview drew first.
    renderFilerRaceHeader();
    // Wire up period toggle
    const toggle = document.getElementById("pulse-period-toggle");
    if (toggle) {
      toggle.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-period]");
        if (!btn) return;
        toggle.querySelectorAll(".toggle-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        pulseCurrentPeriod = btn.dataset.period;
        renderPulsePeriod(pulseCurrentPeriod);
      });
    }
  } catch (e) { console.warn("Campaign Pulse load failed:", e); }
}

function renderPulsePeriod(periodKey) {
  if (!activitySnapshot) return;
  const period = activitySnapshot.periods[periodKey];
  if (!period) return;
  renderPulseRaising(period);
  renderPulseMomentum(period);
  renderPulseDonors(period);

  // Wire up click-to-expand on pulse entry names
  document.querySelectorAll(".pulse-entry-name").forEach(el => {
    el.style.cursor = "pointer";
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      el.classList.toggle("expanded");
    });
  });
}

function pulseEntryHTML(entry, valueHTML, extra = "") {
  const partyClass = entry.party === "Democrat" ? "party-d" : entry.party === "Republican" ? "party-r" : "party-other";
  const partyBadge = entry.party ? `<span class="pulse-entry-party ${partyClass}">${entry.party.charAt(0)}</span>` : "";
  const slug = entry.slug || "";
  const nameHTML = slug
    ? `<a href="#" class="pulse-entry-name pulse-filer-link" onclick="event.preventDefault();selectFilerBySlug('${esc(slug)}')" title="${esc(entry.name || '')}">${esc(entry.name || '')}</a>`
    : `<span class="pulse-entry-name" title="${esc(entry.name || '')}">${esc(entry.name || '')}</span>`;
  return `<div class="pulse-entry">
    ${partyBadge}
    ${nameHTML}
    <span class="pulse-entry-value">${valueHTML}</span>
    ${extra}
  </div>`;
}

function renderPulseRaising(data) {
  const body = document.getElementById("pulse-raising-body");
  if (!body) return;
  let html = "";
  const tiers = [
    { key: "statewide", label: "Statewide" },
    { key: "legislative", label: "Legislative" },
    { key: "local", label: "Local" },
    { key: "committees", label: "Committees" },
  ];
  for (const tier of tiers) {
    const entries = (data.by_office_tier[tier.key] || []);
    if (!entries.length) continue;
    html += `<div class="pulse-tier-label">${tier.label}</div>`;
    // Show top 1, rest collapsed
    html += pulseEntryHTML(entries[0], "$" + Number(entries[0].raised).toLocaleString("en-US", { maximumFractionDigits: 0 }));
    if (entries.length > 1) {
      const extraId = `pulse-raising-extra-${tier.key}`;
      html += `<div id="${extraId}" hidden>`;
      for (let i = 1; i < entries.length; i++) {
        html += pulseEntryHTML(entries[i], "$" + Number(entries[i].raised).toLocaleString("en-US", { maximumFractionDigits: 0 }));
      }
      html += `</div>`;
      html += `<div class="pulse-show-more" data-target="${extraId}">+${entries.length - 1} more</div>`;
    }
  }
  body.innerHTML = html || '<div class="pulse-entry-meta">No data</div>';

  // Wire up show-more toggles
  body.querySelectorAll(".pulse-show-more").forEach(btn => {
    btn.addEventListener("click", () => {
      const target = document.getElementById(btn.dataset.target);
      if (!target) return;
      target.hidden = !target.hidden;
      btn.textContent = target.hidden ? `+${target.children.length} more` : "show fewer";
    });
  });
}

function renderPulseMomentum(data) {
  const body = document.getElementById("pulse-momentum-body");
  if (!body) return;
  let html = "";
  const entries = (data.top_growth || []).slice(0, 10);
  for (const e of entries) {
    html += pulseEntryHTML(e,
      "$" + Number(e.raised).toLocaleString("en-US", { maximumFractionDigits: 0 }),
      `<span class="pulse-growth-pct">+${e.growth_pct}%</span>`
    );
  }
  body.innerHTML = html || '<div class="pulse-entry-meta">No data</div>';
}

function renderPulseDonors(data) {
  const body = document.getElementById("pulse-donors-body");
  if (!body) return;
  let html = "";
  const entries = (data.top_donors || []).slice(0, PULSE_ROWS * 2);
  for (const e of entries) {
    const hasDetails = e.details && e.details.length > 1;
    const donorKey = e.name.toLowerCase();
    const filerLink = donorFilerMap && donorFilerMap[donorKey];
    const nameHTML = filerLink
      ? `<a href="#" class="pulse-entry-name pulse-donor-link" onclick="event.preventDefault();selectFilerBySlug('${esc(filerLink.slug)}')" title="${esc(e.name)}">${esc(e.name)}</a>`
      : `<span class="pulse-entry-name" title="${esc(e.name)}">${esc(e.name)}</span>`;

    const metaHTML = hasDetails
      ? `<div class="pulse-donor-cmtes pulse-donor-expand" data-donor="${esc(e.name)}">${e.committees} committees ▾</div>`
      : (e.committees > 1 ? `<div class="pulse-donor-cmtes">${e.committees} committees</div>` : "");

    html += `<div class="pulse-entry pulse-donor-entry">
      <div class="pulse-donor-top">
        ${nameHTML}
        <span class="pulse-entry-value">$${Number(e.total).toLocaleString("en-US", { maximumFractionDigits: 0 })}</span>
      </div>
      ${metaHTML}
    </div>`;

    if (hasDetails) {
      html += `<div class="pulse-donor-details" data-donor-details="${esc(e.name)}" hidden>`;
      for (const d of e.details) {
        const detailClick = d.slug ? `onclick="selectFilerBySlug('${esc(d.slug)}')" style="cursor:pointer"` : "";
        html += `<div class="pulse-donor-detail" ${detailClick}>
          <span class="pulse-donor-detail-name">${esc(d.filer)}</span>
          <span class="pulse-donor-detail-amt">$${Number(d.amount).toLocaleString("en-US", { maximumFractionDigits: 0 })}</span>
        </div>`;
      }
      html += `</div>`;
    }
  }
  body.innerHTML = html || '<div class="pulse-entry-meta">No data</div>';

  // Wire up expand/collapse
  body.querySelectorAll(".pulse-donor-expand").forEach(btn => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.donor;
      const details = body.querySelector(`[data-donor-details="${name}"]`);
      if (details) {
        details.hidden = !details.hidden;
        btn.textContent = details.hidden ? `${btn.textContent.replace(" ▴", "")} ▾`.replace(" ▾ ▾", " ▾") : btn.textContent.replace(" ▾", " ▴");
      }
    });
  });
}

function selectFilerBySlug(slug) {
  // Navigate to the filer's page via the existing filer selector
  if (!filerIndex) return;
  const filer = filerIndex.find(f => f.slug === slug);
  if (!filer) return;
  // Clear existing selections and select this filer
  state.selectedFilers = [filer];
  // Trigger re-render
  navigateToFiler(slug);
}

// ── Tab loaders map ───────────────────────────────────────────────────────────

const loaders = {
  overview:   loadOverview,
  donors:     loadDonors,
  recipients: loadRecipients,
};

// ── Init ──────────────────────────────────────────────────────────────────────

(async function init() {
  try {
    filerIndex = await DL.getBlob("filer_index");
  } catch (e) {
    filerIndex = [];
  }
  // Load donor→filer map in background (non-blocking)
  ensureDonorFilerMap().catch(() => {});
  initFilerSelector();
  // Cross-page handoff: /donors and /races link to a committee by stashing
  // its slug in sessionStorage before navigating here.
  const handoff = sessionStorage.getItem("openFilerSlug");
  if (handoff) {
    sessionStorage.removeItem("openFilerSlug");
    selectFilerBySlug(handoff);
  }
  renderActiveTab();
})();
