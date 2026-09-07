#!/usr/bin/env bash
# 统一启动本地 Workflow UI（本机或 Docker）。
#
# 用法:
#   ./script/run_local_ui.sh start-server local  [选项]
#   ./script/run_local_ui.sh start-server docker [选项]
#
# LLM / Agent 参数来自 runtime.env（不要写入 workflow.ini）。
# 项目路径仍来自 workflow.ini。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

MODE=""
CONFIG_PATH="$REPO_ROOT/workflow.ini"
ENV_FILE="$REPO_ROOT/runtime.env"
PORT=8765
HOST=""
IMAGE="openeuler-workflow:latest"
DOCKERFILE="$REPO_ROOT/dockerfile.svfmemplus"
BUILD_IMAGE=0
SKIP_PREFLIGHT=0
PUBLISH_HOST="127.0.0.1"
# docker 默认挂载宿主机 GPU 0；local 模式不使用本变量。
DOCKER_GPUS="device=0"

usage() {
  cat <<'EOF'
用法:
  ./script/run_local_ui.sh start-server local|docker [选项]

选项:
  --config PATH          workflow.ini（默认: <repo>/workflow.ini）
  --env-file PATH        runtime.env（默认: <repo>/runtime.env）
  --port PORT            监听端口（默认: 8765）
  --host HOST            绑定地址
                         local 默认 127.0.0.1
                         docker 容器内固定 0.0.0.0，宿主机仅发布到 127.0.0.1
  --image NAME           Docker 镜像名（默认: openeuler-workflow:latest）
  --dockerfile PATH      构建用 Dockerfile（默认: dockerfile.svfmemplus）
  --build                docker 模式启动前先 docker build
  --skip-preflight       跳过 torch / openai / SVF 可执行文件预检
  --gpus SPEC            仅 docker：传给 docker --gpus（默认: device=0）
                         示例: device=0 | device=1 | '"device=0,1"' | all
  --no-gpu               仅 docker：不挂载 GPU（纯 CPU）
  -h, --help             显示帮助

runtime.env 示例字段:
  LLM_TYPE, LLM_MODEL, LLM_BASE_URL,
  DEEPSEEK_API_KEY / QWEN_API_KEY / HW_KEY,
  AGENT_MAX_TURNS, AGENT_CONCLUSION_RESERVE_TURNS,
  ACTIVE_LEARNING_FEEDBACK_TOP_K / BOTTOM_K / RANDOM_K / ...
  ACTIVE_LEARNING_TRAIN_BATCH_SIZE / MAX_UNLABELED / MAX_BATCH_NODES / ...
EOF
}

die() {
  echo "error: $*" >&2
  exit 1
}

require_file() {
  [[ -f "$1" ]] || die "找不到文件: $1"
}

