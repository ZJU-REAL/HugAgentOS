"""Conversation-owned compaction and checkpoint services for the model runtime."""
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable


@dataclass
class CompactionServices:
    run: Callable | None = None
    mid_turn: Callable | None = None
    token_limit: Callable | None = None
    trigger_ratio: Callable | None = None
    checkpoint: Callable | None = None


@lru_cache(maxsize=1)
def compaction_services() -> CompactionServices:
    return CompactionServices()


def register_compaction_services(*, run, mid_turn, token_limit, trigger_ratio, checkpoint):
    services = compaction_services()
    services.run = run
    services.mid_turn = mid_turn
    services.token_limit = token_limit
    services.trigger_ratio = trigger_ratio
    services.checkpoint = checkpoint


async def run_compaction(*args, **kwargs):
    run = compaction_services().run
    if run is None:
        raise RuntimeError("Conversation compaction is not registered")
    return await run(*args, **kwargs)


async def run_mid_turn_compaction(*args, **kwargs):
    run = compaction_services().mid_turn
    if run is None:
        raise RuntimeError("Mid-turn compaction is not registered")
    return await run(*args, **kwargs)


def resolve_token_limit(context_window, *, ratio=None):
    resolve = compaction_services().token_limit
    if resolve is None:
        raise RuntimeError("Compaction token policy is not registered")
    return resolve(context_window, ratio=ratio)


def resolve_trigger_ratio():
    resolve = compaction_services().trigger_ratio
    if resolve is None:
        raise RuntimeError("Compaction trigger policy is not registered")
    return resolve()


def create_context_checkpoint(**kwargs):
    factory = compaction_services().checkpoint
    if factory is None:
        raise RuntimeError("Context checkpoint service is not registered")
    return factory(**kwargs)
