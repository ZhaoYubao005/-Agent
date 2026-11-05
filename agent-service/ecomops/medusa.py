"""Authenticated Medusa API adapter. Credentials never enter tool results."""
import os
import httpx


class Medusa:
    def __init__(self):
        self.url = os.environ.get('MEDUSA_URL', 'http://127.0.0.1:9000')

    async def request(self, method, path, **kwargs):
        async with httpx.AsyncClient(base_url=self.url, timeout=30, trust_env=False) as client:
            auth = await client.post('/auth/user/emailpass', json={
                'email': os.environ['MEDUSA_ADMIN_EMAIL'],
                'password': os.environ['MEDUSA_ADMIN_PASSWORD'],
            })
            auth.raise_for_status()
            response = await client.request(method, path, headers={
                'Authorization': f'Bearer {auth.json()["token"]}'}, **kwargs)
            response.raise_for_status()
            return response.json()

    async def list_all(self, resource, key):
        rows = []
        for offset in range(0, 10000, 100):
            page = await self.request('GET', f'/admin/{resource}',
                                      params={'limit': 100, 'offset': offset})
            rows.extend(page[key])
            if len(rows) >= page['count']:
                return rows
        raise RuntimeError('Snapshot exceeds phase-one 10000-record limit; refusing partial analysis')
