"""Administrator-only independent payment and revenue reconciliation."""
import asyncio
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from ecomops.audit import record
from ecomops.auth import Actor, administrator
from ecomops.database import transaction
from ecomops.finance import (
    MAX_CSV_BYTES,
    MAX_CSV_ROWS,
    BankSettlement,
    FinanceInputError,
    RevenueEntry,
    parse_bank_csv,
    parse_revenue_csv,
    reconcile_bank,
    select_medusa_transactions,
    summarize_revenue,
)
from ecomops.tools import MCPUnavailable, call, commerce_tools


router = APIRouter(prefix='/operations/finance')
Kind = Literal['bank', 'revenue']


class SourceConflict(ValueError):
    """The source reused a previously stored record identifier with changed values."""


def utc_key(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec='microseconds')


def persist_import(kind, rows, digest, actor):
    created = 0
    imported_at = utc_key(datetime.now(timezone.utc))
    # Lock the same key order across concurrent files with overlapping records.
    source_rows = sorted(
        ((row.event_id, utc_key(row.posted_at), row) if kind == 'bank'
         else (row.entry_id, row.date.isoformat(), row) for row in rows),
        key=lambda item: item[0],
    )
    with transaction() as db:
        for source_id, effective_at, row in source_rows:
            inserted = db.execute("""
                INSERT INTO finance_records (kind, source_id, effective_at, payload, source_digest, imported_at)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(kind, source_id) DO NOTHING
            """, (kind, source_id, effective_at, row.model_dump_json(), digest, imported_at)).rowcount
            if inserted:
                created += 1
                continue
            existing = db.execute(
                'SELECT payload FROM finance_records WHERE kind=? AND source_id=?', (kind, source_id),
            ).fetchone()
            if type(row).model_validate_json(existing['payload']) != row:
                raise SourceConflict
        if created:
            record(db, actor, 'finance_import', kind, f'SHA-256 {digest} and {created} new records')
    return created


@router.post('/import/{kind}')
async def import_finance(kind: Kind, request: Request, response: Response,
                         actor: Actor = Depends(administrator)):
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'text/csv':
        raise HTTPException(415, 'Upload a UTF-8 CSV with Content-Type text/csv')
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_CSV_BYTES:
            raise HTTPException(413, 'The CSV exceeds the 1 MiB limit')
        content.extend(chunk)
    raw = bytes(content)
    digest = sha256(raw).hexdigest()
    parser = parse_bank_csv if kind == 'bank' else parse_revenue_csv
    try:
        rows = await run_in_threadpool(parser, raw)
        created = await run_in_threadpool(persist_import, kind, rows, digest, actor)
    except FinanceInputError as error:
        raise HTTPException(422, str(error)) from None
    except SourceConflict:
        raise HTTPException(409, 'An existing source ID has different values. The entire import was rolled back.') from None
    response.headers['Cache-Control'] = 'no-store'
    return {'kind': kind, 'sha256': digest, 'imported_count': created, 'duplicate_count': len(rows) - created}


def read_period(start, end, day, as_of):
    def read(db, kind, lower, upper, model):
        rows = db.execute("""
            SELECT payload FROM finance_records WHERE kind=? AND effective_at>=? AND effective_at<?
            ORDER BY effective_at, source_id LIMIT ?
        """, (kind, lower, upper, MAX_CSV_ROWS + 1)).fetchall()
        if len(rows) > MAX_CSV_ROWS:
            raise HTTPException(422, 'The selected finance day exceeds the record limit')
        return [model.model_validate_json(row['payload']) for row in rows]

    with transaction() as db:
        # Preserve the half-open day window and include postings exactly at the cutoff.
        bank_end = min(end, as_of + timedelta(microseconds=1))
        bank = read(db, 'bank', utc_key(start), utc_key(bank_end), BankSettlement)
        revenue = read(db, 'revenue', day.isoformat(), (day + timedelta(days=1)).isoformat(), RevenueEntry)
    return bank, revenue


async def fetch_medusa_ledger():
    try:
        async with asyncio.timeout(20):
            async with commerce_tools() as session:
                return await call(session, 'list_orders', {'purpose': 'ledger'})
    except (MCPUnavailable, TimeoutError):
        return None


@router.get('/reconciliation', dependencies=[Depends(administrator)])
async def finance_report(day: date, response: Response, zone: str = Query('UTC', max_length=64)):
    if not date(1970, 1, 1) <= day <= date(9999, 12, 30):
        raise HTTPException(422, 'Finance day out of range')
    try:
        local_zone = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, 'Unknown IANA time zone') from None
    start = datetime.combine(day, time.min, local_zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, local_zone).astimezone(timezone.utc)
    as_of = datetime.now(timezone.utc)
    bank, revenue = await run_in_threadpool(read_period, start, end, day, as_of)
    ledger = await fetch_medusa_ledger()
    medusa = select_medusa_transactions(
        ledger, start=start, end=end, bank_refs={(row.event_type, row.reference_id) for row in bank},
        as_of=as_of,
    )
    response.headers['Cache-Control'] = 'no-store'
    return {
        'day': day.isoformat(), 'zone': zone, 'as_of': utc_key(as_of),
        'period_start': utc_key(start), 'period_end': utc_key(end),
        'scope': 'imported_records_only', 'coverage_attested': False,
        'bank_reconciliation': reconcile_bank(bank or None, medusa),
        'recognized_revenue': summarize_revenue(revenue or None),
    }
