"""Domain seams preserve payloads, failures and concrete host authorization."""
import subprocess
import sys


def test_unconfigured_core_seams_are_standalone():
    subprocess.run([sys.executable, "-c", """
import sys
from core.harness.registry.catalog_options import catalog_options
from core.harness.registry.retrieval_context import runtime_request_context
from core.harness.registry.model_credentials import prepare_request_headers
assert catalog_options('kb_spaces', object()) == []
assert runtime_request_context(['kb_one']) == ({}, {})
assert prepare_request_headers('key', 'https://model.invalid') == {
    'Content-Type': 'application/json', 'Authorization': 'Bearer key'}
assert not any(name.startswith(('features.', 'integrations.')) for name in sys.modules)
"""], check=True)


def test_retrieval_host_uses_current_provider_function(monkeypatch):
    from core.harness.registry.retrieval_context import runtime_request_context
    from features.knowledge import edition as external_provider
    from features.knowledge.extension import register_retrieval_context
    selected = ['kb_local', 'remote_one']
    captured = []
    def provider(ids):
        captured.append(ids)
        return {'X-Remote': 'one'}, {'REMOTE_IDS': 'one'}
    register_retrieval_context()
    monkeypatch.setattr(external_provider, 'runtime_request_context', provider)
    assert runtime_request_context(selected) == ({'X-Remote': 'one'}, {'REMOTE_IDS': 'one'})
    assert captured == [selected]


def test_model_host_propagates_account_rejection(monkeypatch):
    import pytest
    from core.harness.registry.model_credentials import prepare_request_headers
    from features.desktop.bridge import model_credentials as desktop_model_credentials
    from features import register_model_credentials
    def reject(*args):
        raise ValueError('account changed')
    register_model_credentials()
    monkeypatch.setattr(desktop_model_credentials, 'prepare_request_headers', reject)
    with pytest.raises(ValueError, match='account changed'):
        prepare_request_headers('reference', 'https://model.invalid')


def test_knowledge_picker_keeps_order_and_omits_deleted(db_session):
    from datetime import timedelta
    from core.db.models import UserShadow
    from core.infra.time import utc_now
    from core.harness.registry.catalog_options import catalog_options
    from features.knowledge.extension import register_catalog_options
    from features.knowledge.models import KBSpace
    register_catalog_options()
    now = utc_now()
    db_session.add(UserShadow(user_id='picker-owner', username='owner'))
    db_session.flush()
    db_session.add_all([
        KBSpace(kb_id='old', user_id='picker-owner', name='Old', created_at=now-timedelta(days=1)),
        KBSpace(kb_id='new', user_id='picker-owner', name='New', description='details', created_at=now),
        KBSpace(kb_id='gone', user_id='picker-owner', name='Gone', deleted_at=now),
    ])
    db_session.commit()
    assert catalog_options('kb_spaces', db_session) == [
        {'id': 'new', 'name': 'New', 'description': 'details'},
        {'id': 'old', 'name': 'Old', 'description': ''},
    ]
