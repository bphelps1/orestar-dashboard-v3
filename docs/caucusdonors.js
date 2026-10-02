/**
 * caucusdonors.js — top three donors to each legislative caucus PAC, Overview tab.
 *
 * Four small panels (House/Senate × Democrat/Republican) on one shared dollar
 * scale, so a bar in one panel can be read against a bar in another. Amounts
 * are cash contributions over the cycle window (Dec of the pre-election year
 * through Nov of the election year), from the merge-aware donor_leaderboard
 * RPC, so donors an admin merged at /admin/donors rank once.
 *
 * The House Republican caucus has changed committees twice; CAUCUS_PACS picks
 * the one that was the caucus's main committee in each cycle.
 */
"use strict";

const CAUCUS_PACS = [
  { caucus: "House Democrats",    party: "D", committee: () => "future_pac_house_builders" },
  { caucus: "House Republicans",  party: "R", committee: y =>
      y >= 2024 ? "evergreen_pac" : y >= 2020 ? "evergreen_oregon_pac" : "promote_oregon_leadership_pac" },
  { caucus: "Senate Democrats",   party: "D", committee: () => "sdlf" },
  { caucus: "Senate Republicans", party: "R", committee: () => "the_leadership_fund" },
];
// Same validated party pair as the race map and the Governor comparison.
const CAUCUS_COLOR = { D: "#2a78d6", R: "#e34948" };
const CAUCUS_TOP = 3;

let caucusCycle = null;
let caucusWired = false;

function caucusCurrentCycle() {
  const now = new Date();
  let y = now.getFullYear();
  if (y % 2 !== 0) y += 1;
  else if (now.getMonth() >= 11) y += 2;
  return y;
}

function caucusWindow(year) {
  return { start: `${year - 2}-12-01`, end: `${year}-11-30` };
}

/** The filer-index row for a slug, and the slug for a "c<filer_id>" donor key. */
function caucusFiler(slug) {
  return ((typeof filerIndex !== "undefined" && filerIndex) || []).find(f => f.slug === slug) || null;
}
function caucusDonorSlug(key) {
  const m = /^c(\d+)$/.exec(key || "");
  if (!m) return null;
  const f = ((typeof filerIndex !== "undefined" && filerIndex) || [])
    .find(r => (r.filer_ids || [r.filer_id]).map(String).includes(m[1]));
  return f ? f.slug : null;
}

/** [{caucus, party, slug, name, raised, donors:[{name,total,slug}]}] for one cycle. */
async function caucusLoad(year) {
  const { start, end } = caucusWindow(year);
  // The cycle total comes from the committee's monthly timeline, the same
  // figure "Compared with past cycles" plots, so the two never disagree.
  // (Summing the donor list instead would miss unitemized small gifts.)
  const timelines = await fetchTimelines(CAUCUS_PACS.map(c => c.committee(year)));
  return Promise.all(CAUCUS_PACS.map(async c => {
    const slug = c.committee(year);
    const filer = caucusFiler(slug);
    const ids = filer ? (filer.filer_ids && filer.filer_ids.length ? filer.filer_ids : [filer.filer_id]) : [];
    let donors = [];
    if (ids.length) {
      try {
        const res = await DL.getDonors({ start, end, filerIds: ids });
        donors = (res.all_time || []).slice(0, CAUCUS_TOP).map(d => ({
          name: d.name, total: d.total || 0, slug: caucusDonorSlug(d.donor_key),
        }));
      } catch (e) { console.warn("[caucus donors]", slug, e.message); }
    }
    const raised = sumCycle(timelines[slug], year);
    return { ...c, slug, name: filer ? filer.name : slug, raised, donors };
  }));
}

function caucusRender(panels, year) {
  const host = document.getElementById("caucus-donors-grid");
  if (!host) return;
  // One scale for all four panels — that is what makes them comparable.
  const max = Math.max(1, ...panels.flatMap(p => p.donors.map(d => d.total)));
  const $ = v => "$" + Math.round(v).toLocaleString("en-US");
  host.innerHTML = panels.map(p => {
    const rows = p.donors.length ? p.donors.map((d, i) => {
      const name = d.slug
        ? `<a href="#" class="caucus-donor-link" data-slug="${esc(d.slug)}">${esc(d.name)}</a>`
        : esc(d.name);
      return `<li class="caucus-row" title="${esc(d.name)} gave ${esc(p.name)} ${$(d.total)}">
        <span class="caucus-rank">${i + 1}</span>
        <span class="caucus-name">${name}</span>
        <span class="caucus-bar-track"><span class="caucus-bar"
          style="width:${(100 * d.total / max).toFixed(1)}%;background:${CAUCUS_COLOR[p.party]}"></span></span>
        <span class="caucus-amt">${$(d.total)}</span>
      </li>`;
    }).join("") : `<li class="caucus-empty">No contributions this cycle</li>`;
    const top = p.donors.reduce((a, d) => a + d.total, 0);
    const share = p.raised > 0 && top > 0
      ? ` · top ${p.donors.length} gave ${Math.round(100 * top / p.raised)}%` : "";
    return `<div class="caucus-panel">
      <div class="caucus-head">
        <h3 class="caucus-title"><span class="rc-party ${p.party}">${p.party}</span> ${esc(p.caucus)}</h3>
        <div class="caucus-raised"><b>${$(p.raised)}</b> raised</div>
      </div>
      <div class="caucus-cmte"><a href="#" class="caucus-donor-link" data-slug="${esc(p.slug)}">${esc(p.name)}</a>${share}</div>
      <ol class="caucus-list">${rows}</ol>
    </div>`;
  }).join("");

  host.querySelectorAll("a.caucus-donor-link").forEach(a =>
    a.addEventListener("click", e => { e.preventDefault(); selectFilerBySlug(a.dataset.slug); }));

  const note = document.getElementById("caucus-donors-note");
  if (note) {
    const current = year === caucusCurrentCycle();
    note.textContent = `Cash contributions, Dec ${year - 2} – ${current ? "today" : `Nov ${year}`}` +
      ` · all four panels share one scale`;
  }
}

async function caucusShow(year) {
  caucusCycle = year;
  const panels = await caucusLoad(year);
  if (caucusCycle === year) caucusRender(panels, year);   // a later pick wins
}

function initCaucusDonors() {
  const sel = document.getElementById("caucus-donors-cycle");
  if (!sel) return;
  if (!caucusWired) {
    caucusWired = true;
    const cur = caucusCurrentCycle();
    sel.innerHTML = [cur, cur - 2, cur - 4]
      .map((y, i) => `<option value="${y}">${y} cycle${i === 0 ? " (so far)" : ""}</option>`).join("");
    sel.addEventListener("change", () => caucusShow(+sel.value));
  }
  return caucusShow(+sel.value);
}
