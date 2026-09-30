"""Ingress API: authenticated webhooks create durable Cases; no ERP writes here."""
from __future__ import annotations
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib, hmac, json
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .config import connection_value, save_runtime_connections, secret_configured, settings
from .database import create_database_engine, is_sqlite_url
from .approval_state import approval_is_expired, utc_now
from .migrations import apply_migrations
from .models import AuditLog, Base, Approval, Case, ChatMemory, ChatMessage, ChatSession, Event, Invocation, LogisticsLane, Operator, Task
from .runtime_status import build_runtime_status
from .tool_trace import build_tool_trace
from .erpnext import ERPNextAdapter
from .case_ask import CaseQuestionAgent
from .context import CaseContextBuilder, validate_case_context_isolation
from .evidence import validate_plan_grounding
from .operator_chat import OperatorChatAgent
from .analytics import AnalyticsAgent, AnalyticsQueryError, is_analytics_question
from .chat_attachments import AttachmentError, ChatAttachment, decode_attachments
from .local_file_tool import LocalFileReadTool, is_local_file_question
from .chat_sessions import active_memories, add_message, delete_session, message_out, recent_history, redact, session_for_identity, session_out, update_summary
from .erp_chat_tools import ERPWorkspaceReadTools
from .tools import BusinessReadTools
from .events import emit

SUPPORTED_EVENTS={'inventory_shortage','price_mismatch','delivery_delay','supplier_delay'}

class LogisticsLaneIn(BaseModel):
    tenant_id: str = Field(default='demo', min_length=1, max_length=80)
    source_warehouse: str = Field(min_length=1, max_length=140)
    target_warehouse: str = Field(min_length=1, max_length=140)
    transit_days: float = Field(gt=0)
    cost_per_unit: float = Field(ge=0)
    currency: str = Field(default='CNY', min_length=1, max_length=12)
    active: bool = True

class ApprovalRevokeIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)

class ApprovalRejectIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)

class CaseCreateIn(BaseModel):
    tenant_id: str = Field(default='demo', min_length=1, max_length=80)
    event_type: str = Field(min_length=1, max_length=80)
    order_id: str = Field(min_length=1, max_length=160)
    source_event_id: str | None = Field(default=None, max_length=160)
    reason: str | None = Field(default=None, max_length=500)
    context: dict[str, Any] = Field(default_factory=dict)

class CaseAskIn(BaseModel):
    question: str = Field(min_length=1, max_length=1000)

class OperatorChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    history: list[dict[str, str]] = Field(default_factory=list)
    session_id: str | None = Field(default=None, max_length=64)

class ChatAttachmentIn(BaseModel):
    filename: str = Field(min_length=1, max_length=260)
    content_type: str = Field(default='', max_length=120)
    data_base64: str = Field(min_length=1, max_length=7_000_000)

class OperatorChatAttachmentIn(OperatorChatIn):
    attachments: list[ChatAttachmentIn] = Field(min_length=1, max_length=5)

class ChatSessionCreateIn(BaseModel):
    title: str | None = Field(default=None, max_length=160)

class ChatSessionRenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=160)

class ChatSessionDeleteIn(BaseModel):
    delete_memory: bool = False

