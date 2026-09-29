#!/usr/bin/env bash
# Keep external networking disabled while permitting the fake loopback HTTP API.
set -euo pipefail
unshare --net -- bash -c '
  set -euo pipefail
  ip link set dev lo up
  exec "$@"
' bash "$@"
