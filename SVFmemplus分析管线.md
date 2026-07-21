# 单项目 SVFmemplus 分析工作流

本文说明当前可执行工作流。详细设计边界见 `PLAN.md`，数据契约见
`contracts/README.md`。

## 1. 定位

主仓库按“一项目一配置、一项目一产物目录”工作。用户切换项目时切换
`workflow.ini`，无需项目数据库、任务队列或 Web 服务。

项目配置指向：

1. 一个待分析的 `.bc` 文件；
2. 对应源码目录；
3. 本项目的管理产物目录；
4. 项目标签与描述（供 FPhandler Agent 提示使用）。

LLM、API Key、Agent 轮次和主动学习反馈选取参数属于运行环境，写在
`runtime.env`，不进入项目 INI。

SVFmemplus、FPhandler 和 ActiveLearning 仍各自提供底层 CLI；正常工作流统一由
Python `orchestrator` 调用。日常交互入口是本地 UI：

```bash
./script/run_local_ui.sh start-server local|docker
```

Shell 另保留旧 `script/config.env` 兼容包装；新项目优先使用 `workflow.ini` +
`runtime.env` + `run_local_ui.sh`。

## 2. 配置

工作流启动时读取两类本地文件（均被 `.gitignore` 忽略，只提交 `*.example`）：

| 文件 | 模板 | 内容 |
|------|------|------|
| `workflow.ini` | `workflow.ini.example` | 项目路径与标签 |
| `runtime.env` | `runtime.env.example` | LLM / Agent / 主动学习反馈等运行时参数 |

### 2.1 workflow.ini（项目）

```bash
cp workflow.ini.example workflow.ini
```

只允许一个 `[project]` section，且必须正好包含下列键：

```ini
[project]
bitcode_path = object2_falconfs/bc_linked/falconfs_ex.bc
source_dir = object2_falconfs/source/FalconFS
artifact_dir = output/falconfs_ex
project_label = falconfs_ex
project_desc =
```

- `bitcode_path` 必须直接指向一个存在的 `.bc` 文件。
- `source_dir` 必须是存在的目录。
- `artifact_dir` 不存在时自动创建。
- `project_label` 非空；写入 FPhandler 的 `PROJECT_LABEL`。
- `project_desc` 可为空白；写入 FPhandler 的 `PROJECT_DESC`。
- 相对路径以 `workflow.ini` 所在目录为基准。
- section、缺少键或多余键都会被拒绝。
- API Key、provider、模型、base URL、轮次、反馈 k 值不得写入本文件。

### 2.2 runtime.env（运行环境）

```bash
cp runtime.env.example runtime.env
chmod 600 runtime.env
```

Docker `--env-file` 兼容格式：`KEY=VALUE`，行首 `#` 为注释。主要字段：

```bash
LLM_TYPE=DeepSeek
# LLM_MODEL=deepseek-chat
# LLM_BASE_URL=https://api.deepseek.com
DEEPSEEK_API_KEY=
# QWEN_API_KEY=
# HW_KEY=

AGENT_MAX_TURNS=128
AGENT_CONCLUSION_RESERVE_TURNS=10

ACTIVE_LEARNING_FEEDBACK_TOP_K=10
ACTIVE_LEARNING_FEEDBACK_BOTTOM_K=10
ACTIVE_LEARNING_FEEDBACK_RANDOM_K=0
ACTIVE_LEARNING_FEEDBACK_RANDOM_SEED=42
ACTIVE_LEARNING_FEEDBACK_RANDOM_STRATEGY=plain
ACTIVE_LEARNING_FEEDBACK_SKIP_CLASSIFIED=0

# SVF_ROOT=
# FPH_ROOT=
# ACTIVE_LEARNING_ROOT=
```

- `LLM_TYPE`：`DeepSeek` / `Qwen` / `HW` / `Example`；按类型填写对应 Key。
- `LLM_MODEL` / `LLM_BASE_URL`：可选，覆盖 provider 默认模型与 OpenAI-compatible 地址。
- `HW` 必须设置 `LLM_BASE_URL`。
- 主动学习 `feedback=fphandler` 时按 top / bottom / random k 选样（重叠去重）。
- 默认工具根目录是仓库下的 `SVFmemplus/`、`FPhandler/`、`ActiveLearning/`；非标准布局用 `SVF_ROOT` 等覆盖。

