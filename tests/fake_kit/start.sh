#!/usr/bin/env bash
# A stand-in for a kit's start.sh: it reads HOST, PORT and CONTAINER_NAME as Mia's AI Lab's kits do, starts the
# server in the background, waits until it answers, and exits 0. FAKE_KIT_FAIL=1 makes it fail like a kit whose
# own checks refuse to start.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
HOST="${HOST:-0.0.0.0}"; PORT="${PORT:-8888}"; CONTAINER_NAME="${CONTAINER_NAME:-fake-kit}"
echo "start user=$(id -un) HOST=$HOST PORT=$PORT CONTAINER_NAME=$CONTAINER_NAME hf=$(command -v hf || echo missing) args=$*" >> run.log
if [[ "${FAKE_KIT_FAIL:-0}" == 1 ]]; then
  echo "[fake kit] refusing to start (FAKE_KIT_FAIL=1)" >&2
  exit 3
fi
nohup python3 server.py "$HOST" "$PORT" > server.log 2>&1 &
echo $! > server.pid
for _ in $(seq 1 100); do
  if python3 -c "import sys, urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/health', timeout=1)" 2>/dev/null; then
    echo "[fake kit] serving on $HOST:$PORT"
    exit 0
  fi
  sleep 0.1
done
echo "[fake kit] the server did not answer" >&2
exit 1
