#!/usr/bin/env python3
"""Edit a chat message stored in the `chat_messages` table.

Usage (from repo root):
    PYTHONPATH=src/backend python src/backend/scripts/edit_chat_message.py list --chat-id <chat_id>
    PYTHONPATH=src/backend python src/backend/scripts/edit_chat_message.py show --message-id <message_id>
    PYTHONPATH=src/backend python src/backend/scripts/edit_chat_message.py edit --message-id <message_id> --content "new content"
    PYTHONPATH=src/backend python src/backend/scripts/edit_chat_message.py edit --message-id <message_id> --from-file new.txt
    PYTHONPATH=src/backend python src/backend/scripts/edit_chat_message.py delete --message-id <message_id>

Or inside the backend container (backend source is mounted at /app/src/backend):
    docker exec -it hugagent-backend python /app/src/backend/scripts/edit_chat_message.py list --chat-id xxx

Notes:
    - Updating `content` keeps the original `message_id` so frontend citation /
      feedback references remain valid.
    - The frontend caches messages in Zustand; users need to refresh the page
      or reopen the session to see the edit.
    - `content` is limited to 100000 chars by a DB CHECK constraint.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# src/backend/scripts/edit_chat_message.py → parents[3] is the repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_SRC = REPO_ROOT / "src" / "backend"

# Make `core.*` importable when run from anywhere.
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

# Load .env from repo root if present.
env_file = REPO_ROOT / ".env"
if env_file.exists():
    load_dotenv(env_file)

from core.db.engine import SessionLocal  # noqa: E402
from core.db.models import ChatMessage, ChatSession  # noqa: E402


MAX_CONTENT_LENGTH = 100_000


def _truncate(text: str, n: int = 80) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[:n] + "…"


def cmd_list(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        session = db.query(ChatSession).filter(ChatSession.chat_id == args.chat_id).first()
        if session is None:
            print(f"[error] chat_id not found: {args.chat_id}", file=sys.stderr)
            return 1

        messages = (
            db.query(ChatMessage)
            .filter(ChatMessage.chat_id == args.chat_id)
            .order_by(ChatMessage.created_at)
            .all()
        )
        print(f"chat_id: {args.chat_id}  ({len(messages)} messages)")
        print(f"{'idx':>3}  {'role':<10}  {'created_at':<26}  message_id                    content")
        print("-" * 120)
        for i, m in enumerate(messages):
            print(
                f"{i:>3}  {m.role:<10}  {str(m.created_at):<26}  "
                f"{m.message_id:<30}  {_truncate(m.content)}"
            )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        msg = db.query(ChatMessage).filter(ChatMessage.message_id == args.message_id).first()
        if msg is None:
            print(f"[error] message_id not found: {args.message_id}", file=sys.stderr)
            return 1
        print(f"message_id:  {msg.message_id}")
        print(f"chat_id:     {msg.chat_id}")
        print(f"role:        {msg.role}")
        print(f"model:       {msg.model}")
        print(f"created_at:  {msg.created_at}")
        print(f"tool_calls:  {msg.tool_calls}")
        print(f"usage:       {msg.usage}")
        print(f"error:       {msg.error}")
        print(f"metadata:    {msg.extra_data}")
        print("----- content -----")
        print(msg.content)
    return 0


def _resolve_new_content(args: argparse.Namespace) -> str:
    if args.content is not None and args.from_file is not None:
        raise SystemExit("[error] --content and --from-file are mutually exclusive")
    if args.content is not None:
        return args.content
    if args.from_file is not None:
        return Path(args.from_file).read_text(encoding="utf-8")
    raise SystemExit("[error] must provide --content or --from-file")


def cmd_edit(args: argparse.Namespace) -> int:
    new_content = _resolve_new_content(args)
    if len(new_content) > MAX_CONTENT_LENGTH:
        print(
            f"[error] content length {len(new_content)} exceeds DB limit {MAX_CONTENT_LENGTH}",
            file=sys.stderr,
        )
        return 1

    with SessionLocal() as db:
        msg = db.query(ChatMessage).filter(ChatMessage.message_id == args.message_id).first()
        if msg is None:
            print(f"[error] message_id not found: {args.message_id}", file=sys.stderr)
            return 1

        old_content = msg.content
        print(f"message_id: {msg.message_id}  (role={msg.role}, chat_id={msg.chat_id})")
        print(f"old length: {len(old_content)}  → new length: {len(new_content)}")
        print(f"old preview: {_truncate(old_content)}")
        print(f"new preview: {_truncate(new_content)}")

        if not args.yes:
            confirm = input("Apply this update? [y/N] ").strip().lower()
            if confirm not in ("y", "yes"):
                print("aborted.")
                return 0

        msg.content = new_content
        db.commit()
        print(f"[ok] updated message_id={msg.message_id}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    with SessionLocal() as db:
        msg = db.query(ChatMessage).filter(ChatMessage.message_id == args.message_id).first()
        if msg is None:
            print(f"[error] message_id not found: {args.message_id}", file=sys.stderr)
            return 1
        print(
            f"About to DELETE message_id={msg.message_id} "
            f"(role={msg.role}, chat_id={msg.chat_id})"
        )
        print(f"content preview: {_truncate(msg.content)}")
        if not args.yes:
            confirm = input("Delete this message? [y/N] ").strip().lower()
            if confirm not in ("y", "yes"):
                print("aborted.")
                return 0
        db.delete(msg)
        db.commit()
        print(f"[ok] deleted message_id={args.message_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Edit chat messages in chat_messages table")
    sub = p.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List all messages in a chat session")
    p_list.add_argument("--chat-id", required=True)
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show full content of one message")
    p_show.add_argument("--message-id", required=True)
    p_show.set_defaults(func=cmd_show)

    p_edit = sub.add_parser("edit", help="Update the content of one message")
    p_edit.add_argument("--message-id", required=True)
    p_edit.add_argument("--content", help="New content as inline string")
    p_edit.add_argument("--from-file", help="Read new content from a UTF-8 text file")
    p_edit.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    p_edit.set_defaults(func=cmd_edit)

    p_del = sub.add_parser("delete", help="Delete one message")
    p_del.add_argument("--message-id", required=True)
    p_del.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    p_del.set_defaults(func=cmd_delete)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
