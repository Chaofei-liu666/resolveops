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
from .agent_core import LLMRequest


OPERATOR_CHAT_SYSTEM = """You are ResolveOps, an enterprise Agent Workbench assistant for order fulfillment exception cases.
You are in the operator-level chat before a specific Case is selected.
This top-level chat is allowed to answer normal harmless questions as well as non-sensitive ResolveOps project questions: architecture, Agent loop, tool contracts, scheduling, persistence, approval controls, desktop runtime, testing, and deployment choices.
Use the supplied ResolveOps implementation notes as authoritative context. Do not claim implementation details are unavailable merely because they are technical.
Boundary: this chat has no ERP write tools. For live ERP or Case facts, rely only on data supplied by the request or the separately-routed read-only analytics result; never invent facts.
Do not force every answer back to order handling. Mention /new or /focus <case-id> only when the user asks to create/analyze a business exception or when it is genuinely relevant.
If the operator asks what underlying model is configured, use the provided configured_model and configured_base_url values.
Never reveal API keys, passwords, tokens, private keys, connection strings containing credentials, or other secret values. You may explain how secrets are stored, configured, and protected without printing their values.
Use the provided recent conversation history to understand references like "刚刚", "继续", and "为什么".
Keep answers concise and practical."""


RESOLVEOPS_IMPLEMENTATION_NOTES = """ResolveOps implementation notes:
- The desktop app is Electron. It starts a local FastAPI API, a Worker, and a SQLite database. Source development also defaults to SQLite; a server deployment can use PostgreSQL through SQLAlchemy.
- The domain records are Case, Task, Event, Approval, Invocation and related execution/audit data. Case state, plan versions, approvals and event history are persisted, so a restarted runtime can resume from saved state.
- A Worker owns the case lifecycle: it builds a context snapshot, runs the LLM-driven read/tool loop, records observations and decision traces, grounds proposed actions in evidence, checks policy, requests human approval, executes approved actions with idempotency protection, then reads back ERP state for verification. Changes or failures can request re-planning or hand off to a human.
- The LLM is not given arbitrary ERP or database access. Tools are typed and schema-validated. Business writes are proposed as an Action Plan and are guarded by evidence, policy and approval; ERP writes are not performed by the top-level chat.
- Independent read-only tool calls within one Agent turn are scheduled with Python concurrent.futures.ThreadPoolExecutor (default maximum four). Duplicate calls are cached by tool name and arguments. Dependent calls and writes remain ordered.
- The main chat recognizes questions about operational records plus an analysis intent such as count, list, trend, grouping, sorting or recent history. Those questions go to a read-only analytics chain: LLM generates scoped SELECT/WITH SQL over tenant-filtered semantic views, the server validates it and caps results at 100 rows, then a second LLM call summarizes the result. Generated SQL and row count are audited.
- The assistant may explain all of the above and other non-sensitive project design details. It must never expose secret values or pretend it has live facts without a read-only query result."""


def chat_system_prompt() -> str:
    return f'{OPERATOR_CHAT_SYSTEM}\n\n{RESOLVEOPS_IMPLEMENTATION_NOTES}'


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
            '我是 ResolveOps，一个用于订单履约异常处理的 Agent Workbench。'
            '你可以让我解释项目、创建新 Case，或显式选择某个 Case 后分析异常。'
        )
    if '能做什么' in normalized or 'what can you do' in normalized:
        return (
            '我可以解释 ResolveOps 的实现与运行机制，包括 Agent Loop、工具调用、并发、状态持久化、审批和桌面运行时；'
            '也可以回答运行数据问题，系统会自动转到只读分析。涉及密码、密钥和 Token 的具体值不会显示。'
        )
    if any(token in normalized for token in {'agent loop', 'agent循环', 'agent loop', '工具调用', '并发', '状态持久化', '记忆', '架构', '实现'}):
        return (
            'ResolveOps 的 Worker 围绕 Case 运行：构建上下文后，LLM 通过受 schema 校验的只读工具收集事实，'
            '将 Observation、计划和事件写入数据库；方案通过证据校验、策略与人工审批后，Executor 才会用幂等键写入 ERP，并回读验证。'
            '同一轮无依赖的只读工具调用会用 ThreadPoolExecutor 并发调度，重复调用会复用缓存；写操作保持有序。'
        )
    if '项目' in normalized or '干什么' in normalized or 'resolveops' in normalized:
        return (
            'ResolveOps 用来处理订单履约异常。它围绕业务 Case 收集证据、规划方案，'
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
        '这是 ResolveOps 的主对话。你可以直接问普通问题，或询问 Agent、工具调用、并发、状态持久化、审批和桌面端实现。'
        '涉及运营数据的统计、列表或趋势问题会自动走只读分析；涉及创建或修改业务数据时，需要进入具体 Case 并经过既有审批流程。'
        '密码、密钥和 Token 等敏感值不会显示。'
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
            {'role': 'system', 'content': chat_system_prompt()},
            {
                'role': 'user',
                'content': (
                    f'current_date={date.today().isoformat()}\n'
                    f'configured_model={settings.llm_model or "not configured"}\n'
                    f'configured_base_url={settings.llm_base_url or "not configured"}\n'
                    'Context: this is the top-level chat. It can answer non-sensitive project questions. '
                    'Live operational analytics are separately routed by the API.'
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

    def stream_answer(self, question: str, history: list[dict[str, str]] | None = None):
        """Stream only the visible General-chat reply, never a business tool trace."""
        safe_history = normalize_chat_history(history)
        if is_model_identity_question(question):
            answer = configured_model_answer(question)['answer']
            yield {'type': 'start', 'source': 'system_config'}
            yield {'type': 'delta', 'text': answer}
            yield {'type': 'done', 'answer': answer, 'source': 'system_config', 'tools_used': []}
            return
        messages: list[dict[str, str]] = [
            {'role': 'system', 'content': chat_system_prompt()},
            {'role': 'user', 'content': (
                f'current_date={date.today().isoformat()}\n'
                f'configured_model={settings.llm_model or "not configured"}\n'
                f'configured_base_url={settings.llm_base_url or "not configured"}\n'
                'Context: this is the top-level chat. It can answer non-sensitive project questions. '
                'Live operational analytics are separately routed by the API.'
            )},
            *safe_history,
            {'role': 'user', 'content': question},
        ]
        emitted = False
        for event in self.llm.stream_chat(LLMRequest(messages=messages, temperature=0.3).to_payload()):
            if event.get('type') == 'delta':
                emitted = True
            if event.get('type') == 'done' and not emitted and not str(event.get('answer') or '').strip():
                event = {'type': 'error', 'error_code': 'empty_llm_answer'}
            if event.get('type') == 'error' and not emitted:
                fallback = fallback_operator_answer(question)
                yield {'type': 'start', 'source': 'fallback'}
                yield {'type': 'delta', 'text': fallback}
                yield {'type': 'done', 'answer': fallback, 'source': 'fallback', 'tools_used': []}
                return
            yield event

    @staticmethod
    def _fallback(question: str, result: LLMResult) -> dict[str, Any]:
        return {
            'question': question,
            'answer': fallback_operator_answer(question),
            'source': 'fallback',
            'tools_used': [],
            'llm': result.telemetry(),
        }
