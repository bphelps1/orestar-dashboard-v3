/**
 * explore.js — filter, download, and query the full transactions dataset.
 *
 * Uses the anon Supabase client (lib/supabase.js):
 *   • Filtered browse/download → live SELECT on the public `transactions` table
 *   • Full-dataset download    → public Storage object (exports/transactions.csv.gz)
 *   • Ad-hoc SQL               → sql-query Edge Function (read-only role)
 */
"use strict";

const PAGE_SIZE = 100;

// Columns shown in the browse table (subset of the full row).
// NOTE: contributor type lives in `book_type` on transaction rows.
// `contributor_type_label`, `party`, and `office` are empty in ORESTAR's
// transaction export (party/office are filer-level metadata), so they are
// deliberately not offered as filters or columns here.
const COLS = [
  { key: "tran_date",                   label: "Date" },
  // ORESTAR's sub-type ("In-Kind Contribution", "Loan Received (Non-Exempt)")
  // says what a row is; the one- or two-letter type code only groups them.
  { key: "sub_type",                    label: "Type" },
  { key: "amount",                      label: "Amount", num: true },
  { key: "filer_canonical",             label: "Committee" },
  { key: "contributor_payee_canonical", label: "Donor / Payee" },
  { key: "book_type",                   label: "Contributor type" },
  { key: "city",                        label: "City" },
  { key: "state",                       label: "State" },
  { key: "employer",                    label: "Employer" },
  { key: "occupation",                  label: "Occupation" },
  { key: "purpose",                     label: "Purpose" },
];
const SELECT_COLS = COLS.map(c => c.key).join(",") + ",tran_id";

let page = 0;
let sortCol = "tran_date";
let sortDir = false; // false = descending
let lastPageCount = 0;
let searchRequest = 0;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtAmount = v => (v == null || v === "") ? "" : Number(v).toLocaleString("en-US", { style: "currency", currency: "USD" });

// Entity-aware donor filter: when the user picks a resolved donor from the
// dropdown, we filter by donor_id (matching every name/address variant)
// instead of a raw-name ilike.
let selectedDonor = null; // {donor_id, display_name}

function readFilters() {
  // The type menu's values are "C" (every contribution) or "C|In-Kind
  // Contribution" (one sub-type within it).
  const [type = "", subType = ""] = $("f-type").value.split("|");
  return {
    filer: $("f-filer").value.trim(),
    payee: $("f-payee").value.trim(),
    donorId: selectedDonor ? selectedDonor.donor_id : "",
    type,
    subType,
    ctype: $("f-ctype").value.trim(),
    dateStart: $("f-date-start").value,
    dateEnd: $("f-date-end").value,
    amtMin: $("f-amt-min").value,
    amtMax: $("f-amt-max").value,
  };
}

/** Apply the current filters to a supabase query builder. */
function nameFilter(canonical, raw, value) {
  // PostgREST quotes protect commas/parentheses in literal organization names.
  const pattern = '"%' + value.replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '%"';
  return `${canonical}.ilike.${pattern},${raw}.ilike.${pattern}`;
}
function applyFilters(q, f) {
  const names = [];
  if (f.filer) names.push(`or(${nameFilter("filer_canonical", "filer", f.filer)})`);
  if (f.donorId) q = q.in("donor_id", f.donorIds || [f.donorId]);
  else if (f.payee) names.push(`or(${nameFilter("contributor_payee_canonical", "contributor_payee", f.payee)})`);
  if (names.length) q = q.or(`and(${names.join(",")})`);
  if (f.type)      q = q.eq("tran_type", f.type);
  if (f.subType)   q = q.eq("sub_type", f.subType);
  if (f.ctype)     q = q.ilike("book_type", `%${f.ctype}%`);
  if (f.dateStart) q = q.gte("tran_date", f.dateStart);
  if (f.dateEnd)   q = q.lte("tran_date", f.dateEnd);
  if (f.amtMin !== "") q = q.gte("amount", Number(f.amtMin));
  if (f.amtMax !== "") q = q.lte("amount", Number(f.amtMax));
  return q;
}

const nonblank = value => String(value ?? "").trim();

