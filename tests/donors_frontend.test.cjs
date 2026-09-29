"use strict";

// Run with: node --test tests/donors_frontend.test.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const root = path.resolve(__dirname, "..");
const app = fs.readFileSync(path.join(root, "docs/app.js"), "utf8");
const donorCode = app.slice(app.indexOf("let donorsLoadVersion = 0;"), app.indexOf("// ── Recipients ─"));
const tableCode = app.slice(app.indexOf("function buildSortableTable("), app.indexOf("// ── Filer selector ─"));
const previewCode = app.slice(app.indexOf("// Preview popover state"), app.indexOf("// Ambiguous chooser popover"));
const errorCode = app.slice(app.indexOf("function showError("), app.indexOf("function esc("));
const tabRenderCode = app.slice(app.indexOf("let activeTabLoadVersion = 0;"), app.indexOf("function onStateChange("));
const plain = value => JSON.parse(JSON.stringify(value));
const donation = (name, total, donor_id) => ({ name, total, ...(donor_id ? { donor_id } : {}) });
const dataFor = rows => ({ all_time: rows, by_year: { 2026: rows } });

function harness({ getDonors, profiles = {}, blob = dataFor([]) } = {}) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: "", innerHTML: "", hidden: false, dataset: {}, style: {},
      getBoundingClientRect() { return { bottom: 200, right: 200, left: 0, top: 0, height: 200, width: 200 }; },
      remove() { this.removed = true; },
      classList: { add() {}, remove() {} },
      addEventListener(type, callback) { this[`on${type}`] = callback; },
      insertAdjacentHTML(position, html) { this.innerHTML += html; },
      querySelector(selector) { return element(`${id} ${selector}`); },
      querySelectorAll() { return []; },
    });
    return elements.get(id);
  }
  const charts = [];
  const rpcCalls = [];
  const context = vm.createContext({
    state: { selectedFilers: [], dateStart: "2024-12-01", dateEnd: "2026-11-30" },
    donorsData: null, donorsViewMode: "summary", donorFilerMap: null, filerIndex: [],
    document: { getElementById: element, querySelector: element, querySelectorAll: () => [],
      createElement: () => element("preview"), body: { appendChild() {} } },
    window: { innerWidth: 1200, innerHeight: 800 },
    DL: {
      async getBlob() { return blob; },
      async getDonors(params) {
        rpcCalls.push(plain(params));
        return getDonors ? getDonors(params) : dataFor([]);
      },
    },
    async ensureDonorFilerMap() {},
    async loadFilerProfile(slug) { return profiles[slug]; },
    renderActiveTab() {},
    filterMonthRows: rows => rows,
    activeCashThroughMonth: () => "2026-11",
    statsFromTimeline: () => ({ totalIn: 100, totalOut: 50, cashOnHand: 50 }),
    makeBarChart(...args) { charts.push(plain(args)); },
    esc: String,
    fmt$: value => `$${Number(value).toFixed(2)}`,
    setTimeout(callback) { callback(); },
  });
  vm.runInContext(tableCode + donorCode + previewCode, context);
  return { context, element, charts, rpcCalls };
}

test("tab retries clear their errors and late failures cannot restore stale warnings", async () => {
  const errors = [];
  const context = vm.createContext({
    activeTab: "donors",
    loaders: { donors: async () => { throw new Error("temporary timeout"); }, overview: async () => {} },
    console: { error() {} },
    document: {
      createElement: () => ({ dataset: {}, remove() { errors.splice(errors.indexOf(this), 1); } }),
      querySelector: () => ({ prepend(el) { errors.unshift(el); } }),
      querySelectorAll: selector => {
        assert.equal(selector, '.error-msg[data-error-scope="tab-load"]');
        return errors.filter(el => el.dataset.errorScope === "tab-load");
      },
    },
  });
  vm.runInContext(errorCode + tabRenderCode, context);
  context.showError("Initialization issue");
  await context.renderActiveTab();
  assert.equal(errors.length, 2);
  assert.match(errors[0].textContent, /donors.*temporary timeout/);
  context.loaders.donors = async () => {};
  await context.renderActiveTab();
  assert.deepEqual(errors.map(el => el.textContent), ["⚠ Initialization issue"]);

  for (const nextTab of ["overview", "donors"]) {
    let rejectOldRequest;
    context.activeTab = "donors";
    context.loaders.donors = () => new Promise((resolve, reject) => { rejectOldRequest = reject; });
    const oldLoad = context.renderActiveTab();
    context.activeTab = nextTab;
    context.loaders.donors = async () => {};
    await context.renderActiveTab();
    rejectOldRequest(new Error("late timeout"));
    await oldLoad;
    assert.deepEqual(errors.map(el => el.textContent), ["⚠ Initialization issue"]);
  }
});

