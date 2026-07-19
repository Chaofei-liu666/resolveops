"""ResolveOps developer CLI.

The CLI is a presentation layer only.  It never talks to ERPNext directly and
does not execute Agent logic.  Every command calls ResolveOps HTTP APIs so the
server-side policy, audit and environment gates remain authoritative.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx


DEFAULT_BASE_URL = 'http://localhost:8090'


class CliError(RuntimeError):
    pass


class ApiClient:
    def __init__(self, base_url: str, operator_key: str | None) -> None:
        self.base_url = base_url.rstrip('/')
        self.operator_key = operator_key

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        headers = {}
        if self.operator_key:
            headers['X-Operator-Key'] = self.operator_key
        response = httpx.request(
            method,
            self.base_url + path,
            headers=headers,
            json=payload,
            timeout=30,
        )
        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise CliError(f'{method} {path} failed: HTTP {response.status_code} {detail}')
        if not response.content:
            return None
        return response.json()


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def print_status(data: dict[str, Any]) -> None:
    print('ResolveOps Runtime Status')
    print(f"status: {data.get('status')}")
    checks = data.get('checks') or {}
    for name, check in checks.items():
        if isinstance(check, dict):
            print(f"- {name}: {'ok' if check.get('ok') else 'degraded'}")
            errors = check.get('errors') or []
            warnings = check.get('warnings') or []
            for error in errors:
                print(f"  error: {error}")
            for warning in warnings:
                print(f"  warning: {warning}")
    queues = data.get('queues') or {}
    if queues:
        print(f"queues: queued={queues.get('queued', 0)} running={queues.get('running', 0)} failed={queues.get('failed', 0)}")


def print_case_summary(case: dict[str, Any]) -> None:
    print(f"Case: {case.get('id')}")
    print(f"type: {case.get('event_type')}  order: {case.get('order_id')}  status: {case.get('status')}  plan_version: {case.get('plan_version')}")
    actions = (case.get('plan') or {}).get('actions') or []
    if actions:
        print('\n[Plan]')
        for idx, action in enumerate(actions, 1):
            action_type = action.get('action_type')
            rationale = action.get('rationale') or action.get('reason') or ''
            print(f"{idx}. {action_type}")
            if rationale:
                print(f"   reason: {rationale}")
    approvals = case.get('approvals') or []
    if approvals:
        print('\n[Approvals]')
        for approval in approvals:
            action = approval.get('action') or {}
            print(f"- {approval.get('id')} status={approval.get('status')} action={action.get('action_type')} required={approval.get('required_roles')}")
    trace = case.get('tool_trace') or {}
    summary = trace.get('summary') or {}
    if summary:
        print('\n[Tool Trace]')
        print(f"read_tools={summary.get('read_tool_count', 0)} actions={summary.get('action_count', 0)}")
    events = case.get('events') or []
    if events:
        print('\n[Recent Events]')
        for event in events[-8:]:
            print(f"- {event.get('kind')}: {event.get('message')}")


def cmd_status(args: argparse.Namespace, client: ApiClient) -> int:
    data = client.request('GET', '/v1/runtime/status')
    if args.json:
        print_json(data)
    else:
        print_status(data)
    return 0


def cmd_case_list(args: argparse.Namespace, client: ApiClient) -> int:
    data = client.request('GET', f'/v1/cases?limit={args.limit}')
    if args.json:
        print_json(data)
    else:
        print('Cases')
        for case in data:
            print(f"- {case.get('id')}  {case.get('event_type')}  {case.get('order_id')}  {case.get('status')}")
    return 0


def cmd_case_show(args: argparse.Namespace, client: ApiClient) -> int:
    data = client.request('GET', f'/v1/cases/{args.case_id}')
    if args.json:
        print_json(data)
    else:
        print_case_summary(data)
    return 0


def cmd_fi_list(args: argparse.Namespace, client: ApiClient) -> int:
    data = client.request('GET', '/v1/fault-injections')
    if args.json:
        print_json(data)
    else:
        print(f"Fault injection enabled: {data.get('enabled')}  env: {data.get('app_env')}")
        for fault in data.get('faults') or []:
            print(f"- {fault.get('fault_type')}: {fault.get('description')}")
    return 0


def cmd_fi_run(args: argparse.Namespace, client: ApiClient) -> int:
    payload = {
        'fault_type': args.fault_type,
        'case_id': args.case_id,
        'item_code': args.item_code,
        'warehouse': args.warehouse,
        'new_qty': args.new_qty,
        'reason': args.reason,
    }
    data = client.request('POST', '/v1/fault-injections/run', {k: v for k, v in payload.items() if v is not None})
    if args.json:
        print_json(data)
    else:
        print('Fault injection applied')
        print(f"type: {data.get('fault_type')}")
        print(f"item: {data.get('item_code')}")
        print(f"warehouse: {data.get('warehouse')}")
        print(f"new_qty: {data.get('new_qty')}")
        erp = data.get('erpnext_result') or {}
        if erp:
            print(f"erpnext_stock_reconciliation: {erp.get('stock_reconciliation')}")
    return 0


def cmd_approval_approve(args: argparse.Namespace, client: ApiClient) -> int:
    data = client.request('POST', f'/v1/approvals/{args.approval_id}/approve')
    print_json(data) if args.json else print(f"approval {args.approval_id}: {data.get('status')}")
    return 0


def cmd_approval_revoke(args: argparse.Namespace, client: ApiClient) -> int:
    payload = {'reason': args.reason} if args.reason else {}
    data = client.request('POST', f'/v1/approvals/{args.approval_id}/revoke', payload)
    print_json(data) if args.json else print(f"approval {args.approval_id}: {data.get('status')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='resolveops', description='ResolveOps API-first Agent CLI')
    parser.add_argument('--base-url', default=os.getenv('RESOLVEOPS_API_URL', DEFAULT_BASE_URL), help='ResolveOps API base URL')
    parser.add_argument('--operator-key', default=os.getenv('RESOLVEOPS_OPERATOR_KEY') or os.getenv('OPERATOR_API_KEY'), help='Operator API key')
    parser.add_argument('--json', action='store_true', help='Print raw JSON response')
    sub = parser.add_subparsers(dest='command', required=True)

    status = sub.add_parser('status', help='Show runtime status')
    status.set_defaults(handler=cmd_status)

    case = sub.add_parser('case', help='Case commands')
    case_sub = case.add_subparsers(dest='case_command', required=True)
    case_list = case_sub.add_parser('list', help='List cases')
    case_list.add_argument('--limit', type=int, default=20)
    case_list.set_defaults(handler=cmd_case_list)
    case_show = case_sub.add_parser('show', help='Show one case')
    case_show.add_argument('case_id')
    case_show.set_defaults(handler=cmd_case_show)

    fi = sub.add_parser('fi', help='Fault injection commands')
    fi_sub = fi.add_subparsers(dest='fi_command', required=True)
    fi_list = fi_sub.add_parser('list', help='List available fault injections')
    fi_list.set_defaults(handler=cmd_fi_list)
    fi_run = fi_sub.add_parser('run', help='Run a fault injection through ResolveOps API')
    fi_run.add_argument('fault_type', choices=['inventory_changed_before_execution'])
    fi_run.add_argument('--case', dest='case_id')
    fi_run.add_argument('--item', dest='item_code', required=True)
    fi_run.add_argument('--warehouse', required=True)
    fi_run.add_argument('--new-qty', type=float, required=True)
    fi_run.add_argument('--reason')
    fi_run.set_defaults(handler=cmd_fi_run)

    approval = sub.add_parser('approval', help='Approval commands')
    approval_sub = approval.add_subparsers(dest='approval_command', required=True)
    approval_approve = approval_sub.add_parser('approve', help='Approve an approval request')
    approval_approve.add_argument('approval_id')
    approval_approve.set_defaults(handler=cmd_approval_approve)
    approval_revoke = approval_sub.add_parser('revoke', help='Revoke an approval request')
    approval_revoke.add_argument('approval_id')
    approval_revoke.add_argument('--reason')
    approval_revoke.set_defaults(handler=cmd_approval_revoke)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = ApiClient(args.base_url, args.operator_key)
    try:
        return args.handler(args, client)
    except CliError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
