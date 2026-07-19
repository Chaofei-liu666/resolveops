"""Deterministic tool-call trace for Case audit and debugging.

The LLM may decide which read tools to call, but the explanation shown to
operators should come from recorded observations, not from a second model pass.
"""
from __future__ import annotations

from typing import Any


TOOL_PURPOSES = {
    'get_order': 'Read the business object under investigation and extract order amount, customer, item, quantity, warehouse and delivery facts.',
    'get_inventory': 'Read current stock for a specific SKU and warehouse before planning transfer or purchase actions.',
    'list_alternative_warehouses': 'Discover candidate warehouses that may be considered for shortage resolution.',
    'get_customer_profile': 'Read customer constraints and risk signals used by planning and approval policy.',
    'get_item_supply_profile': 'Read lead-time and replenishment facts used to judge purchase or supplier-follow-up feasibility.',
    'get_inbound_purchase': 'Read inbound purchase evidence instead of assuming supply exists or does not exist.',
    'get_transfer_options': 'Read configured transfer lanes, transit time and unit cost before proposing internal transfer.',
    'get_reference_price': 'Read reference price evidence for price-mismatch review planning.',
}


def _actions(plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(plan, dict):
        return []
    actions = plan.get('actions')
    if isinstance(actions, list):
        return [action for action in actions if isinstance(action, dict)]
    if plan.get('action_type'):
        return [plan]
    return []


def _action_label(action: dict[str, Any]) -> str:
    return action.get('action_id') or action.get('action_type') or 'unknown_action'


def _same_sku(args: dict[str, Any], action_input: dict[str, Any]) -> bool:
    return not args.get('item_code') or not action_input.get('sku') or args.get('item_code') == action_input.get('sku')


def _supports_action(observation: dict[str, Any], action: dict[str, Any]) -> bool:
    tool = observation.get('tool')
    args = observation.get('arguments') or {}
    result = observation.get('result') or {}
    action_type = action.get('action_type')
    action_input = action.get('input') or action.get('arguments') or {}
    if result.get('error'):
        return False

    if tool == 'get_order':
        return True
    if tool == 'get_customer_profile':
        return True
    if action_type == 'transfer_stock':
        if tool == 'get_inventory':
            return (
                _same_sku(args, action_input)
                and args.get('warehouse') in {action_input.get('source'), action_input.get('target')}
            )
        if tool == 'get_transfer_options':
            lanes = result.get('lanes') or []
            return any(
                lane.get('source') == action_input.get('source')
                and lane.get('target') == action_input.get('target')
                for lane in lanes
            )
    if action_type == 'create_purchase_request':
        if tool == 'get_inventory':
            return _same_sku(args, action_input) and args.get('warehouse') == action_input.get('target')
        if tool in {'get_item_supply_profile', 'get_inbound_purchase'}:
            return _same_sku(args, action_input)
    if action_type == 'create_price_review_ticket':
        if tool == 'get_reference_price':
            return _same_sku(args, action_input)
    if action_type == 'create_supplier_followup_task':
        if tool in {'get_inbound_purchase', 'get_item_supply_profile'}:
            return _same_sku(args, action_input)
    return False


def _result_summary(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {'value': result}
    if result.get('error'):
        return {'error': result.get('error'), 'error_type': result.get('error_type')}
    if tool == 'get_order':
        return {
            'name': result.get('name'),
            'customer': result.get('customer'),
            'grand_total': result.get('grand_total'),
            'item_count': len(result.get('items') or []),
        }
    if tool == 'get_inventory':
        actual = float(result.get('actual_qty') or 0)
        reserved = float(result.get('reserved_qty') or 0)
        return {
            'item_code': result.get('item_code'),
            'warehouse': result.get('warehouse'),
            'actual_qty': actual,
            'reserved_qty': reserved,
            'usable_qty': max(0, actual - reserved),
        }
    if tool == 'get_transfer_options':
        return {
            'target': result.get('target'),
            'lane_count': len(result.get('lanes') or []),
            'lanes': result.get('lanes') or [],
        }
    if tool == 'get_customer_profile':
        return {
            'customer_name': result.get('customer_name'),
            'customer_group': result.get('customer_group'),
            'is_vip': result.get('is_vip'),
            'allows_partial_delivery': result.get('allows_partial_delivery'),
        }
    if tool == 'get_item_supply_profile':
        return {
            'item_code': result.get('item_code'),
            'lead_time_days': result.get('lead_time_days'),
            'minimum_order_qty': result.get('minimum_order_qty'),
            'safety_stock': result.get('safety_stock'),
        }
    if tool == 'get_inbound_purchase':
        return {
            'item_code': result.get('item_code'),
            'purchase_item_count': len(result.get('purchase_items') or []),
            'purchase_items': result.get('purchase_items') or [],
        }
    if tool == 'get_reference_price':
        return {
            'item_code': result.get('item_code'),
            'reference_rate': result.get('reference_rate'),
            'currency': result.get('currency'),
        }
    return {key: result.get(key) for key in list(result.keys())[:8]}


def build_tool_trace(
    observations: list[dict[str, Any]] | None,
    plan: dict[str, Any] | None = None,
    grounding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    actions = _actions(plan)
    rows: list[dict[str, Any]] = []
    action_evidence: dict[str, list[str]] = {_action_label(action): [] for action in actions}

    for index, observation in enumerate(observations or [], start=1):
        tool = observation.get('tool') or 'unknown_tool'
        tool_result = observation.get('tool_result') or {}
        metadata = observation.get('metadata') or {}
        scheduler = observation.get('scheduler') or tool_result.get('scheduler') or {}
        status = tool_result.get('status') or ('failed' if (observation.get('result') or {}).get('error') else 'success')
        evidence_id = f'E-{index:03d}'
        supports = [_action_label(action) for action in actions if _supports_action(observation, action)]
        for action_label in supports:
            action_evidence.setdefault(action_label, []).append(evidence_id)
        rows.append({
            'evidence_id': evidence_id,
            'tool': tool,
            'purpose': TOOL_PURPOSES.get(tool, metadata.get('description') or 'Read external business evidence.'),
            'arguments': observation.get('arguments') or {},
            'status': status,
            'retryable': tool_result.get('retryable'),
            'evidence_usable': tool_result.get('evidence_usable', status == 'success'),
            'source_system': tool_result.get('source_system') or metadata.get('source_system'),
            'scheduler_source': scheduler.get('source'),
            'risk_level': metadata.get('risk_level'),
            'side_effect': metadata.get('side_effect'),
            'result_summary': _result_summary(tool, observation.get('result') or {}),
            'supports_actions': supports,
        })

    failed = [row for row in rows if row['status'] != 'success' or row['evidence_usable'] is False]
    return {
        'summary': {
            'observation_count': len(rows),
            'successful_observation_count': len(rows) - len(failed),
            'failed_observation_count': len(failed),
            'tools_used': sorted({row['tool'] for row in rows}),
            'grounding_allowed': grounding.get('allowed') if isinstance(grounding, dict) else None,
            'grounding_reason': grounding.get('reason') if isinstance(grounding, dict) else None,
        },
        'action_evidence': action_evidence,
        'observations': rows,
    }