class AnalyticsQueryIn(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


class ConnectionSettingsIn(BaseModel):
    """Partial local connection profile. Empty inputs never erase a secret."""
    llm_base_url: str | None = Field(default=None, max_length=500)
    llm_api_key: str | None = Field(default=None, max_length=1000)
    llm_model: str | None = Field(default=None, max_length=200)
    llm_timeout_seconds: float | None = Field(default=None, ge=1, le=300)
    erpnext_base_url: str | None = Field(default=None, max_length=500)
    erpnext_api_key: str | None = Field(default=None, max_length=1000)
    erpnext_api_secret: str | None = Field(default=None, max_length=1000)
    database_url: str | None = Field(default=None, max_length=1000)


class ConnectionTestIn(ConnectionSettingsIn):
    target: Literal['llm', 'erpnext', 'database']

class FaultInjectionRunIn(BaseModel):
    fault_type: Literal['inventory_changed_before_execution']
    case_id: str | None = Field(default=None, max_length=120)
    item_code: str = Field(min_length=1, max_length=140)
    warehouse: str = Field(min_length=1, max_length=140)
    new_qty: float = Field(ge=0)
    company: str | None = Field(default=None, min_length=1, max_length=140)
    difference_account: str | None = Field(default=None, min_length=1, max_length=140)
    valuation_rate: float | None = Field(default=None, ge=0)
    reason: str | None = Field(default=None, max_length=500)

class SandboxSeedIn(BaseModel):
    tenant_id: str = Field(default='demo', min_length=1, max_length=80)
    order_id: str = Field(default='SAL-ORD-2026-00002', min_length=1, max_length=160)
    item_code: str = Field(default='SKU-A12', min_length=1, max_length=140)
    customer: str = Field(default='恒远科技', min_length=1, max_length=140)
    source_warehouse: str = Field(default='重庆仓 - ROPS', min_length=1, max_length=140)
    target_warehouse: str = Field(default='Stores - ROPS', min_length=1, max_length=140)
    source_qty: float = Field(default=40, ge=0)
    target_qty: float = Field(default=0, ge=0)
    transit_days: float = Field(default=1, gt=0)
    cost_per_unit: float = Field(default=8, ge=0)
    company: str | None = Field(default=None, min_length=1, max_length=140)
    difference_account: str | None = Field(default=None, min_length=1, max_length=140)
    valuation_rate: float | None = Field(default=None, ge=0)
    set_stock: bool = True

@dataclass(frozen=True)
class OperatorIdentity:
    subject: str
    role: str
    tenant_id: str = 'demo'

engine=create_database_engine(settings.database_url)
STATIC_DIR=Path(__file__).resolve().parent.parent/'static'

def bootstrap_schema():
    with engine.begin() as db:
        if db.dialect.name == 'postgresql':
            db.execute(text("SELECT pg_advisory_lock(hashtext('resolveops_schema_bootstrap'))"))
        try:
            Base.metadata.create_all(db)
            # SQLite desktop databases are always new local stores created
            # from the current models. PostgreSQL keeps the versioned SQL
            # migration chain needed by long-lived deployment databases.
            if db.dialect.name == 'postgresql':
                apply_migrations(db)
            seed_default_operator(db)
        finally:
            if db.dialect.name == 'postgresql':
                db.execute(text("SELECT pg_advisory_unlock(hashtext('resolveops_schema_bootstrap'))"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap_schema()
    yield

app=FastAPI(title='ResolveOps', version='1.0.0', lifespan=lifespan)
if STATIC_DIR.exists():
    app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')

def sse_event(event: str, data: dict[str, Any]) -> str:
    """Narrow SSE envelope used by the Workbench; never serialize hidden prompts."""
    return f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n'

def sse_response(events):
    return StreamingResponse(events, media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
def audit(db, identity: OperatorIdentity, action: str, resource_type: str, resource_id: str, data=None, case_id: str|None=None):
    db.add(AuditLog(actor=identity.subject,role=identity.role,action=action,resource_type=resource_type,resource_id=resource_id,case_id=case_id,data={**(data or {}),'tenant_id':identity.tenant_id}))
def operator_key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()
def seed_default_operator(db):
    """Local bootstrap only. Production should provision operators through IAM/admin workflow."""
    if not settings.operator_api_key:
        return
    seeds=[('local-ops-admin','ops_admin',settings.operator_api_key)]
    for raw_seed in (settings.operator_seed_keys or '').split(';'):
        if not raw_seed.strip():
            continue
        parts=raw_seed.split(':',2)
        if len(parts) != 3 or not all(parts):
            continue
        seeds.append((parts[0],parts[1],parts[2]))
    for subject, role, key in seeds:
        key_hash=operator_key_hash(key)
        db.execute(
            text(
                """
                INSERT INTO operators(id, tenant_id, subject, role, api_key_hash, status)
                VALUES (:id, 'demo', :subject, :role, :api_key_hash, 'active')
                ON CONFLICT (api_key_hash)
                DO UPDATE SET subject = EXCLUDED.subject, role = EXCLUDED.role, status = 'active'
                """
            ),
            {'id':str(uuid4()),'subject':subject,'role':role,'api_key_hash':key_hash},
        )
def operator_identity_from_db(db, key: str|None) -> OperatorIdentity:
    if not key:
        raise HTTPException(401, 'operator authentication failed')
    key_hash=operator_key_hash(key)
    operator=db.scalar(select(Operator).where(Operator.api_key_hash==key_hash, Operator.status=='active'))
    if not operator:
        raise HTTPException(401, 'operator authentication failed')
    return OperatorIdentity(subject=operator.subject, role=operator.role, tenant_id=operator.tenant_id)
def operator_identity(key: str|None, subject: str|None=None, role: str|None=None) -> OperatorIdentity:
    """Legacy compatibility for unit tests only; request role is not trusted by API routes."""
    if not key or not hmac.compare_digest(key, settings.operator_api_key): raise HTTPException(401, 'operator authentication failed')
    return OperatorIdentity(subject=subject or 'authenticated-operator', role=role or 'operator')
def require_role(identity: OperatorIdentity, *roles: str) -> None:
    if identity.role not in roles: raise HTTPException(403, f'operator role must be one of: {", ".join(roles)}')
def require_fault_injection_enabled() -> None:
    env=(settings.app_env or 'local').strip().lower()
    if env == 'production':
        raise HTTPException(403, 'fault injection is forbidden in production')
    if not settings.enable_fault_injection:
        raise HTTPException(403, 'fault injection is disabled; set ENABLE_FAULT_INJECTION=true in local/test/staging')
def require_non_production_sandbox() -> None:
    env=(settings.app_env or 'local').strip().lower()
    if env == 'production':
        raise HTTPException(403, 'sandbox commands are forbidden in production')
def event_out(e: Event): return {'id':e.id,'kind':e.kind,'message':e.message,'data':e.data,'created_at':e.created_at.isoformat() if e.created_at else None}
def approval_out(a: Approval):
    return {'id':a.id,'case_id':a.case_id,'plan_version':a.plan_version,'status':a.status,'action_hash':a.action,'required_roles':a.required_roles,'approved_roles':a.approved_roles,'approver':a.approver,'expires_at':a.expires_at.isoformat() if a.expires_at else None,'revoked_at':a.revoked_at.isoformat() if a.revoked_at else None,'revoked_by':a.revoked_by,'revocation_reason':a.revocation_reason,'rejected_at':a.rejected_at.isoformat() if a.rejected_at else None,'rejected_by':a.rejected_by,'rejection_reason':a.rejection_reason}
def invocation_out(i: Invocation):
    return {'id':i.id,'case_id':i.case_id,'tool':i.tool,'status':i.status,'external_id':i.external_id,'idempotency_key':i.idempotency_key}
def task_out(t: Task):
    return {'id':t.id,'case_id':t.case_id,'kind':t.kind,'status':t.status,'attempts':t.attempts,'payload':t.payload,'started_at':t.started_at.isoformat() if t.started_at else None,'last_error':t.last_error}
def lane_out(lane: LogisticsLane):
    return {'id':lane.id,'tenant_id':lane.tenant_id,'source_warehouse':lane.source_warehouse,'target_warehouse':lane.target_warehouse,'transit_days':lane.transit_days,'cost_per_unit':lane.cost_per_unit,'currency':lane.currency,'active':lane.active}
def audit_out(log: AuditLog):
    return {'id':log.id,'actor':log.actor,'role':log.role,'action':log.action,'resource_type':log.resource_type,'resource_id':log.resource_id,'case_id':log.case_id,'data':log.data,'created_at':log.created_at.isoformat() if log.created_at else None}
def case_tool_trace(case: Case):
    evidence=case.evidence if isinstance(case.evidence,dict) else {}
    if isinstance(evidence.get('tool_trace'),dict):
        return evidence['tool_trace']
    conclusion=evidence.get('conclusion') if isinstance(evidence.get('conclusion'),dict) else {}
    if conclusion and conclusion.get('status') != 'ready':
        return build_tool_trace(evidence.get('observations') or [],None,None)
    plan=case.plan if isinstance(case.plan,dict) else {}
    return build_tool_trace(evidence.get('observations') or [],plan,plan.get('evidence_grounding') if isinstance(plan,dict) else None)
def case_agent_decision(case: Case):
    evidence=case.evidence if isinstance(case.evidence,dict) else {}
    conclusion=evidence.get('conclusion') if isinstance(evidence.get('conclusion'),dict) else {}
    return {
        'decision_trace': conclusion.get('decision_trace') or [],
        'rejected_actions': conclusion.get('rejected_actions') or [],
        'missing_information': conclusion.get('missing_information') or [],
        'evidence_summary': conclusion.get('evidence_summary') or [],
    }

def token_value(usage: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value=usage.get(key)
        if isinstance(value,(int,float)):
            return int(value)
    return 0

def metric_number(value: Any) -> float | None:
    if isinstance(value,(int,float)):
        return float(value)
    return None

def llm_usage_from_telemetry(telemetry: dict[str, Any] | None) -> dict[str, int | float]:
    if not isinstance(telemetry,dict):
        return {
            'llm_calls':0, 'prompt_tokens':0, 'completion_tokens':0,
            'total_tokens':0, 'token_usage_calls':0,
            'latency_ms_total':0, 'latency_sample_calls':0,
        }
    usage=telemetry.get('usage') if isinstance(telemetry.get('usage'),dict) else {}
    total=token_value(usage,'total_tokens','total_token_count')
    prompt=token_value(usage,'prompt_tokens','input_tokens','prompt_token_count')
    completion=token_value(usage,'completion_tokens','output_tokens','completion_token_count')
    if not total:
        total=prompt+completion
    latency_ms=metric_number(telemetry.get('latency_ms'))
    return {
        'llm_calls':1 if telemetry.get('status') or usage or latency_ms is not None else 0,
        'prompt_tokens':prompt,
        'completion_tokens':completion,
        'total_tokens':total,
        'token_usage_calls':1 if usage else 0,
        'latency_ms_total':latency_ms or 0,
        'latency_sample_calls':1 if latency_ms is not None else 0,
    }

def merge_llm_usage(*items: dict[str, int | float]) -> dict[str, int | float]:
    return {
        'llm_calls':sum(item.get('llm_calls',0) for item in items),
        'prompt_tokens':sum(item.get('prompt_tokens',0) for item in items),
        'completion_tokens':sum(item.get('completion_tokens',0) for item in items),
        'total_tokens':sum(item.get('total_tokens',0) for item in items),
        'token_usage_calls':sum(item.get('token_usage_calls',0) for item in items),
        'latency_ms_total':sum(item.get('latency_ms_total',0) for item in items),
        'latency_sample_calls':sum(item.get('latency_sample_calls',0) for item in items),
    }

def case_llm_usage(case: Case, events: list[Event]) -> dict[str, int | float]:
    evidence=case.evidence if isinstance(case.evidence,dict) else {}
    conclusion=evidence.get('conclusion') if isinstance(evidence.get('conclusion'),dict) else {}
    def telemetry_items(value: Any) -> list[dict[str, Any]]:
        if isinstance(value,dict):
            return [value]
        if isinstance(value,list):
            return [item for item in value if isinstance(item,dict)]
        return []

    usages=[]
    durable_telemetry=False
    # Every investigation publishes its complete request telemetry in the
    # durable decision event. Prefer it over ``case.evidence``, which holds
    # only the newest investigation after a replan.
    for event in events:
        data=event.data or {}
        for field in ('llm_read_loop','llm_planner','llm_repair','llm_plan_repair'):
            items=telemetry_items(data.get(field))
            if items:
                durable_telemetry=True
                usages.extend(llm_usage_from_telemetry(item) for item in items)
        # Older events only embedded an entire conclusion. Keep them readable
        # without double-counting new structured telemetry above.
        event_conclusion=data.get('conclusion') if isinstance(data.get('conclusion'),dict) else None
        if event_conclusion and not any(field in data for field in ('llm_read_loop','llm_planner','llm_repair','llm_plan_repair')):
            planner_field='llm_planner' if event_conclusion.get('llm_planner') else 'llm'
            for field in ('llm_read_loop',planner_field,'llm_repair','llm_plan_repair'):
                usages.extend(llm_usage_from_telemetry(item) for item in telemetry_items(event_conclusion.get(field)))

    if not durable_telemetry:
        # Legacy Cases did not preserve every read-loop request. They retain
        # enough lifecycle events to infer the request count, while token and
        # latency totals stay explicitly limited to observed telemetry.
        planner_field='llm_planner' if conclusion.get('llm_planner') else 'llm'
        for field in ('llm_read_loop',planner_field,'llm_repair','llm_plan_repair'):
            usages.extend(llm_usage_from_telemetry(item) for item in telemetry_items(conclusion.get(field)))

    merged=merge_llm_usage(*usages)
    observed_calls=int(merged['llm_calls'])
    inferred_read_turns=sum(1 for event in events if event.kind=='turn_start')
    inferred_planner_calls=sum(
        1 for event in events
        if event.kind=='agent_decision_trace'
        and ('llm_planner' not in (event.data or {}) or bool((event.data or {}).get('llm_planner')))
    )
    inferred_plan_repairs=sum(1 for event in events if event.kind=='plan_repair_requested')
    merged['observed_llm_calls']=observed_calls
    merged['llm_calls']=max(observed_calls,inferred_read_turns+inferred_planner_calls+inferred_plan_repairs)
    return merged

def tool_latency_stats(tool_events: list[Event]) -> dict[str, float | None]:
    latencies=[]
    for event in tool_events:
        data=event.data or {}
        tool_result=data.get('tool_result') if isinstance(data.get('tool_result'),dict) else {}
        metadata=tool_result.get('metadata') if isinstance(tool_result.get('metadata'),dict) else {}
        scheduler=tool_result.get('scheduler') if isinstance(tool_result.get('scheduler'),dict) else {}
        # Cache and deduplication reuse a prior result. Its latency belongs to
        # the originating execution, not to this logical read invocation.
        if scheduler.get('source') and scheduler.get('source') != 'executed':
            continue
        latency=metric_number(metadata.get('latency_ms'))
        if latency is not None:
            latencies.append(latency)
    if not latencies:
        return {'tool_latency_ms_total':0,'observed_tool_latency_count':0,'avg_tool_latency_ms':None,'max_tool_latency_ms':None}
    return {
        'tool_latency_ms_total':sum(latencies),
        'observed_tool_latency_count':len(latencies),
        'avg_tool_latency_ms':sum(latencies)/len(latencies),
        'max_tool_latency_ms':max(latencies),
    }

def case_queue_wait_ms(case: Case, events: list[Event], tasks: list[Task]) -> float | None:
    starts=[task.started_at for task in tasks if task.started_at]
    if not starts:
        return None
    origins=[]
    if case.created_at:
        origins.append(case.created_at)
    origins.extend(event.created_at for event in events if event.kind=='case_created' and event.created_at)
    if not origins:
        return None
    return max(0,(min(starts).timestamp()-min(origins).timestamp())*1000)

def event_tool_signature(event: Event) -> str | None:
    data=event.data or {}
    tool=data.get('tool')
    arguments=data.get('arguments') if isinstance(data.get('arguments'),dict) else {}
    if not tool:
        return None
    return json.dumps({'tool':tool,'arguments':arguments}, ensure_ascii=False, sort_keys=True)

def count_duplicate_tool_observations(events: list[Event]) -> int:
    """Count repeated reads only within one investigation context.

    A replan deliberately starts a fresh Context Snapshot and may read the
    same ERP fact again; that is not a duplicate scheduler decision.
    """
    seen=set()
    duplicates=0
    for event in events:
        if event.kind=='context_built':
            seen.clear()
            continue
        if event.kind!='tool_observation':
            continue
        signature=event_tool_signature(event)
        if not signature:
            continue
        if signature in seen:
            duplicates+=1
        else:
            seen.add(signature)
    return duplicates

def event_index(kinds: list[str], kind: str) -> int | None:
    try:
        return kinds.index(kind)
    except ValueError:
        return None

def unsafe_continuation_count(kinds: list[str]) -> int:
    count=0
    grounding_failed=event_index(kinds,'evidence_grounding_failed')
    execution_started=event_index(kinds,'execution_started')
    if grounding_failed is not None and execution_started is not None and execution_started>grounding_failed:
        count+=1
    verification_failed=event_index(kinds,'verification_failed')
    resolved_after_failure=verification_failed is not None and 'verification_passed' not in kinds[verification_failed+1:]
    if resolved_after_failure and 'lessons_recorded' in kinds[verification_failed+1:]:
        count+=1
    return count

def argument_correctness_for_case(case: Case, plan_actions: list[dict[str, Any]]) -> tuple[float | None, list[str]]:
    if not plan_actions:
        return None, []
    evidence=case.evidence if isinstance(case.evidence,dict) else {}
    observations=evidence.get('observations') if isinstance(evidence.get('observations'),list) else []
    plan=case.plan if isinstance(case.plan,dict) else {'actions':plan_actions}
    if not observations:
        return 0, ['missing observations for argument validation']
    grounding=validate_plan_grounding(plan,observations,case.event_type)
    problems=grounding.get('problems') or []
    if not problems:
        return 1, []
    action_count=max(1,len(plan_actions))
    problem_penalty=min(action_count,len(problems))/action_count
    return max(0,1-problem_penalty), [str(problem) for problem in problems]

def eval_case_out(case: Case, events: list[Event], approvals: list[Approval], invocations: list[Invocation], tasks: list[Task]):
    kinds=[event.kind for event in events]
    plan_actions=(case.plan or {}).get('actions',[]) if isinstance(case.plan,dict) else []
    tool_events=[event for event in events if event.kind=='tool_observation']
    tool_trace=case_tool_trace(case)
    scheduled_events=[event for event in events if event.kind=='tool_scheduled']
    failed_tool_events=[
        event for event in tool_events
        if ((event.data or {}).get('result') or {}).get('error')
        or (((event.data or {}).get('tool_result') or {}).get('status') == 'failed')
    ]
    scheduler_sources={}
    for event in scheduled_events:
        source=(((event.data or {}).get('scheduler') or {}).get('source')) or 'unknown'
        scheduler_sources[source]=scheduler_sources.get(source,0)+1
    write_count=len(invocations)
    verification_passes=sum(1 for kind in kinds if kind=='verification_passed')
    verification_failures=sum(1 for kind in kinds if kind=='verification_failed')
    recovery_events=[kind for kind in kinds if kind in {'replan_requested','task_requeued','manual_review_required','plan_repair_requested','plan_repair_succeeded'}]
    blocked_events=[kind for kind in kinds if kind in {'context_isolation_failed','evidence_grounding_failed','policy_denied','handoff','worker_failure','verification_failed','approval_expired','approval_revoked'}]
    stage_sequence=[
        kind for kind in kinds
        if kind in {
            'case_created','context_built','context_isolation_sanitized','context_isolation_failed',
            'tool_scheduled','tool_observation','evidence_grounding_passed','evidence_grounding_failed',
            'plan_repair_requested','plan_repair_succeeded','plan_repair_failed',
            'agent_plan_created','approval_requested','approval_partial','approval_granted','approval_expired','approval_revoked',
            'execution_started','replan_requested','verification_passed','verification_failed',
            'lessons_recorded','handoff','manual_review_required','worker_failure',
        }
    ]
    action_evidence=tool_trace.get('action_evidence',{})
    trace_summary=tool_trace.get('summary',{})
    verification_complete=write_count==0 or (verification_passes>=write_count and verification_failures==0)
    has_manual_handoff=any(kind in {'handoff','manual_review_required'} for kind in kinds)
    has_pending_approval=any(approval.status=='pending' for approval in approvals)
    task_succeeded=(
        case.status=='resolved'
        or (case.status=='waiting_approval' and has_pending_approval and 'agent_plan_created' in kinds)
        or (case.status=='manual_review' and has_manual_handoff)
    )
    tool_selection_accuracy=(len(tool_events)-len(failed_tool_events))/len(tool_events) if tool_events else 1
    if plan_actions:
        grounded_actions=sum(
            1
            for idx, action in enumerate(plan_actions,1)
            if action_evidence.get(str(idx))
            or action_evidence.get(action.get('action_id'))
            or action_evidence.get(action.get('action_type'))
        )
        evidence_faithfulness=grounded_actions/len(plan_actions)
    else:
        evidence_faithfulness=1 if not any(kind=='evidence_grounding_failed' for kind in kinds) else 0
    if any(kind=='evidence_grounding_failed' for kind in kinds):
        evidence_faithfulness=0
    argument_correctness,argument_problems=argument_correctness_for_case(case,plan_actions)
    replan_success=None
    if 'replan_requested' in kinds:
        replan_success=case.status in {'resolved','manual_review','waiting_approval'} and 'worker_failure' not in kinds
    timestamps=[event.created_at for event in events if event.created_at]
    if case.created_at:
        timestamps.append(case.created_at)
    if case.updated_at:
        timestamps.append(case.updated_at)
    duration_seconds=None
    if timestamps:
        duration_seconds=max(timestamps).timestamp()-min(timestamps).timestamp()
    llm_usage=case_llm_usage(case, events)
    llm_latency_ms_total=llm_usage.get('latency_ms_total',0)
    observed_llm_calls=llm_usage.get('observed_llm_calls',0)
    llm_latency_sample_count=llm_usage.get('latency_sample_calls',0)
    avg_llm_latency_ms=(llm_latency_ms_total/llm_latency_sample_count) if llm_latency_sample_count else None
    tool_latency=tool_latency_stats(tool_events)
    queue_wait_ms=case_queue_wait_ms(case, events, tasks)
    read_tool_budget=max(1, settings.agent_max_read_tool_calls)
    read_tool_budget_used=len(tool_events)/read_tool_budget
    read_tool_budget_exhausted=any(
        'read-tool budget exhausted' in str(item)
        for item in ((case.evidence or {}).get('conclusion') or {}).get('missing_information',[])
    ) if isinstance(case.evidence,dict) else False
    duplicate_tool_call_count=count_duplicate_tool_observations(events)
    critical_checks=[
        'context_built' in kinds or 'context_isolation_failed' in kinds,
        bool(tool_events) or has_manual_handoff,
        bool(plan_actions) or has_manual_handoff or 'evidence_grounding_failed' in kinds or 'policy_denied' in kinds,
        write_count==0 or 'execution_started' in kinds,
        write_count==0 or verification_complete,
    ]
    critical_stage_coverage=sum(1 for item in critical_checks if item)/len(critical_checks)
    replan_count=sum(1 for kind in kinds if kind=='replan_requested')
    plan_repair_count=sum(1 for kind in kinds if kind=='plan_repair_requested')
    # Kept for aggregate evaluation compatibility. The per-Case UI exposes
    # Replan and Plan Repair separately rather than conflating lifecycle
    # events into a pseudo-count.
    self_correction_count=sum(1 for kind in kinds if kind in {'replan_requested','task_requeued','plan_repair_requested','plan_repair_succeeded'})
    unsafe_count=unsafe_continuation_count(kinds)
    trajectory_quality_score=max(
        0,
        min(
            1,
            (
                critical_stage_coverage
                + tool_selection_accuracy
                + evidence_faithfulness
                + (1 if verification_complete else 0)
            ) / 4
            - min(0.25, duplicate_tool_call_count * 0.05)
            - min(0.5, unsafe_count * 0.25)
        )
    )
    return {
        'case_id':case.id,
        'event_type':case.event_type,
        'order_id':case.order_id,
        'status':case.status,
        'resolved':case.status=='resolved',
        'manual_review':case.status=='manual_review',
        'task_succeeded':task_succeeded,
        'plan_version':case.plan_version,
        'action_count':len(plan_actions),
        'tool_call_count':len(tool_events),
        'scheduled_tool_call_count':len(scheduled_events),
        'tool_failure_count':len(failed_tool_events),
        'tool_selection_accuracy':tool_selection_accuracy,
        'tool_scheduler_sources':scheduler_sources,
        'tool_trace_summary':trace_summary,
        'action_evidence':action_evidence,
        'evidence_faithfulness':evidence_faithfulness,
        'argument_correctness':argument_correctness,
        'argument_correctness_problems':argument_problems,
        'approval_count':len(approvals),
        'pending_approval_count':sum(1 for approval in approvals if approval.status=='pending'),
        'expired_approval_count':sum(1 for approval in approvals if approval.status=='expired'),
        'revoked_approval_count':sum(1 for approval in approvals if approval.status=='revoked'),
        'write_invocation_count':write_count,
        'verification_pass_count':verification_passes,
        'verification_failed_count':verification_failures,
        'verification_complete':verification_complete,
        'recovery_event_count':len(recovery_events),
        'blocked_event_count':len(blocked_events),
        'task_failure_count':sum(1 for task in tasks if task.status=='failed'),
        'has_policy_denial':'policy_denied' in kinds,
        'has_evidence_grounding_failure':'evidence_grounding_failed' in kinds,
        'has_evidence_grounding_passed':'evidence_grounding_passed' in kinds,
        'has_context_isolation_failure':'context_isolation_failed' in kinds,
        'has_context_isolation_sanitized':'context_isolation_sanitized' in kinds,
        'has_replan':'replan_requested' in kinds,
        'replan_success':replan_success,
        'has_approval_expired':'approval_expired' in kinds,
        'has_approval_revoked':'approval_revoked' in kinds,
        'has_manual_handoff':has_manual_handoff,
        'duration_seconds':duration_seconds,
        'llm_call_count':llm_usage['llm_calls'],
        'llm_telemetry_call_count':observed_llm_calls,
        'llm_token_usage_call_count':llm_usage.get('token_usage_calls',0),
        'llm_latency_sample_count':llm_latency_sample_count,
        'llm_prompt_tokens':llm_usage['prompt_tokens'],
        'llm_completion_tokens':llm_usage['completion_tokens'],
        'llm_total_tokens':llm_usage['total_tokens'],
        'llm_latency_ms_total':llm_latency_ms_total,
        'avg_llm_latency_ms':avg_llm_latency_ms,
        'tool_latency_ms_total':tool_latency['tool_latency_ms_total'],
        'observed_tool_latency_count':tool_latency['observed_tool_latency_count'],
        'avg_tool_latency_ms':tool_latency['avg_tool_latency_ms'],
        'max_tool_latency_ms':tool_latency['max_tool_latency_ms'],
        'queue_wait_ms':queue_wait_ms,
        'read_tool_budget':read_tool_budget,
        'read_tool_budget_used':read_tool_budget_used,
        'read_tool_budget_exhausted':read_tool_budget_exhausted,
        'duplicate_tool_call_count':duplicate_tool_call_count,
        'replan_count':replan_count,
        'plan_repair_count':plan_repair_count,
        'critical_stage_coverage':critical_stage_coverage,
        'self_correction_count':self_correction_count,
        'unsafe_continuation_count':unsafe_count,
        'trajectory_quality_score':trajectory_quality_score,
        'stage_sequence':stage_sequence,
        'event_kinds':kinds,
    }


CASE_RUN_METRIC_FIELDS = (
    'case_id', 'status', 'plan_version', 'duration_seconds',
    'llm_call_count', 'llm_telemetry_call_count', 'llm_prompt_tokens', 'llm_completion_tokens',
    'llm_total_tokens', 'llm_token_usage_call_count', 'avg_llm_latency_ms', 'llm_latency_sample_count',
    'tool_call_count', 'scheduled_tool_call_count', 'tool_failure_count',
    'avg_tool_latency_ms', 'max_tool_latency_ms', 'observed_tool_latency_count',
    'replan_count', 'plan_repair_count', 'duplicate_tool_call_count',
)


def case_run_metrics_out(case: Case, events: list[Event], approvals: list[Approval], invocations: list[Invocation], tasks: list[Task]) -> dict:
    """Return observable facts for one Case run, never aggregate evaluation scores."""
    source = eval_case_out(case, events, approvals, invocations, tasks)
    return {field: source.get(field) for field in CASE_RUN_METRIC_FIELDS}
def eval_summary_out(rows):
    total=len(rows)
    resolved=sum(1 for row in rows if row['resolved'])
    manual=sum(1 for row in rows if row['manual_review'])
    task_successes=sum(1 for row in rows if row.get('task_succeeded'))
    writes=sum(row['write_invocation_count'] for row in rows)
    write_cases=sum(1 for row in rows if row['write_invocation_count']>0)
    verified=sum(1 for row in rows if row['write_invocation_count']>0 and row['verification_complete'])
    tool_calls=sum(row.get('tool_call_count',0) for row in rows)
    scheduled_tool_calls=sum(row.get('scheduled_tool_call_count',0) for row in rows)
    tool_failures=sum(row.get('tool_failure_count',0) for row in rows)
    llm_calls=sum(row.get('llm_call_count',0) for row in rows)
    llm_total_tokens=sum(row.get('llm_total_tokens',0) for row in rows)
    llm_prompt_tokens=sum(row.get('llm_prompt_tokens',0) for row in rows)
    llm_completion_tokens=sum(row.get('llm_completion_tokens',0) for row in rows)
    llm_latency_ms_total=sum(row.get('llm_latency_ms_total',0) for row in rows)
    tool_latency_ms_total=sum(row.get('tool_latency_ms_total',0) for row in rows)
    observed_tool_latency_count=sum(row.get('observed_tool_latency_count',0) for row in rows)
    queue_waits=[row.get('queue_wait_ms') for row in rows if isinstance(row.get('queue_wait_ms'),(int,float))]
    max_tool_latencies=[row.get('max_tool_latency_ms') for row in rows if isinstance(row.get('max_tool_latency_ms'),(int,float))]
    replanned=[row for row in rows if row.get('has_replan')]
    durations=[row.get('duration_seconds') for row in rows if isinstance(row.get('duration_seconds'),(int,float))]
    grounding_applicable=[row for row in rows if row.get('action_count',0)>0 or row.get('has_evidence_grounding_passed') or row.get('has_evidence_grounding_failure')]
    argument_applicable=[row for row in rows if isinstance(row.get('argument_correctness'),(int,float))]
    context_failures=sum(1 for row in rows if row.get('has_context_isolation_failure'))
    trajectory_scores=[row.get('trajectory_quality_score') for row in rows if isinstance(row.get('trajectory_quality_score'),(int,float))]
    return {
        'total_cases':total,
        'resolved_cases':resolved,
        'manual_review_cases':manual,
        'task_success_cases':task_successes,
        'task_success_rate':task_successes/total if total else 0,
        'case_resolution_rate':resolved/total if total else 0,
        'avg_read_tool_calls':tool_calls/total if total else 0,
        'avg_scheduled_tool_calls':scheduled_tool_calls/total if total else 0,
        'avg_duration_seconds':sum(durations)/len(durations) if durations else None,
        'llm_call_count':llm_calls,
        'avg_llm_calls_per_case':llm_calls/total if total else 0,
        'llm_total_tokens':llm_total_tokens,
        'llm_prompt_tokens':llm_prompt_tokens,
        'llm_completion_tokens':llm_completion_tokens,
        'avg_llm_tokens_per_case':llm_total_tokens/total if total else 0,
        'llm_latency_ms_total':llm_latency_ms_total,
        'avg_llm_latency_ms':llm_latency_ms_total/llm_calls if llm_calls else None,
        'tool_latency_ms_total':tool_latency_ms_total,
        'observed_tool_latency_count':observed_tool_latency_count,
        'avg_tool_latency_ms':tool_latency_ms_total/observed_tool_latency_count if observed_tool_latency_count else None,
        'max_tool_latency_ms':max(max_tool_latencies) if max_tool_latencies else None,
        'avg_queue_wait_ms':sum(queue_waits)/len(queue_waits) if queue_waits else None,
        'budget_exhausted_cases':sum(1 for row in rows if row.get('read_tool_budget_exhausted')),
        'avg_read_tool_budget_used':sum(row.get('read_tool_budget_used',0) for row in rows)/total if total else 0,
        'avg_trajectory_quality_score':sum(trajectory_scores)/len(trajectory_scores) if trajectory_scores else 0,
        'duplicate_tool_call_count':sum(row.get('duplicate_tool_call_count',0) for row in rows),
        'self_correction_cases':sum(1 for row in rows if row.get('self_correction_count',0)>0),
        'unsafe_continuation_cases':sum(1 for row in rows if row.get('unsafe_continuation_count',0)>0),
        'tool_selection_accuracy':sum(row.get('tool_selection_accuracy',1) for row in rows)/total if total else 0,
        'argument_correctness_rate':sum(row.get('argument_correctness',0) for row in argument_applicable)/len(argument_applicable) if argument_applicable else 1,
        'argument_checked_cases':len(argument_applicable),
        'argument_problem_cases':sum(1 for row in argument_applicable if row.get('argument_correctness',1)<1),
        'tool_failure_rate':tool_failures/tool_calls if tool_calls else 0,
        'tool_failures':tool_failures,
        'planner_coverage_rate':sum(1 for row in rows if row.get('action_count',0)>0 or row.get('has_manual_handoff'))/total if total else 0,
        'avg_action_count':sum(row.get('action_count',0) for row in rows)/total if total else 0,
        'evidence_faithfulness_rate':sum(row.get('evidence_faithfulness',1) for row in grounding_applicable)/len(grounding_applicable) if grounding_applicable else 1,
        'approval_waiting_cases':sum(1 for row in rows if row.get('pending_approval_count',0)>0),
        'approval_expired_cases':sum(1 for row in rows if row.get('expired_approval_count',0)>0 or row.get('has_approval_expired')),
        'approval_revoked_cases':sum(1 for row in rows if row.get('revoked_approval_count',0)>0 or row.get('has_approval_revoked')),
        'cases_with_writes':write_cases,
        'verified_write_cases':verified,
        'verification_pass_rate':verified/write_cases if write_cases else 1,
        'write_invocations':writes,
        'verification_failures':sum(row['verification_failed_count'] for row in rows),
        'policy_denials':sum(1 for row in rows if row['has_policy_denial']),
        'evidence_grounding_passed_cases':sum(1 for row in rows if row.get('has_evidence_grounding_passed')),
        'evidence_grounding_failures':sum(1 for row in rows if row.get('has_evidence_grounding_failure')),
        'evidence_grounding_pass_rate':sum(1 for row in rows if row.get('has_evidence_grounding_passed'))/len(grounding_applicable) if grounding_applicable else 1,
        'context_isolation_sanitized_cases':sum(1 for row in rows if row.get('has_context_isolation_sanitized')),
        'context_isolation_failures':context_failures,
        'context_isolation_pass_rate':(total-context_failures)/total if total else 1,
        'replanned_cases':len(replanned),
        'replan_success_cases':sum(1 for row in replanned if row.get('replan_success')),
        'replan_success_rate':sum(1 for row in replanned if row.get('replan_success'))/len(replanned) if replanned else None,
        'manual_handoff_cases':sum(1 for row in rows if row['has_manual_handoff']),
        'safe_stop_rate':sum(1 for row in rows if row.get('has_manual_handoff') and not row.get('resolved'))/manual if manual else None,
        'task_failures':sum(row['task_failure_count'] for row in rows),
        'cases':rows,
    }
@app.get('/healthz')
def health(): return {'status':'ok'}
@app.get('/readyz')
def readiness():
    try:
        with Session(engine) as db:
            status=build_runtime_status(db)
    except Exception:
        raise HTTPException(503,'not ready')
    if status['status']!='ready':
        raise HTTPException(503,{'status':status['status'],'checks':status['checks']})
    return {'status':'ready'}


@app.get('/v1/operator/me')
def current_operator(x_operator_key: str | None = Header(default=None)):
    """Return the server-validated local operator identity for the Workbench."""
    with Session(engine) as db:
        identity = operator_identity_from_db(db, x_operator_key)
        return {
            'subject': identity.subject,
            'role': identity.role,
            'tenant_id': identity.tenant_id,
        }


@app.get('/v1/runtime/status')
def runtime_status(x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None)):
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        require_role(identity,'ops_admin','config_admin')
        return build_runtime_status(db)


def _database_connection_summary(value: str | None) -> dict[str, Any]:
    if not value:
        return {'configured': False, 'dialect': None, 'host': None, 'database': None}
    try:
        url = make_url(value)
        return {
            'configured': True,
            'dialect': url.get_backend_name(),
            'host': url.host,
            'database': url.database,
        }
    except Exception:
        return {'configured': True, 'dialect': 'unknown', 'host': None, 'database': None}


def _connection_settings_out() -> dict[str, Any]:
    """Configuration shape for the Workbench.  Secret values never leave API."""
    return {
        'llm': {
            'base_url': connection_value('llm_base_url') or '',
            'model': connection_value('llm_model') or '',
            'timeout_seconds': connection_value('llm_timeout_seconds'),
            'api_key_configured': secret_configured('llm_api_key'),
        },
        'erpnext': {
            'base_url': connection_value('erpnext_base_url') or '',
            'api_key_configured': secret_configured('erpnext_api_key'),
            'api_secret_configured': secret_configured('erpnext_api_secret'),
        },
        'database': {
            **_database_connection_summary(connection_value('database_url')),
            'restart_required_for_changes': True,
        },
    }


@app.get('/v1/settings/connections')
def connection_settings(x_operator_key: str | None = Header(default=None)):
    with Session(engine) as db:
        identity = operator_identity_from_db(db, x_operator_key)
        require_role(identity, 'ops_admin', 'config_admin')
        return {'connections': _connection_settings_out(), 'status': build_runtime_status(db)}


@app.put('/v1/settings/connections')
def update_connection_settings(payload: ConnectionSettingsIn, x_operator_key: str | None = Header(default=None)):
    updates = payload.model_dump(exclude_none=True)
    with Session(engine) as db:
        identity = operator_identity_from_db(db, x_operator_key)
        require_role(identity, 'ops_admin', 'config_admin')
        updated, restart_required = save_runtime_connections(updates)
        audit(db, identity, 'connection_settings_updated', 'runtime_configuration', 'local', {
            'updated_fields': sorted(updated),
            'restart_required': restart_required,
        })
        db.commit()
        return {
            'updated_fields': sorted(updated),
            'restart_required': restart_required,
            'message': '数据库连接已保存，需重启 API 与 Worker 后生效。' if restart_required else 'LLM 与 ERP 连接配置已保存。Worker 会在下一项任务前刷新连接。',
            'connections': _connection_settings_out(),
        }


@app.post('/v1/settings/connections/test')
def test_connection_settings(payload: ConnectionTestIn, x_operator_key: str | None = Header(default=None)):
    values = payload.model_dump(exclude_none=True)
    target = values.pop('target')
    with Session(engine) as db:
        identity = operator_identity_from_db(db, x_operator_key)
        require_role(identity, 'ops_admin', 'config_admin')
    # Draft fields are used only for this read-only connectivity check.  They
    # are never persisted by the test endpoint.
    def current(name: str) -> Any:
        value = values.get(name)
        return value if value is not None and (not isinstance(value, str) or value.strip()) else connection_value(name)
    try:
        if target == 'llm':
            base_url, api_key = current('llm_base_url'), current('llm_api_key')
            if not base_url or not api_key:
                raise ValueError('请先填写 LLM 服务地址和 API Key。')
            response = httpx.get(str(base_url).rstrip('/') + '/models', headers={'Authorization': f'Bearer {api_key}'}, timeout=10)
            response.raise_for_status()
            return {'ok': True, 'target': target, 'message': 'LLM 服务连通，认证请求已通过。'}
        if target == 'erpnext':
            base_url, api_key, api_secret = current('erpnext_base_url'), current('erpnext_api_key'), current('erpnext_api_secret')
            if not base_url or not api_key or not api_secret:
                raise ValueError('请先填写 ERPNext 地址、API Key 与 API Secret。')
            response = httpx.get(
                str(base_url).rstrip('/') + '/api/method/frappe.auth.get_logged_user',
                headers={'Authorization': f'token {api_key}:{api_secret}'}, timeout=10,
            )
            response.raise_for_status()
            return {'ok': True, 'target': target, 'message': 'ERPNext 服务连通，集成账号认证已通过。'}
        database_url = current('database_url')
        if not database_url:
            raise ValueError('请先填写数据库连接地址。')
        test_engine = create_database_engine(str(database_url))
        try:
            with test_engine.connect() as connection:
                connection.execute(text('SELECT 1'))
        finally:
            test_engine.dispose()
        return {'ok': True, 'target': target, 'message': '数据库连通，SELECT 1 校验通过。'}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f'{target} 认证或服务响应失败：HTTP {exc.response.status_code}') from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, f'{target} 无法连接。请检查地址、网络和证书。') from exc
    except Exception as exc:
        raise HTTPException(502, f'{target} 连通性检查失败：{type(exc).__name__}') from exc


def sandbox_check_payload(db: Session, seed: SandboxSeedIn | None = None) -> dict:
    payload = seed or SandboxSeedIn()
    erp=ERPNextAdapter(settings.erpnext_base_url,settings.erpnext_api_key,settings.erpnext_api_secret)
    checks: dict[str, Any] = {}

    def check(name: str, fn):
        try:
            value=fn()
            checks[name]={'ok': True, 'value': value}
        except httpx.HTTPStatusError as exc:
            status_code=exc.response.status_code if exc.response is not None else None
            checks[name]={'ok': False, 'error': f'erpnext_http_{status_code}'}
        except httpx.RequestError as exc:
            checks[name]={'ok': False, 'error': 'erpnext_connection_failed', 'message': str(exc)}
        except Exception as exc:
            checks[name]={'ok': False, 'error': type(exc).__name__, 'message': str(exc)}

    check('erpnext_sales_order', lambda: {'name': erp.sales_order(payload.order_id).get('name')})
    check('erpnext_item', lambda: erp.resource_exists('Item', payload.item_code))
    check('erpnext_customer', lambda: erp.resource_exists('Customer', payload.customer))
    company=payload.company or settings.erpnext_company
    difference_account=payload.difference_account or settings.erpnext_stock_difference_account
    if company:
        check('erpnext_company', lambda: erp.resource_exists('Company', company))
    else:
        checks['erpnext_company']={'ok': False, 'error': 'missing_company_config'}
    if difference_account:
        check('erpnext_stock_difference_account', lambda: erp.resource_exists('Account', difference_account))
    else:
        checks['erpnext_stock_difference_account']={'ok': False, 'error': 'missing_difference_account_config'}
    check('erpnext_source_warehouse', lambda: erp.resource_exists('Warehouse', payload.source_warehouse))
    check('erpnext_target_warehouse', lambda: erp.resource_exists('Warehouse', payload.target_warehouse))
    check('source_stock', lambda: erp.stock(payload.item_code, payload.source_warehouse))
    check('target_stock', lambda: erp.stock(payload.item_code, payload.target_warehouse))
    lane=db.scalar(select(LogisticsLane).where(
        LogisticsLane.tenant_id==payload.tenant_id,
        LogisticsLane.source_warehouse==payload.source_warehouse,
        LogisticsLane.target_warehouse==payload.target_warehouse,
    ))
    checks['resolveops_logistics_lane']={
        'ok': lane is not None,
        'value': lane_out(lane) if lane else {
            'tenant_id': payload.tenant_id,
            'source_warehouse': payload.source_warehouse,
            'target_warehouse': payload.target_warehouse,
            'expected_transit_days': payload.transit_days,
            'expected_cost_per_unit': payload.cost_per_unit,
        },
    }
    ready=all(check.get('ok') for check in checks.values())
    return {
        'status': 'ready' if ready else 'degraded',
        'app_env': settings.app_env,
        'defaults': payload.model_dump(),
        'checks': checks,
    }

@app.get('/v1/sandbox/check')
def sandbox_check(x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None)):
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        require_role(identity,'ops_admin','config_admin')
        return sandbox_check_payload(db)

@app.post('/v1/sandbox/seed')
def sandbox_seed(payload: SandboxSeedIn, x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None)):
    require_non_production_sandbox()
    if payload.set_stock:
        require_fault_injection_enabled()
    company=payload.company or settings.erpnext_company
    difference_account=payload.difference_account or settings.erpnext_stock_difference_account
    valuation_rate=payload.valuation_rate if payload.valuation_rate is not None else settings.erpnext_default_valuation_rate
    if payload.set_stock and not company:
        raise HTTPException(422, 'company is required to seed ERPNext stock')
    if payload.set_stock and not difference_account:
        raise HTTPException(422, 'difference_account is required to seed ERPNext stock')
    erp=ERPNextAdapter(settings.erpnext_base_url,settings.erpnext_api_key,settings.erpnext_api_secret)
    actions=[]
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        require_role(identity,'ops_admin','config_admin')
        lane=db.scalar(select(LogisticsLane).where(
            LogisticsLane.tenant_id==payload.tenant_id,
            LogisticsLane.source_warehouse==payload.source_warehouse,
            LogisticsLane.target_warehouse==payload.target_warehouse,
        ))
        if lane:
            lane.transit_days=payload.transit_days
            lane.cost_per_unit=payload.cost_per_unit
            lane.currency='CNY'
            lane.active=True
            actions.append({'type': 'logistics_lane', 'status': 'updated', 'lane': lane_out(lane)})
        else:
            lane=LogisticsLane(
                tenant_id=payload.tenant_id,
                source_warehouse=payload.source_warehouse,
                target_warehouse=payload.target_warehouse,
                transit_days=payload.transit_days,
                cost_per_unit=payload.cost_per_unit,
                currency='CNY',
                active=True,
            )
            db.add(lane)
            db.flush()
            actions.append({'type': 'logistics_lane', 'status': 'created', 'lane': lane_out(lane)})
        if payload.set_stock:
            try:
                # Reset both sides of the demonstration route. A completed
                # transfer changes target stock, so restoring only the source
                # side would make the next Case incorrectly look resolved.
                for label, warehouse, desired_qty in (
                    ('source_stock', payload.source_warehouse, payload.source_qty),
                    ('target_stock', payload.target_warehouse, payload.target_qty),
                ):
                    before=erp.stock(payload.item_code, warehouse)
                    current_qty=float(before.get('actual_qty') or 0)
                    target_qty=float(desired_qty)
                    # ERPNext rejects a Stock Reconciliation with no quantity delta.
                    # A demo seed must be safely repeatable, so keep an already-correct
                    # sandbox unchanged instead of submitting a no-op transaction.
                    if abs(current_qty-target_qty) < 0.000001:
                        result=None
                        after=before
                        status='already_set'
                    else:
                        result=erp.set_stock_balance_for_fault_injection(
                            item_code=payload.item_code,
                            warehouse=warehouse,
                            qty=target_qty,
                            company=company,
                            difference_account=difference_account,
                            valuation_rate=valuation_rate,
                        )
                        after=erp.stock(payload.item_code, warehouse)
                        status='set'
                    actions.append({'type': label, 'status': status, 'before': before, 'after': after, 'erpnext_result': result})
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                raise HTTPException(502, {
                    'error': 'erpnext_sandbox_seed_failed',
                    'erpnext_status_code': status_code,
                    'message': 'ERPNext rejected the sandbox stock seed. Check integration user permissions and accounting fields.',
                }) from exc
        audit(db,identity,'sandbox_seeded','sandbox',payload.tenant_id,{'actions':actions})
        db.commit()
        check=sandbox_check_payload(db,payload)
        return {'status': check['status'], 'actions': actions, 'check': check}
