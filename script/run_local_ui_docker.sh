#!/usr/bin/env bash
# 兼容入口：等同于 run_local_ui.sh start-server docker
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/run_local_ui.sh" start-server docker "$@"
