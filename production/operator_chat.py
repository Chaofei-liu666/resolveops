"""Operator-level no-tool chat.

This is the entry conversation before an operator selects or creates a Case.
It deliberately exposes no ERP tools and performs no business writes.  The
server still owns the LLM call so CLI clients do not need LLM credentials.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .llm_gateway import LLMGateway, LLMResult


OPERATOR_CHAT_SYSTEM = """You are ResolveOps, an enterprise Agent for order fulfillment exception handling.
You are in the operator-level chat before a specific Case is selected.
You may answer naturally about ResolveOps, Agent concepts, project usage, and general conversation.
Do not call tools. Do not claim live ERP facts. Do not invent Case state.
If the operator wants to create an exception, tell them to use /new.
If the operator wants to analyze an existing business exception, tell them to use /case <case-id>.
Keep answers concise and practical."""


def fallback_operator_answer(question: str) -> str:
    """Deterministic fallback for local setups without LLM credentials."""
    normalized = question.lower().strip()
    if any(token in normalized for token in {'你好', 'hello', 'hi', '你是谁'}):
        return (
            '我是 ResolveOps，一个面向订单履约异常处理的企业级 Agent。'
            '你可以让我解释项目、查看 Case、创建新 Case，或进入某个 Case 后分析异常。'
        )
    if '能做什么' in normalized or 'what can you do' in normalized:
        return (
            '我可以处理三类入口任务：解释 ResolveOps 的设计和能力；'
            '通过 /new 创建订单异常 Case；通过 /case <case-id> 进入具体 Case，'
            '查看工具调用、证据、审批、执行和验证过程。'
        )
    if '项目' in normalized or '干什么' in normalized or 'resolveops' in normalized:
        return (
            'ResolveOps 是订单履约异常处置 Agent。它围绕业务 Case 收集证据、规划方案，'
            '经过权限和审批控制后执行动作，并做结果验证。'
        )
    if '客服' in normalized or '区别' in normalized:
        return (
            '普通客服机器人主要回答问题；ResolveOps 的核心是处理业务异常 Case。'
            '它会调用企业系统只读工具收集证据，并在受控审批后执行写操作，最后验证真实业务状态。'
        )
    return (
        '这是 ResolveOps 顶层对话窗口。当前不绑定具体 Case，也不会调用业务工具。'
        '如果要创建异常，请输入 /new；如果要分析某个 Case，请输入 /case <case-id>；'
        '如果要查看已有 Case，请输入 /cases。'
    )


class OperatorChatAgent:
    def __init__(self, llm_gateway: LLMGateway | None = None) -> None:
        self.llm = llm_gateway or LLMGateway()

    def answer(self, question: str) -> dict[str, Any]:
        result = self.llm.chat({
            'messages': [
                {'role': 'system', 'content': OPERATOR_CHAT_SYSTEM},
                {
                    'role': 'user',
                    'content': (
                        f'current_date={date.today().isoformat()}\n'
                        f'operator_question={question}\n'
                        'Answer as ResolveOps. No tools are available in this top-level chat.'
                    ),
                },
            ],
            'temperature': 0.3,
        })
        if not result.ok:
            return self._fallback(question, result)
        content = (result.first_message() or {}).get('content')
        if not isinstance(content, str) or not content.strip():
            return self._fallback(
                question,
                LLMResult(
                    status='failed',
                    error_code='empty_llm_answer',
                    error_type='EmptyAnswer',
                    retryable=False,
                    model=result.model,
                    latency_ms=result.latency_ms,
                    usage=result.usage,
                ),
            )
        return {
            'question': question,
            'answer': content.strip(),
            'source': 'llm',
            'tools_used': [],
            'llm': result.telemetry(),
        }

    @staticmethod
    def _fallback(question: str, result: LLMResult) -> dict[str, Any]:
        return {
            'question': question,
            'answer': fallback_operator_answer(question),
            'source': 'fallback',
            'tools_used': [],
            'llm': result.telemetry(),
        }
