/**
 * donors.js — Admin Donor Dedup/Merge Workflow
 *
 * Loads review_queue.json (fuzzy-matched pairs from process.py),
 * displays them for admin review, and stores decisions in Supabase
 * (donor_review_decisions table) and entity_map.json.
 */

"use strict";

const _cbv = Date.now();

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

async function fetchJSON(path) {
  const sep = path.includes("?") ? "&" : "?";
  const resp = await fetch(`${path}${sep}_v=${_cbv}`);
  if (!resp.ok) throw new Error(`Failed to load ${path}: ${resp.status}`);
  return resp.json();
}

// ── State ───────────────────────────────────────────────────────────────
let reviewQueue = [];       // from review_queue.json
let decisions = new Map();  // pairKey → "merged"|"rejected"
let entityMap = {};         // from entity_map.json (canonical merges)

// ── Auth ───────────────────────────────────────────────────────────────
// Signing in happens at /login; requireAuth sends signed-out visitors there.
document.addEventListener("DOMContentLoaded", async () => {
  const session = await requireAuth();
  if (session) await initApp();
});

async function initApp() {
  await DN.load();
  // The resolver publishes this queue directly to dashboard_cache. Keeping a
  // static fallback made the admin page show an old queue after generated data
  // moved out of Git, which is worse than showing that the live read failed.
  try {
    const sb = await getSupabase();
    const { data, error } = await sb.from("dashboard_cache")
      .select("data").eq("key", "donor_review_queue").single();
    if (error) throw new Error(error.message);
    reviewQueue = data?.data || [];
  } catch (e) {
    console.warn("Could not load donor review queue:", e.message);
    reviewQueue = [];
  }

  // Load existing decisions from Supabase
  try {
    const sb = await getSupabase();
    const { data } = await sb.from("donor_review_decisions").select("*");
    if (data) {
      data.forEach(d => decisions.set(d.pair_key, d.decision));
    }
  } catch (e) {
    console.warn("Could not load decisions from Supabase:", e.message);
  }

  // Load entity map
  try {
    entityMap = await fetchJSON("../data/entity_map.json");
  } catch (e) {
    console.warn("Could not load entity_map.json:", e.message);
    entityMap = {};
  }

  renderPendingPairs();
  renderMergeLog();
  initSearch();
}

// ── Pending Pairs ──────────────────────────────────────────────────────
function pairKey(a, b) {
  return [a, b].sort().join("|||").toLowerCase();
}

function renderPendingPairs(filter = "") {
  const listEl = document.getElementById("pending-list");
  const emptyEl = document.getElementById("pending-empty");
  const countEl = document.getElementById("review-count");

  // Filter out already-decided pairs
  let pending = reviewQueue.filter(item => {
    const key = pairKey(item.a, item.b);
    return !decisions.has(key);
  });

  // Apply text filter
  if (filter) {
    const q = filter.toLowerCase();
    pending = pending.filter(item =>
      item.a.toLowerCase().includes(q) || item.b.toLowerCase().includes(q)
    );
  }

  // Sort by score descending (highest confidence first)
  pending.sort((a, b) => b.score - a.score);

  countEl.textContent = `${pending.length} pairs pending`;

  if (!pending.length) {
    listEl.innerHTML = "";
    emptyEl.hidden = false;
    return;
  }
  emptyEl.hidden = true;

  // Show first 100 to avoid DOM overload
  const shown = pending.slice(0, 100);

  listEl.innerHTML = shown.map((item, i) => {
    const key = pairKey(item.a, item.b);
    // Evidence from the entity resolver (type + corroborating signals)
    let evidenceHtml = "";
    if (item.evidence && item.evidence.type) {
      const ev = item.evidence;
      const labels = {
        name_vs_filer: "Possible committee match",
        same_address: "Same address",
        name_only: "Similar name, no address corroboration",
        possible_name_change: "Possible name change (same address, same first name)",
        guard_conflict: "Committee-like name filed as Individual",
      };
      const extra = [
        ev.addr ? `addr: ${ev.addr}` : "",
        ev.filer_id ? `filer ID: ${ev.filer_id}` : "",
        ev.book_type ? `book type: ${ev.book_type}` : "",
        ev.employer_match ? "employer matches" : "",
      ].filter(Boolean).join(" · ");
      evidenceHtml = `<div class="pair-evidence">${esc(labels[ev.type] || ev.type)}${extra ? " — " + esc(extra) : ""}</div>`;
    }
    return `
      <div class="cluster-card" data-pair-key="${esc(key)}" data-pair-idx="${i}">
        <div class="pair-score">Score: ${item.score}</div>
        ${evidenceHtml}
        <div class="cluster-names">
          <div class="pair-name">
            <label><input type="radio" name="keep-${i}" value="a" checked /> <span>${esc(item.a)}</span></label>
          </div>
          <div class="pair-name">
            <label><input type="radio" name="keep-${i}" value="b" /> <span>${esc(item.b)}</span></label>
          </div>
        </div>
        <div class="cluster-actions">
          <button class="btn-primary btn-merge">Merge (keep selected)</button>
          <button class="btn-small btn-reject">Not duplicates</button>
        </div>
      </div>
    `;
  }).join("");

  // Event delegation — avoids inline onclick with quotes in donor names
  listEl.querySelectorAll(".btn-merge").forEach(btn => {
    const card = btn.closest(".cluster-card");
    btn.addEventListener("click", () => mergePair(+card.dataset.pairIdx, card.dataset.pairKey));
  });
  listEl.querySelectorAll(".btn-reject").forEach(btn => {
    const card = btn.closest(".cluster-card");
    btn.addEventListener("click", () => rejectPair(card.dataset.pairKey));
  });

  if (pending.length > 100) {
    listEl.insertAdjacentHTML("beforeend",
      `<p class="empty-msg">Showing 100 of ${pending.length} pairs. Use filter to narrow.</p>`
    );
  }
}

async function mergePair(idx, key) {
  const pending = reviewQueue.filter(item => !decisions.has(pairKey(item.a, item.b)));
  pending.sort((a, b) => b.score - a.score);
  const item = pending[idx];
  if (!item) return;

  const card = document.querySelector(`[data-pair-key="${key}"]`);
  const keepChoice = card.querySelector(`input[name="keep-${idx}"]:checked`).value;
  const keepName = keepChoice === "a" ? item.a : item.b;
  const mergeName = keepChoice === "a" ? item.b : item.a;

  // Store decision
  decisions.set(key, "merged");

  // Save to Supabase
  try {
    const sb = await getSupabase();
    await sb.from("donor_review_decisions").upsert({
      pair_key: key,
      decision: "merged",
      kept_name: keepName,
      merged_name: mergeName,
      score: item.score,
      decided_at: new Date().toISOString(),
    }, { onConflict: "pair_key" });
  } catch (e) {
    console.warn("Could not save to Supabase:", e.message);
  }

  renderPendingPairs(document.getElementById("review-search").value);
  renderMergeLog();
}

