"""Local authenticated API with durable proposals and receipt audit records."""
import asyncio
import hmac
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, model_validator
from ecomops.graph import graph
from ecomops.receipts import replenish_inventory

app = FastAPI(title='EcomOps Agent', version='0.1.0')
security = HTTPBearer()
apply_lock = asyncio.Lock()


def connect():
    path = Path(os.environ.get('ECOMOPS_AUDIT_DB', '.runtime/audit.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS proposals '
               '(id TEXT PRIMARY KEY, run_id TEXT, payload TEXT, status TEXT, receipt TEXT)')
    return db


def authorize(credentials: HTTPAuthorizationCredentials = Depends(security)):
    expected = os.environ.get('ECOMOPS_API_TOKEN', '')
    if not expected or not hmac.compare_digest(credentials.credentials, expected):
        raise HTTPException(401, 'Invalid service token')


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


@app.post('/inventory/check', dependencies=[Depends(authorize)])
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


@app.post('/recommendations/{proposal_id}/receive', dependencies=[Depends(authorize)])
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
                "UPDATE proposals SET status='applying' WHERE id=? AND status='approved'",
                (proposal_id,),
            ).rowcount
        if not claimed:
            current = retrieve(proposal_id)
            if current['status'] == 'applied':
                return current
            raise HTTPException(409, 'Receipt already claimed')
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


@app.post('/recommendations/{proposal_id}/approve', dependencies=[Depends(authorize)])
def approve(proposal_id: str):
    return transition(proposal_id, ('proposed',), 'approved')


@app.post('/recommendations/{proposal_id}/reject', dependencies=[Depends(authorize)])
def reject(proposal_id: str):
    return transition(proposal_id, ('proposed',), 'rejected')


@app.post('/recommendations/{proposal_id}/cancel', dependencies=[Depends(authorize)])
def cancel(proposal_id: str):
    return transition(proposal_id, ('proposed', 'approved'), 'cancelled')
