// Read-only checks against actual retained SQL profiles and computed SHAP evidence.
const assert = require('node:assert/strict');
const {chromium} = require(process.env.KEENINSIGHT_PLAYWRIGHT_MODULE || 'playwright');
const BASE=process.env.KEENINSIGHT_TEST_URL||'http://127.0.0.1:9108';
(async()=>{
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    const get=async path=>{const r=await page.request.get(BASE+'/api/v1/keeninsight/'+path);assert.ok(r.ok(),path);return r.json();};
    const load=async(file,id)=>{
      await page.goto(BASE+'/keeninsight/'+file+'.html?id='+encodeURIComponent(id));
      await page.waitForFunction(()=>document.getElementById('ki-updated')?.textContent.startsWith('Updated'));
      assert.equal(await page.locator('#ki-error.show').count(),0);
    };
    const logs=await get('sql?kind=execution&limit=30');
    let sql;
    for(const entry of logs.items){const data=await get('sql/'+encodeURIComponent(entry.id));if(data.flameGraph){sql=data;break;}}
    if(!sql)throw new Error('No real retained SQL profile. Run a real long catalog SQL before this test; do not invent samples.');
    await load('sql-detail',sql.id);
    assert.equal(sql.flameGraph.value,sql.sqlProfile.sampleCount);
    assert.equal(sql.sqlProfile.executionId,sql.id);
    if(sql.sqlProfile.participants){
      assert.equal(sql.sqlProfile.sampleCount,sql.sqlProfile.participants.reduce((n,p)=>n+p.sampleCount,0));
      for(const participant of sql.sqlProfile.participants){
        assert.ok((await page.locator('#flameGraphContainer').innerText()).includes(participant.role+' PID '+participant.pid));
      }
    }
    const selector='#flameGraphContainer .flame-bar';
    const count=await page.locator(selector).count();assert.ok(count>3);
    await page.locator(selector).nth(2).click({force:true});
    assert.ok(await page.locator(selector).count()<=count);
    await page.locator('#flameGraphContainer [data-ki-flame-reset]').click();
    assert.equal(await page.locator(selector).count(),count);
    await page.locator('button[data-ki-zoom]').click();await page.waitForSelector('.ki-modal.show');
    assert.ok(await page.locator('.ki-modal .flame-bar').count()>3);await page.keyboard.press('Escape');
    console.log('PASS real SQL profile, exact execution, sample counts, frame zoom, reset and enlarged view:',sql.id);
    const alerts=await get('alerts?limit=50');let alert;
    for(const entry of alerts.items){const data=await get('alerts/'+encodeURIComponent(entry.id));if(data.detailAnalysis.shapAnalysis?.status==='completed'){alert=data;break;}}
    if(!alert)throw new Error('No computed alert SHAP evidence. Trigger a supported real alert before this test.');
    await load('alert-detail',alert.id);
    const shap=alert.detailAnalysis.shapAnalysis;
    assert.equal(await page.locator('.shap-feature').count(),shap.features.length);
    assert.ok(Math.abs(shap.baseValue+shap.sumShapValues-shap.prediction)<1e-5);
    assert.ok((await page.locator('.ki-shap-summary').innerText()).includes(shap.modelVersion));
    assert.ok((await page.locator('.ki-shap-summary').innerText()).includes('not measured TPS gains'));
    if(shap.reliability){
      assert.ok(await page.locator('.ki-shap-trust').count());
      if(!shap.reliability.decisionEligible)assert.ok((await page.locator('.ki-shap-trust').innerText()).includes('excluded from LLM tuning advice'));
    }
    if(shap.independentValidation)assert.ok((await page.locator('.ki-shap-summary').innerText()).includes('Latest independent time-batch MAE'));
    const bars=await page.locator('.ki-shap-diverging .shap-bar-fill').evaluateAll(nodes=>nodes.map(n=>({left:parseFloat(n.style.left),width:parseFloat(n.style.width)})));
    assert.ok(bars.every(b=>b.left>=0&&b.left+b.width<=100.000001));
    assert.equal(await page.locator('.middle-section>.detail-section').count(),2);
    assert.equal(await page.locator('[href*=realtime-monitor]').count(),0);
    assert.deepEqual(errors,[]);
    console.log('PASS actual model SHAP, signed bars, additivity, validation warning and original layout:',alert.id);
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
