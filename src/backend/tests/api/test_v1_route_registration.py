"""The assembled application registers the core v1 routes and the AppException handler."""

import pytest

from core.infra.exceptions import AppException


@pytest.fixture(scope="module")
def app():
    from api.app import app as assembled

    return assembled


@pytest.mark.parametrize(
    "path",
    [
        "/v1/chats",
        "/v1/chats/search",
        "/v1/chats/{chat_id}",
        "/v1/chats/{chat_id}/messages",
        "/v1/me",
        "/v1/users/{user_id}/preferences",
        "/v1/catalog",
        "/v1/catalog/{kind:catalog_kind}/{id}",
        "/v1/catalog/kb",
        "/v1/catalog/kb/{kb_id}/documents",
        "/v1/me/system/access",
        "/v1/me/system/service-configs",
        "/v1/ontologies/governance/access",
        "/v1/ontologies/governance",
        "/v1/me/logs/tools",
        "/v1/me/logs/usage",
        "/v1/models/providers",
        "/v1/models/roles",
    ],
)
def test_v1_route_is_registered(app, path):
    assert path in {getattr(route, "path", None) for route in app.routes}


def test_app_exception_handler_is_registered(app):
    assert AppException in app.exception_handlers
