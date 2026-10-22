/* URL-like input navigates; other text uses the configured default search provider. */
(() => {
  window.browserAddress = value => {
    const input = value.trim();
    if (!input) return null;
    const search = () => 'https://www.bing.com/search?q=' + encodeURIComponent(input);
    if (/^(?:javascript|data|file|vbscript|blob|about|chrome|edge|ftp|mailto):/i.test(input)
        || (/^[a-z][a-z0-9+.-]*:\/\//i.test(input) && !/^https?:\/\//i.test(input))) {
      throw new Error('请输入有效的网页地址');
    }
    // Reject userinfo before parsing: malformed URLs must not send credentials to search.
    if (/^https?:\/\/[^/?#]*@/i.test(input)) {
      throw new Error('请输入有效的网页地址');
    }
    if (/^(?:site|inurl|intitle|filetype|inbody|ext|contains|language|loc):/i.test(input)) return search();
    if (/^[^/?#\s]+:[^/?#\s]*@/.test(input)) throw new Error('请输入有效的网页地址');
    const explicit = /^https?:\/\//i.test(input);
    let url;
    try { url = new URL(explicit ? input : 'https://' + input); }
    catch { return search(); }
    if (url.username || url.password) {
      if (explicit || /^[^\s@]+:[^\s@]*@/.test(input)) throw new Error('请输入有效的网页地址');
      return search();
    }
    if (explicit) return url.href;
    if (/\s/.test(input) || /^https?:/i.test(input)) return search();
    const host = url.hostname;
    const labels = host.split('.');
    const domain = labels.length > 1 && labels.every(label => /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/i.test(label));
    const local = host === 'localhost' || /^\[[0-9a-f:]+\]$/i.test(host);
    const port = /^[a-z0-9.-]+:\d+(?:[/?#]|$)/i.test(input);
    const intranet = /^[a-z0-9-]+\//i.test(input);
    return domain || local || port || intranet ? url.href : search();
  };
})();
