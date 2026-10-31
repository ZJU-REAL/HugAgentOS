"""An injected container environment must not read repository dotenv files."""
from core.config import environment


def test_disabled_dotenv_does_not_read_any_file(monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    def unexpected_read(*args, **kwargs):
        raise AssertionError("dotenv must not be read")
    monkeypatch.setattr(environment, "dotenv_values", unexpected_read)
    environment._load_env_files()
