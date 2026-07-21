"""Standard runtime result envelope for ResolveOps tools.

The LLM still receives business-shaped data such as an order or inventory
record.  The runtime additionally records this envelope so replanning,
auditing, and error handling can reason about tool outcomes consistently.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ToolStatus = Literal['success', 'failed', 'partial', 'unknown']


@dataclass(frozen=True)
class ToolResult:
    status: ToolStatus
    data: dict[str, Any] | None = None
    error_code: str | None = None
    error_type: str | None = None
    retryable: bool = False
    side_effect_committed: bool | None = False
    verification_required: bool = False
    source_system: str | None = None
    source_version: str | None = None
    evidence_usable: bool = True
    metadata: dict[str, Any] | None = None

    @classmethod
    def success(
        cls,
        data: dict[str, Any] | None,
        *,
        source_system: str | None = None,
        source_version: str | None = None,
        verification_required: bool = False,
    ) -> 'ToolResult':
        return cls(
            status='success',
            data=data or {},
            source_system=source_system,
            source_version=source_version,
            verification_required=verification_required,
            evidence_usable=True,
        )

    @classmethod
    def failure(
        cls,
        error_code: str,
        *,
        error_type: str | None = None,
        retryable: bool = False,
        source_system: str | None = None,
        side_effect_committed: bool | None = False,
    ) -> 'ToolResult':
        return cls(
            status='failed',
            data=None,
            error_code=error_code,
            error_type=error_type,
            retryable=retryable,
            source_system=source_system,
            side_effect_committed=side_effect_committed,
            evidence_usable=False,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            'status': self.status,
            'data': self.data,
            'error_code': self.error_code,
            'error_type': self.error_type,
            'retryable': self.retryable,
            'side_effect_committed': self.side_effect_committed,
            'verification_required': self.verification_required,
            'source_system': self.source_system,
            'source_version': self.source_version,
            'evidence_usable': self.evidence_usable,
            'metadata': self.metadata or {},
        }

    def observation_result(self) -> dict[str, Any]:
        """Return the backward-compatible result stored under observation.result."""
        if self.status == 'success':
            return self.data or {}
        return {
            'error': self.error_code or self.status,
            'error_type': self.error_type,
            'retryable': self.retryable,
            'side_effect_committed': self.side_effect_committed,
        }
