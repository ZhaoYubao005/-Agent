import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from ecomops import database
from ecomops.operations_store import change_alert, list_alerts


NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
ALERT = {'id': 'fulfillment_sla:order_1', 'age_hours': 24}


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.delenv('ECOMOPS_DATABASE_URL', raising=False)
    monkeypatch.delenv('ECOMOPS_ALERT_SCAN_INTERVAL_SECONDS', raising=False)
    monkeypatch.delenv('ECOMOPS_ALERT_SCAN_SLA_HOURS', raising=False)
    monkeypatch.setenv('ECOMOPS_AUDIT_DB', str(tmp_path / 'audit.sqlite3'))


async def test_concurrent_worker_claims_one_scan_and_persists_next_run():
    from ecomops.alert_scheduler import alert_scan_status, run_alert_scan_tick

    scanning = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def scan(sla_hours):
        calls.append(sla_hours)
        scanning.set()
        await release.wait()
        return [ALERT]

    first = asyncio.create_task(run_alert_scan_tick(database.connect, scan, 60, 48, now=NOW))
    try:
        await asyncio.wait_for(scanning.wait(), timeout=5)
        assert not await run_alert_scan_tick(database.connect, scan, 60, 48, now=NOW)
    finally:
        release.set()
        completed = await first
    assert completed
    assert not await run_alert_scan_tick(database.connect, scan, 60, 48, now=NOW + timedelta(seconds=59))
    assert calls == [48]
    run = alert_scan_status(database.connect)['last_run']
    assert run['state'] == 'completed'
    assert run['alert_count'] == 1
    assert run['next_run_at'] == (NOW + timedelta(seconds=60)).isoformat()
    assert 'claim' not in run


@pytest.mark.parametrize('status', ['acknowledged', 'ignored', 'closed'])
async def test_next_scan_updates_observation_and_preserves_operator_decision(status):
    from ecomops.alert_scheduler import run_alert_scan_tick

    alert = ALERT.copy()

    async def scan(_):
        return [alert]

    assert await run_alert_scan_tick(database.connect, scan, 30, now=NOW)
    with database.transaction() as db:
        change_alert(db, alert['id'], status)

    alert['age_hours'] = 48
    assert await run_alert_scan_tick(database.connect, scan, 30, now=NOW + timedelta(seconds=30))
    with database.transaction() as db:
        [saved] = list_alerts(db)
        assert saved.pop('last_seen_at')
        assert saved == {**alert, 'status': status}
        assert [row['status'] for row in db.execute('SELECT status FROM order_alert_events')] == [status]


async def test_expired_worker_cannot_write_alerts_after_another_worker_completes():
    from ecomops.alert_scheduler import run_alert_scan_tick

    scanning = asyncio.Event()
    release = asyncio.Event()

    async def stale_scan(_):
        scanning.set()
        await release.wait()
        return [ALERT]

    async def current_scan(_):
        return [{**ALERT, 'age_hours': 48}]

    stale = asyncio.create_task(run_alert_scan_tick(database.connect, stale_scan, 60, now=NOW))
    try:
        await asyncio.wait_for(scanning.wait(), timeout=5)
        assert await run_alert_scan_tick(database.connect, current_scan, 60, now=NOW + timedelta(minutes=10))
    finally:
        release.set()
        completed = await stale
    assert not completed
    with database.transaction() as db:
        [saved] = list_alerts(db)
        assert saved['age_hours'] == 48


async def test_failed_scan_rolls_back_partial_updates_and_retries_with_bounded_backoff():
    from ecomops.alert_scheduler import alert_scan_status, run_alert_scan_tick

    async def invalid_snapshot(_):
        return [ALERT, {'unexpected': True}]

    async def unavailable(_):
        raise RuntimeError('private upstream credentials')

    async def scan(_):
        return [ALERT]

    async def tick(scan_function, seconds):
        return await run_alert_scan_tick(database.connect, scan_function, 45, now=NOW + timedelta(seconds=seconds))

    assert not await tick(invalid_snapshot, 0)
    with database.transaction() as db:
        assert list_alerts(db) == []
    assert not await tick(scan, 29)
    assert not await tick(unavailable, 30)
    run = alert_scan_status(database.connect)['last_run']
    assert run['state'] == 'failed'
    assert run['error'] == 'RuntimeError'
    assert run['failure_count'] == 2
    assert run['next_run_at'] == (NOW + timedelta(seconds=75)).isoformat()
    assert not await tick(scan, 74)
    assert await tick(scan, 75)
    run = alert_scan_status(database.connect)['last_run']
    assert run['state'] == 'completed'
    assert run['error'] is None
    assert run['failure_count'] == 0


async def test_scan_timeout_persists_failure(monkeypatch):
    from ecomops import alert_scheduler

    monkeypatch.setattr(alert_scheduler, 'SCAN_TIMEOUT_SECONDS', 0)

    async def stalled(_):
        await asyncio.Event().wait()

    assert not await alert_scheduler.run_alert_scan_tick(database.connect, stalled, 60, now=NOW)
    run = alert_scheduler.alert_scan_status(database.connect)['last_run']
    assert run['state'] == 'failed'
    assert run['error'] == 'TimeoutError'


@pytest.mark.parametrize('interval', [None, '0', '1'])
async def test_disabled_or_invalid_schedule_does_not_scan(monkeypatch, interval):
    from ecomops.alert_scheduler import alert_scan_status, run_alert_schedule

    if interval is not None:
        monkeypatch.setenv('ECOMOPS_ALERT_SCAN_INTERVAL_SECONDS', interval)

    async def unexpected_scan(_):
        pytest.fail('Schedule must not scan')

    await asyncio.wait_for(run_alert_schedule(database.connect, unexpected_scan), timeout=1)
    status = alert_scan_status(database.connect)
    assert not status['enabled']
    assert status['last_run'] is None
    assert bool(status['configuration_error']) == (interval == '1')
