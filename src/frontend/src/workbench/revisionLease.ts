import {ApiResponseError} from '../utils/apiError';

export type LeaseStatus = 'creating' | 'ready' | 'recovering' | 'revoked' | 'expired' | 'closed';
export interface LeaseReceipt {instanceId: string; revision: string; expiresAt: number}
export interface LeaseClock {
 wall(): number; monotonic(): number;
 schedule(action: () => void, delay: number): ReturnType<typeof setTimeout>;
 cancel(timer: ReturnType<typeof setTimeout>): void;
}
const browserClock: LeaseClock = {
 wall: () => Date.now(), monotonic: () => performance.now(),
 schedule: (action, delay) => setTimeout(action, delay), cancel: timer => clearTimeout(timer),
};
interface Options {
 instanceId: string; revision: string;
 request(method: 'POST' | 'PUT', signal: AbortSignal): Promise<LeaseReceipt>;
 close(): Promise<unknown>;
 changed(status: LeaseStatus): void;
 clock?: LeaseClock;
}
function temporary(error: unknown) {
 if (error instanceof ApiResponseError) return [408,429,500,502,503,504].includes(error.status);
 if (error instanceof TypeError) return true;
 return error instanceof Error && 'code' in error && error.code === 'origin_unavailable';
}

/** One owner for expiry, retries, cancellation and terminal transitions. */
export class RevisionLease {
 private status: LeaseStatus = 'creating';
 private clock: LeaseClock;
 private expiresAt = 0;
 private deadline = 0;
 private created = false;
 private failures = 0;
 private pending?: AbortController;
 private renewal?: ReturnType<typeof setTimeout>;
 private watchdog?: ReturnType<typeof setTimeout>;
 private started = false;
 private options: Options;
 constructor(options: Options) { this.options=options; this.clock = options.clock ?? browserClock; }
 private terminal() { return ['revoked','expired','closed'].includes(this.status); }
 private publish(status: LeaseStatus) {
  this.status = status; this.options.changed(status);
 }
 private remaining() {
  return Math.min(this.expiresAt - this.clock.wall(), this.deadline - this.clock.monotonic());
 }
 canInvoke = () => {
  if (this.created && this.remaining() <= 0 && !this.terminal()) this.finish('expired');
  return this.status === 'ready';
 };
 start() {
  if (this.started) return;
  this.started = true;
  this.publish('creating');
  void this.exchange('POST');
 }
 private finish(status: 'revoked' | 'expired' | 'closed') {
  if (this.terminal()) return;
  if (this.renewal !== undefined) this.clock.cancel(this.renewal);
  if (this.watchdog !== undefined) this.clock.cancel(this.watchdog);
  this.pending?.abort();
  this.publish(status);
 }
 private watch = () => {
  if (this.terminal()) return;
  if (this.remaining() <= 0) { this.finish('expired'); return; }
  this.watchdog = this.clock.schedule(this.watch, Math.min(1000, this.remaining()));
 };
 private async exchange(method: 'POST' | 'PUT') {
  if (this.terminal() || this.pending) return;
  if (this.created && this.remaining() <= 0) { this.finish('expired'); return; }
  const controller = new AbortController();
  this.pending = controller;
  const started = this.clock.monotonic();
  let timedOut = false;
  let timeout: ReturnType<typeof setTimeout> | undefined;
  try {
   // Racing the timeout also bounds a transport which ignores AbortSignal.
   const receipt = await Promise.race([
    this.options.request(method, controller.signal),
    new Promise<never>((_, reject) => {
     timeout = this.clock.schedule(() => {
      timedOut = true; controller.abort(); reject(new TypeError('lease_request_timeout'));
     }, 10000);
    }),
   ]);
   if (this.terminal()) {
    if (method === 'POST') void this.options.close().catch(() => undefined);
    return;
   }
   if (this.created && this.remaining() <= 0) { this.finish('expired'); return; }
   if (receipt.instanceId !== this.options.instanceId || receipt.revision !== this.options.revision
       || !Number.isFinite(receipt.expiresAt)) { this.finish('revoked'); return; }
   this.created = true;
   this.expiresAt = receipt.expiresAt * 1000;
   this.deadline = Math.min(started + 90000,
    this.clock.monotonic() + this.expiresAt - this.clock.wall());
   if (this.remaining() <= 0) { this.finish('expired'); return; }
   this.failures = 0;
   this.publish('ready');
   if (this.watchdog !== undefined) this.clock.cancel(this.watchdog);
   this.watch();
   this.renewal = this.clock.schedule(() => void this.exchange('PUT'),
    Math.min(30000, this.remaining() / 2));
  } catch (error) {
   if (this.terminal()) return;
   if (!this.created || (!timedOut && !temporary(error))) { this.finish('revoked'); return; }
   if (this.remaining() <= 0) { this.finish('expired'); return; }
   this.publish('recovering');
   const delay = Math.min(2000 * 2 ** Math.min(this.failures++, 3), 10000, this.remaining());
   this.renewal = this.clock.schedule(() => void this.exchange('PUT'), delay);
  } finally {
   if (timeout !== undefined) this.clock.cancel(timeout);
   if (this.pending === controller) this.pending = undefined;
  }
 }
 check = () => { this.canInvoke(); };
 dispose() {
  const close = this.created;
  this.finish('closed');
  // Server expiry remains the cleanup boundary if this best-effort close fails.
  if (close) void this.options.close().catch(() => undefined);
 }
}
