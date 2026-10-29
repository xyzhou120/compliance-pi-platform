export const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'})[char]);
export const executionLabels = {queued:'排队中',running:'执行中',paused:'已暂停',completed:'已完成',failed:'执行失败'};
export const resultLabels = {pass:'通过',no_pass:'不通过',pending:'待核验',unverifiable:'无法核验',failed:'技术失败'};
export const decisionLabels = {issue_found:'已发现问题',no_issue_observed:'未发现问题',review_required:'监察待核验',missing_required_data:'资料不足，无法核验',system_processing_incomplete:'系统处理未完成'};
export const legacyResultLabels = {...resultLabels, ...decisionLabels, legacy_pending:'历史待处理'};
export const deliveryLabels = {none:'无需回写',waiting:'等待投递',sending:'投递中',delivered:'已送达',failed:'投递失败'};
export const candidateLabels = {candidate:'候选待验证',source_bound:'来源已绑定'};
export const stageLabels = {run:'运行',source:'资料准备',query:'清单查询',parse:'原生解析',recognition:'识别取证',candidates:'候选验证',pi:'Pi 取证',lease:'租约',evaluation:'确定性评估',callback:'结果回写'};
export const eventLabels = {accepted:'已受理',claimed:'已领取',started:'开始',finished:'结束',submitted:'已提交',requested:'已请求',heartbeat:'续租',checkpoint:'检查点',timing:'耗时记录',tool_request:'工具请求'};
export const propositionLabels = {
 trigger_confirmed:'确认预警或动力事件', threshold_applicable:'适用批准阈值', danger_assessment:'危险研判', stop_and_power_off:'停工与断电', evacuation_executed:'实际撤人', dispatch_reported:'报告调度', relief_completed:'卸压与验收',
 trigger:'触发条件',threshold_trigger:'监测超预警阈值',danger_confirmed:'危险研判结论',warning_danger_confirmed:'预警后危险确认',relief_required:'是否需要解危',relief_measures:'解危施工与验收',relief_violation:'未解危仍继续采掘',applicable_design:'适用防冲设计',stop:'停止作业',power_off:'切断电源',evacuate:'撤人',report:'报告调度',stop_immediate:'立即停工',power_off_immediate:'立即断电',relief:'卸压解危',hazard_analysis:'危险研判',risk_and_design_scope:'评价与设计适用范围',relief_parameters_acceptance:'施工参数与验收',personnel_and_equipment:'人员与设备',signatures_and_notification:'签署与告知',signature_evidence:'确认签字',
};
export const roleLabels = {actual_record:'实际记录',requirement:'批准要求',statement:'陈述 / 候选',regulation:'法规依据'};
export const materialLabels = {prevention_daily:'防冲工作日报表',threshold:'冲击危险性预警临界指标',dispatch_daily:'调度日报',release:'生产组织通知单',relief_acceptance:'防冲钻孔验收记录',design:'防冲设计',analysis:'日分析记录',workface_risk:'工作面危险性评价',coal_risk:'煤层危险性评价',personnel:'人员记录'};
export const cadenceLabels = {daily:'日',weekly:'周',monthly:'月',annual:'年',half_year:'半年',persistent:'按有效版本'};
export const label = (mapping, value, fallback='—') => mapping[value] || value || fallback;
export function propositionName(value) { return propositionLabels[value] || value || '未命名命题'; }
export function locatorText(locator = {}) {
 const parts=[];
 if (locator.page) parts.push(`第 ${locator.page} 页`);
 if (locator.sheet) parts.push(`工作表 ${locator.sheet}`);
 if (locator.table) parts.push(`表 ${locator.table}`);
 if (locator.row) parts.push(`第 ${locator.row} 行`);
 if (locator.column) parts.push(`列 ${locator.column}`);
 if (locator.cell) parts.push(`单元格 ${locator.cell}`);
 if (locator.line) parts.push(`第 ${locator.line} 行`);
 if (locator.paragraph) parts.push(`第 ${locator.paragraph} 段`);
 if (locator.bbox) parts.push(`区域 ${JSON.stringify(locator.bbox)}`);
 return parts.join(' · ') || '定位待补充';
}
export function pageInfo(offset, count, total) { return total ? `${offset+1}–${offset+count} / ${total}` : '0 条'; }
export function dateText(value) {
 if (value == null || value === '') return '—';
 const date = new Date(typeof value === 'number' ? value*1000 : value);
 return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN',{hour12:false});
}
export function reasonText(reason) {
 const processing={pdf_renderer_child_crashed:'PDF渲染子进程异常退出',pdf_renderer_timeout:'PDF页面渲染超时',pdf_renderer_busy:'渲染容量等待超时',page_out_of_range:'页码超出文件范围',page_pixel_budget_exceeded:'页面像素超出处理限制',page_renderer_unavailable:'页面渲染组件不可用',provider_failed:'识别处理失败',parse_file_failed:'文件解析失败',recognize_page_failed:'页面识别失败',run_finalized_before_unit_completed:'本轮结束时该处理未完成'};
 if(processing[reason])return processing[reason];
 const conflict = {'事实证据冲突':'资料中的同一事实出现互斥记录，需核对原文','版本候选':'同类资料存在多个版本线索，系统未自动选择最新版','并列资料':'同类资料允许多份并存'};
 for (const [key,value] of Object.entries(conflict)) if(String(reason).startsWith(key)) return value + String(reason).slice(key.length);
 const known = {pi_execution_disabled:'本轮未启用 Pi 取证',provider_endpoint_missing:'未配置识别服务',semantic_fields_not_independently_verified:'字段语义尚未独立验证',source_bound:'来源已绑定',candidate:'候选待验证',tool_budget_exhausted:'工具调用预算已用尽',tool_failed:'取证工具调用失败',source_complete_false:'资料范围完整性未确认',model_status_ignored:'模型状态声明未采纳'};
 return known[reason] || reason;
}
