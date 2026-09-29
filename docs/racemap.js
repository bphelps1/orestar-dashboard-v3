/**
 * racemap.js — legislative district map, rendered inside the Overview tab.
 *
 * Moved here from the retired /races page. House/Senate only: statewide
 * contests are covered by the comparison chart directly above it.
 *
 * `legislative_map.statewide` is still read elsewhere (the Overview race
 * header shows a gubernatorial committee its field), so only this map's
 * toggle went away — not the data.
 */
"use strict";

let rcChart = null;
let rcData = null;
let rcChamber = "house";
const RC_GEO = { house: "assets/or_house.geojson", senate: "assets/or_senate.geojson" };
const rcRegistered = {};

const rcFmt$ = v => Number(v || 0).toLocaleString("en-US",
  { style: "currency", currency: "USD", maximumFractionDigits: 0 });

function rcPartyBadge(party) {
  const p = (party || "").toLowerCase();
  const cls = p.startsWith("dem") ? "D" : p.startsWith("rep") ? "R" : "other";
  return `<span class="rc-party ${cls}">${cls === "other" ? esc(party || "—") : cls}</span>`;
}

function rcShowDistrict(name) {
  const entry = (rcData[rcChamber] || {})[name];
  const label = `${rcChamber === "house" ? "House" : "Senate"} District ${name}`;
  document.getElementById("rc-panel-title").textContent = label;
  const totalEl = document.getElementById("rc-panel-total");
  const bodyEl = document.getElementById("rc-panel-body");
  if (!entry) {
    totalEl.textContent = "";
    bodyEl.innerHTML = '<span class="rc-empty">No declared candidates on file.</span>';
    return;
  }
  totalEl.textContent =
    `${entry.candidates.length} candidate${entry.candidates.length > 1 ? "s" : ""} · ${rcFmt$(entry.total_raised)} raised this cycle`;
  bodyEl.innerHTML = entry.candidates.map(c => {
    const nm = esc(c.candidate_name || c.name || "");
    const nameHtml = c.slug
      ? `<a href="#" data-slug="${esc(c.slug)}">${nm}</a>`
      : `<span class="rc-no-cmte-name">${nm}</span>`;
    const sub = c.slug ? esc(c.name || "") : "No committee on file";
    const nums = c.slug
      ? `<span>Raised: <b>${rcFmt$(c.raised_cycle)}</b></span>
         <span>Cash on hand: <b>${rcFmt$(c.cash_on_hand)}</b></span>`
      : `<span class="rc-no-cmte">Not reporting contributions</span>`;
    return `<div class="rc-cand${c.slug ? "" : " rc-cand-nocmte"}">
        <div class="rc-cand-name">${nameHtml}${rcPartyBadge(c.party)}</div>
        <div class="rc-cand-sub">${sub}</div>
        <div class="rc-cand-nums">${nums}</div>
      </div>`;
  }).join("");
  bodyEl.innerHTML += renderDistrictHistory(name);

  // Already on the dashboard, so select the filer in place rather than navigating.
  bodyEl.querySelectorAll("a[data-slug]").forEach(a =>
    a.addEventListener("click", e => { e.preventDefault(); selectFilerBySlug(a.dataset.slug); }));
}

/** Past cycles for this district, appended beneath the current match-up. */
function renderDistrictHistory(district) {
  const hist = ((rcData.district_history || {})[rcChamber] || {})[district] || [];
  if (!hist.length) return "";

  const rows = hist.map(h => {
    // Margin comes from official results and is always right. The dollar
    // figure depends on matching candidates to committees, so an incomplete
    // one is labelled rather than presented as fact.
    const marginTxt = h.unopposed
      ? `<span class="rc-h-margin">unopposed</span>`
      : (h.margin_pts != null
          ? `<span class="rc-h-margin">${esc(h.winner_party || "")} +${h.margin_pts.toFixed(1)} pts</span>`
          : `<span class="rc-h-margin rc-h-na">margin n/a</span>`);
    const partial = h.matched < h.total
      ? `<span class="rc-h-partial" title="${h.total - h.matched} of ${h.total} candidates had no committee on file, so this total is a floor">${h.matched}/${h.total} committees</span>`
      : "";
    const who = h.winner ? `<div class="rc-h-winner">${esc(h.winner)} won</div>` : "";
    return `<div class="rc-h-row">
        <div class="rc-h-top">
          <span class="rc-h-cycle">${h.cycle}</span>
          ${marginTxt}
          <span class="rc-h-raised">${rcFmt$(h.raised)}${partial ? " " + partial : ""}</span>
        </div>
        ${who}
      </div>`;
  }).join("");

  return `<div class="rc-history">
      <div class="rc-history-title">Previous cycles</div>
      ${rows}
    </div>`;
}