async function rejectPair(key) {
  decisions.set(key, "rejected");

  try {
    const sb = await getSupabase();
    await sb.from("donor_review_decisions").upsert({
      pair_key: key,
      decision: "rejected",
      decided_at: new Date().toISOString(),
    }, { onConflict: "pair_key" });
  } catch (e) {
    console.warn("Could not save to Supabase:", e.message);
  }

  renderPendingPairs(document.getElementById("review-search").value);
}

// ── Merge Log ──────────────────────────────────────────────────────────
function renderMergeLog() {
  const logEl = document.getElementById("merge-log");
  const emptyEl = document.getElementById("log-empty");

  const merged = [];
  for (const [key, decision] of decisions) {
    if (decision === "merged") {
      // Find the original item
      const item = reviewQueue.find(r => pairKey(r.a, r.b) === key);
      if (item) merged.push(item);
    }
  }

  if (!merged.length) {
    logEl.innerHTML = "";
    emptyEl.hidden = false;
    return;
  }
  emptyEl.hidden = true;

  logEl.innerHTML = merged.map(item => {
    const key = pairKey(item.a, item.b);
    return `
      <div class="merge-entry" data-pair-key="${esc(key)}">
        <div class="merge-info">
          <div class="merge-names">"${esc(item.a)}" ↔ "${esc(item.b)}" (score: ${item.score})</div>
        </div>
        <button class="btn-small btn-undo">Undo</button>
      </div>
    `;
  }).join("");

  logEl.querySelectorAll(".btn-undo").forEach(btn => {
    const entry = btn.closest(".merge-entry");
    btn.addEventListener("click", () => undoDecision(entry.dataset.pairKey));
  });
}

async function undoDecision(key) {
  decisions.delete(key);

  try {
    const sb = await getSupabase();
    await sb.from("donor_review_decisions").delete().eq("pair_key", key);
  } catch (e) {
    console.warn("Could not delete from Supabase:", e.message);
  }

  renderPendingPairs(document.getElementById("review-search").value);
  renderMergeLog();
}

// ── Search filter ──────────────────────────────────────────────────────
function initSearch() {
  const input = document.getElementById("review-search");
  if (!input) return;
  input.addEventListener("input", () => {
    renderPendingPairs(input.value.trim());
  });
}

// ── Tab switching ─────────────────────────────────────────────────────
document.querySelectorAll(".admin-tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".admin-tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".admin-tab").forEach(t => t.hidden = true);
    btn.classList.add("active");
    document.getElementById(btn.dataset.adminTab).hidden = false;
    const tab = btn.dataset.adminTab;
    if (tab === "tab-active-merges" || tab === "tab-manual-merge" || tab === "tab-rejected") {
      loadMergeManager();
    }
    if (tab === "tab-candidate-links") {
      clLoad().catch(e => {
        document.getElementById("cl-list").innerHTML =
          `<p class="empty-msg">Could not load: ${esc(e.message)}</p>`;
      });
    }
    if (tab === "tab-balances") {
      bdLoad().then(bdRender).catch(e => {
        document.getElementById("bd-list").innerHTML =
          `<p class="empty-msg">Could not load: ${esc(e.message)}</p>`;
      });
    }
  });
});

// ── Merge Manager ─────────────────────────────────────────────────────
let allDonorNames = new Set();
let supabaseDecisionRows = [];   // full rows from donor_review_decisions

function buildDonorNameSet() {
  // Collect all unique donor names from the review queue
  reviewQueue.forEach(item => {
    allDonorNames.add(item.a);
    allDonorNames.add(item.b);
  });
  // Also add names from entity_map
  for (const [raw, canonical] of Object.entries(entityMap)) {
    allDonorNames.add(raw);
    allDonorNames.add(canonical);
  }
}

async function loadMergeManager() {
  // Build donor name set for autocomplete (if not already built)
  if (!allDonorNames.size) buildDonorNameSet();

  // Reload full decision rows from Supabase for detailed info
  try {
    const sb = await getSupabase();
    const { data } = await sb.from("donor_review_decisions").select("*");
    supabaseDecisionRows = data || [];
  } catch (e) {
    console.warn("Could not load decisions for merge manager:", e.message);
  }

  // Combine active merges from both sources
  const merges = [];

  // 1. entity_map.json merges
  for (const [raw, canonical] of Object.entries(entityMap)) {
    if (raw !== canonical) {
      merges.push({
        pairKey: pairKey(raw, canonical),
        rawName: raw,
        canonicalName: canonical,
        source: "entity_map",
        score: null,
      });
    }
  }

  // 2. Supabase admin merges
  supabaseDecisionRows.forEach(row => {
    if (row.decision === "merged") {
      merges.push({
        pairKey: row.pair_key,
        rawName: row.merged_name || row.pair_key,
        canonicalName: row.kept_name || "—",
        source: "admin",
        score: row.score,
      });
    }
  });

  renderActiveMerges(merges, "");
  renderRejectedPairs();
  initMergeSearch(merges);
  initMergeCreator();
}

// ── Active Merges ─────────────────────────────────────────────────────
let _cachedMerges = [];

function initMergeSearch(merges) {
  _cachedMerges = merges;
  const input = document.getElementById("merge-search");
  if (!input) return;
  input.addEventListener("input", () => {
    renderActiveMerges(_cachedMerges, input.value.trim());
  });
}

