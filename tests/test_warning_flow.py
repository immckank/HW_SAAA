from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "contracts" / "python"))
sys.path.insert(0, str(ROOT / "FPhandler"))
sys.path.insert(0, str(ROOT / "ActiveLearning"))

from alerts import rank_alerts, select_feedback  # noqa: E402
from alert_document import AlertDocument, UnifiedAlert  # noqa: E402
from warning_contract import new_warning  # noqa: E402


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class WarningFlowTest(unittest.TestCase):
    def test_bof_without_source_location_remains_classifiable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bof" / "warning.json"
            warning = new_warning(
                "svfmemplus",
                "bof",
                {
                    "access": {"function": "resource_overlaps", "kind": "GEP_OOB"},
                    "buffer": {},
                    "variables": [],
                    "range_analysis": [],
                    "evidence": {},
                },
            )
            _write(path, warning)
            alert = UnifiedAlert(AlertDocument.load(str(path)))
            self.assertEqual({"fl": "", "ln": 0, "cl": 0}, alert.get_source_loc())

    def test_fphandler_appends_history_and_recomputes_score(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "uaf" / "warning.json"
            warning = new_warning("svfmemplus", "uaf", {"path": [{}]})
            _write(path, warning)
            document = AlertDocument.load(str(path))
            document.write_classification(
                {"classification": "FP", "reason": "not reachable", "semantic_candidates": []},
                batch_id="B0001",
            )
            stored = json.loads(path.read_text())
            self.assertEqual("FP", stored["classifications"][0]["classification"])
            self.assertEqual(0.375, stored["score"])
            self.assertIsNone(stored["active_learning"])

    def test_display_uses_total_score_but_feedback_uses_model_weight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alerts_dir = root / "alerts"
            high_weight = new_warning("svfmemplus", "uaf", {"path": [{"id": "a"}]})
            low_weight = new_warning("svfmemplus", "uaf", {"path": [{"id": "b"}]})
            high_path = alerts_dir / "uaf" / "high.json"
            low_path = alerts_dir / "uaf" / "low.json"
            _write(high_path, high_weight)
            _write(low_path, low_weight)
            high_doc = AlertDocument.load(str(high_path))
            low_doc = AlertDocument.load(str(low_path))
            high_doc.write_classification({"classification": "FP", "reason": "fp", "semantic_candidates": []})
            low_doc.write_classification({"classification": "TP", "reason": "tp", "semantic_candidates": []})
            high_weight = json.loads(high_path.read_text())
            low_weight = json.loads(low_path.read_text())
            high_weight["graph_ids"] = ["g:high"]
            low_weight["graph_ids"] = ["g:low"]
            _write(high_path, high_weight)
            _write(low_path, low_weight)

            predictions = root / "predictions.csv"
            with predictions.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=["graph_id", "prediction", "score", "model"])
                writer.writeheader()
                writer.writerows(
                    [
                        {"graph_id": "g:high", "prediction": 1, "score": 0.9, "model": "sha256:model"},
                        {"graph_id": "g:low", "prediction": 0, "score": 0.2, "model": "sha256:model"},
                    ]
                )
            ranking = root / "active_learning" / "ranking.jsonl"
            rows = rank_alerts(str(alerts_dir), str(predictions), str(ranking))
            self.assertEqual(low_weight["alert_id"], rows[0]["alert_id"])
            self.assertEqual(0.6, rows[0]["score"])
            self.assertEqual(0.575, rows[1]["score"])

            feedback = root / "feedback.txt"
            selected = select_feedback(str(ranking), str(feedback), top_k=1, bottom_k=0)
            self.assertEqual([str(high_path)], selected)


if __name__ == "__main__":
    unittest.main()
