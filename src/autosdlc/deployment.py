"""Deterministic container artifacts derived from reviewed architecture choices."""

import re
from pathlib import Path

import yaml

# Environment variables the application reads: process.env.X / process.env["X"] (Node.js), os.environ["X"],
# os.environ.get("X") / os.getenv("X") (Python), os.Getenv("X") (Go).
_ENV_READ = re.compile(r"""process\.env(?:\.([A-Z][A-Z0-9_]*)|\[\s*['"]([A-Z][A-Z0-9_]*)['"]\s*\])"""
                       r"""|os\.(?:environ(?:\.get)?\s*[\[(]|getenv\s*\(|Getenv\s*\()\s*['"]([A-Z][A-Z0-9_]*)['"]""")
_SOURCES = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".py", ".go"}
_SKIPPED_FOLDERS = {"node_modules", "dist", "build", "target", "vendor", "__pycache__", "tests", "test"}
# Variables the pipeline sets itself, or keys of the pipeline that must never reach the application.
_NOT_APP_SECRETS = {"POSTGRES_PASSWORD", "OPENCODE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}


def app_secrets(project_dir: Path) -> tuple[list[str], list[str]]:
    """(the secrets the application cannot run without, the other keys/tokens/passwords it reads) from its source.

    A signing secret (JWT_SECRET, SESSION_SECRET) is required: the generated compose.yaml once left JWT_SECRET out, the
    app fell back to a default written in its code, and a token signed with that default was accepted as an admin's."""
    names: set[str] = set()
    for path in project_dir.rglob("*"):
        folders = path.relative_to(project_dir).parts[:-1]
        if path.suffix not in _SOURCES or not path.is_file() or any(
                part in _SKIPPED_FOLDERS or part.startswith(".") for part in folders):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        names.update(next(group for group in match.groups() if group) for match in _ENV_READ.finditer(text))
    names -= _NOT_APP_SECRETS
    required = sorted(name for name in names if "SECRET" in name)
    optional = sorted(name for name in names - set(required) if re.search(r"(?:_KEY|TOKEN|PASSWORD)$", name))
    return required, optional


def render_compose(decisions: dict[str, str], port: int = 8000,
                   secrets: tuple[list[str], list[str]] = ([], [])) -> dict:
    app = {
        "build": ".",
        "environment": {},
    }
    required, optional = secrets
    app["environment"].update({name: f"${{{name}:?set {name}}}" for name in required})
    app["environment"].update({name: f"${{{name}:-}}" for name in optional})
    if decisions["project_type"] in {"backend", "frontend", "fullstack"}:
        app["ports"] = [f"{port}:{port}"]
    services = {"app": app}
    volumes = {}
    if decisions["project_type"] in {"backend", "fullstack"}:
        # Files users upload (e.g. images) live on this volume and survive a container restart.
        app["environment"]["UPLOADS_DIR"] = "/data/uploads"
        app["volumes"] = ["app-data:/data"]
        volumes["app-data"] = {}
    if decisions["database"] == "sqlite":
        app["environment"]["TASK_DB_PATH"] = "/data/app.db"
        app["volumes"] = ["app-data:/data"]
        volumes["app-data"] = {}
    elif decisions["database"] == "postgres":
        app["environment"]["DATABASE_URL"] = "postgresql://app:${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}@db:5432/app"
        app["depends_on"] = {"db": {"condition": "service_healthy"}}
        services["db"] = {
            "image": "postgres:16-alpine",
            "environment": {"POSTGRES_DB": "app", "POSTGRES_USER": "app", "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}"},
            "volumes": ["postgres-data:/var/lib/postgresql/data"],
            "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U app -d app"], "interval": "5s", "retries": 10},
        }
        volumes["postgres-data"] = {}
    if decisions["cache"] == "redis":
        app["environment"]["REDIS_URL"] = "redis://redis:6379/0"
        app.setdefault("depends_on", {})["redis"] = {"condition": "service_healthy"}
        services["redis"] = {
            "image": "redis:7-alpine",
            "healthcheck": {"test": ["CMD", "redis-cli", "ping"], "interval": "5s", "retries": 10},
        }
    if decisions["observability"] != "none":
        app["environment"].update({
            "OTEL_SERVICE_NAME": "generated-app",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4318",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        })
        services["otel-collector"] = {
            "image": "otel/opentelemetry-collector-contrib:0.113.0",
            "command": ["--config=/etc/otelcol/config.yaml"],
            "volumes": ["./otel-collector.yaml:/etc/otelcol/config.yaml:ro"],
            "ports": ["4318:4318"],
        }
        app.setdefault("depends_on", {})["otel-collector"] = {"condition": "service_started"}
    if decisions["observability"] == "openobserve":
        services["openobserve"] = {
            "image": "public.ecr.aws/zinclabs/openobserve:latest",
            "environment": {
                "ZO_ROOT_USER_EMAIL": "${ZO_ROOT_USER_EMAIL:?set ZO_ROOT_USER_EMAIL}",
                "ZO_ROOT_USER_PASSWORD": "${ZO_ROOT_USER_PASSWORD:?set ZO_ROOT_USER_PASSWORD}",
            },
            "ports": ["5080:5080"],
            "volumes": ["openobserve-data:/data"],
        }
        services["otel-collector"]["depends_on"] = {"openobserve": {"condition": "service_started"}}
        services["otel-collector"]["environment"] = {"OPENOBSERVE_BASIC_AUTH": "${OPENOBSERVE_BASIC_AUTH:?set OPENOBSERVE_BASIC_AUTH}"}
        volumes["openobserve-data"] = {}
    if not app["environment"]:
        del app["environment"]
    result = {"services": services}
    if volumes:
        result["volumes"] = volumes
    return result


def write_deployment_files(project_dir: Path, decisions: dict[str, str], port: int = 8000) -> None:
    """Container files only for a Docker delivery; executables and scripts need no compose stack."""
    if decisions.get("delivery", "docker") != "docker":
        return
    (project_dir / ".dockerignore").write_text(
        ".env\n.git\n.venv\n.autosdlc-venv\nnode_modules\n__pycache__\n.pytest_cache\n"
        "test-results\nplaywright-report\ntests\ntest\n",
        encoding="utf-8",
    )
    secrets = app_secrets(project_dir)
    (project_dir / "compose.yaml").write_text(yaml.safe_dump(render_compose(decisions, port, secrets), sort_keys=False),
                                              encoding="utf-8")
    examples = [f"{name}=change-me-to-a-long-random-value" for name in secrets[0]] + [f"{name}=" for name in secrets[1]]
    if decisions["database"] == "postgres":
        examples.append("POSTGRES_PASSWORD=change-me")
    if decisions["observability"] == "openobserve":
        examples += ["ZO_ROOT_USER_EMAIL=admin@example.com", "ZO_ROOT_USER_PASSWORD=change-me", "OPENOBSERVE_BASIC_AUTH=base64-of-email-colon-password"]
    (project_dir / ".env.example").write_text("\n".join(examples) + "\n", encoding="utf-8")
    if decisions["observability"] != "none":
        exporters = {"debug": {}}
        if decisions["observability"] == "openobserve":
            exporters = {"otlphttp/openobserve": {
                "endpoint": "http://openobserve:5080/api/default",
                "headers": {"Authorization": "Basic ${env:OPENOBSERVE_BASIC_AUTH}"},
            }}
        collector = {
            "receivers": {"otlp": {"protocols": {"grpc": {"endpoint": "0.0.0.0:4317"}, "http": {"endpoint": "0.0.0.0:4318"}}}},
            "exporters": exporters,
            "service": {"pipelines": {"traces": {"receivers": ["otlp"], "exporters": list(exporters)}, "metrics": {"receivers": ["otlp"], "exporters": list(exporters)}, "logs": {"receivers": ["otlp"], "exporters": list(exporters)}}},
        }
        (project_dir / "otel-collector.yaml").write_text(yaml.safe_dump(collector, sort_keys=False), encoding="utf-8")
