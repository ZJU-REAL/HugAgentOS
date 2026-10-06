/* Serialize human gestures behind automatic, connection-bound ownership. */
(() => {
  window.installBrowserControl = (channel, getState, render, report) => {
    let tail = Promise.resolve(), pending = 0, activity = 0, generation = 0, cancellation = 'pending_input_cancelled', timer;
    const holds = new Set();
    const owns = () => getState()?.controller === 'user' && getState()?.connection_id === channel.connection();
    const enqueue = task => {
      if (pending >= 64) return Promise.reject(new Error('too_many_pending_commands'));
      pending++;
      const result = tail.then(task);
      tail = result.catch(() => {});
      return result.finally(() => { pending--; });
    };
    const release = () => {
      clearTimeout(timer);
      const seen = activity;
      return enqueue(async () => {
        if (seen !== activity || holds.size || !owns()) return;
        const state = await channel.command('release_control');
        render(state);
      }).catch(report);
    };
    const arm = () => {
      clearTimeout(timer);
      timer = setTimeout(() => { if (holds.size) arm(); else void release(); }, 15000);
    };
    const command = (action, params = {}) => {
      activity++;
      clearTimeout(timer);
      const seen = generation;
      return enqueue(async () => {
        if (seen !== generation) throw new Error(cancellation);
        if (!owns()) render(await channel.command('take_control', {private: true}));
        return channel.command(action, params);
      }).then(result => { report(); return result; }).catch(error => {
        if (seen === generation) { cancellation = error.message; generation++; report(error); }
        throw error;
      }).finally(() => { if (document.hasFocus()) arm(); else void release(); });
    };
    window.addEventListener('blur', () => { setTimeout(() => { void release(); }, 0); });
    return {
      command,
      owns,
      busy() { return holds.size > 0; },
      hold(name, value) { value ? holds.add(name) : holds.delete(name); },
      disconnected() { cancellation = 'connection_lost_result_unknown'; generation++; clearTimeout(timer); holds.clear(); },
    };
  };
})();
