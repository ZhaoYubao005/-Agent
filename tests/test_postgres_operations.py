"""Real PostgreSQL persistence checks using a disposable database."""
import json
import os
import runpy
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ecomops import database
from ecomops.audit import record
from ecomops.inventory_history import save_levels
from ecomops.operations_store import change_alert, get_report, refresh_alerts, save_report


@pytest.fixture
def postgres_database(monkeypatch):
    url = os.environ.get('ECOMOPS_TEST_POSTGRES_URL')
    if url is None:
        pytest.skip('Set ECOMOPS_TEST_POSTGRES_URL to a test server with a CREATEDB role')
    if not url.strip():
        pytest.fail('ECOMOPS_TEST_POSTGRES_URL is empty')
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    name = f'ecomops_test_{uuid4().hex}'
    identifier = sql.Identifier(name)
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as control:
        control.execute(sql.SQL('CREATE DATABASE {}').format(identifier))
        try:
            monkeypatch.setenv('ECOMOPS_DATABASE_URL', make_conninfo(url, dbname=name))
            yield
        finally:
            control.execute(sql.SQL('DROP DATABASE {}').format(identifier))


def test_postgres_saves_operations_across_connections(postgres_database):
    actor = SimpleNamespace(name='fixture-operator', role='operator')
    alert = {'id': 'fulfillment_sla:test-order', 'age_hours': 24}
    level = {'location_id': 'location', 'stocked_quantity': 5,
             'reserved_quantity': 2, 'available_quantity': 3}
    with database.transaction() as db:
        record(db, actor, 'test', 'fixture')
        save_levels(db, [{'id': 'item', 'sku': 'TEST', 'location_levels': [level]}])
        refresh_alerts(db, [alert])
        assert change_alert(db, alert['id'], 'acknowledged')
        original = save_report(db, '2026-11-05', 'UTC', {'current': {'orders': 2}})

    with database.transaction() as db:
        assert save_report(db, '2026-11-05', 'UTC', {'current': {'orders': 5}}) == original
        assert refresh_alerts(db, [alert])[0]['status'] == 'acknowledged'

    with database.connect_read_only() as db:
        assert db.execute('SHOW transaction_read_only').fetchone() == {'transaction_read_only': 'on'}
        assert db.execute('SELECT current_schema() AS name').fetchone() == {'name': 'ecomops_agent'}
        assert db.execute('SELECT actor, action, subject FROM audit_events').fetchone() == {
            'actor': actor.name, 'action': 'test', 'subject': 'fixture',
        }
        assert db.execute('SELECT stocked, reserved, available FROM inventory_observations').fetchone() == {
            'stocked': '5', 'reserved': '2', 'available': '3',
        }
        assert db.execute('SELECT alert_id, status FROM order_alert_events').fetchone() == {
            'alert_id': alert['id'], 'status': 'acknowledged',
        }
        assert get_report(db, '2026-11-05', 'UTC') == original


def test_postgres_receipt_claim_serializes_separate_connections(postgres_database):
    item, location = 'item', 'location'
    payload = json.dumps({'inventory_item_id': item, 'location_id': location})
    with database.transaction() as db:
        db.executemany('INSERT INTO proposals VALUES (?, ?, ?, ?, ?)', [
            (proposal, 'run', payload, 'approved', None) for proposal in ('first', 'second')
        ])

    contender_pid = Queue()

    def contend():
        db = database.open_db()
        try:
            with db:
                contender_pid.put(db.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'])
                return database.claim_receipt(db, 'second', item, location)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=1) as workers:
        with database.transaction() as db:
            assert database.claim_receipt(db, 'first', item, location) == 1
            pending = workers.submit(contend)
            pid = contender_pid.get(timeout=10)
            deadline = time.monotonic() + 10
            while True:
                waiting = db.execute('SELECT 1 FROM pg_locks WHERE pid=? AND NOT granted', (pid,)).fetchone()
                if waiting or pending.done() or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            assert waiting, 'the second connection must wait for the uncommitted receipt claim'
        assert pending.result(timeout=10) == 0

    with database.connect_read_only() as db:
        assert db.execute('SELECT id, status FROM proposals ORDER BY id').fetchall() == [
            {'id': 'first', 'status': 'applying'}, {'id': 'second', 'status': 'approved'},
        ]


def test_sqlite_migration_preserves_rows_and_postgres_identities(postgres_database, tmp_path):
    actor = SimpleNamespace(name='fixture-operator', role='operator')
    source_path = tmp_path / 'legacy.sqlite3'
    source = sqlite3.connect(source_path)
    try:
        database.create_schema(source)
        with source:
            source.execute("INSERT INTO proposals VALUES ('old', 'run', '{}', 'applied', '{}')")
            source.execute("INSERT INTO audit_events VALUES (50, 'old', 'admin', 'applied', 'old', NULL, 'time')")
            source.execute("INSERT INTO order_alerts VALUES ('legacy-alert', '{}', 'open')")
            source.execute("INSERT INTO order_alert_events VALUES (80, 'legacy-alert', 'open', 'time')")
    finally:
        source.close()

    script = Path(__file__).resolve().parents[1] / 'scripts' / 'migrate-operations.py'
    migrate = runpy.run_path(str(script))['migrate']
    original = source_path.read_bytes()
    assert migrate(source_path) == {
        'proposals': 1, 'receipt_batches': 0, 'audit_events': 1, 'inventory_observations': 0, 'order_alerts': 1,
        'order_alert_events': 1, 'daily_reports': 0, 'report_jobs': 0,
    }
    assert source_path.read_bytes() == original
    with pytest.raises(SystemExit, match='Target schema must be empty'):
        migrate(source_path)

    with database.transaction() as db:
        assert db.execute('SELECT id, status, receipt FROM proposals').fetchone() == {
            'id': 'old', 'status': 'applied', 'receipt': '{}',
        }
        record(db, actor, 'test', 'new')
        assert change_alert(db, 'legacy-alert', 'acknowledged')

    with database.connect_read_only() as db:
        assert db.execute('SELECT sequence, subject FROM audit_events ORDER BY sequence').fetchall() == [
            {'sequence': 50, 'subject': 'old'}, {'sequence': 51, 'subject': 'new'},
        ]
        assert db.execute('SELECT sequence, status FROM order_alert_events ORDER BY sequence').fetchall() == [
            {'sequence': 80, 'status': 'open'}, {'sequence': 81, 'status': 'acknowledged'},
        ]
