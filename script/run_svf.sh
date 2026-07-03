#!/usr/bin/env bash
# 运行 SVFmemplus Saber/BOF（等价于 run_pipeline.sh --svf-only）。
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/run_pipeline.sh" --svf-only "$@"
