"""Evidence grounding checks for model-proposed Action Plans.

The LLM may decide what to investigate and what to propose, but it does not get
to decide whether its proposal is sufficiently grounded.  This module validates
the semantic link between read-tool observations and executable actions.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import Any


def usable_qty(stock: dict[str, Any] | None) -> float:
    if not isinstance(stock, dict) or stock.get('error'):
        return 0
    return max(0, float(stock.get('actual_qty') or 0) - float(stock.get('reserved_qty') or 0))


def observations_by_tool(observations: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for observation in observations or []:
        result.setdefault(observation.get('tool'), []).append(observation)
    return result


def first_success(observations: list[dict[str, Any]], tool: str) -> dict[str, Any] | None:
    for observation in observations or []:
        if observation.get('tool') == tool and not (observation.get('result') or {}).get('error'):
            return observation.get('result')
    return None


def matching_inventory(observations: list[dict[str, Any]], sku: str, warehouse: str) -> dict[str, Any] | None:
    for observation in observations or []:
        if observation.get('tool') != 'get_inventory':
            continue
        args = observation.get('arguments') or {}
        result = observation.get('result') or {}
        if result.get('error'):
            continue
        if args.get('item_code') == sku and args.get('warehouse') == warehouse:
            return result
    return None


def matching_lane(observations: list[dict[str, Any]], sku: str, source: str, target: str) -> dict[str, Any] | None:
    for observation in observations or []:
        if observation.get('tool') != 'get_transfer_options':
            continue
        args = observation.get('arguments') or {}
        result = observation.get('result') or {}
        if result.get('error') or args.get('item_code') != sku or args.get('target') != target:
            continue
        for lane in result.get('lanes') or []:
            if lane.get('source') == source and lane.get('target') == target:
                return lane
    return None


def order_item(order: dict[str, Any], sku: str) -> dict[str, Any] | None:
    for item in order.get('items') or []:
        if item.get('item_code') == sku:
            return item
    return None


def order_item_rate(item: dict[str, Any]) -> float | None:
    for key in ('rate', 'base_rate', 'net_rate'):
        if item.get(key) is not None:
            return float(item.get(key) or 0)
    return None


def reference_price_for(observations: list[dict[str, Any]], sku: str) -> dict[str, Any] | None:
    for observation in observations or []:
        if observation.get('tool') != 'get_reference_price':
            continue
        args = observation.get('arguments') or {}
        result = observation.get('result') or {}
        if result.get('error') or args.get('item_code') != sku:
            continue
        if result.get('reference_rate') is None:
            continue
        return result
    return None


def inbound_purchase_for(observations: list[dict[str, Any]], sku: str, purchase_order: str) -> dict[str, Any] | None:
    for observation in observations or []:
        if observation.get('tool') != 'get_inbound_purchase':
            continue
        args = observation.get('arguments') or {}
        result = observation.get('result') or {}
        if result.get('error') or args.get('item_code') != sku:
            continue
        for item in result.get('purchase_items') or []:
            if item.get('purchase_order') == purchase_order:
                return item
    return None


def order_context(observations: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    problems: list[str] = []
    order = first_success(observations, 'get_order')
    if not order:
        return None, None, ['missing successful get_order evidence']
    items = order.get('items') or []
    if not items:
        return order, None, ['order has no item evidence']
    return order, items[0], problems


def required_shortage(observations: list[dict[str, Any]]) -> tuple[float | None, list[str]]:
    order, item, problems = order_context(observations)
    if problems:
        return None, problems
    assert item is not None
    target = item.get('warehouse')
    sku = item.get('item_code')
    required = float(item.get('qty') or 0)
    target_stock = matching_inventory(observations, sku, target)
    if target_stock is None:
        return None, [f'missing target inventory evidence for {sku} at {target}']
    return max(0, required - usable_qty(target_stock)), []


def validates_purchase_timing(observations: list[dict[str, Any]], required_by: str) -> tuple[bool, str | None]:
    supply = first_success(observations, 'get_item_supply_profile')
    if not supply:
        return False, 'missing item supply profile evidence'
    lead_time = supply.get('lead_time_days')
    if lead_time is None:
        return False, 'missing lead_time_days evidence'
    try:
        due = date.fromisoformat(required_by[:10])
        if date.today() + timedelta(days=float(lead_time)) > due:
            return False, 'purchase lead time exceeds required_by date'
    except (ValueError, TypeError):
        return False, 'invalid required_by date'
    return True, None


def validate_price_mismatch_plan(plan: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    actions = plan.get('actions') if isinstance(plan, dict) else None
    if not isinstance(actions, list) or not actions:
        return {'allowed': False, 'reason': 'plan has no actions', 'problems': ['plan has no actions']}
    problems: list[str] = []
    order = first_success(observations, 'get_order')
    if not order:
        problems.append('missing successful get_order evidence')
    if len(actions) != 1:
        problems.append('price_mismatch currently supports exactly one governed review action')

    for action in actions:
        action_type = action.get('action_type')
        action_input = action.get('input') or {}
        if action_type != 'create_price_review_ticket':
            problems.append(f'{action_type} is not allowed for price_mismatch evidence grounding')
            continue
        sku = action_input.get('sku')
        if not sku:
            problems.append('create_price_review_ticket missing sku')
            continue
        item = order_item(order or {}, sku)
        if not item:
            problems.append(f'order has no item evidence for {sku}')
            continue
        rate = order_item_rate(item)
        if rate is None:
            problems.append(f'order item {sku} has no rate evidence')
            continue
        ref = reference_price_for(observations, sku)
        if not ref:
            problems.append(f'missing reference price evidence for {sku}')
            continue
        order_rate = float(action_input.get('order_rate') or 0)
        reference_rate = float(action_input.get('reference_rate') or 0)
        difference = float(action_input.get('difference') or 0)
        if abs(order_rate - rate) > 0.01:
            problems.append(f'order_rate {order_rate} does not match order evidence {rate}')
        if abs(reference_rate - float(ref.get('reference_rate') or 0)) > 0.01:
            problems.append(f'reference_rate {reference_rate} does not match reference price evidence {ref.get("reference_rate")}')
        if abs((order_rate - reference_rate) - difference) > 0.01:
            problems.append('difference does not equal order_rate - reference_rate')
        if abs(difference) <= 0.01:
            problems.append('price difference is zero; no review action is grounded')

    return {
        'allowed': not problems,
        'reason': 'grounded' if not problems else 'evidence_not_sufficient',
        'problems': problems,
        'case_type': 'price_mismatch',
    }


def validate_inventory_shortage_plan(plan: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    actions = plan.get('actions') if isinstance(plan, dict) else None
    if not isinstance(actions, list) or not actions:
        return {'allowed': False, 'reason': 'plan has no actions', 'problems': ['plan has no actions']}

    shortage, base_problems = required_shortage(observations)
    problems = list(base_problems)
    covered = 0.0

    inbound_checked = any(
        observation.get('tool') == 'get_inbound_purchase' and not (observation.get('result') or {}).get('error')
        for observation in observations
    )

    for action in actions:
        action_type = action.get('action_type')
        action_input = action.get('input') or {}
        sku = action_input.get('sku')
        target = action_input.get('target')
        quantity = float(action_input.get('quantity') or 0)

        if quantity <= 0:
            problems.append(f'{action_type} has non-positive quantity')
            continue

        if action_type == 'transfer_stock':
            source = action_input.get('source')
            source_stock = matching_inventory(observations, sku, source)
            if source_stock is None:
                problems.append(f'transfer_stock missing source inventory evidence for {sku} at {source}')
            elif usable_qty(source_stock) < quantity:
                problems.append(f'transfer_stock quantity {quantity} exceeds usable source stock {usable_qty(source_stock)} at {source}')
            if matching_lane(observations, sku, source, target) is None:
                problems.append(f'transfer_stock missing transfer lane evidence from {source} to {target}')
            covered += quantity

        elif action_type == 'create_purchase_request':
            ok, reason = validates_purchase_timing(observations, action_input.get('required_by') or '')
            if not ok:
                problems.append(f'create_purchase_request {reason}')
            if not inbound_checked:
                problems.append('create_purchase_request missing inbound purchase evidence')
            covered += quantity

        else:
            problems.append(f'{action_type} has no evidence grounding rule')

    if shortage is not None and covered < shortage:
        problems.append(f'plan covers {covered} units but shortage is {shortage}')

    return {
        'allowed': not problems,
        'reason': 'grounded' if not problems else 'evidence_not_sufficient',
        'problems': problems,
        'shortage': shortage,
        'covered_quantity': covered,
        'case_type': 'inventory_shortage',
    }


def validate_delivery_delay_plan(plan: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    actions = plan.get('actions') if isinstance(plan, dict) else None
    if not isinstance(actions, list) or not actions:
        return {'allowed': False, 'reason': 'plan has no actions', 'problems': ['plan has no actions']}
    problems: list[str] = []
    order = first_success(observations, 'get_order')
    if not order:
        problems.append('missing successful get_order evidence')
    if len(actions) != 1:
        problems.append('delivery_delay currently supports exactly one governed supplier follow-up action')

    for action in actions:
        action_type = action.get('action_type')
        action_input = action.get('input') or {}
        if action_type != 'create_supplier_followup_task':
            problems.append(f'{action_type} is not allowed for delivery_delay evidence grounding')
            continue
        sku = action_input.get('sku')
        purchase_order = action_input.get('purchase_order')
        if not sku or not purchase_order:
            problems.append('create_supplier_followup_task missing sku or purchase_order')
            continue
        item = order_item(order or {}, sku)
        if not item:
            problems.append(f'order has no item evidence for {sku}')
            continue
        delivery_date = (item.get('delivery_date') or (order or {}).get('delivery_date') or '')
        if not delivery_date:
            problems.append('missing customer delivery date evidence')
            continue
        inbound = inbound_purchase_for(observations, sku, purchase_order)
        if not inbound:
            problems.append(f'missing inbound purchase evidence for {sku} on {purchase_order}')
            continue
        schedule_date = inbound.get('schedule_date')
        if not schedule_date:
            problems.append(f'inbound purchase {purchase_order} has no schedule_date evidence')
            continue
        supplier = action_input.get('supplier')
        if inbound.get('supplier') and supplier != inbound.get('supplier'):
            problems.append(f'supplier {supplier} does not match inbound evidence {inbound.get("supplier")}')
        expected_delivery_date = action_input.get('expected_delivery_date')
        if expected_delivery_date != schedule_date:
            problems.append(f'expected_delivery_date {expected_delivery_date} does not match inbound schedule_date {schedule_date}')
        try:
            due = date.fromisoformat(str(delivery_date)[:10])
            expected = date.fromisoformat(str(schedule_date)[:10])
            computed_delay = (expected - due).days
            delayed_by_days = float(action_input.get('delayed_by_days') or 0)
            if computed_delay <= 0:
                problems.append(f'inbound purchase {purchase_order} is not later than customer delivery date')
            if abs(delayed_by_days - computed_delay) > 0.01:
                problems.append(f'delayed_by_days {delayed_by_days} does not match computed delay {computed_delay}')
        except (TypeError, ValueError):
            problems.append('invalid delivery date evidence')

    return {
        'allowed': not problems,
        'reason': 'grounded' if not problems else 'evidence_not_sufficient',
        'problems': problems,
        'case_type': 'delivery_delay',
    }


def validate_plan_grounding(plan: dict[str, Any], observations: list[dict[str, Any]], case_type: str = 'inventory_shortage') -> dict[str, Any]:
    if case_type == 'price_mismatch':
        return validate_price_mismatch_plan(plan, observations)
    if case_type == 'delivery_delay':
        return validate_delivery_delay_plan(plan, observations)
    return validate_inventory_shortage_plan(plan, observations)
