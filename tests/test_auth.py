"""Shared-password protection: disabled without APP_PASSWORD, exact match required with it."""

from autosdlc.auth import auth_enabled, password_matches


def test_auth_is_disabled_without_a_password(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    assert not auth_enabled()
    assert not password_matches("")
    assert not password_matches("anything")


def test_empty_password_keeps_the_app_open(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "")
    assert not auth_enabled()


def test_only_the_exact_password_matches(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret-ş")
    assert auth_enabled()
    assert password_matches("s3cret-ş")
    assert not password_matches("s3cret")
    assert not password_matches("S3CRET-ş")
    assert not password_matches("")
