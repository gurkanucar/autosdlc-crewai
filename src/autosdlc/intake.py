"""YAML driven architecture questions shared by CLI, API and UI."""

from importlib.resources import files

import yaml

from .i18n import t


CHOICE_LABELS = {
    # Established software terms are not translated (backend, fullstack, CLI…).
    "project_type": {"backend": "Backend", "frontend": "Frontend", "fullstack": "Fullstack", "cli": "CLI", "desktop": "Desktop app",
                     "library": "Library"},
    "database": {"none": "None", "sqlite": "SQLite", "postgres": "PostgreSQL"},
    "cache": {"none": "None", "redis": "Redis"},
    "observability": {"none": "None", "otel": "OpenTelemetry", "openobserve": "OpenObserve"},
    "ui": {"none": "None", "web": "Web UI"},
    "delivery": {"docker": "Docker", "binary": "Executables", "script": "Scripts", "none": "None"},
    "scale": {"normal": "Normal", "small": "Small"},
    # Language names are always written in their own language.
    "language": {"en": "English", "tr": "Türkçe"},
}


def default_delivery(project_type: str) -> str:
    """What the form preselects: services ship as containers, CLIs and desktop apps as executables, libraries as source."""
    return {"cli": "binary", "desktop": "binary", "library": "none"}.get(project_type, "docker")


def choice_label(name: str, value: str) -> str:
    label = CHOICE_LABELS.get(name, {}).get(value, value)
    return label if name == "language" else t(label)


def parse_choice(name: str, answer: str) -> str:
    normalized = answer.strip().casefold()
    for code, label in CHOICE_LABELS.get(name, {}).items():
        if normalized in {code.casefold(), label.casefold(), choice_label(name, code).casefold()}:
            return code
    return answer.strip()


def questions() -> dict[str, dict]:
    source = files("autosdlc").joinpath("config/questions.yaml")
    return yaml.safe_load(source.read_text(encoding="utf-8"))


def question_text(name: str) -> str:
    """The question for a decision, in the active language."""
    return t(questions()[name]["question"])


def templates() -> list[dict]:
    """Example project templates that fill the form in the UI, translated into the active language."""
    source = files("autosdlc").joinpath("config/templates.yaml")
    translated = []
    for item in yaml.safe_load(source.read_text(encoding="utf-8")):
        decisions = {key: (t(value) if key in {"stack", "constraints"} else value) for key, value in item["decisions"].items()}
        translated.append({**item, "title": t(item["title"]), "description": t(item["description"]),
                           "idea": t(item["idea"]), "decisions": decisions})
    return translated


def with_default_delivery(decisions: dict[str, str] | None) -> dict[str, str]:
    """A new project without a delivery choice gets its type's default; validate_decisions alone would pick
    Docker, which is kept for projects created before the decision existed."""
    selected = dict(decisions or {})
    if not selected.get("delivery") and selected.get("project_type"):
        selected["delivery"] = default_delivery(selected["project_type"])
    return selected


def validate_decisions(decisions: dict[str, str] | None) -> dict[str, str]:
    selected = decisions or {}
    catalog = questions()
    unknown = set(selected) - set(catalog)
    if unknown:
        raise ValueError(f"Unknown architecture decisions: {', '.join(sorted(unknown))}")
    project_type = selected.get("project_type")
    if project_type not in catalog["project_type"]["options"]:
        raise ValueError("Choose the project type: " + ", ".join(catalog["project_type"]["options"]))
    missing = [name for name, item in catalog.items() if not item.get("optional") and (not item.get("when") or project_type in item["when"]) and name not in selected]
    if missing:
        raise ValueError(f"These architecture decisions are required: {', '.join(missing)}")
    for name, value in selected.items():
        if catalog[name].get("free_text"):
            if not isinstance(value, str) or (not catalog[name].get("optional") and not value.strip()) or len(value) > (1000 if catalog[name].get("optional") else 200):
                raise ValueError(f"Invalid {name}: enter a short text")
        elif value not in catalog[name]["options"]:
            raise ValueError(f"Invalid {name}: {value}. Options: {catalog[name]['options']}")
    # Decisions that do not fit the project type (e.g. cli + postgres) are ignored.
    normalized = {
        name: "none" if item.get("when") and project_type not in item["when"]
        else selected.get(name, item.get("default", "" if item.get("optional") else "none"))
        for name, item in catalog.items()
    }
    if project_type in {"frontend", "fullstack"}:
        normalized["ui"] = "web"
    return normalized