function renderActiveMerges(merges, searchQuery) {
  const container = document.getElementById("merge-list");

  let filtered = merges;
  if (searchQuery) {
    const q = searchQuery.toLowerCase();
    filtered = merges.filter(m =>
      m.rawName.toLowerCase().includes(q) ||
      m.canonicalName.toLowerCase().includes(q)
    );
  }

  if (!filtered.length) {
    container.innerHTML = `<p class="empty-msg">No active merges found.</p>`;
    return;
  }

  const rows = filtered.map(m => {
    const sourceBadge = m.source === "entity_map"
      ? `<span class="merge-source-badge badge-gray">entity_map</span>`
      : `<span class="merge-source-badge badge-blue">admin</span>`;
    const scoreCell = m.score != null ? m.score : "—";
    const editBtn = m.source === "admin"
      ? `<button class="btn-small btn-edit">Edit</button>`
      : "";
    const splitBtn = `<button class="btn-small btn-split">Split</button>`;

    return `<tr data-pair-key="${esc(m.pairKey)}" data-source="${esc(m.source)}">
      <td>${esc(m.rawName)}</td>
      <td class="merge-arrow-cell">→</td>
      <td>${esc(m.canonicalName)}</td>
      <td>${sourceBadge}</td>
      <td>${scoreCell}</td>
      <td class="merge-actions">${editBtn} ${splitBtn}</td>
    </tr>`;
  }).join("");

  container.innerHTML = `
    <table class="merge-table">
      <thead>
        <tr>
          <th>Raw Name</th>
          <th></th>
          <th>Canonical Name</th>
          <th>Source</th>
          <th>Score</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;

  container.querySelectorAll(".btn-edit").forEach(btn => {
    const row = btn.closest("tr");
    btn.addEventListener("click", () => editMerge(row.dataset.pairKey));
  });
  container.querySelectorAll(".btn-split").forEach(btn => {
    const row = btn.closest("tr");
    btn.addEventListener("click", () => splitMerge(row.dataset.pairKey, row.dataset.source));
  });
}

// ── Edit Merge ────────────────────────────────────────────────────────
async function editMerge(pk) {
  const row = supabaseDecisionRows.find(r => r.pair_key === pk && r.decision === "merged");
  if (!row) return;

  const newName = prompt("Edit canonical name:", row.kept_name);
  if (newName === null || newName.trim() === "" || newName.trim() === row.kept_name) return;

  try {
    const sb = await getSupabase();
    await sb.from("donor_review_decisions").upsert({
      pair_key: pk,
      decision: "merged",
      kept_name: newName.trim(),
      merged_name: row.merged_name,
      score: row.score,
      decided_at: new Date().toISOString(),
    }, { onConflict: "pair_key" });

    // Update local state
    decisions.set(pk, "merged");
  } catch (e) {
    console.warn("Could not update merge:", e.message);
    alert("Error updating merge: " + e.message);
    return;
  }

  await loadMergeManager();
}

// ── Split Merge ───────────────────────────────────────────────────────
async function splitMerge(pk, source) {
  if (source === "entity_map") {
    alert("This merge is in entity_map.json \u2014 edit the file directly to remove it.");
    return;
  }

  if (!confirm("Split this merge? The pair will return to pending review.")) return;

  try {
    const sb = await getSupabase();
    await sb.from("donor_review_decisions").delete().eq("pair_key", pk);
    decisions.delete(pk);
  } catch (e) {
    console.warn("Could not delete decision:", e.message);
    alert("Error splitting merge: " + e.message);
    return;
  }

  await loadMergeManager();
  renderPendingPairs(document.getElementById("review-search").value);
  renderMergeLog();
}

// ── Manual Merge Creator ──────────────────────────────────────────────

/** lowercased merged-away name -> the name it was merged into. */
function mergedAliasMap() {
  const m = new Map();
  for (const row of supabaseDecisionRows) {
    if (row.decision === "merged" && row.merged_name && row.kept_name) {
      m.set(String(row.merged_name).toLowerCase(), row.kept_name);
    }
  }
  return m;
}

let _mergeCreatorInitialized = false;

function initMergeCreator() {
  if (_mergeCreatorInitialized) return;
  _mergeCreatorInitialized = true;

  const inputA = document.getElementById("merge-donor-a");
  const inputB = document.getElementById("merge-donor-b");
  const suggestA = document.getElementById("merge-suggest-a");
  const suggestB = document.getElementById("merge-suggest-b");
  const createBtn = document.getElementById("merge-create-btn");

  function updateCreateBtn() {
    createBtn.disabled = !(inputA.value.trim() && inputB.value.trim());
  }

  function setupAutocomplete(input, suggestEl) {
    input.addEventListener("input", () => {
      updateCreateBtn();
      const q = input.value.trim().toLowerCase();
      if (q.length < 2) { suggestEl.hidden = true; return; }

      const matches = [];
      for (const name of allDonorNames) {
        if (name.toLowerCase().includes(q)) {
          matches.push(name);
          if (matches.length >= 10) break;
        }
      }

      if (!matches.length) { suggestEl.hidden = true; return; }

      // Flag names already merged away, so an accidental no-op pairing is
      // obvious. They stay selectable, and the name they were merged INTO is
      // untouched — further aliases must still be mergeable into it.
      const mergedInto = mergedAliasMap();
      suggestEl.innerHTML = matches.map(m => {
        const into = mergedInto.get(m.toLowerCase());
        return `<div class="merge-suggest-item${into ? " merge-suggest-merged" : ""}" data-name="${esc(m)}">`
          + esc(m)
          + (into ? `<span class="merge-suggest-note">already merged → ${esc(into)}</span>` : "")
          + `</div>`;
      }).join("");
      suggestEl.hidden = false;
    });

    suggestEl.addEventListener("click", (e) => {
      const item = e.target.closest(".merge-suggest-item");
      if (!item) return;
      input.value = item.dataset.name;
      suggestEl.hidden = true;
      updateCreateBtn();
    });

    // Hide suggestions on blur (with delay so click can register)
    input.addEventListener("blur", () => {
      setTimeout(() => { suggestEl.hidden = true; }, 200);
    });
  }

  setupAutocomplete(inputA, suggestA);
  setupAutocomplete(inputB, suggestB);

  createBtn.addEventListener("click", createManualMerge);
}

async function createManualMerge() {
  const inputA = document.getElementById("merge-donor-a");
  const inputB = document.getElementById("merge-donor-b");
  const donorA = inputA.value.trim();
  const canonicalName = inputB.value.trim();

  if (!donorA || !canonicalName) return;

  const pk = pairKey(donorA, canonicalName);

  try {
    const sb = await getSupabase();
    await sb.from("donor_review_decisions").upsert({
      pair_key: pk,
      decision: "merged",
      kept_name: canonicalName,
      merged_name: donorA,
      score: 100,
      decided_at: new Date().toISOString(),
    }, { onConflict: "pair_key" });

    decisions.set(pk, "merged");
  } catch (e) {
    console.warn("Could not create manual merge:", e.message);
    alert("Error creating merge: " + e.message);
    return;
  }

  // Clear inputs
  inputA.value = "";
  inputB.value = "";
  document.getElementById("merge-create-btn").disabled = true;

  await loadMergeManager();
}

// ── Rejected Pairs ────────────────────────────────────────────────────
function renderRejectedPairs() {
  const container = document.getElementById("rejected-list");

  const rejected = supabaseDecisionRows.filter(r => r.decision === "rejected");

  if (!rejected.length) {
    container.innerHTML = `<p class="empty-msg">No rejected pairs.</p>`;
    return;
  }

  container.innerHTML = rejected.map(row => {
    const names = row.pair_key.split("|||");
    const display = names.length === 2
      ? `${esc(names[0])} / ${esc(names[1])}`
      : esc(row.pair_key);
    return `
      <div class="merge-entry" data-pair-key="${esc(row.pair_key)}">
        <div class="merge-info">
          <div class="merge-names">${display}</div>
        </div>
        <button class="btn-small btn-reconsider">Reconsider</button>
      </div>
    `;
  }).join("");

  container.querySelectorAll(".btn-reconsider").forEach(btn => {
    const entry = btn.closest(".merge-entry");
    btn.addEventListener("click", () => reconsiderPair(entry.dataset.pairKey));
  });
}

async function reconsiderPair(pk) {
  if (!confirm("Remove this rejection? The pair will return to pending review.")) return;

  try {
    const sb = await getSupabase();
    await sb.from("donor_review_decisions").delete().eq("pair_key", pk);
    decisions.delete(pk);
  } catch (e) {
    console.warn("Could not delete rejection:", e.message);
    alert("Error: " + e.message);
    return;
  }

  renderRejectedPairs();
  renderPendingPairs(document.getElementById("review-search").value);
}

// ── Entity Merges (address-aware) ────────────────────────────────────────────
//
// The pair-based flow above keys decisions on NAMES, so it cannot express
// "same name, different address" — the case where one company resolves into
// several entities. This section searches resolved entities directly and
// records decisions keyed on alias_key (norm_name|addr_key), which is stable
// across resolver runs. donor_id must NOT be used: it is a content hash of the
// cluster and changes on every run.

const emState = { a: null, selected: new Map(), saving: false };

const emFmt$ = v => Number(v || 0).toLocaleString("en-US",
  { style: "currency", currency: "USD", maximumFractionDigits: 0 });

async function emSearch(q) {
  const sb = await getSupabase();
  const { data, error } = await sb.rpc("donor_search", { p_q: q, p_limit: 50 });
  if (error) { console.warn("donor_search:", error.message); return []; }
  return data || [];
}

function emRenderCard(side) {
  if (side === "b") {
    const el = document.getElementById("em-card-b");
    el.innerHTML = [...emState.selected.values()].map(d => `<div class="em-selected">
      <span>${esc(DN.display(d.display_name))} · ${emFmt$(d.total_given)}</span>
      <button type="button" data-remove="${esc(d.donor_id)}">Remove</button></div>`).join("");
    el.querySelectorAll("[data-remove]").forEach(b => b.addEventListener("click", () => {
      emState.selected.delete(b.dataset.remove); emRenderCard("b");
    }));
    document.querySelectorAll("#em-results-b li[data-donor-id]").forEach(li => {
      li.querySelector("input").checked = emState.selected.has(li.dataset.donorId);
      li.querySelector("input").disabled = emState.saving || li.dataset.donorId === emState.a?.donor_id;
    });
    emSyncButtons(); return;
  }
  const d = emState[side];
  const el = document.getElementById(`em-card-${side}`);
  if (!d) { el.innerHTML = ""; emSyncButtons(); return; }
  const addrs = (d.addresses || []);
  el.innerHTML = `
    <div class="em-card-name">${esc(DN.display(d.display_name))}</div>
    <div class="em-card-meta">${esc([d.book_type,
        [d.city, d.state].filter(Boolean).join(", ")].filter(Boolean).join(" · "))}</div>
    <div class="em-card-nums">
      <span>${emFmt$(d.total_given)} given</span>
      <span>${Number(d.gift_count || 0).toLocaleString()} gifts</span>
      <span>${Number(d.alias_count || 0).toLocaleString()} aliases</span>
    </div>
    <div class="em-card-addrs">
      <div class="em-addr-title">${addrs.length} address${addrs.length === 1 ? "" : "es"}</div>
      ${addrs.slice(0, 6).map(a => `<div class="em-addr">${esc(a)}</div>`).join("")}
      ${addrs.length > 6 ? `<div class="em-addr">…and ${addrs.length - 6} more</div>` : ""}
    </div>`;
  emSyncButtons();
}

function emSyncButtons() {
  const ready = !emState.saving && emState.a?.rep_alias_key && emState.selected.size > 0
    && [...emState.selected.values()].every(b => b.rep_alias_key && b.donor_id !== emState.a.donor_id);
  document.getElementById("em-merge-btn").disabled = !ready;
  document.getElementById("em-separate-btn").disabled = !ready;
  document.getElementById("em-merge-btn").textContent = `Merge ${emState.selected.size} selected into Entity A`;
  for (const side of ["a", "b"]) document.getElementById(`em-search-${side}`).disabled = emState.saving;
  document.querySelectorAll("#em-card-b button").forEach(button => { button.disabled = emState.saving; });

}

function emInitSide(side) {
  const input = document.getElementById(`em-search-${side}`);
  const ul = document.getElementById(`em-results-${side}`);
  let timer = null;
  input.addEventListener("input", () => {
    document.getElementById("em-status").textContent = "";
    if (side === "a") emState.a = null;
    emRenderCard(side);
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { ul.hidden = true; return; }
    timer = setTimeout(async () => {
      const rows = await emSearch(q);
      if (input.value.trim() !== q) return;
      if (!rows.length) { ul.hidden = true; return; }
      ul.innerHTML = rows.map((r, i) => `
        <li data-i="${i}" data-donor-id="${esc(r.donor_id)}">
          <div class="em-r-name">${side === "b" ? `<input type="checkbox" aria-label="Select ${esc(DN.display(r.display_name))}" ${r.donor_id === emState.a?.donor_id ? "disabled" : ""} ${emState.selected.has(r.donor_id) ? "checked" : ""} /> ` : ""}${esc(DN.display(r.display_name))}</div>
          <div class="em-r-meta">${esc([r.book_type,
            [r.city, r.state].filter(Boolean).join(", ")].filter(Boolean).join(" · "))}
            · ${emFmt$(r.total_given)} · ${(r.addresses || []).length} addr</div>
        </li>`).join("");
      ul.hidden = false;
      ul.querySelectorAll("li").forEach((li, i) => li.addEventListener("click", () => {
        if (emState.saving) return;
        if (side === "b") {
          const r = rows[i];
          if (r.donor_id === emState.a?.donor_id) return;
          if (emState.selected.has(r.donor_id)) emState.selected.delete(r.donor_id);
          else emState.selected.set(r.donor_id, r);
          li.querySelector("input").checked = emState.selected.has(r.donor_id);
        } else {
          emState.a = rows[i];
          emState.selected.delete(rows[i].donor_id);
          emRenderCard("b");
          input.value = rows[i].display_name;
          ul.hidden = true;
        }
        emRenderCard(side);
      }));
    }, 220);
  });
  document.addEventListener("click", e => {
    if (!e.target.closest(`#em-search-${side}`) && !e.target.closest(`#em-results-${side}`)) {
      ul.hidden = true;
    }
  });
}

