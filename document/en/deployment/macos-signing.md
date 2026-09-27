# macOS signing and upgrades

[简体中文](../../zh-CN/deployment/macos-signing.md) · [Desktop local mode](desktop-local-mode.md)

## Why upgrades ask for Keychain permission again

The desktop client stores sessions in macOS Keychain. An ad-hoc signature identifies a particular
binary hash, which changes when rebuilt. A previous Always Allow grant cannot automatically follow it.
Reusing the same certificate and application identifier provides a stable code identity.
A fixed self-signed certificate needs no Apple Developer account. Developer ID Application plus
notarization provides Apple distribution trust. Tauri updater `.sig` files serve a separate purpose.

## Without Apple enrollment: a fixed self-signed certificate

Run once on the native Mac builder, from the project's `desktop/` directory:

```bash
python3 scripts/setup-macos-self-signing.py \
  --directory "$HOME/.config/desktop-signing" \
  --create --name Desktop-Local-Signing
```

The helper creates a code-signing certificate valid for ten years, encrypted private key and PKCS#12,
then imports the identity into a dedicated build keychain. Files are private to the build user.
Passwords travel through files or standard input, never process arguments or output. The existing
keychain search list and default are preserved, and no system trust settings are changed.
Repeating setup reuses a completed identity. An incomplete directory is rejected to prevent replacement.

Use this entry point for subsequent builds. It unlocks the keychain within the same SSH session and
sets `HUGAGENT_MACOS_SIGNING_MODE=self-signed` and the pinned certificate fingerprint:

```bash
# From desktop/ in a clean release checkout, with the documented Node/Rust/uv PATH.
HUGAGENT_RELEASE_BUILD=1 python3 "$HOME/.config/desktop-signing/signing.py" \
  --directory "$HOME/.config/desktop-signing" --run npm run build

python3 "$HOME/.config/desktop-signing/signing.py" \
  --directory "$HOME/.config/desktop-signing" \
  --run npm run verify:macos -- --bundle-dir src-tauri/target/release/bundle/macos
```

Self-signed mode supports CI and release builds without a Team ID or Apple notarization credentials.
The verifier checks signature integrity, application identifier, pinned certificate and stable
designated requirement, reporting `appleNotarized: false`. Downloaded apps can still trigger
Gatekeeper prompts because self-signing does not provide Apple notarization.

Securely back up the whole signing directory outside Git, ordinary artifact sync folders and logs.
Keep using the same certificate and private key when moving builders. Renewal, key loss or switching
to Developer ID can change identity and requires a migration plan. The helper preserves Tauri updater
configuration; the existing updater signing key is still required.

## With Apple enrollment: Developer ID and notarization

1. Join the [Apple Developer Program](https://developer.apple.com/programs/enroll/), and configure a
   Developer ID Application certificate and its private key using the
   [Tauri documentation](https://v2.tauri.app/distribute/sign/macos/).
2. Set `HUGAGENT_MACOS_SIGNING_MODE=developer-id` (the default), the full certificate name in
   `APPLE_SIGNING_IDENTITY`, and its matching `APPLE_TEAM_ID`.
3. Release builds also require `APPLE_ID` and `APPLE_PASSWORD` (app-specific password), or
   `APPLE_API_KEY`, `APPLE_API_ISSUER` and `APPLE_API_KEY_PATH`.
   Keep credentials in secure builder configuration or CI Secrets.
4. Run `HUGAGENT_RELEASE_BUILD=1 npm run build`, then run
   `npm run verify:macos -- --bundle-dir src-tauri/target/release/bundle/macos`.
   This mode also checks the stapled notarization ticket and Gatekeeper acceptance.

## Builds and CI

Preflight checks the selected identity and private key. Direct Tauri invocation also checks during
resource preparation and before bundling. A changed signing identity invalidates the Mac runtime
fingerprint so embedded Python and tools are rebuilt under the selected identity.
Use the actual `bundle/macos` path for an explicit Rust target or custom Cargo output directory.
For another brand, specify `--app /path/Example.app --identifier com.example.desktop`.
Self-signed verification can also take `--mode self-signed --certificate-sha1 <40-hex-fingerprint>`.

Public CE Release workflow configuration for self-signing:

- Repository variable: `MACOS_SIGNING_MODE=self-signed`.
- Secrets: `APPLE_CERTIFICATE` (Base64 of the same PKCS#12), `APPLE_CERTIFICATE_PASSWORD`,
  and `APPLE_SIGNING_IDENTITY` (the fixed certificate's 40-hex SHA-1 fingerprint).
- Keep the existing Tauri updater secrets. No Apple account or Team ID is required.

CI imports the identity before signing the runtime and app, then verifies the actual output.
Developer ID is the default mode and additionally needs `APPLE_TEAM_ID`, `APPLE_ID` and
`APPLE_PASSWORD`. Publish the draft only after every check succeeds.
Missing identities stop the build; no silent ad-hoc fallback or automatic certificate rotation occurs.

Only local tests may explicitly set `HUGAGENT_ALLOW_ADHOC=1 APPLE_SIGNING_IDENTITY=-`,
without CI or `HUGAGENT_RELEASE_BUILD=1`. Such packages can prompt again after upgrades and fail
artifact verification.

## Migration and acceptance

Switching from ad-hoc to a fixed certificate can require one more authorization of existing items.
Preserve Keychain service/account names and the application identifier. Do not delete users' keychains,
allow all applications, or move sessions to plaintext.

Client acceptance: log in and authorize an old version, replace it with a newer version signed by
the same certificate, then restart and log in. Also check reinstall, a locked keychain, denied
authorization and logout. System conditions such as a locked keychain can still require authorization.

References: [Apple signing and Keychain identity](https://developer.apple.com/library/archive/technotes/tn2206/)
and [code-signing requirements](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements).
