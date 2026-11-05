"""Load local credentials into a child process without shell interpolation."""
import json
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
c = json.loads((root / '.runtime/credentials.json').read_text())
env = dict(os.environ)
env.update({
    'MEDUSA_ADMIN_EMAIL': 'admin@ecomops.test',
    'MEDUSA_ADMIN_PASSWORD': c['admin_password'],
    'ECOMOPS_API_TOKEN': c['api_token'],
    'ECOMOPS_ALLOW_REPLENISHMENT': os.environ.get('ECOMOPS_ISOLATED_FIXTURES', '0'),
    'ECOMOPS_AUDIT_DB': str(root / '.runtime/audit.sqlite3'),
    'PYTHONPATH': str(root / 'agent-service'),
    'MEDUSA_TELEMETRY_DISABLED': '1',
})
raise SystemExit(subprocess.call(sys.argv[1:], env=env))
