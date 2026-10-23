"""YAML ile tanımlanan ve OpenCode Go kullanan CrewAI ajanları."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from importlib.resources import files as resource_files
from string import Template
from typing import Any

import httpx
from dotenv import load_dotenv


GO_BASE_URL = "https://opencode.ai/inference/go/openai/v1"
DEFAULT_MODEL = "glm-5.3-flash"
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}


class ClarificationNeeded(Exception):
    """Bir ajan, devam etmeden önce kullanıcıdan tek bir karar ister."""

    def __init__(self, question: str):
        super().__init__(question)
        self.question = question


def _parse_file_bundle(raw: str) -> dict[str, str]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        # Modeller JSON'un önüne/arkasına açıklama ekleyebiliyor; en dıştaki nesneyi dene.
        start, end = text.find("{"), text.rfind("}")
        try:
            value = json.loads(text[start:end + 1]) if 0 <= start < end else None
        except json.JSONDecodeError:
            value = None
        if value is None:
            raise ValueError("Ajan geçerli JSON dosyaları döndürmedi")
    files = value.get("files") if isinstance(value, dict) else None
    if not isinstance(files, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in files.items()
    ):
        raise ValueError("Ajan çıktısı files alanı içeren bir JSON nesnesi olmalı")
    return files


class CrewAIEngine:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        try:
            from crewai import Agent, BaseLLM, Crew, Process, Task
            import yaml
        except ImportError as exc:
            raise RuntimeError("Bağımlılıklar eksik. Kurulum için make setup çalıştır") from exc

        load_dotenv()
        self.key = api_key or os.getenv("OPENCODE_API_KEY", "")
        if not self.key:
            raise RuntimeError("OPENCODE_API_KEY gerekli; yerel .env dosyasını kontrol et")
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
                    raise ValueError("Bu projede ajanlara harici araç izni verilmez")
                if isinstance(messages, str):
                    messages = [{"role": "user", "content": messages}]
                # Geçici ağ/hız sınırı hatalarında üstel bekleme ile üç deneme.
                for attempt in range(3):
                    try:
                        response = httpx.post(
                            f"{GO_BASE_URL}/chat/completions",
                            headers={
                                "Authorization": f"Bearer {inner.key}",
                                "x-opencode-session": inner.session_id,
                            },
                            json={"model": inner.model, "messages": messages},
                            timeout=300,
                        )
                        response.raise_for_status()
                        content = response.json()["choices"][0]["message"]["content"]
                        break
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code in RETRY_STATUSES and attempt < 2:
                            time.sleep(2 ** (attempt + 1))
                            continue
                        raise RuntimeError(
                            f"OpenCode Go HTTP {exc.response.status_code} döndürdü"
                        ) from exc
                    except httpx.TransportError as exc:
                        if attempt < 2:
                            time.sleep(2 ** (attempt + 1))
                            continue
                        raise RuntimeError("OpenCode Go isteği başarısız") from exc
                    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                        raise RuntimeError("OpenCode Go isteği veya yanıtı başarısız") from exc
                if not isinstance(content, str) or not content.strip():
                    raise RuntimeError("OpenCode Go boş yanıt döndürdü")
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

    def _ask(self, stage: str, extra: str = "", **values: str) -> str:
        agent_config = self.agents[stage]
        task_config = self.tasks[stage]
        prompt = Template(task_config["description"]).substitute(values) + extra
        prompt += (
            "\nKritik bir kullanıcı kararı eksikse varsayım yapmadan yalnızca "
            "'KULLANICI_SORUSU: <tek, açık Türkçe soru>' biçiminde yanıt ver. "
            "Yeterli bilgi varsa bu biçimi kullanma; görevin normal çıktısını üret."
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
        result = self.Crew(
            agents=[agent], tasks=[task], process=self.Process.sequential, verbose=False
        ).kickoff()
        raw = result.raw.strip()
        match = re.fullmatch(r"KULLANICI_SORUSU:\s*(.{1,1000})", raw, re.DOTALL)
        if match:
            raise ClarificationNeeded(match.group(1).strip())
        return raw

    def _ask_files(self, stage: str, **values: str) -> dict[str, str]:
        """Dosya paketi ister; JSON bozuksa ajana hatayı söyleyip bir kez daha sorar."""
        try:
            return _parse_file_bundle(self._ask(stage, **values))
        except ValueError as exc:
            return _parse_file_bundle(self._ask(stage, extra=(
                f"\nÖNCEKİ YANITIN REDDEDİLDİ: {exc}. Açıklama veya Markdown ekleme; "
                'yalnızca {"files": {...}} biçiminde geçerli JSON döndür.'
            ), **values))

    def document(self, stage: str, idea: str, context: str) -> str:
        return self._ask(stage, idea=idea, context=context[:24000])

    def code(self, idea: str, docs: str, feedback: str | None = None) -> dict[str, str]:
        repair = (
            # Hata özeti genellikle çıktının sonundadır; bu yüzden sondan alınır.
            f"Bu test çıktısına göre uygulamayı düzelt:\n{feedback[-8000:]}\n"
            "tests/ ve test/ altındaki dosyalar Test Ajanı'na aittir ve değiştirilmez; "
            "uygulama kodunu testlere uyacak şekilde düzelt. Yalnızca değişen dosyaları döndür.\n"
            "Mevcut dosyalar (testler dahil):\n"
            f"{json.dumps(self.current_files, ensure_ascii=False)[:30000]}\n"
            if feedback else "İlk uygulamayı oluştur.\n"
        )
        files = self._ask_files("development", idea=idea, context=docs[:20000], feedback=repair)
        self.current_files.update(files)
        return files

    def tests(self, idea: str, docs: str, files: dict[str, str]) -> dict[str, str]:
        return self._ask_files(
            "testing", idea=idea, context=docs[:16000],
            files=json.dumps(files, ensure_ascii=False)[:30000],
        )

