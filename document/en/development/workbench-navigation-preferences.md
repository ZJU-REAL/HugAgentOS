# Workbench navigation preferences

Click “Manage app navigation” in either sidebar to pin, unpin, reorder, or reset navigation. Modern and classic layouts share preferences. Use the move buttons, or focus an item and press Alt+ArrowUp or Alt+ArrowDown.

Administrator-required entries cannot be unpinned. Personal order overrides the default order. Unpinning does not uninstall an app. Reset restores default placement without disabling apps. References to temporarily unavailable installations remain stored; they never grant access.

Preferences are independent for each authenticated user and environment: web, cloud, local, or dual. Different servers retain their own databases. Account and environment changes cancel old requests. Late responses cannot replace the new identity’s state.

The public endpoint is GET/PUT `/v1/workbench/me/navigation-preferences?environment=web`. The server derives identity from authentication and rejects user_id. PUT requires JSON with revision, pinned, and order. pinned=null restores defaults; pinned=[] removes personal pins. Administrator-required entries are merged separately.

Only one concurrent write using the same revision succeeds. The other returns 409. The interface loads current preferences and keeps an explicit conflict notice. Review them and repeat the intended operation. It does not automatically overwrite another device’s configuration. Failed loads can be retried; failed saves retain confirmed preferences.

Storage uses an independent workbench_navigation namespace in content_blocks. It does not replace users_shadow or other preferences. No database schema migration, installation change, or permission change is required.

## Many applications and keyboard navigation

The sidebar measures available height and reserves space for public tools, navigation management, and More. Resizing changes placement without updating preferences. Overflow keeps the saved order.

More opens a compact vertical menu showing icons and names. It includes navigation entries outside the visible rail, including pinned applications displaced by limited height. The current page is marked and unavailable applications are disabled. Browser apps open another window and keep the host page. Search is available in the app center; pinning and ordering remain in page settings.

Tab focuses More; Enter opens its menu. Arrow keys move menu focus. Enter opens an entry. Escape closes More and restores trigger focus. Navigation management retains Alt+Arrow reordering.

The app center shares the directory, icons, and opener. Modern pages without secondary menus show the rail without an empty secondary sidebar. Theme-specific images load separately. Color icons retain their colors. Failed images use a fallback icon. Unopened applications do not create iframes for navigation.

This change requires no schema, installation, or permission migration. The composition layer injects plugin opening. A missing runner reports unavailable functionality. Navigation never treats a plugin as an ordinary external link.

Navigation management is available under Settings → Page Settings, in the Manage app navigation module. Both sidebar layouts omit the separate navigation-management arrow button. Pinning, ordering and reset are performed directly in page settings.

Classic mode shows pinned applications in the user menu instead of duplicating them above the conversation list. Pinning, unpinning and ordering update that menu immediately. Unpinned applications open directly from the More applications submenu. The Apply button follows all page settings modules and confirms sidebar layout changes.
