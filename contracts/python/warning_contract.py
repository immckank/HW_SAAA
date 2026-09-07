"""Shared helpers for the display-oriented warning document.

The warning has one immutable identity payload (producer, type and content),
three nullable downstream sections and one required boolean suppression state.
No version discriminator is stored in the document: incompatible documents are
rejected by shape.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping, MutableMapping


WARNING_TYPES = frozenset({"leak", "dfree", "uaf", "uninit", "bof", "tabular"})
CLASSIFICATIONS = frozenset({"TP", "FP", "UNCERTAIN"})

FPHANDLER_BASE = 0.5
FPHANDLER_TP_DELTA = 0.5
FPHANDLER_FP_DELTA = -0.25
ACTIVE_LEARNING_DEFAULT = 0.5
FPHANDLER_WEIGHT = 0.5
ACTIVE_LEARNING_WEIGHT = 0.5


def canonical_json(value: Any) -> str:
    """Return the canonical UTF-8 JSON representation used for identities."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def calculate_alert_id(producer: str, warning_type: str, content: Mapping[str, Any]) -> str:
    """Hash exactly the immutable warning identity payload."""
    identity = {"producer": producer, "type": warning_type, "content": content}
    digest = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def require_warning(document: Mapping[str, Any], *, verify_id: bool = True) -> None:
    """Check the invariants needed by every Python consumer."""
    if not isinstance(document, Mapping):
        raise ValueError("warning must be an object")
    required = {
        "alert_id", "producer", "type", "content", "graph_ids",
        "suppressed", "classifications", "active_learning", "score",
    }
    missing = required.difference(document)
    extra = set(document).difference(required)
    if missing:
        raise ValueError(f"warning missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise ValueError(f"warning has unknown fields: {', '.join(sorted(extra))}")

    producer = document["producer"]
    warning_type = document["type"]
    content = document["content"]
    if not isinstance(producer, str) or not producer.strip():
        raise ValueError("warning producer must be a non-empty string")
    if warning_type not in WARNING_TYPES:
        raise ValueError(f"unsupported warning type: {warning_type!r}")
    if not isinstance(content, Mapping) or not content:
        raise ValueError("warning content must be a non-empty object")
    if verify_id:
        expected = calculate_alert_id(producer, warning_type, content)
        if document["alert_id"] != expected:
            raise ValueError(f"warning alert_id does not match content: expected {expected}")

    graph_ids = document["graph_ids"]
    if graph_ids is not None and (
        not isinstance(graph_ids, list)
        or any(not isinstance(item, str) or not item for item in graph_ids)
        or len(set(graph_ids)) != len(graph_ids)
    ):
        raise ValueError("warning graph_ids must be null or a unique string array")
    if not isinstance(document["suppressed"], bool):
        raise ValueError("warning suppressed must be a boolean")
    history = document["classifications"]
    if history is not None and (not isinstance(history, list) or not history):
        raise ValueError("warning classifications must be null or a non-empty array")
    if isinstance(history, list):
        for entry in history:
            _require_classification(entry)
    active = document["active_learning"]
    if active is not None:
        if not isinstance(active, Mapping) or set(active) != {"weight", "model"}:
            raise ValueError("warning active_learning must contain only weight and model")
        _bounded_number(active["weight"], "active_learning.weight")
        if not isinstance(active["model"], str) or not active["model"]:
            raise ValueError("warning active_learning.model must be a non-empty string")
    score = _bounded_number(document["score"], "score")
    expected_score = calculate_score(document)
    if not math.isclose(score, expected_score, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError(f"warning score is stale: expected {expected_score}")


def new_warning(producer: str, warning_type: str, content: Mapping[str, Any]) -> dict[str, Any]:
    """Build a warning before any downstream workflow has run."""
    warning = {
        "alert_id": calculate_alert_id(producer, warning_type, content),
        "producer": producer,
        "type": warning_type,
        "content": dict(content),
        "graph_ids": None,
        "suppressed": False,
        "classifications": None,
        "active_learning": None,
        "score": 0.5,
    }
    require_warning(warning)
    return warning


def is_suppressed(document: Mapping[str, Any]) -> bool:
    return document.get("suppressed") is True


def fphandler_score(document: Mapping[str, Any]) -> float:
    value = FPHANDLER_BASE
    history = document.get("classifications")
    if not isinstance(history, list):
        return value
    for entry in history:
        if not isinstance(entry, Mapping):
            continue
        verdict = str(entry.get("classification") or "").upper()
        if verdict == "TP":
            value += FPHANDLER_TP_DELTA
        elif verdict == "FP":
            value += FPHANDLER_FP_DELTA
    return _clamp(value)


def active_learning_weight(document: Mapping[str, Any]) -> float:
    active = document.get("active_learning")
    if not isinstance(active, Mapping):
        return ACTIVE_LEARNING_DEFAULT
    weight = active.get("weight")
    if isinstance(weight, bool) or not isinstance(weight, (int, float)):
        return ACTIVE_LEARNING_DEFAULT
    value = float(weight)
    if not math.isfinite(value):
        return ACTIVE_LEARNING_DEFAULT
    return _clamp(value)


def calculate_score(document: Mapping[str, Any]) -> float:
    if is_suppressed(document):
        return 0.0
    return (
        FPHANDLER_WEIGHT * fphandler_score(document)
        + ACTIVE_LEARNING_WEIGHT * active_learning_weight(document)
    )


def recompute_score(document: MutableMapping[str, Any]) -> float:
    score = calculate_score(document)
    document["score"] = score
    return score


def _bounded_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"warning {field} must be a number")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"warning {field} must be between 0 and 1")
    return number


def _require_semantic_fact(value: Any, field: str) -> None:
    if not isinstance(value, Mapping) or set(value) != {"scope", "fact"}:
        raise ValueError(f"warning {field} entries must contain only scope and fact")
    if value["scope"] not in {
        "base_api", "safe_alloc", "safe_free", "value_range", "source_filter"
    }:
        raise ValueError(f"warning {field} entry has unsupported scope")
    if not isinstance(value["fact"], Mapping) or not value["fact"]:
        raise ValueError(f"warning {field} entry fact must be a non-empty object")


def _require_classification(value: Any) -> None:
    fields = {
        "classification", "reason", "source", "created_at", "round_id",
        "batch_id", "semantic_candidates",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("warning classification entry has invalid fields")
    if value["classification"] not in CLASSIFICATIONS:
        raise ValueError("warning classification entry has invalid classification")
    if not isinstance(value["reason"], str):
        raise ValueError("warning classification reason must be a string")
    if not isinstance(value["source"], str) or not value["source"]:
        raise ValueError("warning classification source must be a non-empty string")
    for field in ("created_at", "round_id", "batch_id"):
        if value[field] is not None and not isinstance(value[field], str):
            raise ValueError(f"warning classification {field} must be null or a string")
    candidates = value["semantic_candidates"]
    if not isinstance(candidates, list):
        raise ValueError("warning classification semantic_candidates must be an array")
    for candidate in candidates:
        _require_semantic_fact(candidate, "classification semantic_candidates")


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
