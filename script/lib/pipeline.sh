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
