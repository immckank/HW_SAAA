#!/usr/bin/env bash
# 运行 SVFmemplus Saber/BOF，输出到 $out。
#
# 用法:
#   ./script/run_svf.sh
#   ./script/run_svf.sh --native
#   ./script/run_svf.sh --checkers leak,dfree
#
# docker 模式：挂载工作区与被测程序，在容器内执行与 native 相同的 run_svf_inner.sh。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"

USE_NATIVE=""
FORCE_CHECKERS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --native) USE_NATIVE=1; shift ;;
    --checkers)
      [[ $# -ge 2 ]] || { echo "error: --checkers 需要参数" >&2; exit 2; }
      FORCE_CHECKERS=$2
      shift 2
      ;;
    -h|--help)
      sed -n '2,9p' "$0"
      exit 0
      ;;
    *)
      echo "error: 未知参数: $1" >&2
      exit 2
      ;;
  esac
done

load_config
if [[ -n "$FORCE_CHECKERS" ]]; then
  parse_defect_types "$FORCE_CHECKERS"
  export defect_types
fi

print_config_summary

shopt -s nullglob
legacy_outputs=(
  "$out"/*_report.json
  "$out"/*_report.md
  "$out"/*_slices.json
  "$out"/*.txt
)
if [[ ${#legacy_outputs[@]} -gt 0 ]]; then
  rm -f -- "${legacy_outputs[@]}"
fi
shopt -u nullglob

_run_svf_native() {
  export bc_path="$bc" out_dir="$out" stem="$stem" saber_src="$src" svf_root="$svf_root"
  bash "$SCRIPT_DIR/lib/run_svf_inner.sh"
}

_run_svf_docker() {
  local bc_base vols=()
  bc_base="$(basename "$bc")"
  vols=(
    -v "$svf_root:/SVFmemplus"
    -v "$bc:/data/$bc_base:ro"
    -v "$out:/output"
    -v "$src:/source/FalconFS:ro"
    -v "$SCRIPT_DIR/lib:/pipeline/lib:ro"
  )
  if [[ -n "${semantic_rules:-}" && -f "$semantic_rules" ]]; then
    vols+=(-v "$semantic_rules:/data/semantic_rules.json:ro")
  fi

  docker run --rm \
    "${vols[@]}" \
    -e "defect_types=$defect_types" \
    -e "bc_path=/data/$bc_base" \
    -e "out_dir=/output" \
    -e "stem=$stem" \
    -e "saber_src=/source/FalconFS" \
    -e "svf_root=/SVFmemplus" \
    -e "host_uid=$(id -u)" \
    -e "host_gid=$(id -g)" \
    -e "semantic_rules=${semantic_rules:-}" \
    -w /SVFmemplus \
    "$svf_docker_image" \
    bash -lc '
      set -euo pipefail
      if [[ -n "${semantic_rules:-}" && -f /data/semantic_rules.json ]]; then
        export semantic_rules=/data/semantic_rules.json
      fi
      bash /pipeline/lib/run_svf_inner.sh
      if [[ -d /output/alerts ]]; then
        chown -R "${host_uid:?}:${host_gid:?}" /output/alerts
      fi
    '
}

if [[ "$USE_NATIVE" == 1 || "$svf_mode" == "native" ]]; then
  _run_svf_native
else
  _run_svf_docker
fi

echo "SVF outputs: $out"
