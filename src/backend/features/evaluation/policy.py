"""Bind the evaluation feature to neutral execution-policy hooks."""
from features.evaluation.runtime import runtime


class EvaluationPolicy:
    def apply_context(self, context):
        return runtime.apply_evaluation_scope(context)

    def parse_scope(self, **identity):
        return runtime.parse_evaluation_scope(**identity)

    def project_agent(self, agent):
        return runtime.evaluation_user_agent(agent)

    def current_paths(self):
        if runtime.CURRENT_EVALUATION_SCOPE.get() is not None:
            from features.evaluation.runtime import paths
            return paths
        return None

    def register_tools(self, toolkit, scope, **options):
        from features.evaluation.runtime.tools import register_evaluation_tools
        register_evaluation_tools(toolkit, scope, **options)

    async def authorize(self, request, user_id, *, agent_scoped=False):
        from features.evaluation.admission import authorize
        await authorize(request, user_id, agent_scoped=agent_scoped)
