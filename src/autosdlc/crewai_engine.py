"""CrewAI agents configured in YAML and backed by OpenCode Go."""

from __future__ import annotations

import json
import os
import uuid
from importlib.resources import files as resource_files
from string import Template
from typing import Any

import httpx
from dotenv import load_dotenv


GO_BASE_URL = "https://opencode.ai/inference/go/openai/v1"
DEFAULT_MODEL = "glm-5.3-flash"


def _parse_file_bundle(raw: str) -> dict[str, str]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Agent did not return valid JSON files") from exc
    files = value.get("files") if isinstance(value, dict) else None
    if not isinstance(files, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in files.items()
    ):
        raise ValueError("Agent output must be a JSON object with a files mapping")
    return files


class CrewAIEngine:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        try:
            from crewai import Agent, BaseLLM, Crew, Process, Task
            import yaml
        except ImportError as exc:
            raise RuntimeError("Dependencies are missing. Install with: pip install -e .") from exc

        load_dotenv()
        self.key = api_key or os.getenv("OPENCODE_API_KEY", "")
        if not self.key:
            raise RuntimeError("OPENCODE_API_KEY is required")
        self.model = model or os.getenv("AUTOSDLC_MODEL", DEFAULT_MODEL)
        self.session_id = "ses_" + uuid.uuid4().hex

        class OpenCodeGoLLM(BaseLLM):
            def __init__(inner, model_name: str, key: str, session_id: str):
                super().__init__(model=model_name, temperature=0.2)
                inner.key = key
                inner.session_id = session_id

            def call(
                inner,
                messages: str | list[dict[str, Any]],
                tools: list[dict] | None = None,
                callbacks: list[Any] | None = None,
                available_functions: dict[str, Any] | None = None,
                **_kwargs: Any,
            ) -> str:
                if tools:
                    raise ValueError("This project does not grant agents external tools")
                if isinstance(messages, str):
                    messages = [{"role": "user", "content": messages}]
                try:
                    response = httpx.post(
                        f"{GO_BASE_URL}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {inner.key}",
                            "x-opencode-session": inner.session_id,
                        },
                        json={"model": inner.model, "messages": messages},
                        timeout=120,
                    )
                    response.raise_for_status()
                    content = response.json()["choices"][0]["message"]["content"]
                except httpx.HTTPStatusError as exc:
                    raise RuntimeError(
                        f"OpenCode Go returned HTTP {exc.response.status_code}"
                    ) from exc
                except (httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
                    raise RuntimeError("OpenCode Go request or response failed") from exc
                if not isinstance(content, str) or not content.strip():
                    raise RuntimeError("OpenCode Go returned empty text")
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
        config_dir = resource_files("autosdlc").joinpath("config")
        self.agents = yaml.safe_load(config_dir.joinpath("agents.yaml").read_text(encoding="utf-8"))
        self.tasks = yaml.safe_load(config_dir.joinpath("tasks.yaml").read_text(encoding="utf-8"))

    def _ask(self, stage: str, **values: str) -> str:
        agent_config = self.agents[stage]
        task_config = self.tasks[stage]
        prompt = Template(task_config["description"]).substitute(values)
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
        result = self.Crew(
            agents=[agent], tasks=[task], process=self.Process.sequential, verbose=False
        ).kickoff()
        return result.raw

    def document(self, stage: str, idea: str, context: str) -> str:
        return self._ask(stage, idea=idea, context=context[:12000])

    def code(self, idea: str, docs: str, feedback: str | None = None) -> dict[str, str]:
        repair = (
            "Current implementation:\n"
            f"{json.dumps(self.current_files, ensure_ascii=False)[:18000]}\n"
            f"Repair the implementation based on this pytest output:\n{feedback[:10000]}\n"
            if feedback else "Create the first implementation.\n"
        )
        raw = self._ask("development", idea=idea, context=docs[:14000], feedback=repair)
        files = _parse_file_bundle(raw)
        self.current_files.update(files)
        return files

    def tests(self, idea: str, docs: str, files: dict[str, str]) -> dict[str, str]:
        raw = self._ask(
            "testing", idea=idea, context=docs[:10000],
            files=json.dumps(files, ensure_ascii=False)[:18000],
        )
        return _parse_file_bundle(raw)

