"""Medusa read retries must never extend to receipt writes."""
import httpx
import pytest

from ecomops import medusa


@pytest.mark.asyncio
@pytest.mark.parametrize(('method', 'expected'), [('GET', 2), ('POST', 1)])
async def test_medusa_retries_only_transient_reads(monkeypatch, method, expected):
    seen = []
    async_client = httpx.AsyncClient

    def upstream(request):
        if request.url.path == '/auth/user/emailpass':
            return httpx.Response(200, json={'token': 'fixture'})
        seen.append(request.method)
        if len(seen) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={'result': []})

    monkeypatch.setenv('MEDUSA_ADMIN_EMAIL', 'admin@example.test')
    monkeypatch.setenv('MEDUSA_ADMIN_PASSWORD', 'fixture')
    monkeypatch.setattr(medusa.httpx, 'AsyncClient',
                        lambda **kwargs: async_client(transport=httpx.MockTransport(upstream), **kwargs))
    if method == 'POST':
        with pytest.raises(httpx.HTTPStatusError):
            await medusa.Medusa().request(method, '/admin/inventory-items/fixture')
    else:
        assert await medusa.Medusa().request(method, '/admin/inventory-items') == {'result': []}
    assert seen == [method] * expected
