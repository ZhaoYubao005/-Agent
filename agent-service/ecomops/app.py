"""Local authenticated API with durable proposals and receipt audit records."""
import asyncio
import csv
import io
import json
import logging
import uuid
from uuid import UUID
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, model_validator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from ecomops.audit import record
from ecomops.auth import Actor, administrator, authorize, operator
from ecomops.database import claim_receipt, connect, transaction
from ecomops.graph import graph
from ecomops.inventory_history import save_levels
from ecomops.tools import MCPUnavailable, call, commerce_tools
from ecomops.operations_graph import operations_graph
from ecomops import operations_store
from ecomops.receipts import replenish_inventory
from ecomops.scheduler import run_schedule
from ecomops.workflow import run_graph

@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(run_schedule(connect, generate_report))
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title='EcomOps Agent', version='0.1.0', lifespan=lifespan)
apply_lock = asyncio.Lock()
logger = logging.getLogger('ecomops.requests')


@app.middleware('http')
async def request_trace(request: Request, call_next):
    request_id = uuid.uuid4().hex
    response = await call_next(request)
    response.headers['X-Request-ID'] = request_id
    route = request.scope.get('route')
    logger.info('request_id=%s method=%s route=%s status=%s', request_id,
                request.method, getattr(route, 'path', 'unknown'), response.status_code)
    return response


@app.exception_handler(MCPUnavailable)
async def upstream_unavailable(request: Request, error: MCPUnavailable):
    return JSONResponse(status_code=503, content={'detail': 'Commerce snapshot unavailable'})


class Policy(BaseModel):
    threshold: int = Field(default=10, ge=1, le=1000)
    target: int = Field(default=30, ge=2, le=10000)

    demand_window_days: int | None = Field(default=None, ge=7, le=180)
    lead_time_days: int = Field(default=7, ge=1, le=180)
    safety_stock: int = Field(default=5, ge=0, le=10000)

    @model_validator(mode='after')
    def validate_target(self):
        if self.target <= self.threshold:
            raise ValueError('target must exceed threshold')
        return self


@app.get('/health')
def health():
    return {'status': 'ok'}


@app.post('/inventory/check', dependencies=[Depends(operator)])
async def check(policy: Policy, actor: Actor = Depends(operator)):
    state, trace = await run_graph(graph, {**policy.model_dump(), 'now': datetime.now(timezone.utc)})
    run_id = str(uuid.uuid4())
    result = []
    with transaction() as db:
        save_levels(db, state['inventory'])
        for proposal in state['recommendations']:
            proposal_id = str(uuid.uuid4())
            db.execute('INSERT INTO proposals VALUES (?, ?, ?, ?, ?)',
                       (proposal_id, run_id, json.dumps(proposal), 'proposed', None))
            record(db, actor, 'proposed', proposal_id)
            result.append({'id': proposal_id, 'status': 'proposed', **proposal})
    return {'run_id': run_id, 'checked_at': datetime.now(timezone.utc).isoformat(),
            'policy': policy.model_dump(),
            'counts': {key: len(state[key]) for key in ['products', 'orders', 'inventory']},
            'recommendations': result, 'trace': trace, 'skipped_inventory_levels': state.get('skipped_inventory_levels', [])}


