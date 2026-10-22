"""Keep standalone evaluation tests on the installed extension path."""
import pytest
from tests.sandbox.conftest import sandbox_session_store, provider_database


@pytest.fixture(autouse=True)
def evaluation_extension():
    from features.evaluation.extension import setup
    setup()
