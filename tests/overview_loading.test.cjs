'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../docs/app.js'),'utf8');
const code=source.slice(source.indexOf('async function loadOverview()'),source.indexOf('/**\n * Candidate / race context'));
function harness(){
 const reads=[],renders=[],elements=new Map();
 const c=vm.createContext({summaryData:null,byTypeDataGlobal:null,timelineData:null,state:{selectedFilers:[{slug:'jason_for_bend'}]},
 document:{getElementById(id){if(!elements.has(id))elements.set(id,{value:''});return elements.get(id)}},
 DL:{async getBlob(key){reads.push(key);if(key==='by_contributor_type' && c.failLabels)throw Error('label timeout');return {date_range_end:'2026-09-21'}}},
 loadFilerProfile:async slug=>({slug}),setOverviewTiles(){},loadTimeline:async()=>{},renderFilerRaceHeader(){},
 renderOverviewGlobal(){renders.push('global')},renderOverviewSingleFiler(p){renders.push(p.slug)},renderOverviewMultiFiler(){renders.push('multi')},});
 vm.runInContext(code,c);return {c,reads,renders};
}
test('candidate overview loads even if statewide label data is unavailable',async()=>{
 const {c,reads,renders}=harness();c.failLabels=true;
 await c.loadOverview();assert.deepEqual(reads,['summary','timeline']);assert.deepEqual(renders,['jason_for_bend']);
});
test('switching from candidate to statewide lazily loads missing chart data once',async()=>{
 const {c,reads,renders}=harness();await c.loadOverview();
 c.state.selectedFilers=[];await c.loadOverview();await c.loadOverview();
 assert.equal(reads.filter(k=>k==='by_contributor_type').length,1);assert.equal(renders.at(-1),'global');
});
test('multi-committee overview also avoids unused statewide chart lookups',async()=>{
 const {c,reads,renders}=harness();c.failLabels=true;c.state.selectedFilers.push({slug:'another'});
 await c.loadOverview();assert.ok(!reads.includes('by_contributor_type'));assert.equal(renders[0],'multi');
});
