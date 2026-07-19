"""Deterministic policy boundary. Models propose; this module authorizes."""
from __future__ import annotations
from .config import settings

READ_TOOLS={'get_order','get_inventory','list_alternative_warehouses','get_customer_profile','get_inbound_purchase','get_transfer_options','get_item_supply_profile'}

def allow_read_tool(name: str, arguments: dict, order: dict | None = None) -> tuple[bool,str]:
    if name not in READ_TOOLS: return False,'tool_not_allowlisted'
    if name=='get_inventory' and order:
        allowed={w.strip() for w in settings.alternative_warehouses.split(',')}
        allowed.add(order['items'][0].get('warehouse'))
        if arguments.get('warehouse') not in allowed: return False,'warehouse_out_of_scope'
    return True,'allowed'

def action_policy(plan: dict, evidence: dict | None) -> dict:
    observations=(evidence or {}).get('observations',[])
    order=next((x['result'] for x in observations if x['tool']=='get_order'),{})
    customer=next((x['result'] for x in observations if x['tool']=='get_customer_profile'),{})
    amount=float(order.get('grand_total',0) or 0)
    customer_group=str(customer.get('customer_group') or '')
    vip=bool(customer.get('is_vip')) or customer_group.upper()=='VIP' or bool(customer.get('custom_is_vip',False))
    if plan['action_type']=='transfer_stock':
        if amount>100000 or vip:
            return {'allowed':True,'reason':'dual_approval_required','required_roles':['warehouse_manager','sales_manager'],'amount':amount,'vip':vip}
        return {'allowed':True,'reason':'approval_required','required_roles':['warehouse_manager'],'amount':amount,'vip':vip}
    if plan['action_type']=='create_purchase_request':
        roles=['procurement_manager']
        if amount>100000 or vip: roles.append('sales_manager')
        return {'allowed':True,'reason':'purchase_request_approval_required','required_roles':roles,'amount':amount,'vip':vip}
    return {'allowed':False,'reason':'no_executor_policy','required_roles':[]}
