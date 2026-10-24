"""PostgreSQL/Redis for the tests: started when the decisions ask for them, reported when they cannot be."""

import pytest

from autosdlc.services import LocalServices, postgres_bin


def test_nothing_is_started_without_a_database_or_cache(tmp_path):
    services = LocalServices(tmp_path, {"database": "sqlite", "cache": "none"})
    assert services.start() == {} and services.notes == []
    services.stop()


def test_missing_servers_are_reported_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr("autosdlc.services.postgres_bin", lambda: None)
    monkeypatch.setattr("autosdlc.services.shutil.which", lambda _name: None)
    services = LocalServices(tmp_path, {"database": "postgres", "cache": "redis"})
    assert services.start() == {}
    assert len(services.notes) == 2 and "PostgreSQL" in services.notes[0] and "Redis" in services.notes[1]
    services.stop()


@pytest.mark.skipif(postgres_bin() is None, reason="PostgreSQL is not installed")
def test_a_real_empty_postgres_database_is_started(tmp_path):
    import subprocess

    services = LocalServices(tmp_path, {"database": "postgres", "cache": "none"})
    env = services.start()
    try:
        assert env["DATABASE_URL"].startswith("postgresql://app:app@127.0.0.1:")
        query = subprocess.run([str(postgres_bin() / "psql"), env["DATABASE_URL"], "-tAc", "select 1"],
                               capture_output=True, text=True, timeout=30)
        assert query.stdout.strip() == "1", query.stderr
    finally:
        services.stop()
