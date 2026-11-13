import pytest


@pytest.fixture(autouse=True)
def _runs_dir(tmp_path, monkeypatch):
    """Las ejecuciones que abren las pruebas (cli.main, jobs) no se escriben en el out/_runs real."""
    monkeypatch.setenv("SOCIAL_AGENT_RUNS_DIR", str(tmp_path / "_runs"))
    monkeypatch.delenv("SOCIAL_AGENT_RUN_ID", raising=False)
