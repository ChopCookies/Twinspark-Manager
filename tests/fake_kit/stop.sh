#!/usr/bin/env bash
# A stand-in for a kit's stop.sh.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
echo "stop user=$(id -un) PORT=${PORT:-8888} CONTAINER_NAME=${CONTAINER_NAME:-fake-kit}" >> run.log
if [[ -f server.pid ]]; then
  kill "$(cat server.pid)" 2>/dev/null || true
  rm -f server.pid
fi
