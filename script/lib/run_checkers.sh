#!/usr/bin/env bash
# 按 defect_types 调度 saber / bof（单 bc，单 out）。
# 依赖环境变量：defect_types 或 DEFECT_TYPES_ARR；可选 semantic_rules。

_ensure_defect_types_arr() {
  if [[ -n "${DEFECT_TYPES_ARR+set}" && ${#DEFECT_TYPES_ARR[@]} -gt 0 ]]; then
    return
  fi
  DEFECT_TYPES_ARR=()
  local token
  IFS=',' read -ra _dtokens <<< "${defect_types:-leak,dfree,uaf,uninit}"
  for token in "${_dtokens[@]}"; do
    token="${token// /}"
    [[ -n "$token" ]] && DEFECT_TYPES_ARR+=("$token")
  done
}

run_one_checker() {
  local bc_path=$1 out_dir=$2 stem=$3 checker=$4
  local semantic_args=()

  if [[ -n "${semantic_rules:-}" && -f "$semantic_rules" ]]; then
    semantic_args=(-saber-semantic-rules="$semantic_rules")
  fi

  case "$checker" in
    leak)
      echo "==> saber -leak $bc_path"
      saber -leak "${semantic_args[@]}" -report-dir="$out_dir" "$bc_path" \
        2>&1
      ;;
    dfree)
      echo "==> saber -dfree $bc_path"
      saber -dfree "${semantic_args[@]}" -report-dir="$out_dir" "$bc_path" \
        2>&1
      ;;
    uaf)
      echo "==> saber -uaf $bc_path"
      saber -uaf "${semantic_args[@]}" -report-dir="$out_dir" "$bc_path" \
        2>&1
      ;;
    uninit)
      echo "==> saber -uninit $bc_path"
      saber -uninit "${semantic_args[@]}" -report-dir="$out_dir" "$bc_path" \
        2>&1
      ;;
    bof)
      echo "==> bof $bc_path"
      bof -report-dir="$out_dir" "$bc_path" 2>&1
      ;;
    *)
      echo "error: 未知 defect_type: $checker" >&2
      return 1
      ;;
  esac
}

run_checkers_for_bc() {
  local bc_path=$1 out_dir=$2 stem=$3
  local checker
  local -a failed=()

  _ensure_defect_types_arr

  for checker in "${DEFECT_TYPES_ARR[@]}"; do
    set +e
    run_one_checker "$bc_path" "$out_dir" "$stem" "$checker"
    local rc=$?
    set -e
    if [[ "$rc" -ne 0 ]]; then
      echo "error: checker $checker 失败 (exit $rc)" >&2
      failed+=("$checker")
    fi
  done

  if [[ ${#failed[@]} -gt 0 ]]; then
    echo "error: 以下 checker 未成功: ${failed[*]}" >&2
    return 1
  fi
}
