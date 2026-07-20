"""CLI entry point for the loopback-only local UI."""
import argparse
import sys


def build_parser():
    parser = argparse.ArgumentParser(description="Local HTML UI for one workflow.ini")
    parser.add_argument("--config", required=True, help="workflow.ini to bind at startup")
    parser.add_argument("--port", type=int, default=8765, help="loopback port (default: 8765)")
    return parser


def main():
    if sys.version_info < (3, 10):
        print("error: local_ui requires Python 3.10 or newer", file=sys.stderr)
        return 2
    args = build_parser().parse_args()
    try:
        from .server import serve

        serve(args.config, args.port)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"error: cannot start local UI: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
