"""Read canonical warnings into compact table rows."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from orchestrator.services import WARNING_TYPES, require_warning

from .project import BoundProject


TYPE_ORDER = ("leak", "dfree", "uaf", "uninit", "bof")


def _compact_text(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _json_preview(value: Any, limit: int = 180) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(value)
    return _compact_text(text, limit)


def _location(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "unknown location"
    location = value.get("location")
    if isinstance(location, Mapping):
        file_name = str(location.get("file") or "?")
        line = location.get("line")
        column = location.get("column")
        result = f"{file_name}:{line}" if line is not None else file_name
        if column is not None:
            result += f":{column}"
    else:
        result = str(value.get("function") or value.get("role") or _json_preview(value, 80))
    role = value.get("role")
    if role:
        result += f" ({role})"
    return result


def summarize_content(warning_type: str, content: Any) -> str:
    if not isinstance(content, Mapping):
        return _json_preview(content)
    if warning_type in {"dfree", "uaf", "uninit"}:
        path = content.get("path")
        if isinstance(path, list) and path:
            first = _location(path[0])
            last = _location(path[-1])
            route = first if len(path) == 1 else f"{first} → {last}"
            return f"{route} · {len(path)} node(s)"
    elif warning_type == "leak":
        allocation = _location(content.get("allocation"))
        paths = content.get("paths")
        path_count = len(paths) if isinstance(paths, list) else 0
        return f"allocation {allocation} · {path_count} path(s)"
    elif warning_type == "bof":
        access = content.get("access")
        if isinstance(access, Mapping):
            parts = [_location(access)]
            if access.get("kind"):
                parts.append(f"kind={access['kind']}")
            if access.get("base"):
                parts.append(f"base={access['base']}")
            return " · ".join(parts)
    return _json_preview(content)


def summarize_graphs(graph_ids: Any) -> str:
    if graph_ids is None:
        return "未关联"
    if graph_ids == []:
        return "已处理，无图"
    return f"{len(graph_ids)} 个图"


def summarize_classifications(history: Any) -> str:
    if history is None:
        return "无"
    if not isinstance(history, list) or not history:
        return _json_preview(history)
    latest = history[-1] if isinstance(history[-1], Mapping) else {}
    parts = [f"{len(history)} 条", f"最新={latest.get('classification', '?')}"]
    if latest.get("source"):
        parts.append(f"source={latest['source']}")
    if latest.get("created_at"):
        parts.append(f"created={latest['created_at']}")
    reason = _compact_text(latest.get("reason"), 120)
    if reason:
        parts.append(f"reason={reason}")
    candidates = latest.get("semantic_candidates")
    if isinstance(candidates, list) and candidates:
        parts.append(f"candidates={len(candidates)}")
    return "；".join(parts)


def warning_row(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "alert_id": document["alert_id"],
        "producer": document["producer"],
        "type": document["type"],
        "content": summarize_content(str(document["type"]), document["content"]),
        "graph_ids": summarize_graphs(document["graph_ids"]),
        "suppressed": document["suppressed"],
        "classifications": summarize_classifications(document["classifications"]),
        "active_learning": document["active_learning"],
        "score": document["score"],
    }


class AlertTable:
    def __init__(self, project: BoundProject):
        self.project = project

    def load(self, warning_type: str = "all", order: str = "desc") -> dict[str, Any]:
        self.project.assert_unchanged()
        if warning_type != "all" and warning_type not in WARNING_TYPES:
            raise ValueError(f"unsupported warning type filter: {warning_type}")
        if order not in {"asc", "desc"}:
            raise ValueError(f"unsupported score order: {order}")

        alerts_root = self.project.config.artifact_dir / "alerts"
        documents: list[dict[str, Any]] = []
        alert_ids: dict[str, Path] = {}
        if alerts_root.is_dir():
            for path in sorted(alerts_root.rglob("*.json")):
                try:
                    with path.open(encoding="utf-8") as stream:
                        document = json.load(stream)
                    require_warning(document)
                except (OSError, json.JSONDecodeError, ValueError) as error:
                    raise ValueError(f"cannot load warning {path}: {error}") from error
                if path.parent.name != document["type"]:
                    raise ValueError(f"warning path/type mismatch: {path}")
                alert_id = str(document["alert_id"])
                if alert_id in alert_ids:
                    raise ValueError(
                        f"duplicate alert_id {alert_id}: {alert_ids[alert_id]} and {path}"
                    )
                alert_ids[alert_id] = path
                documents.append(document)

        total = len(documents)
        if warning_type != "all":
            documents = [item for item in documents if item["type"] == warning_type]
        rows = [warning_row(document) for document in documents]
        rows.sort(key=lambda row: str(row["alert_id"]))
        rows.sort(key=lambda row: float(row["score"]), reverse=order == "desc")
        return {
            "alerts": rows,
            "total": total,
            "filtered": len(rows),
            "type": warning_type,
            "order": order,
        }
