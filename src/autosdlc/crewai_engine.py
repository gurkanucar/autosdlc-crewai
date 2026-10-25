"""CrewAI agents defined in YAML that use OpenCode Go."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from importlib.resources import files as resource_files
from string import Template
from typing import Any, Callable

import httpx
from dotenv import load_dotenv

from .i18n import jargon_rule, prompt_language
from .storage import now


GO_BASE_URL = "https://opencode.ai/inference/go/openai/v1"
DEFAULT_MODEL = "glm-5.3-flash"
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
# How many reasoning characters a model may stream before its first answer word. A reasoning model can otherwise
# deliberate for half an hour (241k characters were seen on a single test-writing call); about 6 minutes on a fast model.
REASONING_BUDGET = int(os.getenv("AUTOSDLC_REASONING_BUDGET", "60000"))
# Stages whose output is code: the plan is already in the prompt, so long hidden reasoning adds little.
REASONING_EFFORT = {"development": "low", "testing": "low"}
BUDGET_NUDGE = (
    "You have already reasoned enough about this task. Do not re-check the rules or the plan again; "
    "write the final answer now, in the required format."
)


class ReasoningBudgetExceeded(RuntimeError):
    """The model streamed more reasoning than REASONING_BUDGET without starting its answer."""


class StopRequested(RuntimeError):
    """The user pressed Stop while a model call was running; the call is abandoned."""


def _stream_completion(url: str, headers: dict[str, str], payload: dict, on_text: Callable[[str], None],
                       stats: dict | None = None, on_progress: Callable[[dict], None] | None = None,
                       should_stop: Callable[[], bool] | None = None,
                       reasoning_budget: int | None = None) -> tuple[str, str | None]:
    """Reads the SSE stream and passes the accumulated text to on_text on every chunk; falls back to plain JSON if the server does not stream.

    Returns (text, finish_reason). stats, if given, receives how many reasoning and answer characters the model
    streamed and the end of its reasoning: a reasoning model can think for many minutes before the first answer word,
    or spend its whole output on reasoning and return no content. on_progress gets stats after every chunk.
    With reasoning_budget the call is abandoned (ReasoningBudgetExceeded) once the model has reasoned that many
    characters without writing any answer; an answer already being written is never cut.
    """
    stats = {} if stats is None else stats
    stats.update(reasoning_chars=0, content_chars=0, reasoning_tail="")
    with httpx.stream("POST", url, headers=headers, json={**payload, "stream": True}, timeout=300) as response:
        if response.status_code >= 400:
            response.read()  # So the error body (the server's reason) can be shown in the message.
        response.raise_for_status()
        if "text/event-stream" not in response.headers.get("content-type", ""):
            response.read()
            choice = response.json()["choices"][0]
            stats["reasoning_chars"] = len(choice["message"].get("reasoning_content") or "")
            on_text(choice["message"]["content"])
            return choice["message"]["content"], choice.get("finish_reason")
        parts: list[str] = []
        finish = None
        for line in response.iter_lines():
            if should_stop is not None and should_stop():
                raise StopRequested("Stopped at the user's request")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            choices = json.loads(data).get("choices") or [{}]
            finish = choices[0].get("finish_reason") or finish
            chunk = choices[0].get("delta") or {}
            reasoning = chunk.get("reasoning_content") or chunk.get("reasoning") or ""
            if reasoning:
                stats["reasoning_chars"] += len(reasoning)
                stats["reasoning_tail"] = (stats["reasoning_tail"] + reasoning)[-2000:]
            delta = chunk.get("content") or ""
            if delta:
                parts.append(delta)
                stats["content_chars"] += len(delta)
                on_text("".join(parts))
            if on_progress is not None and (reasoning or delta):
                on_progress(stats)
            if reasoning_budget and not parts and stats["reasoning_chars"] > reasoning_budget:
                raise ReasoningBudgetExceeded(f"The model reasoned {stats['reasoning_chars']} characters without answering")
        return "".join(parts), finish


class OutputTruncated(ValueError):
    """The model cut the response short because it reached the output length limit."""


PLAN_INSTRUCTION = (
    "\nDO NOT WRITE CODE IN THIS STEP. Instead of the JSON format above, return ONLY the file plan: "
    '{"plan": [{"path": "relative/path", "purpose": "one-sentence purpose"}]}. '
    "List every file needed, including project.yaml and the packaging files the delivery decision asks for; "
    "at most 30 files, no lock files."
)

TEST_PLAN_INSTRUCTION = (
    "\nDO NOT WRITE TESTS IN THIS STEP. Instead of the JSON format above, return ONLY the test file plan: "
    '{"plan": [{"path": "tests/...", "purpose": "one sentence: what it tests and which FR/NFR ids it covers"}]}. '
    "Every FR must appear in at least one purpose. A helper file shared by several tests comes first in the list. "
    "At most 12 files, all under tests/ (or test/)."
)


class ClarificationNeeded(Exception):
    """An agent asks the user for a single decision before it continues."""

    def __init__(self, question: str):
        super().__init__(question)
        self.question = question


def _parse_plan(raw: str) -> list[dict]:
    value = _extract_json(raw)
    items = (value.get("plan") or value.get("files")) if isinstance(value, dict) else None
    plan, seen = [], set()
    for item in items if isinstance(items, list) else []:
        path = item.get("path") if isinstance(item, dict) else item
        if isinstance(path, str) and path.strip() and path not in seen:
            seen.add(path)
            plan.append({"path": path.strip(), "purpose": item.get("purpose", "") if isinstance(item, dict) else ""})
    if not plan:
        raise ValueError("The agent did not return a valid file plan")
    return plan[:40]


# A backslash that does not start a JSON escape: `dist\\calc.exe` written with a single backslash in a .bat file or a path.
_BAD_ESCAPE = re.compile(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})')


def _loads_lenient(text: str):
    """json.loads that also accepts what models get wrong most when code sits inside JSON strings: raw newlines/tabs
    (strict=False) and unescaped backslashes (common in Windows paths and .bat files). Raises JSONDecodeError."""
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        return json.loads(_BAD_ESCAPE.sub(r"\\\\", text), strict=False)


_BUNDLE_START = re.compile(r'\{\s*"(?:files|plan)"\s*:')


def _first_bundle(text: str):
    """The first complete {"files": ...} / {"plan": ...} object, ignoring what comes before and after it.

    Models sometimes answer with the JSON, then explanations, a second attempt or a question; the whole text is then
    not JSON although the first object is perfectly usable.
    """
    decoder = json.JSONDecoder(strict=False)
    for candidate in (text, _BAD_ESCAPE.sub(r"\\\\", text)):
        for start in _BUNDLE_START.finditer(candidate):
            try:
                value, _end = decoder.raw_decode(candidate, start.start())
            except json.JSONDecodeError:
                continue
            return value
    return None


def _extract_json(raw: str):
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return _loads_lenient(text)
    except json.JSONDecodeError:
        # Models may add explanations before/after the JSON; try the outermost object.
        start, end = text.find("{"), text.rfind("}")
        try:
            value = _loads_lenient(text[start:end + 1]) if 0 <= start < end else None
        except json.JSONDecodeError:
            value = None
        if value is None:
            value = _first_bundle(text)
        if value is None:
            raise ValueError("The agent did not return valid JSON files")
    return value


def _parse_file_bundle(raw: str) -> dict[str, str]:
    value = _extract_json(raw)
    files = value.get("files") if isinstance(value, dict) else None
    if not isinstance(files, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in files.items()
    ):
        raise ValueError("The agent output must be a JSON object with a files field")
    return files


class CrewAIEngine:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        try:
            from crewai import Agent, BaseLLM, Crew, Process, Task
            import yaml
        except ImportError as exc:
            raise RuntimeError("Dependencies are missing. Run make setup to install them") from exc

        load_dotenv()
        self.key = api_key or os.getenv("OPENCODE_API_KEY", "")
        if not self.key:
            raise RuntimeError("OPENCODE_API_KEY is required; check the local .env file")
        self.model = model or os.getenv("AUTOSDLC_MODEL", DEFAULT_MODEL)
        self.session_id = "ses_" + uuid.uuid4().hex

        class OpenCodeGoLLM(BaseLLM):
            def __init__(inner, model_name: str, key: str, session_id: str):
                super().__init__(model=model_name, temperature=0.2)
                inner.key = key
                inner.session_id = session_id
                # If set, the response is streamed and the accumulated text is passed here for the live view.
                inner.listener = None
                # True if the last response was cut at the model's output length limit (finish_reason == "length").
                inner.truncated = False
                # If set, every request attempt is reported here as a dict (the run's diagnostic call log).
                inner.call_log = None
                # The stage whose agent makes the current call; only for the call log.
                inner.stage = ""
                # If set, the state of the current request (waiting, reasoning, writing, done, failed) is reported
                # here while it runs, for the live activity view.
                inner.activity = None
                inner.current = {}
                # If set and true, the current call is abandoned (the user pressed Stop).
                inner.should_stop = None
                # Models whose requests were rejected with reasoning_effort; they are called without it.
                inner.no_effort: set[str] = set()

            def _report(inner, phase: str, **fields: Any) -> None:
                if inner.activity is None:
                    return
                try:
                    inner.activity({"kind": "model", **inner.current, "phase": phase, **fields})
                except Exception:
                    pass  # Diagnostics never break a run.

            def _log(inner, attempt: int, started: float, messages: list[dict[str, Any]], outcome: str, **fields: Any) -> None:
                inner._report("done" if outcome == "ok" else "failed", outcome=outcome,
                              **{key: value for key, value in fields.items() if key in {"error", "finish_reason", "reasoning_chars"}})
                if inner.call_log is None:
                    return
                record = {"stage": inner.stage, "model": inner.model, "attempt": attempt + 1, "outcome": outcome,
                          "seconds": round(time.monotonic() - started, 1),
                          "prompt_chars": sum(len(str(message.get("content", ""))) for message in messages), **fields}
                if outcome != "ok":
                    # The end of the prompt says what the agent was asked to do when it failed.
                    record["prompt_tail"] = str(messages[-1].get("content", ""))[-1500:] if messages else ""
                try:
                    inner.call_log(record)
                except Exception:
                    pass  # Diagnostics never break a run.

            def call(
                inner,
                messages: str | list[dict[str, Any]],
                tools: list[dict] | None = None,
                callbacks: list[Any] | None = None,
                available_functions: dict[str, Any] | None = None,
                **_kwargs: Any,
            ) -> str:
                if tools:
                    raise ValueError("External tools are not allowed for agents in this project")
                if isinstance(messages, str):
                    messages = [{"role": "user", "content": messages}]
                # Three attempts with exponential backoff on temporary network/rate-limit errors.
                nudged = False
                for attempt in range(3):
                    if inner.should_stop is not None and inner.should_stop():
                        raise StopRequested("Stopped at the user's request")
                    url = f"{GO_BASE_URL}/chat/completions"
                    headers = {"Authorization": f"Bearer {inner.key}", "x-opencode-session": inner.session_id}
                    payload = {"model": inner.model, "messages": messages}
                    effort = REASONING_EFFORT.get(inner.stage)
                    if effort and inner.model not in inner.no_effort:
                        payload["reasoning_effort"] = effort
                    started, stats = time.monotonic(), {}
                    inner.current = {"stage": inner.stage, "model": inner.model, "attempt": attempt + 1, "started_at": now(),
                                     "prompt_chars": sum(len(str(message.get("content", ""))) for message in messages)}
                    inner._report("waiting")
                    try:
                        if inner.listener is not None:
                            content, finish = _stream_completion(
                                url, headers, payload, inner.listener, stats,
                                lambda progress: inner._report("writing" if progress["content_chars"] else "reasoning", **progress),
                                inner.should_stop, REASONING_BUDGET)
                        else:
                            response = httpx.post(url, headers=headers, json=payload, timeout=300)
                            response.raise_for_status()
                            choice = response.json()["choices"][0]
                            content, finish = choice["message"]["content"], choice.get("finish_reason")
                            stats["reasoning_chars"] = len(choice["message"].get("reasoning_content") or "")
                        break
                    except StopRequested:
                        inner._report("stopped")
                        raise
                    except ReasoningBudgetExceeded:
                        inner._log(attempt, started, messages, "reasoning_budget",
                                   reasoning_chars=stats.get("reasoning_chars", 0))
                        if not nudged and attempt < 2:
                            # Asked once more with an instruction to stop deliberating and answer.
                            nudged = True
                            last = messages[-1]
                            messages = [*messages[:-1], {**last, "content": f"{last.get('content', '')}\n\n{BUDGET_NUDGE}"}]
                            continue
                        model, stage = inner.model, inner.stage
                        raise RuntimeError(f"The {model} model reasoned more than {REASONING_BUDGET} characters "
                                           f"without answering; choose another model for the {stage} stage and "
                                           "continue") from None
                    except httpx.HTTPStatusError as exc:
                        # A 400 is normally permanent, but a gateway can return a temporary 400 too, so it is retried once.
                        status_code = exc.response.status_code
                        try:
                            reason = exc.response.text.strip()[:400]
                        except httpx.ResponseNotRead:
                            reason = ""
                        inner._log(attempt, started, messages, f"http_{status_code}", error=reason)
                        if status_code == 400 and "reasoning_effort" in payload and attempt < 2:
                            # The gateway or model does not accept the parameter: the request is repeated without it.
                            inner.no_effort.add(inner.model)
                            continue
                        if (status_code in RETRY_STATUSES or (status_code == 400 and attempt == 0)) and attempt < 2:
                            time.sleep(2 ** (attempt + 1))
                            continue
                        raise RuntimeError(
                            f"OpenCode Go returned HTTP {exc.response.status_code}" + (f": {reason}" if reason else "")
                        ) from exc
                    except httpx.TransportError as exc:
                        inner._log(attempt, started, messages, "transport_error", error=repr(exc)[:400])
                        if attempt < 2:
                            time.sleep(2 ** (attempt + 1))
                            continue
                        raise RuntimeError("The OpenCode Go request failed") from exc
                    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                        inner._log(attempt, started, messages, "bad_response", error=repr(exc)[:400])
                        raise RuntimeError("The OpenCode Go request or response failed") from exc
                inner.truncated = finish == "length"
                empty = not isinstance(content, str) or not content.strip()
                inner._log(attempt, started, messages, "empty" if empty else "ok", finish_reason=finish,
                           response_chars=len(content) if isinstance(content, str) else 0,
                           reasoning_chars=stats.get("reasoning_chars", 0))
                if empty and finish == "length" and stats.get("reasoning_chars"):
                    # A retry with the same model usually burns out the same way, so the error says what to change.
                    model, chars, stage = inner.model, stats["reasoning_chars"], inner.stage
                    raise RuntimeError(f"OpenCode Go returned an empty response: the {model} model spent its whole "
                                       f"output limit on reasoning ({chars} characters); choose another model for "
                                       f"the {stage} stage and continue")
                if empty:
                    raise RuntimeError("OpenCode Go returned an empty response")
                return content

            def supports_function_calling(inner) -> bool:
                return False

            def supports_stop_words(inner) -> bool:
                return False

            def get_context_window_size(inner) -> int:
                return 32_000

        self.llm = OpenCodeGoLLM(self.model, self.key, self.session_id)
        self.Agent = Agent
        self.Crew = Crew
        self.Process = Process
        self.Task = Task
        self.current_files: dict[str, str] = {}
        # (stage, the agent output so far); RunSession sets it to write the live output file.
        self.output_listener: Callable[[str, str], None] | None = None
        # Language of every document the agents write; RunSession sets it from the project's decisions.
        self.language = "en"
        # Optional model per agent stage (e.g. a faster one for testing); a stage not listed uses self.model.
        self.stage_models: dict[str, str] = {}
        config_dir = resource_files("autosdlc").joinpath("config")
        self.agents = yaml.safe_load(config_dir.joinpath("agents.yaml").read_text(encoding="utf-8"))
        self.tasks = yaml.safe_load(config_dir.joinpath("tasks.yaml").read_text(encoding="utf-8"))

    def _ask(self, stage: str, extra: str = "", task: str | None = None, ask_user: bool = True,
             language: str | None = None, **values: str) -> str:
        """stage selects the agent identity (agents.yaml), task the task template (tasks.yaml; default stage)."""
        agent_config = self.agents[stage]
        task_config = self.tasks[task or stage]
        document_language = prompt_language(language or self.language)
        prompt = Template(task_config["description"]).substitute({**values, "language": document_language}) + extra
        prompt += "\n" + Template(self.tasks.get("language_rules", "")).safe_substitute(
            language=document_language, terms=jargon_rule(language or self.language))
        listener = self.output_listener
        self.llm.stage = stage
        self.llm.model = self.stage_models.get(stage) or self.model
        self.llm.listener = (lambda text: listener(stage, text)) if listener else None
        if ask_user:
            prompt += (
                "\nIf a critical user decision is missing, do not assume; reply only in the form "
                f"'USER_QUESTION: <one clear question in {document_language}>'. "
                "If you have enough information do not use this form; produce the task's normal output."
            )
        agent = self.Agent(
            role=agent_config["role"],
            goal=agent_config["goal"],
            backstory=agent_config["backstory"],
            llm=self.llm,
            allow_delegation=False,
            verbose=False,
            max_iter=3,
        )
        task = self.Task(
            description=prompt,
            expected_output=task_config["expected_output"],
            agent=agent,
        )
        self.llm.truncated = False
        result = self.Crew(
            agents=[agent], tasks=[task], process=self.Process.sequential, verbose=False
        ).kickoff()
        raw = result.raw.strip()
        if self.llm.truncated:
            raise OutputTruncated("The agent output was cut at the model's length limit")
        match = re.fullmatch(r"(?:USER_QUESTION|KULLANICI_SORUSU):\s*(.{1,1000})", raw, re.DOTALL)
        if match:
            raise ClarificationNeeded(match.group(1).strip())
        return raw

    def _ask_files(self, stage: str, extra: str = "", **values: str) -> dict[str, str]:
        """Asks for a file bundle; if the JSON is broken, tells the agent the error and asks once more."""
        raw = ""
        try:
            raw = self._ask(stage, extra=extra, **values)
            return _parse_file_bundle(raw)
        except ValueError as exc:
            # A response that stops mid-string was cut off even when the provider did not report a length stop.
            cut = isinstance(exc, OutputTruncated) or (bool(raw) and not raw.rstrip().rstrip("`").rstrip().endswith("}"))
            shorter = " This time return fewer and shorter files; write only what is needed." if cut else ""
            return _parse_file_bundle(self._ask(stage, extra=extra + (
                f"\nYOUR PREVIOUS RESPONSE WAS REJECTED: {exc}.{shorter} Do not add explanations or Markdown; "
                'return only valid JSON in the form {"files": {...}}.'
            ), **values))

    def _ask_files_batched(self, stage: str, batch_size: int = 4, plan_instruction: str = PLAN_INSTRUCTION,
                           **values: str) -> dict[str, str]:
        """Writing a big project in one response hits the output limit: first a file plan, then small groups.

        Every group sees the files written earlier, so interfaces between modules stay consistent.
        If no plan can be obtained it falls back to single-piece generation.
        """
        raw = ""
        try:
            raw = self._ask(stage, extra=plan_instruction, **values)
            plan = _parse_plan(raw)
        except ValueError:
            try:
                # A model that ignored the plan instruction and wrote the files already gave a usable answer.
                return _parse_file_bundle(raw)
            except ValueError:
                return self._ask_files(stage, **values)
        plan_text = "\n".join(f"- {item['path']}: {item.get('purpose', '')}" for item in plan)
        files: dict[str, str] = {}

        def write(batch: list[str]) -> None:
            extra = (
                f"\nFILE PLAN (the whole project):\n{plan_text}\n"
                f"Files already written (stay consistent with them, do not write them again):\n"
                f"{json.dumps(files, ensure_ascii=False)[:16000]}\n"
                f"NOW WRITE ONLY these files, completely: {', '.join(batch)}. "
                'Return only JSON of the form {"files": {"relative/path": "full content"}}.'
            )
            try:
                files.update(self._ask_files(stage, extra=extra, **values))
            except ValueError:
                # A group the model cannot finish in one response (cut off or broken JSON) is split in two and retried;
                # only a single file that still fails ends the stage.
                if len(batch) == 1:
                    raise
                middle = len(batch) // 2
                write(batch[:middle])
                write(batch[middle:])

        for start in range(0, len(plan), batch_size):
            write([item["path"] for item in plan[start:start + batch_size]])
        return files

    def document(self, stage: str, idea: str, context: str) -> str:
        return self._ask(stage, idea=idea, context=context[:24000])

    def code(self, idea: str, docs: str, feedback: str | None = None, change: str | None = None) -> dict[str, str]:
        current = (
            "Files under tests/ and test/ belong to the Test Agent and must not be changed. Return only changed or new files.\n"
            "Existing files (including tests):\n"
            f"{json.dumps(self.current_files, ensure_ascii=False)[:30000]}\n"
        )
        if change:
            # Change request: the existing application is kept, only the files the request requires change.
            instruction = f"UPDATE THE EXISTING APPLICATION FOR THIS CHANGE:\n{change[:6000]}\n"
            if feedback:
                instruction += f"Output of the test that reproduces the bug:\n{feedback[-6000:]}\n"
            instruction += current
        elif feedback:
            # The error summary is usually at the end of the output, so it is taken from the end.
            instruction = f"Fix the application according to this test output:\n{feedback[-8000:]}\n" + current
        else:
            files = self._ask_files_batched("development", idea=idea, context=docs[:20000], feedback="Create the first version of the application.\n")
            self.current_files.update(files)
            return files
        files = self._ask_files("development", idea=idea, context=docs[:20000], feedback=instruction)
        self.current_files.update(files)
        return files

    def tests(self, idea: str, docs: str, files: dict[str, str], change: str | None = None,
              feedback: str | None = None) -> dict[str, str]:
        extra = ""
        if feedback:
            # The developer cannot edit tests, so a test that does not even compile is repaired by its author.
            extra = (
                "\nYOUR TESTS DO NOT COMPILE OR LOAD. Fix ONLY the test files named in this output; the application "
                "files above are correct and cannot be changed by you. Do not call anything that is not in the "
                f"application files. Return only the corrected test files, complete:\n{feedback[-6000:]}\n"
            )
        if change:
            extra = (
                f"\nYOU ARE WRITING TESTS FOR A CHANGE REQUEST:\n{change[:6000]}\n"
                "The existing test files are above together with the application files; keep them. "
                "Return only new or updated test files. Put the ids of new/changed requirements "
                "(e.g. FR-07) into the test names. If the request is a bug, write a regression test that "
                "reproduces the bug and is EXPECTED TO FAIL with the current code."
            )
        values = {"idea": idea, "context": docs[:16000], "files": json.dumps(files, ensure_ascii=False)[:30000]}
        if extra:
            return self._ask_files("testing", extra=extra, **values)
        # The first test suite in one response makes a reasoning model design every file at once before writing a
        # word (half an hour was seen); a plan and small groups keep each call short.
        return self._ask_files_batched("testing", batch_size=3, plan_instruction=TEST_PLAN_INSTRUCTION, **values)

    def section(self, stage: str, idea: str, context: str, document: str, section: str,
                instruction: str, mode: str, language: str | None = None) -> str:
        """For one section of a document: returns an answer for mode 'question', new Markdown for 'rewrite'/'add'."""
        task = "section_question" if mode == "question" else "section_edit"
        operation = {
            "rewrite": "REWRITE the section according to the instruction and return the section's new version (with its heading).",
            "add": "Write the NEW content to be added RIGHT BELOW the section; do not repeat the existing section.",
            "question": "Answer the question; do not change the document.",
        }[mode]
        return self._ask(
            stage, task=task, ask_user=False, idea=idea, context=context[:12000], document=document[:14000],
            section=section[:6000], instruction=instruction[:2000], operation=operation, language=language,
        )

    def impact(self, idea: str, context: str, change: str, language: str | None = None) -> str:
        return self._ask("impact", ask_user=False, language=language, idea=idea, context=context[:24000], change=change[:6000])
