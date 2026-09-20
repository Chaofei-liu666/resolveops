"""Textual workbench for the ResolveOps operator CLI.

The workbench is intentionally a client of the ResolveOps API.  It does not
talk to ERPNext, hold credentials other than the existing operator key, or
implement a second Agent loop.  The server remains responsible for Case state,
tool execution, policy, approvals, and verification.
"""
from __future__ import annotations

import json
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    RichLog,
    Select,
    Static,
)
from textual.widgets.option_list import Option
from rich.text import Text

from .api_client import ApiClient, ApiClientError, compact_json
from .config import settings


EVENT_STYLE: dict[str, tuple[str, str]] = {
    'case_created': ('CASE', 'white'), 'context_built': ('CONTEXT', 'white'),
    'worker_task_started': ('WORKER', 'blue'),
    'agent_start': ('AGENT', 'magenta'), 'turn_start': ('TURN', 'dim'),
    'tool_call': ('TOOL', 'cyan'), 'tool_result': ('OBSERVATION', 'cyan'),
    'turn_end': ('TURN', 'dim'), 'agent_end': ('STOP', 'yellow'),
    'tool_scheduled': ('TOOL', 'cyan'), 'tool_observation': ('OBSERVATION', 'cyan'),
    'case_question_tool_called': ('TOOL', 'cyan'), 'case_question_tool_observation': ('OBSERVATION', 'cyan'),
    'agent_decision_trace': ('PLAN', 'magenta'), 'agent_plan_created': ('PLAN', 'magenta'),
    'evidence_grounding_passed': ('GROUNDING', 'green'), 'evidence_grounding_failed': ('GROUNDING', 'red'),
    'approval_requested': ('APPROVAL', 'yellow'), 'approval_partial': ('APPROVAL', 'yellow'),
    'approval_granted': ('APPROVAL', 'green'), 'approval_expired': ('APPROVAL', 'red'), 'approval_revoked': ('APPROVAL', 'red'), 'approval_rejected': ('APPROVAL', 'yellow'),
    'execution_started': ('EXECUTOR', 'blue'), 'verification_passed': ('VERIFY', 'green'), 'verification_failed': ('VERIFY', 'red'),
    'execution_blocked': ('EXECUTOR', 'yellow'),
    'replan_requested': ('REPLAN', 'yellow'), 'task_requeued': ('REPLAN', 'yellow'),
    'plan_repair_requested': ('REPLAN', 'yellow'), 'plan_repair_succeeded': ('REPLAN', 'green'), 'plan_repair_failed': ('REPLAN', 'red'),
    'handoff': ('STOP', 'red'), 'manual_review_required': ('STOP', 'red'), 'worker_failure': ('STOP', 'red'),
    'context_isolation_failed': ('STOP', 'red'), 'context_isolation_sanitized': ('CONTEXT', 'yellow'),
    'policy_passed': ('POLICY', 'green'), 'policy_denied': ('POLICY', 'red'),
    'case_question_asked': ('ASK', 'green'), 'case_question_answered': ('ANSWER', 'blue'),
}

STATUS_STYLE = {
    'queued': 'dim',
    'running': 'cyan',
    'waiting_approval': 'yellow',
    'approved': 'yellow',
    'replanning': 'cyan',
    'resolved': 'green',
    'manual_review': 'red',
}

STATUS_TEXT = {
    'queued': '排队中', 'running': '处理中', 'waiting_approval': '等待审批', 'approved': '已批准', 'replanning': '重新调查中',
    'resolved': '已解决', 'manual_review': '人工审核', 'unknown': '未知',
}
EVENT_TEXT = {
    'case_created': '已创建业务 Case', 'worker_task_started': 'Worker 已接手 Case 任务',
    'context_built': '已构建当前 Case Context',
    'agent_start': 'Agent 开始调查', 'turn_start': '开始一轮 Tool 决策', 'tool_call': '调用只读 Tool',
    'tool_result': '已获得 Tool Observation', 'turn_end': '本轮 Agent 调查结束', 'agent_end': 'Agent 调查结束',
    'tool_scheduled': '调用只读 Tool', 'tool_observation': '获得 Tool Observation',
    'case_question_tool_called': '调用只读 Tool', 'case_question_tool_observation': '获得 Tool Observation',
    'agent_decision_trace': '已生成可审计决策摘要',
    'agent_plan_created': '已生成 Action Plan', 'evidence_grounding_passed': 'Action Plan 通过 Evidence Grounding',
    'evidence_grounding_failed': 'Action Plan 缺少必要 Evidence', 'approval_requested': '已创建待 Approval 的 Plan',
    'approval_partial': 'Approval 尚未满足全部角色', 'approval_granted': 'Approval 已通过',
    'approval_expired': 'Approval 已过期', 'approval_revoked': 'Approval 已取消', 'approval_rejected': 'Approval 已驳回，开始 Replan',
    'execution_started': '开始 Governed Execution', 'verification_passed': 'Read-after-write Verify 通过', 'verification_failed': 'Read-after-write Verify 失败',
    'execution_blocked': '旧 Action Plan 的写操作已被阻断',
    'replan_requested': '业务状态变化，已请求 Replan', 'task_requeued': '只读 Agent 任务已重新入队',
    'plan_repair_requested': 'Evidence Grounding 未通过，开始受限 Plan Repair',
    'plan_repair_succeeded': 'Plan Repair 已生成可验证的 Action Plan', 'plan_repair_failed': 'Plan Repair 未生成可执行 Plan',
    'handoff': '已 Handoff 至人工处理', 'manual_review_required': '需要 Manual Review', 'worker_failure': '后台 Worker 任务失败',
    'context_isolation_failed': 'Case Context 隔离检查未通过，已停止自动处理',
    'context_isolation_sanitized': 'Case Context 已移除越界任务字段',
    'policy_passed': 'Policy Engine 已允许全部 Action Plan 项', 'policy_denied': 'Policy Engine 拒绝执行',
    'case_question_asked': '已提交 Case Question', 'case_question_answered': '已生成 Case Answer',
}


def local_demo_operator_keys() -> dict[str, str]:
    """Return local-only seeded identities for an approval demonstration.

    The API still authorizes every request by the selected API key. This is
    unavailable outside ``local`` so the Workbench cannot impersonate roles in
    a deployed environment.
    """
    if (settings.app_env or 'local').strip().lower() != 'local':
        return {}
    identities = {'ops_admin': settings.operator_api_key}
    for raw_seed in (settings.operator_seed_keys or '').split(';'):
        subject, separator, remainder = raw_seed.strip().partition(':')
        role, separator2, key = remainder.partition(':')
        if subject and separator and role and separator2 and key:
            identities.setdefault(role.strip(), key.strip())
    return identities


def event_label(event: dict[str, Any]) -> tuple[str, str]:
    return EVENT_STYLE.get(str(event.get('kind') or ''), ('EVENT', 'white'))


def case_option_text(case: dict[str, Any]) -> Text:
    status = str(case.get('status') or 'unknown')
    event_type = str(case.get('event_type') or 'unknown')
    order_id = str(case.get('order_id') or '-')
    text = Text()
    text.append(f'[{STATUS_TEXT.get(status, status)}]', style=STATUS_STYLE.get(status, 'white'))
    text.append(f' {event_type}\n{order_id}\n{str(case.get("id") or "")[:8]}', style='white')
    return text


TOOL_GOALS = {
    'get_order': '确认订单商品、需求量、目标仓与交付约束',
    'get_inventory': '核实仓库实际库存、预留量与可用库存',
    'get_customer_profile': '核实客户是否允许拆单及服务约束',
    'get_item_supply_profile': '评估补货提前期是否满足交期',
    'list_alternative_warehouses': '确定允许作为调拨来源的仓库范围',
    'get_transfer_options': '核实调拨路径、时效与单位成本',
    'get_inbound_purchase': '核实在途采购能否支撑交付',
    'get_reference_price': '核实订单价格与参考价格是否一致',
}


def _compact_value(value: Any) -> str:
    if value is None or value == '':
        return '未知'
    if isinstance(value, float):
        return f'{value:g}'
    return str(value)


def observation_facts(tool: str, result: dict[str, Any]) -> str:
    """Render business facts, not raw API payloads or field-name lists."""
    if result.get('error'):
        return f"查询失败：{result.get('error')}；该业务事实保持未知"
    if tool == 'get_order':
        items = result.get('items') if isinstance(result.get('items'), list) else []
        item = items[0] if items and isinstance(items[0], dict) else {}
        return ' · '.join(filter(None, [
            f"订单={_compact_value(result.get('name'))}",
            f"商品={_compact_value(item.get('item_code'))}",
            f"需求量={_compact_value(item.get('qty'))}",
            f"目标仓={_compact_value(item.get('warehouse'))}",
            f"交付日={_compact_value(item.get('delivery_date') or result.get('delivery_date'))}",
        ]))
    if tool == 'get_inventory':
        actual = result.get('actual_qty')
        reserved = result.get('reserved_qty')
        try:
            if actual is None or reserved is None:
                raise ValueError('inventory fields missing')
            usable = max(0, float(actual) - float(reserved))
        except (TypeError, ValueError):
            usable = '未知'
        return f"仓库={_compact_value(result.get('warehouse'))} · 实际={_compact_value(actual)} · 预留={_compact_value(reserved)} · 可用={_compact_value(usable)}"
    if tool == 'get_customer_profile':
        partial = result.get('allows_partial_delivery')
        partial_text = '允许' if partial is True else '不允许' if partial is False else '未知'
        return f"客户={_compact_value(result.get('customer_name'))} · 分组={_compact_value(result.get('customer_group'))} · 可拆单={partial_text}"
    if tool == 'get_item_supply_profile':
        return f"商品={_compact_value(result.get('item_code'))} · 补货提前期={_compact_value(result.get('lead_time_days'))} 天 · MOQ={_compact_value(result.get('minimum_order_qty'))} · 安全库存={_compact_value(result.get('safety_stock'))}"
    if tool == 'list_alternative_warehouses':
        warehouses = result.get('warehouses') if isinstance(result.get('warehouses'), list) else []
        return '允许调拨来源：' + ('、'.join(str(item) for item in warehouses[:4]) if warehouses else '无')
    if tool == 'get_transfer_options':
        lanes = result.get('lanes') if isinstance(result.get('lanes'), list) else []
        if not lanes:
            return '未找到可用调拨路径'
        facts = []
        for lane in lanes[:3]:
            if isinstance(lane, dict):
                facts.append(f"{_compact_value(lane.get('source'))}→{_compact_value(lane.get('target'))}，{_compact_value(lane.get('transit_days'))} 天，{_compact_value(lane.get('cost_per_unit'))} {_compact_value(lane.get('currency'))}/件")
        return '；'.join(facts) or '未找到可用调拨路径'
    if tool == 'get_inbound_purchase':
        items = result.get('purchase_items') if isinstance(result.get('purchase_items'), list) else []
        if not items:
            return '未找到可用在途采购记录'
        facts = []
        for item in items[:3]:
            if isinstance(item, dict):
                facts.append(f"{_compact_value(item.get('purchase_order'))} · {_compact_value(item.get('supplier'))} · 剩余={_compact_value(item.get('remaining_qty'))} · 到货={_compact_value(item.get('schedule_date'))}")
        return '；'.join(facts)
    if tool == 'get_reference_price':
        return f"商品={_compact_value(result.get('item_code'))} · 参考价={_compact_value(result.get('reference_rate'))} {_compact_value(result.get('currency'))} · 价目表={_compact_value(result.get('price_list'))}"
    scalar_items = [f'{key}={_compact_value(value)}' for key, value in result.items() if not isinstance(value, (dict, list))]
    return ' · '.join(scalar_items[:5]) or '返回结果为空'


