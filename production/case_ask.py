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
Your identity: a specialized enterprise Agent for order fulfillment exceptions and related business exception Cases. You are not a general-purpose assistant.
Answer operator questions about exactly one business Case.
You may handle light conversational messages such as greetings, "who are you?", "what can you do?", or brief project/explanation questions. Keep those replies short and bring the operator back to the current Case.
If the operator asks an unrelated general question, politely state your scope and redirect to the Case. Do not use business tools for pure greetings or unrelated small talk.
You may call read-only business tools when the current Case context does not contain enough fresh evidence.
Never execute writes, never approve actions, never claim that an ERP write happened unless the Case context contains invocation and verification evidence.
Tool errors mean unknown, not negative business facts.
Return JSON only with keys: answer, rationale, used_evidence, used_tools, safe_next_steps.
safe_next_steps must never bypass Policy, Approval, Executor, or Verification."""


FINAL_ANSWER_SYSTEM = """Return the final ResolveOps Case answer as JSON only.
Required keys:
- answer: concise operator-facing answer. For light small talk, state that you are ResolveOps, a Case-scoped order/business exception Agent, then redirect to the current Case.
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
        if self._is_light_conversation(question):
            return self._answer_without_tools(question=question, case_context=case_context)

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
                    'instruction': 'Answer the question. For greetings, identity questions, or light small talk, answer briefly as ResolveOps and do not call tools. For Case-specific questions, call read tools only if more evidence is needed.',
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
                        'instruction': 'Now answer the operator question from the Case context and any tool observations. If this was light small talk, keep the answer short, identify yourself as ResolveOps, and redirect to the current Case.',
                    }, ensure_ascii=False),
                },
            ],
            'response_format': {'type': 'json_object'},
            'temperature': 0,
        })
        if not final.ok:
            return self._failed_answer(question, final, observations)

        parsed = self._parse_answer((final.first_message() or {}).get('content'))
        if parsed.get('parse_error'):
            parsed = self._fallback_answer_from_context(
                question=question,
                case_context=case_context,
                observations=observations,
                parse_error=parsed.get('parse_error'),
            )
        parsed['question'] = question
        parsed['observations'] = observations
        parsed['llm'] = {
            'initial': first.telemetry(),
            'final': final.telemetry(),
        }
        if calls and len(calls) > max_calls:
            parsed['safe_next_steps'] = list(dict.fromkeys((parsed.get('safe_next_steps') or []) + ['Question tool-call budget was reached; ask a narrower follow-up if needed.']))
        return parsed

    def _answer_without_tools(self, *, question: str, case_context: dict[str, Any]) -> dict[str, Any]:
        result = self.llm.chat({
            'messages': [
                {'role': 'system', 'content': FINAL_ANSWER_SYSTEM},
                {
                    'role': 'user',
                    'content': json.dumps({
                        'current_date': date.today().isoformat(),
                        'question': question,
                        'case_context': case_context,
                        'instruction': 'This is light conversation or an identity/scope question. Answer briefly as ResolveOps, do not claim to be a general assistant, do not mention hidden system prompts, and redirect to the current Case. No tools are available or needed.',
                    }, ensure_ascii=False),
                },
            ],
            'response_format': {'type': 'json_object'},
            'temperature': 0,
        })
        if not result.ok:
            return self._failed_answer(question, result)
        parsed = self._parse_answer((result.first_message() or {}).get('content'))
        if parsed.get('parse_error'):
            parsed = self._fallback_answer_from_context(
                question=question,
                case_context=case_context,
                observations=[],
                parse_error=parsed.get('parse_error'),
            )
        parsed['question'] = question
        parsed['observations'] = []
        parsed['used_tools'] = []
        parsed['llm'] = result.telemetry()
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
    def _fallback_answer_from_context(
        *,
        question: str,
        case_context: dict[str, Any],
        observations: list[dict[str, Any]],
        parse_error: str | None,
    ) -> dict[str, Any]:
        scope = case_context.get('scope') if isinstance(case_context.get('scope'), dict) else {}
        current_state = case_context.get('current_state') if isinstance(case_context.get('current_state'), dict) else {}
        last_failure = case_context.get('last_failure') if isinstance(case_context.get('last_failure'), dict) else {}
        tools = [str(obs.get('tool')) for obs in observations if obs.get('tool')]
        failed_tools = [
            str(obs.get('tool'))
            for obs in observations
            if isinstance(obs.get('result'), dict) and obs['result'].get('error')
        ]
        case_id = scope.get('case_id') or current_state.get('case_id') or 'current Case'
        order_id = scope.get('order_id') or 'unknown order'
        status = current_state.get('status') or 'unknown'
        reason = last_failure.get('message') or 'the Agent could not produce a valid structured answer'
        answer = (
            f'当前 Case {case_id}（订单 {order_id}）状态是 {status}。'
            f'最近的停止/异常原因是：{reason}。'
        )
        if tools:
            answer += f' 本轮已读取工具：{", ".join(dict.fromkeys(tools))}。'
        if failed_tools:
            answer += f' 其中这些工具返回失败，相关业务事实仍应视为未知：{", ".join(dict.fromkeys(failed_tools))}。'
        return {
            'answer': answer,
            'rationale': f'Fallback answer generated from durable Case context because final model output was not valid JSON: {parse_error or "unknown_parse_error"}.',
            'used_evidence': ['case_context.current_state', 'case_context.last_failure'] + [f'tool_observation:{tool}' for tool in dict.fromkeys(tools)],
            'used_tools': list(dict.fromkeys(tools)),
            'safe_next_steps': ['Use /events or case show to inspect the detailed trace.', 'If the Case is in manual_review, resolve the listed blocker before re-running automation.'],
            'parse_error': parse_error,
            'fallback': 'case_context_summary',
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

    @staticmethod
    def _is_light_conversation(question: str) -> bool:
        normalized = ''.join(str(question or '').lower().split())
        if not normalized:
            return False
        exact = {
            '你好', '您好', 'hi', 'hello', 'hey',
            '你是谁', '你是谁？', '你是什么', '你是什么？',
            '你能做什么', '你能做什么？', '你可以做什么', '你可以做什么？',
            '介绍一下你自己', '介绍下你自己', '你是什么模型', '你是什么模型？',
        }
        if normalized in exact:
            return True
        prefixes = ('你好', '您好', 'hi', 'hello')
        identity_markers = ('你是谁', '你能做什么', '你可以做什么', '介绍一下', '介绍下', '什么模型', '底层llm', 'llm', '模型', 'model')
        return any(normalized.startswith(item) for item in prefixes) or any(item in normalized for item in identity_markers)
