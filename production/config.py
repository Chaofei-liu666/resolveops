from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    database_url: str = 'postgresql+psycopg://resolveops:resolveops@postgres:5432/resolveops'
    erpnext_base_url: str
    erpnext_api_key: str
    erpnext_api_secret: str
    webhook_secret: str
    operator_api_key: str
    alternative_warehouses: str = '重庆仓,上海仓'
    task_lease_seconds: int = 90
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    agent_max_investigation_turns: int = 8
    agent_max_read_tool_calls: int = 12
    agent_max_replans: int = 2


settings = Settings()
