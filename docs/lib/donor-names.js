/** Shared display spelling. Identity and adopted mixed-case labels stay intact. */
"use strict";
const DN = (() => {
  let aliases = new Map(), pending;
  const key = value => String(value || '').trim().replace(/\s+/g, ' ').toLowerCase();
  function load() {
    if (!pending) pending = (async () => {
      const sb = await getSupabase(), found = new Map();
      for (let start = 0; ; start += 1000) {
        const {data,error} = await sb.from('donor_display_aliases').select('alias,display_name').order('alias').range(start,start+999);
        if (error) throw new Error(`Could not load adopted donor spellings: ${error.message}`);
        for (const row of data) found.set(row.alias,row.display_name);
        if (data.length < 1000) break;
      }
      aliases = found;
    })().catch(error => { pending = null; throw error; });
    return pending;
  }
  function display(value) {
    let name = String(value || "").trim().replace(/\s+/g, " ");
    const seen = new Set();
    while (aliases.has(key(name)) && !seen.has(key(name))) {
      seen.add(key(name)); name = aliases.get(key(name));
    }
    if (name.toLowerCase() === "miscellaneous cash contributions $100 and under")
      return "Miscellaneous Cash Contributions $100 and under";
    if (!seen.size && (name === name.toUpperCase() || name === name.toLowerCase())) {
      const acronyms = new Set(['PAC','LLC','USA','IBM','SEIU','AFSCME','AFT','UFCW','OBRC','NW']);
      name = name.replace(/[A-Za-z]+/g, w => acronyms.has(w.toUpperCase()) ? w.toUpperCase() : w[0].toUpperCase() + w.slice(1).toLowerCase());
    }
    // Correct known words without title-casing brands, personal names or adopted aliases.
    return name.replace(/\bat\s*&\s*t\b/gi, "AT&T")
      .replace(/\b(pac|llc|usa|ibm|seiu|afscme|aft|ufcw|obrc)\b/gi, w => w.toUpperCase())
      .replace(/cooperative/gi, "Cooperative");
  }
  function tree(value) {
    if (Array.isArray(value)) return value.map(tree);
    if (!value || typeof value !== "object") return value;
    const out = Object.fromEntries(Object.entries(value).map(([k,v]) => [k,tree(v)]));
    if (out.name && (out.donor_id || out.donor_key || 'total' in out)) out.name = display(out.name);
    for (const key of ['display_name','donor','contributor_payee_canonical'])
      if (typeof out[key] === 'string') out[key] = display(out[key]);
    return out;
  }
  return { display, tree, load };
})();
