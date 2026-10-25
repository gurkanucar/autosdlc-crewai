"""A reasoning model that deliberates without answering is stopped, nudged once, and then reported."""

import json

import httpx
import pytest

from autosdlc.crewai_engine import BUDGET_NUDGE, REASONING_BUDGET, TEST_PLAN_INSTRUCTION, CrewAIEngine


class Stream:
    status_code, headers = 200, {"content-type": "text/event-stream"}

    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        yield from self.lines


def _overthinking():
    chunk = "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": "x" * 1000}}]})
    return [chunk] * (REASONING_BUDGET // 1000 + 5)


def _answer(text):
    return ["data: " + json.dumps({"choices": [{"delta": {"reasoning_content": "short"}}]}),
            "data: " + json.dumps({"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}),
            "data: [DONE]"]


def _engine(monkeypatch, responses, stage="testing"):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setattr("autosdlc.crewai_engine.time.sleep", lambda seconds: None)
    payloads = []

    def fake_stream(method, url, *, headers, json, timeout):
        payloads.append(json)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return Stream(response)

    monkeypatch.setattr("autosdlc.crewai_engine.httpx.stream", fake_stream)
    engine = CrewAIEngine(api_key="test-key")
    engine.llm.stage = stage
    engine.llm.listener = lambda text: None
    return engine, payloads


def test_overthinking_is_cut_and_the_model_is_told_to_answer(monkeypatch):
    engine, payloads = _engine(monkeypatch, [_overthinking(), _answer("OK")])
    assert engine.llm.call("write tests") == "OK"
    assert len(payloads) == 2
    assert BUDGET_NUDGE not in payloads[0]["messages"][-1]["content"]
    assert payloads[1]["messages"][-1]["content"].endswith(BUDGET_NUDGE)


def test_a_model_that_keeps_overthinking_names_the_stage_to_change(monkeypatch):
    engine, _payloads = _engine(monkeypatch, [_overthinking(), _overthinking()])
    with pytest.raises(RuntimeError, match="reasoned more than .* choose another model for the testing stage"):
        engine.llm.call("write tests")


def test_code_stages_ask_for_low_reasoning_effort(monkeypatch):
    engine, payloads = _engine(monkeypatch, [_answer("OK"), _answer("OK")])
    engine.llm.call("write tests")
    engine.llm.stage = "planning"
    engine.llm.call("write a plan")
    assert payloads[0]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in payloads[1]


def test_a_gateway_that_rejects_reasoning_effort_is_called_without_it(monkeypatch):
    request = httpx.Request("POST", "https://example.test")
    rejected = httpx.HTTPStatusError("bad", request=request,
                                     response=httpx.Response(400, request=request, text="unknown reasoning_effort"))
    engine, payloads = _engine(monkeypatch, [rejected, _answer("OK"), _answer("OK")])
    assert engine.llm.call("write tests") == "OK"
    assert "reasoning_effort" in payloads[0] and "reasoning_effort" not in payloads[1]
    engine.llm.call("write more tests")
    assert "reasoning_effort" not in payloads[2]


def test_the_first_test_suite_is_planned_and_written_in_groups():
    engine = CrewAIEngine.__new__(CrewAIEngine)
    asked = []

    def ask(stage, extra="", **values):
        asked.append(extra)
        if extra == TEST_PLAN_INSTRUCTION:
            return json.dumps({"plan": [{"path": f"tests/t{index}.py", "purpose": "FR-01"} for index in range(4)]})
        wanted = extra.split("NOW WRITE ONLY these files, completely: ")[1].split(". Return only")[0]
        return json.dumps({"files": {name.strip(): "def test(): pass\n" for name in wanted.split(",")}})

    engine._ask = ask
    files = engine.tests("Book tracker", "docs", {"app.py": "x = 1"})
    assert sorted(files) == [f"tests/t{index}.py" for index in range(4)]
    assert len(asked) == 3  # the plan, then groups of three and one


def test_files_written_instead_of_a_plan_are_kept():
    engine = CrewAIEngine.__new__(CrewAIEngine)
    asked = []

    def ask(stage, extra="", **values):
        asked.append(extra)
        return json.dumps({"files": {"tests/t.py": "def test(): pass\n"}})

    engine._ask = ask
    assert engine.tests("Book tracker", "docs", {}) == {"tests/t.py": "def test(): pass\n"}
    assert len(asked) == 1


def test_test_repairs_still_use_a_single_response():
    engine = CrewAIEngine.__new__(CrewAIEngine)
    asked = []

    def ask(stage, extra="", **values):
        asked.append(extra)
        return json.dumps({"files": {"tests/t.py": "def test(): pass\n"}})

    engine._ask = ask
    assert engine.tests("Book tracker", "docs", {}, feedback="SyntaxError") == {"tests/t.py": "def test(): pass\n"}
    assert len(asked) == 1 and "DO NOT COMPILE" in asked[0]
