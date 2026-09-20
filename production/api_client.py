"""HTTP/SSE client shared by the command CLI and the Textual Workbench."""
from __future__ import annotations

import json
from typing import Any, Iterator

import httpx


class ApiClientError(RuntimeError):
    pass


def compact_json(value: Any, max_len: int = 180) -> str:
    if value is None:
        return ''
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return text if len(text) <= max_len else text[: max_len - 3] + '...'


class ApiClient:
    def __init__(self, base_url: str, operator_key: str | None) -> None:
        self.base_url = base_url.rstrip('/')
        self.operator_key = operator_key

    def _headers(self) -> dict[str, str]:
        return {'X-Operator-Key': self.operator_key} if self.operator_key else {}

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        response = httpx.request(method, self.base_url + path, headers=self._headers(), json=payload, timeout=30)
        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise ApiClientError(f'{method} {path} failed: HTTP {response.status_code} {detail}')
        return response.json() if response.content else None

    def stream(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Iterator[tuple[str, dict[str, Any]]]:
        """Read the narrow ResolveOps SSE protocol without exposing httpx to UI."""
        with httpx.stream(method, self.base_url + path, headers=self._headers(), json=payload, timeout=90) as response:
            if response.status_code >= 400:
                raise ApiClientError(f'{method} {path} failed: HTTP {response.status_code} {response.read().decode(errors="replace")}')
            event = 'message'
            for line in response.iter_lines():
                if not line:
                    continue
                if line.startswith('event:'):
                    event = line[6:].strip() or 'message'
                elif line.startswith('data:'):
                    try:
                        data = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        data = {'text': line[5:].strip()}
                    yield event, data if isinstance(data, dict) else {'value': data}
