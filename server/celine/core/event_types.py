"""Every event type the server sends, on a conversation's log (Store.event) or the
global feed (Store.global_event, emit_global), by name: an emit and a match use the
same constant, never a string of their own. The web reads the same strings
(web/src/lib/bus.ts)."""

AGENT_STATE = "agent.state"

APPROVAL_ASKED = "approval.asked"
APPROVAL_CLOSED = "approval.closed"
APPROVALS_CHANGED = "approvals.changed"

ATTACHMENT_DESCRIBED = "attachment.described"

ARTIFACT_UPDATED = "artifact.updated"

BROWSER_STATE = "browser.state"

CITATIONS_CHECKED = "citations.checked"

CONTEXT_AGED = "context.aged"
CONTEXT_COMPACT_FAILED = "context.compact_failed"
CONTEXT_COMPACT_SKIPPED = "context.compact_skipped"
CONTEXT_COMPACTED = "context.compacted"
CONTEXT_COMPACTING = "context.compacting"
CONTEXT_EMERGENCY = "context.emergency"
CONTEXT_USAGE = "context.usage"

CONVERSATION_ARCHIVED = "conversation.archived"
CONVERSATION_AUTO_CONTEXT = "conversation.auto_context"
CONVERSATION_CREATED = "conversation.created"
CONVERSATION_DELETED = "conversation.deleted"
CONVERSATION_NOTES_OFF = "conversation.notes_off"
CONVERSATION_PINNED = "conversation.pinned"
CONVERSATION_PURGED = "conversation.purged"
CONVERSATION_RESTORED = "conversation.restored"
CONVERSATION_SETTINGS = "conversation.settings"
CONVERSATION_TITLE = "conversation.title"
CONVERSATION_UNARCHIVED = "conversation.unarchived"

DOCUMENTS_CHANGED = "documents.changed"
DOCUMENTS_DELETED = "documents.deleted"
DOCUMENTS_ITEM = "documents.item"

EVENTS_ADDED = "events.added"

FEATURES_CHANGED = "features.changed"

FROZEN_CHANGED = "frozen.changed"

GUARD_BANNER = "guard.banner"
GUARD_PASSES = "guard.passes"

HER_CHANGED = "her.changed"
HER_HOLD = "her.hold"

IMAGES_BATCH = "images.batch"
IMAGES_NOTICE = "images.notice"

INTERCOM_STATE = "intercom.state"

JOB_STATE = "job.state"

KB_CHANGED = "kb.changed"
KB_JOB = "kb.job"
KB_TASK = "kb.task"
KB_TASK_REMOVED = "kb.task.removed"

LLM_QUEUE = "llm.queue"

MESSAGE_ATTACHMENTS = "message.attachments"
MESSAGE_CREATED = "message.created"
MESSAGE_DELTA = "message.delta"
MESSAGE_DONE = "message.done"
MESSAGE_PURGED = "message.purged"
MESSAGE_REASONING = "message.reasoning"
MESSAGE_REOPENED = "message.reopened"
MESSAGE_REWRITTEN = "message.rewritten"

MISSION_STATE = "mission.state"

MODEL_LOADED = "model.loaded"

MODELS_STATE = "models.state"

MUSIC_BATCH = "music.batch"

OVERRIDES_CHANGED = "overrides.changed"

PLAN_STATE = "plan.state"

PUSH_SUBSCRIPTIONS = "push.subscriptions"

RUN_STATE = "run.state"

SETTINGS_CHANGED = "settings.changed"

SHARE_UPDATED = "share.updated"

SOURCE_STATE = "source.state"

TERMINAL_STATE = "terminal.state"

TOOL_DETACHED = "tool.detached"
TOOL_DONE = "tool.done"
TOOL_FORMING = "tool.forming"
TOOL_GATED = "tool.gated"
TOOL_PROGRESS = "tool.progress"
TOOL_RESET = "tool.reset"
TOOL_STARTED = "tool.started"

TURN_STATE = "turn.state"

VERSION_CHANGED = "version.changed"

# one word: a turn that failed (its log), its mood, a notice on the feed
ERROR = "error"
MOOD = "mood"
NOTICE = "notice"
