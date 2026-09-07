"""Application services for analysis, triage and active learning."""
from __future__ import annotations

import json
import os
import random
import shutil
import sys
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from .config import WorkflowConfig
from .runner import REPOSITORY_ROOT, ToolRunner
from .workspace import ArtifactWorkspace, sha256_file, write_json_atomic
from .xlsx_import import (
    DEFAULT_PRODUCER,
    build_tabular_warning,
    path_matches_source,
    read_xlsx_rows,
)

def _dependency_path(default: Path, *environment_names: str) -> Path:
    for name in environment_names:
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser().resolve()
    return default


_fphandler_root = _dependency_path(
    REPOSITORY_ROOT / "FPhandler", "FPH_ROOT", "fph_root"
)
_script_root = _dependency_path(
    Path("/pipeline") if Path("/pipeline/reconcile_semantic_warnings.py").is_file()
    else REPOSITORY_ROOT / "script",
    "ORCHESTRATOR_SCRIPT_ROOT",
)

for dependency in (
    REPOSITORY_ROOT / "contracts" / "python",
    _fphandler_root,
    _script_root,
):
    if str(dependency) not in sys.path:
        sys.path.insert(0, str(dependency))

from reconcile_semantic_warnings import reconcile  # noqa: E402
from semantic_rules import validate as validate_semantic_facts  # noqa: E402
from warning_contract import WARNING_TYPES, require_warning  # noqa: E402


ProgressCallback = Callable[["ProgressEvent"], None]
VALID_CHECKERS = ("leak", "dfree", "uaf", "uninit", "bof")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _operation_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class ProgressEvent:
    operation_id: str
    phase: str
    message: str
    created_at: str = field(default_factory=_now)


@dataclass(frozen=True)
class AnalyzeRequest:
    config_path: str | Path = "workflow.ini"
    checkers: tuple[str, ...] = VALID_CHECKERS
    new_baseline: bool = False


@dataclass
class AnalyzeResult:
    operation_id: str
    baseline_created: bool
    new_baseline: bool
    counts: dict[str, int]
    alerts_dir: str
    log_path: str
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TriageRequest:
    config_path: str | Path = "workflow.ini"
    alert_ids: tuple[str, ...] = ()
    mode: Literal["classify", "expand-semantics"] = "classify"
    round_id: str | None = None
    classification_source: str = "fphandler"


@dataclass
class TriageResult:
    operation_id: str
    requested: int
    classified: int
    skipped_suppressed: list[str]
    semantic_facts_added: int
    reanalysis: AnalyzeResult | None
    fphandler_exit_code: int
    log_path: str
    reanalysis_error: str | None = None
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        return value


@dataclass(frozen=True)
class ActiveLearningRequest:
    config_path: str | Path = "workflow.ini"
    rounds: int = 1
    feedback: Literal["none", "fphandler"] = "none"
    initial_model: str = "random"


@dataclass
class ActiveLearningResult:
    operation_id: str
    requested_rounds: int
    completed_rounds: int
    feedback: str
    weighted_alerts: int
    skipped_suppressed: int
    checkpoints: list[str]
    stopped_reason: str | None
    log_path: str
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ImportXlsxRequest:
    config_path: str | Path = "workflow.ini"
    xlsx_path: str | Path = ""
    producer: str = DEFAULT_PRODUCER
    initial_weight: float = 0.5
    path_filter: bool = True
    mode: Literal["merge", "replace-producer"] = "merge"


@dataclass
class ImportXlsxResult:
    operation_id: str
    producer: str
    mode: str
    path_filter: bool
    xlsx_path: str
    counts: dict[str, int]
    alerts_dir: str
    log_path: str
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _PreparedAnalysis:
    alerts: Path
    counts: dict[str, int]
    state: dict[str, Any]
    baseline_created: bool


def _emitter(operation_id: str, callback: ProgressCallback | None):
    def emit(phase: str, message: str) -> None:
        if callback:
            callback(ProgressEvent(operation_id, phase, message))

    return emit


def _normalize_checkers(checkers: Iterable[str]) -> tuple[str, ...]:
    selected = {str(item).strip() for item in checkers if str(item).strip()}
    if not selected:
        raise ValueError("at least one checker is required")
    invalid = sorted(selected - set(VALID_CHECKERS))
    if invalid:
        raise ValueError("unsupported checker(s): " + ", ".join(invalid))
    return tuple(checker for checker in VALID_CHECKERS if checker in selected)


