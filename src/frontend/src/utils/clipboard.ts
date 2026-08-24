/**
 * Copy text to the clipboard, compatible with non-secure contexts (HTTP / non-localhost).
 *
 * `navigator.clipboard` is only available in a secure context (HTTPS or localhost); when
 * accessing a deployed environment via http://intranet-IP it is undefined or throws on call
 * (surfacing as "copy failed"). Here we prefer the modern API, and when it is unavailable fall
 * back to `document.execCommand('copy')`, covering all HTTP/HTTPS environments.
 *
 * @returns whether the copy succeeded
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  // Preferred: the modern Clipboard API under a secure context
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // Fall through to the execCommand fallback below
    }
  }
  // Fallback: execCommand (HTTP / older browsers)
  //
  // Use a span + Range selection rather than `textarea.select()`: the latter moves focus to the
  // textarea, and inside an antd Modal (rc-dialog focus trap) it gets synchronously snatched back
  // to the dialog, clearing the selection before execCommand runs and failing the copy.
  // A Range selection only changes the Selection, not document.activeElement, so the focus trap is
  // not triggered and copying works even inside the dialog.
  try {
    const span = document.createElement('span');
    span.textContent = text;
    span.style.whiteSpace = 'pre';
    return execCommandCopyNode(span);
  } catch {
    return false;
  }
}

/**
 * Copy rich HTML to the clipboard (with a plain-text fallback representation), so pasting
 * into Word / WPS / mail clients keeps the original structure (e.g. a real table), while
 * plain-text editors receive `plainText`.
 *
 * Modern path: `ClipboardItem` with both `text/html` and `text/plain` flavors (secure
 * contexts only). Fallback for HTTP intranet deployments: render the HTML into a hidden
 * node, select it with a Range and `execCommand('copy')` — the browser serializes the
 * selection as rich text, which also pastes into Word as a table.
 *
 * @returns whether the copy succeeded
 */
export async function copyHtmlToClipboard(html: string, plainText: string): Promise<boolean> {
  if (window.isSecureContext && navigator.clipboard && typeof ClipboardItem !== 'undefined') {
    try {
      await navigator.clipboard.write([
        new ClipboardItem({
          'text/html': new Blob([html], { type: 'text/html' }),
          'text/plain': new Blob([plainText], { type: 'text/plain' }),
        }),
      ]);
      return true;
    } catch {
      // Fall through to the execCommand fallback below
    }
  }
  try {
    const host = document.createElement('div');
    host.innerHTML = html;
    return execCommandCopyNode(host);
  } catch {
    return false;
  }
}

/** Mount `node` off-screen, select its contents with a Range and run execCommand('copy'). */
function execCommandCopyNode(node: HTMLElement): boolean {
  node.style.position = 'fixed';
  node.style.top = '0';
  node.style.left = '0';
  node.style.opacity = '0';
  node.style.pointerEvents = 'none';
  document.body.appendChild(node);

  const selection = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(node);
  selection?.removeAllRanges();
  selection?.addRange(range);

  let ok = false;
  try {
    ok = document.execCommand('copy');
  } finally {
    selection?.removeAllRanges();
    document.body.removeChild(node);
  }
  return ok;
}
