"""Central LLM provider boundary for ResolveOps.

The Agent should not call provider HTTP APIs directly.  This gateway keeps
timeouts, error normalization, latency, model name, and token usage in one
place so runtime evals can reason about LLM behavior.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any

import httpx

from .config import settings


@dataclass(frozen=True)
class LLMResult:
    status: str
    response: dict[str, Any] | None = None
    error_code: str | None = None
    error_type: str | None = None
    retryable: bool = False
    model: str | None = None
    latency_ms: int | None = None
    usage: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == 'success'

    def first_message(self) -> dict[str, Any] | None:
        if not self.response:
            return None
        choices = self.response.get('choices') or []
        if not choices:
            return None
        message = choices[0].get('message')
        return message if isinstance(message, dict) else None

    def telemetry(self) -> dict[str, Any]:
        return {
            'status': self.status,
            'error_code': self.error_code,
            'error_type': self.error_type,
            'retryable': self.retryable,
            'model': self.model,
            'latency_ms': self.latency_ms,
            'usage': self.usage or {},
        }


class LLMGateway:
    def __init__(self, *, base_url: str | None = None, api_key: str | None = None, model: str | None = None, timeout_seconds: float = 30):
        self.base_url = (base_url if base_url is not None else settings.llm_base_url)
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.model = model if model is not None else settings.llm_model
        self.timeout_seconds = timeout_seconds

    def chat(self, payload: dict[str, Any]) -> LLMResult:
        if not self.base_url or not self.api_key or not self.model:
            return LLMResult(status='failed', error_code='llm_not_configured', error_type='ConfigurationError', retryable=False, model=self.model)
        request_payload = {**payload, 'model': payload.get('model') or self.model}
        started = monotonic()
        try:
            response = httpx.post(
                self.base_url.rstrip().rstrip('/') + '/chat/completions',
                headers={'Authorization': f'Bearer {self.api_key}'},
                json=request_payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            return LLMResult(
                status='success',
                response=data,
                model=request_payload.get('model'),
                latency_ms=int((monotonic() - started) * 1000),
                usage=data.get('usage') if isinstance(data.get('usage'), dict) else {},
            )
        except httpx.TimeoutException as exc:
            return LLMResult(status='failed', error_code='llm_timeout', error_type=type(exc).__name__, retryable=True, model=request_payload.get('model'), latency_ms=int((monotonic() - started) * 1000))
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
            return LLMResult(status='failed', error_code='llm_http_error', error_type=f'HTTP_{exc.response.status_code}', retryable=retryable, model=request_payload.get('model'), latency_ms=int((monotonic() - started) * 1000))
        except Exception as exc:
            return LLMResult(status='failed', error_code='llm_provider_error', error_type=type(exc).__name__, retryable=True, model=request_payload.get('model'), latency_ms=int((monotonic() - started) * 1000))
