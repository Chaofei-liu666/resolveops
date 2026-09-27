const state = { activeView: 'dashboard', cases: [], selectedCaseId: null, selectedDetail: null, runtime: null, identity: null, tracePoller: null, tracePollBusy: false };
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const key = () => localStorage.getItem('resolveops.operatorKey') || '';
const setKey = (value) => localStorage.setItem('resolveops.operatorKey', value);
const clearKey = () => localStorage.removeItem('resolveops.operatorKey');
const statusLabel = (value) => ({ queued: '排队中', running: '处理中', waiting_approval: '等待审批', resolved: '已解决', manual_review: '人工处理', replanning: '正在 Replan', approved: '已批准', pending: '待审批', rejected: '已驳回', expired: '已过期' }[value] || value || '未知');
const statusTone = (value) => ['resolved', 'approved', 'success', 'ready'].includes(value) ? 'ok' : ['waiting_approval', 'pending', 'queued', 'replanning'].includes(value) ? 'warn' : ['manual_review', 'failed', 'rejected', 'expired'].includes(value) ? 'bad' : 'muted';
const dateText = (value) => value ? new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—';
const pretty = (value) => JSON.stringify(value ?? {}, null, 2);

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'X-Operator-Key': key(), ...(options.headers || {}) } });
  if (!response.ok) { const body = await response.json().catch(() => ({ detail: response.statusText })); throw new Error(body.detail || response.statusText); }
  return response.status === 204 ? null : response.json();
}

function setApiStatus(label, tone = 'muted') {
  const node = $('#api-status'); node.className = `status-pill ${tone}`; node.textContent = label;
  $('#identity-label').textContent = state.identity ? `${state.identity.subject} · ${state.identity.role}` : '连接本地服务';
  $('#identity-dot').className = `dot ${tone}`;
}

function identityText() {
  return state.identity ? `${state.identity.subject} · ${state.identity.role} · tenant=${state.identity.tenant_id}` : '当前浏览器尚未验证身份。';
}

function openIdentityDialog() {
  const guide = $('#role-guide');
  if (state.identity && [...guide.options].some((option) => option.value === state.identity.role)) guide.value = state.identity.role;
  $('#operator-key').value = '';
  $('#identity-current').textContent = identityText();
  $('#identity-dialog').showModal();
}

async function useOperatorKey(value, requestedRole) {
  const previousKey = key();
  if (!value) throw new Error('未找到可用的 Operator API Key。');
  setKey(value);
  const identity = await refreshData();
  if (!identity) {
    if (previousKey) { setKey(previousKey); await refreshData(); } else clearKey();
    throw new Error('服务器未接受此 Key。请检查本机演示身份配置。');
  }
  if (requestedRole && identity.role !== requestedRole) {
    setKey(previousKey);
    await refreshData();
    throw new Error(`服务器验证出的角色是 ${identity.role}，与所选角色不一致。`);
  }
  $('#identity-current').textContent = identityText();
  return identity;
}

async function switchIdentity() {
  const button = $('#switch-identity'); const role = $('#role-guide').value;
  button.disabled = true; const original = button.textContent; button.textContent = '验证中…';
  try {
    const manualKey = $('#operator-key').value.trim();
    if (manualKey) await useOperatorKey(manualKey);
    else {
      if (!window.resolveOpsDesktop?.demoRoleKey) throw new Error('自动切换仅在新版 ResolveOps Desktop 安装版可用；请手动输入 Key。');
      await useOperatorKey(await window.resolveOpsDesktop.demoRoleKey(role), role);
    }
    $('#identity-dialog').close();
  } catch (error) { $('#identity-current').textContent = `切换失败：${error.message}`; }
  finally { button.disabled = false; button.textContent = original; }
}