async function emRecord(decision) {
  const a = emState.a, selected = [...emState.selected.values()];
  if (emState.saving || !a?.rep_alias_key || !selected.length || selected.some(b => !b.rep_alias_key || b.donor_id === a.donor_id)) return;
  emState.saving = true; emSyncButtons();
  const status = document.getElementById("em-status");
  status.textContent = "Saving…";
  try {
    const sb = await getSupabase();
    const session = await getSession();
    const records = selected.map(b => {
      const [ka, kb] = [a.rep_alias_key, b.rep_alias_key].sort();
      return {
      merge_key: `${ka}|||${kb}`,
      alias_a: ka, alias_b: kb,
      decision,
      keep_alias_key: a.rep_alias_key,
      decided_at: new Date().toISOString(),
      label_a: ka === a.rep_alias_key ? a.display_name : b.display_name,
      label_b: kb === b.rep_alias_key ? b.display_name : a.display_name,
      decided_by: session?.user?.email || null,
      };
    });
    const { error } = await sb.from("donor_merge_overrides").upsert(records);
    if (error) throw new Error(error.message);
    status.textContent = decision === "merged"
      ? "Merge applied. Refresh other pages to see the combined donor."
      : "Marked as separate — they will not be merged.";
    emState.a = null;
    emState.selected.clear();
    ["a", "b"].forEach(s => {
      document.getElementById(`em-search-${s}`).value = "";
      emRenderCard(s);
    });
    await emLoadList();
  } catch (e) {
    status.textContent = "Failed: " + e.message;
  } finally {
    emState.saving = false; emSyncButtons();
  }
}

