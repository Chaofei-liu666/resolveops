const state = {
  selected: null,
  cases: [],
  detail: null,
  audit: [],
  auditError: null,
  evalSummary: null,
  evalError: null,
  eventFilter: "important",
};

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  "\"": "&quot;",
  "'": "&#39;",
}[c]));
const pretty = (value) => JSON.stringify(value ?? {}, null, 2);
const key = () => localStorage.getItem("resolveops.operatorKey") || "";
const operator = () => localStorage.getItem("resolveops.operator") || "console-operator";
const role = () => localStorage.getItem("resolveops.operatorRole") || "operator";

function headers(extra = {}) {
  return {
    "X-Operator-Key": key(),
    "X-Operator": operator(),
    "X-Operator-Role": role(),
    ...extra,
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...headers(options.headers || {}) },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail || response.statusText);
  }
  return response.json();
}

function statusClass(status) {
  return `status ${escapeHtml(status || "muted")}`;
}

function renderCaseList() {
  $("#case-list").innerHTML = state.cases.map((item) => `
    <div class="case-item ${item.id === state.selected ? "active" : ""}" data-case="${escapeHtml(item.id)}">
      <strong>${escapeHtml(item.order_id)}</strong>
      <small>${escapeHtml(item.id)}</small>
      <small>${escapeHtml(item.actions?.join(" + ") || "no plan yet")}</small>
      <span class="${statusClass(item.status)}">${escapeHtml(item.status)}</span>
    </div>
  `).join("");

  document.querySelectorAll("[data-case]").forEach((node) => {
    node.addEventListener("click", () => selectCase(node.dataset.case));
  });
}

function percent(value) {
  return `${Math.round((Number(value || 0)) * 100)}%`;
}

function evalSummaryHtml() {
  if (state.evalError) return `<section class="card"><h3>Evaluation summary</h3><p class="muted">${escapeHtml(state.evalError)}</p></section>`;
  if (!state.evalSummary) return "";
  const item = state.evalSummary;
  return `
    <section class="card">
      <div class="card-title-row">
        <h3>Evaluation summary</h3>
        <span class="badge">latest ${escapeHtml(item.total_cases)} cases</span>
      </div>
      <div class="metric-grid">
        <div class="metric"><strong>${escapeHtml(percent(item.case_resolution_rate))}</strong><span>Resolution rate</span></div>
        <div class="metric"><strong>${escapeHtml(percent(item.verification_pass_rate))}</strong><span>Verification pass rate</span></div>
        <div class="metric"><strong>${escapeHtml(item.replanned_cases)}</strong><span>Replanned cases</span></div>
        <div class="metric"><strong>${escapeHtml(item.manual_handoff_cases)}</strong><span>Manual handoffs</span></div>
        <div class="metric"><strong>${escapeHtml(item.evidence_grounding_failures)}</strong><span>Grounding failures</span></div>
        <div class="metric"><strong>${escapeHtml(item.policy_denials)}</strong><span>Policy denials</span></div>
        <div class="metric"><strong>${escapeHtml(item.task_failures)}</strong><span>Task failures</span></div>
      </div>
    </section>
  `;
}

function observation(tool) {
  return (state.detail?.evidence?.observations || []).filter((item) => item.tool === tool);
}

function evidenceHtml() {
  const order = observation("get_order")[0]?.result;
  const targetStock = observation("get_inventory").find((x) => x.arguments?.warehouse === order?.items?.[0]?.warehouse)?.result;
  const sourceStocks = observation("get_inventory").filter((x) => x.arguments?.warehouse !== order?.items?.[0]?.warehouse);
  const customer = observation("get_customer_profile")[0]?.result;
  const item = observation("get_item_supply_profile")[0]?.result;
  const lanes = observation("get_transfer_options")[0]?.result?.lanes || [];
  const inbound = observation("get_inbound_purchase")[0]?.result?.purchase_items || [];

  return `
    <div class="kv">
      <div>Order</div><div>${escapeHtml(order?.name || state.detail?.order_id)}</div>
      <div>Item / qty</div><div>${escapeHtml(order?.items?.[0]?.item_code)} / ${escapeHtml(order?.items?.[0]?.qty)}</div>
      <div>Delivery date</div><div>${escapeHtml(order?.delivery_date || order?.items?.[0]?.delivery_date)}</div>
      <div>Target stock</div><div>${escapeHtml(targetStock?.warehouse)} actual ${escapeHtml(targetStock?.actual_qty)} reserved ${escapeHtml(targetStock?.reserved_qty)}</div>
      <div>Source stock</div><div>${sourceStocks.map((x) => `${x.result?.warehouse}: actual ${x.result?.actual_qty}`).map(escapeHtml).join("<br>") || "none"}</div>
      <div>Customer split</div><div>${escapeHtml(customer?.allows_partial_delivery)}</div>
      <div>Lead time</div><div>${escapeHtml(item?.lead_time_days)} days</div>
      <div>Transfer lanes</div><div>${lanes.map((x) => `${x.source} -> ${x.target}: ${x.transit_days} days, ${x.cost_per_unit} ${x.currency}/unit`).map(escapeHtml).join("<br>") || "none"}</div>
      <div>Inbound PO</div><div>${inbound.length ? inbound.map((x) => escapeHtml(`${x.purchase_order}: ${x.remaining_qty}`)).join("<br>") : "none"}</div>
    </div>
  `;
}

