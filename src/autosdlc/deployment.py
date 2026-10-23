"""Deterministic container artifacts derived from reviewed architecture choices."""

from pathlib import Path

import yaml


def render_compose(decisions: dict[str, str], port: int = 8000) -> dict:
    app = {
        "build": ".",
        "environment": {},
    }
    if decisions["project_type"] in {"backend", "frontend", "fullstack"}:
        app["ports"] = [f"{port}:{port}"]
    services = {"app": app}
    volumes = {}
    if decisions["database"] == "sqlite":
        app["environment"]["TASK_DB_PATH"] = "/data/app.db"
        app["volumes"] = ["app-data:/data"]
        volumes["app-data"] = {}
    else:
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
        volumes["openobserve-data"] = {}
    result = {"services": services}
    if volumes:
        result["volumes"] = volumes
    return result


def write_deployment_files(project_dir: Path, decisions: dict[str, str], port: int = 8000) -> None:
    (project_dir / ".dockerignore").write_text(
        ".env\n.git\n.venv\n.autosdlc-venv\nnode_modules\n__pycache__\n.pytest_cache\n"
        "test-results\nplaywright-report\ntests\ntest\n",
        encoding="utf-8",
    )
    (project_dir / "compose.yaml").write_text(yaml.safe_dump(render_compose(decisions, port), sort_keys=False), encoding="utf-8")
    examples = []
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
            services = yaml.safe_load((project_dir / "compose.yaml").read_text(encoding="utf-8"))
            services["services"]["otel-collector"]["environment"] = {"OPENOBSERVE_BASIC_AUTH": "${OPENOBSERVE_BASIC_AUTH:?set OPENOBSERVE_BASIC_AUTH}"}
            (project_dir / "compose.yaml").write_text(yaml.safe_dump(services, sort_keys=False), encoding="utf-8")
        collector = {
            "receivers": {"otlp": {"protocols": {"grpc": {"endpoint": "0.0.0.0:4317"}, "http": {"endpoint": "0.0.0.0:4318"}}}},
            "exporters": exporters,
            "service": {"pipelines": {"traces": {"receivers": ["otlp"], "exporters": list(exporters)}, "metrics": {"receivers": ["otlp"], "exporters": list(exporters)}, "logs": {"receivers": ["otlp"], "exporters": list(exporters)}}},
        }
        (project_dir / "otel-collector.yaml").write_text(yaml.safe_dump(collector, sort_keys=False), encoding="utf-8")
