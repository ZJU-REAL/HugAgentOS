"""Fixed operations shared by the cloud broker and the desktop device link."""

import re
from string import Formatter
from urllib.parse import urlencode

READS = {
    "chat.list",
    "chat.search",
    "chat.get",
    "chat.history",
    "run.active",
    "run.list",
    "run.status",
    "project.list",
    "project.get",
    "project.chats",
}
# Path fields come from the route template and are always required.
ROUTES = {
    "chat.list": ("GET", "/v1/chats"),
    "chat.search": ("GET", "/v1/chats/search"),
    "chat.create": ("POST", "/v1/chats"),
    "chat.get": ("GET", "/v1/chats/{chat_id}"),
    "chat.history": ("GET", "/v1/chats/{chat_id}/messages"),
    "chat.update": ("PATCH", "/v1/chats/{chat_id}"),
    "chat.delete": ("DELETE", "/v1/chats/{chat_id}"),
    "chat.send": ("POST", "/v1/agents/responses"),
    "run.active": ("GET", "/v1/chats/{chat_id}/active-run"),
    "run.list": ("GET", "/v1/chats/active-runs"),
    "run.status": ("GET", "/v1/chat-runs/{run_id}"),
    "run.cancel": ("POST", "/v1/chat-runs/{run_id}/cancel"),
    "run.steer": ("POST", "/v1/chat-runs/{run_id}/steer"),
    "project.list": ("GET", "/v1/projects"),
    "project.get": ("GET", "/v1/projects/{project_id}"),
    "project.chats": ("GET", "/v1/projects/{project_id}/chats"),
}
FIELDS = {
    "chat.list": {"page", "page_size"},
    "chat.search": {"q", "page", "page_size", "scope"},
    "chat.create": {"title", "project_id"},
    "chat.get": {"chat_id"},
    "chat.history": {"chat_id", "page", "page_size", "order"},
    "chat.update": {"chat_id", "title", "pinned", "favorite"},
    "chat.delete": {"chat_id"},
    "chat.send": {"chat_id", "message"},
    "run.active": {"chat_id"},
    "run.list": set(),
    "run.status": {"run_id"},
    "run.cancel": {"run_id"},
    "run.steer": {"run_id", "message", "steer_id", "delivery_mode"},
    "project.list": {"q", "page", "page_size"},
    "project.get": {"project_id"},
    "project.chats": {"project_id", "page", "page_size"},
}
# Required fields that are not part of the path.
REQUIRED = {
    "chat.search": {"q"},
    "chat.send": {"chat_id", "message"},
    "run.steer": {"message", "steer_id"},
}
IDENTIFIERS = {"chat_id", "run_id", "steer_id", "project_id"}
# What each operation is for, read by agents that call the operations as a tool.
GUIDE = {
    "project.list": "the user's projects; q filters by name or description",
    "project.get": "one project: name, description, instructions",
    "project.chats": "the chats inside one project",
    "chat.list": "recent chats, newest first",
    "chat.search": "find chats by title (scope=title) or also by content (scope=all)",
    "chat.get": "one chat: title, project",
    "chat.history": "a chat's messages, newest first by default",
    "chat.create": "a new chat, inside a project when project_id is given; it then follows the project's instructions and files",
    "chat.update": "rename, pin or favorite a chat",
    "chat.delete": "delete a chat",
    "chat.send": "send a message to a chat; starts a run that works in the background and returns its run_id",
    "run.active": "the run working in one chat now, if any",
    "run.list": "all runs working now",
    "run.status": "one run's status: pending, running, needs_attention, completed, failed or cancelled",
    "run.cancel": "stop a run",
    "run.steer": "add a message to a working run; steer_id is any new unique id",
}


def path_fields(operation):
    return {name for _, name, _, _ in Formatter().parse(ROUTES[operation][1]) if name}


def validate(operation, args):
    if operation not in FIELDS or not isinstance(args, dict) or set(args) - FIELDS[operation]:
        raise ValueError("Unsupported session operation or arguments")
    if (REQUIRED.get(operation, set()) | path_fields(operation)) - set(args):
        raise ValueError("Missing session operation arguments")
    for key, value in args.items():
        if key in IDENTIFIERS:
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
                raise ValueError("Invalid identifier")
            if value.startswith(("pa_chat_", "pa_run_")):
                raise ValueError("Use Personal Agent task APIs for this identifier")
        elif key in {"page", "page_size"}:
            if type(value) is not int or not 1 <= value <= (100 if key == "page_size" else 100000):
                raise ValueError("Invalid page")
        elif key in {"pinned", "favorite"}:
            if type(value) is not bool:
                raise ValueError("Expected boolean")
        elif (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > (10000 if key == "message" else 300)
        ):
            raise ValueError("Invalid text argument")
    if args.get("scope", "title") not in {"title", "all"}:
        raise ValueError("Invalid search scope")
    if args.get("order", "desc") not in {"asc", "desc"}:
        raise ValueError("Invalid message order")
    if args.get("delivery_mode", "steer") not in {"steer", "followUp", "nextRun"}:
        raise ValueError("Invalid delivery mode")
    return dict(args)


def endpoint(operation, args):
    args = validate(operation, args)
    method, template = ROUTES[operation]
    in_path = path_fields(operation)
    path = template.format(**args)
    body = {k: v for k, v in args.items() if k not in in_path}
    if operation == "chat.send":
        body["stream"] = True
    if operation == "chat.history":
        body.setdefault("order", "desc")  # The CLI lists newest first; the API defaults to oldest.
    if method == "GET":
        path += "?" + urlencode(body) if body else ""
        body = None
    return method, path, body
