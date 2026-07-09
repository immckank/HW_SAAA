"""FPhandler 全局配置：由 script/config.env 经 common.sh export 的环境变量驱动。

用法:
  source script/lib/common.sh && load_config
  cd FPhandler && python3 run.py --config ../script/config.py
"""
from __future__ import annotations

import os
_VALID = frozenset({"leak", "dfree", "uaf", "uninit", "bof"})
_CATEGORY_BY_DEFECT_TYPE = {
    "leak": "MEMORY_LEAK",
    "dfree": "DOUBLE_FREE",
    "uaf": "USE_AFTER_FREE",
    "uninit": "UNINIT_USE",
    "bof": "BUFFER_OVERFLOW",
}
_DEFAULT_DEFECT_TYPES = "leak,dfree,uaf,uninit"


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"缺少环境变量 {name}；请先 source script/lib/common.sh && load_config"
        )
    return value


def _abs(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _parse_defect_types(raw: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        token = part.strip()
        if not token or token in seen:
            continue
        if token not in _VALID:
            raise RuntimeError(
                f"未知 defect_type: {token}（合法: {', '.join(sorted(_VALID))}）"
            )
        seen.add(token)
        out.append(token)
    if not out:
        raise RuntimeError("defect_types 为空")
    return out


_bc = _abs(_require_env("bc"))
_out = _abs(_require_env("out"))
_src = _abs(_require_env("src"))
_stem = os.environ.get("stem", "").strip() or os.path.splitext(os.path.basename(_bc))[0]
_defect_types = _parse_defect_types(
    os.environ.get("defect_types", _DEFAULT_DEFECT_TYPES)
)
_svf_root = _abs(os.environ.get("svf_root", os.path.join(os.path.dirname(_bc), "..", "SVFmemplus")))

OUTPUT_DIR = _out
PROJECT_ROOT = _src
BITCODE_PATH = _bc
BC_STEM = _stem
DEFECT_TYPES = _defect_types
ALERT_CATEGORIES = [_CATEGORY_BY_DEFECT_TYPE[item] for item in _defect_types]

ALERT_DIR = os.path.join(_out, "alerts")

RUN_LOG_STEM = os.environ.get("project_label", "").strip() or _stem
RUN_SESSION_TIME_STR = None
PROJECT_LABEL = RUN_LOG_STEM
PROJECT_DESC = os.environ.get("project_desc", "")

RES_ROOT_PATH = os.path.join(_out, "fphandler")
SEMANTIC_RULE_REPOSITORY = os.path.join(_out, "semantic_rules.json")
ACTIVE_LEARNING_ROOT = os.path.abspath(
    os.environ.get(
        "active_learning_root",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "ActiveLearning"),
    )
)
ACTIVE_LEARNING_OUTPUT_DIR = os.path.join(_out, "active_learning")
ACTIVE_LEARNING_DATASET = os.path.join(
    ACTIVE_LEARNING_OUTPUT_DIR, "predict_dataset"
)
ACTIVE_LEARNING_PREDICTIONS = os.path.join(
    ACTIVE_LEARNING_OUTPUT_DIR, "predictions.csv"
)
ACTIVE_LEARNING_RANKING = os.path.join(ACTIVE_LEARNING_OUTPUT_DIR, "ranking.jsonl")
ACTIVE_LEARNING_FEEDBACK_ALERTS = os.path.join(
    ACTIVE_LEARNING_OUTPUT_DIR, "feedback_alerts.txt"
)
ACTIVE_LEARNING_LABELS = os.path.join(ACTIVE_LEARNING_OUTPUT_DIR, "labels.jsonl")

LLM_TYPE = os.environ.get("llm_type", "DeepSeek")
SVF_ROOT = _svf_root
ALERT_BATCH_SIZE = 8
BOF_BATCH_SIZE = 32
AGENT_MAX_TURNS = int(os.environ.get("agent_max_turns", "64"))
AGENT_CONCLUSION_RESERVE_TURNS = int(
    os.environ.get("agent_conclusion_reserve_turns", "10")
)
STATS_ONLY = False