/** Preserve source columns, but never let an absent derived label hide a recorded name. */
function transactionNames(row, donor = null) {
  const selectedName = donor && String(row.donor_id) === String(donor.donor_id)
    ? nonblank(donor.display_name) : "";
  const payee = nonblank(row.contributor_payee_canonical) || selectedName
    || nonblank(row.contributor_payee) || "Not reported";
  return { ...row,
    filer_canonical: nonblank(row.filer_canonical) || nonblank(row.filer)
      || (nonblank(row.filer_id) ? `Committee ${row.filer_id}` : "Not reported"),
    contributor_payee_canonical: typeof DN === "undefined" ? payee : DN.display(payee),
  };
}

/** The browse RPC omits source names. Fetch only incomplete rows by primary key. */
async function completeTransactionNames(sb, rows, donor) {
  const missing = rows.filter(r => !nonblank(r.filer_canonical) || !nonblank(r.contributor_payee_canonical));
  let sources = new Map();
  if (missing.length) {
    const { data, error } = await sb.from("transactions")
      .select("tran_id,filer,filer_id,contributor_payee,donor_id")
      .in("tran_id", missing.map(r => r.tran_id));
    if (error) throw new Error(`Could not load transaction names: ${error.message}`);
    sources = new Map((data || []).map(r => [String(r.tran_id), r]));
  }
  return rows.map(row => transactionNames({ ...sources.get(String(row.tran_id)), ...row }, donor));
}

function showError(el, msg) {
  const box = $(el);
  if (!msg) { box.hidden = true; box.textContent = ""; return; }
  box.hidden = false;
  box.textContent = msg;
}

// ── Browse ──────────────────────────────────────────────────────────────────
async function runSearch() {
  const request = ++searchRequest;
  const donor = selectedDonor;
  showError("xp-error", "");
  $("xp-status").textContent = "Loading…";
  try {
    await DN.load();
    const f = readFilters();
    // A one-character substring matches millions of rows and can't be sorted
    // inside any sane timeout. Two characters is also what the donor
    // autocomplete requires, so the two behave consistently.
    const tooShort = [f.filer, f.payee].filter(v => v && v.length < 2);
    if (tooShort.length) {
      $("xp-status").textContent = "";
      showError("xp-error", "Enter at least 2 characters to search by name.");
      return;
    }
    const sb = await getSupabase();
    // Goes through explore_transactions() rather than PostgREST filters: for a
    // substring search the planner mis-estimates selectivity and walks the date
    // index row by row (~2s, HTTP 500 under load). The function denies that
    // plan so the trigram index is used instead — "nike" 3.5s -> 0.23s. It is
    // search_transactions() plus sub-types (migration 034).
    const { data, error } = await sb.rpc("explore_transactions", {
      p_filer: f.filer, p_payee: f.payee, p_donor_id: f.donorId,
      p_tran_type: f.type, p_sub_type: f.subType, p_book_type: f.ctype,
      p_date_from: f.dateStart || null, p_date_to: f.dateEnd || null,
      p_amt_min: f.amtMin === "" ? null : Number(f.amtMin),
      p_amt_max: f.amtMax === "" ? null : Number(f.amtMax),
      p_sort: sortCol, p_asc: sortDir,
      p_limit: PAGE_SIZE, p_offset: page * PAGE_SIZE,
    });
    if (error) throw new Error(error.message);
    if (typeof DN !== "undefined") await DN.load();
    const namedRows = await completeTransactionNames(sb, data, donor);
    if (request !== searchRequest) return;
    lastPageCount = data.length;
    renderTable(namedRows);
    $("xp-status").textContent = data.length
      ? `Showing ${page * PAGE_SIZE + 1}–${page * PAGE_SIZE + data.length}`
      : "No matching transactions";
    $("pg-prev").disabled = page === 0;
    $("pg-next").disabled = data.length < PAGE_SIZE;
    $("pg-label").textContent = `Page ${page + 1}`;
  } catch (e) {
    if (request !== searchRequest) return;
    $("xp-status").textContent = "";
    // A very broad term ("oregon" matches 124k rows) can still exceed the
    // server's statement timeout: sorting that many rows by date is expensive
    // however it's planned. Picking the donor from the dropdown filters by
    // donor_id — an indexed lookup that stays ~0.2s at any breadth.
    const timedOut = /timeout|57014|statement/i.test(e.message || "");
    showError("xp-error", timedOut
      ? "That search matched too many records to sort in time. Narrow it — add a "
        + "date range or amount, or pick a specific donor from the suggestions "
        + "under “Donor / payee”, which is much faster."
      : e.message);
  }
}