strip_wrapping_quotes() {
  local value="$1"
  if [[ ${#value} -ge 2 ]]; then
    if [[ "$value" == \"*\" || "$value" == \'*\' ]]; then
      value="${value:1:${#value}-2}"
    fi
  fi
  printf '%s' "$value"
}

# 将 Docker 兼容的 KEY=VALUE 文件导出到当前 shell。
load_runtime_env() {
  local file="$1" line key value
  require_file "$file"
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" == *"="* ]] || die "runtime.env 非法行（需要 KEY=VALUE）: $line"
    key="${line%%=*}"
    value="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    key="${key#"${key%%[![:space:]]*}"}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "runtime.env 非法变量名: $key"
    value="$(strip_wrapping_quotes "$value")"
    export "$key=$value"
  done <"$file"
}

validate_runtime_env() {
  local llm_type key_var
  llm_type="${LLM_TYPE:-DeepSeek}"
  case "$llm_type" in
    DeepSeek) key_var=DEEPSEEK_API_KEY ;;
    Qwen) key_var=QWEN_API_KEY ;;
    HW) key_var=HW_KEY ;;
    Example) key_var="" ;;
    *) die "不支持的 LLM_TYPE=$llm_type（期望 DeepSeek/Qwen/HW/Example）" ;;
  esac
  if [[ -n "$key_var" && -z "${!key_var:-}" ]]; then
    die "LLM_TYPE=$llm_type 需要在 $ENV_FILE 中设置 $key_var"
  fi
  if [[ "$llm_type" == "HW" && -z "${LLM_BASE_URL:-}" ]]; then
    die "LLM_TYPE=HW 需要设置 LLM_BASE_URL"
  fi
  if [[ -n "${AGENT_MAX_TURNS:-}" ]] && ! [[ "$AGENT_MAX_TURNS" =~ ^[1-9][0-9]*$ ]]; then
    die "AGENT_MAX_TURNS 必须是正整数"
  fi
  if [[ -n "${AGENT_CONCLUSION_RESERVE_TURNS:-}" ]] && ! [[ "$AGENT_CONCLUSION_RESERVE_TURNS" =~ ^[0-9]+$ ]]; then
    die "AGENT_CONCLUSION_RESERVE_TURNS 必须是非负整数"
  fi
  for key in \
    ACTIVE_LEARNING_FEEDBACK_TOP_K \
    ACTIVE_LEARNING_FEEDBACK_BOTTOM_K \
    ACTIVE_LEARNING_FEEDBACK_RANDOM_K \
    ACTIVE_LEARNING_FEEDBACK_RANDOM_SEED
  do
    value="${!key:-}"
    if [[ -n "$value" ]] && ! [[ "$value" =~ ^[0-9]+$ ]]; then
      die "$key 必须是非负整数"
    fi
  done
  strategy="${ACTIVE_LEARNING_FEEDBACK_RANDOM_STRATEGY:-plain}"
  case "$strategy" in
    plain|type) ;;
    *) die "ACTIVE_LEARNING_FEEDBACK_RANDOM_STRATEGY 必须是 plain 或 type" ;;
  esac
  skip="${ACTIVE_LEARNING_FEEDBACK_SKIP_CLASSIFIED:-0}"
  case "$skip" in
    0|1|true|false|yes|no|on|off) ;;
    *) die "ACTIVE_LEARNING_FEEDBACK_SKIP_CLASSIFIED 必须是布尔值" ;;
  esac
  for key in \
    ACTIVE_LEARNING_TRAIN_BATCH_SIZE \
    ACTIVE_LEARNING_TRAIN_MAX_UNLABELED \
    ACTIVE_LEARNING_TRAIN_EPOCHS \
    ACTIVE_LEARNING_TRAIN_PATIENCE \
    ACTIVE_LEARNING_TRAIN_MIN_LABELS \
    ACTIVE_LEARNING_TRAIN_MAX_BATCH_NODES \
    ACTIVE_LEARNING_TRAIN_MAX_BATCH_EDGES
  do
    value="${!key:-}"
    if [[ -n "$value" ]] && ! [[ "$value" =~ ^[0-9]+$ ]]; then
      die "$key 必须是非负整数"
    fi
  done
}

print_runtime_summary() {
  echo "runtime:"
  echo "  LLM_TYPE=${LLM_TYPE:-DeepSeek}"
  echo "  LLM_MODEL=${LLM_MODEL:-(provider default)}"
  echo "  LLM_BASE_URL=${LLM_BASE_URL:-(provider default)}"
  echo "  AGENT_MAX_TURNS=${AGENT_MAX_TURNS:-64}"
  echo "  AGENT_CONCLUSION_RESERVE_TURNS=${AGENT_CONCLUSION_RESERVE_TURNS:-10}"
  echo "  AL_FEEDBACK=top ${ACTIVE_LEARNING_FEEDBACK_TOP_K:-10} + bottom ${ACTIVE_LEARNING_FEEDBACK_BOTTOM_K:-10} + random ${ACTIVE_LEARNING_FEEDBACK_RANDOM_K:-0}"
  echo "  AL_TRAIN=batch ${ACTIVE_LEARNING_TRAIN_BATCH_SIZE:-1} max_unlabeled ${ACTIVE_LEARNING_TRAIN_MAX_UNLABELED:-64}"
  echo "  env-file=$ENV_FILE"
  echo "  config=$CONFIG_PATH"
}

check_python_deps() {
  local python_bin="$1"
  echo "preflight: python packages via $python_bin"
  "$python_bin" -c '
import importlib, sys
missing = []
for name in ("openai", "torch", "torch_geometric"):
    try:
        importlib.import_module(name)
    except Exception as exc:
        missing.append(f"{name}: {exc}")
if missing:
    print("error: missing or broken dependencies:", file=sys.stderr)
    for item in missing:
        print(f"  - {item}", file=sys.stderr)
    raise SystemExit(1)
print("ok: openai, torch, torch_geometric")
'
}

