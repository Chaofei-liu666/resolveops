"""Ingress API: authenticated webhooks create durable Cases; no ERP writes here."""
from __future__ import annotations
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib, hmac, json
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .config import settings
from .approval_state import approval_is_expired, utc_now
from .migrations import apply_migrations
from .models import AuditLog, Base, Approval, Case, Event, Invocation, LogisticsLane, Operator, Task
from .runtime_status import build_runtime_status
from .tool_trace import build_tool_trace
from .erpnext import ERPNextAdapter
from .case_ask import CaseQuestionAgent
from .context import CaseContextBuilder, validate_case_context_isolation
from .operator_chat import OperatorChatAgent
from .tools import BusinessReadTools

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

@dataclass(frozen=True)
class OperatorIdentity:
    subject: str
    role: str
    tenant_id: str = 'demo'

engine=create_engine(settings.database_url, pool_pre_ping=True)
STATIC_DIR=Path(__file__).resolve().parent.parent/'static'

def bootstrap_schema():
    with engine.begin() as db:
        db.execute(text("SELECT pg_advisory_lock(hashtext('resolveops_schema_bootstrap'))"))
        try:
            Base.metadata.create_all(db)
            apply_migrations(db)
            seed_default_operator(db)
        finally:
            db.execute(text("SELECT pg_advisory_unlock(hashtext('resolveops_schema_bootstrap'))"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap_schema()
    yield

app=FastAPI(title='ResolveOps', version='1.0.0', lifespan=lifespan)
if STATIC_DIR.exists():
    app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')
def emit(db, case_id, kind, message, data=None): db.add(Event(case_id=case_id,kind=kind,message=message,data=data or {}))
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
def event_out(e: Event): return {'id':e.id,'kind':e.kind,'message':e.message,'data':e.data,'created_at':e.created_at.isoformat() if e.created_at else None}
def approval_out(a: Approval):
    return {'id':a.id,'case_id':a.case_id,'plan_version':a.plan_version,'status':a.status,'action_hash':a.action_hash,'action':a.action,'required_roles':a.required_roles,'approved_roles':a.approved_roles,'approver':a.approver,'expires_at':a.expires_at.isoformat() if a.expires_at else None,'revoked_at':a.revoked_at.isoformat() if a.revoked_at else None,'revoked_by':a.revoked_by,'revocation_reason':a.revocation_reason}
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

def llm_usage_from_telemetry(telemetry: dict[str, Any] | None) -> dict[str, int]:
    if not isinstance(telemetry,dict):
        return {'llm_calls':0,'prompt_tokens':0,'completion_tokens':0,'total_tokens':0}
    usage=telemetry.get('usage') if isinstance(telemetry.get('usage'),dict) else {}
    total=token_value(usage,'total_tokens','total_token_count')
    prompt=token_value(usage,'prompt_tokens','input_tokens','prompt_token_count')
    completion=token_value(usage,'completion_tokens','output_tokens','completion_token_count')
    if not total:
        total=prompt+completion
    return {
        'llm_calls':1 if telemetry.get('status')=='success' or usage else 0,
        'prompt_tokens':prompt,
        'completion_tokens':completion,
        'total_tokens':total,
    }

def merge_llm_usage(*items: dict[str, int]) -> dict[str, int]:
    return {
        'llm_calls':sum(item.get('llm_calls',0) for item in items),
        'prompt_tokens':sum(item.get('prompt_tokens',0) for item in items),
        'completion_tokens':sum(item.get('completion_tokens',0) for item in items),
        'total_tokens':sum(item.get('total_tokens',0) for item in items),
    }

def case_llm_usage(case: Case, events: list[Event]) -> dict[str, int]:
    evidence=case.evidence if isinstance(case.evidence,dict) else {}
    conclusion=evidence.get('conclusion') if isinstance(evidence.get('conclusion'),dict) else {}
    usages=[
        llm_usage_from_telemetry(conclusion.get('llm') if isinstance(conclusion,dict) else None),
        llm_usage_from_telemetry(conclusion.get('llm_repair') if isinstance(conclusion,dict) else None),
    ]
    # Handoff events can preserve a failed or fallback conclusion. Include only
    # token-bearing telemetry if present; do not count the same successful
    # conclusion twice when it is already stored on the Case.
    for event in events:
        data=event.data or {}
        event_conclusion=data.get('conclusion') if isinstance(data.get('conclusion'),dict) else None
        if event_conclusion and event_conclusion is not conclusion:
            telemetry=event_conclusion.get('llm') if isinstance(event_conclusion.get('llm'),dict) else None
            usage=llm_usage_from_telemetry(telemetry)
            if usage.get('total_tokens'):
                usages.append(usage)
    return merge_llm_usage(*usages)

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
    recovery_events=[kind for kind in kinds if kind in {'replan_requested','task_requeued','manual_review_required'}]
    blocked_events=[kind for kind in kinds if kind in {'context_isolation_failed','evidence_grounding_failed','policy_denied','handoff','worker_failure','verification_failed','approval_expired','approval_revoked'}]
    stage_sequence=[
        kind for kind in kinds
        if kind in {
            'case_created','context_built','context_isolation_sanitized','context_isolation_failed',
            'tool_scheduled','tool_observation','evidence_grounding_passed','evidence_grounding_failed',
            'agent_plan_created','approval_requested','approval_partial','approval_granted','approval_expired','approval_revoked',
            'execution_started','replan_requested','verification_passed','verification_failed',
            'lessons_recorded','handoff','manual_review_required','worker_failure',
        }
    ]
    action_evidence=tool_trace.get('action_evidence',{})
    trace_summary=tool_trace.get('summary',{})
    verification_complete=write_count==0 or (verification_passes>=write_count and verification_failures==0)
    has_manual_handoff=any(kind in {'handoff','manual_review_required'} for kind in kinds)
    task_succeeded=case.status=='resolved' or (case.status=='manual_review' and has_manual_handoff)
    tool_selection_accuracy=(len(tool_events)-len(failed_tool_events))/len(tool_events) if tool_events else 1
    if plan_actions:
        grounded_actions=sum(1 for idx, action in enumerate(plan_actions,1) if action_evidence.get(str(idx)) or action_evidence.get(action.get('action_type')))
        evidence_faithfulness=grounded_actions/len(plan_actions)
    else:
        evidence_faithfulness=1 if not any(kind=='evidence_grounding_failed' for kind in kinds) else 0
    if any(kind=='evidence_grounding_failed' for kind in kinds):
        evidence_faithfulness=0
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
    read_tool_budget=max(1, settings.agent_max_read_tool_calls)
    read_tool_budget_used=len(tool_events)/read_tool_budget
    read_tool_budget_exhausted=any(
        'read-tool budget exhausted' in str(item)
        for item in ((case.evidence or {}).get('conclusion') or {}).get('missing_information',[])
    ) if isinstance(case.evidence,dict) else False
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
        'llm_prompt_tokens':llm_usage['prompt_tokens'],
        'llm_completion_tokens':llm_usage['completion_tokens'],
        'llm_total_tokens':llm_usage['total_tokens'],
        'read_tool_budget':read_tool_budget,
        'read_tool_budget_used':read_tool_budget_used,
        'read_tool_budget_exhausted':read_tool_budget_exhausted,
        'stage_sequence':stage_sequence,
        'event_kinds':kinds,
    }
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
    replanned=[row for row in rows if row.get('has_replan')]
    durations=[row.get('duration_seconds') for row in rows if isinstance(row.get('duration_seconds'),(int,float))]
    grounding_applicable=[row for row in rows if row.get('action_count',0)>0 or row.get('has_evidence_grounding_passed') or row.get('has_evidence_grounding_failure')]
    context_failures=sum(1 for row in rows if row.get('has_context_isolation_failure'))
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
        'budget_exhausted_cases':sum(1 for row in rows if row.get('read_tool_budget_exhausted')),
        'avg_read_tool_budget_used':sum(row.get('read_tool_budget_used',0) for row in rows)/total if total else 0,
        'tool_selection_accuracy':sum(row.get('tool_selection_accuracy',1) for row in rows)/total if total else 0,
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
@app.get('/v1/runtime/status')
def runtime_status(x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None)):
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        require_role(identity,'ops_admin','config_admin')
        return build_runtime_status(db)
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
        case=db.get(Case,case_id)
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

