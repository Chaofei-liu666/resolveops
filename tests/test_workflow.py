from fastapi.testclient import TestClient

from app import app


def test_inventory_case_closes_after_bound_approval():
    with TestClient(app) as client:
        # CASE-1042 is seeded once; a fresh database is not required for this smoke test.
        case = client.get('/api/cases/CASE-1042').json()
        if case['status'] == 'new':
            plan = client.post('/api/cases/CASE-1042/investigate').json()
            client.post(f"/api/approvals/{plan['approval_id']}/approve", json={'approver': 'test'})
        client.post('/api/cases/CASE-1042/execute')
        assert client.get('/api/cases/CASE-1042').json()['status'] == 'resolved'
