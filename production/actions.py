"""Action tools and Action Plan normalization.

Write operations are tools too, but they are not directly callable by the LLM.
The LLM may propose them in an Action Plan; ResolveOps executes them only after
policy checks, bound approvals, idempotency control, and verification.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from uuid import uuid4
from .tools import ToolSpec, object_schema

Validator = Callable[[dict[str, Any]], dict[str, Any]]
ResourceKeys = Callable[[dict[str, Any]], list[str]]

ACTION_PROFILES: dict[str, set[str]] = {
    'inventory_shortage': {
        'transfer_stock',
        'create_purchase_request',
        'draft_customer_notification',
        'create_manual_ticket',
    },
    'price_mismatch': {
        'create_price_review_ticket',
        'create_manual_ticket',
    },
    'delivery_delay': {
        'create_supplier_followup_task',
        'create_manual_ticket',
    },
}


def action_types_for_case(event_type: str | None) -> set[str]:
    return ACTION_PROFILES.get(event_type or 'inventory_shortage', {'create_manual_ticket'})


@dataclass(frozen=True)
class ActionDefinition:
    action_type: str
    title: str
    executor: str | None
    approval_policy: str
    validator: Validator | None = None
    resource_keys: ResourceKeys | None = None
    verification: dict[str, Any] | None = None
    compensation: dict[str, Any] | None = None
    tool_spec: ToolSpec | None = None

    @property
    def executable(self) -> bool:
        return self.executor is not None


def transfer_input(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError('transfer_stock input must be an object')
    required = {'source', 'target', 'sku', 'quantity'}
    if not required <= data.keys() or any(data[k] in (None, '') for k in required):
        raise ValueError('transfer_stock requires source, target, sku and quantity')
    quantity = float(data['quantity'])
    if quantity <= 0:
        raise ValueError('transfer_stock quantity must be positive')
    return {'source': str(data['source']), 'target': str(data['target']), 'sku': str(data['sku']), 'quantity': quantity}


def purchase_request_input(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError('create_purchase_request input must be an object')
    required = {'target', 'sku', 'quantity', 'required_by'}
    if not required <= data.keys() or any(data[k] in (None, '') for k in required):
        raise ValueError('create_purchase_request requires target, sku, quantity and required_by')
    quantity = float(data['quantity'])
    if quantity <= 0:
        raise ValueError('create_purchase_request quantity must be positive')
    return {'target': str(data['target']), 'sku': str(data['sku']), 'quantity': quantity, 'required_by': str(data['required_by'])}


def price_review_input(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError('create_price_review_ticket input must be an object')
    required = {'sku', 'order_rate', 'reference_rate', 'difference'}
    if not required <= data.keys() or any(data[k] in (None, '') for k in required):
        raise ValueError('create_price_review_ticket requires sku, order_rate, reference_rate and difference')
    order_rate = float(data['order_rate'])
    reference_rate = float(data['reference_rate'])
    difference = float(data['difference'])
    if abs((order_rate - reference_rate) - difference) > 0.01:
        raise ValueError('create_price_review_ticket difference must equal order_rate - reference_rate')
    if abs(difference) <= 0.01:
        raise ValueError('create_price_review_ticket requires a non-zero price difference')
    return {
        'sku': str(data['sku']),
        'order_rate': order_rate,
        'reference_rate': reference_rate,
        'difference': difference,
        'reason': str(data.get('reason') or 'Order rate differs from reference price.'),
    }


def supplier_followup_input(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError('create_supplier_followup_task input must be an object')
    required = {'sku', 'purchase_order', 'supplier', 'expected_delivery_date', 'delayed_by_days'}
    if not required <= data.keys() or any(data[k] in (None, '') for k in required):
        raise ValueError('create_supplier_followup_task requires sku, purchase_order, supplier, expected_delivery_date and delayed_by_days')
    delayed_by_days = float(data['delayed_by_days'])
    if delayed_by_days <= 0:
        raise ValueError('create_supplier_followup_task delayed_by_days must be positive')
    return {
        'sku': str(data['sku']),
        'purchase_order': str(data['purchase_order']),
        'supplier': str(data['supplier']),
        'expected_delivery_date': str(data['expected_delivery_date']),
        'delayed_by_days': delayed_by_days,
        'reason': str(data.get('reason') or 'Inbound purchase delivery is later than the customer delivery date.'),
    }


ACTION_TOOL_SPECS: dict[str, ToolSpec] = {
    'transfer_stock': ToolSpec(
        name='transfer_stock',
        description='Create a draft inventory transfer between permitted warehouses.',
        parameters=object_schema(
            {
                'source': {'type': 'string'},
                'target': {'type': 'string'},
                'sku': {'type': 'string'},
                'quantity': {'type': 'number'},
            },
            ['source', 'target', 'sku', 'quantity'],
        ),
        permission='inventory:transfer:create_draft',
        side_effect='create_draft_record',
        risk_level='medium',
        source_system='ERPNextAdapter',
        executor_ref='erp.transfer_stock',
        llm_callable=False,
        requires_approval=True,
        idempotency_required=True,
        resource_keys=['inventory:{source}:{sku}'],
        verification={'tool': 'get_stock_entry', 'assertions': ['draft', 'source', 'target', 'sku', 'quantity']},
        compensation={'strategy': 'cancel_draft_transfer'},
    ),
    'create_purchase_request': ToolSpec(
        name='create_purchase_request',
        description='Create a draft replenishment request; never creates a Purchase Order.',
        parameters=object_schema(
            {
                'target': {'type': 'string'},
                'sku': {'type': 'string'},
                'quantity': {'type': 'number'},
                'required_by': {'type': 'string'},
            },
            ['target', 'sku', 'quantity', 'required_by'],
        ),
        permission='procurement:material_request:create_draft',
        side_effect='create_draft_record',
        risk_level='medium',
        source_system='ERPNextAdapter',
        executor_ref='erp.create_purchase_request',
        llm_callable=False,
        requires_approval=True,
        idempotency_required=True,
        resource_keys=['replenishment:{target}:{sku}'],
        verification={'tool': 'get_material_request', 'assertions': ['draft', 'target', 'sku', 'quantity', 'required_by']},
        compensation={'strategy': 'cancel_draft_purchase_request'},
    ),
    'draft_customer_notification': ToolSpec(
        name='draft_customer_notification',
        description='Draft a customer-facing notification without sending it.',
        parameters=object_schema(),
        permission='customer_communication:draft',
        side_effect='create_draft_record',
        risk_level='medium',
        source_system='CommunicationAdapter',
        executor_ref=None,
        llm_callable=False,
        requires_approval=True,
    ),
    'create_price_review_ticket': ToolSpec(
        name='create_price_review_ticket',
        description='Create a governed local price-review record for a detected order/reference price mismatch; never changes ERP prices.',
        parameters=object_schema(
            {
                'sku': {'type': 'string'},
                'order_rate': {'type': 'number'},
                'reference_rate': {'type': 'number'},
                'difference': {'type': 'number'},
                'reason': {'type': 'string'},
            },
            ['sku', 'order_rate', 'reference_rate', 'difference'],
        ),
        permission='pricing:review:create',
        side_effect='create_review_record',
        risk_level='high',
        source_system='ResolveOpsDB',
        executor_ref='resolveops.create_price_review_ticket',
        llm_callable=False,
        requires_approval=True,
        idempotency_required=True,
        resource_keys=['pricing:{sku}'],
        verification={'tool': 'get_price_review', 'assertions': ['draft', 'sku', 'order_rate', 'reference_rate', 'difference']},
        compensation={'strategy': 'close_draft_price_review'},
    ),
    'create_supplier_followup_task': ToolSpec(
        name='create_supplier_followup_task',
        description='Create a governed supplier/procurement follow-up record for an inbound delivery delay; never changes ERP purchase or sales documents.',
        parameters=object_schema(
            {
                'sku': {'type': 'string'},
                'purchase_order': {'type': 'string'},
                'supplier': {'type': 'string'},
                'expected_delivery_date': {'type': 'string'},
                'delayed_by_days': {'type': 'number'},
                'reason': {'type': 'string'},
            },
            ['sku', 'purchase_order', 'supplier', 'expected_delivery_date', 'delayed_by_days'],
        ),
        permission='supplier_followup:create',
        side_effect='create_followup_record',
        risk_level='medium',
        source_system='ResolveOpsDB',
        executor_ref='resolveops.create_supplier_followup_task',
        llm_callable=False,
        requires_approval=True,
        idempotency_required=True,
        resource_keys=['supplier_followup:{purchase_order}:{sku}'],
        verification={'tool': 'get_supplier_followup', 'assertions': ['draft', 'sku', 'purchase_order', 'supplier', 'expected_delivery_date']},
        compensation={'strategy': 'close_draft_supplier_followup'},
    ),
    'create_manual_ticket': ToolSpec(
        name='create_manual_ticket',
        description='Create a human handoff ticket for unresolved business exceptions.',
        parameters=object_schema(),
        permission='ticket:create',
        side_effect='create_task_record',
        risk_level='low',
        source_system='TicketAdapter',
        executor_ref=None,
        llm_callable=False,
        requires_approval=False,
    ),
}


REGISTRY: dict[str, ActionDefinition] = {
    'transfer_stock': ActionDefinition(
        action_type='transfer_stock',
        title='Create transfer draft',
        executor=ACTION_TOOL_SPECS['transfer_stock'].executor_ref,
        approval_policy='warehouse_manager',
        validator=transfer_input,
        resource_keys=lambda x: [f"inventory:{x['source']}:{x['sku']}"],
        verification=ACTION_TOOL_SPECS['transfer_stock'].verification,
        compensation=ACTION_TOOL_SPECS['transfer_stock'].compensation,
        tool_spec=ACTION_TOOL_SPECS['transfer_stock'],
    ),
    'create_purchase_request': ActionDefinition(
        action_type='create_purchase_request',
        title='Create purchase request draft',
        executor=ACTION_TOOL_SPECS['create_purchase_request'].executor_ref,
        approval_policy='procurement_manager',
        validator=purchase_request_input,
        verification=ACTION_TOOL_SPECS['create_purchase_request'].verification,
        compensation=ACTION_TOOL_SPECS['create_purchase_request'].compensation,
        tool_spec=ACTION_TOOL_SPECS['create_purchase_request'],
    ),
    'draft_customer_notification': ActionDefinition(
        'draft_customer_notification',
        'Draft customer notification',
        None,
        'sales_owner',
        tool_spec=ACTION_TOOL_SPECS['draft_customer_notification'],
    ),
    'create_price_review_ticket': ActionDefinition(
        action_type='create_price_review_ticket',
        title='Create price review ticket',
        executor=ACTION_TOOL_SPECS['create_price_review_ticket'].executor_ref,
        approval_policy='sales_and_finance',
        validator=price_review_input,
        resource_keys=lambda x: [f"pricing:{x['sku']}"],
        verification=ACTION_TOOL_SPECS['create_price_review_ticket'].verification,
        compensation=ACTION_TOOL_SPECS['create_price_review_ticket'].compensation,
        tool_spec=ACTION_TOOL_SPECS['create_price_review_ticket'],
    ),
    'create_supplier_followup_task': ActionDefinition(
        action_type='create_supplier_followup_task',
        title='Create supplier follow-up task',
        executor=ACTION_TOOL_SPECS['create_supplier_followup_task'].executor_ref,
        approval_policy='procurement_manager',
        validator=supplier_followup_input,
        resource_keys=lambda x: [f"supplier_followup:{x['purchase_order']}:{x['sku']}"],
        verification=ACTION_TOOL_SPECS['create_supplier_followup_task'].verification,
        compensation=ACTION_TOOL_SPECS['create_supplier_followup_task'].compensation,
        tool_spec=ACTION_TOOL_SPECS['create_supplier_followup_task'],
    ),
    'create_manual_ticket': ActionDefinition(
        'create_manual_ticket',
        'Create manual handoff ticket',
        None,
        'none',
        tool_spec=ACTION_TOOL_SPECS['create_manual_ticket'],
    ),
}


def normalize_proposal(proposal: dict[str, Any], rationale: str, evidence_refs: list[str] | None = None) -> dict[str, Any]:
    """Make every proposed write operation a governed Action Tool envelope."""
    action_type = proposal.get('action_type', 'transfer_stock')
    definition = REGISTRY.get(action_type)
    if not definition:
        raise ValueError(f'action_type not registered: {action_type}')
    raw_input = proposal.get('input') or proposal.get('arguments') or {k: proposal.get(k) for k in ('source', 'target', 'sku', 'quantity') if k in proposal}
    inputs = definition.validator(raw_input) if definition.validator else raw_input
    return {
        'action_id': str(uuid4()),
        'action_type': action_type,
        'title': proposal.get('title', definition.title),
        'evidence_refs': evidence_refs or [],
        'input': inputs,
        'preconditions': proposal.get('preconditions', []),
        'expected_effect': proposal.get('expected_effect', []),
        'risk': {'level': proposal.get('risk', definition.tool_spec.risk_level if definition.tool_spec else 'medium'), 'approval_policy': definition.approval_policy},
        'execution': {
            'executor': definition.executor,
            'idempotency_scope': 'case + action version',
            'resource_keys': definition.resource_keys(inputs) if definition.resource_keys else [],
        },
        'verification': definition.verification or {},
        'compensation': definition.compensation or {},
        'tool': definition.tool_spec.metadata() if definition.tool_spec else {},
        'rationale': rationale,
        'executable': definition.executable,
    }


def normalize_plan(proposals: list[dict[str, Any]], rationale: str, evidence_refs: list[str] | None = None, allowed_action_types: set[str] | None = None) -> dict[str, Any]:
    """A plan may contain one action or a coordinated set of actions."""
    if not isinstance(proposals, list) or not proposals or len(proposals) > 3:
        raise ValueError('recommended_actions must contain between one and three actions')
    actions = [normalize_proposal(proposal, rationale, evidence_refs) for proposal in proposals]
    if allowed_action_types is not None:
        disallowed = [action['action_type'] for action in actions if action['action_type'] not in allowed_action_types]
        if disallowed:
            raise ValueError(f'action_type not enabled for this case type: {", ".join(sorted(set(disallowed)))}')
    action_types = [action['action_type'] for action in actions]
    if len(set(action_types)) != len(action_types):
        raise ValueError('a plan may not repeat the same action type')
    return {'plan_id': str(uuid4()), 'actions': actions, 'rationale': rationale, 'state': 'proposed'}


def definition_for(action_type: str) -> ActionDefinition | None:
    return REGISTRY.get(action_type)


def action_tool_spec(action_type: str) -> ToolSpec | None:
    return ACTION_TOOL_SPECS.get(action_type)


def planner_action_catalog(event_type: str | None = None) -> list[dict[str, Any]]:
    """Return write Action Schemas visible to the planner but not executable by it.

    This is the single source of truth for actions the LLM may propose.  The
    same registry is later used by normalize_plan(), Policy Engine, Executor,
    and Verification.  This avoids prompt-only action definitions drifting away
    from runtime validation.
    """
    catalog = []
    enabled = action_types_for_case(event_type) if event_type else set(REGISTRY)
    for definition in REGISTRY.values():
        if definition.action_type not in enabled:
            continue
        spec = definition.tool_spec
        if not spec:
            continue
        catalog.append({
            'action_type': definition.action_type,
            'title': definition.title,
            'description': spec.description,
            'input_schema': spec.parameters,
            'executable': definition.executable,
            'llm_directly_callable': spec.llm_callable,
            'requires_approval': spec.requires_approval,
            'side_effect': spec.side_effect,
            'risk_level': spec.risk_level,
            'approval_policy': definition.approval_policy,
            'verification': spec.verification or {},
            'compensation': spec.compensation or {},
        })
    return catalog


def planner_action_instructions(event_type: str | None = None) -> str:
    """Compact planner instructions generated from the Action Registry."""
    lines = [
        'Available write actions are Action Plan schemas, not directly callable tools.',
        'Return them only inside recommended_actions. Do not attempt direct ERP writes.',
        'Each recommended action must use action_type and input matching one schema below:',
    ]
    for item in planner_action_catalog(event_type):
        required = item['input_schema'].get('required', [])
        properties = item['input_schema'].get('properties', {})
        fields = ', '.join(f'{name}:{properties.get(name, {}).get("type", "any")}' for name in required) or 'no input'
        executable = 'executable after policy/approval' if item['executable'] else 'planning/handoff only'
        lines.append(f"- {item['action_type']} input={{ {fields} }}; {executable}; side_effect={item['side_effect']}; approval={item['approval_policy']}")
    return '\n'.join(lines)


def registered_action_types() -> set[str]:
    return set(REGISTRY)
