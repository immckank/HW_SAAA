#!/usr/bin/env bash
# Saber/BOF 工具链环境：仅负责 source setup.sh / build.sh，与 graph-reader 无关。
# graph-reader 由 FPhandler CommandCaller 自行在 subprocess 内管理环境。

_saber_bof_ready() {
  command -v saber >/dev/null 2>&1 && command -v bof >/dev/null 2>&1
}

_saber_bof_collect_missing() {
  local -n _out=$1
  _out=()
  command -v saber >/dev/null 2>&1 || _out+=(saber)
  command -v bof >/dev/null 2>&1 || _out+=(bof)
}

ensure_saber_bof_env() {
  local svf_root=$1
  local missing=()
  local saved_script_dir="${SCRIPT_DIR-}"
  cd "$svf_root"
  # shellcheck disable=SC1091
  source ./setup.sh Release
  if _saber_bof_ready; then
    [[ -n "$saved_script_dir" ]] && SCRIPT_DIR="$saved_script_dir"
    return 0
  fi
  _saber_bof_collect_missing missing
  echo "==> SVFmemplus 未编译（缺少: ${missing[*]}），正在 source ./build.sh ..."
  local had_u=0
  [[ $- == *u* ]] && had_u=1
  set +u
  # shellcheck disable=SC1091
  source ./build.sh
  (( had_u )) && set -u
  [[ -n "$saved_script_dir" ]] && SCRIPT_DIR="$saved_script_dir"
  # shellcheck disable=SC1091
  source ./setup.sh Release
  if _saber_bof_ready; then
    [[ -n "$saved_script_dir" ]] && SCRIPT_DIR="$saved_script_dir"
    return 0
  fi
  missing=()
  _saber_bof_collect_missing missing
  [[ -n "$saved_script_dir" ]] && SCRIPT_DIR="$saved_script_dir"
  echo "error: build 完成后仍缺少: ${missing[*]}，请检查 $svf_root/Release-build/bin" >&2
  return 1
}
