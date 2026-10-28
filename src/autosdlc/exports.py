"""Portable download bundles of the project and the SDLC outputs."""

from __future__ import annotations

import io
import json
import os
import platform
import re
import sys
import zipfile
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .storage import clock, display_zone

SKIPPED_DIRS = {".autosdlc-venv", ".venv", "node_modules", ".git", "__pycache__",
                ".pytest_cache", "test-results", "dist", "build", "target"}
# Mach-O magic numbers (32/64 bit, both byte orders); 0xCAFEBABE is left out because Java class files share it.
MACH_O = {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"}


def is_compiled_binary(path: Path) -> bool:
    """ELF, Windows PE or Mach-O executable: a build output (e.g. `go build -o calc`), never source."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(64)
            if head[:4] == b"\x7fELF" or head[:4] in MACH_O:
                return True
            if head[:2] != b"MZ" or len(head) < 64:
                return False
            handle.seek(int.from_bytes(head[60:64], "little"))
            return handle.read(4) == b"PE\0\0"
    except OSError:
        return False


def export_files(root: Path) -> list[Path]:
    """Leaves dependencies, local secrets and symbolic links out of the bundle."""
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in SKIPPED_DIRS
                         and not (Path(directory) / name).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            secret = name == ".env" or (name.startswith(".env.") and name not in {".env.example", ".env.sample", ".env.template"})
            if not secret and not path.is_symlink() and path.is_file() and not is_compiled_binary(path):
                files.append(path.relative_to(root))
    return files


def export_zip(project: Path, output: Path, kind: str = "all") -> bytes:
    """all: project + all stage outputs; code: sources; documents: documents and reports."""
    if kind not in {"all", "code", "documents"}:
        raise ValueError(f"Unknown bundle: {kind}")
    entries: dict[str, Path] = {}
    if kind in {"all", "code"}:
        for relative in export_files(project):
            if kind == "code" and relative.parts[0] == "docs":
                continue
            entries[f"project/{relative.as_posix()}"] = project / relative
    if kind == "documents":
        for relative in export_files(project / "docs"):
            entries[f"documents/{relative.as_posix()}"] = project / "docs" / relative
        if (project / "README.md").is_file():
            entries["README.md"] = project / "README.md"
    if kind in {"all", "documents"}:
        for relative in export_files(output):
            if kind == "documents" and relative.suffix.lower() not in {".md", ".yaml", ".yml", ".txt", ".svg", ".png", ".pdf", ".html"}:
                continue
            prefix = "sdlc" if kind == "all" else "documents"
            entries[f"{prefix}/{relative.as_posix()}"] = output / relative
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(entries.items()):
            archive.write(path, name)
    return buffer.getvalue()


def project_zip(root: Path, files: list[Path]) -> bytes:
    """Zips the given files (relative to root), e.g. a version snapshot."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in files:
            archive.write(root / relative, relative.as_posix())
    return buffer.getvalue()


# The SDLC documents in reading order; the ones a run has not written yet are left out.
PDF_DOCUMENTS = ["planning.md", "requirements.md", "architecture.md", "design_review.md", "test_report.md",
                 "traceability.md", "deployment.md", "maintenance_report.md", "CHANGELOG.md"]
MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.esm.min.mjs"
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_PDF_STYLE = """
body {font-family: "DejaVu Sans", Arial, sans-serif; font-size: 10.5pt; line-height: 1.45; color: #1f2937;}
h1 {font-size: 18pt; border-bottom: 2px solid #e5e7eb; padding-bottom: 4pt;} h2 {font-size: 14pt;} h3 {font-size: 12pt;}
.document {break-before: page;} .cover {text-align: center; padding-top: 40pt;} .cover svg {width: 100%; height: auto;}
table {border-collapse: collapse; width: 100%; margin: 8pt 0; font-size: 9pt;} tr {break-inside: avoid;}
th, td {border: 1px solid #d1d5db; padding: 3pt 5pt; text-align: left; vertical-align: top;} th {background: #f3f4f6;}
pre {background: #f6f8fa; padding: 6pt; font-size: 8pt; white-space: pre-wrap; word-break: break-word;}
code {font-family: "DejaVu Sans Mono", monospace; font-size: 9pt;}
.mermaid {background: none; text-align: center; break-inside: avoid;} .mermaid svg {max-width: 100%; height: auto;}
.mermaid-failed {font-size: 8pt; color: #b91c1c;}
.toc {display: inline-block; text-align: left; margin-top: 24pt; font-size: 11pt;} .toc a {color: #1f2937; text-decoration: none;}
"""
# Draws every diagram; one with broken Mermaid code keeps its source under a note instead of stopping the others.
_PDF_SCRIPT = """<script type="module">
import mermaid from "%s";
mermaid.initialize({startOnLoad: false, theme: "neutral", securityLevel: "strict"});
for (const block of document.querySelectorAll("pre.mermaid")) {
  const source = block.textContent;
  try {
    const {svg} = await mermaid.render("diagram" + Math.random().toString(36).slice(2), source);
    block.innerHTML = svg;
  } catch (error) {
    block.classList.remove("mermaid");
    block.insertAdjacentHTML("beforebegin", '<p class="mermaid-failed">%s</p>');
  }
}
window.diagramsDone = true;
</script>"""


def documents_html(output: Path, title: str, lifecycle_svg: str | None = None,
                   failed_note: str = "The diagram could not be drawn; its source follows.") -> str:
    """One HTML page of every SDLC document: Markdown tables and code blocks, ```mermaid blocks left for the browser to draw."""
    from html import escape

    from markdown_it import MarkdownIt

    # html=False: the documents are model output, so any raw HTML in them is shown as text, never run.
    renderer = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    default_fence = renderer.renderer.rules["fence"]

    def fence(_renderer, tokens, index, options, env):
        token = tokens[index]
        if token.info.strip().lower() == "mermaid":
            return f'<pre class="mermaid">{escape(token.content)}</pre>\n'
        return default_fence(tokens, index, options, env)

    renderer.add_render_rule("fence", fence)
    sections, toc = [], []
    for number, name in enumerate(PDF_DOCUMENTS, 1):
        path = output / name
        if not path.is_file():
            continue
        text = _HTML_COMMENT.sub("", path.read_text(encoding="utf-8", errors="replace"))
        heading = next((line.lstrip("#").strip() for line in text.splitlines() if line.startswith("# ")), name)
        toc.append(f'<li><a href="#doc{number}">{escape(heading)}</a></li>')
        sections.append(f'<section class="document" id="doc{number}">{renderer.render(text)}</section>')
    cover = f'<section class="cover"><h1>{escape(title)}</h1>{lifecycle_svg or ""}<ol class="toc">{"".join(toc)}</ol></section>'
    script = _PDF_SCRIPT % (MERMAID_CDN, escape(failed_note).replace("'", "&#39;"))
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{escape(title)}</title>'
            f'<style>{_PDF_STYLE}</style></head><body>{cover}{"".join(sections)}{script}</body></html>')


