"""Host binding for scoped job action operations."""
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

@dataclass
class JobRegistration:
    factory: Callable | None = None

@lru_cache(maxsize=1)
def job_registration():
    return JobRegistration()

def register_job_operations(factory: Callable):
    job_registration().factory = factory

def job_operations(**scope):
    factory = job_registration().factory
    if factory is None:
        raise RuntimeError("Job operations are not registered")
    return factory(**scope)
