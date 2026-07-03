#!/usr/bin/env bash
# Saber/BOF 工具链环境：source setup.sh / build.sh；与 graph-reader 无关。
# graph-reader 由 FPhandler CommandCaller 在 subprocess 内 source setup.sh 启动。

_svf_tools_ready() {
  command -v saber >/dev/null 2>&1 \
    && command -v bof >/dev/null 2>&1 \
    && command -v graph-reader >/dev/null 2>&1
}

_svf_tools_collect_missing() {
  local -n _out=$1
  _out=()
  command -v saber >/dev/null 2>&1 || _out+=(saber)
  command -v bof >/dev/null 2>&1 || _out+=(bof)
  command -v graph-reader >/dev/null 2>&1 || _out+=(graph-reader)
}

ensure_saber_bof_env() {
  local svf_root=$1
  local missing=()
  local saved_script_dir="${SCRIPT_DIR-}"
  cd "$svf_root"

  if [[ "${FORCE_BUILD:-}" == 1 ]]; then
    echo "==> --force: 正在 source ./build.sh ..."
    local had_u=0
    [[ $- == *u* ]] && had_u=1
    set +u
    # shellcheck disable=SC1091
    source ./build.sh
    (( had_u )) && set -u
    # shellcheck disable=SC1091
    source ./setup.sh Release
  else
    # shellcheck disable=SC1091
    source ./setup.sh Release
    if _svf_tools_ready; then
      [[ -n "$saved_script_dir" ]] && SCRIPT_DIR="$saved_script_dir"
      return 0
    fi
    _svf_tools_collect_missing missing
    echo "==> SVFmemplus 未编译（缺少: ${missing[*]}），正在 source ./build.sh ..."
    had_u=0
    [[ $- == *u* ]] && had_u=1
    set +u
    # shellcheck disable=SC1091
    source ./build.sh
    (( had_u )) && set -u
    # shellcheck disable=SC1091
    source ./setup.sh Release
  fi

  [[ -n "$saved_script_dir" ]] && SCRIPT_DIR="$saved_script_dir"

  if _svf_tools_ready; then
    return 0
  fi

  missing=()
  _svf_tools_collect_missing missing
  echo "error: build 完成后仍缺少: ${missing[*]}，请检查 $svf_root/Release-build/bin" >&2
  return 1
}
