#!/bin/bash
# start.sh — launch the tool. Web UI by default; --cli for the terminal menu.
#   ./start.sh                 # web UI (prints its address; password robolegion)
#   ./start.sh --port 8080     # web UI on a specific port
#   ./start.sh --reset-password  # web password back to the default
#   ./start.sh --cli [args]    # old terminal menu (app.py)
cd "$(dirname "$0")"
if [ "${1:-}" = "--cli" ]; then
  shift
  exec python3 app.py "$@"
fi
exec python3 webapp.py "$@"
