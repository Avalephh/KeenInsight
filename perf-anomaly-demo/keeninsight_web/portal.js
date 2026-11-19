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
      filterControl('dbFilter','db');filterControl('categoryFilter','category',[['','All'],...['control_flow','data_flow'].map(x=>[x,x])]);
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
      </div>${raw(data,'Full alert record / export schema')}`;
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
    const hot=(d.hotFunctions||[]).map(f=>{
      const source=(d.functionCodeExamples||[]).find(s=>s.function===f.Function);
      return `<div class="function-item"><div class="function-header"><div class="function-name">${esc(f.Function)}</div></div><div class="function-info-row"><div class="function-meta">${source?esc(source.file)+':'+source.line:'Source location not retained for this function'}</div><div class="function-sources"><span class="source-tag">perf differential sampling</span></div></div></div>`;
    }).join('')||empty('No measured hot functions retained.');
    const knobs=Object.entries(app.requested_configuration||d.knobs?.selected_candidate?.configuration||{}).map(([name,value])=>{
      const setting=app.pre_settings?.[name],skipped=app.skipped_configuration?.[name];
      return `<div class="knob-item"><div class="knob-name">${esc(name)}<div class="ki-row-meta">${esc(skipped?'Not applied: '+skipped.reason:app.status||'Candidate only')}</div></div><div class="knob-value"><span class="knob-current">${shown(setting?.setting)}</span><span class="knob-arrow">→</span><span class="knob-recommended">${shown(value)} ${esc(setting?.unit||'')}</span></div></div>`;
    }).join('')||empty('No candidate parameters recorded.');
    const s=d.shapAnalysis;
    let shap=empty(s?.reason||'No SHAP evidence retained for this alert. New completed captures run the local model; historical differential rates are not substituted.');
    if(s?.status==='completed'&&s.features?.length){
      const max=Math.max(...s.features.map(f=>Math.abs(f.shapValue)),1e-12);
      const trust=s.reliability||{status:'unverified',decisionEligible:false,reasons:['Historical evidence has no explicit reliability assessment; no claim of retrospective prompt exclusion.']};
      const trustLabel=!s.reliability?'Historical evidence · trust not assessed':trust.decisionEligible===true?'Experimental advisory':'Low confidence · excluded from LLM tuning advice';
      const independent=s.independentValidation?`<div>Latest independent time-batch MAE: ${number(s.independentValidation.maePercentagePoints)} pp · R² ${number(s.independentValidation.r2,3)} · ${number(s.independentValidation.rows,0)} held-out measurements</div>`:'';
      shap=`<div class="ki-shap-summary"><strong>Experimental model · foreground TPS retention</strong><div class="ki-shap-trust" role="note"><strong>${trustLabel}</strong><br>${esc((trust.reasons||[]).join(' '))}</div><div>Background ${number(s.baseValue*100)}% → prediction ${number(s.prediction*100)}%</div><div>Historical validation MAE: ${number(s.validation?.maePercentagePoints)} pp · R² ${number(s.validation?.r2,3)} · ${s.backgroundRows} measured training cases</div>${independent}<div class="ki-row-meta">${esc(s.validation?.method)}<br>${esc(s.modelVersion)} · ${esc(s.domain?.status)} · additivity residual ${Number(s.additivityResidual).toExponential(2)}</div><p class="ki-sample-note">Green increases the model prediction; red decreases it. Units: percentage points (pp), not measured TPS gains or causal parameter effects.</p></div>`+s.features.map(f=>{
        const positive=f.shapValue>=0,width=Math.abs(f.shapValue)/max*50;
        return `<div class="shap-feature"><div class="shap-name" title="${esc((f.parameters||[]).join(', '))}">${esc(f.name)}</div><div class="shap-value" title="Inclusive sampled function rate, not CPU utilization">${f.isAggregate?'aggregate':number(f.value)}</div><div class="shap-bar ki-shap-diverging"><div class="shap-bar-fill ${positive?'low':'high'}" style="position:absolute;left:${positive?50:50-width}%;width:${width}%"></div></div><div class="shap-impact" style="color:${positive?'#008943':'#c62c20'}">${positive?'+':''}${number(f.shapValue*100,3)} pp</div></div>`;
      }).join('');
    }
    return window.KeenInsightLayouts.alert({rules,profiling,shap,functions:hot,knobs,explanation:shown(data.diagnosis?.explanation||data.message||'No root-cause explanation retained.'),beforeTuning_latency:c.baseline_ms==null?'Not measured':number(c.baseline_ms)+' ms',afterTuning_latency:c.tuned_ms==null?'Not measured':number(c.tuned_ms)+' ms',beforeTuning_restored:'—',afterTuning_restored:c.restored_ms==null?'Not measured':number(c.restored_ms)+' ms',beforeTuning_state:esc(c.metric||'No validation'),afterTuning_state:tag(app.status||'not_applied')})+`<p class="ki-sample-note">Measured SQL replay comparison, not a predicted TPS / CPU improvement. Workflow completion does not mean the alert resolved.</p><details class="ki-evidence"><summary>Workflow evidence · runtime call chains, LLM tuning, apply, retest and restoration</summary><div class="ki-evidence-body">${alertEvidence(data)}</div></details>`;
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
        target='functionTableBody';rows=items.map(r=>[esc(r.id),`<strong>${esc(r.name)}</strong><div class="ki-row-meta">${esc(r.file)}:${r.line}</div>`,`<span class="db-badge pg">PostgreSQL</span>`,shown(r.category),`<div class="code-block">${esc(r.functionFlow||'Runtime call chains are retained per alert, not this static reference.')}</div>`,`<div class="code-block">${esc(r.dataFlow||'Not retained')} → ${esc(r.description)}</div>`,evidenceButton(r)]);break;
      case 'alertRules':
        target='ruleTableBody';rows=items.map(r=>[`<div class="rule-name">${esc(r.name)}</div><div class="rule-desc">${shown(r.reason)}</div>`,shown(r.desc),`<code class="ki-flow">${esc(r.alertField)}</code>`,'In expression',number(r.period)+' s',tag(r.state)+' '+tag(r.health),evidenceButton(r)]);break;
      case 'overview':
        document.getElementById('overviewContent').innerHTML=overview(data);break;
    }
    if(target){
      const tbody=document.getElementById(target),table=tbody.closest('table');table.classList.add('ki-live-table');
      tbody.innerHTML=rows.length?rows.map(cells).join(''):`<tr><td colspan="${table.querySelectorAll('thead th').length}">${empty()}</td></tr>`;
    }
    body.querySelectorAll('[data-ki-record]').forEach(n=>n.onclick=()=>{const item=items.find(x=>String(x.id)===n.dataset.kiRecord);if(item)showModal(item.title||item.name||item.id,code(item));});
    body.querySelectorAll('[data-ki-run-detail]').forEach(n=>n.onclick=()=>showRunDetail(n.dataset.kiRunDetail,n.dataset.kiRunIncidents));
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
    const timeout=setTimeout(()=>local.abort(),15000);
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
