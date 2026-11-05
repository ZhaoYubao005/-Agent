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


def test_scheduled_report_is_claimed_and_saved_once(monkeypatch, tmp_path):
    import asyncio
    from datetime import datetime, timezone
    from ecomops.database import transaction
    from ecomops.operations_store import save_report
    from ecomops.scheduler import run_tick

    monkeypatch.setenv('ECOMOPS_AUDIT_DB', str(tmp_path / 'scheduler.db'))
    now = datetime(2026, 11, 6, 8, tzinfo=timezone.utc)
    runs = []

    async def generate(day, zone):
        runs.append(day)
        await asyncio.sleep(0)
        with transaction() as db:
            return save_report(db, day.isoformat(), zone, {'current': {'orders': 1}})

    async def run():
        await asyncio.gather(
            run_tick(connect, generate, 'UTC', 7, now),
            run_tick(connect, generate, 'UTC', 7, now),
        )

    asyncio.run(run())
    assert len(runs) == 1
    with transaction() as db:
        row = db.execute('SELECT day, zone, state FROM report_jobs').fetchone()
        assert dict(row) == {'day': '2026-11-05', 'zone': 'UTC', 'state': 'completed'}


def test_receipt_intent_survives_uncertain_medusa_response(monkeypatch, tmp_path):
    from uuid import uuid4
    import json
    from ecomops import app as api
    from ecomops.database import transaction

    monkeypatch.setenv('ECOMOPS_AUDIT_DB', str(tmp_path / 'receipt.db'))
    monkeypatch.setenv('MEDUSA_URL', 'http://127.0.0.1:9000')
    monkeypatch.setenv('ECOMOPS_ALLOW_REPLENISHMENT', '1')
    monkeypatch.setenv('ECOMOPS_ISOLATED_FIXTURES', '1')
    monkeypatch.delenv('ECOMOPS_ACCOUNTS', raising=False)
    monkeypatch.setenv('ECOMOPS_API_TOKEN', 'fixture-admin')
    proposal = {'inventory_item_id': 'i', 'location_id': 'l', 'quantity': 12,
                'expected_stocked': 3, 'reserved_quantity': 2}
    with transaction() as db:
        db.execute('INSERT INTO proposals VALUES (?, ?, ?, ?, ?)',
                   ('p', 'r', json.dumps(proposal), 'approved', None))
    request_id = str(uuid4())
    calls = []

    async def uncertain(**kwargs):
        calls.append(kwargs['quantity'])
        with transaction() as db:
            row = db.execute('SELECT request_id, quantity, status FROM receipt_batches').fetchone()
            assert row is not None
            assert dict(row) == {'request_id': request_id, 'quantity': 4, 'status': 'applying'}
        raise TimeoutError

    monkeypatch.setattr(api, 'replenish_inventory', uncertain)
    client = TestClient(app, headers={'Authorization': 'Bearer fixture-admin'})
    path = '/recommendations/p'
    request = {'request_id': request_id, 'quantity': 4}
    assert client.post(path + '/receive', json=request).status_code == 409
    batches = client.get(path + '/batches').json()
    assert batches[0]['status'] == 'needs_review'
    replay = client.post(path + '/receive', json=request)
    assert replay.status_code == 409
    assert replay.json()['detail']['request_id'] == request_id
    assert client.post(path + '/reconcile', json={'note': 'Checked physical count and stock',
                                                 'outcome': 'not_received'}).status_code == 200
    reconciled = client.get(path + '/batches').json()[0]
    assert reconciled['receipt'] == {'outcome': 'not_received'}
    assert reconciled['status'] == 'reconciled'
    assert client.post(path + '/receive', json=request).status_code == 409
    assert calls == [4]
