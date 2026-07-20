"""Case-centered read-only question handling.

This module gives ResolveOps an operator-facing Agent interaction without
turning the system into a generic chatbot.  A question is always scoped to one
Case, can use only the read tools enabled for that Case type, and never creates
approvals or write invocations.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from .config import settings
from .llm_gateway import LLMGateway
from .tool_result import ToolResult
from .tool_scheduler import ReadToolCall, ReadToolScheduler


CASE_ASK_SYSTEM = """You are ResolveOps' Case Inquiry Agent.
Answer operator questions about exactly one business Case.
You may call read-only business tools when the current Case context does not contain enough fresh evidence.
Never execute writes, never approve actions, never claim that an ERP write happened unless the Case context contains invocation and verification evidence.
Tool errors mean unknown, not negative business facts.
Return JSON only with keys: answer, rationale, used_evidence, used_tools, safe_next_steps.
safe_next_steps must never bypass Policy, Approval, Executor, or Verification."""


FINAL_ANSWER_SYSTEM = """Return the final ResolveOps Case answer as JSON only.
Required keys:
- answer: concise operator-facing answer
- rationale: why this answer follows from the Case context and tool results
- used_evidence: array of evidence IDs or short evidence descriptions
- used_tools: array of read tool names used in this answer
- safe_next_steps: array of safe next steps; do not include direct ERP writes or approval bypasses"""


class CaseQuestionAgent:
    def __init__(self, tools: Any, llm_gateway: LLMGateway | None = None) -> None:
        self.tools = tools
        self.llm = llm_gateway or LLMGateway()

    def answer(
        self,
        *,
        order_id: str,
        question: str,
        case_context: dict[str, Any],
        on_observation,
    ) -> dict[str, Any]:
        seen: dict[tuple[str, str], ToolResult] = {}
        observations: list[dict[str, Any]] = []
        messages = [
            {'role': 'system', 'content': CASE_ASK_SYSTEM},
            {
                'role': 'user',
                'content': json.dumps({
                    'current_date': date.today().isoformat(),
                    'question': question,
                    'case_context': case_context,
                    'instruction': 'Answer the question. Call read tools only if more evidence is needed.',
                }, ensure_ascii=False),
            },
        ]

        first = self.llm.chat({
            'messages': messages,
            'tools': self.tools.definitions(),
            'tool_choice': 'auto',
            'temperature': 0,
        })
        if not first.ok:
            return self._failed_answer(question, first)

        first_message = first.first_message() or {}
        messages.append(first_message)
        calls = first_message.get('tool_calls') or []
        max_calls = max(1, min(settings.agent_max_read_tool_calls, 6))
        scheduled: list[ReadToolCall] = []
        malformed: list[tuple[dict[str, Any], ToolResult]] = []

        for call in calls[:max_calls]:
            function = call.get('function') or {}
            tool_name = function.get('name')
            if not tool_name:
                malformed.append((call, ToolResult.failure('missing_tool_name', error_type='InvalidToolCall')))
                continue
            try:
                args = json.loads(function.get('arguments') or '{}')
                if not isinstance(args, dict):
                    raise ValueError('tool arguments must be a JSON object')
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                malformed.append((call, ToolResult.failure('invalid_tool_arguments', error_type=type(exc).__name__)))
                continue
            scheduled.append(ReadToolCall(call_id=call.get('id') or f'call-{len(scheduled)}', name=tool_name, arguments=args))

        for call, result in malformed:
            function = call.get('function') or {}
            observation = result.observation_result()
            tool_name = function.get('name') or 'unknown_tool'
            record = {
                'tool': tool_name,
                'arguments': {},
                'result': observation,
                'tool_result': result.to_dict(),
                'scheduler': {'source': 'invalid_arguments'},
            }
            observations.append(record)
            on_observation(record)
            messages.append({'role': 'tool', 'tool_call_id': call.get('id') or 'invalid-call', 'content': json.dumps(result.to_dict(), ensure_ascii=False)})

        scheduler = ReadToolScheduler(self.tools, max_workers=settings.agent_read_tool_parallelism)
        for execution in scheduler.execute_batch(scheduled, order_id, seen):
            tool_result = execution.result
            record = {
                'tool': execution.call.name,
                'arguments': execution.call.arguments,
                'result': tool_result.observation_result(),
                'tool_result': {**tool_result.to_dict(), 'scheduler': {'source': execution.source, 'signature': execution.signature}},
                'scheduler': {'source': execution.source, 'signature': execution.signature},
            }
            observations.append(record)
            on_observation(record)
            messages.append({
                'role': 'tool',
                'tool_call_id': execution.call.call_id,
                'content': json.dumps(record['tool_result'], ensure_ascii=False),
            })

        final = self.llm.chat({
            'messages': [
                {'role': 'system', 'content': FINAL_ANSWER_SYSTEM},
                *messages[1:],
                {
                    'role': 'user',
                    'content': json.dumps({
                        'question': question,
                        'observations': observations,
                        'case_context': case_context,
                        'instruction': 'Now answer the operator question from the Case context and any tool observations.',
                    }, ensure_ascii=False),
                },
            ],
            'response_format': {'type': 'json_object'},
            'temperature': 0,
        })
        if not final.ok:
            return self._failed_answer(question, final, observations)

        parsed = self._parse_answer((final.first_message() or {}).get('content'))
        parsed['question'] = question
        parsed['observations'] = observations
        parsed['llm'] = {
            'initial': first.telemetry(),
            'final': final.telemetry(),
        }
        if calls and len(calls) > max_calls:
            parsed['safe_next_steps'] = list(dict.fromkeys((parsed.get('safe_next_steps') or []) + ['Question tool-call budget was reached; ask a narrower follow-up if needed.']))
        return parsed

    @staticmethod
    def _parse_answer(content: Any) -> dict[str, Any]:
        if not isinstance(content, str):
            return {
                'answer': 'The model returned no structured answer.',
                'rationale': 'No JSON answer was available.',
                'used_evidence': [],
                'used_tools': [],
                'safe_next_steps': ['Review the Case manually.'],
                'parse_error': 'empty_answer',
            }
        cleaned = content.strip()
        if cleaned.startswith('```'):
            cleaned = cleaned.split('\n', 1)[1] if '\n' in cleaned else ''
            if cleaned.rstrip().endswith('```'):
                cleaned = cleaned.rstrip()[:-3].strip()
        try:
            result = json.loads(cleaned)
            if not isinstance(result, dict):
                raise ValueError('answer must be an object')
            answer = result.get('answer')
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError('answer is required')
            for key in ('used_evidence', 'used_tools', 'safe_next_steps'):
                if isinstance(result.get(key), str):
                    result[key] = [result[key]]
                elif not isinstance(result.get(key), list):
                    result[key] = []
            if not isinstance(result.get('rationale'), str):
                result['rationale'] = ''
            return result
        except (ValueError, json.JSONDecodeError):
            return {
                'answer': cleaned or 'The model answer could not be parsed.',
                'rationale': 'The model did not return the required JSON shape.',
                'used_evidence': [],
                'used_tools': [],
                'safe_next_steps': ['Review the Case manually.'],
                'parse_error': 'required_json_schema_mismatch',
            }

    @staticmethod
    def _failed_answer(question: str, result, observations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            'question': question,
            'answer': 'The Case question could not be answered because the LLM call failed.',
            'rationale': result.error_code or 'llm_error',
            'used_evidence': [],
            'used_tools': [],
            'safe_next_steps': ['Use case show or eval case to inspect the current Case state manually.'],
            'observations': observations or [],
            'llm': result.telemetry(),
        }
