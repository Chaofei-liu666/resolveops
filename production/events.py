"""Durable event sink shared by API ingress and background workers."""
from __future__ import annotations

from typing import Any

from .models import Event


def emit(db, case_id: str, kind: str, message: str, data: dict[str, Any] | None = None) -> None:
    db.add(Event(case_id=case_id, kind=kind, message=message, data=data or {}))
