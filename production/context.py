"""Case-scoped context assembly for the investigation agent.

This module is the isolation boundary for concurrent Cases.  The builder takes
one case_id and only reads state linked to that Case.  Long-term memory can be
added later as a separate, explicitly scoped input; it must not be mixed with
durable Case execution state.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .memory import relevant_lessons_for_case
from .models import Approval, Case, Event, Invocation, Task


def _compact_events(events: list[Event], limit: int = 12) -> list[dict[str, Any]]:
    recent = events[-limit:]
    return [
        {
            'kind': event.kind,
            'message': event.message,
            'data': event.data or {},
            'created_at': event.created_at.isoformat() if event.created_at else None,
        }
        for event in recent
    ]


def _tool_observations(case: Case) -> list[dict[str, Any]]:
    evidence = case.evidence if isinstance(case.evidence, dict) else {}
    observations = evidence.get('observations') if isinstance(evidence, dict) else []
    return observations if isinstance(observations, list) else []


def build_case_context(
    case: Case,
    events: list[Event],
    approvals: list[Approval],
    invocations: list[Invocation],
    tasks: list[Task],
    task_context: dict[str, Any] | None = None,
    lessons: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the only runtime context allowed into the LLM for a Case.

    The returned payload intentionally contains identifiers that prove scope:
    case_id, tenant_id, order_id, plan_version, and task case_ids.  Tests can
    assert these to prevent accidental cross-Case leakage.
    """
    task_context = task_context or {}
    lessons = lessons or []
    observations = _tool_observations(case)
    event_kinds = [event.kind for event in events]
    failed_or_replan_events = [
        event for event in events
        if event.kind in {'replan_requested', 'verification_failed', 'worker_failure', 'handoff', 'manual_review_required'}
    ]
    pending_approvals = [approval for approval in approvals if approval.status == 'pending']
    approved_approvals = [approval for approval in approvals if approval.status == 'approved']
    consumed_approvals = [approval for approval in approvals if approval.status == 'consumed']

    return {
        'scope': {
            'case_id': case.id,
            'tenant_id': case.tenant_id,
            'event_type': case.event_type,
            'order_id': case.order_id,
            'plan_version': case.plan_version,
        },
        'current_state': {
            'status': case.status,
            'pending_approval_count': len(pending_approvals),
            'approved_but_not_executed_count': len(approved_approvals),
            'consumed_approval_count': len(consumed_approvals),
            'write_invocation_count': len(invocations),
            'task_attempts': sum(task.attempts or 0 for task in tasks),
        },
        'task_context': {
            'reason': task_context.get('reason'),
            'previous_plan': task_context.get('previous_plan'),
        },
        'confirmed_observations': observations,
        'previous_plan': case.plan,
        'last_failure': {
            'kind': failed_or_replan_events[-1].kind,
            'message': failed_or_replan_events[-1].message,
            'data': failed_or_replan_events[-1].data or {},
        } if failed_or_replan_events else None,
        'recent_events': _compact_events(events),
        'approval_refs': [
            {
                'approval_id': approval.id,
                'case_id': approval.case_id,
                'plan_version': approval.plan_version,
                'status': approval.status,
                'required_roles': approval.required_roles,
                'approved_roles': approval.approved_roles,
                'action_hash': approval.action_hash,
            }
            for approval in approvals
        ],
        'invocation_refs': [
            {
                'invocation_id': invocation.id,
                'case_id': invocation.case_id,
                'tool': invocation.tool,
                'status': invocation.status,
                'external_id': invocation.external_id,
                'idempotency_key': invocation.idempotency_key,
            }
            for invocation in invocations
        ],
        'task_refs': [
            {
                'task_id': task.id,
                'case_id': task.case_id,
                'kind': task.kind,
                'status': task.status,
                'attempts': task.attempts,
                'last_error': task.last_error,
            }
            for task in tasks
        ],
        'event_kinds': event_kinds,
        'long_term_memory': {
            'type': 'verified_case_lessons',
            'usage_rule': 'Lessons are planning hints only. They never replace live ERP reads, policy checks, approvals, idempotency, or verification.',
            'lessons': lessons,
        },
        'isolation': {
            'rule': 'Durable execution records must have record.case_id == scope.case_id. Long-term lessons must have lesson.tenant_id == scope.tenant_id and are planning hints only.',
            'case_ids_present': sorted({
                case.id,
                *[event.case_id for event in events],
                *[approval.case_id for approval in approvals],
                *[invocation.case_id for invocation in invocations],
                *[task.case_id for task in tasks],
            }),
            'lesson_tenant_ids_present': sorted({lesson.get('tenant_id') for lesson in lessons if lesson.get('tenant_id')}),
        },
    }


class CaseContextBuilder:
    def __init__(self, db: Session):
        self.db = db

    def build(self, case_id: str, task_context: dict[str, Any] | None = None) -> dict[str, Any]:
        case = self.db.get(Case, case_id)
        if case is None:
            raise ValueError(f'case not found: {case_id}')
        events = self.db.scalars(select(Event).where(Event.case_id == case_id).order_by(Event.created_at)).all()
        approvals = self.db.scalars(select(Approval).where(Approval.case_id == case_id).order_by(Approval.plan_version, Approval.id)).all()
        invocations = self.db.scalars(select(Invocation).where(Invocation.case_id == case_id)).all()
        tasks = self.db.scalars(select(Task).where(Task.case_id == case_id)).all()
        lessons = relevant_lessons_for_case(self.db, case)
        return build_case_context(case, events, approvals, invocations, tasks, task_context, lessons)
