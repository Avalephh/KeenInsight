// Runs actual PostgreSQL + configured LLM through the real experiment buttons.
// Temporarily unpublishes only exact original-Q31 actions, preserving history.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require(process.env.KEENINSIGHT_PLAYWRIGHT_MODULE||'playwright');
const BASE=process.env.KEENINSIGHT_TEST_URL||'http://127.0.0.1:9108';
const OUTPUT=process.env.KEENINSIGHT_E2E_OUTPUT;
if(!OUTPUT)throw new Error('KEENINSIGHT_E2E_OUTPUT is required');
const audit={startedAt:new Date().toISOString(),scope:'real browser buttons, fresh LLM, single baseline and single automatic replay'};
const save=()=>fs.writeFileSync(OUTPUT,JSON.stringify(audit,null,2));

(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1600,height:1050}});
 const jsErrors=[];page.on('pageerror',e=>jsErrors.push(e.message));
 const get=async path=>{const r=await page.request.get(BASE+path);assert.ok(r.ok(),path);return r.json();};
 const post=async(path,body={})=>{const r=await page.request.post(BASE+path,{data:body});assert.ok(r.ok(),await r.text());return r.json();};
 const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
 let previous=[];
 try{
  const observations=await get('/api/v1/keeninsight/sql?kind=observation&q=lab-tpcds_q31&limit=50');
  const original=observations.items.find(row=>row.sqlKey.includes(':lab-tpcds_q31:'));
  assert.ok(original,'preserved exact original Q31 fingerprint');
  const detail=await get('/api/v1/sql/'+encodeURIComponent(original.sqlKey));
  const listed=(await get('/api/v1/improvements?limit=200')).improvements||[];
  const improvements=await Promise.all(listed.filter(row=>row.status==='active').map(row=>get('/api/v1/improvements/'+row.improvement_id)));
  // Query the published actions through their own existing IDs, not invented
  // settings; only the original exact Q31 literal identity is selected.
  previous=improvements.filter(row=>row.status==='active'&&row.canonical_sql===detail.canonical_sql);
  audit.previousActionIds=previous.map(row=>row.improvement_id);save();
  for(const row of previous)await post('/api/v1/actions/improvements/'+row.improvement_id+'/rollback');
  await page.goto(BASE+'/lab');
  await page.waitForFunction(()=>document.querySelector('#sqlCatalog option[value="tpcds_q31"]'));
  await page.selectOption('#sqlCatalog','tpcds_q31');
  await page.waitForFunction(()=>document.getElementById('sqlText').textContent.includes('WITH ss'));
  await page.waitForFunction(()=>!document.getElementById('startDream').disabled,null,{timeout:15000});
  let starts=0,replays=0;
  page.on('request',r=>{if(r.method()==='POST'&&r.url().endsWith('/lab/dream/start'))starts++;if(r.method()==='POST'&&r.url().endsWith('/lab/dream/replay'))replays++;});
  const response=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/lab/dream/start'));
  await page.evaluate(()=>{document.getElementById('startDream').click();document.getElementById('startDream').click();});
  const started=await(await response).json();
  assert.equal(started.status,'queued');audit.runId=started.run.run_id;save();
  console.log('REAL BROWSER START',audit.runId,'POSTs',starts);
  let run,deadline=Date.now()+1200000,nextLog=0;
  while(Date.now()<deadline){
   run=await get('/api/v1/lab/runs/'+audit.runId);
   if(Date.now()>=nextLog){console.log('REAL BROWSER',run.phase,run.status);nextLog=Date.now()+20000;}
   if(!['queued','running'].includes(run.status))break;
   await sleep(2000);
  }
  assert.equal(run.status,'completed');audit.jobId=run.result.job_id;
  const job=await get('/api/v1/jobs/'+audit.jobId);audit.analysis=job;
  assert.equal(job.status,'completed');
  assert.equal(job.result.bridge_publication.status,'active','fresh candidate must be active, not a prior action');
  await page.waitForFunction(()=>!document.getElementById('replayDream').disabled,null,{timeout:15000});
  const replayResponse=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/lab/dream/replay'));
  await page.evaluate(()=>{document.getElementById('replayDream').click();document.getElementById('replayDream').click();});
  assert.equal((await(await replayResponse).json()).status,'queued');
  deadline=Date.now()+240000;
  while(Date.now()<deadline){
   run=await get('/api/v1/lab/runs/'+audit.runId);
   if(!['queued','running'].includes(run.status))break;
   await sleep(1000);
  }
  assert.equal(run.status,'completed');assert.ok(run.result.optimized_execution_id);
  assert.equal(starts,1);assert.equal(replays,1);
  assert.equal(run.executions.filter(row=>row.phase==='baseline').length,1);
  assert.equal(run.executions.filter(row=>row.phase==='optimized').length,1);
  audit.comparison=run.result.comparison;
  audit.executionIds=run.executions.map(row=>row.execution_id);
  audit.portal=await get('/api/v1/keeninsight/sql/'+run.result.baseline_execution_id);
  assert.equal(audit.portal.job.id,audit.jobId);
  assert.equal(audit.portal.tuning_results.status,'active');
  assert.ok(audit.portal.diagnosisTrace);
  assert.ok(audit.portal.executionHistory.some(row=>row.executionId===run.result.optimized_execution_id));
  const overview=await get('/api/v1/keeninsight/overview');
  const summary=overview.recentRuns.find(row=>row.run_id===audit.runId);
  assert.ok(summary);assert.equal(summary.comparison.optimized_ms,run.result.comparison.optimized_ms);
  audit.summary=summary;audit.requestCounts={start:starts,replay:replays};
  audit.jsErrors=jsErrors;assert.deepEqual(jsErrors,[]);
  audit.finishedAt=new Date().toISOString();audit.passed=true;save();
  console.log('REAL BROWSER PASS',JSON.stringify({run:audit.runId,comparison:audit.comparison,requests:audit.requestCounts}));
 }catch(error){
  audit.error=String(error);audit.passed=false;save();
  for(const row of previous){try{await post('/api/v1/actions/improvements/'+row.improvement_id+'/activate');}catch{}}
  throw error;
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
