"""YAML driven architecture questions shared by CLI, API and UI."""

from importlib.resources import files

import yaml


def questions() -> dict[str, dict]:
    source = files("autosdlc").joinpath("config/questions.yaml")
    return yaml.safe_load(source.read_text(encoding="utf-8"))


def validate_decisions(decisions: dict[str, str] | None) -> dict[str, str]:
    selected = decisions or {}
    catalog = questions()
    unknown = set(selected) - set(catalog)
    if unknown:
        raise ValueError(f"Unknown architecture choices: {', '.join(sorted(unknown))}")
    project_type = selected.get("project_type")
    if project_type not in catalog["project_type"]["options"]:
        raise ValueError("Choose project_type from: " + ", ".join(catalog["project_type"]["options"]))
    missing = [name for name, item in catalog.items() if (not item.get("when") or project_type in item["when"]) and name not in selected]
    if missing:
        raise ValueError(f"Architecture choices required: {', '.join(missing)}")
    for name, value in selected.items():
        if catalog[name].get("free_text"):
            if not isinstance(value, str) or not value.strip() or len(value) > 120:
                raise ValueError(f"Invalid {name}: supply 1–120 characters")
        elif value not in catalog[name]["options"]:
            raise ValueError(f"Invalid {name}: {value}. Choose from {catalog[name]['options']}")
    normalized = {name: selected.get(name, "none") for name in catalog}
    if project_type in {"frontend", "fullstack"}:
        normalized["ui"] = "web"
    return normalized
