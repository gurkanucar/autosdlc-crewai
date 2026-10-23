import io
import zipfile

from autosdlc.exports import export_zip


def test_packages_include_all_sources_and_latest_stage_documents(tmp_path):
    project, output = tmp_path / "project", tmp_path / "run"
    (project / "docs").mkdir(parents=True)
    output.mkdir()
    (project / "README.md").write_text("# Proje", encoding="utf-8")
    (project / "docs" / "planning.md").write_text("old", encoding="utf-8")
    (output / "planning.md").write_text("latest", encoding="utf-8")
    (output / "test_report.md").write_text("passed", encoding="utf-8")
    for index in range(305):
        (project / f"source{index}.py").write_text("pass", encoding="utf-8")
    for name in (".env", ".env.local", ".env.production"):
        (project / name).write_text("SECRET=private", encoding="utf-8")
    (project / ".env.example").write_text("SECRET=", encoding="utf-8")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "dependency.js").write_text("ignored", encoding="utf-8")
    with zipfile.ZipFile(io.BytesIO(export_zip(project, output))) as archive:
        names = archive.namelist()
        assert "project/source304.py" in names
        assert "sdlc/test_report.md" in names
        assert "project/.env.example" in names
        assert not any("node_modules" in name or name.endswith(("/.env", ".env.local", ".env.production")) for name in names)
    with zipfile.ZipFile(io.BytesIO(export_zip(project, output, "documents"))) as archive:
        assert archive.read("documents/planning.md") == b"latest"
        assert "README.md" in archive.namelist()
        assert not any(name.endswith(".py") for name in archive.namelist())
    with zipfile.ZipFile(io.BytesIO(export_zip(project, output, "code"))) as archive:
        assert len([name for name in archive.namelist() if name.endswith(".py")]) == 305
        assert not any("/docs/" in name for name in archive.namelist())
