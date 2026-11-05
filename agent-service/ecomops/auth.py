"""Role-scoped credentials for local and internal operators."""
import hmac
import json
import os
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


@dataclass(frozen=True)
class Actor:
    name: str
    role: str


security = HTTPBearer()


def authorize(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    configured = os.environ.get('ECOMOPS_ACCOUNTS')
    try:
        accounts = json.loads(configured) if configured else {}
        if not isinstance(accounts, dict):
            raise ValueError
        if any(not isinstance(entry, dict)
               or entry.get('role') not in {'admin', 'operator', 'viewer'}
               or not isinstance(entry.get('token'), str)
               or not entry['token'] for entry in accounts.values()):
            raise ValueError
        if len({entry['token'] for entry in accounts.values()}) != len(accounts):
            raise ValueError
    except ValueError:
        raise HTTPException(503, 'Account configuration unavailable') from None
    legacy = os.environ.get('ECOMOPS_API_TOKEN')
    if legacy and not configured:
        accounts = {'local-service': {'role': 'admin', 'token': legacy}}
    for name, account in accounts.items():
        token = account['token']
        if token and hmac.compare_digest(credentials.credentials.encode(), token.encode()):
            actor = Actor(name, account['role'])
            request.state.actor = actor
            return actor
    raise HTTPException(401, 'Invalid service token')


def operator(actor: Actor = Depends(authorize)):
    if actor.role not in {'admin', 'operator'}:
        raise HTTPException(403, 'Operator access required')
    return actor


def administrator(actor: Actor = Depends(authorize)):
    if actor.role != 'admin':
        raise HTTPException(403, 'Administrator access required')
    return actor
