/* DOM input proxy keeps IME composition local and submits completed text once. */
(() => {
  window.installBrowserInput = (screen, keyboard, getState, control) => {
    let composing = false, movePending = false, lastMove, wheelPending = false, wheel, pointer = null, pressedButton = 'left';
    const pressedKeys = new Set();
    const ownsControl = control.owns;
    const ready = () => getState() && screen.dataset.revision === String(getState().viewport_revision) && screen.dataset.tab === getState().active_tab;
    const send = params => {
      const state = getState();
      if (!state || !ready()) return Promise.resolve();
      return control.command('input', { ...params, viewport_revision: state.viewport_revision }).catch(() => {});
    };
    const position = event => {
      const bounds = screen.getBoundingClientRect();
      return { x: (event.clientX - bounds.left) * screen.width / bounds.width,
        y: (event.clientY - bounds.top) * screen.height / bounds.height };
    };
    const focus = event => {
      keyboard.style.left = (event.clientX - screen.parentElement.getBoundingClientRect().left) + 'px';
      keyboard.style.top = (event.clientY - screen.parentElement.getBoundingClientRect().top) + 'px';
      keyboard.focus({ preventScroll: true });
    };
    const touch = window.installBrowserTouch(screen, getState, control, send,
      (point, dx, dy) => queueScroll(point, dx, dy), focus);
    screen.addEventListener('pointerdown', event => {
      if (!ready()) return;
      if (touch('down', event)) return;
      event.preventDefault();
      control.hold('pointer', true);
      pointer = event.pointerId;
      pressedButton = ['left', 'middle', 'right'][event.button] || 'left';
      screen.setPointerCapture(pointer);
      const point = position(event);
      keyboard.style.left = (event.clientX - screen.parentElement.getBoundingClientRect().left) + 'px';
      keyboard.style.top = (event.clientY - screen.parentElement.getBoundingClientRect().top) + 'px';
      keyboard.focus({ preventScroll: true });
      send({ kind: 'down', button: ['left', 'middle', 'right'][event.button], ...point });
    });
    screen.addEventListener('pointerup', event => {
      if (touch('up', event)) return;
      send({ kind: 'up', button: ['left', 'middle', 'right'][event.button], ...position(event) });
      if (screen.hasPointerCapture(event.pointerId)) screen.releasePointerCapture(event.pointerId);
      pointer = null;
      control.hold('pointer', false);
    });
    screen.addEventListener('pointercancel', event => {
      if (touch('cancel', event)) return;
      if (pointer !== null) send({ kind: 'up', button: pressedButton, ...position(event) });
      pointer = null;
      control.hold('pointer', false);
    });
    const move = async () => {
      if (movePending) return;
      movePending = true;
      try {
        while (lastMove && (ownsControl() || pointer !== null)) {
          const point = lastMove;
          lastMove = undefined;
          await send({ kind: 'move', ...point });
        }
      } finally { movePending = false; }
    };
    screen.addEventListener('pointermove', event => {
      if (touch('move', event)) return;
      if (!ownsControl() && pointer === null) return;
      lastMove = position(event);
      void move();
    });
    const scroll = async () => {
      if (wheelPending) return;
      wheelPending = true;
      try {
        while (wheel && ready()) {
          const next = wheel;
          wheel = undefined;
          const { tab, revision, ...params } = next;
          if (tab !== getState()?.active_tab || revision !== getState()?.viewport_revision) continue;
          await send({ kind: 'wheel', ...params });
        }
      } finally { wheelPending = false; wheel = undefined; }
    };
    const queueScroll = (point, dx, dy) => {
      const state = getState();
      if (wheel && (wheel.tab !== state?.active_tab || wheel.revision !== state?.viewport_revision)) wheel = undefined;
      wheel = { ...point, tab: state?.active_tab, revision: state?.viewport_revision, dx: (wheel?.dx || 0) + dx, dy: (wheel?.dy || 0) + dy };
      void scroll();
    };
    screen.addEventListener('wheel', event => {
      if (!ready()) return;
      event.preventDefault();
      keyboard.focus({ preventScroll: true });
      const bounds = screen.getBoundingClientRect();
      const unit = event.deltaMode === WheelEvent.DOM_DELTA_LINE
        ? parseFloat(getComputedStyle(screen).fontSize) * 1.2
        : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? bounds.height : 1;
      const dx = event.deltaX * unit * screen.width / bounds.width;
      const dy = event.deltaY * unit * screen.height / bounds.height;
      queueScroll(position(event), dx, dy);
    }, { passive: false });
    screen.addEventListener('contextmenu', event => event.preventDefault());
    keyboard.addEventListener('compositionstart', () => { composing = true; control.hold('composition', true); });
    const flush = () => {
      if (keyboard.value) { send({ kind: 'text', text: keyboard.value }); keyboard.value = ''; }
    };
    keyboard.addEventListener('compositionend', () => { composing = false; control.hold('composition', false); flush(); });
    keyboard.addEventListener('input', () => { if (!composing) flush(); });
    keyboard.addEventListener('paste', event => {
      event.preventDefault();
      send({ kind: 'text', text: event.clipboardData.getData('text/plain') });
    });
    const keyName = event => ({ Control: 'Control', Meta: 'Meta', Alt: 'Alt', Shift: 'Shift', ' ': 'Space' }[event.key] || event.key);
    keyboard.addEventListener('keydown', event => {
      if (composing || event.isComposing || event.key === 'Process') return;
      if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) return;
      event.preventDefault();
      pressedKeys.add(keyName(event));
      control.hold('key:' + keyName(event), true);
      send({ kind: 'key_down', key: keyName(event) });
    });
    keyboard.addEventListener('keyup', event => {
      pressedKeys.delete(keyName(event));
      control.hold('key:' + keyName(event), false);
      if (!composing && (event.key.length > 1 || event.ctrlKey || event.metaKey || event.altKey)) {
        event.preventDefault();
        send({ kind: 'key_up', key: keyName(event) });
      }
    });
    keyboard.addEventListener('blur', () => {
      composing = false;
      control.hold('composition', false);
      keyboard.value = '';
      for (const key of pressedKeys) { control.hold('key:' + key, false); send({ kind: 'key_up', key }); }
      pressedKeys.clear();
    });
  };
})();