def documents_pdf(html: str, timeout_ms: int = 60_000) -> bytes:
    """Prints the page with headless Chromium after Mermaid has drawn the diagrams. Only the Mermaid CDN is reachable:
    the documents are model output and must not load anything else."""
    from playwright.sync_api import sync_playwright

    host = MERMAID_CDN.split("/")[2]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.continue_() if route.request.url.split("/")[2:3] == [host]
                       or route.request.url.startswith("data:") else route.abort())
            page.set_content(html, wait_until="load")
            if 'class="mermaid"' in html:
                try:
                    page.wait_for_function("window.diagramsDone === true", timeout=timeout_ms)
                except Exception:
                    pass  # CDN unreachable: the diagrams stay as source code, the rest of the PDF is still useful.
            return page.pdf(format="A4", print_background=True, display_header_footer=True,
                            header_template="<span></span>",
                            footer_template='<div style="font-size:8pt;width:100%;text-align:center;color:#6b7280">'
                                            '<span class="pageNumber"></span> / <span class="totalPages"></span></div>',
                            margin={"top": "16mm", "bottom": "18mm", "left": "14mm", "right": "14mm"})
        finally:
            browser.close()


# Per file in the failure report: logs can grow large, and their end holds the error.
DIAGNOSTIC_FILE_LIMIT = 2_000_000
SECRET_PATTERNS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1***"),
    # Only environment-style assignments (PGPASSWORD=x, export API_KEY=x), not code such as `const TOKEN_KEY = 'token'`.
    (re.compile(r"\b([A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD)[A-Z0-9_]*=)[^\s'\"]+"), r"\1***"),
    (re.compile(r"(://[^\s:/@]+:)[^\s@/]+(@)"), r"\1***\2"),
    (re.compile(r"(sk-)[A-Za-z0-9_-]{8,}"), r"\1***"),
]


