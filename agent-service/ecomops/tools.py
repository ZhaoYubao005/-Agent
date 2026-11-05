"""MCP client: each connection performs the actual protocol handshake."""
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]


@asynccontextmanager
async def commerce_tools():
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT / 'agent-service')
    params = StdioServerParameters(command=sys.executable,
                                  args=[str(ROOT / 'mcp-server/server.py')], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def call(session, name, arguments=None):
    result = await session.call_tool(name, arguments or {})
    if result.is_error:
        # Avoid leaking upstream HTTP auth payloads or credentials.
        raise RuntimeError(f'MCP tool {name} failed')
    if result.structured_content is not None:
        data = result.structured_content
        return data['result'] if set(data) == {'result'} else data
    content = [json.loads(x.text) for x in result.content if x.type == 'text']
    return content[0] if len(content) == 1 else content
