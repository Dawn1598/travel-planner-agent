#!/bin/sh
# First installation only. Uploaded archives live in the private incoming folder.
set -eu
root=/opt/travel-planner
if [ -e "$root/app/manage.py" ]; then
    echo 'An installation already exists; back it up before updating.' >&2
    exit 1
fi
umask 022
mkdir -p "$root/app" "$root/shared" "$root/shared/runtime" "$root/run" "$root/logs"
chmod 755 "$root" "$root/app"
tar -xzf "$root/incoming/code.tar.gz" -C "$root/app"
tar -xzf "$root/incoming/private.tar.gz" -C "$root/shared"
chmod 600 "$root/shared/.env" "$root/shared/db.sqlite3"
if [ -f "$root/shared/runtime/xhs-state.json" ]; then
    chmod 600 "$root/shared/runtime/xhs-state.json"
fi
ln -s "$root/shared/.env" "$root/app/.env"
ln -s "$root/shared/db.sqlite3" "$root/app/db.sqlite3"
ln -s "$root/shared/runtime" "$root/app/.runtime"
chmod 755 "$root/app/deploy/start-web.sh" "$root/app/deploy/travelctl"
base_python=$(command -v "${PYTHON_BIN:-python3}")
"$base_python" -m venv "$root/venv"
"$root/venv/bin/python" -m pip install --disable-pip-version-check --no-cache-dir -r "$root/app/requirements.txt" -r "$root/app/deploy/requirements.txt"
cd "$root/app"
"$root/venv/bin/python" manage.py migrate --noinput
"$root/venv/bin/python" manage.py collectstatic --noinput
"$root/venv/bin/python" manage.py check
"$root/venv/bin/python" -m pip check
"$root/venv/bin/python" -m pip freeze > requirements.server.lock.txt
sh "$root/app/deploy/install-browser.sh"
/usr/sbin/nginx -t -c "$root/app/deploy/nginx.conf"
ln -s "$root/app/deploy/travelctl" /usr/local/bin/travelctl
echo 'Installation ready. Start with travelctl start.'