def _load_semantic_repository(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read semantic facts {path}: {error}") from error
    errors = validate_semantic_facts(value)
    if errors:
        raise ValueError(f"invalid semantic facts {path}: " + "; ".join(errors))
    return value


def _semantic_fact_count(path: Path) -> int:
    value = _load_semantic_repository(path)
    return sum(len(scope["facts"]) for scope in value["scopes"].values())


def _warning_index(root: Path) -> dict[str, tuple[Path, dict[str, Any]]]:
    result: dict[str, tuple[Path, dict[str, Any]]] = {}
    if not root.is_dir():
        return result
    for path in sorted(root.rglob("*.json")):
        with path.open(encoding="utf-8") as stream:
            document = json.load(stream)
        require_warning(document)
        if path.parent.name != document["type"]:
            raise ValueError(f"warning path/type mismatch: {path}")
        alert_id = str(document["alert_id"])
        if alert_id in result:
            raise ValueError(f"duplicate alert_id {alert_id}: {path}")
        result[alert_id] = (path, document)
    return result


def _baseline_identity(
    config: WorkflowConfig,
    runner: ToolRunner,
    checkers: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "bitcode_path": str(config.bitcode_path),
        "bitcode_sha256": sha256_file(config.bitcode_path),
        "source_dir": str(config.source_dir),
        "checkers": list(checkers),
        "analyzer_sha256": runner.analyzer_hash(checkers),
    }


def _check_baseline(
    workspace: ArtifactWorkspace,
    identity: dict[str, Any],
    *,
    new_baseline: bool,
) -> tuple[dict[str, Any] | None, bool]:
    state = workspace.read_state()
    if state is None:
        if workspace.alerts.is_dir() and any(workspace.alerts.rglob("*.json")) and not new_baseline:
            raise ValueError("alerts exist without orchestrator state; use --new-baseline")
        return None, True
    previous = state.get("baseline")
    if previous != identity and not new_baseline:
        changed = sorted(
            key
            for key in set(previous or {}) | set(identity)
            if not isinstance(previous, dict) or previous.get(key) != identity.get(key)
        )
        raise ValueError(
            "analysis baseline changed (" + ", ".join(changed) + "); use --new-baseline"
        )
    return state, previous != identity


def _prepare_analysis(
    config: WorkflowConfig,
    workspace: ArtifactWorkspace,
    runner: ToolRunner,
    *,
    operation_id: str,
    checkers: tuple[str, ...],
    new_baseline: bool,
    semantic_facts: Path,
    stage: Path,
) -> _PreparedAnalysis:
    _load_semantic_repository(semantic_facts)
    identity = _baseline_identity(config, runner, checkers)
    previous_state, baseline_changed = _check_baseline(
        workspace, identity, new_baseline=new_baseline
    )
    output = stage / "svf-output"
    output.mkdir(parents=True, exist_ok=True)
    runner.run_svf(config, checkers, output, semantic_facts)
    generated = output / "alerts"
    generated.mkdir(parents=True, exist_ok=True)
    generated_index = _warning_index(generated)
    unexpected = sorted({doc[1]["type"] for doc in generated_index.values()} - set(checkers))
    if unexpected:
        raise ValueError("analyzer emitted unrequested warning types: " + ", ".join(unexpected))

    baseline_created = previous_state is None or baseline_changed or new_baseline
    if baseline_created:
        for path, document in generated_index.values():
            document["suppressed"] = False
            from warning_contract import recompute_score

            recompute_score(document)
            write_json_atomic(path, document)
        counts = {
            "previous": len(_warning_index(workspace.alerts)),
            "generated": len(generated_index),
            "surviving": 0,
            "reappeared": 0,
            "new": len(generated_index),
            "newly_suppressed": 0,
            "suppressed": 0,
            "current_after": len(generated_index),
        }
    else:
        counts = reconcile(workspace.alerts, generated)

    state = {
        "baseline": identity,
        "semantic_facts_sha256": sha256_file(semantic_facts),
        "last_operation": {
            "operation_id": operation_id,
            "kind": "analyze",
            "completed_at": _now(),
            "counts": counts,
        },
    }
    return _PreparedAnalysis(generated, counts, state, baseline_created)


def analyze(
    request: AnalyzeRequest,
    progress: ProgressCallback | None = None,
    cancel: Any = None,
    *,
    runner: ToolRunner | None = None,
) -> AnalyzeResult:
    operation_id = _operation_id("analyze")
    emit = _emitter(operation_id, progress)
    config = WorkflowConfig.load(request.config_path)
    workspace = ArtifactWorkspace(config)
    log_path = workspace.logs / f"{operation_id}.log"
    actual_runner = runner or ToolRunner(log_path, emit, cancel)
    checkers = _normalize_checkers(request.checkers)
    with workspace.lock():
        workspace.ensure_semantic_repository(
            REPOSITORY_ROOT / "contracts" / "examples" / "semantic-fact-v2.json"
        )
        stage = workspace.staging(operation_id)
        emit("analyze", "preparing staged analysis")
        try:
            prepared = _prepare_analysis(
                config,
                workspace,
                actual_runner,
                operation_id=operation_id,
                checkers=checkers,
                new_baseline=request.new_baseline,
                semantic_facts=workspace.semantic_facts,
                stage=stage,
            )
            workspace.replace_directory(prepared.alerts, workspace.alerts)
            if prepared.baseline_created and workspace.graphs.exists():
                shutil.rmtree(workspace.graphs)
            workspace.write_state(prepared.state)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    emit("complete", "analysis committed")
    return AnalyzeResult(
        operation_id=operation_id,
        baseline_created=prepared.baseline_created,
        new_baseline=request.new_baseline,
        counts={key: int(value) for key, value in prepared.counts.items()},
        alerts_dir=str(workspace.alerts),
        log_path=str(log_path),
    )


def _assert_current_baseline(
    config: WorkflowConfig, workspace: ArtifactWorkspace, runner: ToolRunner
) -> tuple[str, ...]:
    state = workspace.read_state()
    if state is None or not isinstance(state.get("baseline"), dict):
        raise ValueError("project has no analysis baseline; run analyze first")
    baseline = state["baseline"]
    if baseline.get("kind") == "tabular-import":
        if baseline.get("source_dir") != str(config.source_dir):
            raise ValueError(
                "tabular import baseline source_dir changed; re-import or run analyze"
            )
        return ()
    checkers = _normalize_checkers(baseline.get("checkers") or ())
    identity = _baseline_identity(config, runner, checkers)
    _check_baseline(workspace, identity, new_baseline=False)
    semantic_hash = state.get("semantic_facts_sha256")
    if not workspace.semantic_facts.is_file() or semantic_hash != sha256_file(
        workspace.semantic_facts
    ):
        raise ValueError("semantic facts changed since the last analysis; run analyze first")
    return checkers


def _assert_triage_baseline(
    config: WorkflowConfig,
    workspace: ArtifactWorkspace,
    runner: ToolRunner,
    documents: list[dict[str, Any]],
) -> tuple[str, ...]:
    if documents and all(item.get("type") == "tabular" for item in documents):
        state = workspace.read_state()
        if state is None:
            # Allow triage of imported tabular alerts even before state was written.
            if any(workspace.alerts.rglob("*.json")):
                return ()
            raise ValueError("project has no alerts; import xlsx or run analyze first")
        baseline = state.get("baseline")
        if isinstance(baseline, dict) and baseline.get("kind") == "tabular-import":
            if baseline.get("source_dir") != str(config.source_dir):
                raise ValueError(
                    "tabular import baseline source_dir changed; re-import xlsx"
                )
            return ()
        if isinstance(baseline, dict) and baseline.get("checkers"):
            return _assert_current_baseline(config, workspace, runner)
        return ()
    return _assert_current_baseline(config, workspace, runner)


def _alert_digest_name(alert_id: str) -> str:
    text = str(alert_id)
    if text.startswith("sha256:"):
        text = text[len("sha256:") :]
    return f"{text}.json"


def import_xlsx(
    request: ImportXlsxRequest,
    progress: ProgressCallback | None = None,
    cancel: Any = None,
    *,
    runner: ToolRunner | None = None,
) -> ImportXlsxResult:
    del cancel, runner  # import does not invoke external analyzers
    if request.mode not in {"merge", "replace-producer"}:
        raise ValueError("mode must be merge or replace-producer")
    producer = str(request.producer or "").strip() or DEFAULT_PRODUCER
    weight = float(request.initial_weight)
    if not 0.0 <= weight <= 1.0:
        raise ValueError("initial_weight must be between 0 and 1")
    xlsx_path = Path(request.xlsx_path).expanduser()
    if not xlsx_path.is_absolute():
        xlsx_path = (Path.cwd() / xlsx_path).resolve()
    else:
        xlsx_path = xlsx_path.resolve()

    operation_id = _operation_id("import-xlsx")
    emit = _emitter(operation_id, progress)
    config = WorkflowConfig.load(request.config_path)
    workspace = ArtifactWorkspace(config)
    log_path = workspace.logs / f"{operation_id}.log"
    log_path.write_text(
        f"import-xlsx producer={producer} mode={request.mode} path={xlsx_path}\n",
        encoding="utf-8",
    )

    emit("import", f"reading spreadsheet {xlsx_path}")
    rows = read_xlsx_rows(xlsx_path)
    counts = {
        "read": len(rows),
        "imported": 0,
        "skipped_dup": 0,
        "skipped_path": 0,
        "replaced": 0,
        "failed": 0,
    }

    with workspace.lock():
        workspace.ensure_semantic_repository(
            REPOSITORY_ROOT / "contracts" / "examples" / "semantic-fact-v2.json"
        )
        workspace.alerts.mkdir(parents=True, exist_ok=True)
        if request.mode == "replace-producer":
            removed = 0
            for path in list(workspace.alerts.rglob("*.json")):
                try:
                    with path.open(encoding="utf-8") as stream:
                        document = json.load(stream)
                    if document.get("producer") == producer:
                        path.unlink()
                        removed += 1
                except (OSError, json.JSONDecodeError, TypeError):
                    continue
            counts["replaced"] = removed
            emit("import", f"removed {removed} existing alert(s) for producer={producer}")

        existing = _warning_index(workspace.alerts)
        for row in rows:
            if request.path_filter and not path_matches_source(row["file"], config.source_dir):
                counts["skipped_path"] += 1
                continue
            try:
                warning = build_tabular_warning(
                    producer=producer,
                    norm=row["norm"],
                    snippet=row["snippet"],
                    file_name=row["file"],
                    line=row["line"],
                    rule_name=row["rule_name"],
                    initial_weight=weight,
                )
            except ValueError:
                counts["failed"] += 1
                continue
            alert_id = str(warning["alert_id"])
            if alert_id in existing and request.mode == "merge":
                counts["skipped_dup"] += 1
                continue
            target = workspace.alerts / "tabular" / _alert_digest_name(alert_id)
            write_json_atomic(target, warning)
            existing[alert_id] = (target, warning)
            counts["imported"] += 1

        state = workspace.read_state() or {}
        baseline = state.get("baseline")
        if not isinstance(baseline, dict) or baseline.get("kind") == "tabular-import":
            state["baseline"] = {
                "kind": "tabular-import",
                "producer": producer,
                "source_dir": str(config.source_dir),
            }
        if "semantic_facts_sha256" not in state and workspace.semantic_facts.is_file():
            state["semantic_facts_sha256"] = sha256_file(workspace.semantic_facts)
        state["last_operation"] = {
            "operation_id": operation_id,
            "kind": "import-xlsx",
            "completed_at": _now(),
            "counts": counts,
            "producer": producer,
            "xlsx_path": str(xlsx_path),
        }
        workspace.write_state(state)

    emit("complete", f"imported {counts['imported']} alert(s)")
    return ImportXlsxResult(
        operation_id=operation_id,
        producer=producer,
        mode=request.mode,
        path_filter=bool(request.path_filter),
        xlsx_path=str(xlsx_path),
        counts=counts,
        alerts_dir=str(workspace.alerts),
        log_path=str(log_path),
        ok=counts["failed"] == 0,
    )


def _classification_count(document: dict[str, Any]) -> int:
    history = document.get("classifications")
    return len(history) if isinstance(history, list) else 0


def _run_fph_locked(
    config: WorkflowConfig,
    workspace: ArtifactWorkspace,
    runner: ToolRunner,
    request: TriageRequest,
    stage: Path,
) -> tuple[int, int, list[str], Path | None, int]:
    stage.mkdir(parents=True, exist_ok=True)
    index = _warning_index(workspace.alerts)
    unique_ids = list(dict.fromkeys(request.alert_ids))
    missing = [alert_id for alert_id in unique_ids if alert_id not in index]
    if missing:
        raise ValueError("unknown alert_id(s): " + ", ".join(missing))
    skipped = [alert_id for alert_id in unique_ids if index[alert_id][1]["suppressed"]]
    selected = [alert_id for alert_id in unique_ids if alert_id not in set(skipped)]
    before = {alert_id: _classification_count(index[alert_id][1]) for alert_id in selected}
    if not selected:
        return 0, 0, skipped, None, 0

    alert_list = stage / "alerts.txt"
    alert_list.write_text(
        "\n".join(str(index[alert_id][0]) for alert_id in selected) + "\n",
        encoding="utf-8",
    )
    config_py = stage / "fph_config.py"
    runner.write_fph_config(config, config_py)
    semantic_output = None
    semantic_before = 0
    if request.mode == "expand-semantics":
        semantic_output = stage / "semantic_facts.json"
        shutil.copy2(workspace.semantic_facts, semantic_output)
        semantic_before = _semantic_fact_count(semantic_output)
    exit_code = runner.run_fphandler(
        config,
        config_py,
        alert_list,
        semantic_mode="append" if request.mode == "expand-semantics" else "off",
        semantic_output=semantic_output,
        round_id=request.round_id,
        source=request.classification_source,
    )
    after_index = _warning_index(workspace.alerts)
    classified = sum(
        _classification_count(after_index[alert_id][1]) > before[alert_id]
        for alert_id in selected
    )
    facts_added = (
        _semantic_fact_count(semantic_output) - semantic_before
        if semantic_output is not None
        else 0
    )
    return exit_code, classified, skipped, semantic_output, facts_added


def triage(
    request: TriageRequest,
    progress: ProgressCallback | None = None,
    cancel: Any = None,
    *,
    runner: ToolRunner | None = None,
) -> TriageResult:
    if request.mode not in {"classify", "expand-semantics"}:
        raise ValueError(f"unsupported triage mode: {request.mode}")
    if not request.alert_ids:
        raise ValueError("at least one alert_id is required")
    operation_id = _operation_id("triage")
    emit = _emitter(operation_id, progress)
    config = WorkflowConfig.load(request.config_path)
    workspace = ArtifactWorkspace(config)
    log_path = workspace.logs / f"{operation_id}.log"
    actual_runner = runner or ToolRunner(log_path, emit, cancel)
    reanalysis = None
    reanalysis_error = None
    with workspace.lock():
        workspace.ensure_semantic_repository(
            REPOSITORY_ROOT / "contracts" / "examples" / "semantic-fact-v2.json"
        )
        index = _warning_index(workspace.alerts)
        selected_docs = []
        for alert_id in dict.fromkeys(request.alert_ids):
            if alert_id not in index:
                raise ValueError(f"unknown alert_id(s): {alert_id}")
            selected_docs.append(index[alert_id][1])
        if request.mode == "expand-semantics" and any(
            doc.get("type") == "tabular" for doc in selected_docs
        ):
            raise ValueError("expand-semantics is not supported for tabular spreadsheet alerts")
        checkers = _assert_triage_baseline(config, workspace, actual_runner, selected_docs)
        stage = workspace.staging(operation_id)
        try:
            exit_code, classified, skipped, semantic_stage, facts_added = _run_fph_locked(
                config, workspace, actual_runner, request, stage
            )
            if semantic_stage is not None and facts_added > 0:
                emit("reanalyze", "semantic facts changed; running one analysis")
                analysis_stage = stage / "reanalysis"
                analysis_stage.mkdir()
                try:
                    prepared = _prepare_analysis(
                        config,
                        workspace,
                        actual_runner,
                        operation_id=operation_id,
                        checkers=checkers,
                        new_baseline=False,
                        semantic_facts=semantic_stage,
                        stage=analysis_stage,
                    )
                    semantic_backup = stage / "semantic_facts.previous.json"
                    shutil.copy2(workspace.semantic_facts, semantic_backup)
                    semantic_install = workspace.semantic_facts.with_suffix(".json.install")
                    shutil.copy2(semantic_stage, semantic_install)
                    os.replace(semantic_install, workspace.semantic_facts)
                    try:
                        workspace.replace_directory(prepared.alerts, workspace.alerts)
                    except BaseException:
                        os.replace(semantic_backup, workspace.semantic_facts)
                        raise
                    workspace.write_state(prepared.state)
                    reanalysis = AnalyzeResult(
                        operation_id=operation_id,
                        baseline_created=False,
                        new_baseline=False,
                        counts={key: int(value) for key, value in prepared.counts.items()},
                        alerts_dir=str(workspace.alerts),
                        log_path=str(log_path),
                    )
                except Exception as error:  # classifications remain valid on failure
                    reanalysis_error = str(error)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    ok = exit_code == 0 and reanalysis_error is None
    emit("complete", "triage finished")
    return TriageResult(
        operation_id=operation_id,
        requested=len(dict.fromkeys(request.alert_ids)),
        classified=classified,
        skipped_suppressed=skipped,
        semantic_facts_added=facts_added,
        reanalysis=reanalysis,
        fphandler_exit_code=exit_code,
        log_path=str(log_path),
        reanalysis_error=reanalysis_error,
        ok=ok,
    )


def _resolve_initial_model(
    config: WorkflowConfig, workspace: ArtifactWorkspace, model: str
) -> Path | None:
    if model == "random":
        return None
    if model == "latest":
        latest = workspace.models / "latest.json"
        if not latest.is_file():
            raise ValueError("initial model latest requested but models/latest.json is missing")
        value = json.loads(latest.read_text(encoding="utf-8"))
        checkpoint = workspace.root / str(value.get("checkpoint") or "")
    else:
        checkpoint = Path(model).expanduser()
        if not checkpoint.is_absolute():
            checkpoint = config.path.parent / checkpoint
        checkpoint = checkpoint.resolve()
    if not checkpoint.is_file():
        raise ValueError(f"initial model checkpoint does not exist: {checkpoint}")
    return checkpoint


def _ensure_graphs(
    config: WorkflowConfig,
    workspace: ArtifactWorkspace,
    runner: ToolRunner,
    stage: Path,
) -> None:
    graph_stage = stage / "graphs"
    if workspace.graphs.is_dir():
        shutil.copytree(workspace.graphs, graph_stage)
    else:
        graph_stage.mkdir(parents=True)
    dataset = graph_stage / "predict_dataset"
    raw = dataset / "raw" / config.bitcode_path.stem
    if not raw.is_dir() or not any(raw.glob("*.node.csv")):
        raw.mkdir(parents=True, exist_ok=True)
        runner.run_graph_export(config, raw)
    alert_stage = stage / "graph-alerts"
    workspace.copy_alerts(alert_stage)
    runner.run_active_cli(
        config,
        [
            "ensure-alert-graphs",
            "--alerts-dir",
            str(alert_stage),
            "--dataset",
            str(dataset),
            "--manifest-output",
            str(graph_stage / "manifest.json"),
        ],
    )
    _warning_index(alert_stage)
    workspace.replace_directory(graph_stage, workspace.graphs)
    workspace.replace_directory(alert_stage, workspace.alerts)


def _prepare_prediction(
    config: WorkflowConfig,
    workspace: ArtifactWorkspace,
    runner: ToolRunner,
    stage: Path,
    model: Path | None,
    model_id: str | None = None,
) -> tuple[Path, Path, int]:
    alert_stage = stage / "alerts"
    workspace.copy_alerts(alert_stage)
    predictions = stage / "predictions.csv"
    command = [
        "predict",
        "--dataset",
        str(workspace.graphs / "predict_dataset"),
        "--output",
        str(predictions),
    ]
    if model is None:
        command.append("--random-weights")
    else:
        command.extend(["--model", str(model)])
        label = (model_id or _checkpoint_model_id(workspace, model)).strip()
        if label:
            command.extend(["--model-id", label])
    runner.run_active_cli(config, command)
    ranking = stage / "ranking.jsonl"
    runner.run_active_cli(
        config,
        [
            "rank-alerts",
            "--alerts-dir",
            str(alert_stage),
            "--predictions",
            str(predictions),
            "--output",
            str(ranking),
        ],
    )
    index = _warning_index(alert_stage)
    weighted = sum(not document[1]["suppressed"] for document in index.values())
    return alert_stage, ranking, weighted


def _checkpoint_model_id(workspace: ArtifactWorkspace, checkpoint: Path) -> str:
    resolved = checkpoint.resolve()
    try:
        return str(resolved.relative_to(workspace.root.resolve()))
    except ValueError:
        return str(resolved)


def _env_int(name: str, default: int, *aliases: str) -> int:
    for key in (name, *aliases):
        raw = os.environ.get(key)
        if raw is None or not str(raw).strip():
            continue
        try:
            return int(str(raw).strip())
        except ValueError as error:
            raise ValueError(f"{key} must be an integer") from error
    return default


def _env_float(name: str, default: float, *aliases: str) -> float:
    for key in (name, *aliases):
        raw = os.environ.get(key)
        if raw is None or not str(raw).strip():
            continue
        try:
            return float(str(raw).strip())
        except ValueError as error:
            raise ValueError(f"{key} must be a number") from error
    return default


def _env_flag(name: str, default: bool, *aliases: str) -> bool:
    for key in (name, *aliases):
        raw = os.environ.get(key)
        if raw is None or not str(raw).strip():
            continue
        value = str(raw).strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{key} must be a boolean-like value")
    return default


def _env_choice(name: str, default: str, allowed: set[str], *aliases: str) -> str:
    for key in (name, *aliases):
        raw = os.environ.get(key)
        if raw is None or not str(raw).strip():
            continue
        value = str(raw).strip()
        if value not in allowed:
            raise ValueError(f"{key} must be one of: {', '.join(sorted(allowed))}")
        return value
    return default


def _feedback_alert_path(row: dict[str, Any], alerts_dir: Path | None) -> Path:
    """Resolve a ranking row to a readable alert JSON path.

    Ranking rows may still point at a staging directory that was moved into
    ``workspace.alerts`` by ``replace_directory``; fall back to alert_id lookup.
    """
    path = Path(str(row.get("path") or ""))
    if path.is_file():
        return path
    if alerts_dir is None:
        return path
    alert_id = str(row.get("alert_id") or "")
    warning_type = str(row.get("type") or "")
    digest = alert_id.split(":", 1)[-1] if alert_id else ""
    if digest and warning_type:
        candidate = alerts_dir / warning_type / f"{digest}.json"
        if candidate.is_file():
            return candidate
    if digest:
        matches = sorted(alerts_dir.rglob(f"{digest}.json"))
        if len(matches) == 1:
            return matches[0]
    return path


def _feedback_is_classified(row: dict[str, Any], alerts_dir: Path | None = None) -> bool:
    path = _feedback_alert_path(row, alerts_dir)
    if not path.is_file():
        return False
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    classifications = document.get("classifications")
    if not isinstance(classifications, list) or not classifications:
        return False
    latest = classifications[-1]
    return isinstance(latest, dict) and bool(latest.get("classification"))


def _sample_feedback_rows(
    rows: list[dict[str, Any]], sample_size: int, seed: int, strategy: str
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    if strategy == "plain":
        shuffled = list(rows)
        rng.shuffle(shuffled)
        return shuffled[:sample_size]
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get("type") or "unknown")].append(row)
    for bucket in buckets.values():
        rng.shuffle(bucket)
    selected: list[dict[str, Any]] = []
    types = sorted(buckets)
    while len(selected) < sample_size and types:
        remaining: list[str] = []
        for warning_type in types:
            bucket = buckets[warning_type]
            if bucket:
                selected.append(bucket.pop())
            if bucket:
                remaining.append(warning_type)
            if len(selected) >= sample_size:
                break
        types = remaining
    return selected


