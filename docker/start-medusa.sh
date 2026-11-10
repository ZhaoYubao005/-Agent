#!/bin/sh
set -eu

DATABASE_URL=$(node -p '"postgres://ecomops:" + encodeURIComponent(process.env.POSTGRES_PASSWORD) + "@postgres:5432/ecomops?sslmode=disable"')
export DATABASE_URL
unset POSTGRES_PASSWORD

cd /app/commerce/apps/backend/.medusa/server
medusa=/app/commerce/node_modules/.bin/medusa
"$medusa" db:migrate
"$medusa" db:migrate:search
"$medusa" exec ./src/scripts/ensure-demo-admin.js
"$medusa" exec ./src/scripts/seed-ecomops.js
if [ "${ECOMOPS_STOCK_CAS_MODE:-}" = controlled ]; then
    "$medusa" exec ./src/scripts/setup-ecomops-stock-cas.js
fi
exec "$medusa" start
