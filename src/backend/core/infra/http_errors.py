"""Read safe HTTP failure envelopes without exposing arbitrary upstream text."""
def response_error(response, fallback):
    if response.headers.get("content-type", "").split(";", 1)[0].strip().endswith("json"):
        try:
            value = response.json()
        except ValueError:
            return fallback
        if isinstance(value, dict):
            detail = value.get("detail")
            if isinstance(detail, str):
                return detail[:1000]
    return fallback
