"""Komut satırı arayüzü."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from .intake import choice_label, parse_choice, questions, validate_decisions
from .runner import run_project
from .storage import History


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autosdlc", description="CrewAI ile SDLC proje üreticisi")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Yeni SDLC çalışması başlat")
    run.add_argument("--idea", help="Kısa proje fikri")
    run.add_argument("--config", type=Path, help="Fikir, kararlar ve hedef klasörü içeren YAML dosyası")
    run.add_argument("--model", help="OpenCode Go model kimliği")
    run.add_argument("--max-repairs", type=int, default=2)
    run.add_argument("--output", type=Path, help="Projeyi oluşturulacak yeni klasör")
    run.add_argument("--no-prompt", action="store_true", help="Eksik karar varsa soru sormadan hata ver")
    for name, item in questions().items():
        run.add_argument(f"--{name}", choices=item.get("options"))

    history = commands.add_parser("history", help="Son çalışmaları listele")
    history.add_argument("--limit", type=int, default=20)
    show = commands.add_parser("show", help="Bir çalışmayı ve aşama olaylarını göster")
    show.add_argument("run_id")

    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    if args.command == "run":
        try:
            request = {}
            if args.config:
                request = yaml.safe_load(args.config.read_text(encoding="utf-8"))
                if not isinstance(request, dict) or set(request) - {"idea", "decisions", "output", "model"}:
                    raise ValueError("İstek YAML dosyasında yalnızca idea, decisions, output ve model olabilir")
            idea = args.idea or request.get("idea", "")
            if not idea and not args.no_prompt:
                idea = input("Hangi proje oluşturulsun? ").strip()
            output = args.output or request.get("output")
            model = args.model or request.get("model")
            raw_decisions = request.get("decisions", {})
            if not isinstance(raw_decisions, dict):
                raise ValueError("İstek YAML dosyasındaki decisions bir sözlük olmalı")
            selected = raw_decisions.copy()
            selected.update({name: getattr(args, name) for name in questions() if getattr(args, name) is not None})
            if not args.no_prompt:
                for name, item in questions().items():
                    if item.get("when") and selected.get("project_type") not in item["when"]:
                        continue
                    if name not in selected:
                        while True:
                            choices = f" ({'/'.join(choice_label(name, value) for value in item['options'])})" if "options" in item else ""
                            answer = input(f"{item['question']}{choices}: ").strip()
                            if item.get("free_text") and (answer or item.get("optional")):
                                selected[name] = answer
                                break
                            choice = parse_choice(name, answer)
                            if choice in item.get("options", []):
                                selected[name] = choice
                                break
                            print(f"Seçeneklerden birini gir: {', '.join(choice_label(name, value) for value in item.get('options', []))}" if "options" in item else "Bir değer gir")
            selected = validate_decisions(selected)
            result = run_project(
                idea,
                workspace=workspace,
                max_repairs=args.max_repairs,
                model=model,
                decisions=selected,
                output=output,
                on_question=(lambda _run_id, stage, question: input(f"{stage} ajanı soruyor: {question}\nYanıt: ")) if not args.no_prompt else None,
            )
        except (RuntimeError, ValueError, EOFError, OSError, yaml.YAMLError) as exc:
            parser.exit(2, f"Hata: {exc}\n")
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
        parser.exit(2, f"Çalışma bulunamadı: {args.run_id}\n")
    print(json.dumps(item, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