def _select_feedback_ids(
    ranking: Path,
    excluded: set[str],
    *,
    alerts_dir: Path | None = None,
) -> list[str]:
    # Default policy: keep all hard labels for training, and only send randomly
    # sampled *unclassified* alerts to FPhandler (no score-based top/bottom).
    top_k = _env_int(
        "ACTIVE_LEARNING_FEEDBACK_TOP_K",
        0,
        "active_learning_feedback_top_k",
    )
    bottom_k = _env_int(
        "ACTIVE_LEARNING_FEEDBACK_BOTTOM_K",
        0,
        "active_learning_feedback_bottom_k",
    )
    random_k = _env_int(
        "ACTIVE_LEARNING_FEEDBACK_RANDOM_K",
        10,
        "active_learning_feedback_random_k",
    )
    random_seed = _env_int(
        "ACTIVE_LEARNING_FEEDBACK_RANDOM_SEED",
        42,
        "active_learning_feedback_random_seed",
    )
    skip_classified = _env_flag(
        "ACTIVE_LEARNING_FEEDBACK_SKIP_CLASSIFIED",
        True,
        "active_learning_feedback_skip_classified",
    )
    random_strategy = _env_choice(
        "ACTIVE_LEARNING_FEEDBACK_RANDOM_STRATEGY",
        "type",
        {"plain", "type"},
        "active_learning_feedback_random_strategy",
    )
    if top_k < 0 or bottom_k < 0 or random_k < 0:
        raise ValueError("active learning feedback k values must be >= 0")

    rows: list[dict[str, Any]] = []
    with ranking.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            alert_id = str(row["alert_id"])
            if alert_id in excluded:
                continue
            classified = _feedback_is_classified(row, alerts_dir)
            if skip_classified and classified:
                continue
            row = dict(row)
            row["is_classified"] = classified
            resolved = _feedback_alert_path(row, alerts_dir)
            if resolved.is_file():
                row["path"] = str(resolved)
            rows.append(row)
    rows.sort(key=lambda item: (-float(item["weight"]), str(item["type"]), item["alert_id"]))

    selected: list[str] = []
    seen: set[str] = set()

    def add(row: dict[str, Any]) -> None:
        alert_id = str(row["alert_id"])
        if alert_id in seen:
            return
        seen.add(alert_id)
        selected.append(alert_id)

    for row in rows[:top_k]:
        add(row)
    if bottom_k:
        for row in rows[-bottom_k:]:
            add(row)
    if random_k:
        candidates = [
            row
            for row in rows
            if str(row["alert_id"]) not in seen and not bool(row.get("is_classified"))
        ]
        for row in _sample_feedback_rows(candidates, random_k, random_seed, random_strategy):
            add(row)
    return selected


