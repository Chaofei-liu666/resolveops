"""Narrow, API-first ERPNext boundary. No browser automation or DB access."""
from __future__ import annotations
import json
from datetime import date, datetime
from typing import Any
import httpx


class ERPNextAdapter:
    def __init__(self, base_url: str, api_key: str, api_secret: str) -> None:
        self.base_url = base_url.rstrip('/')
        self.headers = {'Authorization': f'token {api_key}:{api_secret}'}

    def _get(self, path: str, params: dict | None = None) -> Any:
        response = httpx.get(self.base_url + path, headers=self.headers, params=params, timeout=15)
        response.raise_for_status()
        return response.json()['data']

    def _post_resource(self, doctype: str, payload: dict[str, Any], timeout: int = 20) -> dict:
        response = httpx.post(self.base_url + f'/api/resource/{doctype}', headers=self.headers, json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()['data']

    def _submit(self, doctype: str, name: str) -> dict:
        doc = self._get(f'/api/resource/{doctype}/{name}')
        response = httpx.post(self.base_url + '/api/method/frappe.client.submit', headers=self.headers, json={'doc': doc}, timeout=20)
        response.raise_for_status()
        return response.json().get('message') or response.json()

    def sales_order(self, name: str) -> dict:
        return self._get(f'/api/resource/Sales Order/{name}')

    def stock(self, item_code: str, warehouse: str) -> dict:
        filters = f'[["item_code","=","{item_code}"],["warehouse","=","{warehouse}"]]'
        data = self._get('/api/resource/Bin', {'filters': filters, 'fields': '["actual_qty","reserved_qty","warehouse"]'})
        return data[0] if data else {'actual_qty': 0, 'reserved_qty': 0, 'warehouse': warehouse}

    def create_transfer_draft(self, *, source: str, target: str, item_code: str, qty: float, idempotency_key: str) -> str:
        # ERPNext must contain this custom field; it is the system-of-record
        # idempotency key and is checked again during timeout recovery.
        payload = {'stock_entry_type': 'Material Transfer', 'purpose': 'Material Transfer',
                   'custom_resolveops_idempotency_key': idempotency_key,
                   'items': [{'s_warehouse': source, 't_warehouse': target, 'item_code': item_code, 'qty': qty}]}
        return self._post_resource('Stock Entry', payload)['name']

    def stock_entry(self, name: str) -> dict:
        return self._get(f'/api/resource/Stock Entry/{name}')

    def create_purchase_request(self, *, target: str, item_code: str, qty: float, required_by: str, company: str | None, idempotency_key: str) -> str:
        """Create a reversible ERPNext Material Request; never place a Purchase Order."""
        payload = {
            'material_request_type': 'Purchase',
            'schedule_date': required_by,
            'custom_resolveops_idempotency_key': idempotency_key,
            'items': [{'item_code': item_code, 'qty': qty, 'schedule_date': required_by, 'warehouse': target}],
        }
        if company:
            payload['company'] = company
        return self._post_resource('Material Request', payload)['name']

    def material_request(self, name: str) -> dict:
        return self._get(f'/api/resource/Material Request/{name}')

    def customer(self, name: str) -> dict:
        return self._get(f'/api/resource/Customer/{name}')

    def item(self, name: str) -> dict:
        return self._get(f'/api/resource/Item/{name}')

    def reference_price(self, item_code: str) -> dict:
        """Read selling reference price from ERPNext Item Price.

        This is a reference signal for price-review planning only.  It is not
        authority to mutate a Sales Order price.
        """
        filters = json.dumps([["item_code", "=", item_code], ["selling", "=", 1]], ensure_ascii=False)
        fields = json.dumps(["name", "item_code", "price_list", "price_list_rate", "currency", "valid_from", "valid_upto"])
        data = self._get('/api/resource/Item Price', {
            'filters': filters,
            'fields': fields,
            'limit_page_length': 20,
        })
        if not data:
            return {'item_code': item_code, 'reference_rate': None, 'prices': []}
        price = data[0]
        return {
            'item_code': item_code,
            'reference_rate': float(price.get('price_list_rate') or 0),
            'price_list': price.get('price_list'),
            'currency': price.get('currency'),
            'prices': data,
        }

    def inbound_purchase_items(self, item_code: str) -> list[dict]:
        """Read inbound supply through permitted Purchase Order headers.

        ERPNext often denies direct REST access to child-table doctypes even
        when the service account may read Purchase Orders.  Fetching bounded
        parent documents also gives the Agent submitted status and dates.
        """
        orders = self._get('/api/resource/Purchase Order', {
            'fields': '["name","supplier","status","docstatus","schedule_date"]',
            'limit_page_length': 20,
        })
        inbound=[]
        for header in orders:
            if header.get('docstatus') != 1 or header.get('status') in {'Closed', 'Cancelled'}:
                continue
            order=self._get(f"/api/resource/Purchase Order/{header['name']}")
            for item in order.get('items',[]):
                if item.get('item_code') != item_code:
                    continue
                remaining=float(item.get('qty',0))-float(item.get('received_qty',0))
                if remaining > 0:
                    inbound.append({
                        'purchase_order': header['name'],
                        'supplier': order.get('supplier') or header.get('supplier'),
                        'remaining_qty': remaining,
                        'schedule_date': item.get('schedule_date') or header.get('schedule_date'),
                        'status': header.get('status'),
                    })
        return inbound

    def set_stock_balance_for_fault_injection(
        self,
        *,
        item_code: str,
        warehouse: str,
        qty: float,
        company: str,
        difference_account: str,
        valuation_rate: float,
    ) -> dict:
        """Change ERPNext sandbox stock through official REST APIs.

        This helper creates and submits a Stock Reconciliation, so ERPNext
        remains the system of record for the changed business state. It should
        only be called through ResolveOps' fault-injection safety gate.
        """
        payload = {
            'purpose': 'Stock Reconciliation',
            'company': company,
            'posting_date': date.today().isoformat(),
            'posting_time': datetime.now().strftime('%H:%M:%S'),
            'difference_account': difference_account,
            'items': [{
                'item_code': item_code,
                'warehouse': warehouse,
                'qty': qty,
                'valuation_rate': valuation_rate,
            }],
        }
        created = self._post_resource('Stock Reconciliation', payload)
        submitted = self._submit('Stock Reconciliation', created['name'])
        return {
            'stock_reconciliation': created['name'],
            'submitted': True,
            'docstatus': submitted.get('docstatus') if isinstance(submitted, dict) else None,
        }


# Backward-compatible alias. The Agent tool layer should depend on
# ERPNextAdapter, while older code/tests may still import ERPNext.
ERPNext = ERPNextAdapter
