// Read-only browser integration checks against a running local bridge.
// KEENINSIGHT_PLAYWRIGHT_MODULE may point to a separately installed Playwright.
const assert = require('node:assert/strict');
const { chromium } = require(process.env.KEENINSIGHT_PLAYWRIGHT_MODULE || 'playwright');
const BASE = process.env.KEENINSIGHT_TEST_URL || 'http://127.0.0.1:9108';

(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1050 }, acceptDownloads: true });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const screenshot=async name=>{
      if(!process.env.KEENINSIGHT_SCREENSHOT_DIR)return;
      const style=await page.addStyleTag({content:'html,body,#container{height:auto!important}.main-wrapper,.content-area{overflow:visible!important}'});
      await page.screenshot({path:process.env.KEENINSIGHT_SCREENSHOT_DIR+'/'+name,fullPage:true});
      await style.evaluate(node=>node.remove());
    };
    const load = async (file, query = '') => {
      await page.goto(`${BASE}/keeninsight/${file}.html${query}`);
      await page.waitForFunction(() => document.getElementById('ki-updated')?.textContent.startsWith('Updated'), null, {timeout: 20000});
      assert.equal(await page.locator('#ki-error.show').count(), 0, file);
      assert.equal(await page.locator('[onclick*="realtime-monitor"],a[href*="realtime-monitor"]').count(), 0, file);
      assert.equal(await page.locator('#ki-root>.ki-heading').count(),0,'Do not replace original page layout with a generic renderer');
      assert.ok(await page.locator('.sidebar-menu .menu-item svg').count()>=7,'Original menu icons must remain');
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,file+' document overflow');
      console.log('PASS page', file);
    };
    for (const file of ['template', 'host-list', 'alert-list', 'alert-rules', 'sql-log', 'dba-rules', 'manual', 'code-source', 'tuning-knowledge']) {
      await load(file);
      if(file==='template'){
        for(const text of ['Automatic workflow configuration','Workflow outcomes','SysInsight live alert workflows','DREAM diagnosis and application','Data coverage and known limits']){
          assert.ok((await page.locator('#ki-body').innerText()).includes(text),text);
        }
        await screenshot('keeninsight_overview_live.png');
        console.log('PASS overview includes configuration, both workflows and evidence limits');
      }
      if (file === 'host-list')await screenshot('keeninsight_hosts_live.png');
      if (file === 'manual') {
        const first = await page.locator('.snippet-list tbody tr').first().innerText();
        const changed = page.waitForResponse(response => response.url().includes('offset=50') && response.ok());
        await page.locator('#ki-next').click();
        await changed;
        await page.waitForFunction(old => document.querySelector('.snippet-list tbody tr')?.innerText !== old, first);
        await page.locator('[data-ki-record]').first().click();
        await page.waitForSelector('.ki-modal.show');
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('.ki-modal.show').count(),0);
        console.log('PASS server-side pagination');
      }
    }
    await load('sql-log');
    const kindChanged = page.waitForResponse(response => response.url().includes('kind=execution') && response.ok());
    await page.locator('#ki-kind').selectOption('execution');
    await kindChanged;
    const filtered=page.waitForResponse(response=>response.url().includes('result=failed')&&response.ok());
    await page.locator('#statusFilter').selectOption('failed');
    const filteredData=await(await filtered).json();
    assert.ok(filteredData.items.every(row=>row.result==='failed'));
    // Retention removes yesterday's fixtures. Search an actually retained
    // record, not a hard-coded Q44 execution that may no longer exist.
    const records=await(await page.request.get(BASE+'/api/v1/keeninsight/sql?limit=1')).json();
    assert.ok(records.items.length,'A real SQL observation or execution is required');
    const selected=records.items[0];
    const resetFilters=page.waitForResponse(response=>response.url().includes('kind=all')&&response.ok());
    await page.locator('#ki-kind').selectOption('all');
    await resetFilters;
    await page.locator('#statusFilter').selectOption('');
    const keyword=selected.sql.replace(/^\s*(?:\/\*[\s\S]*?\*\/|--[^\n]*\n)\s*/,'').trim().split(/\s+/)[0];
    const searched=page.waitForResponse(response=>response.url().includes('q='+encodeURIComponent(keyword))&&response.ok());
    await page.locator('#ki-search').fill(keyword);
    await searched;
    await page.waitForSelector('#ki-body a.ki-detail-link');
    await page.locator('#ki-body a.ki-detail-link').first().click();
    await page.waitForFunction(() => document.getElementById('ki-updated')?.textContent.startsWith('Updated'));
    for(const title of ['SQL Basic Information','Root Causes','Matched Diagnosis Rules','Multiclass scoring model','SQL Lifecycle','Tuning Results'])assert.ok((await page.locator('#ki-body').innerText()).includes(title),title);
    assert.equal(await page.locator('#detailContent>.middle-section>.detail-section').count(),2);
    assert.equal(await page.locator('#detailContent>.top-section').count(),1);
    await screenshot('keeninsight_sql_detail_live.png');
    const download = page.waitForEvent('download');
    await page.locator('#ki-export').click();
    const downloaded = await download;
    assert.ok(downloaded.suggestedFilename().endsWith('.json'));
    console.log('PASS SQL search, detail navigation and JSON export');

    await load('alert-list');
    if (await page.locator('#ki-body a.ki-detail-link').count()) {
      await page.locator('#ki-body a.ki-detail-link').first().click();
      await page.waitForFunction(() => document.getElementById('ki-body')?.textContent.includes('Runtime call chains'));
      console.log('PASS real alert detail');
    }
    const alertData=await (await page.request.get(BASE+'/api/v1/keeninsight/alerts?limit=200')).json();
    const completed=alertData.items.find(item=>item.status==='completed'&&item.applicationStatus);
    if(completed){
      await load('alert-detail','?id='+encodeURIComponent(completed.id));
      const text=await page.locator('#ki-body').textContent();
      assert.ok(text.includes('Real LLM calls')&&text.includes(completed.applicationStatus));
      assert.ok(text.includes('Function records')&&text.includes('Workflow stages'));
      for(const title of ['Differential Profiling Analysis','SHAP Model Analysis Results','Function Analysis','Tuning Results'])assert.ok((await page.locator('#ki-body').innerText()).includes(title),title);
      assert.equal(await page.locator('#detailContent>.middle-section>.detail-section').count(),2);
      const detailData=await(await page.request.get(BASE+'/api/v1/keeninsight/alerts/'+encodeURIComponent(completed.id))).json();
      const shap=detailData.detailAnalysis.shapAnalysis;
      assert.equal(await page.locator('.shap-feature').count(),shap?.status==='completed'?shap.features.length:0,'SHAP bars must correspond to retained computed values');
      if(shap?.status==='completed'){
        assert.ok(Math.abs(shap.baseValue+shap.sumShapValues-shap.prediction)<1e-5);
        assert.ok((await page.locator('.ki-shap-summary').innerText()).includes(shap.modelVersion));
      }
      await screenshot('keeninsight_alert_detail_live.png');
      console.log('PASS completed real alert has LLM, perf, stages and restoration evidence');
    }
    const summary=await(await page.request.get(BASE+'/api/v1/keeninsight/overview')).json();
    const measured=summary.recentDreamJobs.find(item=>item.model&&item.applicationStatus==='active');
    if(measured){
      await load('sql-detail','?id='+encodeURIComponent(measured.sqlKey)+'&job_id='+encodeURIComponent(measured.id));
      const text=await page.locator('#ki-body').textContent();
      assert.ok(text.includes(measured.id)&&text.includes(measured.model));
      assert.ok(text.includes('Actual LLM diagnosis trace')&&text.includes('Automatic application'));
      assert.ok(text.includes('DREAM job history'));
      console.log('PASS exact DREAM job, raw diagnosis, lifecycle and automatic application');
    }
    const knowledge=await(await page.request.get(BASE+'/api/v1/keeninsight/knowledge?limit=50')).json();
    for(const item of knowledge.items){
      const data=await(await page.request.get(BASE+'/api/v1/keeninsight/sql/'+encodeURIComponent(item.sqlKey))).json();
      if(!data.job?.historyExpired)continue;
      await load('sql-detail','?id='+encodeURIComponent(item.sqlKey)+'&job_id='+encodeURIComponent(data.job.id));
      assert.ok(data.job.model,'Retained evidence must expose its actual model');
      assert.ok(data.diagnosisTrace,'Retained diagnosis must remain readable');
      await page.locator('.ki-evidence>summary').click();
      const text=await page.locator('#ki-body').innerText();
      assert.ok(text.includes(data.job.model)&&text.includes('24-hour task history expired'));
      console.log('PASS active-action evidence remains visible after task history expires');
      break;
    }
    await load('sql-log');
    const previous = await page.locator('#ki-body').innerText();
    await page.route('**/api/v1/keeninsight/sql?*', route => route.fulfill({status: 503, contentType: 'application/json', body: '{"error":"browser verification: upstream unavailable"}'}));
    await page.locator('#ki-refresh').click();
    await page.waitForSelector('#ki-error.show');
    assert.equal(await page.locator('#ki-body').innerText(), previous);
    await page.unroute('**/api/v1/keeninsight/sql?*');
    await page.locator('#ki-refresh').click();
    await page.waitForFunction(() => !document.getElementById('ki-error').classList.contains('show'));
    console.log('PASS error preserves data; refresh recovers');
    const timestamp = await page.locator('#ki-updated').innerText();
    await page.locator('h1').click();
    await page.waitForFunction(old => document.getElementById('ki-updated').textContent !== old, timestamp, {timeout: 14000});
    console.log('PASS automatic refresh');
    assert.deepEqual(errors, []);
    console.log('PASS no browser JavaScript errors');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
