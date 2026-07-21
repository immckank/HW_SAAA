from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "contracts" / "python"))

from local_ui.alerts import AlertTable, summarize_content  # noqa: E402
from local_ui.operations import OperationBusyError, OperationManager  # noqa: E402
from local_ui.project import BoundProject, ConfigurationChangedError  # noqa: E402
from local_ui.server import LocalUIApp, LocalUIServer  # noqa: E402
from orchestrator import ProgressEvent  # noqa: E402
from orchestrator.workspace import write_json_atomic  # noqa: E402
from warning_contract import new_warning, recompute_score  # noqa: E402


class FakeResult:
    def __init__(self, **values: Any):
        self.values = {"ok": True, **values}

    def to_dict(self) -> dict[str, Any]:
        return dict(self.values)


def _project(root: Path) -> Path:
    bitcode = root / "program.bc"
    source = root / "source"
    artifacts = root / "artifacts"
    bitcode.write_bytes(b"bc")
    source.mkdir()
    config = root / "workflow.ini"
    config.write_text(
        "[project]\n"
        f"bitcode_path = {bitcode}\n"
        f"source_dir = {source}\n"
        f"artifact_dir = {artifacts}\n"
        "project_label = test-project\n"
        "project_desc = test description\n",
        encoding="utf-8",
    )
    return config


def _store_warning(artifacts: Path, warning: dict[str, Any]) -> None:
    digest = warning["alert_id"].split(":", 1)[1]
    write_json_atomic(artifacts / "alerts" / warning["type"] / f"{digest}.json", warning)


