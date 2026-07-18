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
    span.style.position = 'fixed';
    span.style.top = '0';
    span.style.left = '0';
    span.style.opacity = '0';
    document.body.appendChild(span);

    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(span);
    selection?.removeAllRanges();
    selection?.addRange(range);

    let ok = false;
    try {
      ok = document.execCommand('copy');
    } finally {
      selection?.removeAllRanges();
      document.body.removeChild(span);
    }
    return ok;
  } catch {
    return false;
  }
}
