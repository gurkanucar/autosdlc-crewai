"""project.yaml of a CLI/library: server-only keys the model fills in anyway must not reach the runner."""

import yaml

from autosdlc.intake import validate_decisions
from autosdlc.project import read_project_manifest

GO_CLI_MANIFEST = {
    "port": 8080,
    "unit_test": ["go", "test", "./..."],
    "api_test": ["./calc", "6", "/", "3"],
    "ui_test": ["./calc"],
    "start": ["./calc"],
}


def _project(tmp_path, manifest):
    (tmp_path / "project.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    return tmp_path


def test_cli_manifest_keeps_only_the_unit_test(tmp_path):
    decisions = validate_decisions({"project_type": "cli", "stack": "Go 1.24"})
    manifest = read_project_manifest(_project(tmp_path, GO_CLI_MANIFEST), decisions)
    assert manifest["unit_test"] == ["go", "test", "./..."]
    assert not {"api_test", "ui_test", "start"} & set(manifest)


def test_library_manifest_keeps_only_the_unit_test(tmp_path):
    decisions = validate_decisions({"project_type": "library", "stack": "Python"})
    manifest = read_project_manifest(_project(tmp_path, GO_CLI_MANIFEST), decisions)
    assert not {"api_test", "ui_test", "start"} & set(manifest)


def test_backend_manifest_is_untouched(tmp_path):
    decisions = validate_decisions({"project_type": "backend", "stack": "Python", "database": "sqlite", "cache": "none",
                                    "observability": "none", "ui": "none"})
    manifest = read_project_manifest(_project(tmp_path, GO_CLI_MANIFEST), decisions)
    assert manifest["api_test"] and manifest["start"]
