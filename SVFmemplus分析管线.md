# SVFmemplus 分析管线

## 概述

管线基础形态分为两步：**Step1 静态分析**（SVFmemplus）产出单警报 JSON；**Step2 LLM 分拣**（FPhandler）读取同一 JSON、调用 Agent 研判，并把结论写回原文件。

在 Step1 与 Step2 之上，**主动学习闭环**（`script/run_active_learning_loop.sh`）串联图导出、模型排序、反馈选择与 LLM 标注，形成可重复的多轮迭代：

```text
SVFmemplus 产出警报集合
  → ActiveLearning 对警报排序（rank-alerts）
  → 按排序选出部分警报待反馈（select-feedback；原设计为人工，现由 FPhandler LLM Agent 替代）
  → FPhandler 单条分类并追加 classifications[] 历史
  → collect-feedback 汇总 labels.jsonl
  → （多轮）用累积标签重训模型后重跑，排序逐步更精准
```

Saber 四类（leak / dfree / uaf / uninit）与 BOF 均使用统一的单警报 JSON，并由 FPhandler 研判。

---

## Step0 环境准备

### 被测程序

1. 编译被测程序，生成 LLVM bitcode（`.bc`）
2. 在 `script/config.env` 中配置 `bc=`、`src=`、`out=`

示例（Object2 FalconFS）：

| 配置项 | 示例值 |
|--------|--------|
| `bc` | `object2_falconfs/bc_linked/falconfs_ex.bc` |
| `src` | `object2_falconfs/source/FalconFS` |
| `out` | `output/falconfs_ex` |

源码中已植入相关缺陷，见 `object2_falconfs/object2-内存文件缺陷植入报告.md`。

### SVFmemplus 构建

Docker 镜像 `nf-image:llvm21`（见 `dockerfile.svfmemplus`）；主动学习验收镜像为 `SAAA:latest`（在同一 Dockerfile 上构建，预装 ActiveLearning 依赖）：

```bash
# 仅 SVFmemplus 构建
docker run --rm \
  -v "/path/to/SVFmemplus":/SVFmemplus \
  -w /SVFmemplus \
  nf-image:llvm21 \
  bash -lc 'source ./build.sh'

# 主动学习闭环（config.env 中 docker_name="SAAA:latest"）
docker build -f dockerfile.svfmemplus -t SAAA:latest .
```

或在已满足依赖的环境中：

```bash
cd SVFmemplus
source ./build.sh    # 仅首次构建
source ./setup.sh    # 每次使用前加载 PATH
```

---

## 1. 总体需求配置与运行模式

### 1.1 全局配置

复制并编辑 `script/config.env`：

```bash
cp script/config.env.example script/config.env
```

| 变量 | 含义 | 默认值 / 示例 |
|------|------|---------------|
| `ws` | 工作区根目录 | `script/` 的上级目录 |
| `bc` | 被测 bitcode 绝对或相对路径 | `object2_falconfs/bc_linked/falconfs_ex.bc` |
| `out` | 统一输出目录（SVF 与 FPhandler 共用） | `output/falconfs_ex` |
| `src` | 源码根目录（Docker 内挂载为 `/source/FalconFS`） | `object2_falconfs/source/FalconFS` |
| `defect_types` | 要运行的 checker，逗号分隔 | `leak,dfree,uaf,uninit` |
| `svf_root` | SVFmemplus 源码路径 | `$ws/SVFmemplus` |
| `docker_name` | Docker 镜像名；**空** = 全局本机执行；**非空** = SVF + FPhandler 均在同一容器内 | `""` 或 `nf-image:llvm21` |
| `fph_root` | FPhandler 路径 | `$ws/FPhandler` |
| `active_learning_root` | ActiveLearning 路径 | `$ws/ActiveLearning` |
| `llm_type` | LLM 后端：`DeepSeek` / `Qwen` / `Example` / `HW` | `DeepSeek` |
| `project_label` | 传给 Agent 的项目标识 | bc 文件名 stem |
| `project_desc` | 项目背景描述（可选） | 空 |
| `semantic_rules` | 已审核语义规则 JSON（可选，Saber 加载） | 空 |
| `deepseek_api_key` 等 | 对应 LLM 的 API Key | — |