function activate(view) {
  state.activeView = view;
  document.querySelectorAll('[data-panel]').forEach((panel) => { const visible = panel.dataset.panel === view; panel.hidden = !visible; panel.classList.toggle('is-visible', visible); });
  document.querySelectorAll('[data-view]').forEach((button) => button.classList.toggle('is-active', button.dataset.view === view));
  const names = { dashboard: ['运营总览', '订单异常工作台'], cases: ['CASE WORKSPACE', 'Case 工作台'], approvals: ['HUMAN-IN-THE-LOOP', '待审批'], traces: ['TRACE / EVENTS', '执行轨迹'], settings: ['LOCAL CONFIGURATION', '系统配置'] };
  $('#page-eyebrow').textContent = names[view][0]; $('#page-title').textContent = names[view][1];
  if (view === 'settings') loadSettings();
  if (view === 'traces') { renderTrace(); syncTracePolling(); }
  else stopTracePolling();
}

function pendingApprovals() { return state.cases.filter((item) => (item.approval_statuses || []).includes('pending')); }

function renderDashboard() {
  const waiting = state.cases.filter((item) => item.status === 'waiting_approval').length;
  const running = state.cases.filter((item) => ['queued', 'running', 'replanning'].includes(item.status)).length;
  const resolved = state.cases.filter((item) => item.status === 'resolved').length;
  const metrics = [['待处理 Case', running, '需要 Agent 继续处理'], ['待审批', waiting, '等待人工确认'], ['已解决', resolved, '当前列表中的完成 Case'], ['服务状态', state.runtime?.status === 'ready' ? '正常' : '待检查', state.runtime?.status === 'ready' ? '数据库与迁移可用' : '连接管理员后刷新']];
  $('#dashboard-metrics').innerHTML = metrics.map(([label, value, detail], index) => `<article class="metric-card metric-${index}"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(detail)}</small></article>`).join('');
  $('#dashboard-cases').innerHTML = state.cases.slice(0, 6).map(caseRow).join('') || empty('暂无可见 Case', '创建或接收 ERP 事件后会显示在这里。');
  $('#dashboard-approvals').innerHTML = pendingApprovals().slice(0, 4).map((item) => `<button class="approval-row" data-open-case="${esc(item.id)}"><span><strong>${esc(item.order_id)}</strong><small>${esc((item.actions || []).join(' · ') || item.event_type)}</small></span><b class="status-pill warn">待审批</b></button>`).join('') || empty('没有待审批事项', '通过审批的 Action 才会进入 Executor。');
  document.querySelectorAll('[data-open-case]').forEach((button) => button.addEventListener('click', () => selectCase(button.dataset.openCase, true)));
}

function caseRow(item) {
  return `<button class="case-row ${item.id === state.selectedCaseId ? 'selected' : ''}" data-open-case="${esc(item.id)}"><span class="row-main"><strong>${esc(item.order_id)}</strong><small>${esc(item.event_type)} · ${esc((item.actions || []).join(' · ') || '等待计划')}</small></span><span class="row-tail"><em class="status-pill ${statusTone(item.status)}">${esc(statusLabel(item.status))}</em><small>${esc(dateText(item.updated_at))}</small></span></button>`;
}

function empty(title, detail) { return `<div class="empty-inline"><strong>${esc(title)}</strong><span>${esc(detail)}</span></div>`; }

function renderCaseList() {
  $('#case-count').textContent = state.cases.length || '0'; $('#case-rail-count').textContent = `${state.cases.length} 条`;
  $('#case-list').innerHTML = state.cases.map(caseRow).join('') || empty('暂无 Case', '等待 ERP 事件。');
  document.querySelectorAll('[data-open-case]').forEach((button) => button.addEventListener('click', () => selectCase(button.dataset.openCase, true)));
}

function actionCards(actions) {
  if (!actions.length) return empty('暂无 Action Plan', 'Agent 仍在读取业务事实或已安全停止。');
  return actions.map((action) => `<article class="action-card"><div class="card-row"><strong>${esc(action.action_type)}</strong><span class="mono">${esc(action.action_id || 'Action ID 待记录')}</span></div><p>${esc(action.rationale || '受控 Action，等待 Grounding、Policy 与 Approval。')}</p><div class="evidence-refs">Evidence：${esc((action.evidence_refs || []).join(' · ') || '尚未绑定')}</div></article>`).join('');
}

