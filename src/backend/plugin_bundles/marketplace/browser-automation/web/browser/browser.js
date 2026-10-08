/* Browser presentation lives entirely inside the plugin. */
(() => {
  const t = value => window.BrowserText.text(value);
  const channel = window.BrowserChannel;
  const $ = id => document.getElementById(id);
  const screen = $('screen');
  let state, connected = false, navigating = false;
  const report = error => {
    const code = error?.message || (error ? String(error) : '');
    const text = {
      connection_lost_result_unknown: '连接中断。请检查刚才的操作结果。',
      pending_input_cancelled: '操作已取消，请重新操作。',
      too_many_pending_commands: '操作过于频繁，请稍后重试。',
      resource_disconnected: '浏览器正在重连，请稍后操作。',
      stale_viewport: '网页尺寸已调整，请重新操作。',
      stale_control_epoch: '控制权已更新，请重新操作。',
      control_already_owned: '浏览器正在由另一个窗口操作。',
    }[code] || code;
    $('notice').textContent = t(text);
  };
  let control;
  const command = (action, params = {}) => control.command(action, params);
  const act = (action, params = {}) => { void command(action, params).catch(() => {}); };
  const overlay = text => { $('overlay').hidden = !text; $('overlay').textContent = text || ''; };
  const updateOverlay = () => {
    const active = state?.tabs.find(tab => tab.id === state.active_tab);
    if (state?.closed) overlay(t('浏览器已关闭'));
    else if (navigating) overlay(t('正在加载网页…'));
    else if (active?.url === 'about:blank') overlay(t('输入网址或搜索内容，按 Enter 打开'));
    else if (!state?.active_tab) overlay(t('点击＋打开标签页'));
    else if (screen.dataset.tab !== state.active_tab || screen.dataset.revision !== String(state.viewport_revision)) overlay(t('正在加载网页…'));
    else overlay();
  };
  const navigateTo = async (value, newTab = false) => {
    const input = value.trim();
    if (!input) return;
    try {
      const url = window.browserAddress(input);
      navigating = true;
      updateOverlay();
      const active = state?.tabs.find(tab => tab.id === state.active_tab);
      const action = !active || (newTab && active.url !== 'about:blank') ? 'new_tab' : 'navigate';
      return await command(action, {url});
    } catch (error) { report(error); throw error; }
    finally { navigating = false; updateOverlay(); }
  };
  const render = next => {
    const tabChanged = next.active_tab !== state?.active_tab;
    const layoutChanged = tabChanged || next.viewport_revision !== state?.viewport_revision;
    state = next;
    if (layoutChanged) { frames.invalidate(); overlay(t('正在加载网页…')); }
    channel.epoch(state.epoch);
    const active = state.tabs.find(tab => tab.id === state.active_tab);
    if (tabChanged || document.activeElement !== $('address')) $('address').value = active?.url === 'about:blank' ? '' : active?.url || '';
    if (tabChanged && active?.url === 'about:blank') $('address').focus();
    renderTabs(state);
    view.state();
    $('files').replaceChildren();
    for (const download of state.downloads) {
      const button = document.createElement('button');
      button.textContent = t('下载') + ' ' + download.name;
      button.onclick = () => channel.download(download.id);
      $('files').append(button);
    }
    if (state.filechoosers.length) {
      const button = document.createElement('button');
      button.textContent = t('选择上传文件');
      button.onclick = () => $('upload').click();
      $('files').append(button);
    }
    const dialog = state.dialogs.find(item => item.tab_id === state.active_tab);
    $('dialog').hidden = !dialog;
    if (dialog) {
      $('dialog-message').textContent = dialog.message;
      $('dialog-text').hidden = dialog.kind !== 'prompt';
    }
    updateOverlay();
  };
  control = window.installBrowserControl(channel, () => state, render, report);
  const view = window.installBrowserViewport($('stage'), screen, channel, () => state, report, control.busy, value => { $('zoom-reset').textContent = Math.round(value * 100) + '%'; });
  $('zoom-out').onclick = () => view.zoom(-1);
  $('zoom-in').onclick = () => view.zoom(1);
  $('zoom-reset').onclick = () => view.zoom(0);
  const zoomKeys = new Set();
  window.addEventListener('keydown', event => {
    if (event.isComposing || !(event.ctrlKey || event.metaKey) || event.altKey) return;
    const direction = ['+', '='].includes(event.key) ? 1 : event.key === '-' ? -1 : event.key === '0' ? 0 : null;
    if (direction === null) return;
    event.preventDefault(); event.stopImmediatePropagation();
    zoomKeys.add(event.code);
    view.zoom(direction);
  }, true);
  window.addEventListener('keyup', event => {
    if (!zoomKeys.delete(event.code)) return;
    event.preventDefault(); event.stopImmediatePropagation();
  }, true);
  const renderTabs = window.installBrowserTabs($('tabs'), command);
  const frames = window.installBrowserFrames(screen, () => state, () => { view.frame(); updateOverlay(); }, report);
  channel.listen(async (event, data) => {
    if (event.type === 'navigate') {
      try { await navigateTo(event.url, true); channel.navigationResult(event.id); }
      catch (error) { channel.navigationResult(event.id, error); }
    } else if (event.type === 'attached') {
      connected = true;
      $('connection').title = t('已连接');
      $('connection').dataset.connected = 'true';
      view.connected(true);
      report();
      updateOverlay();
    } else if (event.type === 'state') render(event);
    else if (event.type === 'frame') {
      frames.push(event, data);
    } else if (event.type === 'dialog') {
      $('dialog').hidden = false;
      $('dialog-message').textContent = event.message;
      $('dialog-text').hidden = event.kind !== 'prompt';
    } else if (event.type === 'connection') {
      connected = false;
      $('connection').title = t('连接中断');
      $('connection').dataset.connected = 'false';
      frames.invalidate();
      control.disconnected();
      view.connected(false);
      overlay(event.error || t('连接中断，正在重连…'));
    } else if (event.type === 'stream_error') report(new Error(event.error));
    else if (event.type === 'closed') { frames.invalidate(); overlay(t('浏览器已关闭')); }
  });
  window.addEventListener('pagehide', () => frames.invalidate());
  const navigate = () => { void navigateTo($('address').value).catch(() => {}); };
  $('navigate-open').onclick = navigate;
  $('address').onkeydown = event => {
    if (!event.isComposing && event.key === 'Enter') { event.preventDefault(); navigate(); }
  };
  for (const button of document.querySelectorAll('[data-action]')) button.onclick = () => act(button.dataset.action);
  for (const [id, accept] of [['dialog-accept', true], ['dialog-dismiss', false]]) {
    $(id).onclick = async () => {
      try { await command('dialog', { accept, text: $('dialog-text').value }); $('dialog').hidden = true; }
      catch { /* keep the dialog available for retry */ }
    };
  }
  $('upload').onchange = async () => {
    try {
      const files = [];
      let size = 0;
      for (const file of $('upload').files) {
        size += file.size;
        if (size > 8 * 1024 * 1024) throw new Error(t('上传文件总大小不能超过 8 MiB'));
        const bytes = new Uint8Array(await file.arrayBuffer());
        let text = '';
        for (let i = 0; i < bytes.length; i += 8192) text += String.fromCharCode(...bytes.subarray(i, i + 8192));
        files.push({ name: file.name, mime_type: file.type, data: btoa(text) });
      }
      await command('upload', { files });
    } catch (error) { report(error); }
    finally { $('upload').value = ''; }
  };
  window.installBrowserInput(screen, $('keyboard'), () => state, control);
  setInterval(() => { if (connected) void channel.command('heartbeat').catch(() => {}); }, 15000);
})();
