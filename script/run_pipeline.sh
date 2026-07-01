#!/usr/bin/env bash
# 完整管线：SVFmemplus → FPhandler
#
# 用法:
#   ./script/run_pipeline.sh
#   ./script/run_pipeline.sh --svf-only
#   ./script/run_pipeline.sh --fph-only
#   ./script/run_pipeline.sh --stats-only

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"

RUN_SVF=1
RUN_FPH=1
EXTRA_SVF=()
FPH_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --svf-only) RUN_FPH=0; shift ;;
    --fph-only) RUN_SVF=0; shift ;;
    --native) EXTRA_SVF+=(--native); shift ;;
    --checkers)
      [[ $# -ge 2 ]] || { echo "error: --checkers 需要参数" >&2; exit 2; }
      EXTRA_SVF+=(--checkers "$2")
      shift 2
      ;;
    --stats-only) FPH_ARGS+=(--stats-only); shift ;;
    -h|--help)
      sed -n '2,9p' "$0"
      exit 0
      ;;
    *)
      echo "error: 未知参数: $1" >&2
      exit 2
      ;;
  esac
done

load_config
print_config_summary

if [[ "$RUN_SVF" -eq 1 ]]; then
  "$SCRIPT_DIR/run_svf.sh" "${EXTRA_SVF[@]}"
fi

if [[ "$RUN_FPH" -eq 1 ]]; then
  cd "$fph_root"
  python3 run.py --config "$SCRIPT_DIR/config.py" "${FPH_ARGS[@]}"
fi
