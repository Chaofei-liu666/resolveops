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
import time
from pathlib import Path
from typing import Any

import httpx


DEFAULT_BASE_URL = 'http://localhost:8090'
CONFIG_DIR_NAME = '.resolveops'
CONFIG_FILE_NAME = 'config.json'
TERMINAL_CASE_STATUSES = {'waiting_approval', 'manual_review', 'resolved'}
ANSI = {
    'dim': '\033[2m',
    'reset': '\033[0m',
    'red': '\033[31m',
    'green': '\033[32m',
    'yellow': '\033[33m',
    'blue': '\033[34m',
    'magenta': '\033[35m',
    'cyan': '\033[36m',
    'white': '\033[37m',
}


class CliError(RuntimeError):
    pass


def config_dir() -> Path:
    override = os.getenv('RESOLVEOPS_CONFIG_HOME')
    return Path(override).expanduser() if override else Path.home() / CONFIG_DIR_NAME


def config_path() -> Path:
    return config_dir() / CONFIG_FILE_NAME


def load_cli_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        raise CliError(f'failed to read CLI config {path}: {exc}') from exc
    if not isinstance(data, dict):
        raise CliError(f'CLI config must be a JSON object: {path}')
    return data


def load_or_create_cli_config() -> tuple[dict[str, Any], bool]:
    path = config_path()
    if path.exists():
        return load_cli_config(), False
    data = {
        'api_url': DEFAULT_BASE_URL,
        'operator_key': '',
    }
    save_cli_config(data)
    return data, True


