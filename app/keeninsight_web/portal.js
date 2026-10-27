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
    'sql-log.html': ['SQL Logs', 'sql'],
    'sql-detail.html': ['SQL Detail', 'sql'],
    'dba-rules.html': ['Diagnosis Rules', 'diagnosisRules'],
    'manual.html': ['Manual & Parameter Reference', 'manual'],
    'code-source.html': ['Code Source', 'code'],
    'tuning-knowledge.html': ['Tuning-derived Knowledge', 'knowledge'],
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
  const jobLink = (key,id,label) => `<a class="ki-detail-link" href="sql-detail.html?id=${encodeURIComponent(key)}&job_id=${encodeURIComponent(id)}">${esc(label)}</a>`;
  const percent = value => value==null?'Not measured':number(value*100)+'%';
  const stages = rows => table(['Stage','State','Started / recorded','Duration (ms)'],(rows||[]).map(s=>[esc(s.name||s.phase),tag(s.status),date(s.startedAt||s.recordedAt),number(s.durationMs)]));
  const timeline = rows => table(['Phase','Started','Duration (ms)','Method / source','State'],(rows||[]).map(s=>[s.executionId?link('sql-detail.html',s.executionId,s.phase):esc(s.phase),date(s.startedAt),number(s.durationMs),shown(s.method||s.source),tag(s.status)]));
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
  const back=header.querySelector('.back-btn');if(back){actions.prepend(back);back.onclick=()=>location.href=config[1]==='sql'?'sql-log.html':'alert-list.html';}
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
    if(config[1]==='sql'){
      filterArea.insertAdjacentHTML('beforeend','<div class="filter-item"><span class="filter-label">Record:</span><select class="filter-select" id="ki-kind"><option value="all">All · executions first</option><option value="execution">Individual executions</option><option value="observation">Fingerprint statistics</option></select></div>');
      document.getElementById('ki-kind').onchange=e=>{kind=e.target.value;offset=0;refresh(true);};
      filterControl('statusFilter','result',[['','All'],['completed','Completed'],['failed','Failed'],['observed','Observed']]);
      filterControl('timeFilter','duration');filterControl('typeFilter','type');
    }else if(config[1]==='alerts'){
      filterControl('severityFilter','severity');filterControl('statusFilter','status',[['','All'],...['running','completed','failed','skipped'].map(x=>[x,x])]);
      // Search now matches alert name, ID, host and trigger description.
      document.getElementById('ki-search').placeholder='Host, alert ID or trigger reason';
      document.getElementById('ki-search').previousElementSibling.textContent='Keyword:';
      filterControl('reasonFilter','reason');
    }else if(config[1]==='code'){
      filterControl('dbFilter','db');filterControl('categoryFilter','category',[['','All'],...['control_flow','data_flow'].map(x=>[x,x])]);
    }else if(config[1]==='diagnosisRules'){
      filterControl('categoryFilter','category',[['','All'],['SQL rewrite','SQL rewrite']]);
      filterControl('statusFilter','status',[['','All'],['available','Available']]);
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

  function sqlEvidence(data) {
    const d=data.diagnosis_results||{},t=data.tuning_results||{},a=data.availability||{};
    const before=t.old_time,after=t.new_time,max=Math.max(before||0,after||0,1);
    const bars=(before!=null&&after!=null)?[['Original',before,'#86909c'],['Optimized',after,'#3370ff']].map(([label,val,color])=>`<div class="ki-bar-label"><span>${label}</span><strong>${number(val)} s</strong></div><div class="ki-bar"><div style="width:${Math.max(0,Math.min(100,val/max*100))}%;background:${color}"></div></div>`).join('')+`<p>Time reduction: <strong>${number(t.improvement_ratio==null?null:t.improvement_ratio*100)}%</strong></p>`:empty('No measured before/after pair recorded');
    const active=data.activeApplication;
    const history=(data.jobHistory||[]).map(j=>[jobLink(data.sqlKey,j.job_id,j.job_id),date(j.requested_at),tag(j.status),shown(j.error)]);
    return `<div class="ki-breadcrumb"><a href="template.html">Overview</a> / <a href="sql-log.html">SQL Logs</a> / ${esc(data.id)}</div>
      <div class="ki-stats"><div class="ki-stat"><strong>${number(data.executionTime)} <small>ms</small></strong><span>${data.kind==='observation'?'Mean per fingerprint call':'Measured execution time'}</span></div><div class="ki-stat"><strong>${number(data.calls,0)}</strong><span>Calls</span></div><div class="ki-stat"><strong>${tag(data.jobStatus||'not_requested')}</strong><span>DREAM job</span></div><div class="ki-stat"><strong>${number(active?.hitCount??t.hitCount,0)}</strong><span>Automatic application hits</span></div></div>
      <div class="ki-grid">${panel('SQL',code(data.sql)+kv([['Database',shown(data.database)],['Source',shown(data.source)],['Record kind',shown(data.kind)],['Status',tag(data.result)],['Execution error',shown(data.execution?.error||'None')]]))}
      ${panel('Measured optimization result',bars+kv([['Candidate status',tag(t.status||'no_candidate')],['Apply kind / scope',shown([t.applyKind,t.applyScope].filter(Boolean).join(' / '))],['Model',shown(data.job?.model||'No retained model evidence')],['Validation result',shown(t.message||'No validation result retained')],['Benchmark provenance',shown(t.baselineProvenance)]]))}
      ${panel('Diagnosis',kv([['Root causes',esc((d.root_causes||[]).join(', '))||'Not diagnosed'],['Explanation',shown(d.explanation||'No explanation returned')],['Confidence',Object.keys(d.confidence||{}).length?code(d.confidence):empty(a.confidence)],['Job',shown(data.job?.id||'Not submitted')],['History availability',shown(data.job?.historyNote||'Within retained task history')],['Trigger',shown(data.job?.triggerSource||'Historical job did not retain trigger provenance')],['Trigger completion time',data.job?.sourceCompletionMs!=null?number(data.job.sourceCompletionMs)+' ms':'Not retained / manual baseline'],['Job message',shown(data.job?.error||'No job error')]])+(data.diagnosisTrace?raw(data.diagnosisTrace,'Actual LLM diagnosis trace'):empty('Historical job has no raw diagnosis trace')))}
      ${panel('Tuning action',(t.fix_action?code(t.fix_action):'')+(t.rewrite_sql?code(t.rewrite_sql):'')+(!t.fix_action&&!t.rewrite_sql?empty('No executable action returned; see diagnosis and validation reason'):'')+(t.action_explanation?raw(t.action_explanation,'Action explanation'):'')+raw(t.validation,'Application checks'))}
      ${panel('Automatic application',active?kv([['Published action',shown(active.id)],['State',tag(active.status)],['Scope',shown(active.applyScope)],['Kind',shown(active.applyKind)],['Observed hits',number(active.hitCount,0)]])+raw(active.validation,'Publication evidence'):empty('No active action. A completed diagnosis or an unvalidated candidate is not automatically applied.'))}
      ${panel('Rewrite semantic checks',data.evaluation?.semantic_validation?code(data.evaluation.semantic_validation)+'<p class="ki-sub">QED outcome and result-set comparisons are recorded separately. Matching the current dataset alone is not a proof for all future data.</p>':empty('No semantic-check trace was retained for this historical result, or no rewrite was evaluated.'))}
      ${panel('Execution plan',data.plan?code(data.plan):empty(a.plan))}
      ${panel('Rule evidence',(d.rule_matches_detail?.length?'<p class="ki-sub">Structural candidates actually supplied to the action LLM; not claims that each transformation was applied.</p>'+code(d.rule_matches_detail):empty('This planner did not emit rule-level match evidence. The rule library is available; listed rules are not claimed as executed.'))+(data.actionTrace?raw(data.actionTrace,'Actual action LLM input / response'):''))}
      ${panel('SQL lifecycle',Array.isArray(data.lifecycle)?timeline(data.lifecycle):data.lifecycle?code(data.lifecycle):empty(a.lifecycle))}
      ${panel('DREAM job history',history.length?table(['Job','Requested','State','Message'],history):empty('No DREAM job has been submitted for this SQL'))}
      ${panel('SQL-scoped profiling',data.flameGraph?code(data.flameGraph):empty((a.flameGraph||'Not collected')+'. SQL-internal CPU and memory phase attribution is not instrumented; the lifecycle above uses real execution/job timestamps.'))}
      </div>${raw(data,'Full SQL record / export schema')}`;
  }

  function alertEvidence(data) {
    const d=data.detailAnalysis||{};
    const c=d.comparison||{},diag=data.diagnosis||{},perf=d.perfCapture||{};
    return `<div class="ki-breadcrumb"><a href="template.html">Overview</a> / <a href="alert-list.html">Alert List</a> / ${esc(data.id)}</div><div class="ki-grid">
      ${panel('Alert',kv([['Reason',shown(data.reason)],['Time',date(data.time)],['Host',shown(data.host)],['Severity',tag(data.severity)],['Workflow status',tag(data.rawStatus)],['Trigger state (historical)',tag(data.triggerState||'not_retained')],['Duration',number(data.durationMs)+' ms'],['Message',shown(data.message||data.error||'No workflow error')]])+raw(d.triggeredRule,'Trigger evidence')+'<p class="ki-sub">Workflow completion does not mean the alert resolved or performance improved.</p>')}
      ${panel('Diagnosis',kv([['Model',shown(diag.model||'Not yet called')],['Real LLM calls',number(diag.llmCallCount,0)],['Candidates generated',number(diag.candidateCount,0)],['Differential functions',number(diag.hotFunctionCount,0)],['Hot functions',shown((data.rootCause||[]).join(', ')||'Not yet available')],['Meaning',shown(diag.explanation)]])+raw(diag.matchedParameters||[],'Matched source parameters'))}
      ${panel('Backend capture',kv([['Selection',shown(d.backendSnapshot?.selection||'Historical active PID capture')],['Capture started',date(perf.perf?.requested_at)],['Capture status',tag(perf.perf?.status||'not_captured')],['Postprocess',tag(perf.postprocess?.status||'not_captured')],['Function records',number(perf.postprocess?.function_count,0)]])+raw(d.backendSnapshot||{},'Backend snapshot'))}
      ${panel('Measured apply / retest / restore',kv([['Validation metric',shown(c.metric||'No comparison completed')],['Baseline',number(c.baseline_ms)+' ms'],['Tuned',number(c.tuned_ms)+' ms'],['Restored',number(c.restored_ms)+' ms'],['Measured time reduction',percent(c.tuned_improvement_ratio)],['Restoration',tag(d.application?.status||'not_applied')]])+raw(d.validationSql||{},'Actual replay SQL')+raw(d.application,'Application and restoration'))}
      ${panel('Workflow stages',stages(d.stages))}
      ${panel('Differential profiling',d.differentialProfiling?code(d.differentialProfiling):empty(data.availability?.profiling))}
      ${panel('Runtime call chains',d.runtimeCallChains?.length?code(d.runtimeCallChains):empty(data.message||'No runtime stack evidence retained yet'))}
      ${panel('Source / parameter evidence',d.functionCodeExamples?.length?code(d.functionCodeExamples):empty('No source evidence retained yet'))}
      ${panel('Candidate parameters',Object.keys(d.knobs||{}).length?code(d.knobs):empty('No candidate recorded yet'))}
      ${panel('SHAP model evidence',d.shapAnalysis?raw(d.shapAnalysis,'Model, training validation, exact SHAP values and additivity check'):empty(data.availability?.shapAnalysis))}
      ${panel('Metric evidence',raw(d.runtimeData||{},'Measured host / database window')+'<p class="ki-sub">Time-series monitoring remains in Grafana. Differential profiling and model SHAP attribution are distinct evidence sources.</p>')}
      </div>${raw(data,'Full alert record / export schema')}`;
  }

  function flame(data,path=[]) {
    if(!data||!Number.isFinite(data.value)||data.value<=0)return empty('SQL-specific flame graph not collected. Alert-level stacks are not attributed to this SQL.');
    let focus=data;
    for(const index of path){if(!Number.isInteger(index)||!focus.children?.[index]){focus=data;path=[];break;}focus=focus.children[index];}
    const levels=[];
    function walk(node,depth,start,nodePath){
      if(depth>100||!Number.isFinite(node.value)||node.value<0)return;
      const width=Math.max(0,Math.min(100-start,node.value/focus.value*100));
      (levels[depth]??=[]).push(`<div class="flame-bar" role="button" tabindex="0" data-ki-flame-path="${esc(JSON.stringify(nodePath))}" style="position:absolute;left:${start}%;width:${width}%;background:hsl(${35-depth%5*5} 90% ${62-depth%4*4}%)" title="${esc(node.name)} · ${number(node.value)} samples · ${number(node.value/data.value*100,1)}% of sampled CPU events"><span class="flame-bar-text">${esc(node.name)}</span></div>`);
      let next=start;for(const [index,child] of (node.children||[]).entries()){walk(child,depth+1,next,nodePath.concat(index));next+=Math.max(0,Number(child.value)||0)/focus.value*100;}
    }
    walk(focus,0,0,path);
    return '<div class="ki-flame-wrapper"><div class="ki-flame-tools"><span class="ki-sample-note">'+number(data.value,0)+' real CPU samples · hover for details · click a frame to zoom (not a time axis)</span><button class="ki-button" data-ki-flame-reset>Reset zoom</button></div><div class="flame-graph">'+levels.reverse().map(row=>`<div class="flame-level" style="position:relative;height:24px">${row.join('')}</div>`).join('')+'</div></div>';
  }
  function sqlDetail(data){
    const d=data.diagnosis_results||{},t=data.tuning_results||{};
    const rules=(d.rule_matches_detail||[]).map(r=>`<div class="rule-match-item"><div class="rule-principle">${esc(r.Principle||r.name||r.conditions||JSON.stringify(r))}</div></div>`).join('')||empty('No rule-level match evidence retained.');
    const scores=Object.entries(d.confidence||{}).map(([name,value])=>`<div class="root-cause-item"><div class="root-cause-name">${esc(name)}</div><div class="root-cause-confidence">Model score: <span class="confidence-value">${number(value,4)}</span></div></div>`).join('')||empty('No model scores recorded.');
    const life=Array.isArray(data.lifecycle)?`<div class="ki-lifecycle"><div class="lifecycle-stages">${data.lifecycle.map(s=>`<div class="lifecycle-stage"><div class="lifecycle-name">${esc(s.phase)}</div><div class="lifecycle-metrics"><div class="lifecycle-metric"><strong>${number(s.durationMs)} ms</strong> elapsed</div><div class="ki-row-meta">${shown(s.method||s.source)} · ${shown(s.status)}</div></div></div>`).join('')}</div><p class="ki-sample-note">Measured execution / job lifecycle. Parsing, network, CPU and memory phases are not separately instrumented.</p></div>`:'';
    const profile=data.sqlProfile;
    const participants=(profile?.participants||[]).map(p=>`<span class="ki-row-meta">${esc(p.role)} PID ${number(p.pid,0)}: ${number(p.sampleCount,0)} samples</span>`).join('<br>');
    const profileInfo=profile?`<p class="ki-sample-note">${tag(profile.status)} · Profile ${esc(profile.profileId)} · PID ${number(profile.pid,0)} · ${number(profile.frequencyHz,0)} Hz · validated ${number(profile.captureSeconds,3)} s<br>${profile.executionId?'Exact execution: '+esc(profile.executionId):'Latest sampled execution of this fingerprint; not the aggregate of all calls'} · ${date(profile.startedAt||profile.requestedAt)}<br>${esc(profile.scope)} · ${esc(profile.parallelCoverage)}<br>${esc(profile.reason||'Partial on-CPU sample window; does not measure I/O or lock waits.')}${participants?'<br>'+participants:''}</p>`:'';
    const values={sql:esc(data.sql),time:date(data.time),type:shown(data.type),rules,scores,causes:esc((d.root_causes||[]).join(', ')||'Not diagnosed'),diagnosis:shown(d.explanation||data.job?.error||'No diagnosis explanation retained.'),explanation:`<div style="padding:12px;background:#f7f8fa;border-radius:6px;font-size:13px;line-height:1.8">${shown(t.action_explanation||d.explanation||'No explanation returned.')}</div>`,flame:profileInfo+flame(data.flameGraph)+life,rewrite:esc(t.rewrite_sql||t.fix_action||'No executable tuning action retained.'),before:t.old_time==null?'Not measured':number(t.old_time,3)+' s',after:t.new_time==null?'Not measured':number(t.new_time,3)+' s'};
    return window.KeenInsightLayouts.sql(values)+`<p class="ki-sample-note">${tag(t.status||'no_candidate')} · ${tag(data.activeApplication?.status||'not_published')} · Model: ${shown(data.job?.model)} · Time reduction: ${percent(t.improvement_ratio)}. Candidate evaluation is distinct from automatic replay.</p><details class="ki-evidence"><summary>Workflow evidence · automatic application, execution plan and LLM traces</summary><div class="ki-evidence-body">${sqlEvidence(data)}</div></details>`;
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

  function overview(data) {
    const auto=data.automation||{};
    const summaries=Object.entries(data.workflowCounts||{}).map(([name,values])=>[esc(name.replaceAll('_',' ')),...['running','completed','failed','skipped','active','candidate','no_action'].map(state=>number(values[state]||0,0))]);
    const experiments=(data.recentRuns||[]).map(r=>{
      const c=r.comparison||{},tp=r.kind==='sysinsight';
      const measurement=tp?`${number(c.baseline_tps)} → ${number(c.pressure_before_tps)} → ${number(c.pressure_after_tps)} TPS`:`${number(c.baseline_ms)} → ${number(c.optimized_ms)} ms`;
      const gain=tp?c.tuning_gain_ratio:c.improvement_ratio;
      return [date(r.requested_at),`${esc(r.title||r.scenario_id)}<div class="ki-sub">${esc(r.run_id)}</div>`,`${tag(r.status)} ${tag(r.phase)}`,measurement+`<div class="ki-sub">${tp?'Normal → pressure → tuned under pressure':'Original → automatic replay'} · change ${percent(gain)}</div>`,tp?`${tag(r.tuningStatus||'awaiting_result')}<div class="ki-sub">${esc(r.candidateSourceType||'No candidate source')}</div>`:r.sqlKey?link('sql-detail.html',r.sqlKey,'SQL result'):'Waiting for original SQL',`${(r.incidentIds||[]).map(id=>link('alert-detail.html',id,'Alert')).join(' · ')}${raw(r,'Experiment evidence / provenance')}`];
    });
    const alerts=(data.recentAlerts||[]).map(a=>[date(a.time),link('alert-detail.html',a.id,a.id),tag(a.status),shown(a.diagnosis?.model),number(a.diagnosis?.llmCallCount,0),tag(a.applicationStatus||'not_applied'),percent(a.comparison?.tuned_improvement_ratio),shown(a.message||a.error||'No workflow error')]);
    const jobs=(data.recentDreamJobs||[]).map(j=>[date(j.time),jobLink(j.sqlKey,j.id,j.queryId||j.id),`${tag(j.status)} ${tag(j.analysisOutcome||'pending')}<div class="ki-sub">${esc(j.triggerSource||'Historical trigger not retained')}</div>`,tag(j.applicationStatus||'not_published'),shown(j.model||'Not yet called'),`${number(j.oldTimeSeconds)} → ${number(j.newTimeSeconds)} s`,shown(j.message||'No job error')]);
    return `<div class="ki-stats ki-overview-stats">${Object.entries(data.counts||{}).map(([k,v])=>`<div class="ki-stat"><strong>${v}</strong><span>${esc(k.replaceAll('_',' '))}</span></div>`).join('')}</div>`+
      panel('Automatic workflow configuration',kv([['Model',shown(auto.model)],['DREAM auto trigger',`Completed AP SQL ≥ ${number(auto.dreamTriggerMs/1000)} s`],['DREAM gate',tag(auto.dreamGate?.status||'not_observed')],['Automatic apply',shown(auto.autoApplyEnabled?'Enabled':'Disabled')],['Current alert',tag(auto.currentAlertFiring?'firing':'inactive')],['Collector',shown(auto.collectionError||'Healthy')]])+'<p class="ki-sub">Manual experiments can explicitly request analysis below the automatic threshold. Rewrites apply to bridge-managed executions, not arbitrary external clients.</p>')+
      panel('Workflow outcomes',table(['Workflow / actions','Running','Completed','Failed','Skipped','Active','Candidate','No action'],summaries)+'<p class="ki-sub">Completed means processing finished, not guaranteed benefit. Candidate and no_action outcomes are not active improvements.</p>')+
      panel('Recent experiments',table(['Time','Scenario','State / phase','Measured result','Tuning source / SQL','Evidence'],experiments))+
      panel('SysInsight live alert workflows',table(['Time','Alert','State','Model','LLM calls','Restoration','Time reduction','Message'],alerts))+
      panel('DREAM diagnosis and application',table(['Time','SQL / exact job','State / outcome','Application','Model','Original → evaluated','Message'],jobs))+
      panel('Data coverage and known limits',coverage(data.capabilities||[]))+
      panel('Data categories',table(['Category','Page','Stored data'],data.schema.categories.map(r=>[esc(r.name),`<a href="${esc(r.page)}">${esc(r.page)}</a>`,esc(r.description)])));
  }

  function render(data) {
    const body=document.getElementById('ki-body'),items=data.items||[];
    if(detail){
      document.getElementById('detailContent').innerHTML=config[1]==='sql'?sqlDetail(data):alertDetail(data);
      const title=document.getElementById('pageTitle');title.textContent=(config[1]==='sql'?'SQL Detail':'Alert Details')+' · '+data.id.slice(0,36);title.title=data.id;
      body.querySelectorAll('.sql-statement').forEach(n=>n.classList.add('ki-detail-statement'));
      const t=data.tuning_results||{};
      if(t.old_time==null||t.new_time==null||t.new_time>=t.old_time)body.querySelectorAll('.bottom-section .info-value,.bottom-section [style*="success-color"]').forEach(n=>n.classList.add('ki-neutral-result'));
      const comparison=data.detailAnalysis?.comparison;
      if(comparison&&!(comparison.tuned_ms<comparison.baseline_ms))body.querySelectorAll('.tuning-metric-value.success').forEach(n=>n.classList.add('ki-neutral-result'));
      body.querySelectorAll('[data-ki-zoom]').forEach(n=>{if(!data.flameGraph){n.removeAttribute('style');if(n.tagName==='BUTTON')disabled(n,'SQL-specific stacks have not been captured');}else if(n.tagName==='BUTTON')n.onclick=()=>showModal('SQL-specific flame graph',flame(data.flameGraph));});
      body.querySelectorAll('[data-ki-knowledge]').forEach(n=>n.onclick=()=>showModal('Related Knowledge Base',config[1]==='sql'?code(data.diagnosis_results?.rule_matches_detail||[])+code(data.actionTrace||{}):code(data.detailAnalysis?.functionCodeExamples||[])));
      return;
    }
    let rows=[],target;
    const cells=values=>'<tr>'+values.map(v=>`<td>${v??'—'}</td>`).join('')+'</tr>';
    const evidenceButton=(r,label='Details')=>`<button class="action-btn" data-ki-record="${esc(r.id)}">${label}</button>`;
    switch(config[1]){
      case 'hosts':
        target='hostTableBody';
        rows=items.map(h=>['<div class="checkbox-wrapper"><input type="checkbox" disabled aria-label="Host selection unavailable"></div>',`<div class="host-name"><div class="host-name-main">${esc(h.name)}</div><div class="host-name-sub">${esc(h.id)}</div></div>`,`<span class="ip-address">${esc(h.ip)}:${h.port}</span>`,`<span class="status-tag ${h.status==='online'?'status-running':'status-warning'}">${esc(h.status)}</span>${h.collectionError?`<div class="ki-row-meta">${esc(h.collectionError)}</div>`:''}`,`<span class="db-tag">${esc(h.dbType)}</span><div class="ki-row-meta">${esc(h.database)}</div>`,...['cpuPercent','memoryPercent','diskPercent'].map(key=>usage(h.utilization?.[key],h.utilization)),'<span class="region-tag">Not configured</span>',h.createdAt?date(h.createdAt):'<span class="ki-row-meta" title="Host creation time is not collected; the refresh timestamp is not a creation date">Not recorded</span>',`<div class="action-buttons">${evidenceButton(h)}<a class="action-btn" href="sql-log.html">SQL</a><a class="action-btn" href="alert-list.html">Alerts</a><a class="action-btn" href="http://127.0.0.1:3000/" target="_blank" rel="noopener">Grafana</a></div>`]);break;
      case 'sql':
        target='logTableBody';rows=items.map(s=>[`<span class="sql-id">${esc(s.id)}</span><div class="ki-row-meta">${date(s.time)}<br>${esc(s.kind)}</div>`,`<a class="sql-text" href="sql-detail.html?id=${encodeURIComponent(s.id)}" title="${esc(s.sql)}">${esc(s.sql.slice(0,350))}${s.sql.length>350?'…':''}</a><div class="ki-row-meta">${number(s.executionTime)} ms · ${s.kind==='observation'?'mean / call':'individual execution'} · calls ${number(s.calls,0)}</div>`,esc(s.type),`${tag(s.result)}<div class="ki-row-meta">DREAM ${tag(s.jobStatus||'not_requested')} · ${tag(s.improvementStatus||'not_published')}</div>`,link('sql-detail.html',s.id,'Details')]);
        document.getElementById('hostName').textContent=items[0]?.host||'No retained SQL';document.getElementById('hostIP').textContent='127.0.0.1';document.getElementById('hostType').textContent='PostgreSQL';break;
      case 'alerts':
        target='alertTableBody';rows=items.map(a=>[tag(a.severity),`<span class="alert-id">${esc(a.id)}</span>`,date(a.time),`<span class="alert-host">${esc(a.host)}</span>`,`<div class="alert-reason">${esc(a.reason)}</div><div class="ki-row-meta">${shown(a.message||a.error||'No workflow error')}</div>`,`${tag(a.rawStatus)}<div class="ki-row-meta">${tag(a.applicationStatus||'not_applied')}</div>`,link('alert-detail.html',a.id,'View Details')]);
        {const select=document.getElementById('reasonFilter'),selected=select.value;select.innerHTML='<option value="">All</option>'+[...new Set([...items.map(a=>a.reason),selected].filter(Boolean))].map(r=>`<option value="${esc(r)}">${esc(r)}</option>`).join('');select.value=selected;}break;
      case 'manual':
        document.getElementById('snippetsContainer').innerHTML=items.length?`<table class="snippet-list"><tbody>${items.map(r=>cells([`<div class="snippet-source">${esc(r.source)}</div>`,`<div class="snippet-title">${esc(r.title)}</div><div class="snippet-text">${esc(r.content||r.description)}</div><div class="snippet-meta"><div class="snippet-tags">${[r.type,r.context,r.restart?'restart required':'no server restart'].filter(Boolean).map(x=>`<span class="snippet-tag">${esc(x)}</span>`).join('')}</div><div class="snippet-time">Default: ${r.default===''?'(empty string)':shown(r.default)} ${esc(r.unit||'')}</div></div>`,evidenceButton(r)])).join('')}</tbody></table>`:empty();
        document.querySelectorAll('#snippetsContainer tr').forEach(tr=>['snippet-source-col','snippet-content-col','snippet-action-col'].forEach((c,i)=>tr.cells[i].className=c));break;
      case 'code':
        target='functionTableBody';rows=items.map(r=>[esc(r.id),`<strong>${esc(r.name)}</strong><div class="ki-row-meta">${esc(r.file)}:${r.line}</div>`,`<span class="db-badge pg">PostgreSQL</span>`,shown(r.category),`<div class="code-block">${esc(r.functionFlow||'Runtime call chains are retained per alert, not this static reference.')}</div>`,`<div class="code-block">${esc(r.dataFlow||'Not retained')} → ${esc(r.description)}</div>`,evidenceButton(r)]);break;
      case 'diagnosisRules':
        target='ruleTableBody';rows=items.map(r=>[`<span class="rule-id">${esc(r.id)}</span>`,`<span class="rule-name">${esc(r.name)}</span>`,`<span class="rule-category">${esc(r.category)}</span>`,`<div class="rule-description">${esc(r.description)}</div>`,'SQL structure','Pattern-based',tag(r.status),evidenceButton(r)]);break;
      case 'alertRules':
        target='ruleTableBody';rows=items.map(r=>[`<div class="rule-name">${esc(r.name)}</div><div class="rule-desc">${shown(r.reason)}</div>`,shown(r.desc),`<code class="ki-flow">${esc(r.alertField)}</code>`,'In expression',number(r.period)+' s',tag(r.state)+' '+tag(r.health),evidenceButton(r)]);break;
      case 'knowledge':
        document.getElementById('tuningContainer').innerHTML=items.length?`<table class="tuning-list"><tbody>${items.map(r=>`<tr><td class="tuning-col-condition"><div class="tuning-label">Condition</div><div class="tuning-condition">${shown(r.condition||'No root cause retained')}</div><div class="cluster-info"><span class="cluster-badge">${esc(r.source)}</span>${tag(r.status)}</div></td><td class="tuning-col-recommendation"><div class="tuning-label">Recommendation / Action</div><div class="tuning-recommendation ki-flow">${esc(r.content||'No action retained')}</div><div class="ki-row-meta">${link('sql-detail.html',r.sqlKey,'SQL details')} · Hits: ${number(r.hitCount,0)} · ${evidenceButton(r,'Evidence')}</div></td><td class="tuning-col-confidence"><div class="tuning-label">Measured time reduction</div><div class="confidence-badge ${r.measuredImprovement>0?'high':'low'}">${percent(r.measuredImprovement)}</div><div class="performance-gain">${number(r.oldTimeSeconds,3)} → ${number(r.newTimeSeconds,3)} s</div><div class="ki-row-meta">Confidence: not measured<br>${date(r.updatedAt)}</div></td></tr>`).join('')}</tbody></table>`:empty();break;
      case 'overview':
        document.getElementById('overviewContent').innerHTML=overview(data);break;
    }
    if(target){
      const tbody=document.getElementById(target),table=tbody.closest('table');table.classList.add('ki-live-table');
      tbody.innerHTML=rows.length?rows.map(cells).join(''):`<tr><td colspan="${table.querySelectorAll('thead th').length}">${empty()}</td></tr>`;
    }
    body.querySelectorAll('[data-ki-record]').forEach(n=>n.onclick=()=>{const item=items.find(x=>String(x.id)===n.dataset.kiRecord);if(item)showModal(item.title||item.name||item.id,code(item));});
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
  function flameInteraction(e){
    const target=e.target.closest('[data-ki-flame-path],[data-ki-flame-reset]');
    if(!target||!current?.flameGraph)return;
    if(e.type==='keydown'&&!['Enter',' '].includes(e.key))return;
    e.preventDefault();e.stopPropagation();
    const path=target.hasAttribute('data-ki-flame-reset')?[]:JSON.parse(target.dataset.kiFlamePath);
    target.closest('.ki-flame-wrapper').outerHTML=flame(current.flameGraph,path);
  }
  document.addEventListener('click',flameInteraction);document.addEventListener('keydown',flameInteraction);
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
      const selectedJob=new URLSearchParams(location.search).get('job_id');
      if(selectedJob&&detail&&config[1]==='sql')query.set('job_id',selectedJob);
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
