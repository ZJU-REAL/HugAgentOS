"""HugAgentOS cloud/device CLI. JSON output is suitable for Personal Agent."""

import argparse
import json
import os
import sys
import time
import uuid
from .transport import Client


# Commands of the local server, which `hugagent-server` owns. This client only
# manages sessions and never runs them, so it points the caller there instead.
SERVER_COMMANDS = {"onboard", "serve", "doctor", "engine"}
# The server's options that may precede its command (`--port 3002 serve`).
SERVER_OPTIONS = {"--host", "--port"}


def server_command(argv):
    """The server command among argv's leading server options, if any."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in SERVER_COMMANDS:
            return token
        if token in SERVER_OPTIONS:
            index += 2
        elif token.split("=", 1)[0] in SERVER_OPTIONS:
            index += 1
        else:
            return None
    return None


def paging(sub):
    sub.add_argument("--page", type=int, default=1)
    sub.add_argument("--page-size", type=int, default=20)


def parser():
    p = argparse.ArgumentParser(
        description="Manage HugAgentOS cloud and device sessions and project chats."
    )
    p.add_argument("--server", default=os.environ.get("HUGAGENT_SERVER"))
    p.add_argument("--target", default="cloud", help="cloud or device:<id>")
    p.add_argument(
        "--request-key",
        default=None,
        help="Reuse this key after a timeout; changed arguments are refused",
    )
    p.add_argument(
        "--wait", type=int, default=35, help="Seconds to wait for an operation receipt, maximum 60"
    )
    commands = p.add_subparsers(dest="group", required=True)
    chat = commands.add_parser("chat").add_subparsers(dest="action", required=True)
    for action in ("list", "search", "new", "get", "history", "update", "delete", "send"):
        sub = chat.add_parser(action)
        if action in {"list", "search", "history"}:
            paging(sub)
        if action == "search":
            sub.add_argument("q")
            sub.add_argument("--scope", choices=["title", "all"], default="title")
        if action == "new":
            sub.add_argument("--title", default="新对话")
            sub.add_argument("--project", dest="project_id", help="Project id from project list")
        if action in {"get", "history", "update", "delete", "send"}:
            sub.add_argument("chat_id")
        if action == "history":
            sub.add_argument("--order", choices=["asc", "desc"], default="desc")
        if action == "update":
            sub.add_argument("--title")
            sub.add_argument("--pinned", choices=["true", "false"])
            sub.add_argument("--favorite", choices=["true", "false"])
        if action == "send":
            sub.add_argument("message")
    runs = commands.add_parser("run").add_subparsers(dest="action", required=True)
    for action in ("active", "list", "status", "cancel", "steer", "follow"):
        sub = runs.add_parser(action)
        if action == "active":
            sub.add_argument("chat_id")
        if action in {"status", "cancel", "steer", "follow"}:
            sub.add_argument("run_id")
        if action == "steer":
            sub.add_argument("message")
            sub.add_argument(
                "--delivery-mode", choices=["steer", "followUp", "nextRun"], default="steer"
            )
        if action == "follow":
            sub.add_argument("--timeout", type=int, default=120)
    projects = commands.add_parser("project").add_subparsers(dest="action", required=True)
    listing = projects.add_parser("list")
    listing.add_argument("--query", dest="q", help="Match project name or description")
    paging(listing)
    projects.add_parser("get").add_argument("project_id")
    chats = projects.add_parser("chats")
    chats.add_argument("project_id")
    paging(chats)
    devices = commands.add_parser("device").add_subparsers(dest="action", required=True)
    devices.add_parser("list")
    revoke = devices.add_parser("revoke")
    revoke.add_argument("device_id")
    receipts = commands.add_parser("operation").add_subparsers(dest="action", required=True)
    receipts.add_parser("get").add_argument("operation_id")
    return p


def operation(client, target, name, args, key, wait):
    job = client.request(
        "POST",
        "/v1/personal-agent/session-manager/operations",
        {"target": target, "operation": name, "args": args, "request_key": key},
    )
    if "id" not in job:
        return job
    end = time.monotonic() + max(0, min(60, wait))
    while job["status"] in {"queued", "sent"} and time.monotonic() < end:
        time.sleep(0.3)
        job = client.request("GET", "/v1/personal-agent/session-manager/operations/" + job["id"])
    return job


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = server_command(argv)
    if command:
        print(json.dumps({"error": command + " is a server command: run `hugagent-server "
                          + command + "` on a full HugAgentOS installation. "
                          "This lightweight client only manages sessions."}), file=sys.stderr)
        return 2
    args = parser().parse_args(argv)
    try:
        client = Client(args.server or "", os.environ.get("HUGAGENT_API_KEY", ""))
        if args.group == "device":
            if args.action == "list":
                result = client.request("GET", "/v1/personal-agent/session-manager/devices")
            else:
                result = client.request("DELETE", "/v1/personal-agent/devices/" + args.device_id)
        elif args.group == "operation":
            result = client.request(
                "GET", "/v1/personal-agent/session-manager/operations/" + args.operation_id
            )
        else:
            excluded = {"server", "target", "request_key", "wait", "group", "action", "timeout"}
            values = {k: v for k, v in vars(args).items() if k not in excluded and v is not None}
            for key in ("pinned", "favorite"):
                if key in values:
                    values[key] = values[key] == "true"
            if args.group == "run" and args.action == "steer":
                values["steer_id"] = (args.request_key or uuid.uuid4().hex)[:64]
            name = (
                args.group
                + "."
                + ({"new": "create", "follow": "status"}.get(args.action, args.action))
            )
            request_key = args.request_key or uuid.uuid4().hex
            result = operation(client, args.target, name, values, request_key, args.wait)
            if args.group == "run" and args.action == "follow":
                end = time.monotonic() + max(1, min(3600, args.timeout))
                while (
                    result.get("status") == "completed"
                    and isinstance(result.get("result"), dict)
                    and result["result"].get("status") in {"pending", "running"}
                    and time.monotonic() < end
                ):
                    time.sleep(1)
                    result = operation(
                        client, args.target, name, values, uuid.uuid4().hex, args.wait
                    )
        print(json.dumps(result, ensure_ascii=False))
        if isinstance(result, dict) and (
            result.get("outcome_unknown")
            or result.get("status") in {"expired", "revoked", "outcome_unknown"}
            or (isinstance(result.get("result"), dict) and result["result"].get("error"))
        ):
            return 1
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