@app.get('/')
def console():
    if not STATIC_DIR.exists(): raise HTTPException(404,'console static files not found')
    return FileResponse(STATIC_DIR/'index.html')


@app.post('/v1/webhooks/erpnext')
async def erp_webhook(request: Request, x_resolveops_signature: str=Header(...)):
    body=await request.body(); expected='sha256='+hmac.new(settings.webhook_secret.encode(),body,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,x_resolveops_signature): raise HTTPException(401,'invalid webhook signature')
    payload=json.loads(body)
    if payload.get('event') not in SUPPORTED_EVENTS: raise HTTPException(422,'unsupported event')
    if not payload.get('order_id') or not payload.get('tenant_id') or not payload.get('event_id'): raise HTTPException(422,'event_id, order_id and tenant_id required')
    with Session(engine) as db:
        existing=db.scalar(select(Case).where(Case.tenant_id==payload['tenant_id'], Case.source_event_id==payload['event_id']))
        if existing: return {'case_id':existing.id,'status':existing.status,'duplicate':True}
        event_type = 'delivery_delay' if payload['event'] == 'supplier_delay' else payload['event']
        case=Case(tenant_id=payload['tenant_id'],source_event_id=payload['event_id'],event_type=event_type,order_id=payload['order_id']); db.add(case)
        try: db.flush()
        except IntegrityError:
            # A concurrent delivery passed the read check. The unique index is
            # authoritative; return the Case created by the winning request.
            db.rollback()
            existing=db.scalar(select(Case).where(Case.tenant_id==payload['tenant_id'], Case.source_event_id==payload['event_id']))
            return {'case_id':existing.id,'status':existing.status,'duplicate':True}
        emit(db,case.id,'case_created','Trusted ERPNext webhook received.',{'event_id':payload.get('event_id'),'event_type':event_type,'source_event':payload.get('event')})
        db.add(Task(case_id=case.id,kind='investigate')); db.commit(); return {'case_id':case.id,'status':'queued','duplicate':False}