def _wait(manager: OperationManager, timeout: float = 2.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.current()
        if state["status"] != "running":
            return state
        time.sleep(0.01)
    raise AssertionError("operation did not finish")


class AlertTableTest(unittest.TestCase):
    def test_load_summarize_filter_and_sort_all_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = BoundProject(_project(root))
            artifacts = project.config.artifact_dir

            uaf = new_warning(
                "svfmemplus",
                "uaf",
                {
                    "path": [
                        {"role": "free", "location": {"file": "cache.c", "line": 10}},
                        {"role": "use", "location": {"file": "cache.c", "line": 14}},
                    ],
                    "evidence": {"checker": {"report_kind": "local"}},
                },
            )
            uaf["graph_ids"] = ["graph:a"]
            uaf["classifications"] = [
                {
                    "classification": "TP",
                    "reason": "confirmed <script>alert(1)</script>",
                    "source": "fphandler",
                    "created_at": "2026-07-20T00:00:00+00:00",
                    "round_id": None,
                    "batch_id": "B1",
                    "semantic_candidates": [],
                }
            ]
            uaf["active_learning"] = {"weight": 0.8, "model": "sha256:model"}
            recompute_score(uaf)

            leak = new_warning(
                "svfmemplus",
                "leak",
                {
                    "allocation": {
                        "role": "allocation",
                        "location": {"file": "alloc.c", "line": 5},
                    },
                    "paths": [{"condition": {}, "path": [{"role": "exit"}]}],
                    "leak_condition": {"kind": "test"},
                    "evidence": {"checker": {"report_kind": "local"}},
                },
            )
            leak["graph_ids"] = []
            recompute_score(leak)

            bof = new_warning(
                "svfmemplus",
                "bof",
                {
                    "access": {
                        "location": {"file": "copy.c", "line": 22},
                        "kind": "write",
                        "base": "buf",
                    }
                },
            )
            bof["suppressed"] = True
            recompute_score(bof)

            for warning in (uaf, leak, bof):
                _store_warning(artifacts, warning)

            table = AlertTable(project)
            descending = table.load()
            self.assertEqual(3, descending["total"])
            self.assertEqual([uaf["alert_id"], leak["alert_id"], bof["alert_id"]], [
                row["alert_id"] for row in descending["alerts"]
            ])
            self.assertIn("cache.c:10 (free) → cache.c:14 (use)", descending["alerts"][0]["content"])
            self.assertEqual("1 个图", descending["alerts"][0]["graph_ids"])
            self.assertIn("<script>alert(1)</script>", descending["alerts"][0]["classifications"])
            self.assertEqual({"weight": 0.8, "model": "sha256:model"}, descending["alerts"][0]["active_learning"])

            ascending = table.load(order="asc")
            self.assertEqual([bof["alert_id"], leak["alert_id"], uaf["alert_id"]], [
                row["alert_id"] for row in ascending["alerts"]
            ])
            filtered = table.load("leak", "desc")
            self.assertEqual(3, filtered["total"])
            self.assertEqual(1, filtered["filtered"])
            self.assertEqual("已处理，无图", filtered["alerts"][0]["graph_ids"])
            self.assertIn("allocation alloc.c:5", filtered["alerts"][0]["content"])

    def test_summary_falls_back_for_minimal_contract_content(self) -> None:
        self.assertIn("id", summarize_content("uaf", {"path": [{"id": "a"}]}))

    def test_invalid_warning_fails_the_whole_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = BoundProject(_project(root))
            invalid = project.config.artifact_dir / "alerts" / "uaf" / "bad.json"
            invalid.parent.mkdir(parents=True)
            invalid.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot load warning"):
                AlertTable(project).load()

    def test_bound_configuration_must_not_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _project(root)
            project = BoundProject(config)
            config.write_text(config.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationChangedError, "restart the UI"):
                project.to_dict()


class OperationManagerTest(unittest.TestCase):
    def test_all_requests_map_to_orchestrator_services(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = BoundProject(_project(Path(directory)))
            seen: dict[str, Any] = {}

            def service(kind: str):
                def run(request, progress, cancel):
                    seen[kind] = request
                    progress(ProgressEvent("fake-op", kind, "working"))
                    return FakeResult(kind=kind)
                return run

            manager = OperationManager(
                project,
                services={kind: service(kind) for kind in ("analyze", "triage", "active-learning")},
            )

            manager.submit("analyze", {"checkers": ["uaf", "leak"], "new_baseline": True})
            state = _wait(manager)
            self.assertEqual("succeeded", state["status"])
            self.assertEqual(("uaf", "leak"), seen["analyze"].checkers)
            self.assertTrue(seen["analyze"].new_baseline)
            self.assertEqual("working", state["progress"][0]["message"])

            manager.submit(
                "triage",
                {
                    "alert_ids": "sha256:a\n\nsha256:b\nsha256:a\n",
                    "mode": "expand-semantics",
                    "round_id": " round-1 ",
                    "classification_source": "manual",
                },
            )
            _wait(manager)
            self.assertEqual(("sha256:a", "sha256:b"), seen["triage"].alert_ids)
            self.assertEqual("round-1", seen["triage"].round_id)

            manager.submit(
                "active-learning",
                {"rounds": 2, "feedback": "fphandler", "initial_model": " latest "},
            )
            _wait(manager)
            self.assertEqual(2, seen["active-learning"].rounds)
            self.assertEqual("latest", seen["active-learning"].initial_model)

    def test_rejects_invalid_parameters_and_concurrent_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = BoundProject(_project(Path(directory)))
            started = threading.Event()
            release = threading.Event()

            def blocked(request, progress, cancel):
                started.set()
                release.wait(2)
                return FakeResult()

            manager = OperationManager(project, services={"analyze": blocked})
            with self.assertRaisesRegex(ValueError, "non-empty array"):
                manager.submit("analyze", {"checkers": [], "new_baseline": False})
            manager.submit("analyze", {"checkers": ["uaf"], "new_baseline": False})
            self.assertTrue(started.wait(1))
            with self.assertRaises(OperationBusyError):
                manager.submit("analyze", {"checkers": ["leak"], "new_baseline": False})
            release.set()
            self.assertEqual("succeeded", _wait(manager)["status"])


class LocalUIServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = _project(self.root)

        def service(request, progress, cancel):
            progress(ProgressEvent("http-op", "test", "from HTTP"))
            return FakeResult(operation="done")

        services = {kind: service for kind in ("analyze", "triage", "active-learning")}
        self.app = LocalUIApp(self.config, services=services)
        self.server = LocalUIServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.app.operations.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temporary.cleanup()

    def request(self, path: str, payload: Any = None):
        data = None
        headers = {}
        method = "GET"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
            method = "POST"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, response.headers, response.read()

    def test_static_project_alerts_and_operation_endpoints(self) -> None:
        status, headers, content = self.request("/")
        self.assertEqual(200, status)
        self.assertIn(b"workflow", content)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

        status, _, content = self.request("/api/project")
        project = json.loads(content)
        self.assertEqual(str(self.config.resolve()), project["config_path"])

        status, _, content = self.request("/api/alerts?type=all&order=desc")
        self.assertEqual([], json.loads(content)["alerts"])

        status, _, content = self.request(
            "/api/operations/analyze",
            {"checkers": ["uaf"], "new_baseline": False},
        )
        self.assertEqual(202, status)
        self.assertEqual("running", json.loads(content)["status"])
        state = _wait(self.app.operations)
        self.assertEqual("succeeded", state["status"])

    def test_bad_request_and_changed_configuration_have_explicit_status(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/operations/analyze", {"checkers": []})
        self.assertEqual(400, caught.exception.code)

        self.config.write_text(self.config.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/project")
        self.assertEqual(409, caught.exception.code)

    def test_frontend_uses_text_content_for_warning_values(self) -> None:
        _, _, content = self.request("/app.js")
        script = content.decode("utf-8")
        self.assertIn("cell.textContent", script)
        self.assertNotIn("innerHTML", script)


if __name__ == "__main__":
    unittest.main()
