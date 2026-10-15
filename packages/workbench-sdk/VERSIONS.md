# Compatibility

| SDK | Workbench protocol | Plugin UI | Use |
| --- | --- | --- | --- |
| 1.0.0 | 1.0.0 | v3 workspace | Independent application pages |
| Existing plugin bridge | Existing contract | v1 / v2 | Existing canvas and tool-result pages |

A plugin business version is separate from SDK, protocol and UI versions.
Protocol major versions must match. Required capabilities must be present.
Unknown optional declaration fields can be ignored; they never grant permission.
Keep the SDK bundled in package-local assets for offline operation.

## Upgrade

Keep the application ID stable. Obtain the application key from the authenticated catalog.
The key binds the exact local/cloud execution source, opaque service origin, full installation SHA-256 and UTC creation instant with microseconds.
Never synthesize a key from the plugin slug, name or application ID.
Upgrades retain the installation key. Reinstallation creates a new key.
Old URLs and channels must not silently fall back to another installation.
A new permission requires administrator approval before use.

UI v1/v2 pages remain on their existing bridge.
To add a workspace page, declare UI v3, a workspace module and an application referencing that module.
Rebuild web assets with the matching SDK. Validate the package, then test it in an isolated real host.
Keep original packages and data snapshots for recovery.
A downgrade requires checking that new state and data can be represented by the old version.
Do not restore only an old snapshot after new writes.
