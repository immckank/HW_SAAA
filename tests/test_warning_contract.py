from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "contracts" / "python"))

from warning_contract import (  # noqa: E402
    calculate_alert_id,
    new_warning,
    recompute_score,
    require_warning,
)


def classification(value: str) -> dict:
    return {
        "classification": value,
        "reason": value,
        "source": "fphandler",
        "created_at": "2026-07-20T00:00:00+00:00",
        "round_id": None,
        "batch_id": "B0001",
        "semantic_candidates": [],
    }


class WarningContractTest(unittest.TestCase):
    def test_identity_uses_only_producer_type_and_content(self) -> None:
        left = {"path": [{"role": "use", "location": {"line": 9, "file": "a.c"}}]}
        right = {"path": [{"location": {"file": "a.c", "line": 9}, "role": "use"}]}
        self.assertEqual(
            calculate_alert_id("svfmemplus", "uaf", left),
            calculate_alert_id("svfmemplus", "uaf", right),
        )
        warning = new_warning("svfmemplus", "uaf", left)
        original = warning["alert_id"]
        warning["graph_ids"] = ["g:1"]
        warning["active_learning"] = {"weight": 0.8, "model": "sha256:model"}
        warning["classifications"] = [classification("TP")]
        recompute_score(warning)
        require_warning(warning)
        self.assertEqual(original, warning["alert_id"])

    def test_score_history_model_and_suppression(self) -> None:
        warning = new_warning("svfmemplus", "uaf", {"path": [{}]})
        self.assertEqual(0.5, warning["score"])
        warning["classifications"] = [classification("FP")]
        self.assertEqual(0.375, recompute_score(warning))
        warning["classifications"].append(classification("TP"))
        self.assertEqual(0.625, recompute_score(warning))
        warning["active_learning"] = {"weight": 0.9, "model": "random"}
        self.assertEqual(0.825, recompute_score(warning))
        warning["suppressed"] = True
        self.assertEqual(0.0, recompute_score(warning))

    def test_old_shape_is_rejected(self) -> None:
        warning = new_warning("svfmemplus", "uaf", {"path": [{}]})
        legacy = copy.deepcopy(warning)
        legacy["category"] = "USE_AFTER_FREE"
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            require_warning(legacy)
        legacy = copy.deepcopy(warning)
        legacy.pop("suppressed")
        legacy["suppressed_by"] = None
        with self.assertRaisesRegex(ValueError, "missing fields"):
            require_warning(legacy)

    def test_stale_score_and_malformed_stage_data_are_rejected(self) -> None:
        warning = new_warning("svfmemplus", "uaf", {"path": [{}]})
        warning["score"] = 0.9
        with self.assertRaisesRegex(ValueError, "score is stale"):
            require_warning(warning)
        warning = new_warning("svfmemplus", "uaf", {"path": [{}]})
        warning["suppressed"] = None
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            require_warning(warning)

    def test_example_id_and_schema_json_are_valid(self) -> None:
        example = json.loads((ROOT / "contracts/examples/warning-uaf.json").read_text())
        require_warning(example)
        schema = json.loads((ROOT / "contracts/schemas/warning.schema.json").read_text())
        self.assertNotIn("schema_version", schema["properties"])


if __name__ == "__main__":
    unittest.main()