`defect_types` 合法值：`leak`、`dfree`、`uaf`、`uninit`、`bof`。

### 1.2 运行模式

**本机模式（`docker_name=""`）**

- `run_pipeline.sh` / `run_svf.sh` 直接在宿主机执行
- SVF 阶段由 `ensure_saber_bof_env` 负责 `source setup.sh` 或 `source build.sh`
- FPhandler 的 `CommandCaller` 在 subprocess 内 `source setup.sh` 后启动 `graph-reader`（不污染 Python 进程，不唤起 Docker）

**容器模式（`docker_name` 非空）**

- 入口脚本启动**一个**容器，SVF 与 FPhandler **均在同一容器内**完成
- 挂载：`SVFmemplus`、`bc`、`out`、`src`（只读）、`FPhandler`、`script/`、可选 `semantic_rules`
- 容器内设置 `SABER_SOURCE_ROOT=/source/FalconFS`，供报告嵌入源码上下文
- 运行前会删除 `out/` 下旧格式产物（`*_report.json`、`*_slices.json`、`*.txt` 等），保证目录内只有统一的 `alerts/` 树

### 1.3 运行命令

```bash
# 仅 Step1：静态分析
./script/run_svf.sh

# 完整管线：SVF → FPhandler
./script/run_pipeline.sh

# 主动学习闭环一轮：SVF → heap graph export → 随机/配置模型推理
# → 警报排序 → 每类选一条 → FPhandler LLM 反馈 → classifications[] + labels.jsonl
# 详见下文「5. 主动学习闭环」
./script/run_active_learning_loop.sh

# 分步 / 调试
./script/run_pipeline.sh --svf-only          # 只跑 SVF
./script/run_pipeline.sh --fph-only          # 只跑 FPhandler（需 alerts/ 已存在）
./script/run_pipeline.sh --stats-only        # 只统计警报 JSON，不调 LLM
./script/run_pipeline.sh --force             # 强制重编 SVFmemplus 后再跑
./script/run_svf.sh --checkers leak,dfree    # 只跑指定 checker
./script/run_svf.sh --force                  # 强制重编后再跑 SVF

# 单独跑 FPhandler（需先 load_config）
source script/lib/common.sh && load_config
cd FPhandler && python3 run.py --config ../script/config.py
```

FPhandler 配置由 `script/config.py` 读取上述环境变量，无需再维护各 object 独立 config（旧版 `FPhandler/script/object*/config_*.py` 仅作历史参考）。

FPhandler 默认每批处理 8 条相关警报。磁盘中的 `sha256:` ID 继续作为稳定
canonical ID；Agent 使用 `B0001-A01` 形式的批内短 ID。conclusion 必须显式
携带一个或多个 ID，可将同结论应用到一组警报，也可分多轮提交；只有本批全部
警报均已分类后 Agent 循环才终止并写回。

### 1.4 分析目标

| 模块 | checker | 缺陷类型 |
|------|---------|----------|
| Saber | `-leak` | 内存泄漏（NeverFree / PartialLeak） |
| Saber | `-dfree` | 重复释放（DoubleFree） |
| Saber | `-uaf` | 释放后使用（UseAfterFree） |
| Saber | `-uninit` | 未初始化使用（Uninitialized Use） |
| BOF | `bof` | 缓冲区越界（BufferOverflow） |

另有 `graph-reader -stat=false <input.bc>` 供 FPhandler 运行时查询 IR / 源码细节。

---

## 2. SVFmemplus 警报产出形式

### 2.1 Saber 统一单警报 JSON（当前默认）

Saber 通过 `-report-dir=$out` 写入，每条告警一个文件：