def _active_learning_train_cli_args() -> list[str]:
    """Build explicit train CLI flags from runtime.env (UI path)."""
    return [
        "--epochs",
        str(
            _env_int(
                "ACTIVE_LEARNING_TRAIN_EPOCHS",
                50,
                "active_learning_train_epochs",
            )
        ),
        "--lr",
        str(
            _env_float(
                "ACTIVE_LEARNING_TRAIN_LR",
                1e-3,
                "active_learning_train_lr",
            )
        ),
        "--weight-decay",
        str(
            _env_float(
                "ACTIVE_LEARNING_TRAIN_WEIGHT_DECAY",
                5e-4,
                "active_learning_train_weight_decay",
            )
        ),
        "--batch-size",
        str(
            _env_int(
                "ACTIVE_LEARNING_TRAIN_BATCH_SIZE",
                1,
                "active_learning_train_batch_size",
            )
        ),
        "--val-ratio",
        str(
            _env_float(
                "ACTIVE_LEARNING_TRAIN_VAL_RATIO",
                0.2,
                "active_learning_train_val_ratio",
            )
        ),
        "--patience",
        str(
            _env_int(
                "ACTIVE_LEARNING_TRAIN_PATIENCE",
                10,
                "active_learning_train_patience",
            )
        ),
        "--min-labels",
        str(
            _env_int(
                "ACTIVE_LEARNING_TRAIN_MIN_LABELS",
                2,
                "active_learning_train_min_labels",
            )
        ),
        "--uncertain-weight",
        str(
            _env_float(
                "ACTIVE_LEARNING_UNCERTAIN_WEIGHT",
                0.3,
                "active_learning_uncertain_weight",
            )
        ),
        "--hard-positive-weight",
        str(
            _env_float(
                "ACTIVE_LEARNING_HARD_POSITIVE_WEIGHT",
                0.0,
                "active_learning_hard_positive_weight",
            )
        ),
        "--unlabeled-weight",
        str(
            _env_float(
                "ACTIVE_LEARNING_UNLABELED_WEIGHT",
                0.0,
                "active_learning_unlabeled_weight",
            )
        ),
        "--weak-pos",
        str(
            _env_float(
                "ACTIVE_LEARNING_WEAK_POS",
                0.7,
                "active_learning_weak_pos",
            )
        ),
        "--weak-neg",
        str(
            _env_float(
                "ACTIVE_LEARNING_WEAK_NEG",
                0.3,
                "active_learning_weak_neg",
            )
        ),
        "--max-unlabeled",
        str(
            _env_int(
                "ACTIVE_LEARNING_TRAIN_MAX_UNLABELED",
                64,
                "active_learning_max_unlabeled",
            )
        ),
        "--max-batch-nodes",
        str(
            _env_int(
                "ACTIVE_LEARNING_TRAIN_MAX_BATCH_NODES",
                50_000,
                "active_learning_train_max_batch_nodes",
            )
        ),
        "--max-batch-edges",
        str(
            _env_int(
                "ACTIVE_LEARNING_TRAIN_MAX_BATCH_EDGES",
                80_000,
                "active_learning_train_max_batch_edges",
            )
        ),
    ]


