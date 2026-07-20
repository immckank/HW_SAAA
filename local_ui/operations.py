"""Single-operation background adapter around the orchestrator services."""
from __future__ import annotations

import copy
import threading
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from orchestrator import (
    ActiveLearningRequest,
    AnalyzeRequest,
    TriageRequest,
    analyze,
    run_active_learning,
    triage,
)
from orchestrator.services import VALID_CHECKERS

from .project import BoundProject


class OperationBusyError(RuntimeError):
    """Raised when a second operation is submitted while one is running."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _require_mapping(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("operation parameters must be a JSON object")
    return payload


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    text = value.strip()
    return text or None


def _required_text(value: Any, field: str) -> str:
    result = _optional_text(value, field)
    if result is None:
        raise ValueError(f"{field} must not be empty")
    return result


class OperationManager:
    def __init__(
        self,
        project: BoundProject,
        *,
        services: Mapping[str, Callable[..., Any]] | None = None,
    ):
        self.project = project
        self._services = dict(
            services
            or {
                "analyze": analyze,
                "triage": triage,
                "active-learning": run_active_learning,
            }
        )
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel: threading.Event | None = None
        self._state: dict[str, Any] = {
            "status": "idle",
            "submission_id": None,
            "kind": None,
            "started_at": None,
            "completed_at": None,
            "progress": [],
            "result": None,
            "error": None,
        }

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state["status"] == "running"

    @staticmethod
    def _alert_ids(value: Any) -> tuple[str, ...]:
        if not isinstance(value, str):
            raise ValueError("alert_ids must be a newline-separated string")
        identifiers = [line.strip() for line in value.splitlines() if line.strip()]
        identifiers = list(dict.fromkeys(identifiers))
        if not identifiers:
            raise ValueError("at least one alert_id is required")
        return tuple(identifiers)

    def _prepare(self, kind: str, raw_payload: Any) -> Any:
        payload = _require_mapping(raw_payload)
        config_path: str | Path = self.project.config_path
        if kind == "analyze":
            checkers = payload.get("checkers")
            if not isinstance(checkers, list) or not checkers:
                raise ValueError("checkers must be a non-empty array")
            if any(not isinstance(item, str) for item in checkers):
                raise ValueError("every checker must be a string")
            selected = tuple(dict.fromkeys(item.strip() for item in checkers if item.strip()))
            invalid = sorted(set(selected) - set(VALID_CHECKERS))
            if invalid:
                raise ValueError("unsupported checker(s): " + ", ".join(invalid))
            if not selected:
                raise ValueError("at least one checker is required")
            new_baseline = payload.get("new_baseline", False)
            if not isinstance(new_baseline, bool):
                raise ValueError("new_baseline must be a boolean")
            return AnalyzeRequest(config_path, selected, new_baseline)

        if kind == "triage":
            mode = payload.get("mode")
            if mode not in {"classify", "expand-semantics"}:
                raise ValueError("mode must be classify or expand-semantics")
            return TriageRequest(
                config_path=config_path,
                alert_ids=self._alert_ids(payload.get("alert_ids")),
                mode=mode,
                round_id=_optional_text(payload.get("round_id"), "round_id"),
                classification_source=_required_text(
                    payload.get("classification_source", "fphandler"),
                    "classification_source",
                ),
            )

        if kind == "active-learning":
            rounds = payload.get("rounds")
            if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1:
                raise ValueError("rounds must be a positive integer")
            feedback = payload.get("feedback")
            if feedback not in {"none", "fphandler"}:
                raise ValueError("feedback must be none or fphandler")
            if feedback == "none" and rounds != 1:
                raise ValueError("feedback=none requires rounds=1")
            return ActiveLearningRequest(
                config_path=config_path,
                rounds=rounds,
                feedback=feedback,
                initial_model=_required_text(payload.get("initial_model"), "initial_model"),
            )

        raise ValueError(f"unsupported operation: {kind}")

    def submit(self, kind: str, payload: Any) -> dict[str, Any]:
        with self._lock:
            if self._state["status"] == "running":
                raise OperationBusyError("another workflow operation is already running")
        self.project.assert_unchanged()
        request = self._prepare(kind, payload)
        with self._lock:
            if self._state["status"] == "running":
                raise OperationBusyError("another workflow operation is already running")
            submission_id = uuid.uuid4().hex
            cancel = threading.Event()
            self._cancel = cancel
            self._state = {
                "status": "running",
                "submission_id": submission_id,
                "kind": kind,
                "started_at": _now(),
                "completed_at": None,
                "progress": [],
                "result": None,
                "error": None,
            }
            self._thread = threading.Thread(
                target=self._run,
                args=(kind, request, cancel),
                name=f"local-ui-{kind}",
                daemon=False,
            )
            self._thread.start()
            return copy.deepcopy(self._state)

    @staticmethod
    def _event_dict(event: Any) -> dict[str, Any]:
        if is_dataclass(event):
            return asdict(event)
        if isinstance(event, Mapping):
            return dict(event)
        return {
            "operation_id": getattr(event, "operation_id", None),
            "phase": getattr(event, "phase", "progress"),
            "message": str(getattr(event, "message", event)),
            "created_at": getattr(event, "created_at", _now()),
        }

    def _progress(self, event: Any) -> None:
        value = self._event_dict(event)
        with self._lock:
            if self._state["status"] == "running":
                self._state["progress"].append(value)
                self._state["progress"] = self._state["progress"][-500:]

    def _run(self, kind: str, request: Any, cancel: threading.Event) -> None:
        try:
            result = self._services[kind](request, progress=self._progress, cancel=cancel)
            value = result.to_dict() if hasattr(result, "to_dict") else result
            ok = bool(value.get("ok", True)) if isinstance(value, Mapping) else True
            with self._lock:
                self._state["status"] = "succeeded" if ok else "failed"
                self._state["result"] = value
                self._state["error"] = None if ok else "operation completed with ok=false"
                self._state["completed_at"] = _now()
        except BaseException as error:
            with self._lock:
                self._state["status"] = "failed"
                self._state["error"] = str(error) or error.__class__.__name__
                self._state["completed_at"] = _now()

    def current(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def shutdown(self) -> None:
        with self._lock:
            thread = self._thread
            cancel = self._cancel
            if thread is not None and thread.is_alive() and cancel is not None:
                cancel.set()
        if thread is not None and thread.is_alive():
            thread.join()
