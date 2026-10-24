"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import yaml

from .i18n import set_language, t
from .intake import applies, choice_label, parse_choice, question_text, questions, validate_decisions, with_default_delivery
from .runner import run_project
from .storage import History


def main(argv: list[str] | None = None) -> int:
    # Messages follow AUTOSDLC_LANG (default English); the language of the generated documents is the --language decision.
    set_language(os.getenv("AUTOSDLC_LANG"))
    parser = argparse.ArgumentParser(prog="autosdlc", description=t("SDLC project generator built with CrewAI"))
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help=t("Start a new SDLC run"))
    run.add_argument("--idea", help=t("Short project idea"))
    run.add_argument("--config", type=Path, help=t("YAML file with the idea, decisions and target folder"))
    run.add_argument("--model", help=t("OpenCode Go model id"))
    run.add_argument("--max-repairs", type=int, default=2)
    run.add_argument("--output", type=Path, help=t("New folder the project is created in"))
    run.add_argument("--no-prompt", action="store_true", help=t("Fail instead of asking when a decision is missing"))
    for name, item in questions().items():
        run.add_argument(f"--{name}", choices=item.get("options"))

    history = commands.add_parser("history", help=t("List the latest runs"))
    history.add_argument("--limit", type=int, default=20)
    show = commands.add_parser("show", help=t("Show a run and its stage events"))
    show.add_argument("run_id")

    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    if args.command == "run":
        try:
            request = {}
            if args.config:
                request = yaml.safe_load(args.config.read_text(encoding="utf-8"))
                if not isinstance(request, dict) or set(request) - {"idea", "decisions", "output", "model"}:
                    raise ValueError("The request YAML file may only contain idea, decisions, output and model")
            idea = args.idea or request.get("idea", "")
            if not idea and not args.no_prompt:
                idea = input(t("Which project should be created? ") + " ").strip()
            output = args.output or request.get("output")
            model = args.model or request.get("model")
            raw_decisions = request.get("decisions", {})
            if not isinstance(raw_decisions, dict):
                raise ValueError("decisions in the request YAML file must be a mapping")
            selected = raw_decisions.copy()
            selected.update({name: getattr(args, name) for name in questions() if getattr(args, name) is not None})
            if not args.no_prompt:
                for name, item in questions().items():
                    if not applies(item, with_default_delivery(selected)):
                        continue
                    if name not in selected:
                        while True:
                            choices = f" ({'/'.join(choice_label(name, value) for value in item['options'])})" if "options" in item else ""
                            answer = input(f"{question_text(name)}{choices}: ").strip()
                            if item.get("free_text") and (answer or item.get("optional")):
                                selected[name] = answer
                                break
                            if not answer and item.get("optional"):
                                break
                            choice = parse_choice(name, answer)
                            if choice in item.get("options", []):
                                selected[name] = choice
                                break
                            print(t("Enter one of the options: {options}", options=", ".join(choice_label(name, value) for value in item.get("options", []))) if "options" in item else t("Enter a value"))
            selected = validate_decisions(with_default_delivery(selected))
            result = run_project(
                idea,
                workspace=workspace,
                max_repairs=args.max_repairs,
                model=model,
                decisions=selected,
                output=output,
                on_question=(lambda _run_id, stage, question: input(t("The {stage} agent is asking: {question}", stage=stage, question=question) + "\n" + t("Answer") + ": ")) if not args.no_prompt else None,
            )
        except (RuntimeError, ValueError, EOFError, OSError, yaml.YAMLError) as exc:
            parser.exit(2, t("Error: {error}", error=exc) + "\n")
        print(json.dumps({
            "id": result.id,
            "status": result.status,
            "attempts": result.attempts,
            "output_dir": str(result.output_dir),
            "project_dir": str(result.project_dir),
            "error": result.error,
        }, ensure_ascii=False, indent=2))
        return 0 if result.status == "completed" else 1

    db = History(workspace / "runs" / "history.sqlite3")
    if args.command == "history":
        print(json.dumps(db.list(args.limit), ensure_ascii=False, indent=2))
        return 0
    item = db.get(args.run_id)
    if item is None:
        parser.exit(2, t("Run not found: {run_id}", run_id=args.run_id) + "\n")
    print(json.dumps(item, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

