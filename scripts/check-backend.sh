#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# This named test database is separate from application data and is cleared by pytest.
docker compose up -d db
docker compose exec -T db psql -U cloudops -d postgres <<'SQL'
SELECT 'CREATE DATABASE cloudops_test' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'cloudops_test')\gexec
SQL
docker compose run --build --rm test
