# Container-responsive layout

The chat toolbar responds to its pane's available width. Home, conversation, project,
and split views share the same layout.

- Tools and model/send controls form separate groups and wrap when necessary.
- Labels collapse progressively while icons, accessible names, and mode exits remain.
  Model names stay visible with ellipsis for long text.
- Touch devices use real 44px targets in normal flow, without overlapping invisible hit layers.
- Project, capability, and knowledge cards use intrinsic grids; long names cannot widen tracks.
- Safe areas, page height, and portaled menus retain their viewport-based constraints.

Implementation: frontend components/chat/ComposerToolbar.tsx,
styles/chat/toolbar-layout.css, styles/projects.css, and styles/catalog/.
A container supplies width and its descendants respond. Do not query the container
from itself or duplicate CSS breakpoints in JavaScript.

From the WSL project directory, enter src/frontend with a supported Node.js version:

    npm run test:container-layout-browser
    npm run test:canvas-layout-browser
    npm run test:chat-width-browser
    npm run lint
    npm run build

The new suite covers narrow containers inside a wide window, phone viewports,
fractional boundaries, light/dark themes, long names, all modes, touch targets,
keyboard exits, and send/stop callbacks.
