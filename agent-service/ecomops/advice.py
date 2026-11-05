"""Rank evidence-backed advice without accepting model-authored facts."""
import json
import os
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


Action = Literal['review_orders', 'reconcile_payments', 'review_fulfillment', 'reconcile_inventory']
ACTION_TEXT: dict[Action, str] = {
    'review_orders': 'Compare current and prior order counts before changing operations.',
    'reconcile_payments': 'Reconcile payment and refund records before estimating revenue.',
    'review_fulfillment': 'Review the orders flagged by the fulfillment SLA rule.',
    'reconcile_inventory': 'Verify stock levels and reservations for inventory flagged by the rules.',
}


class Advice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    actions: list[Action] = Field(min_length=1, max_length=4)


async def interpret_report(current, previous, snapshot=None):
    facts = {
        'current_orders': current['orders'],
        'previous_orders': previous['orders'],
        'revenue_available': current['revenue'] is not None,
    }
    if snapshot is not None:
        facts.update({key: snapshot[key] for key in (
            'fulfillment_sla_alerts', 'inventory_shortage_alerts', 'inventory_levels_out_of_stock',
        )})
    summary = f"Orders: {facts['current_orders']} versus {facts['previous_orders']} in the prior period."
    allowed: list[Action] = []
    if facts.get('inventory_shortage_alerts') or facts.get('inventory_levels_out_of_stock'):
        allowed.append('reconcile_inventory')
    if facts.get('fulfillment_sla_alerts'):
        allowed.append('review_fulfillment')
    if not facts['revenue_available']:
        summary += ' Revenue is unavailable because payment and refund records are missing from this snapshot.'
        allowed.append('reconcile_payments')
    allowed.append('review_orders')
    fallback = {
        'source': 'rules',
        'summary': summary,
        'actions': [ACTION_TEXT[action] for action in allowed],
    }
    base_url = os.environ.get('ECOMOPS_LLM_URL')
    model = os.environ.get('ECOMOPS_LLM_MODEL')
    protocol = os.environ.get('ECOMOPS_LLM_PROTOCOL') or 'openai'
    if not base_url or not model:
        return {**fallback, 'trace': 'provider_disabled'}
    if protocol not in {'openai', 'ollama'}:
        return {**fallback, 'trace': 'invalid_provider_configuration'}
    key = os.environ.get('ECOMOPS_LLM_API_KEY')
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    payload = {
        'model': model,
        'stream': False,
        'messages': [
            {'role': 'system', 'content': (
                'Rank the most relevant manual review actions using only the supplied data. '
                'Return a JSON object with an actions array containing one to four codes '
                'from allowed_actions in priority order. Do not include any other fields.'
            )},
            {'role': 'user', 'content': json.dumps({**facts, 'allowed_actions': allowed})},
        ],
    }
    if protocol == 'ollama':
        path = '/api/chat'
        payload.update(format=Advice.model_json_schema(), options={'temperature': 0, 'num_predict': 120})
    else:
        path = '/chat/completions'
        payload.update(response_format={'type': 'json_object'}, max_tokens=150)
    try:
        timeout = 30 if protocol == 'ollama' else 12
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(base_url.rstrip('/') + path, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            message = data['message'] if protocol == 'ollama' else data['choices'][0]['message']
            advice = Advice.model_validate_json(message['content'])
    except (httpx.HTTPError, httpx.InvalidURL):
        return {**fallback, 'trace': 'provider_unavailable'}
    except (ValidationError, ValueError, KeyError, IndexError, TypeError):
        return {**fallback, 'trace': 'invalid_provider_output'}
    if any(action not in allowed for action in advice.actions):
        return {**fallback, 'trace': 'unsupported_provider_action'}
    return {
        'source': 'llm',
        'summary': summary,
        'actions': [ACTION_TEXT[action] for action in dict.fromkeys(advice.actions)],
        'trace': 'structured_output_validated',
    }
