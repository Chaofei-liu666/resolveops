"""Explicit boundary between durable Agent context and provider messages."""
from __future__ import annotations

import json
from datetime import date
from typing import Any

_VISIBLE_ROLES = {'system', 'user', 'assistant', 'tool'}


def convert_to_llm_messages(
    *,
    system_prompt: str,
    case_context: dict[str, Any] | None = None,
    user_message: str | None = None,
    transcript: list[dict[str, Any]] | None = None,
    instruction: str | None = None,
) -> list[dict[str, Any]]:
    """Serialize only provider-standard messages.

    CaseContext, scheduler data and UI-only state stay Python data until this
    boundary.  This prevents a future runtime/UI detail from silently becoming
    an LLM instruction.
    """
    messages: list[dict[str, Any]] = [{'role': 'system', 'content': system_prompt}]
    for item in transcript or []:
        if not isinstance(item, dict) or item.get('role') not in _VISIBLE_ROLES:
            continue
        content = item.get('content')
        if isinstance(content, (str, list)):
            clean = {'role': item['role'], 'content': content}
            if item['role'] == 'tool' and item.get('tool_call_id'):
                clean['tool_call_id'] = str(item['tool_call_id'])
            messages.append(clean)
    if user_message is not None or case_context is not None:
        payload: dict[str, Any] = {'current_date': date.today().isoformat()}
        if user_message is not None:
            payload['question'] = user_message
        if case_context is not None:
            payload['case_context'] = case_context
        if instruction:
            payload['instruction'] = instruction
        messages.append({'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)})
    return messages
