from __future__ import annotations

import argparse
import importlib
import time
from pathlib import Path
from typing import Sequence


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="validate_vertical",
        description="CLI helper for running shipped vertical demos",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    orchestration = subparsers.add_parser(
        "orchestration", help="Validate the orchestration code analysis vertical"
    )
    orchestration.add_argument(
        "--input",
        required=True,
        metavar="PATH",
        help="Path to the Python module or package to analyze",
    )
    orchestration.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would run without starting any agents",
    )

    choreography = subparsers.add_parser(
        "choreography", help="Validate the choreography research vertical"
    )
    choreography.add_argument(
        "--topic",
        required=True,
        metavar="TOPIC",
        help="Research topic to run through the choreography workflow",
    )
    choreography.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would run without starting any agents",
    )

    args = parser.parse_args(argv)

    if args.command == "orchestration":
        path = Path(args.input).expanduser()
        if not path.exists():
            parser.error(f"input path {path} does not exist")
        args.input = path
    else:
        topic = args.topic.strip()
        if not topic:
            parser.error("topic must not be empty")
        args.topic = topic

    return args


def _print_dry_run(command: str, detail: str) -> None:
    print(f"DRY RUN: {command} would execute with {detail}")
    raise SystemExit(0)


def _invoke_entry_point(module_name: str, forwarded_args: Sequence[str]) -> int:
    module = importlib.import_module(module_name)
    entry_main = getattr(module, "main", None)
    if entry_main is None:
        raise RuntimeError(f"Module {module_name} is missing a main() function")
    result = entry_main(forwarded_args)
    if isinstance(result, int):
        return int(result)
    return 0


def _format_elapsed(elapsed: float, exit_code: int) -> str:
    return f"Elapsed {elapsed:.2f} seconds | exit status {exit_code}"


def _run_orchestration(args: argparse.Namespace) -> int:
    if args.dry_run:
        _print_dry_run("orchestration", f"input={args.input}")
    return _invoke_entry_point("orchestration.code_analysis", [str(args.input)])


def _run_choreography(args: argparse.Namespace) -> int:
    if args.dry_run:
        _print_dry_run("choreography", f"topic={args.topic}")
    return _invoke_entry_point("choreography.research", [args.topic])


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_arguments(argv)
    start = time.perf_counter()
    executor = _run_orchestration if args.command == "orchestration" else _run_choreography
    exit_code = 0
    dry_run = bool(getattr(args, "dry_run", False))
    try:
        exit_code = executor(args)
    except SystemExit as exc:  # propagate entry point exit codes
        exit_code = int(exc.code or 0)
        if dry_run:
            raise SystemExit(exit_code)
    except Exception as exc:  # noqa: BLE001
        print(f"{exc.__class__.__name__}: {exc}")
        elapsed = time.perf_counter() - start
        print(_format_elapsed(elapsed, 1))
        raise SystemExit(1) from exc
    elapsed = time.perf_counter() - start
    print(_format_elapsed(elapsed, exit_code))
    raise SystemExit(exit_code)


if __name__ == "__main__":  # pragma: no cover
    main()