@app.get('/v1/cases')
def case_list(x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None), limit:int=50):
    limit=max(1,min(limit,100))
    with Session(engine) as db:
        operator_identity_from_db(db,x_operator_key)
        cases=db.scalars(select(Case).order_by(Case.updated_at.desc()).limit(limit)).all()
        result=[]
        for case in cases:
            approvals=db.scalars(select(Approval).where(Approval.case_id==case.id)).all()
            invocations=db.scalars(select(Invocation).where(Invocation.case_id==case.id)).all()
            latest=db.scalars(select(Event).where(Event.case_id==case.id).order_by(Event.created_at.desc()).limit(1)).first()
            actions=(case.plan or {}).get('actions',[]) if isinstance(case.plan,dict) else []
            result.append({
                'id':case.id,'tenant_id':case.tenant_id,'source_event_id':case.source_event_id,'event_type':case.event_type,'order_id':case.order_id,
                'status':case.status,'plan_version':case.plan_version,'created_at':case.created_at.isoformat() if case.created_at else None,
                'updated_at':case.updated_at.isoformat() if case.updated_at else None,
                'actions':[a.get('action_type') for a in actions],
                'approval_statuses':[a.status for a in approvals],
                'invocation_count':len(invocations),
                'latest_event':{'kind':latest.kind,'message':latest.message,'created_at':latest.created_at.isoformat() if latest.created_at else None} if latest else None,
            })
        return result
