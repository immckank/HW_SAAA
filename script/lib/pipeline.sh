#!/usr/bin/env bash
# 管线阶段函数（由 run_pipeline.sh source）。

# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/svf_env.sh"
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/run_checkers.sh"

cleanup_legacy_outputs() {
  shopt -s nullglob
  local legacy=(
    "$out"/*_report.json
    "$out"/*_report.md
    "$out"/*_slices.json
    "$out"/*.txt
  )
  if [[ ${#legacy[@]} -gt 0 ]]; then
    rm -f -- "${legacy[@]}"
  fi
  shopt -u nullglob
}

run_svf_phase() {
  cleanup_legacy_outputs
  ensure_saber_bof_env "$svf_root"
  export SABER_SOURCE_ROOT="$src"
  run_checkers_for_bc "$bc" "$out" "$stem"
  if pipeline_in_container && [[ -d "$out/alerts" && -n "${host_uid:-}" && -n "${host_gid:-}" ]]; then
    chown -R "${host_uid}:${host_gid}" "$out/alerts" 2>/dev/null || true
  fi
}

run_fph_phase() {
  local config_py="${PIPELINE_SCRIPT_DIR}/config.py"
  if pipeline_in_container; then
    config_py="/pipeline/config.py"
  fi
  cd "$fph_root"
  python3 run.py --config "$config_py" "${FPH_ARGS[@]}"
}

run_active_learning_export_phase() {
  mkdir -p "$out/active_learning"
  ensure_saber_bof_env "$svf_root"
  if ! command -v svf-al-export >/dev/null 2>&1; then
    echo "error: svf-al-export not found; rebuild SVFmemplus after adding ActiveLearning export tool" >&2
    return 1
  fi
  svf-al-export --output-dir "$out/active_learning/predict_dataset/raw/$stem" "$bc"
}

run_active_learning_predict_phase() {
  local dataset="$out/active_learning/predict_dataset"
  local predictions="$out/active_learning/predictions.csv"
  PYTHONPATH="$active_learning_root${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m cli predict \
      --dataset "$dataset" \
      --output "$predictions" \
      --random-weights
}

run_active_learning_rank_phase() {
  PYTHONPATH="$active_learning_root${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m cli rank-alerts \
      --alerts-dir "$out/alerts" \
      --predictions "$out/active_learning/predictions.csv" \
      --output "$out/active_learning/ranking.jsonl"
}

run_active_learning_select_feedback_phase() {
  PYTHONPATH="$active_learning_root${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m cli select-feedback \
      --ranking "$out/active_learning/ranking.jsonl" \
      --output "$out/active_learning/feedback_alerts.txt" \
      --per-category "${ACTIVE_LEARNING_FEEDBACK_PER_CATEGORY:-1}"
}

run_active_learning_collect_feedback_phase() {
  PYTHONPATH="$active_learning_root${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m cli collect-feedback \
      --alerts-dir "$out/alerts" \
      --output "$out/active_learning/labels.jsonl"
}

count_pending_alerts() {
  local alerts_dir="$out/alerts"
  python3 - "$alerts_dir" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
pending = 0
if root.is_dir():
    for path in root.rglob("*.json"):
        try:
            with path.open(encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(document, dict) and document.get("classification") is None:
            pending += 1
print(pending)
PY
}