旧 `script/config.env` 使用小写变量和 Bash 赋值，不适合直接传给
`docker --env-file`；新 UI 路径只认 `runtime.env`。

## 3. 本地 UI 服务（核心入口）

核心脚本：`script/run_local_ui.sh`。Docker 兼容入口
`script/run_local_ui_docker.sh` 等价于 `start-server docker`。

### 3.1 启动时读取的文件

| 路径 | 必需 | 用途 |
|------|------|------|
| `workflow.ini` | 是 | 绑定项目；传给 `python3 -m local_ui --config` |
| `runtime.env` | 是 | 导出 / `--env-file` 注入 LLM、Agent、反馈选取等环境变量 |
| `dockerfile.svfmemplus` | docker 且需构建时 | `docker build -f … -t openeuler-workflow:latest` |
| `SVFmemplus/Release-build/bin/saber` | 预检默认开启 | SVF 可执行文件存在性检查 |
| `SVFmemplus/Release-build/bin/bof` | 预检默认开启 | 同上 |
| 仓库根目录本身 | docker | `-v "$REPO:$REPO"` 同路径挂载，保证 INI 内绝对/相对路径在容器内仍有效 |

`local` 模式还会用本机 `python3` 预检 `openai`、`torch`、`torch_geometric`；
`docker` 模式在镜像内做同样预检。

### 3.2 用法

```bash
# 本机（默认监听 127.0.0.1:8765）
./script/run_local_ui.sh start-server local

# 容器（容器内 0.0.0.0，仅发布到宿主机 127.0.0.1:8765）
./script/run_local_ui.sh start-server docker --build

# 常用选项
./script/run_local_ui.sh start-server local \
  --config /path/to/workflow.ini \
  --env-file /path/to/runtime.env \
  --port 8765 \
  --host 127.0.0.1
```

| 选项 | 说明 |
|------|------|
| `--config` | `workflow.ini`，默认 `<repo>/workflow.ini` |
| `--env-file` | `runtime.env`，默认 `<repo>/runtime.env` |
| `--port` | 端口，默认 `8765` |
| `--host` | 仅 local 有效，默认 `127.0.0.1`；docker 固定容器内 `0.0.0.0` |
| `--image` | 镜像名，默认 `openeuler-workflow:latest` |
| `--dockerfile` | 默认 `dockerfile.svfmemplus` |
| `--build` | docker 启动前强制构建镜像 |
| `--skip-preflight` | 跳过 Python 依赖与 SVF 二进制预检 |

浏览器打开 `http://127.0.0.1:8765`。无图形界面时用 SSH 转发：

```bash
ssh -L 8765:127.0.0.1:8765 USER@HOST
```

UI 启动时绑定一份 `workflow.ini`；运行期若修改该文件需重启 UI。页面可查看
Warning、按 score / 类型筛选，并提交 analyze、triage、active-learning 操作
（经 `local_ui` → `orchestrator` 服务）。

也可不经包装脚本直接启动（需自行 `export` / 注入与 `runtime.env` 相同的环境变量）：

```bash
python3 -m local_ui --config workflow.ini --host 127.0.0.1 --port 8765
```

## 4. 管理产物

```text
<artifact_dir>/
├── semantic_facts.json
├── alerts/
│   ├── leak/<digest>.json
│   ├── dfree/<digest>.json
│   ├── uaf/<digest>.json
│   ├── uninit/<digest>.json
│   └── bof/<digest>.json
├── graphs/
│   ├── manifest.json
│   └── predict_dataset/
├── models/
│   ├── <active-learning-operation>/
│   │   ├── round-001.pt
│   │   └── round-001.json
│   └── latest.json
└── .orchestrator/
    ├── state.json
    ├── lock
    ├── logs/
    └── staging/
```

源码和 BC 不要求复制进该目录。`state.json` 只保存当前基线身份和最后一次成功
分析的信息；它不是数据库，也不保存完整运行快照。

