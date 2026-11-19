// Compare the actual browser presentation with English.zip served locally.
// This only reads product APIs/pages. Screenshots and audit JSON are test artifacts.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium}=require(process.env.KEENINSIGHT_PLAYWRIGHT_MODULE||'playwright');
const base=process.env.KEENINSIGHT_TEST_URL||'http://127.0.0.1:9108';
const original=process.env.KEENINSIGHT_ORIGINAL_URL||'http://127.0.0.1:9119';
const output=process.env.KEENINSIGHT_SCREENSHOT_DIR;
const pages=['template','host-list','alert-list','alert-rules','sql-log','dba-rules','manual','code-source','tuning-knowledge','alert-detail','sql-detail'];

async function geometry(page){
  return page.evaluate(()=>{
    const selectors=['#navbar','.sidebar','.content-area','.page-header','.page-title',
      '.filter-section','.search-section','.table-container','.top-section','.middle-section','.bottom-section'];
    const result={};
    for(const selector of selectors){
      const node=document.querySelector(selector);if(!node)continue;
      const box=node.getBoundingClientRect(),s=getComputedStyle(node);
      result[selector]={x:Math.round(box.x),width:Math.round(box.width),fontSize:s.fontSize,
        fontFamily:s.fontFamily,color:s.color,background:s.backgroundColor,padding:s.padding,
        columns:s.gridTemplateColumns};
    }
    return {layout:result,headers:[...document.querySelectorAll('thead th')].map(n=>n.textContent.trim()),
      sections:[...document.querySelectorAll('#detailContent .detail-title')].map(n=>n.textContent.trim().replace(/\s+/g,' ')),
      bodyOverflow:document.documentElement.scrollWidth>innerWidth};
  });
}
(async()=>{
  assert.ok(output,'Set KEENINSIGHT_SCREENSHOT_DIR');
  fs.mkdirSync(output,{recursive:true});
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const report={startedAt:new Date().toISOString(),viewport:{width:1440,height:1050},pages:[],apiErrors:[],consoleErrors:[]};
  try{
    const live=await browser.newPage({viewport:report.viewport});
    const reference=await browser.newPage({viewport:report.viewport});
    live.on('pageerror',e=>report.consoleErrors.push(e.message));
    live.on('response',r=>{if(r.url().includes('/api/v1/keeninsight/')&&!r.ok())report.apiErrors.push({url:r.url(),status:r.status()});});
    const alerts=await(await live.request.get(base+'/api/v1/keeninsight/alerts?limit=100')).json();
    const sql=await(await live.request.get(base+'/api/v1/keeninsight/sql?limit=1')).json();
    const alert=alerts.items.find(r=>r.status==='completed')||alerts.items[0];
    for(const name of pages){
      let query='';
      if(name==='alert-detail'){assert.ok(alert);query='?id='+encodeURIComponent(alert.id);}
      if(name==='sql-detail'){assert.ok(sql.items.length);query='?id='+encodeURIComponent(sql.items[0].id);}
      await reference.goto(original+'/'+name+'.html');
      if(name==='alert-detail'||name==='sql-detail'){
        const identity=await reference.evaluate(isAlert=>isAlert?alertData.find(a=>a.detailAnalysis)?.id:sqlLogData[0]?.id,name==='alert-detail');
        assert.ok(identity,'Archive detail fixture exists');
        await reference.goto(original+'/'+name+'.html?id='+encodeURIComponent(identity));
      }
      await live.goto(base+'/keeninsight/'+name+'.html'+query);
      await live.waitForFunction(()=>document.getElementById('ki-updated')?.textContent.startsWith('Updated'),null,{timeout:25000});
      assert.equal(await live.locator('#ki-error.show').count(),0,name+' loaded data');
      const originalStyles=await reference.locator('style').allTextContents();
      const liveStyles=await live.locator('style').allTextContents();
      assert.deepEqual(liveStyles,originalStyles,name+' archive inline style');
      const before=await geometry(reference),after=await geometry(live);
      const differences=[];
      for(const selector of ['#navbar','.sidebar','.content-area','.page-header','.page-title']){
        for(const key of ['x','width','fontSize','fontFamily','color','background','padding']){
          const a=before.layout[selector]?.[key],b=after.layout[selector]?.[key];
          if(a!==b)differences.push({selector,property:key,original:a,live:b});
        }
      }
      const entry={page:name,inlineStylesIdentical:true,original:before,live:after,
        shellDifferences:differences,loadedAt:await live.locator('#ki-updated').textContent(),
        removedMonitorLinks:await live.locator('a[href*="realtime-monitor"],[onclick*="realtime-monitor"]').count()};
      report.pages.push(entry);
      await reference.screenshot({path:path.join(output,name+'-original.png')});
      await live.screenshot({path:path.join(output,name+'-live.png')});
      assert.equal(entry.removedMonitorLinks,0);
      assert.equal(after.bodyOverflow,false,name+' no document overflow');
      console.log('PAGE',name,'CSS identical; shell differences',differences.length,'loaded');
    }
    assert.deepEqual(report.consoleErrors,[]);
    assert.deepEqual(report.apiErrors,[]);
    report.status='completed';
  }catch(error){report.status='failed';report.error=String(error);throw error;}
  finally{
    report.finishedAt=new Date().toISOString();
    fs.writeFileSync(path.join(output,'presentation-audit.json'),JSON.stringify(report,null,2));
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exit(1);});
