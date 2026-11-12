/**
 * Input methods (pinyin, kana…) and the Enter key — decided once, for the whole app.
 *
 * Enter that confirms an IME candidate must never send, submit or save. Chrome marks that
 * keydown with `isComposing`, but WebKit — Safari and the desktop app's WKWebView — ends the
 * composition *before* the confirming keydown, so `isComposing` is already false there. That
 * keydown still reports keyCode 229, and it arrives right after `compositionend`.
 *
 * `installImeEnterGuard()` (called once in main.tsx, before anything renders) stops such an
 * Enter in the capture phase at the window, so React handlers, antd `onPressEnter`, implicit
 * form submission and editor keymaps never see it. Components therefore handle a plain Enter
 * and must not test `isComposing` / keyCode 229 themselves (enforced by scripts/check-ime-enter.mjs).
 * Editors that also need to leave other keys (Tab, arrows) to the IME use `isImeKey`.
 */

// The confirming keydown follows compositionend at once; a person pressing Enter on purpose takes far longer.
const AFTER_COMPOSITION_MS = 50;
let lastCompositionEnd = -Infinity;
let installed = false;

/** True when this key belongs to an input method rather than to the page. */
export function isImeKey(event: KeyboardEvent | { nativeEvent: KeyboardEvent }): boolean {
  const native = 'nativeEvent' in event ? event.nativeEvent : event;
  return native.isComposing || native.keyCode === 229 || native.timeStamp - lastCompositionEnd < AFTER_COMPOSITION_MS;
}

function isEnter(event: KeyboardEvent): boolean {
  // Windows Chrome reports key "Process" for an IME-owned Enter; the physical code still says Enter.
  return event.key === 'Enter' || event.code === 'Enter' || event.code === 'NumpadEnter';
}

export function installImeEnterGuard(target: Window = window): void {
  if (installed) return;
  installed = true;
  target.addEventListener('compositionend', event => { lastCompositionEnd = event.timeStamp; }, true);
  target.addEventListener('keydown', event => {
    if (!isEnter(event) || !isImeKey(event)) return;
    // The IME has already taken the key: no newline, no implicit form submission, no app handler.
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
}
