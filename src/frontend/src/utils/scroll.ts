// When the distance from the bottom > this threshold, treat it as the user actively scrolling up, and streaming messages no longer force the page back to the bottom
export const SCROLL_FOLLOW_THRESHOLD = 100;
// Display threshold for the "back to bottom" button; smaller than SCROLL_FOLLOW_THRESHOLD to ensure the user sees the button as soon as they scroll up slightly
export const SCROLL_TO_BOTTOM_BTN_THRESHOLD = 80;

export function distanceFromBottom(el: HTMLElement): number {
  return el.scrollHeight - el.scrollTop - el.clientHeight;
}

// MQL is a live object (automatically reflects system-setting changes); module-level caching avoids rebuilding it per delta during streaming follow-scroll
const reducedMotionMql = window.matchMedia('(prefers-reduced-motion: reduce)');

export function scrollElementToBottom(el: HTMLElement, smooth = false): void {
  if (smooth && !reducedMotionMql.matches) {
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  } else {
    el.scrollTop = el.scrollHeight;
  }
}
