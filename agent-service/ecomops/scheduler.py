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
    from ecomops.operations_store import get_report, operations_db

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
        now = datetime.now(timezone.utc)
        day = scheduled_day(now, zone, hour)
        if day:
            token = now.isoformat()
            with operations_db(connect) as db:
                saved = get_report(db, day.isoformat(), zone)
                claimed = not saved and db.execute("""
                    INSERT INTO report_jobs (day, zone, claim, state) VALUES (?, ?, ?, 'running')
                    ON CONFLICT(day, zone) DO UPDATE SET claim=excluded.claim, state='running'
                    WHERE report_jobs.claim < ?
                """, (day.isoformat(), zone, token, (now - timedelta(minutes=5)).isoformat())).rowcount
            if claimed:
                status = 'completed'
                try:
                    async with asyncio.timeout(120):
                        await generate(day, zone)
                except Exception as error:
                    status = 'failed'
                    logger.warning('daily_report job failed: %s', type(error).__name__)
                with operations_db(connect) as db:
                    db.execute('UPDATE report_jobs SET state=? WHERE day=? AND zone=? AND claim=?',
                               (status, day.isoformat(), zone, token))
        await asyncio.sleep(30)
