# 单项目告警分析工作流计划

## 1. 当前定位

主仓库是一个单机、单项目的分析工作流，不是多租户或多项目并发运行的平台。用户通过选择不同的基础配置切换项目。

当前实施边界：

1. SVFmemplus 加载项目 `semantic-fact/v2` 并产生 Warning。
2. FPhandler 对用户指定的 Warning 进行分类，可选生成语义 fact。
3. ActiveLearning 仅在用户显式调用时执行，为活动 Warning 回写权重。
4. `orchestrator` Python 包管理阶段、文件一致性和外部进程，Shell 仅保留兼容入口。
5. 不引入数据库、任务队列、HTTP API、用户权限或项目注册表。

## 2. 仓库职责

| 位置 | 职责 |
| --- | --- |
| `contracts/` | Warning 和 semantic fact 契约、稳定 ID 和共享计分函数 |
| `SVFmemplus/` | C/C++ 程序分析、语义 fact 消费、Warning 和图导出 |
| `FPhandler/` | Agent 告警研判、分类历史追加和语义候选生成 |
| `ActiveLearning/` | 图构建、模型预测、反馈选择和训练 |
| `orchestrator/` | 配置、基线、文件事务、阶段编排与 CLI |
| `script/` | 旧 `config.env` 和容器启动的兼容包装 |

业务依赖方向保持为：

```text
contracts <- SVFmemplus / FPhandler / ActiveLearning
contracts <- orchestrator -> SVFmemplus / FPhandler / ActiveLearning CLI
simple UI -> orchestrator Python services
```

## 3. 基础配置与数据目录

### 3.1 workflow.ini

配置仅保存三个固化条目：

```ini
[project]
bitcode_path = /path/to/project/program.bc
source_dir = /path/to/project/source
artifact_dir = /path/to/project/analysis-data
```

约束：

- 只允许 `[project]` 和上述三个键；未知键直接拒绝。
- 相对路径以 INI 文件所在目录为基准。
- `bitcode_path` 必须直接指向一个存在的 `.bc` 文件，不扫描或猜测目录内目标。
- `source_dir` 必须存在；`artifact_dir` 可由编排器创建。
- LLM Key、provider、工具根目录和容器信息是运行环境，不进入项目数据。

### 3.2 管理产物

```text
<artifact_dir>/
  semantic_facts.json
  alerts/<type>/<digest>.json
  graphs/
    manifest.json
    predict_dataset/
  models/
    <active-learning-run-id>/
      round-001.pt
      round-001.json
    latest.json
  .orchestrator/
    state.json
    lock
    staging/
    logs/
```

- `state.json` 仅记录 BC 路径与哈希、源码目录、checker 集合、SVF 可执行文件哈希、语义库哈希和最后成功操作，不是用户配置；INI 文件自身路径不属于基线身份。
- 每次批量更改在 `.orchestrator/staging/<operation-id>` 完成，验证后再替换 canonical 目录。
- 同一 `artifact_dir` 只允许一个写操作，由文件锁保护。
- 不保存告警运行快照或数据库实体；分类历史在 Warning 内，模型历史为每轮 checkpoint。

## 4. Warning 契约

Warning 顶层字段固定为：

```text
alert_id
producer
type
content
graph_ids
suppressed
classifications
active_learning
score
```

关键语义：

- `alert_id` 是规范化 `{producer,type,content}` 的 SHA-256；可变阶段字段不参与身份。
- `graph_ids=null` 表示未关联，`[]` 表示已执行但无图，非空数组为实际关联。
- `suppressed` 是必填布尔值。新警报为 `false`；旧警报在新分析结果中消失时为 `true`；重现时回到 `false`。
- 不保存具体由哪条 semantic fact 压缩告警，也不管理 semantic-hit 产物。
- `classifications` 只追加分类记录，不覆盖旧结果。
- `active_learning` 只保存最新 `{weight,model}`，`model` 是 checkpoint 内容哈希或可重现的随机模型标识。
- `suppressed=true` 时 `score=0`，且 FPhandler 和 ActiveLearning 跳过该警报。
- 其余分值保持现有公式：FPhandler 分量从 0.5 起，TP +0.5、FP -0.25、UNCERTAIN +0，与模型 weight 各占 0.5。

## 5. semantic-fact/v2

语义库仍是项目级单 JSON，且只有五个 scope：

1. `base_api`：项目 alloc/free/mem_transfer API。
2. `safe_alloc`：特定有序调用上下文中安全管理的 allocation。
3. `safe_free`：特定有序调用上下文中安全的 free。
4. `value_range`：BOF 稳定 variable key 的权威整数闭区间。
5. `source_filter`：checker 在 source/seed 选择时忽略的项目相对路径。

fact 不增加 ID、revision、status、priority、时间、provenance 或通用 effect/condition。FPhandler 候选只使用 `{"scope":"...","fact":{...}}`，经强类型校验和规范化去重后生效。候选来源保留在当次 classification 中。

## 6. Python 服务与 CLI

唯一业务入口：

