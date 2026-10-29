import { escapeHtml as esc, executionLabels, resultLabels, decisionLabels, legacyResultLabels, deliveryLabels, propositionName, roleLabels, materialLabels, cadenceLabels, label, locatorText, pageInfo, dateText, reasonText, candidateLabels, stageLabels, eventLabels } from './labels.js';

const state = {
  view: location.hash.replace('#','') || 'runs', ruleMode:'current', runtime: null, runId: sessionStorage.getItem('complianceRunId') || '', run: null, report: null, plan: null,
  tasks: { page:0, limit:20, total:0 }, files:{ page:0,total:0 }, candidates:{ page:0,total:0 }, evidence:{ page:0,total:0,query:'',file:'' }, evidenceLimit:20, progressAfter:0, poll:null, request:0, previewUrl:null, navigation:null, connected:false, loading:false, controller:new AbortController(),
  candidateRows:[], selectedReview:null, questionSequence:0, auditSequence:0, compareSequence:0, reviewSequence:0,
};
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const classFor = (value) => ({pass:'good',no_pass:'bad',pending:'caution',unverifiable:'neutral',failed:'bad',issue_found:'bad',no_issue_observed:'good',review_required:'caution',missing_required_data:'neutral',system_processing_incomplete:'bad',legacy_pending:'neutral',queued:'neutral',running:'neutral',paused:'caution',completed:'good',delivered:'good',waiting:'caution',sending:'neutral'})[value] || 'neutral';
const badge = (value, labels = resultLabels) => `<span class="badge ${classFor(value)}">${esc(label(labels,value))}</span>`;
const outcomeBadge = (status, decision) => badge(decision || (status==='pending'?'legacy_pending':status), legacyResultLabels);
const api = async (path, options = {}) => {
  const headers = { Accept:'application/json', ...(options.body ? {'Content-Type':'application/json'} : {}), ...(options.headers || {}) };
  const response = await fetch(path,{...options,headers,signal:options.signal || (options.persist ? AbortSignal.timeout(30000) : state.controller.signal)});
  if (options.raw && response.ok) return {response,headers:response.headers};
  const body = await response.json().catch(()=>({}));
  if (!response.ok) { const error = new Error(typeof body.detail==='string'?body.detail:response.status===422?'请求参数格式无效':`HTTP ${response.status}`); error.status=response.status; throw error; }
  return {body,headers:response.headers,response};
};
function notify(message, type='info') { if(/aborted|abort/i.test(String(message))) return; const node=$('#notice'); node.className=`notice ${type}`; node.hidden=!message; node.innerHTML=`<span>${esc(message)}</span><button type="button" aria-label="关闭">×</button>`; node.querySelector('button').onclick=()=>{node.hidden=true}; }
function setView(view) {
  view=['runs','evidence','rules'].includes(view)?view:'runs';
  if(view!==state.view) abortRequests();
  state.view=view; history.replaceState(null,'',`#${view}`);
  $$('.nav-item').forEach((item)=>item.classList.toggle('active',item.dataset.view===view));
  $$('.view').forEach((item)=>{item.hidden=item.id!==`view-${view}`;});
  $('#pageTitle').textContent={runs:'检查运行',evidence:'证据导航',rules:'规则计划'}[view];
  if(state.connected){
    if(view==='evidence'){loadEvidence();loadNavigation();}
    if(view==='rules')loadRules();
  }
  updatePoll();
}
function abortRequests(){ state.request++; state.controller.abort(); state.controller=new AbortController(); }
function isCurrent(id){ return id===state.request; }
function runIdOf(run){ return run?.runId || run?.run_id || ''; }
async function loadRuntime() {
  try {
    const {body}=await api('/v1/runtime');
    state.connected=true; state.runtime=body;
    $('#environment').textContent=`${body.environment || 'LOCAL'} / ${body.ledger || '服务账本'}`;
    $('#piMode').textContent=body.pi_enabled?'Pi 取证已启用':'Pi 未启用 · 本地演示';
    $('#healthDot').classList.add('online'); $('#connectionState').textContent='服务在线';
    $('#newItem').innerHTML=(body.supported_items || []).map((item)=>`<option value="${esc(item.itemCode)}">${esc(item.name || item.itemCode)}</option>`).join('');
    return true;
  } catch(error) {
    if(error.name==='AbortError') return false;
    state.connected=false; $('#connectionState').textContent='服务不可达';
    $('#healthDot').classList.remove('online');
    notify(error.message,'error');
    return false;
  }
}
async function loadTasks(){ const id=state.request; const sequence=state.tasks.sequence=(state.tasks.sequence||0)+1; const params=new URLSearchParams({offset:String(state.tasks.page*state.tasks.limit),limit:String(state.tasks.limit)}); if($('#filterMine').value) params.set('mine_id',$('#filterMine').value); if($('#filterCycle').value) params.set('check_cycle',$('#filterCycle').value); try { const {body}=await api(`/inspection/tasks?${params}`); if(!isCurrent(id) || sequence!==state.tasks.sequence) return; state.tasks.total=body.total||0; renderTasks(body.items||[]); $('#listFreshness').textContent=`更新于 ${new Date().toLocaleTimeString('zh-CN',{hour12:false})}`; } catch(error){ if(isCurrent(id)) $('#taskRows').innerHTML=`<tr><td colspan="7" class="empty-cell">${esc(error.message)}</td></tr>`; } }
function renderTasks(items){ $('#taskTotal').textContent=state.tasks.total; const start=state.tasks.page*state.tasks.limit; $('#taskPageInfo').textContent=pageInfo(start,items.length,state.tasks.total); $('#taskPrev').disabled=state.tasks.page===0; $('#taskNext').disabled=(start+state.tasks.limit)>=state.tasks.total; $('#taskRows').innerHTML=items.length ? items.map((task)=>{ const run=task.runs?.[0]||{}; const id=runIdOf(run); return `<tr class="${id===state.runId?'selected':''}" data-run-id="${esc(id)}"><td><div class="row-title">${esc(task.mineId)}<small>${esc(run.name || run.itemName || run.itemCode || '检查项')} · ${esc(task.taskId)}</small></div></td><td class="mono">${esc(task.checkCycle)}</td><td>${badge(run.state,executionLabels)}</td><td>${outcomeBadge(run.result_status,run.decision_class)}</td><td>${badge(run.delivery?.state || run.delivery_state,deliveryLabels)}</td><td class="mono">${esc(dateText(task.created_at))}</td><td><button class="text-button open-run" type="button" data-run-id="${esc(id)}">查看 →</button></td></tr>` }).join(''):'<tr><td colspan="7" class="empty-cell">当前没有任务记录。</td></tr>'; }
async function loadRun(id) {
  if(!id) return;
  const same=state.runId===id;
  abortRequests(); state.runId=id; sessionStorage.setItem('complianceRunId',id);
  const token=state.request;
  if(!same){
    state.files.page=0;state.candidates.page=0;state.evidence.page=0;state.progressAfter=0;
    state.run=null;state.report=null;state.plan=null;state.navigation=null;state.evidenceRows=[];state.reviewPlan=null;
    $('#selectedRun').hidden=true;
    $('#evidenceInspector').textContent='选择一条证据，查看精确引用与文件定位。';
    $('#evidenceList').replaceChildren();$('#navigationContent').textContent='正在读取所选运行…';
    $('#previewDialog').close();clearPreview();
    state.candidateRows=[];state.selectedReview=null;state.questionSequence++;state.compareSequence++;state.reviewSequence++;
    $('#questionAnswer').textContent='可查看运行状态与缺口、具体命题或原文来源。';
    $('#compareResult').replaceChildren();$('#compareRunId').value='';$('#reviewForm').hidden=true;
    $('#auditSummary').textContent='正在读取所选运行…';$('#auditRows').replaceChildren();$('#workUnitRows').replaceChildren();
    $('#questionButton').disabled=false;$('#compareButton').disabled=false;$('#saveReview').disabled=false;
  }
  try {
    const [summary,report,plan,explanation]=await Promise.all([
      api(`/v1/runs/${encodeURIComponent(id)}/summary`),
      api(`/v1/runs/${encodeURIComponent(id)}/report`),
      api(`/v1/runs/${encodeURIComponent(id)}/plan`),
      api(`/v1/runs/${encodeURIComponent(id)}/explanation`).catch(()=>({body:null})),
    ]);
    if(token!==state.request) return;
    const events=same?(state.run?.events || []):[];
    state.run={...summary.body,events}; state.report=report.body.result || null; state.plan=plan.body.plan || plan.body; state.reviewPlan=explanation.body;
    renderRun(); updateQuestionPropositions(); await loadProgress(token);
    $$('#taskRows tr[data-run-id]').forEach((row)=>row.classList.toggle('selected',row.dataset.runId===id));
    if(state.view==='evidence'){await loadEvidence(0);await loadNavigation();}
    if(state.view==='rules') await loadRules();
    if(!$('#detail-files').hidden) await loadFiles();
    if(!$('#detail-candidates').hidden) await loadCandidates();
    if(!$('#detail-audit').hidden) await loadAudit();
    updatePoll();
  } catch(error) { handleError(error); }
}
async function loadProgress(token=state.request) {
  if(!state.runId || !state.run || token!==state.request) return;
  try {
    const {body}=await api(`/v1/runs/${encodeURIComponent(state.runId)}/progress?after=${state.progressAfter}&limit=100`);
    if(token!==state.request) return;
    const events=body.events || [];
    if(events.length){state.run.events.push(...events);state.progressAfter=events.at(-1).sequence;renderTimeline();}
    $('#progressMore').hidden=events.length<100;
  } catch(error){handleError(error);}
}
function relationSummary(report){
  const audit=report?.source_relations, counts=audit?.counts||{};
  if(!audit) return '';
  const conflict=(audit.relations||[]).filter(row=>row.kind==='fact_conflict' && row.blocks_verdict);
  const candidate=(audit.relations||[]).filter(row=>row.kind==='version_candidate');
  const fields={status:'执行状态',value:'临界指标值',hazard_confirmed:'危险确认',immediate:'立即处置',route_conforms:'撤人路线',acceptance_passed:'验收结果'};
  const valueLabels={done:'已实施',not_done:'明确未实施',true:'是',false:'否'};
  const lines=[];
  if(conflict.length){
    lines.push(`<strong>需要人工核对的事实冲突（${conflict.length}组）</strong>`);
    conflict.slice(0,3).forEach(row=>{
      const names=(row.files||[]).map(file=>file.original_name||file.file_id).join('、');
      const values=(row.differences||[]).map(item=>(valueLabels[String(item.value)]||String(item.value??'—'))+(item.unit?` ${item.unit}`:'')).join(' / ');
      lines.push(`<div class="relation-line">${esc(row.context?.workface||'未标明地点')} · ${esc(fields[row.context?.field]||row.context?.field||'事实字段')}：${esc(values)}<small>来源：${esc(names)}</small></div>`);
    });
  } else if(candidate.length){
    lines.push(`<strong>版本提示（${candidate.length}组）</strong><div class="relation-line">同类资料存在不同文件，系统保留全部来源，未自动选择“最新版”。</div>`);
  }
  if(counts.parallel) lines.push(`<div class="relation-note">并列资料 ${counts.parallel} 组：同类多份资料本身不构成冲突。</div>`);
  return lines.length?`<div class="report-relations">${lines.join('')}</div>`:'';
}
function reviewPlanSummary(plan){
  if(!plan || !Array.isArray(plan.actions) || !plan.actions.length)return '';
  const labels={system:'系统处理',source:'资料来源',business:'业务复核'};
  const actionRows=plan.actions.map((action)=>{
    const allTargets=action.targets||[], targets=allTargets.slice(0,12).map(target=>{
      const pages=(target.units||[]).map(unit=>{
        const page=/^page:(\d+)$/.exec(unit);
        if(!page)return `<span>${esc(unit==='document'?'整份文档':unit)}</span>`;
        const renderable=target.format==='pdf'||['png','jpg','jpeg','tif','tiff','webp'].includes(target.format);
        return renderable?`<button type="button" class="text-button preview-file" data-file-id="${esc(target.file_id)}" data-file-name="${esc(target.file_name)}" data-page="${esc(page[1])}">第${esc(page[1])}页</button>`:`<span>第${esc(page[1])}页</span>`;
      }).join(' ');
      return `<details class="review-target"><summary>${esc(target.file_name)} · ${esc(target.unit_count)} 个定位</summary><button type="button" class="text-button preview-file" data-file-id="${esc(target.file_id)}" data-file-name="${esc(target.file_name)}">打开原始文件</button><div class="ref-buttons">${pages}</div>${target.related_propositions?.length?`<small>关联检查步骤：${esc(target.related_propositions.map(propositionName).join('、'))}（是否适用仍按事件判断）</small>`:''}</details>`;
    }).join('');
    const moreTargets=allTargets.length>12?`<p class="muted">还有 ${esc(allTargets.length-12)} 个文件定位，请到“覆盖与对比”查看完整单元清单。</p>`:'';
    const citations=(action.evidence_refs||[]).map(ref=>`<button type="button" class="text-button" data-ref="${esc(ref)}">查看原文引用</button>`).join('');
    const count=action.unit_count?` · ${esc(action.unit_count)} 个单元`:'';
    return `<details class="review-action"${action.code==='provider_failed'||action.code==='processing_incomplete'?' open':''}><summary><span class="review-owner">${esc(labels[action.owner]||action.owner||'待处理')}</span>${esc(action.title)}${count}</summary><p>${esc(action.reason)}</p>${action.related_propositions?.length?`<p>命题：${esc(action.related_propositions.map(propositionName).join('、'))}</p>`:''}<p><strong>下一步：</strong>${esc(action.next_step)}</p>${targets}${moreTargets}${citations}</details>`;
  }).join('');
  const outcomes=plan.file_outcomes, notable=(outcomes?.items||[]).filter(item=>item.notable).slice(0,12);
  const outcomeRows=notable.map(item=>`<details class="file-outcome"><summary>${esc(item.file_name)} · ${esc({business_issue:'有问题线索',processing_failed:'处理失败',unfinished:'未完成',candidate_review:'候选待复核',needs_recognition:'识别未完整',needs_search:'待命题检索',unprocessed:'未处理',processed:'已处理'}[item.status]||item.status)}</summary><p>${esc(item.description)}</p><p>证据片段 ${esc(item.evidence_count)} · 异常单元 ${(item.problem_units||[]).length}</p>${(item.failures||[]).map(failure=>`<p class="muted">${esc(failure.stage||'处理')} ${failure.page?`第${esc(failure.page)}页`:''}：${esc(failure.reason)}</p>`).join('')}<button type="button" class="text-button preview-file" data-file-id="${esc(item.file_id)}" data-file-name="${esc(item.file_name)}">打开原始文件</button></details>`).join('');
  const outcomeMore=outcomes&&outcomes.notable_count>12?`<p class="muted">还有 ${esc(outcomes.notable_count-12)} 份需处理或复核的文件，请到“原始文件”和“覆盖与对比”查看完整列表。</p>`:'';
  return `<div class="review-plan"><div class="review-plan-head"><strong>具体原因与处理清单</strong><span>系统处理、资料补充、业务复核分别列明。展开文件名后可点击具体页码。</span></div>${actionRows}${outcomeRows?`<h4>按文件结果</h4>${outcomeRows}${outcomeMore}`:''}</div>`;
}
function renderRun(){ const run=state.run||{}, report=state.report||{}; const review=state.reviewPlan, decision=report.decision_class || review?.decision_class, decisionSummary=report.decision_summary || review?.decision_summary; $('#selectedRun').hidden=false; $('#runTitle').textContent=`${run.mineId || run.mine_id} · ${run.name || run.itemCode || run.item_code}`; $('#runIdentity').textContent=`${runIdOf(run)} / ${run.ruleRevision || '—'}`; $('#executionMetric').innerHTML=badge(run.state || run.execution_state,executionLabels); $('#resultMetric').innerHTML=outcomeBadge(report.status || run.result_status,decision); $('#evidenceMetric').textContent=run.evidence_count ?? run.evidence?.length ?? '—'; $('#gapsMetric').textContent=run.coverage_gap_count ?? report.coverage_gaps?.length ?? '—'; $('#resultBadge').innerHTML=outcomeBadge(report.status || run.result_status,decision); const collectionIssue=review && !review.has_propositions && ['pending','unverifiable'].includes(report.status); const title=decisionSummary || (collectionIssue?'取证未闭合，尚无事件级结论':report.description); $('#resultSummary').innerHTML=report.status ? `<div class="report-description">${esc(title)}</div><div class="report-detail">${esc(review?.summary || report.detail || '')}</div>${reviewPlanSummary(review)}${relationSummary(report)}<details class="stored-result"><summary>查看本轮存档说明</summary><p>${esc(report.description)}</p><p>${esc(report.detail || '')}</p></details><div class="report-delivery">回写：${badge(run.delivery?.state || run.delivery_state,deliveryLabels)} · 存档结论保持原样，原因说明仅解释本轮记录。</div>`:'<p class="muted">任务尚未定版。</p>'; const control=$('#controlButton'); if(['queued','running','paused'].includes(run.state)){control.hidden=false;control.textContent=run.control_state==='paused' || run.state==='paused'?'恢复执行':'暂停执行';}else control.hidden=true; renderTimeline(); renderPropositions(); renderPlan(); }
function renderTimeline(){ const events=state.run?.events || []; $('#timeline').innerHTML=events.length?events.slice(-10).reverse().map((event)=>`<li><div><strong>${esc(stageLabels[event.stage || 'run'] || event.stage || '运行')} / ${esc(eventLabels[event.event] || event.event || '')}</strong><small>${esc(event.status||'')} ${event.elapsed_ms?`· ${event.elapsed_ms} ms`:''}</small></div></li>`).join(''):'<li><div><strong>等待执行</strong><small>worker 领取后显示阶段</small></div></li>'; }
function renderPropositions(){
  const props=state.report?.propositions || [];
  $('#propositionList').innerHTML=props.length?props.map(prop=>`<div class="proposition-row"><div><h4>${esc(propositionName(prop.code))}</h4><p>${esc(prop.detail||'')}</p></div><span class="badge ${prop.state==='true'?'good':prop.state==='false'?'bad':'caution'}">${esc({true:'已证明',false:'明确反证',missing:'缺证',unreadable:'不可读'}[prop.state]||prop.state)}</span><button type="button" class="text-button proposition-evidence" data-refs="${esc((prop.evidence_refs||[]).join(','))}">查看引用 →</button></div>`).join(''):'<div class="blank">当前结果没有可展示的命题。</div>';
  const findings=state.report?.findings||[];
  const kinds={suspected_major_hazard:'疑似重大事故隐患 · 待人工确认',suspected_violation:'疑似违规',review_required:'待人工复核'};
  $('#findingSection').hidden=!findings.length;
  $('#findingList').innerHTML=findings.map(finding=>`<li><strong>${esc(kinds[finding.finding_classification]||'问题线索')}</strong> · ${esc(propositionName(finding.code))}<div>${esc(finding.detail||'')}</div><button type="button" class="text-button proposition-evidence" data-refs="${esc((finding.evidence_refs||[]).join(','))}">查看引用 →</button></li>`).join('');
  const gaps=state.report?.coverage_gaps||[];
  $('#gapsList').innerHTML=gaps.length?gaps.map(gap=>`<li>${esc(reasonText(gap))}</li>`).join(''):'<li>本轮没有记录覆盖缺口。</li>';
  renderTriggerReview();
  renderSourceRelations();
}
function renderTriggerReview(){
  let section=$('#triggerReview');
  if(!section){section=document.createElement('div');section.id='triggerReview';section.className='source-relations';$('#detail-propositions').append(section);}
  const review=state.report?.trigger_review;
  section.hidden=!review;
  if(!review)return;
  const profile=review.threshold_profiles||{}, comparison=review.threshold_comparison||{}, monitoring=review.monitoring_candidates||{};
  const tech={ground_sound:'地音',microseismic:'微震',stress:'应力',drill_cuttings:'钻屑',electromagnetic:'电磁辐射',multi_parameter:'多参量综合'};
  const rowLabels={single:'单份资料',duplicate:'重复来源',equivalent_versions:'条件相近的多份资料',conflict_or_parallel:'条件不同，适用版本待核',unresolved_extraction:'等级条件未完整提取'};
  const versions=(comparison.comparisons||[]).map(row=>`<li><strong>${esc(tech[row.technology]||row.technology)}</strong>：${esc(rowLabels[row.classification]||row.classification)} · ${esc(row.document_ids?.length||0)} 份文件</li>`).join('');
  const rules=(profile.rows||[]).slice(0,30).map(row=>`<li>${esc(tech[row.technology]||row.technology)} · ${esc(row.grade_label)}：${esc(row.condition_text?.slice(0,260)||'条件未读取')}${row.evidence_refs?.[0]?` <button type="button" class="text-button" data-ref="${esc(row.evidence_refs[0])}">查看原文</button>`:''}<small>状态：${esc(row.status==='candidate_rule_text'?'待核对的等级原文':'条件不完整')}</small></li>`).join('');
  const observations=(monitoring.candidates||[]).slice(0,20).map(row=>`<li>${esc(row.technology||'监测技术待确认')} · ${esc(row.probe_id||'探头待确认')} · ${esc(row.location||row.workface||'地点待确认')} · ${esc(row.dev_value??row.value??'数值待确认')}${esc(row.unit||'')} · ${esc(row.sample_time||row.shift_id||'时段待确认')}${row.source?.evidence_id?` <button type="button" class="text-button" data-ref="${esc(row.source.evidence_id)}">查看来源</button>`:''}</li>`).join('');
  const pairs=(review.adjacent_probe_groups?.groups||[]).slice(0,12).map(group=>`<li>${esc(group.workface)} · ${esc(group.location)} · ${esc(group.probe_ids?.join('、'))} · ${esc(group.time_key?.join?.(' / ')||group.time_key||'时段待确认')}</li>`).join('');
  section.innerHTML=`<h3>触发证据核对</h3><p class="muted">等级按本轮指标原文保留；监测值与同地点双探头仍是候选，未自动生成触发结论。</p><details><summary>指标版本与等级 · ${esc(profile.documents?.length||0)} 份</summary><ul class="gaps-list">${versions||'<li>尚无可比较的等级条目。</li>'}${rules}</ul></details><details><summary>日报监测候选 · ${esc(monitoring.candidates?.length||0)} 条</summary><ul class="gaps-list">${observations||'<li>尚无可定位的监测候选。</li>'}</ul>${monitoring.candidates?.length>20?'<p class="muted">这里只展示前20条，完整候选可导出结果查看。</p>':''}</details><details><summary>同地点双探头候选 · ${esc(review.adjacent_probe_groups?.groups?.length||0)} 组</summary><ul class="gaps-list">${pairs||'<li>本轮没有可证明同地点同时间的双探头记录。</li>'}</ul></details>`;
}
function renderSourceRelations(){
  let section=$('#sourceRelations');
  if(!section){section=document.createElement('div');section.id='sourceRelations';section.className='source-relations';$('#detail-propositions').append(section);}
  const audit=state.report?.source_relations, rows=audit?.relations||[];
  section.hidden=!audit;
  const labels={duplicate:'重复来源',parallel:'并列资料',version_candidate:'版本待比对',fact_conflict:'事实冲突'};
  const fields={status:'执行状态',value:'临界指标值',hazard_confirmed:'危险确认',immediate:'立即处置',route_conforms:'撤人路线',acceptance_passed:'验收结果'};
  const values={done:'已实施',not_done:'明确未实施',true:'是',false:'否'};
  section.innerHTML='<h3>资料关系与冲突依据</h3><p class="muted">文件日期、类别相同可以有多份资料。版本提示不自动代表业务事实冲突。</p>'+rows.map(row=>{
    const names=new Map((row.files||[]).map(file=>[file.file_id,file.original_name||file.file_id]));
    const files=(row.files||[]).map(file=>`<li><button type="button" class="text-button preview-file" data-file-id="${esc(file.file_id)}" data-file-name="${esc(file.original_name)}">${esc(file.original_name||file.file_id)}</button><small class="muted">填报日期：${esc(file.report_date||'未登记')} · 文件名期间线索：${esc((file.filename_period_hints||[]).join('、')||'未提取')} · SHA256 ${esc((file.content_hash||'').slice(0,16))}…</small></li>`).join('');
    const differences=(row.differences||[]).map(item=>`<li>${esc(fields[row.context?.field]||row.context?.field)}：${esc(values[String(item.value)]||String(item.value))} ${esc(item.unit||'')}</li>`).join('');
    const citations=(row.citations||[]).map(item=>`<div class="relation-line"><button class="text-button" type="button" data-ref="${esc(item.evidence_id)}">${esc(names.get(item.file_id)||item.file_id)} · ${esc(locatorText(item.locator))}</button><blockquote class="source-quote">${esc(item.quote)}</blockquote></div>`).join('');
    const affected=(row.affected_propositions||[]).map(prop=>propositionName(prop.code)).join('、');
    const context=row.context||{},when=context.time||(context.valid_from?`${context.valid_from} 至 ${context.valid_to}`:'时间未登记');
    return `<details${row.kind==='fact_conflict'?' open':''}><summary>${esc(labels[row.kind]||row.kind)} · ${(row.files||[]).length} 份${row.blocks_verdict?' · 影响命题判定':''}</summary><p>${esc(row.reason)}</p>${row.context?`<p>${esc(context.workface)} · 事件 ${esc(context.event_id)} · ${esc(when)}${context.action?` · ${esc(propositionName(context.action))}`:''}</p>`:''}${affected?`<p>受影响命题：${esc(affected)}</p>`:''}<ul>${files}${differences}</ul>${citations}</details>`;
  }).join('');
}
function fileOutcomeLabel(fileId){
  const item=(state.reviewPlan?.file_outcomes?.items||[]).find(row=>row.file_id===fileId);
  if(!item)return '';
  const labels={business_issue:'有问题线索',processing_failed:'处理失败',unfinished:'未完成',candidate_review:'识别候选待核验',needs_recognition:'识别未完整',needs_search:'待命题检索',unprocessed:'未处理',processed:'已处理'};
  const problems=(item.failures||[]).map(failure=>(failure.page?'第'+failure.page+'页：':'')+reasonText(failure.reason)).join('；');
  return `<small class="file-outcome-label">${esc(labels[item.status]||item.status)}${problems?' · '+esc(problems):''}</small>`;
}
async function loadFiles(page=state.files.page){ if(!state.runId)return; const current=state.request; const sequence=state.files.sequence=(state.files.sequence||0)+1; state.files.page=page; try { const {body}=await api(`/v1/runs/${encodeURIComponent(state.runId)}/files?offset=${page*20}&limit=20`); if(current!==state.request || sequence!==state.files.sequence)return; state.files.total=body.total||0; $('#fileCount').textContent=`${state.files.total} 个`; $('#filePageInfo').textContent=pageInfo(page*20,body.items?.length||0,state.files.total); $('#filePrev').disabled=page===0; $('#fileNext').disabled=(page+1)*20>=state.files.total; $('#fileRows').innerHTML=(body.items||[]).map((file)=>`<tr><td>${esc(file.original_name||file.file_id)}<small>${esc(file.file_id)}</small>${fileOutcomeLabel(file.file_id)}</td><td>${esc(roleLabels[file.source_role]||file.source_role||'—')}</td><td class="mono">${esc((file.content_hash||'').slice(0,16))}…</td><td><button type="button" class="text-button preview-file" data-file-id="${esc(file.file_id)}" data-file-name="${esc(file.original_name || file.file_id)}">原文 →</button></td></tr>`).join('')||'<tr><td colspan="4" class="empty-cell">没有文件。</td></tr>'; } catch(error){ $('#fileRows').innerHTML=`<tr><td colspan="4" class="empty-cell">${esc(error.message)}</td></tr>`; } }
function updateQuestionPropositions(){
  const selected=$('#questionProposition').value,props=state.report?.propositions||[];
  $('#questionProposition').innerHTML='<option value="">不限命题</option>'+[...new Set(props.map(prop=>prop.code).filter(Boolean))].map(code=>`<option value="${esc(code)}">${esc(propositionName(code))}</option>`).join('');
  if([...$('#questionProposition').options].some(option=>option.value===selected))$('#questionProposition').value=selected;
}
async function loadCandidates(page=state.candidates.page){
  if(!state.runId)return;
  const current=state.request,runId=state.runId;
  const sequence=state.candidates.sequence=(state.candidates.sequence||0)+1;state.candidates.page=page;
  try {
    const {body}=await api(`/v1/runs/${encodeURIComponent(runId)}/candidates?offset=${page*10}&limit=10`);
    if(current!==state.request || sequence!==state.candidates.sequence || runId!==state.runId)return;
    state.candidates.total=body.total||0;state.candidateRows=body.items||[];
    $('#candidatePageInfo').textContent=pageInfo(page*10,state.candidateRows.length,state.candidates.total);
    $('#candidatePrev').disabled=page===0;$('#candidateNext').disabled=(page+1)*10>=state.candidates.total;
    $('#candidateList').innerHTML=state.candidateRows.map((candidate,index)=>{
      const refs=registeredRefs(candidate.evidence_refs),fields=reviewBindings(candidate);
      return `<div class="candidate"><div><strong class="mono">${esc(candidate.candidate_id||'candidate')}</strong> ${badge(candidate.validation_status||'candidate',candidateLabels)}</div><dl class="metadata-list candidate-fields">${Object.entries(candidate.fields||{}).map(([key,value])=>`<dt>${esc(fieldName(key))}</dt><dd>${esc(displayValue(value))}</dd>`).join('')}</dl>${(candidate.reasons||[]).length?`<p class="muted">${esc(candidate.reasons.map(reasonText).join('；'))}</p>`:''}<div class="ref-buttons">${refs.map(ref=>`<button type="button" data-ref="${esc(ref)}">查看引用 ${esc(ref.slice(0,12))}</button>`).join('')}</div><button type="button" class="text-button review-candidate" data-candidate-index="${index}" ${Object.keys(fields).length?'':'disabled'}>记录复核意见</button>${Object.keys(fields).length?'':'<p class="muted">尚无逐字段引用，待补充后记录字段复核。</p>'}</div>`;
    }).join('')||'<p class="muted">本轮没有模型候选。</p>';
    await loadReviews();
  } catch(error){if(error.name!=='AbortError' && current===state.request && sequence===state.candidates.sequence)$('#candidateList').textContent=error.message;}
}
function displayValue(value){if(value==null)return '—';if(typeof value==='boolean')return value?'是':'否';if(Array.isArray(value))return value.map(displayValue).join('、');if(typeof value==='object')return Object.entries(value).map(([key,item])=>`${fieldName(key)}：${displayValue(item)}`).join('；');return String(value);}
function fieldName(value){return {event_id:'事件',workface:'工作面',time:'时间',action:'动作',status:'状态',value:'数值',unit:'单位',method:'方法',metric:'指标',point:'测点',actor_role:'执行人角色',target_scope:'目标范围',hazard_confirmed:'危险研判',immediate:'立即执行',route_conforms:'路线符合'}[value]||value;}
function registeredRefs(value){return [...new Set((Array.isArray(value)?value:[]).map(ref=>typeof ref==='string'?ref:ref?.evidence_id).filter(ref=>typeof ref==='string' && ref.trim()))];}
function reviewBindings(candidate){
  const bindings=Object.keys(candidate.field_bindings||{}).length?candidate.field_bindings:(candidate.field_evidence||{}),fields={};
  for(const name of Object.keys(candidate.fields||{})){
    const supplied=bindings[name]||bindings[`fields.${name}`]||[];
    const refs=registeredRefs(Array.isArray(supplied)?supplied:[supplied]);
    if(refs.length)fields[name]=refs;
  }
  return fields;
}
const coverageLabels={not_indexed:'尚未解析',not_searched:'尚未检索',searched_hit:'已检索有引用',searched_no_hit:'已检索无命中',source_absent:'来源缺失',unreadable:'不可读',provider_failed:'识别失败',conflicted:'存在冲突',readable:'原文可读',empty:'空白单元',searched:'已检索',complete:'覆盖完成',read:'已读取',read_no_hit:'已读取无命中',indexed:'已解析'};
const unitLabels={queued:'等待处理',running:'处理中',succeeded:'已完成',failed:'失败'};
function gapText(gap){if(typeof gap==='string')return reasonText(gap);if(!gap || typeof gap!=='object')return String(gap||'未闭合项');return [gap.file_id||gap.material_group,gap.unit_id,reasonText(gap.reason||gap.state||'未闭合项')].filter(Boolean).join(' · ');}
async function loadAudit(){
  if(!state.runId)return;
  const runId=state.runId,current=state.request,sequence=++state.auditSequence;
  $('#refreshAudit').disabled=true;
  try{
    const [audit,work]=await Promise.all([api(`/v1/runs/${encodeURIComponent(runId)}/audit`),api(`/v1/runs/${encodeURIComponent(runId)}/work-units`)]);
    if(current!==state.request || runId!==state.runId || sequence!==state.auditSequence)return;
    const body=audit.body,units=body.units||[],workUnits=work.body.items||[],gaps=body.gaps||[];
    $('#auditSummary').innerHTML=`<dl class="metadata-list"><dt>资料范围</dt><dd>${body.source_complete?'已形成范围证明':'完整性尚未证明'}</dd><dt>取证覆盖</dt><dd>${body.coverage_complete?'已闭合':'仍有未处理或未验证单元'}</dd><dt>资料单元</dt><dd>${esc(units.length)} 个</dd></dl>${gaps.length?`<ul class="gaps-list">${gaps.slice(0,30).map(gap=>`<li>${esc(gapText(gap))}</li>`).join('')}</ul>`:'<p class="muted">当前审计未记录缺口。</p>'}${gaps.length>30?'<p class="muted">这里只展示前30项缺口，可导出结果查看完整记录。</p>':''}`;
    $('#auditRows').innerHTML=units.slice(0,200).map(unit=>`<tr><td class="mono">${esc(unit.file_id||unit.material_group||'—')}</td><td class="mono">${esc(unit.unit_id||'—')}</td><td>${esc(coverageLabels[unit.state]||unit.state||'待核对')}</td></tr>`).join('')||'<tr><td colspan="3" class="empty-cell">尚无资料单元记录。</td></tr>';
    $('#workUnitCount').textContent=`${workUnits.length} 个${workUnits.length>200?' · 展示前200个':''}`;
    $('#workUnitRows').innerHTML=workUnits.slice(0,200).map(unit=>`<tr><td>${esc({parse_file:'文件解析',recognize_pages:'页面识别',recognize_page:'页面识别'}[unit.kind]||unit.kind||'资料处理')}<small class="mono">${esc((unit.work_unit_id||'').slice(0,20))}</small></td><td>${esc(unitLabels[unit.state]||unit.state||'—')}</td><td>${esc(unit.attempt??'—')}</td></tr>`).join('')||'<tr><td colspan="3" class="empty-cell">尚无处理单元，等待取证开始。</td></tr>';
  }catch(error){if(error.name!=='AbortError' && current===state.request && sequence===state.auditSequence)$('#auditSummary').textContent=`读取审计失败：${error.message}`;}
  finally{if(sequence===state.auditSequence)$('#refreshAudit').disabled=false;}
}
async function askQuestion(event){
  event.preventDefault();if(!state.runId){notify('请先选择检查运行。');return;}
  const button=$('#questionButton');if(button.disabled)return;
  const runId=state.runId,current=state.request,sequence=++state.questionSequence;
  const question=$('#inspectionQuestion').value.trim();if(!question)return;
  button.disabled=true;$('#questionAnswer').textContent='正在核对本轮记录…';
  try{
    const request={question},proposition=$('#questionProposition').value;if(proposition)request.proposition_id=proposition;
    const {body}=await api(`/v1/runs/${encodeURIComponent(runId)}/questions`,{method:'POST',body:JSON.stringify(request)});
    if(current!==state.request || runId!==state.runId || sequence!==state.questionSequence)return;
    $('#questionAnswer').innerHTML=`<p class="answer-text">${esc(body.answer||'当前记录没有可引用的回答。')}</p><div class="ref-buttons">${registeredRefs(body.evidence_refs).map(ref=>`<button type="button" data-ref="${esc(ref)}">查看原文 ${esc(ref.slice(0,12))}</button>`).join('')}</div>${(body.limitations||[]).length?`<ul class="answer-limitations">${body.limitations.map(value=>`<li>${esc(value)}</li>`).join('')}</ul>`:''}`;
  }catch(error){if(current===state.request && sequence===state.questionSequence && runId===state.runId)$('#questionAnswer').textContent=error.name==='AbortError'?'请求已取消，可重新提问。':`核对失败：${error.message}`;}
  finally{if(sequence===state.questionSequence)button.disabled=false;}
}
async function compareRuns(event){
  event.preventDefault();if(!state.runId){notify('请先选择检查运行。');return;}
  const button=$('#compareButton');if(button.disabled)return;
  const other=$('#compareRunId').value.trim();if(!other)return;
  const runId=state.runId,current=state.request,sequence=++state.compareSequence;
  button.disabled=true;$('#compareResult').textContent='正在比较冻结记录…';
  try{
    const {body}=await api(`/v1/runs/${encodeURIComponent(runId)}/compare/${encodeURIComponent(other)}`);
    if(current!==state.request || runId!==state.runId || sequence!==state.compareSequence)return;
    const files=body.files||{},props=body.propositions||{};
    const rows=[['新增资料',files.added||[]],['移除资料',files.removed||[]],['版本变化',files.changed||[]]];
    const propRows=[['新增命题',props.added||[]],['移除命题',props.removed||[]],['命题变化',props.changed||[]]];
    $('#compareResult').innerHTML=`<p>当前运行 ${badge(body.before_status)} → 对照运行 ${badge(body.after_status)}</p><p class="muted">${body.rule_changed?'两次运行的规则版本不同，需同时核对规则影响。':'两次运行的规则版本相同。'}历史结果保持各自记录。</p>${rows.map(([title,items])=>`<h4>${title} · ${items.length}</h4>${items.length?`<ul class="comparison-list">${items.slice(0,30).map(item=>`<li>${esc(item.original_name||item.file_id||item.after?.original_name||item.key||'来源资料')}${item.before?`<small class="mono">${esc((item.before.content_hash||'').slice(0,16))} → ${esc((item.after?.content_hash||'').slice(0,16))}</small>`:''}</li>`).join('')}</ul>`:'<p class="muted">无变化</p>'}`).join('')}${propRows.map(([title,items])=>`<h4>${title} · ${items.length}</h4>${items.length?`<ul class="comparison-list">${items.slice(0,30).map(item=>{const key=Array.isArray(item.key)?item.key:[],name=propositionName(item.code||key[2]||'命题');return `<li><strong>${esc(name)}</strong> ${esc(item.state?propositionState(item.state):`${propositionState(item.before?.state)} → ${propositionState(item.after?.state)}`)}<small>${esc(item.detail||item.after?.detail||'')}</small></li>`;}).join('')}</ul>`:'<p class="muted">无变化</p>'}`).join('')}${(body.limitations||[]).map(text=>`<p class="muted">${esc(text)}</p>`).join('')}`;
  }catch(error){if(current===state.request && sequence===state.compareSequence && runId===state.runId)$('#compareResult').textContent=error.name==='AbortError'?'比较已取消，可重新执行。':`无法比较：${error.message}`;}
  finally{if(sequence===state.compareSequence)button.disabled=false;}
}
function propositionState(value){return {true:'已证明',false:'明确反证',missing:'缺证',unreadable:'不可读',candidate:'候选'}[value]||value||'尚无记录';}
async function loadReviews(){
  if(!state.runId)return;
  const runId=state.runId,current=state.request,sequence=++state.reviewSequence;
  try{
    const {body}=await api(`/v1/runs/${encodeURIComponent(runId)}/reviews`);
    if(current!==state.request || runId!==state.runId || sequence!==state.reviewSequence)return;
    $('#reviewList').innerHTML=(body.items||[]).slice(-30).reverse().map(review=>`<div class="review-entry"><strong class="mono">${esc(review.candidate_id||'候选复核')}</strong><p>${esc(review.comment||'已记录字段引用核对意见。')}</p><small class="muted">${esc(review.created_at?dateText(review.created_at):'本轮记录')} · ${esc(review.review_id||'')}</small></div>`).join('')||'<p class="muted">本轮尚无复核意见。</p>';
  }catch(error){if(error.name!=='AbortError' && current===state.request && sequence===state.reviewSequence)$('#reviewList').textContent=`复核记录暂不可读：${error.message}`;}
}
function startReview(index){
  const candidate=state.candidateRows[index];if(!candidate)return;
  const fields=reviewBindings(candidate);if(!Object.keys(fields).length){notify('候选尚无逐字段原文引用。');return;}
  state.selectedReview={candidate_id:candidate.candidate_id,fields,runId:state.runId};
  $('#reviewContext').textContent=`候选 ${candidate.candidate_id} · 请逐项核对原文后勾选`;
  $('#reviewFields').innerHTML=Object.entries(fields).map(([name,refs])=>`<label class="review-field"><input type="checkbox" name="reviewField" value="${esc(name)}"><span>${esc(fieldName(name))}<small>${esc(refs.map(ref=>ref.slice(0,12)).join('、'))}</small></span></label>`).join('');
  $('#reviewComment').value='';$('#reviewError').textContent='';$('#reviewForm').hidden=false;
  $('#reviewForm').scrollIntoView({block:'nearest'});
}
async function submitReview(event){
  event.preventDefault();const selected=state.selectedReview,button=$('#saveReview');if(!selected || button.disabled || selected.runId!==state.runId)return;
  const names=$$('#reviewFields input:checked').map(input=>input.value),comment=$('#reviewComment').value.trim();
  if(!names.length){$('#reviewError').textContent='请先核对并勾选至少一个字段。';return;}if(!comment)return;
  const runId=state.runId,current=state.request;button.disabled=true;$('#reviewError').textContent='';
  try{
    const request={review_id:crypto.randomUUID(),candidate_id:selected.candidate_id,fields:Object.fromEntries(names.map(name=>[name,selected.fields[name]])),comment};
    await api(`/v1/runs/${encodeURIComponent(runId)}/reviews`,{method:'POST',body:JSON.stringify(request)});
    if(current!==state.request || runId!==state.runId)return;
    $('#reviewForm').hidden=true;state.selectedReview=null;notify('复核意见已记录，历史核验结果未改动。','success');await loadReviews();
  }catch(error){if(error.name!=='AbortError' && current===state.request && runId===state.runId)$('#reviewError').textContent=error.message;}
  finally{button.disabled=false;}
}
async function loadEvidence(page=state.evidence.page){ if(!state.runId){$('#evidenceScope').textContent='请先在检查运行中选择一项检查。';return} state.evidence.page=page; const id=state.request; const sequence=state.evidence.sequence=(state.evidence.sequence||0)+1; const params=new URLSearchParams({query:state.evidence.query,file_id:state.evidence.file,offset:String(page*state.evidenceLimit),limit:String(state.evidenceLimit)}); try { const {body}=await api(`/v1/runs/${encodeURIComponent(state.runId)}/evidence?${params}`); if(id!==state.request || sequence!==state.evidence.sequence)return; state.evidence.total=body.total||0; $('#evidenceScope').textContent=`${state.runId} · 冻结范围内 ${state.evidence.total} 条匹配`; $('#evidenceCount').textContent=state.evidence.total; $('#evidencePageInfo').textContent=pageInfo(page*state.evidenceLimit,body.items?.length||0,state.evidence.total); $('#evidencePrev').disabled=page===0; $('#evidenceNext').disabled=(page+1)*state.evidenceLimit>=state.evidence.total; $('#evidenceList').innerHTML=(body.items||[]).map((item,index)=>`<button type="button" class="evidence-item" data-evidence-index="${index}"><div class="evidence-meta"><span>${esc(materialLabels[item.material_group]||roleLabels[item.source_role]||'来源')}</span><span>${esc(item.evidence_id?.slice(0,12))}</span></div><div class="evidence-quote">${esc(item.quote||item.text)}</div><div class="evidence-source">${esc(item.original_name||item.file_id)} · ${esc(locatorText(item.locator))}</div></button>`).join('')||'<div class="blank">没有匹配片段。未命中不等于资料不存在。</div>'; state.evidenceRows=body.items||[]; } catch(error){ if(id===state.request)notify(error.message,'error'); } }
async function inspectEvidence(item,button) {
  if(!item?.evidence_id || !state.runId) return;
  const runId=state.runId, requestId=state.request;
  const sequence=state.inspectorSequence=(state.inspectorSequence||0)+1;
  $$('.evidence-item').forEach((node)=>node.classList.remove('selected')); button?.classList.add('selected');
  $('#evidenceInspector').textContent='正在读取证据…';
  try {
    const {body}=await api(`/v1/runs/${encodeURIComponent(runId)}/evidence/${encodeURIComponent(item.evidence_id)}`);
    if(runId!==state.runId || requestId!==state.request || sequence!==state.inspectorSequence) return;
    $('#evidenceInspector').innerHTML=`<dl class="metadata-list"><dt>文件</dt><dd>${esc(body.original_name || body.file_id)}</dd><dt>版本</dt><dd class="mono">${esc(body.content_hash)}</dd><dt>定位</dt><dd>${esc(locatorText(body.locator))}</dd><dt>来源角色</dt><dd>${esc(roleLabels[body.source_role] || body.source_role)}</dd></dl><blockquote class="source-quote">${esc(body.quote || body.text)}</blockquote><div class="inspector-actions"><button type="button" class="secondary" id="inspectOpenSource">查看原文</button><button type="button" class="secondary" id="inspectCopy">复制引用</button></div>`;
    $('#inspectCopy').onclick=async()=>{try{await navigator.clipboard.writeText(JSON.stringify(body,null,2));notify('引用已复制');}catch(_){notify('浏览器未允许复制，可从结果导出引用。');}};
    $('#inspectOpenSource').onclick=()=>previewFile(body.file_id,body.original_name || body.file_id,body.locator?.page);
  } catch(error){if(error.name!=='AbortError' && requestId===state.request && sequence===state.inspectorSequence)$('#evidenceInspector').textContent=error.message;}
}
async function loadNavigation() {
  if(!state.runId){$('#navigationContent').textContent='请先选择运行。';return;}
  const current=state.request;
  try {
    const {body}=await api(`/v1/runs/${encodeURIComponent(state.runId)}/navigation`);
    if(current!==state.request) return;
    state.navigation=body; const filter=$('#navigationFilter').value;
    $('#navigationFilter').innerHTML='<option value="">全部命题</option>'+[...new Set((body.propositions || []).map((prop)=>prop.code))].map((code)=>`<option value="${esc(code)}">${esc(propositionName(code))}</option>`).join('');
    $('#navigationFilter').value=filter; renderNavigation(body);
  } catch(error){if(error.name!=='AbortError')$('#navigationContent').textContent=error.message;}
}
function renderNavigation(body) {
  const filter=$('#navigationFilter').value;
  const props=(body.propositions || []).filter((prop)=>!filter || prop.code===filter);
  const registry=new Map((body.evidence || []).map((item)=>[item.evidence_id,item]));
  const labels={true:'已证明',false:'明确反证',missing:'缺证',unreadable:'不可读'};
  const propositionRows=props.map((prop)=>`<div class="navigation-row"><div class="navigation-heading">${esc(propositionName(prop.code))} <span class="badge ${prop.state==='true'?'good':prop.state==='false'?'bad':'caution'}">${esc(labels[prop.state] || prop.state)}</span></div><p class="muted">${esc(prop.workface || '位置未标明')} · ${esc(prop.event_id || '事件未绑定')}</p><p>${esc(prop.detail || '')}</p><div class="ref-buttons">${(prop.evidence_refs || []).map((ref)=>`<button type="button" data-ref="${esc(ref)}">${esc(registry.get(ref)?.original_name || registry.get(ref)?.file_id || ref.slice(0,12))} · ${esc(locatorText(registry.get(ref)?.locator))}</button>`).join('') || '<span class="muted">暂无注册引用</span>'}</div></div>`).join('');
  const relations=(body.association_links || []).map((link)=>`<div class="relationship"><strong>${esc(link.association_state==='verified_reference'?'已验证关联':link.temporal_partition?'候选关联':'来源关联')}</strong> · ${esc(link.workface || '')}<p>${esc(link.reason || '')}</p><div class="mono muted">${esc(link.source_fact_id || link.fact_id || '')} → ${esc(link.event_id || '')}</div>${(link.limitations || []).map((value)=>`<p class="muted">${esc(value)}</p>`).join('')}</div>`).join('');
  const gaps=(body.gaps || []).map((gap)=>`<li>${esc(reasonText(gap))}</li>`).join('');
  $('#navigationContent').innerHTML=(propositionRows || '<p class="muted">本轮尚无可证明的命题。</p>')+(relations?`<h4>跨文件关联</h4>${relations}`:'')+(gaps?`<h4>证据缺口</h4><ul class="gaps-list">${gaps}</ul>`:'');
}
async function loadRules() {
  try {
    const frozen=state.ruleMode==='frozen' && state.runId;
    const path=frozen?`/v1/runs/${encodeURIComponent(state.runId)}/plan`:'/v1/rules/stop_work_unload/plan';
    const requestId=state.request;
    const {body}=await api(path);
    if(requestId!==state.request)return;
    state.plan=body.plan || body;
    $('#ruleContext').textContent=frozen?`所选运行冻结版本 · ${state.runId}`:'当前规则预览';
    renderPlan();
  } catch(error){if(error.name!=='AbortError')$('#planSource').textContent=error.message;}
}
function renderPlan(plan=state.plan || {}){
  $('#planName').textContent=plan.name||'冲击危险停工撤人与卸压';
  $('#planVersion').textContent=`计划版本 ${plan.planRevision||plan.revision||'—'} · 规则资产 ${plan.assetRevision||plan.sourceRevision||'—'}`;
  const source=plan.source||{},legacy=plan.legacySource||{};
  $('#planSource').innerHTML=`<dt>规则来源</dt><dd>${esc(source.reference||source.filename||'—')}</dd><dt>来源类型</dt><dd>${esc(source.type==='user_confirmed_policy'?'用户确认规则':source.sheet||'—')}</dd><dt>历史溯源</dt><dd>${esc(legacy.filename||'无')} ${legacy.sha256?`· SHA256 ${esc(legacy.sha256.slice(0,16))}…`:''}</dd><dt>状态</dt><dd>${esc(plan.status||'development-draft')}</dd>`;
  $('#planPropositions').innerHTML=(plan.requiredPropositions||[]).map(code=>`<div><strong>${esc(propositionName(code))}</strong><span class="muted">需要冻结证据或明确缺口</span></div>`).join('')||'<p class="muted">未加载命题。</p>';
  const conditionFor=group=>(plan.conditionalMaterials||[]).find(row=>row.materialGroups?.includes(group));
  const materialMode=item=>item.required?'基础资料':conditionFor(item.materialGroup)?.when?.proposition==='trigger'?'确认触发后':conditionFor(item.materialGroup)?.when?.proposition==='warning_danger_confirmed'?'预警且研判危险后':'按事件需要';
  $('#planMaterials').innerHTML=`<table><thead><tr><th>资料组</th><th>资料名称</th><th>频率</th><th>启用方式</th></tr></thead><tbody>${(plan.materialRequirements||[]).map(item=>`<tr><td>${esc(materialLabels[item.materialGroup]||item.materialGroup)}</td><td>${esc(item.displayName||materialLabels[item.materialGroup]||item.materialGroup)}</td><td>${esc(cadenceLabels[item.cadence]||item.cadence)}</td><td>${materialMode(item)}</td></tr>`).join('')}</tbody></table>`;
}
async function previewFile(fileId,fileName=fileId,page) {
  if(!state.runId) return;
  const requestId=state.request;
  const sequence=state.previewSequence=(state.previewSequence||0)+1;
  $('#previewDialog').showModal(); $('#previewTitle').textContent=fileName; $('#previewBody').textContent='正在读取原始文件…';$('#previewMetadata').textContent=Number.isInteger(page)?`定位：第 ${page} 页`:'';
  $('#downloadFile').disabled=true;
  try {
    const path=Number.isInteger(page)?`/v1/runs/${encodeURIComponent(state.runId)}/files/${encodeURIComponent(fileId)}/pages/${page}/image`:`/v1/runs/${encodeURIComponent(state.runId)}/files/${encodeURIComponent(fileId)}/content`;
    const {response}=await api(path,{raw:true}); const blob=await response.blob();
    if(requestId!==state.request || sequence!==state.previewSequence || !$('#previewDialog').open) return;
    clearPreview(); state.previewUrl=URL.createObjectURL(blob); const type=response.headers.get('content-type') || '';
    if(type.includes('pdf')) $('#previewBody').innerHTML=`<iframe title="原始 PDF" src="${state.previewUrl}"></iframe>`;
    else if(type.startsWith('image/')) $('#previewBody').innerHTML=`<img alt="原始文件页面" src="${state.previewUrl}">`;
    else if(type.startsWith('text/') || type.includes('json')){const pre=document.createElement('pre');pre.textContent=await blob.text();$('#previewBody').replaceChildren(pre);}
    else $('#previewBody').innerHTML='<div class="blank">此格式需使用本机 Word / Excel 打开。可下载冻结原件继续核对。</div>';
    $('#downloadFile').disabled=false;
    $('#downloadFile').onclick=async()=>{
      if(Number.isInteger(page)){
        const {response:original}=await api(`/v1/runs/${encodeURIComponent(state.runId)}/files/${encodeURIComponent(fileId)}/content`,{raw:true}); downloadBlob(await original.blob(),fileName);
      } else downloadBlob(blob,fileName);
    };
  } catch(error){if(error.name!=='AbortError' && requestId===state.request && sequence===state.previewSequence)$('#previewBody').textContent=error.message;}
}
async function exportResult(){if(!state.runId)return;const {body}=await api(`/v1/runs/${encodeURIComponent(state.runId)}/report`);const blob=new Blob([JSON.stringify(body,null,2)],{type:'application/json'});const link=document.createElement('a');const url=URL.createObjectURL(blob);link.href=url;link.download=`compliance-${state.runId}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
async function controlRun(){if(!state.runId||!state.run)return;const action=state.run.control_state==='paused'||state.run.state==='paused'?'resume':'pause';try{await api(`/inspection/tasks/${encodeURIComponent(state.run.taskId)}/control`,{method:'POST',body:JSON.stringify({action}),persist:true});notify(action==='pause'?'任务已请求暂停':'任务已请求恢复','success');await loadRun(state.runId);}catch(error){notify(error.message,'error');}}
async function poll() {
  if(document.hidden || !state.connected || state.loading || state.view!=='runs') return;
  state.loading=true;
  const requestId=state.request, runId=state.runId;
  try {
    await loadTasks();
    if(runId && state.run && (['queued','running','paused'].includes(state.run.state) || ['waiting','sending'].includes(state.run.delivery?.state))){
      const oldStatus=state.run.result_status;
      const {body}=await api(`/v1/runs/${encodeURIComponent(runId)}/summary`);
      if(requestId!==state.request)return;
      state.run={...state.run,...body};
      if(body.result_status!==oldStatus){state.report=(await api(`/v1/runs/${encodeURIComponent(runId)}/report`)).body.result;state.reviewPlan=(await api(`/v1/runs/${encodeURIComponent(runId)}/explanation`).catch(()=>({body:null}))).body;}
      await loadProgress(requestId);renderRun();updateQuestionPropositions();
      if(!$('#detail-files').hidden)await loadFiles();
      if(!$('#detail-candidates').hidden)await loadCandidates();
    }
  } catch(error){handleError(error);} finally{state.loading=false;}
}
function updatePoll() {
  if(state.poll) clearTimeout(state.poll);
  state.poll=null;
  if(document.hidden || !state.connected || state.view!=='runs')return;
  state.poll=setTimeout(async()=>{await poll();updatePoll();},3000);
}
function handleError(error) {
  if(error?.name==='AbortError') return;
  notify(error?.message || '请求失败，请重试。','error');
}
function clearPreview() {
  if(state.previewUrl) { URL.revokeObjectURL(state.previewUrl); state.previewUrl=null; }
}
function downloadBlob(blob, fileName) {
  const link=document.createElement('a');
  const url=URL.createObjectURL(blob);
  link.href=url; link.download=fileName || 'source-document';
  document.body.append(link); link.click(); link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function openReference(evidenceId) {
  if(!evidenceId) { notify('该命题尚无注册证据。'); return; }
  if(state.view!=='evidence') setView('evidence');
  await inspectEvidence({evidence_id:evidenceId});
  $('#evidenceInspector').scrollIntoView({block:'nearest',behavior:'smooth'});
}
async function submitTask(event) {
  event.preventDefault();
  const button=$('#submitTaskButton');
  if(button.disabled) return;
  button.disabled=true; $('#taskError').textContent='';
  const form=new FormData(event.target);
  const request={mineId:String(form.get('mineId')||'').trim(),checkCycle:String(form.get('checkCycle')||''),executionId:crypto.randomUUID(),status:'running',items:[{itemCode:form.get('itemCode')}]};
  const callback=String(form.get('resultCallbackUrl')||'').trim();
  if(callback) request.resultCallbackUrl=callback;
  try {
    const signature=JSON.stringify({...request,executionId:undefined});
    if(state.pendingSubmission?.signature===signature) request.executionId=state.pendingSubmission.executionId;
    state.pendingSubmission={signature,executionId:request.executionId};
    const {headers}=await api('/inspection/tasks',{method:'POST',body:JSON.stringify(request),persist:true});
    const taskId=headers.get('X-Inspection-Task');
    if(!taskId) throw new Error('任务已受理，但服务未返回任务标识。请刷新运行列表确认。');
    const {body:task}=await api(`/inspection/tasks/${encodeURIComponent(taskId)}/summary`);
    state.pendingSubmission=null; $('#taskDialog').close(); state.tasks.page=0;
    setView('runs'); await loadTasks();
    const run=task.runs?.[0];
    if(run) await loadRun(runIdOf(run));
    notify('检查任务已受理，等待控制 worker 执行。','success');
  } catch(error) {
    if(error?.name!=='AbortError') $('#taskError').textContent=error.message;
  } finally { button.disabled=false; }
}
function bind() {
  window.addEventListener('hashchange',()=>setView(location.hash.replace('#','') || 'runs'));
  $('#refreshButton').onclick=refresh;
  $$('[data-rule-mode]').forEach(button=>button.onclick=()=>{state.ruleMode=button.dataset.ruleMode;$$('[data-rule-mode]').forEach(item=>item.setAttribute('aria-pressed',String(item===button)));loadRules();});
  $('#newTaskButton').onclick=()=>{
    if(!state.connected){notify('服务尚未连接，请刷新页面重试。','error');return;}
    $('#taskError').textContent='';
    $('#taskDialog').showModal();
  };
  $('#taskForm').onsubmit=submitTask;
  $('#filterForm').onsubmit=(event)=>{event.preventDefault();state.tasks.page=0;loadTasks();};
  $('#resetFilters').onclick=()=>{$('#filterMine').value='';$('#filterCycle').value='';state.tasks.page=0;loadTasks();};
  $('#taskPrev').onclick=()=>{state.tasks.page=Math.max(0,state.tasks.page-1);loadTasks();};
  $('#taskNext').onclick=()=>{state.tasks.page++;loadTasks();};
  $('#filePrev').onclick=()=>loadFiles(Math.max(0,state.files.page-1));
  $('#fileNext').onclick=()=>loadFiles(state.files.page+1);
  $('#candidatePrev').onclick=()=>loadCandidates(Math.max(0,state.candidates.page-1));
  $('#candidateNext').onclick=()=>loadCandidates(state.candidates.page+1);
  $('#evidenceForm').onsubmit=(event)=>{event.preventDefault();state.evidence.query=$('#evidenceQuery').value;state.evidence.file=$('#evidenceFile').value;loadEvidence(0);};
  $('#evidencePrev').onclick=()=>loadEvidence(Math.max(0,state.evidence.page-1));
  $('#evidenceNext').onclick=()=>loadEvidence(state.evidence.page+1);
  $('#backToRun').onclick=()=>setView('runs');
  $('#jumpEvidence').onclick=()=>setView('evidence');
  $('#controlButton').onclick=controlRun;
  $('#exportButton').onclick=()=>exportResult().catch(handleError);
  $('#navigationFilter').onchange=()=>{if(state.navigation)renderNavigation(state.navigation);};
  $('#questionForm').onsubmit=askQuestion;
  $('#compareForm').onsubmit=compareRuns;
  $('#refreshAudit').onclick=loadAudit;
  $('#reviewForm').onsubmit=submitReview;
  $('#cancelReview').onclick=()=>{$('#reviewForm').hidden=true;state.selectedReview=null;};
  $('#progressMore').onclick=()=>loadProgress();
  $$('[data-close]').forEach((button)=>button.onclick=()=>document.getElementById(button.dataset.close).close());
  $('#previewDialog').addEventListener('close',clearPreview);
  $$('.detail-tabs [role="tab"]').forEach((tab)=>tab.onclick=()=>{
    $$('.detail-tabs [role="tab"]').forEach((item)=>item.setAttribute('aria-selected',String(item===tab)));
    ['propositions','files','candidates','audit'].forEach((item)=>$(`#detail-${item}`).hidden=item!==tab.dataset.detail);
    if(tab.dataset.detail==='files')loadFiles();
    if(tab.dataset.detail==='candidates')loadCandidates();
    if(tab.dataset.detail==='audit')loadAudit();
  });
  document.addEventListener('click',(event)=>{
    const row=event.target.closest('#taskRows tr[data-run-id]'); if(row){loadRun(row.dataset.runId);return;}
    const preview=event.target.closest('.preview-file'); if(preview){previewFile(preview.dataset.fileId,preview.dataset.fileName,preview.dataset.page?Number(preview.dataset.page):undefined);return;}
    const evidence=event.target.closest('.evidence-item'); if(evidence){inspectEvidence(state.evidenceRows[Number(evidence.dataset.evidenceIndex)],evidence);return;}
    const ref=event.target.closest('[data-ref]'); if(ref){openReference(ref.dataset.ref);return;}
    const prop=event.target.closest('.proposition-evidence'); if(prop){openReference(prop.dataset.refs?.split(',')[0]);return;}
    const review=event.target.closest('.review-candidate');if(review){startReview(Number(review.dataset.candidateIndex));return;}
  });
  document.addEventListener('visibilitychange',()=>{
    if(document.hidden){abortRequests();if(state.poll)clearTimeout(state.poll);state.poll=null;}
    else if(state.connected){if(state.view==='runs')poll();else if(state.view==='evidence'){loadEvidence();loadNavigation();}updatePoll();}
  });
  window.addEventListener('pagehide',()=>{abortRequests();clearPreview();if(state.poll)clearTimeout(state.poll);});
}

async function refresh() {
  if(!await loadRuntime())return;
  await loadTasks();
  if(state.runId)await loadRun(state.runId);
  if(state.view==='rules')await loadRules();
  if(state.view==='evidence'){await loadEvidence();await loadNavigation();}
  updatePoll();
}
$('#newCycle').value=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit'}).format(new Date());
bind();setView(state.view);refresh();
