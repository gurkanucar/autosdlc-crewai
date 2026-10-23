"""Translation catalogs: every source string is translated, jargon stays English and the language reaches the agents."""

import ast
import re
from pathlib import Path

import pytest
import yaml

from autosdlc.i18n import LANGUAGES, catalog, glossary, jargon_rule, normalize, set_language, t, translate_stored

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [ROOT / "streamlit_app.py", *sorted((ROOT / "src/autosdlc").glob("*.py"))]
LABEL_DICTS = {"STATUS_LABELS", "STATE_LABELS", "SECTION_ACTIONS", "CHANGE_STATUS", "EVENT_NAMES", "CHANGE_KINDS", "FORM_LABELS"}
# Words that are the same in English and Turkish, or protocol/status codes rather than text.
SAME_IN_TURKISH = {"Bug", "Normal", "Library", "CLI", "Frontend", "Backend", "Fullstack", "Redis", "SQLite", "PostgreSQL", "OpenTelemetry",
                   "OpenObserve", "README", "model", "Observability", "Web UI", "Cache"}


@pytest.fixture(autouse=True)
def english():
    set_language("en")
    yield
    set_language("en")


def _text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = ""
        for part in node.values:
            if isinstance(part, ast.Constant):
                out += part.value
            else:
                inner = part.value
                out += "{" + (inner.id if isinstance(inner, ast.Name) else inner.attr if isinstance(inner, ast.Attribute) else "value") + "}"
        return out
    return None


def source_strings() -> set[str]:
    """Every English text the code passes to t()/_t(), the label dictionaries and the stage table."""
    found: set[str] = set()
    for path in SOURCES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                if name in {"t", "_t"} and node.args and _text(node.args[0]):
                    found.add(_text(node.args[0]))
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in LABEL_DICTS for target in node.targets):
                for value in ast.walk(node.value):
                    if isinstance(value, ast.Dict):
                        for item in value.values:
                            for const in ast.walk(item):
                                if isinstance(const, ast.Constant) and isinstance(const.value, str) and " " in const.value + " " and re.search(r"[A-Za-z]{3}", const.value) and not re.fullmatch(r"[a-z_]+", const.value):
                                    found.add(const.value)
    from autosdlc.lifecycle import STAGES
    for stage in STAGES:
        found.update({stage.label, stage.agent, stage.description, stage.phase, stage.artifact})
    return found


def test_every_source_string_has_a_turkish_translation():
    turkish = catalog("tr")
    missing = sorted(text for text in source_strings() if text not in turkish and text not in SAME_IN_TURKISH
                     and not re.fullmatch(r"[\w.]+\.(md|yaml)|[a-z]+ ?[a-z]*", text))
    assert not missing, "Add these to config/locales/tr/*.yaml:\n" + "\n".join(missing)


def test_turkish_catalog_has_no_dead_placeholders():
    for source, target in catalog("tr").items():
        assert set(re.findall(r"\{(\w+)", source)) == set(re.findall(r"\{(\w+)", target)), source


def test_technical_jargon_is_never_translated():
    terms = glossary()["keep_english"]
    avoid = glossary()["avoid"]["tr"]
    for source, target in catalog("tr").items():
        for term in terms:
            if re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", source, re.IGNORECASE):
                assert re.search(re.escape(term), target, re.IGNORECASE), f"{term!r} was translated in: {source!r}"
        for bad in avoid:
            assert bad not in target.casefold(), f"{bad!r} must not appear in: {target!r}"


def test_prompts_carry_the_language_and_the_jargon_rule():
    tasks = yaml.safe_load((ROOT / "src/autosdlc/config/tasks.yaml").read_text(encoding="utf-8"))
    assert "$language" in tasks["language_rules"] and "$terms" in tasks["language_rules"]
    rule = jargon_rule("tr")
    assert "backend" in rule and "fullstack" in rule and '"arka uç"' in rule
    assert '"arka uç"' not in jargon_rule("en")
    for name, task in tasks.items():
        if name != "language_rules" and name not in {"development", "testing"}:
            assert "$language" in task["description"], name


def test_translation_falls_back_to_english_and_formats_placeholders():
    set_language("tr")
    assert t("Running") == "Çalışıyor"
    assert t("{count} rounds", count=2) == "2 tur"
    assert t("A text that was never translated") == "A text that was never translated"
    assert t("Running", language="en") == "Running"
    assert normalize("tr-TR") == "tr" and normalize("de") == "en" and normalize(None) == "en"


def test_stored_english_details_are_translated_when_displayed():
    assert translate_stored("OpenCode Go returned HTTP 400: bad request", "tr") == "OpenCode Go HTTP 400 döndürdü: bad request"
    assert translate_stored("2 sections updated, 0 sections added", "tr") == "2 bölüm güncellendi, 0 bölüm eklendi"
    assert translate_stored("round 1", "tr") == "tur 1"
    assert translate_stored("something unknown", "tr") == "something unknown"
    assert translate_stored("round 1", "en") == "round 1"


def test_language_names_are_listed():
    assert set(LANGUAGES) == {"en", "tr"} and LANGUAGES["tr"] == "Türkçe"
