/* A touch tap clicks; a drag scrolls without beginning mouse selection. */
(() => {
  window.installBrowserTouch = (screen, getState, control, send, scroll, focus) => {
    const pointers = new Set();
    let gesture, serial = 0;
    const position = event => {
      const bounds = screen.getBoundingClientRect();
      return { x: (event.clientX - bounds.left) * screen.width / bounds.width,
        y: (event.clientY - bounds.top) * screen.height / bounds.height };
    };
    const valid = () => gesture && !gesture.cancelled
      && gesture.tab === getState()?.active_tab && gesture.revision === getState()?.viewport_revision;
    return (type, event) => {
      if (event.pointerType !== 'touch') return false;
      event.preventDefault();
      if (type === 'down') {
        pointers.add(event.pointerId);
        screen.setPointerCapture(event.pointerId);
        if (gesture) { gesture.cancelled = true; return true; }
        const state = getState();
        gesture = { hold: 'touch:' + ++serial, id: event.pointerId, tab: state?.active_tab, revision: state?.viewport_revision,
          origin: position(event), last: position(event), startX: event.clientX, startY: event.clientY, scrolling: false };
        control.hold(gesture.hold, true);
      } else if (type === 'move' && gesture?.id === event.pointerId && valid()) {
        const point = position(event);
        if (!gesture.scrolling && Math.hypot(event.clientX - gesture.startX, event.clientY - gesture.startY) >= 8) gesture.scrolling = true;
        if (gesture.scrolling) {
          scroll(gesture.origin, gesture.last.x - point.x, gesture.last.y - point.y);
          gesture.last = point;
        }
      } else if (type === 'up' || type === 'cancel') {
        let pendingTap = false;
        pointers.delete(event.pointerId);
        if (screen.hasPointerCapture(event.pointerId)) screen.releasePointerCapture(event.pointerId);
        if (type === 'up' && gesture?.id === event.pointerId && valid() && !gesture.scrolling && !pointers.size) {
          focus(event);
          const hold = gesture.hold;
          pendingTap = true;
          void Promise.all([
            send({kind: 'down', button: 'left', ...gesture.origin}),
            send({kind: 'up', button: 'left', ...gesture.origin}),
          ]).finally(() => control.hold(hold, false));
        }
        if (!pointers.size) { if (gesture && !pendingTap) control.hold(gesture.hold, false); gesture = undefined; }
        else if (gesture) gesture.cancelled = true;
      }
      return true;
    };
  };
})();
