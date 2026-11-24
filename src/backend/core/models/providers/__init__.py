"""Multi-vendor model provider support: registry + per-engine model implementations."""

from core.models.providers.registry import PROVIDER_SPECS, ProviderField, ProviderSpec, get_spec, is_known, split_provider_extra, to_frontend_schema, validate_payload
