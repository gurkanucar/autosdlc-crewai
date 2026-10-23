"""Message translation: source strings are English, `t("Running")` returns them in the active language.

Catalogs live in config/locales/<code>/*.yaml as flat `English text: translation` mappings (every file in the
folder is merged). A string without a translation is shown in English, so a new language can be added gradually:
add a folder and an entry in LANGUAGES. Placeholders use str.format syntax: `t("{count} stages", count=3)`.

The active language is thread-local so background runs and UI sessions can use different languages at once.
Text that is stored (event details in the database) is kept in English and translated when displayed with
`translate_stored`, which also matches catalog entries that contain placeholders.
"""

from __future__ import annotations

import re
import threading
from functools import lru_cache
from importlib.resources import files

import yaml


DEFAULT_LANGUAGE = "en"
# code -> name written in that language (shown in selectors, never translated)
LANGUAGES = {"en": "English", "tr": "Türkçe"}
# English name of each language, used inside agent prompts ("Write all documents in Turkish").
PROMPT_NAMES = {"en": "English", "tr": "Turkish"}
_state = threading.local()
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@lru_cache(maxsize=None)
def catalog(language: str) -> dict[str, str]:
    folder = files("autosdlc").joinpath(f"config/locales/{language}")
    if not folder.is_dir():
        return {}
    merged: dict[str, str] = {}
    for item in sorted(folder.iterdir(), key=lambda entry: entry.name):
        if item.name.endswith(".yaml"):
            merged.update({str(k): str(v) for k, v in (yaml.safe_load(item.read_text(encoding="utf-8")) or {}).items()})
    return merged


def normalize(language: str | None) -> str:
    code = (language or "").strip().lower()[:2]
    return code if code in LANGUAGES else DEFAULT_LANGUAGE


def set_language(language: str | None) -> None:
    _state.language = normalize(language)


def current_language() -> str:
    return getattr(_state, "language", DEFAULT_LANGUAGE)


def t(message: str, /, *, language: str | None = None, **values: object) -> str:
    """Translate `message` into the active (or given) language and fill its placeholders."""
    code = normalize(language) if language else current_language()
    text = catalog(code).get(message, message) if code != DEFAULT_LANGUAGE else message
    if not values:
        return text
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError):
        return text


@lru_cache(maxsize=None)
def _patterns(language: str) -> list[tuple[re.Pattern, str]]:
    """Catalog entries with placeholders as regexes, longest first, to translate already formatted text."""
    patterns = []
    for source, target in catalog(language).items():
        if not _PLACEHOLDER.search(source):
            continue
        regex, position, names = "", 0, set()
        for match in _PLACEHOLDER.finditer(source):
            name = match.group(1)
            # A repeated placeholder must match the same text again.
            group = f"(?P={name})" if name in names else f"(?P<{name}>.+?)"
            names.add(name)
            regex += re.escape(source[position:match.start()]) + group
            position = match.end()
        regex += re.escape(source[position:])
        patterns.append((re.compile(f"^{regex}$", re.DOTALL), target))
    return sorted(patterns, key=lambda item: -len(item[0].pattern))


def translate_stored(text: str, language: str | None = None) -> str:
    """Translate a stored, already formatted English message (exact entries and ones with placeholders)."""
    code = normalize(language) if language else current_language()
    if code == DEFAULT_LANGUAGE or not text:
        return text
    exact = catalog(code).get(text)
    if exact is not None:
        return exact
    for pattern, target in _patterns(code):
        match = pattern.match(text)
        if match:
            try:
                return target.format(**match.groupdict())
            except (KeyError, IndexError, ValueError):
                return text
    return text


def prompt_language(language: str | None = None) -> str:
    """English language name for agent prompts, e.g. 'Turkish'."""
    return PROMPT_NAMES[normalize(language) if language else current_language()]


@lru_cache(maxsize=None)
def glossary() -> dict:
    """Terms that stay in English in every language (config/glossary.yaml)."""
    source = files("autosdlc").joinpath("config/glossary.yaml")
    return yaml.safe_load(source.read_text(encoding="utf-8")) or {}


def jargon_rule(language: str | None = None) -> str:
    """Prompt text that forbids translating technical terms, with the language's known bad translations."""
    code = normalize(language) if language else current_language()
    data = glossary()
    text = ", ".join(data.get("keep_english", []))
    avoid = data.get("avoid", {}).get(code, [])
    if avoid:
        text += ". Never write literal translations such as: " + ", ".join(f'"{item}"' for item in avoid)
    return text
