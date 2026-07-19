"""Durable worker. The rule planner is deliberately narrow: shortage only."""
from __future__ import annotations
import hashlib, json, time
from datetime import UTC, datetime, timedelta
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from .config import settings
from .context import CaseContextBuilder
from .erpnext import ERPNextAdapter
from .main import emit
from .models import Approval, Base, Case, Invocation, Task
from .tools import BusinessReadTools
from .agent import InvestigationAgent
from .actions import definition_for, normalize_plan, normalize_proposal
from .evidence import validate_plan_grounding
from .executors import executor_for
from .memory import record_verified_lessons
from .policy import action_policy
engine=create_engine(settings.database_url,pool_pre_ping=True); erp=ERPNextAdapter(settings.erpnext_base_url,settings.erpnext_api_key,settings.erpnext_api_secret)
def ensure_schema():
    """Worker may start before the API container; migrations must not rely on order."""
    with engine.begin() as db:
        db.execute(text("SELECT pg_advisory_lock(hashtext('resolveops_schema_bootstrap'))"))
        try:
            Base.metadata.create_all(db)
            db.execute(text('ALTER TABLE cases ADD COLUMN IF NOT EXISTS source_event_id VARCHAR(160)'))
            db.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_cases_tenant_source_event ON cases (tenant_id, source_event_id) WHERE source_event_id IS NOT NULL'))
            db.execute(text('ALTER TABLE tasks ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ'))
            db.execute(text('ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_error TEXT'))
            db.execute(text("ALTER TABLE approvals ADD COLUMN IF NOT EXISTS required_roles JSON NOT NULL DEFAULT '[\"warehouse_manager\"]'::json"))
            db.execute(text("ALTER TABLE approvals ADD COLUMN IF NOT EXISTS approved_roles JSON NOT NULL DEFAULT '[]'::json"))
        finally:
            db.execute(text("SELECT pg_advisory_unlock(hashtext('resolveops_schema_bootstrap'))"))
def digest(action, version): return hashlib.sha256(json.dumps({'action':action,'version':version},sort_keys=True).encode()).hexdigest()
def claim(db):
    return db.scalars(select(Task).where(Task.status=='queued').with_for_update(skip_locked=True).limit(1)).first()
def replan_count(db, case_id):
    return db.scalar(select(func.count()).select_from(Task).where(Task.case_id==case_id,Task.kind=='investigate',Task.payload['reason'].as_string().is_not(None))) or 0

def recover_expired_leases(db):
    """Recover safely after a Worker dies during a task.

    Investigation is read-only, so it may be requeued. Execution can have
    reached ERPNext just before the process died; it is never blindly retried.
    """
    cutoff=datetime.now(UTC)-timedelta(seconds=settings.task_lease_seconds)
    stale=db.scalars(select(Task).where(Task.status=='running', Task.started_at < cutoff).with_for_update(skip_locked=True)).all()
    for task in stale:
        case=db.get(Case,task.case_id)
        if task.kind=='investigate':
            task.status='queued'; task.started_at=None; task.last_error='worker lease expired; safe read-only task requeued'
            emit(db,case.id,'task_requeued','Investigation Worker stopped; read-only task was safely requeued.',{'task_id':task.id})
        else:
            task.status='failed'; task.last_error='worker lease expired during possible ERP write'
            case.status='manual_review'
            emit(db,case.id,'manual_review_required','Worker stopped during a possible ERP write. No automatic retry is allowed; verify ERPNext by idempotency key first.',{'task_id':task.id})
    if stale: db.commit()
