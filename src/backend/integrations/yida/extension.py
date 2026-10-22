"""Installed integration contribution."""
NAME = "yida"

def register_credentials():
    from core.harness.registry.skill_content import register_skill_content_adapter
    from integrations.yida.skill_content import adapt
    register_skill_content_adapter("yida_workspace", adapt)
    from integrations.yida.sandbox import register
    register()

def setup():
    register_credentials()
    return True