function planHtml() {
  const plan = state.detail?.plan;
  const actions = Array.isArray(plan?.actions) ? plan.actions : [];
  if (!actions.length) return `<p class="muted">No active plan.</p>`;
  return actions.map((action) => `
    <div class="action">
      <div class="row">
        <strong>${escapeHtml(action.action_type)}</strong>
        <span class="badge">${escapeHtml(action.policy?.reason || action.risk?.level)}</span>
      </div>
      <div class="code">${escapeHtml(pretty(action.input))}</div>
      <p class="muted">${escapeHtml(action.rationale || "")}</p>
    </div>
  `).join("");
}

function approvalHtml() {
  const approvals = state.detail?.approvals || [];
  if (!approvals.length) return `<p class="muted">No approvals yet.</p>`;
  return approvals.map((approval) => {
    const remaining = (approval.required_roles || []).filter((role) => !(approval.approved_roles || []).includes(role));
    const buttons = remaining.map((role) => `
      <button data-approve="${escapeHtml(approval.id)}" data-role="${escapeHtml(role)}">Approve as ${escapeHtml(role)}</button>
    `).join("");
    return `
      <div class="approval">
        <div class="row">
          <strong>${escapeHtml(approval.action?.action_type)}</strong>
          <span class="badge ${escapeHtml(approval.status)}">${escapeHtml(approval.status)}</span>
        </div>
        <div class="kv">
          <div>Plan version</div><div>v${escapeHtml(approval.plan_version)}</div>
          <div>Required</div><div>${escapeHtml((approval.required_roles || []).join(", "))}</div>
          <div>Approved</div><div>${escapeHtml((approval.approved_roles || []).join(", ") || "none")}</div>
          <div>Action hash</div><div>${escapeHtml((approval.action_hash || "").slice(0, 18))}</div>
        </div>
        <div class="approve-buttons">${approval.status === "pending" ? buttons : ""}</div>
      </div>
    `;
  }).join("");
}

function invocationHtml() {
  const invocations = state.detail?.invocations || [];
  if (!invocations.length) return `<p class="muted">No ERP write invocation.</p>`;
  return invocations.map((item) => `
    <div class="invocation">
      <div class="row">
        <strong>${escapeHtml(item.tool)}</strong>
        <span class="badge ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>
      </div>
      <div class="kv">
        <div>External document</div><div>${escapeHtml(item.external_id || "pending")}</div>
        <div>Idempotency key</div><div>${escapeHtml(item.idempotency_key)}</div>
      </div>
    </div>
  `).join("");
}

const IMPORTANT_EVENTS = new Set([
  "case_created",
  "agent_plan_created",
  "evidence_grounding_failed",
  "approval_partial",
  "approval_granted",
  "replan_requested",
  "verification_passed",
  "verification_failed",
  "manual_review_required",
  "handoff",
  "worker_failure",
  "task_requeued",
]);

