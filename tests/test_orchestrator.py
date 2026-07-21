from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "contracts" / "python"))

from orchestrator import (  # noqa: E402
    ActiveLearningRequest,
    AnalyzeRequest,
    TriageRequest,
    analyze,
    run_active_learning,
    triage,
)
from orchestrator.config import WorkflowConfig  # noqa: E402
from orchestrator.__main__ import build_parser  # noqa: E402
from orchestrator.workspace import write_json_atomic  # noqa: E402
from warning_contract import new_warning, recompute_score  # noqa: E402


def _classification() -> dict[str, Any]:
    return {
        "classification": "TP",
        "reason": "fake runner",
        "source": "fphandler",
        "created_at": "2026-07-20T00:00:00+00:00",
        "round_id": None,
        "batch_id": "B0001",
        "semantic_candidates": [],
    }


class FakeRunner:
    def __init__(self, outputs: list[list[dict[str, Any]]]):
        self.outputs = list(outputs)
        self.graph_ids: list[str] = []

    def analyzer_hash(self, checkers: tuple[str, ...]) -> str:
        return "sha256:fake-" + "-".join(checkers)

    def run_svf(self, config, checkers, output_dir: Path, semantic_facts: Path) -> None:
        if not self.outputs:
            raise AssertionError("fake SVF has no configured output")
        for warning in self.outputs.pop(0):
            digest = warning["alert_id"].split(":", 1)[1]
            write_json_atomic(output_dir / "alerts" / warning["type"] / f"{digest}.json", warning)

    def write_fph_config(self, config, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fake\n", encoding="utf-8")

    def run_fphandler(
        self,
        config,
        config_py: Path,
        alert_list: Path,
        *,
        semantic_mode: str,
        semantic_output: Path | None,
        round_id: str | None,
        source: str,
    ) -> int:
        for raw_path in alert_list.read_text(encoding="utf-8").splitlines():
            path = Path(raw_path)
            warning = json.loads(path.read_text(encoding="utf-8"))
            entry = _classification()
            entry["source"] = source
            entry["round_id"] = round_id
            history = warning.get("classifications") or []
            history.append(entry)
            warning["classifications"] = history
            recompute_score(warning)
            write_json_atomic(path, warning)
        if semantic_mode == "append" and semantic_output is not None:
            repository = json.loads(semantic_output.read_text(encoding="utf-8"))
            repository["scopes"]["base_api"]["facts"].append(
                {"function": "fake_alloc", "kind": "alloc"}
            )
            write_json_atomic(semantic_output, repository)
        return 0

    def run_graph_export(self, config, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "0.node.csv").write_text("id,type,level,pointedBy\n0,0,0,0\n")

    @staticmethod
    def _argument(arguments: list[str], name: str) -> Path:
        return Path(arguments[arguments.index(name) + 1])

    def run_active_cli(self, config, arguments: list[str]) -> None:
        command = arguments[0]
        if command == "ensure-alert-graphs":
            alerts = self._argument(arguments, "--alerts-dir")
            self.graph_ids = []
            for path in alerts.rglob("*.json"):
                warning = json.loads(path.read_text(encoding="utf-8"))
                if warning["suppressed"]:
                    continue
                graph_id = "g:" + warning["alert_id"].split(":", 1)[1][:12]
                warning["graph_ids"] = [graph_id]
                self.graph_ids.append(graph_id)
                write_json_atomic(path, warning)
            manifest = self._argument(arguments, "--manifest-output")
            write_json_atomic(manifest, {"graphs": self.graph_ids})
        elif command == "predict":
            output = self._argument(arguments, "--output")
            output.parent.mkdir(parents=True, exist_ok=True)
            model = "random" if "--random-weights" in arguments else "sha256:fake-model"
            with output.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(
                    stream, fieldnames=["graph_id", "prediction", "score", "model"]
                )
                writer.writeheader()
                for index, graph_id in enumerate(self.graph_ids):
                    writer.writerow(
                        {
                            "graph_id": graph_id,
                            "prediction": 1,
                            "score": max(0.05, 0.8 - index * 0.02),
                            "model": model,
                        }
                    )
        elif command == "rank-alerts":
            alerts = self._argument(arguments, "--alerts-dir")
            predictions = self._argument(arguments, "--predictions")
            output = self._argument(arguments, "--output")
            with predictions.open(newline="", encoding="utf-8") as stream:
                prediction_rows = {row["graph_id"]: row for row in csv.DictReader(stream)}
            rows = []
            for path in alerts.rglob("*.json"):
                warning = json.loads(path.read_text(encoding="utf-8"))
                if warning["suppressed"]:
                    continue
                row = prediction_rows[warning["graph_ids"][0]]
                warning["active_learning"] = {
                    "weight": float(row["score"]),
                    "model": row["model"],
                }
                recompute_score(warning)
                write_json_atomic(path, warning)
                rows.append(
                    {
                        "alert_id": warning["alert_id"],
                        "type": warning["type"],
                        "path": str(path),
                        "graph_ids": warning["graph_ids"],
                        "weight": float(row["score"]),
                        "score": warning["score"],
                    }
                )
            output.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
        elif command == "collect-feedback":
            output = self._argument(arguments, "--output")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text('{"graph_id":"g:test","classification":"TP"}\n')
        elif command == "train":
            checkpoint_dir = self._argument(arguments, "--checkpoint-dir")
            round_id = arguments[arguments.index("--round-id") + 1]
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            (checkpoint_dir / f"{round_id}.pt").write_bytes(
                ("checkpoint:" + round_id).encode("utf-8")
            )
        else:
            raise AssertionError(f"unexpected active command in fake: {command}")

    @staticmethod
    def write_latest_model(path: Path, checkpoint: Path, run_id: str, round_number: int) -> None:
        import hashlib

        write_json_atomic(
            path,
            {
                "checkpoint": str(checkpoint.relative_to(path.parent.parent)),
                "sha256": "sha256:" + hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
                "run_id": run_id,
                "round": round_number,
            },
        )


class OrchestratorTest(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        bitcode = root / "bc"
        source = root / "source"
        artifacts = root / "artifacts"
        bitcode.mkdir()
        source.mkdir()
        (bitcode / "program.bc").write_bytes(b"bc-v1")
        config = root / "workflow.ini"
        config.write_text(
            "[project]\n"
            f"bitcode_path = {bitcode / 'program.bc'}\n"
            f"source_dir = {source}\n"
            f"artifact_dir = {artifacts}\n"
            "project_label = program\n"
            "project_desc =\n",
            encoding="utf-8",
        )
        return config

    def test_ini_requires_one_bitcode_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root)
            loaded = WorkflowConfig.load(config)
            self.assertEqual("program.bc", loaded.bitcode_path.name)
            self.assertEqual((root / "source").resolve(), loaded.source_dir)
            config.write_text(
                config.read_text(encoding="utf-8").replace("program.bc", "missing.bc"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "one .bc file"):
                WorkflowConfig.load(config)

    def test_ini_resolves_relative_paths_and_rejects_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source").mkdir()
            (root / "program.bc").write_bytes(b"bc")
            config = root / "workflow.ini"
            config.write_text(
                "[project]\n"
                "bitcode_path = program.bc\n"
                "source_dir = source\n"
                "artifact_dir = artifacts\n"
                "project_label = program\n"
                "project_desc =\n",
                encoding="utf-8",
            )
            loaded = WorkflowConfig.load(config)
            self.assertEqual((root / "program.bc").resolve(), loaded.bitcode_path)
            self.assertEqual("program", loaded.project_label)
            self.assertEqual("", loaded.project_desc)
            self.assertTrue((root / "artifacts").is_dir())
            config.write_text(
                config.read_text(encoding="utf-8") + "database = warnings.db\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unknown database"):
                WorkflowConfig.load(config)

    def test_cli_accepts_multiple_alert_ids(self) -> None:
        args = build_parser().parse_args(
            [
                "--config", "workflow.ini", "triage", "--mode", "classify",
                "--alerts", "sha256:a", "sha256:b",
            ]
        )
        self.assertEqual(["sha256:a", "sha256:b"], args.alerts)

    def test_analysis_reconciles_and_requires_explicit_new_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root)
            first = new_warning("svfmemplus", "uaf", {"path": [{"id": "a"}]})
            disappeared = new_warning("svfmemplus", "uaf", {"path": [{"id": "b"}]})
            new = new_warning("svfmemplus", "bof", {"access": {"id": "c"}})
            runner = FakeRunner([[first, disappeared], [first, new]])
            initial = analyze(AnalyzeRequest(config), runner=runner)
            self.assertTrue(initial.baseline_created)
            second = analyze(AnalyzeRequest(config), runner=runner)
            self.assertEqual(1, second.counts["newly_suppressed"])
            alerts = {
                document["alert_id"]: document
                for _, document in _warning_documents(root / "artifacts" / "alerts")
            }
            self.assertTrue(alerts[disappeared["alert_id"]]["suppressed"])
            self.assertFalse(alerts[new["alert_id"]]["suppressed"])

            (root / "bc" / "program.bc").write_bytes(b"bc-v2")
            with self.assertRaisesRegex(ValueError, "new-baseline"):
                analyze(AnalyzeRequest(config), runner=FakeRunner([[first]]))
            reset = analyze(
                AnalyzeRequest(config, new_baseline=True), runner=FakeRunner([[new]])
            )
            self.assertTrue(reset.baseline_created)
            self.assertEqual(1, len(list((root / "artifacts" / "alerts").rglob("*.json"))))

    def test_checker_order_and_config_filename_do_not_change_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root)
            warning = new_warning("svfmemplus", "uaf", {"path": [{"id": "a"}]})
            runner = FakeRunner([[warning], [warning], [warning]])
            analyze(AnalyzeRequest(config, ("uaf", "bof")), runner=runner)
            reordered = analyze(AnalyzeRequest(config, ("bof", "uaf")), runner=runner)
            self.assertFalse(reordered.baseline_created)
            renamed = root / "renamed.ini"
            renamed.write_text(config.read_text(encoding="utf-8"), encoding="utf-8")
            same_project = analyze(
                AnalyzeRequest(renamed, ("uaf", "bof")), runner=runner
            )
            self.assertFalse(same_project.baseline_created)

    def test_invalid_staged_warning_does_not_replace_current_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root)
            warning = new_warning("svfmemplus", "uaf", {"path": [{"id": "valid"}]})
            malformed = json.loads(json.dumps(warning))
            malformed["score"] = 0.9
            runner = FakeRunner([[warning], [malformed]])
            analyze(AnalyzeRequest(config), runner=runner)
            alert_path = next((root / "artifacts" / "alerts").rglob("*.json"))
            before = alert_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "score is stale"):
                analyze(AnalyzeRequest(config), runner=runner)
            self.assertEqual(before, alert_path.read_bytes())

    def test_downstream_rejects_semantic_library_not_yet_analyzed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root)
            warning = new_warning("svfmemplus", "uaf", {"path": [{"id": "a"}]})
            runner = FakeRunner([[warning]])
            analyze(AnalyzeRequest(config), runner=runner)
            semantic = root / "artifacts" / "semantic_facts.json"
            repository = json.loads(semantic.read_text(encoding="utf-8"))
            repository["scopes"]["base_api"]["facts"].append(
                {"function": "manual_alloc", "kind": "alloc"}
            )
            write_json_atomic(semantic, repository)
            with self.assertRaisesRegex(ValueError, "run analyze first"):
                triage(
                    TriageRequest(config, (warning["alert_id"],), "classify"),
                    runner=runner,
                )

    def test_triage_modes_and_single_semantic_reanalysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root)
            active = new_warning("svfmemplus", "uaf", {"path": [{"id": "active"}]})
            runner = FakeRunner([[active], []])
            analyze(AnalyzeRequest(config), runner=runner)
            result = triage(
                TriageRequest(
                    config_path=config,
                    alert_ids=(active["alert_id"],),
                    mode="expand-semantics",
                ),
                runner=runner,
            )
            self.assertTrue(result.ok)
            self.assertEqual(1, result.semantic_facts_added)
            self.assertIsNotNone(result.reanalysis)
            stored = dict(_warning_documents(root / "artifacts" / "alerts"))
            warning = next(iter(stored.values()))
            self.assertTrue(warning["suppressed"])
            self.assertEqual(1, len(warning["classifications"]))

    def test_failed_semantic_reanalysis_rolls_back_facts_but_keeps_classification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root)
            warning = new_warning("svfmemplus", "uaf", {"path": [{"id": "active"}]})
            malformed = new_warning("svfmemplus", "uaf", {"path": [{"id": "bad"}]})
            malformed["score"] = 0.9
            runner = FakeRunner([[warning], [malformed]])
            analyze(AnalyzeRequest(config), runner=runner)
            result = triage(
                TriageRequest(config, (warning["alert_id"],), "expand-semantics"),
                runner=runner,
            )
            self.assertFalse(result.ok)
            self.assertIsNotNone(result.reanalysis_error)
            semantic = json.loads(
                (root / "artifacts" / "semantic_facts.json").read_text(encoding="utf-8")
            )
            self.assertEqual([], semantic["scopes"]["base_api"]["facts"])
            stored = next(_warning_documents(root / "artifacts" / "alerts"))[1]
            self.assertEqual(1, len(stored["classifications"]))
            self.assertFalse(stored["suppressed"])

    def test_active_learning_without_feedback_weights_only_active_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root)
            active = new_warning("svfmemplus", "uaf", {"path": [{"id": "active"}]})
            suppressed = new_warning("svfmemplus", "uaf", {"path": [{"id": "old"}]})
            runner = FakeRunner([[active, suppressed], [active]])
            analyze(AnalyzeRequest(config), runner=runner)
            analyze(AnalyzeRequest(config), runner=runner)
            result = run_active_learning(
                ActiveLearningRequest(config, 1, "none", "random"), runner=runner
            )
            self.assertEqual(1, result.weighted_alerts)
            self.assertEqual(1, result.skipped_suppressed)
            documents = [document for _, document in _warning_documents(root / "artifacts" / "alerts")]
            active_doc = next(item for item in documents if not item["suppressed"])
            suppressed_doc = next(item for item in documents if item["suppressed"])
            self.assertIsNotNone(active_doc["active_learning"])
            self.assertIsNone(suppressed_doc["active_learning"])
            with self.assertRaisesRegex(ValueError, "requires rounds=1"):
                run_active_learning(
                    ActiveLearningRequest(config, 2, "none", "random"), runner=runner
                )

    def test_active_learning_with_only_suppressed_alerts_is_a_successful_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root)
            warning = new_warning("svfmemplus", "uaf", {"path": [{"id": "old"}]})
            runner = FakeRunner([[warning], []])
            analyze(AnalyzeRequest(config), runner=runner)
            analyze(AnalyzeRequest(config), runner=runner)
            result = run_active_learning(
                ActiveLearningRequest(config, 1, "none", "random"), runner=runner
            )
            self.assertTrue(result.ok)
            self.assertEqual(0, result.weighted_alerts)
            self.assertEqual(1, result.skipped_suppressed)
            self.assertEqual("no active warnings", result.stopped_reason)

    def test_active_learning_feedback_keeps_each_round_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._project(root)
            warnings = [
                new_warning("svfmemplus", "uaf", {"path": [{"id": str(index)}]})
                for index in range(25)
            ]
            runner = FakeRunner([warnings])
            analyze(AnalyzeRequest(config), runner=runner)
            result = run_active_learning(
                ActiveLearningRequest(config, 2, "fphandler", "random"), runner=runner
            )
            self.assertTrue(result.ok)
            self.assertEqual(2, result.completed_rounds)
            self.assertEqual(2, len(result.checkpoints))
            latest = json.loads(
                (root / "artifacts" / "models" / "latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(2, latest["round"])
            for checkpoint in result.checkpoints:
                self.assertTrue(Path(checkpoint).is_file())


def _warning_documents(root: Path):
    for path in root.rglob("*.json"):
        yield path, json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
