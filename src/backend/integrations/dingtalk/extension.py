"""Installed integration contribution."""
NAME = "dingtalk"

def register_credentials():
    from integrations.dingtalk.sandbox import register
    register()

def setup():
    register_credentials()
    return True
