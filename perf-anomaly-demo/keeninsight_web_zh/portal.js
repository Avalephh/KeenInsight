'use strict';
(() => {
  const API = '/api/v1/keeninsight/';
  const page = location.pathname.split('/').pop();
  const config = {
    'template.html': ['KeenInsight Overview', 'overview'],
    'host-list.html': ['Hosts & Clusters', 'hosts'],
    'alert-list.html': ['Alert List', 'alerts'],
    'alert-detail.html': ['Alert Details', 'alerts'],
    'alert-rules.html': ['Alert Rules', 'alertRules'],
    'manual.html': ['Manual & Parameter Reference', 'manual'],
    'code-source.html': ['Code Source', 'code'],
  }[page] || ['KeenInsight', 'overview'];
  const detail = page.endsWith('-detail.html');
  const root = document.getElementById('ki-root');
  // English.zip owns presentation. This adapter only supplies live data and controls.
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const shown = value => value == null || value === '' ? '—' : esc(typeof value === 'object' ? JSON.stringify(value) : value);
  const number = (value, digits=2) => value == null ? '—' : Number(value).toLocaleString(undefined,{maximumFractionDigits:digits});
  const date = value => value ? esc(new Date(value).toLocaleString()) : '—';
  const code = value => `<pre class="ki-code">${esc(typeof value === 'string' ? value : JSON.stringify(value, null, 2))}</pre>`;
  const raw = (value, title='Evidence JSON') => `<details class="ki-raw"><summary>${esc(title)}</summary>${code(value)}</details>`;
  const empty = text => `<div class="ki-empty">${esc(text || 'No retained records. New executions will appear here automatically.')}</div>`;
  const panel = (title, content) => `<section class="ki-panel"><h2>${esc(title)}</h2>${content}</section>`;
  const tag = value => `<span class="ki-tag ${['completed','active','online','recorded','success'].includes(value)?'good':['failed','error','blocked','stale'].includes(value)?'bad':['running','queued','firing'].includes(value)?'live':''}">${shown(value)}</span>`;
  const usage=(value,metadata)=>value==null?'<span class="ki-row-meta" title="No current exporter measurement available">Not measured</span>':`<div class="usage-bar" title="${esc(metadata?.scope)} · ${esc(metadata?.source)} · ${esc(metadata?.queriedAt)}"><div class="progress-container"><div class="progress-bar ${value>=80?'progress-high':value>=60?'progress-medium':'progress-low'}" style="width:${Math.max(0,Math.min(100,value))}%"></div></div><span class="usage-value">${number(value,1)}%</span></div>`;
  const link = (file, id, label) => `<a class="ki-detail-link" href="${file}?id=${encodeURIComponent(id)}">${esc(label)}</a>`;
  const percent = value => value==null?'Not measured':number(value*100)+'%';
  const stages = rows => table(['Stage','State','Started / recorded','Duration (ms)'],(rows||[]).map(s=>[esc(s.name||s.phase),tag(s.status),date(s.startedAt||s.recordedAt),number(s.durationMs)]));
  const coverage = values => table(['Capability','Availability','Meaning'],values.map(c=>[esc(c.name),tag(c.state),esc(c.detail)]));
  const kv = values => `<dl class="ki-kv">${values.map(([k,v])=>`<dt>${esc(k)}</dt><dd>${v}</dd>`).join('')}</dl>`;
  const table = (headers, rows) => rows.length ? `<div class="ki-table-wrap"><table class="ki-table"><thead><tr>${headers.map(x=>`<th>${esc(x)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c??'—'}</td>`).join('')}</tr>`).join('')}</tbody></table></div>` : empty();
  let offset=0, total=0, current=null, busy=false, search='', kind='all', controller=null, searchTimer=null, limit=50;
  const header=root.querySelector('.page-header');
  const body=document.createElement('div'); body.id='ki-body';
  [...root.children].filter(node=>node!==header).forEach(node=>body.appendChild(node));
  root.appendChild(body);
  const actions=header.querySelector('.page-actions')||document.createElement('div');
  actions.classList.add('page-actions','ki-actions'); if(!actions.parentNode)header.appendChild(actions);
  const back=header.querySelector('.back-btn');if(back){actions.prepend(back);back.onclick=()=>location.href='alert-list.html';}
  const refreshButton=[...actions.querySelectorAll('button')].find(b=>b.textContent.trim()==='Refresh');
  if(refreshButton)refreshButton.id='ki-refresh';
  else actions.insertAdjacentHTML('beforeend','<button class="btn btn-default ki-button" id="ki-refresh">Refresh</button>');
  actions.insertAdjacentHTML('beforeend','<button class="btn btn-default ki-button" id="ki-export">Export JSON</button>');
  header.insertAdjacentHTML('afterend','<div class="ki-live-meta"><span id="ki-updated">Connecting…</span><span>Live records · 24 h history</span></div><div class="ki-banner" id="ki-error" role="alert"></div>');
  const disabled=(node,why='Read-only: no management API is connected')=>{node.disabled=true;node.setAttribute('aria-disabled','true');node.title=why;};
  root.querySelectorAll('button').forEach(b=>{if(/^(Upload|Add Host|Batch Start|Batch Stop|\+ New Rule)$/.test(b.textContent.trim()))disabled(b);});
  root.querySelectorAll('.view-btn:not(.active),.custom-checkbox').forEach(b=>disabled(b));
  root.querySelectorAll('.logo').forEach(b=>b.onclick=()=>location.href='template.html');
  document.querySelector('.logo')?.addEventListener('click',()=>location.href='template.html');
  document.querySelectorAll('.nav-action').forEach(b=>{
    if(b.textContent.trim()==='Console')b.onclick=()=>location.href='/lab';
    else {b.title='Not connected to a management API';b.setAttribute('aria-disabled','true');}
  });
  const filters={};
  function filterControl(id,key,options){
    const node=document.getElementById(id);if(!node)return;
    if(options)node.innerHTML=options.map(([v,label])=>`<option value="${esc(v)}">${esc(label)}</option>`).join('');
    node.onchange=()=>{filters[key]=node.value;offset=0;refresh(true);};
  }
  if(!detail&&config[1]!=='overview'){
    let filterArea=body.querySelector('.filter-section,.search-section');
    if(!filterArea){body.insertAdjacentHTML('afterbegin','<div class="filter-section ki-extra-filter"><div class="filter-item"><span class="filter-label">Keyword:</span><input class="filter-input" id="keywordFilter" placeholder="Search live records"></div><button class="filter-btn" data-ki-search>Search</button><button class="filter-btn reset" data-ki-reset>Reset</button></div>');filterArea=body.firstElementChild;}
    const input=document.getElementById('keywordFilter')||document.getElementById('hostFilter')||body.querySelector('.search-input');
    if(input){input.id='ki-search';input.setAttribute('aria-label','Search');input.addEventListener('input',e=>{search=e.target.value;offset=0;clearTimeout(searchTimer);searchTimer=setTimeout(()=>refresh(true),300);});}
    filterArea.querySelectorAll('button').forEach(b=>b.onclick=()=>{clearTimeout(searchTimer);if(b.textContent.trim()==='Reset'){filterArea.querySelectorAll('input,select').forEach(n=>{n.value='';});search='';kind='all';Object.keys(filters).forEach(k=>delete filters[k]);const recordKind=document.getElementById('ki-kind');if(recordKind)recordKind.value='all';}offset=0;refresh(true);});
    if(config[1]==='alerts'){
      filterControl('severityFilter','severity');filterControl('statusFilter','status',[['','All'],...['running','completed','failed','skipped'].map(x=>[x,x])]);
      // Search now matches alert name, ID, host and trigger description.
      document.getElementById('ki-search').placeholder='Host, alert ID or trigger reason';
      document.getElementById('ki-search').previousElementSibling.textContent='Keyword:';
      filterControl('reasonFilter','reason');
    }else if(config[1]==='code'){
      filterControl('dbFilter','db');filterControl('categoryFilter','category',[['','全部'],['control_flow','控制流'],['data_flow','数据流']]);
    }else if(config[1]==='hosts'){
      const selects=filterArea.querySelectorAll('select');
      selects[0].innerHTML='<option value="">All Status</option><option value="online">Online</option><option value="stale">Stale collection</option>';
      selects[0].onchange=()=>{filters.status=selects[0].value;offset=0;refresh(true);};
      selects[1].onchange=()=>{filters.dbType=selects[1].value;offset=0;refresh(true);};
      disabled(selects[2],'No region metadata configured');disabled(selects[3],'Host utilization charts are provided by Grafana');
      const ip=filterArea.querySelectorAll('input')[1];ip.oninput=()=>{filters.ip=ip.value;offset=0;clearTimeout(searchTimer);searchTimer=setTimeout(()=>refresh(true),300);};
      const size=body.querySelector('.page-size-select');size.value='50 items/page';size.onchange=()=>{limit=parseInt(size.value,10);offset=0;refresh(true);};
      [...body.querySelectorAll('button')].filter(b=>b.textContent.trim()==='Batch Export').forEach(b=>b.onclick=()=>document.getElementById('ki-export').click());
    }
  }

  function alertEvidence(data) {
    const d=data.detailAnalysis||{};
    const c=d.comparison||{},diag=data.diagnosis||{},perf=d.perfCapture||{};
    const chains=(d.runtimeCallChains||[]).map(x=>[number(x.samples,0),shown((x.matched_key_functions||[]).join(', ')),shown((x.frames||[]).join(' → '))]);
    return `<div class="ki-breadcrumb"><a href="template.html">Overview</a> / <a href="alert-list.html">Alert List</a> / ${esc(data.id)}</div><div class="ki-grid">
      ${panel('Alert',kv([['Reason',shown(data.reason)],['Time',date(data.time)],['Host',shown(data.host)],['Severity',tag(data.severity)],['Workflow status',tag(data.rawStatus)],['Trigger state (historical)',tag(data.triggerState||'not_retained')],['Duration',number(data.durationMs)+' ms'],['Message',shown(data.message||data.error||'No workflow error')]])+raw(d.triggeredRule,'Trigger evidence')+'<p class="ki-sub">Workflow completion does not mean the alert resolved or performance improved.</p>')}
      ${panel('Diagnosis',kv([['Model',shown(diag.model||'Not yet called')],['Real LLM calls',number(diag.llmCallCount,0)],['Candidates generated',number(diag.candidateCount,0)],['Differential functions',number(diag.hotFunctionCount,0)],['Hot functions',shown((data.rootCause||[]).join(', ')||'Not yet available')],['Meaning',shown(diag.explanation)]])+raw(diag.matchedParameters||[],'Matched source parameters'))}
      ${panel('Backend capture',kv([['Selection',shown(d.backendSnapshot?.selection||'Historical active PID capture')],['Capture started',date(perf.perf?.requested_at)],['Capture status',tag(perf.perf?.status||'not_captured')],['Postprocess',tag(perf.postprocess?.status||'not_captured')],['Function records',number(perf.postprocess?.function_count,0)]])+raw(d.backendSnapshot||{},'Backend snapshot'))}
      ${panel('Measured apply / retest / restore',kv([['Validation metric',shown(c.metric||'No comparison completed')],['Baseline',number(c.baseline_ms)+' ms'],['Tuned',number(c.tuned_ms)+' ms'],['Restored',number(c.restored_ms)+' ms'],['Measured time reduction',percent(c.tuned_improvement_ratio)],['Restoration',tag(d.application?.status||'not_applied')]])+raw(d.validationSql||{},'Actual replay SQL')+raw(d.application,'Application and restoration'))}
      ${panel('Workflow stages',stages(d.stages))}
      ${panel('Differential profiling',d.differentialProfiling?code(d.differentialProfiling):empty(data.availability?.profiling))}
      ${panel('perf flame graph / call chains',(chains.length?table(['Samples','Matched functions','Call path'],chains):empty(data.message||'No runtime stack evidence retained yet'))+raw(perf.perf||{},'perf command, duration and artifact')+raw(perf.postprocess||{},'Fold / function-range postprocess'))}
      ${panel('Source / parameter evidence',d.functionCodeExamples?.length?code(d.functionCodeExamples):empty('No source evidence retained yet'))}
      ${panel('Candidate parameters',Object.keys(d.knobs||{}).length?code(d.knobs):empty('No candidate recorded yet'))}
      ${panel('SHAP model evidence',d.shapAnalysis?raw(d.shapAnalysis,'Model, training validation, exact SHAP values and additivity check'):empty(data.availability?.shapAnalysis))}
      ${panel('Metric evidence',raw(d.runtimeData||{},'Measured host / database window')+'<p class="ki-sub">Time-series monitoring remains in Grafana. Differential profiling and model SHAP attribution are distinct evidence sources.</p>')}
      </div><p class="ki-sub">This page shows a compact evidence view; large function/stack arrays are truncated for fast loading. <a class="ki-button" href="${esc(data.fullEvidenceUrl||('/api/v1/keeninsight/alerts/'+encodeURIComponent(data.id)+'?full=1'))}" download="${esc(data.id)}-full-evidence.json">Download full evidence JSON</a></p>`;
  }

  function renderTuningRationale(r) {
    if(!r) return '';
    const root=r.root_cause?`<div class="ki-tuning-root">${esc(r.root_cause)}</div>`:'';
    const validation=r.validation?`<h3>复测结果</h3><div class="ki-tuning-validation">${esc(r.validation)}</div>`:'';
    return `<div class="ki-tuning-rationale">${root}${validation}<p class="ki-sample-note">以上说明由 ${esc(r.model||'gpt-6.1-sol')} 基于本次候选理由、参数证据和复测结果生成；各参数的具体调优原因已移到下方调优操作中，关联不代表因果。</p></div>`;
  }

  function alertDetail(data){
    const d=data.detailAnalysis||{},rule=d.triggeredRule||{},app=d.application||{},c=d.comparison||{};
    const rules=`<div style="background:#f7f8fa;padding:16px;border-radius:6px"><div style="font-size:12px;color:#86909c;margin-bottom:8px">${shown(rule.name)}</div><div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">${[['Host',shown(data.host)],['Actual value',shown(rule.actual)],['Trigger state (historical)',tag(rule.state||'not_retained')],['Workflow status',tag(data.rawStatus)]].map(([k,v])=>`<div><div style="font-size:11px;color:#86909c">${k}</div><div style="font-size:14px;font-weight:600;color:#282f38">${v}</div></div>`).join('')}</div></div>`;
    const functions=d.differentialProfiling?.key_functions||[];
    const max=Math.max(1,...functions.map(f=>Math.abs(Number(f['Diff From Mean'])||0)));
    const profiling=functions.length?'<p class="ki-sample-note">Original profiler sample-rate units; inclusive recursion may exceed 100. Normal mean reconstructed from the recorded signed difference. Bar: relative absolute difference, not SHAP.</p><div class="profiling-row header"><div>Function Name</div><div>Normal</div><div>Abnormal</div><div>Difference</div><div>Diff Degree</div></div>'+functions.map(f=>{
      const rate=Number(f['Sample Rate']),delta=Number(f['Diff From Mean'])*(f.Change===1?1:-1),normal=rate-delta;
      return `<div class="profiling-row"><div class="profiling-function">${esc(f.Function)}</div><div class="profiling-value">${number(normal)}</div><div class="profiling-value">${number(rate)}</div><div class="profiling-diff ${delta>=0?'high':'low'}">${delta>=0?'+':''}${number(delta)}</div><div class="profiling-bar"><div class="profiling-bar-fill ${delta>=0?'high':'low'}" style="width:${Math.min(100,Math.abs(delta)/max*100)}%"></div></div></div>`;
    }).join(''):empty('No differential sampling evidence retained.');
    const hotList=d.hotFunctions||[];
    const hot=hotList.map(f=>{
      const source=(d.functionCodeExamples||[]).find(s=>s.function===f.Function);
      const location=f.SourceFile?`${f.SourceFile}${f.SourceLine?':'+f.SourceLine:''}`:(source?`${source.file}:${source.line}`:'未在固定 PostgreSQL 12.22 源码中定位（生成代码、扩展或系统符号）');
      const evidence=f.Evidence==='runtime_call_chain'?'perf 调用链':f.Evidence==='source_association'?'源码关联':'perf 差分采样';
      return `<div class="function-item ki-clickable" data-ki-function="${esc(f.Function)}" tabindex="0" role="button" aria-label="查看函数详情 ${esc(f.Function)}"><div class="function-header"><div class="function-name">${esc(f.Function)}</div><span class="ki-function-open">详情 ›</span></div><div class="function-info-row"><div class="function-meta">${esc(location)}</div><div class="function-sources"><span class="source-tag">${evidence}</span></div></div></div>`;
    }).join('');
    const hotNote=d.hotFunctionsDerived?'<p class="ki-sample-note">该告警没有差分采样结果，以下函数由运行时 perf 调用链和源码关联生成。</p>':'';
    const functionsHtml=hotList.length?hotNote+hot:empty(data.rawStatus==='skipped'
      ?'该告警被跳过：采样窗口内没有存活的业务后端，因此没有函数证据。'
      :'该告警在函数采样前中断，没有可用的函数证据。');
    const backfillNote=d.backfillGenerated?'<p class="ki-sample-note">该告警的差分画像、SHAP 和函数分析由保留的 perf 数据事后生成；调优候选与前后对比仍以原始流程记录为准。</p>':'';
    const interrupted=d.backfillGenerated||(d.stages||[]).some(s=>['running','failed'].includes(String(s.status)));
    const normalizeKnob=v=>{
      let value=String(v??'').trim().replace(/^['"]|['"]$/g,''),lower=value.toLowerCase();
      if(['on','true','yes','1'].includes(lower))return'bool:true';
      if(['off','false','no','0'].includes(lower))return'bool:false';
      const numeric=Number(value);
      return value!==''&&Number.isFinite(numeric)?'num:'+numeric:'str:'+value;
    };
    const rationaleByParam=new Map(((d.tuningRationale||{}).actions||[]).map(a=>[String(a.parameter),a]));
    const knobEntries=Object.entries(app.requested_configuration||d.knobs?.selected_candidate?.configuration||{}).filter(([name,value])=>{
      const setting=app.pre_settings?.[name];
      return !setting||normalizeKnob(setting.setting)!==normalizeKnob(value);
    });
    const knobs=knobEntries.map(([name,value])=>{
      const setting=app.pre_settings?.[name],skipped=app.skipped_configuration?.[name],rationale=rationaleByParam.get(String(name));
      const reason=rationale?.reason?`<div class="knob-reason"><span class="knob-reason-label">调优原因</span>${esc(rationale.reason)}</div>`:'';
      const evidence=(rationale?.evidence||[]).length?`<details class="knob-evidence"><summary>关联证据</summary><div class="ki-row-meta">${rationale.evidence.map(x=>esc(x)).join('；')}</div></details>`:'';
      return `<div class="knob-item"><div class="knob-name">${esc(name)}<div class="ki-row-meta">${esc(skipped?'Not applied: '+skipped.reason:app.status||'Candidate only')}</div></div><div class="knob-value"><span class="knob-current">${shown(setting?.setting)}</span><span class="knob-arrow">→</span><span class="knob-recommended">${shown(value)} ${esc(setting?.unit||'')}</span></div>${reason}${evidence}</div>`;
    }).join('')||empty(interrupted?'该告警在调优候选生成前中断，未产生可用的候选参数。':'本次没有实际参数变化。');
    const s=d.shapAnalysis;
    let shap=empty('暂无 SHAP 证据');
    if(s?.status==='completed'&&s.features?.length){
      const max=Math.max(...s.features.map(f=>Math.abs(f.shapValue)),1e-12);
      shap=`<p class="ki-sample-note">绿色表示模型预测升高，红色表示降低。单位为百分点（pp）。</p>`+s.features.map(f=>{
        const positive=f.shapValue>=0,width=Math.abs(f.shapValue)/max*50;
        return `<div class="shap-feature"><div class="shap-name" title="${esc((f.parameters||[]).join(', '))}">${esc(f.name)}</div><div class="shap-value">${f.isAggregate?'aggregate':number(f.value)}</div><div class="shap-bar ki-shap-diverging"><div class="shap-bar-fill ${positive?'low':'high'}" style="position:absolute;left:${positive?50:50-width}%;width:${width}%"></div></div><div class="shap-impact" style="color:${positive?'#008943':'#c62c20'}">${positive?'+':''}${number(f.shapValue*100,3)} pp</div></div>`;
      }).join('');
    }
    const notMeasured=interrupted?'未完成（工作流中断）':'Not measured';
    const noValidation=interrupted?'调优未执行':'No validation';
    const rationaleHtml=renderTuningRationale(d.tuningRationale);
    const lab=d.relatedLabRun||{},lc=lab.comparison||{};
    const hasLabTps=lc.baseline_tps!=null&&lc.pressure_before_tps!=null&&lc.pressure_after_tps!=null;
    const tps=value=>value==null?'—':Number(value).toFixed(2)+' TPS';
    const tpsSummary='';
    const replayNote='';
    const tuningLabels=hasLabTps?{
      beforeTuning_label1:'压力调优前 TPS',beforeTuning_label2:'基线 TPS',beforeTuning_label3:'实验状态',
      afterTuning_label1:'压力调优后 TPS',afterTuning_label2:'调优提升',afterTuning_label3:'调优后/基线'
    }:{};
    return backfillNote+window.KeenInsightLayouts.alert({rules,profiling,shap,functions:functionsHtml,knobs,explanation:rationaleHtml||shown(data.diagnosis?.explanation||data.message||'No root-cause explanation retained.'),...tuningLabels,tpsSummary,replayNote,beforeTuning_latency:hasLabTps?tps(lc.pressure_before_tps):(c.baseline_ms==null?notMeasured:number(c.baseline_ms)+' ms'),afterTuning_latency:hasLabTps?tps(lc.pressure_after_tps):(c.tuned_ms==null?notMeasured:number(c.tuned_ms)+' ms'),beforeTuning_restored:hasLabTps?tps(lc.baseline_tps):'—',afterTuning_restored:hasLabTps?(lc.tuning_gain_tps==null?'—':`+${number(lc.tuning_gain_tps)} TPS (${percent(lc.tuning_gain_ratio)})`):(c.restored_ms==null?notMeasured:number(c.restored_ms)+' ms'),beforeTuning_state:hasLabTps?tag(lab.status||'completed'):esc(c.metric||noValidation),afterTuning_state:hasLabTps?percent(lc.recovery_ratio):tag(app.status||'not_applied')})+`<details class="ki-evidence"><summary>Workflow evidence · runtime call chains, LLM tuning, apply, retest and restoration</summary><div class="ki-evidence-body">${alertEvidence(data)}</div></details>`;
  }

  async function showRunDetail(runId, incidentParam) {
    try {
      const response=await fetch('/api/v1/lab/runs/'+encodeURIComponent(runId),{cache:'no-store'});
      if(!response.ok)throw new Error(await response.text());
      const d=await response.json(), r=d.result||{}, c=r.comparison||{}, t=r.tuning||{}, cfg=r.config||d.config||{};
      const phases=Object.entries(r.phases||{}).map(([name,p])=>{
        const b=(p.business_tps_summary||{}).steady_tps, pr=(p.pressure_tps_summary||{}).steady_tps;
        const rc=[(p.control||{}).returncode,(p.external||{}).returncode].filter(v=>v!==undefined&&v!==null).join(' / ');
        return [esc(name),number(p.duration_seconds,0)+' s',number(b),number(pr),shown(rc||'-')];
      });
      const incidents=[...new Set([...String(incidentParam||'').split(','),...(r.sysinsight_state?.current_incidents||[]).map(x=>x.incident_id),...(r.incidentIds||[])])].filter(Boolean);
      const html=`<div class="ki-stats"><div class="ki-stat"><strong>${tag(d.status)}</strong><span>${esc(d.phase||'')}</span></div><div class="ki-stat"><strong>${number(c.baseline_tps)}</strong><span>Baseline TPS</span></div><div class="ki-stat"><strong>${number(c.pressure_before_tps)}</strong><span>Pressure before</span></div><div class="ki-stat"><strong>${number(c.pressure_after_tps)}</strong><span>Pressure after</span></div><div class="ki-stat"><strong>${percent(c.tuning_gain_ratio)}</strong><span>Change</span></div></div>`+
        panel('Run',kv([['Run ID',esc(d.run_id)],['Scenario',esc(r.scenario?.title||d.scenario_id||'—')],['Kind',esc(d.kind||'—')],['Requested',date(d.requested_at)],['Started',date(d.started_at)],['Finished',date(d.finished_at)],['Error',shown(d.error||'None')]]))+
        panel('Configuration',code(cfg))+
        panel('Pressure comparison',kv([['Baseline TPS',number(c.baseline_tps)],['Pressure before TPS',number(c.pressure_before_tps)],['Pressure after TPS',number(c.pressure_after_tps)],['Tuning gain TPS',number(c.tuning_gain_tps)],['Tuning gain ratio',percent(c.tuning_gain_ratio)],['External pressure before',number(c.external_pressure_before_tps)],['External pressure after',number(c.external_pressure_after_tps)],['Same pressure observed',shown(c.same_pressure_observed)],['Comparison valid',shown(c.comparison_valid)],['Recovery ratio',percent(c.recovery_ratio)]])+(c.comparison_valid===false?'<p class="ki-sub">Comparison is not valid because the before/after external pressure differed beyond the allowed window. Re-run with longer pressure windows for a demo result.</p>':''))+
        panel('Tuning candidate',kv([['Status',tag(t.status||'unknown')],['Source',shown(t.source||'—')],['Source type',shown(t.source_type||'—')],['Reason',shown(t.reason||'—')]])+raw(t.application||{} ,'Application / restore evidence')+(t.blocked_by_safety?raw(t.blocked_by_safety,'Blocked settings'):''))+
        panel('Phases',phases.length?table(['Phase','Duration','Target TPS (steady)','Pressure TPS (steady)','Return codes'],phases):empty('No phase records retained.'))+
        panel('Linked alerts',incidents.length?incidents.map(id=>link('alert-detail.html',id,id)).join(' · '):empty('No alert incident linked to this run.'))+
        raw(d,'Full run record');
      showModal('Tuning run · '+runId,html);
    } catch(error) {
      showModal('Tuning run','<div class="ki-empty">Failed to load run detail: '+esc(error.message)+'</div>');
    }
  }

  async function showFunctionDetail(name, data) {
    const d=data.detailAnalysis||{};
    const hot=(d.hotFunctions||[]).find(f=>f.Function===name)||{};
    const examples=(d.functionCodeExamples||[]).filter(s=>s.function===name);
    const chains=(d.runtimeCallChains||[]).filter(c=>(c.matched_key_functions||[]).includes(name));
    const evidence=hot.Evidence==='runtime_call_chain'?'perf 调用链':hot.Evidence==='source_association'?'源码关联':'perf 差分采样';
    const location=hot.SourceFile?`${hot.SourceFile}${hot.SourceLine?':'+hot.SourceLine:''}`:'未在固定 PostgreSQL 12.22 源码中定位';
    const summary=kv([['函数',esc(name)],['源码位置',esc(location)],['证据来源',esc(evidence)],['采样率',shown(hot['Sample Rate'])],['相对均值差',shown(hot['Diff From Mean'])]]);
    const exampleRows=examples.map(s=>[esc(s.knob_name||s.guc_variable||'—'),esc(s.context||'—'),esc(`${s.file}:${s.line}`),esc(s.source_line||'')]);
    const relatedExamples=exampleRows.length?panel('相关参数与源码证据',table(['参数','类型','位置','源码行'],exampleRows)):'';
    const chainRows=chains.map(c=>[number(c.samples,0),esc((c.frames||[]).join(' → ').slice(0,800))]);
    const relatedChains=chainRows.length?panel('相关运行时调用链',table(['样本数','调用路径'],chainRows)):'';
    showModal(name, summary+'<p class="ki-sample-note">正在读取源码说明…</p>');
    let detail=null,loadError='';
    try {
      const response=await fetch('/api/v1/keeninsight/functionDetails/'+encodeURIComponent(name),{cache:'no-store'});
      const payload=await response.json();
      if(!response.ok)throw new Error(payload.error||('HTTP '+response.status));
      detail=payload;
    } catch(error) {
      loadError=error.message;
    }
    if(!detail) {
      showModal(name, summary+`<p class="ki-empty">无法读取该函数的源码说明：${esc(loadError)}</p>`+relatedExamples+relatedChains);
      return;
    }
    const model=detail.descriptionModel?` · ${esc(detail.descriptionModel)}`:'';
    const descriptionTitle=detail.descriptionSource==='llm'?`函数说明（AI 生成${model}）`:detail.descriptionSource==='source_comment'?'函数说明（PostgreSQL 源码注释原文）':'函数说明';
    const description=detail.description?`<p class="ki-function-description">${esc(detail.description)}</p>`:'<p class="ki-sample-note">暂无函数说明；源码中没有注释，AI 说明也未能生成。</p>';
    const signature=detail.signature?`<h3>函数签名</h3><pre class="ki-code">${esc(detail.signature)}</pre>`:'';
    const numbered=String(detail.excerpt||'').split('\n').map((line,index)=>String((detail.excerptStartLine||1)+index).padStart(5,' ')+'  '+line).join('\n');
    const excerpt=numbered?`<h3>源码片段</h3><pre class="ki-code">${esc(numbered)}</pre>`:'';
    showModal(name, `<div class="ki-function-detail">${summary}<h3>${descriptionTitle}</h3>${description}${signature}${relatedExamples}${relatedChains}${excerpt}</div>`);
  }

  function overview(data) {
    const auto=data.automation||{};
    const summaries=Object.entries(data.workflowCounts||{}).map(([name,values])=>[esc(name.replaceAll('_',' ')),...['running','completed','failed','skipped'].map(state=>number(values[state]||0,0))]);
    const experiments=(data.recentRuns||[]).map(r=>{
      const c=r.comparison||{};
      const measurement=`${number(c.baseline_tps)} → ${number(c.pressure_before_tps)} → ${number(c.pressure_after_tps)} TPS`;
      return [date(r.requested_at),`${esc(r.title||r.scenario_id)}<div class="ki-sub">${esc(r.run_id)}</div>`,`${tag(r.status)} ${tag(r.phase)}`,measurement+`<div class="ki-sub">Normal → pressure → tuned under pressure · change ${percent(c.tuning_gain_ratio)}</div>`,`${tag(r.tuningStatus||'awaiting_result')}<div class="ki-sub">${esc(r.candidateSourceType||'No candidate source')}</div>`,`<button class="action-btn" data-ki-run-detail="${esc(r.run_id)}" data-ki-run-incidents="${esc((r.incidentIds||[]).join(','))}">Details</button> ${(r.incidentIds||[]).map(id=>link('alert-detail.html',id,'Alert')).join(' · ')}${raw(r,'Experiment evidence / provenance')}`];
    });
    const alerts=(data.recentAlerts||[]).map(a=>[date(a.time),link('alert-detail.html',a.id,a.id),tag(a.status),shown(a.diagnosis?.model),number(a.diagnosis?.llmCallCount,0),tag(a.applicationStatus||'not_applied'),percent(a.comparison?.tuned_improvement_ratio),shown(a.message||a.error||'No workflow error')]);
    return `<div class="ki-stats ki-overview-stats">${Object.entries(data.counts||{}).map(([k,v])=>`<div class="ki-stat"><strong>${v}</strong><span>${esc(k.replaceAll('_',' '))}</span></div>`).join('')}</div>`+
      panel('Automatic workflow configuration',kv([['Model',shown(auto.model)],['Current alert',tag(auto.currentAlertFiring?'firing':'inactive')],['Collector',shown(auto.collectionError||'Healthy')]])+'<p class="ki-sub">System-parameter tuning runs through the SysInsight alert workflow.</p>')+
      panel('Workflow outcomes',table(['Workflow / actions','Running','Completed','Failed','Skipped'],summaries)+'<p class="ki-sub">Completed means processing finished, not guaranteed benefit.</p>')+
      panel('Tuning history (recent experiments)',table(['Time','Scenario','State / phase','Measured result','Tuning source','Details / evidence'],experiments))+
      panel('SysInsight live alert workflows',table(['Time','Alert','State','Model','LLM calls','Restoration','Time reduction','Message'],alerts))+
      panel('Data coverage and known limits',coverage(data.capabilities||[]))+
      panel('Data categories',table(['Category','Page','Stored data'],data.schema.categories.map(r=>[esc(r.name),`<a href="${esc(r.page)}">${esc(r.page)}</a>`,esc(r.description)])));
  }

  function render(data) {
    const body=document.getElementById('ki-body'),items=data.items||[];
    if(detail){
      document.getElementById('detailContent').innerHTML=alertDetail(data);
      const title=document.getElementById('pageTitle');title.textContent='Alert Details · '+data.id.slice(0,36);title.title=data.id;
      const comparison=data.detailAnalysis?.comparison;
      if(comparison&&!(comparison.tuned_ms<comparison.baseline_ms))body.querySelectorAll('.tuning-metric-value.success').forEach(n=>n.classList.add('ki-neutral-result'));
      body.querySelectorAll('[data-ki-knowledge]').forEach(n=>n.onclick=()=>showModal('Related Knowledge Base',code(data.detailAnalysis?.functionCodeExamples||[])));
      body.querySelectorAll('[data-ki-function]').forEach(n=>{
        const open=()=>showFunctionDetail(n.dataset.kiFunction,data);
        n.onclick=open;
        n.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open();}};
      });
      return;
    }
    let rows=[],target;
    const cells=values=>'<tr>'+values.map(v=>`<td>${v??'—'}</td>`).join('')+'</tr>';
    const evidenceButton=(r,label='Details')=>`<button class="action-btn" data-ki-record="${esc(r.id)}">${label}</button>`;
    switch(config[1]){
      case 'hosts':
        target='hostTableBody';
        rows=items.map(h=>['<div class="checkbox-wrapper"><input type="checkbox" disabled aria-label="Host selection unavailable"></div>',`<div class="host-name"><div class="host-name-main">${esc(h.name)}</div><div class="host-name-sub">${esc(h.id)}</div></div>`,`<span class="ip-address">${esc(h.ip)}:${h.port}</span>`,`<span class="status-tag ${h.status==='online'?'status-running':'status-warning'}">${esc(h.status)}</span>${h.collectionError?`<div class="ki-row-meta">${esc(h.collectionError)}</div>`:''}`,`<span class="db-tag">${esc(h.dbType)}</span><div class="ki-row-meta">${esc(h.database)}</div>`,...['cpuPercent','memoryPercent','diskPercent'].map(key=>usage(h.utilization?.[key],h.utilization)),'<span class="region-tag">Not configured</span>',h.createdAt?date(h.createdAt):'<span class="ki-row-meta" title="Host creation time is not collected; the refresh timestamp is not a creation date">Not recorded</span>',`<div class="action-buttons">${evidenceButton(h)}<a class="action-btn" href="alert-list.html">Alerts</a><a class="action-btn" href="http://123.56.83.97:8002/" target="_blank" rel="noopener">Grafana</a></div>`]);break;
      case 'alerts':
        target='alertTableBody';rows=items.map(a=>[tag(a.severity),`<span class="alert-id">${esc(a.id)}</span>`,date(a.time),`<span class="alert-host">${esc(a.host)}</span>`,`<div class="alert-reason">${esc(a.reason)}</div><div class="ki-row-meta">${shown(a.message||a.error||'No workflow error')}</div>`,`${tag(a.rawStatus)}<div class="ki-row-meta">${tag(a.applicationStatus||'not_applied')}</div>`,link('alert-detail.html',a.id,'View Details')]);
        {const select=document.getElementById('reasonFilter'),selected=select.value;select.innerHTML='<option value="">All</option>'+[...new Set([...items.map(a=>a.reason),selected].filter(Boolean))].map(r=>`<option value="${esc(r)}">${esc(r)}</option>`).join('');select.value=selected;}break;
      case 'manual':
        document.getElementById('snippetsContainer').innerHTML=items.length?`<table class="snippet-list"><tbody>${items.map(r=>cells([`<div class="snippet-source">${esc(r.source)}</div>`,`<div class="snippet-title">${esc(r.title)}</div><div class="snippet-text">${esc(r.content||r.description)}</div><div class="snippet-meta"><div class="snippet-tags">${[r.type,r.context,r.restart?'restart required':'no server restart'].filter(Boolean).map(x=>`<span class="snippet-tag">${esc(x)}</span>`).join('')}</div><div class="snippet-time">Default: ${r.default===''?'(empty string)':shown(r.default)} ${esc(r.unit||'')}</div></div>`,evidenceButton(r)])).join('')}</tbody></table>`:empty();
        document.querySelectorAll('#snippetsContainer tr').forEach(tr=>['snippet-source-col','snippet-content-col','snippet-action-col'].forEach((c,i)=>tr.cells[i].className=c));break;
      case 'code':
        target='functionTableBody';rows=items.map(r=>{
          const control=r.controlFlowFunctions||[],data=r.dataFlowFunctions||[],flow=[];
          if(control.length)flow.push('控制流：'+control.join(', '));
          if(data.length)flow.push('数据流：'+data.join(', '));
          return [esc(r.id),`<strong>${esc(r.name)}</strong><div class="ki-row-meta">${esc(r.file)}:${r.line}</div>`,`<span class="db-badge pg">PostgreSQL</span>`,`<div class="code-block">${esc(flow.length?flow.join('；'):(r.functionFlow||'—'))}</div>`,`<div class="code-block">${esc(r.dataFlow||'—')} → ${esc(r.description||'—')}</div>${r.codeSnippet?`<div class="ki-row-meta">${esc(r.codeSnippet)}</div>`:''}`,evidenceButton(r)];
        });break;
      case 'alertRules': {
        const metricLabels={'target_business_tps':'目标业务 TPS'};
        target='ruleTableBody';
        rows=items.map(r=>[
          `<div class="rule-name">${esc(r.name)}</div>`,
          esc(metricLabels[r.metric]||r.metric||'—'),
          r.dropPercent==null?'—':`下降 ${number(r.dropPercent,0)}%`,
          r.period==null?'—':`${number(r.period,0)} 秒`,
          `${tag(r.state)} ${tag(r.health)}`,
          `<button class="action-btn" data-ki-rule-detail="${esc(r.id)}">详情</button>`
        ]);
        break;
      }
      case 'overview':
        document.getElementById('overviewContent').innerHTML=overview(data);break;
    }
    if(target){
      const tbody=document.getElementById(target),table=tbody.closest('table');table.classList.add('ki-live-table');
      tbody.innerHTML=rows.length?rows.map(cells).join(''):`<tr><td colspan="${table.querySelectorAll('thead th').length}">${empty()}</td></tr>`;
    }
    body.querySelectorAll('[data-ki-record]').forEach(n=>n.onclick=()=>{const item=items.find(x=>String(x.id)===n.dataset.kiRecord);if(item)showModal(item.title||item.name||item.id,code(item));});
    body.querySelectorAll('[data-ki-run-detail]').forEach(n=>n.onclick=()=>showRunDetail(n.dataset.kiRunDetail,n.dataset.kiRunIncidents));
    body.querySelectorAll('[data-ki-rule-detail]').forEach(n=>n.onclick=()=>{
      const r=items.find(x=>String(x.id)===n.dataset.kiRuleDetail);
      if(!r)return;
      const metricLabels={'target_business_tps':'目标业务 TPS'};
      const metric=metricLabels[r.metric]||r.metric||'—';
      const condition=r.dropPercent==null?'—':`下降 ${number(r.dropPercent,0)}%`;
      showModal('规则详情',`<dl class="ki-kv"><dt>指标</dt><dd>${esc(metric)}</dd><dt>触发条件</dt><dd>${esc(condition)}</dd><dt>持续时间</dt><dd>${r.period==null?'—':number(r.period,0)+' 秒'}</dd><dt>状态</dt><dd>${tag(r.state)} ${tag(r.health)}</dd></dl>`+raw({query:r.alertField,summary:r.reason,description:r.desc},'原始规则'));
    });
    total=data.total||0;
    if(config[1]==='overview')return;
    let pager=body.querySelector('.table-footer,.data-container>.pagination,.table-container>.pagination,#ki-pager');
    if(!pager){pager=document.createElement('div');pager.id='ki-pager';body.appendChild(pager);}
    const pageNumber=Math.floor(offset/limit)+1;
    pager.innerHTML=`<div class="pagination ki-live-pagination"><span class="page-info">Total <span id="totalCount">${total}</span> records · ${total?offset+1:0}–${Math.min(offset+items.length,total)}</span><div><button class="page-btn" id="ki-prev" ${offset===0?'disabled':''}>Previous</button><button class="page-btn active" disabled>${pageNumber}</button><button class="page-btn" id="ki-next" ${offset+limit>=total?'disabled':''}>Next</button></div></div>`;
    document.getElementById('ki-prev').onclick=()=>{offset=Math.max(0,offset-limit);refresh(true);};
    document.getElementById('ki-next').onclick=()=>{offset+=limit;refresh(true);};
  }

  const modal=document.createElement('div');modal.className='ki-modal';modal.setAttribute('role','dialog');modal.setAttribute('aria-modal','true');modal.setAttribute('aria-label','Evidence details');
  modal.innerHTML='<div class="ki-modal-card"><div class="ki-modal-header"><span id="ki-modal-title"></span><button class="ki-button" id="ki-modal-close" aria-label="Close">×</button></div><div id="ki-modal-body"></div></div>';document.body.appendChild(modal);
  let modalFocus=null;
  function hideModal(){modal.classList.remove('show');modalFocus?.focus();}
  function showModal(title,html){modalFocus=document.activeElement;document.getElementById('ki-modal-title').textContent=title;document.getElementById('ki-modal-body').innerHTML=html;modal.classList.add('show');document.getElementById('ki-modal-close').focus();}
  document.getElementById('ki-modal-close').onclick=hideModal;modal.onclick=e=>{if(e.target===modal)hideModal();};
  document.addEventListener('keydown',e=>{if(e.key==='Escape')hideModal();if(e.key==='Tab'&&modal.classList.contains('show')){const nodes=[...modal.querySelectorAll('button,a[href],input,select,[tabindex="0"]')];if(e.shiftKey&&document.activeElement===nodes[0]){e.preventDefault();nodes.at(-1).focus();}else if(!e.shiftKey&&document.activeElement===nodes.at(-1)){e.preventDefault();nodes[0].focus();}}});

  async function refresh(replace=false) {
    if(busy&&!replace)return;
    controller?.abort();controller=new AbortController();const local=controller;
    busy=true;document.getElementById('ki-refresh').disabled=true;
    const timeout=setTimeout(()=>local.abort(),30000);
    try {
      const id=new URLSearchParams(location.search).get('id');
      if(detail&&!id)throw new Error('Missing record ID');
      const resource=config[1]+(detail?'/'+encodeURIComponent(id):'');
      const query=new URLSearchParams({limit:String(limit),offset:String(offset),q:search,kind,...filters});
      const response=await fetch(API+resource+'?'+query,{cache:'no-store',signal:local.signal});
      const data=await response.json();
      if(!response.ok)throw new Error(data.error||`HTTP ${response.status}`);
      if(local!==controller)return;
      current=data;render(data);document.getElementById('ki-updated').textContent='Updated '+new Date(data.updatedAt).toLocaleTimeString();
      document.getElementById('ki-error').className='ki-banner';
    } catch(e) {
      if(local!==controller)return;
      const banner=document.getElementById('ki-error');banner.textContent=`Refresh failed: ${e.name==='AbortError'?'request timed out':e.message}. ${current?'Showing previous data.':'No live data loaded.'}`;banner.className='ki-banner show';
      document.getElementById('ki-updated').textContent='Disconnected';
    } finally {
      clearTimeout(timeout);
      if(local===controller){busy=false;document.getElementById('ki-refresh').disabled=false;}
    }
  }
  document.getElementById('ki-refresh').onclick=()=>refresh(true);
  document.getElementById('ki-export').onclick=()=>{
    if(!current)return;
    const url=URL.createObjectURL(new Blob([JSON.stringify(current,null,2)],{type:'application/json'}));
    const a=document.createElement('a');a.href=url;a.download=`keeninsight-${config[1]}-${Date.now()}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  setInterval(()=>{if(!document.hidden&&!document.activeElement?.matches('input,select')&&!document.querySelector('details[open]')&&!modal.classList.contains('show'))refresh();},10000);
  refresh();
})();
