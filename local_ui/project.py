"""A workflow configuration bound for the lifetime of the UI process."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from orchestrator.config import WorkflowConfig


class ConfigurationChangedError(RuntimeError):
    """Raised when the INI bound at startup changes on disk."""


class BoundProject:
    def __init__(self, config_path: str | Path, *, env_file: str | Path | None = None):
        self.config = WorkflowConfig.load(config_path)
        self.config_path = self.config.path
        self.env_file = Path(env_file).resolve() if env_file else None
        self._config_digest = self._read_digest()

    def _read_digest(self) -> str:
        try:
            content = self.config_path.read_bytes()
        except OSError as error:
            raise ConfigurationChangedError(
                f"bound configuration is no longer readable: {self.config_path}"
            ) from error
        return hashlib.sha256(content).hexdigest()

    def assert_unchanged(self) -> None:
        if self._read_digest() != self._config_digest:
            raise ConfigurationChangedError(
                "workflow.ini changed after the UI started; restart the UI to bind it again"
            )

    def to_dict(self) -> dict[str, Any]:
        self.assert_unchanged()
        return {
            "config_path": str(self.config_path),
            "env_file": str(self.env_file) if self.env_file else None,
            "bitcode_path": str(self.config.bitcode_path),
            "source_dir": str(self.config.source_dir),
            "artifact_dir": str(self.config.artifact_dir),
            "project_label": self.config.project_label,
            "project_desc": self.config.project_desc,
        }