@app.post('/v1/chat')
def operator_chat(payload: OperatorChatIn, x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None)):
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        answer=OperatorChatAgent().answer(payload.question, history=payload.history)
        audit(db,identity,'operator_chat_answered','operator_chat',identity.subject,{
            'question':payload.question,
            'history_items':len(payload.history or []),
            'source':answer.get('source'),
            'tools_used':answer.get('tools_used') or [],
            'llm':answer.get('llm') or {},
        })
        db.commit()
        return answer
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
def eval_summary(x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None), limit:int=50):
    limit=max(1,min(limit,200))
    with Session(engine) as db:
        identity=operator_identity_from_db(db,x_operator_key)
        require_role(identity,'ops_admin','config_admin')
        cases=db.scalars(select(Case).order_by(Case.updated_at.desc()).limit(limit)).all()
        rows=[]
        for case in cases:
            events=db.scalars(select(Event).where(Event.case_id==case.id).order_by(Event.created_at)).all()
            approvals=db.scalars(select(Approval).where(Approval.case_id==case.id)).all()
            invocations=db.scalars(select(Invocation).where(Invocation.case_id==case.id)).all()
            tasks=db.scalars(select(Task).where(Task.case_id==case.id)).all()
            rows.append(eval_case_out(case,events,approvals,invocations,tasks))
        return eval_summary_out(rows)

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
