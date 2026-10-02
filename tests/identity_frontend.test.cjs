'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname,'..');
const read = file => fs.readFileSync(path.join(root,file),'utf8');
const plain = value => JSON.parse(JSON.stringify(value));
function identityHarness(overrides = {}) {
 const tables = {
  donor_identity_map: [{donor_id:'a',canonical_id:'a',canonical_name:'Acme'}, {donor_id:'b',canonical_id:'a',canonical_name:'Acme'}],
  donor_identity_labels: [{label:'old acme',canonical_id:'a',canonical_name:'Acme'}],
  donor_merge_filers: [{filer_id:'1'}], ...overrides,
 };
 const reads=[];
 const ctx=vm.createContext({getSupabase:async()=>({from:table=>({select(){return this;},order(){return this;},in(col,values){(this.filters ||= {})[col]=values;return this;},async limit(n){reads.push({table,filters:this.filters,limit:n});return {data:tables[table].filter(r=>Object.entries(this.filters).every(([col,values])=>values.includes(r[col]))).slice(0,n)};},async range(start,end){reads.push(table);return {data:tables[table].slice(start,end+1)};}})})});
 vm.runInContext(read('docs/lib/identity.js')+'\nthis.identity=ID;',ctx);
 return {ctx,id:ctx.identity,reads};
}
test('identity reads include every member and preserve unrelated IDs', async()=>{
 const {id,reads}=identityHarness();
 assert.deepEqual(plain(await id.members('b')),['a','b']);
 assert.deepEqual(plain(await id.members('x')),['x']);
 assert.equal(reads.filter(t=>t==='donor_identity_map').length,1);
 assert.equal(await id.affectsFilers(['1']),true);
 assert.equal(await id.affectsFilers(['2']),false);
});
test('name-only tooltip caches combine only labels the database found unambiguous',async()=>{
 const {id}=identityHarness();
 const input={monthly:{top_donors:[{name:'Old Acme',total:200},{name:'Acme',donor_id:'a',total:100},{name:'Unrelated',donor_id:'x',total:50}]}};
 const output=await id.rekeyBlob(input);
 assert.deepEqual(plain(output.monthly.top_donors),[
  {name:'Acme',donor_id:'a',donor_key:'a',total:300},
  {name:'Unrelated',donor_id:'x',total:50},
 ]);
 assert.equal(input.monthly.top_donors.length,3);
});
test('pagination does not silently lose merge members after the API row cap',async()=>{
 const rows=Array.from({length:1001},(_,i)=>({donor_id:String(i),canonical_id:'0',canonical_name:'Group'}));
 const {id,reads}=identityHarness({donor_identity_map:rows});
 assert.equal((await id.members('1000')).length,1001);
 assert.equal(reads.length,2);
});
test('affected cached profiles and global rankings use freshly grouped data',async()=>{
 const calls=[];
 const data={all_time:[{name:'Acme',donor_id:'a',donor_key:'a',total:600}],by_year:{2026:[]}};
 const old={name:'Candidate',filer_ids:['1'],top_donors:[{name:'Acme',total:100}]};
 const ctx=vm.createContext({ID:{hasMerges:async()=>true,affectsFilers:async ids=>ids.includes('1')},
  getSupabase:async()=>({from:()=>({select(){return this;},eq(){return this;},async single(){return {data:{detail:old,filer_id:'1'}};}}),
   rpc:async(name,params)=>{calls.push({name,params});return {data};}})});
 vm.runInContext(read('docs/lib/data.js')+'\nthis.data=DL;',ctx);
 const profile=await ctx.data.getFilerDetail('candidate');
 assert.equal(profile.top_donors[0].total,600);
 assert.equal(old.top_donors[0].total,100);
 assert.deepEqual(plain(calls.map(c=>c.params.p_filer_ids)),[['1']]);
});
test('the statewide donor table is read as stored, with newer merges folded in',async()=>{
 const stored={all_time:[{name:'Acme',donor_id:'a',donor_key:'a',total:600},{name:'Old Acme',donor_id:'b',donor_key:'b',total:50},
   {name:'Unrelated',donor_id:'x',donor_key:'x',total:400}],by_year:{2026:[{name:'Old Acme',donor_id:'b',donor_key:'b',total:50},{name:'Acme',donor_id:'a',donor_key:'a',total:20}]}};
 const merges=[{donor_id:'b',canonical_id:'a',canonical_name:'Acme'}];
 const ctx=vm.createContext({getSupabase:async()=>({from:table=>({select(){return this;},eq(){return this;},order(){return this;},
   async range(){return {data:table==='donor_identity_map'?merges:[]};},
   async single(){assert.equal(table,'dashboard_cache');return {data:{data:stored}};}}),
   rpc:async()=>{throw Error('statewide ranking must not be recomputed');}})});
 vm.runInContext(read('docs/lib/identity.js')+'\n'+read('docs/lib/data.js')+'\nthis.data=DL;',ctx);
 const table=await ctx.data.getBlob('top_donors');
 assert.deepEqual(plain(table.all_time.map(r=>[r.donor_id,r.total])),[['a',650],['x',400]]);
 assert.deepEqual(plain(table.by_year[2026].map(r=>[r.donor_id,r.total])),[['a',70]]);
});
test('an unaffected profile keeps its cache rather than re-querying donor history',async()=>{
 const old={name:'Other',filer_ids:['2'],top_donors:[]};
 const ctx=vm.createContext({ID:{affectsFilers:async()=>false},getSupabase:async()=>({from:()=>({select(){return this;},eq(){return this;},async single(){return {data:{detail:old}};}}),rpc:async()=>{throw Error('unexpected query');}})});
 vm.runInContext(read('docs/lib/data.js')+'\nthis.data=DL;',ctx);
 assert.equal(await ctx.data.getFilerDetail('other'),old);
});
test('merge detection reads only the requested scope and shares concurrent checks',async()=>{
 const {id,reads}=identityHarness({donor_merge_filers:Array.from({length:7000},(_,i)=>({filer_id:String(i)}))});
 assert.deepEqual(await Promise.all([id.affectsFilers(['18661','1']),id.affectsFilers(['1','18661'])]),[true,true]);
 assert.equal(reads.filter(r=>r==='donor_merge_filers').length,0);
 const scoped=reads.filter(r=>r.table==='donor_merge_filers');
 assert.equal(reads.filter(r=>r.table==='transactions').length,0);
 assert.equal(scoped.length,1);assert.deepEqual(plain(scoped[0].filters.filer_id),['1','18661']);assert.equal(scoped[0].limit,1);
 assert.equal(await id.affectsFilers([]),false);
});

 test('stored filer lookup does not query transaction history, regardless of merge count',async()=>{
 const rows=Array.from({length:205},(_,i)=>({donor_id:String(i),canonical_id:'0'}));
 const {id,reads}=identityHarness({donor_identity_map:rows,donor_merge_filers:[{filer_id:'1'}]});
 assert.equal(await id.affectsFilers([' 1 ']),true);
 assert.equal(await id.affectsFilers(['2']),false);
 assert.equal(reads.filter(r=>r.table==='donor_merge_filers').length,2);
 assert.equal(reads.filter(r=>r.table==='transactions').length,0);
 });
 test('failed merge probes are retried and never cached as unaffected',async()=>{
 let attempts=0;
 const ctx=vm.createContext({getSupabase:async()=>({from:table=>({select(){return this},order(){return this},in(){return this},range:async()=>({data:[{donor_id:'a',canonical_id:'a'}]}),limit:async()=>++attempts===1?{error:{message:'timeout'}}:{data:[{filer_id:'1'}]}})})});
 vm.runInContext(read('docs/lib/identity.js')+'\nthis.identity=ID;',ctx);
 await assert.rejects(ctx.identity.affectsFilers(['1']),/timeout/);
 assert.equal(await ctx.identity.affectsFilers(['1']),true);assert.equal(attempts,2);
 });

