import pytest


@pytest.fixture(autouse=True)
def disable_tracing(monkeypatch):
    monkeypatch.setenv("JUJU_DISPATCH_PATH", "")
    monkeypatch.setenv("CHARM_TRACING_ENABLED", "0")
