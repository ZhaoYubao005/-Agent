#!/bin/bash

# The PostgreSQL image runs this on a fresh volume. On an existing volume,
# run it explicitly after configuring the dedicated Agent password.
if [ -n "${ECOMOPS_POSTGRES_PASSWORD:-}" ]; then
    psql --no-psqlrc --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
SELECT pg_advisory_lock(hashtext('ecomops_agent_database_setup'));
\getenv agent_password ECOMOPS_POSTGRES_PASSWORD

SELECT 'CREATE ROLE ecomops_agent'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ecomops_agent')
\gexec
ALTER ROLE ecomops_agent WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS PASSWORD :'agent_password';

SELECT 'CREATE DATABASE ecomops_agent OWNER ecomops_agent'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'ecomops_agent')
\gexec
REVOKE CONNECT ON DATABASE ecomops_agent FROM PUBLIC;
REVOKE CONNECT ON DATABASE ecomops FROM PUBLIC, ecomops_agent;
SQL
fi
