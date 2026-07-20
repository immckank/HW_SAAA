"""External tool adapter used by the workflow services."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .config import WorkflowConfig
from .workspace import sha256_file


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class CommandFailure(RuntimeError):
    def __init__(self, command: list[str], returncode: int):
        super().__init__(f"command failed with exit {returncode}: {' '.join(command)}")
        self.command = command
        self.returncode = returncode


class ToolRunner:
    def __init__(
        self,
        log_path: Path,
        progress: Callable[[str, str], None] | None = None,
        cancel: Any = None,
    ):
        self.log_path = log_path
        self.progress = progress
        self.cancel = cancel
        self.svf_root = Path(
            os.environ.get("SVF_ROOT", os.environ.get("svf_root", REPOSITORY_ROOT / "SVFmemplus"))
        ).resolve()
        self.fph_root = Path(
            os.environ.get("FPH_ROOT", os.environ.get("fph_root", REPOSITORY_ROOT / "FPhandler"))
        ).resolve()
        self.active_root = Path(
            os.environ.get(
                "ACTIVE_LEARNING_ROOT",
                os.environ.get("active_learning_root", REPOSITORY_ROOT / "ActiveLearning"),
            )
        ).resolve()

    def _notify(self, phase: str, message: str) -> None:
        if self.progress:
            self.progress(phase, message)

    def _environment(self, config: WorkflowConfig) -> dict[str, str]:
        environment = dict(os.environ)
        prefixes = [
            self.svf_root / "Release-build" / "bin",
            self.svf_root / "llvm-21.1.0.obj" / "bin",
            self.svf_root / "z3.obj" / "bin",
        ]
        libraries = [
            self.svf_root / "Release-build" / "lib",
            self.svf_root / "llvm-21.1.0.obj" / "lib",
            self.svf_root / "z3.obj" / "bin",
        ]
        environment["PATH"] = os.pathsep.join(
            [str(path) for path in prefixes if path.is_dir()]
            + [environment.get("PATH", "")]
        )
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(
            [str(path) for path in libraries if path.is_dir()]
            + [environment.get("LD_LIBRARY_PATH", "")]
        )
        environment["SABER_SOURCE_ROOT"] = str(config.source_dir)
        return environment

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        check: bool = True,
    ) -> int:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write("$ " + " ".join(command) + "\n")
            log.flush()
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
            )
            while process.poll() is None:
                if self.cancel is not None and self.cancel.is_set():
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                    raise RuntimeError("operation cancelled")
                time.sleep(0.1)
            returncode = int(process.returncode or 0)
        if check and returncode != 0:
            raise CommandFailure(command, returncode)
        return returncode

    def analyzer_hash(self, checkers: tuple[str, ...]) -> str:
        paths = []
        if any(checker != "bof" for checker in checkers):
            paths.append(self.svf_root / "Release-build" / "bin" / "saber")
        if "bof" in checkers:
            paths.append(self.svf_root / "Release-build" / "bin" / "bof")
        missing = [path for path in paths if not path.is_file()]
        if missing:
            raise ValueError("missing SVF executable(s): " + ", ".join(map(str, missing)))
        joined = "\n".join(sha256_file(path) for path in paths)
        import hashlib

        return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def run_svf(
        self,
        config: WorkflowConfig,
        checkers: tuple[str, ...],
        output_dir: Path,
        semantic_facts: Path,
    ) -> None:
        environment = self._environment(config)
        binary_dir = self.svf_root / "Release-build" / "bin"
        for checker in checkers:
            self._notify("analyze", f"running {checker}")
            if checker == "bof":
                command = [str(binary_dir / "bof")]
            else:
                command = [str(binary_dir / "saber"), f"-{checker}"]
            command.extend(
                [
                    f"-semantic-facts={semantic_facts}",
                    f"-report-dir={output_dir}",
                    str(config.bitcode_path),
                ]
            )
            self._run(command, cwd=self.svf_root, environment=environment)

    def write_fph_config(self, config: WorkflowConfig, path: Path) -> None:
        values = {
            "OUTPUT_DIR": str(config.artifact_dir),
            "PROJECT_ROOT": str(config.source_dir),
            "BITCODE_PATH": str(config.bitcode_path),
            "BC_STEM": config.bitcode_path.stem,
            "DEFECT_TYPES": ["leak", "dfree", "uaf", "uninit", "bof"],
            "ALERT_TYPES": ["leak", "dfree", "uaf", "uninit", "bof"],
            "ALERT_DIR": str(config.artifact_dir / "alerts"),
            "RUN_LOG_STEM": config.bitcode_path.stem,
            "RUN_SESSION_TIME_STR": None,
            "PROJECT_LABEL": config.bitcode_path.stem,
            "PROJECT_DESC": "",
            "RES_ROOT_PATH": str(config.artifact_dir / ".orchestrator" / "logs" / "fphandler"),
            "SEMANTIC_FACT_REPOSITORY": str(config.artifact_dir / "semantic_facts.json"),
            "SEMANTIC_RULE_REPOSITORY": str(config.artifact_dir / "semantic_facts.json"),
            "ACTIVE_LEARNING_ROOT": str(self.active_root),
            "LLM_TYPE": os.environ.get("LLM_TYPE", os.environ.get("llm_type", "DeepSeek")),
            "SVF_ROOT": str(self.svf_root),
            "ALERT_BATCH_SIZE": 8,
            "BOF_BATCH_SIZE": 32,
            "AGENT_MAX_TURNS": int(os.environ.get("AGENT_MAX_TURNS", "64")),
            "AGENT_CONCLUSION_RESERVE_TURNS": int(
                os.environ.get("AGENT_CONCLUSION_RESERVE_TURNS", "10")
            ),
            "STATS_ONLY": False,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{key} = {value!r}" for key, value in values.items()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_fphandler(
        self,
        config: WorkflowConfig,
        config_py: Path,
        alert_list: Path,
        *,
        semantic_mode: str,
        semantic_output: Path | None,
        round_id: str | None,
        source: str,
    ) -> int:
        environment = self._environment(config)
        command = [
            sys.executable,
            str(self.fph_root / "run.py"),
            "--config",
            str(config_py),
            "--alert-list",
            str(alert_list),
            "--force-reclassify",
            "--classification-source",
            source,
            "--semantic-mode",
            semantic_mode,
        ]
        if semantic_output is not None:
            command.extend(["--semantic-output", str(semantic_output)])
        if round_id:
            command.extend(["--round-id", round_id])
        self._notify("triage", f"classifying alerts in {alert_list.name}")
        return self._run(
            command,
            cwd=self.fph_root,
            environment=environment,
            check=False,
        )

    def run_graph_export(self, config: WorkflowConfig, output_dir: Path) -> None:
        environment = self._environment(config)
        command = [
            str(self.svf_root / "Release-build" / "bin" / "svf-al-export"),
            "--output-dir",
            str(output_dir),
            str(config.bitcode_path),
        ]
        self._notify("graphs", "exporting SVF graphs")
        self._run(command, cwd=self.svf_root, environment=environment)

    def run_active_cli(self, config: WorkflowConfig, arguments: list[str]) -> None:
        environment = self._environment(config)
        python_path = [str(self.active_root), str(REPOSITORY_ROOT / "contracts" / "python")]
        if environment.get("PYTHONPATH"):
            python_path.append(environment["PYTHONPATH"])
        environment["PYTHONPATH"] = os.pathsep.join(python_path)
        self._run(
            [sys.executable, str(self.active_root / "cli.py"), *arguments],
            cwd=self.active_root,
            environment=environment,
        )

    @staticmethod
    def write_latest_model(path: Path, checkpoint: Path, run_id: str, round_number: int) -> None:
        relative = checkpoint.relative_to(path.parent.parent)
        value = {
            "checkpoint": str(relative),
            "sha256": sha256_file(checkpoint),
            "run_id": run_id,
            "round": round_number,
        }
        from .workspace import write_json_atomic

        write_json_atomic(path, value)