```text
$out/alerts/
├── memory_leak/<sha256>.json
├── double_free/<sha256>.json
├── use_after_free/<sha256>.json
└── uninit_use/<sha256>.json
```

文件名 `<sha256>` 由告警稳定身份（category + 关键位置 + 报告类型等）哈希得到；`alert_id` 字段为 `sha256:<hash>`。

**公共字段**

| 字段 | 说明 |
|------|------|
| `alert_id` | 稳定 ID，形如 `sha256:fe384cf...` |
| `category` | `MEMORY_LEAK` / `DOUBLE_FREE` / `USE_AFTER_FREE` / `UNINIT_USE` |
| `evidence` | `memory_object`（类型、分配器、描述符等）+ `checker`（报告子类型、是否截断等） |
| `classification` | 初始为 `null`；FPhandler 写回 `TP` / `FP` / `UNCERTAIN` |
| `reason` | 初始为 `""`；FPhandler 写回研判理由 |
| `active_learning` | 主动学习元数据，包含图 ID、score、rank、match_status |
| `classifications` | 多轮分类历史数组；顶层 classification/reason 是最新结论缓存 |

**主动学习字段**

```json
{
  "active_learning": {
    "schema_version": "active-learning/v1",
    "graph_ids": [],
    "match_status": "unresolved",
    "score": null,
    "rank": null,
    "last_model": null
  },
  "classifications": []
}
```

`$out/active_learning/` 目录结构：

```text
active_learning/
├── predict_dataset/raw/<stem>/
│   ├── 0.node.csv
│   ├── 0.edge.csv
│   ├── idToGraph.csv
│   └── graph_index.csv
├── predictions.csv
├── ranking.jsonl
├── feedback_alerts.txt
└── labels.jsonl
```

CSV schema:

- node: `id,pattern,type,level,pointedBy`
- edge: `srcid,tgtid,type`
- `idToGraph.csv`: 无表头，每行一个 graph ID
- `graph_index.csv`: `graph_id,object_id,file,line,column,source_loc`，用于将告警证据位置回填到 `active_learning.graph_ids`

`edge.type` 与模型 `edge_type_vocab_size=100` 对齐：
`0=IntraDirectVF`，`1=IntraIndirectVF`，`2=CallDirVF`，`3=RetDirVF`，
`4=CallIndVF`，`5=RetIndVF`，`6=ThreadMHPIndirectVF`，`7..99` 保留。

**非 leak 类（dfree / uaf / uninit）**

- `path`：裁剪后的 SVFG 值流 witness，节点按时间顺序排列
- 节点常见 `role`：`allocation`、`object_origin`、`free` / `first_free` / `second_free`、`use`、`branch` 等
- 每个节点含 `location`（file/line/column）、`function`、`ir`，以及可选 `source_context`（源码片段）
- `branch` 节点带 `condition: true/false`，表示所需控制流分支

**leak 类（特殊结构）**

- `allocation`：分配点（单节点，含 `source_context`）
- `paths`：可能安全释放对象的路径列表；每条含 `outcome`、`condition`（路径条件）、`path`（witness 节点）
- `leak_condition`：安全条件并集的补集，表示构成泄漏的危险条件
- **不含** 单数 `path` 字段

**直接调用示例**

```bash
saber -leak   -report-dir=$out $bc
saber -dfree  -report-dir=$out $bc
saber -uaf    -report-dir=$out $bc
saber -uninit -report-dir=$out $bc

# 可选：加载人工审核通过的语义规则
saber -uninit \
  -saber-semantic-rules=$out/semantic_rules.approved.json \
  -report-dir=$out \
  $bc
```

终端 stdout 仅作运行日志，**不是**下游输入。

### 2.2 BOF 统一单警报 JSON

BOF 将最终去重后的 MUST/MAY 告警写入
`$out/alerts/buffer_overflow/<sha256>.json`。告警覆盖 GEP、memcpy/memmove、
memset 与 strcpy/strcat 等访问类型。

