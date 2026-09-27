$ErrorActionPreference = "Stop"

$env:DATABASE_URL = "sqlite:///./data/resolveops-test.db"
python -m pytest -q
