from datetime import datetime
from uuid import uuid4
from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase): pass

class Case(Base):
    __tablename__='cases'
    id: Mapped[str]=mapped_column(String, primary_key=True, default=lambda:str(uuid4()))
    tenant_id: Mapped[str]=mapped_column(String(80), index=True)
    # ERP's immutable event id is the ingress idempotency boundary.  A partial
    # unique index is installed by bootstrap for non-null values.
    source_event_id: Mapped[str|None]=mapped_column(String(160), nullable=True)
    event_type: Mapped[str]=mapped_column(String(80), default='inventory_shortage', index=True)
    order_id: Mapped[str]=mapped_column(String(140), index=True)
    status: Mapped[str]=mapped_column(String(40), default='queued')
    plan_version: Mapped[int]=mapped_column(Integer, default=0)
    evidence: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    plan: Mapped[dict|None]=mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class Event(Base):
    __tablename__='case_events'
    id: Mapped[str]=mapped_column(String, primary_key=True, default=lambda:str(uuid4()))
    case_id: Mapped[str]=mapped_column(String, index=True)
    kind: Mapped[str]=mapped_column(String(60)); message: Mapped[str]=mapped_column(Text)
    data: Mapped[dict]=mapped_column(JSON, default=dict)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now())

class Approval(Base):
    __tablename__='approvals'
    id: Mapped[str]=mapped_column(String, primary_key=True, default=lambda:str(uuid4()))
    case_id: Mapped[str]=mapped_column(String, index=True); plan_version: Mapped[int]=mapped_column(Integer)
    action_hash: Mapped[str]=mapped_column(String(64)); action: Mapped[dict]=mapped_column(JSON)
    status: Mapped[str]=mapped_column(String(20), default='pending'); approver: Mapped[str|None]=mapped_column(String, nullable=True)
    required_roles: Mapped[list]=mapped_column(JSON, default=list)
    approved_roles: Mapped[list]=mapped_column(JSON, default=list)
    expires_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str|None]=mapped_column(String(140), nullable=True)
    revocation_reason: Mapped[str|None]=mapped_column(Text, nullable=True)

class Task(Base):
    __tablename__='tasks'
    id: Mapped[str]=mapped_column(String, primary_key=True, default=lambda:str(uuid4()))
    case_id: Mapped[str]=mapped_column(String, index=True); kind: Mapped[str]=mapped_column(String(40)); payload: Mapped[dict]=mapped_column(JSON, default=dict)
    status: Mapped[str]=mapped_column(String(20), default='queued'); attempts: Mapped[int]=mapped_column(Integer, default=0)
    started_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str|None]=mapped_column(Text, nullable=True)

class Invocation(Base):
    __tablename__='tool_invocations'
    id: Mapped[str]=mapped_column(String, primary_key=True, default=lambda:str(uuid4()))
    idempotency_key: Mapped[str]=mapped_column(String, unique=True); case_id: Mapped[str]=mapped_column(String, index=True)
    tool: Mapped[str]=mapped_column(String); status: Mapped[str]=mapped_column(String); external_id: Mapped[str|None]=mapped_column(String, nullable=True)

class LogisticsLane(Base):
    __tablename__='logistics_lanes'
    id: Mapped[str]=mapped_column(String, primary_key=True, default=lambda:str(uuid4()))
    tenant_id: Mapped[str]=mapped_column(String(80), index=True, default='demo')
    source_warehouse: Mapped[str]=mapped_column(String(140), index=True)
    target_warehouse: Mapped[str]=mapped_column(String(140), index=True)
    transit_days: Mapped[float]=mapped_column(Float)
    cost_per_unit: Mapped[float]=mapped_column(Float)
    currency: Mapped[str]=mapped_column(String(12), default='CNY')
    active: Mapped[bool]=mapped_column(Boolean, default=True)