同一 `artifact_dir` 同时只允许一个写操作。分析器和模型先写 staging，验证成功后
再替换正式产物。

## 5. Warning

Warning 顶层字段固定为：

```json
{
  "alert_id": "sha256:...",
  "producer": "svfmemplus",
  "type": "uaf",
  "content": {},
  "graph_ids": null,
  "suppressed": false,
  "classifications": null,
  "active_learning": null,
  "score": 0.5
}
```

`alert_id` 只由规范化的 `{producer,type,content}` 计算。图、分类、主动学习权重和
抑制状态不参与身份。

`suppressed` 是必填布尔值：

- 新产生或本次仍存在的警报为 `false`；
- 同一基线下，上次存在但本次分析消失的警报保留并设为 `true`；
- 已抑制警报以后重新出现时恢复为 `false`。

工作流不保存 semantic fact 到警报的命中归因，也不管理 semantic-hit 日志。
`suppressed=true` 时 `score=0`，FPhandler 和 ActiveLearning 都跳过该警报。

`classifications` 是只追加历史；`active_learning` 只保存最新
`{weight,model}`。详细字段和计分规则见 `contracts/README.md`。

## 6. SVF 分析

首次分析：

```bash
python3 -m orchestrator --config workflow.ini analyze
```

只运行部分 checker：

```bash
python3 -m orchestrator --config workflow.ini analyze \
  --checkers leak,dfree,uaf,uninit
```

每次分析都会加载 `artifact_dir/semantic_facts.json` 的最新内容，在 staging 中运行
指定 checker，并与正式 `alerts/` 内的上一次警报按 `alert_id` 对账。所有外部命令
成功且新 Warning 全部通过契约校验后才提交。

以下内容构成基线身份：

- BC 解析后路径和 SHA-256；
- 源码目录；
- checker 集合；
- 对应 SVF 可执行文件哈希。

checker 按集合比较，命令行中的排列顺序不会建立新基线；`workflow.ini` 文件自身的
位置也不属于基线身份。

这些内容变化时，普通分析会拒绝运行。用户确认切换分析基线后执行：

```bash
python3 -m orchestrator --config workflow.ini analyze --new-baseline
```

新基线替换警报和图关联结果，但保留项目语义库及已有模型 checkpoint。第一次接管
旧 `output` 时也应使用 `--new-baseline`，旧 Warning 不做隐式形状迁移。

## 7. FPhandler

### 7.1 只分类

```bash
python3 -m orchestrator --config workflow.ini triage \
  --mode classify \
  --alerts sha256:... sha256:...
```

也可重复使用 `--alert`，或从每行一个 ID 的文件读取：

```bash
python3 -m orchestrator --config workflow.ini triage \
  --mode classify --alerts-file selected-alerts.txt
```

编排器在调用 Agent 前去重并验证所有 ID；未知 ID 直接拒绝，已抑制警报列入结果的
`skipped_suppressed`。普通分类提示和工具接口不允许生成语义候选。

Agent 使用的 `PROJECT_LABEL` / `PROJECT_DESC` 来自 `workflow.ini`；`LLM_TYPE`、
API Key、`LLM_MODEL`、`LLM_BASE_URL`、`AGENT_MAX_TURNS`、
`AGENT_CONCLUSION_RESERVE_TURNS` 来自 `runtime.env`（或等价环境变量）。

### 7.2 分类并扩充语义库

```bash
python3 -m orchestrator --config workflow.ini triage \
  --mode expand-semantics \
  --alerts sha256:... sha256:...
```

FPhandler 在分类时可生成最小 `{"scope":"...","fact":{...}}` 候选。候选只写入
staging 语义库，并经过固定五类 scope 的强类型校验、规范化去重和值域冲突检查。

本批完成、部分完成或 Agent 以其他方式终止后，只要确有新合法 fact，编排器就触发
恰好一次 SVF 重分析：

- 重分析成功：一起提交新语义库和对账后的警报；
- 重分析失败：正式语义库和警报集保持原状，已经写回的分类历史仍保留；
- 没有新增 fact：不重分析。