function planEvolutionHtml() {
  const approvals = state.detail?.approvals || [];
  const versions = [...new Set(approvals.map((item) => item.plan_version))].sort((a, b) => a - b);
  if (!versions.length) return `<p class="muted">No plan version has been created yet.</p>`;

  return `
    <div class="plan-flow">
      ${versions.map((version) => {
        const versionApprovals = approvals.filter((item) => item.plan_version === version);
        const invalidated = versionApprovals.some((item) => item.status === "invalidated");
        const consumed = versionApprovals.length > 0 && versionApprovals.every((item) => item.status === "consumed");
        const active = version === state.detail.plan_version;
        const status = invalidated ? "invalidated" : consumed ? "consumed" : active ? "active" : "superseded";
        return `
          <div class="plan-version ${status}">
            <div class="row">
              <strong>Plan v${escapeHtml(version)}</strong>
              <span class="badge ${escapeHtml(status)}">${escapeHtml(status)}</span>
            </div>
            ${versionApprovals.map((approval) => `
              <div class="mini-action">
                <div>${escapeHtml(approval.action?.action_type)}</div>
                <span>${escapeHtml(approval.action?.input ? pretty(approval.action.input) : "{}")}</span>
                <em>${escapeHtml(approval.status)}</em>
              </div>
            `).join("")}
          </div>
        `;
      }).join("")}
    </div>
  `;
}

function toolCallHtml() {
  const observations = state.detail?.evidence?.observations || [];
  if (!observations.length) return `<p class="muted">No tool calls recorded.</p>`;
  return observations.map((item, index) => {
    const hasError = Boolean(item.result?.error);
    const metadata = item.metadata || {};
    return `
      <div class="tool-call">
        <div class="row">
          <strong>${index + 1}. ${escapeHtml(item.tool)}</strong>
          <span class="badge ${hasError ? "failed" : "succeeded"}">${hasError ? "error" : "ok"}</span>
        </div>
        <div class="kv compact">
          <div>Permission</div><div>${escapeHtml(metadata.permission || "-")}</div>
          <div>Side effect</div><div>${escapeHtml(metadata.side_effect || "-")}</div>
          <div>Risk</div><div>${escapeHtml(metadata.risk_level || "-")}</div>
          <div>Source</div><div>${escapeHtml(metadata.source_system || "-")}</div>
        </div>
        <div class="code">${escapeHtml(pretty(item.arguments))}</div>
        <details>
          <summary>Result</summary>
          <div class="code">${escapeHtml(pretty(item.result))}</div>
        </details>
      </div>
    `;
  }).join("");
}

function eventFilterHtml() {
  return `
    <div class="segmented">
      <button class="${state.eventFilter === "important" ? "active" : ""}" data-event-filter="important">Important</button>
      <button class="${state.eventFilter === "tools" ? "active" : ""}" data-event-filter="tools">Tool calls</button>
      <button class="${state.eventFilter === "all" ? "active" : ""}" data-event-filter="all">All</button>
    </div>
  `;
}

function eventHtml() {
  const events = state.detail?.events || [];
  const filtered = events.filter((event) => {
    if (state.eventFilter === "all") return true;
    if (state.eventFilter === "tools") return event.kind === "tool_observation";
    return IMPORTANT_EVENTS.has(event.kind);
  });
  if (!filtered.length) return `<p class="muted">No events in this filter.</p>`;
  return filtered.slice().reverse().map((event) => `
    <div class="event">
      <time>${escapeHtml(event.created_at ? new Date(event.created_at).toLocaleString() : "")}</time>
      <strong>${escapeHtml(event.kind)}</strong>
      <p>${escapeHtml(event.message)}</p>
      ${Object.keys(event.data || {}).length ? `<details><summary>Event data</summary><div class="code">${escapeHtml(pretty(event.data))}</div></details>` : ""}
    </div>
  `).join("");
}

function auditHtml() {
  if (state.auditError) return `<p class="muted">${escapeHtml(state.auditError)}</p>`;
  if (!state.audit.length) return `<p class="muted">No audit records visible for current role.</p>`;
  return state.audit.map((item) => `
    <div class="audit-row">
      <div class="row">
        <strong>${escapeHtml(item.action)}</strong>
        <span class="badge">${escapeHtml(item.role)}</span>
      </div>
      <div class="kv">
        <div>Actor</div><div>${escapeHtml(item.actor)}</div>
        <div>Resource</div><div>${escapeHtml(item.resource_type)} / ${escapeHtml(item.resource_id)}</div>
        <div>Case</div><div>${escapeHtml(item.case_id || "-")}</div>
        <div>Time</div><div>${escapeHtml(item.created_at ? new Date(item.created_at).toLocaleString() : "")}</div>
      </div>
      ${Object.keys(item.data || {}).length ? `<details><summary>Audit data</summary><div class="code">${escapeHtml(pretty(item.data))}</div></details>` : ""}
    </div>
  `).join("");
}

