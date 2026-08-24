/**
 * 正文表格复制：给 mdToHtml 渲染出的每个 <table> 包一层容器并注入右上角复制按钮，
 * 点击后把表格以富文本（text/html，内联边框样式）+ 纯文本（TSV）双格式写入剪贴板，
 * 粘贴到 Word / WPS / 邮件里仍是带边框的真表格，粘到纯文本编辑器里退化为制表符分隔。
 *
 * 按钮是 marked postprocess 阶段注入的原生 DOM（正文经 dangerouslySetInnerHTML 落地，
 * React 事件挂不上去），所以点击走 document 级事件委托，一次安装、全站生效。
 */
import { t } from '../i18n';
import { copyHtmlToClipboard } from './clipboard';

const COPY_ICON =
  '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  + '<rect x="9" y="9" width="12" height="12" rx="2"/>'
  + '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'
  + '</svg>';

const DONE_ICON =
  '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  + '<path d="M20 6 9 17l-5-5"/>'
  + '</svg>';

const TABLE_OPEN_RE = /<table(\s[^>]*)?>/g;

let handlerInstalled = false;

/**
 * marked postprocess 钩子：把 HTML 里的每个表格包成
 * `<div class="jx-mdTable"><button class="jx-mdTable-copy">…</button><table>…</table></div>`。
 * 代码块里的 <table> 已被 marked 转义成 &lt;table&gt;，不会误命中。
 */
export function wrapTablesWithCopy(html: string): string {
  if (!html.includes('<table')) return html;
  ensureCopyHandler();
  const label = t('复制表格');
  return html
    .replace(
      TABLE_OPEN_RE,
      (m) =>
        `<div class="jx-mdTable"><button type="button" class="jx-mdTable-copy" title="${label}" aria-label="${label}">${COPY_ICON}</button>${m}`,
    )
    .replace(/<\/table>/g, '</table></div>');
}

function ensureCopyHandler(): void {
  if (handlerInstalled || typeof document === 'undefined') return;
  handlerInstalled = true;
  document.addEventListener('click', (e) => {
    const btn = (e.target as HTMLElement | null)?.closest?.('.jx-mdTable-copy') as HTMLButtonElement | null;
    if (!btn) return;
    const table = btn.closest('.jx-mdTable')?.querySelector('table');
    if (!table) return;
    e.preventDefault();
    void copyTable(table, btn);
  });
}

async function copyTable(table: HTMLTableElement, btn: HTMLButtonElement): Promise<void> {
  if (btn.dataset.copying) return;
  btn.dataset.copying = '1';
  try {
    const ok = await copyHtmlToClipboard(buildRichTableHtml(table), buildPlainTable(table));
    flashButton(btn, ok);
  } finally {
    delete btn.dataset.copying;
  }
}

/* dark-ok-begin: 下面拼的是写进剪贴板的导出表格（Word 粘贴用），脱离应用主题独立存在，
   一律白纸黑字内联样式，不参与深色令牌化。 */
function buildRichTableHtml(table: HTMLTableElement): string {
  const clone = table.cloneNode(true) as HTMLTableElement;
  // 剥掉引用锚点的角标与 React 引用占位（复制进文档没有悬浮卡语境，只留正文文字）
  clone.querySelectorAll('[data-jxcit], sup.jx-citeRef-idx, .jx-mdTable-copy').forEach((el) => el.remove());
  clone.removeAttribute('class');
  clone.setAttribute('style', 'border-collapse:collapse;font-family:inherit;');
  clone.querySelectorAll('th, td').forEach((cell) => {
    const align = cell.getAttribute('align') || 'left';
    const base = `border:1px solid #b0b0b0;padding:4pt 8pt;text-align:${align};font-size:10.5pt;color:#1a1a1a;`;
    cell.setAttribute(
      'style',
      cell.tagName === 'TH' ? `${base}background:#f2f2f2;font-weight:700;` : base,
    );
  });
  return clone.outerHTML;
}
/* dark-ok-end */

function buildPlainTable(table: HTMLTableElement): string {
  return Array.from(table.querySelectorAll('tr'))
    .map((tr) =>
      Array.from(tr.querySelectorAll('th, td'))
        .map((cell) => ((cell as HTMLElement).innerText || '').replace(/\s+/g, ' ').trim())
        .join('\t'),
    )
    .join('\n');
}

function flashButton(btn: HTMLButtonElement, ok: boolean): void {
  const original = btn.innerHTML;
  const originalTitle = btn.title;
  btn.classList.add(ok ? 'jx-mdTable-copy--done' : 'jx-mdTable-copy--fail');
  btn.innerHTML = ok ? DONE_ICON : original;
  btn.title = ok ? t('已复制') : t('复制失败');
  window.setTimeout(() => {
    btn.classList.remove('jx-mdTable-copy--done', 'jx-mdTable-copy--fail');
    btn.innerHTML = original;
    btn.title = originalTitle;
  }, 1500);
}