def action_summary(action: dict[str, Any]) -> str:
    action_type = _compact_value(action.get('action_type'))
    payload = action.get('input') if isinstance(action.get('input'), dict) else action.get('arguments') if isinstance(action.get('arguments'), dict) else {}
    parts = [f'{key}={_compact_value(value)}' for key, value in payload.items() if key in {'source', 'target', 'sku', 'quantity', 'required_by', 'supplier', 'purchase_order', 'expected_delivery_date'}]
    return f"{action_type}" + (f"（{'，'.join(parts)}）" if parts else '')


def event_summary(event: dict[str, Any], *, expanded: bool = False) -> str:
    """Produce a compact stage summary for the default Trace view."""
    kind = str(event.get('kind') or 'event')
    message = EVENT_TEXT.get(kind) or str(event.get('message') or '')
    data = event.get('data') if isinstance(event.get('data'), dict) else {}
    details: list[str] = []
    if kind in {'approval_requested', 'approval_partial', 'approval_granted'}:
        approval_id = data.get('approval_id')
        if approval_id:
            details.append(f'Approval={str(approval_id)[:8]}')
        roles = data.get('required_roles') or data.get('remaining_roles')
        if roles:
            details.append('角色=' + ', '.join(str(role) for role in roles))
    elif kind == 'agent_plan_created':
        actions = data.get('actions') or data.get('recommended_actions') or []
        if actions:
            names = [action_summary(item) for item in actions if isinstance(item, dict)]
            if names:
                details.append('Action Plan：' + '；'.join(names))
    if expanded and data:
        details.append(compact_json(data, 800))
    suffix = f"  {' · '.join(details)}" if details else ''
    return f'{message}{suffix}'.strip()


def event_detail(event: dict[str, Any]) -> str:
    """Render a complete event with readable, inspectable payload details."""
    kind = str(event.get('kind') or 'event')
    data = event.get('data') if isinstance(event.get('data'), dict) else {}
    message = str(event.get('message') or EVENT_TEXT.get(kind) or kind)
    if not data:
        return message
    payload = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    return f'{message}\n{payload}'


def tool_card_text(tool_index: int, observation_index: int, tool: str, result: dict[str, Any]) -> str:
    """Join a Tool call and its Observation into one default-view card."""
    goal = TOOL_GOALS.get(tool, '获取当前决策所需业务事实')
    facts = observation_facts(tool, result)
    return (
        f'{tool}\n'
        f'  目的：{goal}\n'
        f'  [OBSERVATION {observation_index:02d}] {facts}'
    )


def _event_data(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get('data')
    return value if isinstance(value, dict) else {}


def _first_event(events: list[dict[str, Any]], *kinds: str) -> dict[str, Any] | None:
    return next((event for event in events if event.get('kind') in kinds), None)


def context_snapshot_text(
    case: dict[str, Any],
    context_event: dict[str, Any] | None = None,
    *,
    run: int = 1,
) -> str:
    """Show Context only where it becomes an input to a real Agent run."""
    events = [event for event in case.get('events') or [] if isinstance(event, dict)]
    event = context_event or _first_event(events, 'context_built')
    data = _event_data(event or {})
    scope = data.get('scope') if isinstance(data.get('scope'), dict) else {}
    state = data.get('current_state') if isinstance(data.get('current_state'), dict) else {}
    memory_count = int(data.get('memory_count') or 0)
    observations = int(data.get('confirmed_observation_count') or 0)
    case_id = str(scope.get('case_id') or case.get('id') or '-')[:8]
    tenant = scope.get('tenant_id') or case.get('tenant_id') or '-'
    order = scope.get('order_id') or case.get('order_id') or '-'
    status = STATUS_TEXT.get(str(state.get('status') or case.get('status')), state.get('status') or case.get('status') or '未知')
    plan_version = state.get('plan_version', scope.get('plan_version', case.get('plan_version', 0)))
    lines = [
        'Context Snapshot' if run == 1 else f'Context Snapshot · Replan {run - 1}',
        f'Case scope={case_id} · tenant={tenant} · order={order}',
        f'Short-term Context · 已确认 Observation={observations} · 状态={status} · Plan v{plan_version}',
    ]
    if memory_count:
        lines.append(f'Memory · 使用 {memory_count} 条 verified lessons 作为规划提示')
    return '\n'.join(lines)


def _tool_trace_rows(case: dict[str, Any]) -> list[dict[str, Any]]:
    trace = case.get('tool_trace') if isinstance(case.get('tool_trace'), dict) else {}
    rows = trace.get('observations') if isinstance(trace.get('observations'), list) else []
    return [row for row in rows if isinstance(row, dict)]


def _tool_evidence_lookup(case: dict[str, Any]) -> tuple[dict[str, tuple[int, str]], dict[str, list[str]]]:
    """Return E-id -> (Tool number, name), plus the persisted action evidence map."""
    trace = case.get('tool_trace') if isinstance(case.get('tool_trace'), dict) else {}
    lookup: dict[str, tuple[int, str]] = {}
    for index, row in enumerate(_tool_trace_rows(case), 1):
        evidence_id = str(row.get('evidence_id') or f'E-{index:03d}')
        lookup[evidence_id] = (index, str(row.get('tool') or 'unknown_tool'))
    action_map = trace.get('action_evidence') if isinstance(trace.get('action_evidence'), dict) else {}
    return lookup, {str(key): [str(item) for item in value] for key, value in action_map.items() if isinstance(value, list)}


def action_evidence_text(
    case: dict[str, Any],
    action: dict[str, Any],
    evidence_tool_numbers: dict[str, int] | None = None,
) -> str:
    """Render durable evidence bindings without implying a false serial order."""
    lookup, action_map = _tool_evidence_lookup(case)
    action_id = str(action.get('action_id') or action.get('action_type') or '')
    evidence_refs = action.get('evidence_refs') if isinstance(action.get('evidence_refs'), list) else []
    evidence_ids = action.get('evidence_ids') if isinstance(action.get('evidence_ids'), list) else []
    ids = [str(item) for item in evidence_refs or evidence_ids] or action_map.get(action_id, [])
    refs = []
    for evidence_id in ids:
        number = (evidence_tool_numbers or {}).get(evidence_id)
        if number is None:
            number, _ = lookup.get(evidence_id, (None, None))
        refs.append(f'TOOL {number:02d} / {evidence_id}' if number else evidence_id)
    return ' · '.join(refs) if refs else '未记录可引用的 Evidence'


def planner_decision_data(case: dict[str, Any]) -> dict[str, Any]:
    """Return the persisted, operator-safe Planner explanation; never hidden CoT."""
    decision = case.get('agent_decision')
    if isinstance(decision, dict) and any(decision.get(key) for key in ('decision_trace', 'evidence_summary', 'rejected_actions', 'missing_information')):
        return decision
    events = [event for event in case.get('events') or [] if isinstance(event, dict)]
    for kind in ('agent_plan_created', 'agent_decision_trace'):
        event = next((item for item in reversed(events) if item.get('kind') == kind), None)
        if event:
            return _event_data(event)
    return {}


def planner_decision_text(decision: dict[str, Any]) -> str:
    """Format persisted Planner audit fields without attempting to recreate CoT."""
    evidence = decision.get('evidence_summary') if isinstance(decision.get('evidence_summary'), list) else []
    trace = decision.get('decision_trace') if isinstance(decision.get('decision_trace'), list) else []
    rejected = decision.get('rejected_actions') if isinstance(decision.get('rejected_actions'), list) else []
    missing = decision.get('missing_information') if isinstance(decision.get('missing_information'), list) else []
    lines: list[str] = ['Decision Summary · auditable model output']
    if evidence:
        lines.append('Evidence Summary')
        lines.extend(f'  · {str(item)[:260]}' for item in evidence[:3] if str(item).strip())
    if trace:
        lines.append('Decision Trace')
        lines.extend(f'  · {str(item)[:300]}' for item in trace[:6] if str(item).strip())
    if rejected:
        lines.append('Rejected Actions')
        for item in rejected[:3]:
            if isinstance(item, dict):
                action = item.get('action_type') or item.get('action') or 'unknown_action'
                reason = item.get('reason') or item.get('rationale') or '未采用'
                lines.append(f'  · {action}: {str(reason)[:260]}')
    if missing:
        lines.append('Open Questions')
        lines.extend(f'  · {str(item)[:240]}' for item in missing[:3] if str(item).strip())
    return '\n'.join(lines) if len(lines) > 1 else ''


def action_input_text(action: dict[str, Any]) -> str:
    payload = action.get('input') if isinstance(action.get('input'), dict) else action.get('arguments') if isinstance(action.get('arguments'), dict) else {}
    fields = ('source', 'target', 'sku', 'quantity', 'required_by', 'supplier', 'purchase_order', 'expected_delivery_date')
    values = [f'{key}={_compact_value(payload[key])}' for key in fields if key in payload]
    return ' · '.join(values) or '无结构化输入参数'


def build_turn_batches(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Pair durable lifecycle calls with observations and group them by LLM turn.

    The worker emits ``tool_call`` before ReadToolScheduler runs and emits
    ``tool_observation`` after it returns. The grouping is a presentation of
    that persisted control flow, not a claim about hidden model reasoning.
    """
    events = [event for event in case.get('events') or [] if isinstance(event, dict)]
    pending: dict[str, list[dict[str, Any]]] = {}
    batches: dict[tuple[int, int], list[dict[str, Any]]] = {}
    turn_end_reasons: dict[tuple[int, int], str] = {}
    declared_turns: set[tuple[int, int]] = set()
    fallback_turn = 1
    run = 1
    tool_index = 0
    observation_index = 0
    for event in events:
        kind = str(event.get('kind') or '')
        data = _event_data(event)
        tool = str(data.get('tool') or '')
        event_turn = ResolveOpsWorkbench._event_turn(data)
        if kind == 'context_built':
            if declared_turns or batches:
                run += 1
                fallback_turn = 1
            continue
        if kind == 'turn_start' and event_turn:
            declared_turns.add((run, event_turn))
        elif kind == 'turn_end' and event_turn:
            declared_turns.add((run, event_turn))
            turn_end_reasons[(run, event_turn)] = str(data.get('reason') or '')
        if kind == 'tool_call' and tool:
            pending.setdefault(tool, []).append({'run': run, 'turn': ResolveOpsWorkbench._event_turn(data), 'event': event})
        elif kind in {'tool_scheduled', 'case_question_tool_called'} and tool and not pending.get(tool):
            pending.setdefault(tool, []).append({'run': run, 'turn': ResolveOpsWorkbench._event_turn(data), 'event': event})
        elif kind in {'tool_observation', 'case_question_tool_observation'} and tool:
            queued = (pending.get(tool) or [])
            call = queued.pop(0) if queued else {'run': run, 'turn': 0, 'event': {}}
            if not queued:
                pending.pop(tool, None)
            turn = int(call.get('turn') or 0) or fallback_turn
            call_run = int(call.get('run') or run)
            fallback_turn = max(fallback_turn, turn)
            tool_index += 1
            observation_index += 1
            batches.setdefault((call_run, turn), []).append({
                'tool_index': tool_index,
                'observation_index': observation_index,
                'run': call_run,
                'turn': turn,
                'tool': tool,
                'call': call.get('event') or {},
                'observation': event,
                'result': data.get('result') if isinstance(data.get('result'), dict) else {},
            })
    for tool, queued in pending.items():
        for call in queued:
            turn = int(call.get('turn') or 0) or fallback_turn
            call_run = int(call.get('run') or run)
            tool_index += 1
            batches.setdefault((call_run, turn), []).append({
                'tool_index': tool_index,
                'observation_index': None,
                'run': call_run,
                'turn': turn,
                'tool': tool,
                'call': call.get('event') or {},
                'observation': {},
                'result': {},
            })
    all_turns = sorted(declared_turns | set(batches))
    return [
        {
            'run': run_index,
            'turn': turn,
            'tools': batches.get((run_index, turn), []),
            'parallel': len(batches.get((run_index, turn), [])) > 1,
            'end_reason': turn_end_reasons.get((run_index, turn), ''),
        }
        for run_index, turn in all_turns
    ]


def build_investigation_runs(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Split durable history at each real ``context_built`` boundary.

    A replan starts a new investigation with turn numbers starting at one.
    Keeping that boundary prevents a historical TURN 01 from being visually
    merged with the TURN 01 of a later replan.
    """
    events = [event for event in case.get('events') or [] if isinstance(event, dict)]
    context_positions = [index for index, event in enumerate(events) if event.get('kind') == 'context_built']
    if not context_positions:
        return [{'run': 1, 'context_event': None, 'events': events}]
    runs: list[dict[str, Any]] = []
    for index, start in enumerate(context_positions):
        end = context_positions[index + 1] if index + 1 < len(context_positions) else len(events)
        runs.append({
            'run': index + 1,
            'context_event': events[start],
            'events': events[start:end],
        })
    return runs


def context_update_text(
    tools: list[dict[str, Any]],
    evidence_by_observation: dict[int, str],
    before_count: int,
) -> str:
    """Show the explicit state change between Agent turns, never hidden CoT."""
    confirmed = [
        item for item in tools
        if item.get('observation_index') and not (item.get('result') or {}).get('error')
    ]
    evidence: list[str] = []
    for item in confirmed:
        observation_index = int(item['observation_index'])
        evidence_id = evidence_by_observation.get(observation_index, f'E-{observation_index:03d}')
        evidence.append(f'{evidence_id} · {item.get("tool")}')
    after_count = before_count + len(confirmed)
    lines = ['Context Update']
    lines.append('写入：' + ('；'.join(evidence[:4]) if evidence else '本批无已确认业务事实'))
    lines.append(f'Short-term Context：{before_count} → {after_count} 条已确认 Observation')
    return '\n'.join(lines)


class ConfirmApprovalScreen(ModalScreen[dict[str, str] | None]):
    """Explicit confirmation boundary for approval writes from the workbench."""

    CSS = """
    ConfirmApprovalScreen { align: center middle; }
    #confirm-card { width: 72; height: auto; padding: 1 2; border: round $warning; background: $surface; }
    #confirm-actions { height: auto; margin-top: 1; align: right middle; }
    #approval-reason { margin-top: 1; }
    """

    def __init__(self, approval: dict[str, Any], *, operation: str) -> None:
        super().__init__()
        self.approval = approval
        self.operation = operation

    def compose(self) -> ComposeResult:
        action = self.approval.get('action') if isinstance(self.approval.get('action'), dict) else {}
        action_type = action.get('action_type') or 'governed_action'
        title = '确认批准绑定的 Action Plan？' if self.operation == 'approve' else (
            '确认驳回 Action Plan 并重新调查？' if self.operation == 'reject' else '确认取消 Approval 并停止执行？'
        )
        yield Vertical(
            Label(title, id='confirm-title'),
            Static(
                f"Approval ID：{self.approval.get('id')}\n"
                f"Action：{action_type}\n"
                f"所需角色：{', '.join(self.approval.get('required_roles') or [])}",
            ),
            Input(placeholder='驳回原因（必填，会作为下一轮 Agent Context）', id='approval-reason') if self.operation == 'reject' else (
                Input(placeholder='取消原因（可选）', id='approval-reason') if self.operation == 'revoke' else Static('服务端会再次校验你的操作角色和审批绑定关系。')
            ),
            Horizontal(Button('取消', id='cancel'), Button('确认', variant='warning', id='confirm'), id='confirm-actions'),
            id='confirm-card',
        )

    @on(Button.Pressed, '#cancel')
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, '#confirm')
    def confirm(self) -> None:
        reason = self.query_one('#approval-reason', Input).value.strip() if self.operation in {'reject', 'revoke'} else ''
        if self.operation == 'reject' and len(reason) < 3:
            self.notify('请填写至少 3 个字符的驳回原因；它会作为下一轮 Agent 的受限 Context。', severity='warning')
            return
        self.dismiss({'operation': self.operation, 'reason': reason})


class ConfirmSandboxResetScreen(ModalScreen[bool]):
    """Confirm the explicit local-only mutation that resets demo inventory."""

    CSS = """
    ConfirmSandboxResetScreen { align: center middle; }
    #sandbox-reset-card { width: 72; height: auto; padding: 1 2; border: round $warning; background: $surface; }
    #sandbox-reset-actions { height: auto; margin-top: 1; align: right middle; }
    """

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label('确认重置 ERP 沙箱演示库存？'),
            Static(
                '将通过 ResolveOps API 创建 ERPNext Stock Reconciliation：\n'
                '重庆仓 - ROPS → 40 件；Stores - ROPS → 0 件。\n'
                '仅 local/test/staging 且 ops_admin/config_admin 有权执行。',
            ),
            Horizontal(Button('取消', id='cancel'), Button('确认重置', variant='warning', id='confirm'), id='sandbox-reset-actions'),
            id='sandbox-reset-card',
        )

    @on(Button.Pressed, '#cancel')
    def cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, '#confirm')
    def confirm(self) -> None:
        self.dismiss(True)


