"""YAML driven architecture questions shared by CLI, API and UI."""

from importlib.resources import files

import yaml


CHOICE_LABELS = {
    "project_type": {"backend": "Arka uç", "frontend": "Ön yüz", "fullstack": "Tam yığın", "cli": "Komut satırı", "library": "Kütüphane"},
    "database": {"none": "Yok", "sqlite": "SQLite", "postgres": "PostgreSQL"},
    "cache": {"none": "Yok", "redis": "Redis"},
    "observability": {"none": "Yok", "otel": "OpenTelemetry", "openobserve": "OpenObserve"},
    "ui": {"none": "Yok", "web": "Web arayüzü"},
}


def choice_label(name: str, value: str) -> str:
    return CHOICE_LABELS.get(name, {}).get(value, value)


def parse_choice(name: str, answer: str) -> str:
    normalized = answer.strip().casefold()
    for code, label in CHOICE_LABELS.get(name, {}).items():
        if normalized in {code.casefold(), label.casefold()}:
            return code
    return answer.strip()


def questions() -> dict[str, dict]:
    source = files("autosdlc").joinpath("config/questions.yaml")
    return yaml.safe_load(source.read_text(encoding="utf-8"))


def validate_decisions(decisions: dict[str, str] | None) -> dict[str, str]:
    selected = decisions or {}
    catalog = questions()
    unknown = set(selected) - set(catalog)
    if unknown:
        raise ValueError(f"Bilinmeyen mimari kararlar: {', '.join(sorted(unknown))}")
    project_type = selected.get("project_type")
    if project_type not in catalog["project_type"]["options"]:
        raise ValueError("Proje türünü seç: " + ", ".join(catalog["project_type"]["options"]))
    missing = [name for name, item in catalog.items() if not item.get("optional") and (not item.get("when") or project_type in item["when"]) and name not in selected]
    if missing:
        raise ValueError(f"Şu mimari kararlar gerekli: {', '.join(missing)}")
    for name, value in selected.items():
        if catalog[name].get("free_text"):
            if not isinstance(value, str) or (not catalog[name].get("optional") and not value.strip()) or len(value) > (1000 if catalog[name].get("optional") else 200):
                raise ValueError(f"Geçersiz {name}: kısa bir metin gir")
        elif value not in catalog[name]["options"]:
            raise ValueError(f"Geçersiz {name}: {value}. Seçenekler: {catalog[name]['options']}")
    # Proje türüne uymayan kararlar (ör. cli + postgres) yok sayılır.
    normalized = {
        name: "none" if item.get("when") and project_type not in item["when"]
        else selected.get(name, "" if item.get("optional") else "none")
        for name, item in catalog.items()
    }
    if project_type in {"frontend", "fullstack"}:
        normalized["ui"] = "web"
    return normalized
