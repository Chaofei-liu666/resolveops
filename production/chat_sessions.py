"""Persistence helpers for the operator chat workspace.

Chat messages belong to one operator and tenant through their parent session.
Attachments are represented by metadata only; their bytes stay in the request.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import ChatMemory, ChatMessage, ChatSession


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def redact(value: str) -> str:
    value = str(value or '').replace('\x00', '')
    value = re.sub(r'(?im)\b(api[_ -]?key|secret|password|passwd|token)\s*([:=])\s*([^\s,;]+)', r'\1\2[已隐藏]', value)
    value = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]{12,}', 'Bearer [已隐藏]', value)
    return value.strip()


def session_for_identity(db: Session, session_id: str, *, tenant_id: str, subject: str) -> ChatSession | None:
    return db.scalar(select(ChatSession).where(
        ChatSession.id == session_id,
        ChatSession.tenant_id == tenant_id,
        ChatSession.operator_subject == subject,
    ))


def session_out(session: ChatSession, *, message_count: int = 0, memory: ChatMemory | None = None) -> dict[str, Any]:
    return {
        'id': session.id,
        'title': session.title,
        'summary': session.summary or '',
        'message_count': message_count,
        'memory_saved': memory is not None or session.memory_saved_at is not None,
        'created_at': _iso(session.created_at),
        'updated_at': _iso(session.updated_at),
    }


def message_out(message: ChatMessage) -> dict[str, Any]:
    return {
        'id': message.id,
        'role': message.role,
        'content': message.content,
        'route': message.route,
        'references': message.references or {},
        'attachments': message.attachments or [],
        'created_at': _iso(message.created_at),
    }


def recent_history(db: Session, session_id: str, *, limit: int = 12) -> list[dict[str, str]]:
    messages = list(db.scalars(
        select(ChatMessage).where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.desc()).limit(limit)
    ).all())
    messages.reverse()
    return [{'role': item.role, 'content': item.content} for item in messages if item.role in {'user', 'assistant'}]


def add_message(
    db: Session,
    session: ChatSession,
    *,
    role: str,
    content: str,
    route: str | None = None,
    references: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> ChatMessage:
    message = ChatMessage(
        session_id=session.id,
        role=role,
        content=redact(content),
        route=route,
        references=references or {},
        attachments=attachments or [],
    )
    db.add(message)
    session.updated_at = datetime.now(UTC)
    return message


def update_summary(session: ChatSession, messages: list[dict[str, str]]) -> None:
    """Keep a short local context cue; full history is still restored separately."""
    turns = [item for item in messages if item.get('role') == 'user'][-3:]
    if not turns:
        return
    summary = '；'.join(redact(item.get('content', ''))[:140] for item in turns if item.get('content'))
    session.summary = summary[:500]


def delete_session(db: Session, session: ChatSession, *, delete_memory: bool) -> None:
    db.execute(delete(ChatMessage).where(ChatMessage.session_id == session.id))
    if delete_memory:
        db.execute(delete(ChatMemory).where(ChatMemory.session_id == session.id))
    db.delete(session)


def active_memories(db: Session, *, tenant_id: str, subject: str, exclude_session_id: str) -> list[ChatMemory]:
    return list(db.scalars(
        select(ChatMemory).where(
            ChatMemory.tenant_id == tenant_id,
            ChatMemory.operator_subject == subject,
            ChatMemory.session_id != exclude_session_id,
        ).order_by(ChatMemory.updated_at.desc()).limit(20)
    ).all())
