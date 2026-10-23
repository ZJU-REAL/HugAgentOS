"""Installed integration contribution."""
NAME = "email"

def register_credentials():
    from integrations.email.sandbox import register
    register()

def setup():
    register_credentials()
    return True
