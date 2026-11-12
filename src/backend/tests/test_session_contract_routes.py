"""Every session operation in hugagent_cli.contract names a route this backend serves.

The cloud broker and the desktop device link both build requests from
``contract.ROUTES``. Nothing else checks that those method + path pairs exist, so a
renamed route or a changed method would fail every operation at run time only.
"""

import pytest
from starlette.routing import Match

from hugagent_cli import contract

SAMPLE = {"chat_id": "chat_0123456789abcdef", "run_id": "run_0123456789abcdef",
          "project_id": "prj_0123456789abcdef", "steer_id": "steer_1", "message": "hello", "q": "x"}


def _sample_args(operation):
    needed = contract.path_fields(operation) | contract.REQUIRED.get(operation, set())
    return {name: SAMPLE[name] for name in needed}


def _routes():
    from api.app import app

    return app.routes


def _match(routes, method, path):
    scope = {"type": "http", "method": method, "path": path.split("?", 1)[0], "root_path": ""}
    full = [route for route in routes if route.matches(scope)[0] == Match.FULL]
    if full:
        return full[0]
    # A PARTIAL match is the same path under another method.
    return None


@pytest.mark.parametrize("operation", sorted(contract.ROUTES))
def test_operation_route_exists(operation):
    method, path, _ = contract.endpoint(operation, _sample_args(operation))
    route = _match(_routes(), method, path)
    assert route is not None, f"{operation}: no route serves {method} {path}"


@pytest.mark.parametrize("operation", sorted(contract.ROUTES))
def test_operation_route_is_not_shadowed(operation):
    """The first route that fully matches is the one Starlette runs; it must be the operation's own.

    e.g. ``GET /v1/chats/search`` must not land on ``GET /v1/chats/{chat_id}``.
    """
    method, template = contract.ROUTES[operation]
    path = contract.endpoint(operation, _sample_args(operation))[1]
    route = _match(_routes(), method, path)
    assert route is not None
    assert getattr(route, "path", None) == template, (
        f"{operation}: {method} {path} is served by {getattr(route, 'path', route)!r}, not {template!r}"
    )


def test_every_operation_has_fields_and_a_guide():
    assert set(contract.ROUTES) == set(contract.FIELDS) == set(contract.GUIDE)
    assert contract.READS <= set(contract.ROUTES)
    assert all(contract.ROUTES[op][0] == "GET" for op in contract.READS)
