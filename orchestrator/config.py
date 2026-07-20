"""Strict three-entry workflow configuration."""
from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path


PROJECT_KEYS = frozenset({"bitcode_path", "source_dir", "artifact_dir"})


@dataclass(frozen=True)
class WorkflowConfig:
    path: Path
    bitcode_path: Path
    source_dir: Path
    artifact_dir: Path

    @classmethod
    def load(cls, path: str | Path = "workflow.ini") -> "WorkflowConfig":
        config_path = Path(path).expanduser().resolve()
        if not config_path.is_file():
            raise ValueError(f"configuration file does not exist: {config_path}")
        parser = configparser.ConfigParser(interpolation=None)
        try:
            with config_path.open(encoding="utf-8") as stream:
                parser.read_file(stream)
        except (configparser.Error, OSError) as error:
            raise ValueError(f"cannot read configuration {config_path}: {error}") from error
        if parser.sections() != ["project"] or parser.defaults():
            raise ValueError("configuration must contain exactly one [project] section")
        section = parser["project"]
        keys = set(section)
        if keys != PROJECT_KEYS:
            missing = sorted(PROJECT_KEYS - keys)
            extra = sorted(keys - PROJECT_KEYS)
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if extra:
                details.append("unknown " + ", ".join(extra))
            raise ValueError("invalid [project] fields: " + "; ".join(details))

        def resolve_directory(name: str, *, create: bool = False) -> Path:
            raw = section.get(name, "").strip()
            if not raw:
                raise ValueError(f"{name} must not be empty")
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = config_path.parent / candidate
            candidate = candidate.resolve()
            if create:
                candidate.mkdir(parents=True, exist_ok=True)
            if not candidate.is_dir():
                raise ValueError(f"{name} is not a directory: {candidate}")
            return candidate

        source_dir = resolve_directory("source_dir")
        artifact_dir = resolve_directory("artifact_dir", create=True)
        raw_bitcode = section.get("bitcode_path", "").strip()
        if not raw_bitcode:
            raise ValueError("bitcode_path must not be empty")
        bitcode_path = Path(raw_bitcode).expanduser()
        if not bitcode_path.is_absolute():
            bitcode_path = config_path.parent / bitcode_path
        bitcode_path = bitcode_path.resolve()
        if not bitcode_path.is_file() or bitcode_path.suffix != ".bc":
            raise ValueError(f"bitcode_path must reference one .bc file: {bitcode_path}")
        return cls(
            path=config_path,
            bitcode_path=bitcode_path,
            source_dir=source_dir,
            artifact_dir=artifact_dir,
        )
