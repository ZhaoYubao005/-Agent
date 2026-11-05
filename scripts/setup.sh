#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/init-local.py
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
(cd commerce && npm ci)
if ! docker container inspect ecomops-postgres >/dev/null 2>&1; then
  docker run -d --name ecomops-postgres --env-file .runtime/postgres.env \
    -p 127.0.0.1:5432:5432 -v ecomops-postgres:/var/lib/postgresql/data \
    --restart unless-stopped \
    postgres:17-bookworm@sha256:839fbb5a4942a7758dd93e18b42ea5649807491a0545b5eeeb8f0b8bb11857a5
else
  docker start ecomops-postgres >/dev/null
fi
for attempt in {1..30}; do
  if docker exec ecomops-postgres pg_isready -U ecomops -d ecomops >/dev/null; then break; fi
  sleep 1
done
docker exec ecomops-postgres pg_isready -U ecomops -d ecomops
root_dir="$PWD"
(
  cd commerce/apps/backend
  export MEDUSA_TELEMETRY_DISABLED=1
  npx medusa db:migrate
  npx medusa db:migrate:search
  python3 "$root_dir/scripts/run-with-env.py" npx medusa exec ./src/scripts/ensure-demo-admin.ts
  npx medusa exec ./src/scripts/seed-ecomops.ts
  npm run build
)
