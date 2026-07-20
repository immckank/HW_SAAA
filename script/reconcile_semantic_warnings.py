#!/usr/bin/env python3
"""Reconcile a staged analyzer result with the current warning collection.

The analyzer emits only warnings that are active under the current semantic
library.  Reconciliation retains disappeared warnings with ``suppressed=true``
and restores the mutable workflow fields of warnings that remain or reappear.
No semantic-fact attribution is stored.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

CONTRACTS_PYTHON = Path(__file__).resolve().parents[1] / "contracts" / "python"
if str(CONTRACTS_PYTHON) not in sys.path:
    sys.path.insert(0, str(CONTRACTS_PYTHON))

from warning_contract import recompute_score, require_warning  # noqa: E402


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"warning is not an object: {path}")
    require_warning(value)
    return value


def _warning_index(root: Path) -> dict[str, tuple[Path, dict[str, Any]]]:
    result: dict[str, tuple[Path, dict[str, Any]]] = {}
    if not root.is_dir():
        return result
    for path in sorted(root.rglob("*.json")):
        document = _read_json(path)
        alert_id = str(document["alert_id"])
        if alert_id in result:
            raise ValueError(f"duplicate alert_id {alert_id}: {path}")
        result[alert_id] = (path, document)
    return result


def _write_json(path: Path, document: dict[str, Any]) -> None:
    require_warning(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(tmp, path)


def reconcile(
    parent_root: Path,
    current_root: Path,
    _legacy_hits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Merge current analyzer output with the complete previous collection."""
    parent = _warning_index(parent_root)
    current = _warning_index(current_root)
    stats: dict[str, Any] = {
        "previous": len(parent),
        "generated": len(current),
        "surviving": 0,
        "reappeared": 0,
        "new": 0,
        "newly_suppressed": 0,
        "suppressed": 0,
        "current_after": 0,
    }

    for alert_id, (path, document) in current.items():
        previous = parent.get(alert_id)
        if previous is None:
            document["suppressed"] = False
            recompute_score(document)
            _write_json(path, document)
            stats["new"] += 1
            continue
        old = previous[1]
        was_suppressed = old["suppressed"] is True
        for field in ("graph_ids", "classifications", "active_learning"):
            document[field] = old[field]
        document["suppressed"] = False
        recompute_score(document)
        _write_json(path, document)
        if was_suppressed:
            stats["reappeared"] += 1
        else:
            stats["surviving"] += 1

    for alert_id, (old_path, document) in parent.items():
        if alert_id in current:
            continue
        if document["suppressed"] is not True:
            stats["newly_suppressed"] += 1
        document["suppressed"] = True
        recompute_score(document)
        relative = old_path.relative_to(parent_root)
        _write_json(current_root / relative, document)
        stats["suppressed"] += 1

    stats["current_after"] = len(_warning_index(current_root))
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-alerts", required=True, type=Path)
    parser.add_argument("--current-alerts", required=True, type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    stats = reconcile(args.parent_alerts, args.current_alerts)
    rendered = json.dumps(stats, ensure_ascii=False, indent=2)
    print(rendered)
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.summary.with_suffix(args.summary.suffix + ".tmp")
        tmp.write_text(rendered + "\n", encoding="utf-8")
        os.replace(tmp, args.summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
