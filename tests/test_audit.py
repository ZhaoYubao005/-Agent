"""Regression checks for durable audit and recovery."""
from fastapi.testclient import TestClient

from ecomops.app import app, connect


def test_reconciliation_is_audited_and_cannot_replay(monkeypatch, tmp_path):
    monkeypatch.setenv('ECOMOPS_AUDIT_DB', str(tmp_path / 'audit.db'))
    monkeypatch.delenv('ECOMOPS_ACCOUNTS', raising=False)
    monkeypatch.setenv('ECOMOPS_API_TOKEN', 'fixture-admin')
    with connect() as db:
        db.execute("INSERT INTO proposals VALUES ('p', 'r', '{}', 'needs_review', NULL)")
    client = TestClient(app, headers={'Authorization': 'Bearer fixture-admin'})
    path = '/recommendations/p'
    response = client.post(path + '/reconcile', json={'note': 'Compared physical count and Medusa history'})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'reconciled'
    assert client.post(path + '/receive').status_code == 409
    events = client.get('/operations/audit').json()
    assert [(event['actor'], event['action'], event['subject']) for event in events] == [
        ('local-service', 'reconciled', 'p'),
    ]


def test_schedule_uses_local_completed_day():
    from datetime import date, datetime, timezone
    from ecomops.scheduler import scheduled_day

    midnight = datetime(2026, 11, 5, 16, tzinfo=timezone.utc)
    assert scheduled_day(midnight, 'Asia/Shanghai', 0) == date(2026, 11, 5)
    assert scheduled_day(midnight, 'Asia/Shanghai', 1) is None
