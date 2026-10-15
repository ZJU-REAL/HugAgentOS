# Workbench platform adapters

The host provides one interface for Web, Tauri and Electron: `openExternal`,
`pickFile`, `saveFile` and `runtimeCapabilities`. Plugins use the versioned SDK
channel. They receive no Node, filesystem, Tauri invoke or desktop credentials.

| Runtime | External links | File picker | Save |
|---|---|---|---|
| Web | Separate browser window | Browser chooser | Browser download |
| Tauri | Confirmed system browser | Native chooser | Native save dialog |
| Electron | Confirmed system browser | Native chooser | Native save dialog |
| Unknown desktop shell | Explicit unavailable result | Unavailable | Unavailable |

An adapter belongs to one opened application and one account/service lifetime.
Changing account or service cancels pending operations and releases file grants.
Local execution is advertised only while that execution plane is ready.
Cloud and dual applications retain their exact source; failure does not select
a same-name local installation.

The host must check the current installation, package revision and effective
approval before every plugin operation. A host confirmation uses a trusted
button click. Plugin-supplied gesture flags have no effect. External URLs allow
only HTTP/HTTPS without URL credentials. File save names cannot contain paths.

File selection creates an opaque, expiring grant. The native host snapshots the
chosen regular file; it does not return its local path. The host reads or uploads
the selection through the authorized file flow. File contents never travel in
the generic message channel. Saving uses an owner-authorized file resource.
Native file transfers are limited to 8 MiB, with a 32 MiB native snapshot budget.

SDK methods are `runtimeCapabilities(connection)` and
`openExternal(connection, url)`. Required capabilities must be negotiated.
The host provides the file adapter to its controlled file service; plugins
continue using `file.pick` and `file.save` with public file references.

Forwarded third-party HTML has an opaque sandbox origin in desktop windows.
Systems requiring their own origin/storage must use the external browser flow.
Native menus, update handling and the existing desktop session remain shell-owned.

Adapter unit checks and loopback proxy tests do not prove native device support.
Release acceptance must test malicious frames, native dialogs, cancellation,
account/service switching and each applicable local/cloud/dual mode on the
actual Windows, macOS and UOS runtime.

Device selection uses `pickFile({source:"device"})`. The host confirmation names the actual local or cloud upload destination. Only the selected file is uploaded after approval and checked by the authenticated file-selection broker. The default `personal` source selects an already owned artifact.
