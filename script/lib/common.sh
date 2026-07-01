#!/usr/bin/env bash
# 全局管线公共函数：加载 config.env、解析 bc/out/src/defect_types。

set -euo pipefail

VALID_DEFECT_TYPES=(leak dfree uaf uninit bof)

script_root() {
  cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
}

config_file_path() {
  printf '%s\n' "$(script_root)/config.env"
}

parse_defect_types() {
  local raw="${1:-leak,dfree,uaf,uninit}"
  local token seen="" dt
  DEFECT_TYPES_ARR=()
  IFS=',' read -ra _tokens <<< "$raw"
  for token in "${_tokens[@]}"; do
    dt="${token// /}"
    [[ -n "$dt" ]] || continue
    if [[ " ${seen} " != *" $dt "* ]]; then
      local valid=0
      for v in "${VALID_DEFECT_TYPES[@]}"; do
        if [[ "$dt" == "$v" ]]; then
          valid=1
          break
        fi
      done
      if [[ "$valid" -eq 0 ]]; then
        echo "error: 未知 defect_type: $dt（合法: ${VALID_DEFECT_TYPES[*]}）" >&2
        return 1
      fi
      DEFECT_TYPES_ARR+=("$dt")
      seen="$seen $dt"
    fi
  done
  if [[ ${#DEFECT_TYPES_ARR[@]} -eq 0 ]]; then
    echo "error: defect_types 为空" >&2
    return 1
  fi
  defect_types="$(IFS=,; echo "${DEFECT_TYPES_ARR[*]}")"
}

load_config() {
  local cfg
  cfg="$(config_file_path)"
  if [[ ! -f "$cfg" ]]; then
    echo "error: 未找到配置文件: $cfg" >&2
    echo "hint: cp script/config.env.example script/config.env" >&2
    return 1
  fi
  # shellcheck disable=SC1090
  source "$cfg"

  : "${bc:?config 缺少 bc=}"
  : "${out:?config 缺少 out=}"
  : "${src:?config 缺少 src=}"

  ws="${ws:-$(script_root)}"
  bc="$(cd "$(dirname "$bc")" && pwd)/$(basename "$bc")"
  out="$(mkdir -p "$out" && cd "$out" && pwd)"
  src="$(cd "$src" && pwd)"
  stem="$(basename "$bc" .bc)"

  svf_root="${svf_root:-$ws/SVFmemplus}"
  fph_root="${fph_root:-$ws/FPhandler}"
  svf_docker_image="${svf_docker_image:-nf-image:llvm21}"
  svf_mode="${svf_mode:-docker}"
  llm_type="${llm_type:-DeepSeek}"
  project_label="${project_label:-$stem}"
  project_desc="${project_desc:-}"
  semantic_rules="${semantic_rules:-}"
  deepseek_api_key="${deepseek_api_key:-}"
  qwen_api_key="${qwen_api_key:-}"
  example_key="${example_key:-}"
  hw_key="${hw_key:-}"

  parse_defect_types "${defect_types:-leak,dfree,uaf,uninit}"

  export ws bc out src stem defect_types
  export svf_root fph_root svf_docker_image svf_mode
  export llm_type project_label project_desc semantic_rules
  export DEEPSEEK_API_KEY="$deepseek_api_key"
  export QWEN_API_KEY="$qwen_api_key"
  export EXAMPLE_KEY="$example_key"
  export HW_KEY="$hw_key"
  export SVF_DOCKER_IMAGE="$svf_docker_image"

  [[ -f "$bc" ]] || { echo "error: bc 不存在: $bc" >&2; return 1; }
  [[ -d "$src" ]] || { echo "error: src 不存在: $src" >&2; return 1; }
}

print_config_summary() {
  cat <<EOF
pipeline config
  bc           = $bc
  out          = $out
  src          = $src
  stem         = $stem
  defect_types = $defect_types
  svf          = $svf_mode ($svf_root)
  fph          = $fph_root
EOF
}
