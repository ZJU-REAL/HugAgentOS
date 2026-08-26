/**
 * Generate a RFC 4122 v4 UUID, compatible with non-secure contexts (HTTP / non-localhost).
 *
 * `crypto.randomUUID` is only exposed in a secure context (HTTPS or localhost); when accessing a
 * deployed environment via http://intranet-IP it is undefined, so calling it directly throws a
 * TypeError and takes down the caller. `crypto.getRandomValues` carries no such restriction, so we
 * prefer the native generator and otherwise assemble the same v4 layout from CSPRNG bytes,
 * covering all HTTP/HTTPS environments.
 */
export function newOperationId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    crypto.getRandomValues(bytes);
  } else {
    // Last resort for environments without any Web Crypto surface. The value only has to be
    // unique per request — the backend treats it as an idempotency key, not as a secret.
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10xx
  const hex: string[] = [];
  for (let i = 0; i < bytes.length; i += 1) hex.push(bytes[i].toString(16).padStart(2, '0'));
  return `${hex.slice(0, 4).join('')}-${hex.slice(4, 6).join('')}-${hex.slice(6, 8).join('')}-${hex.slice(8, 10).join('')}-${hex.slice(10, 16).join('')}`;
}
