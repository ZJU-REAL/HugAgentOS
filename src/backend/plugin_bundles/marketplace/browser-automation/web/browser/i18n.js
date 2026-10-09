/* Package-owned translations follow the host locale. */
(() => {
  let english = false;
  const translations = {
    '画面正在更新，本次输入未发送，请等待后重试。': 'Frame updating. Input was not sent; wait and retry.',
    '域名解析失败，请检查 DNS 配置。': 'DNS resolution failed. Check your DNS configuration.',
    '清空下载列表': 'Clear downloads', '交还控制权': 'Release control',
    '缩小网页': 'Zoom out', '放大网页': 'Zoom in', '重置缩放': 'Reset zoom',
    '连接中断。请检查刚才的操作结果。': 'Connection interrupted. Check the result of your last action.',
    '操作已取消，请重新操作。': 'Action cancelled. Please try again.',
    '操作过于频繁，请稍后重试。': 'Too many actions. Please try again shortly.',
    '浏览器正在重连，请稍后操作。': 'Reconnecting. Please wait.',
    '网页尺寸已调整，请重新操作。': 'Page resized. Please try again.',
    '控制权已更新，请重新操作。': 'Control changed. Please try again.',
    '浏览器正在由另一个窗口操作。': 'Another window is controlling the browser.',

    '请输入有效的网页地址': 'Enter a valid web address',
    '输入网址或搜索内容，按 Enter 打开': 'Enter an address or search, then press Enter',
    '搜索 Bing 或输入网页地址': 'Search Bing or enter a web address',
    '新标签页': 'New tab', '新建标签页': 'New tab', '打开网页': 'Open web page',
    '已连接': 'Connected', '正在加载网页…': 'Loading page…', '点击＋打开标签页': 'Click + to open a tab',
    '浏览器': 'Browser', '连接中': 'Connecting', '正在连接浏览器': 'Connecting to browser',
    '浏览器控制': 'Browser controls', '后退': 'Back', '前进': 'Forward', '刷新': 'Reload',
    '网页地址': 'Web address', '输入网页地址': 'Enter a web address', '打开': 'Go',
    '关闭浏览器': 'Close browser', '关闭标签页': 'Close tab', '浏览器标签页': 'Browser tabs',
    '浏览器页面': 'Browser page', '浏览器输入': 'Browser input',
    '正在连接浏览器…': 'Connecting to browser…', '弹窗输入': 'Dialog input',
    '确认': 'Confirm', '取消': 'Cancel', '浏览器已关闭': 'Browser closed',
    '连接中断': 'Disconnected', '连接中断，正在重连…': 'Disconnected; reconnecting…',
    '选择上传文件': 'Choose upload files', '下载': 'Download',
    '上传文件总大小不能超过 8 MiB': 'Total upload size must not exceed 8 MiB',
    '连接超时': 'Connection timed out', '浏览器面板已关闭': 'Browser panel closed',
    '浏览器尚未连接': 'Browser is not connected',
    '操作超时，结果未知，请先检查页面': 'Operation timed out; inspect the page before retrying',
  };
  const text = value => english ? translations[value] || value : value;
  window.BrowserText = { text };
  window.addEventListener('message', event => {
    if (event.source !== parent || event.data?.type !== 'host:init') return;
    english = !String(event.data.locale || 'zh-CN').startsWith('zh');
    document.documentElement.lang = english ? 'en' : 'zh-CN';
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (translations[node.textContent.trim()]) node.textContent = text(node.textContent.trim());
    }
    for (const element of document.querySelectorAll('[title],[aria-label],[placeholder]')) {
      for (const name of ['title', 'aria-label', 'placeholder']) {
        if (element.hasAttribute(name)) element.setAttribute(name, text(element.getAttribute(name)));
      }
    }
  });
})();
