#!/bin/sh
# Container entrypoint: prepare runtime state, then run the given command.
set -e

mkdir -p "${APP_VAR_DIR:-/app/var}"

if [ "${DJANGO_MIGRATE_ON_START:-true}" = "true" ]; then
    python manage.py migrate --noinput
fi

exec "$@"
