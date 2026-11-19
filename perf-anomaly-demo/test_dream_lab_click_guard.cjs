// Real HTTP regression against an already completed, freshly measured run.
// No fixture results, forced alerts or duplicate database SQL execution.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require(process.env.KEENINSIGHT_PLAYWRIGHT_MODULE||'playwright');
const BASE=process.env.KEENINSIGHT_TEST_URL||'http://127.0.0.1:9108';
const RUN=process.env.KEENINSIGHT_E2E_RUN;
const OUTPUT=process.env.KEENINSIGHT_E2E_OUTPUT;
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage();
 try{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const get=async path=>{const r=await page.request.get(BASE+path);assert.ok(r.ok());return r.json();};
  const before=await get('/api/v1/lab/runs/'+RUN);
  assert.equal(before.status,'completed');assert.ok(before.result.optimized_execution_id);
  await page.goto(BASE+'/lab');
  await page.waitForFunction(()=>document.querySelector('#sqlCatalog option[value="tpcds_q31"]'));
  await page.selectOption('#sqlCatalog','tpcds_q31');
  await page.waitForFunction(()=>document.getElementById('sqlText').textContent.includes('WITH ss'));
  let starts=0,replays=0;
  page.on('request',r=>{if(r.method()==='POST'&&r.url().endsWith('/lab/dream/start'))starts++;if(r.method()==='POST'&&r.url().endsWith('/lab/dream/replay'))replays++;});
  const startResponse=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/lab/dream/start'));
  await page.evaluate(()=>{startDream();startDream();});
  assert.equal((await(await startResponse).json()).status,'already_completed');
  const replayResponse=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/lab/dream/replay'));
  await page.evaluate(()=>{replayDream();replayDream();});
  assert.equal((await(await replayResponse).json()).status,'already_completed');
  await page.waitForTimeout(500);
  assert.equal(starts,1);assert.equal(replays,1);assert.deepEqual(errors,[]);
  const after=await get('/api/v1/lab/runs/'+RUN);
  assert.equal(after.executions.length,before.executions.length);
  assert.equal(after.executions.filter(row=>row.phase==='baseline').length,1);
  assert.equal(after.executions.filter(row=>row.phase==='optimized').length,1);
  const detail=await get('/api/v1/keeninsight/sql/'+after.result.baseline_execution_id);
  assert.equal(detail.job.id,after.result.job_id);
  const summary=(await get('/api/v1/keeninsight/overview')).recentRuns.find(row=>row.run_id===RUN);
  assert.equal(summary.comparison.optimized_ms,after.result.comparison.optimized_ms);
  const report={passed:true,verifiedAt:new Date().toISOString(),runId:RUN,jobId:after.result.job_id,
    doubleInvocationRequests:{start:starts,replay:replays},executionCountUnchanged:true,
    actualExecutions:after.executions,comparison:after.result.comparison,
    portal:{job:detail.job,diagnosisTracePresent:!!detail.diagnosisTrace,lifecycle:detail.executionHistory},summary,jsErrors:errors};
  fs.writeFileSync(OUTPUT,JSON.stringify(report,null,2));
  console.log('PASS real frontend duplicate calls: one POST each, no additional SQL; baseline and optimized exactly once; portal and summary match');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
