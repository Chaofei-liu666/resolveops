"""Business-facing read tools exposed to the LLM.

This module is intentionally not an ERPNext API wrapper.  It defines the
business tool surface the Agent may see, then delegates implementation to a
system adapter such as ERPNextAdapter.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from .config import settings
from .erpnext import ERPNextAdapter
from .models import LogisticsLane
from .policy import allow_read_tool
from .tool_result import ToolResult

engine = create_engine(settings.database_url, pool_pre_ping=True)

ToolExecutor = Callable[[dict[str, Any], str], dict[str, Any]]

READ_TOOL_PROFILES: dict[str, set[str]] = {
    'inventory_shortage': {
        'get_order',
        'get_inventory',
        'list_alternative_warehouses',
        'get_customer_profile',
        'get_item_supply_profile',
        'get_inbound_purchase',
        'get_transfer_options',
    },
    'price_mismatch': {
        'get_order',
        'get_reference_price',
        'get_customer_profile',
    },
    'delivery_delay': {
        'get_order',
        'get_inbound_purchase',
        'get_item_supply_profile',
        'get_customer_profile',
    },
}


def read_tool_names_for_case(event_type: str | None) -> set[str]:
    return READ_TOOL_PROFILES.get(event_type or 'inventory_shortage', {'get_order'})


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    permission: str
    side_effect: str
    risk_level: str
    source_system: str
    executor: ToolExecutor | None = None
    executor_ref: str | None = None
    llm_callable: bool = True
    requires_approval: bool = False
    idempotency_required: bool = False
    resource_keys: list[str] | None = None
    verification: dict[str, Any] | None = None
    compensation: dict[str, Any] | None = None
    llm_description: str | None = None

    def to_openai_tool(self) -> dict[str, Any]:
        """Return the minimal schema visible to the LLM provider.

        Runtime governance fields stay in metadata(); they are deliberately not
        included here to keep token cost low and avoid distracting the model.
        """
        if not self.llm_callable:
            raise ValueError(f'{self.name} is not directly callable by the LLM')
        return {
            'type': 'function',
            'function': {
                'name': self.name,
                'description': self.llm_description or self.description,
                'parameters': self.parameters,
            },
        }

    def metadata(self) -> dict[str, Any]:
        """Runtime metadata for audit, console rendering, and evals."""
        return {
            'name': self.name,
            'permission': self.permission,
            'side_effect': self.side_effect,
            'risk_level': self.risk_level,
            'source_system': self.source_system,
            'description': self.description,
            'llm_callable': self.llm_callable,
            'requires_approval': self.requires_approval,
            'idempotency_required': self.idempotency_required,
            'resource_keys': self.resource_keys or [],
            'executor': self.executor_ref,
            'verification': self.verification or {},
            'compensation': self.compensation or {},
        }


class ToolRegistry:
    """Registry owns the LLM-visible schema and the internal execution map."""

    def __init__(self, specs: list[ToolSpec], enabled_names: set[str] | None = None) -> None:
        self.all_specs = {spec.name: spec for spec in specs}
        self.enabled_names = set(enabled_names or self.all_specs)
        self.specs = {name: spec for name, spec in self.all_specs.items() if name in self.enabled_names}

    def definitions(self) -> list[dict[str, Any]]:
        return [spec.to_openai_tool() for spec in self.specs.values() if spec.llm_callable]

    def metadata(self, name: str) -> dict[str, Any]:
        spec = self.all_specs.get(name)
        if not spec:
            return {'name': name, 'permission': None, 'side_effect': None, 'risk_level': None, 'source_system': None}
        result = spec.metadata()
        result['enabled_for_case'] = name in self.enabled_names
        return result

    def execute(self, name: str, arguments: dict[str, Any], order_id: str) -> dict[str, Any]:
        return self.execute_result(name, arguments, order_id).observation_result()

    def execute_result(self, name: str, arguments: dict[str, Any], order_id: str) -> ToolResult:
        spec = self.specs.get(name)
        if not spec:
            return ToolResult.failure('tool_not_registered')
        if not spec.executor:
            return ToolResult.failure('tool_not_executable_by_this_runtime', source_system=spec.source_system)
        data = spec.executor(arguments, order_id)
        return ToolResult.success(
            data,
            source_system=spec.source_system,
            verification_required=spec.verification is not None,
        )


PARTIAL_DELIVERY_FIELD_CANDIDATES = (
    'custom_allows_partial_delivery',
    'custom_allow_partial_delivery',
    'custom_\u5141\u8bb8\u62c6\u5355\u53d1\u8d27',
)


def object_schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        'type': 'object',
        'properties': properties or {},
        'required': required or [],
        'additionalProperties': False,
    }


def summarize_customer_profile(customer: dict[str, Any]) -> dict[str, Any]:
    partial_value = None
    partial_source = None
    for field in PARTIAL_DELIVERY_FIELD_CANDIDATES:
        if field in customer:
            partial_source = field
            partial_value = bool(customer.get(field))
            break

    return {
        'customer_name': customer.get('customer_name') or customer.get('name'),
        'customer_group': customer.get('customer_group'),
        'territory': customer.get('territory'),
        'is_vip': customer.get('customer_group', '').upper() == 'VIP' or bool(customer.get('custom_is_vip', False)),
        'allows_partial_delivery': partial_value,
        'evidence_fields': {
            'allows_partial_delivery': partial_source,
        },
    }


def summarize_item_supply_profile(item: dict[str, Any]) -> dict[str, Any]:
    return {
        'item_code': item.get('item_code') or item.get('name'),
        'item_name': item.get('item_name'),
        'lead_time_days': item.get('lead_time_days'),
        'minimum_order_qty': item.get('min_order_qty'),
        'safety_stock': item.get('safety_stock'),
        'is_stock_item': bool(item.get('is_stock_item')),
    }


def transfer_options(item_code: str, target: str) -> dict[str, Any]:
    allowed_sources = {w.strip() for w in settings.alternative_warehouses.split(',') if w.strip()}
    with Session(engine) as db:
        lanes = db.scalars(
            select(LogisticsLane).where(
                LogisticsLane.tenant_id == 'demo',
                LogisticsLane.active.is_(True),
                LogisticsLane.target_warehouse == target,
                LogisticsLane.source_warehouse.in_(allowed_sources),
            )
        ).all()
    return {
        'item_code': item_code,
        'target': target,
        'lanes': [
            {
                'source': lane.source_warehouse,
                'target': lane.target_warehouse,
                'transit_days': lane.transit_days,
                'cost_per_unit': lane.cost_per_unit,
                'currency': lane.currency,
            }
            for lane in lanes
        ],
    }


class BusinessReadTools:
    """LLM-facing read tool facade for business facts.

    The Agent sees stable business tools such as get_inventory.  The facade
    hides whether the fact comes from ERPNext, SAP, a WMS, CRM, or another
    enterprise system.
    """

    def __init__(self, adapter: ERPNextAdapter, event_type: str = 'inventory_shortage'):
        self.adapter = adapter
        self.event_type = event_type
        self.enabled_tool_names = read_tool_names_for_case(event_type)
        self.registry = ToolRegistry(self._specs(), self.enabled_tool_names)

    def definitions(self) -> list[dict[str, Any]]:
        return self.registry.definitions()

    def metadata(self, name: str) -> dict[str, Any]:
        return self.registry.metadata(name)

    def execute(self, name: str, arguments: dict[str, Any], order_id: str) -> dict[str, Any]:
        return self.execute_result(name, arguments, order_id).observation_result()

    def execute_result(self, name: str, arguments: dict[str, Any], order_id: str) -> ToolResult:
        try:
            if name not in self.enabled_tool_names:
                return ToolResult.failure('tool_not_enabled_for_case_type', source_system=self.metadata(name).get('source_system'))
            order = self.adapter.sales_order(order_id) if name == 'get_inventory' else None
            allowed, reason = allow_read_tool(name, arguments, order)
            if not allowed:
                return ToolResult.failure(reason, source_system=self.metadata(name).get('source_system'))
            return self.registry.execute_result(name, arguments, order_id)
        except Exception as exc:
            return ToolResult.failure(
                'tool_execution_failed',
                error_type=type(exc).__name__,
                retryable=True,
                source_system=self.metadata(name).get('source_system'),
                side_effect_committed=False,
            )

    def _specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name='get_order',
                description='Read the business order and its item lines for the current case.',
                llm_description='Read the current order.',
                parameters=object_schema(),
                permission='order:read',
                side_effect='none',
                risk_level='low',
                source_system='ERPNextAdapter',
                executor=lambda _args, order_id: self.adapter.sales_order(order_id),
            ),
            ToolSpec(
                name='get_inventory',
                description='Read current and reserved inventory for one permitted warehouse.',
                llm_description='Read inventory for a permitted warehouse.',
                parameters=object_schema(
                    {'item_code': {'type': 'string'}, 'warehouse': {'type': 'string'}},
                    ['item_code', 'warehouse'],
                ),
                permission='inventory:read',
                side_effect='none',
                risk_level='low',
                source_system='ERPNextAdapter',
                executor=lambda args, _order_id: self.adapter.stock(args['item_code'], args['warehouse']),
            ),
            ToolSpec(
                name='list_alternative_warehouses',
                description='Return warehouses permitted as transfer sources.',
                llm_description='List permitted transfer source warehouses.',
                parameters=object_schema(),
                permission='inventory:read',
                side_effect='none',
                risk_level='low',
                source_system='ResolveOpsConfig',
                executor=lambda _args, _order_id: {'warehouses': [w.strip() for w in settings.alternative_warehouses.split(',') if w.strip()]},
            ),
            ToolSpec(
                name='get_customer_profile',
                description='Read the order customer profile for delivery constraints or risk signals.',
                llm_description='Read customer delivery constraints.',
                parameters=object_schema({'customer_id': {'type': 'string'}}, ['customer_id']),
                permission='customer:read',
                side_effect='none',
                risk_level='medium',
                source_system='ERPNextAdapter',
                executor=lambda args, _order_id: summarize_customer_profile(self.adapter.customer(args['customer_id'])),
            ),
            ToolSpec(
                name='get_item_supply_profile',
                description='Read item replenishment facts such as lead time, minimum order quantity, and safety stock.',
                llm_description='Read item replenishment facts.',
                parameters=object_schema({'item_code': {'type': 'string'}}, ['item_code']),
                permission='item:read',
                side_effect='none',
                risk_level='low',
                source_system='ERPNextAdapter',
                executor=lambda args, _order_id: summarize_item_supply_profile(self.adapter.item(args['item_code'])),
            ),
            ToolSpec(
                name='get_reference_price',
                description='Read selling reference price for a SKU from the enterprise pricing source.',
                llm_description='Read reference selling price for a SKU.',
                parameters=object_schema({'item_code': {'type': 'string'}}, ['item_code']),
                permission='pricing:read',
                side_effect='none',
                risk_level='medium',
                source_system='ERPNextAdapter',
                executor=lambda args, _order_id: self.adapter.reference_price(args['item_code']),
            ),
            ToolSpec(
                name='get_inbound_purchase',
                description='Read inbound supply records for a SKU. Failed lookup means unknown, not no supply.',
                llm_description='Read inbound supply for a SKU.',
                parameters=object_schema({'item_code': {'type': 'string'}}, ['item_code']),
                permission='purchase:read',
                side_effect='none',
                risk_level='medium',
                source_system='ERPNextAdapter',
                executor=lambda args, _order_id: {'purchase_items': self.adapter.inbound_purchase_items(args['item_code'])},
            ),
            ToolSpec(
                name='get_transfer_options',
                description='Read configured transfer lanes with transit days and unit cost for a target warehouse.',
                llm_description='Read transfer lanes for a target warehouse.',
                parameters=object_schema({'item_code': {'type': 'string'}, 'target': {'type': 'string'}}, ['item_code', 'target']),
                permission='logistics_lane:read',
                side_effect='none',
                risk_level='low',
                source_system='ResolveOpsConfig',
                executor=lambda args, _order_id: transfer_options(args['item_code'], args['target']),
            ),
        ]


# Backward-compatible alias. Existing code can keep importing ERPReadTools,
# while new architecture language should refer to BusinessReadTools.
ERPReadTools = BusinessReadTools
