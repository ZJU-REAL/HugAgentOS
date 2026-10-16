# Changelog

## Unreleased

- Add bundled declaration validation and loopback permission preview tools.
- Add an independently built workspace example with child navigation and data/file requests.
- Add API, error handling and version compatibility documentation.
- Keep SDK 1.0.0 wire behavior unchanged. Preview fixtures are not production authorization.

## 1.0.0

Workspace handshake, bounded request/cancel, theme and locale updates, disposal.
UI v1/v2 remain on the existing host bridge.

# Changelog

## 1.0.0

Workspace handshake, bounded request/cancel, theme and locale updates, disposal. UI v1/v2 remain on the existing host bridge.

WB-09 adds typed data query, file picker/save, chat draft, and navigation helpers. JSON validation rejects binary and unsafe structures before cloning. Document exit disposes sessions. Cancellation always settles requests after port failures.
