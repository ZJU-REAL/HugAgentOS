"""Readers for the calls a fake job SDK namespace records."""


def _updates(ns):
    return [p for path, p in ns["_calls"] if path == "ledger" and p.get("op") == "update"]


def _logs(ns):
    return [str(p.get("message") or "") for path, p in ns["_calls"] if path == "log"]
