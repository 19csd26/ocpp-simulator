#!/usr/bin/env bash
# Run the simulator on this machine and share it through a free tunnel, protected by a token.
#   scripts/share.sh                 # Cloudflare quick tunnel (needs `cloudflared`), else ngrok
#   SIM_TOKEN=mysecret scripts/share.sh
#   TUNNEL=ngrok PORT=8100 scripts/share.sh
# The simulator keeps running on your machine, so a CSMS on localhost stays reachable from it.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8100}"
export SIM_TOKEN="${SIM_TOKEN:-$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-32)}"
TUNNEL="${TUNNEL:-$(command -v cloudflared >/dev/null && echo cloudflared || echo ngrok)}"
command -v "$TUNNEL" >/dev/null || { echo "Install a tunnel first: brew install cloudflared  (or ngrok)"; exit 1; }
[ -d ui/dist ] || { echo "Building the UI..."; (cd ui && npm install --silent && npm run build --silent); }

PY=backend/.venv/bin/python; [ -x "$PY" ] || PY=python3
(cd backend && exec "../$PY" -m ocpp_simulator --port "$PORT") 2>/dev/null &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT
sleep 2

echo
echo "  Access token (people need it to log in):  $SIM_TOKEN"
echo "  Local:                                    http://127.0.0.1:$PORT"
echo "  The public URL is printed by $TUNNEL below. Press Ctrl+C to stop sharing."
echo
case "$TUNNEL" in
  cloudflared) cloudflared tunnel --url "http://127.0.0.1:$PORT" ;;
  ngrok)       ngrok http "$PORT" ;;
  *)           echo "Unknown TUNNEL=$TUNNEL (use cloudflared or ngrok)"; exit 1 ;;
esac
