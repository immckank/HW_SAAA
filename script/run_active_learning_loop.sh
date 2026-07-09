#!/usr/bin/env bash
# One active-learning feedback loop:
#   SVFmemplus -> graph export -> random/model prediction -> ranking -> FPhandler feedback.

set -euo pipefail

PIPELINE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$PIPELINE_SCRIPT_DIR/lib/common.sh"
# shellcheck disable=SC1091
source "$PIPELINE_SCRIPT_DIR/lib/pipeline.sh"

ROUND_ID="${ROUND_ID:-round-$(date -u +%Y%m%dT%H%M%SZ)}"

if ! pipeline_in_container; then
  load_config
  print_config_summary
  if should_use_docker; then
    pipeline_docker_vols
    pipeline_docker_env
    exec docker run --rm \
      "${PIPELINE_DOCKER_VOLS[@]}" \
      "${PIPELINE_DOCKER_ENV[@]}" \
      -w /SVFmemplus \
      "$docker_name" \
      bash /pipeline/run_active_learning_loop.sh
  fi
else
  load_config
fi

run_svf_phase
run_active_learning_export_phase
run_active_learning_predict_phase
run_active_learning_rank_phase
run_active_learning_select_feedback_phase

FPH_ARGS=(
  --alert-list "$out/active_learning/feedback_alerts.txt"
  --force-reclassify
  --round-id "$ROUND_ID"
  --classification-source "active-learning-fphandler"
)
run_fph_phase
run_active_learning_collect_feedback_phase

echo "active learning round completed: $ROUND_ID"
echo "ranking: $out/active_learning/ranking.jsonl"
echo "feedback labels: $out/active_learning/labels.jsonl"
