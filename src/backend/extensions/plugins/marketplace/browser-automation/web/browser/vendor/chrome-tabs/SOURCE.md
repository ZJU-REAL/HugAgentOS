# Chrome Tabs visual assets

Source: https://github.com/adamschwartz/chrome-tabs
Commit: 1046a3fd4d164bc3550d82dc46ff1f13c6438e2f
License: MIT (LICENSE.txt).

chrome-tabs.css is the upstream stylesheet, unchanged.
tab.html is the tabTemplate from js/chrome-tabs.js, with the close div changed to an accessible button.
The template is embedded in index.html at packaging time (no runtime fetch).

The plugin uses these original SVG curves, dividers, title and active-tab styles.
Our adapter renders worker-owned tabs with accessible keyboard selection.
Draggabilly and upstream demo controls are not shipped.