@app.post('/v1/cases')
def create_case(payload: CaseCreateIn, x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None)):
    event_type = 'delivery_delay' if payload.event_type == 'supplier_delay' else payload.event_type
    if payload.event_type not in SUPPORTED_EVENTS or event_type == 'supplier_delay':
        raise HTTPException(422,'unsupported event_type')
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        require_role(identity,'ops_admin','config_admin','sales_manager','warehouse_manager','procurement_manager','finance_manager')
        if payload.source_event_id:
            existing=db.scalar(select(Case).where(Case.tenant_id==payload.tenant_id, Case.source_event_id==payload.source_event_id))
            if existing:
                audit(db,identity,'case_create_duplicate','case',existing.id,{'source_event_id':payload.source_event_id,'event_type':event_type},case_id=existing.id)
                db.commit()
                return {'case_id':existing.id,'status':existing.status,'duplicate':True}
        case=Case(tenant_id=payload.tenant_id,source_event_id=payload.source_event_id,event_type=event_type,order_id=payload.order_id)
        db.add(case)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            if not payload.source_event_id:
                raise
            existing=db.scalar(select(Case).where(Case.tenant_id==payload.tenant_id, Case.source_event_id==payload.source_event_id))
            return {'case_id':existing.id,'status':existing.status,'duplicate':True}
        data={'source':'api','event_type':event_type,'requested_event_type':payload.event_type,'order_id':payload.order_id,'reason':payload.reason,'context':payload.context}
        emit(db,case.id,'case_created','Operator-created Case received.',data)
        audit(db,identity,'case_created','case',case.id,data,case_id=case.id)
        db.add(Task(case_id=case.id,kind='investigate',payload={'source':'api','context':payload.context,'reason':payload.reason}))
        db.commit()
        return {'case_id':case.id,'status':'queued','duplicate':False}

@app.post('/v1/cases/{case_id}/ask')
def ask_case(case_id:str, payload: CaseAskIn, x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None)):
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        case=db.scalar(select(Case).where(Case.id == case_id, Case.tenant_id == identity.tenant_id))
        if not case:
            raise HTTPException(404,'case not found')
        context=CaseContextBuilder(db).build(case_id, {'reason':'operator_case_question'})
        isolation=validate_case_context_isolation(context)
        if not isolation['allowed']:
            emit(db,case.id,'context_isolation_failed','Case question blocked because context isolation failed.',{'question':payload.question,'isolation':isolation})
            audit(db,identity,'case_question_blocked','case',case.id,{'question':payload.question,'isolation':isolation},case_id=case.id)
            db.commit()
            raise HTTPException(409, {'error':'context_isolation_failed','isolation':isolation})
        if isolation.get('warnings'):
            emit(db,case.id,'context_isolation_sanitized','Case question context was sanitized before LLM use.',{'question':payload.question,'isolation':isolation})

        emit(db,case.id,'case_question_asked','Operator asked a Case-scoped question.',{'question':payload.question,'actor':identity.subject,'role':identity.role})
        tools=BusinessReadTools(ERPNextAdapter(settings.erpnext_base_url,settings.erpnext_api_key,settings.erpnext_api_secret), case.event_type)

        def record_observation(observation: dict[str, Any]) -> None:
            emit(
                db,
                case.id,
                'case_question_tool_observation',
                f"Case question called read tool: {observation.get('tool')}.",
                observation,
            )

        answer=CaseQuestionAgent(tools).answer(
            order_id=case.order_id,
            question=payload.question,
            case_context=context,
            on_observation=record_observation,
        )
        emit(db,case.id,'case_question_answered','Agent answered a Case-scoped question without executing writes.',{
            'question':payload.question,
            'answer':answer.get('answer'),
            'used_tools':answer.get('used_tools') or [],
            'observation_count':len(answer.get('observations') or []),
        })
        audit(db,identity,'case_question_answered','case',case.id,{'question':payload.question,'used_tools':answer.get('used_tools') or []},case_id=case.id)
        db.commit()
        return {
            'case_id':case.id,
            'status':case.status,
            'event_type':case.event_type,
            'order_id':case.order_id,
            **answer,
        }

def _first_case_id(question: str) -> str | None:
    import re
    # ``\b`` is Unicode-aware in Python: a Chinese character immediately before
    # a UUID counts as a word character, so ``查看<uuid>`` was not detected.
    # Restrict the guards to UUID characters instead, which accepts natural
    # Chinese, punctuation and parentheses around a Case ID.
    match = re.search(
        r'(?<![0-9A-Za-z-])[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}(?![0-9A-Za-z-])',
        question,
    )
    return match.group(0) if match else None


def _case_id_from_business_reference(db: Session, identity: OperatorIdentity, question: str) -> str | None:
    """Resolve an unambiguous order/source-event reference within this tenant."""
    question_folded = question.casefold()
    candidates = list(db.scalars(
        select(Case).where(Case.tenant_id == identity.tenant_id).order_by(Case.updated_at.desc()).limit(200)
    ).all())
    matches = [
        case for case in candidates
        if case.order_id.casefold() in question_folded
        or (case.source_event_id and case.source_event_id.casefold() in question_folded)
    ]
    # The chat must not guess if a reference maps to more than one Case.
    return matches[0].id if len(matches) == 1 else None


def _is_case_follow_up(question: str) -> bool:
    """Whether a vague phrase is asking about the previously discussed Case."""
    import re
    normalized = question.casefold()
    has_reference = bool(re.search(r'这个|那个|该|它|刚才|上个|上一个|上述|前面|之前', normalized))
    has_case_subject = bool(re.search(r'case|订单|审批|执行|轨迹|异常|人工|状态|处理|库存|采购|计划|事件|调用', normalized))
    return has_reference and has_case_subject


def _is_implicit_case_follow_up(question: str) -> bool:
    """Recognize a contextual Case question such as “为什么还在等待审批”."""
    import re
    normalized = question.casefold()
    subject = bool(re.search(r'case|订单|审批|执行|轨迹|异常|人工|状态|处理|库存|采购|计划|事件|调用', normalized))
    follow_up = bool(re.search(r'为什么|怎么|如何|是否|还在|当前|下一步|结果|原因|情况', normalized))
    return subject and follow_up and not is_analytics_question(question)


def _recent_session_case_ids(db: Session, session: ChatSession | None, identity: OperatorIdentity) -> list[str]:
    if session is None:
        return []
    messages = list(db.scalars(
        select(ChatMessage).where(ChatMessage.session_id == session.id).order_by(ChatMessage.created_at.desc()).limit(30)
    ).all())
    seen: list[str] = []
    for message in messages:
        case_id = str((message.references or {}).get('case_id') or '')
        if case_id and case_id not in seen:
            seen.append(case_id)
    if not seen:
        return []
    visible = set(db.scalars(select(Case.id).where(
        Case.tenant_id == identity.tenant_id,
        Case.id.in_(seen),
    )).all())
    return [case_id for case_id in seen if case_id in visible]


def _case_reference_clarification(db: Session, case_ids: list[str]) -> str:
    cases = list(db.scalars(select(Case).where(Case.id.in_(case_ids))).all())
    by_id = {case.id: case for case in cases}
    choices = [
        f'{by_id[case_id].order_id}（{case_id[:8]}…）'
        for case_id in case_ids if case_id in by_id
    ]
    if choices:
        return '本次对话里提到过多个 Case：' + '、'.join(choices) + '。请说明要查询哪一个。'
    return '请提供要查询的 Case ID 或订单号。'


def _needs_erp_identifier(question: str) -> bool:
    """Detect a real-time ERP question that lacks an unambiguous business object."""
    import re
    normalized = question.casefold()
    asks_erp = bool(re.search(r'erpnext|\berp\b|实时.*(?:订单|库存|客户|物料|采购)|(?:订单|库存|客户|物料|采购).*实时', normalized))
    broad_request = bool(re.search(r'所有数据|全部数据|全量|整个(?:erp|系统)|所有订单|全部订单', normalized))
    return asks_erp and broad_request


def _erp_identifier_clarification() -> str:
    return (
        '可以查询 ERPNext 的实时数据，但“所有数据”范围不明确，也不会批量导出整个 ERP。'
        '请说明要查的对象和范围，例如销售订单号、物料编码与仓库、客户名称，或采购订单与时间范围。'
    )


def _erp_order_id_from_question(question: str) -> str | None:
    """Extract an explicit Sales Order-like identifier without guessing names."""
    import re
    if not re.search(r'erpnext|\berp\b|订单|sales\s*order', question, re.IGNORECASE):
        return None
    match = re.search(
        r'(?<![A-Za-z0-9_-])([A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+){2,})(?![A-Za-z0-9_-])',
        question,
    )
    return match.group(1) if match else None


def _erp_order_answer_from_chat(question: str, order_id: str) -> dict[str, Any]:
    """Read one explicitly named ERPNext Sales Order and expose a compact fact view."""
    try:
        order = ERPNextAdapter(
            settings.erpnext_base_url, settings.erpnext_api_key, settings.erpnext_api_secret,
        ).sales_order(order_id)
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code == 404:
            answer = f'ERPNext 中没有找到销售订单 {order_id}，或当前连接身份无权读取它。'
        elif status_code in {401, 403}:
            answer = 'ERPNext 拒绝了当前只读查询，请检查连接配置或 API 用户权限。'
        else:
            answer = f'ERPNext 查询订单 {order_id} 失败（HTTP {status_code or "未知"}）。'
        return {'question': question, 'answer': answer, 'route': 'erpnext', 'source': 'erpnext', 'tools_used': ['get_order']}
    except httpx.RequestError:
        return {
            'question': question,
            'answer': '暂时无法连接 ERPNext，无法确认该订单的实时数据。请检查 ERPNext 服务和连接配置后重试。',
            'route': 'erpnext', 'source': 'erpnext', 'tools_used': ['get_order'],
        }

    item_lines = []
    for item in (order.get('items') or [])[:8]:
        code = item.get('item_code') or item.get('item_name') or '未命名物料'
        qty = item.get('qty')
        delivered = item.get('delivered_qty')
        warehouse = item.get('warehouse')
        fragments = [str(code)]
        if qty is not None:
            fragments.append(f'数量 {qty}')
        if delivered is not None:
            fragments.append(f'已交付 {delivered}')
        if warehouse:
            fragments.append(f'仓库 {warehouse}')
        item_lines.append('；'.join(fragments))
    facts = [
        f'订单：{order.get("name") or order_id}',
        f'状态：{order.get("status") or "未返回"}',
        f'客户：{order.get("customer") or "未返回"}',
    ]
    if order.get('transaction_date'):
        facts.append(f'下单日期：{order["transaction_date"]}')
    if order.get('delivery_date'):
        facts.append(f'交付日期：{order["delivery_date"]}')
    if order.get('grand_total') is not None:
        facts.append(f'金额：{order["grand_total"]} {order.get("currency") or ""}'.strip())
    answer = 'ERPNext 实时订单信息\n' + '\n'.join(facts)
    if item_lines:
        answer += '\n物料：\n' + '\n'.join(f'- {line}' for line in item_lines)
    return {
        'question': question, 'answer': answer, 'route': 'erpnext', 'source': 'erpnext',
        'tools_used': ['get_order'], 'order_id': order.get('name') or order_id,
    }