// The unfiltered log is the newest decisions; a filter is answered by the
// database across every decision ever recorded. Filtering only the rows already
// fetched made anything older than the newest 200 impossible to find or undo:
// the Amazon Web Services merge was decision 601 of 710 three days after it
// was saved, and could not be reached from this tab at all.
const EM_LOG_RECENT = 200;
const EM_LOG_MATCHES = 500;
const EM_LOG_COLUMNS = ["label_a", "label_b", "alias_a", "alias_b"];

/** A search term safe inside a PostgREST or() filter. Quoting keeps commas,
 * periods and parentheses ("Amazon.com, Inc.") from being read as syntax. */
function emOrTerm(term) {
  return `"*${String(term).replace(/[\\"]/g, c => "\\" + c)}*"`;
}

/** The query behind the log: newest first, and bounded either way. */
function emLogQuery(sb, filter) {
  const q = String(filter || "").trim();
  const query = sb.from("donor_merge_overrides").select("*", { count: "exact" })
    .order("decided_at", { ascending: false });
  if (!q) return query.limit(EM_LOG_RECENT);
  return query.or(EM_LOG_COLUMNS.map(c => `${c}.ilike.${emOrTerm(q)}`).join(","))
    .limit(EM_LOG_MATCHES);
}

/** What the log says about how much of it is on screen. */
function emLogNote(filter, shown, total) {
  const q = String(filter || "").trim();
  if (q) {
    return total > shown
      ? `Showing ${shown} of ${total} matching decisions — narrow the filter to see the rest.`
      : `${total} matching decision${total === 1 ? "" : "s"}.`;
  }
  return total > shown
    ? `Showing the ${shown} most recent of ${total} decisions. Filter to reach older ones.`
    : "";
}

async function emLoadList(filter = "") {
  const el = document.getElementById("em-list");
  if (!el) return;
  try {
    const sb = await getSupabase();
    const { data, error, count } = await emLogQuery(sb, filter);
    if (error) throw new Error(error.message);
    const rows = data || [];
    const q = String(filter || "").trim();
    if (!rows.length) {
      el.innerHTML = `<p class="empty-msg">${q
        ? `No decision matches \u201c${esc(q)}\u201d.` : "No entity decisions recorded yet."}</p>`;
      return;
    }
    const note = emLogNote(q, rows.length, count ?? rows.length);
    el.innerHTML = (note ? `<p class="em-log-note">${esc(note)}</p>` : "") + rows.map(r => `
      <div class="em-row">
        <div>
          <span class="em-badge em-${esc(r.decision)}">${esc(r.decision)}</span>
          <strong>${esc(r.label_a || r.alias_a)}</strong>
          ${r.decision === "merged" ? "\uff0b" : "\u2715"}
          <strong>${esc(r.label_b || r.alias_b)}</strong>
          <div class="em-r-meta">${esc(r.alias_a)} \u2194 ${esc(r.alias_b)}</div>
        </div>
        <button class="btn-link em-undo" data-key="${esc(r.merge_key)}">Undo</button>
      </div>`).join("");
    el.querySelectorAll(".em-undo").forEach(btn => btn.addEventListener("click", () => emUndo(btn)));
  } catch (e) {
    el.innerHTML = `<p class="empty-msg">Could not load: ${esc(e.message)}</p>`;
  }
}

/** Remove one decision — and say so when nothing was removed, rather than
 * reloading a list that still shows it. */
async function emUndo(btn) {
  btn.disabled = true;
  let message = null;
  try {
    const sb = await getSupabase();
    const { data, error } = await sb.from("donor_merge_overrides").delete()
      .eq("merge_key", btn.dataset.key).select("merge_key");
    if (error) message = `Undo failed: ${error.message}`;
    else if (!data?.length) message = "Undo failed: nothing was removed";
  } catch (e) {
    message = `Undo failed: ${e.message}`;
  }
  if (message) {
    btn.disabled = false;
    btn.textContent = message;
    return;
  }
  await emLoadList(document.getElementById("em-filter").value.trim());
}

function initEntityMerge() {
  if (!document.getElementById("em-search-a")) return;
  emInitSide("a");
  emInitSide("b");
  document.getElementById("em-merge-btn").addEventListener("click", () => emRecord("merged"));
  document.getElementById("em-separate-btn").addEventListener("click", () => emRecord("separate"));
  const filter = document.getElementById("em-filter");
  let ft = null;
  filter.addEventListener("input", () => {
    clearTimeout(ft);
    ft = setTimeout(() => emLoadList(filter.value.trim()), 200);
  });
  emLoadList();
}

document.addEventListener("DOMContentLoaded", initEntityMerge);

// ══════════════════════════════════════════════════════════════════════
// Candidate → committee links
//
// The matcher pairs a filed candidate with a committee using the district
// plus a tiered name comparison, and reports what it could not pair. Some of
// those genuinely have no committee; a few have one the matcher was right to
// refuse — "David Nelson" filed for HD 17 while a committee of that exact name
// reads "State Senator, 28th District", which is plausibly a different person.
//
// Guessing either way is the failure this tab exists to prevent: a wrong link
// shows one candidate's money under another's name, a missed one shows a
// funded candidate at $0, and both look equally confident on the map.
// ══════════════════════════════════════════════════════════════════════

