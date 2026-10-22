"""Framework-neutral context additions emitted by execution policies."""

def reminder_item(text, origin, *, priority=850, token_budget=4000):
    return dict(content=f"<system-reminder>\n{text}\n</system-reminder>",
                kind="reminder", origin=origin, trust="system",
                priority=priority, token_budget=token_budget)
