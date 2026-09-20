"""Central LLM provider boundary for ResolveOps.

The Agent should not call provider HTTP APIs directly.  This gateway keeps
timeouts, error normalization, latency, model name, and token usage in one
place so runtime evals can reason about LLM behavior.
"""
from __future__ import annotations

from time import monotonic, sleep
from typing import Any, Iterator

import httpx

from .config import settings
from .agent_core.contracts import LLMRequest, LLMResult


class LLMGateway:
    def __init__(self, *, base_url: str | None = None, api_key: str | None = None, model: str | None = None, timeout_seconds: float | None = None, max_retries: int | None = None):
        self.base_url = (base_url if base_url is not None else settings.llm_base_url)
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.model = model if model is not None else settings.llm_model
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else settings.llm_timeout_seconds
        self.max_retries = max_retries if max_retries is not None else settings.llm_max_retries

    def chat(self, payload: dict[str, Any]) -> LLMResult:
        if not self.base_url or not self.api_key or not self.model:
            return LLMResult(status='failed', error_code='llm_not_configured', error_type='ConfigurationError', retryable=False, model=self.model)
        request_payload = {**payload, 'model': payload.get('model') or self.model}
        started = monotonic()
        attempts = max(1, self.max_retries + 1)
        for attempt in range(1, attempts + 1):
            try:
                response = httpx.post(
                    self.base_url.rstrip().rstrip('/') + '/chat/completions',
                    headers={'Authorization': f'Bearer {self.api_key}'},
                    json=request_payload,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                data = response.json()
                choices = data.get('choices') or []
                finish_reason = choices[0].get('finish_reason') if choices and isinstance(choices[0], dict) else None
                return LLMResult(
                    status='success',
                    response=data,
                    model=request_payload.get('model'),
                    latency_ms=int((monotonic() - started) * 1000),
                    usage=data.get('usage') if isinstance(data.get('usage'), dict) else {},
                    attempts=attempt,
                    finish_reason=finish_reason if isinstance(finish_reason, str) else None,
                )
            except httpx.TimeoutException as exc:
                error_code, error_type, retryable = 'llm_timeout', type(exc).__name__, True
            except httpx.HTTPStatusError as exc:
                error_code, error_type = 'llm_http_error', f'HTTP_{exc.response.status_code}'
                retryable = exc.response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
            except Exception as exc:
                error_code, error_type, retryable = 'llm_provider_error', type(exc).__name__, True
            if retryable and attempt < attempts:
                # Chat-completion requests are read-only provider calls. One
                # bounded retry handles transient provider/network failures;
                # no ERP write is retried here.
                sleep(0.5)
                continue
            return LLMResult(
                status='failed', error_code=error_code, error_type=error_type,
                retryable=retryable, model=request_payload.get('model'),
                latency_ms=int((monotonic() - started) * 1000), attempts=attempt,
            )

    def stream_chat(self, payload: dict[str, Any] | LLMRequest) -> Iterator[dict[str, Any]]:
        """Yield provider text deltas through one normalized, public-safe shape.

        This is used only for operator-facing answers.  Investigation/planning
        remains event-streamed from durable Case events, never hidden reasoning.
        """
        if not self.base_url or not self.api_key or not self.model:
            yield {'type': 'error', 'error_code': 'llm_not_configured', 'error_type': 'ConfigurationError'}
            return
        request_payload = payload.to_payload() if isinstance(payload, LLMRequest) else dict(payload)
        request_payload.update({'model': request_payload.get('model') or self.model, 'stream': True})
        started = monotonic()
        answer_parts: list[str] = []
        finish_reason: str | None = None
        usage: dict[str, Any] = {}
        yielded_text = False
        try:
            with httpx.stream(
                'POST',
                self.base_url.rstrip().rstrip('/') + '/chat/completions',
                headers={'Authorization': f'Bearer {self.api_key}'},
                json=request_payload,
                timeout=self.timeout_seconds,
            ) as response:
                response.raise_for_status()
                yield {'type': 'start', 'model': request_payload['model']}
                for raw_line in response.iter_lines():
                    line = raw_line.strip() if isinstance(raw_line, str) else ''
                    if not line.startswith('data:'):
                        continue
                    data_text = line[5:].strip()
                    if data_text == '[DONE]':
                        break
                    try:
                        chunk = __import__('json').loads(data_text)
                    except ValueError:
                        continue
                    choices = chunk.get('choices') or []
                    if choices and isinstance(choices[0], dict):
                        choice = choices[0]
                        delta = choice.get('delta') if isinstance(choice.get('delta'), dict) else {}
                        content = delta.get('content')
                        if isinstance(content, str) and content:
                            answer_parts.append(content)
                            yielded_text = True
                            yield {'type': 'delta', 'text': content}
                        if isinstance(choice.get('finish_reason'), str):
                            finish_reason = choice['finish_reason']
                    if isinstance(chunk.get('usage'), dict):
                        usage = chunk['usage']
            yield {
                'type': 'done', 'answer': ''.join(answer_parts), 'model': request_payload['model'],
                'finish_reason': finish_reason, 'usage': usage,
                'latency_ms': int((monotonic() - started) * 1000), 'emitted_delta': yielded_text,
            }
        except httpx.TimeoutException as exc:
            yield {'type': 'error', 'error_code': 'llm_timeout', 'error_type': type(exc).__name__, 'emitted_delta': yielded_text}
        except httpx.HTTPStatusError as exc:
            yield {'type': 'error', 'error_code': 'llm_http_error', 'error_type': f'HTTP_{exc.response.status_code}', 'emitted_delta': yielded_text}
        except Exception as exc:
            yield {'type': 'error', 'error_code': 'llm_provider_error', 'error_type': type(exc).__name__, 'emitted_delta': yielded_text}
