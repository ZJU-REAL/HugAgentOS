"""Installed channel tool contribution; receivers retain their existing lifecycle."""
NAME = "channels"
def register_tools():
    from integrations.channels.tools.attachment import register_native_contribution
    register_native_contribution()
def setup():
    return True
