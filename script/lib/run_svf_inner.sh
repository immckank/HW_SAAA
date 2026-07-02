#!/usr/bin/env bash
# Saber/BOF 执行核心：native 与 docker 共用同一流程。
# 由 run_svf.sh 设置 bc_path / out_dir / stem / saber_src / svf_root / defect_types 后调用。

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$LIB_DIR/svf_env.sh"
# shellcheck disable=SC1091
source "$LIB_DIR/run_checkers.sh"

: "${bc_path:?bc_path 未设置}"
: "${out_dir:?out_dir 未设置}"
: "${stem:?stem 未设置}"
: "${saber_src:?saber_src 未设置}"
: "${svf_root:?svf_root 未设置}"

cd "$svf_root"
# shellcheck disable=SC1091
source ./setup.sh Release
ensure_saber_bof_env "$svf_root"
export SABER_SOURCE_ROOT="$saber_src"

run_checkers_for_bc "$bc_path" "$out_dir" "$stem"
