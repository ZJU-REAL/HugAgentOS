"""Register pass-through capability build governance."""

from core.ontology.build_validator import BuildValidationReport


def _no_import_policy(*_args, **_kwargs):
    from core.ontology.policy import PluginImportOntologyValidationPolicy

    return PluginImportOntologyValidationPolicy(user_enabled=False, forced=False)


def register():
    from core.harness.registry.build_governance import register_build_governance

    register_build_governance(
        ensure=lambda *_args, **_kwargs: BuildValidationReport(valid=True),
        validate=lambda _db, **_kwargs: BuildValidationReport(valid=True),
        tag_options=lambda _db, _asset_type: [],
        import_policy=_no_import_policy,
    )
