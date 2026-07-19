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


def test_cli_returns_error_for_failed_api(monkeypatch, capsys):
    monkeypatch.setattr(cli.httpx, 'request', lambda *args, **kwargs: FakeResponse(status_code=403, data={'detail': 'disabled'}))

    result = cli.main(['--base-url', 'http://api.local', 'fi', 'list'])

    assert result == 1
    assert 'HTTP 403' in capsys.readouterr().err