def investigate(db, c, task_context=None):
    if settings.llm_base_url and settings.llm_api_key and settings.llm_model:
        return investigate_with_agent(db, c, task_context or {})
    so=erp.sales_order(c.order_id); item=so['items'][0]; target=item.get('warehouse'); required=float(item['qty']); local=erp.stock(item['item_code'],target)
    # ERPNext's reserved quantity is unavailable to a new transfer. A shortage
    # is therefore based on usable stock, never by adding reservations back.
    local_available=max(0, float(local['actual_qty'])-float(local.get('reserved_qty',0)))
    shortage=max(0, required-local_available); choices=[]
    for wh in settings.alternative_warehouses.split(','):
        stock=erp.stock(item['item_code'],wh.strip()); available=float(stock['actual_qty'])-float(stock.get('reserved_qty',0))
        if available>=shortage: choices.append((wh.strip(),available))
    c.evidence={'order':c.order_id,'sku':item['item_code'],'required':required,'local_available':local_available,'shortage':shortage,'alternatives':choices}
    if not choices: c.status='manual_review'; emit(db,c.id,'handoff','No safe transfer candidate; human review required.'); return
    c.plan_version+=1; source=choices[0][0]
    action=normalize_proposal({'action_type':'transfer_stock','arguments':{'source':source,'target':target,'sku':item['item_code'],'quantity':shortage},'risk':'medium'},'Deterministic pilot fallback selected the first safe alternative warehouse.')
    c.plan=action; c.status='waiting_approval'
    a=Approval(case_id=c.id,plan_version=c.plan_version,action=action,action_hash=digest(action,c.plan_version),required_roles=['warehouse_manager']); db.add(a); db.flush()
    emit(db,c.id,'approval_requested','Plan-bound approval created.',{'approval_id':a.id,'action_hash':a.action_hash})

def investigate_with_agent(db, c, task_context):
    observations=[]
    tool_surface=BusinessReadTools(erp)
    case_context=CaseContextBuilder(db).build(c.id, task_context or {})
    def observe(name, args, result, tool_result=None):
        metadata=tool_surface.metadata(name)
        observation={'tool':name,'arguments':args,'result':result,'metadata':metadata}
        if tool_result is not None:
            observation['tool_result']=tool_result
        observations.append(observation)
        emit(db,c.id,'tool_observation',f'Agent called read tool: {name}.',{'arguments':args,'result':result,'tool_result':tool_result,'metadata':metadata})
    conclusion=InvestigationAgent(tool_surface).run(c.order_id, observe, case_context)
    previous_evidence=c.evidence
    c.evidence={'case_context':case_context,'observations':observations,'conclusion':conclusion,'replanning_context':task_context or None,'previous_evidence':previous_evidence if task_context else None}; proposals=conclusion.get('recommended_actions',[])
    if conclusion.get('status')!='ready' or not proposals:
        c.status='manual_review'; emit(db,c.id,'handoff','Agent ended investigation without a safe executable proposal.',{'conclusion':conclusion}); return
    try: plan=normalize_plan(proposals,conclusion.get('rationale',''),[f'E-{index+1:03d}' for index in range(len(observations))])
    except (ValueError, TypeError) as exc:
        c.status='manual_review'; emit(db,c.id,'handoff','Agent proposal failed Action Plan validation.',{'error':str(exc)}); return
    grounding=validate_plan_grounding(plan,observations)
    if not grounding['allowed']:
        c.plan=plan; c.status='manual_review'; emit(db,c.id,'evidence_grounding_failed','Agent plan is not sufficiently supported by read-tool evidence.',grounding); return
    plan['evidence_grounding']=grounding
    for action in plan['actions']:
        decision=action_policy(action,{'observations':observations}); action['policy']=decision
        if not decision['allowed']:
            c.plan=plan; c.status='manual_review'; emit(db,c.id,'policy_denied','Policy Engine denied an action in the recommended plan.',{'action_id':action['action_id'],**decision}); return
    c.plan_version+=1; c.plan=plan; c.status='waiting_approval'; approvals=[]
    for action in plan['actions']:
        a=Approval(case_id=c.id,plan_version=c.plan_version,action=action,action_hash=digest(action,c.plan_version),required_roles=action['policy']['required_roles']); db.add(a); db.flush(); approvals.append({'approval_id':a.id,'action_id':action['action_id'],'action_type':action['action_type']})
    emit(db,c.id,'agent_plan_created','Agent produced a multi-action plan from observed tool evidence; policy created bound approvals.',{'approvals':approvals,'alternatives':conclusion.get('alternatives',[])})
