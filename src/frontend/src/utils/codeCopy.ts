import { t } from '../i18n';
import { copyToClipboard } from './clipboard';

let handlerInstalled = false;
const copying = new WeakSet<HTMLButtonElement>();
const resetTimers = new WeakMap<HTMLButtonElement, ReturnType<typeof setTimeout>>();

function showCopyResult(button: HTMLButtonElement, ok: boolean): void {
  const previous = resetTimers.get(button);
  if (previous) clearTimeout(previous);
  button.dataset.copyState = ok ? 'done' : 'fail';
  resetTimers.set(button, setTimeout(() => {
    delete button.dataset.copyState;
    resetTimers.delete(button);
  }, 2000));
}

/** Native event delegation also covers code blocks appended during streaming. */
export function wrapCodeWithCopy(codeHtml: string): string {
  if (!handlerInstalled && typeof document !== 'undefined') {
    handlerInstalled = true;
    document.addEventListener('click', (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const button = target.closest<HTMLButtonElement>('.jx-mdCode-copy');
      const code = button?.closest('.jx-mdCode')?.querySelector('pre > code');
      if (!button || !code) return;
      event.preventDefault();
      event.stopPropagation();
      if (copying.has(button)) return;
      copying.add(button);
      // Read at click time so streaming updates cannot leave a stale copy payload.
      void copyToClipboard(code.textContent ?? '').then((ok) => {
        showCopyResult(button, ok);
      }).finally(() => copying.delete(button));
    });
  }
  return '<div class="jx-mdCode"><div class="jx-mdCode-toolbar">'
    + '<button type="button" class="jx-mdCode-copy" aria-live="polite">'
    + '<span class="jx-mdCode-copy-default"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">'
    + '<rect x="9" y="9" width="12" height="12" rx="2"/>'
    + '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
    + t('复制') + '</span>'
    + '<span class="jx-mdCode-copy-done"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">'
    + '<path d="M20 6 9 17l-5-5"/></svg>' + t('已复制') + '</span>'
    + '<span class="jx-mdCode-copy-fail">' + t('复制失败') + '</span>'
    + '</button></div>' + codeHtml + '</div>';
}
