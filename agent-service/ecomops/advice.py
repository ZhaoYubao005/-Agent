"""Optional structured interpretation of aggregate metrics."""
import os

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Advice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    summary: str = Field(min_length=1, max_length=500)
    actions: list[str] = Field(max_length=4)


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
    if not base_url or not model:
        return {**fallback, 'trace': 'provider_disabled'}
    key = os.environ.get('ECOMOPS_LLM_API_KEY')
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    try:
        async with httpx.AsyncClient(timeout=12, trust_env=False) as client:
            response = await client.post(
                base_url.rstrip('/') + '/chat/completions',
                headers=headers,
                json={
                    'model': model,
                    'response_format': {'type': 'json_object'},
                    'max_tokens': 400,
                    'messages': [
                        {'role': 'system', 'content': (
                            'Explain only the supplied aggregate facts. Do not infer revenue, causality '
                            'or customer details. Suggest manual review only. Return JSON with a short '
                            'summary string and an actions array of up to four short strings.'
                        )},
                        {'role': 'user', 'content': str(facts)},
                    ],
                },
            )
            response.raise_for_status()
            advice = Advice.model_validate_json(response.json()['choices'][0]['message']['content'])
            if any(len(action) > 300 for action in advice.actions):
                return {**fallback, 'trace': 'invalid_provider_output'}
    except httpx.HTTPError:
        return {**fallback, 'trace': 'provider_unavailable'}
    except (ValidationError, ValueError, KeyError, IndexError, TypeError):
        return {**fallback, 'trace': 'invalid_provider_output'}
    return {'source': 'llm', **advice.model_dump(), 'trace': 'structured_output_validated'}