async function rcLoadChamber(which) {
  rcChamber = which;
  document.querySelectorAll("#rc-chamber button").forEach(b =>
    b.classList.toggle("active", b.dataset.chamber === which));

  if (!rcRegistered[which]) {
    const resp = await fetch(RC_GEO[which]);
    if (!resp.ok) throw new Error(`Missing ${RC_GEO[which]}`);
    echarts.registerMap("or_" + which, await resp.json());
    rcRegistered[which] = true;
  }

  const districts = rcData[which] || {};
  const total = which === "house" ? 60 : 30;
  const data = [];
  for (let d = 1; d <= total; d++) {
    const e = districts[String(d)];
    data.push({ name: String(d), value: e ? e.total_raised : null });
  }
  const max = Math.max(1, ...data.map(x => x.value || 0));

  rcChart.setOption({
    tooltip: {
      trigger: "item",
      formatter: p => {
        const e = districts[p.name];
        const label = `${which === "house" ? "House" : "Senate"} District ${p.name}`;
        if (!e) return `<b>${label}</b><br/>No declared candidates`;
        const top = e.candidates[0];
        return `<b>${label}</b><br/>${e.candidates.length} candidate(s) · ${rcFmt$(e.total_raised)}`
          + (top ? `<br/>Top: ${esc(top.candidate_name || top.name)}` : "");
      },
    },
    visualMap: {
      min: 0, max, calculable: true, orient: "horizontal", left: "center", bottom: 4,
      text: ["More raised", "Less"],
      // Sequential = one hue, light → dark
      inRange: { color: ["#cde2fb", "#86b6ef", "#2a78d6", "#184f95"] },
      textStyle: { color: "#718096" },
      formatter: v => "$" + (v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : Math.round(v / 1e3) + "K"),
    },
    series: [{
      type: "map", map: "or_" + which, roam: true, nameProperty: "name",
      selectedMode: "single",        // so a filtered filer's district can be lit
      itemStyle: { areaColor: "#edf2f7", borderColor: "#fff", borderWidth: 1 },
      emphasis: { label: { show: true }, itemStyle: { areaColor: "#eda100" } },
      // Outline, not fill: the fill is the visualMap's to spend on "how much
      // was raised", and repainting it would cost the district its value on
      // the very scale the map exists to show.
      select: {
        label: { show: true },
        itemStyle: { borderColor: "#1a202c", borderWidth: 2.5, areaColor: "inherit" },
      },
      label: { show: false },
      data,
    }],
  }, { notMerge: true });

  document.getElementById("rc-panel-title").textContent = "Select a district";
  document.getElementById("rc-panel-total").textContent = "";
  document.getElementById("rc-panel-body").innerHTML =
    '<span class="rc-empty">Click any district on the map.</span>';
}

/** Which seat a committee is running for this cycle, or null. */
function rcDistrictForSlug(slug) {
  if (!rcData || !slug) return null;
  for (const chamber of ["house", "senate"]) {
    for (const [district, entry] of Object.entries(rcData[chamber] || {})) {
      if ((entry.candidates || []).some(c => c.slug === slug)) return { chamber, district };
    }
  }
  return null;
}

/**
 * Select a district and open its panel — used when the page is filtered to one
 * committee, so the map answers "where is this race" instead of sitting on the
 * statewide default.
 */
async function rcSelectDistrict(chamber, district) {
  if (!rcChart) return;
  if (chamber !== rcChamber) await rcLoadChamber(chamber);
  rcChart.dispatchAction({ type: "select", seriesIndex: 0, name: String(district) });
  rcShowDistrict(String(district));
}

/** Drop any selection and restore the statewide prompt. */
function rcClearSelection() {
  if (!rcChart) return;
  rcChart.dispatchAction({ type: "unselect", seriesIndex: 0,
                           name: Object.keys(rcData[rcChamber] || {}) });
  document.getElementById("rc-panel-title").textContent = "Select a district";
  document.getElementById("rc-panel-total").textContent = "";
  document.getElementById("rc-panel-body").innerHTML =
    '<span class="rc-empty">Click any district on the map.</span>';
}

async function initRaceMap(snapshot) {
  const box = document.getElementById("rc-map");
  if (!box) return;
  rcData = (snapshot || {}).legislative_map;
  if (!rcData) { box.innerHTML = '<span class="rc-empty">Race map data not available yet.</span>'; return; }

  const note = document.getElementById("rc-note");
  if (note && rcData.election) {
    note.textContent = `${rcData.election} · districts shaded by total raised this cycle. Click one for its candidates.`;
  }

  rcChart = echarts.init(box, null, { renderer: "svg" });
  rcChart.on("click", p => { if (p.componentType === "series") rcShowDistrict(p.name); });
  window.addEventListener("resize", () => rcChart && rcChart.resize());
  document.getElementById("rc-chamber").addEventListener("click", e => {
    const b = e.target.closest("button[data-chamber]");
    if (b) rcLoadChamber(b.dataset.chamber);
  });
  await rcLoadChamber("house");
}