test("legacy whitespace/case labels combine before ranking; resolved names stay distinct", () => {
  const { context } = harness();
  const result = plain(context.normalizeDonorRows([
    donation("Miscellaneous Cash Contributions $100 and under ", 102256183.54, "old-1"),
    donation(" miscellaneous\u00a0cash contributions $100 and UNDER", 36328851.54, "old-2"),
    donation("Alex Smith", 20, "person-1"),
    donation("ALEX SMITH ", 30, "person-2"),
    donation(" Other  Donor ", 10.01), donation("other donor", 20.02),
  ]));
  assert.equal(result[0].name, "Miscellaneous Cash Contributions $100 and under");
  assert.equal(result[0].total, 138585035.08);
  assert.equal(result.filter(row => /alex smith/i.test(row.name)).length, 2);
  assert.equal(result.find(row => row.name === "Other Donor").total, 30.03);
});

test("2026 cycle chart and summary use exact dates and returned total", async () => {
  const current = donation("Friends of Julie Fahey", 440832.50, "fahey");
  const { context, rpcCalls, charts, element } = harness({ getDonors: () => dataFor([current]) });
  await context.loadDonors();
  assert.deepEqual(rpcCalls, [{ start: "2024-12-01", end: "2026-11-30" }]);
  assert.deepEqual(charts.at(-1)[2], [440832.5]);
  assert.match(element("table-donors tbody").innerHTML, /\$440832\.50/);
  assert.equal(element("donor-year-group").hidden, true);
});

test("by-year uses period totals and identities; search uses the latest filter", async () => {
  let current = {
    all_time: [donation("Alex Smith", 11, "one"), donation("Alex Smith", 22, "two")],
    by_year: { 2026: [donation("ALEX SMITH ", 11, "one"), donation("Alex Smith", 22, "two")] },
  };
  const { context, element } = harness({ getDonors: () => current });
  context.donorsViewMode = "by-year";
  await context.loadDonors();
  assert.match(element("donors-by-year-thead").innerHTML, /Selected Period/);
  const originalRows = element("#table-donors-by-year tbody").innerHTML;
  assert.match(originalRows, /\$22\.00<\/td>\s*<td class="num">\$22\.00/);
  assert.match(originalRows, /\$11\.00<\/td>\s*<td class="num">\$11\.00/);
  current = dataFor([donation("Latest donor", 5)]);
  context.state.dateStart = "2026-09-01";
  await context.loadDonors();
  element("donors-by-year-search").value = "latest";
  element("donors-by-year-search").oninput();
  assert.match(element("#table-donors-by-year tbody").innerHTML, /Latest donor/);
  assert.doesNotMatch(element("#table-donors-by-year tbody").innerHTML, /Alex/);
});

test("single and multi filer requests retain exact dates and all profile filer IDs", async () => {
  const { context, rpcCalls, element } = harness({
    profiles: { a: { name: "Committee A", filer_ids: [12, "13"] }, b: { name: "Committee B" } },
    getDonors: ({ filerIds }) => dataFor([donation(filerIds[0] === "12" ? "Local donor A" : "Local donor B", 10)]),
  });
  context.state.selectedFilers = [{ slug: "a" }];
  await context.loadDonors();
  assert.deepEqual(rpcCalls[0].filerIds, ["12", "13"]);
  context.state.selectedFilers.push({ slug: "b", filer_id: 99 });
  await context.loadDonors();
  assert.deepEqual(rpcCalls.at(-1), { start: "2024-12-01", end: "2026-11-30", filerIds: ["99"] });
  assert.match(element("#table-donors-multi tbody").innerHTML, /Local donor A/);
  assert.match(element("#table-donors-multi tbody").innerHTML, /Local donor B/);
});

