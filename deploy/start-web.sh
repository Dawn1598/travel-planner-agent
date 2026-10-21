#!/bin/sh
set -eu
cd /opt/travel-planner/app
/opt/travel-planner/venv/bin/python manage.py recover_runs
exec /opt/travel-planner/venv/bin/gunicorn config.wsgi:application \
  --bind 127.0.0.1:18017 --workers 1 --worker-class gthread --threads 8 \
  --control-socket /opt/travel-planner/run/gunicorn.ctl \
  --timeout 600 --graceful-timeout 30 --access-logfile - --error-logfile -
