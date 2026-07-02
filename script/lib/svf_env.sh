#!/usr/bin/env bash
# 进入 SVFmemplus 目录，确保已编译并 export PATH（供 native / docker 共用）。

_svf_tools_ready() {
  command -v saber >/dev/null 2>&1 && command -v graph-reader >/dev/null 2>&1
}

_svf_collect_missing() {
  local -n _out=$1
  _out=()
  command -v saber >/dev/null 2>&1 || _out+=(saber)
  command -v graph-reader >/dev/null 2>&1 || _out+=(graph-reader)
}

ensure_svf_env() {
  local svf_root=$1
  local missing=()
  # build.sh 会重写 SCRIPT_DIR；保留调用方脚本目录，避免后续路径拼接错误
  local saved_script_dir="${SCRIPT_DIR-}"
  cd "$svf_root"
  # shellcheck disable=SC1091
  source ./setup.sh Release
  if _svf_tools_ready; then
    [[ -n "$saved_script_dir" ]] && SCRIPT_DIR="$saved_script_dir"
    return 0
  fi
  _svf_collect_missing missing
  echo "==> SVFmemplus 未编译（缺少: ${missing[*]}），正在 source ./build.sh ..."
  local had_u=0
  [[ $- == *u* ]] && had_u=1
  set +u
  # shellcheck disable=SC1091
  source ./build.sh
  (( had_u )) && set -u
  [[ -n "$saved_script_dir" ]] && SCRIPT_DIR="$saved_script_dir"
  # build.sh 末尾会 source setup.sh；再确认一次
  # shellcheck disable=SC1091
  source ./setup.sh Release
  if _svf_tools_ready; then
    [[ -n "$saved_script_dir" ]] && SCRIPT_DIR="$saved_script_dir"
    return 0
  fi
  missing=()
  _svf_collect_missing missing
  [[ -n "$saved_script_dir" ]] && SCRIPT_DIR="$saved_script_dir"
  echo "error: build 完成后仍缺少: ${missing[*]}，请检查 $svf_root/Release-build/bin" >&2
  return 1
}