test("missing selected committee identity cannot become a statewide query", async () => {
  const { context, rpcCalls } = harness({ profiles: { unknown: { name: "Unknown" } } });
  context.state.selectedFilers = [{ slug: "unknown" }];
  await assert.rejects(context.loadDonors(), /No filer ID available/);
  assert.equal(rpcCalls.length, 0);
});

test("an older request cannot overwrite a newer filter result", async () => {
  const pending = [];
  const { context, charts, rpcCalls } = harness({ getDonors: () => new Promise(resolve => pending.push(resolve)) });
  const first = context.loadDonors();
  await new Promise(resolve => setImmediate(resolve));
  context.state.dateStart = "2026-09-01";
  context.state.dateEnd = "2026-09-12";
  const second = context.loadDonors();
  await new Promise(resolve => setImmediate(resolve));
  pending[1](dataFor([donation("New selection", 20)]));
  await second;
  pending[0](dataFor([donation("Old selection", 999)]));
  await first;
  assert.deepEqual(rpcCalls[0], { start: "2024-12-01", end: "2026-11-30" });
  assert.deepEqual(charts.at(-1)[1], ["New selection"]);
  assert.equal(charts.length, 1);
});

test("pending donor results are discarded when filters change on another tab", async () => {
  for (const changeFilter of [
    context => { context.state.dateStart = "2026-09-01"; },
    context => { context.state.selectedFilers = [{ slug: "new-selection", filer_id: 42 }]; },
  ]) {
    let resolveRequest;
    const { context, charts } = harness({ getDonors: () => new Promise(resolve => { resolveRequest = resolve; }) });
    const loading = context.loadDonors();
    await new Promise(resolve => setImmediate(resolve));
    // Other tabs do not increment the Donors render generation.
    changeFilter(context);
    resolveRequest(dataFor([donation("Outdated donor", 999)]));
    await loading;
    assert.equal(charts.length, 0);
  }
});

test("preview top-five donors use exact dates and scoped IDs without changing account stats", async () => {
  const { context, rpcCalls, element } = harness({
    profiles: { future_pac: { name: "Future PAC", filer_ids: [999], top_donors: [donation("Old donor", 1200000)] } },
    getDonors: () => dataFor([donation("Friends of Julie Fahey", 440832.5)]),
  });
  await context.showPreviewPopover("future_pac", element("anchor"));
  assert.deepEqual(rpcCalls, [{ start: "2024-12-01", end: "2026-11-30", filerIds: ["999"] }]);
  assert.match(element("preview").innerHTML, /Friends of Julie Fahey.*\$440832\.50/);
  assert.match(element("preview").innerHTML, /Contributions:<\/span><span>\$100\.00/);
  assert.doesNotMatch(element("preview").innerHTML, /Old donor/);
});

test("preview cannot show results for dates that changed during loading", async () => {
  let resolveRequest;
  const { context, element } = harness({
    profiles: { committee: { name: "Committee", filer_ids: [999] } },
    getDonors: () => new Promise(resolve => { resolveRequest = resolve; }),
  });
  const loading = context.showPreviewPopover("committee", element("anchor"));
  await new Promise(resolve => setImmediate(resolve));
  context.state.dateEnd = "2026-09-12";
  resolveRequest(dataFor([donation("Outdated donor", 999)]));
  await loading;
  assert.equal(element("preview").removed, true);
});

test("no-date cache groups labels before graph/table limits; summary search refreshes", async () => {
  const { context, charts, element, rpcCalls } = harness({ blob: dataFor([
    donation("Small Donor ", 4), donation("small donor", 5), donation("Other donor", 8),
  ]) });
  context.state.dateStart = context.state.dateEnd = "";
  await context.loadDonors();
  assert.deepEqual(charts.at(-1)[2], [9, 8]);
  assert.equal(rpcCalls.length, 0);
  context.renderDonorSummary([donation("New donor", 3)]);
  element("donors-summary-search").value = "new";
  element("donors-summary-search").oninput();
  assert.match(element("table-donors tbody").innerHTML, /New donor/);
  assert.doesNotMatch(element("table-donors tbody").innerHTML, /Small Donor/);
});

