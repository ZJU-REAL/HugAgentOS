"""Framework-neutral context additions emitted by execution policies."""

REMINDER_OPEN = "<system-reminder>"
REMINDER_CLOSE = "</system-reminder>"


def wrap_reminder(content: str) -> str:
    """Wrap text in ``<system-reminder>`` tags, the one format every reminder uses.

    Reminders travel as ``user``-role context, never mid-conversation ``system``
    messages: those make models exit the ReAct loop defensively.
    """
    return f"{REMINDER_OPEN}\n{content.strip()}\n{REMINDER_CLOSE}"


def reminder_item(text, origin, *, priority=850, token_budget=4000):
    return dict(content=wrap_reminder(text),
                kind="reminder", origin=origin, trust="system",
                priority=priority, token_budget=token_budget)
