import json

from production import config


def test_runtime_connection_profile_persists_secrets_without_live_database_swap(tmp_path, monkeypatch):
    path = tmp_path / 'connections.json'
    monkeypatch.setenv('RESOLVEOPS_RUNTIME_CONFIG_PATH', str(path))
    original = {
        name: getattr(config.settings, name)
        for name in ('llm_base_url', 'llm_api_key', 'llm_model', 'erpnext_base_url', 'database_url')
    }
    try:
        updated, restart_required = config.save_runtime_connections({
            'llm_base_url': 'https://llm.example/v1',
            'llm_api_key': 'llm-secret',
            'llm_model': 'demo-model',
            'erpnext_base_url': 'https://erp.example',
            'database_url': 'postgresql+psycopg://new:secret@db.example:5432/newdb',
        })
        assert {'llm_base_url', 'llm_api_key', 'erpnext_base_url', 'database_url'} <= updated
        assert restart_required is True
        assert config.settings.llm_model == 'demo-model'
        # Database changes remain persisted for process restart rather than
        # splitting a running Worker across live SQLAlchemy engines.
        assert config.settings.database_url == original['database_url']
        payload = json.loads(path.read_text(encoding='utf-8'))
        assert payload['values']['llm_api_key'] == 'llm-secret'
        assert payload['values']['database_url'].endswith('/newdb')
    finally:
        for name, value in original.items():
            setattr(config.settings, name, value)