let clUnmatched = [];      // review queue, from the snapshot
let clCommittees = [];     // candidate committees, for search
let clDecisions = [];      // rows already recorded
let clDecisionsError = null;

/** Normalised name tokens, minus initials and honorifics — mirrors the scraper. */
const CL_NOISE = new Set(["dr","mr","mrs","ms","miss","rev","hon","sen","rep","gov",
  "jr","sr","ii","iii","iv","vi","md","phd","dds","esq","cpa","rn"]);
function clKey(name) {
  return (name || "").toLowerCase().replace(/[^a-z ]/g, " ").split(/\s+/)
    .filter(t => t.length > 1 && !CL_NOISE.has(t)).join(" ");
}

async function clLoad() {
  const sb = await getSupabase();
  const [snap, idx, dec] = await Promise.all([
    sb.from("dashboard_cache").select("data").eq("key", "activity_snapshot").single(),
    sb.from("dashboard_cache").select("data").eq("key", "filer_index").single(),
    sb.from("candidate_committee_links").select("*").order("decided_at", { ascending: false }),
  ]);
  if (snap.error) throw new Error(snap.error.message);
  if (idx.error) throw new Error(idx.error.message);

  const lm = (snap.data?.data || {}).legislative_map || {};
  // Older snapshots stored display strings; those cannot be acted on, so skip
  // them rather than render a row whose buttons would write a malformed key.
  clUnmatched = (lm.unmatched || []).filter(u => u && typeof u === "object" && u.key);
  clCommittees = (idx.data?.data || []).filter(f => f.committee_type === "Candidate Committee");
  // An unreadable table must not render as "no decisions recorded" — that
  // reads as a clean slate and invites re-deciding what is already decided.
  clDecisionsError = dec.error ? dec.error.message : null;
  clDecisions = dec.error ? [] : (dec.data || []);
  clRender();
  clRenderDecisions();
}

function clRender(filter = "") {
  const el = document.getElementById("cl-list");
  const decided = new Set(clDecisions.map(d => d.candidate_key));
  const q = filter.toLowerCase();
  const rows = clUnmatched
    .filter(u => !decided.has(u.key))
    .filter(u => !q || `${u.ballot_name} ${u.office_district}`.toLowerCase().includes(q));

  document.getElementById("cl-count").textContent = clDecisionsError
    ? `${rows.length} listed · decisions unavailable`
    : `${rows.length} awaiting review · ${decided.size} decided`;

  if (!rows.length) {
    el.innerHTML = '<p class="empty-msg">Nothing awaiting review.</p>';
    return;
  }
  el.innerHTML = rows.map(u => `
    <div class="em-row" data-key="${esc(u.key)}">
      <div>
        <b>${esc(u.ballot_name)}</b>
        <span class="section-desc"> — ${esc(u.office_district)}${u.party ? " · " + esc(u.party) : ""}</span>
      </div>
      <div class="cl-suggest">${clSuggestHtml(u)}</div>
      <div class="merge-input-group" style="margin-top:8px">
        <input type="text" class="cl-search" placeholder="Search committees…"
               data-key="${esc(u.key)}" autocomplete="off" />
        <div class="merge-suggestions cl-results" hidden></div>
      </div>
      <button class="btn-small cl-none" data-key="${esc(u.key)}">No committee exists</button>
    </div>`).join("");

  el.querySelectorAll(".cl-search").forEach(inp =>
    inp.addEventListener("input", () => clSearch(inp)));
  el.querySelectorAll(".cl-none").forEach(btn =>
    btn.addEventListener("click", () => clDecide(btn.dataset.key, null, "none")));
  el.querySelectorAll("[data-pick]").forEach(btn =>
    btn.addEventListener("click", () => clDecide(btn.dataset.key, btn.dataset.pick, "link")));
}

/**
 * Committees the matcher declined but a person might accept — shown as
 * suggestions only. These are exactly the pairs that are too uncertain to
 * automate, which is why they arrive here with an explicit "why" instead of
 * being linked silently.
 */
function clSuggestHtml(u) {
  const k = clKey(u.ballot_name);
  const kt = new Set(k.split(" ").filter(Boolean));
  if (!kt.size) return "";
  const hits = clCommittees.map(f => {
    const ct = new Set(clKey(f.candidate_name).split(" ").filter(Boolean));
    const shared = [...kt].filter(t => ct.has(t)).length;
    if (!shared) return null;
    const exact = shared === kt.size && shared === ct.size;
    if (!exact && shared < 2) return null;
    return { f, why: exact ? "same name, different office" : `${shared} name tokens in common` };
  }).filter(Boolean).slice(0, 4);
  if (!hits.length) return '<div class="section-desc">No similarly-named committee found.</div>';
  return `<div class="section-desc">Possible matches — check the office before linking:</div>` +
    hits.map(({ f, why }) => `
      <div class="cl-hit">
        <span><b>${esc(f.name)}</b> · ${esc(f.candidate_name || "")}
          <span class="section-desc">(${esc(f.office_district || "no office on file")}) — ${why}</span></span>
        <button class="btn-small" data-key="${esc(u.key)}" data-pick="${esc(f.filer_id)}">Link</button>
      </div>`).join("");
}

function clSearch(inp) {
  const box = inp.parentElement.querySelector(".cl-results");
  const q = inp.value.trim().toLowerCase();
  if (q.length < 2) { box.hidden = true; return; }
  const hits = clCommittees.filter(f =>
    `${f.name} ${f.candidate_name || ""}`.toLowerCase().includes(q)).slice(0, 8);
  box.hidden = false;
  box.innerHTML = hits.length ? hits.map(f => `
    <div class="cl-hit">
      <span><b>${esc(f.name)}</b> · ${esc(f.candidate_name || "")}
        <span class="section-desc">(${esc(f.office_district || "no office on file")})</span></span>
      <button class="btn-small" data-key="${esc(inp.dataset.key)}" data-pick="${esc(f.filer_id)}">Link</button>
    </div>`).join("") : '<div class="section-desc">No match.</div>';
  box.querySelectorAll("[data-pick]").forEach(btn =>
    btn.addEventListener("click", () => clDecide(btn.dataset.key, btn.dataset.pick, "link")));
}

async function clDecide(key, filerId, decision) {
  const u = clUnmatched.find(x => x.key === key);
  if (!u) return;
  const cmte = filerId ? clCommittees.find(f => String(f.filer_id) === String(filerId)) : null;
  try {
    const sb = await getSupabase();
    const { data: { session } } = await sb.auth.getSession();
    const { error } = await sb.from("candidate_committee_links").upsert({
      candidate_key: key,
      ballot_name: u.ballot_name,
      office_district: u.office_district,
      filer_id: filerId || null,
      decision,
      committee_name: cmte ? cmte.name : null,
      decided_by: session?.user?.email || null,
    });
    if (error) throw new Error(error.message);
    await clLoad();
  } catch (e) {
    alert(`Could not save: ${e.message}`);
  }
}

