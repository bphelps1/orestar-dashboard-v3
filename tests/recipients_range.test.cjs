'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../docs/app.js'),'utf8');
// The Recipients section, plus the cycle dates the Cycle buttons use.
const code=source.slice(source.indexOf('// ── Recipients ──'),source.indexOf('// ── Timeline ──'))
 +'\n'+source.match(/function cycleRange\(electionYear\) \{[\s\S]*?\n\}\n/)[0]
 +'\n'+source.match(/function donorFilerIds\(profile, entry\) \{[\s\S]*?\n\}\n/)[0];
const plain=v=>JSON.parse(JSON.stringify(v));

function harness(){
 const els=new Map(),charts=[],tables=[],calls=[];
 const get=id=>{if(!els.has(id))els.set(id,{id,textContent:'',hidden:false,value:'',insertAdjacentHTML(){},addEventListener(){}});return els.get(id)};
 const c=vm.createContext({console,state:{selectedFilers:[],dateStart:'',dateEnd:''},recipientsData:null,filerIndex:[],
  document:{getElementById:get},fmt$:v=>`$${v}`,
  makeBarChart:(id,labels,values)=>charts.push({id,labels,values}),
  buildSortableTable:(id,rows,cols)=>tables.push({id,rows,cols}),
  loadFilerProfile:async slug=>({name:'Friends of Test',slug,filer_ids:['11','12'],top_payees:[{name:'All-time payee',total:9}]}),
  DL:{
   getBlob:async key=>{calls.push(['blob',key]);return {all_time:[{name:'All-time PAC',total:100}],by_year:{2022:[{name:'Governor 2022',total:50}]}}},
   getRecipients:async args=>{calls.push(['recipients',args]);return c.recipientRows(args)},
   getPayees:async args=>{calls.push(['payees',args]);return [{name:'Canal Partners Media',total:2316402}]},
  },
  recipientRows:()=>[{name:'Defeat the Costly Tax on Sales',total:16526149.4},{name:'Bring Balance to Salem PAC',total:6121523.56}],
 });
 vm.runInContext(code,c);
 return {c,get,charts,tables,calls};
}

test('a cycle button ranks recipients for exactly its dates, not whole calendar years',async()=>{
 const {c,get,charts,tables,calls}=harness();
 Object.assign(c.state,{dateStart:'2022-12-01',dateEnd:'2024-11-30'});
 await c.loadRecipients();
 assert.deepEqual(plain(calls),[['recipients',{start:'2022-12-01',end:'2024-11-30'}]],'the database, not the calendar-year blob');
 assert.deepEqual(plain(charts.at(-1).labels),['Defeat the Costly Tax on Sales','Bring Balance to Salem PAC']);
 assert.equal(tables.at(-1).rows[0].total,16526149.4);
 assert.equal(get('recipients-table-title').textContent,'Top 100 Recipients, 2024 cycle');
 assert.equal(get('recipients-chart-title').textContent,'Top 20 Recipients (by Contributions Received), 2024 cycle');
 assert.equal(get('recipient-year-group').hidden,true,'the calendar-year selector steps aside');
});

test('with no dates the calendar-year blob still drives the tab and its year selector',async()=>{
 const {c,get,tables,calls}=harness();
 await c.loadRecipients();
 assert.deepEqual(plain(calls),[['blob','top_recipients']]);
 assert.equal(tables.at(-1).rows[0].name,'All-time PAC');
 assert.equal(get('recipients-table-title').textContent,'Top 100 Recipients');
 assert.equal(get('recipient-year-group').hidden,false);
});

test('a slower answer for an earlier click does not overwrite the newer cycle',async()=>{
 const {c,get,tables}=harness();
 const pending=new Map();
 c.recipientRows=args=>new Promise(r=>pending.set(args.start,r));
 Object.assign(c.state,{dateStart:'2024-12-01',dateEnd:'2026-11-30'});
 const first=c.loadRecipients();
 Object.assign(c.state,{dateStart:'2022-12-01',dateEnd:'2024-11-30'});
 const second=c.loadRecipients();
 await null;
 pending.get('2022-12-01')([{name:'2024 cycle leader',total:2}]);await second;
 pending.get('2024-12-01')([{name:'2026 cycle leader',total:1}]);await first;
 assert.equal(tables.length,1,'only the current request renders');
 assert.equal(tables[0].rows[0].name,'2024 cycle leader');
 assert.equal(get('recipients-table-title').textContent,'Top 100 Recipients, 2024 cycle');
});

test("a committee's payees use its filer IDs and the exact dates too",async()=>{
 const {c,get,tables,calls}=harness();
 c.state.selectedFilers=[{slug:'friends_of_test'}];
 Object.assign(c.state,{dateStart:'2024-12-01',dateEnd:'2026-11-30'});
 await c.loadRecipients();
 assert.deepEqual(plain(calls),[['payees',{filerIds:['11','12'],start:'2024-12-01',end:'2026-11-30'}]]);
 assert.equal(tables.at(-1).rows[0].name,'Canal Partners Media');
 assert.equal(get('recipients-table-title').textContent,'Top Spending by Friends of Test, 2026 cycle');
 // Without dates, the profile's all-time list, as before.
 Object.assign(c.state,{dateStart:'',dateEnd:''});calls.length=0;
 await c.loadRecipients();
 assert.equal(calls.length,0);assert.equal(tables.at(-1).rows[0].name,'All-time payee');
});

test('custom dates are named as dates, and a failed lookup says so instead of showing stale rows',async()=>{
 const {c,get,tables}=harness();
 Object.assign(c.state,{dateStart:'2025-03-01',dateEnd:'2025-06-30'});
 assert.equal(c.rangeCaption(),'Mar 1, 2025 – Jun 30, 2025');
 Object.assign(c.state,{dateStart:'2025-03-01',dateEnd:''});
 assert.equal(c.rangeCaption(),'since Mar 1, 2025');
 Object.assign(c.state,{dateStart:'2025-03-01',dateEnd:'2025-06-30'});
 c.recipientRows=async()=>{throw new Error('canceling statement due to statement timeout')};
 await c.loadRecipients();
 assert.match(get('recipients-table-title').textContent,/^Could not load Mar 1, 2025 – Jun 30, 2025: canceling statement/);
 assert.equal(tables.at(-1).rows.length,0,'the previous rows are cleared');
});
