import assert from 'node:assert/strict';

import {
  desktopMenuActionUrl,
  desktopWindowActionUrl,
} from '../src/desktop/desktopBridge';
import { useDesktopOverlayStore } from '../src/desktop/overlayStore';

assert.equal(desktopMenuActionUrl('check_update'), '/__desktop/menu?action=check_update');
assert.equal(desktopWindowActionUrl('toggle-maximize'), '/__desktop/win?action=toggle-maximize');

const overlays = useDesktopOverlayStore.getState();
overlays.openOverlay('account-menu');
assert.equal(useDesktopOverlayStore.getState().activeOverlay, 'account-menu');

useDesktopOverlayStore.getState().openOverlay('desktop-file-menu');
assert.equal(useDesktopOverlayStore.getState().activeOverlay, 'desktop-file-menu');

useDesktopOverlayStore.getState().closeOverlay('account-menu');
assert.equal(useDesktopOverlayStore.getState().activeOverlay, 'desktop-file-menu');

useDesktopOverlayStore.getState().toggleOverlay('desktop-file-menu');
assert.equal(useDesktopOverlayStore.getState().activeOverlay, null);

console.log('desktop overlay tests passed');
