"""Read-only ERPNext tools used by the main chat outside a Case.

The Case Agent keeps its narrow, case-type-specific surface.  This module is
for ordinary operational questions such as “有哪些仓库” and deliberately maps
them to named ERPNext REST resources instead of arbitrary SQL or write APIs.
"""
from __future__ import annotations

from typing import Any

from .erpnext import ERPNextAdapter
from .tool_result import ToolResult, annotate_tool_result
from .tools import ToolRegistry, ToolSpec, object_schema


class ERPWorkspaceReadTools:
    """Typed, side-effect-free ERPNext reads for the main conversation."""

    def __init__(self, adapter: ERPNextAdapter) -> None:
        self.adapter = adapter
        self.registry = ToolRegistry(self._specs())

    def definitions(self) -> list[dict[str, Any]]:
        return self.registry.definitions()

    def metadata(self, name: str) -> dict[str, Any]:
        return self.registry.metadata(name)

    def execute_result(self, name: str, arguments: dict[str, Any], _context_id: str) -> ToolResult:
        failure = self.registry.validate_arguments(name, arguments)
        if failure:
            return annotate_tool_result(failure, pipeline_stage='schema_validation')
        try:
            return annotate_tool_result(self.registry.execute_result(name, arguments, ''), pipeline_stage='tool_execution')
        except Exception as exc:
            return annotate_tool_result(
                ToolResult.failure(
                    'erpnext_read_failed', error_type=type(exc).__name__, retryable=True,
                    source_system='ERPNextAdapter', side_effect_committed=False,
                ),
                pipeline_stage='tool_execution',
            )

    def _specs(self) -> list[ToolSpec]:
        limit = {'type': 'integer', 'description': 'Maximum records to return, from 1 to 100.'}
        return [
            ToolSpec(
                name='list_warehouses', description='List ERPNext warehouses and their hierarchy.',
                llm_description='List real-time ERPNext warehouses.', parameters=object_schema({'limit': limit}),
                permission='warehouse:read', side_effect='none', risk_level='low', source_system='ERPNextAdapter',
                executor=lambda args, _ctx: {'warehouses': self.adapter.warehouses(limit=args.get('limit', 100))},
            ),
            ToolSpec(
                name='list_sales_orders', description='List recent ERPNext sales orders, optionally by status.',
                llm_description='List real-time sales orders.',
                parameters=object_schema({'status': {'type': 'string'}, 'limit': limit}),
                permission='order:read', side_effect='none', risk_level='low', source_system='ERPNextAdapter',
                executor=lambda args, _ctx: {'sales_orders': self.adapter.sales_orders(status=args.get('status'), limit=args.get('limit', 100))},
            ),
            ToolSpec(
                name='get_sales_order', description='Read one ERPNext Sales Order by its exact ID.',
                llm_description='Read an ERPNext sales order.',
                parameters=object_schema({'order_id': {'type': 'string'}}, ['order_id']),
                permission='order:read', side_effect='none', risk_level='low', source_system='ERPNextAdapter',
                executor=lambda args, _ctx: self.adapter.sales_order(args['order_id']),
            ),
            ToolSpec(
                name='list_customers', description='List ERPNext customers.', llm_description='List real-time ERPNext customers.',
                parameters=object_schema({'limit': limit}), permission='customer:read', side_effect='none', risk_level='low', source_system='ERPNextAdapter',
                executor=lambda args, _ctx: {'customers': self.adapter.customers(limit=args.get('limit', 100))},
            ),
            ToolSpec(
                name='get_customer', description='Read one ERPNext customer by exact ID.', llm_description='Read an ERPNext customer.',
                parameters=object_schema({'customer_id': {'type': 'string'}}, ['customer_id']),
                permission='customer:read', side_effect='none', risk_level='low', source_system='ERPNextAdapter',
                executor=lambda args, _ctx: self.adapter.customer(args['customer_id']),
            ),
            ToolSpec(
                name='list_items', description='List ERPNext items.', llm_description='List real-time ERPNext items.',
                parameters=object_schema({'limit': limit}), permission='item:read', side_effect='none', risk_level='low', source_system='ERPNextAdapter',
                executor=lambda args, _ctx: {'items': self.adapter.items(limit=args.get('limit', 100))},
            ),
            ToolSpec(
                name='get_item', description='Read one ERPNext item by exact item code.', llm_description='Read an ERPNext item.',
                parameters=object_schema({'item_code': {'type': 'string'}}, ['item_code']),
                permission='item:read', side_effect='none', risk_level='low', source_system='ERPNextAdapter',
                executor=lambda args, _ctx: self.adapter.item(args['item_code']),
            ),
            ToolSpec(
                name='list_purchase_orders', description='List recent ERPNext purchase orders, optionally by status.',
                llm_description='List real-time ERPNext purchase orders.',
                parameters=object_schema({'status': {'type': 'string'}, 'limit': limit}),
                permission='purchase:read', side_effect='none', risk_level='low', source_system='ERPNextAdapter',
                executor=lambda args, _ctx: {'purchase_orders': self.adapter.purchase_orders(status=args.get('status'), limit=args.get('limit', 100))},
            ),
            ToolSpec(
                name='get_inventory', description='Read real-time stock for an item in one warehouse.', llm_description='Read ERPNext inventory.',
                parameters=object_schema({'item_code': {'type': 'string'}, 'warehouse': {'type': 'string'}}, ['item_code', 'warehouse']),
                permission='inventory:read', side_effect='none', risk_level='low', source_system='ERPNextAdapter',
                executor=lambda args, _ctx: self.adapter.stock(args['item_code'], args['warehouse']),
            ),
            ToolSpec(
                name='get_reference_price', description='Read ERPNext selling reference price for an item.', llm_description='Read ERPNext item price.',
                parameters=object_schema({'item_code': {'type': 'string'}}, ['item_code']),
                permission='pricing:read', side_effect='none', risk_level='low', source_system='ERPNextAdapter',
                executor=lambda args, _ctx: self.adapter.reference_price(args['item_code']),
            ),
            ToolSpec(
                name='get_inbound_purchase', description='Read open inbound purchase supply for an item.', llm_description='Read inbound purchase supply.',
                parameters=object_schema({'item_code': {'type': 'string'}}, ['item_code']),
                permission='purchase:read', side_effect='none', risk_level='low', source_system='ERPNextAdapter',
                executor=lambda args, _ctx: {'purchase_items': self.adapter.inbound_purchase_items(args['item_code'])},
            ),
        ]
