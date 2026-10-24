"""This computer's link to its owner's Personal Agent.

Runs only in the desktop execution plane (the backend the desktop shell starts with
the identity bridge). The link polls the cloud device mailbox; it opens no port.
"""

from personal_agent.device_link.driver import ACTIONS

SESSIONS = "hugagent.sessions"
CAPABILITIES = ACTIONS | {SESSIONS}
