"""Owner and conversation scope shared by feature services.

This object composes services. Callers select the service they need explicitly;
no catch-all proxy exposes unrelated operations on one service.
"""


class AgentContext:
    def __init__(self, db, user_id, chat_id=None):
        from personal_agent.identity.service import IdentityService
        from personal_agent.conversation.service import ConversationService
        from personal_agent.tasks.service import TaskService

        self.db, self.user_id, self.chat_id = db, str(user_id), chat_id
        self.identity = IdentityService(self)
        self.conversation = ConversationService(self)
        self.tasks = TaskService(self)
