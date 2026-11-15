#!/bin/bash
# Runs the website and the background worker side by side. If either one stops, both stop,
# so the host restarts the container instead of leaving a half-working service.
set -u
mkdir -p "$(dirname "$MARGIN_DB")" "$MARGIN_BACKUP_DIR"

# Refuse to start with placeholder settings (prints exactly what is missing).
python - <<'PY' || exit 1
from margin.config import Config
p = Config.from_env().problems()
if p:
    print("Not ready to start:\n- " + "\n- ".join(p)); raise SystemExit(1)
PY

python -m margin init-db
python -m margin worker &
WORKER=$!
# One web process with several threads: the rate limiter and SQLite both assume a single process.
gunicorn --bind "0.0.0.0:${PORT:-8080}" --workers 1 --threads 8 --timeout 60 --access-logfile - "margin.api:create_app()" &
WEB=$!

trap 'kill $WORKER $WEB 2>/dev/null; wait; exit 0' TERM INT
wait -n
kill $WORKER $WEB 2>/dev/null
wait
exit 1
