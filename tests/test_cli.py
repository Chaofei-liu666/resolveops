import json
import sys
import types

import production.cli as cli


class FakeResponse:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.content = json.dumps(self._data).encode()
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


def test_cli_status_calls_runtime_status_with_operator_key(monkeypatch, capsys):
    calls = []

    def fake_request(method, url, headers=None, json=None, timeout=None):
        calls.append({'method': method, 'url': url, 'headers': headers, 'json': json, 'timeout': timeout})
        return FakeResponse(data={
            'status': 'ready',
            'checks': {'database': {'ok': True}, 'configuration': {'ok': True}},
            'queues': {'queued': 0, 'running': 0, 'failed': 0},
        })

    monkeypatch.setattr(cli.httpx, 'request', fake_request)
    result = cli.main(['--base-url', 'http://api.local', '--operator-key', 'ops-key', 'status'])

    assert result == 0
    assert calls == [{
        'method': 'GET',
        'url': 'http://api.local/v1/runtime/status',
        'headers': {'X-Operator-Key': 'ops-key'},
        'json': None,
        'timeout': 30,
    }]
    assert 'ResolveOps Runtime Status' in capsys.readouterr().out


def test_cli_init_creates_local_config_template(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('RESOLVEOPS_CONFIG_HOME', str(tmp_path))

    result = cli.main(['init'])

    assert result == 0
    config_file = tmp_path / 'config.json'
    assert config_file.exists()
    data = json.loads(config_file.read_text(encoding='utf-8'))
    assert data == {'api_url': 'http://localhost:8090', 'operator_key': ''}
    out = capsys.readouterr().out
    assert 'ResolveOps CLI' in out
    assert '[Init] Checking local CLI config' in out
    assert '[Next] Start Workbench' in out


def test_cli_config_set_and_show_mask_secret(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('RESOLVEOPS_CONFIG_HOME', str(tmp_path))

    assert cli.main(['config', 'set', 'api_url', 'http://api.local/']) == 0
    assert cli.main(['config', 'set', 'operator_key', 'abcdef1234567890']) == 0
    assert cli.main(['config', 'show']) == 0

    data = json.loads((tmp_path / 'config.json').read_text(encoding='utf-8'))
    assert data == {'api_url': 'http://api.local', 'operator_key': 'abcdef1234567890'}
    out = capsys.readouterr().out
    assert 'http://api.local' in out
    assert 'abcdef1234567890' not in out
    assert 'abcd********7890' in out


def test_cli_status_reads_local_config_when_flags_are_omitted(monkeypatch, tmp_path):
    (tmp_path / 'config.json').write_text(
        json.dumps({'api_url': 'http://api.local', 'operator_key': 'ops-key'}),
        encoding='utf-8',
    )
    monkeypatch.setenv('RESOLVEOPS_CONFIG_HOME', str(tmp_path))
    monkeypatch.delenv('RESOLVEOPS_API_URL', raising=False)
    monkeypatch.delenv('RESOLVEOPS_OPERATOR_KEY', raising=False)
    monkeypatch.delenv('OPERATOR_API_KEY', raising=False)
    calls = []

    def fake_request(method, url, headers=None, json=None, timeout=None):
        calls.append({'method': method, 'url': url, 'headers': headers})
        return FakeResponse(data={
            'status': 'ready',
            'checks': {'database': {'ok': True}},
            'queues': {'queued': 0, 'running': 0, 'failed': 0},
        })

    monkeypatch.setattr(cli.httpx, 'request', fake_request)

    result = cli.main(['status'])

    assert result == 0
    assert calls == [{
        'method': 'GET',
        'url': 'http://api.local/v1/runtime/status',
        'headers': {'X-Operator-Key': 'ops-key'},
    }]


def test_cli_api_command_auto_creates_config_template_when_missing_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('RESOLVEOPS_CONFIG_HOME', str(tmp_path))
    monkeypatch.delenv('RESOLVEOPS_API_URL', raising=False)
    monkeypatch.delenv('RESOLVEOPS_OPERATOR_KEY', raising=False)
    monkeypatch.delenv('OPERATOR_API_KEY', raising=False)

    result = cli.main(['status'])

    assert result == 1
    config_file = tmp_path / 'config.json'
    assert config_file.exists()
    data = json.loads(config_file.read_text(encoding='utf-8'))
    assert data == {'api_url': 'http://localhost:8090', 'operator_key': ''}
    err = capsys.readouterr().err
    assert 'missing operator_key' in err
    assert str(config_file) in err


def test_cli_fault_injection_posts_resolveops_payload(monkeypatch, capsys):
    calls = []

    def fake_request(method, url, headers=None, json=None, timeout=None):
        calls.append({'method': method, 'url': url, 'headers': headers, 'json': json})
        return FakeResponse(data={
            'status': 'applied',
            'fault_type': 'inventory_changed_before_execution',
            'item_code': 'SKU-A12',
            'warehouse': '重庆仓 - ROPS',
            'new_qty': 0,
            'erpnext_result': {'stock_reconciliation': 'MAT-RECO-TEST'},
        })

    monkeypatch.setattr(cli.httpx, 'request', fake_request)
    result = cli.main([
        '--base-url', 'http://api.local',
        '--operator-key', 'ops-key',
        'fi', 'run', 'inventory_changed_before_execution',
        '--case', 'CASE-1',
        '--item', 'SKU-A12',
        '--warehouse', '重庆仓 - ROPS',
        '--new-qty', '0',
        '--reason', 'simulate stock consumed',
    ])

    assert result == 0
    assert calls[0]['method'] == 'POST'
    assert calls[0]['url'] == 'http://api.local/v1/fault-injections/run'
    assert calls[0]['headers'] == {'X-Operator-Key': 'ops-key'}
    assert calls[0]['json'] == {
        'fault_type': 'inventory_changed_before_execution',
        'case_id': 'CASE-1',
        'item_code': 'SKU-A12',
        'warehouse': '重庆仓 - ROPS',
        'new_qty': 0.0,
        'reason': 'simulate stock consumed',
    }
    assert 'MAT-RECO-TEST' in capsys.readouterr().out


def test_cli_case_create_posts_resolveops_payload(monkeypatch, capsys):
    calls = []

    def fake_request(method, url, headers=None, json=None, timeout=None):
        calls.append({'method': method, 'url': url, 'headers': headers, 'json': json})
        return FakeResponse(data={'case_id': 'CASE-1', 'status': 'queued', 'duplicate': False})

    monkeypatch.setattr(cli.httpx, 'request', fake_request)
    result = cli.main([
        '--base-url', 'http://api.local',
        '--operator-key', 'sales-key',
        'case', 'create',
        '--type', 'inventory_shortage',
        '--order', 'SAL-ORD-2026-00002',
        '--source-event-id', 'cli-event-1',
        '--reason', 'created from terminal',
    ])

    assert result == 0
    assert calls[0]['method'] == 'POST'
    assert calls[0]['url'] == 'http://api.local/v1/cases'
    assert calls[0]['headers'] == {'X-Operator-Key': 'sales-key'}
    assert calls[0]['json'] == {
        'tenant_id': 'demo',
        'event_type': 'inventory_shortage',
        'order_id': 'SAL-ORD-2026-00002',
        'source_event_id': 'cli-event-1',
        'reason': 'created from terminal',
    }
    assert 'CASE-1' in capsys.readouterr().out


def test_cli_case_ask_posts_question(monkeypatch, capsys):
    calls = []

    def fake_request(method, url, headers=None, json=None, timeout=None):
        calls.append({'method': method, 'url': url, 'headers': headers, 'json': json, 'timeout': timeout})
        return FakeResponse(data={
            'case_id': 'CASE-1',
            'event_type': 'inventory_shortage',
            'order_id': 'SO-1',
            'status': 'waiting_approval',
            'question': 'Why not purchase?',
            'answer': 'Transfer is faster than purchase based on current evidence.',
            'rationale': 'Transfer route takes 1 day while purchase lead time is 3 days.',
            'used_tools': ['get_item_supply_profile', 'get_transfer_options'],
            'used_evidence': ['transfer transit_days=1', 'lead_time_days=3'],
            'safe_next_steps': ['Approve the bound action or ask for replanning.'],
            'observations': [{'tool': 'get_transfer_options', 'scheduler': {'source': 'executed'}, 'result': {'lanes': []}}],
        })

    monkeypatch.setattr(cli.httpx, 'request', fake_request)
    result = cli.main([
        '--base-url', 'http://api.local',
        '--operator-key', 'ops-key',
        'case', 'ask', 'CASE-1', 'Why not purchase?',
    ])

    assert result == 0
    assert calls == [{
        'method': 'POST',
        'url': 'http://api.local/v1/cases/CASE-1/ask',
        'headers': {'X-Operator-Key': 'ops-key'},
        'json': {'question': 'Why not purchase?'},
        'timeout': 30,
    }]
    out = capsys.readouterr().out
    assert '[You] Why not purchase?' in out
    assert '[Answer]' in out
    assert 'Transfer is faster' in out
    assert '[Tool]' in out
    assert 'get_transfer_options' in out
    assert '[Rationale]' not in out
    assert '[Safe Next Steps]' not in out


def test_cli_case_ask_verbose_prints_rationale_and_safe_steps(monkeypatch, capsys):
    def fake_request(method, url, headers=None, json=None, timeout=None):
        return FakeResponse(data={
            'case_id': 'CASE-1',
            'event_type': 'inventory_shortage',
            'order_id': 'SO-1',
            'status': 'waiting_approval',
            'question': 'Why not purchase?',
            'answer': 'Transfer is faster.',
            'rationale': 'Transfer takes 1 day.',
            'used_evidence': ['transfer transit_days=1'],
            'safe_next_steps': ['Approve the bound action if appropriate.'],
            'observations': [],
        })

    monkeypatch.setattr(cli.httpx, 'request', fake_request)
    result = cli.main([
        '--base-url', 'http://api.local',
        '--operator-key', 'ops-key',
        'case', 'ask', 'CASE-1', 'Why not purchase?',
        '--verbose',
    ])

    assert result == 0
    out = capsys.readouterr().out
    assert '[Rationale]' in out
    assert 'Transfer takes 1 day.' in out
    assert '[Used Evidence]' in out
    assert '[Safe Next Steps]' in out


def test_cli_case_show_prints_agent_decision_trace(capsys):
    cli.print_case_summary({
        'id': 'CASE-1',
        'event_type': 'inventory_shortage',
        'order_id': 'SO-1',
        'status': 'waiting_approval',
        'plan_version': 1,
        'plan': {'actions': [{'action_type': 'transfer_stock', 'rationale': 'fastest supported action'}]},
        'agent_decision': {
            'decision_trace': ['Observed shortage, then compared transfer and purchase evidence.'],
            'rejected_actions': [{'action_type': 'create_purchase_request', 'reason': 'lead time misses delivery date'}],
            'missing_information': ['supplier unit cost remains unknown'],
        },
        'approvals': [],
        'tool_trace': {},
        'events': [],
    })

    out = capsys.readouterr().out
    assert '[Agent Decision Trace]' in out
    assert 'Observed shortage' in out
    assert 'create_purchase_request: lead time misses delivery date' in out
    assert 'supplier unit cost remains unknown' in out


def test_cli_console_launches_the_single_interactive_workbench(monkeypatch):
    calls = []
    fake_console = types.ModuleType('production.console')

    def run_workbench(client, *, case_limit, poll_interval):
        calls.append({'client': client, 'case_limit': case_limit, 'poll_interval': poll_interval})
        return 0

    fake_console.run_workbench = run_workbench
    monkeypatch.setitem(sys.modules, 'production.console', fake_console)

    result = cli.main([
        '--base-url', 'http://api.local',
        '--operator-key', 'ops-key',
        'console', '--case-limit', '12', '--poll-interval', '2',
    ])

    assert result == 0
    assert calls[0]['client'].base_url == 'http://api.local'
    assert calls[0]['client'].operator_key == 'ops-key'
    assert calls[0]['case_limit'] == 12
    assert calls[0]['poll_interval'] == 2.0


def test_cli_exposes_one_interactive_command():
    parser = cli.build_parser()
    subparsers = next(action for action in parser._actions if action.dest == 'command')
    assert 'console' in subparsers.choices
    assert 'chat' not in subparsers.choices
    case_parser = subparsers.choices['case']
    case_subparsers = next(action for action in case_parser._actions if action.dest == 'case_command')
    assert 'chat' not in case_subparsers.choices
    assert 'watch' not in case_subparsers.choices


def test_cli_eval_summary_calls_eval_endpoint(monkeypatch, capsys):
    calls = []

    def fake_request(method, url, headers=None, json=None, timeout=None):
        calls.append({'method': method, 'url': url, 'headers': headers, 'json': json, 'timeout': timeout})
        return FakeResponse(data={
            'total_cases': 2,
            'resolved_cases': 1,
            'manual_review_cases': 1,
            'case_resolution_rate': 0.5,
            'avg_read_tool_calls': 3,
            'tool_failure_rate': 0.25,
            'tool_failures': 1,
            'approval_waiting_cases': 0,
            'cases_with_writes': 1,
            'verified_write_cases': 1,
            'verification_pass_rate': 1,
            'policy_denials': 0,
            'replanned_cases': 1,
            'manual_handoff_cases': 1,
            'task_failures': 0,
            'context_isolation_sanitized_cases': 1,
            'context_isolation_failures': 0,
            'evidence_grounding_passed_cases': 1,
            'evidence_grounding_failures': 0,
            'cases': [
                {
                    'case_id': 'CASE-1',
                    'event_type': 'inventory_shortage',
                    'order_id': 'SO-1',
                    'status': 'resolved',
                    'tool_call_count': 4,
                    'write_invocation_count': 1,
                    'has_replan': True,
                }
            ],
        })

    monkeypatch.setattr(cli.httpx, 'request', fake_request)
    result = cli.main([
        '--base-url', 'http://api.local',
        '--operator-key', 'ops-key',
        'eval', 'summary',
        '--limit', '20',
        '--cases',
    ])

    assert result == 0
    assert calls == [{
        'method': 'GET',
        'url': 'http://api.local/v1/evals/summary?limit=20',
        'headers': {'X-Operator-Key': 'ops-key'},
        'json': None,
        'timeout': 30,
    }]
    out = capsys.readouterr().out
    assert 'ResolveOps Eval Summary' in out
    assert 'resolution=50.0%' in out
    assert 'CASE-1' in out


def test_cli_eval_case_calls_case_eval_endpoint(monkeypatch, capsys):
    calls = []

    def fake_request(method, url, headers=None, json=None, timeout=None):
        calls.append({'method': method, 'url': url, 'headers': headers, 'json': json, 'timeout': timeout})
        return FakeResponse(data={
            'case_id': 'CASE-1',
            'event_type': 'inventory_shortage',
            'order_id': 'SO-1',
            'status': 'resolved',
            'plan_version': 1,
            'resolved': True,
            'manual_review': False,
            'verification_complete': True,
            'tool_call_count': 3,
            'scheduled_tool_call_count': 3,
            'tool_failure_count': 0,
            'tool_scheduler_sources': {'executed': 3},
            'tool_trace_summary': {'tools_used': ['get_order', 'get_inventory']},
            'approval_count': 1,
            'pending_approval_count': 0,
            'expired_approval_count': 0,
            'revoked_approval_count': 0,
            'has_policy_denial': False,
            'write_invocation_count': 1,
            'verification_pass_count': 1,
            'verification_failed_count': 0,
            'has_replan': False,
            'recovery_event_count': 0,
            'blocked_event_count': 0,
            'has_manual_handoff': False,
            'has_context_isolation_sanitized': False,
            'has_context_isolation_failure': False,
            'has_evidence_grounding_passed': True,
            'has_evidence_grounding_failure': False,
            'stage_sequence': ['case_created', 'tool_observation', 'verification_passed'],
        })

    monkeypatch.setattr(cli.httpx, 'request', fake_request)
    result = cli.main([
        '--base-url', 'http://api.local',
        '--operator-key', 'ops-key',
        'eval', 'case', 'CASE-1',
    ])

    assert result == 0
    assert calls == [{
        'method': 'GET',
        'url': 'http://api.local/v1/evals/cases/CASE-1',
        'headers': {'X-Operator-Key': 'ops-key'},
        'json': None,
        'timeout': 30,
    }]
    out = capsys.readouterr().out
    assert 'ResolveOps Case Eval' in out
    assert 'verification=passed' in out
    assert 'get_order, get_inventory' in out
    assert 'Key Stage Sequence' in out


def test_cli_doctor_checks_api_runtime_and_sandbox(monkeypatch, capsys):
    calls = []

    def fake_request(method, url, headers=None, json=None, timeout=None):
        calls.append({'method': method, 'url': url, 'headers': headers, 'json': json, 'timeout': timeout})
        if url.endswith('/healthz'):
            return FakeResponse(data={'status': 'ok'})
        if url.endswith('/v1/runtime/status'):
            return FakeResponse(data={'status': 'ready', 'checks': {'database': {'ok': True}}})
        if url.endswith('/v1/sandbox/check'):
            return FakeResponse(data={
                'status': 'ready',
                'app_env': 'local',
                'defaults': {'order_id': 'SAL-ORD-2026-00002', 'item_code': 'SKU-A12'},
                'checks': {'erpnext_sales_order': {'ok': True, 'value': {'name': 'SAL-ORD-2026-00002'}}},
            })
        return FakeResponse(status_code=404, data={'detail': 'not found'})

    monkeypatch.setattr(cli.httpx, 'request', fake_request)
    result = cli.main(['--base-url', 'http://api.local', '--operator-key', 'ops-key', 'doctor'])

    assert result == 0
    assert [call['url'] for call in calls] == [
        'http://api.local/healthz',
        'http://api.local/v1/runtime/status',
        'http://api.local/v1/sandbox/check',
    ]
    assert all(call['headers'] == {'X-Operator-Key': 'ops-key'} for call in calls)
    out = capsys.readouterr().out
    assert 'ResolveOps Doctor' in out
    assert 'ERPNext sandbox check' in out


def test_cli_sandbox_seed_posts_through_resolveops_api(monkeypatch, capsys):
    calls = []

    def fake_request(method, url, headers=None, json=None, timeout=None):
        calls.append({'method': method, 'url': url, 'headers': headers, 'json': json, 'timeout': timeout})
        return FakeResponse(data={
            'status': 'ready',
            'actions': [
                {'type': 'logistics_lane', 'status': 'updated'},
                {'type': 'source_stock', 'status': 'set', 'erpnext_result': {'stock_reconciliation': 'MAT-RECO-TEST'}},
                {'type': 'target_stock', 'status': 'set', 'erpnext_result': {'stock_reconciliation': 'MAT-RECO-TARGET'}},
            ],
            'check': {
                'status': 'ready',
                'app_env': 'local',
                'defaults': {'order_id': 'SAL-ORD-2026-00002', 'item_code': 'SKU-A12'},
                'checks': {'source_stock': {'ok': True, 'value': {'actual_qty': 40}}},
            },
        })

    monkeypatch.setattr(cli.httpx, 'request', fake_request)
    result = cli.main([
        '--base-url', 'http://api.local',
        '--operator-key', 'ops-key',
        'sandbox', 'seed',
        '--source-qty', '40',
    ])

    assert result == 0
    assert calls == [{
        'method': 'POST',
        'url': 'http://api.local/v1/sandbox/seed',
        'headers': {'X-Operator-Key': 'ops-key'},
        'json': {
            'tenant_id': 'demo',
            'order_id': 'SAL-ORD-2026-00002',
            'item_code': 'SKU-A12',
            'customer': '恒远科技',
            'source_warehouse': '重庆仓 - ROPS',
            'target_warehouse': 'Stores - ROPS',
            'source_qty': 40.0,
            'target_qty': 0,
            'transit_days': 1,
            'cost_per_unit': 8,
            'set_stock': True,
        },
        'timeout': 30,
    }]
    out = capsys.readouterr().out
    assert 'Sandbox seed completed' in out
    assert 'MAT-RECO-TEST' in out


def test_cli_returns_error_for_failed_api(monkeypatch, capsys):
    monkeypatch.setattr(cli.httpx, 'request', lambda *args, **kwargs: FakeResponse(status_code=403, data={'detail': 'disabled'}))

    result = cli.main(['--base-url', 'http://api.local', '--operator-key', 'ops-key', 'fi', 'list'])

    assert result == 1
    err = capsys.readouterr().err
    assert 'HTTP 403' in err
    assert 'Edit CLI config' in err
