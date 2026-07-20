# 单项目 SVFmemplus 分析工作流

本文说明当前可执行工作流。详细设计边界见 `PLAN.md`，数据契约见
`contracts/README.md`。

## 1. 定位

主仓库按“一项目一配置、一项目一产物目录”工作。用户切换项目时切换
`workflow.ini`，无需项目数据库、任务队列或 Web 服务。

三个项目配置条目分别指向：

1. 一个待分析的 `.bc` 文件；
2. 对应源码目录；
3. 本项目的管理产物目录。

SVFmemplus、FPhandler 和 ActiveLearning 仍各自提供底层 CLI；正常工作流统一由
Python `orchestrator` 调用。Shell 只保留旧 `config.env` 和容器运行的兼容入口。

## 2. 配置

复制模板：

```bash
cp workflow.ini.example workflow.ini
```

配置只有一个 section 和三个键：

```ini
[project]
bitcode_path = object2_falconfs/bc_linked/single-target/xxx.bc
source_dir = object2_falconfs/source/FalconFS
artifact_dir = output/falconfs_ex
```

- `bitcode_path` 必须直接指向一个存在的 `.bc` 文件。
- `source_dir` 必须是存在的目录。
- `artifact_dir` 不存在时自动创建。
- 相对路径以 `workflow.ini` 所在目录为基准。
- section、缺少键或多余键都会被拒绝。
- LLM provider、API Key 和三个工具仓库的位置通过运行环境提供，不写入项目配置。

默认工具位置是主仓库下的 `SVFmemplus/`、`FPhandler/` 和 `ActiveLearning/`。
非标准布局可设置 `SVF_ROOT`、`FPH_ROOT`、`ACTIVE_LEARNING_ROOT`。

## 3. 管理产物

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

## 4. Warning

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

## 5. SVF 分析

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

## 6. FPhandler

### 6.1 只分类

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

### 6.2 分类并扩充语义库

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

## 7. ActiveLearning

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

`feedback=none` 只允许一轮。`feedback=fphandler` 每轮从当前排序中选最高 10 条和
最低 10 条，同一次调用中不重复选择；反馈只使用普通分类模式，不隐式扩充语义库。

每轮训练成功后保存不可变 `.pt` 和 manifest，再用新模型为所有
`suppressed=false` 的警报重新预测。只有 checkpoint 与对应 Warning 权重均提交成功后
才更新 `models/latest.json`。缺少可选警报、硬标签不足或训练跳过时提前终止，并在
结果中给出原因。

## 8. Python 服务接口

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

## 9. 旧 Shell 入口

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

新项目应优先直接使用 `workflow.ini` 和 Python CLI。容器相关挂载仍由旧 Shell 兼容层
处理。

## 10. 验证

```bash
python3 -m unittest discover -s tests -v
bash -n script/run_pipeline.sh script/run_active_learning_loop.sh \
  script/lib/orchestrator_compat.sh script/lib/common.sh
cmake --build SVFmemplus/Release-build --target saber bof -j2
```

检查实际项目时，先使用一个明确的 `.bc` 路径建立基线，再运行第二次普通 analyze，
即可同时验证配置、工具执行、状态记录和警报对账路径。
