"""Every URL is owned by exactly one route in the assembled application.

Starlette resolves a request to the *first* registered route whose pattern
matches, so a wildcard segment (``/{kind}/{id}``) silently swallows a sibling
router's literal path (``/kb/{kb_id}``) whenever it happens to be registered
earlier. That is how renaming a knowledge base answered
"At least one field must be provided": ``PATCH /v1/catalog/kb/{kb_id}`` never
reached the knowledge-base router. Registration order must not decide
ownership, so this guard fails on any overlap instead of relying on order.
"""

import re

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Match

_PARAM = re.compile(r"\{[^}]+\}")


@pytest.fixture(scope="module")
def app():
    from api.app import app as assembled

    return assembled


def _api_routes(app):
    return [route for route in app.routes if isinstance(route, APIRoute)]


def _label(route):
    return f"{sorted(route.methods)} {route.path} -> {route.endpoint.__module__}.{route.endpoint.__name__}"


def _resolve(app, method, path):
    scope = {"type": "http", "method": method, "path": path, "root_path": ""}
    for route in _api_routes(app):
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route
    return None


def test_no_route_is_shadowed_by_an_earlier_wildcard(app):
    routes = _api_routes(app)
    shadowed = []
    for index, later in enumerate(routes):
        # A concrete URL a client would send for the later route.
        sample = _PARAM.sub("x0", later.path)
        for earlier in routes[:index]:
            if earlier.path == later.path or not (earlier.methods & later.methods):
                continue
            if earlier.path_regex.match(sample):
                shadowed.append(f"{_label(later)}  is shadowed by  {_label(earlier)}")
    assert not shadowed, "\n".join(shadowed)


@pytest.mark.parametrize(
    ("method", "path", "endpoint"),
    [
        ("PATCH", "/v1/catalog/kb/kb_123", "update_kb_space"),
        ("PATCH", "/v1/catalog/skills/some-skill", "update_catalog_item"),
        ("PATCH", "/v1/catalog/mcp/some-server", "update_catalog_item"),
        ("PATCH", "/v1/catalog/knowledge_bases/kb_123", "update_catalog_item"),
    ],
)
def test_catalog_urls_reach_their_owner(app, method, path, endpoint):
    route = _resolve(app, method, path)
    assert route is not None, f"{method} {path} matches no route"
    assert route.endpoint.__name__ == endpoint, _label(route)
