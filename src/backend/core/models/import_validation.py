"""Validate the resulting model/role graph before an import mutates any row."""
from core.db.models import ModelProvider, ModelRoleAssignment
from core.models.providers.registry import get_spec


def validate_import_graph(db, data, overwrite):
    from core.models.repository import ROLE_DEFINITIONS
    providers = {row.provider_id: {"provider_type": row.provider_type, "provider": row.provider, "extra_config": row.extra_config or {}} for row in db.query(ModelProvider).all()}
    for row in data.get("providers", []):
        key = row.get("provider_id")
        if not key:
            raise ValueError("模型配置缺少 provider_id")
        if key in providers and not overwrite:
            continue
        providers[key] = {**providers.get(key, {}), **row}
        value = providers[key]
        if value.get("provider_type") not in get_spec(value.get("provider")).supports_types:
            raise ValueError("模型用途与所选协议不匹配")
    roles = {row.role_key: row.provider_id for row in db.query(ModelRoleAssignment).all()}
    roles.update({row["role_key"]: row["provider_id"] for row in data.get("role_assignments", []) if row.get("role_key") in ROLE_DEFINITIONS})
    for key, provider_id in roles.items():
        if key not in ROLE_DEFINITIONS:
            continue
        provider = providers.get(provider_id)
        definition = ROLE_DEFINITIONS[key]
        if not provider or provider.get("provider_type") != definition["type"]:
            raise ValueError(f"模型类型与角色不匹配：{key}")
        required = definition.get("requires")
        if required and not (provider.get("extra_config") or {}).get(required):
            raise ValueError(f"模型缺少角色所需能力：{key}")
