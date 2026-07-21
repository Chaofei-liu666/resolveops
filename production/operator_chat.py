"""Operator-level no-tool chat.

This is the entry conversation before an operator selects or creates a Case.
It deliberately exposes no ERP tools and performs no business writes.  The
server still owns the LLM call so CLI clients do not need LLM credentials.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .config import settings
from .llm_gateway import LLMGateway, LLMResult


OPERATOR_CHAT_SYSTEM = """You are ResolveOps, an enterprise Agent for order fulfillment exception handling.
You are in the operator-level chat before a specific Case is selected.
This top-level chat is allowed to behave like a normal assistant for harmless no-tool conversation: explanation, writing, lightweight math, technical discussion, project Q&A, and creative requests are allowed.
Boundary: no ERP tools are available here, no business writes are allowed, and you must not claim live ERP or Case facts that were not provided.
Do not force every answer back to order handling. Mention /new or /case <case-id> only when the user asks to create/analyze a business exception or when it is genuinely relevant.
If the operator asks what underlying model is configured, use the provided configured_model and configured_base_url values.
Never reveal API keys or secrets.
Use the provided recent conversation history to understand references like "刚刚", "继续", and "为什么".
Keep answers concise and practical."""


def is_model_identity_question(question: str) -> bool:
    normalized = question.lower().strip()
    markers = (
        '底层模型',
        '什么模型',
        '模型是什么',
        'llm',
        'model',
        'base_url',
        'api配置',
        'api 配置',
    )
    return any(marker in normalized for marker in markers)


def configured_model_answer(question: str) -> dict[str, Any]:
    model = settings.llm_model or 'not configured'
    base_url = settings.llm_base_url or 'not configured'
    return {
        'question': question,
        'answer': (
            f'当前 ResolveOps 服务端配置的底层模型是 `{model}`，LLM 接口地址是 `{base_url}`。'
            '这些值来自服务端环境变量或 .env 配置。出于安全原因，我不会显示 LLM_API_KEY。'
        ),
        'source': 'system_config',
        'tools_used': [],
        'llm': {'status': 'not_called', 'reason': 'answered_from_resolveops_llm_config'},
    }


def normalize_chat_history(history: list[dict[str, str]] | None, *, max_items: int = 12) -> list[dict[str, str]]:
    """Keep a small, safe sliding window for top-level no-tool chat."""
    normalized: list[dict[str, str]] = []
    for item in (history or [])[-max_items:]:
        if not isinstance(item, dict):
            continue
        role = item.get('role')
        content = item.get('content')
        if role not in {'user', 'assistant'} or not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        normalized.append({'role': role, 'content': content[:2000]})
    return normalized


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
    if '诗' in normalized or 'poem' in normalized:
        return (
            '可以。这里是一首短诗：\n'
            '订单在夜色里等待，\n'
            '库存与承诺隔着一程山海；\n'
            '证据点亮下一步路，\n'
            '让异常也能被稳妥安排。'
        )
    return (
        '这是 ResolveOps 顶层对话窗口。当前不绑定具体 Case，也不会调用业务工具。'
        '如果要创建异常，请输入 /new；如果要分析某个 Case，请输入 /case <case-id>；'
        '如果要查看已有 Case，请输入 /cases。普通无工具问题也可以直接问。'
    )


class OperatorChatAgent:
    def __init__(self, llm_gateway: LLMGateway | None = None) -> None:
        self.llm = llm_gateway or LLMGateway()

    def answer(self, question: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
        safe_history = normalize_chat_history(history)
        if is_model_identity_question(question):
            answer = configured_model_answer(question)
            answer['history_items'] = len(safe_history)
            return answer
        messages: list[dict[str, str]] = [
            {'role': 'system', 'content': OPERATOR_CHAT_SYSTEM},
            {
                'role': 'user',
                'content': (
                    f'current_date={date.today().isoformat()}\n'
                    f'configured_model={settings.llm_model or "not configured"}\n'
                    f'configured_base_url={settings.llm_base_url or "not configured"}\n'
                    'Context: this is top-level ResolveOps chat. No tools are available here.'
                ),
            },
        ]
        messages.extend(safe_history)
        messages.append({'role': 'user', 'content': question})
        result = self.llm.chat({
            'messages': messages,
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
            'history_items': len(safe_history),
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
