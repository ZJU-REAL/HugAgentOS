"""Install ontology governance at the application composition boundary."""

def register():
    from core.harness.registry.build_governance import register_build_governance
    from core.ontology.build_validator import ensure_ontology_build_valid, OntologyBuildValidator
    from core.ontology.service import OntologyService
    from core.ontology.policy import resolve_plugin_import_ontology_validation
    register_build_governance(
        ensure=ensure_ontology_build_valid,
        validate=lambda db, **kwargs: OntologyBuildValidator(db).validate(**kwargs),
        tag_options=lambda db, asset_type: OntologyService(db).list_asset_tag_options(asset_type),
        import_policy=resolve_plugin_import_ontology_validation,
    )
