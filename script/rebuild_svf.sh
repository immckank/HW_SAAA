#!/usr/bin/env bash
# 在 Docker 镜像内强制全量重新编译 SVFmemplus（始终 source ./build.sh，不跳过）。
#
# 用法:
#   ./script/rebuild_svf.sh
#   ./script/rebuild_svf.sh debug
#   ./script/rebuild_svf.sh debug sta_lib
#
# build.sh 参数原样传入容器（如 debug、sta_lib、dyn_lib、nortti）。
# 读取 script/config.env 中的 svf_root、docker_name；不依赖 bc/out/src。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"

load_svf_build_config() {
  local cfg
  cfg="$(config_file_path)"
  if [[ -f "$cfg" ]]; then
    # shellcheck disable=SC1090
    source "$cfg"
  else
    echo "hint: config not found at ${cfg}, using default svf_root / docker_name" >&2
  fi

  ws="${ws:-$(cd "$SCRIPT_DIR/.." && pwd)}"
  svf_root="${svf_root:-$ws/SVFmemplus}"
  docker_name="${docker_name:-}"

  svf_root="$(cd "$svf_root" && pwd)"
  export ws svf_root docker_name
}

detect_build_type() {
  local arg
  BUILD_TYPE=Release
  for arg in "$@"; do
    if [[ "$arg" =~ ^[Dd]ebug$ ]]; then
      BUILD_TYPE=Debug
      return 0
    fi
  done
}

quote_args() {
  local -n _src=$1
  local -n _dst=$2
  local a
  _dst=()
  for a in "${_src[@]}"; do
    _dst+=("$(printf '%q' "$a")")
  done
}

BUILD_ARGS=("$@")

load_svf_build_config
detect_build_type ${BUILD_ARGS+"${BUILD_ARGS[@]}"}

if [[ -z "$docker_name" ]]; then
  echo "error: rebuild_svf 需要在容器内编译，请在 config.env 设置 docker_name" >&2
  exit 1
fi

if [[ ! -f "$svf_root/build.sh" ]]; then
  echo "error: SVFmemplus 目录无效（缺少 build.sh）: $svf_root" >&2
  exit 1
fi

quoted=()
quote_args BUILD_ARGS quoted
build_arg_str="${quoted[*]}"

echo "==> 强制重编 SVFmemplus（Docker）"
echo "    svf_root    = $svf_root"
echo "    docker_name = $docker_name"
echo "    build_type  = $BUILD_TYPE"
if [[ ${#BUILD_ARGS[@]} -gt 0 ]]; then
  echo "    build.sh args = ${BUILD_ARGS[*]}"
fi

docker run --rm \
  -v "$svf_root:/SVFmemplus" \
  -w /SVFmemplus \
  "$docker_name" \
  bash -lc "
    set -eo pipefail
    cd /SVFmemplus
    source ./setup.sh ${BUILD_TYPE}
    echo '==> 容器内执行: source ./build.sh ${build_arg_str}'
    set +u
    source ./build.sh ${build_arg_str}
  "

docker run --rm \
  -v "$svf_root:/SVFmemplus" \
  -w /SVFmemplus \
  "$docker_name" \
  bash -lc "
    set -eo pipefail
    source ./setup.sh ${BUILD_TYPE}
    missing=()
    command -v saber >/dev/null 2>&1 || missing+=(saber)
    command -v graph-reader >/dev/null 2>&1 || missing+=(graph-reader)
    if [[ \${#missing[@]} -gt 0 ]]; then
      echo \"error: 编译后仍缺少: \${missing[*]}\" >&2
      exit 1
    fi
    echo '==> 编译产物验证通过'
    saber --version 2>/dev/null || saber -h 2>/dev/null | head -1 || true
  "

echo "==> SVFmemplus 重编完成: $svf_root/${BUILD_TYPE}-build/bin"