def save_cli_config(data: dict[str, Any]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return path


def masked(value: str | None) -> str:
    if not value:
        return ''
    if len(value) <= 8:
        return '*' * len(value)
    return value[:4] + '*' * (len(value) - 8) + value[-4:]


def config_edit_hint(created: bool = False) -> str:
    prefix = f'Created config template: {config_path()}\n' if created else ''
    return (
        f'{prefix}'
        f'Edit CLI config: {config_path()}\n'
        'Required fields: api_url, operator_key\n'
        'Or run: python resolveops.py config set operator_key <your-operator-key>'
    )


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
            hint = ''
            if response.status_code in {401, 403}:
                hint = '\n' + config_edit_hint()
            raise CliError(f'{method} {path} failed: HTTP {response.status_code} {detail}{hint}')
        if not response.content:
            return None
        return response.json()


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def supports_color() -> bool:
    return not os.getenv('NO_COLOR') and sys.stdout.isatty()


def paint(text: str, color: str) -> str:
    if not supports_color():
        return text
    return f"{ANSI.get(color, '')}{text}{ANSI['reset']}"


def compact_json(value: Any, max_len: int = 180) -> str:
    if value is None:
        return ''
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return text if len(text) <= max_len else text[: max_len - 3] + '...'


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
    agent_decision = case.get('agent_decision') or {}
    decision_trace = agent_decision.get('decision_trace') or []
    rejected_actions = agent_decision.get('rejected_actions') or []
    missing_information = agent_decision.get('missing_information') or []
    if decision_trace or rejected_actions or missing_information:
        print('\n[Agent Decision Trace]')
        for idx, item in enumerate(decision_trace, 1):
            print(f"{idx}. {item}")
        if rejected_actions:
            print('rejected_actions:')
            for item in rejected_actions:
                if isinstance(item, dict):
                    action_type = item.get('action_type') or 'unknown'
                    reason = item.get('reason') or ''
                    print(f"- {action_type}: {reason}")
                else:
                    print(f"- {item}")
        if missing_information:
            print('missing_information:')
            for item in missing_information:
                print(f"- {item}")
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


def event_style(kind: str) -> tuple[str, str]:
    if kind in {'case_created', 'context_built'}:
        return '[Case]', 'white'
    if kind in {'tool_scheduled', 'tool_observation', 'case_question_tool_observation'}:
        return '[Tool]', 'cyan'
    if kind in {'agent_decision_trace', 'agent_plan_created', 'evidence_grounding_passed'}:
        return '[Agent]', 'magenta'
    if kind in {'approval_requested', 'approval_partial', 'approval_granted'}:
        return '[Approval]', 'yellow'
    if kind in {'execution_started'}:
        return '[Executor]', 'blue'
    if kind in {'verification_passed', 'lessons_recorded'}:
        return '[Verify]', 'green'
    if kind in {'handoff', 'manual_review_required', 'worker_failure', 'policy_denied', 'evidence_grounding_failed', 'verification_failed', 'approval_expired', 'approval_revoked'}:
        return '[Stop]', 'red'
    if kind in {'replan_requested', 'task_requeued'}:
        return '[Replan]', 'yellow'
    if kind in {'case_question_asked', 'case_question_answered'}:
        return '[Ask]', 'green'
    return '[Event]', 'white'


def format_event(event: dict[str, Any]) -> str:
    kind = event.get('kind') or 'event'
    label, color = event_style(kind)
    data = event.get('data') or {}
    message = event.get('message') or ''
    created = event.get('created_at') or ''
    head = f"{paint(label, color)} {paint(kind, 'dim')} {message}"

    if kind in {'tool_scheduled', 'tool_observation', 'case_question_tool_observation'}:
        tool = data.get('tool')
        if not tool and 'Agent called read tool:' in message:
            tool = message.split('Agent called read tool:', 1)[1].strip().rstrip('.')
        if not tool and 'Case question called read tool:' in message:
            tool = message.split('Case question called read tool:', 1)[1].strip().rstrip('.')
        status = data.get('status') or ((data.get('tool_result') or {}).get('status') if isinstance(data.get('tool_result'), dict) else None)
        scheduler = data.get('scheduler') or ((data.get('tool_result') or {}).get('scheduler') if isinstance(data.get('tool_result'), dict) else {})
        result = data.get('result')
        suffix = f" tool={tool}"
        if status:
            suffix += f" status={status}"
        if scheduler:
            suffix += f" scheduler={compact_json(scheduler, 80)}"
        if result:
            suffix += f" result={compact_json(result, 180)}"
        return f"{head}{suffix}"

    if kind == 'agent_decision_trace':
        trace = data.get('decision_trace') or []
        rejected = data.get('rejected_actions') or []
        missing = data.get('missing_information') or []
        details = []
        if trace:
            details.append(f"decisions={compact_json(trace, 220)}")
        if rejected:
            details.append(f"rejected={compact_json(rejected, 180)}")
        if missing:
            details.append(f"missing={compact_json(missing, 160)}")
        return f"{head} {' '.join(details)}".rstrip()

    if kind in {'agent_plan_created', 'approval_requested', 'approval_partial', 'approval_granted'}:
        return f"{head} {compact_json(data, 240)}".rstrip()

    if kind in {'replan_requested', 'handoff', 'manual_review_required', 'worker_failure', 'verification_failed'}:
        return f"{head} {compact_json(data, 240)}".rstrip()

    return f"{head} {paint(created, 'dim')}".rstrip()


def print_case_watch_header(case_id: str) -> None:
    print(paint('ResolveOps live Case trace', 'green'))
    print(f"case: {case_id}")
    print(paint('Press Ctrl+C to stop watching.', 'dim'))


def print_case_answer(data: dict[str, Any], *, verbose: bool = False, show_case: bool = True, show_question: bool = True) -> None:
    if show_case:
        print(paint(
            f"case: {data.get('case_id')} type={data.get('event_type')} order={data.get('order_id')} status={data.get('status')}",
            'dim',
        ))
    if show_question:
        print(f"\n{paint('[You]', 'yellow')} {data.get('question')}")
    observations = data.get('observations') or []
    if observations:
        print()
        for obs in observations:
            tool = obs.get('tool')
            source = ((obs.get('scheduler') or {}).get('source')) or 'unknown'
            result = obs.get('result') or {}
            if isinstance(result, dict) and result.get('error'):
                summary = f"error={result.get('error')}"
            else:
                summary = ', '.join(str(key) for key in list(result.keys())[:5]) if isinstance(result, dict) else type(result).__name__
            print(f"{paint('[Tool]', 'cyan')} {tool} source={source} result={summary}")
    print(f"\n{paint('[Answer]', 'blue')}")
    print(data.get('answer') or '')
    if verbose:
        rationale = data.get('rationale')
        if rationale:
            print(f"\n{paint('[Rationale]', 'dim')}")
            print(rationale)
        evidence = data.get('used_evidence') or []
        if evidence:
            print(f"\n{paint('[Used Evidence]', 'dim')}")
            for item in evidence:
                print(f"- {item}")
        next_steps = data.get('safe_next_steps') or []
        if next_steps:
            print(f"\n{paint('[Safe Next Steps]', 'dim')}")
            for step in next_steps:
                print(f"- {step}")


def print_case_chat_help() -> None:
    print('Commands:')
    print('  /show        show current Case summary')
    print('  /events      show recent Case event trace')
    print('  /help        show this help')
    print('  /exit        leave the Case chat')
    print('Any other input is sent as a Case-scoped read-only Agent question.')
    print('General no-tool chat is allowed; business tools stay scoped to Case questions.')


def print_operator_chat_help() -> None:
    print('Commands:')
    print('  /status          show ResolveOps runtime status')
    print('  /cases           list recent Cases')
    print('  /new             create a new Case interactively')
    print('  /case <case-id>  enter one Case-scoped Agent chat')
    print('  /help            show this help')
    print('  /exit            leave ResolveOps chat')
    print('Free text is handled as a no-tool operator-level chat with short-term context.')
    print('Case-specific questions require /case <case-id> so business context stays isolated.')


def looks_like_case_creation(text: str) -> bool:
    normalized = text.lower()
    markers = (
        'create case', 'new case', 'open case',
        '创建case', '创建 case', '新建case', '新建 case',
        '库存不足', '价格异常', '价格不一致', '延期', '供应商延期',
    )
    return any(marker in normalized for marker in markers)


def prompt_required(label: str, *, default: str | None = None) -> str:
    suffix = f' [{default}]' if default else ''
    while True:
        value = input(f'{label}{suffix}: ').strip()
        if value:
            return value
        if default is not None:
            return default
        print(paint('This field is required.', 'red'))


def choose_event_type() -> str:
    options = {
        '1': 'inventory_shortage',
        '2': 'price_mismatch',
        '3': 'delivery_delay',
        '4': 'supplier_delay',
    }
    print('Case type:')
    print('  1. inventory_shortage  库存不足')
    print('  2. price_mismatch      价格不一致')
    print('  3. delivery_delay      交付延期')
    print('  4. supplier_delay      供应商延期')
    while True:
        value = input('Choose type [1]: ').strip() or '1'
        if value in options:
            return options[value]
        if value in options.values():
            return value
        print(paint('Invalid Case type. Use 1-4 or the event_type name.', 'red'))


def create_case_interactively(client: ApiClient) -> dict[str, Any]:
    print(paint('[New Case]', 'green'))
    event_type = choose_event_type()
    order_id = prompt_required('Order ID')
    reason = prompt_required('Reason', default='created from ResolveOps chat')
    payload = {
        'tenant_id': 'demo',
        'event_type': event_type,
        'order_id': order_id,
        'reason': reason,
    }
    data = client.request('POST', '/v1/cases', payload)
    duplicate = ' duplicate=true' if data.get('duplicate') else ''
    print(f"case created: {data.get('case_id')} status={data.get('status')}{duplicate}")
    print(f"next: /case {data.get('case_id')}")
    return data


def print_recent_case_events(case: dict[str, Any], limit: int = 12) -> None:
    events = case.get('events') or []
    if not events:
        print('No events recorded for this Case.')
        return
    for event in events[-limit:]:
        print(format_event(event))


def print_eval_summary(data: dict[str, Any], show_cases: bool = False) -> None:
    total = data.get('total_cases', 0)
    resolved = data.get('resolved_cases', 0)
    manual = data.get('manual_review_cases', 0)
    waiting = data.get('approval_waiting_cases', 0)

    print('ResolveOps Eval Summary')
    print(f"cases: total={total} resolved={resolved} manual_review={manual} waiting_approval={waiting}")
    print(
        f"core: task_success={fmt_percent(data.get('task_success_rate'))} "
        f"resolution={fmt_percent(data.get('case_resolution_rate'))} "
        f"tool_selection={fmt_percent(data.get('tool_selection_accuracy'))} "
        f"evidence_faithfulness={fmt_percent(data.get('evidence_faithfulness_rate'))} "
        f"verification={fmt_percent(data.get('verification_pass_rate'))}"
    )
    replan_success = data.get('replan_success_rate')
    replan_success_text = 'n/a' if replan_success is None else fmt_percent(replan_success)
    avg_duration = data.get('avg_duration_seconds')
    duration_text = 'n/a' if not isinstance(avg_duration, (int, float)) else f'{avg_duration:.2f}s'
    print(
        f"efficiency: avg_read_calls={data.get('avg_read_tool_calls', 0):.2f} "
        f"avg_duration={duration_text} replan_success={replan_success_text}"
    )
    print(
        f"diagnostics: grounding_failures={data.get('evidence_grounding_failures', 0)} "
        f"policy_denials={data.get('policy_denials', 0)} "
        f"context_failures={data.get('context_isolation_failures', 0)} "
        f"task_failures={data.get('task_failures', 0)}"
    )
    if show_cases:
        cases = data.get('cases') or []
        print('\n[Cases]')
        for row in cases:
            print(
                f"- {row.get('case_id')} {row.get('event_type')} {row.get('order_id')} "
                f"status={row.get('status')} success={row.get('task_succeeded')} "
                f"tools={row.get('tool_call_count', 0)} "
                f"faithfulness={fmt_percent(row.get('evidence_faithfulness'))} "
                f"replan={row.get('has_replan', False)}"
            )

def compact_stage_sequence(sequence: list[Any]) -> list[str]:
    milestones = []
    noisy = {'tool_scheduled', 'tool_observation'}
    for item in sequence:
        kind = str(item)
        if kind in noisy:
            continue
        if not milestones or milestones[-1] != kind:
            milestones.append(kind)
    return milestones


def print_eval_case(data: dict[str, Any], show_events: bool = False) -> None:
    trace = data.get('tool_trace_summary') or {}
    scheduler_sources = data.get('tool_scheduler_sources') or {}
    write_count = data.get('write_invocation_count', 0)
    if write_count:
        verification_status = 'passed' if data.get('verification_complete') else 'failed_or_incomplete'
    else:
        verification_status = 'not_applicable_no_write'
    print('ResolveOps Case Eval')
    print(
        f"case: {data.get('case_id')} type={data.get('event_type')} "
        f"order={data.get('order_id')} status={data.get('status')} "
        f"plan_version={data.get('plan_version')}"
    )
    print(
        f"outcome: task_succeeded={data.get('task_succeeded')} "
        f"resolved={data.get('resolved')} manual_review={data.get('manual_review')} "
        f"verification={verification_status}"
    )
    print(
        f"tools: observed={data.get('tool_call_count', 0)} "
        f"scheduled={data.get('scheduled_tool_call_count', 0)} "
        f"failed={data.get('tool_failure_count', 0)} "
        f"selection_accuracy={fmt_percent(data.get('tool_selection_accuracy'))} "
        f"unique={', '.join(trace.get('tools_used') or []) or 'none'}"
    )
    duration = data.get('duration_seconds')
    duration_text = 'n/a' if not isinstance(duration, (int, float)) else f'{duration:.2f}s'
    print(
        f"agent_quality: evidence_faithfulness={fmt_percent(data.get('evidence_faithfulness'))} "
        f"replan_success={data.get('replan_success')} duration={duration_text}"
    )
    if scheduler_sources:
        sources = ', '.join(f'{key}={value}' for key, value in sorted(scheduler_sources.items()))
        print(f"tool_scheduler_sources: {sources}")
    print(
        f"policy: approvals={data.get('approval_count', 0)} "
        f"pending={data.get('pending_approval_count', 0)} "
        f"expired={data.get('expired_approval_count', 0)} "
        f"revoked={data.get('revoked_approval_count', 0)} "
        f"policy_denial={data.get('has_policy_denial')}"
    )
    print(
        f"writes: invocations={write_count} "
        f"verification_passes={data.get('verification_pass_count', 0)} "
        f"verification_failures={data.get('verification_failed_count', 0)}"
    )
    print(
        f"recovery: replanned={data.get('has_replan')} "
        f"recovery_events={data.get('recovery_event_count', 0)} "
        f"blocked_events={data.get('blocked_event_count', 0)} "
        f"manual_handoff={data.get('has_manual_handoff')}"
    )
    print(
        f"context: isolation_sanitized={data.get('has_context_isolation_sanitized')} "
        f"isolation_failed={data.get('has_context_isolation_failure')} "
        f"grounding_passed={data.get('has_evidence_grounding_passed')} "
        f"grounding_failed={data.get('has_evidence_grounding_failure')}"
    )
    sequence = [str(item) for item in (data.get('stage_sequence') or [])]
    if sequence:
        label = 'Full Stage Sequence' if show_events else 'Key Stage Sequence'
        visible_sequence = sequence if show_events else compact_stage_sequence(sequence)
        print(f'\n[{label}]')
        print(' -> '.join(visible_sequence))
        if not show_events and len(visible_sequence) != len(sequence):
            print(f"(tool events hidden: {len(sequence) - len(visible_sequence)}; use --events for full sequence)")


def cmd_status(args: argparse.Namespace, client: ApiClient) -> int:
    data = client.request('GET', '/v1/runtime/status')
    if args.json:
        print_json(data)
    else:
        print_status(data)
    return 0


def cmd_init(args: argparse.Namespace, client: ApiClient | None = None) -> int:
    path = config_path()
    created_dir = False
    created_file = False
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        created_dir = True
    if not path.exists():
        save_cli_config({
            'api_url': DEFAULT_BASE_URL,
            'operator_key': '',
        })
        created_file = True

    print(paint('ResolveOps CLI', 'green'))
    print('API-first Agent for enterprise order exception handling.')
    print()
    print(paint('[Init]', 'blue') + ' Checking local CLI config...')
    dir_status = 'created' if created_dir else 'exists'
    file_status = 'created' if created_file else 'exists'
    print(f"{paint('[OK]', 'green')} Config directory {dir_status}: {path.parent}")
    print(f"{paint('[OK]', 'green')} Config file {file_status}: {path}")
    print()
    if created_file:
        print(paint('[Config]', 'yellow') + ' Set your ResolveOps API endpoint and operator key:')
        print(f"python resolveops.py config set api_url {DEFAULT_BASE_URL}")
        print('python resolveops.py config set operator_key <your-operator-key>')
        print()
    else:
        try:
            current = load_cli_config()
            api_url = current.get('api_url') or DEFAULT_BASE_URL
            key_state = 'set' if current.get('operator_key') else 'missing'
            key_color = 'green' if key_state == 'set' else 'yellow'
            print(paint('[Config]', 'green') + f' Loaded api_url={api_url} operator_key={paint(key_state, key_color)}')
            if key_state == 'missing':
                print('Set it once with:')
                print('python resolveops.py config set operator_key <your-operator-key>')
                print()
        except CliError as exc:
            print(paint('[Config]', 'red') + f' Invalid config: {exc}')
            print(f'Edit: {path}')
            print()
    print(paint('[Next]', 'cyan') + ' Start chat:')
    print('python resolveops.py chat')
    print()
    print(paint('[Note]', 'dim') + ' Case commands still require an explicit <case-id> to preserve Case context isolation.')
    return 0


def cmd_config_show(args: argparse.Namespace, client: ApiClient | None = None) -> int:
    data = load_cli_config()
    visible = dict(data)
    if 'operator_key' in visible:
        visible['operator_key'] = masked(str(visible.get('operator_key') or ''))
    print(f"config_file: {config_path()}")
    print_json(visible)
    return 0


def cmd_config_set(args: argparse.Namespace, client: ApiClient | None = None) -> int:
    data = load_cli_config()
    if args.key == 'api_url':
        data['api_url'] = args.value.rstrip('/')
    elif args.key == 'operator_key':
        data['operator_key'] = args.value.strip()
    else:
        raise CliError(f'unsupported config key: {args.key}')
    path = save_cli_config(data)
    display_value = masked(data['operator_key']) if args.key == 'operator_key' else data[args.key]
    print(f"updated {args.key}: {display_value}")
    print(f"config_file: {path}")
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


def cmd_case_ask(args: argparse.Namespace, client: ApiClient) -> int:
    question = ' '.join(args.question) if isinstance(args.question, list) else args.question
    data = client.request('POST', f'/v1/cases/{args.case_id}/ask', {'question': question})
    if args.json:
        print_json(data)
    else:
        print_case_answer(data, verbose=args.verbose)
    return 0


def cmd_case_watch(args: argparse.Namespace, client: ApiClient) -> int:
    print_case_watch_header(args.case_id)
    seen: set[str] = set()
    start = time.monotonic()
    last_status = None
    try:
        while True:
            data = client.request('GET', f'/v1/cases/{args.case_id}')
            status = data.get('status')
            if status != last_status:
                print(f"{paint('status', 'green')}: {status}")
                last_status = status
            for event in data.get('events') or []:
                event_id = str(event.get('id') or f"{event.get('kind')}:{event.get('created_at')}")
                if event_id in seen:
                    continue
                seen.add(event_id)
                print(format_event(event))
            if not args.follow and status in TERMINAL_CASE_STATUSES:
                break
            if args.timeout and time.monotonic() - start >= args.timeout:
                print(paint('watch timeout reached', 'yellow'))
                break
            time.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:
        print()
        print(paint('watch stopped', 'yellow'))
    return 0


def cmd_case_chat(args: argparse.Namespace, client: ApiClient) -> int:
    case = client.request('GET', f'/v1/cases/{args.case_id}')
    print(paint('ResolveOps Case Chat', 'green'))
    print(f"case: {case.get('id')} type={case.get('event_type')} order={case.get('order_id')} status={case.get('status')}")
    print_case_chat_help()
    prompt = f"{paint('[You]', 'yellow')} resolveops {str(args.case_id)[:8]}> "
    while True:
        try:
            question = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print(paint('chat stopped', 'yellow'))
            return 0
        if not question:
            continue
        lowered = question.lower()
        if lowered in {'/exit', '/quit', 'exit', 'quit'}:
            return 0
        if lowered in {'/help', 'help', '?'}:
            print_case_chat_help()
            continue
        if lowered == '/show':
            case = client.request('GET', f'/v1/cases/{args.case_id}')
            print_case_summary(case)
            continue
        if lowered == '/events':
            case = client.request('GET', f'/v1/cases/{args.case_id}')
            print_recent_case_events(case, limit=args.events)
            continue
        try:
            data = client.request('POST', f'/v1/cases/{args.case_id}/ask', {'question': question})
        except CliError as exc:
            print(paint(f"error: {exc}", 'red'))
            continue
        print_case_answer(data, verbose=args.verbose, show_case=False, show_question=False)


def cmd_chat(args: argparse.Namespace, client: ApiClient) -> int:
    print(paint('ResolveOps Chat', 'green'))
    print('Operator-level LLM chat. No ERP tools are called here.')
    print_operator_chat_help()
    prompt = f"{paint('[You]', 'yellow')} resolveops> "
    history: list[dict[str, str]] = []
    max_history_items = 12
    while True:
        try:
            text = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print(paint('chat stopped', 'yellow'))
            return 0
        if not text:
            continue
        lowered = text.lower()
        if lowered in {'/exit', '/quit', 'exit', 'quit'}:
            return 0
        if lowered in {'/help', 'help', '?'}:
            print_operator_chat_help()
            continue
        if lowered == '/status':
            try:
                print_status(client.request('GET', '/v1/runtime/status'))
            except CliError as exc:
                print(paint(f"error: {exc}", 'red'))
            continue
        if lowered == '/cases':
            try:
                data = client.request('GET', f'/v1/cases?limit={args.limit}')
                print('Cases')
                for case in data:
                    print(f"- {case.get('id')}  {case.get('event_type')}  {case.get('order_id')}  {case.get('status')}")
            except CliError as exc:
                print(paint(f"error: {exc}", 'red'))
            continue
        if lowered == '/new':
            try:
                create_case_interactively(client)
            except CliError as exc:
                print(paint(f"error: {exc}", 'red'))
            continue
        if lowered.startswith('/case '):
            case_id = text.split(maxsplit=1)[1].strip()
            nested_args = argparse.Namespace(case_id=case_id, events=args.events, verbose=args.verbose)
            return cmd_case_chat(nested_args, client)
        try:
            data = client.request('POST', '/v1/chat', {'question': text, 'history': history[-max_history_items:]})
        except CliError as exc:
            print(paint(f"error: {exc}", 'red'))
            continue
        source = data.get('source')
        suffix = f" {paint(f'({source})', 'dim')}" if source else ''
        answer_text = data.get('answer') or ''
        print(f"\n{paint('[Answer]', 'blue')}{suffix}")
        print(answer_text)
        history.append({'role': 'user', 'content': text})
        history.append({'role': 'assistant', 'content': answer_text})
        history = history[-max_history_items:]


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


def cmd_eval_case(args: argparse.Namespace, client: ApiClient) -> int:
    data = client.request('GET', f'/v1/evals/cases/{args.case_id}')
    if args.json:
        print_json(data)
    else:
        print_eval_case(data, show_events=args.events)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='resolveops', description='ResolveOps API-first Agent CLI')
    parser.add_argument('--base-url', default=None, help='ResolveOps API base URL')
    parser.add_argument('--operator-key', default=None, help='Operator API key')
    parser.add_argument('--json', action='store_true', help='Print raw JSON response')
    sub = parser.add_subparsers(dest='command', required=True)

    init = sub.add_parser('init', help='Create local CLI config template')
    init.set_defaults(handler=cmd_init)

    config = sub.add_parser('config', help='Local CLI config commands')
    config_sub = config.add_subparsers(dest='config_command', required=True)
    config_show = config_sub.add_parser('show', help='Show local CLI config with secrets masked')
    config_show.set_defaults(handler=cmd_config_show)
    config_set = config_sub.add_parser('set', help='Set a local CLI config value')
    config_set.add_argument('key', choices=['api_url', 'operator_key'])
    config_set.add_argument('value')
    config_set.set_defaults(handler=cmd_config_set)

    status = sub.add_parser('status', help='Show runtime status')
    status.set_defaults(handler=cmd_status)

    chat = sub.add_parser('chat', help='Open the ResolveOps operator chat')
    chat.add_argument('--limit', type=int, default=10, help='Number of cases shown by /cases')
    chat.add_argument('--events', type=int, default=12, help='Number of recent events shown after entering /case')
    chat.add_argument('--verbose', action='store_true', help='Show rationale, used evidence and safe next steps in Case chat')
    chat.set_defaults(handler=cmd_chat)

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
    case_ask = case_sub.add_parser('ask', help='Ask a read-only Agent question about one Case')
    case_ask.add_argument('case_id')
    case_ask.add_argument('question', nargs='+')
    case_ask.add_argument('--verbose', action='store_true', help='Show rationale, used evidence and safe next steps')
    case_ask.set_defaults(handler=cmd_case_ask)
    case_watch = case_sub.add_parser('watch', help='Watch a live colorized Case event trace')
    case_watch.add_argument('case_id')
    case_watch.add_argument('--interval', type=float, default=1.0, help='Polling interval in seconds')
    case_watch.add_argument('--timeout', type=float, default=60.0, help='Maximum watch time in seconds; 0 disables timeout')
    case_watch.add_argument('--follow', action='store_true', help='Keep watching after waiting_approval/manual_review/resolved')
    case_watch.set_defaults(handler=cmd_case_watch)
    case_chat = case_sub.add_parser('chat', help='Open an interactive Case-scoped Agent chat')
    case_chat.add_argument('case_id')
    case_chat.add_argument('--events', type=int, default=12, help='Number of recent events shown by /events')
    case_chat.add_argument('--verbose', action='store_true', help='Show rationale, used evidence and safe next steps for each answer')
    case_chat.set_defaults(handler=cmd_case_chat)

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
    eval_case = eval_sub.add_parser('case', help='Show execution-quality metrics for one Case')
    eval_case.add_argument('case_id')
    eval_case.add_argument('--events', action='store_true', help='Include the full event sequence')
    eval_case.set_defaults(handler=cmd_eval_case)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config_created = False
        if args.command in {'init', 'config'}:
            config = {}
        else:
            config, config_created = load_or_create_cli_config()
        base_url = (
            args.base_url
            or os.getenv('RESOLVEOPS_API_URL')
            or config.get('api_url')
            or DEFAULT_BASE_URL
        )
        operator_key = (
            args.operator_key
            or os.getenv('RESOLVEOPS_OPERATOR_KEY')
            or os.getenv('OPERATOR_API_KEY')
            or config.get('operator_key')
        )
        if args.command not in {'init', 'config'} and not operator_key:
            raise CliError('missing operator_key.\n' + config_edit_hint(created=config_created))
        client = ApiClient(base_url, operator_key)
        return args.handler(args, client)
    except CliError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
