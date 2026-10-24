"""Multi-vendor model provider support: registry + per-engine model implementations."""

from core.models.providers.registry import PROVIDER_SPECS, ProviderField, ProviderSpec, get_spec, is_known, list_specs, split_provider_extra, to_frontend_schema, validate_payload
