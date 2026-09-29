const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function harness(){
 const elements=new Map();
 const document={addEventListener(){},getElementById(id){if(!elements.has(id))elements.set(id,{value:'',textContent:'',innerHTML:'',hidden:false,querySelectorAll:()=>[]});return elements.get(id);}};
 const c=vm.createContext({document,console,setTimeout,clearTimeout});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../docs/explore.js'),'utf8'),c);
 return {c,elements,document};
}
test('browse restores both source names with one bounded primary-key lookup',async()=>{
 const {c}=harness();let calls=0;
 const sb={from(table){assert.equal(table,'transactions');return {select(cols){assert.match(cols,/filer_id/);return {async in(key,ids){calls++;assert.equal(key,'tran_id');assert.deepEqual(Array.from(ids),[4957470]);return {data:[{tran_id:4957470,filer:'Elect Zach Hudson',filer_id:'17995',contributor_payee:'Oregon Trial Lawyers Association PAC (39)',donor_id:'c39'}]};}};}};}};
 const result=await c.completeTransactionNames(sb,[{tran_id:4957470,filer_canonical:null,contributor_payee_canonical:null,amount:1000},{tran_id:2,filer_canonical:'Other committee',contributor_payee_canonical:'Other donor'}],{donor_id:'c39',display_name:'Oregon Trial Lawyers Association PAC'});
 assert.equal(calls,1);assert.equal(result[0].filer_canonical,'Elect Zach Hudson');assert.equal(result[0].contributor_payee_canonical,'Oregon Trial Lawyers Association PAC');assert.equal(result[0].amount,1000);
 assert.equal(result[0].contributor_payee,'Oregon Trial Lawyers Association PAC (39)');assert.equal(result[1].filer_canonical,'Other committee');
});
test('complete rows require no source query; whitespace labels fall back safely and preserve CSV source fields',async()=>{
 const {c}=harness();
 await c.completeTransactionNames({from(){throw Error('unnecessary query');}},[{filer_canonical:'Committee',contributor_payee_canonical:'Donor'}]);
 const row=c.transactionNames({filer_canonical:'  ',filer:'Source Committee',contributor_payee_canonical:'\t',contributor_payee:'Original Donor',donor_id:'other'},{donor_id:'c39',display_name:'Wrong Donor'});
 assert.equal(row.filer_canonical,'Source Committee');assert.equal(row.contributor_payee_canonical,'Original Donor');assert.equal(row.contributor_payee,'Original Donor');
 assert.equal(c.transactionNames({filer_id:'123'}).filer_canonical,'Committee 123');assert.equal(c.transactionNames({}).contributor_payee_canonical,'Not reported');
});
test('adopted spelling applies to fallback names and recovered HTML is escaped',()=>{
 const {c,document}=harness();c.DN={display:s=>s==='At&T'?'AT&T':s};
 const row=c.transactionNames({filer:'<script>bad</script>',contributor_payee:'At&T'});
 assert.equal(row.contributor_payee_canonical,'AT&T');c.renderTable([row]);
 assert.match(document.getElementById('xp-tbody').innerHTML,/&lt;script&gt;/);assert.doesNotMatch(document.getElementById('xp-tbody').innerHTML,/<script>/);
});
test('source-name lookup errors are surfaced rather than silently showing blank cells',async()=>{
 const {c}=harness();const sb={from:()=>({select:()=>({in:async()=>({error:{message:'failed'}})})})};
 await assert.rejects(c.completeTransactionNames(sb,[{tran_id:1}]),/Could not load transaction names: failed/);
});
test('filtered CSV fills display columns while retaining original transaction names',async()=>{
 const {c,document}=harness();let downloaded;
 const source={tran_id:1,filer:'Source Committee',filer_canonical:null,contributor_payee:'Original PAC (39)',contributor_payee_canonical:null,donor_id:'c39',amount:500};
 c.getSupabase=async()=>({from:()=>({select:()=>({order:()=>({range:async()=>({data:[source]})})})})});
 c.triggerDownload=(csv,mime,name)=>{downloaded={csv,mime,name};};
 await c.downloadFiltered();
 assert.match(downloaded.csv,/Source Committee,Source Committee,Original PAC \(39\),Original PAC \(39\)/);
 assert.equal(downloaded.name,'orestar_filtered.csv');assert.equal(source.filer_canonical,null);
 assert.equal(document.getElementById('xp-status').textContent,'Downloaded 1 rows.');
});
test('CSV matches recorded and canonical names together, safely quoting punctuation',()=>{
 const {c}=harness();const calls=[];const q={or(v){calls.push(['or',v]);return this;},in(k,v){calls.push(['in',k,v]);return this;}};
 const base={amtMin:'',amtMax:''};
 c.applyFilters(q,{...base,filer:'Daniel Nguyen',payee:'Acme, "Inc." (PAC)'});
 assert.equal(calls.length,1);assert.equal(calls[0][0],'or');
 assert.match(calls[0][1],/and\(or\(filer_canonical/);assert.match(calls[0][1],/contributor_payee\.ilike/);assert.ok(calls[0][1].includes('\\"Inc.\\"'));
 calls.length=0;c.applyFilters(q,{...base,donorId:'a',donorIds:['a','b'],payee:'ignored'});
 assert.deepEqual(calls,[['in','donor_id',['a','b']]]);
});
test('selected donor CSV resolves saved merge members before reading transactions',async()=>{
 const {c}=harness();let used;
 vm.runInContext('selectedDonor={donor_id:"a",display_name:"Merged PAC"}',c);
 const query={select(){return this;},in(key,ids){used={key,ids};return this;},order(){return this;},range:async()=>({data:[]})};
 c.getSupabase=async()=>({rpc:async(name,params)=>{assert.equal(name,'donor_group_ids');assert.equal(params.p_donor_id,'a');return {data:['a','b']};},from:()=>query});
 await c.downloadFiltered();assert.equal(used.key,'donor_id');assert.deepEqual(Array.from(used.ids),['a','b']);
});
test('type menu splits into a type code and an ORESTAR sub-type for both browse and CSV',()=>{
 const {c,elements,document}=harness();const calls=[];const q={eq(k,v){calls.push([k,v]);return this;}};
 document.getElementById('f-type').value='C|In-Kind Contribution';
 let f=c.readFilters();assert.equal(f.type,'C');assert.equal(f.subType,'In-Kind Contribution');
 c.applyFilters(q,{...f,amtMin:'',amtMax:''});
 assert.deepEqual(calls,[['tran_type','C'],['sub_type','In-Kind Contribution']]);
 elements.get('f-type').value='E';f=c.readFilters();assert.equal(f.type,'E');assert.equal(f.subType,'');
 elements.get('f-type').value='';f=c.readFilters();assert.equal(f.type,'');assert.equal(f.subType,'');
});
test('browse asks explore_transactions for the chosen sub-type',async()=>{
 const {c,elements}=harness();let call;
 c.DN={load:async()=>{},display:s=>s};
 c.getSupabase=async()=>({rpc:async(name,params)=>{call={name,params};return {data:[]};}});
 elements.set('f-type',{value:'OR|Lost or Returned Check',textContent:'',innerHTML:'',hidden:false,querySelectorAll:()=>[]});
 await c.runSearch();
 assert.equal(call.name,'explore_transactions');
 assert.equal(call.params.p_tran_type,'OR');assert.equal(call.params.p_sub_type,'Lost or Returned Check');
 assert.equal(elements.get('xp-status').textContent,'No matching transactions');
});
test('rows are labelled by sub-type, falling back to the type code when none is recorded',()=>{
 const {c,document}=harness();c.DN={display:s=>s};
 c.renderTable([{tran_type:'C',sub_type:'In-Kind Contribution',amount:500},{tran_type:'OR',sub_type:null,amount:5}]);
 const body=document.getElementById('xp-tbody').innerHTML;
 assert.match(body,/<td class="">In-Kind Contribution<\/td>/);assert.match(body,/<td class="">OR<\/td>/);
 assert.doesNotMatch(body,/<td class="">C<\/td>/);
 assert.match(document.getElementById('xp-thead').innerHTML,/data-col="sub_type">Type/);
});
