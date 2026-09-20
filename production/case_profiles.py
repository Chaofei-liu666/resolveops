"""Small domain profile registry; not a generic plugin system.

Each supported exception type owns its allowed read surface and candidate
governed actions.  Prompts remain in ``agent.py`` because they are business
policy, while this data keeps routing decisions auditable and easy to extend.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CaseProfile:
    event_type: str
    read_tools: frozenset[str]
    action_types: frozenset[str]


PROFILES: dict[str, CaseProfile] = {
    'inventory_shortage': CaseProfile('inventory_shortage', frozenset({
        'get_order', 'get_inventory', 'list_alternative_warehouses', 'get_customer_profile',
        'get_item_supply_profile', 'get_inbound_purchase', 'get_transfer_options',
    }), frozenset({'transfer_stock', 'create_purchase_request', 'draft_customer_notification', 'create_manual_ticket'})),
    'price_mismatch': CaseProfile('price_mismatch', frozenset({
        'get_order', 'get_reference_price', 'get_customer_profile',
    }), frozenset({'create_price_review_ticket', 'create_manual_ticket'})),
    'delivery_delay': CaseProfile('delivery_delay', frozenset({
        'get_order', 'get_inbound_purchase', 'get_item_supply_profile', 'get_customer_profile',
    }), frozenset({'create_supplier_followup_task', 'create_manual_ticket'})),
}


def case_profile(event_type: str | None) -> CaseProfile:
    return PROFILES.get(event_type or 'inventory_shortage', PROFILES['inventory_shortage'])
