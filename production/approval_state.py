from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utc_now() -> datetime:
    return datetime.now(UTC)


def approval_expiry(ttl_seconds: int) -> datetime:
    return utc_now() + timedelta(seconds=max(1, ttl_seconds))


def approval_is_expired(approval, now: datetime | None = None) -> bool:
    expires_at = getattr(approval, 'expires_at', None)
    if expires_at is None:
        return False
    current = now or utc_now()
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= current
