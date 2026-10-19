# Standalone preview API

Modified for HugAgentOS; see ../../../NOTICE.

The generated document provides `window.hugagent` before fragment scripts run.
This is a local visualization runtime, not a HugAgentOS application API.

```js
const saved = window.hugagent.widgetState?.privateContent?.count;
const count = typeof saved === 'number' ? saved : 5;
window.addEventListener('hugagent:set_globals', event => {
  // Restore local widgetState and theme without writing state again.
});
// Only after a user changes a control:
window.hugagent.setWidgetState({
  modelContent: null,
  privateContent: { count: 7 }
}).catch(() => { /* Continue with the current in-memory state. */ });
```

Snapshots replace the previous JSON object. Omitted fields become null. The
serialized limit is 16 KiB. Images, secrets and executable values are unsupported.
Standalone browsers may persist state locally; Canvas's opaque sandbox can deny
storage. A rejected save does not prevent ordinary interactions. No state is sent
to the Agent, chat history or server. Theme follows browser light/dark preference.

No follow-up messaging, tool invocation or host design-control API is exposed.
Use native inputs and local JavaScript for design controls. External navigation
may be blocked by the Canvas sandbox; keep essential actions within the preview.
