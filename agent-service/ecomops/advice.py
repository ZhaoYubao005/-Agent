"""Optional structured interpretation of aggregate metrics."""
import json
import os
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Advice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    summary: str = Field(min_length=1, max_length=500)
    actions: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(max_length=4)


async def interpret_report(current, previous):
    facts = {
        'current_orders': current['orders'],
        'previous_orders': previous['orders'],
        'revenue_available': current['revenue'] is not None,
    }
    fallback = {
        'source': 'rules',
        'summary': f"Orders: {facts['current_orders']} versus {facts['previous_orders']} in the prior period.",
        'actions': ['Review fulfillment alerts and reconcile payment data separately.'],
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
                'Explain only the supplied aggregate facts. Do not infer revenue, causality '
                'or customer details. Suggest manual review only. Return JSON with a short '
                'summary string and an actions array of up to four short strings.'
            )},
            {'role': 'user', 'content': json.dumps(facts)},
        ],
    }
    if protocol == 'ollama':
        path = '/api/chat'
        payload.update(format=Advice.model_json_schema(), options={'temperature': 0, 'num_predict': 250})
    else:
        path = '/chat/completions'
        payload.update(response_format={'type': 'json_object'}, max_tokens=400)
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
    return {'source': 'llm', **advice.model_dump(), 'trace': 'structured_output_validated'}