function renderDetail() {
  if (!state.detail) return;
  $("#page-title").textContent = `${state.detail.order_id} · ${state.detail.id}`;
  $("#status-chip").className = statusClass(state.detail.status);
  $("#status-chip").textContent = state.detail.status;
  $("#detail").className = "detail";
  $("#detail").innerHTML = `
    ${evalSummaryHtml()}
    <div class="grid">
      <section class="card">
        <h3>Case</h3>
        <div class="kv">
          <div>Tenant</div><div>${escapeHtml(state.detail.tenant_id)}</div>
          <div>Source event</div><div>${escapeHtml(state.detail.source_event_id)}</div>
          <div>Plan version</div><div>v${escapeHtml(state.detail.plan_version)}</div>
          <div>Updated</div><div>${escapeHtml(state.detail.updated_at ? new Date(state.detail.updated_at).toLocaleString() : "")}</div>
        </div>
      </section>
      <section class="card">
        <h3>Agent conclusion</h3>
        <p class="muted">${escapeHtml(state.detail.evidence?.conclusion?.rationale || "No conclusion yet.")}</p>
        <div class="code">${escapeHtml(pretty(state.detail.evidence?.conclusion?.missing_information || []))}</div>
      </section>
    </div>
    <div class="grid">
      <section class="card"><h3>Evidence</h3>${evidenceHtml()}</section>
      <section class="card"><h3>Action plan</h3>${planHtml()}</section>
    </div>
    <div class="grid">
      <section class="card"><h3>Plan evolution</h3>${planEvolutionHtml()}</section>
      <section class="card"><h3>Tool calls</h3>${toolCallHtml()}</section>
    </div>
    <div class="grid">
      <section class="card"><h3>Approvals</h3>${approvalHtml()}</section>
      <section class="card"><h3>ERP write verification</h3>${invocationHtml()}</section>
    </div>
    <section class="card"><h3>Audit trail</h3>${auditHtml()}</section>
    <section class="card"><div class="card-title-row"><h3>Event timeline</h3>${eventFilterHtml()}</div>${eventHtml()}</section>
  `;

  document.querySelectorAll("[data-approve]").forEach((node) => {
    node.addEventListener("click", () => approve(node.dataset.approve, node.dataset.role));
  });
  document.querySelectorAll("[data-event-filter]").forEach((node) => {
    node.addEventListener("click", () => {
      state.eventFilter = node.dataset.eventFilter;
      renderDetail();
    });
  });
}

async function loadCases() {
  state.cases = await api("/v1/cases?limit=50");
  try {
    state.evalSummary = await api("/v1/evals/summary?limit=50");
    state.evalError = null;
  } catch (error) {
    state.evalSummary = null;
    state.evalError = `Evaluation summary requires ops_admin/config_admin role: ${error.message}`;
  }
  if (!state.selected && state.cases[0]) state.selected = state.cases[0].id;
  renderCaseList();
}

async function loadDetail() {
  if (!state.selected) return;
  state.detail = await api(`/v1/cases/${state.selected}`);
  try {
    state.audit = await api(`/v1/audit?case_id=${encodeURIComponent(state.selected)}&limit=20`);
    state.auditError = null;
  } catch (error) {
    state.audit = [];
    state.auditError = `Audit requires ops_admin/config_admin role: ${error.message}`;
  }
  renderDetail();
}

async function selectCase(id) {
  state.selected = id;
  renderCaseList();
  await loadDetail();
}

async function approve(approvalId, role) {
  const operator = `console-${role}`;
  await api(`/v1/approvals/${approvalId}/approve`, {
    method: "POST",
    headers: {
      "X-Operator": operator,
      "X-Operator-Role": role,
    },
  });
  await refresh();
}

async function refresh() {
  try {
    await loadCases();
    await loadDetail();
  } catch (error) {
    $("#detail").className = "detail empty";
    $("#detail").textContent = error.message;
  }
}

$("#operator-key").value = key();
$("#operator-name").value = operator();
$("#operator-role").value = role();
$("#save-key").addEventListener("click", () => {
  localStorage.setItem("resolveops.operatorKey", $("#operator-key").value.trim());
  localStorage.setItem("resolveops.operator", $("#operator-name").value.trim() || "console-operator");
  localStorage.setItem("resolveops.operatorRole", $("#operator-role").value);
  refresh();
});
$("#refresh").addEventListener("click", refresh);

refresh();