def _is_erp_workspace_question(question: str) -> bool:
    import re
    return bool(re.search(
        r'erpnext|\berp\b|仓库|库存|销售订单|采购订单|客户|物料|商品|价格|在途采购',
        question, re.IGNORECASE,
    ))


def _direct_erp_list_tool(question: str) -> tuple[str, str, str] | None:
    """Return a certain list intent; ambiguous or compound questions use the Agent."""
    import re
    normalized = question.casefold()
    list_cue = bool(re.search(r'有哪些|哪些|列出|列表|最近|全部|所有', normalized))
    if not list_cue:
        return None
    candidates = [
        ('仓库', 'list_warehouses', 'warehouses'),
        (r'销售订单|sales\s*order', 'list_sales_orders', 'sales_orders'),
        (r'采购订单|purchase\s*order', 'list_purchase_orders', 'purchase_orders'),
        ('客户', 'list_customers', 'customers'),
        ('物料|商品', 'list_items', 'items'),
    ]
    matched = [item for item in candidates if re.search(item[0], normalized, re.IGNORECASE)]
    return matched[0] if len(matched) == 1 else None


def _direct_erp_list_answer(question: str, tool_name: str, result_key: str) -> dict[str, Any]:
    tools = ERPWorkspaceReadTools(
        ERPNextAdapter(settings.erpnext_base_url, settings.erpnext_api_key, settings.erpnext_api_secret)
    )
    result = tools.execute_result(tool_name, {}, '')
    if result.status != 'success':
        return {
            'question': question, 'route': 'erpnext', 'source': 'erpnext', 'tools_used': [tool_name],
            'answer': 'ERPNext 实时查询未完成。请检查 ERPNext 服务、连接配置和当前 API 用户的读取权限后重试。',
        }
    rows = (result.data or {}).get(result_key) or []
    names = [str(row.get('warehouse_name') or row.get('customer_name') or row.get('item_name') or row.get('name') or '') for row in rows]
    names = [name for name in names if name]
    labels = {
        'warehouses': '仓库', 'sales_orders': '销售订单', 'purchase_orders': '采购订单',
        'customers': '客户', 'items': '物料',
    }
    label = labels[result_key]
    if not names:
        answer = f'ERPNext 当前没有返回可读取的{label}。'
    else:
        answer = f'ERPNext 实时返回 {len(names)} 个{label}：\n' + '\n'.join(f'- {name}' for name in names)
    return {
        'question': question, 'answer': answer, 'route': 'erpnext', 'source': 'erpnext',
        'tools_used': [tool_name], 'rows': rows, 'row_count': len(rows),
    }


def _erp_workspace_answer_from_chat(question: str) -> dict[str, Any]:
    """Answer a non-Case operational question with typed ERPNext read tools."""
    tools = ERPWorkspaceReadTools(
        ERPNextAdapter(settings.erpnext_base_url, settings.erpnext_api_key, settings.erpnext_api_secret)
    )
    context = {
        'scope': {'mode': 'erpnext_workspace_read', 'source_system': 'ERPNext'},
        'current_state': {'status': 'read_only', 'case_id': None, 'order_id': None},
        'instruction': (
            'This is a direct ERPNext read-only inquiry, not a Case. Use the typed ERP tools when they can answer the question. '
            'If the requested object is ambiguous, ask one concise clarifying question. Do not describe schemas in place of a query.'
        ),
    }
    answer = CaseQuestionAgent(tools).answer(
        order_id='', question=question, case_context=context, on_observation=lambda _observation: None,
    )
    return {
        'question': question, 'route': 'erpnext', 'source': 'erpnext',
        **answer,
    }


def _session_detail(db: Session, session: ChatSession) -> dict[str, Any]:
    messages = list(db.scalars(
        select(ChatMessage).where(ChatMessage.session_id == session.id).order_by(ChatMessage.created_at)
    ).all())
    memory = db.scalar(select(ChatMemory).where(ChatMemory.session_id == session.id))
    references = [message.references or {} for message in messages]
    attachments = [attachment for message in messages for attachment in (message.attachments or [])]
    case_ids = list(dict.fromkeys(str(ref['case_id']) for ref in references if ref.get('case_id')))
    memory_ids = list(dict.fromkeys(str(memory_id) for ref in references for memory_id in (ref.get('memory_ids') or [])))
    local_file_read_count = sum(int(ref.get('local_file_read_count') or 0) for ref in references)
    return {
        **session_out(session, message_count=len(messages), memory=memory),
        'messages': [message_out(message) for message in messages],
        'context': {
            'summary': session.summary or '尚未形成会话摘要。',
            'case_ids': case_ids,
            'attachments': attachments,
            'local_file_read_count': local_file_read_count,
            'memory_ids': memory_ids,
            'memory_saved': memory is not None,
        },
    }


def _case_answer_from_chat(db: Session, identity: OperatorIdentity, question: str, case_id: str) -> dict[str, Any] | None:
    case = db.scalar(select(Case).where(Case.id == case_id, Case.tenant_id == identity.tenant_id))
    if not case:
        return None
    context = CaseContextBuilder(db).build(case.id, {'reason': 'operator_chat_case_question'})
    isolation = validate_case_context_isolation(context)
    if not isolation['allowed']:
        raise HTTPException(409, {'error': 'context_isolation_failed', 'isolation': isolation})
    tools = BusinessReadTools(ERPNextAdapter(settings.erpnext_base_url, settings.erpnext_api_key, settings.erpnext_api_secret), case.event_type)

    # A main-chat read must not append operational events to the Case trace.
    # The normal audit record below remains sufficient for accountability.
    def record_observation(_observation: dict[str, Any]) -> None:
        return None
    answer = CaseQuestionAgent(tools).answer(
        order_id=case.order_id, question=question, case_context=context, on_observation=record_observation,
    )
    return {
        'question': question, 'route': 'case', 'case_id': case.id, 'status': case.status,
        'event_type': case.event_type, 'order_id': case.order_id, **answer,
    }


def _selected_chat_memories(db: Session, identity: OperatorIdentity, session: ChatSession | None, question: str, agent: OperatorChatAgent) -> list[dict[str, str]]:
    if session is None or not hasattr(agent, 'select_relevant_memories'):
        return []
    candidates = active_memories(db, tenant_id=identity.tenant_id, subject=identity.subject, exclude_session_id=session.id)
    public = [{'id': item.id, 'title': item.title, 'content': item.content} for item in candidates]
    selected_ids = set(agent.select_relevant_memories(question, public))
    return [item for item in public if item['id'] in selected_ids]


def _answer_operator_chat(
    db: Session,
    identity: OperatorIdentity,
    payload: OperatorChatIn,
    attachments: list[ChatAttachment] | None = None,
    session: ChatSession | None = None,
):
    attachments = attachments or []
    uploaded_metadata = [attachment.audit_metadata() for attachment in attachments]
    history = recent_history(db, session.id) if session else payload.history
    if session:
        if session.title == '新对话' and not history:
            session.title = payload.question.strip().replace('\n', ' ')[:32] or '新对话'
        add_message(db, session, role='user', content=payload.question, attachments=uploaded_metadata)

    local_file_selection: dict[str, Any] | None = None
    agent = OperatorChatAgent()
    if not attachments and is_local_file_question(payload.question):
        local_tool = LocalFileReadTool()
        candidates = local_tool.find_candidates(payload.question)
        if candidates:
            selected_paths = agent.plan_local_file_reads(payload.question, [candidate.to_public() for candidate in candidates])
            attachments = local_tool.read_selected(candidates, selected_paths)
            local_file_selection = {
                'candidate_count': len(candidates), 'selected_paths': selected_paths, 'read_count': len(attachments),
            }

    result: dict[str, Any]
    case_id = _first_case_id(payload.question) if not attachments else None
    if not case_id and not attachments:
        case_id = _case_id_from_business_reference(db, identity, payload.question)
    referenced_case_ids = _recent_session_case_ids(db, session, identity) if not attachments else []
    clarification: str | None = None
    if not case_id and _is_case_follow_up(payload.question):
        if len(referenced_case_ids) == 1:
            case_id = referenced_case_ids[0]
        elif len(referenced_case_ids) > 1:
            clarification = _case_reference_clarification(db, referenced_case_ids)
        else:
            clarification = '请提供要查询的 Case ID 或订单号，我会读取它的状态、计划、审批和执行轨迹。'
    elif not case_id and len(referenced_case_ids) == 1 and _is_implicit_case_follow_up(payload.question):
        case_id = referenced_case_ids[0]

    erp_order_id = _erp_order_id_from_question(payload.question) if not case_id and not attachments else None
    erp_list_intent = _direct_erp_list_tool(payload.question) if not case_id and not attachments else None
    if case_id:
        result = _case_answer_from_chat(db, identity, payload.question, case_id) or {}
        if not result:
            result = {'question': payload.question, 'route': 'chat', 'source': 'system', 'tools_used': [],
                      'answer': '当前租户中没有找到这个 Case，或你没有查看它的权限。请检查 Case ID 是否完整。'}
    elif clarification:
        result = {'question': payload.question, 'route': 'clarification', 'source': 'clarification', 'tools_used': [],
                  'answer': clarification}
    elif erp_order_id:
        result = _erp_order_answer_from_chat(payload.question, erp_order_id)
    elif not attachments and _needs_erp_identifier(payload.question):
        result = {'question': payload.question, 'route': 'clarification', 'source': 'clarification', 'tools_used': [],
                  'answer': _erp_identifier_clarification()}
    elif erp_list_intent:
        tool_name, result_key = erp_list_intent[1], erp_list_intent[2]
        result = _direct_erp_list_answer(payload.question, tool_name, result_key)
    elif not attachments and _is_erp_workspace_question(payload.question):
        result = _erp_workspace_answer_from_chat(payload.question)
    elif not attachments and is_analytics_question(payload.question):
        try:
            analytics_result = AnalyticsAgent().run(question=payload.question, db=db, tenant_id=identity.tenant_id)
        except AnalyticsQueryError as exc:
            audit(db, identity, 'analytics_query_rejected', 'operator_chat', identity.subject, {
                'question': payload.question, 'reason': str(exc), 'route': 'analytics',
            })
            db.commit()
            raise HTTPException(422, str(exc)) from exc
        result = {
            'question': payload.question, 'answer': analytics_result.answer, 'route': 'analytics',
            'source': 'analytics', 'sql': analytics_result.sql, 'rows': analytics_result.rows,
            'row_count': analytics_result.row_count, 'truncated': analytics_result.truncated, 'llm': analytics_result.llm,
        }
    else:
        memories = _selected_chat_memories(db, identity, session, payload.question, agent)
        kwargs: dict[str, Any] = {'history': history}
        if attachments:
            kwargs['attachments'] = attachments
        if memories:
            kwargs['memories'] = memories
        result = agent.answer(payload.question, **kwargs)
        result['route'] = 'chat'

    references = {
        'case_id': result.get('case_id'),
        'memory_ids': result.get('memory_ids') or [],
        'local_file_read_count': (local_file_selection or {}).get('read_count', 0),
    }
    audit(db, identity, 'operator_chat_answered', 'operator_chat', session.id if session else identity.subject, {
        'question': payload.question, 'history_items': len(history or []), 'source': result.get('source'),
        'route': result.get('route'), 'tools_used': result.get('used_tools') or [],
        'attachments': uploaded_metadata, 'local_file_selection': local_file_selection, 'llm': result.get('llm') or {},
    }, case_id=result.get('case_id'))
    if session:
        add_message(db, session, role='assistant', content=result.get('answer') or '未获得回答。', route=result.get('route'), references=references)
        update_summary(session, recent_history(db, session.id, limit=8))
        result['session'] = _session_detail(db, session)
    db.commit()
    return result


@app.post('/v1/chat')
def operator_chat(payload: OperatorChatIn, x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None)):
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        session = session_for_identity(db, payload.session_id, tenant_id=identity.tenant_id, subject=identity.subject) if payload.session_id else None
        if payload.session_id and session is None:
            raise HTTPException(404, 'chat session not found')
        return _answer_operator_chat(db, identity, payload, session=session)


@app.post('/v1/chat/attachments')
def operator_chat_with_attachments(payload: OperatorChatAttachmentIn, x_operator_key: str | None = Header(default=None)):
    try:
        attachments = decode_attachments([item.model_dump() for item in payload.attachments])
    except AttachmentError as exc:
        raise HTTPException(422, str(exc)) from exc
    with Session(engine) as db:
        identity = operator_identity_from_db(db, x_operator_key)
        session = session_for_identity(db, payload.session_id, tenant_id=identity.tenant_id, subject=identity.subject) if payload.session_id else None
        if payload.session_id and session is None:
            raise HTTPException(404, 'chat session not found')
        return _answer_operator_chat(db, identity, payload, attachments, session=session)