class AuditLog(Base):
    __tablename__='audit_logs'
    id: Mapped[str]=mapped_column(String, primary_key=True, default=lambda:str(uuid4()))
    actor: Mapped[str]=mapped_column(String(140))
    role: Mapped[str]=mapped_column(String(80))
    action: Mapped[str]=mapped_column(String(80), index=True)
    resource_type: Mapped[str]=mapped_column(String(80), index=True)
    resource_id: Mapped[str]=mapped_column(String(160), index=True)
    case_id: Mapped[str|None]=mapped_column(String, nullable=True, index=True)
    data: Mapped[dict]=mapped_column(JSON, default=dict)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now())

class Operator(Base):
    __tablename__='operators'
    id: Mapped[str]=mapped_column(String, primary_key=True, default=lambda:str(uuid4()))
    tenant_id: Mapped[str]=mapped_column(String(80), index=True, default='demo')
    subject: Mapped[str]=mapped_column(String(140), index=True)
    role: Mapped[str]=mapped_column(String(80), index=True)
    api_key_hash: Mapped[str]=mapped_column(String(64), unique=True, index=True)
    status: Mapped[str]=mapped_column(String(20), default='active', index=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now())

class CaseLesson(Base):
    __tablename__='case_lessons'
    id: Mapped[str]=mapped_column(String, primary_key=True, default=lambda:str(uuid4()))
    tenant_id: Mapped[str]=mapped_column(String(80), index=True)
    lesson_type: Mapped[str]=mapped_column(String(80), index=True)
    subject_type: Mapped[str]=mapped_column(String(80), index=True)
    subject_id: Mapped[str]=mapped_column(String(160), index=True)
    content: Mapped[str]=mapped_column(Text)
    evidence_case_id: Mapped[str]=mapped_column(String, index=True)
    source_action_type: Mapped[str|None]=mapped_column(String(80), nullable=True, index=True)
    confidence: Mapped[float]=mapped_column(Float, default=1.0)
    status: Mapped[str]=mapped_column(String(20), default='active', index=True)
    data: Mapped[dict]=mapped_column(JSON, default=dict)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now())

class PriceReview(Base):
    __tablename__='price_reviews'
    id: Mapped[str]=mapped_column(String, primary_key=True, default=lambda:str(uuid4()))
    tenant_id: Mapped[str]=mapped_column(String(80), index=True)
    case_id: Mapped[str]=mapped_column(String, index=True)
    order_id: Mapped[str]=mapped_column(String(140), index=True)
    sku: Mapped[str]=mapped_column(String(140), index=True)
    order_rate: Mapped[float]=mapped_column(Float)
    reference_rate: Mapped[float]=mapped_column(Float)
    difference: Mapped[float]=mapped_column(Float)
    status: Mapped[str]=mapped_column(String(40), default='draft', index=True)
    idempotency_key: Mapped[str]=mapped_column(String(220), unique=True)
    data: Mapped[dict]=mapped_column(JSON, default=dict)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now())

class SupplierFollowup(Base):
    __tablename__='supplier_followups'
    id: Mapped[str]=mapped_column(String, primary_key=True, default=lambda:str(uuid4()))
    tenant_id: Mapped[str]=mapped_column(String(80), index=True)
    case_id: Mapped[str]=mapped_column(String, index=True)
    order_id: Mapped[str]=mapped_column(String(140), index=True)
    sku: Mapped[str]=mapped_column(String(140), index=True)
    purchase_order: Mapped[str]=mapped_column(String(140), index=True)
    supplier: Mapped[str]=mapped_column(String(180), index=True)
    expected_delivery_date: Mapped[str]=mapped_column(String(40))
    delayed_by_days: Mapped[float]=mapped_column(Float)
    status: Mapped[str]=mapped_column(String(40), default='draft', index=True)
    idempotency_key: Mapped[str]=mapped_column(String(220), unique=True)
    data: Mapped[dict]=mapped_column(JSON, default=dict)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), server_default=func.now())
