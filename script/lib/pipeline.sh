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

cleanup_legacy_warning_dirs() {
  local alerts_dir="$out/alerts"
  local legacy
  for legacy in memory_leak double_free use_after_free uninit_use buffer_overflow; do
    if [[ -d "$alerts_dir/$legacy" ]]; then
      echo "==> 删除不兼容的旧 Warning 目录: $alerts_dir/$legacy"
      rm -rf -- "$alerts_dir/$legacy"
    fi
  done
}

count_alert_files() {
  local alerts_dir="$out/alerts"
  python3 - "$alerts_dir" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
count = 0
if root.is_dir():
    count = sum(1 for _ in root.rglob("*.json"))
print(count)
PY
}

run_svf_phase() {
  local alert_count parent_tmp=""
  cleanup_legacy_warning_dirs
  alert_count="$(count_alert_files)"
  if [[ "${FORCE_SVF:-}" != "1" ]]; then
    if (( alert_count > 0 )); then
      echo "==> alerts/ 已有 ${alert_count} 个警报 JSON，跳过 SVF 分析（FORCE_SVF=1 可强制重跑）"
      return 0
    fi
  fi
  if (( alert_count > 0 )); then
    parent_tmp="$(mktemp -d "${TMPDIR:-/tmp}/semantic-alerts.XXXXXX")"
    mkdir -p "$parent_tmp/alerts"
    cp -a "$out/alerts/." "$parent_tmp/alerts/"
  fi
  cleanup_legacy_outputs
  ensure_saber_bof_env "$svf_root"
  export SABER_SOURCE_ROOT="$src"
  if ! run_checkers_for_bc "$bc" "$out" "$stem"; then
    if [[ -n "$parent_tmp" ]]; then
      cp -a "$parent_tmp/alerts/." "$out/alerts/"
      rm -rf -- "$parent_tmp"
    fi
    return 1
  fi
  if [[ -n "$parent_tmp" ]]; then
    if ! python3 "$PIPELINE_SCRIPT_DIR/reconcile_semantic_warnings.py" \
        --parent-alerts "$parent_tmp/alerts" \
        --current-alerts "$out/alerts" \
        --summary "$out/semantic-reconciliation.json"; then
      cp -a "$parent_tmp/alerts/." "$out/alerts/"
      rm -rf -- "$parent_tmp"
      return 1
    fi
    rm -rf -- "$parent_tmp"
  fi
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

count_export_graphs() {
  local export_dir="$out/active_learning/predict_dataset/raw/$stem"
  python3 - "$export_dir" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
count = 0
if root.is_dir():
    count = sum(1 for _ in root.glob("*.node.csv"))
print(count)
PY
}

run_active_learning_export_phase() {
  local export_dir="$out/active_learning/predict_dataset/raw/$stem"
  local graph_count
  if [[ "${FORCE_SVF:-}" != "1" ]]; then
    graph_count="$(count_export_graphs)"
    if (( graph_count > 0 )); then
      echo "==> predict_dataset 已有 ${graph_count} 个图，跳过 svf-al-export（FORCE_SVF=1 可强制重跑）"
      return 0
    fi
  fi
  mkdir -p "$out/active_learning"
  ensure_saber_bof_env "$svf_root"
  if ! command -v svf-al-export >/dev/null 2>&1; then
    echo "error: svf-al-export not found; rebuild SVFmemplus after adding ActiveLearning export tool" >&2
    return 1
  fi
  svf-al-export --output-dir "$export_dir" "$bc"
}

run_active_learning_ensure_alert_graphs_phase() {
  local dataset="$out/active_learning/predict_dataset"
  PYTHONPATH="$active_learning_root${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m cli ensure-alert-graphs \
      --alerts-dir "$out/alerts" \
      --dataset "$dataset" \
      --manifest-output "$out/active_learning/alert_graphs_manifest.json"
}

active_learning_resolve_model() {
  # Prefer explicit config path; otherwise reuse latest checkpoint from prior rounds.
  local configured="${active_learning_model_path:-}"
  local latest="${active_learning_checkpoint_dir:-$out/active_learning/checkpoints}/latest.pt"
  if [[ -n "$configured" && -f "$configured" ]]; then
    ACTIVE_LEARNING_RESOLVED_MODEL="$configured"
    return 0
  fi
  if [[ -f "$latest" ]]; then
    ACTIVE_LEARNING_RESOLVED_MODEL="$latest"
    return 0
  fi
  ACTIVE_LEARNING_RESOLVED_MODEL=""
}

run_active_learning_predict_phase() {
  local dataset="$out/active_learning/predict_dataset"
  local predictions="$out/active_learning/predictions.csv"
  local predict_args=(--dataset "$dataset" --output "$predictions")
  active_learning_resolve_model
  if [[ -n "${ACTIVE_LEARNING_RESOLVED_MODEL:-}" ]]; then
    echo "active learning predict: loading model $ACTIVE_LEARNING_RESOLVED_MODEL"
    predict_args+=(--model "$ACTIVE_LEARNING_RESOLVED_MODEL")
  else
    echo "active learning predict: no checkpoint configured/found; using random weights"
    predict_args+=(--random-weights)
  fi
  PYTHONPATH="$active_learning_root${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m cli predict "${predict_args[@]}"
}

run_active_learning_rank_phase() {
  PYTHONPATH="$active_learning_root${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m cli rank-alerts \
      --alerts-dir "$out/alerts" \
      --predictions "$out/active_learning/predictions.csv" \
      --output "$out/active_learning/ranking.jsonl" \
      --diagnostics-output "$out/active_learning/ranking_diagnostics.json"
}

run_active_learning_select_feedback_phase() {
  local feedback="$out/active_learning/feedback_alerts.txt"
  local prev="$out/active_learning/feedback_alerts.prev.txt"
  local select_args=(
    --ranking "$out/active_learning/ranking.jsonl"
    --output "$feedback"
    --top-k "${active_learning_feedback_top_k:-10}"
    --bottom-k "${active_learning_feedback_bottom_k:-10}"
    --random-k "${active_learning_feedback_random_k:-0}"
    --random-seed "${active_learning_feedback_random_seed:-42}"
    --random-strategy "${active_learning_feedback_random_strategy:-plain}"
    --manifest-output "$out/active_learning/feedback_selection.json"
  )
  if [[ "${active_learning_feedback_skip_classified:-0}" == "1" ]]; then
    select_args+=(--skip-classified)
  fi
  if [[ -f "$feedback" ]]; then
    cp -f -- "$feedback" "$prev"
  fi
  PYTHONPATH="$active_learning_root${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m cli select-feedback "${select_args[@]}"
  if [[ -f "$prev" && -f "$feedback" ]] && cmp -s -- "$prev" "$feedback"; then
    echo "warning: active learning feedback set identical to previous round (possible stall); continuing anyway" >&2
  fi
}

run_active_learning_collect_feedback_phase() {
  PYTHONPATH="$active_learning_root${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m cli collect-feedback \
      --alerts-dir "$out/alerts" \
      --output "$out/active_learning/labels.jsonl"
}

active_learning_round_artifact_dir() {
  local round_id="$1"
  printf '%s/active_learning/rounds/%s\n' "$out" "$round_id"
}

active_learning_archive_round_artifacts() {
  local round_id="$1"
  local dest
  dest="$(active_learning_round_artifact_dir "$round_id")"
  mkdir -p "$dest"
  local artifact
  for artifact in alert_graphs_manifest.json predictions.csv ranking.jsonl ranking_diagnostics.json feedback_alerts.txt feedback_selection.json labels.jsonl; do
    if [[ -f "$out/active_learning/$artifact" ]]; then
      cp -f -- "$out/active_learning/$artifact" "$dest/$artifact"
    fi
  done
  local ckpt_dir="${active_learning_checkpoint_dir:-$out/active_learning/checkpoints}"
  if [[ -f "$ckpt_dir/${round_id}.pt" ]]; then
    cp -f -- "$ckpt_dir/${round_id}.pt" "$dest/checkpoint.pt"
  fi
  local completed_at
  completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  cat >"$dest/manifest.json" <<EOF
{
  "round_id": "$round_id",
  "completed_at": "$completed_at",
  "artifacts": {
    "predictions": "predictions.csv",
    "alert_graphs": "alert_graphs_manifest.json",
    "ranking": "ranking.jsonl",
    "ranking_diagnostics": "ranking_diagnostics.json",
    "feedback_alerts": "feedback_alerts.txt",
    "feedback_selection": "feedback_selection.json",
    "labels": "labels.jsonl",
    "checkpoint": "checkpoint.pt"
  }
}
EOF
}

active_learning_append_round_summary() {
  local round_id="$1"
  local summary="$out/active_learning/rounds/summary.jsonl"
  local feedback_count label_count completed_at
  feedback_count=0
  if [[ -f "$out/active_learning/feedback_alerts.txt" ]]; then
    feedback_count="$(grep -cve '^[[:space:]]*$' "$out/active_learning/feedback_alerts.txt" || true)"
  fi
  label_count=0
  if [[ -f "$out/active_learning/labels.jsonl" ]]; then
    label_count="$(grep -cve '^[[:space:]]*$' "$out/active_learning/labels.jsonl" || true)"
  fi
  completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  mkdir -p "$(dirname "$summary")"
  printf '{"round_id":"%s","completed_at":"%s","feedback_count":%s,"label_count":%s}\n' \
    "$round_id" "$completed_at" "$feedback_count" "$label_count" >>"$summary"
}

active_learning_resolve_round_id() {
  local round_index="$1"
  local total_rounds="$2"
  local prefix="${ACTIVE_LEARNING_ROUND_PREFIX:-}"

  if [[ "$total_rounds" -le 1 ]]; then
    printf '%s\n' "${ROUND_ID:-round-$(date -u +%Y%m%dT%H%M%SZ)}"
    return 0
  fi
  if [[ -z "$prefix" ]]; then
    prefix="${ROUND_ID:-round-$(date -u +%Y%m%dT%H%M%SZ)}"
  fi
  printf '%s-%03d\n' "$prefix" "$round_index"
}

run_active_learning_feedback_round() {
  local round_id="$1"
  export ROUND_ID="$round_id"
  echo "=== active learning feedback round: $ROUND_ID ==="

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
  run_active_learning_ensure_alert_graphs_phase
  run_active_learning_collect_feedback_phase
  run_active_learning_train_phase

  active_learning_archive_round_artifacts "$ROUND_ID"
  active_learning_append_round_summary "$ROUND_ID"

  # After the first trained round, prefer checkpoints/latest.pt for later rounds.
  active_learning_model_path=""
  export active_learning_model_path
}

run_active_learning_train_phase() {
  local dataset="$out/active_learning/predict_dataset"
  local labels="$out/active_learning/labels.jsonl"
  local ckpt_dir="${active_learning_checkpoint_dir:-$out/active_learning/checkpoints}"
  local train_args=(
    --dataset "$dataset"
    --labels "$labels"
    --checkpoint-dir "$ckpt_dir"
    --epochs "${active_learning_train_epochs:-50}"
    --lr "${active_learning_train_lr:-0.001}"
    --weight-decay "${active_learning_train_weight_decay:-0.0005}"
    --batch-size "${active_learning_train_batch_size:-8}"
    --val-ratio "${active_learning_train_val_ratio:-0.2}"
    --patience "${active_learning_train_patience:-10}"
    --min-labels "${active_learning_train_min_labels:-2}"
    --uncertain-weight "${active_learning_uncertain_weight:-0.3}"
    --unlabeled-weight "${active_learning_unlabeled_weight:-0.1}"
    --weak-pos "${active_learning_weak_pos:-0.7}"
    --weak-neg "${active_learning_weak_neg:-0.3}"
    --max-unlabeled "${active_learning_max_unlabeled:-512}"
    --max-batch-nodes "${active_learning_train_max_batch_nodes:-50000}"
    --max-batch-edges "${active_learning_train_max_batch_edges:-80000}"
  )
  if [[ -n "${ROUND_ID:-}" ]]; then
    train_args+=(--round-id "$ROUND_ID")
  fi
  active_learning_resolve_model
  if [[ -n "${ACTIVE_LEARNING_RESOLVED_MODEL:-}" ]]; then
    train_args+=(--init-model "$ACTIVE_LEARNING_RESOLVED_MODEL")
  fi
  mkdir -p "$ckpt_dir"
  echo "active learning train: labels=$labels checkpoint_dir=$ckpt_dir"
  PYTHONPATH="$active_learning_root${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m cli train "${train_args[@]}"
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
        if not isinstance(document, dict):
            continue
        if document.get("suppressed") is True:
            continue
        history = document.get("classifications")
        latest = history[-1] if isinstance(history, list) and history else None
        label = latest.get("classification") if isinstance(latest, dict) else None
        if label is None:
            pending += 1
print(pending)
PY
}
