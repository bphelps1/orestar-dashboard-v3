/** Saved entity merges use stored canonical identities; refresh the page to read
 * the latest decisions. Raw transaction records and reviewed links stay intact.
 */
"use strict";
const ID = (() => {
  let mapping, labels;
  const filerChecks = new Map();
  const labelKey = name => String(name || '').trim().replace(/\s+/g, ' ').toLowerCase();
  async function readAll(table) {
    const sb = await getSupabase();
    const rows = [];
    for (let start = 0; ; start += 1000) {
      const { data, error } = await sb.from(table).select('*').order(
        table === 'donor_identity_map' ? 'donor_id' : table === 'donor_identity_labels' ? 'label' : 'filer_id'
      ).range(start, start + 999);
      if (error) throw new Error(`Could not read saved donor merges (${table}): ${error.message}`);
      rows.push(...data);
      if (data.length < 1000) return rows;
    }
  }
  function loadMap() {
    if (!mapping) mapping = readAll('donor_identity_map').then(rows => new Map(rows.map(r => [r.donor_id, r])))
      .catch(error => { mapping = null; throw error; });
    return mapping;
  }
  async function hasMerges() { return (await loadMap()).size > 0; }
  async function affectsFilers(ids) {
    const scope = [...new Set(ids.filter(id => id != null && String(id).trim()).map(id => String(id).trim()))].sort();
    if (!scope.length || !await hasMerges()) return false;
    const key = JSON.stringify(scope);
    if (!filerChecks.has(key)) filerChecks.set(key, (async () => {
      const sb = await getSupabase();
      // Migration 027 maintains this indexed list on merge saves/imports.
      // Page loads never probe transaction history to detect saved merges.
      const { data, error } = await sb.from('donor_merge_filers').select('filer_id')
        .in('filer_id', scope).limit(1);
      if (error) throw new Error(`Could not check saved donor merges: ${error.message}`);
      return data.length > 0;
    })().catch(error => { filerChecks.delete(key); throw error; }));
    return filerChecks.get(key);
  }
  async function members(id) {
    const map = await loadMap();
    const canonical = map.get(id)?.canonical_id || id;
    return [...new Set([canonical, ...[...map.values()].filter(r => r.canonical_id === canonical).map(r => r.donor_id)])];
  }
  function loadLabels() {
    if (!labels) labels = readAll('donor_identity_labels').then(rows => new Map(rows.map(r => [r.label, r])))
      .catch(error => { labels = null; throw error; });
    return labels;
  }

  /** One donor table, with merged identities collapsed onto the canonical one. */
  function mergeRows(items, map, names) {
    const out = new Map();
    for (const item of items) {
      const identity = item.donor_id || item.donor_key;
      const match = identity ? map.get(identity) : names.get(labelKey(item.name));
      const key = match?.canonical_id || identity || `name:${labelKey(item.name)}`;
      const row = match ? { ...item, name: match.canonical_name, donor_id: key, donor_key: key } : { ...item };
      if (out.has(key)) out.get(key).total += Number(row.total || 0);
      else out.set(key, { ...row, total: Number(row.total || 0) });
    }
    return [...out.values()].sort((a,b) => b.total-a.total);
  }

  async function rekeyBlob(blob) {
    const map = await loadMap();
    if (!map.size) return blob;
    function needsLabels(value, key) {
      if (Array.isArray(value)) return key === 'top_donors'
        ? value.some(item => !item.donor_id && !item.donor_key)
        : value.some(item => needsLabels(item));
      return value && typeof value === 'object'
        ? Object.entries(value).some(([k,v]) => needsLabels(v,k)) : false;
    }
    const names = needsLabels(blob) ? await loadLabels() : new Map();
    function walk(value, key) {
      if (Array.isArray(value)) return key === 'top_donors' ? mergeRows(value, map, names) : value.map(v => walk(v));
      if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k,v]) => [k,walk(v,k)]));
      return value;
    }
    return walk(blob);
  }

  /**
   * A filer's per-year donor tables, with saved merges applied.
   *
   * rekeyBlob only collapses the all-time `top_donors` array: in a by-year
   * blob the rows sit under "2024", "2026", … and that walk passes them
   * through untouched, because the whole-blob path re-queries the merged
   * totals per filer instead. The chamber list reads hundreds of these blobs
   * at once and cannot afford a query each, so it merges them here — which is
   * why an organization an admin has already merged, such as Oregon Beverage
   * Recycling Cooperative under its two mailing addresses, was still ranking
   * as two donors.
   */
  async function rekeyDonorYears(byYear) {
    const map = await loadMap();
    if (!map.size || !byYear) return byYear;
    const years = Object.entries(byYear);
    const names = years.some(([, items]) => (items || []).some(i => !i.donor_id && !i.donor_key))
      ? await loadLabels() : new Map();
    return Object.fromEntries(years.map(([year, items]) =>
      [year, Array.isArray(items) ? mergeRows(items, map, names) : items]));
  }

  return { loadMap, hasMerges, affectsFilers, members, rekeyBlob, rekeyDonorYears };
})();
