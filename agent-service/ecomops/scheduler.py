"""Run the most recently completed local business day on a bounded schedule."""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def scheduled_day(now, zone, hour):
    local = now.astimezone(ZoneInfo(zone))
    if local.hour >= hour:
        return local.date() - timedelta(days=1)
    return None


async def run_schedule(connect, generate):
    zone = os.environ.get('ECOMOPS_REPORT_ZONE')
    hour = os.environ.get('ECOMOPS_REPORT_HOUR')
    if not zone and not hour:
        return
    try:
        ZoneInfo(zone or '')
        hour = int(hour or '')
        if not 0 <= hour <= 23:
            raise ValueError('Hour out of range')
    except (ValueError, KeyError):
        logger.error('Invalid daily report scheduling configuration')
        return
    while True:
        try:
            await run_tick(connect, generate, zone, hour)
        except Exception as error:
            logger.warning('daily_report scheduler unavailable: %s', type(error).__name__)
        await asyncio.sleep(30)


async def run_tick(connect, generate, zone, hour, now=None):
    now = now or datetime.now(timezone.utc)
    day = scheduled_day(now, zone, hour)
    if day:
        await execute_report_job(connect, generate, day, zone, now)


async def execute_report_job(connect, generate, day, zone, now=None):
    from ecomops.operations_store import get_report, operations_db

    now = now or datetime.now(timezone.utc)
    token = now.isoformat()
    key = (day.isoformat(), zone)
    with operations_db(connect) as db:
        saved = get_report(db, *key)
        if saved:
            db.execute("UPDATE report_jobs SET state='completed' WHERE day=? AND zone=? AND state='running'", key)
            return saved
        claimed = db.execute("""
            INSERT INTO report_jobs (day, zone, claim, state) VALUES (?, ?, ?, 'running')
            ON CONFLICT(day, zone) DO UPDATE SET claim=excluded.claim, state='running'
            WHERE report_jobs.claim < ?
        """, (*key, token, (now - timedelta(minutes=5)).isoformat())).rowcount
    if not claimed:
        return None

    status = 'failed'
    try:
        async with asyncio.timeout(120):
            report = await generate(day, zone)
        status = 'completed'
        return report
    finally:
        with operations_db(connect) as db:
            db.execute('UPDATE report_jobs SET state=? WHERE day=? AND zone=? AND claim=?',
                       (status, *key, token))
