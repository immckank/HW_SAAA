"""Application services for analysis, triage and active learning."""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from .config import WorkflowConfig
from .runner import REPOSITORY_ROOT, ToolRunner
from .workspace import ArtifactWorkspace, sha256_file, write_json_atomic

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
    checkers = _normalize_checkers(state["baseline"].get("checkers") or ())
    identity = _baseline_identity(config, runner, checkers)
    _check_baseline(workspace, identity, new_baseline=False)
    semantic_hash = state.get("semantic_facts_sha256")
    if not workspace.semantic_facts.is_file() or semantic_hash != sha256_file(
        workspace.semantic_facts
    ):
        raise ValueError("semantic facts changed since the last analysis; run analyze first")
    return checkers


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
        checkers = _assert_current_baseline(config, workspace, actual_runner)
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


def _select_feedback_ids(ranking: Path, excluded: set[str]) -> list[str]:
    rows = []
    with ranking.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                row = json.loads(line)
                if row["alert_id"] not in excluded:
                    rows.append(row)
    rows.sort(key=lambda row: (-float(row["weight"]), str(row["type"]), row["alert_id"]))
    selected = rows[:10]
    if len(rows) > 10:
        selected.extend(rows[-10:])
    return list(dict.fromkeys(str(row["alert_id"]) for row in selected))


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
                        ids = _select_feedback_ids(ranking, selected_ids)
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
                        prepared_alerts, ranking, weighted = _prepare_prediction(
                            config,
                            workspace,
                            actual_runner,
                            prediction_stage,
                            staged_checkpoint,
                        )
                        checkpoint = model_dir / f"{round_id}.pt"
                        os.replace(staged_checkpoint, checkpoint)
                        manifest = {
                            "run_id": operation_id,
                            "round": round_number,
                            "checkpoint": str(checkpoint.relative_to(workspace.root)),
                            "sha256": sha256_file(checkpoint),
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