每条告警使用 `category: BUFFER_OVERFLOW`，不含 `path`。`access` 记录内存
访问位置、基址、访问类型、索引或长度表达式及最终访问值域；`buffer` 记录
合法容量值域；`variables` 记录参与推理的变量；`range_analysis` 按顺序记录
值域种子、归纳传播、guard 收窄、最终访问范围和边界比较。`evidence.checker`
保留 BOF 的 MUST/MAY 静态结论。

启用 BOF 只需在 `defect_types` 中追加 `bof`：

```bash
# config.env
defect_types=leak,dfree,uaf,uninit,bof
```

或直接：

```bash
bof -report-dir=$out $bc
```

BOF 在最终告警发射阶段直接生成上述格式，不经过报告后处理。

### 2.3 语义规则反馈（需求1 部分落地）

LLM 研判时通过 `semantic_candidates` 提出可复用的函数语义（如 uninit 场景下的 `initializer`）。FPhandler 会将候选**逐条追加**到 `$out/semantic_rules.json`（单一文件，`semantic-rules/v1`，`status: proposed`）；相同内容的规则按 id 去重，不会生成多份文件。

人工标记为 `approved` 后，由 Saber 通过 `-saber-semantic-rules=` 加载，用于提升后续静态分析精度。

---

## 3. FPhandler 消费警报的方式

### 3.1 输入发现

- 根目录：`OUTPUT_DIR/alerts`（即 `config.env` 的 `out=` + `/alerts`）
- 只扫描 `defect_types` 对应类别目录中的 `.json` 文件
- FPhandler 信任 checker 生成的告警结构，只校验自己写回的分类值

**不做的事：**

- 不读取 Saber 终端文本（`*.txt`）
- 不做跨文件 slice 对齐
- 不按源码位置猜测警报对应关系

### 3.2 研判流程

```
discover alerts/ → 加载 JSON → 跳过已有 classification 的条目
    → 按规则分批 → 启动 graph-reader → LLM Agent（工具调用循环）
    → 原子写回 classification + reason 到原 JSON
```

**批处理策略**（`ALERT_BATCH_SIZE=8`，可在 `script/config.py` 调整）：

| category | 分批键 |
|----------|--------|
| `USE_AFTER_FREE` | 同一 `free` 位置（file + line） |
| `UNINIT_USE` | 同一 `evidence.memory_object.type`（或 descriptor） |
| `BUFFER_OVERFLOW` | 同一 `access.kind`（GEP / memcpy / memset / strcpy） |
| `MEMORY_LEAK` / `DOUBLE_FREE` | 每条独立 |

- 单条和多条统一调用 `set_conclusion`
- 每次调用必须传入 1–N 个批内短 ID；同一次调用中的警报共享分类和理由
- 不同分类可分多次调用；只有批内所有 ID 均已分类后才结束循环并写回

**Agent 可用工具**（通过 `graph-reader` / 源码树）：

- `dump_source_snippet`、`dump_source_line`
- `find_current_function`、`find_function_body`、`find_callers`

Prompt 中直接嵌入完整警报 JSON；对 leak 类会说明 `paths` /
`leak_condition` 语义，对 BOF 会说明 `variables` / `range_analysis` 是按序
排列的值域推理证据。

### 3.3 输出与写回

写回字段（同文件、原子替换）：

```json
{
  "classification": "TP" | "FP" | "UNCERTAIN",
  "reason": "研判理由文本"
}
```

运行日志与辅助产物目录：

```text
$out/fphandler/          # RES_ROOT_PATH（分析 trace 日志）
$out/semantic_rules.json  # LLM 提出的语义规则候选（semantic-rules/v1，逐条追加）
```

LLM 在 `set_conclusion` 中返回的 `semantic_candidates` 会自动追加到 `$out/semantic_rules.json`（`status: proposed`）。人工审核后可导出 approved 规则供 Saber 加载：