def execute(db,c,approval_id):
    a=db.get(Approval,approval_id)
    plan=c.plan or {}
    actions=plan.get('actions') if isinstance(plan,dict) else None
    actions=actions if isinstance(actions,list) else [plan]
    action=next((item for item in actions if item.get('action_id')==a.action.get('action_id')),None) if a else None
    if not a or not action or a.status!='approved' or a.action_hash!=digest(action,c.plan_version): c.status='manual_review'; emit(db,c.id,'handoff','Approval no longer matches its plan action.'); return
    definition=definition_for(action.get('action_type'))
    if not definition or not definition.executable:
        c.status='manual_review'; emit(db,c.id,'handoff','No approved executor exists for this Action Plan type.',{'action_type':action.get('action_type')}); return
    executor=executor_for(definition.action_type)
    if not executor:
        c.status='manual_review'; emit(db,c.id,'handoff','Executor registry has no implementation for this Action Plan type.',{'action_type':definition.action_type}); return
    action_input=action.get('input',action.get('arguments')); key=f'{c.id}:{action["action_id"]}:v{c.plan_version}'; inv=db.scalar(select(Invocation).where(Invocation.idempotency_key==key))
    if inv and inv.status=='succeeded': name=inv.external_id
    elif inv: c.status='manual_review'; emit(db,c.id,'handoff','Previous write has unknown outcome; do not retry blindly.'); return
    else:
        preflight=executor.preflight(db,erp,action_input)
        if not preflight.get('ok'):
            if preflight.get('reason')=='resource_busy':
                raise RuntimeError('resource busy')
            a.status='invalidated'; c.status='replanning'
            reason=preflight.get('message') or preflight.get('reason') or 'preflight failed'
            if replan_count(db,c.id) >= settings.agent_max_replans:
                c.status='manual_review'
                emit(db,c.id,'manual_review_required','Automatic replan budget exhausted; human review required before another plan is created.',{'max_replans':settings.agent_max_replans,'reason':reason})
                return
            db.add(Task(case_id=c.id,kind='investigate',payload={'reason':reason,'previous_plan':plan}))
            emit(db,c.id,'replan_requested','Write preflight failed; old approval invalidated and Agent replanning queued.',{'preflight':preflight,'old_plan':plan})
            return
        execution_context=executor.context(erp,c.order_id,action_input)
        inv=Invocation(idempotency_key=key,case_id=c.id,tool=executor.invocation_tool,status='pending'); db.add(inv); db.flush()
        name=executor.write(erp,action_input,execution_context.get('company'),key)
        inv.status='succeeded'; inv.external_id=name; a.status='consumed'
    verification=executor.verify(erp,name,action_input)
    verified=verification['verified']; event_data=verification['event_data']
    if verified:
        pending=db.scalars(select(Approval).where(Approval.case_id==c.id,Approval.plan_version==c.plan_version,Approval.status!='consumed')).all()
        c.status='resolved' if not pending else 'waiting_approval'
        emit(db,c.id,'verification_passed','ERPNext read-after-write verification passed.',{'action_id':action['action_id'],**event_data,'case_complete':not pending})
        if c.status=='resolved':
            lessons=record_verified_lessons(db,c,action,{'verified':True,'event_data':event_data})
            if lessons:
                emit(db,c.id,'lessons_recorded','Verified Case Lessons were recorded as planning hints for future Cases.',{'lesson_ids':[lesson.id for lesson in lessons]})
    else: c.status='manual_review'; emit(db,c.id,'verification_failed','Write result cannot be verified; automation stopped.',event_data)
def once():
    with Session(engine) as db:
        recover_expired_leases(db)
        task=claim(db)
        if not task:return False
        task.status='running'; task.attempts+=1; task.started_at=datetime.now(UTC); task.last_error=None; db.commit()
        try:
            c=db.get(Case,task.case_id); investigate(db,c,task.payload) if task.kind=='investigate' else execute(db,c,task.payload['approval_id']); task.status='done'; db.commit()
        except Exception as e: task.status='failed'; task.last_error=type(e).__name__; c=db.get(Case,task.case_id); c.status='manual_review'; emit(db,c.id,'worker_failure','Execution stopped for human review.',{'error':type(e).__name__}); db.commit()
    return True
if __name__=='__main__':
    ensure_schema()
    while True:
        if not once(): time.sleep(1)