function renderCaseDetail() {
  const detail = state.selectedDetail; if (!detail) return;
  const conclusion = detail.evidence?.conclusion || {}; const actions = Array.isArray(detail.plan?.actions) ? detail.plan.actions : [];
  const observations = detail.tool_trace?.observations || detail.evidence?.observations || [];
  const decision = detail.agent_decision || {};
  $('#case-detail').className = 'case-detail';
  $('#case-detail').innerHTML = `<header class="case-heading"><div><p class="eyebrow">${esc(detail.event_type)}</p><h2>${esc(detail.order_id)} <span class="status-pill ${statusTone(detail.status)}">${esc(statusLabel(detail.status))}</span></h2><p>Case ID · <span class="mono">${esc(detail.id)}</span> · Plan v${esc(detail.plan_version)}</p></div><button class="button secondary" data-go="traces">查看执行轨迹</button></header>
    <div class="case-summary"><section><h3>Agent 决策</h3><p>${esc(conclusion.rationale || '尚未形成可执行结论。')}</p><div class="fact-chips">${(decision.evidence_summary || []).slice(0, 4).map((fact) => `<span>${esc(fact)}</span>`).join('') || '<span>等待确认业务事实</span>'}</div></section><section><h3>已确认 Observation</h3><strong>${observations.length}</strong><p>每条 Observation 都可在执行轨迹中回溯到对应 Tool 与 Evidence。</p></section></div>
    <section class="case-section"><div class="surface-head"><div><p class="eyebrow">ACTION PROPOSAL</p><h3>推荐处理方案</h3></div></div>${actionCards(actions)}</section>
    <section class="case-section"><div class="surface-head"><div><p class="eyebrow">AUDITABLE DECISION</p><h3>决策依据</h3></div></div><ul class="audit-list">${(decision.decision_trace || []).map((item) => `<li>${esc(item)}</li>`).join('') || '<li>尚无可展示的审计决策依据。</li>'}</ul></section>
    <section class="case-section"><div class="surface-head"><div><p class="eyebrow">HUMAN-IN-THE-LOOP</p><h3>审批状态</h3></div></div>${approvalCards(detail.approvals || [])}</section>`;
  document.querySelectorAll('[data-go]').forEach((button) => button.addEventListener('click', () => activate(button.dataset.go)));
  bindApprovalActions();
}

function approvalCards(approvals) {
  if (!approvals.length) return empty('暂无审批', '当前 Case 尚未进入人工审批环节。');
  return approvals.map((approval) => `<article class="approval-card"><div class="card-row"><strong>${esc(approval.action?.action_type || 'Action')}</strong><span class="status-pill ${statusTone(approval.status)}">${esc(statusLabel(approval.status))}</span></div><dl><dt>Plan</dt><dd>v${esc(approval.plan_version)}</dd><dt>需要角色</dt><dd>${esc((approval.required_roles || []).join(' · ') || '—')}</dd><dt>已批准</dt><dd>${esc((approval.approved_roles || []).join(' · ') || '—')}</dd></dl>${approval.status === 'pending' ? `<div class="approval-actions"><button class="button" data-approve="${esc(approval.id)}">批准</button><button class="button secondary" data-reject="${esc(approval.id)}">驳回并 Replan</button></div>` : ''}</article>`).join('');
}

function bindApprovalActions() {
  document.querySelectorAll('[data-approve]').forEach((button) => button.addEventListener('click', () => approve(button.dataset.approve, button)));
  document.querySelectorAll('[data-reject]').forEach((button) => button.addEventListener('click', () => rejectAndReplan(button.dataset.reject, button)));
}

async function approve(approvalId, button) {
  button.disabled = true;
  try { await api(`/v1/approvals/${encodeURIComponent(approvalId)}/approve`, { method: 'POST' }); await refreshData(); await selectCase(state.selectedCaseId, false); }
  catch (error) { window.alert(`审批未完成：${error.message}`); }
  finally { button.disabled = false; }
}

