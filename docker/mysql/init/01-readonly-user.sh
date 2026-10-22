#!/bin/bash
# Runs once, when the bundled MySQL volume is first initialised.
# Creates the least-privilege account the app uses: SELECT on the app schema
# only (no writes, no FILE, no PROCESS, no EXECUTE).
set -euo pipefail

if [ -z "${MYSQL_READONLY_USER:-}" ] || [ -z "${MYSQL_READONLY_PASSWORD:-}" ]; then
    echo "MYSQL_READONLY_USER/MYSQL_READONLY_PASSWORD not set; skipping read-only user." >&2
    exit 0
fi

mysql --protocol=socket -uroot -p"${MYSQL_ROOT_PASSWORD}" <<SQL
CREATE USER IF NOT EXISTS '${MYSQL_READONLY_USER}'@'%' IDENTIFIED BY '${MYSQL_READONLY_PASSWORD}';
GRANT SELECT, SHOW VIEW ON \`${MYSQL_DATABASE}\`.* TO '${MYSQL_READONLY_USER}'@'%';
ALTER USER '${MYSQL_READONLY_USER}'@'%' WITH MAX_USER_CONNECTIONS 20;
FLUSH PRIVILEGES;
SQL
echo "Created read-only MySQL user '${MYSQL_READONLY_USER}'."
