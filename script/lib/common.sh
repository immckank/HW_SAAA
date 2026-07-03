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
  if pipeline_in_container; then
    : "${bc:?容器内缺少 bc=}"
    : "${out:?容器内缺少 out=}"
    : "${src:?容器内缺少 src=}"
    : "${svf_root:?容器内缺少 svf_root=}"
    : "${fph_root:?容器内缺少 fph_root=}"

    stem="${stem:-$(basename "$bc" .bc)}"
    ws="${ws:-$(cd "$(script_root)/.." && pwd)}"
    docker_name="${docker_name:-}"
    llm_type="${llm_type:-DeepSeek}"
    project_label="${project_label:-$stem}"
    project_desc="${project_desc:-}"
    semantic_rules="${semantic_rules:-}"

    parse_defect_types "${defect_types:-leak,dfree,uaf,uninit}"

    export ws bc out src stem defect_types
    export svf_root fph_root docker_name
    export llm_type project_label project_desc semantic_rules
    export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-}"
    export QWEN_API_KEY="${QWEN_API_KEY:-}"
    export EXAMPLE_KEY="${EXAMPLE_KEY:-}"
    export HW_KEY="${HW_KEY:-}"
    return 0
  fi

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
  docker_name="${docker_name:-}"
  llm_type="${llm_type:-DeepSeek}"
  project_label="${project_label:-$stem}"
  project_desc="${project_desc:-}"
  semantic_rules="${semantic_rules:-}"
  deepseek_api_key="${deepseek_api_key:-}"
  qwen_api_key="${qwen_api_key:-}"
  example_key="${example_key:-}"
  hw_key="${hw_key:-}"

  parse_defect_types "${defect_types:-leak,dfree,uaf,uninit}"

  svf_root="$(cd "$svf_root" && pwd)"
  fph_root="$(cd "$fph_root" && pwd)"

  export ws bc out src stem defect_types
  export svf_root fph_root docker_name
  export llm_type project_label project_desc semantic_rules
  export DEEPSEEK_API_KEY="$deepseek_api_key"
  export QWEN_API_KEY="$qwen_api_key"
  export EXAMPLE_KEY="$example_key"
  export HW_KEY="$hw_key"

  [[ -f "$bc" ]] || { echo "error: bc 不存在: $bc" >&2; return 1; }
  [[ -d "$src" ]] || { echo "error: src 不存在: $src" >&2; return 1; }
}

print_config_summary() {
  local runtime="${docker_name:-native}"
  [[ -z "$docker_name" ]] && runtime="native"
  cat <<EOF
pipeline config
  bc           = $bc
  out          = $out
  src          = $src
  stem         = $stem
  defect_types = $defect_types
  runtime      = $runtime
  svf_root     = $svf_root
  fph          = $fph_root
EOF
}

pipeline_in_container() {
  [[ "${PIPELINE_IN_CONTAINER:-}" == 1 ]]
}

should_use_docker() {
  [[ -n "${docker_name:-}" ]] && ! pipeline_in_container
}

# 构建 docker run 挂载与 env
pipeline_docker_vols() {
  local bc_base
  bc_base="$(basename "$bc")"
  PIPELINE_DOCKER_VOLS=(
    -v "$svf_root:/SVFmemplus"
    -v "$bc:/data/$bc_base:ro"
    -v "$out:/output"
    -v "$src:/source/FalconFS:ro"
    -v "$fph_root:/FPhandler"
    -v "$(script_root):/pipeline:ro"
  )
  if [[ -n "${semantic_rules:-}" && -f "$semantic_rules" ]]; then
    PIPELINE_DOCKER_VOLS+=(-v "$semantic_rules:/data/semantic_rules.json:ro")
  fi
}

pipeline_docker_env() {
  local bc_base host_uid host_gid
  bc_base="$(basename "$bc")"
  host_uid="$(id -u)"
  host_gid="$(id -g)"
  PIPELINE_DOCKER_ENV=(
    -e PIPELINE_IN_CONTAINER=1
    -e "ws=$ws"
    -e "bc=/data/$bc_base"
    -e "out=/output"
    -e "src=/source/FalconFS"
    -e "svf_root=/SVFmemplus"
    -e "fph_root=/FPhandler"
    -e "stem=$stem"
    -e "defect_types=$defect_types"
    -e "docker_name=$docker_name"
    -e "llm_type=$llm_type"
    -e "project_label=$project_label"
    -e "project_desc=$project_desc"
    -e "host_uid=$host_uid"
    -e "host_gid=$host_gid"
    -e "DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY:-}"
    -e "QWEN_API_KEY=${QWEN_API_KEY:-}"
    -e "EXAMPLE_KEY=${EXAMPLE_KEY:-}"
    -e "HW_KEY=${HW_KEY:-}"
    -e "agent_max_turns=${agent_max_turns:-64}"
    -e "agent_conclusion_reserve_turns=${agent_conclusion_reserve_turns:-10}"
  )
  if [[ -n "${semantic_rules:-}" && -f "$semantic_rules" ]]; then
    PIPELINE_DOCKER_ENV+=(-e "semantic_rules=/data/semantic_rules.json")
  else
    PIPELINE_DOCKER_ENV+=(-e "semantic_rules=")
  fi
  if [[ -n "${FORCE_BUILD:-}" ]]; then
    PIPELINE_DOCKER_ENV+=(-e FORCE_BUILD=1)
  fi
}

exec_in_docker() {
  pipeline_docker_vols
  pipeline_docker_env
  docker run --rm \
    "${PIPELINE_DOCKER_VOLS[@]}" \
    "${PIPELINE_DOCKER_ENV[@]}" \
    -w /SVFmemplus \
    "$docker_name" \
    bash /pipeline/run_pipeline.sh "$@"
}
