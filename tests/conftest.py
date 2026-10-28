import pytest


@pytest.fixture(autouse=True)
def opencode_key(monkeypatch):
    # The engine reads its key only from the environment.
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
