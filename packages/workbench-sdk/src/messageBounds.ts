/** JSON-only messages: validate before serialization or structured cloning. */
export const MAX_MESSAGE_BYTES = 256 * 1024;
const MAX_NODES = 4096;
const MAX_DEPTH = 16;
export function validateMessage(value: unknown): void {
 let nodes = 0, bytes = 0;
 const ancestors = new Set<object>();
 function charge(size: number) {
  bytes += size;
  if (bytes > MAX_MESSAGE_BYTES) throw new Error('message_too_large');
 }
 function string(value: string) {
  if (value.length > MAX_MESSAGE_BYTES) throw new Error('message_too_large');
  charge(2);
  for (let i = 0; i < value.length; i++) {
   const c = value.charCodeAt(i);
   if (c < 32 || c === 34 || c === 92) charge(c < 32 ? 6 : 2);
   else if (c < 128) charge(1);
   else if (c < 2048) charge(2);
   else if (c >= 0xd800 && c <= 0xdbff && value.charCodeAt(i + 1) >= 0xdc00 && value.charCodeAt(i + 1) <= 0xdfff) {charge(4); i++;}
   else charge(c >= 0xd800 && c <= 0xdfff ? 6 : 3);
  }
 }
 function visit(value: unknown, depth: number) {
  if (++nodes > MAX_NODES || depth > MAX_DEPTH) throw new Error('invalid_message');
  if (value === null) {charge(4); return;}
  if (typeof value === 'string') {string(value); return;}
  if (typeof value === 'boolean') {charge(5); return;}
  if (typeof value === 'number' && Number.isFinite(value)) {charge(24); return;}
  if (typeof value !== 'object') throw new Error('invalid_message');
  if (ancestors.has(value)) throw new Error('invalid_message');
  const array = Array.isArray(value);
  const prototype = Object.getPrototypeOf(value);
  if (array ? prototype !== Array.prototype : prototype !== Object.prototype && prototype !== null) throw new Error('invalid_message');
  if (array && value.length > MAX_NODES) throw new Error('invalid_message');
  ancestors.add(value);
  charge(2);
  const keys = Reflect.ownKeys(value);
  if (keys.length > MAX_NODES + (array ? 1 : 0)) throw new Error('invalid_message');
  let count = 0;
  for (const key of keys) {
   if (array && key === 'length') continue;
   if (typeof key !== 'string' || (array && key !== String(count))) throw new Error('invalid_message');
   const descriptor = Object.getOwnPropertyDescriptor(value, key);
   if (!descriptor || !descriptor.enumerable || !('value' in descriptor)) throw new Error('invalid_message');
   if (!array) {string(key); charge(1);}
   charge(1); visit(descriptor.value, depth + 1); count++;
  }
  if (array && count !== value.length) throw new Error('invalid_message');
  ancestors.delete(value);
 }
 visit(value, 0);
}
