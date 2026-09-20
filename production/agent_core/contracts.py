"""Provider-neutral contracts for the Agent runtime."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

StopReason = Literal[
    'completed', 'max_turns', 'max_tool_calls', 'model_truncated', 'llm_error', 'aborted'
]


@dataclass(frozen=True)
class LLMRequest:
    """The complete, immutable request snapshot handed to an LLM adapter."""

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    temperature: float | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {'messages': self.messages}
        if self.tools is not None:
            payload['tools'] = self.tools
        if self.tool_choice is not None:
            payload['tool_choice'] = self.tool_choice
        if self.response_format is not None:
            payload['response_format'] = self.response_format
        if self.temperature is not None:
            payload['temperature'] = self.temperature
        return payload


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
    attempts: int = 1
    finish_reason: str | None = None

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
            'attempts': self.attempts,
            'finish_reason': self.finish_reason,
        }


class LLMClient(Protocol):
    """Adapters keep transport details behind this intentionally small edge."""

    def chat(self, payload: dict[str, Any]) -> LLMResult: ...


@dataclass(frozen=True)
class AgentEvent:
    kind: Literal['agent_start', 'turn_start', 'tool_call', 'tool_result', 'turn_end', 'agent_end']
    data: dict[str, Any]


@dataclass(frozen=True)
class ToolSchema:
    """The smallest declaration that may be exposed to an LLM."""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            'type': 'function',
            'function': {'name': self.name, 'description': self.description, 'parameters': self.parameters},
        }
