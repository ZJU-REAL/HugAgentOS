"""Installed integration contribution."""
NAME = "lark"

def register_credentials():
    from integrations.lark.sandbox import register
    register()

def setup():
    register_credentials()
    return True
