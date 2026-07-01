#!/usr/bin/env bash
# 运行 SVFmemplus Saber/BOF，输出到 $out。
#
# 用法:
#   ./script/run_svf.sh
#   ./script/run_svf.sh --native
#   ./script/run_svf.sh --checkers leak,dfree
#
# Docker 模式下每个 checker 单独起一个容器。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/run_checkers.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/svf_env.sh"

USE_NATIVE=0
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

# The unified pipeline has no text/aggregate/slice consumers. Remove artifacts
# from the replaced format so the output directory has one authoritative source.
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

_docker_volumes() {
  local bc_base=$1
  local -n _out_arr=$2
  _out_arr=(
    -v "$svf_root:/SVFmemplus"
    -v "$bc:/data/$bc_base:ro"
    -v "$out:/output"
    -v "$src:/source/FalconFS:ro"
    -v "$SCRIPT_DIR/lib/run_checkers.sh:/pipeline/run_checkers.sh:ro"
    -v "$SCRIPT_DIR/lib/svf_env.sh:/pipeline/svf_env.sh:ro"
  )
  if [[ -n "${semantic_rules:-}" && -f "$semantic_rules" ]]; then
    _out_arr+=(-v "$semantic_rules:/data/semantic_rules.json:ro")
  fi
}

_docker_ensure_built() {
  local bc_base=$1
  shift
  local -a vols=("$@")
  docker run --rm \
    "${vols[@]}" \
    -w /SVFmemplus \
    "$svf_docker_image" \
    bash -lc 'source /pipeline/svf_env.sh && ensure_svf_env /SVFmemplus'
}

_docker_run_checker() {
  local checker=$1 bc_base=$2
  shift 2
  local -a vols=("$@")
  docker run --rm \
    "${vols[@]}" \
    -e "defect_types=$checker" \
    -e "checker=$checker" \
    -e "stem=$stem" \
    -e "host_uid=$(id -u)" \
    -e "host_gid=$(id -g)" \
    -e "bc_container=/data/$bc_base" \
    -e "semantic_rules=${semantic_rules:-}" \
    -w /SVFmemplus \
    "$svf_docker_image" \
    bash -lc '
      set -euo pipefail
      source ./setup.sh Release
      bc="${bc_container:?}"
      out=/output
      export SABER_SOURCE_ROOT=/source/FalconFS
      stem="${stem:?}"
      if [[ -n "${semantic_rules:-}" && -f /data/semantic_rules.json ]]; then
        semantic_rules=/data/semantic_rules.json
      fi
      source /pipeline/run_checkers.sh
      run_one_checker "$bc" "$out" "$stem" "${checker:?}"
      chown -R "${host_uid:?}:${host_gid:?}" "$out/alerts"
    '
}

run_docker() {
  local bc_base vols=()
  bc_base="$(basename "$bc")"
  _docker_volumes "$bc_base" vols
  _docker_ensure_built "$bc_base" "${vols[@]}"

  local checker
  local -a failed=()
  for checker in "${DEFECT_TYPES_ARR[@]}"; do
    echo "==> docker checker: $checker"
    set +e
    _docker_run_checker "$checker" "$bc_base" "${vols[@]}"
    local rc=$?
    set -e
    if [[ "$rc" -ne 0 ]]; then
      echo "error: checker $checker 失败 (exit $rc)" >&2
      failed+=("$checker")
    fi
  done

  if [[ ${#failed[@]} -gt 0 ]]; then
    echo "error: 以下 checker 未成功: ${failed[*]}" >&2
    echo "hint: 可单独重试，例如 ./script/run_svf.sh --checkers dfree" >&2
    return 1
  fi
}

_run_in_svf_env() {
  bash -lc "
    set -euo pipefail
    source '$SCRIPT_DIR/lib/svf_env.sh'
    ensure_svf_env '$svf_root'
    defect_types='$defect_types'
    semantic_rules='${semantic_rules:-}'
    export SABER_SOURCE_ROOT='$src'
    source '$SCRIPT_DIR/lib/run_checkers.sh'
    run_checkers_for_bc '$bc' '$out' '$stem'
  "
}

if [[ "$USE_NATIVE" -eq 1 || "$svf_mode" == "native" ]]; then
  _run_in_svf_env
else
  run_docker
fi

echo "SVF outputs: $out"
