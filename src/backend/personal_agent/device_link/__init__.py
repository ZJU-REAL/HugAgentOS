"""This computer's link to its owner's Personal Agent.

Runs only in the desktop execution plane (the backend the desktop shell starts with
the identity bridge). The link polls the cloud device mailbox; it opens no port.
"""

from personal_agent.device_link.driver import ACTIONS

SESSIONS = "hugagent.sessions"
# Reported only while the macOS accessibility host (embedded Cua Driver) is usable.
UI_ELEMENTS = "ui.elements"
CAPABILITIES = ACTIONS | {SESSIONS, UI_ELEMENTS}
