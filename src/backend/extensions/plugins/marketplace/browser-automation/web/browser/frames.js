/* One active decode and one replaceable frame bound memory and display latency. */
(() => {
  window.installBrowserFrames = (screen, getState, painted, report) => {
    const context = screen.getContext('2d');
    let pending, decoding = false, generation = 0;
    const current = event => {
      const state = getState();
      return state && !state.closed && event.tab_id === state.active_tab && event.viewport_revision === state.viewport_revision;
    };
    const drain = async () => {
      decoding = true;
      try {
        while (pending) {
          const {event, data, seen} = pending;
          pending = undefined;
          if (!current(event) || seen !== generation) continue;
          let image;
          try {
            image = await createImageBitmap(new Blob([data], {type: 'image/jpeg'}));
            if (!current(event) || seen !== generation) continue;
            const {width, height} = event.viewport;
            if (screen.width !== width) screen.width = width;
            if (screen.height !== height) screen.height = height;
            context.drawImage(image, 0, 0, screen.width, screen.height);
            screen.dataset.revision = String(event.viewport_revision);
            screen.dataset.tab = event.tab_id;
            painted();
          } catch (error) {
            if (seen === generation) report(error);
          } finally {
            image?.close();
          }
        }
      } finally {
        decoding = false;
      }
    };
    return {
      push(event, data) {
        if (!current(event)) return;
        pending = {event, data, seen: generation};
        if (!decoding) void drain();
      },
      invalidate() { generation++; pending = undefined; },
    };
  };
})();
