"""Bounded report backfill after a scheduler outage."""
from datetime import date, datetime, timezone

import pytest

from ecomops.database import connect, transaction
from ecomops.operations_store import save_report
from ecomops.scheduler import run_tick


@pytest.mark.asyncio
async def test_report_catchup_respects_local_cutoff_and_lookback(monkeypatch, tmp_path):
    monkeypatch.delenv('ECOMOPS_DATABASE_URL', raising=False)
    monkeypatch.setenv('ECOMOPS_AUDIT_DB', str(tmp_path / 'schedule.db'))
    # Nov 6 at 06:00 local. The report for Nov 5 is due one hour later.
    now = datetime(2026, 11, 5, 22, tzinfo=timezone.utc)
    runs = []

    async def generate(day, zone):
        runs.append(day)
        with transaction() as db:
            return save_report(db, day.isoformat(), zone, {'current': {'orders': 1}})

    for _ in range(4):
        await run_tick(connect, generate, 'Asia/Shanghai', 7, now, lookback_days=3)

    assert runs == [date(2026, 11, 4), date(2026, 11, 3), date(2026, 11, 2)]
    with transaction() as db:
        rows = db.execute('SELECT day, state FROM report_jobs ORDER BY day DESC').fetchall()
    assert [(row['day'], row['state']) for row in rows] == [
        (day.isoformat(), 'completed') for day in runs
    ]
