"""Ingress API: authenticated webhooks create durable Cases; no ERP writes here."""
from __future__ import annotations
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib, hmac, json
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .config import settings
from .models import AuditLog, Base, Approval, Case, Event, Invocation, LogisticsLane, Task

SUPPORTED_EVENTS={'inventory_shortage','price_mismatch','delivery_delay','supplier_delay'}

class LogisticsLaneIn(BaseModel):
    tenant_id: str = Field(default='demo', min_length=1, max_length=80)
    source_warehouse: str = Field(min_length=1, max_length=140)
    target_warehouse: str = Field(min_length=1, max_length=140)
    transit_days: float = Field(gt=0)
    cost_per_unit: float = Field(ge=0)
    currency: str = Field(default='CNY', min_length=1, max_length=12)
    active: bool = True

@dataclass(frozen=True)
class OperatorIdentity:
    subject: str
    role: str

engine=create_engine(settings.database_url, pool_pre_ping=True)
STATIC_DIR=Path(__file__).resolve().parent.parent/'static'

def bootstrap_schema():
    with engine.begin() as db:
        db.execute(text("SELECT pg_advisory_lock(hashtext('resolveops_schema_bootstrap'))"))
        try:
            Base.metadata.create_all(db)
            # Initial online migration. Kept idempotent so an existing deployment
            # can upgrade safely; production CI should run this as a versioned
            # migration. The advisory lock prevents API/Worker startup races.
            db.execute(text('ALTER TABLE cases ADD COLUMN IF NOT EXISTS source_event_id VARCHAR(160)'))
            db.execute(text("ALTER TABLE cases ADD COLUMN IF NOT EXISTS event_type VARCHAR(80) NOT NULL DEFAULT 'inventory_shortage'"))
            db.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_cases_tenant_source_event ON cases (tenant_id, source_event_id) WHERE source_event_id IS NOT NULL'))
            db.execute(text('ALTER TABLE tasks ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ'))
            db.execute(text('ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_error TEXT'))
            db.execute(text("ALTER TABLE approvals ADD COLUMN IF NOT EXISTS required_roles JSON NOT NULL DEFAULT '[\"warehouse_manager\"]'::json"))
            db.execute(text("ALTER TABLE approvals ADD COLUMN IF NOT EXISTS approved_roles JSON NOT NULL DEFAULT '[]'::json"))
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
    db.add(AuditLog(actor=identity.subject,role=identity.role,action=action,resource_type=resource_type,resource_id=resource_id,case_id=case_id,data=data or {}))
def operator_identity(key: str|None, subject: str|None=None, role: str|None=None) -> OperatorIdentity:
    """Temporary API-key auth boundary; production should receive identity from SSO/API Gateway."""
    if not key or not hmac.compare_digest(key, settings.operator_api_key): raise HTTPException(401, 'operator authentication failed')
    return OperatorIdentity(subject=subject or 'authenticated-operator', role=role or 'operator')
def require_role(identity: OperatorIdentity, *roles: str) -> None:
    if identity.role not in roles: raise HTTPException(403, f'operator role must be one of: {", ".join(roles)}')
def event_out(e: Event): return {'id':e.id,'kind':e.kind,'message':e.message,'data':e.data,'created_at':e.created_at.isoformat() if e.created_at else None}
def approval_out(a: Approval):
    return {'id':a.id,'case_id':a.case_id,'plan_version':a.plan_version,'status':a.status,'action_hash':a.action_hash,'action':a.action,'required_roles':a.required_roles,'approved_roles':a.approved_roles,'approver':a.approver}
def invocation_out(i: Invocation):
    return {'id':i.id,'case_id':i.case_id,'tool':i.tool,'status':i.status,'external_id':i.external_id,'idempotency_key':i.idempotency_key}
def task_out(t: Task):
    return {'id':t.id,'case_id':t.case_id,'kind':t.kind,'status':t.status,'attempts':t.attempts,'payload':t.payload,'started_at':t.started_at.isoformat() if t.started_at else None,'last_error':t.last_error}
def lane_out(lane: LogisticsLane):
    return {'id':lane.id,'tenant_id':lane.tenant_id,'source_warehouse':lane.source_warehouse,'target_warehouse':lane.target_warehouse,'transit_days':lane.transit_days,'cost_per_unit':lane.cost_per_unit,'currency':lane.currency,'active':lane.active}
def audit_out(log: AuditLog):
    return {'id':log.id,'actor':log.actor,'role':log.role,'action':log.action,'resource_type':log.resource_type,'resource_id':log.resource_id,'case_id':log.case_id,'data':log.data,'created_at':log.created_at.isoformat() if log.created_at else None}
