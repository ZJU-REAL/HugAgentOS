/** Host requests are bounded, acknowledged and discarded when their frame leaves. */
export function navigationRequests(post: (message: unknown) => void) {
  let pending: { id: string; resolve: () => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> } | null = null;
  const cancel = (reason: string) => {
    if (!pending) return;
    clearTimeout(pending.timer);
    pending.reject(new Error(reason));
    pending = null;
  };
  return {
    navigate(url: string): Promise<void> {
      if (pending) return Promise.reject(new Error('navigation_in_progress'));
      return new Promise((resolve, reject) => {
        const id = crypto.randomUUID();
        const timer = setTimeout(() => cancel('navigation_timeout_result_unknown'), 40_000);
        pending = { id, resolve, reject, timer };
        post({ type: 'host:navigate', id, url });
      });
    },
    receive(message: Record<string, unknown>) {
      if (!pending || message.id !== pending.id) return;
      clearTimeout(pending.timer);
      const call = pending;
      pending = null;
      if (message.ok === true) call.resolve();
      else call.reject(new Error(typeof message.error === 'string' ? message.error : 'navigation_failed'));
    },
    dispose() { cancel('module_unmounted'); },
  };
}