class NewCaseScreen(ModalScreen[dict[str, str] | None]):
    CSS = """
    NewCaseScreen { align: center middle; }
    #new-case-card { width: 76; height: auto; padding: 1 2; border: round $accent; background: $surface; }
    #new-case-card Input, #new-case-card Select { margin-top: 1; }
    #new-case-actions { height: auto; margin-top: 1; align: right middle; }
    """

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label('创建 ResolveOps Business Case'),
            Select(
                [
                    ('库存不足 · inventory_shortage', 'inventory_shortage'),
                    ('价格不一致 · price_mismatch', 'price_mismatch'),
                    ('交付延期 · delivery_delay', 'delivery_delay'),
                ],
                value='inventory_shortage',
                id='new-case-type',
            ),
            Input(placeholder='ERPNext 销售订单编号，例如 SAL-ORD-2026-00002', id='new-case-order'),
            Input(value='由 ResolveOps 工作台创建', placeholder='异常说明（可选）', id='new-case-reason'),
            Horizontal(Button('取消', id='cancel'), Button('创建 Case', variant='primary', id='create'), id='new-case-actions'),
            id='new-case-card',
        )

    @on(Button.Pressed, '#cancel')
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, '#create')
    def create(self) -> None:
        order_id = self.query_one('#new-case-order', Input).value.strip()
        if not order_id:
            self.notify('必须填写订单编号。', severity='error')
            return
        event_type = self.query_one('#new-case-type', Select).value
        self.dismiss({
            'tenant_id': 'demo',
            'event_type': str(event_type),
            'order_id': order_id,
            'reason': self.query_one('#new-case-reason', Input).value.strip() or '由 ResolveOps 工作台创建',
        })


