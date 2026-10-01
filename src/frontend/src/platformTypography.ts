/**
 * Select once before mounting any app entry (including admin/share/desktop).
 * Font availability remains the browser's job; OS detection only sets priority.
 */
export function fontPlatform(platform: string, userAgent: string): 'apple' | 'windows' | 'other' {
  // iPad desktop mode reports MacIntel; it should use the Apple stack too.
  if (/mac|iphone|ipad|ipod/i.test(platform)) return 'apple';
  if (/win/i.test(platform)) return 'windows';
  if (/iphone|ipad|ipod|macintosh/i.test(userAgent)) return 'apple';
  if (/windows/i.test(userAgent)) return 'windows';
  return 'other';
}

// Shared style entry runs this for both the full application and CE.
document.documentElement.dataset.fontPlatform = fontPlatform(navigator.platform, navigator.userAgent);
