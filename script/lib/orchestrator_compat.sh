#!/usr/bin/env bash
# Legacy config.env to strict workflow.ini compatibility helpers.

orchestrator_root() {
  if [[ -n "${ORCHESTRATOR_ROOT:-}" ]]; then
    printf '%s\n' "$ORCHESTRATOR_ROOT"
  else
    cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd
  fi
}

prepare_workflow_config() {
  if [[ -n "${WORKFLOW_CONFIG:-}" ]]; then
    [[ -f "$WORKFLOW_CONFIG" ]] || {
      echo "error: WORKFLOW_CONFIG 不存在: $WORKFLOW_CONFIG" >&2
      return 1
    }
    ORCHESTRATOR_CONFIG="$WORKFLOW_CONFIG"
    ORCHESTRATOR_COMPAT_TMP=""
    return 0
  fi

  ORCHESTRATOR_COMPAT_TMP="$(mktemp -d "${TMPDIR:-/tmp}/workflow-config.XXXXXX")"
  ORCHESTRATOR_CONFIG="$ORCHESTRATOR_COMPAT_TMP/workflow.ini"
  {
    printf '[project]\n'
    printf 'bitcode_path = %s\n' "$bc"
    printf 'source_dir = %s\n' "$src"
    printf 'artifact_dir = %s\n' "$out"
  } >"$ORCHESTRATOR_CONFIG"
}

cleanup_workflow_config() {
  if [[ -n "${ORCHESTRATOR_COMPAT_TMP:-}" && -d "$ORCHESTRATOR_COMPAT_TMP" ]]; then
    rm -rf -- "$ORCHESTRATOR_COMPAT_TMP"
  fi
}

run_orchestrator() {
  local root
  root="$(orchestrator_root)"
  PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m orchestrator --config "$ORCHESTRATOR_CONFIG" "$@"
}

write_pending_alert_ids() {
  local destination=$1
  python3 - "$out/alerts" "$destination" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
ids = []
for path in sorted(root.rglob("*.json")):
    with path.open(encoding="utf-8") as stream:
        warning = json.load(stream)
    if warning.get("suppressed") is True:
        continue
    history = warning.get("classifications")
    if not isinstance(history, list) or not history:
        ids.append(str(warning["alert_id"]))
destination.write_text("\n".join(ids) + ("\n" if ids else ""), encoding="utf-8")
PY
}