/** A cell's text; a row without a recorded sub-type still shows its type code. */
function cellText(row, col) {
  if (col.key === "sub_type") return row.sub_type || row.tran_type;
  if (col.key === "contributor_payee_canonical") return DN.display(row[col.key]);
  return row[col.key];
}

function renderTable(rows) {
  $("xp-thead").innerHTML = "<tr>" + COLS.map(c =>
    `<th class="${c.num ? "num" : ""}" data-col="${c.key}">${c.label}${sortCol === c.key ? (sortDir ? " ▲" : " ▼") : ""}</th>`
  ).join("") + "</tr>";
  $("xp-tbody").innerHTML = rows.map(r => "<tr>" + COLS.map(c =>
    `<td class="${c.num ? "num" : ""}">${c.num ? fmtAmount(r[c.key]) : esc(cellText(r, c))}</td>`
  ).join("") + "</tr>").join("");
  $("xp-thead").querySelectorAll("th").forEach(th => {
    th.onclick = () => {
      const col = th.dataset.col;
      if (sortCol === col) sortDir = !sortDir; else { sortCol = col; sortDir = false; }
      page = 0;
      runSearch();
    };
  });
}

// ── Downloads ───────────────────────────────────────────────────────────────
// The API caps any single response at 1,000 rows, so a one-shot download would
// silently truncate (a committee can have 20k+ transactions). We page through
// with .range() and assemble the CSV here, so researchers get the complete
// filtered set without weakening the server-side cap.
const DOWNLOAD_CHUNK = 1000;
const DOWNLOAD_MAX_ROWS = 100000;   // ceiling so a browser can't hang on a huge export

async function downloadFiltered() {
  showError("xp-error", "");
  const btn = $("btn-download");
  btn.disabled = true;
  try {
    const sb = await getSupabase();
    const filters = readFilters();
    const donor = selectedDonor;
    if (filters.donorId) {
      const { data, error } = await sb.rpc("donor_group_ids", { p_donor_id: filters.donorId });
      if (error) throw new Error(`Could not resolve selected donor: ${error.message}`);
      filters.donorIds = data?.length ? data : [filters.donorId];
    }
    if (typeof DN !== "undefined") await DN.load();
    let rows = [];
    let truncated = false;

    for (let offset = 0; ; offset += DOWNLOAD_CHUNK) {
      $("xp-status").textContent = `Preparing CSV… ${rows.length.toLocaleString()} rows`;
      const { data, error } = await applyFilters(sb.from("transactions").select("*"), filters)
        // Stable key so paging can't skip or repeat rows between requests.
        .order("tran_id", { ascending: true })
        .range(offset, offset + DOWNLOAD_CHUNK - 1);
      if (error) throw new Error(error.message);
      rows = rows.concat(data.map(row => transactionNames(row, donor)));
      if (data.length < DOWNLOAD_CHUNK) break;
      if (rows.length >= DOWNLOAD_MAX_ROWS) { truncated = true; break; }
    }

    if (!rows.length) {
      $("xp-status").textContent = "No rows match these filters — nothing to download.";
      return;
    }

    const cols = Object.keys(rows[0]);
    const csv = [cols.map(csvCell).join(",")]
      .concat(rows.map(r => cols.map(c => csvCell(r[c])).join(",")))
      .join("\n");
    triggerDownload(csv, "text/csv", "orestar_filtered.csv");
    $("xp-status").textContent = truncated
      ? `Downloaded first ${rows.length.toLocaleString()} rows — narrow the filters to get the rest.`
      : `Downloaded ${rows.length.toLocaleString()} rows.`;
  } catch (e) {
    $("xp-status").textContent = "";
    showError("xp-error", e.message);
  } finally {
    btn.disabled = false;
  }
}