@app.get('/v1/chat/sessions')
def list_chat_sessions(x_operator_key: str | None = Header(default=None)):
    with Session(engine) as db:
        identity = operator_identity_from_db(db, x_operator_key)
        sessions = list(db.scalars(select(ChatSession).where(
            ChatSession.tenant_id == identity.tenant_id, ChatSession.operator_subject == identity.subject,
        ).order_by(ChatSession.updated_at.desc()).limit(100)).all())
        memories = {item.session_id: item for item in db.scalars(select(ChatMemory).where(
            ChatMemory.tenant_id == identity.tenant_id, ChatMemory.operator_subject == identity.subject,
        )).all()}
        return [session_out(item, message_count=db.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.session_id == item.id)) or 0, memory=memories.get(item.id)) for item in sessions]


@app.post('/v1/chat/sessions')
def create_chat_session(payload: ChatSessionCreateIn, x_operator_key: str | None = Header(default=None)):
    with Session(engine) as db:
        identity = operator_identity_from_db(db, x_operator_key)
        session = ChatSession(tenant_id=identity.tenant_id, operator_subject=identity.subject, title=(payload.title or '新对话').strip()[:160] or '新对话')
        db.add(session); db.flush(); audit(db, identity, 'chat_session_created', 'chat_session', session.id, {}); db.commit()
        return _session_detail(db, session)


@app.get('/v1/chat/sessions/{session_id}')
def get_chat_session(session_id: str, x_operator_key: str | None = Header(default=None)):
    with Session(engine) as db:
        identity = operator_identity_from_db(db, x_operator_key)
        session = session_for_identity(db, session_id, tenant_id=identity.tenant_id, subject=identity.subject)
        if not session: raise HTTPException(404, 'chat session not found')
        return _session_detail(db, session)


@app.patch('/v1/chat/sessions/{session_id}')
def rename_chat_session(session_id: str, payload: ChatSessionRenameIn, x_operator_key: str | None = Header(default=None)):
    with Session(engine) as db:
        identity = operator_identity_from_db(db, x_operator_key)
        session = session_for_identity(db, session_id, tenant_id=identity.tenant_id, subject=identity.subject)
        if not session: raise HTTPException(404, 'chat session not found')
        session.title = payload.title.strip(); audit(db, identity, 'chat_session_renamed', 'chat_session', session.id, {}); db.commit()
        return _session_detail(db, session)


@app.delete('/v1/chat/sessions/{session_id}')
def remove_chat_session(session_id: str, payload: ChatSessionDeleteIn, x_operator_key: str | None = Header(default=None)):
    with Session(engine) as db:
        identity = operator_identity_from_db(db, x_operator_key)
        session = session_for_identity(db, session_id, tenant_id=identity.tenant_id, subject=identity.subject)
        if not session: raise HTTPException(404, 'chat session not found')
        delete_session(db, session, delete_memory=payload.delete_memory)
        audit(db, identity, 'chat_session_deleted', 'chat_session', session_id, {'delete_memory': payload.delete_memory})
        db.commit()
    return None


@app.post('/v1/chat/sessions/{session_id}/memory')
def persist_chat_memory(session_id: str, x_operator_key: str | None = Header(default=None)):
    with Session(engine) as db:
        identity = operator_identity_from_db(db, x_operator_key)
        session = session_for_identity(db, session_id, tenant_id=identity.tenant_id, subject=identity.subject)
        if not session: raise HTTPException(404, 'chat session not found')
        messages = recent_history(db, session.id, limit=30)
        content = redact(OperatorChatAgent().summarize_memory(session.title, messages)) or session.title
        memory = db.scalar(select(ChatMemory).where(ChatMemory.session_id == session.id))
        if memory is None:
            memory = ChatMemory(session_id=session.id, tenant_id=identity.tenant_id, operator_subject=identity.subject, title=session.title, content=content)
            db.add(memory)
        else:
            memory.title, memory.content = session.title, content
        session.memory_saved_at = datetime.now(UTC)
        audit(db, identity, 'chat_memory_persisted', 'chat_session', session.id, {})
        db.commit()
        return _session_detail(db, session)


@app.delete('/v1/chat/sessions/{session_id}/memory')
def remove_chat_memory(session_id: str, x_operator_key: str | None = Header(default=None)):
    with Session(engine) as db:
        identity = operator_identity_from_db(db, x_operator_key)
        session = session_for_identity(db, session_id, tenant_id=identity.tenant_id, subject=identity.subject)
        if not session: raise HTTPException(404, 'chat session not found')
        memory = db.scalar(select(ChatMemory).where(ChatMemory.session_id == session.id))
        if memory: db.delete(memory)
        session.memory_saved_at = None
        audit(db, identity, 'chat_memory_deleted', 'chat_session', session.id, {})
        db.commit()
        return _session_detail(db, session)

@app.post('/v1/chat/stream')
def operator_chat_stream(payload: OperatorChatIn, x_operator_key:str|None=Header(default=None)):
    """SSE for the Workbench General view. General chat has no ERP tools."""
    with Session(engine) as db:
        identity=operator_identity_from_db(db, x_operator_key)
    def events():
        completed: dict[str, Any] | None = None
        for item in OperatorChatAgent().stream_answer(payload.question, history=payload.history):
            kind = str(item.get('type') or 'message')
            if kind == 'done':
                completed = item
            yield sse_event(kind, item)
        if completed is not None:
            with Session(engine) as db:
                audit(db, identity, 'operator_chat_streamed', 'operator_chat', identity.subject, {
                    'question': payload.question, 'history_items': len(payload.history or []),
                    'source': completed.get('source'), 'tools_used': [], 'streaming': True,
                })
                db.commit()
    return sse_response(events())

@app.post('/v1/analytics/query')
def analytics_query(payload: AnalyticsQueryIn, x_operator_key: str | None = Header(default=None)):
    """Natural-language, read-only analysis of tenant-scoped runtime records."""
    with Session(engine) as db:
        identity = operator_identity_from_db(db, x_operator_key)
        try:
            result = AnalyticsAgent().run(question=payload.question, db=db, tenant_id=identity.tenant_id)
        except AnalyticsQueryError as exc:
            audit(db, identity, 'analytics_query_rejected', 'analytics_query', identity.subject, {
                'question': payload.question, 'reason': str(exc),
            })
            db.commit()
            raise HTTPException(422, str(exc)) from exc
        audit(db, identity, 'analytics_query_executed', 'analytics_query', identity.subject, {
            'question': payload.question,
            'sql': result.sql,
            'row_count': result.row_count,
            'truncated': result.truncated,
            'llm': result.llm,
        })
        db.commit()
        return {
            'answer': result.answer,
            'sql': result.sql,
            'rows': result.rows,
            'row_count': result.row_count,
            'truncated': result.truncated,
            'llm': result.llm,
        }

@app.post('/v1/cases/{case_id}/ask/stream')
def ask_case_stream(case_id:str, payload: CaseAskIn, x_operator_key:str|None=Header(default=None)):
    """SSE Case Q&A: server-controlled read tools, streamed final answer."""
    with Session(engine) as db:
        identity=operator_identity_from_db(db, x_operator_key)
        case=db.scalar(select(Case).where(Case.id == case_id, Case.tenant_id == identity.tenant_id))
        if not case:
            raise HTTPException(404, 'case not found')
        context=CaseContextBuilder(db).build(case_id, {'reason':'operator_case_question_stream'})
        isolation=validate_case_context_isolation(context)
        if not isolation['allowed']:
            raise HTTPException(409, {'error':'context_isolation_failed','isolation':isolation})
        order_id, event_type, durable_case_id = case.order_id, case.event_type, case.id
        emit(db, durable_case_id, 'case_question_asked', 'Operator asked a streaming Case-scoped question.', {
            'question': payload.question, 'actor': identity.subject, 'role': identity.role, 'streaming': True,
        })
        db.commit()
    def events():
        completed: dict[str, Any] | None = None
        tools=BusinessReadTools(ERPNextAdapter(settings.erpnext_base_url, settings.erpnext_api_key, settings.erpnext_api_secret), event_type)
        def record_observation(observation: dict[str, Any]) -> None:
            with Session(engine) as event_db:
                emit(event_db, durable_case_id, 'case_question_tool_called', f"Case question called read tool: {observation.get('tool')}.", {
                    'tool': observation.get('tool'), 'arguments': observation.get('arguments') or {},
                })
                emit(event_db, durable_case_id, 'case_question_tool_observation', f"Case question called read tool: {observation.get('tool')}.", observation)
                event_db.commit()
        for item in CaseQuestionAgent(tools).stream_answer(
            order_id=order_id, question=payload.question, case_context=context, on_observation=record_observation,
        ):
            kind=str(item.get('type') or 'message')
            if kind == 'done':
                completed=item
            yield sse_event(kind, item)
        if completed is not None:
            with Session(engine) as event_db:
                emit(event_db, durable_case_id, 'case_question_answered', 'Agent streamed a Case-scoped answer without executing writes.', {
                    'question': payload.question, 'answer': completed.get('answer'),
                    'used_tools': completed.get('used_tools') or [], 'streaming': True,
                })
                audit(event_db, identity, 'case_question_streamed', 'case', durable_case_id, {
                    'question': payload.question, 'used_tools': completed.get('used_tools') or [], 'streaming': True,
                }, case_id=durable_case_id)
                event_db.commit()
    return sse_response(events())
@app.get('/v1/config/logistics-lanes')
def logistics_lanes(x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None), tenant_id:str='demo', active:bool|None=None):
    with Session(engine) as db:
        operator_identity_from_db(db,x_operator_key)
        query=select(LogisticsLane).where(LogisticsLane.tenant_id==tenant_id).order_by(LogisticsLane.source_warehouse,LogisticsLane.target_warehouse)
        if active is not None: query=query.where(LogisticsLane.active.is_(active))
        return [lane_out(lane) for lane in db.scalars(query).all()]
@app.post('/v1/config/logistics-lanes')
def upsert_logistics_lane(payload: LogisticsLaneIn, x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None)):
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        require_role(identity,'config_admin','ops_admin')
        lane=db.scalar(select(LogisticsLane).where(
            LogisticsLane.tenant_id==payload.tenant_id,
            LogisticsLane.source_warehouse==payload.source_warehouse,
            LogisticsLane.target_warehouse==payload.target_warehouse,
        ))
        if lane is None:
            lane=LogisticsLane(**payload.model_dump()); db.add(lane)
        else:
            for key,value in payload.model_dump().items(): setattr(lane,key,value)
        db.flush()
        audit(db,identity,'logistics_lane_upsert','logistics_lane',lane.id,{'lane':lane_out(lane)})
        db.commit(); db.refresh(lane); return lane_out(lane)
@app.get('/v1/audit')
def audit_logs(x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None), case_id:str|None=None, limit:int=100):
    limit=max(1,min(limit,200))
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        require_role(identity,'ops_admin','config_admin')
        query=select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
        if case_id: query=select(AuditLog).where(AuditLog.case_id==case_id).order_by(AuditLog.created_at.desc()).limit(limit)
        return [audit_out(log) for log in db.scalars(query).all()]
@app.get('/v1/evals/summary')
def eval_summary(x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None), limit:int=50, suite:str|None=None, source_event_prefix:str|None=None):
    limit=max(1,min(limit,200))
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        require_role(identity,'ops_admin','config_admin')
        query=select(Case).order_by(Case.updated_at.desc()).limit(limit)
        prefix=source_event_prefix
        if suite:
            prefix=f'eval:{suite}:'
        if prefix:
            query=select(Case).where(Case.source_event_id.like(f'{prefix}%')).order_by(Case.updated_at.desc()).limit(limit)
        cases=db.scalars(query).all()
        rows=[]
        for case in cases:
            events=db.scalars(select(Event).where(Event.case_id==case.id).order_by(Event.created_at)).all()
            approvals=db.scalars(select(Approval).where(Approval.case_id==case.id)).all()
            invocations=db.scalars(select(Invocation).where(Invocation.case_id==case.id)).all()
            tasks=db.scalars(select(Task).where(Task.case_id==case.id)).all()
            rows.append(eval_case_out(case,events,approvals,invocations,tasks))
        result=eval_summary_out(rows)
        result['eval_suite']=suite
        result['source_event_prefix']=prefix
        return result

@app.get('/v1/evals/cases/{case_id}')
def eval_case(case_id:str, x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None)):
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        require_role(identity,'ops_admin','config_admin')
        case=db.get(Case,case_id)
        if not case:
            raise HTTPException(404,'case not found')
        events=db.scalars(select(Event).where(Event.case_id==case.id).order_by(Event.created_at)).all()
        approvals=db.scalars(select(Approval).where(Approval.case_id==case.id)).all()
        invocations=db.scalars(select(Invocation).where(Invocation.case_id==case.id)).all()
        tasks=db.scalars(select(Task).where(Task.case_id==case.id)).all()
        return eval_case_out(case,events,approvals,invocations,tasks)

@app.get('/v1/fault-injections')
def fault_injection_catalog(x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None)):
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        require_role(identity,'ops_admin','config_admin')
        return {
            'enabled': bool(settings.enable_fault_injection),
            'app_env': settings.app_env,
            'faults': [{
                'fault_type': 'inventory_changed_before_execution',
                'description': 'Use ERPNext Stock Reconciliation through ResolveOps to change sandbox stock before an approved write executes.',
                'required_fields': ['item_code', 'warehouse', 'new_qty'],
                'optional_fields': ['case_id', 'company', 'difference_account', 'valuation_rate', 'reason'],
                'safety': 'Forbidden in production; requires ENABLE_FAULT_INJECTION=true and ops_admin/config_admin.',
            }],
        }

