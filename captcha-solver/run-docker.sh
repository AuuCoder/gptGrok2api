#!/bin/sh
set -eu

python -c 'import cloakbrowser; cloakbrowser.ensure_binary()'
Xvfb :99 -screen 0 1920x1080x24 -ac -nolisten tcp &
export DISPLAY=:99

exec python -m uvicorn server:app --host 0.0.0.0 --port 8877
