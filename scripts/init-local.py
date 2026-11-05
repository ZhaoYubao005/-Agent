"""Generate local-only development credentials; never overwrite existing files."""
import json
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
runtime = root / '.runtime'
runtime.mkdir(mode=0o700, exist_ok=True)
config_path = runtime / 'credentials.json'
if not config_path.exists():
    config_path.write_text(json.dumps({k: secrets.token_hex(24) for k in
                                     ['db_password', 'jwt_secret', 'cookie_secret', 'admin_password', 'api_token']}))
    config_path.chmod(0o600)
c = json.loads(config_path.read_text())
if 'api_token' not in c:
    c['api_token'] = secrets.token_hex(24)
    config_path.write_text(json.dumps(c))
files = {
    runtime / 'postgres.env': f'POSTGRES_USER=ecomops\nPOSTGRES_DB=ecomops\nPOSTGRES_PASSWORD={c["db_password"]}\n',
    root / 'commerce/apps/backend/.env': (
        f'DATABASE_URL=postgres://ecomops:{c["db_password"]}@127.0.0.1:5432/ecomops\n'
        f'JWT_SECRET={c["jwt_secret"]}\nCOOKIE_SECRET={c["cookie_secret"]}\n'
        'STORE_CORS=http://localhost:8000\nADMIN_CORS=http://localhost:9000\n'
        'AUTH_CORS=http://localhost:9000\n'
    ),
}
for path, content in files.items():
    if not path.exists():
        path.write_text(content)
        path.chmod(0o600)
print('Local credentials prepared (values withheld).')
