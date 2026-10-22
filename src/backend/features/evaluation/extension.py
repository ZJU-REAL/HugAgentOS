"""Reserved evaluation sessions use a strict OpenSandbox provider."""
from core.harness.registry import register_sandbox_route, sandbox_route

NAME = "evaluation"


def evaluation_provider():
    from core.config.settings import settings
    from core.sandbox.errors import SandboxError
    from features.evaluation.sandbox_provider.provider import get_evaluation_provider

    if settings.sandbox.provider != "opensandbox":
        raise SandboxError("Evaluation requires OpenSandbox; fallback is forbidden")
    return get_evaluation_provider()


def setup():
    from core.harness.registry.evaluation import register_evaluation_policy
    from features.evaluation.policy import EvaluationPolicy
    register_evaluation_policy(EvaluationPolicy())
    if sandbox_route("eval_") is None:
        register_sandbox_route("eval_", evaluation_provider)
    return True
