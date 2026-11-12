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
# The routes cap some identifiers below the general 100 (SteerChatRunRequest.steer_id: 64).
IDENTIFIER_LIMITS = {"steer_id": 64}
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
            limit = IDENTIFIER_LIMITS.get(key, 100)
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,%d}" % limit, value):
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


def contract_version():
    """Fingerprint of the operations both ends agree on. A desktop reports it with each heartbeat,
    so a cloud can tell an out-of-date desktop from a broken one."""
    import hashlib
    import json

    shape = {
        "routes": {op: list(route) for op, route in sorted(ROUTES.items())},
        "fields": {op: sorted(fields) for op, fields in sorted(FIELDS.items())},
        "required": {op: sorted(fields) for op, fields in sorted(REQUIRED.items())},
    }
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:16]


# ── Receipts ─────────────────────────────────────────────────────────────────────────────
# What one operation's receipt says. The desktop link writes it; the cloud reads it and tells the
# agent. Success is the route's data, unchanged. A failure is a dict with "error" (a short code,
# e.g. "http_404", "timed_out", "local_failure") and exactly one of:
#
#   not_executed: True     nothing took effect: refused, invalid, not found, failed before the
#                          request was sent — and every failed read, since a read changes nothing.
#   outcome_unknown: True  a write that may have taken effect. Check before repeating it.
#
# Optional detail on a failure: http_status (int), detail (the server's or exception's message,
# at most DETAIL_LIMIT characters), exception (class name), retryable (bool: the same request may
# succeed later, e.g. a timeout or 5xx on a read). Older desktops send only error + one flag;
# receipt_status() reads both.
DETAIL_LIMIT = 500
STATUS_COMPLETED = "completed"
STATUS_NOT_EXECUTED = "not_executed"
STATUS_OUTCOME_UNKNOWN = "outcome_unknown"
STATUS_FAILED = "failed"
# HTTP statuses that mean "try again later", not "this request is wrong".
RETRYABLE_HTTP = {408, 425, 429}


def _failure(flag, error, http_status, detail, exception, retryable):
    receipt = {"error": str(error), flag: True}
    if http_status is not None:
        receipt["http_status"] = int(http_status)
    if detail:
        text = str(detail)
        receipt["detail"] = text if len(text) <= DETAIL_LIMIT else text[:DETAIL_LIMIT] + "…"
    if exception:
        receipt["exception"] = str(exception)
    if retryable is not None:
        receipt["retryable"] = bool(retryable)
    return receipt


def not_executed(error, *, http_status=None, detail=None, exception=None, retryable=None):
    """A receipt for an operation that certainly took no effect."""
    return _failure("not_executed", error, http_status, detail, exception, retryable)


def outcome_unknown(error, *, http_status=None, detail=None, exception=None):
    """A receipt for a write that may have taken effect."""
    return _failure("outcome_unknown", error, http_status, detail, exception, None)


def failure(operation, error, *, sent, http_status=None, detail=None, exception=None):
    """The one place that decides what kind of failure a receipt reports.

    sent: whether the request may have reached the route that changes something.
    - a read never has an unknown outcome;
    - nothing sent means nothing happened;
    - an HTTP 4xx answer is a refusal: the route rejected the request before acting on it;
    - anything else after a write was sent (5xx, dropped connection, timeout) may have taken effect.
    """
    client_error = http_status is not None and 400 <= int(http_status) < 500
    retryable = (
        int(http_status) in RETRYABLE_HTTP if client_error else True
    )
    if operation in READS or not sent or client_error:
        return not_executed(error, http_status=http_status, detail=detail, exception=exception, retryable=retryable)
    return outcome_unknown(error, http_status=http_status, detail=detail, exception=exception)


def receipt_status(receipt):
    """completed, not_executed, outcome_unknown or failed — for any receipt, old or new."""
    if not isinstance(receipt, dict):
        return STATUS_COMPLETED
    if receipt.get("outcome_unknown"):
        return STATUS_OUTCOME_UNKNOWN
    if receipt.get("not_executed"):
        return STATUS_NOT_EXECUTED
    if receipt.get("error"):
        return STATUS_FAILED
    return STATUS_COMPLETED