function triggerDownload(text, mime, filename) {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ── SQL box ─────────────────────────────────────────────────────────────────
let sqlColumns = [];
let sqlRows = [];

async function runSQL() {
  showError("sql-error", "");
  $("sql-status").textContent = "Running…";
  $("btn-sql-csv").disabled = true;
  try {
    const sb = await getSupabase();
    const { data, error } = await sb.functions.invoke("sql-query", { body: { sql: $("sql-input").value } });
    if (error) {
      // Edge function returned a non-2xx; surface its JSON error if present.
      let msg = error.message;
      try { msg = (await error.context.json()).error || msg; } catch { /* ignore */ }
      throw new Error(msg);
    }
    if (data.error) throw new Error(data.error);
    sqlColumns = data.columns || [];
    sqlRows = data.rows || [];
    renderSQL(sqlColumns, sqlRows);
    $("sql-status").textContent =
      `${sqlRows.length} row${sqlRows.length === 1 ? "" : "s"}${data.truncated ? " (truncated)" : ""}`;
    $("btn-sql-csv").disabled = sqlRows.length === 0;
  } catch (e) {
    $("sql-status").textContent = "";
    renderSQL([], []);
    showError("sql-error", e.message);
  }
}

function renderSQL(cols, rows) {
  $("sql-thead").innerHTML = "<tr>" + cols.map(c => `<th>${esc(c)}</th>`).join("") + "</tr>";
  $("sql-tbody").innerHTML = rows.map(r => "<tr>" + cols.map(c => `<td>${esc(r[c])}</td>`).join("") + "</tr>").join("");
}

function downloadSQLCsv() {
  const head = sqlColumns.map(csvCell).join(",");
  const body = sqlRows.map(r => sqlColumns.map(c => csvCell(r[c])).join(",")).join("\n");
  triggerDownload(head + "\n" + body, "text/csv", "orestar_query.csv");
}

function csvCell(v) {
  const s = String(v ?? "");
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

// ── Donor entity autocomplete ───────────────────────────────────────────────
let donorSearchTimer = null;

function initDonorAutocomplete() {
  const input = $("f-payee");
  const ul = $("f-payee-results");
  input.addEventListener("input", () => {
    selectedDonor = null; // typing clears any picked entity
    clearTimeout(donorSearchTimer);
    const q = input.value.trim();
    if (q.length < 2) { ul.hidden = true; return; }
    donorSearchTimer = setTimeout(async () => {
      try {
        await DN.load();
        const sb = await getSupabase();
        // Alias-aware, same as /donors: a raw spelling finds the entity.
        const { data } = await sb.rpc("search_donors", { p_q: q, p_limit: 8 });
        if (!data || !data.length) { ul.hidden = true; return; }
        ul.innerHTML = data.map((d, i) =>
          `<li data-idx="${i}">${esc(DN.display(d.display_name))}
             <div class="sub">${esc([d.book_type, [d.city, d.state].filter(Boolean).join(", ")].filter(Boolean).join(" · "))}
             · ${Number(d.total_given || 0).toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 })}</div></li>`).join("");
        ul.hidden = false;
        ul.querySelectorAll("li").forEach((li, i) => li.addEventListener("mousedown", () => {
          selectedDonor = data[i];
          input.value = DN.display(data[i].display_name);
          ul.hidden = true;
          page = 0;
          runSearch();
        }));
      } catch { ul.hidden = true; }
    }, 220);
  });
  document.addEventListener("click", e => {
    if (!e.target.closest("#f-payee") && !e.target.closest("#f-payee-results")) ul.hidden = true;
  });
}

// ── Init ────────────────────────────────────────────────────────────────────
function resetFilters() {
  ["f-filer", "f-payee", "f-ctype", "f-date-start", "f-date-end", "f-amt-min", "f-amt-max"]
    .forEach(id => { $(id).value = ""; });
  $("f-type").value = "";
  selectedDonor = null;
  page = 0; sortCol = "tran_date"; sortDir = false;
  runSearch();
}

document.addEventListener("DOMContentLoaded", () => {
  $("btn-search").onclick = () => { page = 0; runSearch(); };
  $("btn-reset").onclick = resetFilters;
  $("btn-download").onclick = downloadFiltered;
  $("btn-sql").onclick = runSQL;
  $("btn-sql-csv").onclick = downloadSQLCsv;
  $("pg-prev").onclick = () => { if (page > 0) { page--; runSearch(); } };
  $("pg-next").onclick = () => { if (lastPageCount === PAGE_SIZE) { page++; runSearch(); } };
  ["f-filer", "f-payee", "f-ctype"].forEach(id =>
    $(id).addEventListener("keydown", e => { if (e.key === "Enter") { page = 0; runSearch(); } }));
  initDonorAutocomplete();
  runSearch();
});
