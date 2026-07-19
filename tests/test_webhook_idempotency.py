"""Database-level acceptance test for the ingress idempotency invariant."""
import os
import uuid
import pytest
from sqlalchemy import text


@pytest.mark.skipif(not os.getenv('RESOLVEOPS_INTEGRATION_DB'), reason='requires disposable PostgreSQL')
def test_same_tenant_event_can_only_exist_once():
    from production.main import engine
    tenant, event = 'test-tenant', f'evt-{uuid.uuid4()}'
    with engine.begin() as db:
        db.execute(text("INSERT INTO cases(id, tenant_id, source_event_id, order_id, status, plan_version) VALUES (:id,:t,:e,'SO-test','queued',0)"), {'id':str(uuid.uuid4()),'t':tenant,'e':event})
        with pytest.raises(Exception):
            db.execute(text("INSERT INTO cases(id, tenant_id, source_event_id, order_id, status, plan_version) VALUES (:id,:t,:e,'SO-test','queued',0)"), {'id':str(uuid.uuid4()),'t':tenant,'e':event})
