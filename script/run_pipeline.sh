#!/usr/bin/env bash
# Compatibility wrapper around the Python orchestrator.

set -euo pipefail

PIPELINE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$PIPELINE_SCRIPT_DIR/lib/common.sh"
# shellcheck disable=SC1091
source "$PIPELINE_SCRIPT_DIR/lib/orchestrator_compat.sh"

RUN_SVF=1
RUN_FPH=1
NEW_BASELINE=0
STATS_ONLY=0
CHECKERS=""
TRIAGE_MODE=classify
ORIGINAL_ARGS=("$@")

while [[ $# -gt 0 ]]; do
  case "$1" in
    --svf-only) RUN_FPH=0; shift ;;
    --fph-only) RUN_SVF=0; shift ;;
    --stats-only) STATS_ONLY=1; RUN_SVF=0; RUN_FPH=0; shift ;;
    --new-baseline) NEW_BASELINE=1; shift ;;
    --force|--force-svf) shift ;; # analyze is never skipped by the orchestrator
    --expand-semantics) TRIAGE_MODE=expand-semantics; shift ;;
    --checkers)
      [[ $# -ge 2 ]] || { echo "error: --checkers 需要参数" >&2; exit 2; }
      CHECKERS=$2
      shift 2
      ;;
    -h|--help)
      sed -n '1,26p' "$0"
      exit 0
      ;;
    *) echo "error: 未知参数: $1" >&2; exit 2 ;;
  esac
done

if ! pipeline_in_container; then
  load_config
  if should_use_docker; then
    exec_in_docker "${ORIGINAL_ARGS[@]}"
  fi
else
  load_config
fi

if [[ "$STATS_ONLY" -eq 1 ]]; then
  exec "$PIPELINE_SCRIPT_DIR/stats_alerts.sh" "$out"
fi

prepare_workflow_config
trap cleanup_workflow_config EXIT

if [[ "$RUN_SVF" -eq 1 ]]; then
  analyze_args=(analyze)
  [[ -n "$CHECKERS" ]] && analyze_args+=(--checkers "$CHECKERS")
  [[ "$NEW_BASELINE" -eq 1 ]] && analyze_args+=(--new-baseline)
  run_orchestrator "${analyze_args[@]}"
fi

if [[ "$RUN_FPH" -eq 1 ]]; then
  pending="$(mktemp "${TMPDIR:-/tmp}/pending-alerts.XXXXXX")"
  write_pending_alert_ids "$pending"
  if [[ -s "$pending" ]]; then
    run_orchestrator triage --mode "$TRIAGE_MODE" --alerts-file "$pending"
  else
    echo "FPhandler: no pending active alerts"
  fi
  rm -f -- "$pending"
fi