@app.post('/v1/fault-injections/run')
def run_fault_injection(payload: FaultInjectionRunIn, x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None)):
    require_fault_injection_enabled()
    company=payload.company or settings.erpnext_company
    difference_account=payload.difference_account or settings.erpnext_stock_difference_account
    valuation_rate=payload.valuation_rate if payload.valuation_rate is not None else settings.erpnext_default_valuation_rate
    if not company:
        raise HTTPException(422, 'company is required for ERPNext Stock Reconciliation')
    if not difference_account:
        raise HTTPException(422, 'difference_account is required for ERPNext Stock Reconciliation')
    erp=ERPNextAdapter(settings.erpnext_base_url,settings.erpnext_api_key,settings.erpnext_api_secret)
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        require_role(identity,'ops_admin','config_admin')
        case=None
        if payload.case_id:
            case=db.get(Case,payload.case_id)
            if not case:
                raise HTTPException(404,'case not found')
        before=erp.stock(payload.item_code,payload.warehouse)
        try:
            result=erp.set_stock_balance_for_fault_injection(
                item_code=payload.item_code,
                warehouse=payload.warehouse,
                qty=payload.new_qty,
                company=company,
                difference_account=difference_account,
                valuation_rate=valuation_rate,
            )
            after=erp.stock(payload.item_code,payload.warehouse)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            raise HTTPException(
                502,
                {
                    'error': 'erpnext_fault_injection_failed',
                    'erpnext_status_code': status_code,
                    'message': 'ERPNext rejected the Stock Reconciliation request. Check the API user permissions and required accounting fields.',
                },
            ) from exc
        data={
            'fault_type': payload.fault_type,
            'item_code': payload.item_code,
            'warehouse': payload.warehouse,
            'new_qty': payload.new_qty,
            'before': before,
            'after': after,
            'erpnext_result': result,
            'reason': payload.reason,
        }
        if case:
            emit(db,case.id,'fault_injected','Fault injection changed ERPNext sandbox business state through ResolveOps.',data)
        audit(db,identity,'fault_injection_run','fault_injection',payload.fault_type,data,case_id=case.id if case else None)
        db.commit()
        return {'status':'applied', **data}
@app.get('/v1/cases/{case_id}')
def case_detail(case_id:str, x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None)):
    with Session(engine) as db:
        operator_identity_from_db(db,x_operator_key)
        case=db.get(Case,case_id)
        if not case: raise HTTPException(404,'case not found')
        events=db.scalars(select(Event).where(Event.case_id==case_id).order_by(Event.created_at)).all()
        approvals=db.scalars(select(Approval).where(Approval.case_id==case_id).order_by(Approval.plan_version,Approval.id)).all()
        invocations=db.scalars(select(Invocation).where(Invocation.case_id==case_id)).all()
        tasks=db.scalars(select(Task).where(Task.case_id==case_id)).all()
        return {
            'id':case.id,'tenant_id':case.tenant_id,'source_event_id':case.source_event_id,'event_type':case.event_type,'order_id':case.order_id,
            'status':case.status,'plan_version':case.plan_version,'plan':case.plan,'evidence':case.evidence,
            'tool_trace':case_tool_trace(case),
            'agent_decision':case_agent_decision(case),
            'created_at':case.created_at.isoformat() if case.created_at else None,'updated_at':case.updated_at.isoformat() if case.updated_at else None,
            'approvals':[approval_out(a) for a in approvals],
            'invocations':[invocation_out(i) for i in invocations],
            'tasks':[task_out(t) for t in tasks],
            'events':[event_out(e) for e in events],
        }

@app.get('/v1/cases/{case_id}/metrics')
def case_run_metrics(case_id:str, x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None)):
    """Read technical observability facts for the selected Case.

    Any authenticated operator who can inspect the Case can inspect its own
    run metrics.  Aggregate evaluation scores remain under the ops-only eval
    endpoints and are deliberately excluded here.
    """
    with Session(engine) as db:
        operator_identity_from_db(db,x_operator_key)
        case=db.get(Case,case_id)
        if not case: raise HTTPException(404,'case not found')
        events=db.scalars(select(Event).where(Event.case_id==case_id).order_by(Event.created_at)).all()
        approvals=db.scalars(select(Approval).where(Approval.case_id==case_id)).all()
        invocations=db.scalars(select(Invocation).where(Invocation.case_id==case_id)).all()
        tasks=db.scalars(select(Task).where(Task.case_id==case_id)).all()
        return case_run_metrics_out(case,events,approvals,invocations,tasks)

@app.post('/v1/approvals/{approval_id}/approve')
def approve(approval_id:str, x_operator_key:str|None=Header(default=None, alias='X-Operator-Key'), x_operator:str|None=Header(default=None, alias='X-Operator'), x_operator_role:str|None=Header(default=None, alias='X-Operator-Role')):
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        a=db.scalar(select(Approval).where(Approval.id==approval_id).with_for_update())
        if not a: raise HTTPException(404,'approval not found')
        case=db.get(Case,a.case_id)
        if a.status!='pending': raise HTTPException(409,'approval unavailable')
        if approval_is_expired(a):
            a.status='expired'
            if case: case.status='manual_review'
            data={'approval_id':a.id,'expires_at':a.expires_at.isoformat() if a.expires_at else None,'action_hash':a.action_hash,'plan_version':a.plan_version}
            emit(db,a.case_id,'approval_expired','Approval expired before all required roles approved it.',data)
            audit(db,identity,'approval_expired','approval',a.id,data,case_id=a.case_id)
            db.commit()
            raise HTTPException(409,'approval expired')
        role=identity.role; required=set(a.required_roles or ['warehouse_manager'])
        if role not in required:
            audit(db,identity,'approval_rejected','approval',a.id,{'reason':'role_not_required','required_roles':sorted(required),'action_hash':a.action_hash,'plan_version':a.plan_version,'action_type':a.action.get('action_type')},case_id=a.case_id)
            db.commit()
            raise HTTPException(403,'operator role is not required for this approval')
        approved=set(a.approved_roles or []); approved.add(role); a.approved_roles=sorted(approved); a.approver=identity.subject
        audit_data={'approval_id':a.id,'role':role,'approved_roles':a.approved_roles,'required_roles':sorted(required),'action_hash':a.action_hash,'plan_version':a.plan_version,'action_type':a.action.get('action_type')}
        if required <= approved:
            a.status='approved'; case.status='approved'; db.add(Task(case_id=case.id,kind='execute',payload={'approval_id':a.id})); emit(db,case.id,'approval_granted','All required roles approved the bound action.',audit_data); audit(db,identity,'approval_granted','approval',a.id,audit_data,case_id=case.id); result='queued'
        else:
            audit_data['remaining_roles']=sorted(required-approved); emit(db,case.id,'approval_partial','One required role approved; action remains blocked.',audit_data); audit(db,identity,'approval_partial','approval',a.id,audit_data,case_id=case.id); result='pending'
        db.commit(); return {'status':result,'approved_roles':a.approved_roles,'required_roles':sorted(required)}

@app.post('/v1/approvals/{approval_id}/revoke')
def revoke_approval(approval_id:str, payload: ApprovalRevokeIn|None=None, x_operator_key:str|None=Header(default=None, alias='X-Operator-Key'), x_operator:str|None=Header(default=None, alias='X-Operator'), x_operator_role:str|None=Header(default=None, alias='X-Operator-Role')):
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        a=db.scalar(select(Approval).where(Approval.id==approval_id).with_for_update())
        if not a: raise HTTPException(404,'approval not found')
        if a.status in {'consumed','expired','revoked'}: raise HTTPException(409,'approval cannot be revoked')
        required=set(a.required_roles or ['warehouse_manager'])
        if identity.role!='ops_admin' and identity.role not in required:
            audit(db,identity,'approval_revoke_rejected','approval',a.id,{'reason':'role_not_allowed','required_roles':sorted(required),'status':a.status},case_id=a.case_id)
            db.commit()
            raise HTTPException(403,'operator role cannot revoke this approval')
        case=db.get(Case,a.case_id)
        a.status='revoked'; a.revoked_at=utc_now(); a.revoked_by=identity.subject; a.revocation_reason=(payload.reason if payload else None)
        if case: case.status='manual_review'
        data={'approval_id':a.id,'revoked_by':a.revoked_by,'revoked_at':a.revoked_at.isoformat(),'reason':a.revocation_reason,'action_hash':a.action_hash,'plan_version':a.plan_version,'previous_approved_roles':a.approved_roles}
        emit(db,a.case_id,'approval_revoked','Approval was revoked; automatic execution is stopped until a new plan is approved.',data)
        audit(db,identity,'approval_revoked','approval',a.id,data,case_id=a.case_id)
        db.commit()
        return {'status':'revoked','approval':approval_out(a)}


@app.post('/v1/approvals/{approval_id}/reject-replan')
def reject_approval_and_replan(approval_id: str, payload: ApprovalRejectIn, x_operator_key: str|None=Header(default=None, alias='X-Operator-Key'), x_operator: str|None=Header(default=None, alias='X-Operator'), x_operator_role: str|None=Header(default=None, alias='X-Operator-Role')):
    """Reject a pending proposal and queue a fresh read-only investigation.

    A rejection never edits or executes the old Action Plan.  It invalidates
    the remaining approvals for that plan version, preserves the operator's
    reason as Case-scoped context, and requires the replacement plan to pass
    grounding, policy and a brand-new approval boundary.
    """
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        approval=db.scalar(select(Approval).where(Approval.id==approval_id).with_for_update())
        if not approval:
            raise HTTPException(404,'approval not found')
        case=db.get(Case,approval.case_id)
        if not case:
            raise HTTPException(404,'case not found')
        if approval.status!='pending' or case.status!='waiting_approval':
            raise HTTPException(409,'only a pending approval on a waiting Case can be rejected for replanning')
        required=set(approval.required_roles or ['warehouse_manager'])
        if identity.role!='ops_admin' and identity.role not in required:
            audit(db,identity,'approval_reject_replan_rejected','approval',approval.id,{'reason':'role_not_allowed','required_roles':sorted(required),'status':approval.status},case_id=approval.case_id)
            db.commit()
            raise HTTPException(403,'operator role cannot reject this approval')
        prior_replans=db.scalar(select(func.count()).select_from(Event).where(Event.case_id==case.id,Event.kind=='replan_requested')) or 0
        if prior_replans >= settings.agent_max_replans:
            case.status='manual_review'
            emit(db,case.id,'manual_review_required','Approval was rejected, but the bounded Replan budget is exhausted. Human review is required.',{'approval_id':approval.id,'reason':payload.reason,'max_replans':settings.agent_max_replans})
            audit(db,identity,'approval_reject_replan_blocked','approval',approval.id,{'reason':payload.reason,'max_replans':settings.agent_max_replans},case_id=case.id)
            db.commit()
            raise HTTPException(409,'replan budget exhausted')

        active_approvals=db.scalars(select(Approval).where(
            Approval.case_id==case.id,
            Approval.plan_version==case.plan_version,
            Approval.status.in_(['pending','approved']),
        ).with_for_update()).all()
        invalidated_ids=[]
        now=utc_now()
        for item in active_approvals:
            if item.id==approval.id:
                item.status='rejected'
                item.rejected_at=now
                item.rejected_by=identity.subject
                item.rejection_reason=payload.reason
            else:
                item.status='invalidated'
                invalidated_ids.append(item.id)
        # A waiting Case has no legitimate write in flight. Mark any stale
        # queued executions terminal rather than letting them race the new
        # investigation; running writes are intentionally rejected above by
        # the waiting-state guard.
        queued_executes=db.scalars(select(Task).where(Task.case_id==case.id,Task.kind=='execute',Task.status=='queued').with_for_update()).all()
        for task in queued_executes:
            task.status='cancelled'
            task.last_error='cancelled because the bound Action Plan was rejected for replanning'

        old_plan=case.plan if isinstance(case.plan,dict) else {}
        case.status='replanning'
        task=Task(case_id=case.id,kind='investigate',payload={
            'reason':f'Approval rejected by {identity.role}: {payload.reason}',
            'previous_plan':old_plan,
            'rejected_approval_id':approval.id,
        })
        db.add(task)
        data={
            'approval_id':approval.id,
            'reason':payload.reason,
            'rejected_by':identity.subject,
            'role':identity.role,
            'plan_version':case.plan_version,
            'invalidated_approval_ids':invalidated_ids,
            'old_plan':old_plan,
        }
        emit(db,case.id,'approval_rejected','Operator rejected the bound Action Plan and requested a fresh Agent investigation.',data)
        emit(db,case.id,'replan_requested','Approval rejection invalidated the old Plan; a fresh read-only Agent investigation was queued.',data | {'source':'approval_rejection','task_kind':'investigate'})
        audit(db,identity,'approval_rejected_replan','approval',approval.id,data,case_id=case.id)
        db.commit()
        return {'status':'replan_queued','approval':approval_out(approval),'case_id':case.id,'task_kind':'investigate'}
