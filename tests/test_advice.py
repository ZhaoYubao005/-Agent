"""Exercise both provider wire formats and fail-safe advice paths."""
import json

import httpx
import pytest

from ecomops import advice


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['openai', 'ollama'])
async def test_provider_sends_aggregate_data_and_validates_schema(monkeypatch, protocol):
    async_client = httpx.AsyncClient

    def handler(request):
        payload = json.loads(request.content)
        assert 'customer_name' not in payload['messages'][1]['content']
        if protocol == 'ollama':
            assert request.url.path == '/api/chat'
            assert payload['format']['additionalProperties'] is False
        else:
            assert request.url.path == '/v1/chat/completions'
        content = json.dumps({'summary': 'Two versus one order', 'actions': ['Review alerts']})
        message = {'content': content}
        return httpx.Response(200, json={'message': message, 'choices': [{'message': message}]})

    monkeypatch.setenv('ECOMOPS_LLM_URL', 'http://localhost' + ('/v1' if protocol == 'openai' else ''))
    monkeypatch.setenv('ECOMOPS_LLM_MODEL', 'fixture-model')
    monkeypatch.setenv('ECOMOPS_LLM_PROTOCOL', protocol)
    monkeypatch.setattr(advice.httpx, 'AsyncClient',
                        lambda **kwargs: async_client(transport=httpx.MockTransport(handler), **kwargs))
    result = await advice.interpret_report(
        {'orders': 2, 'revenue': None, 'customer_name': 'synthetic customer'}, {'orders': 1},
    )
    assert result['source'] == 'llm'


@pytest.mark.asyncio
@pytest.mark.parametrize(('reply', 'trace'), [
    (httpx.Response(503), 'provider_unavailable'),
    (httpx.Response(200, json={'choices': [{'message': {'content': '{"summary": "approve", "write": true}'}}]}),
     'invalid_provider_output'),
])
async def test_unusable_provider_response_falls_back(monkeypatch, reply, trace):
    async_client = httpx.AsyncClient
    monkeypatch.setenv('ECOMOPS_LLM_URL', 'http://localhost/v1')
    monkeypatch.setenv('ECOMOPS_LLM_MODEL', 'fixture-model')
    monkeypatch.setenv('ECOMOPS_LLM_PROTOCOL', 'openai')
    monkeypatch.setattr(advice.httpx, 'AsyncClient',
                        lambda **kwargs: async_client(transport=httpx.MockTransport(lambda _: reply), **kwargs))
    result = await advice.interpret_report({'orders': 2, 'revenue': None}, {'orders': 1})
    assert result['source'] == 'rules'
    assert result['trace'] == trace
