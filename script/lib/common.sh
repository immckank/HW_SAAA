#!/usr/bin/env bash
# 全局管线公共函数：加载 config.env、解析 bc/out/src/defect_types。

set -euo pipefail

VALID_DEFECT_TYPES=(leak dfree uaf uninit bof)

script_root() {
  cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
}

config_file_path() {
  if [[ -n "${PIPELINE_CONFIG:-}" ]]; then
    printf '%s\n' "$PIPELINE_CONFIG"
    return 0
  fi
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
    : "${active_learning_root:?容器内缺少 active_learning_root=}"

    stem="${stem:-$(basename "$bc" .bc)}"
    ws="${ws:-$(cd "$(script_root)/.." && pwd)}"
    docker_name="${docker_name:-}"
    llm_type="${llm_type:-DeepSeek}"
    project_label="${project_label:-$stem}"
    project_desc="${project_desc:-}"
    semantic_rules="${semantic_rules:-$out/semantic_facts.json}"
    active_learning_feedback_top_k="${active_learning_feedback_top_k:-10}"
    active_learning_feedback_bottom_k="${active_learning_feedback_bottom_k:-10}"
    active_learning_feedback_random_k="${active_learning_feedback_random_k:-0}"
    active_learning_feedback_random_seed="${active_learning_feedback_random_seed:-42}"
    active_learning_feedback_random_strategy="${active_learning_feedback_random_strategy:-plain}"
    active_learning_feedback_skip_classified="${active_learning_feedback_skip_classified:-0}"
    active_learning_rounds="${active_learning_rounds:-1}"
    active_learning_round_prefix="${active_learning_round_prefix:-}"
    active_learning_model_path="${active_learning_model_path:-}"
    active_learning_checkpoint_dir="${active_learning_checkpoint_dir:-$out/active_learning/checkpoints}"
    active_learning_train_epochs="${active_learning_train_epochs:-50}"
    active_learning_train_lr="${active_learning_train_lr:-0.001}"
    active_learning_train_weight_decay="${active_learning_train_weight_decay:-0.0005}"
    active_learning_train_batch_size="${active_learning_train_batch_size:-8}"
    active_learning_train_val_ratio="${active_learning_train_val_ratio:-0.2}"
    active_learning_train_patience="${active_learning_train_patience:-10}"
    active_learning_train_min_labels="${active_learning_train_min_labels:-2}"
    active_learning_uncertain_weight="${active_learning_uncertain_weight:-0.3}"
    active_learning_unlabeled_weight="${active_learning_unlabeled_weight:-0.1}"
    active_learning_weak_pos="${active_learning_weak_pos:-0.7}"
    active_learning_weak_neg="${active_learning_weak_neg:-0.3}"
    active_learning_max_unlabeled="${active_learning_max_unlabeled:-512}"

    parse_defect_types "${defect_types:-leak,dfree,uaf,uninit}"

    export ws bc out src stem defect_types
    export svf_root fph_root active_learning_root docker_name
    export llm_type project_label project_desc semantic_rules
    export active_learning_feedback_top_k active_learning_feedback_bottom_k
    export active_learning_feedback_random_k active_learning_feedback_random_seed
    export active_learning_feedback_random_strategy
    export active_learning_feedback_skip_classified
    export active_learning_rounds active_learning_round_prefix
    export active_learning_model_path active_learning_checkpoint_dir
    export active_learning_train_epochs active_learning_train_lr
    export active_learning_train_weight_decay active_learning_train_batch_size
    export active_learning_train_val_ratio active_learning_train_patience
    export active_learning_train_min_labels
    export active_learning_uncertain_weight active_learning_unlabeled_weight
    export active_learning_weak_pos active_learning_weak_neg
    export active_learning_max_unlabeled
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
  active_learning_root="${active_learning_root:-$ws/ActiveLearning}"
  docker_name="${docker_name:-}"
  llm_type="${llm_type:-DeepSeek}"
  project_label="${project_label:-$stem}"
  project_desc="${project_desc:-}"
  semantic_rules="${semantic_rules:-}"
  active_learning_feedback_top_k="${active_learning_feedback_top_k:-10}"
  active_learning_feedback_bottom_k="${active_learning_feedback_bottom_k:-10}"
  active_learning_feedback_random_k="${active_learning_feedback_random_k:-0}"
  active_learning_feedback_random_seed="${active_learning_feedback_random_seed:-42}"
  active_learning_feedback_random_strategy="${active_learning_feedback_random_strategy:-plain}"
  active_learning_feedback_skip_classified="${active_learning_feedback_skip_classified:-0}"
  active_learning_rounds="${active_learning_rounds:-1}"
  active_learning_round_prefix="${active_learning_round_prefix:-}"
  active_learning_model_path="${active_learning_model_path:-}"
  active_learning_checkpoint_dir="${active_learning_checkpoint_dir:-$out/active_learning/checkpoints}"
  active_learning_train_epochs="${active_learning_train_epochs:-50}"
  active_learning_train_lr="${active_learning_train_lr:-0.001}"
  active_learning_train_weight_decay="${active_learning_train_weight_decay:-0.0005}"
  active_learning_train_batch_size="${active_learning_train_batch_size:-8}"
  active_learning_train_val_ratio="${active_learning_train_val_ratio:-0.2}"
  active_learning_train_patience="${active_learning_train_patience:-10}"
  active_learning_train_min_labels="${active_learning_train_min_labels:-2}"
  active_learning_uncertain_weight="${active_learning_uncertain_weight:-0.3}"
  active_learning_unlabeled_weight="${active_learning_unlabeled_weight:-0.1}"
  active_learning_weak_pos="${active_learning_weak_pos:-0.7}"
  active_learning_weak_neg="${active_learning_weak_neg:-0.3}"
  active_learning_max_unlabeled="${active_learning_max_unlabeled:-512}"
  deepseek_api_key="${deepseek_api_key:-}"
  qwen_api_key="${qwen_api_key:-}"
  example_key="${example_key:-}"
  hw_key="${hw_key:-}"

  parse_defect_types "${defect_types:-leak,dfree,uaf,uninit}"

  svf_root="$(cd "$svf_root" && pwd)"
  fph_root="$(cd "$fph_root" && pwd)"
  active_learning_root="$(cd "$active_learning_root" && pwd)"
  if [[ -z "$semantic_rules" ]]; then
    semantic_rules="$out/semantic_facts.json"
  elif [[ "$semantic_rules" != /* ]]; then
    semantic_rules="$ws/$semantic_rules"
  fi
  mkdir -p "$(dirname "$semantic_rules")"
  if [[ -n "$active_learning_model_path" ]]; then
    # Allow relative paths from workspace; do not require file to exist yet.
    if [[ "$active_learning_model_path" != /* ]]; then
      active_learning_model_path="$ws/$active_learning_model_path"
    fi
  fi
  if [[ "$active_learning_checkpoint_dir" != /* ]]; then
    active_learning_checkpoint_dir="$out/$active_learning_checkpoint_dir"
  fi
  mkdir -p "$active_learning_checkpoint_dir"

  export ws bc out src stem defect_types
  export svf_root fph_root active_learning_root docker_name
  export llm_type project_label project_desc semantic_rules
  export active_learning_feedback_top_k active_learning_feedback_bottom_k
  export active_learning_feedback_random_k active_learning_feedback_random_seed
  export active_learning_feedback_random_strategy
  export active_learning_feedback_skip_classified
  export active_learning_rounds active_learning_round_prefix
  export active_learning_model_path active_learning_checkpoint_dir
  export active_learning_train_epochs active_learning_train_lr
  export active_learning_train_weight_decay active_learning_train_batch_size
  export active_learning_train_val_ratio active_learning_train_patience
  export active_learning_train_min_labels
  export active_learning_uncertain_weight active_learning_unlabeled_weight
  export active_learning_weak_pos active_learning_weak_neg
  export active_learning_max_unlabeled
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
  local model_display="${active_learning_model_path:-}"
  [[ -z "$model_display" ]] && model_display="(empty → latest.pt or random)"
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
  active       = $active_learning_root
  semantic     = $semantic_rules
  al_rounds    = ${active_learning_rounds:-1}
  al_feedback  = top ${active_learning_feedback_top_k:-10} + bottom ${active_learning_feedback_bottom_k:-10} + random ${active_learning_feedback_random_k:-0}
  al_model     = $model_display
  al_ckpt_dir  = ${active_learning_checkpoint_dir:-$out/active_learning/checkpoints}
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
  local bc_base semantic_dir main_root
  bc_base="$(basename "$bc")"
  main_root="$(cd "$(script_root)/.." && pwd)"
  PIPELINE_DOCKER_VOLS=(
    -v "$svf_root:/SVFmemplus"
    -v "$bc:/data/$bc_base:ro"
    -v "$out:/output"
    -v "$src:/source/FalconFS:ro"
    -v "$fph_root:/FPhandler"
    -v "$active_learning_root:/ActiveLearning"
    -v "$(script_root):/pipeline:ro"
    -v "$main_root/orchestrator:/orchestrator-package/orchestrator:ro"
    -v "$main_root/contracts:/orchestrator-package/contracts:ro"
  )
  if [[ -n "${semantic_rules:-}" && "$semantic_rules" != "$out"/* ]]; then
    semantic_dir="$(cd "$(dirname "$semantic_rules")" && pwd)"
    PIPELINE_DOCKER_VOLS+=(-v "$semantic_dir:/semantic-facts")
  fi
}

pipeline_docker_env() {
  local bc_base host_uid host_gid model_in container_ckpt semantic_in
  bc_base="$(basename "$bc")"
  host_uid="$(id -u)"
  host_gid="$(id -g)"
  model_in="${active_learning_model_path:-}"
  if [[ -n "$model_in" && -n "${out:-}" && "$model_in" == "$out"* ]]; then
    model_in="/output${model_in#"$out"}"
  fi
  container_ckpt="/output/active_learning/checkpoints"
  if [[ -n "${active_learning_checkpoint_dir:-}" && -n "${out:-}" && "$active_learning_checkpoint_dir" == "$out"* ]]; then
    container_ckpt="/output${active_learning_checkpoint_dir#"$out"}"
  fi
  semantic_in="${semantic_rules:-}"
  if [[ -n "$semantic_in" && "$semantic_in" == "$out"* ]]; then
    semantic_in="/output${semantic_in#"$out"}"
  elif [[ -n "$semantic_in" ]]; then
    semantic_in="/semantic-facts/$(basename "$semantic_in")"
  fi
  PIPELINE_DOCKER_ENV=(
    -e PIPELINE_IN_CONTAINER=1
    -e ORCHESTRATOR_ROOT=/orchestrator-package
    -e "ws=$ws"
    -e "bc=/data/$bc_base"
    -e "out=/output"
    -e "src=/source/FalconFS"
    -e "svf_root=/SVFmemplus"
    -e "fph_root=/FPhandler"
    -e "active_learning_root=/ActiveLearning"
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
    -e "active_learning_feedback_top_k=${active_learning_feedback_top_k:-10}"
    -e "active_learning_feedback_bottom_k=${active_learning_feedback_bottom_k:-10}"
    -e "active_learning_feedback_random_k=${active_learning_feedback_random_k:-0}"
    -e "active_learning_feedback_random_seed=${active_learning_feedback_random_seed:-42}"
    -e "active_learning_feedback_random_strategy=${active_learning_feedback_random_strategy:-plain}"
    -e "active_learning_feedback_skip_classified=${active_learning_feedback_skip_classified:-0}"
    -e "active_learning_rounds=${active_learning_rounds:-1}"
    -e "active_learning_round_prefix=${active_learning_round_prefix:-}"
    -e "active_learning_model_path=$model_in"
    -e "active_learning_checkpoint_dir=$container_ckpt"
    -e "active_learning_train_epochs=${active_learning_train_epochs:-50}"
    -e "active_learning_train_lr=${active_learning_train_lr:-0.001}"
    -e "active_learning_train_weight_decay=${active_learning_train_weight_decay:-0.0005}"
    -e "active_learning_train_batch_size=${active_learning_train_batch_size:-8}"
    -e "active_learning_train_val_ratio=${active_learning_train_val_ratio:-0.2}"
    -e "active_learning_train_patience=${active_learning_train_patience:-10}"
    -e "active_learning_train_min_labels=${active_learning_train_min_labels:-2}"
    -e "active_learning_uncertain_weight=${active_learning_uncertain_weight:-0.3}"
    -e "active_learning_unlabeled_weight=${active_learning_unlabeled_weight:-0.1}"
    -e "active_learning_weak_pos=${active_learning_weak_pos:-0.7}"
    -e "active_learning_weak_neg=${active_learning_weak_neg:-0.3}"
    -e "active_learning_max_unlabeled=${active_learning_max_unlabeled:-512}"
  )
  PIPELINE_DOCKER_ENV+=(-e "semantic_rules=$semantic_in")
  if [[ -n "${FORCE_BUILD:-}" ]]; then
    PIPELINE_DOCKER_ENV+=(-e FORCE_BUILD=1)
  fi
  if [[ "${FORCE_SVF:-}" == "1" ]]; then
    PIPELINE_DOCKER_ENV+=(-e FORCE_SVF=1)
  fi
}

exec_in_docker() {
  pipeline_docker_vols
  pipeline_docker_env
  exec docker run --rm \
    "${PIPELINE_DOCKER_VOLS[@]}" \
    "${PIPELINE_DOCKER_ENV[@]}" \
    -w /SVFmemplus \
    "$docker_name" \
    bash /pipeline/run_pipeline.sh "$@"
}
