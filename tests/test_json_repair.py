"""Model output with code inside JSON strings: the usual mistakes are repaired instead of failing the stage."""

import json

import pytest

from autosdlc.crewai_engine import _parse_file_bundle, _parse_plan

BAT = "@echo off\r\ncd /d %~dp0\r\ndist\\calc.exe %*\r\n"


def test_valid_json_is_untouched():
    raw = json.dumps({"files": {"run.bat": BAT, "main.go": 'fmt.Println("a\\nb")'}})
    assert _parse_file_bundle(raw) == {"run.bat": BAT, "main.go": 'fmt.Println("a\\nb")'}


def test_unescaped_backslashes_in_windows_paths_are_repaired():
    # In the raw model output: \r\n are proper escapes, while \c, \b-looking and \U, \x are Windows path separators.
    raw = r'{"files": {"run.bat": "@echo off\r\ndist\calc.exe %*\r\ncopy a\x C:\Users\x"}}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw, strict=False)
    files = _parse_file_bundle(raw)
    assert files["run.bat"] == "@echo off\r\ndist\\calc.exe %*\r\ncopy a\\x C:\\Users\\x"


def test_raw_newlines_and_tabs_inside_strings_are_accepted():
    raw = '{"files": {"main.py": "def f():\n\treturn 1\n"}}'
    assert _parse_file_bundle(raw) == {"main.py": "def f():\n\treturn 1\n"}


def test_fenced_and_chatty_output_with_bad_escapes():
    raw = 'Here you go:\n```json\n{"files": {"build.bat": "go build -o dist\\app.exe"}}\n```\nDone.'
    assert _parse_file_bundle(raw) == {"build.bat": "go build -o dist\\app.exe"}


def test_plan_with_bad_escapes():
    plan = _parse_plan('{"plan": [{"path": "run.bat", "purpose": "starts dist\\app.exe"}]}')
    assert plan[0]["path"] == "run.bat"


def test_garbage_still_fails():
    with pytest.raises(ValueError, match="valid JSON"):
        _parse_file_bundle('{"files": {"a": "unterminated')


def _engine(responder):
    """A CrewAIEngine without the CrewAI machinery: `_ask` is answered by the test."""
    from autosdlc.crewai_engine import CrewAIEngine

    engine = CrewAIEngine.__new__(CrewAIEngine)
    engine.asked = []

    def ask(stage, extra="", **values):
        engine.asked.append(extra)
        return responder(extra)

    engine._ask = ask
    return engine


def _plan(*paths):
    return json.dumps({"plan": [{"path": path, "purpose": "x"} for path in paths]})


def _files_in(extra):
    wanted = extra.split("NOW WRITE ONLY these files, completely: ")[1].split(". Return only")[0]
    return [name.strip() for name in wanted.split(",")]


def test_a_group_cut_off_mid_response_is_split_and_retried():
    paths = ["a.go", "b.go", "c.go", "d.go"]

    def respond(extra):
        if "FILE PLAN" not in extra:
            return _plan(*paths)
        names = _files_in(extra)
        if len(names) > 2:  # four files do not fit in one response: the model stops in the middle of a string
            return '{"files": {"a.go": "package main\nfunc big() {' + "x" * 50
        return json.dumps({"files": {name: f"// {name}" for name in names}})

    engine = _engine(respond)
    assert engine._ask_files_batched("development") == {name: f"// {name}" for name in paths}
    assert any("fewer and shorter" in extra for extra in engine.asked)


def test_a_single_file_that_never_parses_ends_the_stage():
    def respond(extra):
        return _plan("a.go", "b.go") if "FILE PLAN" not in extra else '{"files": {"a.go": "cut'

    with pytest.raises(ValueError, match="valid JSON"):
        _engine(respond)._ask_files_batched("development")


def test_first_bundle_wins_when_the_model_adds_prose_and_a_second_attempt():
    # Captured from a real run: valid JSON, then a self-critique and another block in the same response.
    raw = ('```json\n{"files": {"tests/a_test.go": "package tests\n"}}\n```\n\n'
           '**USER_QUESTION:** the JSON above was invalid\n```json\n{"files": {"tests/a_test.go": "second"}}\n```')
    assert _parse_file_bundle(raw) == {"tests/a_test.go": "package tests\n"}


def test_a_broken_first_bundle_falls_through_to_the_next_one():
    raw = '{"files": {"a.go": "cut\n\nTrying again:\n{"files": {"a.go": "whole"}}'
    assert _parse_file_bundle(raw) == {"a.go": "whole"}
