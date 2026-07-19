"""Verified Case Lessons: lightweight long-term memory.

Lessons are not ERP facts and are not execution authority.  They are planning
hints generated only after a Case has a verified outcome.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Case, CaseLesson


def _case_observations(case: Case) -> list[dict[str, Any]]:
    evidence = case.evidence if isinstance(case.evidence, dict) else {}
    observations = evidence.get('observations') if isinstance(evidence, dict) else []
    return observations if isinstance(observations, list) else []


def _first_observation(case: Case, tool_name: str) -> dict[str, Any] | None:
    for observation in _case_observations(case):
        if observation.get('tool') == tool_name and not (observation.get('result') or {}).get('error'):
            return observation
    return None


def _order_entities(case: Case) -> dict[str, str | None]:
    order = (_first_observation(case, 'get_order') or {}).get('result') or {}
    item = (order.get('items') or [{}])[0] if isinstance(order.get('items'), list) else {}
    customer = order.get('customer') or order.get('customer_name')
    return {
        'customer_id': customer,
        'sku': item.get('item_code'),
        'target_warehouse': item.get('warehouse'),
    }


def _lesson_exists(db: Session, lesson: dict[str, Any]) -> bool:
    return db.scalar(
        select(CaseLesson).where(
            CaseLesson.tenant_id == lesson['tenant_id'],
            CaseLesson.lesson_type == lesson['lesson_type'],
            CaseLesson.subject_type == lesson['subject_type'],
            CaseLesson.subject_id == lesson['subject_id'],
            CaseLesson.evidence_case_id == lesson['evidence_case_id'],
            CaseLesson.source_action_type == lesson.get('source_action_type'),
        )
    ) is not None


def candidate_lessons_from_verified_action(case: Case, action: dict[str, Any], verification: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract deterministic lessons from a verified action.

    These are conservative by design.  The content describes a reusable
    planning hint, while data keeps the exact action/verification evidence.
    """
    if case.status != 'resolved':
        return []
    if not verification.get('verified'):
        return []

    action_type = action.get('action_type')
    action_input = action.get('input') or {}
    entities = _order_entities(case)
    lessons: list[dict[str, Any]] = []

    if action_type == 'transfer_stock':
        source = action_input.get('source')
        target = action_input.get('target') or entities.get('target_warehouse')
        sku = action_input.get('sku') or entities.get('sku')
        if source and target:
            lessons.append({
                'tenant_id': case.tenant_id,
                'lesson_type': 'resolution_pattern',
                'subject_type': 'lane',
                'subject_id': f'{source}->{target}',
                'content': f'Verified transfer draft from {source} to {target} resolved or contributed to resolving a shortage. Use as a planning hint only; re-read live inventory and transfer options before execution.',
                'evidence_case_id': case.id,
                'source_action_type': action_type,
                'confidence': 1.0,
                'status': 'active',
                'data': {'action_input': action_input, 'verification': verification, 'sku': sku},
            })
        if sku:
            lessons.append({
                'tenant_id': case.tenant_id,
                'lesson_type': 'operational_lesson',
                'subject_type': 'action_type',
                'subject_id': action_type,
                'content': 'Before executing inventory transfer, re-read source inventory and invalidate the approval if availability changed.',
                'evidence_case_id': case.id,
                'source_action_type': action_type,
                'confidence': 1.0,
                'status': 'active',
                'data': {'action_input': action_input, 'verification': verification, 'sku': sku},
            })

    if action_type == 'create_purchase_request':
        sku = action_input.get('sku') or entities.get('sku')
        required_by = action_input.get('required_by')
        if sku:
            lessons.append({
                'tenant_id': case.tenant_id,
                'lesson_type': 'resolution_pattern',
                'subject_type': 'sku',
                'subject_id': sku,
                'content': f'Verified purchase request draft for {sku} was useful when transfer alone was insufficient. Treat as planning experience; still validate lead time, inbound supply, and approval policy.',
                'evidence_case_id': case.id,
                'source_action_type': action_type,
                'confidence': 1.0,
                'status': 'active',
                'data': {'action_input': action_input, 'verification': verification, 'required_by': required_by},
            })

    customer_id = entities.get('customer_id')
    customer = (_first_observation(case, 'get_customer_profile') or {}).get('result') or {}
    if customer_id and customer.get('allows_partial_delivery') is not None:
        allowed = bool(customer.get('allows_partial_delivery'))
        lessons.append({
            'tenant_id': case.tenant_id,
            'lesson_type': 'customer_preference',
            'subject_type': 'customer',
            'subject_id': customer_id,
            'content': f'Customer {customer_id} {"allows" if allowed else "does not allow"} partial delivery according to verified case evidence. Re-check customer master data before using this hint.',
            'evidence_case_id': case.id,
            'source_action_type': action_type,
            'confidence': 0.9,
            'status': 'active',
            'data': {'customer_profile': customer, 'action_type': action_type},
        })

    return lessons


def record_verified_lessons(db: Session, case: Case, action: dict[str, Any], verification: dict[str, Any]) -> list[CaseLesson]:
    created: list[CaseLesson] = []
    for lesson_data in candidate_lessons_from_verified_action(case, action, verification):
        if _lesson_exists(db, lesson_data):
            continue
        lesson = CaseLesson(**lesson_data)
        db.add(lesson)
        db.flush()
        created.append(lesson)
    return created


def relevant_lessons_for_case(db: Session, case: Case, limit: int = 8) -> list[dict[str, Any]]:
    """Return active lessons scoped to the same tenant and related entities."""
    entities = _order_entities(case)
    subjects: list[tuple[str, str]] = [('action_type', 'transfer_stock'), ('action_type', 'create_purchase_request')]
    if entities.get('customer_id'):
        subjects.append(('customer', entities['customer_id']))
    if entities.get('sku'):
        subjects.append(('sku', entities['sku']))
    if entities.get('target_warehouse'):
        subjects.append(('warehouse', entities['target_warehouse']))

    if not subjects:
        return []

    rows = []
    for subject_type, subject_id in subjects:
        rows.extend(
            db.scalars(
                select(CaseLesson).where(
                    CaseLesson.tenant_id == case.tenant_id,
                    CaseLesson.status == 'active',
                    CaseLesson.subject_type == subject_type,
                    CaseLesson.subject_id == subject_id,
                ).order_by(CaseLesson.created_at.desc()).limit(limit)
            ).all()
        )

    deduped: dict[str, CaseLesson] = {}
    for row in rows:
        deduped[row.id] = row

    return [
        {
            'lesson_id': lesson.id,
            'tenant_id': lesson.tenant_id,
            'lesson_type': lesson.lesson_type,
            'subject_type': lesson.subject_type,
            'subject_id': lesson.subject_id,
            'content': lesson.content,
            'evidence_case_id': lesson.evidence_case_id,
            'source_action_type': lesson.source_action_type,
            'confidence': lesson.confidence,
            'status': lesson.status,
            'data': lesson.data or {},
        }
        for lesson in list(deduped.values())[:limit]
    ]
