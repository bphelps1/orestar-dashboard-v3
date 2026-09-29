'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const src = fs.readFileSync(require('node:path').join(__dirname, '../docs/admin/donors.js'), 'utf8');
function harness(error = null) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { textContent: '', disabled: false, value: '' });
    return elements.get(id);
  };
  const writes = [];
  const ctx = vm.createContext({ document: { getElementById: element, querySelectorAll: () => [] },
    getSession: async () => ({ user: { email: 'admin@example.test' } }),
    getSupabase: async () => ({ from: () => ({ upsert: async rows => { writes.push(rows); return { error }; } }) }),
    emLoadList: async () => {},
  });
  vm.runInContext(src.slice(src.indexOf('const emState ='), src.indexOf('async function emLoadList('))
    + '\nemRenderCard = () => {}; this.state = emState;', ctx);
  ctx.state.a = { donor_id: 'a', rep_alias_key: 'z|a', display_name: 'Keep' };
  ctx.state.selected.set('b', { donor_id: 'b', rep_alias_key: 'b|a', display_name: 'Alias B' });
  ctx.state.selected.set('c', { donor_id: 'c', rep_alias_key: 'c|a', display_name: 'Alias C' });
  return { ctx, writes, element };
}
test('bulk merge is a single atomic upsert with labels matching sorted keys', async () => {
  const { ctx, writes, element } = harness();
  await ctx.emRecord('merged');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].length, 2);
  assert.equal(writes[0][0].alias_a, 'b|a');
  assert.equal(writes[0][0].label_a, 'Alias B');
  assert.equal(writes[0][0].label_b, 'Keep');
  assert.ok(writes[0].every(row => row.keep_alias_key === 'z|a'));
  assert.match(element('em-status').textContent, /Merge applied/);
  assert.equal(ctx.state.selected.size, 0);
  assert.equal(ctx.state.saving, false);
});
test('failed bulk merge retains selections for retry and shows the error', async () => {
  const { ctx, writes, element } = harness({ message: 'write rejected' });
  await ctx.emRecord('merged');
  assert.equal(writes.length, 1);
  assert.equal(ctx.state.selected.size, 2);
  assert.equal(ctx.state.a.display_name, 'Keep');
  assert.match(element('em-status').textContent, /write rejected/);
  assert.equal(ctx.state.saving, false);
});
test('self merges and duplicate submissions cannot write', async () => {
  const { ctx, writes } = harness();
  ctx.state.selected.set('a', ctx.state.a);
  await ctx.emRecord('merged');
  assert.equal(writes.length, 0);
  ctx.state.selected.delete('a');
  ctx.state.saving = true;
  await ctx.emRecord('merged');
  assert.equal(writes.length, 0);
});

// ── The decision log ───────────────────────────────────────────────────────
//
// It showed the newest 200 decisions and filtered only those, so the Amazon
// Web Services merge — decision 601 of 710 — could not be found or undone.
function logHarness({ rows = [], count = rows.length, error = null, deleted = [{ merge_key: 'k' }],
                      deleteError = null } = {}) {
  const calls = [];
  const el = { innerHTML: '', listeners: [] };
  const buttons = [];
  const builder = () => {
    const q = { ops: [] };
    const chain = new Proxy(q, { get: (t, name) => {
      if (name === 'then') return (resolve) => resolve(q.op === 'delete'
        ? { data: deleteError ? null : deleted, error: deleteError }
        : { data: error ? null : rows, error, count });
      return (...args) => { t.ops.push([name, ...args]); if (name === 'delete') t.op = 'delete'; return chain; };
    } });
    calls.push(q);
    return chain;
  };
  const ctx = vm.createContext({
    document: { getElementById: id => id === 'em-list' ? el : { value: '' } },
    getSupabase: async () => ({ from: builder }),
    esc: s => String(s ?? ''),
  });
  vm.runInContext(src.slice(src.indexOf('// The unfiltered log is the newest'), src.indexOf('function initEntityMerge(')), ctx);
  el.querySelectorAll = () => buttons;
  return { ctx, calls, el, buttons };
}
const opNames = q => q.ops.map(o => o[0]);