def run_active_learning(
    request: ActiveLearningRequest,
    progress: ProgressCallback | None = None,
    cancel: Any = None,
    *,
    runner: ToolRunner | None = None,
) -> ActiveLearningResult:
    if request.rounds < 1:
        raise ValueError("rounds must be a positive integer")
    if request.feedback not in {"none", "fphandler"}:
        raise ValueError(f"unsupported feedback mode: {request.feedback}")
    if request.feedback == "none" and request.rounds != 1:
        raise ValueError("feedback=none requires rounds=1")
    operation_id = _operation_id("active-learning")
    emit = _emitter(operation_id, progress)
    config = WorkflowConfig.load(request.config_path)
    workspace = ArtifactWorkspace(config)
    log_path = workspace.logs / f"{operation_id}.log"
    actual_runner = runner or ToolRunner(log_path, emit, cancel)
    model = _resolve_initial_model(config, workspace, request.initial_model)
    checkpoints: list[str] = []
    stopped_reason = None
    completed_rounds = 0
    weighted = 0
    operation_failed = False
    with workspace.lock():
        _assert_current_baseline(config, workspace, actual_runner)
        index = _warning_index(workspace.alerts)
        skipped_suppressed = sum(document[1]["suppressed"] for document in index.values())
        if skipped_suppressed == len(index):
            stopped_reason = "no active warnings"
        else:
            stage = workspace.staging(operation_id)
            try:
                _ensure_graphs(config, workspace, actual_runner, stage / "graph-setup")
                initial_stage = stage / "initial-prediction"
                initial_stage.mkdir(parents=True)
                alert_stage, ranking, weighted = _prepare_prediction(
                    config, workspace, actual_runner, initial_stage, model
                )
                workspace.replace_directory(alert_stage, workspace.alerts)
                if request.feedback == "none":
                    completed_rounds = 1
                else:
                    selected_ids: set[str] = set()
                    model_dir = workspace.models / operation_id
                    model_dir.mkdir(parents=True, exist_ok=True)
                    for round_number in range(1, request.rounds + 1):
                        ids = _select_feedback_ids(
                            ranking, selected_ids, alerts_dir=workspace.alerts
                        )
                        if not ids:
                            stopped_reason = "no unselected active warnings remain"
                            break
                        selected_ids.update(ids)
                        round_id = f"round-{round_number:03d}"
                        round_stage = stage / round_id
                        round_stage.mkdir()
                        fph_request = TriageRequest(
                            config_path=config.path,
                            alert_ids=tuple(ids),
                            mode="classify",
                            round_id=f"{operation_id}-{round_id}",
                            classification_source="active-learning-fphandler",
                        )
                        exit_code, _, _, _, _ = _run_fph_locked(
                            config, workspace, actual_runner, fph_request, round_stage / "fph"
                        )
                        if exit_code != 0:
                            stopped_reason = (
                                f"FPhandler failed in {round_id} with exit {exit_code}"
                            )
                            operation_failed = True
                            break
                        labels = round_stage / "labels.jsonl"
                        actual_runner.run_active_cli(
                            config,
                            [
                                "collect-feedback",
                                "--alerts-dir",
                                str(workspace.alerts),
                                "--output",
                                str(labels),
                            ],
                        )
                        checkpoint_stage = round_stage / "checkpoints"
                        train_args = [
                            "train",
                            "--dataset",
                            str(workspace.graphs / "predict_dataset"),
                            "--labels",
                            str(labels),
                            "--checkpoint-dir",
                            str(checkpoint_stage),
                            "--round-id",
                            round_id,
                            *_active_learning_train_cli_args(),
                        ]
                        if model is not None:
                            train_args.extend(["--init-model", str(model)])
                        actual_runner.run_active_cli(config, train_args)
                        staged_checkpoint = checkpoint_stage / f"{round_id}.pt"
                        if not staged_checkpoint.is_file():
                            stopped_reason = f"training skipped in {round_id}"
                            break
                        prediction_stage = round_stage / "prediction"
                        prediction_stage.mkdir()
                        checkpoint = model_dir / f"{round_id}.pt"
                        prepared_alerts, ranking, weighted = _prepare_prediction(
                            config,
                            workspace,
                            actual_runner,
                            prediction_stage,
                            staged_checkpoint,
                            model_id=_checkpoint_model_id(workspace, checkpoint),
                        )
                        os.replace(staged_checkpoint, checkpoint)
                        manifest = {
                            "run_id": operation_id,
                            "round": round_number,
                            "checkpoint": str(checkpoint.relative_to(workspace.root)),
                            "created_at": _now(),
                            "feedback_alert_ids": ids,
                        }
                        write_json_atomic(model_dir / f"{round_id}.json", manifest)
                        workspace.replace_directory(prepared_alerts, workspace.alerts)
                        actual_runner.write_latest_model(
                            workspace.models / "latest.json",
                            checkpoint,
                            operation_id,
                            round_number,
                        )
                        model = checkpoint
                        checkpoints.append(str(checkpoint))
                        completed_rounds += 1
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
    emit("complete", "active learning finished")
    return ActiveLearningResult(
        operation_id=operation_id,
        requested_rounds=request.rounds,
        completed_rounds=completed_rounds,
        feedback=request.feedback,
        weighted_alerts=weighted,
        skipped_suppressed=skipped_suppressed,
        checkpoints=checkpoints,
        stopped_reason=stopped_reason,
        log_path=str(log_path),
        ok=not operation_failed,
    )
