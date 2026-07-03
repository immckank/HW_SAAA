#!/usr/bin/env bash
# 完整管线：SVFmemplus → FPhandler
#
# 用法:
#   ./script/run_pipeline.sh
#   ./script/run_pipeline.sh --svf-only
#   ./script/run_pipeline.sh --fph-only
#   ./script/run_pipeline.sh --stats-only
#   ./script/run_pipeline.sh --force
#   ./script/run_svf.sh              # 等价于 --svf-only
#
# docker_name 非空时，SVF 与 FPhandler 均在同一容器内执行。

set -euo pipefail

PIPELINE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$PIPELINE_SCRIPT_DIR/lib/common.sh"
# shellcheck disable=SC1091
source "$PIPELINE_SCRIPT_DIR/lib/pipeline.sh"

RUN_SVF=1
RUN_FPH=1
FORCE_BUILD=""
EXTRA_CHECKERS=""
FPH_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --svf-only) RUN_FPH=0; shift ;;
    --fph-only) RUN_SVF=0; shift ;;
    --force) FORCE_BUILD=1; shift ;;
    --checkers)
      [[ $# -ge 2 ]] || { echo "error: --checkers 需要参数" >&2; exit 2; }
      EXTRA_CHECKERS=$2
      shift 2
      ;;
    --stats-only) FPH_ARGS+=(--stats-only); shift ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *)
      echo "error: 未知参数: $1" >&2
      exit 2
      ;;
  esac
done

if ! pipeline_in_container; then
  load_config
  if [[ -n "$EXTRA_CHECKERS" ]]; then
    parse_defect_types "$EXTRA_CHECKERS"
    export defect_types
  fi
  if [[ -n "$FORCE_BUILD" ]]; then
    export FORCE_BUILD=1
  fi
  print_config_summary
  if should_use_docker; then
    local docker_args=()
    [[ "$RUN_FPH" -eq 0 ]] && docker_args+=(--svf-only)
    [[ "$RUN_SVF" -eq 0 ]] && docker_args+=(--fph-only)
    [[ -n "$FORCE_BUILD" ]] && docker_args+=(--force)
    [[ -n "$EXTRA_CHECKERS" ]] && docker_args+=(--checkers "$EXTRA_CHECKERS")
    if [[ ${#FPH_ARGS[@]} -gt 0 ]]; then
      docker_args+=("${FPH_ARGS[@]}")
    fi
    exec_in_docker "${docker_args[@]}"
  fi
else
  load_config
  if [[ -n "$EXTRA_CHECKERS" ]]; then
    parse_defect_types "$EXTRA_CHECKERS"
    export defect_types
  fi
  if [[ -n "$FORCE_BUILD" ]]; then
    export FORCE_BUILD=1
  fi
fi

if [[ "$RUN_SVF" -eq 1 ]]; then
  run_svf_phase
  echo "SVF outputs: $out"
fi

if [[ "$RUN_FPH" -eq 1 ]]; then
  run_fph_phase
fi
