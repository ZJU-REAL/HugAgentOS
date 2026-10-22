# Desktop

Owns the device capability store, candidate resolution, runtime views, cloud capability authorization, workcopies and desktop observation records. Desktop schemas are installed on cloud and local hosts independently of whether local file-store execution is enabled.

The host registers `run_capabilities.provider.DesktopRunCapabilityProvider` through the core run-capability interface. `capabilities.paths` controls whether the current runtime has a local capability root. Selection and account authorization are evaluated at use time.

Installation records and their SQLAlchemy commit/rollback notifications live in `capabilities.registry`; immutable row projections live in `registry_types`. Skill candidates, hashing, identity, views and publication use one shared cache/lock state. Cloud capability API groups preserve the original route order and observation route class; model and MCP streaming share one HTTP client and close each upstream response.

Core dependencies include database/session primitives, extension ports, sandbox runtime and capability package parsing. Direct feature dependencies are marketplace archives and knowledge access permissions. Workbench presentation uses a host-injected package view. Sites register upload handlers and receipt localization; automation registers local dispatch and durable operation callbacks. Desktop does not import those component implementations, so feature dependencies remain acyclic. Remaining core-to-feature layering is handled in P6.

Verification covers selection/account revocation, projection persistence, token tampering and expiry, gateway credential/header isolation, streaming response cleanup, workcopy compare-and-swap, original route/schema snapshots, and CE empty-database upgrade/release checks.
