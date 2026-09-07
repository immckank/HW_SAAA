"""Command-line interface for the single-project orchestrator."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .services import (
    ActiveLearningRequest,
    AnalyzeRequest,
    ImportXlsxRequest,
    ProgressEvent,
    TriageRequest,
    analyze,
    import_xlsx,
    run_active_learning,
    triage,
)
from .xlsx_import import DEFAULT_PRODUCER


def _progress(event: ProgressEvent) -> None:
    print(f"[{event.phase}] {event.message}", file=sys.stderr)


def _read_alert_ids(
    values: list[str], grouped_values: list[str], path: str | None
) -> tuple[str, ...]:
    result = [*values, *grouped_values]
    if path:
        with Path(path).expanduser().open(encoding="utf-8") as stream:
            result.extend(line.strip() for line in stream if line.strip())
    return tuple(dict.fromkeys(result))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Single-project warning workflow")
    parser.add_argument("--config", default="workflow.ini", help="three-entry project INI")
    commands = parser.add_subparsers(dest="command", required=True)

    analyze_parser = commands.add_parser("analyze", help="run SVF and reconcile warnings")
    analyze_parser.add_argument(
        "--checkers",
        default="leak,dfree,uaf,uninit,bof",
        help="comma-separated checker names",
    )
    analyze_parser.add_argument("--new-baseline", action="store_true")

    triage_parser = commands.add_parser("triage", help="classify selected alerts")
    triage_parser.add_argument(
        "--mode", choices=("classify", "expand-semantics"), required=True
    )
    triage_parser.add_argument("--alert", action="append", default=[], metavar="ALERT_ID")
    triage_parser.add_argument(
        "--alerts", nargs="+", default=[], metavar="ALERT_ID",
        help="one or more alert IDs",
    )
    triage_parser.add_argument("--alerts-file")
    triage_parser.add_argument("--round-id")
    triage_parser.add_argument("--classification-source", default="fphandler")

    active = commands.add_parser("active-learning", help="weight active alerts")
    active.add_argument("--rounds", required=True, type=int)
    active.add_argument("--feedback", required=True, choices=("none", "fphandler"))
    active.add_argument("--initial-model", required=True, help="random, latest, or checkpoint path")

    import_parser = commands.add_parser(
        "import-xlsx", help="import tabular SAST spreadsheet alerts"
    )
    import_parser.add_argument("--xlsx", required=True, help="path to xlsx alert list")
    import_parser.add_argument("--producer", default=DEFAULT_PRODUCER)
    import_parser.add_argument("--initial-weight", type=float, default=0.5)
    import_parser.add_argument(
        "--mode",
        choices=("merge", "replace-producer"),
        default="merge",
        help="merge keeps existing IDs; replace-producer clears this producer first",
    )
    import_parser.add_argument(
        "--no-path-filter",
        action="store_true",
        help="import all rows without matching source_dir",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "analyze":
            checkers = tuple(item.strip() for item in args.checkers.split(",") if item.strip())
            result = analyze(
                AnalyzeRequest(args.config, checkers, args.new_baseline), progress=_progress
            )
        elif args.command == "triage":
            alert_ids = _read_alert_ids(args.alert, args.alerts, args.alerts_file)
            result = triage(
                TriageRequest(
                    config_path=args.config,
                    alert_ids=alert_ids,
                    mode=args.mode,
                    round_id=args.round_id,
                    classification_source=args.classification_source,
                ),
                progress=_progress,
            )
        elif args.command == "import-xlsx":
            result = import_xlsx(
                ImportXlsxRequest(
                    config_path=args.config,
                    xlsx_path=args.xlsx,
                    producer=args.producer,
                    initial_weight=args.initial_weight,
                    path_filter=not args.no_path_filter,
                    mode=args.mode,
                ),
                progress=_progress,
            )
        else:
            result = run_active_learning(
                ActiveLearningRequest(
                    config_path=args.config,
                    rounds=args.rounds,
                    feedback=args.feedback,
                    initial_model=args.initial_model,
                ),
                progress=_progress,
            )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