def eval_case_out(case: Case, events: list[Event], approvals: list[Approval], invocations: list[Invocation], tasks: list[Task]):
    kinds=[event.kind for event in events]
    plan_actions=(case.plan or {}).get('actions',[]) if isinstance(case.plan,dict) else []
    write_count=len(invocations)
    verification_passes=sum(1 for kind in kinds if kind=='verification_passed')
    verification_failures=sum(1 for kind in kinds if kind=='verification_failed')
    recovery_events=[kind for kind in kinds if kind in {'replan_requested','task_requeued','manual_review_required'}]
    blocked_events=[kind for kind in kinds if kind in {'evidence_grounding_failed','policy_denied','handoff','worker_failure','verification_failed'}]
    return {
        'case_id':case.id,
        'event_type':case.event_type,
        'order_id':case.order_id,
        'status':case.status,
        'resolved':case.status=='resolved',
        'manual_review':case.status=='manual_review',
        'plan_version':case.plan_version,
        'action_count':len(plan_actions),
        'tool_call_count':sum(1 for kind in kinds if kind=='tool_observation'),
        'approval_count':len(approvals),
        'pending_approval_count':sum(1 for approval in approvals if approval.status=='pending'),
        'write_invocation_count':write_count,
        'verification_pass_count':verification_passes,
        'verification_failed_count':verification_failures,
        'verification_complete':write_count==0 or (verification_passes>=write_count and verification_failures==0),
        'recovery_event_count':len(recovery_events),
        'blocked_event_count':len(blocked_events),
        'task_failure_count':sum(1 for task in tasks if task.status=='failed'),
        'has_policy_denial':'policy_denied' in kinds,
        'has_evidence_grounding_failure':'evidence_grounding_failed' in kinds,
        'has_replan':'replan_requested' in kinds,
        'has_manual_handoff':any(kind in {'handoff','manual_review_required'} for kind in kinds),
        'event_kinds':kinds,
    }
def eval_summary_out(rows):
    total=len(rows)
    resolved=sum(1 for row in rows if row['resolved'])
    manual=sum(1 for row in rows if row['manual_review'])
    writes=sum(row['write_invocation_count'] for row in rows)
    verified=sum(1 for row in rows if row['write_invocation_count']>0 and row['verification_complete'])
    return {
        'total_cases':total,
        'resolved_cases':resolved,
        'manual_review_cases':manual,
        'case_resolution_rate':resolved/total if total else 0,
        'cases_with_writes':sum(1 for row in rows if row['write_invocation_count']>0),
        'verified_write_cases':verified,
        'verification_pass_rate':verified/sum(1 for row in rows if row['write_invocation_count']>0) if any(row['write_invocation_count']>0 for row in rows) else 1,
        'write_invocations':writes,
        'verification_failures':sum(row['verification_failed_count'] for row in rows),
        'policy_denials':sum(1 for row in rows if row['has_policy_denial']),
        'evidence_grounding_failures':sum(1 for row in rows if row.get('has_evidence_grounding_failure')),
        'replanned_cases':sum(1 for row in rows if row['has_replan']),
        'manual_handoff_cases':sum(1 for row in rows if row['has_manual_handoff']),
        'task_failures':sum(row['task_failure_count'] for row in rows),
        'cases':rows,
    }
@app.get('/healthz')
def health(): return {'status':'ok'}
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
    operator_identity(x_operator_key,x_operator,x_operator_role)
    limit=max(1,min(limit,100))
    with Session(engine) as db:
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
@app.get('/v1/config/logistics-lanes')
def logistics_lanes(x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None), tenant_id:str='demo', active:bool|None=None):
    operator_identity(x_operator_key,x_operator,x_operator_role)
    with Session(engine) as db:
        query=select(LogisticsLane).where(LogisticsLane.tenant_id==tenant_id).order_by(LogisticsLane.source_warehouse,LogisticsLane.target_warehouse)
        if active is not None: query=query.where(LogisticsLane.active.is_(active))
        return [lane_out(lane) for lane in db.scalars(query).all()]
@app.post('/v1/config/logistics-lanes')
def upsert_logistics_lane(payload: LogisticsLaneIn, x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None)):
    identity=operator_identity(x_operator_key,x_operator,x_operator_role)
    require_role(identity,'config_admin','ops_admin')
    with Session(engine) as db:
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
    identity=operator_identity(x_operator_key,x_operator,x_operator_role)
    require_role(identity,'ops_admin','config_admin')
    limit=max(1,min(limit,200))
    with Session(engine) as db:
        query=select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
        if case_id: query=select(AuditLog).where(AuditLog.case_id==case_id).order_by(AuditLog.created_at.desc()).limit(limit)
        return [audit_out(log) for log in db.scalars(query).all()]
