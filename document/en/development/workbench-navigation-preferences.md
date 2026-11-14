# Module navigation and the application directory

The sidebar switches modules. App Center discovers and opens individual applications. These surfaces do not share a pin list.

The modern and classic layouts each have their own module arrangement, stored in page config as `navigation.layouts.modern` and `navigation.layouts.classic`. Each holds `sidebar_items` (primary navigation, in order) and `more_items` (the More menu, in order); a module in neither list is hidden. Administrators arrange each layout in Config → Page configuration → Sidebar layout, and users see the arrangement of the sidebar layout they chose in Settings. Without configuration both layouts use the product default: My Space, Automation and Ability Center in primary navigation; Projects, Sites, App Center and Lab under More. Disabling the lab permission (`lab_enabled`) hides Lab; disabling the sites permission (`sites_enabled`) hides Sites. The two are independent. Local-only mode hides My Space; Automation retains its user permission gate. Settings always stays in the user avatar menu. The legacy `sidebar_items` / `menu_items` fields only feed the workbench layout policy bridge; the sidebars no longer read them.

Opening a module from More displays its secondary sidebar. That sidebar uses the same module list and marks the current page. Reload preserves module navigation. Selecting a module from More on desktop reopens a collapsed sidebar. Classic layout uses the same module model, with text navigation in its primary sidebar and a separate secondary module column.

Plugin, external and individual scenario applications appear only in App Center. The icon/name/description card grid shows mode commands, plugin and external applications; source information, unavailable states and launch behavior remain available there. Neither sidebar nor the user menu lists individual applications. Page settings only select, confirm and save classic/modern layout; application pinning and ordering controls are removed.

## Code boundaries

`utils/pageConfigDefaults.ts` defines the finite module set `MODULE_KEYS`, the default arrangement and its normalization (deduplication, unknown keys dropped, each module in one place). `components/sidebar/moduleNavigation.ts` turns an arrangement plus visibility into primary navigation and More. `useModuleNavigation` reads the page configuration for the current sidebar layout, user permissions and deployment mode. It does not read the application directory or application navigation preferences. `MoreModules` and `ModuleSecondaryNavigation` consume this same model.

`applicationEntries.ts` converts the server directory into App Center view data; it no longer returns a sidebar model. New declarations, application count, application placement policies and stored pins cannot change module navigation. The old mixed navigation, overflow menu, navigation manager and frontend preferences client are deleted. There is no legacy fallback or compatibility switch.

This frontend repair does not change the public GET/PUT `/v1/workbench/me/navigation-preferences?environment=web` contract. Authentication, environment isolation, revision concurrency checks and existing records remain server responsibilities. The current sidebar does not read or write that endpoint. The server still enforces application discovery and launch authorization.

## Regression checks

Run `npm run test:sidebar-navigation` from the frontend directory. It checks rendered menus and secondary sidebars, reload and reopen behavior, both layouts, application cards and launch, unavailable states, permissions, local-only mode, large directories and stale placement policies. Existing module tests also cover capability categories, task ownership and the mobile drawer.

`.github/workflows/sidebar-navigation.yml` runs for relevant frontend/shared-package pull requests and development/product branch pushes. It does not deploy. Repository administrators can configure this check as a required branch-protection check.
