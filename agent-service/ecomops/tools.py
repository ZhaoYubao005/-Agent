"""MCP client: each connection performs the actual protocol handshake."""
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

class MCPUnavailable(RuntimeError):
    """Read-only commerce data could not be loaded within the request budget."""


ROOT = Path(__file__).resolve().parents[2]


@asynccontextmanager
async def commerce_tools():
    keys = {'MEDUSA_URL', 'MEDUSA_ADMIN_EMAIL', 'MEDUSA_ADMIN_PASSWORD'}
    if os.environ.get('ECOMOPS_MCP_REPORT_READ') == '1':
        keys.update({'ECOMOPS_MCP_REPORT_READ', 'ECOMOPS_AUDIT_DB', 'ECOMOPS_DATABASE_URL'})
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env['PYTHONPATH'] = str(ROOT / 'agent-service')
    params = StdioServerParameters(command=sys.executable,
                                  args=[str(ROOT / 'mcp-server/server.py')], env=env)
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                async with asyncio.timeout(15):
                    await session.initialize()
                yield session
    except Exception as error:
        cause = error
        while isinstance(cause, ExceptionGroup) and len(cause.exceptions) == 1:
            cause = cause.exceptions[0]
        if isinstance(cause, MCPUnavailable):
            raise cause from None
        raise MCPUnavailable('MCP session failed') from error


async def call(session, name, arguments=None):
    try:
        async with asyncio.timeout(90):
            result = await session.call_tool(name, arguments or {})
    except Exception as error:
        raise MCPUnavailable(f'MCP tool {name} failed') from error
    if result.is_error:
        # Avoid leaking upstream HTTP auth payloads or credentials.
        raise MCPUnavailable(f'MCP tool {name} failed')
    if result.structured_content is not None:
        data = result.structured_content
        return data['result'] if set(data) == {'result'} else data
    content = [json.loads(x.text) for x in result.content if x.type == 'text']
    return content[0] if len(content) == 1 else content
