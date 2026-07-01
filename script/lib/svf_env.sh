#!/usr/bin/env bash
# 进入 SVFmemplus 目录，确保已编译并 export PATH（供 native / docker 共用）。

ensure_svf_env() {
  local svf_root=$1
  cd "$svf_root"
  # shellcheck disable=SC1091
  source ./setup.sh Release
  if command -v saber >/dev/null 2>&1; then
    return 0
  fi
  echo "==> SVFmemplus 未编译（找不到 saber），正在 source ./build.sh ..."
  local had_u=0
  [[ $- == *u* ]] && had_u=1
  set +u
  # shellcheck disable=SC1091
  source ./build.sh
  (( had_u )) && set -u
  # build.sh 末尾会 source setup.sh；再确认一次
  if ! command -v saber >/dev/null 2>&1; then
    echo "error: build 完成后仍找不到 saber，请检查 $svf_root/Release-build/bin" >&2
    return 1
  fi
}