async function rejectAndReplan(approvalId, button) {
  const reason = window.prompt('请输入驳回原因；系统将保留旧计划并发起新的受限只读调查。');
  if (!reason?.trim()) return;
  button.disabled = true;
  try { await api(`/v1/approvals/${encodeURIComponent(approvalId)}/reject-replan`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason: reason.trim() }) }); await refreshData(); await selectCase(state.selectedCaseId, false); }
  catch (error) { window.alert(`Replan 未启动：${error.message}`); }
  finally { button.disabled = false; }
}

function renderApprovals() {
  const approvalCases = pendingApprovals();
  $('#approval-count').textContent = approvalCases.length || '0';
  $('#approval-list').innerHTML = approvalCases.map((item) => `<button class="approval-card action-link" data-open-case="${esc(item.id)}"><div class="card-row"><strong>${esc(item.order_id)}</strong><span class="status-pill warn">待审批</span></div><p>${esc((item.actions || []).join(' · ') || item.event_type)}</p><small>打开 Case 查看 Action、Evidence 与批准操作</small></button>`).join('') || empty('没有待审批事项', '新的 Action Plan 进入审批后会显示在这里。');
  document.querySelectorAll('[data-open-case]').forEach((button) => button.addEventListener('click', () => selectCase(button.dataset.openCase, true)));
}

function traceStage(event) { return ({ context_built: 'Context Snapshot', agent_start: 'Agent Loop', turn_start: 'LLM Turn', tool_call: 'Tool Call', tool_observation: 'Observation', agent_decision_trace: 'Action Proposal', evidence_grounding_passed: 'Evidence Grounding', policy_passed: 'Policy Check', approval_requested: 'Human Approval', execution_started: 'Executor', verification_passed: 'Verify', replan_requested: 'Replan', handoff: 'Stop / Handoff' }[event.kind] || event.kind); }
function isLiveCase(detail) { return ['queued', 'running', 'replanning'].includes(detail?.status); }

function stopTracePolling() {
  if (state.tracePoller) window.clearInterval(state.tracePoller);
  state.tracePoller = null;
}

async function refreshLiveTrace() {
  if (state.tracePollBusy || state.activeView !== 'traces' || !state.selectedCaseId || !key()) return;
  state.tracePollBusy = true;
  try {
    const detail = await api(`/v1/cases/${encodeURIComponent(state.selectedCaseId)}`);
    const previous = state.selectedDetail;
    const changed = !previous || detail.status !== previous.status || detail.plan_version !== previous.plan_version || (detail.events || []).length !== (previous.events || []).length;
    state.selectedDetail = detail;
    state.cases = state.cases.map((item) => item.id === detail.id ? { ...item, status: detail.status, plan_version: detail.plan_version } : item);
    if (changed) { renderTrace(); renderCaseList(); }
    syncTracePolling();
  } catch (_) {
    // A manual refresh remains available if the local API is restarting.
  } finally { state.tracePollBusy = false; }
}

function syncTracePolling() {
  const shouldPoll = state.activeView === 'traces' && isLiveCase(state.selectedDetail);
  if (!shouldPoll) { stopTracePolling(); return; }
  if (!state.tracePoller) state.tracePoller = window.setInterval(refreshLiveTrace, 900);
}

function renderTrace() {
  const select = $('#trace-case-select'); select.innerHTML = `<option value="">选择一个 Case</option>${state.cases.map((item) => `<option value="${esc(item.id)}">${esc(item.order_id)} · ${esc(statusLabel(item.status))}</option>`).join('')}`; select.value = state.selectedCaseId || '';
  select.onchange = () => selectCase(select.value, false).then(() => renderTrace());
  const detail = state.selectedDetail; if (!detail) return;
  $('#trace-detail').className = 'trace-view';
  const live = isLiveCase(detail);
  $('#trace-detail').innerHTML = `<div class="trace-header"><div><strong>${esc(detail.order_id)}</strong><span class="status-pill ${statusTone(detail.status)}">${esc(statusLabel(detail.status))}</span>${live ? '<span class="status-pill ok">实时更新</span>' : ''}</div><span class="mono">${esc(detail.id)}</span></div><div class="timeline">${(detail.events || []).map((event) => `<article class="timeline-item"><span class="timeline-dot"></span><div><div class="card-row"><strong>${esc(traceStage(event))}</strong><time>${esc(dateText(event.created_at))}</time></div><p>${esc(event.message || '')}</p>${Object.keys(event.data || {}).length ? `<details><summary>技术详情</summary><pre>${esc(pretty(event.data))}</pre></details>` : ''}</div></article>`).join('') || empty('暂无生命周期事件', '历史 Case 可能没有记录新的运行轨迹。')}</div>`;
}