```bash
python3 FPhandler/semantic_rules.py $out/semantic_rules.json \
  --approve <rule-id> \
  --export-approved $out/semantic_rules.approved.json
```

然后在 `config.env` 设置 `semantic_rules=$out/semantic_rules.approved.json` 后重跑静态分析。

**幂等性**：已有 `classification` 的非空警报会被跳过，可安全重复 `./script/run_pipeline.sh --fph-only`。

### 3.5 单警报分类与多轮历史

主动学习闭环要求同一条警报可被多次分类，且每次结论带时间戳留档。实现分两层：

| 组件 | 路径 | 职责 |
|------|------|------|
| `AlertDocument.write_classification` | `FPhandler/alert_document.py` | 追加 `classifications[]` 条目，并同步顶层 `classification` / `reason` |
| `SingleAlertClassifier` | `FPhandler/single_alert_classifier.py` | 对单条警报调用 Agent、取回结论后写入历史（供闭环与测试复用） |

每次成功分类追加一条历史，字段如下：

```json
{
  "classification": "TP",
  "reason": "研判理由",
  "source": "active-learning-fphandler",
  "created_at": "2026-07-09T12:00:00+00:00",
  "round_id": "round-20260709T120000Z",
  "batch_id": "B0001",
  "semantic_candidates": []
}
```

- `created_at`：UTC ISO8601 时间戳，标识本条分类发生时刻
- `round_id`：主动学习轮次 ID，由 `run_active_learning_loop.sh` 的 `ROUND_ID` 传入
- `source`：区分普通管线（`fphandler`）与闭环驱动（`active-learning-fphandler`）
- 顶层 `classification` / `reason` 始终镜像**最新**一条历史，兼容旧消费方

闭环模式下 `run.py` 接受额外参数，只处理选中警报并允许覆盖已有结论：

| 参数 | 含义 |
|------|------|
| `--alert-list PATH` | 换行分隔的警报 JSON 路径列表（来自 `feedback_alerts.txt`） |
| `--force-reclassify` | 即使已有顶层 `classification` 也重新分类 |
| `--round-id ID` | 写入 `classifications[].round_id` |
| `--classification-source NAME` | 写入 `classifications[].source` |

`run_active_learning_loop.sh` 为每轮设置：

```bash
FPH_ARGS=(
  --alert-list "$out/active_learning/feedback_alerts.txt"
  --force-reclassify
  --round-id "$ROUND_ID"
  --classification-source "active-learning-fphandler"
)
```

离线烟测可将 `llm_type=Example`，无需 API Key，选中警报会写入 `UNCERTAIN` 占位结论。

**只读统计**：

```bash
./script/run_pipeline.sh --stats-only
# 或
python3 FPhandler/run.py --config script/config.py --stats-only
```

### 3.4 配置要点（FPhandler 侧）

`script/config.py` 从环境变量映射：

| Python 变量 | 来源 |
|-------------|------|
| `OUTPUT_DIR` / `ALERT_DIR` | `out` / `out/alerts` |
| `BITCODE_PATH` | `bc` |
| `PROJECT_ROOT` | `src` |
| `LLM_TYPE` | `llm_type` |
| `SVF_ROOT` | `svf_root`（CommandCaller 定位 setup.sh） |
| `ALERT_BATCH_SIZE` | 默认 `8` |
| `AGENT_MAX_TURNS` | `agent_max_turns`，Agent 每批最大轮次，默认 `32` |
| `AGENT_CONCLUSION_RESERVE_TURNS` | `agent_conclusion_reserve_turns`，批末强制提交结论的保留轮次，默认 `10` |

API Key 通过 `load_config` 导出为 `DEEPSEEK_API_KEY` 等环境变量。

---

## 4. 五类告警统一性审计

### 4.1 已统一的接口

五类 checker 均直接生成 `$out/alerts/<category>/<sha256>.json`，公共外壳为：

```json
{
  "alert_id": "sha256:...",
  "category": "...",
  "classification": null,
  "reason": ""
}
```

