import type { KeyboardEvent } from 'react';
export function navigationKeyboard(event: KeyboardEvent<HTMLElement>) {
  if (event.altKey || event.ctrlKey || event.metaKey || !['ArrowUp','ArrowDown','Home','End'].includes(event.key)) return;
  const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
  if (!buttons.length) return;
  const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
  if (index < 0) return;
  event.preventDefault();
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 :
    event.key === 'ArrowDown' ? (index + 1) % buttons.length : (index + buttons.length - 1) % buttons.length;
  buttons[next].focus();
}
