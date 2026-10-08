# Chrome Tabs host visual assets

Source: https://github.com/adamschwartz/chrome-tabs
Commit: 1046a3fd4d164bc3550d82dc46ff1f13c6438e2f
License: MIT (LICENSE.txt).

chrome-tabs.css preserves upstream rules, also used by the isolated browser plugin.
Host packaging adds a documented third-party color exemption and reduced-motion override.
ChromeTabBackground.tsx adapts the upstream tab geometry to React with unique SVG IDs.
CanvasTabBar adapts markup to host-owned file, plugin and agent tabs.
No upstream JavaScript or demo controls are included.
