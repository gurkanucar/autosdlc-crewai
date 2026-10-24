"""Delivery decision: Docker only when chosen, executables built after passing tests, binaries kept out of bundles."""

import io
import zipfile
from pathlib import Path

import pytest
import yaml

from autosdlc.deployment import write_deployment_files
from autosdlc.exports import export_zip
from autosdlc.intake import default_delivery, validate_decisions, with_default_delivery
from autosdlc.project import read_project_manifest
from autosdlc.runner import run_project
from autosdlc.storage import History

ELF = b"\x7fELF" + b"\0" * 60
# Stands in for `go build`: cross-compiled executables in dist/, plus a stray binary at the root like `go build -o calc`.
BUILD_SCRIPT = (
    "from pathlib import Path\n"
    "Path('dist').mkdir(exist_ok=True)\n"
    "Path('dist/calc-linux-amd64').write_bytes(b'\\x7fELF' + bytes(60))\n"
    "Path('dist/calc-windows-amd64.exe').write_bytes(b'MZ' + bytes(62))\n"
    "Path('calc').write_bytes(b'\\x7fELF' + bytes(60))\n"
)


class CliEngine:
    """A passing Python CLI whose package command is the build script above (or a failing one)."""

    def __init__(self, package: str | None = "[python, build.py]", build: str = BUILD_SCRIPT):
        self.package = package
        self.build = build
        self.feedback = []

    def document(self, stage: str, idea: str, context: str) -> str:
        if stage == "analysis":
            return "# Requirements\n\nFR-01: add two numbers.\nNFR-01: start fast.\n"
        if stage == "design":
            return "# Design\n\n```mermaid\nflowchart LR\n A --> B\n```\n\n```mermaid\nsequenceDiagram\n A->>B: run\n```\n"
        if stage == "review":
            return "# Review\n\nVERDICT: APPROVE\n"
        return f"# {stage}\n\n{context[-400:]}\n"

    def code(self, idea: str, docs: str, feedback: str | None = None) -> dict[str, str]:
        self.feedback.append(feedback)
        manifest = "unit_test: [python, -m, pytest, -q, tests]\n" + (f"package: {self.package}\n" if self.package else "")
        return {"calc.py": "def add(a, b):\n    return a + b\n", "build.py": self.build, "project.yaml": manifest}

    def tests(self, idea: str, docs: str, files: dict[str, str]) -> dict[str, str]:
        return {"tests/test_calc.py": "from calc import add\n\ndef test_fr01_add():\n    assert add(2, 3) == 5\n"}


def _decisions(delivery: str) -> dict[str, str]:
    return {"project_type": "cli", "stack": "Python", "delivery": delivery, "scale": "small"}


def _run(tmp_path, monkeypatch, engine, delivery):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    result = run_project("Calculator", workspace=tmp_path, engine=engine, decisions=_decisions(delivery))
    events = History(tmp_path / "runs" / "history.sqlite3").get(result.id)["events"]
    return result, {(event["stage"], event["status"]): event["detail"] for event in events}


def test_defaults_follow_the_project_type():
    assert default_delivery("cli") == "binary"
    assert default_delivery("library") == "none"
    assert default_delivery("backend") == "docker"
    assert with_default_delivery({"project_type": "library"})["delivery"] == "none"
    assert with_default_delivery({"project_type": "cli", "delivery": "script"})["delivery"] == "script"


def test_projects_from_before_the_decision_stay_on_docker():
    old = {"project_type": "cli", "stack": "Go"}
    assert validate_decisions(old)["delivery"] == "docker"


def test_dockerfile_is_required_only_for_docker(tmp_path):
    (tmp_path / "project.yaml").write_text("unit_test: [go, test, ./...]\npackage: sh build.sh\n", encoding="utf-8")
    manifest = read_project_manifest(tmp_path, validate_decisions(_decisions("binary")))
    assert manifest["package"] == ["sh", "build.sh"]
    with pytest.raises(ValueError, match="Dockerfile"):
        read_project_manifest(tmp_path, validate_decisions(_decisions("docker")))


def test_compose_files_only_for_docker(tmp_path):
    write_deployment_files(tmp_path, validate_decisions(_decisions("binary")))
    assert not (tmp_path / "compose.yaml").exists() and not (tmp_path / ".dockerignore").exists()
    write_deployment_files(tmp_path, validate_decisions(_decisions("docker")))
    assert (tmp_path / "compose.yaml").is_file()


def test_binary_delivery_builds_executables_after_the_tests(tmp_path, monkeypatch):
    result, events = _run(tmp_path, monkeypatch, CliEngine(), "binary")

    assert result.status == "completed", result.error
    assert events[("deployment", "packaged")] == "calc-linux-amd64, calc-windows-amd64.exe"
    assert not (result.project_dir / "Dockerfile").exists() and not (result.project_dir / "compose.yaml").exists()
    readme = (result.project_dir / "README.md").read_text(encoding="utf-8")
    assert "dist/calc-windows-amd64.exe" in readme and "docker compose" not in readme
    # The deployment agent sees what was actually built.
    assert "calc-windows-amd64.exe" in (result.output_dir / "deployment.md").read_text(encoding="utf-8")
    # The stray root binary and dist/ stay out of the version snapshot and the download bundle.
    snapshot = result.output_dir / "iterations" / "v1" / "project"
    assert (snapshot / "calc.py").is_file() and not (snapshot / "calc").exists() and not (snapshot / "dist").exists()
    names = zipfile.ZipFile(io.BytesIO(export_zip(result.project_dir, result.output_dir, "all"))).namelist()
    assert "project/calc.py" in names
    assert not any(name.endswith("/calc") or "/dist/" in name for name in names)


