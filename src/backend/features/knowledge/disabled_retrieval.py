"""Community edition disables external knowledge retrieval."""

MAX_RETRIEVE_TOKENS = 50_000
RETRIEVE_REQUEST_TIMEOUT_SECONDS = 10
RETRIEVE_TOTAL_TIMEOUT_SECONDS = 60
RETRIEVE_MAX_CONCURRENCY = 3


class DatasetRetrievalTimeoutError(TimeoutError):
    pass


class DatasetRetrievalUnavailableError(RuntimeError):
    pass


def retrieve_dataset_content(*args, **kwargs) -> list:
    return []


async def retrieve_dataset_content_async(*args, **kwargs) -> list:
    return []


def list_external_datasets(**kwargs) -> list:
    return []


__all__ = [
    "DatasetRetrievalTimeoutError",
    "DatasetRetrievalUnavailableError",
    "MAX_RETRIEVE_TOKENS",
    "RETRIEVE_MAX_CONCURRENCY",
    "RETRIEVE_REQUEST_TIMEOUT_SECONDS",
    "RETRIEVE_TOTAL_TIMEOUT_SECONDS",
    "list_external_datasets",
    "retrieve_dataset_content",
    "retrieve_dataset_content_async",
]
