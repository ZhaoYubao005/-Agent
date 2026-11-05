"""Local authenticated API with durable proposals and receipt audit records."""
import asyncio
import csv
import io
import json
import os
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, model_validator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from ecomops.auth import administrator, authorize, operator
from ecomops.graph import graph
from ecomops.operations_graph import operations_graph
from ecomops import operations_store
from ecomops.receipts import replenish_inventory

app = FastAPI(title='EcomOps Agent', version='0.1.0')
apply_lock = asyncio.Lock()


def connect():
    path = Path(os.environ.get('ECOMOPS_AUDIT_DB', '.runtime/audit.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS proposals '
               '(id TEXT PRIMARY KEY, run_id TEXT, payload TEXT, status TEXT, receipt TEXT)')
    return db


class Policy(BaseModel):
    threshold: int = Field(default=10, ge=1, le=1000)
    target: int = Field(default=30, ge=2, le=10000)

    @model_validator(mode='after')
    def validate_target(self):
        if self.target <= self.threshold:
            raise ValueError('target must exceed threshold')
        return self


@app.get('/health')
def health():
    return {'status': 'ok'}


@app.post('/inventory/check', dependencies=[Depends(operator)])
async def check(policy: Policy):
    state = await graph.ainvoke(policy.model_dump())
    run_id = str(uuid.uuid4())
    result = []
    with connect() as db:
        for proposal in state['recommendations']:
            proposal_id = str(uuid.uuid4())
            db.execute('INSERT INTO proposals VALUES (?, ?, ?, ?, ?)',
                       (proposal_id, run_id, json.dumps(proposal), 'proposed', None))
            result.append({'id': proposal_id, 'status': 'proposed', **proposal})
    return {'run_id': run_id, 'checked_at': datetime.now(timezone.utc).isoformat(),
            'policy': policy.model_dump(),
            'counts': {key: len(state[key]) for key in ['products', 'orders', 'inventory']},
            'recommendations': result}


@app.get('/recommendations/{proposal_id}', dependencies=[Depends(authorize)])
def retrieve(proposal_id: str):
    with connect() as db:
        row = db.execute('SELECT * FROM proposals WHERE id=?', (proposal_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Proposal not found')
    return {'id': row['id'], 'run_id': row['run_id'], 'status': row['status'],
            'proposal': json.loads(row['payload']),
            'receipt': json.loads(row['receipt']) if row['receipt'] else None}


@app.post('/recommendations/{proposal_id}/receive', dependencies=[Depends(operator)])
async def receive(proposal_id: str):
    # One worker only in phase one. Crash during application leaves status applying:
    # never blindly replay an ambiguous receipt; inspect Medusa and audit it first.
    async with apply_lock:
        current = retrieve(proposal_id)
        if current['status'] == 'applied':
            return current
        if current['status'] != 'approved':
            raise HTTPException(409, 'Receipt pending reconciliation; do not retry stock writes')
        proposal = current['proposal']
        with connect() as db:
            claimed = db.execute(
                """
                UPDATE proposals SET status='applying'
                WHERE id=? AND status='approved' AND NOT EXISTS (
                    SELECT 1 FROM proposals AS pending
                    WHERE pending.status IN ('applying', 'needs_review')
                    AND json_extract(pending.payload, '$.inventory_item_id')=?
                    AND json_extract(pending.payload, '$.location_id')=?
                )
                """,
                (proposal_id, proposal['inventory_item_id'], proposal['location_id']),
            ).rowcount
        if not claimed:
            current = retrieve(proposal_id)
            if current['status'] == 'applied':
                return current
            raise HTTPException(409, 'Receipt already claimed or this stock location requires reconciliation')
        try:
            receipt = await replenish_inventory(
                inventory_item_id=proposal['inventory_item_id'],
                location_id=proposal['location_id'],
                quantity=proposal['quantity'],
                expected_stocked=proposal['expected_stocked'],
                expected_reserved=proposal['reserved_quantity'],
            )
        except Exception:
            with connect() as db:
                db.execute('UPDATE proposals SET status=? WHERE id=?', ('needs_review', proposal_id))
            raise HTTPException(409, 'Stock application failed or is uncertain; reconcile with Medusa')
        with connect() as db:
            db.execute('UPDATE proposals SET status=?, receipt=? WHERE id=?',
                       ('applied', json.dumps(receipt), proposal_id))
        return retrieve(proposal_id)


def transition(proposal_id: str, allowed: tuple[str, ...], target: str):
    with connect() as db:
        placeholders = ','.join('?' for _ in allowed)
        db.execute(
            f'UPDATE proposals SET status=? WHERE id=? AND status IN ({placeholders})',
            (target, proposal_id, *allowed),
        )
    current = retrieve(proposal_id)
    if current['status'] != target:
        raise HTTPException(409, 'Proposal state does not allow this action')
    return current


@app.post('/recommendations/{proposal_id}/approve', dependencies=[Depends(administrator)])
def approve(proposal_id: str):
    return transition(proposal_id, ('proposed',), 'approved')


@app.post('/recommendations/{proposal_id}/reject', dependencies=[Depends(administrator)])
def reject(proposal_id: str):
    return transition(proposal_id, ('proposed',), 'rejected')


@app.post('/recommendations/{proposal_id}/cancel', dependencies=[Depends(operator)])
def cancel(proposal_id: str):
    return transition(proposal_id, ('proposed', 'approved'), 'cancelled')


@app.get('/operations/orders/alerts', dependencies=[Depends(authorize)])
async def order_alerts(sla_hours: int = Query(default=24, ge=1, le=720)):
    state = await operations_graph.ainvoke({
        'now': datetime.now(timezone.utc), 'sla_hours': sla_hours,
    })
    return state['result']


@app.get('/operations/reports/daily', dependencies=[Depends(authorize)])
async def daily_report(day: date, zone: str = Query(default='UTC', max_length=64)):
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        raise HTTPException(422, 'Unknown IANA time zone')
    if not date(1970, 1, 2) <= day <= date(9999, 12, 30):
        raise HTTPException(422, 'Report date out of range')
    state = await operations_graph.ainvoke({
        'day': day, 'zone': zone, 'now': datetime.now(timezone.utc), 'sla_hours': 24,
    })
    return state['result']


class AlertAction(BaseModel):
    status: Literal['acknowledged', 'ignored', 'closed', 'open']


@app.post('/operations/orders/alerts/refresh', dependencies=[Depends(operator)])
async def refresh_order_alerts(sla_hours: int = Query(default=24, ge=1, le=720)):
    result = await order_alerts(sla_hours)
    with operations_store.operations_db(connect) as db:
        return operations_store.refresh_alerts(db, result['alerts'])


@app.get('/operations/orders/alerts/saved', dependencies=[Depends(authorize)])
def saved_alerts():
    with operations_store.operations_db(connect) as db:
        return operations_store.list_alerts(db)


@app.post('/operations/orders/alerts/{alert_id}/status', dependencies=[Depends(operator)])
def set_alert_status(alert_id: str, action: AlertAction):
    try:
        with operations_store.operations_db(connect) as db:
            operations_store.change_alert(db, alert_id, action.status)
    except KeyError:
        raise HTTPException(404, 'Alert not found')
    except ValueError as error:
        raise HTTPException(409, str(error))
    return {'id': alert_id, 'status': action.status}


@app.post('/operations/reports/daily', dependencies=[Depends(operator)])
async def generate_report(day: date, zone: str = Query(default='UTC', max_length=64)):
    with operations_store.operations_db(connect) as db:
        existing = operations_store.get_report(db, day.isoformat(), zone)
    if existing:
        return existing
    result = await daily_report(day, zone)
    with operations_store.operations_db(connect) as db:
        return operations_store.save_report(db, day.isoformat(), zone, result)


@app.get('/operations/reports/history', dependencies=[Depends(authorize)])
def report_history(limit: int = Query(default=30, ge=1, le=100)):
    with operations_store.operations_db(connect) as db:
        return [dict(row) for row in db.execute(
            'SELECT day, zone, generated_at FROM daily_reports ORDER BY day DESC, zone LIMIT ?',
            (limit,),
        )]


@app.get('/operations/reports/history/{day}', dependencies=[Depends(authorize)])
def historical_report(day: date, zone: str = Query(default='UTC', max_length=64)):
    with operations_store.operations_db(connect) as db:
        result = operations_store.get_report(db, day.isoformat(), zone)
    if result is None:
        raise HTTPException(404, 'Report not found')
    return result


@app.get('/console', include_in_schema=False)
def console():
    return FileResponse(Path(__file__).with_name('console.html'), headers={
        'Cache-Control': 'no-store',
        'Content-Security-Policy': "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    })


@app.get('/operations/reports/history/{day}/export', dependencies=[Depends(authorize)])
def export_report(day: date, zone: str = Query(default='UTC', max_length=64)):
    report = historical_report(day, zone)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['period', 'metric', 'value'])
    for period in ('current', 'previous'):
        snapshot = report[period]
        for metric in ('period_start', 'period_end', 'orders', 'orders_missing_items',
                       'revenue_unavailable_reason'):
            writer.writerow([period, metric, snapshot[metric]])
    return Response(output.getvalue(), media_type='text/csv', headers={
        'Content-Disposition': f'attachment; filename="ecomops-{day.isoformat()}.csv"',
    })
