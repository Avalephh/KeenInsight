// Read-only proof that a new real console run is visible across the portal.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.KEENINSIGHT_PLAYWRIGHT_MODULE||'playwright');
const base=process.env.KEENINSIGHT_TEST_URL||'http://127.0.0.1:9108';
const out=process.env.KEENINSIGHT_SCREENSHOT_DIR;
const runId=process.env.KEENINSIGHT_AP_RUN_ID;
(async()=>{
  assert.ok(runId&&out);
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const report={startedAt:new Date().toISOString(),runId,errors:[]};
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1050}});
    page.on('pageerror',e=>report.errors.push(e.message));
    const get=async route=>{
      const response=await page.request.get(base+route);assert.ok(response.ok());return response.json();
    };
    const run=await get('/api/v1/lab/runs/'+runId);
    assert.equal(run.status,'completed');
    const result=run.result,jobId=result.job_id;
    assert.ok(result.optimized_execution_id,'An actual optimized replay is required');
    const logs=await get('/api/v1/keeninsight/sql?kind=execution&q='+run.scenario_id+'&limit=200');
    assert.ok(logs.items.some(r=>r.id===result.baseline_execution_id));
    assert.ok(logs.items.some(r=>r.id===result.optimized_execution_id));
    const before=await get('/api/v1/keeninsight/sql/'+result.baseline_execution_id);
    const after=await get('/api/v1/keeninsight/sql/'+result.optimized_execution_id);
    assert.equal(before.execution.run_id,runId);
    assert.equal(after.execution.run_id,runId);
    assert.equal(before.job.id,jobId);
    assert.equal(after.job.id,jobId);
    assert.equal(after.activeApplication.status,'active');
    assert.ok(after.activeApplication.hitCount>=1);
    assert.ok(after.diagnosisTrace?.attempts?.length);
    assert.ok(after.actionTrace?.raw_response);
    report.data={jobId,baselineMs:result.baseline.duration_ms,optimizedMs:result.optimized.duration_ms,
      improvementRatio:result.comparison.improvement_ratio,model:after.job.model,
      activeApplication:after.activeApplication,lifecycle:after.lifecycle,
      sqlProfile:before.sqlProfile,baselineExecutionId:result.baseline_execution_id,
      optimizedExecutionId:result.optimized_execution_id};
    await page.goto(base+'/keeninsight/sql-log.html');
    await page.waitForFunction(()=>document.getElementById('ki-updated')?.textContent.startsWith('Updated'));
    await page.locator('#ki-search').fill(run.scenario_id);
    await page.waitForFunction(id=>document.getElementById('ki-body')?.textContent.includes(id),result.baseline_execution_id,{timeout:20000});
    await page.waitForFunction(id=>document.getElementById('ki-body')?.textContent.includes(id),result.optimized_execution_id,{timeout:20000});
    await page.screenshot({path:path.join(out,'new-sql-baseline-and-replay.png')});
    await page.goto(base+'/keeninsight/sql-detail.html?id='+encodeURIComponent(result.optimized_execution_id)+'&job_id='+jobId);
    await page.waitForFunction(()=>document.getElementById('ki-updated')?.textContent.startsWith('Updated'));
    await page.locator('.ki-evidence>summary').click();
    const text=await page.locator('#ki-body').innerText();
    assert.ok(text.includes(jobId)&&text.includes(after.job.model));
    assert.equal(await page.locator('#detailContent>.middle-section>.detail-section').count(),2);
    await page.screenshot({path:path.join(out,'new-q9-sql-detail.png')});
    await page.goto(base+'/keeninsight/template.html');
    await page.waitForFunction(()=>document.getElementById('ki-updated')?.textContent.startsWith('Updated'));
    await page.waitForFunction(id=>document.getElementById('ki-body')?.textContent.includes(id),jobId,{timeout:20000});
    await page.waitForFunction(id=>document.getElementById('ki-body')?.textContent.includes(id),runId,{timeout:20000});
    await page.screenshot({path:path.join(out,'summary-new-q9.png')});
    assert.deepEqual(report.errors,[]);
    report.status='completed';
    console.log('PASS_NEW_AP_PORTAL',JSON.stringify({jobId,baselineMs:report.data.baselineMs,optimizedMs:report.data.optimizedMs,model:report.data.model}));
  }catch(error){report.status='failed';report.error=String(error);throw error;}
  finally{
    report.finishedAt=new Date().toISOString();
    fs.writeFileSync(path.join(out,'new-sql-audit.json'),JSON.stringify(report,null,2));
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exit(1);});
