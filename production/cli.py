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


def fmt_percent(value: Any) -> str:
    try:
        return f'{float(value) * 100:.1f}%'
    except (TypeError, ValueError):
        return 'n/a'


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
        active = queues.get('active') or {}
        history = queues.get('history') or {}
        if active or history:
            print(
                f"queues: active_queued={active.get('queued', queues.get('queued', 0))} "
                f"active_running={active.get('running', queues.get('running', 0))}"
            )
            if history:
                print(
                    f"history: done={history.get('done', 0)} "
                    f"failed_total={history.get('failed', queues.get('failed', 0))}"
                )
        else:
            print(f"queues: queued={queues.get('queued', 0)} running={queues.get('running', 0)} failed={queues.get('failed', 0)}")


def print_case_summary(case: dict[str, Any]) -> None:
    print(f"Case: {case.get('id')}")
    status = case.get('status')
    print(f"type: {case.get('event_type')}  order: {case.get('order_id')}  status: {status}  plan_version: {case.get('plan_version')}")
    actions = (case.get('plan') or {}).get('actions') or []
    if actions:
        approvals = case.get('approvals') or []
        stale_plan = status == 'manual_review' and approvals and all(
            approval.get('status') in {'invalidated', 'expired', 'revoked'}
            for approval in approvals
        )
        print('\n[Last Plan]' if stale_plan else '\n[Plan]')
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
    events = case.get('events') or []
    manual_events = [
        event for event in events
        if event.get('kind') in {'handoff', 'manual_review_required', 'replan_requested'}
    ]
    if status == 'manual_review' and manual_events:
        latest = manual_events[-1]
        print('\n[Manual Review]')
        print(f"reason: {latest.get('message')}")
        data = latest.get('data') or {}
        conclusion = data.get('conclusion') if isinstance(data, dict) else None
        if isinstance(conclusion, dict):
            missing = conclusion.get('missing_information') or []
            rationale = conclusion.get('rationale')
            if rationale:
                print(f"rationale: {rationale}")
            if missing:
                print(f"missing_information={', '.join(str(item) for item in missing)}")
    trace = case.get('tool_trace') or {}
    summary = trace.get('summary') or {}
    if summary:
        print('\n[Tool Trace]')
        read_count = summary.get('read_tool_count', summary.get('observation_count', 0))
        action_count = summary.get('action_count', len(actions))
        tools_used = summary.get('tools_used') or []
        print(f"read_tools={read_count} actions={action_count}")
        if tools_used:
            print(f"tools_used={', '.join(tools_used)}")
        grounding = summary.get('grounding_allowed')
        if grounding is not None:
            print(f"grounding_allowed={grounding}")
    action_evidence = trace.get('action_evidence') or {}
    if action_evidence:
        print('\n[Action Evidence]')
        for action_id, evidence_ids in action_evidence.items():
            evidence_text = ', '.join(evidence_ids) if evidence_ids else 'none'
            print(f"- {action_id}: {evidence_text}")
    if events:
        print('\n[Recent Events]')
        for event in events[-8:]:
            print(f"- {event.get('kind')}: {event.get('message')}")


def print_eval_summary(data: dict[str, Any], show_cases: bool = False) -> None:
    total = data.get('total_cases', 0)
    resolved = data.get('resolved_cases', 0)
    manual = data.get('manual_review_cases', 0)
    waiting = data.get('approval_waiting_cases', 0)
    writes = data.get('cases_with_writes', 0)
    verified = data.get('verified_write_cases', 0)

    print('ResolveOps Eval Summary')
    print(f"cases: total={total} resolved={resolved} manual_review={manual} waiting_approval={waiting}")
    print(
        f"rates: resolution={fmt_percent(data.get('case_resolution_rate'))} "
        f"verification={fmt_percent(data.get('verification_pass_rate'))} "
        f"tool_failure={fmt_percent(data.get('tool_failure_rate'))}"
    )
    print(
        f"tools: avg_read_calls={data.get('avg_read_tool_calls', 0):.2f} "
        f"tool_failures={data.get('tool_failures', 0)}"
    )
    print(
        f"governance: write_cases={writes} verified_write_cases={verified} "
        f"policy_denials={data.get('policy_denials', 0)}"
    )
    print(
        f"recovery: replanned_cases={data.get('replanned_cases', 0)} "
        f"manual_handoff_cases={data.get('manual_handoff_cases', 0)} "
        f"task_failures={data.get('task_failures', 0)}"
    )
    print(
        f"context: sanitized={data.get('context_isolation_sanitized_cases', 0)} "
        f"failures={data.get('context_isolation_failures', 0)} "
        f"grounding_passed={data.get('evidence_grounding_passed_cases', 0)} "
        f"grounding_failures={data.get('evidence_grounding_failures', 0)}"
    )
    if show_cases:
        cases = data.get('cases') or []
        print('\n[Cases]')
        for row in cases:
            print(
                f"- {row.get('case_id')} {row.get('event_type')} {row.get('order_id')} "
                f"status={row.get('status')} tools={row.get('tool_call_count', 0)} "
                f"writes={row.get('write_invocation_count', 0)} "
                f"replan={row.get('has_replan', False)}"
            )


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


def cmd_case_create(args: argparse.Namespace, client: ApiClient) -> int:
    payload = {
        'tenant_id': args.tenant_id,
        'event_type': args.event_type,
        'order_id': args.order_id,
        'source_event_id': args.source_event_id,
        'reason': args.reason,
    }
    data = client.request('POST', '/v1/cases', {k: v for k, v in payload.items() if v is not None})
    if args.json:
        print_json(data)
    else:
        duplicate = ' duplicate=true' if data.get('duplicate') else ''
        print(f"case created: {data.get('case_id')} status={data.get('status')}{duplicate}")
        print(f"next: python resolveops.py case show {data.get('case_id')}")
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
        'company': args.company,
        'difference_account': args.difference_account,
        'valuation_rate': args.valuation_rate,
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


def cmd_eval_summary(args: argparse.Namespace, client: ApiClient) -> int:
    data = client.request('GET', f'/v1/evals/summary?limit={args.limit}')
    if args.json:
        print_json(data)
    else:
        print_eval_summary(data, show_cases=args.cases)
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
    case_create = case_sub.add_parser('create', help='Create a Case and queue investigation')
    case_create.add_argument('--type', dest='event_type', choices=['inventory_shortage', 'price_mismatch', 'delivery_delay', 'supplier_delay'], required=True)
    case_create.add_argument('--order', dest='order_id', required=True)
    case_create.add_argument('--tenant', dest='tenant_id', default='demo')
    case_create.add_argument('--source-event-id')
    case_create.add_argument('--reason')
    case_create.set_defaults(handler=cmd_case_create)
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
    fi_run.add_argument('--company')
    fi_run.add_argument('--difference-account')
    fi_run.add_argument('--valuation-rate', type=float)
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

    eval_parser = sub.add_parser('eval', help='Evaluation and reliability summaries')
    eval_sub = eval_parser.add_subparsers(dest='eval_command', required=True)
    eval_summary = eval_sub.add_parser('summary', help='Show aggregate Agent execution quality metrics')
    eval_summary.add_argument('--limit', type=int, default=50, help='Number of recent cases to evaluate')
    eval_summary.add_argument('--cases', action='store_true', help='Include per-case rows')
    eval_summary.set_defaults(handler=cmd_eval_summary)
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
