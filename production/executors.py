"""Registered executors for governed write Action Tools.

The Worker owns orchestration concerns such as approval state, idempotency
records, stale-state replanning and Case transitions.  This module owns the
action-specific adapter call and read-after-write verification logic.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from sqlalchemy import select, text
from .erpnext import ERPNextAdapter
from .models import Case, PriceReview

PreflightFn = Callable[[Any, ERPNextAdapter, dict[str, Any]], dict[str, Any]]
ContextFn = Callable[[ERPNextAdapter, str, dict[str, Any]], dict[str, Any]]
WriteFn = Callable[[Any, ERPNextAdapter, dict[str, Any], str | None, str, Case], str]
VerifyFn = Callable[[Any, ERPNextAdapter, str, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class WriteActionExecutor:
    action_type: str
    invocation_tool: str
    preflight: PreflightFn
    context: ContextFn
    write: WriteFn
    verify: VerifyFn


def ok_preflight(_db: Any, _erp: ERPNextAdapter, _action_input: dict[str, Any]) -> dict[str, Any]:
    return {'ok': True}


def empty_context(_erp: ERPNextAdapter, _order_id: str, _action_input: dict[str, Any]) -> dict[str, Any]:
    return {}


def purchase_request_context(erp: ERPNextAdapter, order_id: str, _action_input: dict[str, Any]) -> dict[str, Any]:
    return {'company': erp.sales_order(order_id).get('company')}


def preflight_transfer_stock(db: Any, erp: ERPNextAdapter, action_input: dict[str, Any]) -> dict[str, Any]:
    # PostgreSQL advisory lock serializes writes on shared source inventory.
    locked=db.scalar(
        text('SELECT pg_try_advisory_xact_lock(hashtext(:k))'),
        {'k':f"stock:{action_input['source']}:{action_input['sku']}"},
    )
    if not locked:
        return {'ok': False, 'reason': 'resource_busy', 'retryable': True}
    fresh=erp.stock(action_input['sku'],action_input['source'])
    available=float(fresh['actual_qty'])-float(fresh.get('reserved_qty',0))
    if available < action_input['quantity']:
        return {
            'ok': False,
            'reason': 'source_inventory_changed',
            'retryable': False,
            'fresh_inventory': fresh,
            'message': f"Source inventory changed: {action_input['source']} has {available} usable units, below approved quantity {action_input['quantity']}.",
        }
    return {'ok': True, 'fresh_inventory': fresh}


def write_transfer_stock(_db: Any, erp: ERPNextAdapter, action_input: dict[str, Any], _company: str | None, idempotency_key: str, _case: Case) -> str:
    return erp.create_transfer_draft(
        source=action_input['source'],
        target=action_input['target'],
        item_code=action_input['sku'],
        qty=action_input['quantity'],
        idempotency_key=idempotency_key,
    )


def verify_transfer_stock(_db: Any, erp: ERPNextAdapter, external_id: str, action_input: dict[str, Any]) -> dict[str, Any]:
    doc=erp.stock_entry(external_id); item=doc['items'][0]
    verified=doc['docstatus']==0 and float(item['qty'])==float(action_input['quantity']) and item['s_warehouse']==action_input['source'] and item['t_warehouse']==action_input['target']
    return {'verified':verified,'event_data':{'stock_entry':external_id}}


def write_purchase_request(_db: Any, erp: ERPNextAdapter, action_input: dict[str, Any], company: str | None, idempotency_key: str, _case: Case) -> str:
    return erp.create_purchase_request(
        target=action_input['target'],
        item_code=action_input['sku'],
        qty=action_input['quantity'],
        required_by=action_input['required_by'],
        company=company,
        idempotency_key=idempotency_key,
    )


def verify_purchase_request(_db: Any, erp: ERPNextAdapter, external_id: str, action_input: dict[str, Any]) -> dict[str, Any]:
    doc=erp.material_request(external_id); item=doc['items'][0]
    verified=doc['docstatus']==0 and doc['material_request_type']=='Purchase' and float(item['qty'])==float(action_input['quantity']) and item.get('warehouse')==action_input['target'] and item['item_code']==action_input['sku']
    return {'verified':verified,'event_data':{'material_request':external_id}}


def write_price_review(db: Any, _erp: ERPNextAdapter, action_input: dict[str, Any], _company: str | None, idempotency_key: str, case: Case) -> str:
    existing = db.scalar(select(PriceReview).where(PriceReview.idempotency_key == idempotency_key))
    if existing:
        return existing.id
    review = PriceReview(
        tenant_id=case.tenant_id,
        case_id=case.id,
        order_id=case.order_id,
        sku=action_input['sku'],
        order_rate=float(action_input['order_rate']),
        reference_rate=float(action_input['reference_rate']),
        difference=float(action_input['difference']),
        idempotency_key=idempotency_key,
        data={'reason': action_input.get('reason'), 'source': 'agent_action_plan'},
    )
    db.add(review)
    db.flush()
    return review.id


def verify_price_review(db: Any, _erp: ERPNextAdapter, review_id: str, action_input: dict[str, Any]) -> dict[str, Any]:
    review = db.get(PriceReview, review_id)
    verified = bool(
        review
        and review.status == 'draft'
        and review.sku == action_input['sku']
        and float(review.order_rate) == float(action_input['order_rate'])
        and float(review.reference_rate) == float(action_input['reference_rate'])
        and float(review.difference) == float(action_input['difference'])
    )
    return {'verified': verified, 'event_data': {'price_review': review_id}}


EXECUTOR_REGISTRY: dict[str, WriteActionExecutor] = {
    'transfer_stock': WriteActionExecutor(
        action_type='transfer_stock',
        invocation_tool='create_transfer_draft',
        preflight=preflight_transfer_stock,
        context=empty_context,
        write=write_transfer_stock,
        verify=verify_transfer_stock,
    ),
    'create_purchase_request': WriteActionExecutor(
        action_type='create_purchase_request',
        invocation_tool='create_purchase_request_draft',
        preflight=ok_preflight,
        context=purchase_request_context,
        write=write_purchase_request,
        verify=verify_purchase_request,
    ),
    'create_price_review_ticket': WriteActionExecutor(
        action_type='create_price_review_ticket',
        invocation_tool='create_price_review_ticket',
        preflight=ok_preflight,
        context=empty_context,
        write=write_price_review,
        verify=verify_price_review,
    ),
}


def executor_for(action_type: str) -> WriteActionExecutor | None:
    return EXECUTOR_REGISTRY.get(action_type)
