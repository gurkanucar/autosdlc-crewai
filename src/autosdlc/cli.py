"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runner import run_project
from .storage import History


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autosdlc", description="SDLC classroom workflow")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Generate a new SDLC run")
    run.add_argument("--idea", required=True, help="A short project idea")
    run.add_argument("--model", help="OpenCode Go Chat Completions model ID")
    run.add_argument("--max-repairs", type=int, default=2)

    history = commands.add_parser("history", help="List recent runs")
    history.add_argument("--limit", type=int, default=20)
    show = commands.add_parser("show", help="Show one run and its stage events")
    show.add_argument("run_id")

    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    if args.command == "run":
        try:
            result = run_project(
                args.idea,
                workspace=workspace,
                max_repairs=args.max_repairs,
                model=args.model,
            )
        except (RuntimeError, ValueError) as exc:
            parser.exit(2, f"Error: {exc}\n")
        print(json.dumps({
            "id": result.id,
            "status": result.status,
            "attempts": result.attempts,
            "output_dir": str(result.output_dir),
            "error": result.error,
        }, ensure_ascii=False, indent=2))
        return 0 if result.status == "completed" else 1

    db = History(workspace / "runs" / "history.sqlite3")
    if args.command == "history":
        print(json.dumps(db.list(args.limit), ensure_ascii=False, indent=2))
        return 0
    item = db.get(args.run_id)
    if item is None:
        parser.exit(2, f"Run not found: {args.run_id}\n")
    print(json.dumps(item, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

