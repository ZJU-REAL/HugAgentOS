"""SQLAlchemy ORM models.

Split into submodules by domain; this __init__ re-exports all model classes verbatim (plus Base/JSONType/INETType),
so `from core.db.models import X` and `from core.db.models import *` both stay unchanged.
"""

from core.db.engine import Base
from core.db.models.column_types import INETType, JSONType

from core.db.edition import EDITION_MODEL_EXPORTS
from core.db.models.chat import ChatCompactionState, ChatMessage, ChatSession
from core.db.models.chat_runs import ChatRun, ChatRunOperation, ChatSteerQueueItem
from core.db.models.chat_resources import ChatSandboxSnapshot, ToolMediaBlob, MessageFeedback
from core.db.models.identity import LocalUser, UserApiKey, UserFolder, UserShadow
from core.db.models.project import Project, ProjectFavorite

globals().update(EDITION_MODEL_EXPORTS)
from core.db.models.admin import AdminMcpServer, AdminPromptPart, AdminSkill, InstalledPlugin, SkillDependencyRequest
from core.db.models.agent import AgentLoop, LoopIteration, Plan, PlanStep, UserAgent
from core.db.models.agent_api import AgentApiCallLog
from core.db.models.artifact import Artifact, ContentBlock

from core.db.models.chat_mode import ChatMode
from core.db.models.config import ModelProvider, ModelRoleAssignment, SystemConfig

from core.db.models.knowledge import CatalogOverride
from core.db.models.logs import (
    HarnessEventCursor,
    HarnessEventLog,
    HarnessUsageAttempt,
    HarnessUsageCursor,
    SkillCallLog,
    SubAgentCallLog,
    ToolCallLog,
    ToolEffectLease,
    ToolEffectLedger,
    ToolEffectReceipt,
    RemoteToolEffect,
)
from core.db.models.evolution import (
    EvolutionAgentProfile,
    EvolutionCandidate,
    EvolutionCreditDecision,
    EvolutionEpisode,
    EvolutionEvaluation,
    EvolutionEvidencePack,
    EvolutionMemoryOp,
    EvolutionPromotionLink,
    EvolutionRelease,
    EvolutionTraceEvent,
)
from core.db.models.memory import (
    MemoryOutbox,
    MemoryRefShadow,
    MemorySanitizerRule,
    ProfileMemory,
)
from core.db.models.ontology import (
    OntologyDraft,
    OntologyEnforcementEvent,
    OntologyPack,
    OntologyPackVersion,
    OntologyReviewRun,
)
from core.db.models.command import CommandTask
from core.db.models.job import JOB_LIVE_STATUSES, JOB_TERMINAL_STATUSES, Job, JobCall, JobItem
from core.db.edition import EDITION_MODEL_NAMES, edition_model_exports
globals().update(edition_model_exports())


def __getattr__(name):
    # Edition models are absent above only when the edition model package
    # itself started this import; it has finished loading by first access.
    if name in EDITION_MODEL_NAMES:
        value = edition_model_exports()[name]
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    'ChatMode',
    'Base',
    'JSONType',
    'INETType',
    'UserShadow',
    'UserApiKey',
    'AgentApiCallLog',
    'LocalUser',
    'UserFolder',
    'Project',
    'ProjectFavorite',
    'ChatSession',
    *EDITION_MODEL_EXPORTS.keys(),
    'ChatMessage',
    'ChatCompactionState',
    'ChatRun',
    'CommandTask',
    'ChatRunOperation',
    'ChatSteerQueueItem',
    'ToolEffectLedger',
    'ToolEffectLease',
    'ToolEffectReceipt',
    'RemoteToolEffect',
    'HarnessUsageCursor',
    'HarnessUsageAttempt',
    'HarnessEventCursor',
    'HarnessEventLog',
    'MessageFeedback',
    'ChatSandboxSnapshot',
    'ToolMediaBlob',
    'CatalogOverride',
    'Artifact',
    'ContentBlock',
    'ModelProvider',
    'SystemConfig',
    'ModelRoleAssignment',
    'AdminSkill',
    'SkillDependencyRequest',
    'AdminPromptPart',
    'AdminMcpServer',
    'InstalledPlugin',
    'UserAgent',
    'Plan',
    'PlanStep',
    'AgentLoop',
    'LoopIteration',
    'Job',
    'JobItem',
    'JobCall',
    'JOB_TERMINAL_STATUSES',
    'JOB_LIVE_STATUSES',
    'ToolCallLog',
    'SubAgentCallLog',
    'SkillCallLog',
    'ProfileMemory',
    'MemoryOutbox',
    'MemorySanitizerRule',
    'MemoryRefShadow',
    'EvolutionEpisode',
    'EvolutionTraceEvent',
    'EvolutionCandidate',
    'EvolutionRelease',
    'EvolutionEvaluation',
    'EvolutionAgentProfile',
    'EvolutionMemoryOp',
    'EvolutionEvidencePack',
    'EvolutionCreditDecision',
    'EvolutionPromotionLink',
    'OntologyPack',
    'OntologyPackVersion',
    'OntologyEnforcementEvent',
    'OntologyReviewRun',
    'OntologyDraft',
    *EDITION_MODEL_NAMES,
]


from .plugin_resource import PluginResource, PluginResourceTicket, PluginResourceCheckpoint

__all__ = [name for name in __all__ if name in globals() or name in EDITION_MODEL_NAMES]