test('an unfiltered log is the newest 200, and says so when there are more', async () => {
  const { ctx, calls, el } = logHarness({ rows: [{ decision: 'merged', label_a: 'A', label_b: 'B', merge_key: 'k' }], count: 710 });
  await ctx.emLoadList('');
  assert.deepEqual(calls[0].ops.find(o => o[0] === 'limit'), ['limit', 200]);
  assert.ok(!opNames(calls[0]).includes('or'), 'no filter means no or()');
  assert.match(el.innerHTML, /Showing the 1 most recent of 710 decisions\. Filter to reach older ones\./);
});

test('a filter is answered by the database across every decision', async () => {
  const { ctx, calls } = logHarness({ rows: [{ decision: 'merged', label_a: 'Amazon.com, Inc.', label_b: 'Amazon Web Services', merge_key: 'k' }] });
  await ctx.emLoadList('Amazon.com, Inc.');
  const or = calls[0].ops.find(o => o[0] === 'or');
  assert.ok(or, 'filtered server-side');
  // Every column, quoted so the comma and periods are not read as syntax.
  for (const col of ['label_a', 'label_b', 'alias_a', 'alias_b']) {
    assert.ok(or[1].includes(`${col}.ilike."*Amazon.com, Inc.*"`), or[1]);
  }
  assert.deepEqual(calls[0].ops.find(o => o[0] === 'limit'), ['limit', 500]);
});

test('quotes and backslashes in a search cannot break out of the filter', () => {
  const { ctx } = logHarness();
  assert.equal(ctx.emOrTerm('a"b\\c'), '"*a\\"b\\\\c*"');
});

test('the note tells you when a filter matched more than is shown', () => {
  const { ctx } = logHarness();
  assert.equal(ctx.emLogNote('amazon', 500, 612), 'Showing 500 of 612 matching decisions — narrow the filter to see the rest.');
  assert.equal(ctx.emLogNote('amazon', 2, 2), '2 matching decisions.');
  assert.equal(ctx.emLogNote('', 12, 12), '', 'a short log needs no note');
});

test('an empty search result names what was searched for', async () => {
  const { ctx, el } = logHarness({ rows: [] });
  await ctx.emLoadList('Weyerhauser');
  assert.match(el.innerHTML, /No decision matches .Weyerhauser./);
});

test('undo that fails says so instead of silently reloading', async () => {
  const btn = { dataset: { key: 'k' }, disabled: false, textContent: 'Undo' };
  const { ctx, calls } = logHarness({ deleteError: { message: 'DELETE requires a WHERE clause' } });
  await ctx.emUndo(btn);
  assert.equal(btn.textContent, 'Undo failed: DELETE requires a WHERE clause');
  assert.equal(btn.disabled, false, 'the button can be tried again');
  assert.equal(calls.length, 1, 'no reload after a failure');
});

test('undo that removes nothing says so too', async () => {
  const btn = { dataset: { key: 'k' }, disabled: false, textContent: 'Undo' };
  const { ctx } = logHarness({ deleted: [] });
  await ctx.emUndo(btn);
  assert.equal(btn.textContent, 'Undo failed: nothing was removed');
});

test('undo that works deletes exactly that decision and reloads', async () => {
  const btn = { dataset: { key: 'amazon|||aws' }, disabled: false, textContent: 'Undo' };
  const { ctx, calls } = logHarness();
  await ctx.emUndo(btn);
  assert.deepEqual(calls[0].ops.find(o => o[0] === 'eq'), ['eq', 'merge_key', 'amazon|||aws']);
  assert.ok(opNames(calls[0]).includes('select'), 'asks for the removed rows back');
  assert.equal(calls.length, 2, 'reloaded the log');
});