function clRenderDecisions() {
  const el = document.getElementById("cl-decisions");
  if (clDecisionsError) {
    el.innerHTML = `<p class="empty-msg">Could not read recorded decisions — ` +
      `${esc(clDecisionsError)}. The count above is not trustworthy until this loads.</p>`;
    return;
  }
  if (!clDecisions.length) {
    el.innerHTML = '<p class="empty-msg">No decisions recorded yet.</p>';
    return;
  }
  el.innerHTML = clDecisions.map(d => `
    <div class="em-row">
      <span><b>${esc(d.ballot_name)}</b>
        <span class="section-desc"> — ${esc(d.office_district)}</span><br/>
        ${d.decision === "link"
          ? `→ ${esc(d.committee_name || d.filer_id)} <span class="section-desc">(${esc(d.filer_id)})</span>`
          : '<span class="section-desc">confirmed: no committee</span>'}
      </span>
      <button class="btn-small cl-undo" data-key="${esc(d.candidate_key)}">Undo</button>
    </div>`).join("");
  el.querySelectorAll(".cl-undo").forEach(btn => btn.addEventListener("click", async () => {
    const sb = await getSupabase();
    await sb.from("candidate_committee_links").delete().eq("candidate_key", btn.dataset.key);
    clLoad();
  }));
}

document.addEventListener("input", e => {
  if (e.target && e.target.id === "cl-filter") clRender(e.target.value.trim());
});

// ── Balance Discrepancies ─────────────────────────────────────────────
//
// Committees whose calculated cash on hand disagrees with the ending balance
// on their own ORESTAR account summary.
//
// The list is precomputed into a dashboard_cache blob rather than derived
// here: the raw material is 7,268 filer_detail rows carrying full timelines
// and donor lists, and pulling all of that into the browser to find the ~2,700
// that disagree would be absurd.
//
// Only paired captures reach this list.  Each row contains the app balance and
// ORESTAR balance frozen together; current activity is context, not part of the
// judged difference.

const BD_SCHEMA_VERSION = 2;
const BD_BASIS = "paired_capture_window_v1";

let bdRows = null;      // active + audit/unpaired buckets, tagged with _bucket
let bdMeta = {};