def test_packaging_is_announced_before_it_runs(tmp_path, monkeypatch):
    # A Rust GUI release build can take many minutes; the deployment stage must show up before it starts.
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    result = run_project("Calculator", workspace=tmp_path, engine=CliEngine(), decisions=_decisions("binary"))
    events = [(event["stage"], event["status"]) for event in History(tmp_path / "runs" / "history.sqlite3").get(result.id)["events"]]
    order = [events.index(key) for key in (("testing", "passed"), ("deployment", "started"),
                                           ("deployment", "package_started"), ("deployment", "packaged"))]
    assert order == sorted(order)
    assert events.count(("deployment", "started")) == 1


def test_packaging_timeout_stops_the_whole_build(tmp_path, monkeypatch):
    # build.sh starts cargo, which starts rustc: a timeout must stop them all and must not wait for their output pipes.
    import time

    import autosdlc.runner

    monkeypatch.setattr(autosdlc.runner, "PACKAGE_TIMEOUT", 2)
    build = "import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\nimport time\ntime.sleep(60)\n"
    started = time.monotonic()
    engine = CliEngine(build=build)
    result, events = _run(tmp_path, monkeypatch, engine, "binary")
    assert result.status == "failed"
    assert "timed out" in events[("deployment", "package_failed")]
    assert time.monotonic() - started < 40
    # A timeout is not a code error: the developer is not asked to fix it.
    assert engine.feedback == [None]


def test_packaging_that_cannot_be_fixed_fails_the_project(tmp_path, monkeypatch):
    # The user asked for executables; passing tests without them is not a finished project.
    engine = CliEngine(build="raise SystemExit('error: could not compile `todo` (bin \"todo\")')\n")
    result, events = _run(tmp_path, monkeypatch, engine, "binary")
    assert result.status == "failed"
    assert "executables could not be built" in result.error
    assert events[("deployment", "package_failed")].startswith("exit 1")
    assert sum("RELEASE BUILD" in (feedback or "") for feedback in engine.feedback) == 2
    assert "No executables were built." in (result.project_dir / "README.md").read_text(encoding="utf-8")


def test_a_failed_release_build_goes_back_to_the_developer(tmp_path, monkeypatch):
    class FixableBuild(CliEngine):
        def code(self, idea, docs, feedback=None):
            files = super().code(idea, docs, feedback)
            if not (feedback and "RELEASE BUILD" in feedback):
                files["build.py"] = "raise SystemExit('src/main.rs:52:5: method `update` is not a member of trait')\n"
            return files

    engine = FixableBuild()
    result, events = _run(tmp_path, monkeypatch, engine, "binary")
    assert result.status == "completed", result.error
    assert "src/main.rs:52:5" in engine.feedback[-1]
    assert events[("deployment", "packaged")] == "calc-linux-amd64, calc-windows-amd64.exe"
    assert events[("testing", "passed")] == "after the packaging fix"


def test_binary_without_package_command_is_reported(tmp_path, monkeypatch):
    result, events = _run(tmp_path, monkeypatch, CliEngine(package=None), "binary")
    assert result.status == "completed", result.error
    assert ("deployment", "package_unavailable") in events
    assert not (result.project_dir / "dist").exists()


def test_script_delivery_runs_no_package_step(tmp_path, monkeypatch):
    result, events = _run(tmp_path, monkeypatch, CliEngine(), "script")
    assert result.status == "completed", result.error
    assert not any(stage == "deployment" and status.startswith("package") for stage, status in events)
    assert not (result.project_dir / "dist").exists()
    assert "run.bat" in (result.project_dir / "README.md").read_text(encoding="utf-8")
    assert yaml.safe_load((result.output_dir / "decisions.yaml").read_text(encoding="utf-8"))["delivery"] == "script"


def test_a_file_in_the_reserved_docs_folder_is_skipped_not_fatal(tmp_path, monkeypatch):
    class DocsEngine(CliEngine):
        def code(self, idea, docs, feedback=None):
            return {**super().code(idea, docs, feedback), "docs/NOTES.md": "# my own notes\n"}

    result, events = _run(tmp_path, monkeypatch, DocsEngine(), "binary")
    assert result.status == "completed", result.error
    assert events[("development", "files_skipped")] == "docs/NOTES.md"
    assert not (result.project_dir / "docs" / "NOTES.md").exists()


def test_go_mod_requires_are_stripped_so_tidy_resolves_real_versions():
    from autosdlc.runner import strip_go_requires

    go_mod = (
        "module gununsozu\n\ngo 1.24\n\n"
        "require (\n\tgithub.com/lxn/walk v0.0.0-20210112085537-c381da492f70\n\tgolang.org/x/sys v0.49.0 // indirect\n)\n\n"
        "require github.com/lxn/win v0.0.0-20210218163916-a377121e959e\n\n"
        "replace example.com/x => ../x\n"
    )
    assert strip_go_requires(go_mod) == "module gununsozu\n\ngo 1.24\n\n\n\nreplace example.com/x => ../x\n"
    assert strip_go_requires("module m\n\ngo 1.24\n") == "module m\n\ngo 1.24\n"