test('ID-bearing chart donors do not load name-only identity labels',async()=>{
 const {id,reads}=identityHarness();
 const out=await id.rekeyBlob({by_year:{2026:[{top_donors:[{donor_id:'b',name:'Old Acme',total:100},{donor_id:'a',name:'Acme',total:200}]}]}});
 assert.equal(out.by_year[2026][0].top_donors[0].total,300);
 assert.equal(reads.includes('donor_identity_labels'),false);
});
test('empty chart data does not load label identities',async()=>{
 const {id,reads}=identityHarness();
 await id.rekeyBlob({by_year:{},top_donors:[]});
 assert.equal(reads.includes('donor_identity_labels'),false);
});

// A chamber's donor history is read straight out of the by-year tables, where
// rekeyBlob leaves the rows alone — so the merges have to be applied there.
test('per-year donor tables collapse merged identities onto the canonical one',async()=>{
 const {id,reads}=identityHarness();
 const out=await id.rekeyDonorYears({
  2024:[{donor_id:'b',name:'Acme of Clackamas',total:1000},{donor_id:'x',name:'Other',total:4000}],
  2026:[{donor_id:'b',name:'Acme of Clackamas',total:500},{donor_id:'a',name:'Acme',total:250}],
 });
 // One row per identity per year, summed, under the canonical name, still by total.
 assert.deepEqual(plain(out[2024]),[
  {donor_id:'x',name:'Other',total:4000},
  {donor_id:'a',donor_key:'a',name:'Acme',total:1000},
 ]);
 assert.deepEqual(plain(out[2026]),[{donor_id:'a',donor_key:'a',name:'Acme',total:750}]);
 assert.equal(reads.includes('donor_identity_labels'),false,'every row carries an id');
});
test('per-year tables with no merges saved are handed back untouched',async()=>{
 const {id}=identityHarness({donor_identity_map:[]});
 const byYear={2026:[{donor_id:'b',name:'Acme of Clackamas',total:500}]};
 assert.equal(await id.rekeyDonorYears(byYear),byYear);
});
test('a chamber of donor history is merged as it is read',async()=>{
 const blobs={fahey:{2026:[{donor_id:'a',name:'Acme',total:1000},{donor_id:'b',name:'Acme of Clackamas',total:500}]}};
 const ctx=vm.createContext({ID:{hasMerges:async()=>true,
  rekeyDonorYears:async byYear=>Object.fromEntries(Object.entries(byYear).map(([y,rows])=>
   [y,[{donor_id:'a',name:'Acme',total:rows.reduce((t,r)=>t+r.total,0)}]]))},
  getSupabase:async()=>({from:()=>({select(){return this;},in:async(col,slugs)=>
   ({data:slugs.map(slug=>({slug,top_donors_by_year:blobs[slug]}))})})})});
 vm.runInContext(read('docs/lib/data.js')+'\nthis.data=DL;',ctx);
 const out=await ctx.data.getFilerDonorYears(['fahey']);
 assert.deepEqual(plain(out.get('fahey')[2026]),[{donor_id:'a',name:'Acme',total:1500}]);
});
test('a database with no merges saved does not rewrite the chamber blobs',async()=>{
 const byYear={2026:[{donor_id:'b',name:'Acme of Clackamas',total:500}]};
 const ctx=vm.createContext({ID:{hasMerges:async()=>false,
  rekeyDonorYears:async()=>{throw Error('unexpected re-key');}},
  getSupabase:async()=>({from:()=>({select(){return this;},in:async()=>
   ({data:[{slug:'fahey',top_donors_by_year:byYear}]})})})});
 vm.runInContext(read('docs/lib/data.js')+'\nthis.data=DL;',ctx);
 assert.equal((await ctx.data.getFilerDonorYears(['fahey'])).get('fahey'),byYear);
});