test("data layer memoizes exact scope, supports open ends, and retries failed requests", async () => {
  const calls = [];
  let fail = false;
  const context = vm.createContext({
    async getSupabase() { return {
      async rpc(name, params) {
        calls.push({ name, params: plain(params) });
        return fail ? { error: { message: "temporary error" } } : { data: dataFor([]) };
      },
    }; },
  });
  vm.runInContext(fs.readFileSync(path.join(root, "docs/lib/data.js"), "utf8") + "\nglobalThis.dataLayer = DL;", context);
  const dl = context.dataLayer;
  const first = dl.getDonors({ start: "2024-12-01", end: "2026-11-30", filerIds: [2, "1", 2] });
  const same = dl.getDonors({ start: "2024-12-01", end: "2026-11-30", filerIds: ["1", "2"] });
  assert.equal(first, same);
  await first;
  assert.deepEqual(calls[0], { name: "donor_leaderboard", params: { p_start: "2024-12-01", p_end: "2026-11-30", p_filer_ids: ["1", "2"] } });
  await dl.getDonors({ end: "2026-09-12" });
  assert.deepEqual(calls.at(-1).params, { p_start: null, p_end: "2026-09-12", p_filer_ids: null });
  await assert.rejects(dl.getDonors({ filerIds: [] }), /no filer ID/);
  fail = true;
  await assert.rejects(dl.getDonors({ start: "2026-09-01" }), /temporary error/);
  fail = false;
  await dl.getDonors({ start: "2026-09-01" });
  assert.equal(calls.length, 4);
});

test('Donor Lookup uses original committee names when canonical names are blank', async () => {
  const code = fs.readFileSync(path.join(root, 'docs/donors.js'), 'utf8');
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { querySelector: () => element(`${id} tbody`) });
    return elements.get(id);
  };
  const data = [
    { filer_canonical: null, filer: 'Friends of Tina Kotek', amount: 20000 },
    { filer_canonical: '  ', filer: 'Committee to Elect Lucetta Elmer', amount: 2500 },
    { filer_canonical: 'Friends of Julie Fahey', filer: 'Raw name', amount: 2500 },
    { filer_canonical: null, filer: null, filer_id: '99', amount: 500 },
  ];
  const query = { select() { return this; }, in() { return this; }, eq() { return this; }, order() { return this; },
    async range() { return { data }; } };
  const ctx = vm.createContext({ $: element, esc: String, fmt$: String, PAGE: 25, txnPage: 0,
    currentDonor: { donor_id: 'uber' }, getSupabase: async () => ({ from: () => query }) });
  vm.runInContext(code.slice(code.indexOf('async function loadTxns()'), code.indexOf('// ── Init')), ctx);
  await ctx.loadTxns();
  const html = element('dn-txns tbody').innerHTML;
  for (const name of ['Friends of Tina Kotek', 'Committee to Elect Lucetta Elmer', 'Friends of Julie Fahey', 'Committee 99']) assert.ok(html.includes(name));
  assert.ok(!html.includes('Raw name'));
});

test('Top Recipients never renders a blank linked or unlinked committee label', () => {
  const code = fs.readFileSync(path.join(root, 'docs/donors.js'), 'utf8');
  const tbody = { innerHTML: '' };
  const ctx = vm.createContext({
    $: () => ({ querySelector: () => tbody, querySelectorAll: () => [] }),
    esc: s => String(s).replaceAll('<', '&lt;'), fmtN: String, fmt$: String,
  });
  vm.runInContext(code.slice(code.indexOf('function renderRecipients('), code.indexOf('async function loadTxns(')), ctx);
  ctx.renderRecipients([
    { filer: null, filer_id: '4792', slug: 'friends_of_tina_kotek', n: 2, total: 22500 },
    { filer: '  ', filer_id: '20136', n: 1, total: 2500 },
    { filer: null, n: 1, total: 500 },
    { filer: '<Named Committee>', n: 1, total: 500 },
  ]);
  assert.match(tbody.innerHTML, />Committee 4792<\/a>/);
  assert.match(tbody.innerHTML, /Committee 20136/);
  assert.match(tbody.innerHTML, /Committee name unavailable/);
  assert.match(tbody.innerHTML, /&lt;Named Committee>/);
});