async function selectCase(id, switchToCases) {
  if (!id) return; state.selectedCaseId = id; renderCaseList();
  try { state.selectedDetail = await api(`/v1/cases/${encodeURIComponent(id)}`); renderCaseDetail(); renderTrace(); if (switchToCases) activate('cases'); else syncTracePolling(); }
  catch (error) { $('#case-detail').className = 'case-detail empty'; $('#case-detail').textContent = error.message; }
}

function formValues() {
  const form = $('#connection-form'); const data = Object.fromEntries(new FormData(form).entries());
  Object.entries(data).forEach(([name, value]) => { if (value === '') delete data[name]; if (name === 'llm_timeout_seconds' && value !== '') data[name] = Number(value); });
  return data;
}
function showSettingsResult(message, tone = 'ok') { const node = $('#settings-result'); node.hidden = false; node.className = `notice ${tone}`; node.textContent = message; }
function configState(configured) { return configured ? ['已配置', 'ok'] : ['待配置', 'warn']; }
function serviceCard(label, status, detail, tone) { return `<article class="service-card"><span class="dot ${tone}"></span><div><strong>${esc(label)}</strong><b>${esc(status)}</b><small>${esc(detail)}</small></div></article>`; }

async function loadSettings() {
  const notice = $('#settings-auth-notice');
  if (!key() || !state.identity) { notice.hidden = false; notice.textContent = '当前浏览器未验证管理员身份。左下角可输入本地 Key；页面中的“已配置”仅表示服务端已有连接凭据。'; $('#service-status-grid').innerHTML = ''; return; }
  try {
    const payload = await api('/v1/settings/connections'); const connections = payload.connections; state.runtime = payload.status; notice.hidden = true;
    const form = $('#connection-form'); form.llm_base_url.value = connections.llm.base_url; form.llm_model.value = connections.llm.model; form.llm_timeout_seconds.value = connections.llm.timeout_seconds || 60; form.erpnext_base_url.value = connections.erpnext.base_url;
    const [llmLabel, llmTone] = configState(connections.llm.api_key_configured && connections.llm.base_url && connections.llm.model);
    const [erpLabel, erpTone] = configState(connections.erpnext.api_key_configured && connections.erpnext.api_secret_configured && connections.erpnext.base_url);
    const databaseTone = payload.status.checks?.database?.ok ? 'ok' : 'bad';
    $('#llm-state').className = `status-pill ${llmTone}`; $('#llm-state').textContent = llmLabel; $('#erp-state').className = `status-pill ${erpTone}`; $('#erp-state').textContent = erpLabel; $('#database-state').className = `status-pill ${databaseTone}`; $('#database-state').textContent = payload.status.checks?.database?.ok ? '正常' : '异常';
    $('#database-summary').textContent = connections.database.configured ? `${connections.database.dialect || '数据库'} · ${connections.database.host || '本地'} · ${connections.database.database || '默认库'}` : '尚未配置数据库连接。';
    $('#service-status-grid').innerHTML = [serviceCard('数据库', payload.status.checks?.database?.ok ? '正常' : '异常', '当前运行数据库', databaseTone), serviceCard('LLM', llmLabel, '服务端已有模型连接凭据', llmTone), serviceCard('ERPNext', erpLabel, '服务端已有 ERP 连接凭据', erpTone)].join('');
  } catch (error) { notice.hidden = false; notice.textContent = `无法读取配置：${error.message}`; $('#service-status-grid').innerHTML = ''; }
}

