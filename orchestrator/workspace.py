"""Artifact workspace primitives: hashes, locking and atomic installation."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from .config import WorkflowConfig


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


class WorkspaceLock(AbstractContextManager["WorkspaceLock"]):
    def __init__(self, path: Path):
        self.path = path
        self._stream = None

    def __enter__(self) -> "WorkspaceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._stream.close()
            self._stream = None
            raise RuntimeError(f"artifact directory is busy: {self.path.parent.parent}") from error
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._stream is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None


class ArtifactWorkspace:
    def __init__(self, config: WorkflowConfig):
        self.config = config
        self.root = config.artifact_dir
        self.alerts = self.root / "alerts"
        self.graphs = self.root / "graphs"
        self.models = self.root / "models"
        self.semantic_facts = self.root / "semantic_facts.json"
        self.control = self.root / ".orchestrator"
        self.state_path = self.control / "state.json"
        self.lock_path = self.control / "lock"
        self.staging_root = self.control / "staging"
        self.logs = self.control / "logs"
        for directory in (self.control, self.staging_root, self.logs, self.models):
            directory.mkdir(parents=True, exist_ok=True)

    def lock(self) -> WorkspaceLock:
        return WorkspaceLock(self.lock_path)

    def staging(self, operation_id: str) -> Path:
        path = self.staging_root / operation_id
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
        return path

    def read_state(self) -> dict[str, Any] | None:
        if not self.state_path.is_file():
            return None
        with self.state_path.open(encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError(f"invalid orchestrator state: {self.state_path}")
        return value

    def write_state(self, value: dict[str, Any]) -> None:
        write_json_atomic(self.state_path, value)

    def ensure_semantic_repository(self, template: Path) -> None:
        if self.semantic_facts.exists():
            return
        self.semantic_facts.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(template, self.semantic_facts)

    def replace_directory(self, staged: Path, target: Path) -> None:
        if not staged.is_dir():
            raise ValueError(f"staged directory is missing: {staged}")
        backup = target.with_name(target.name + ".previous")
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(staged, target)
        except BaseException:
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)

    def copy_alerts(self, destination: Path) -> None:
        if self.alerts.is_dir():
            shutil.copytree(self.alerts, destination)
        else:
            destination.mkdir(parents=True)