- `alert_id` 同时作为文件名和跨次运行的身份；重跑时保留已有
  `classification` / `reason`。
- 每个 checker 只清理自己类别下已经消失的旧文件，不删除其他类别结果。
- FPhandler 通过 `AlertDocument` / `UnifiedAlert` 消费所选类别，统一完成发现、
  短 ID 映射、Agent 调用和原子写回。
- Saber 四类用 `path` 或 leak 的 `allocation + paths + leak_condition` 表达
  值流；BOF 用 `access + buffer + variables + range_analysis` 表达值域证明，
  不强制伪造 `path`。

### 4.2 公共实现

- `UnifiedAlertWriter` 统一完成 SHA-256、目录创建、旧分类保留、原子替换和
  本类别 stale 文件清理；Saber 与 BOF 只提供稳定身份和类别证据。
- `SourceEvidence` 统一解析 SVF 源码位置、映射源码根目录并读取上下文窗口。
- FPhandler 的类别行为表集中提供主位置、分批键和 prompt 说明，主流程不再包含
  按类别展开的条件链。
- `defect_types` 同时控制 checker 调度和 FPhandler 消费范围，其他类别的历史结果
  保留在输出目录中。
- BOF 正式报告由 `BofAlertReporter` 负责；可选 sidecar 只处理自身中间数据，
  FPhandler 是分类写回的唯一入口。
- 旧 `memory_defect.py` 消费模型已经删除，五类告警统一使用 `UnifiedAlert`。

### 4.3 失败语义

FPhandler 不补全、不猜测也不降级处理生产者字段。类别行为所需字段缺失、Agent
未完整分类或写回失败时，本次执行直接失败。告警结构的正确性由对应 checker
实现保证。

上述统一不改变 checker 的分析算法、告警字段、稳定身份或类别证据形状。

---

## 5. 主动学习闭环

### 5.1 阶段划分

闭环由 `script/run_active_learning_loop.sh` 驱动；各阶段函数定义在 `script/lib/pipeline.sh`，本机与容器（`docker_name` 非空时挂载 `script/` 为 `/pipeline/`）共用同一脚本。

| 阶段 | 函数 / 命令 | 输入 | 输出 |
|------|-------------|------|------|
| 1. 静态分析 | `run_svf_phase` | `$bc` | `$out/alerts/` |
| 2. 图导出 | `run_active_learning_export_phase` → `svf-al-export` | `$bc` | `$out/active_learning/predict_dataset/raw/<stem>/` |
| 3. 模型推理 | `run_active_learning_predict_phase` → `cli predict` | predict_dataset | `predictions.csv` |
| 4. 警报排序 | `run_active_learning_rank_phase` → `cli rank-alerts` | alerts + predictions | `ranking.jsonl`，并回填各警报 `active_learning.score/rank` |
| 5. 反馈选取 | `run_active_learning_select_feedback_phase` → `cli select-feedback` | ranking | `feedback_alerts.txt`（默认每类 1 条，`ACTIVE_LEARNING_FEEDBACK_PER_CATEGORY` 可调） |
| 6. LLM 标注 | `run_fph_phase`（带 `FPH_ARGS`） | feedback_alerts.txt | 各选中警报 `classifications[]` 新增条目 |
| 7. 标签汇总 | `run_active_learning_collect_feedback_phase` → `cli collect-feedback` | 已分类 alerts | `labels.jsonl` |

一轮完整命令：

```bash
# 默认 ROUND_ID=round-<UTC时间戳>
./script/run_active_learning_loop.sh

# 指定轮次 ID（多轮迭代时便于对齐训练数据）
ROUND_ID=round-002 ./script/run_active_learning_loop.sh
```

### 5.2 数据流与模块分工