def redact(text: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# Terminal color codes: a tool that forces color (pnpm passes FORCE_COLOR to its scripts) wraps every character of a
# code frame in them, which buries the error the agent has to fix.
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def plain(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def _tail(path: Path, limit: int = DIAGNOSTIC_FILE_LIMIT) -> str:
    data = path.read_bytes()
    text = plain(data[-limit:].decode("utf-8", errors="replace"))
    if len(data) > limit:
        text = f"[... first {len(data) - limit} bytes left out ...]\n" + text
    return redact(text)


def diagnostic_files(output: Path) -> dict[str, Path]:
    """What shows where a run broke: the traceback, the model call log, build/test/server logs and live agent output.

    Generated code and the throwaway database data are left out; the code has its own download.
    """
    files: dict[str, Path] = {}
    if not output.is_dir():
        return files
    for path in sorted(output.iterdir()):
        if path.is_file() and path.suffix in {".txt", ".jsonl", ".yaml", ".md"}:
            files[path.name] = path
    for folder in ("build_checks", "test_attempts", "live"):
        for path in sorted((output / folder).glob("*")) if (output / folder).is_dir() else []:
            if path.is_file():
                files[f"{folder}/{path.name}"] = path
    tmp = output / "tmp"
    for path in sorted(tmp.rglob("*.log")) if tmp.is_dir() else []:
        if path.is_file() and not path.is_symlink():
            files[path.relative_to(output).as_posix()] = path
    return files


def _app_version() -> str:
    try:
        return version("autosdlc")
    except PackageNotFoundError:
        return "unknown"


def diagnostics_summary(row: dict, files: dict[str, Path]) -> str:
    from .runner import SDLC_STAGES, completed_from_events  # The runner imports this module.

    events = row.get("events", [])
    completed = completed_from_events(events)
    stopped_at = next((stage for stage in SDLC_STAGES if stage not in completed), "-")
    problems = [event for event in events if event["status"] in {"failed", "package_failed", "gate_failed", "interrupted"}]
    lines = [
        f"# Failure report · {row.get('title') or row['idea'][:80]}",
        "",
        f"- Run: `{row['id']}` · status **{row['status']}** · model `{row.get('model') or '-'}`",
        f"- Created {clock(row['created_at'], True)} · finished {clock(row['completed_at'], True) if row.get('completed_at') else '-'}"
        f" · test attempts {row.get('attempts', 0)} · times in {display_zone()}",
        f"- Stopped at stage: **{stopped_at}** · completed: {', '.join(s for s in SDLC_STAGES if s in completed) or '-'}",
        f"- AutoSDLC {_app_version()} · Python {platform.python_version()} · {sys.platform}",
        "",
        "## Error",
        "",
        "```",
        redact(row.get("error") or "(none recorded)"),
        "```",
        "",
        "## Problem events",
        "",
    ]
    lines += [f"- {clock(event['created_at'], True)} · {event['stage']} · {event['status']}: {redact(event['detail'])[:600]}"
              for event in problems[-20:]] or ["- (none)"]
    lines += ["", "## Where to look", "",
              "- `failure.txt`: the full traceback of every failure (which function raised the error).",
              "- `llm_calls.jsonl`: one line per model request (stage, outcome, HTTP status, finish_reason, sizes, duration).",
              "- `timeline.txt`: every event in order, with full details.",
              "- `build_checks/`: the full output of every failed compile check (the event keeps only its end).",
              "- `tmp/*.log`, `test_attempts/`: compile, test, server and packaging output.",
              "- `live/`: the last streamed output of each agent.",
              "", "## Included files", ""]
    lines += [f"- `{name}`" for name in files] or ["- (none)"]
    return "\n".join(lines) + "\n"


def _call_line(record: dict) -> str:
    fields = [clock(record["time"]) if record.get("time") else "", record.get("stage", ""), record.get("outcome", ""),
              f"finish={record.get('finish_reason')}", f"{record.get('seconds')}s", f"prompt={record.get('prompt_chars')}",
              f"response={record.get('response_chars', '-')}", f"reasoning={record.get('reasoning_chars', '-')}"]
    if record.get("error"):
        fields.append(str(record["error"])[:200])
    return " · ".join(str(field) for field in fields)


def diagnostics_text(row: dict) -> str:
    """The failure report as one short text to paste into a chat or an issue: the summary, the latest model calls and
    the end of the latest build, test and traceback output."""
    output = Path(row["output_dir"])
    text = diagnostics_summary(row, {}).split("## Where to look")[0].rstrip()
    text += "\n\n## Latest events\n\n" + "\n".join(
        f"- {clock(event['created_at'])} · {event['stage']} · {event['status']}: {redact(plain(event['detail']))[-200:].strip()}"
        for event in row.get("events", [])[-8:])
    calls_path = output / "llm_calls.jsonl"
    if calls_path.is_file():
        calls = []
        for line in calls_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]:
            try:
                calls.append(_call_line(json.loads(line)))
            except ValueError:
                continue
        text += "\n\n## Latest model calls\n\n" + "\n".join(f"- {call}" for call in calls)
    latest = [sorted((output / folder).glob("*.txt"))[-1:] if (output / folder).is_dir() else [] for folder in ("build_checks", "test_attempts")]
    for title, paths, limit in [("Latest compile check", latest[0], 3000), ("Latest test attempt", latest[1], 3000),
                                ("Traceback", [output / "failure.txt"], 2500)]:
        if paths and paths[0].is_file():
            # The time shows when an older file (e.g. a compile check from before the tests) is not the latest step.
            written = clock(datetime.fromtimestamp(paths[0].stat().st_mtime, timezone.utc).isoformat(), True)
            text += f"\n\n## {title} ({paths[0].name}, {written}, end)\n\n```\n{_tail(paths[0], limit).strip()}\n```"
    return text + "\n"


def diagnostics_zip(row: dict, changes: list[dict] | None = None, iterations: list[dict] | None = None) -> bytes:
    """One ZIP to see where a run broke; secrets like API keys and tokens are masked."""
    output = Path(row["output_dir"])
    files = diagnostic_files(output)
    timeline = "\n".join(f"{clock(event['created_at'], True)}\t{event['stage']}\t{event['status']}\t{event['detail']}"
                         for event in row.get("events", []))
    record = {**row, "changes": changes or [], "iterations": iterations or []}
    project = Path(row["project_dir"]) if row.get("project_dir") else None
    listing = ""
    if project and project.is_dir():
        listing = "\n".join(f"{(project / path).stat().st_size:>10}  {path.as_posix()}" for path in export_files(project))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("summary.md", diagnostics_summary(row, files))
        archive.writestr("run.json", redact(json.dumps(record, ensure_ascii=False, indent=2, default=str)))
        archive.writestr("timeline.txt", redact(timeline) + "\n")
        archive.writestr("project_files.txt", listing + "\n")
        if project and project.is_dir():
            # The tests and the commands that run them: a failure often comes from how the two fit together.
            for path in export_files(project):
                name = path.as_posix()
                if (name == "project.yaml" or name.startswith(("tests/", "test/", "scripts/"))) and (
                        (project / path).stat().st_size <= 100_000):
                    try:
                        archive.writestr(f"project/{name}", redact((project / path).read_text(encoding="utf-8")))
                    except (OSError, UnicodeDecodeError):
                        continue
        for name, path in files.items():
            try:
                archive.writestr(name, _tail(path))
            except OSError as exc:
                archive.writestr(name + ".unreadable", str(exc))
    return buffer.getvalue()
