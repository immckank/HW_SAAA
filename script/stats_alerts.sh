#!/usr/bin/env bash
# 统计 alerts 目录下各 classification 数量，并列出 TP / UN 警报文件路径。
#
# 用法:
#   ./script/stats_alerts.sh
#   ./script/stats_alerts.sh ./output/object1_memory_defects
#   ./script/stats_alerts.sh ./output/object1_memory_defects/alerts
#   ./script/stats_alerts.sh --alerts-dir ./output/object1_memory_defects
#
# 默认读取 config.env 的 out=，统计 $out/alerts。
# 显式传参时可传 out 根目录或 out/alerts，脚本会自动解析。
# 若目录不存在或无 JSON，说明路径配置有误。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/common.sh"

ALERTS_DIR=""

usage() {
  sed -n '2,10p' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --alerts-dir)
      [[ $# -ge 2 ]] || { echo "error: --alerts-dir 需要参数" >&2; exit 2; }
      ALERTS_DIR=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "error: 未知参数: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [[ -n "$ALERTS_DIR" ]]; then
        echo "error: 重复指定 alerts 目录" >&2
        exit 2
      fi
      ALERTS_DIR=$1
      shift
      ;;
  esac
done

load_config

resolve_alerts_dir() {
  local input="${1:-$out/alerts}"
  local resolved parent base

  if [[ ! -e "$input" ]]; then
    echo "error: 路径不存在: $input" >&2
    echo "hint: 应指向 config.env 的 out= 或其下的 alerts/" >&2
    return 1
  fi

  parent="$(cd "$(dirname "$input")" && pwd)"
  base="$(basename "$input")"
  resolved="$parent/$base"

  if [[ -d "$resolved/alerts" ]]; then
    resolved="$resolved/alerts"
  elif [[ "$base" != "alerts" ]]; then
    echo "error: 未找到 alerts 子目录: $resolved/alerts" >&2
    echo "hint: 目录结构应为 <out>/alerts/<category>/*.json" >&2
    return 1
  fi

  if [[ ! -d "$resolved" ]]; then
    echo "error: alerts 目录不存在: $resolved" >&2
    return 1
  fi

  printf '%s\n' "$resolved"
}

ALERTS_DIR="$(resolve_alerts_dir "${ALERTS_DIR:-}")"

python3 - "$ALERTS_DIR" <<'PY'
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(sys.argv[1])
LABELS = ("TP", "FP", "UN", "null")
JSON_TO_LABEL = {
    "TP": "TP",
    "FP": "FP",
    "UNCERTAIN": "UN",
    None: "null",
}


def normalize_classification(raw) -> str:
    if raw is None:
        return "null"
    text = str(raw).strip()
    if not text or text.lower() == "null":
        return "null"
    mapped = JSON_TO_LABEL.get(text)
    if mapped is None:
        return f"unknown:{text}"
    return mapped


def count_bucket() -> Counter:
    return Counter({label: 0 for label in LABELS})


paths = sorted(ROOT.glob("*/*.json"))
if not paths:
    print(f"error: 未找到警报 JSON: {ROOT}/*/*.json", file=sys.stderr)
    print("hint: 路径应形如 <out>/alerts/<category>/*.json", file=sys.stderr)
    raise SystemExit(1)

overall = count_bucket()
by_category: dict[str, Counter] = defaultdict(count_bucket)
tp_paths: list[str] = []
un_paths: list[str] = []
invalid = 0

for path in paths:
    category = path.parent.name
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        invalid += 1
        print(f"error: 无法读取 {path}: {error}", file=sys.stderr)
        continue

    label = normalize_classification(data.get("classification"))
    overall[label] += 1
    by_category[category][label] += 1

    if label == "TP":
        tp_paths.append(str(path.resolve()))
    elif label == "UN":
        un_paths.append(str(path.resolve()))

if invalid:
    raise SystemExit(1)

def print_counter(title: str, counter: Counter) -> None:
    print(title)
    for label in LABELS:
        print(f"  {label}: {counter[label]}")
    extras = sorted(key for key in counter if key not in LABELS and counter[key])
    for label in extras:
        print(f"  {label}: {counter[label]}")

print("=== Alert Classification Stats ===")
print(f"root:  {ROOT.resolve()}")
print(f"total: {len(paths)}")
print()
print_counter("Overall:", overall)
print()
print("By category:")
for category in sorted(by_category):
    print(f"  [{category}]")
    counter = by_category[category]
    for label in LABELS:
        print(f"    {label}: {counter[label]}")
    extras = sorted(key for key in counter if key not in LABELS and counter[key])
    for label in extras:
        print(f"    {label}: {counter[label]}")
print()
print(f"=== TP alerts ({len(tp_paths)}) ===")
for path in tp_paths:
    print(path)
print()
print(f"=== UN alerts ({len(un_paths)}) ===")
for path in un_paths:
    print(path)
PY