const bd$ = v => (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US",
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const bdMaybe$ = v => (v == null || !Number.isFinite(Number(v))) ? "—" : bd$(Number(v));

function bdUnpairedRow(row, fallbackReason = "capture_missing") {
  return {
    ...row,
    _bucket: "unpaired",
    status: row.status || "unpaired",
    reason: row.reason || fallbackReason,
    current_calculated: row.current_calculated != null
      ? row.current_calculated : row.calculated,
    latest_orestar: row.latest_orestar != null ? row.latest_orestar : row.orestar,
    current_tran_count: row.current_tran_count != null
      ? row.current_tran_count : row.tran_count,
  };
}

// Fail closed across the schema rollout. Supabase may still hold the old
// live-app-vs-old-summary blob after this UI deploys; those rows are useful as
// leads, but they are not paired evidence and must never acquire the
// "current discrepancy" label merely because they lived in `rows`.
function bdNormalizeBlob(blob) {
  const pairedPayload = blob.schema_version === BD_SCHEMA_VERSION
    && blob.basis === BD_BASIS;
  const rawActionable = blob.rows || [];
  const actionable = pairedPayload
    ? rawActionable.filter(r => r.comparison_status === "paired")
    : [];
  const rejectedActionable = rawActionable
    .filter(r => !pairedPayload || r.comparison_status !== "paired")
    .map(r => bdUnpairedRow(
      r,
      pairedPayload ? "invalid_actionable_row" : "legacy_live_summary_payload",
    ));
  const explicitUnpaired = (blob.unpaired_rows || []).map(r => bdUnpairedRow(r));
  const unpairedRows = [...rejectedActionable, ...explicitUnpaired];

  const rows = [
    ...actionable.map(r => ({ ...r, _bucket: "actionable" })),
    ...(pairedPayload ? (blob.refresh_rows || []) : [])
      .map(r => ({ ...r, _bucket: "refresh" })),
    ...(pairedPayload ? (blob.nonactionable_rows || []) : [])
      .map(r => ({ ...r, _bucket: "nonactionable" })),
    ...unpairedRows,
  ];
  const flagged = pairedPayload
    ? (blob.flagged != null ? blob.flagged : actionable.length)
    : 0;
  const comparable = pairedPayload
    ? (blob.comparable != null ? blob.comparable : (blob.checked || 0))
    : 0;
  const unpaired = pairedPayload
    ? (blob.unpaired != null ? blob.unpaired : unpairedRows.length)
    : Math.max(blob.checked || 0, unpairedRows.length);

  return {
    rows,
    meta: {
      population: pairedPayload
        ? (blob.population != null ? blob.population : (blob.checked || 0))
        : (blob.population != null ? blob.population : (blob.checked || 0)),
      checked: blob.checked || 0,
      unchecked: pairedPayload ? (blob.unchecked || 0) : (blob.checked || 0),
      paired: pairedPayload
        ? (blob.paired != null ? blob.paired : comparable) : 0,
      comparable,
      unpaired,
      refreshNeeded: pairedPayload
        ? (blob.refresh_needed != null
          ? blob.refresh_needed : (blob.newer_app_data || 0)) : 0,
      nonactionable: pairedPayload ? (blob.nonactionable || 0) : 0,
      flagged,
      newerAppData: pairedPayload ? (blob.newer_app_data || 0) : 0,
      newerAppDataAmount: pairedPayload ? (blob.newer_app_data_amount || 0) : 0,
      generated: blob.generated || "",
      pairedPayload,
    },
  };
}

async function bdLoad() {
  if (bdRows) return;
  // dashboard_cache only, deliberately no file fallback. process.py writes this
  // blob to data/aggregated/, which is outside the served tree, so a fallback
  // could only ever read a copy committed by hand — and a hand-copied 600 KB
  // file that nothing refreshes goes stale silently, which is worse than an
  // error that says what is wrong.
  const sb = await getSupabase();
  const { data, error } = await sb.from("dashboard_cache")
    .select("data").eq("key", "balance_discrepancies").single();
  if (error) {
    throw new Error(
      "Balance comparison has not been generated yet — it is built by the next "
      + "data refresh. (" + error.message + ")");
  }
  const blob = data.data;
  const normalized = bdNormalizeBlob(blob);
  bdRows = normalized.rows;
  bdMeta = normalized.meta;
}

function bdRender() {
  const q    = (document.getElementById("bd-filter").value || "").trim().toLowerCase();
  const min  = parseFloat(document.getElementById("bd-min").value) || 0.01;
  const kind = document.getElementById("bd-kind").value;
  const status = document.getElementById("bd-status").value;

  const rows = (bdRows || []).filter(r => {
    const unpaired = r._bucket === "unpaired";
    const judged = Number(r.delta);
    if (status !== "all" && r._bucket !== status) return false;
    // There is intentionally no delta to threshold until both sides are
    // paired. Keep every refusal visible so coverage problems can be measured.
    if (!unpaired && Math.abs(judged) < min) return false;
    if (kind === "active"  && r.dormant) return false;
    if (kind === "dormant" && !r.dormant) return false;
    if (q && !(r.name || "").toLowerCase().includes(q)
          && !String(r.filer_id || "").includes(q)) return false;
    return true;
  });

  const agree = Math.max(0, bdMeta.comparable - bdMeta.flagged);
  document.getElementById("bd-count").textContent =
    `${rows.length.toLocaleString()} shown · ${bdMeta.flagged.toLocaleString()} flagged of `
    + `${bdMeta.comparable.toLocaleString()} current pairs · ${agree.toLocaleString()} agree · `
    + `${bdMeta.refreshNeeded.toLocaleString()} need refresh · `
    + `${bdMeta.unpaired.toLocaleString()} unpaired`
    + (bdMeta.unchecked ? ` (${bdMeta.unchecked.toLocaleString()} incomplete scopes)` : "")
    + ` · `
    + `${bdMeta.nonactionable.toLocaleString()} closed/non-actionable`;

  if (!rows.length) {
    document.getElementById("bd-list").innerHTML =
      '<p class="empty-msg">No committees match these filters.</p>';
    return;
  }

  // Capped: past a few hundred rows this stops being a review tool and starts
  // being a scrolling exercise. The filters are the way to reach the rest.
  const CAP = 300;
  const shown = rows.slice(0, CAP);
  const body = shown.map(r => {
    // `delta` is immutable across its bounded capture window. The current app
    // change is context only and never rewrites that audit fact.
    const unpaired = r._bucket === "unpaired";
    const judged = Number(r.delta);
    const sev = r._bucket !== "actionable" ? "gray"
              : Math.abs(judged) >= 100000 ? "red"
              : Math.abs(judged) >= 10000  ? "yellow" : "gray";
    const since = (!unpaired && r.app_balance_change_since_capture != null
                   && Math.abs(r.app_balance_change_since_capture) > 0.01)
      ? `<span class="bd-since" title="Change in the app's calculated balance since the paired capture; shown for context and not included in the discrepancy">`
        + `${r.app_balance_change_since_capture > 0 ? "+" : ""}${bd$(r.app_balance_change_since_capture)} since</span>`
      : "";
    const appValue = unpaired ? r.current_calculated : r.calculated;
    const orestarValue = unpaired ? r.latest_orestar : r.orestar;
    let reason = {
      legacy_live_summary_payload: "legacy data; awaiting paired refresh",
      invalid_actionable_row: "invalid paired-row metadata",
      capture_missing: "paired capture missing",
      app_snapshot_scope_missing_or_ambiguous: "app snapshot scope missing or ambiguous",
      app_snapshot_changed_before_capture: "app snapshot changed during capture",
      app_snapshot_source_stale: "app snapshot source was stale",
      calculation_version_mismatch: "calculation version mismatch",
      capture_version_mismatch: "capture version mismatch",
      summary_scope_incomplete: "ORESTAR summary scope incomplete",
      annual_summary_years_unpaired: "annual summary years awaiting paired refresh",
    }[r.reason] || String(r.reason || "unpaired capture").replaceAll("_", " ");
    if (r.missing_filer_ids && r.missing_filer_ids.length) {
      reason += `; missing filer ${r.missing_filer_ids.join(", ")}`;
    }
    return `<tr>
      <td><a href="../index.html?filer=${encodeURIComponent(r.slug)}" target="_blank">${esc(r.name || r.slug)}</a>
          ${r._bucket === "actionable" ? '<span class="bd-tag">current discrepancy</span>' : ""}
          ${r._bucket === "refresh" ? '<span class="bd-tag bd-tag-stale" title="The app state, current ORESTAR summary, or a cash-relevant annual summary still needs a paired refresh before this difference is actionable">needs refresh</span>' : ""}
          ${r._bucket === "nonactionable" ? '<span class="bd-tag" title="A trailing blank account summary suggests this committee closed; keep the captured difference for audit, but do not drive a transaction backfill from it">trailing blank / likely closed</span>' : ""}
          ${unpaired ? `<span class="bd-tag bd-tag-unpaired" title="No trustworthy comparison exists until the ORESTAR value and exact app transaction snapshot are captured together">unpaired / unusable</span><span class="bd-reason">${esc(reason)}</span>` : ""}
          ${r.dormant ? '<span class="bd-tag" title="No transactions in the year ORESTAR reports">dormant</span>' : ""}
          ${r.closed ? '<span class="bd-tag" title="ORESTAR reports no activity and a $0.00 balance for this committee">closed</span>' : ""}
          ${r.newer_app_data ? `<span class="bd-tag bd-tag-stale" title="The app's calculated state has changed since this pair was captured. The discrepancy still compares the two frozen values; refresh the pair to make it current.">newer app data</span>` : ""}</td>
      <td class="bd-num">${bdMaybe$(appValue)}</td>
      <td class="bd-num">${bdMaybe$(orestarValue)}</td>
      <td class="bd-num bd-delta bd-${sev}">${unpaired ? "—" : `${judged > 0 ? "+" : ""}${bd$(judged)}${since}`}</td>
      <td class="bd-num">${(unpaired ? (r.current_tran_count || 0) : (r.tran_count || 0)).toLocaleString()}</td>
      <td>${r.orestar_year || "—"}</td>
    </tr>`;
  }).join("");

  document.getElementById("bd-list").innerHTML = `
    <table class="bd-table">
      <thead><tr>
        <th>Committee</th><th class="bd-num">App snapshot</th><th class="bd-num">ORESTAR capture</th>
        <th class="bd-num" title="The published app snapshot and ORESTAR read form one bounded capture window. Any later change moves the pair out of the active discrepancy list.">Difference</th><th class="bd-num">Txns</th><th>Yr</th>
      </tr></thead>
      <tbody>${body}</tbody>
    </table>
    ${rows.length > CAP
      ? `<p class="section-desc">Showing the ${CAP} largest of ${rows.length.toLocaleString()} — narrow the filters to see the rest.</p>`
      : ""}`;
}

["bd-filter", "bd-min", "bd-kind", "bd-status"].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.addEventListener(id === "bd-filter" ? "input" : "change", bdRender);
});
