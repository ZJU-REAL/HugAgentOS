"""Governance policy applied before capability definitions are persisted."""
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable


@dataclass
class BuildGovernance:
    ensure: Callable | None = None
    validate: Callable | None = None
    tag_options: Callable | None = None
    import_policy: Callable | None = None


@lru_cache(maxsize=1)
def build_governance() -> BuildGovernance:
    return BuildGovernance()


def register_build_governance(*, ensure, validate, tag_options, import_policy):
    services = build_governance()
    services.ensure, services.validate = ensure, validate
    services.tag_options, services.import_policy = tag_options, import_policy


def ensure_ontology_build_valid(*args, **kwargs):
    ensure = build_governance().ensure
    if ensure is None:
        raise RuntimeError("Capability build governance is not registered")
    return ensure(*args, **kwargs)


def validate_build(db, **kwargs):
    validate = build_governance().validate
    if validate is None:
        raise RuntimeError("Capability build validator is not registered")
    return validate(db, **kwargs)


def list_asset_tag_options(db, asset_type):
    options = build_governance().tag_options
    if options is None:
        raise RuntimeError("Capability tag governance is not registered")
    return options(db, asset_type)


def resolve_plugin_import_ontology_validation(*args, **kwargs):
    policy = build_governance().import_policy
    if policy is None:
        raise RuntimeError("Capability import governance is not registered")
    return policy(*args, **kwargs)
