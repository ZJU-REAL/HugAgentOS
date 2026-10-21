/* Adapter for MIT chrome-tabs markup. Browser state owns the tab lifecycle. */
(() => {
  window.installBrowserTabs = (element, command) => {
    const content = element.querySelector('.chrome-tabs-content');
    const template = document.getElementById('chrome-tab-template');
    const nodes = new Map();
    let activeTab;
    document.getElementById('tab-add').onclick = () => { void window.BrowserChannel.newTab().catch(() => {}); };
    return state => {
      document.getElementById('tab-add').disabled = state.closed;
      const present = new Set(state.tabs.map(tab => tab.id));
      for (const [id, node] of nodes) if (!present.has(id)) { node.remove(); nodes.delete(id); }
      for (const tab of state.tabs) {
        let node = nodes.get(tab.id);
        if (!node) {
          node = template.content.firstElementChild.cloneNode(true);
          node.setAttribute('role', 'tab');
          node.querySelector('.chrome-tab-favicon').hidden = true;
          const select = () => { void command('select_tab', {tab_id: tab.id}).catch(() => {}); };
          node.onclick = select;
          node.querySelector('.chrome-tab-close').onclick = event => {
            event.stopPropagation();
            void command('close_tab', {tab_id: tab.id}).catch(() => {});
          };
          node.onkeydown = event => {
            if (event.target === node && ['Enter', ' '].includes(event.key)) { event.preventDefault(); select(); }
          };
          nodes.set(tab.id, node);
          content.append(node);
        }
        const active = tab.id === state.active_tab;
        node.toggleAttribute('active', active);
        node.setAttribute('aria-selected', String(active));
        node.tabIndex = active ? 0 : -1;
        node.title = tab.url;
        node.querySelector('.chrome-tab-title').textContent = tab.title || (tab.url === 'about:blank' ? '' : tab.url) || window.BrowserText.text('新标签页');
      }
      if (state.active_tab !== activeTab) {
        activeTab = state.active_tab;
        nodes.get(activeTab)?.scrollIntoView({block: 'nearest', inline: 'nearest'});
      }
    };
  };
})();
