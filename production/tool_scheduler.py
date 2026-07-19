"""Read-only tool scheduling for the investigation Agent.

The scheduler is deliberately limited to LLM-visible read tools.  Write actions
remain Action Plan entries and are executed later by the governed Executor
layer after evidence grounding, policy, approval, idempotency, and verification.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from .tool_result import ToolResult


@dataclass(frozen=True)
class ReadToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ReadToolExecution:
    call: ReadToolCall
    result: ToolResult
    source: str
    signature: tuple[str, str]


def tool_signature(name: str, arguments: dict[str, Any]) -> tuple[str, str]:
    return name, json.dumps(arguments, sort_keys=True, ensure_ascii=False)


class ReadToolScheduler:
    """Execute independent read-tool calls with caching and result envelopes."""

    def __init__(self, tools: Any, *, max_workers: int = 4):
        self.tools = tools
        self.max_workers = max(1, int(max_workers or 1))

    def execute_batch(
        self,
        calls: list[ReadToolCall],
        order_id: str,
        seen: dict[tuple[str, str], ToolResult] | None = None,
    ) -> list[ReadToolExecution]:
        """Execute a batch and return results in the original call order.

        Multiple identical calls in the same batch share a single underlying
        execution.  Calls already present in `seen` are returned from cache.
        """
        seen = seen if seen is not None else {}
        executions: dict[tuple[str, str], ToolResult] = {}
        sources: dict[tuple[str, str], str] = {}
        first_calls: dict[tuple[str, str], ReadToolCall] = {}

        for call in calls:
            signature = tool_signature(call.name, call.arguments)
            if signature in seen:
                executions[signature] = seen[signature]
                sources[signature] = 'cache'
            elif signature not in first_calls:
                first_calls[signature] = call

        if first_calls:
            workers = min(self.max_workers, len(first_calls))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_to_signature = {
                    pool.submit(self._execute_one, call, order_id): signature
                    for signature, call in first_calls.items()
                    if signature not in executions
                }
                for future in as_completed(future_to_signature):
                    signature = future_to_signature[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = ToolResult.failure('tool_scheduler_failed', error_type=type(exc).__name__, retryable=True)
                    executions[signature] = result
                    seen[signature] = result
                    sources[signature] = 'executed'

        return [
            ReadToolExecution(
                call=call,
                result=executions[tool_signature(call.name, call.arguments)],
                source=sources.get(tool_signature(call.name, call.arguments), 'deduped'),
                signature=tool_signature(call.name, call.arguments),
            )
            for call in calls
        ]

    def _execute_one(self, call: ReadToolCall, order_id: str) -> ToolResult:
        if hasattr(self.tools, 'execute_result'):
            result = self.tools.execute_result(call.name, call.arguments, order_id)
            return result if isinstance(result, ToolResult) else ToolResult.success(result if isinstance(result, dict) else {'value': result})
        result = self.tools.execute(call.name, call.arguments, order_id)
        if isinstance(result, dict) and result.get('error'):
            return ToolResult.failure(result.get('error'), error_type=result.get('error_type'))
        return ToolResult.success(result if isinstance(result, dict) else {'value': result})
