"""Shared stand-ins for the vision bridge tests."""


class _FakeCfg:
    base_url = "http://fake/v1"
    api_key = "k"
    model_name = "fake-vl"
    temperature = 0.6
    max_tokens = 4096
    context_length = 0
    timeout = 30
    provider = "openai_compatible"
    provider_extra: dict = {}
    extra: dict = {}
