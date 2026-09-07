"""Desktop capability runtime: partitioned storage, name resolution, flat runtime views.

Three layers, deliberately separated (capability plan v2.0, ch. 04):

- **store** — ``<root>/<kind>s/<profile>/<key>/<revision>/``: immutable versions, one
  sub-tree per source profile, same-named keys from different profiles coexist.
- **resolver** — the single place that decides which candidate a runtime name points
  to for this device/user. Explicit user choice → explicit request → the current
  account's usable candidate → the only usable candidate → otherwise a conflict.
- **view** — a flat directory of directory links (``<view>/<runtime_name>``) the
  execution plane and the model see. The model-facing path contract
  ``/workspace/skills/<name>`` never changes.

The package is inert unless :func:`paths.capability_root` is set — only the desktop
local runtime keeps a file store; cloud deployments keep their database truth.
"""
