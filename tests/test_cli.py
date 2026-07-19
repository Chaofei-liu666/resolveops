import json

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


def test_cli_returns_error_for_failed_api(monkeypatch, capsys):
    monkeypatch.setattr(cli.httpx, 'request', lambda *args, **kwargs: FakeResponse(status_code=403, data={'detail': 'disabled'}))

    result = cli.main(['--base-url', 'http://api.local', 'fi', 'list'])

    assert result == 1
    assert 'HTTP 403' in capsys.readouterr().err
