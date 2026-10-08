"""Reusable workflow adapter: callers inject the authorized tool invocation function."""
async def fill_and_verify(invoke, resource_id, url, fields):
    await invoke("browser_action", resource_id=resource_id, action="navigate", params={"url": url})
    before = await invoke("browser_observe", resource_id=resource_id)
    for selector, text in fields.items():
        await invoke("browser_action", resource_id=resource_id, action="fill", params={"selector": selector, "text": text})
    after = await invoke("browser_observe", resource_id=resource_id)
    return {"before": before, "after": after}
