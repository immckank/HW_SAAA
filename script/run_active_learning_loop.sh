#!/usr/bin/env bash
# Compatibility wrapper for orchestrator active-learning.

set -euo pipefail

PIPELINE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$PIPELINE_SCRIPT_DIR/lib/common.sh"
# shellcheck disable=SC1091
source "$PIPELINE_SCRIPT_DIR/lib/orchestrator_compat.sh"

ROUNDS=""
FEEDBACK=fphandler
INITIAL_MODEL=""
NEW_BASELINE=0
ORIGINAL_ARGS=("$@")
while [[ $# -gt 0 ]]; do
  case "$1" in
    --rounds) ROUNDS=$2; shift 2 ;;
    --feedback) FEEDBACK=$2; shift 2 ;;
    --initial-model) INITIAL_MODEL=$2; shift 2 ;;
    --new-baseline|--force-svf) NEW_BASELINE=1; shift ;;
    -h|--help)
      sed -n '1,18p' "$0"
      exit 0
      ;;
    *) echo "error: 未知参数: $1" >&2; exit 2 ;;
  esac
done

if ! pipeline_in_container; then
  load_config
  if should_use_docker; then
    pipeline_docker_vols
    pipeline_docker_env
    exec docker run --rm \
      "${PIPELINE_DOCKER_VOLS[@]}" \
      "${PIPELINE_DOCKER_ENV[@]}" \
      -w /SVFmemplus \
      "$docker_name" \
      bash /pipeline/run_active_learning_loop.sh "${ORIGINAL_ARGS[@]}"
  fi
else
  load_config
fi

ROUNDS="${ROUNDS:-${active_learning_rounds:-1}}"
if [[ -z "$INITIAL_MODEL" ]]; then
  INITIAL_MODEL="${active_learning_model_path:-random}"
  [[ -n "$INITIAL_MODEL" ]] || INITIAL_MODEL=random
fi

prepare_workflow_config
trap cleanup_workflow_config EXIT
analyze_args=(analyze)
[[ "$NEW_BASELINE" -eq 1 ]] && analyze_args+=(--new-baseline)
run_orchestrator "${analyze_args[@]}"
run_orchestrator active-learning \
  --rounds "$ROUNDS" \
  --feedback "$FEEDBACK" \
  --initial-model "$INITIAL_MODEL"
