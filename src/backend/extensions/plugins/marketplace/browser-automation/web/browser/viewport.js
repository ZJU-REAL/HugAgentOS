/* Resize the remote layout, and map input to the displayed bitmap rectangle. */
(() => {
  window.installBrowserViewport = (stage, screen, channel, getState, report, busy, zoomChanged = () => {}) => {
    const zooms = new Map();
    const steps = [.5, .67, .75, .8, .9, 1, 1.1, 1.25, 1.5, 1.75, 2];
    const zoom = () => zooms.get(getState()?.active_tab) || 1;
    let activeTab, desired, applied, running = false, connected = false, debounce;
    const fit = () => {
      const scale = Math.min(stage.clientWidth / screen.width, stage.clientHeight / screen.height);
      screen.style.width = Math.max(0, screen.width * scale) + 'px';
      screen.style.height = Math.max(0, screen.height * scale) + 'px';
    };
    const flush = async () => {
      if (running || !connected || !desired || !getState()?.active_tab || getState()?.dialogs?.length) return;
      if (busy()) { clearTimeout(debounce); debounce = setTimeout(() => { void flush(); }, 120); return; }
      running = true;
      let requestedTab;
      try {
        while (!busy() && !getState()?.dialogs?.length && connected && getState()?.active_tab && desired && JSON.stringify(desired) !== applied) {
          const next = desired;
          const tab = getState().active_tab;
          requestedTab = tab;
          await channel.command('resize', { ...next, tab_id: tab });
          applied = getState()?.active_tab === tab ? JSON.stringify(next) : undefined;
        }
      } catch (error) { report(error); }
      finally {
        running = false;
        if (connected && requestedTab && requestedTab !== getState()?.active_tab) {
          clearTimeout(debounce); debounce = setTimeout(() => { void flush(); }, 0);
        }
      }
    };
    const measure = () => {
      fit();
      const limits = getState()?.viewport_limits;
      if (!limits || !stage.clientWidth || !stage.clientHeight) return;
      const next = {
        width: Math.max(limits.min_width, Math.min(limits.max_width, Math.round(stage.clientWidth / zoom()))),
        height: Math.max(limits.min_height, Math.min(limits.max_height, Math.round(stage.clientHeight / zoom()))),
      };
      if (JSON.stringify(next) === JSON.stringify(desired) && applied === JSON.stringify(next)) return;
      desired = next;
      clearTimeout(debounce);
      debounce = setTimeout(() => { void flush(); }, 120);
    };
    new ResizeObserver(measure).observe(stage);
    return {
      zoom(direction) {
        const tab = getState()?.active_tab;
        if (!tab) return;
        const current = zoom();
        const limits = getState()?.viewport_limits;
        const available = steps.filter(value => !limits || (
          stage.clientWidth / value >= limits.min_width && stage.clientWidth / value <= limits.max_width
          && stage.clientHeight / value >= limits.min_height && stage.clientHeight / value <= limits.max_height));
        const next = direction === 0 ? 1 : direction > 0
          ? available.find(value => value > current + .001) || current
          : available.findLast(value => value < current - .001) || current;
        zooms.set(tab, next);
        zoomChanged(next);
        measure();
      },
      frame() { fit(); },
      state() {
        const tab = getState()?.active_tab;
        if (tab !== activeTab) { activeTab = tab; applied = undefined; }
        for (const tab of zooms.keys()) if (!getState()?.tabs.some(item => item.id === tab)) zooms.delete(tab);
        zoomChanged(zoom());
        measure();
      },
      connected(value) { connected = value; if (value) { applied = undefined; measure(); } },
    };
  };
})();
