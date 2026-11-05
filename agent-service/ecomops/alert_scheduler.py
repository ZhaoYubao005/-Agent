"""Periodically persist the order workflow's observations under a database lease."""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ecomops.operations_store import operations_db, refresh_alerts


logger = logging.getLogger(__name__)
MIN_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 86_400
SCAN_TIMEOUT_SECONDS = 90
LEASE_SECONDS = 120
POLL_SECONDS = 5


def _configuration():
    interval = int(os.environ.get('ECOMOPS_ALERT_SCAN_INTERVAL_SECONDS') or '0')
    if interval == 0:
        return None
    if not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
        raise ValueError('Invalid order alert scan interval')
    sla = int(os.environ.get('ECOMOPS_ALERT_SCAN_SLA_HOURS') or '24')
    if not 1 <= sla <= 720:
        raise ValueError('Invalid order alert scan SLA')
    return interval, sla


def alert_scan_status(connect):
    error = None
    try:
        config = _configuration()
    except ValueError:
        config = None
        error = 'Invalid order alert scan configuration'
    interval, sla = config or (None, None)
    with operations_db(connect) as db:
        row = db.execute("""
            SELECT state, next_run_at, lease_until, started_at, finished_at,
                   failure_count, alert_count, error
            FROM order_alert_scan_jobs WHERE id='orders'
        """).fetchone()
    return {
        'enabled': config is not None, 'interval_seconds': interval, 'sla_hours': sla,
        'configuration_error': error, 'last_run': dict(row) if row is not None else None,
    }


async def run_alert_schedule(connect, scan):
    """Run an optional schedule with the same read-only scanner as manual refresh."""
    try:
        config = _configuration()
    except ValueError:
        logger.error('Invalid order alert scan configuration')
        return
    if config is None:
        return
    while True:
        try:
            await run_alert_scan_tick(connect, scan, *config)
        except Exception as error:
            logger.warning('Order alert scheduler unavailable: %s', type(error).__name__)
        await asyncio.sleep(POLL_SECONDS)


def _claim(connect, token, started):
    lease_until = (started + timedelta(seconds=LEASE_SECONDS)).isoformat()
    started_at = started.isoformat()
    with operations_db(connect) as db:
        return db.execute("""
            INSERT INTO order_alert_scan_jobs
                (id, claim, state, next_run_at, lease_until, started_at, failure_count)
            VALUES ('orders', ?, 'running', ?, ?, ?, 0)
            ON CONFLICT(id) DO UPDATE SET
                claim=excluded.claim, state='running', next_run_at=excluded.next_run_at,
                lease_until=excluded.lease_until, started_at=excluded.started_at,
                finished_at=NULL, alert_count=NULL, error=NULL,
                failure_count=order_alert_scan_jobs.failure_count +
                    CASE WHEN order_alert_scan_jobs.state='running' THEN 1 ELSE 0 END
            WHERE order_alert_scan_jobs.next_run_at <= excluded.started_at
                AND (order_alert_scan_jobs.lease_until IS NULL
                    OR order_alert_scan_jobs.lease_until <= excluded.started_at)
            RETURNING failure_count
        """, (token, lease_until, lease_until, started_at)).fetchone()


def _complete(connect, token, finished, interval_seconds, alerts):
    next_run_at = (finished + timedelta(seconds=interval_seconds)).isoformat()
    with operations_db(connect) as db:
        # Fence the observation write before touching alert rows. The transaction
        # rolls back both the completion marker and any partial alert refresh.
        updated = db.execute("""
            UPDATE order_alert_scan_jobs SET
                claim=NULL, state='completed', next_run_at=?, lease_until=NULL,
                finished_at=?, failure_count=0, alert_count=?, error=NULL
            WHERE id='orders' AND claim=? AND lease_until>?
        """, (next_run_at, finished.isoformat(), len(alerts), token, finished.isoformat())).rowcount
        if not updated:
            return False
        refresh_alerts(db, alerts)
        return True


def _fail(connect, token, finished, interval_seconds, failure_count, error):
    delay = min(interval_seconds, MIN_INTERVAL_SECONDS * 2 ** min(failure_count - 1, 12))
    next_run_at = (finished + timedelta(seconds=delay)).isoformat()
    with operations_db(connect) as db:
        db.execute("""
            UPDATE order_alert_scan_jobs SET
                claim=NULL, state='failed', next_run_at=?, lease_until=NULL,
                finished_at=?, failure_count=?, alert_count=NULL, error=?
            WHERE id='orders' AND claim=? AND lease_until>?
        """, (next_run_at, finished.isoformat(), failure_count, error, token, finished.isoformat()))


async def run_alert_scan_tick(connect, scan, interval_seconds, sla_hours=24, *, now=None):
    """Claim one due scan, with a bounded read and an atomic alert refresh."""
    token = uuid4().hex
    started = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    claimed = await asyncio.to_thread(_claim, connect, token, started)
    if claimed is None:
        return False
    try:
        async with asyncio.timeout(SCAN_TIMEOUT_SECONDS):
            alerts = await scan(sla_hours)
        finished = started if now is not None else datetime.now(timezone.utc)
        return await asyncio.to_thread(_complete, connect, token, finished, interval_seconds, alerts)
    except Exception as error:
        kind = type(error).__name__
        finished = started if now is not None else datetime.now(timezone.utc)
        logger.warning('Order alert scan failed: %s', kind)
        await asyncio.to_thread(_fail, connect, token, finished, interval_seconds,
                                claimed['failure_count'] + 1, kind)
        return False