check_svf_binaries() {
  local svf_root="${1:-${SVF_ROOT:-$REPO_ROOT/SVFmemplus}}"
  local saber="$svf_root/Release-build/bin/saber"
  local bof="$svf_root/Release-build/bin/bof"
  echo "preflight: SVF binaries under $svf_root"
  [[ -x "$saber" ]] || die "缺少可执行文件: $saber"
  [[ -x "$bof" ]] || die "缺少可执行文件: $bof"
  echo "ok: saber, bof"
}

ensure_workflow_ini() {
  if [[ ! -f "$CONFIG_PATH" ]]; then
    if [[ -f "$REPO_ROOT/workflow.ini.example" && "$CONFIG_PATH" == "$REPO_ROOT/workflow.ini" ]]; then
      die "缺少 $CONFIG_PATH；请先: cp workflow.ini.example workflow.ini"
    fi
    die "缺少 workflow.ini: $CONFIG_PATH"
  fi
}

ensure_runtime_env_file() {
  if [[ ! -f "$ENV_FILE" ]]; then
    if [[ -f "$REPO_ROOT/runtime.env.example" && "$ENV_FILE" == "$REPO_ROOT/runtime.env" ]]; then
      die "缺少 $ENV_FILE；请先: cp runtime.env.example runtime.env && chmod 600 runtime.env"
    fi
    die "缺少 runtime.env: $ENV_FILE"
  fi
}

start_local() {
  local bind_host="${HOST:-127.0.0.1}"
  ensure_workflow_ini
  ensure_runtime_env_file
  load_runtime_env "$ENV_FILE"
  validate_runtime_env
  print_runtime_summary
  if [[ "$SKIP_PREFLIGHT" -eq 0 ]]; then
    check_python_deps python3
    check_svf_binaries
  fi
  cd "$REPO_ROOT"
  exec python3 -m local_ui \
    --config "$CONFIG_PATH" \
    --env-file "$ENV_FILE" \
    --host "$bind_host" \
    --port "$PORT"
}

docker_image_exists() {
  docker image inspect "$IMAGE" >/dev/null 2>&1
}

require_docker_gpu() {
  local probe
  [[ -n "$DOCKER_GPUS" ]] || return 0
  echo "gpu probe: docker --gpus $DOCKER_GPUS"
  set +e
  # 不依赖业务镜像：只验证 Docker 能否申请 GPU device。
  probe="$(docker run --rm --gpus "$DOCKER_GPUS" ubuntu:24.04 true 2>&1)"
  status=$?
  set -e
  if [[ $status -ne 0 ]]; then
    echo "$probe" >&2
    die "docker --gpus 不可用（常见原因: 未安装 nvidia-container-toolkit）。请先执行: ./script/install_nvidia_container_toolkit.sh"
  fi
  echo "ok: docker --gpus $DOCKER_GPUS"
}