class ResolveOpsWorkbench(App[None]):
    """One interactive surface for general chat and explicitly selected Cases."""

    TITLE = 'ResolveOps Agent - 企业 ERP 订单异常处理'
    SUB_TITLE = ''
    CSS = """
    Screen { layout: vertical; }
    #main { height: 1fr; }
    #sidebar { width: 34; min-width: 28; border-right: solid $primary; padding: 1; }
    #workspace { width: 1fr; padding: 1; }
    #runtime-status, #context-banner, #approval-card { margin-bottom: 1; padding: 0 1; border: round $secondary; }
    #context-banner { border: round $accent; }
    #case-list { height: 1fr; border: round $primary; }
    #trace { height: 1fr; border: round $primary; background: $surface; padding: 0 1; }
    #execution-canvas { height: 1fr; border: round $primary; background: $surface; padding: 1 2; }
    .canvas-stage { height: auto; margin: 0 0 1 0; padding: 0 1; border: round $secondary; }
    .canvas-context { border: round $accent; }
    .canvas-turn { border: round $secondary; color: $text-muted; }
    .canvas-batch { border: round $accent; color: $accent; }
    .canvas-aggregation { border: round $secondary; }
    .canvas-context-update { border: dashed $accent; color: $text-muted; margin-left: 2; }
    .canvas-metrics { border: dashed $secondary; color: $text-muted; }
    .canvas-plan { border: round $primary; color: $text; }
    .canvas-grounding-pass { border: round $success; color: $success; }
    .canvas-grounding-fail { border: round $error; color: $error; }
    .canvas-policy { border: round $warning; }
    .canvas-stop { border: round $warning; }
    .canvas-reject { border: round $error; color: $error; }
    .canvas-connector { height: auto; color: $text-muted; margin-left: 3; }
    .parallel-grid { height: auto; margin: 0 0 1 2; }
    .parallel-row { height: auto; margin: 0 0 1 0; }
    .parallel-fork, .parallel-join { height: auto; color: $accent; margin-left: 4; }
    .tool-card { width: 1fr; min-width: 0; height: auto; padding: 0 1; margin-right: 1; border: round $accent; }
    .tool-card-failed { border: round $error; }
    .action-card { height: auto; margin: 0 0 1 2; padding: 0 1; border: round $primary; }
    .write-tool-card { border: round $primary; color: $text; }
    .auxiliary-card { border: dashed $secondary; color: $text-muted; }
    .conversation-card { height: auto; margin: 0 0 1 0; padding: 0 1; border: round $secondary; }
    #approval-card { height: auto; min-height: 8; max-height: 10; }
    #approval-summary { height: auto; min-height: 3; }
    #approval-actions { height: 3; min-height: 3; margin-top: 1; }
    #approve-action, #reject-replan-action { height: 3; min-height: 3; }
    #command-input { margin-top: 1; }
    .status-green { color: $success; }
    .status-yellow { color: $warning; }
    .status-red { color: $error; }
    .status-cyan { color: $accent; }
    .status-dim { color: $text-muted; }
    """
    BINDINGS = [
        ('ctrl+r', 'refresh', '刷新'), ('ctrl+b', 'back_to_general', 'General'),
        ('ctrl+n', 'new_case', '新建 Case'), ('ctrl+e', 'show_eval', '评估'), ('ctrl+c', 'quit', '退出'),
    ]

    def __init__(self, client: ApiClient, *, case_limit: int = 24, poll_interval: float = 1.0) -> None:
        super().__init__()
        self.client = client
        self.case_limit = case_limit
        self.poll_interval = max(0.5, poll_interval)
        self._demo_operator_keys = local_demo_operator_keys()
        self.active_operator_role = next(
            (role for role, key in self._demo_operator_keys.items() if key == getattr(client, 'operator_key', None)),
            None,
        )
        self.cases: list[dict[str, Any]] = []
        self.active_case_id: str | None = None
        self.active_case: dict[str, Any] | None = None
        self.seen_event_ids: set[str] = set()
        self.general_history: list[dict[str, str]] = []
        self.general_transcript: list[tuple[str, str, str]] = []
        self.case_transcript: dict[str, list[tuple[str, str, str]]] = {}
        self.expanded_events = False
        self._tool_index = 0
        self._observation_index = 0
        self._pending_tool_cards: dict[str, list[tuple[int, int, dict[str, Any]]]] = {}
        self._turn_tool_counts: dict[int, int] = {}
        self._turn_batch_rendered: set[int] = set()
        self._scheduler_parallelism: int | None = None
        self._detail_tool_index = 0
        self._detail_observation_index = 0
        self._detail_pending_tools: dict[str, list[int]] = {}
        self._detail_completed_tools: dict[str, list[int]] = {}
        self._has_decision_trace = False
        self._has_plan_action = False
        self._case_streaming = False
        self._case_stream_answer = ''
        self._case_stream_question = ''
        self._last_canvas_signature: str | None = None
        self._case_metrics: dict[str, dict[str, Any]] = {}
        self.metrics_expanded = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id='main'):
            with Vertical(id='sidebar'):
                yield Static('正在检查运行状态…', id='runtime-status')
                yield Label('最近 Case')
                yield OptionList(id='case-list')
            with Vertical(id='workspace'):
                yield Static('', id='context-banner')
                yield VerticalScroll(id='execution-canvas')
                yield RichLog(id='trace', wrap=True, highlight=False, markup=False)
                with Vertical(id='approval-card'):
                    yield Static('当前没有待处理审批。', id='approval-summary')
                    yield Horizontal(
                        Button('批准', id='approve-action', disabled=True), Button('驳回并 Replan', id='reject-replan-action', disabled=True),
                        id='approval-actions',
                    )
                yield Input(placeholder='输入问题，或使用 /help 查看命令', id='command-input')
        yield Footer()

    def on_mount(self) -> None:
        self._render_operator_identity()
        self.refresh_runtime()
        self.refresh_cases()
        self.set_interval(self.poll_interval, self.poll_active_case)
        self.set_interval(4.0, self.refresh_cases)
        self.call_after_refresh(self.focus_input)
        self._render_general()

    def focus_input(self) -> None:
        self.query_one('#command-input', Input).focus()

    def _trace(self) -> RichLog:
        return self.query_one('#trace', RichLog)

    def _render_operator_identity(self) -> None:
        # Identity is shown only beside a pending approval, where it affects
        # the operator's next action. Keep the sidebar focused on Case list.
        return

    def _canvas(self) -> VerticalScroll:
        return self.query_one('#execution-canvas', VerticalScroll)

    def _write(self, label: str, text: str, style: str = 'white') -> None:
        line = Text()
        line.append(f'[{label}] ', style=style)
        line.append(text)
        self._trace().write(line, scroll_end=True)

    def _clear_trace(self) -> None:
        self._trace().clear()

    def _render_general(self) -> None:
        self._clear_trace()
        self._trace().display = True
        self._canvas().display = False
        self.query_one('#context-banner', Static).display = False
        self.query_one('#command-input', Input).placeholder = '向 ResolveOps 提问，或使用 /new、/focus <Case ID>、/help'
        self._set_approval_card(None)
        for label, text, style in self.general_transcript:
            self._write(label, text, style)

    def _render_case(self, case: dict[str, Any], *, append_only: bool = False) -> None:
        self.query_one('#context-banner', Static).display = True
        self.query_one('#context-banner', Static).update(
            f"CASE · {case.get('event_type')} · {case.get('order_id')} · {STATUS_TEXT.get(str(case.get('status')), case.get('status'))} · {str(case.get('id'))[:8]}"
        )
        self.query_one('#command-input', Input).placeholder = '向当前 Case 提问，或使用 /back、/events、/approve <审批ID>'
        if self.expanded_events:
            self._canvas().display = False
            self._trace().display = True
            if not append_only:
                self._clear_trace()
                self.seen_event_ids.clear()
                self._detail_tool_index = 0
                self._detail_observation_index = 0
                self._detail_pending_tools.clear()
                self._detail_completed_tools.clear()
            for event in case.get('events') or []:
                self._append_event(event)
            for label, text, style in self.case_transcript.get(str(case.get('id')), []):
                self._write(label, text, style)
        else:
            self._trace().display = False
            self._canvas().display = True
            signature = self._canvas_signature(case)
            if not self._case_streaming and (not append_only or signature != self._last_canvas_signature):
                self._last_canvas_signature = signature
                self._rebuild_execution_canvas(case)
        self._set_approval_card(case)

    @staticmethod
    def _canvas_signature(case: dict[str, Any]) -> str:
        """Only redraw the expensive widget tree when durable Case state changed."""
        payload = {
            'status': case.get('status'),
            'plan_version': case.get('plan_version'),
            'plan': case.get('plan'),
            'tool_trace': case.get('tool_trace'),
            'approvals': case.get('approvals'),
            'events': [
                {'id': event.get('id'), 'kind': event.get('kind'), 'data': event.get('data')}
                for event in case.get('events') or [] if isinstance(event, dict)
            ],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(',', ':'))

    @staticmethod
    def _stage(text: str, classes: str = 'canvas-stage') -> Static:
        return Static(text, classes=classes)

    @staticmethod
    def _connector() -> Static:
        return Static('↓', classes='canvas-connector')

    def _rebuild_execution_canvas(self, case: dict[str, Any]) -> None:
        """Build the visible path from durable events rather than appending logs.

        This deliberately re-renders the bounded Case history.  A completed
        scheduler batch is then laid out as one unit, even while the worker had
        persisted its tool observations one at a time.
        """
        widgets = self._execution_widgets(case)
        self.run_worker(self._replace_canvas_children(widgets), exclusive=True, group='execution-canvas')

    async def _replace_canvas_children(self, widgets: list[Any]) -> None:
        canvas = self._canvas()
        await canvas.remove_children()
        if widgets:
            await canvas.mount(*widgets)
        canvas.scroll_home(animate=False)

    def _execution_widgets(self, case: dict[str, Any]) -> list[Any]:
        events = [event for event in case.get('events') or [] if isinstance(event, dict)]
        widgets: list[Any] = []
        if not events:
            widgets.extend([
                self._stage(context_snapshot_text(case), 'canvas-stage canvas-context'),
                self._connector(),
                self._stage('等待 Worker 创建 Agent 执行链路。', 'canvas-stage canvas-turn'),
            ])
            return widgets

        all_batches = build_turn_batches(case)
        observation_items = [item for batch in all_batches for item in batch['tools'] if item.get('observation_index')]
        latest_run = max((int(batch['run']) for batch in all_batches), default=1)
        latest_observations = [item for item in observation_items if int(item['run']) == latest_run]
        trace_rows = _tool_trace_rows(case)
        source_items = latest_observations if len(trace_rows) == len(latest_observations) else observation_items
        evidence_by_observation = {
            int(item['observation_index']): str(row.get('evidence_id') or f"E-{index:03d}")
            for index, (item, row) in enumerate(zip(source_items, trace_rows), 1)
        }
        context_event = _first_event(events, 'context_built')
        limits = _event_data(context_event or {}).get('agent_limits') if context_event else {}
        limits = limits if isinstance(limits, dict) else {}
        worker_cap = limits.get('read_tool_parallelism')

        for run_data in build_investigation_runs(case):
            run = int(run_data['run'])
            run_events = run_data['events']
            context_data = _event_data(run_data.get('context_event') or {})
            current_context_count = int(context_data.get('confirmed_observation_count') or 0)
            widgets.extend([
                self._stage(context_snapshot_text(case, run_data.get('context_event'), run=run), 'canvas-stage canvas-context'),
                self._connector(),
            ])
            if _first_event(run_events, 'agent_start'):
                widgets.extend([
                    self._stage('AGENT · ReadToolLoop\nLLM 仅在当前 Case Context 内选择 read-only Tool；写操作由后续受控边界执行。', 'canvas-stage canvas-turn'),
                    self._connector(),
                ])

            run_turns = [batch for batch in all_batches if int(batch['run']) == run]
            for batch in run_turns:
                turn = int(batch['turn'])
                tools = batch['tools']
                widgets.extend([
                    self._stage(
                        f'TURN {turn:02d} · ReAct（Reason → Read）\n输入 Context：Case + {current_context_count} 条已确认 Observation；选择下一批业务读取。',
                        'canvas-stage canvas-turn',
                    ),
                    self._connector(),
                ])
                if tools:
                    cards = [self._tool_card(item, evidence_by_observation.get(int(item.get('observation_index') or 0))) for item in tools]
                    if batch['parallel']:
                        columns = self._parallel_columns(worker_cap, len(cards))
                        widgets.extend([
                            self._stage(f'PARALLEL BATCH · Turn {turn:02d}', 'canvas-stage canvas-batch'),
                            Static('↓ fan-out · 独立 read-only Tool 同步执行', classes='parallel-fork'),
                        ])
                        rows = [Horizontal(*cards[start:start + columns], classes='parallel-row') for start in range(0, len(cards), columns)]
                        widgets.append(Vertical(*rows, classes='parallel-grid'))
                        widgets.append(Static('↓ join · 等待本批全部 Observation', classes='parallel-join'))
                    else:
                        widgets.extend(cards)
                    observation_count = sum(1 for item in tools if item.get('observation_index'))
                    widgets.extend([
                        self._connector(),
                        self._stage(f'Observation Aggregation\n{observation_count} 条结构化 Observation 已汇总。', 'canvas-stage canvas-aggregation'),
                        self._connector(),
                        self._stage(context_update_text(tools, evidence_by_observation, current_context_count), 'canvas-stage canvas-context-update'),
                        self._connector(),
                    ])
                    current_context_count += sum(
                        1 for item in tools
                        if item.get('observation_index') and not (item.get('result') or {}).get('error')
                    )

            plan_event = next((event for event in reversed(run_events) if event.get('kind') == 'agent_plan_created'), None)
            plan_data = _event_data(plan_event or {})
            actions = plan_data.get('actions') if isinstance(plan_data.get('actions'), list) else []
            run_evidence_tool_numbers = {
                evidence_by_observation.get(int(item['observation_index']), f"E-{int(item['observation_index']):03d}"): int(item['tool_index'])
                for batch in run_turns
                for item in batch['tools']
                if item.get('observation_index')
            }
            decision_event = next((event for event in reversed(run_events) if event.get('kind') == 'agent_decision_trace'), None)
            decision_data = _event_data(decision_event or plan_event or {})
            if actions:
                final_turn = max((int(batch['turn']) for batch in run_turns), default=0)
                action_turn = final_turn if any(batch.get('end_reason') == 'no_tool_calls' for batch in run_turns if int(batch['turn']) == final_turn) else final_turn + 1
                widgets.extend([
                    self._stage(
                        f'TURN {action_turn:02d} · ReAct（Reason → Propose）\n读取已确认 Context，生成 typed write intent；LLM 不直接写入 ERP。',
                        'canvas-stage canvas-turn',
                    ),
                    self._connector(),
                ])
                decision_card = self._planner_decision_card(decision_data)
                if decision_card:
                    widgets.append(decision_card)
                for action in (item for item in actions if isinstance(item, dict)):
                    widgets.append(self._write_tool_card(case, action, run_evidence_tool_numbers))
                widgets.append(self._connector())
            elif decision_event:
                decision_card = self._planner_decision_card(decision_data)
                if decision_card:
                    widgets.append(decision_card)
                gaps = decision_data.get('missing_information') if isinstance(decision_data.get('missing_information'), list) else []
                detail = '；'.join(str(item) for item in gaps[:2]) or '未产生可执行写操作'
                widgets.extend([self._stage(f'PLAN · 未生成可执行 Write Tool\n{detail}', 'canvas-stage canvas-reject'), self._connector()])

            grounding_event = next((event for event in reversed(run_events) if event.get('kind') in {'evidence_grounding_failed', 'evidence_grounding_passed'}), None)
            if grounding_event:
                grounding_payload = _event_data(grounding_event)
                grounding = grounding_payload.get('grounding') if isinstance(grounding_payload.get('grounding'), dict) else grounding_payload
                allowed = bool(grounding.get('allowed'))
                problems = grounding.get('problems') if isinstance(grounding.get('problems'), list) else []
                lines = [f"Evidence Grounding · {'PASS' if allowed else 'FAIL'}", f"规则结果={grounding.get('reason') or ('grounded' if allowed else 'evidence_not_sufficient')}"]
                if problems:
                    lines.append('失败原因：' + '；'.join(str(item) for item in problems[:3]))
                widgets.extend([self._stage('\n'.join(lines), 'canvas-stage canvas-grounding-pass' if allowed else 'canvas-stage canvas-grounding-fail'), self._connector()])

            policy_event = next((event for event in reversed(run_events) if event.get('kind') in {'policy_denied', 'policy_passed'}), None)
            if policy_event:
                policy_data = _event_data(policy_event)
                decisions = policy_data.get('decisions') if isinstance(policy_data.get('decisions'), list) else []
                policy_lines = [
                    f"{item.get('action_id') or item.get('action_type') or 'Write Tool'} · {'ALLOW' if item.get('allowed') else 'DENY'} · roles={','.join(str(role) for role in item.get('required_roles') or []) or '无'}"
                    for item in decisions if isinstance(item, dict)
                ]
                text = f"Policy · {'ALLOW' if policy_event.get('kind') == 'policy_passed' else 'DENY'}\n" + ('\n'.join(policy_lines) or event_summary(policy_event))
                widgets.extend([self._stage(text, 'canvas-stage canvas-policy' if policy_event.get('kind') == 'policy_passed' else 'canvas-stage canvas-reject'), self._connector()])

            if actions:
                approval_stage = self._approval_stage_for_plan(case, plan_data)
                if approval_stage:
                    widgets.extend([approval_stage, self._connector()])

            for event in run_events:
                label, text, classes = self._governance_stage(event)
                if label:
                    widgets.extend([self._stage(f'{label}\n{text}', classes), self._connector()])

        metrics_card = self._run_metrics_widget(case)
        if metrics_card:
            widgets.append(metrics_card)

        if self.case_transcript.get(str(case.get('id'))):
            widgets.append(self._stage('Case Conversation', 'canvas-stage canvas-turn'))
            for label, text, _ in self.case_transcript[str(case.get('id'))]:
                widgets.append(Static(f'{label}\n{text}', classes='conversation-card'))
        if self._case_streaming and self.active_case_id == str(case.get('id')):
            widgets.append(self._stage('Case Conversation · 正在生成', 'canvas-stage canvas-turn'))
            widgets.append(Static(f'USER\n{self._case_stream_question}', classes='conversation-card'))
            widgets.append(Static(f'ANSWER\n{self._case_stream_answer or "…"}', id='case-stream-answer', classes='conversation-card'))
        return widgets[:-1] if widgets and isinstance(widgets[-1], Static) and 'canvas-connector' in widgets[-1].classes else widgets

    @staticmethod
    def _parallel_columns(worker_cap: Any, count: int) -> int:
        """Keep every Tool visible while preserving the scheduler's real cap."""
        try:
            cap = int(worker_cap)
        except (TypeError, ValueError):
            cap = 4
        return max(1, min(count, cap, 4))

    @staticmethod
    def _planner_decision_card(decision: dict[str, Any]) -> Static | None:
        """Display the model's persisted audit summary, not hidden reasoning."""
        text = planner_decision_text(decision)
        return Static(text, classes='action-card auxiliary-card') if text else None

    def _tool_card(self, item: dict[str, Any], evidence_id: str | None) -> Static:
        tool = str(item.get('tool') or 'unknown_tool')
        result = item.get('result') if isinstance(item.get('result'), dict) else {}
        observation_index = item.get('observation_index')
        status = 'failed' if result.get('error') else 'success' if observation_index else 'pending'
        observation = f'Observation {int(observation_index):02d}' if observation_index else 'Observation pending'
        evidence = evidence_id or (f'E-{int(observation_index):03d}' if observation_index else '—')
        lines = [
            f"TOOL {int(item['tool_index']):02d} · {tool}",
            f'{observation} · {evidence} · {status}',
            TOOL_GOALS.get(tool, '获取当前决策所需业务事实'),
            observation_facts(tool, result) if observation_index else '尚未返回 Observation',
        ]
        classes = 'tool-card tool-card-failed' if status == 'failed' else 'tool-card'
        return Static('\n'.join(lines), classes=classes)

    @staticmethod
    def _write_tool_card(
        case: dict[str, Any],
        action: dict[str, Any],
        evidence_tool_numbers: dict[str, int] | None = None,
    ) -> Static:
        """Render the proposed write as a Tool, distinct from the LLM's summary.

        It is deliberately labelled as an intent.  The actual ERP write occurs
        only after grounding, policy and approval have passed.
        """
        action_type = str(action.get('action_type') or 'unknown_write_tool')
        lines = [
            f'WRITE TOOL · {action_type}',
            f"Action ID: {action.get('action_id') or '未记录'}",
            f'Input: {action_input_text(action)}',
            f'Evidence Binding: {action_evidence_text(case, action, evidence_tool_numbers)}',
            '状态=proposed · 尚未执行 ERP 写入',
        ]
        return Static('\n'.join(lines), classes='action-card write-tool-card')

    @staticmethod
    def _metric_int(metrics: dict[str, Any], name: str) -> int | None:
        value = metrics.get(name)
        return int(value) if isinstance(value, (int, float)) else None

    @staticmethod
    def _metric_seconds(value: Any) -> str:
        return f'{float(value):.1f} s' if isinstance(value, (int, float)) else '未记录'

    @staticmethod
    def _metric_ms(value: Any) -> str:
        return f'{float(value):.0f} ms' if isinstance(value, (int, float)) else '未记录'

    def _run_metrics_widget(self, case: dict[str, Any]) -> Static:
        """Render measured per-Case runtime facts, never aggregate scores."""
        case_id = str(case.get('id') or '')
        metrics = self._case_metrics.get(case_id)
        if not metrics:
            return self._stage('Run Metrics · 正在收集当前 Case 的运行数据…', 'canvas-stage canvas-metrics')

        llm_calls = self._metric_int(metrics, 'llm_call_count') or 0
        llm_telemetry_calls = self._metric_int(metrics, 'llm_telemetry_call_count') or 0
        token_usage_calls = self._metric_int(metrics, 'llm_token_usage_call_count') or 0
        llm_latency_samples = self._metric_int(metrics, 'llm_latency_sample_count') or 0
        tokens = self._metric_int(metrics, 'llm_total_tokens')
        tool_calls = self._metric_int(metrics, 'tool_call_count') or 0
        failed_tools = self._metric_int(metrics, 'tool_failure_count') or 0
        parallel_batches = sum(1 for batch in build_turn_batches(case) if batch.get('parallel'))
        replans = self._metric_int(metrics, 'replan_count') or 0
        plan_repairs = self._metric_int(metrics, 'plan_repair_count') or 0
        if tokens is None:
            token_text = 'Token 未返回'
        elif llm_calls and token_usage_calls < llm_calls:
            token_text = f'Token（已观测 {token_usage_calls}/{llm_calls}）{tokens}'
        else:
            token_text = f'Token {tokens}'
        summary = (
            f'Run Metrics · LLM 请求 {llm_calls} 次 · {token_text} · '
            f'Tool {tool_calls}（失败 {failed_tools}）· 并行批次 {parallel_batches} · '
            f'事件跨度 {self._metric_seconds(metrics.get("duration_seconds"))} · Replan {replans}'
        )
        if not self.metrics_expanded:
            return self._stage(summary + ' · /metrics 查看详情', 'canvas-stage canvas-metrics')

        successful_tools = max(0, tool_calls - failed_tools)
        lines = [summary]
        telemetry_text = f'telemetry={llm_telemetry_calls}/{llm_calls}'
        latency_text = (
            f'avg latency={self._metric_ms(metrics.get("avg_llm_latency_ms"))}'
            f'（样本 {llm_latency_samples}/{llm_calls}）'
        )
        lines.append(
            f"LLM · {telemetry_text} · input={self._metric_int(metrics, 'llm_prompt_tokens') if self._metric_int(metrics, 'llm_prompt_tokens') is not None else '未返回'}"
            f" · output={self._metric_int(metrics, 'llm_completion_tokens') if self._metric_int(metrics, 'llm_completion_tokens') is not None else '未返回'}"
            f" · {latency_text}"
        )
        lines.append(
            f"Tool · success={successful_tools} · failure={failed_tools} · 执行延迟样本={self._metric_int(metrics, 'observed_tool_latency_count') or 0}/{tool_calls}"
            f" · avg={self._metric_ms(metrics.get('avg_tool_latency_ms'))}"
            f" · max={self._metric_ms(metrics.get('max_tool_latency_ms'))}"
        )
        lines.append(
            f"Scheduler · completed={self._metric_int(metrics, 'scheduled_tool_call_count') or 0}"
            f" · same-context repeat={self._metric_int(metrics, 'duplicate_tool_call_count') or 0}"
        )
        lines.append(f'Recovery · Replan={replans} · Plan Repair={plan_repairs}')
        lines.append('/metrics 收起详情')
        return self._stage('\n'.join(lines), 'canvas-stage canvas-metrics')

    def _approval_stage_for_plan(self, case: dict[str, Any], plan_data: dict[str, Any]) -> Static | None:
        """Show the real approval objects created for this accepted proposal."""
        created = plan_data.get('approvals') if isinstance(plan_data.get('approvals'), list) else []
        if not created:
            return None
        current = {
            str(item.get('id')): item
            for item in case.get('approvals') or []
            if isinstance(item, dict)
        }
        lines = ['Approval']
        for item in created:
            if not isinstance(item, dict):
                continue
            approval_id = str(item.get('approval_id') or '-')
            approval = current.get(approval_id, {})
            action_type = item.get('action_type') or 'Write Tool'
            status = approval.get('status') or 'created'
            roles = approval.get('required_roles') or []
            lines.append(
                f'{approval_id[:8]} · {action_type} · {status}'
                + (f" · roles={','.join(str(role) for role in roles)}" if roles else '')
            )
        return self._stage('\n'.join(lines), 'canvas-stage canvas-policy') if len(lines) > 1 else None

    def _approval_stage_widgets(self, case: dict[str, Any], events: list[dict[str, Any]]) -> list[Any]:
        approvals = [item for item in case.get('approvals') or [] if isinstance(item, dict)]
        approval_events = [event for event in events if str(event.get('kind') or '').startswith('approval_')]
        if not approvals and not approval_events:
            return []
        lines = ['Approval']
        for approval in approvals:
            action = approval.get('action') if isinstance(approval.get('action'), dict) else {}
            roles = ', '.join(str(role) for role in approval.get('required_roles') or []) or '无'
            lines.append(f"{str(approval.get('id') or '-')[:8]} · {action.get('action_type') or 'Action'} · {approval.get('status') or 'unknown'} · roles={roles}")
        if not approvals:
            lines.append('已记录审批生命周期事件，当前未返回审批对象。')
        return [self._stage('\n'.join(lines), 'canvas-stage canvas-policy')]

    @staticmethod
    def _governance_stage(event: dict[str, Any]) -> tuple[str, str, str]:
        kind = str(event.get('kind') or '')
        data = _event_data(event)
        if kind == 'execution_started':
            return 'Executor · Governed Execution', f"Action={data.get('action_id') or data.get('action_type') or 'unknown'} · 已进入受控写操作边界", 'canvas-stage canvas-policy'
        if kind == 'execution_blocked':
            return 'Executor · Blocked', '旧 Approval 已失效；残留写任务已在 Executor 前被阻断。', 'canvas-stage canvas-stop'
        if kind == 'verification_passed':
            return 'Verify · PASS', f"Action={data.get('action_id') or 'unknown'} · Read-after-write verification 通过", 'canvas-stage canvas-grounding-pass'
        if kind == 'verification_failed':
            return 'Verify · FAIL', event_summary(event), 'canvas-stage canvas-grounding-fail'
        if kind == 'approval_rejected':
            return 'Approval Rejected', '审批意见已写入受限 Context；旧 Action Plan 已失效，等待新的调查任务。', 'canvas-stage canvas-reject'
        if kind == 'replan_requested':
            source = data.get('source')
            if source == 'approval_rejection':
                return 'Replan Requested', '审批驳回原因将与旧 Plan 一起写入 Case Context，Agent 会重新读取 ERP 事实后生成新 Plan。', 'canvas-stage canvas-reject'
            return 'Replan Requested', '写前预检未通过；旧 Approval 已失效，失败事实将写回 Context 后重新调查。', 'canvas-stage canvas-reject'
        if kind == 'task_requeued':
            return 'Replan Queued', '新的调查任务已入队，下一段 Context Snapshot 将启动新的 Agent Turn。', 'canvas-stage canvas-policy'
        if kind in {'plan_repair_requested', 'plan_repair_succeeded'}:
            return 'Plan Repair · bounded', event_summary(event), 'canvas-stage canvas-context-update'
        if kind == 'plan_repair_failed':
            return 'Plan Repair · failed', event_summary(event), 'canvas-stage canvas-reject'
        if kind == 'agent_end':
            # The read-only loop's low-level stop is retained in /events.  The
            # default canvas instead continues with the business-visible
            # Planner proposal turn, so it does not misleadingly look terminal.
            return '', '', ''
        if kind in {'handoff', 'manual_review_required', 'worker_failure', 'context_isolation_failed'}:
            return 'Stop / Handoff', event_summary(event), 'canvas-stage canvas-stop'
        return '', '', ''

    def _append_event(self, event: dict[str, Any]) -> None:
        event_id = str(event.get('id') or '')
        if event_id and event_id in self.seen_event_ids:
            return
        if event_id:
            self.seen_event_ids.add(event_id)
        kind = str(event.get('kind') or '')
        data = event.get('data') if isinstance(event.get('data'), dict) else {}
        tool_name = str(data.get('tool') or '')

        if self.expanded_events:
            self._append_event_detail(event, tool_name)
            return

        if kind == 'context_built':
            self._update_scheduler_limit(data)
            self._write('CONTEXT', 'Context Snapshot 已生成', 'white')
            return
        if kind == 'agent_start':
            self._write('AGENT', 'ReadToolLoop 已启动 · LLM 仅可调用当前 Tool Registry 中的 read-only Tool', 'magenta')
            return
        if kind == 'turn_start':
            turn = self._event_turn(data)
            self._write(f'TURN {turn:02d}' if turn else 'TURN', 'LLM 评估当前 Context 并选择下一批 Tool', 'dim')
            return

        # New runs include lifecycle ``tool_call`` events; older Cases start at
        # ``tool_scheduled``.  In the default view neither is a finished unit.
        # Buffer it until the paired Observation arrives, then emit one card.
        if kind == 'tool_call':
            self._queue_tool_card(tool_name, event)
            return
        if kind == 'tool_result':
            return
        if kind in {'tool_scheduled', 'case_question_tool_called'}:
            if not self._pending_tool_cards.get(tool_name):
                self._queue_tool_card(tool_name, event)
            return
        if kind in {'tool_observation', 'case_question_tool_observation'}:
            self._render_tool_card(tool_name, event)
            return
        if kind == 'turn_end':
            turn = self._event_turn(data)
            count = self._turn_tool_counts.get(turn, 0)
            if count:
                self._write('OBSERVATION', f'Turn {turn:02d} · Observation Aggregation 已完成（{count} 条结果已回送 LLM）', 'cyan')
            else:
                self._write(f'TURN {turn:02d}' if turn else 'TURN', 'LLM 未发起新的 Tool Call，转入 Planner', 'dim')
            return
        if kind == 'agent_decision_trace':
            self._flush_pending_tool_cards()
            self._has_decision_trace = True
            self._has_plan_action = bool(data.get('recommended_actions') or data.get('actions'))
            self._write_plan_card(data)
            return
        if kind == 'agent_plan_created':
            if not self._has_plan_action:
                self._write_plan_card(data)
                self._has_plan_action = bool(data.get('recommended_actions') or data.get('actions'))
            return
        if kind == 'agent_end':
            self._flush_pending_tool_cards()
            stop_reason = str(data.get('stop_reason') or 'completed')
            count = data.get('observation_count')
            suffix = f' · 已获得 {count} 条 Observation' if count is not None else ''
            self._write('AGENT', f'调查完成 · stop_reason={stop_reason}{suffix}', 'yellow')
            return
        label, style = event_label(event)
        self._write(label, event_summary(event), style)

    def _queue_tool_card(self, tool_name: str, event: dict[str, Any]) -> None:
        if not tool_name:
            return
        self._tool_index += 1
        data = event.get('data') if isinstance(event.get('data'), dict) else {}
        turn = self._event_turn(data)
        if turn:
            self._turn_tool_counts[turn] = self._turn_tool_counts.get(turn, 0) + 1
        self._pending_tool_cards.setdefault(tool_name, []).append((self._tool_index, turn, event))

    def _render_tool_card(self, tool_name: str, observation: dict[str, Any]) -> None:
        queued = self._pending_tool_cards.get(tool_name) or []
        if queued:
            tool_index, turn, _ = queued.pop(0)
            if not queued:
                self._pending_tool_cards.pop(tool_name, None)
        else:
            self._tool_index += 1
            tool_index = self._tool_index
            turn = 0
        self._observation_index += 1
        data = observation.get('data') if isinstance(observation.get('data'), dict) else {}
        result = data.get('result') if isinstance(data.get('result'), dict) else {}
        self._write_turn_batch(turn)
        lines = tool_card_text(tool_index, self._observation_index, tool_name or 'unknown_tool', result).splitlines()
        card = f'  ├─ {lines[0]}' + ''.join(f'\n  │  {line}' for line in lines[1:])
        self._write(
            f'TOOL {tool_index:02d}',
            card,
            'cyan',
        )

    @staticmethod
    def _event_turn(data: dict[str, Any]) -> int:
        try:
            return int(data.get('turn') or 0)
        except (TypeError, ValueError):
            return 0

    def _write_turn_batch(self, turn: int) -> None:
        if not turn or turn in self._turn_batch_rendered:
            return
        count = self._turn_tool_counts.get(turn, 0)
        if not count:
            return
        self._turn_batch_rendered.add(turn)
        if count > 1:
            cap = self._scheduler_parallelism or '?'
            self._write('PARALLEL BATCH', f'Turn {turn:02d} · {count} 个独立 Tool · ReadToolScheduler worker ≤{cap}', 'cyan')
        else:
            self._write('TOOL BATCH', f'Turn {turn:02d} · 1 个 read-only Tool', 'cyan')

    def _flush_pending_tool_cards(self) -> None:
        """Keep incomplete calls visible when an Agent run stops before a result."""
        for tool_name, queued in list(self._pending_tool_cards.items()):
            for tool_index, turn, _ in queued:
                self._write_turn_batch(turn)
                self._write(
                    f'TOOL {tool_index:02d}',
                    f'  ├─ {tool_name}\n  │  目的：{TOOL_GOALS.get(tool_name, "获取当前决策所需业务事实")}\n  │  状态：未获得 Observation',
                    'yellow',
                )
        self._pending_tool_cards.clear()

    def _update_scheduler_limit(self, data: dict[str, Any]) -> None:
        limits = data.get('agent_limits') if isinstance(data.get('agent_limits'), dict) else {}
        parallelism = limits.get('read_tool_parallelism')
        try:
            self._scheduler_parallelism = int(parallelism) if parallelism is not None else None
        except (TypeError, ValueError):
            self._scheduler_parallelism = None

    def _write_plan_card(self, data: dict[str, Any]) -> None:
        actions = data.get('recommended_actions') or data.get('actions') or []
        action_text = '；'.join(action_summary(item) for item in actions if isinstance(item, dict)) or '未生成可执行 Action'
        lines = [f'推荐 Action：{action_text}']
        rationale = str(data.get('rationale') or '').strip()
        if rationale:
            lines.append(f'  结论：{rationale[:360]}')
        decisions = data.get('decision_trace') if isinstance(data.get('decision_trace'), list) else []
        for item in decisions[:2]:
            text = str(item).strip()
            if text:
                lines.append(f'  依据：{text[:360]}')
        evidence = data.get('evidence_summary') if isinstance(data.get('evidence_summary'), list) else []
        for item in evidence[:2]:
            text = str(item).strip()
            if text:
                lines.append(f'  Evidence：{text[:360]}')
        rejected = data.get('rejected_actions') if isinstance(data.get('rejected_actions'), list) else []
        for item in rejected[:2]:
            if isinstance(item, dict):
                action = item.get('action_type') or item.get('action') or 'unknown_action'
                reason = item.get('reason') or item.get('rationale') or '未选择'
                lines.append(f'  未采用 {action}：{str(reason)[:360]}')
        gaps = data.get('missing_information') if isinstance(data.get('missing_information'), list) else []
        for item in gaps[:2]:
            text = str(item).strip()
            if text:
                lines.append(f'  待确认：{text[:240]}')
        self._write('PLAN', '\n'.join(lines), 'magenta')

    def _append_event_detail(self, event: dict[str, Any], tool_name: str) -> None:
        """Render every durable event in order for technical inspection."""
        kind = str(event.get('kind') or '')
        if kind in {'tool_call', 'tool_scheduled', 'case_question_tool_called'}:
            queue = self._detail_pending_tools.setdefault(tool_name, [])
            if kind == 'tool_call' or not queue:
                self._detail_tool_index += 1
                queue.append(self._detail_tool_index)
            index = queue[0]
            label = f'TOOL {index:02d}' if kind != 'tool_scheduled' else f'SCHEDULER {index:02d}'
            self._write(label, event_detail(event), 'cyan')
            return
        if kind in {'tool_observation', 'case_question_tool_observation'}:
            queue = self._detail_pending_tools.get(tool_name) or []
            if queue:
                tool_index = queue.pop(0)
                if not queue:
                    self._detail_pending_tools.pop(tool_name, None)
            else:
                self._detail_tool_index += 1
                tool_index = self._detail_tool_index
            self._detail_completed_tools.setdefault(tool_name, []).append(tool_index)
            self._detail_observation_index += 1
            self._write(f'OBSERVATION {self._detail_observation_index:02d}', event_detail(event), 'cyan')
            return
        if kind == 'tool_result':
            completed = self._detail_completed_tools.get(tool_name) or []
            index = completed.pop(0) if completed else (self._detail_pending_tools.get(tool_name) or [0])[0]
            if not completed:
                self._detail_completed_tools.pop(tool_name, None)
            label = f'TOOL RESULT {index:02d}' if index else 'TOOL RESULT'
            self._write(label, event_detail(event), 'cyan')
            return
        label, style = event_label(event)
        self._write(label, event_detail(event), style)

    def _write_trace_list(self, label: str, values: Any, style: str, *, limit: int) -> None:
        if not isinstance(values, list):
            return
        for index, value in enumerate(values[:limit], 1):
            if isinstance(value, dict):
                text = compact_json(value, 360)
            else:
                text = str(value).strip()
            if text:
                self._write(f'{label} {index:02d}', text[:500], style)

    def _set_approval_card(self, case: dict[str, Any] | None) -> None:
        card = self.query_one('#approval-card', Vertical)
        summary = self.query_one('#approval-summary', Static)
        approve = self.query_one('#approve-action', Button)
        reject = self.query_one('#reject-replan-action', Button)
        pending = [] if not case else [item for item in case.get('approvals') or [] if item.get('status') == 'pending']
        if len(pending) != 1:
            card.display = False
            if not case:
                summary.update('当前没有待处理审批。')
            elif pending:
                ids = ', '.join(str(item.get('id'))[:8] for item in pending)
                summary.update(f'存在多个待审批项：{ids}\n请使用 /approve <审批ID> 或 /reject <审批ID>。')
            else:
                summary.update('当前案例没有待审批项。')
            approve.disabled = True
            reject.disabled = True
            approve.tooltip = None
            reject.tooltip = None
            return
        approval = pending[0]
        card.display = True
        action = approval.get('action') if isinstance(approval.get('action'), dict) else {}
        short_id = str(approval.get('id'))[:8]
        summary.update(
            f"待审批 · {short_id}\n"
            f"动作={action.get('action_type') or '受控动作'} · 所需角色={', '.join(approval.get('required_roles') or [])}\n"
            f"当前身份={self.active_operator_role or '已认证 Operator'}"
        )
        # The Approval ID is already shown in the card. Keep actions short so
        # both remain fully clickable inside the compact Workbench sidebar.
        approve.label = '批准'
        reject.label = '驳回并 Replan'
        approve.disabled = False
        reject.disabled = False
        approve.tooltip = str(approval.get('id'))
        reject.tooltip = str(approval.get('id'))

    def _active_pending_approval(self) -> dict[str, Any] | None:
        if not self.active_case:
            return None
        pending = [item for item in self.active_case.get('approvals') or [] if item.get('status') == 'pending']
        return pending[0] if len(pending) == 1 else None

    @work(thread=True, exclusive=True)
    def refresh_runtime(self) -> None:
        try:
            status = self.client.request('GET', '/v1/runtime/status')
            text = f"运行状态 · {status.get('status', '未知')}\n数据库：{self._check_state(status, 'database')} · 配置：{self._check_state(status, 'configuration')}"
            self.call_from_thread(self._set_runtime_status, text)
        except Exception as exc:
            self.call_from_thread(self._set_runtime_status, f'运行状态不可用\n{exc}')

    def _set_runtime_status(self, text: str) -> None:
        self.query_one('#runtime-status', Static).update(text)

    @staticmethod
    def _check_state(status: dict[str, Any], name: str) -> str:
        checks = status.get('checks') if isinstance(status.get('checks'), dict) else {}
        value = checks.get(name) if isinstance(checks, dict) else {}
        return '正常' if isinstance(value, dict) and value.get('ok') else '异常'

    @work(thread=True, exclusive=True)
    def refresh_cases(self) -> None:
        try:
            cases = self.client.request('GET', f'/v1/cases?limit={self.case_limit}')
            self.call_from_thread(self._render_case_list, cases if isinstance(cases, list) else [])
        except Exception as exc:
            self.call_from_thread(self.notify, f'刷新案例失败：{exc}', severity='error')

    def _render_case_list(self, cases: list[dict[str, Any]]) -> None:
        self.cases = cases
        options = [Option(case_option_text(case), id=str(case.get('id'))) for case in cases]
        case_list = self.query_one('#case-list', OptionList)
        case_list.clear_options()
        case_list.add_options(options)
        if self.active_case_id:
            for index, case in enumerate(cases):
                if str(case.get('id')) == self.active_case_id:
                    case_list.highlighted = index
                    break

    @work(thread=True, exclusive=True)
    def load_case(self, case_id: str) -> None:
        try:
            case = self.client.request('GET', f'/v1/cases/{case_id}')
            self.call_from_thread(self._activate_case, case)
        except Exception as exc:
            self.call_from_thread(self.notify, f'加载案例 {case_id} 失败：{exc}', severity='error')

    def _activate_case(self, case: dict[str, Any]) -> None:
        self.active_case_id = str(case.get('id'))
        self.active_case = case
        self.expanded_events = False
        self.metrics_expanded = False
        self._last_canvas_signature = None
        self._render_case(case)
        self.load_case_metrics(self.active_case_id)
        self.focus_input()

    @work(thread=True, exclusive=True, group='case-metrics')
    def load_case_metrics(self, case_id: str) -> None:
        try:
            metrics = self.client.request('GET', f'/v1/cases/{case_id}/metrics')
            self.call_from_thread(self._receive_case_metrics, case_id, metrics if isinstance(metrics, dict) else {})
        except Exception:
            # Metrics are a presentation convenience. The Case execution path
            # and approval controls must remain usable if they are unavailable.
            return

    def _receive_case_metrics(self, case_id: str, metrics: dict[str, Any]) -> None:
        self._case_metrics[case_id] = metrics
        if (
            case_id == self.active_case_id
            and self.active_case
            and not self.expanded_events
            and not self._case_streaming
        ):
            self._rebuild_execution_canvas(self.active_case)

    @work(thread=True, exclusive=True)
    def poll_active_case(self) -> None:
        case_id = self.active_case_id
        if not case_id:
            return
        try:
            case = self.client.request('GET', f'/v1/cases/{case_id}')
            self.call_from_thread(self._apply_case_poll, case_id, case)
        except Exception:
            # Polling is a convenience.  Avoid noisy transient network notices;
            # explicit refresh and all user actions still surface failures.
            return

    def _apply_case_poll(self, case_id: str, case: dict[str, Any]) -> None:
        if self.active_case_id != case_id:
            return
        self.active_case = case
        if (
            not self.expanded_events
            and not self._case_streaming
            and self._canvas_signature(case) == self._last_canvas_signature
        ):
            return
        self._render_case(case, append_only=True)
        self.load_case_metrics(case_id)

    @on(OptionList.OptionSelected, '#case-list')
    def select_case(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id
        if option_id:
            self.load_case(str(option_id))

    @on(Input.Submitted, '#command-input')
    def submit_input(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ''
        if not text:
            return
        if text.startswith('/'):
            self.handle_command(text)
        elif self.active_case_id:
            self.ask_case(self.active_case_id, text)
        else:
            self.ask_general(text, list(self.general_history[-12:]))

    def handle_command(self, text: str) -> None:
        command, _, rest = text.partition(' ')
        argument = rest.strip()
        if command == '/help':
            self._write('帮助', '/new 新建案例 · /focus <案例ID> 进入案例 · /back 返回通用对话 · /refresh 刷新 · /events 切换 Trace 详情 · /metrics 展开当前 Case 运行指标 · /eval 评估 · /role <角色> 切换本地演示身份 · /reset-demo 重置演示库存 · /approve <审批ID> 批准 · /reject <审批ID> 驳回并 Replan · /revoke <审批ID> 取消 · /quit 退出', 'dim')
        elif command == '/new':
            self.action_new_case()
        elif command == '/focus':
            if not argument:
                self.notify('用法：/focus <案例ID>', severity='warning')
            else:
                self.load_case(argument)
        elif command == '/back':
            self.action_back_to_general()
        elif command == '/refresh':
            self.action_refresh()
        elif command == '/events':
            if not self.active_case:
                self.notify('请先选择一个案例。', severity='warning')
            else:
                self.expanded_events = not self.expanded_events
                self._render_case(self.active_case)
            if self.expanded_events:
                self._write('SYSTEM', '已切换至 Trace 技术详情。', 'dim')
            else:
                self.notify('已切换至 Agent 执行画布。', severity='information')
        elif command == '/metrics':
            if not self.active_case:
                self.notify('请先选择一个 Case。', severity='warning')
            elif self.expanded_events:
                self.notify('请先用 /events 返回 Agent 执行画布。', severity='warning')
            else:
                self.metrics_expanded = not self.metrics_expanded
                self._last_canvas_signature = None
                self._render_case(self.active_case)
        elif command == '/eval':
            self.action_show_eval()
        elif command == '/role':
            self.switch_demo_role(argument)
        elif command == '/reset-demo':
            self.open_sandbox_reset()
        elif command in {'/approve', '/reject', '/revoke'}:
            if not argument:
                self._open_active_approval('approve' if command == '/approve' else ('reject' if command == '/reject' else 'revoke'))
            else:
                self._approval_by_id(argument, 'approve' if command == '/approve' else ('reject' if command == '/reject' else 'revoke'))
        elif command == '/quit':
            self.exit()
        else:
            self.notify(f'未知命令：{command}。请使用 /help。', severity='warning')
        self.focus_input()

    def switch_demo_role(self, role: str) -> None:
        """Switch among configured local demo identities using real API keys."""
        normalized = role.strip()
        if not self._demo_operator_keys:
            self.notify('角色切换仅在配置了 OPERATOR_SEED_KEYS 的 local 环境可用。', severity='warning')
            return
        if not normalized:
            self._write('身份', '可用角色：' + '、'.join(self._demo_operator_keys), 'dim')
            return
        key = self._demo_operator_keys.get(normalized)
        if not key:
            self.notify('未知角色。可用角色：' + '、'.join(self._demo_operator_keys), severity='warning')
            return
        self.client.operator_key = key
        self.active_operator_role = normalized
        self._render_operator_identity()
        self._write('身份', f'已切换为 {normalized}；后续审批由服务端按此 API Key 鉴权。', 'green')
        self.action_refresh()

    def open_sandbox_reset(self) -> None:
        self.push_screen(ConfirmSandboxResetScreen(), self._sandbox_reset_response)

    def _sandbox_reset_response(self, confirmed: bool) -> None:
        if confirmed:
            self.reset_demo_sandbox()

    @work(thread=True)
    def reset_demo_sandbox(self) -> None:
        try:
            data = self.client.request('POST', '/v1/sandbox/seed', {})
            actions = data.get('actions') if isinstance(data, dict) else []
            summary = ' · '.join(
                f"{item.get('type')}={item.get('status')}"
                for item in actions if isinstance(item, dict)
            ) or '已提交库存重置'
            self.call_from_thread(self._sandbox_reset_completed, summary)
        except Exception as exc:
            self.call_from_thread(self.notify, f'演示库存重置失败：{exc}', severity='error')

    def _sandbox_reset_completed(self, summary: str) -> None:
        self._write('SANDBOX', f'演示库存已恢复：{summary}', 'green')
        self.notify('演示库存已恢复，可新建同一订单的 Case。', severity='information')
        self.action_refresh()

    def action_refresh(self) -> None:
        self.refresh_runtime()
        self.refresh_cases()
        if self.active_case_id:
            self.load_case(self.active_case_id)

    def action_back_to_general(self) -> None:
        self.active_case_id = None
        self.active_case = None
        self.seen_event_ids.clear()
        self.expanded_events = False
        self._last_canvas_signature = None
        self._render_general()
        self.focus_input()

    def action_new_case(self) -> None:
        self.push_screen(NewCaseScreen(), self._new_case_response)

    def _new_case_response(self, payload: dict[str, str] | None) -> None:
        if payload:
            self.create_case(payload)

    @work(thread=True)
    def create_case(self, payload: dict[str, str]) -> None:
        try:
            result = self.client.request('POST', '/v1/cases', payload)
            case_id = str(result.get('case_id') or '')
            if not case_id:
                raise ApiClientError('Case create API returned no case_id')
            self.call_from_thread(self.notify, f"案例{'已存在' if result.get('duplicate') else '已创建'}：{case_id[:8]}", severity='information')
            self.call_from_thread(self.refresh_cases)
            self.call_from_thread(self.load_case, case_id)
        except Exception as exc:
            self.call_from_thread(self.notify, f'创建案例失败：{exc}', severity='error')

    @work(thread=True)
    def ask_general(self, question: str, history: list[dict[str, str]]) -> None:
        try:
            answer_parts: list[str] = []
            self.call_from_thread(self._begin_stream, 'general', question)
            for event, data in self.client.stream('POST', '/v1/chat/stream', {'question': question, 'history': history}):
                if event == 'delta':
                    part = str(data.get('text') or '')
                    answer_parts.append(part)
                    self.call_from_thread(self._append_stream_delta, 'general', part)
                elif event == 'error':
                    raise ApiClientError(str(data.get('error_code') or data.get('error_type') or 'stream_failed'))
            self.call_from_thread(self._finish_general_stream, question, ''.join(answer_parts))
        except Exception as exc:
            self.call_from_thread(self._receive_general_error, question, str(exc))

    def _begin_stream(self, scope: str, question: str) -> None:
        if (scope == 'general' and self.active_case_id is None) or (scope == 'case' and self.active_case_id):
            if scope == 'case' and self.active_case and not self.expanded_events:
                self._case_streaming = True
                self._case_stream_question = question
                self._case_stream_answer = ''
                self._rebuild_execution_canvas(self.active_case)
            else:
                self._write('USER', question, 'yellow')
                self._write('ANSWER', '', 'blue')

    def _append_stream_delta(self, scope: str, text: str) -> None:
        # RichLog has no mutable last line. Small deltas are appended without
        # clearing business trace; the complete reply remains in transcript.
        if not text:
            return
        if scope == 'case' and self.active_case_id and not self.expanded_events:
            self._case_stream_answer += text
            try:
                answer = self.query_one('#case-stream-answer', Static)
            except Exception:
                # The first delta can arrive before the asynchronous canvas
                # mount completes; the next delta (or final rebuild) will show it.
                return
            answer.update(f'ANSWER\n{self._case_stream_answer}')
            self._canvas().scroll_end(animate=False)
            return
        if text:
            self._trace().write(Text(text, style='blue'), scroll_end=True)

    def _finish_general_stream(self, question: str, answer: str) -> None:
        self.general_transcript.extend([('USER', question, 'yellow'), ('ANSWER', answer, 'blue')])
        self.general_history.extend([{'role': 'user', 'content': question}, {'role': 'assistant', 'content': answer}])
        self.focus_input()

    def _receive_general_answer(self, question: str, data: dict[str, Any]) -> None:
        answer = str(data.get('answer') or '')
        self.general_transcript.extend([('USER', question, 'yellow'), ('ANSWER', answer, 'blue')])
        self.general_history.extend([{'role': 'user', 'content': question}, {'role': 'assistant', 'content': answer}])
        if self.active_case_id is None:
            self._write('USER', question, 'yellow')
            source = str(data.get('source') or 'llm')
            self._write('ANSWER', f'（{source}）{answer}', 'blue')
        self.focus_input()

    def _receive_general_error(self, question: str, error: str) -> None:
        self.general_transcript.extend([('USER', question, 'yellow'), ('ERROR', error, 'red')])
        if self.active_case_id is None:
            self._write('USER', question, 'yellow')
            self._write('ERROR', error, 'red')
        self.focus_input()

    @work(thread=True)
    def ask_case(self, case_id: str, question: str) -> None:
        try:
            answer_parts: list[str] = []
            tools: list[dict[str, Any]] = []
            self.call_from_thread(self._begin_stream, 'case', question)
            for event, data in self.client.stream('POST', f'/v1/cases/{case_id}/ask/stream', {'question': question}):
                if event == 'tool':
                    tools.append(data)
                    self.call_from_thread(self._append_live_tool, data)
                elif event == 'delta':
                    part = str(data.get('text') or '')
                    answer_parts.append(part)
                    self.call_from_thread(self._append_stream_delta, 'case', part)
                elif event == 'error':
                    raise ApiClientError(str(data.get('error_code') or data.get('error_type') or 'stream_failed'))
            self.call_from_thread(self._finish_case_stream, case_id, question, ''.join(answer_parts), tools)
        except Exception as exc:
            self.call_from_thread(self._receive_case_error, case_id, question, str(exc))

    def _finish_case_stream(self, case_id: str, question: str, answer: str, tools: list[dict[str, Any]]) -> None:
        lines: list[tuple[str, str, str]] = [('USER', question, 'yellow')]
        lines.append(('ANSWER', answer, 'blue'))
        self.case_transcript.setdefault(case_id, []).extend(lines)
        self._case_streaming = False
        self._case_stream_answer = ''
        self._case_stream_question = ''
        if self.active_case_id == case_id:
            self.load_case(case_id)
        self.focus_input()

    def _append_live_tool(self, data: dict[str, Any]) -> None:
        """Show a streamed Case-question tool call using the same trace index."""
        self._tool_index += 1
        detail = f"调用只读 Tool · {data.get('tool') or 'read_tool'} · {data.get('status') or '未知'}"
        if self.active_case_id and not self.expanded_events:
            self._case_stream_answer += f"\n\nTool · {data.get('tool') or 'read_tool'} · {data.get('status') or '未知'}"
            try:
                self.query_one('#case-stream-answer', Static).update(f'ANSWER\n{self._case_stream_answer}')
            except Exception:
                pass
        else:
            self._write(f'TOOL {self._tool_index:02d}', detail, 'cyan')

    def _receive_case_answer(self, case_id: str, question: str, data: dict[str, Any]) -> None:
        answer = str(data.get('answer') or '')
        lines: list[tuple[str, str, str]] = [('USER', question, 'yellow')]
        for observation in data.get('observations') or []:
            tool = observation.get('tool') or 'read_tool'
            result = observation.get('result') if isinstance(observation.get('result'), dict) else {}
            detail = f"{tool} · {'error=' + str(result.get('error')) if result.get('error') else 'fields=' + ', '.join(str(key) for key in list(result)[:5])}"
            lines.append(('TOOL', detail, 'cyan'))
        lines.append(('ANSWER', answer, 'blue'))
        self.case_transcript.setdefault(case_id, []).extend(lines)
        if self.active_case_id == case_id:
            for label, text, style in lines:
                self._write(label, text, style)
            self.load_case(case_id)
        self.focus_input()

    def _receive_case_error(self, case_id: str, question: str, error: str) -> None:
        lines = [('USER', question, 'yellow'), ('ERROR', error, 'red')]
        self.case_transcript.setdefault(case_id, []).extend(lines)
        self._case_streaming = False
        self._case_stream_answer = ''
        self._case_stream_question = ''
        if self.active_case_id == case_id:
            if self.active_case and not self.expanded_events:
                self._rebuild_execution_canvas(self.active_case)
            else:
                for label, text, style in lines:
                    self._write(label, text, style)
        self.focus_input()

    def action_show_eval(self) -> None:
        self.load_eval_summary()

    @work(thread=True)
    def load_eval_summary(self) -> None:
        try:
            data = self.client.request('GET', f'/v1/evals/summary?limit={self.case_limit}')
            text = (
                f"评估 · 案例数={data.get('total_cases', 0)} · 任务完成率={self._percent(data.get('task_success_rate'))}\n"
                f"证据一致性={self._percent(data.get('evidence_faithfulness_rate'))} · "
                f"写后验证通过率={self._percent(data.get('verification_pass_rate'))}\n"
                f"平均耗时={data.get('avg_duration_seconds', 'n/a')} 秒 · "
                f"平均只读工具数={data.get('avg_read_tool_calls', 'n/a')}"
            )
            self.call_from_thread(self._write, 'EVAL', text, 'magenta')
        except Exception as exc:
            self.call_from_thread(self.notify, f'加载评估失败：{exc}', severity='error')

    @staticmethod
    def _percent(value: Any) -> str:
        return 'n/a' if not isinstance(value, (int, float)) else f'{value * 100:.0f}%'

    @on(Button.Pressed, '#approve-action')
    def approve_active(self) -> None:
        self._open_active_approval('approve')

    @on(Button.Pressed, '#reject-replan-action')
    def reject_and_replan_active(self) -> None:
        self._open_active_approval('reject')

    def _open_active_approval(self, operation: str) -> None:
        approval = self._active_pending_approval()
        if approval is None:
            self.notify('当前案例没有唯一的待审批项。请使用 /approve <审批ID> 或 /reject <审批ID>。', severity='warning')
            return
        self.push_screen(ConfirmApprovalScreen(approval, operation=operation), lambda result: self._approval_response(approval, result))

    def _approval_by_id(self, approval_id: str, operation: str) -> None:
        approval = {'id': approval_id, 'action': {}, 'required_roles': []}
        if self.active_case:
            approval = next((item for item in self.active_case.get('approvals') or [] if str(item.get('id')) == approval_id), approval)
        self.push_screen(ConfirmApprovalScreen(approval, operation=operation), lambda result: self._approval_response(approval, result))

    def _approval_response(self, approval: dict[str, Any], result: dict[str, str] | None) -> None:
        if result:
            self.change_approval(str(approval.get('id')), result['operation'], result.get('reason') or '')

    @work(thread=True)
    def change_approval(self, approval_id: str, operation: str, reason: str) -> None:
        try:
            if operation == 'approve':
                data = self.client.request('POST', f'/v1/approvals/{approval_id}/approve')
            elif operation == 'reject':
                data = self.client.request('POST', f'/v1/approvals/{approval_id}/reject-replan', {'reason': reason})
            else:
                data = self.client.request('POST', f'/v1/approvals/{approval_id}/revoke', {'reason': reason} if reason else {})
            self.call_from_thread(self._approval_changed, approval_id, operation, data)
        except Exception as exc:
            self.call_from_thread(self.notify, f'审批操作失败：{exc}', severity='error')

    def _approval_changed(self, approval_id: str, operation: str, data: dict[str, Any]) -> None:
        operation_text = '批准' if operation == 'approve' else ('驳回并 Replan' if operation == 'reject' else '取消')
        self._write('APPROVAL', f"已提交{operation_text} {approval_id[:8]} · 服务端状态={data.get('status')}", 'green')
        self.notify(f'审批{operation_text}请求已提交。', severity='information')
        if self.active_case_id:
            self.load_case(self.active_case_id)


def run_workbench(client: ApiClient, *, case_limit: int = 24, poll_interval: float = 1.0) -> int:
    """Run the local interactive shell without exposing server internals."""
    ResolveOpsWorkbench(client, case_limit=case_limit, poll_interval=poll_interval).run()
    return 0