```python
analyze(AnalyzeRequest, progress=None, cancel=None) -> AnalyzeResult
triage(TriageRequest, progress=None, cancel=None) -> TriageResult
run_active_learning(ActiveLearningRequest, progress=None, cancel=None) -> ActiveLearningResult
```

CLI 是上述服务的薄包装：

```bash
python -m orchestrator --config workflow.ini analyze \
  [--checkers leak,dfree,uaf,uninit,bof] [--new-baseline]

python -m orchestrator --config workflow.ini triage \
  --mode classify|expand-semantics \
  (--alerts ALERT_ID ... | --alerts-file PATH)

python -m orchestrator --config workflow.ini active-learning \
  --rounds N --feedback none|fphandler \
  --initial-model random|latest|PATH
```

- Result 是可 JSON 序列化的 dataclass，包含 operation ID、数量、跳过/失败原因、checkpoint 和重分析结果。
- `progress` 回调产生阶段事件，供后续本地界面显示；`cancel` 用于终止外部进程组。
- CLI 配置/参数错误返回 2，执行或部分失败返回 1，完整成功返回 0。

## 7. 工作流

### 7.1 SVF 分析

1. 严格校验 INI、`bitcode_path`、目录和语义库。
2. 核对 BC SHA-256、源码目录、checker 集合和 SVF 可执行文件哈希。
3. 在 staging 中使用最新 `semantic_facts.json` 运行所有指定 checker。
4. 只有所有 checker 成功且 Warning 契约验证通过后才对账。
5. `旧 ID - 新 ID` 保留旧 Warning 并置 `suppressed=true`；新结果中的 ID 置 `false`；全新 ID 直接加入。
6. 相同 ID 保留已有图、分类和模型权重。
7. BC、源码目录、checker 集合或 SVF 版本变化时需 `--new-baseline`；新基线替换警报和图，但保留语义库和模型 checkpoint。

### 7.2 FPhandler

- `classify`：只对指定告警追加分类；提示和工具契约不包含 semantic candidates，不写语义库。
- `expand-semantics`：分类的同时生成候选 fact；FPhandler 只更新 staging 语义库。
- 所有选中警报结束、部分失败或 Agent 正常终止后，若有新合法 fact，只触发一次 SVF 重分析。
- 重分析成功后才一起提交新语义库和新警报集；失败时保留旧语义库/告警，已写入的分类仍有效。
- 无效 alert ID 在调用 Agent 前拒绝；重复 ID 去重；被压缩警报跳过并在 Result 中列出。

### 7.3 ActiveLearning

- 只处理 `suppressed=false` 的 Warning，为它们全部回写最新 `{weight,model}`。
- `feedback=none` 时要求 `rounds=1`，只执行一次预测和权重回写。
- `feedback=fphandler` 先用初始模型预测，每轮选择 weight 最高 10 条和最低 10 条，同一次运行不重复选择。
- ActiveLearning 反馈使用 FPhandler `classify` 模式，不隐式扩充语义库。
- 每轮训练后保存不可变 checkpoint 和 manifest，再用新模型预测全部活动警报。
- `models/latest.json` 只在 checkpoint 和对应 Warning 权重均成功提交后更新。
- 没有可选警报、缺少 TP/FP 硬标签或训练跳过时提前结束并返回原因。

## 8. 测试与验收

必须覆盖：

- INI 相对路径、未知键、缺少目录和 `bitcode_path` 非 `.bc`/不存在。
- Warning 布尔压缩契约、计分和下游跳过行为。
- 首次基线、消失、重现、新警报、基线身份变化和显式新基线。
- checker 失败、Warning 校验失败时 canonical 数据不变。
- FPhandler 两种模式、候选去重、无新 fact 不重跑、部分结束后单次重分析。
- ActiveLearning 无反馈单轮、有反馈多轮、压缩警报跳过、每轮 checkpoint 和最终模型哈希。
- service Result、progress 回调、CLI 参数和退出码。

## 9. 兼容与迁移

- `workflow.ini.example` 是新配置模板。
- `script/run_pipeline.sh` 和 `script/run_active_learning_loop.sh` 仍能读取旧 `config.env`，但只将 `bc/src/out` 映射为临时 INI 并调用 orchestrator。
- 第一次在旧产物目录使用新编排器时需 `--new-baseline`，旧 Warning 外壳不做隐式迁移。
- Shell 不再根据产物存在性跳过阶段，不生成 `.changed` 标记，不维护轮次或警报对账状态。

## 10. 远期可视化与平台化（非当前实施范围）

首选演进是简单本地界面：

- 选择或编辑 `workflow.ini`。
- 通过按钮直接调用三个 Python service。
- 使用 progress callback 显示阶段和日志。
- 读取 `alerts/`、`semantic_facts.json`、图 manifest 和模型 manifest 进行展示与编辑。

只有出现以下需求时才启动原平台化思路：

- 多用户或权限隔离。
- 远程分析和长期后台 worker。
- 多项目并发运行。
- 需要可查询的大量运行历史或外部分析器批量导入。

届时可以在不改变 Warning 和 semantic fact 契约的前提下增加 FastAPI、持久化实体、worker 和 Web 页面。远期组件不得反向引入 rule ID、revision、审批流程或通用语义规则引擎。
