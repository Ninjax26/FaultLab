#!/bin/sh
set -eu

alembic upgrade head

python -m faultlab.worker.main &
worker_pid=$!
api_pid=""

shutdown() {
    if [ -n "$api_pid" ]; then
        kill -TERM "$api_pid" 2>/dev/null || true
    fi
    kill -TERM "$worker_pid" 2>/dev/null || true
    if [ -n "$api_pid" ]; then
        wait "$api_pid" 2>/dev/null || true
    fi
    wait "$worker_pid" 2>/dev/null || true
}

trap shutdown INT TERM

uvicorn faultlab.api.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-10000}" &
api_pid=$!

set +e
wait "$api_pid"
exit_code=$?
set -e

shutdown
trap - INT TERM
exit "$exit_code"