@app.get('/recommendations/{proposal_id}', dependencies=[Depends(authorize)])
def retrieve(proposal_id: str):
    with transaction() as db:
        row = db.execute('SELECT * FROM proposals WHERE id=?', (proposal_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Proposal not found')
    return {'id': row['id'], 'run_id': row['run_id'], 'status': row['status'],
            'proposal': json.loads(row['payload']),
            'receipt': json.loads(row['receipt']) if row['receipt'] else None}


class ReceiptConfirmation(BaseModel):
    request_id: UUID
    quantity: int = Field(ge=1, le=10000, strict=True)


@app.post('/recommendations/{proposal_id}/receive')
async def receive(proposal_id: str, confirmation: ReceiptConfirmation | None = None,
                  actor: Actor = Depends(operator)):
    # A crash after the Medusa request leaves the proposal applying for manual reconciliation.
    async with apply_lock:
        current = retrieve(proposal_id)
        request_id = str(confirmation.request_id) if confirmation else 'full'
        with transaction() as db:
            batch = db.execute("""
                SELECT quantity, status, receipt FROM receipt_batches
                WHERE proposal_id=? AND request_id=?
            """, (proposal_id, request_id)).fetchone()
        if batch:
            if confirmation and confirmation.quantity != batch['quantity']:
                raise HTTPException(409, 'Receipt request ID was already used for a different quantity')
            return {**current, 'status': batch['status'], 'receipt': json.loads(batch['receipt'])}
        if current['status'] == 'applied' and not confirmation:
            return current
        if current['status'] not in {'approved', 'partially_received'}:
            raise HTTPException(409, 'Proposal state does not allow receipt')
        proposal = current['proposal']
        previous_receipt = current['receipt']
        received = previous_receipt['received_quantity'] if previous_receipt else 0
        remaining = proposal['quantity'] - received
        quantity = confirmation.quantity if confirmation else remaining
        if not 1 <= quantity <= remaining:
            raise HTTPException(422, 'Received quantity exceeds the outstanding proposal quantity')
        expected_stocked = previous_receipt['after_stocked'] if previous_receipt else proposal['expected_stocked']
        with transaction() as db:
            claimed = claim_receipt(
                db, proposal_id, proposal['inventory_item_id'], proposal['location_id'],
                current['status'], json.dumps(previous_receipt) if previous_receipt else None,
            )
            if claimed:
                record(db, actor, 'applying', proposal_id, f'Quantity: {quantity}')
        if not claimed:
            raise HTTPException(409, 'Receipt already claimed or this stock location requires reconciliation')
        try:
            receipt = await replenish_inventory(
                inventory_item_id=proposal['inventory_item_id'],
                location_id=proposal['location_id'],
                quantity=quantity,
                expected_stocked=expected_stocked,
                expected_reserved=proposal['reserved_quantity'],
            )
        except Exception:
            with transaction() as db:
                db.execute('UPDATE proposals SET status=? WHERE id=?', ('needs_review', proposal_id))
                record(db, actor, 'needs_review', proposal_id)
            raise HTTPException(409, 'Stock application failed or is uncertain; reconcile with Medusa')
        receipt.update(batch_quantity=quantity, received_quantity=received + quantity,
                       remaining_quantity=remaining - quantity)
        target = 'applied' if receipt['remaining_quantity'] == 0 else 'partially_received'
        payload = json.dumps(receipt)
        with transaction() as db:
            db.execute('UPDATE proposals SET status=?, receipt=? WHERE id=?',
                       (target, payload, proposal_id))
            db.execute('INSERT INTO receipt_batches VALUES (?, ?, ?, ?, ?)',
                       (proposal_id, request_id, quantity, target, payload))
            record(db, actor, target, proposal_id, f'Quantity: {quantity}')
        return {**current, 'status': target, 'receipt': receipt}


def transition(proposal_id: str, allowed: tuple[str, ...], target: str, actor: Actor, note=None):
    with transaction() as db:
        placeholders = ','.join('?' for _ in allowed)
        changed = db.execute(
            f'UPDATE proposals SET status=? WHERE id=? AND status IN ({placeholders})',
            (target, proposal_id, *allowed),
        ).rowcount
        if changed:
            record(db, actor, target, proposal_id, note)
    current = retrieve(proposal_id)
    if current['status'] != target:
        raise HTTPException(409, 'Proposal state does not allow this action')
    return current


@app.post('/recommendations/{proposal_id}/approve', dependencies=[Depends(administrator)])
def approve(proposal_id: str, actor: Actor = Depends(administrator)):
    return transition(proposal_id, ('proposed',), 'approved', actor)


@app.post('/recommendations/{proposal_id}/reject', dependencies=[Depends(administrator)])
def reject(proposal_id: str, actor: Actor = Depends(administrator)):
    return transition(proposal_id, ('proposed',), 'rejected', actor)


@app.post('/recommendations/{proposal_id}/cancel', dependencies=[Depends(operator)])
def cancel(proposal_id: str, actor: Actor = Depends(operator)):
    return transition(proposal_id, ('proposed', 'approved', 'partially_received'), 'cancelled', actor)


@app.get('/operations/orders/alerts', dependencies=[Depends(authorize)])
async def order_alerts(sla_hours: int = Query(default=24, ge=1, le=720)):
    state, trace = await run_graph(operations_graph, {
        'now': datetime.now(timezone.utc), 'sla_hours': sla_hours,
    })
    return {**state['result'], 'trace': trace}


@app.get('/operations/reports/daily', dependencies=[Depends(authorize)])
async def daily_report(day: date, zone: str = Query(default='UTC', max_length=64)):
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        raise HTTPException(422, 'Unknown IANA time zone')
    if not date(1970, 1, 2) <= day <= date(9999, 12, 30):
        raise HTTPException(422, 'Report date out of range')
    state, trace = await run_graph(operations_graph, {
        'day': day, 'zone': zone, 'now': datetime.now(timezone.utc), 'sla_hours': 24,
    })
    return {**state['result'], 'trace': trace}


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
def set_alert_status(alert_id: str, action: AlertAction, actor: Actor = Depends(operator)):
    try:
        with operations_store.operations_db(connect) as db:
            if operations_store.change_alert(db, alert_id, action.status):
                record(db, actor, f'alert_{action.status}', alert_id)
    except KeyError:
        raise HTTPException(404, 'Alert not found')
    except ValueError as error:
        raise HTTPException(409, str(error))
    return {'id': alert_id, 'status': action.status}


@app.post('/operations/reports/daily', dependencies=[Depends(operator)])
async def generate_report(day: date, zone: str = Query(default='UTC', max_length=64)):
    try:
        report_zone = ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        raise HTTPException(422, 'Unknown IANA time zone')
    if day >= datetime.now(report_zone).date():
        raise HTTPException(422, 'Only a completed business day can be saved. Use the report preview for today.')
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
        for metric in ('statuses', 'fulfillment_statuses', 'payment_statuses',
                       'ordered_units_by_variant', 'order_value_by_currency'):
            for name, value in snapshot.get(metric, {}).items():
                writer.writerow([period, f'{metric}.{name}', value])
    return Response(output.getvalue(), media_type='text/csv', headers={
        'Content-Disposition': f'attachment; filename="ecomops-{day.isoformat()}.csv"',
    })


class Reconciliation(BaseModel):
    note: str = Field(min_length=8, max_length=500, pattern=r'\S')


@app.post('/recommendations/{proposal_id}/reconcile')
def reconcile(proposal_id: str, details: Reconciliation, actor: Actor = Depends(administrator)):
    return transition(proposal_id, ('needs_review',), 'reconciled', actor, details.note)


@app.get('/operations/audit', dependencies=[Depends(administrator)])
def audit_log(limit: int = Query(default=50, ge=1, le=200)):
    with transaction() as db:
        return [dict(row) for row in db.execute(
            'SELECT * FROM audit_events ORDER BY sequence DESC LIMIT ?', (limit,),
        )]


@app.get('/operations/tasks', dependencies=[Depends(authorize)])
def task_history(limit: int = Query(default=30, ge=1, le=100)):
    with operations_store.operations_db(connect) as db:
        return [dict(row) for row in db.execute(
            'SELECT day, zone, claim, state FROM report_jobs ORDER BY claim DESC LIMIT ?', (limit,),
        )]


@app.get('/inventory/levels', dependencies=[Depends(authorize)])
async def inventory_levels(sku: str | None = Query(None, max_length=200),
                           location_id: str | None = Query(None, max_length=200)):
    async with commerce_tools() as session:
        inventory = await call(session, 'list_inventory')
    return [
        {'inventory_item_id': item['id'], 'sku': item.get('sku'), **level}
        for item in inventory if sku is None or item.get('sku') == sku
        for level in item.get('location_levels') or []
        if location_id is None or level['location_id'] == location_id
    ]


@app.get('/inventory/history', dependencies=[Depends(authorize)])
def inventory_history(sku: str = Query(min_length=1, max_length=200),
                      location_id: str = Query(min_length=1, max_length=200),
                      limit: int = Query(default=30, ge=1, le=100)):
    with transaction() as db:
        return [dict(row) for row in db.execute(
            """SELECT * FROM inventory_observations WHERE sku=? AND location_id=?
               ORDER BY observed_at DESC LIMIT ?""",
            (sku, location_id, limit),
        )]


@app.get('/identity')
def identity(actor: Actor = Depends(authorize)):
    return {'account': actor.name, 'role': actor.role}