async function testConnection(target, button) {
  button.disabled = true; const original = button.textContent; button.textContent = '检查中…';
  try { const result = await api('/v1/settings/connections/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target, ...formValues() }) }); showSettingsResult(result.message, 'ok'); }
  catch (error) { showSettingsResult(error.message, 'bad'); }
  finally { button.disabled = false; button.textContent = original; }
}

async function saveConnections(event) {
  event.preventDefault(); const submit = event.submitter || $('#connection-form button[type="submit"]'); submit.disabled = true;
  try { const result = await api('/v1/settings/connections', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(formValues()) }); showSettingsResult(result.message, result.restart_required ? 'warning' : 'ok'); $('#connection-form').querySelectorAll('input[type="password"]').forEach((input) => input.value = ''); await loadSettings(); }
  catch (error) { showSettingsResult(error.message, 'bad'); }
  finally { submit.disabled = false; }
}

function openCreateCaseDialog() {
  if (!key() || !state.identity) {
    $('#identity-current').textContent = '创建 Case 前需要先验证本地操作员身份。';
    $('#identity-dialog').showModal();
    return;
  }
  $('#create-case-dialog').showModal();
}

async function createCase(event) {
  event.preventDefault();
  const form = $('#create-case-form'); const submit = $('#submit-create-case');
  const payload = Object.fromEntries(new FormData(form).entries());
  submit.disabled = true; submit.textContent = '创建中…';
  try {
    const result = await api('/v1/cases', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    $('#create-case-dialog').close(); await refreshData(); await selectCase(result.case_id, true);
  } catch (error) { window.alert(`Case 未创建：${error.message}`); }
  finally { submit.disabled = false; submit.textContent = '创建并开始调查'; }
}

async function refreshData() {
  if (!key()) { state.identity = null; setApiStatus('未认证', 'muted'); renderDashboard(); renderCaseList(); renderApprovals(); return null; }
  try {
    state.identity = await api('/v1/operator/me');
    state.cases = await api('/v1/cases?limit=50'); $('#case-count').textContent = state.cases.length;
    try { state.runtime = await api('/v1/runtime/status'); } catch (_) { state.runtime = null; }
    setApiStatus(`已认证 · ${state.identity.role}`, 'ok'); renderDashboard(); renderCaseList(); renderApprovals(); if (state.selectedCaseId) await selectCase(state.selectedCaseId, false); return state.identity;
  } catch (error) { state.identity = null; setApiStatus('认证失败', 'bad'); $('#dashboard-cases').innerHTML = empty('无法读取 Case', error.message); return null; }
}

document.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => activate(button.dataset.view)));
document.querySelectorAll('[data-go]').forEach((button) => button.addEventListener('click', () => activate(button.dataset.go)));
document.querySelectorAll('[data-open-create-case]').forEach((button) => button.addEventListener('click', openCreateCaseDialog));
document.querySelectorAll('[data-open-identity]').forEach((button) => button.addEventListener('click', openIdentityDialog));
$('#refresh-button').addEventListener('click', refreshData); $('#settings-refresh').addEventListener('click', loadSettings);
$('#connection-form').addEventListener('submit', saveConnections); document.querySelectorAll('[data-test]').forEach((button) => button.addEventListener('click', () => testConnection(button.dataset.test, button)));
$('#identity-button').addEventListener('click', openIdentityDialog);
$('#switch-identity').addEventListener('click', switchIdentity);
$('#clear-key').addEventListener('click', () => { clearKey(); state.identity = null; $('#operator-key').value = ''; $('#identity-current').textContent = '已退出当前身份。'; setApiStatus('未认证', 'muted'); renderDashboard(); renderCaseList(); renderApprovals(); });
$('#create-case-form').addEventListener('submit', createCase); $('#cancel-create-case').addEventListener('click', () => $('#create-case-dialog').close());
if (key()) refreshData(); else { renderDashboard(); renderCaseList(); renderApprovals(); }
