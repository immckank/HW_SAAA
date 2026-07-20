from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "FPhandler"))
sys.path.insert(0, str(ROOT / "script"))
sys.path.insert(0, str(ROOT / "contracts" / "python"))

from reconcile_semantic_warnings import reconcile  # noqa: E402
from semantic_rule_repository import append_candidates, load_repository  # noqa: E402
from semantic_rules import empty_repository, validate  # noqa: E402
from warning_contract import new_warning, recompute_score  # noqa: E402


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _classification(value: str) -> dict:
    return {
        "classification": value,
        "reason": "test",
        "source": "fphandler",
        "created_at": "2026-07-20T00:00:00+00:00",
        "round_id": None,
        "batch_id": "B0001",
        "semantic_candidates": [],
    }


def _warning(warning_type: str, content: dict, classification: str | None = None) -> dict:
    warning = new_warning("svfmemplus", warning_type, content)
    if classification:
        warning["classifications"] = [_classification(classification)]
        recompute_score(warning)
    return warning


class SemanticFactContractTest(unittest.TestCase):
    def test_empty_repository_has_exact_fixed_scopes(self) -> None:
        repository = empty_repository()
        self.assertEqual([], validate(repository))
        self.assertEqual(
            {"base_api", "safe_alloc", "safe_free", "value_range", "source_filter"},
            set(repository["scopes"]),
        )

    def test_contract_template_matches_runtime_empty_repository(self) -> None:
        template = json.loads(
            (ROOT / "contracts/examples/semantic-fact-v2.json").read_text(encoding="utf-8")
        )
        schema = json.loads(
            (ROOT / "contracts/schemas/semantic-fact-v2.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual([], validate(template))
        self.assertEqual(empty_repository(), template)
        self.assertEqual("semantic-fact/v2", schema["$id"])

    def test_context_requires_nonempty_call_chain_and_has_no_management_fields(self) -> None:
        repository = empty_repository()
        repository["scopes"]["safe_free"]["facts"].append(
            {"function": "kfree", "kind": "free", "call_chain": []}
        )
        self.assertTrue(any("call_chain" in item for item in validate(repository)))
        repository["scopes"]["safe_free"]["facts"][0]["id"] = "not-allowed"
        self.assertTrue(any("unknown fields" in item for item in validate(repository)))

    def test_fphandler_appends_valid_facts_and_rejects_conflicting_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "semantic_facts.json")
            candidates = [
                {
                    "scope": "base_api",
                    "fact": {"function": "project_alloc", "kind": "alloc"},
                },
                {
                    "scope": "value_range",
                    "fact": {
                        "function": "copy_record",
                        "kind": "range",
                        "variable": "arg:1",
                        "lower": 0,
                        "upper": 31,
                    },
                },
            ]
            self.assertEqual(2, append_candidates(path, "alert:a", candidates))
            self.assertEqual(0, append_candidates(path, "alert:a", candidates))
            conflict = [{
                "scope": "value_range",
                "fact": {
                    "function": "copy_record",
                    "kind": "range",
                    "variable": "arg:1",
                    "lower": 0,
                    "upper": 63,
                },
            }]
            self.assertEqual(0, append_candidates(path, "alert:b", conflict))
            self.assertEqual([], validate(load_repository(path)))


class SemanticWarningReconciliationTest(unittest.TestCase):
    def test_disappeared_warning_is_retained_as_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent"
            current = root / "current"
            warning = _warning("bof", {
                "access": {
                    "function": "copy_record",
                    "location": {"file": "src/record.c", "line": 27},
                },
                "variables": [{
                    "semantic_key": "arg:1",
                    "semantic_location": {"file": "src/record.c", "line": 12},
                }],
            })
            _write(parent / "bof" / "bof.json", warning)
            stats = reconcile(parent, current)
            self.assertEqual(1, stats["suppressed"])
            restored = json.loads(
                (current / "bof" / "bof.json").read_text(encoding="utf-8")
            )
            self.assertTrue(restored["suppressed"])
            self.assertEqual(0.0, restored["score"])

    def test_all_disappearances_are_preserved_and_reappearance_clears_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent"
            current = root / "current"
            old_path = parent / "uaf" / "old.json"
            unrelated_path = parent / "uaf" / "unrelated.json"
            survivor_path = parent / "leak" / "survivor.json"
            old = _warning("uaf", {
                "path": [{
                    "role": "free",
                    "location": {"file": "src/cache.c", "line": 41, "column": 2},
                }],
                "evidence": {"checker": {"report_kind": "test"}},
            }, "FP")
            unrelated = _warning("uaf", {
                "path": [{
                    "role": "free",
                    "location": {"file": "src/other.c", "line": 9, "column": 1},
                }],
                "evidence": {"checker": {"report_kind": "test"}},
            })
            survivor_content = {
                "allocation": {
                    "role": "allocation",
                    "location": {"file": "src/alloc.c", "line": 7, "column": 1}
                },
                "paths": [],
                "leak_condition": {"op": "true"},
                "evidence": {"checker": {"report_kind": "alloc_source"}},
            }
            survivor = _warning("leak", survivor_content, "TP")
            survivor["graph_ids"] = ["g:1"]
            survivor["active_learning"] = {"weight": 0.8, "model": "sha256:model"}
            survivor["suppressed"] = True
            recompute_score(survivor)
            _write(old_path, old)
            _write(unrelated_path, unrelated)
            _write(survivor_path, survivor)
            _write(
                current / "leak" / "survivor.json",
                _warning("leak", survivor_content),
            )
            new = _warning("bof", {
                "access": {"location": {"file": "src/new.c", "line": 3}},
                "variables": [],
            })
            _write(
                current / "bof" / "new.json",
                new,
            )
            stats = reconcile(parent, current)

            self.assertEqual(2, stats["suppressed"])
            self.assertEqual(1, stats["reappeared"])
            suppressed = json.loads(
                (current / "uaf" / "old.json").read_text(encoding="utf-8")
            )
            self.assertTrue(suppressed["suppressed"])
            self.assertEqual(0.0, suppressed["score"])
            unrelated_result = json.loads(
                (current / "uaf" / "unrelated.json").read_text(encoding="utf-8")
            )
            self.assertTrue(unrelated_result["suppressed"])
            new_result = json.loads((current / "bof" / "new.json").read_text())
            self.assertFalse(new_result["suppressed"])
            survived = json.loads(
                (current / "leak" / "survivor.json").read_text(encoding="utf-8")
            )
            self.assertEqual(survivor["classifications"], survived["classifications"])
            self.assertEqual(survivor["graph_ids"], survived["graph_ids"])
            self.assertEqual(survivor["active_learning"], survived["active_learning"])
            self.assertFalse(survived["suppressed"])
            self.assertEqual(0.9, survived["score"])


if __name__ == "__main__":
    unittest.main()