start_docker() {
  local bind_host="0.0.0.0"
  local publish="${PUBLISH_HOST}:${PORT}:${PORT}"
  local svf_root
  local -a gpu_args=()

  command -v docker >/dev/null 2>&1 || die "未找到 docker 命令"
  ensure_workflow_ini
  ensure_runtime_env_file
  load_runtime_env "$ENV_FILE"
  validate_runtime_env
  print_runtime_summary
  svf_root="${SVF_ROOT:-$REPO_ROOT/SVFmemplus}"

  if [[ -n "$DOCKER_GPUS" ]]; then
    gpu_args=(--gpus "$DOCKER_GPUS")
    echo "gpu: docker --gpus $DOCKER_GPUS"
    require_docker_gpu
  else
    echo "gpu: disabled (--no-gpu)"
  fi

  if [[ "$BUILD_IMAGE" -eq 1 ]] || ! docker_image_exists; then
    echo "docker build: -f $DOCKERFILE -t $IMAGE"
    docker build -f "$DOCKERFILE" -t "$IMAGE" "$REPO_ROOT"
  fi

  if [[ "$SKIP_PREFLIGHT" -eq 0 ]]; then
    echo "preflight: inside container"
    docker run --rm \
      "${gpu_args[@]}" \
      --user "$(id -u):$(id -g)" \
      -e HOME=/tmp \
      --env-file "$ENV_FILE" \
      -e "SVF_ROOT=$svf_root" \
      -e "WORKFLOW_EXPECT_CUDA=$([ -n "$DOCKER_GPUS" ] && echo 1 || echo 0)" \
      -v "$REPO_ROOT:$REPO_ROOT" \
      -v "$svf_root:$svf_root" \
      -w "$REPO_ROOT" \
      "$IMAGE" \
      python3 -c '
import importlib, os, sys
from pathlib import Path
missing = []
for name in ("openai", "torch", "torch_geometric"):
    try:
        importlib.import_module(name)
    except Exception as exc:
        missing.append(f"{name}: {exc}")
if missing:
    print("error: missing or broken dependencies:", file=sys.stderr)
    for item in missing:
        print(f"  - {item}", file=sys.stderr)
    raise SystemExit(1)
print("ok: openai, torch, torch_geometric")
import torch
cuda_ok = torch.cuda.is_available()
print(
    f"ok: torch.cuda.is_available={cuda_ok} "
    f"device_count={torch.cuda.device_count()} "
    f"torch={torch.__version__} cuda_built={torch.version.cuda}"
)
if os.environ.get("WORKFLOW_EXPECT_CUDA") == "1" and not cuda_ok:
    print(
        "error: 已请求 GPU，但 torch.cuda 不可用；"
        "请确认 nvidia-container-toolkit 正常，并用 --build 重建 CUDA 版镜像",
        file=sys.stderr,
    )
    raise SystemExit(1)
root = Path(os.environ.get("SVF_ROOT", ""))
for binary in ("saber", "bof"):
    path = root / "Release-build" / "bin" / binary
    if not path.is_file() or not os.access(path, os.X_OK):
        print(f"error: missing executable: {path}", file=sys.stderr)
        raise SystemExit(1)
print("ok: saber, bof")
'
  fi

  if [[ -n "$HOST" && "$HOST" != "0.0.0.0" ]]; then
    echo "warning: docker 模式忽略 --host=$HOST，容器内固定监听 0.0.0.0" >&2
  fi

  echo "docker run: publish $publish -> container ${bind_host}:${PORT}"
  exec docker run --rm --init \
    "${gpu_args[@]}" \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    --env-file "$ENV_FILE" \
    -p "$publish" \
    -v "$REPO_ROOT:$REPO_ROOT" \
    -w "$REPO_ROOT" \
    "$IMAGE" \
    python3 -m local_ui \
      --config "$CONFIG_PATH" \
      --env-file "$ENV_FILE" \
      --host "$bind_host" \
      --port "$PORT"
}

parse_args() {
  [[ $# -ge 1 ]] || { usage; exit 2; }
  case "$1" in
    -h|--help) usage; exit 0 ;;
    start-server) shift ;;
    *) die "未知命令: $1（期望 start-server）" ;;
  esac
  [[ $# -ge 1 ]] || die "start-server 需要 local 或 docker"
  MODE="$1"
  shift
  case "$MODE" in
    local|docker) ;;
    *) die "start-server 模式必须是 local 或 docker，收到: $MODE" ;;
  esac

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --config)
        [[ $# -ge 2 ]] || die "--config 需要参数"
        CONFIG_PATH="$2"
        shift 2
        ;;
      --env-file)
        [[ $# -ge 2 ]] || die "--env-file 需要参数"
        ENV_FILE="$2"
        shift 2
        ;;
      --port)
        [[ $# -ge 2 ]] || die "--port 需要参数"
        PORT="$2"
        shift 2
        ;;
      --host)
        [[ $# -ge 2 ]] || die "--host 需要参数"
        HOST="$2"
        shift 2
        ;;
      --image)
        [[ $# -ge 2 ]] || die "--image 需要参数"
        IMAGE="$2"
        shift 2
        ;;
      --dockerfile)
        [[ $# -ge 2 ]] || die "--dockerfile 需要参数"
        DOCKERFILE="$2"
        shift 2
        ;;
      --build) BUILD_IMAGE=1; shift ;;
      --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
      --gpus)
        [[ $# -ge 2 ]] || die "--gpus 需要参数（如 device=0 / device=1 / all）"
        DOCKER_GPUS="$2"
        shift 2
        ;;
      --no-gpu)
        DOCKER_GPUS=""
        shift
        ;;
      -h|--help) usage; exit 0 ;;
      *) die "未知参数: $1" ;;
    esac
  done

  [[ "$PORT" =~ ^[1-9][0-9]*$ && "$PORT" -le 65535 ]] || die "非法端口: $PORT"
}

main() {
  parse_args "$@"
  case "$MODE" in
    local) start_local ;;
    docker) start_docker ;;
  esac
}

main "$@"