@app.get('/v1/evals/summary')
def eval_summary(x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None), limit:int=50):
    identity=operator_identity(x_operator_key,x_operator,x_operator_role)
    require_role(identity,'ops_admin','config_admin')
    limit=max(1,min(limit,200))
    with Session(engine) as db:
        cases=db.scalars(select(Case).order_by(Case.updated_at.desc()).limit(limit)).all()
        rows=[]
        for case in cases:
            events=db.scalars(select(Event).where(Event.case_id==case.id).order_by(Event.created_at)).all()
            approvals=db.scalars(select(Approval).where(Approval.case_id==case.id)).all()
            invocations=db.scalars(select(Invocation).where(Invocation.case_id==case.id)).all()
            tasks=db.scalars(select(Task).where(Task.case_id==case.id)).all()
            rows.append(eval_case_out(case,events,approvals,invocations,tasks))
        return eval_summary_out(rows)
@app.get('/v1/cases/{case_id}')
def case_detail(case_id:str, x_operator_key:str|None=Header(default=None), x_operator:str|None=Header(default=None), x_operator_role:str|None=Header(default=None)):
    operator_identity(x_operator_key,x_operator,x_operator_role)
    with Session(engine) as db:
        case=db.get(Case,case_id)
        if not case: raise HTTPException(404,'case not found')
        events=db.scalars(select(Event).where(Event.case_id==case_id).order_by(Event.created_at)).all()
        approvals=db.scalars(select(Approval).where(Approval.case_id==case_id).order_by(Approval.plan_version,Approval.id)).all()
        invocations=db.scalars(select(Invocation).where(Invocation.case_id==case_id)).all()
        tasks=db.scalars(select(Task).where(Task.case_id==case_id)).all()
        return {
            'id':case.id,'tenant_id':case.tenant_id,'source_event_id':case.source_event_id,'event_type':case.event_type,'order_id':case.order_id,
            'status':case.status,'plan_version':case.plan_version,'plan':case.plan,'evidence':case.evidence,
            'created_at':case.created_at.isoformat() if case.created_at else None,'updated_at':case.updated_at.isoformat() if case.updated_at else None,
            'approvals':[approval_out(a) for a in approvals],
            'invocations':[invocation_out(i) for i in invocations],
            'tasks':[task_out(t) for t in tasks],
            'events':[event_out(e) for e in events],
        }
@app.post('/v1/approvals/{approval_id}/approve')
def approve(approval_id:str, x_operator_key:str|None=Header(default=None, alias='X-Operator-Key'), x_operator:str|None=Header(default=None, alias='X-Operator'), x_operator_role:str|None=Header(default=None, alias='X-Operator-Role')):
    identity=operator_identity(x_operator_key,x_operator,x_operator_role)
    with Session(engine) as db:
        a=db.get(Approval,approval_id)
        if not a or a.status!='pending': raise HTTPException(409,'approval unavailable')
        role=identity.role; required=set(a.required_roles or ['warehouse_manager'])
        if role not in required:
            audit(db,identity,'approval_rejected','approval',a.id,{'reason':'role_not_required','required_roles':sorted(required),'action_hash':a.action_hash,'plan_version':a.plan_version,'action_type':a.action.get('action_type')},case_id=a.case_id)
            db.commit()
            raise HTTPException(403,'operator role is not required for this approval')
        approved=set(a.approved_roles or []); approved.add(role); a.approved_roles=sorted(approved); a.approver=identity.subject; case=db.get(Case,a.case_id)
        audit_data={'approval_id':a.id,'role':role,'approved_roles':a.approved_roles,'required_roles':sorted(required),'action_hash':a.action_hash,'plan_version':a.plan_version,'action_type':a.action.get('action_type')}
        if required <= approved:
            a.status='approved'; case.status='approved'; db.add(Task(case_id=case.id,kind='execute',payload={'approval_id':a.id})); emit(db,case.id,'approval_granted','All required roles approved the bound action.',audit_data); audit(db,identity,'approval_granted','approval',a.id,audit_data,case_id=case.id); result='queued'
        else:
            audit_data['remaining_roles']=sorted(required-approved); emit(db,case.id,'approval_partial','One required role approved; action remains blocked.',audit_data); audit(db,identity,'approval_partial','approval',a.id,audit_data,case_id=case.id); result='pending'
        db.commit(); return {'status':result,'approved_roles':a.approved_roles,'required_roles':sorted(required)}