```text
SVFIR + Andersen + SVFG
  └─ svf-al-export（CI heap object 值流邻域）
       └─ *.node.csv / *.edge.csv / idToGraph.csv / graph_index.csv
            └─ ActiveLearning RGCN（acceptance 可用 --random-weights）
                 └─ predictions.csv
                      └─ rank-alerts（ActiveLearning/alerts.py）
                           ├─ 警报证据位置 ↔ graph_index 匹配，回填 active_learning.graph_ids
                           └─ 按 score 排序，写出 ranking.jsonl
                                └─ select-feedback → feedback_alerts.txt
                                     └─ FPhandler Agent（替代人工反馈）
                                          └─ classifications[] + labels.jsonl
```

`ActiveLearning/alerts.py` 承担**警报 → 模型输入 / 排序参数**的转换与回填（将证据位置映射到 `heap:<object-id>` 图 ID、写回 score/rank）。该逻辑以模块函数实现，后续接入其他静态分析器时可在此扩展，而不改 SVFmemplus 警报 JSON 外壳。

### 5.3 人工反馈 → LLM Agent

原流程中 `select-feedback` 产出的警报需人工判 TP/FP。当前默认由 FPhandler 在同一容器/本机环境内完成：`--alert-list` 限定范围，`--force-reclassify` 允许同警报多轮追加历史。训练数据生成应读取 `classifications[]`（含 `created_at`、`round_id`），而非仅顶层 `classification`。

多轮迭代时：用 `labels.jsonl` 与历史 `classifications[]` 重训或微调 RGCN → 去掉 `--random-weights` 加载 checkpoint → 重跑 `run_active_learning_loop.sh`；每轮排序会随模型与标签累积而调整，`round_id` 区分各轮反馈。

### 5.4 验收（falconfs_ex 一轮五类）

针对 `object2_falconfs/bc_linked/falconfs_ex.bc`，`defect_types=leak,dfree,uaf,uninit,bof`：

1. 删除旧 `output/falconfs_ex` 后执行 `./script/run_active_learning_loop.sh`
2. `alerts/` 下五类均有产物；`active_learning/` 下图数据、排序、反馈列表齐全
3. `feedback_alerts.txt` 中每类至少 1 条；对应 JSON 的 `classifications[]` 含本轮 `round_id` 与 `created_at`
4. `labels.jsonl` 非空；`llm_type=Example` 时可无 API Key 跑通全流程

---

## 需求与后续

**需求1（部分实现）**：通过 Saber 语义规则接口，将 LLM 发现的初始化 / API 行为建模回静态分析器。

**需求2（已实现）**：五类告警均直接输出单警报 JSON；Saber 提供值流路径与条件，BOF 提供内存访问位置、参与变量和值域推理链。

**需求3（已实现）**：图模型排序模块消费 `alerts/` 中稳定 `alert_id` 与 `active_learning` 字段；`classifications[]` 记录多轮 LLM/人工反馈，供重训与更精准排序。

**后续**：在随机权重验收通过后，用真实 checkpoint 替换 `--random-weights`，并基于多轮 `labels.jsonl` 闭环重训。

---

## 目录关系速查

```text
openEuler分析流程/
├── script/
│   ├── config.env                  # 全局配置（bc / out / src / defect_types / LLM）
│   ├── config.py                   # FPhandler 配置入口
│   ├── run_svf.sh                  # Step1
│   ├── run_pipeline.sh             # Step1 + Step2
│   ├── run_active_learning_loop.sh # 主动学习闭环（一轮）
│   └── lib/pipeline.sh             # 各阶段函数（SVF / AL / FPhandler）
├── SVFmemplus/                     # 静态分析器源码（含 svf-al-export）
├── ActiveLearning/                 # 图模型推理、排序、标签汇总（cli + alerts.py）
├── FPhandler/                      # LLM 分拣（run.py、single_alert_classifier.py）
└── output/<run>/                   # 示例输出
    ├── alerts/                     # 五类单警报 JSON（权威数据源）
    ├── active_learning/            # 图数据、排序、反馈选取、labels.jsonl
    └── fphandler/                  # FPhandler 日志与语义规则
```