项目语义库固定为 `semantic-fact/v2`，只有 `base_api`、`safe_alloc`、
`safe_free`、`value_range` 和 `source_filter` 五个 scope。空库模板和 Schema 位于
`contracts/examples/semantic-fact-v2.json` 与
`contracts/schemas/semantic-fact-v2.schema.json`。

## 8. ActiveLearning

主动学习不会随普通流程自动执行，必须显式调用。

只用初始模型为全部活动警报生成权重：

```bash
python3 -m orchestrator --config workflow.ini active-learning \
  --rounds 1 --feedback none --initial-model random
```

带 FPhandler 反馈的多轮执行：

```bash
python3 -m orchestrator --config workflow.ini active-learning \
  --rounds 5 --feedback fphandler --initial-model latest
```

`--initial-model` 接受：

- `random`：固定随机种子的可重现初始权重；
- `latest`：读取 `models/latest.json`；
- checkpoint 路径：使用指定模型。

`feedback=none` 只允许一轮。`feedback=fphandler` 每轮按 `runtime.env` 中的
`ACTIVE_LEARNING_FEEDBACK_*` 从当前排序选样（默认 top 10 + bottom 10，
`RANDOM_K=0`）；同一次调用中不重复选择。反馈只使用普通分类模式，不隐式扩充语义库。

每轮训练成功后保存不可变 `.pt` 和 manifest，再用新模型为所有
`suppressed=false` 的警报重新预测。只有 checkpoint 与对应 Warning 权重均提交成功后
才更新 `models/latest.json`。缺少可选警报、硬标签不足或训练跳过时提前终止，并在
结果中给出原因。

## 9. Python 服务接口

后续本地界面应直接调用三个服务，而不是解析 Shell 输出：

```python
from orchestrator import (
    ActiveLearningRequest,
    AnalyzeRequest,
    TriageRequest,
    analyze,
    run_active_learning,
    triage,
)
```

```text
analyze(AnalyzeRequest, progress=None, cancel=None) -> AnalyzeResult
triage(TriageRequest, progress=None, cancel=None) -> TriageResult
run_active_learning(ActiveLearningRequest, progress=None, cancel=None) -> ActiveLearningResult
```

Result 可直接 JSON 序列化。`progress` 接收阶段事件；`cancel` 可使用带 `is_set()` 的
对象，取消时编排器终止当前外部进程组。

CLI 的配置/参数错误退出码为 2，执行失败或部分失败为 1，完整成功为 0。进度写入
stderr，最终 Result JSON 写入 stdout，完整外部命令输出写入
`.orchestrator/logs/`。

`local_ui` 后台操作正是调用上述三个服务；推荐日常通过
`./script/run_local_ui.sh` 启动 UI，而不是手写 HTTP 客户端。

## 10. 旧 Shell 入口

旧入口仍可读取 `script/config.env`：

```bash
./script/run_pipeline.sh
./script/run_pipeline.sh --svf-only
./script/run_pipeline.sh --fph-only --expand-semantics
./script/run_active_learning_loop.sh \
  --rounds 5 --feedback fphandler --initial-model random
```

它们只把旧 `bc/src/out` 映射为临时 `workflow.ini` 并调用 Python orchestrator。
`script/lib/pipeline.sh` 和 `script/lib/run_checkers.sh` 是未被新入口加载的旧阶段实现，
不得作为新的业务接口。

新项目应优先使用 `workflow.ini`、`runtime.env` 和
`./script/run_local_ui.sh start-server …`。

## 11. 验证

```bash
python3 -m unittest discover -s tests -v
bash -n script/run_local_ui.sh script/run_local_ui_docker.sh \
  script/run_pipeline.sh script/run_active_learning_loop.sh \
  script/lib/orchestrator_compat.sh script/lib/common.sh
cmake --build SVFmemplus/Release-build --target saber bof -j2
```

检查实际项目时：先准备 `workflow.ini` 与 `runtime.env`，用
`./script/run_local_ui.sh start-server local` 打开 UI，或 CLI 对一个明确的 `.bc`
建立基线后再跑第二次普通 analyze，即可同时验证配置、工具执行、状态记录和警报对账路径。
