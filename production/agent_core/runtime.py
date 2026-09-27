"""Bounded provider/tool loop with no ResolveOps business knowledge."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .contracts import AgentEvent, LLMClient, LLMRequest, StopReason
from ..tool_result import ToolResult, annotate_tool_result
from ..tool_scheduler import ReadToolCall, ReadToolScheduler


@dataclass(frozen=True)
class ReadLoopResult:
    messages: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    failed_tools: list[str]
    stop_reason: StopReason
    last_llm_telemetry: dict[str, Any] | None = None
    llm_telemetries: list[dict[str, Any]] | None = None


class ReadToolLoop:
    """Generic ``LLM → read tool → observation → LLM`` loop.

    It deliberately has no idea which tools/actions are meaningful.  The
    ResolveOps layer supplies a prompt, a registry and a later planner.
    """

    def __init__(
        self,
        *,
        llm: LLMClient,
        tools: Any,
        max_turns: int,
        max_tool_calls: int,
        parallelism: int,
        tool_definitions_for_turn: Callable[[int], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.llm, self.tools = llm, tools
        self.max_turns = max(1, max_turns)
        self.max_tool_calls = max(1, max_tool_calls)
        self.parallelism = max(1, parallelism)
        self.tool_definitions_for_turn = tool_definitions_for_turn

    def run(
        self,
        *,
        messages: list[dict[str, Any]],
        order_id: str,
        on_observation: Callable[[str, dict[str, Any], dict[str, Any], dict[str, Any]], None],
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> ReadLoopResult:
        emit = on_event or (lambda _event: None)
        working = list(messages)
        seen: dict[tuple[str, str], ToolResult] = {}
        observations: list[dict[str, Any]] = []
        failed_tools: list[str] = []
        stop_reason: StopReason = 'completed'
        last_telemetry: dict[str, Any] | None = None
        llm_telemetries: list[dict[str, Any]] = []
        scheduler = ReadToolScheduler(self.tools, max_workers=self.parallelism)
        emit(AgentEvent('agent_start', {'max_turns': self.max_turns, 'max_tool_calls': self.max_tool_calls}))

        def record_tool_result(
            *,
            turn: int,
            name: str,
            arguments: dict[str, Any],
            call_id: str,
            tool_result: ToolResult,
            scheduler_source: str,
            signature: Any = None,
        ) -> None:
            """Emit one normalized Observation for every Tool-call outcome."""
            result_data = tool_result.observation_result()
            scheduler_meta = {'source': scheduler_source}
            if signature is not None:
                scheduler_meta['signature'] = signature
            tool_result_data = {**tool_result.to_dict(), 'scheduler': scheduler_meta}
            observations.append({
                'tool': name, 'arguments': arguments, 'result': result_data,
                'tool_result': tool_result_data, 'scheduler': scheduler_meta,
            })
            if result_data.get('error'):
                failed_tools.append(name)
            on_observation(name, arguments, result_data, tool_result_data)
            working.append({'role': 'tool', 'tool_call_id': call_id, 'content': json.dumps(tool_result_data, ensure_ascii=False)})
            metadata = tool_result.metadata or {}
            stage = str(metadata.get('pipeline_stage') or '')
            if stage == 'schema_validation':
                emit(AgentEvent('tool_validation_failed', {
                    'turn': turn, 'tool': name, 'error_code': tool_result.error_code,
                    'schema_errors': metadata.get('schema_errors') or [],
                }))
            elif stage in {'tool_guardrails', 'tool_surface'}:
                emit(AgentEvent('tool_guardrail_blocked', {
                    'turn': turn, 'tool': name, 'error_code': tool_result.error_code,
                    'reason': metadata.get('guardrail_reason') or tool_result.error_code,
                }))
            elif stage == 'argument_parse':
                emit(AgentEvent('tool_argument_parse_failed', {
                    'turn': turn, 'tool': name, 'error_code': tool_result.error_code,
                }))
            emit(AgentEvent('tool_result', {
                'turn': turn, 'tool': name, 'status': tool_result.status,
                'source': scheduler_source, 'pipeline_stage': stage or 'result_normalization',
                'error_code': tool_result.error_code,
            }))

        for turn in range(1, self.max_turns + 1):
            emit(AgentEvent('turn_start', {'turn': turn}))
            visible_tools = self.tool_definitions_for_turn(turn) if self.tool_definitions_for_turn else self.tools.definitions()
            visible_names = {
                str(item.get('function', {}).get('name') or '')
                for item in visible_tools if isinstance(item, dict)
            }
            result = self.llm.chat(LLMRequest(
                messages=working, tools=visible_tools, tool_choice='auto', temperature=0
            ).to_payload())
            last_telemetry = result.telemetry()
            llm_telemetries.append(last_telemetry)
            if not result.ok:
                stop_reason = 'llm_error'
                emit(AgentEvent('turn_end', {'turn': turn, 'reason': stop_reason}))
                break
            if result.finish_reason == 'length':
                stop_reason = 'model_truncated'
                emit(AgentEvent('turn_end', {'turn': turn, 'reason': stop_reason}))
                break

            message = result.first_message() or {}
            working.append(message)
            calls = message.get('tool_calls') or []
            if not calls:
                emit(AgentEvent('turn_end', {'turn': turn, 'reason': 'no_tool_calls'}))
                break

            batch: list[ReadToolCall] = []
            for call_index, call in enumerate(calls):
                if len(observations) + len(batch) >= self.max_tool_calls:
                    stop_reason = 'max_tool_calls'
                    break
                function = call.get('function') or {}
                name = function.get('name') or 'unknown_tool'
                call_id = call.get('id') or f'call-{turn}-{call_index}'
                try:
                    arguments = json.loads(function.get('arguments') or '{}')
                    if not isinstance(arguments, dict):
                        raise ValueError('tool arguments must be an object')
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    emit(AgentEvent('tool_call', {'turn': turn, 'tool': name, 'arguments': {}}))
                    record_tool_result(
                        turn=turn, name=name, arguments={}, call_id=call_id,
                        tool_result=annotate_tool_result(
                            ToolResult.failure('invalid_tool_arguments', error_type=type(exc).__name__),
                            pipeline_stage='argument_parse',
                        ),
                        scheduler_source='invalid_arguments',
                    )
                    continue
                emit(AgentEvent('tool_call', {'turn': turn, 'tool': name, 'arguments': arguments}))
                if name not in visible_names:
                    record_tool_result(
                        turn=turn, name=name, arguments=arguments, call_id=call_id,
                        tool_result=annotate_tool_result(
                            ToolResult.failure('tool_not_available_this_turn'), pipeline_stage='tool_surface',
                        ),
                        scheduler_source='tool_surface',
                    )
                    continue
                batch.append(ReadToolCall(call_id=call_id, name=name, arguments=arguments))

            for execution in scheduler.execute_batch(batch, order_id, seen):
                record_tool_result(
                    turn=turn, name=execution.call.name, arguments=execution.call.arguments,
                    call_id=execution.call.call_id, tool_result=execution.result,
                    scheduler_source=execution.source, signature=execution.signature,
                )
            emit(AgentEvent('turn_end', {'turn': turn, 'reason': stop_reason if stop_reason != 'completed' else 'tool_calls_processed'}))
            if stop_reason != 'completed':
                break
        else:
            stop_reason = 'max_turns'

        emit(AgentEvent('agent_end', {'stop_reason': stop_reason, 'observation_count': len(observations)}))
        return ReadLoopResult(working, observations, failed_tools, stop_reason, last_telemetry, llm_telemetries)
