// Real TP console request + open-page automatic refresh verification.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.KEENINSIGHT_PLAYWRIGHT_MODULE||'playwright');
const base=process.env.KEENINSIGHT_TEST_URL||'http://127.0.0.1:9108';
const output=process.env.KEENINSIGHT_SCREENSHOT_DIR;
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
  assert.ok(output);fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const report={startedAt:new Date().toISOString(),checks:[],errors:[]};
  try{
    const context=await browser.newContext({viewport:{width:1440,height:1050}});
    const summary=await context.newPage(),alerts=await context.newPage();
    for(const page of [summary,alerts])page.on('pageerror',e=>report.errors.push(e.message));
    const get=async url=>{
      const response=await context.request.get(base+url);assert.ok(response.ok(),url);
      return response.json();
    };
    report.before=await get('/api/v1/keeninsight/overview');
    const old=new Set((await get('/api/v1/keeninsight/alerts?limit=200')).items.map(r=>r.id));
    await summary.goto(base+'/keeninsight/template.html');
    await alerts.goto(base+'/keeninsight/alert-list.html');
    for(const page of [summary,alerts])await page.waitForFunction(()=>document.getElementById('ki-updated')?.textContent.startsWith('Updated'));
    const request={scenario_id:'tp_stock_level_hot',normal_clients:2,pressure_clients:5,baseline_seconds:45,pressure_seconds:120};
    const response=await context.request.post(base+'/api/v1/actions/lab/tpcc/start',{data:request});
    assert.ok(response.ok(),await response.text());
    const start=await response.json();report.runId=start.run.run_id;report.request=request;
    fs.writeFileSync(path.join(output,'live-write-audit.json'),JSON.stringify(report,null,2));
    console.log('REAL_TP_STARTED',report.runId);
    let newAlert,run,deadline=Date.now()+12*60*1000;
    while(Date.now()<deadline){
      run=await get('/api/v1/lab/runs/'+report.runId);
      const records=(await get('/api/v1/keeninsight/alerts?limit=200')).items;
      newAlert=newAlert||records.find(r=>!old.has(r.id));
      if(newAlert&&!report.alertAppearedWithoutReload){
        await alerts.waitForFunction(id=>document.getElementById('ki-body')?.textContent.includes(id),newAlert.id,{timeout:25000});
        report.alertAppearedWithoutReload={id:newAlert.id,at:new Date().toISOString()};
        await alerts.screenshot({path:path.join(output,'new-alert-auto-refresh.png')});
        console.log('NEW_ALERT_VISIBLE_WITHOUT_RELOAD',newAlert.id);
      }
      if(newAlert&&!report.experimentAppearedWithoutReload){
        await summary.waitForFunction(id=>document.getElementById('ki-body')?.textContent.includes(id),report.runId,{timeout:25000});
        report.experimentAppearedWithoutReload=true;
        console.log('NEW_TP_VISIBLE_IN_OPEN_SUMMARY',report.runId);
      }
      const fresh=records.filter(r=>!old.has(r.id));
      fs.writeFileSync(path.join(output,'live-write-audit.json'),JSON.stringify({...report,latestRunState:run.status,latestRunPhase:run.phase,newAlerts:fresh},null,2));
      if(!['queued','running'].includes(run.status)&&fresh.length&&fresh.every(r=>!['queued','running'].includes(r.status)))break;
      await delay(3000);
    }
    assert.ok(newAlert,'A genuine new alert must be recorded');
    assert.ok(!['running','queued'].includes(run.status),'Experiment must finish');
    report.run={runId:run.run_id,status:run.status,phase:run.phase,comparison:run.result?.comparison,tuning:run.result?.tuning?.source_type};
    report.after=await get('/api/v1/keeninsight/overview');
    const finalAlerts=(await get('/api/v1/keeninsight/alerts?limit=200')).items.filter(r=>!old.has(r.id));
    report.newAlerts=finalAlerts;
    assert.ok(finalAlerts.some(r=>r.status==='completed'),'At least one real alert workflow must complete');
    assert.ok(finalAlerts.every(r=>!['running','queued'].includes(r.status)),'All triggered workflows must finish');
    await summary.waitForFunction(id=>document.getElementById('ki-body')?.textContent.includes(id),report.runId,{timeout:20000});
    await summary.locator('#ki-refresh').click();
    await summary.waitForFunction(()=>!document.getElementById('ki-refresh').disabled);
    await summary.screenshot({path:path.join(output,'summary-after-new-experiment.png')});
    const completed=finalAlerts.find(r=>r.status==='completed');
    const detail=await context.newPage();
    await detail.goto(base+'/keeninsight/alert-detail.html?id='+encodeURIComponent(completed.id));
    await detail.waitForFunction(()=>document.getElementById('ki-updated')?.textContent.startsWith('Updated'));
    const data=await get('/api/v1/keeninsight/alerts/'+completed.id);
    report.completedAlertEvidence={id:completed.id,model:data.diagnosis?.model,calls:data.diagnosis?.llmCallCount,
      stageCount:data.detailAnalysis?.stages?.length,application:data.detailAnalysis?.application?.status};
    await detail.screenshot({path:path.join(output,'new-completed-alert-detail.png')});
    assert.deepEqual(report.errors,[]);
    report.status='completed';console.log('PASS_REAL_WRITE_AND_BROWSER_REFRESH',JSON.stringify(report.completedAlertEvidence));
  }catch(error){report.status='failed';report.error=String(error);throw error;}
  finally{
    report.finishedAt=new Date().toISOString();
    fs.writeFileSync(path.join(output,'live-write-audit.json'),JSON.stringify(report,null,2));
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exit(1);});
